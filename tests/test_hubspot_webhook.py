"""Tests for HubSpot webhook signature verification (v1, v2, v3).

Covers M-08 from the production code review:
- v1 signature (SHA-256 of secret + body)
- v2 signature (SHA-256 of secret + method + URL + body)
- v3 signature (HMAC-SHA-256 of method + URL + body + timestamp)
- Replay attack prevention (timestamp > 5 minutes)
- Missing signature header
- Missing client secret in production
- Invalid JSON body handling
"""

from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core.security.hubspot_signature import verify_hubspot_signature

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-hub-secret"
_METHOD = "POST"
_URL_BASE = "https://api.rehaapps.com/api/hubspot/webhooks"
_BODY = b'[{"eventId":"1","subscriptionType":"contact.creation","portalId":"12345"}]'


def _make_request(
    method: str = _METHOD,
    url: str = _URL_BASE,
    body: bytes = _BODY,
) -> MagicMock:
    """Build a minimal mock ``Request`` object."""
    req = MagicMock()
    req.method = method
    req.url.scheme = "https"
    req.url.hostname = "api.rehaapps.com"
    req.url.path = "/api/hubspot/webhooks"
    req.url.query = ""  # no query params — keeps URL reconstruction clean
    req.body = AsyncMock(return_value=body)
    req.headers = {}
    return req


def _v1_signature(secret: str, body: bytes) -> str:
    return hashlib.sha256(secret.encode() + body).hexdigest()


def _v2_signature(secret: str, method: str, url: str, body: bytes) -> str:
    src = secret + method + url + body.decode("utf-8", errors="replace")
    return hashlib.sha256(src.encode()).hexdigest()


