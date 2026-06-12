import hashlib
import hmac
import time
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.core.security.slack_signature import verify_slack_signature


@pytest.mark.asyncio
async def test_valid_signature_passes(monkeypatch):
    """Verify that a correctly signed Slack request passes verification."""
    # Mock settings.SLACK_SIGNING_SECRET
    monkeypatch.setattr(
        "app.core.config.settings.SLACK_SIGNING_SECRET",
        MagicMock(get_secret_value=lambda: "test-secret"),
    )

    ts = str(int(time.time()))
    body = b"payload=test"
    secret = "test-secret"

    # Replicate Slack's signature calculation
    sig_basestring = f"v0:{ts}:{body.decode()}"
    signature = (
        "v0="
        + hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    )

    headers = {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": signature}

    # Should not raise any exception
    await verify_slack_signature(headers, body)


@pytest.mark.asyncio
async def test_expired_timestamp_raises():
    """Verify that requests older than 5 minutes are rejected for security."""
    headers = {
        "X-Slack-Request-Timestamp": str(int(time.time()) - 400),  # > 300s
        "X-Slack-Signature": "v0=abc",
    }
    with pytest.raises(HTTPException) as exc:
        await verify_slack_signature(headers, b"body")
    assert exc.value.status_code == 401
    assert "timeout" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_invalid_signature_raises(monkeypatch):
    """Verify that tampered or incorrectly signed requests are rejected."""
    monkeypatch.setattr(
        "app.core.config.settings.SLACK_SIGNING_SECRET",
        MagicMock(get_secret_value=lambda: "test-secret"),
    )

    headers = {
        "X-Slack-Request-Timestamp": str(int(time.time())),
        "X-Slack-Signature": "v0=wrong",
    }
    with pytest.raises(HTTPException) as exc:
        await verify_slack_signature(headers, b"body")
    assert exc.value.status_code == 401
    assert "invalid signature" in exc.value.detail.lower()
