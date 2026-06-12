# ruff: noqa: E501  # noqa: D100
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Request

from app.core.config import settings


def generate_and_store_state(
    request: Request, provider: str, context: str | None = None
) -> str:
    """Generates a random salt, stores it in the session, and returns a combined state.

    The state is formatted as 'salt.context'.
    This provides CSRF protection for OAuth flows while allowing context preservation.
    """
    salt = secrets.token_urlsafe(16)
    request.session[f"oauth_state_{provider}"] = salt
    return f"{salt}.{context or ''}"


def verify_and_clear_state(
    request: Request, provider: str, state: str | None
) -> str | None:
    """Verifies the state from the request against the session and returns the context.

    Returns the original context if valid, None otherwise.
    """
    if not state:
        return None

    try:
        parts = state.split(".", 1)
        salt = parts[0]
        context = parts[1] if len(parts) > 1 else ""

        stored_salt = request.session.pop(f"oauth_state_{provider}", None)

        if stored_salt and secrets.compare_digest(str(stored_salt), salt):
            return context
    except Exception as e:
        from app.core.logging import get_logger

        get_logger("state.validator").error("Verification error: %s", e)

    return None


def encode_state(state: str) -> str:
    """Base64 encodes a plain state string (e.g. 'hs_148238284') to prevent triggering Cloudflare WAF."""
    if not state:
        return ""
    # Only base64 encode if it looks like a raw workspace or slack ID
    if state.startswith("hs_") or (state.isalnum() and len(state) >= 9):
        return base64.urlsafe_b64encode(state.encode()).decode().strip("=")
    return state


def decode_state(encoded: str) -> str:
    """Base64 decodes a state string if it was encoded by encode_state."""
    if not encoded:
        return ""
    try:
        val = encoded
        padding = len(val) % 4
        if padding:
            val += "=" * (4 - padding)
        decoded = base64.urlsafe_b64decode(val.encode()).decode()
        if decoded.startswith("hs_") or (decoded.isalnum() and len(decoded) >= 9):
            return decoded
    except Exception:
        pass
    return encoded


def encode_state_context(data: dict[str, Any]) -> str:
    """Encodes a dictionary context into a safe base64 string for the state parameter."""
    json_str = json.dumps(data)
    return base64.urlsafe_b64encode(json_str.encode()).decode().strip("=")


def decode_state_context(encoded: str) -> dict[str, Any]:
    """Decodes a base64 string back into a context dictionary."""
    if not encoded:
        return {}
    try:
        # Add padding back if necessary
        padding = len(encoded) % 4
        if padding:
            encoded += "=" * (4 - padding)
        json_str = base64.urlsafe_b64decode(encoded.encode()).decode()
        return json.loads(json_str)
    except Exception:
        return {}


# --- Legacy Sign-based state (keeping for compatibility if needed) ---
# ... (rest of the file)


# --- Legacy Sign-based state (keeping for compatibility if needed) ---


def sign_state(state: str) -> str:
    """Signs a state parameter with a timestamp and HMAC."""
    timestamp = str(int(time.time()))
    message = f"{state}:{timestamp}".encode()
    signature = hmac.new(
        settings.SLACK_SIGNING_SECRET.get_secret_value().encode(),
        message,
        hashlib.sha256,
    ).digest()

    encoded_sig = base64.urlsafe_b64encode(signature).decode().strip("=")
    return f"{state}.{timestamp}.{encoded_sig}"


def verify_state(signed_state: str, max_age: int = 600) -> str | None:
    """Verifies a signed state parameter and returns the original state if valid."""
    try:
        parts = signed_state.split(".")
        if len(parts) != 3:  # noqa: PLR2004
            return None

        state, timestamp, signature = parts

        # Check expiration
        if int(time.time()) - int(timestamp) > max_age:
            return None

        # Verify signature
        message = f"{state}:{timestamp}".encode()
        expected_signature = hmac.new(
            settings.SLACK_SIGNING_SECRET.get_secret_value().encode(),
            message,
            hashlib.sha256,
        ).digest()

        encoded_expected = (
            base64.urlsafe_b64encode(expected_signature).decode().strip("=")
        )

        if hmac.compare_digest(encoded_expected, signature):
            return state

        return None
    except Exception:
        return None
