from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from app.connectors.common.base import BaseChannel, BaseOAuthResult
from app.core.config import settings
from app.core.logging import get_logger
from app.core.models.channel import Identity, NormalizedEvent, OutboundMessage
from app.utils.helpers import HTTPClient

logger = get_logger("hubspot.channel")


class HubSpotOAuthResult(BaseOAuthResult):
    """HubSpot-specific OAuth metadata."""

    portal_id: str | None = Field(default=...)
    hub_domain: str | None = Field(default=None)


# Map HubSpot subscription types -> internal event types
EVENT_TYPE_MAP: dict[str, str] = {
    "contact.creation": "contact_created",
    "contact.propertyChange": "contact_updated",
    "deal.creation": "deal_created",
    "deal.propertyChange": "deal_updated",
    "ticket.creation": "ticket_created",
    "ticket.propertyChange": "ticket_updated",
    "task.creation": "task_created",
    "task.propertyChange": "task_updated",
    "meeting.creation": "meeting_created",
    "meeting.propertyChange": "meeting_updated",
    "company.creation": "company_created",
    "company.propertyChange": "company_updated",
    "call.creation": "call_created",
    "call.propertyChange": "call_updated",
    "note.creation": "note_created",
    "note.propertyChange": "note_updated",
    "email.creation": "email_created",
    "email.propertyChange": "email_updated",
}


class HubSpotChannel(BaseChannel):
    """Unified HubSpot channel implementation.
    Handles both infrastructure (OAuth) and domain logic (Normalization).
    """

    channel_name: str = "hubspot"
    supports_cards: bool = False
    supports_ephemeral: bool = False
    supports_threading: bool = False

    @property
    def bot_token(self) -> str | None:
        return None  # HubSpot doesn't use bot tokens in this way

    @bot_token.setter
    def bot_token(self, value: str | None) -> None:
        pass

    def __init__(self, corr_id: str, **_kwargs: Any) -> None:
        self.corr_id = corr_id
        self.http_client = HTTPClient.get_client(corr_id=corr_id)

    # -----------------------------
    # Authentication & Transport
    # -----------------------------
    async def exchange_token(
        self, code: str, redirect_uri: str | None = None
    ) -> HubSpotOAuthResult:
        """Exchanges a HubSpot authorization code for access and refresh tokens."""
        logger.debug("Exchanging HubSpot OAuth code")

        data = {
            "grant_type": "authorization_code",
            "client_id": settings.HUBSPOT_CLIENT_ID,
            "client_secret": settings.HUBSPOT_CLIENT_SECRET.get_secret_value(),
            "redirect_uri": redirect_uri
            or settings.HUBSPOT_REDIRECT_URI.unicode_string(),
            "code": code,
        }

        from app.utils.helpers import get_hub_api_host

        base_host = get_hub_api_host(None)  # Initial exchange on global or default host

        resp = await self.http_client.post(f"{base_host}/oauth/v1/token", data=data)
        resp.raise_for_status()
        payload = resp.json()

        if "access_token" not in payload:
            logger.error("Invalid HubSpot OAuth response: %s", payload)
            raise RuntimeError("HubSpot OAuth response missing access_token")

        logger.debug("HubSpot OAuth token exchange successful")

        return HubSpotOAuthResult(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            portal_id=str(payload.get("hub_id", "")),
            hub_domain=payload.get("hub_domain"),
            raw=payload,
        )

    # -----------------------------
    # Inbound Event Normalization
    # -----------------------------
    async def normalize_event(
        self, workspace_id: str, raw_event: Mapping[str, Any]
    ) -> NormalizedEvent:
        """Converts HubSpot webhook payloads to standard internal events."""
        subscription_type = raw_event.get("subscriptionType", "unknown")
        event_type = EVENT_TYPE_MAP.get(subscription_type, "unknown")

        identity = self._extract_identity(raw_event)

        return NormalizedEvent(
            workspace_id=workspace_id,
            source="hubspot",
            event_type=event_type,
            identity=identity,
            payload=dict(raw_event),
            timestamp=str(raw_event.get("occurredAt", "")),
        )

    def _extract_identity(self, raw_event: Mapping[str, Any]) -> Identity:
        """Helper to extract object identity from HubSpot event data."""
        return Identity(
            external_id=str(raw_event.get("objectId", "")),
            provider="hubspot",
            email=raw_event.get("email"),
            source="hubspot",
        )

    # -----------------------------
    # Identity Resolution
    # -----------------------------
    async def resolve_identity(self, event: NormalizedEvent) -> Identity | None:
        """Resolves a HubSpot identity (currently returns embedded identity)."""
        return event.identity

    # -----------------------------
    # Outbound Communication
    # -----------------------------
    async def send_message(
        self,
        message: OutboundMessage,
        **kwargs: Any,
    ) -> Mapping[str, Any] | None:
        """HubSpot is not a chat channel; outbound handled via dedicated services."""
        logger.debug("HubSpotChannel.send_message called (noop)")
        return None

    # -----------------------------
    # Lifecycle Hooks
    # -----------------------------
    async def install(self, payload: Mapping[str, Any]) -> None:
        """Post-install hook for HubSpot integration."""
        logger.debug("HubSpot install event received (handled upstream)")

    async def uninstall(self, payload: Mapping[str, Any]) -> None:
        """Post-uninstall hook for HubSpot integration."""
        logger.debug("HubSpot uninstall event received (handled upstream)")

    async def validate_install_payload(self, payload: Mapping[str, Any]) -> None:
        """Validates HubSpot installation metadata."""
        if "hub_id" not in payload and "portalId" not in payload:
            logger.error("Invalid HubSpot install payload")
            raise ValueError("Invalid HubSpot install payload")