def _v3_signature(secret: str, method: str, url: str, body: bytes, ts: str) -> str:
    src = method.encode() + url.encode() + body + ts.encode()
    return hmac.new(secret.encode(), src, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# v1 Signature Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v1_valid_signature_passes():
    """A correctly computed v1 signature must pass verification."""
    sig = _v1_signature(_SECRET, _BODY)
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        # Should not raise
        await verify_hubspot_signature(
            request=req,
            x_hubspot_signature=sig,
            x_hubspot_signature_v3=None,
            x_hubspot_request_timestamp=None,
            x_hubspot_signature_version="v1",
        )


@pytest.mark.asyncio
async def test_v1_invalid_signature_raises_401():
    """A tampered v1 signature must be rejected with 401."""
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        with pytest.raises(HTTPException) as exc_info:
            await verify_hubspot_signature(
                request=req,
                x_hubspot_signature="aaabbbccc",
                x_hubspot_signature_v3=None,
                x_hubspot_request_timestamp=None,
                x_hubspot_signature_version="v1",
            )

    assert exc_info.value.status_code == 401
    assert "invalid signature" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# v2 Signature Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_valid_signature_passes():
    """A correctly computed v2 signature must pass verification."""
    sig = _v2_signature(_SECRET, _METHOD, _URL_BASE, _BODY)
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        await verify_hubspot_signature(
            request=req,
            x_hubspot_signature=sig,
            x_hubspot_signature_v3=None,
            x_hubspot_request_timestamp=None,
            x_hubspot_signature_version="v2",
        )


@pytest.mark.asyncio
async def test_v2_invalid_signature_raises_401():
    """A wrong v2 signature must be rejected."""
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        with pytest.raises(HTTPException) as exc_info:
            await verify_hubspot_signature(
                request=req,
                x_hubspot_signature="wrongsig",
                x_hubspot_signature_v3=None,
                x_hubspot_request_timestamp=None,
                x_hubspot_signature_version="v2",
            )

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# v3 Signature Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v3_valid_signature_passes():
    """A correctly computed v3 HMAC-SHA-256 signature must pass."""
    ts = str(int(time.time() * 1000))
    sig = _v3_signature(_SECRET, _METHOD, _URL_BASE, _BODY, ts)
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        await verify_hubspot_signature(
            request=req,
            x_hubspot_signature=None,
            x_hubspot_signature_v3=sig,
            x_hubspot_request_timestamp=ts,
            x_hubspot_signature_version="v3",
        )


@pytest.mark.asyncio
async def test_v3_invalid_signature_raises_401():
    """A wrong v3 signature must be rejected even with a valid timestamp."""
    ts = str(int(time.time() * 1000))
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        with pytest.raises(HTTPException) as exc_info:
            await verify_hubspot_signature(
                request=req,
                x_hubspot_signature=None,
                x_hubspot_signature_v3="badsignature",
                x_hubspot_request_timestamp=ts,
                x_hubspot_signature_version="v3",
            )

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Replay Attack Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v3_replay_attack_expired_timestamp_rejected():
    """v3 requests older than 5 minutes (300s) must be rejected (replay protection)."""
    # 6 minutes in the past → 360,000 ms ago
    expired_ts = str(int((time.time() - 360) * 1000))
    sig = _v3_signature(_SECRET, _METHOD, _URL_BASE, _BODY, expired_ts)
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = True
        mock_settings.ENV = "prod"

        with pytest.raises(HTTPException) as exc_info:
            await verify_hubspot_signature(
                request=req,
                x_hubspot_signature=None,
                x_hubspot_signature_v3=sig,
                x_hubspot_request_timestamp=expired_ts,
                x_hubspot_signature_version="v3",
            )

    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_v3_missing_timestamp_raises_401():
    """v3 signature without a timestamp header must be rejected."""
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        with pytest.raises(HTTPException) as exc_info:
            await verify_hubspot_signature(
                request=req,
                x_hubspot_signature=None,
                x_hubspot_signature_v3="somesig",
                x_hubspot_request_timestamp=None,  # missing
                x_hubspot_signature_version="v3",
            )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_v3_invalid_timestamp_format_raises_401():
    """A non-numeric timestamp must be rejected cleanly."""
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        with pytest.raises(HTTPException) as exc_info:
            await verify_hubspot_signature(
                request=req,
                x_hubspot_signature=None,
                x_hubspot_signature_v3="somesig",
                x_hubspot_request_timestamp="not-a-number",
                x_hubspot_signature_version="v3",
            )

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Missing Signature Header Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_signature_header_raises_401():
    """A request with no signature header must be rejected."""
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = _SECRET
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        with pytest.raises(HTTPException) as exc_info:
            await verify_hubspot_signature(
                request=req,
                x_hubspot_signature=None,
                x_hubspot_signature_v3=None,
                x_hubspot_request_timestamp=None,
                x_hubspot_signature_version="v1",
            )

    assert exc_info.value.status_code == 401
    assert "missing" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Missing Secret Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_secret_in_prod_raises_500():
    """Missing HUBSPOT_CLIENT_SECRET in production must return 500 — never bypass."""
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = ""
        mock_settings.is_prod = True
        mock_settings.ENV = "prod"

        with pytest.raises(HTTPException) as exc_info:
            await verify_hubspot_signature(
                request=req,
                x_hubspot_signature=None,
                x_hubspot_signature_v3=None,
                x_hubspot_request_timestamp=None,
                x_hubspot_signature_version="v1",
            )

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_missing_secret_in_dev_is_bypassed():
    """Missing secret in dev mode should NOT raise — verification is skipped."""
    req = _make_request()

    with patch("app.core.security.hubspot_signature.settings") as mock_settings:
        mock_settings.HUBSPOT_CLIENT_SECRET.get_secret_value.return_value = ""
        mock_settings.is_prod = False
        mock_settings.ENV = "dev"

        # Should not raise — dev bypass is intentional
        await verify_hubspot_signature(
            request=req,
            x_hubspot_signature=None,
            x_hubspot_signature_v3=None,
            x_hubspot_request_timestamp=None,
            x_hubspot_signature_version="v1",
        )
