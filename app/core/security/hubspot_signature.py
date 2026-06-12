# app/core/security/hubspot_signature.py  # noqa: D100
from __future__ import annotations

# ruff: noqa: E501
import hashlib
import hmac
import time

from fastapi import Header, HTTPException, Request

from app.core.config import settings
from app.core.logging import get_logger

# HubSpot signature version header constants
_SIG_V1 = "v1"
_SIG_V2 = "v2"
_SIG_V3 = "v3"

logger = get_logger("hubspot.security")

_MAX_TIMESTAMP_AGE_SECONDS = 300  # 5-minute replay window (mirrors Slack)


async def verify_hubspot_signature(
    request: Request,
    x_hubspot_signature: str | None = Header(None),
    x_hubspot_signature_v3: str | None = Header(None),
    x_hubspot_request_timestamp: str | None = Header(None),
    x_hubspot_signature_version: str = Header(_SIG_V1),
) -> None:
    """Verify the HubSpot webhook / API-extension signature.

    Guards:
    - Aborts with HTTP 500 in production when the secret is not configured.
    - Skips verification in non-production environments when the secret is absent.
    - Validates v1 / v2 / v3 HMAC signatures per the HubSpot spec.
    - Enforces a 5-minute replay window for all signature versions.
    """
    # --- Secret availability guard ---
    if not settings.HUBSPOT_CLIENT_SECRET.get_secret_value():
        if settings.is_prod:
            logger.critical(
                "HUBSPOT_CLIENT_SECRET missing in PRODUCTION! Refusing webhook."
            )
            raise HTTPException(
                status_code=500,
                detail="Secure webhook verification is required in production.",
            )
        logger.warning(
            "HUBSPOT_CLIENT_SECRET not set, skipping signature verification in %s environment.",
            settings.ENV,
        )
        return

    signature = x_hubspot_signature_v3 or x_hubspot_signature
    if not signature:
        raise HTTPException(status_code=401, detail="Missing HubSpot signature header")

    body_bytes = await request.body()
    secret = settings.HUBSPOT_CLIENT_SECRET.get_secret_value()
    version = x_hubspot_signature_version.lower()

    url_base = f"{request.url.scheme}://{request.url.hostname}{request.url.path}"
    # UI Extensions (hubspot.fetch) automatically append query parameters which HubSpot hashes.
    # Standard webhook calls typically do not include query params, but we keep this for
    # compatibility with UI extension callers that share this dependency.
    if request.url.query:
        url_base += f"?{request.url.query}"

    # --- Replay attack protection for v1/v2 ---
    # v3 embeds the timestamp inside the hash itself (replay-safe by design).
    # v1/v2 do not, so we enforce a 5-minute staleness window independently.
    if version in ("v1", "v2") and x_hubspot_request_timestamp:
        try:
            # HubSpot sends timestamps in milliseconds
            ts_seconds = int(x_hubspot_request_timestamp) / 1000
            drift = abs(time.time() - ts_seconds)
            if drift > _MAX_TIMESTAMP_AGE_SECONDS:
                logger.error(
                    "HubSpot signature timeout (drift=%.2fs, version=%s)",
                    drift,
                    version,
                )
                raise HTTPException(status_code=401, detail="Signature timeout")
        except HTTPException:
            raise
        except (ValueError, TypeError):
            logger.warning(
                "HubSpot timestamp header present but unparsable: %r",
                x_hubspot_request_timestamp,
            )
            # Non-fatal: header format is optional for v1/v2 callers

    if version == _SIG_V3 and x_hubspot_signature_v3:
        if not x_hubspot_request_timestamp:
            raise HTTPException(status_code=401, detail="Missing v3 timestamp")

        try:
            ts_seconds = int(x_hubspot_request_timestamp) / 1000
            if abs(time.time() - ts_seconds) > _MAX_TIMESTAMP_AGE_SECONDS:
                raise HTTPException(status_code=401, detail="Signature expired")
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="Invalid v3 timestamp format")

        source_bytes = (
            request.method.encode()
            + url_base.encode()
            + body_bytes
            + x_hubspot_request_timestamp.encode()
        )
        expected = hmac.new(
            secret.encode("utf-8"), source_bytes, hashlib.sha256
        ).hexdigest()
        signature_to_check = x_hubspot_signature_v3
    elif version == _SIG_V2:
        source_string = (
            secret
            + request.method
            + url_base
            + body_bytes.decode("utf-8", errors="replace")
        )
        expected = hashlib.sha256(source_string.encode("utf-8")).hexdigest()
        signature_to_check = x_hubspot_signature or ""
    else:
        source_bytes = secret.encode("utf-8") + body_bytes
        expected = hashlib.sha256(source_bytes).hexdigest()
        signature_to_check = x_hubspot_signature or ""

    if not hmac.compare_digest(expected, signature_to_check):
        logger.error(
            "HubSpot signature mismatch (version=%s). "
            "URL: %s, Method: %s. Expected: %s, Received: %s",
            version,
            url_base,
            request.method,
            expected,
            signature_to_check,
        )
        logger.debug("Request Headers: %s", dict(request.headers))
        raise HTTPException(status_code=401, detail="Invalid signature")

    logger.debug("HubSpot signature verified successfully")
