"""app/core/security/provider_signature.py — provider-agnostic request signature verification.

This module provides a single FastAPI dependency ``verify_provider_signature``
that is used as a uniform entry-point for all provider interaction endpoints
(currently Slack).  Routing to the correct underlying verifier is done here so
that callers (e.g. ``interactions_router``) remain provider-agnostic.
"""

from __future__ import annotations

from fastapi import Depends, Request

from app.core.logging import get_corr_id
from app.core.security.slack_signature import slack_signature_required


async def verify_provider_signature(
    request: Request,
    corr_id: str = Depends(get_corr_id),
) -> None:
    """FastAPI dependency that verifies the inbound request signature.

    Currently delegates to Slack's HMAC-SHA256 signature verifier.
    Extend this function to support additional providers (e.g. Teams,
    WhatsApp) by inspecting a routing header or URL prefix.
    """
    await slack_signature_required(request, corr_id=corr_id)
