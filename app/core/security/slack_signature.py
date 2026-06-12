# app/core/security/slack_signature.py  # noqa: D100
from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

from fastapi import Depends, HTTPException, Request

from app.core.config import settings
from app.core.logging import get_corr_id, get_logger

logger = get_logger("slack.security")


async def slack_signature_required(
    request: Request,
    corr_id: str = Depends(get_corr_id),
) -> None:
    """FastAPI dependency to enforce Slack signature verification."""
    body = await request.body()
    await verify_slack_signature(request.headers, body, corr_id=corr_id)


async def verify_slack_signature(
    headers: Mapping[str, str],
    body: bytes,
    *,
    corr_id: str | None = None,
) -> None:
    """Verify Slack request signature.

    Slack signs:
        v0:{timestamp}:{raw_body}

    Signature header:
        X-Slack-Signature: v0=hex(hmac_sha256(secret, basestring))

    Timestamp must be within 5 minutes to prevent replay attacks.
    """
    timestamp = headers.get("X-Slack-Request-Timestamp")
    signature = headers.get("X-Slack-Signature")
    secret = settings.SLACK_SIGNING_SECRET.get_secret_value()

    if not timestamp or not signature:
        logger.error("Missing Slack signature headers")
        raise HTTPException(
            status_code=401,
            detail="Missing Slack signature headers",
        )

    if not secret:
        logger.error("Missing Slack signing secret")
        raise HTTPException(
            status_code=500,
            detail="Server misconfiguration",
        )

    try:
        ts = int(timestamp)
    except ValueError:
        logger.error("Invalid Slack timestamp: %s", timestamp)
        raise HTTPException(status_code=401, detail="Invalid Slack timestamp")

    now = time.time()
    drift = abs(now - ts)
    if drift > 60 * 5:
        logger.error(
            "Slack signature timeout (drift=%.2fs, ts=%s, now=%.2f)", drift, ts, now
        )
        raise HTTPException(status_code=401, detail="Signature timeout")

    try:
        payload_str = body.decode("utf-8")
    except UnicodeDecodeError:
        logger.error("Failed to decode Slack request body (length=%s)", len(body))
        raise HTTPException(status_code=400, detail="Invalid encoding")

    basestring = f"v0:{timestamp}:{payload_str}"

    my_signature = (
        "v0="
        + hmac.new(
            secret.encode(),
            basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )

    if not hmac.compare_digest(my_signature, signature):
        logger.error(
            "Slack signature mismatch (body_len=%s, ts=%s, corr_id=%s)",
            len(body),
            timestamp,
            corr_id or "unknown",
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    logger.debug("Slack signature verified (drift=%.2fs)", drift)
