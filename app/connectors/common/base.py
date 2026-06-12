from __future__ import annotations  # noqa: D100

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.models.channel import (
    Identity,
    NormalizedEvent,
    OutboundMessage,
)


class BaseOAuthResult(BaseModel):
    """Standardized result for OAuth exchanges."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    access_token: str
    refresh_token: str | None = None
    expires_at: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    # Common but optional platform IDs
    portal_id: str | None = Field(default=None)
    team_id: str | None = Field(default=None)


class BaseChannel(ABC):
    """Unified abstract base class for all communication channels (Slack, WhatsApp).

    Rules Applied:
        - Combines infrastructure (OAuth) with domain logic (Normalization, Messaging).
        - Every channel must implement transport, normalization, and lifecycle hooks.
    """

    channel_name: str = "unknown"
    supports_cards: bool = False
    supports_ephemeral: bool = False
    supports_threading: bool = False

    # -----------------------------
    # Authentication & Transport
    # -----------------------------
    @abstractmethod
    async def exchange_token(
        self, code: str, redirect_uri: str | None = None
    ) -> BaseOAuthResult:
        """Handles the OAuth code-to-token exchange for this channel."""
        raise NotImplementedError

    # -----------------------------
    # Inbound Event Normalization
    # -----------------------------
    @abstractmethod
    async def normalize_event(
        self,
        workspace_id: str,
        raw_event: Mapping[str, Any],
    ) -> NormalizedEvent:
        """Converts raw provider webhooks into a standard NormalizedEvent."""
        raise NotImplementedError

    # -----------------------------
    # Identity Resolution
    # -----------------------------
    @abstractmethod
    async def resolve_identity(
        self,
        event: NormalizedEvent,
    ) -> Identity | None:
        """Maps a channel-specific user ID to a CRM identity."""
        raise NotImplementedError

    # -----------------------------
    # Outbound Communication
    # -----------------------------
    @abstractmethod
    async def send_message(
        self,
        message: OutboundMessage,
        **kwargs: Any,
    ) -> Mapping[str, Any] | None:
        """Sends an outbound message or notification to the channel."""
        raise NotImplementedError

    # -----------------------------
    # Lifecycle Hooks
    # -----------------------------
    @abstractmethod
    async def install(self, payload: Mapping[str, Any]) -> None:
        """Hook called during the installation/OAuth flow."""
        raise NotImplementedError

    @abstractmethod
    async def uninstall(self, payload: Mapping[str, Any]) -> None:
        """Hook called during the uninstallation/deauthorization flow."""
        raise NotImplementedError

    async def validate_install_payload(self, payload: Mapping[str, Any]) -> None:
        """Optional hook to validate installation data."""
        return

    async def get_default_channel_id(self) -> str | None:
        """Optional method to get the default channel ID for the workspace."""
        return None

    async def apps_uninstall(self) -> bool:
        """Uninstalls the app from the platform."""
        return True

    async def resolve_channel_name(self, name: str) -> str | None:
        """Resolves a channel name to an ID."""
        return None

    async def get_user_by_email(self, email: str) -> str | None:
        """Resolves a user ID by email."""
        return None

    async def send_dm(
        self, user_id: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> Mapping[str, Any] | None:
        """Sends a direct message."""
        return None

    async def send_via_response_url(
        self,
        response_url: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        replace_original: bool = False,
    ) -> bool:
        """Sends a response via webhook."""
        return False

    async def chat_unfurl(
        self, channel: str, ts: str, unfurls: dict[str, dict[str, Any]]
    ) -> Any:
        """Handles link unfurling."""
        return None

    async def get_thread_replies(
        self, channel_id: str, thread_ts: str
    ) -> list[dict[str, Any]]:
        """Optional method to fetch thread replies."""
        return []

    @property
    def bot_token(self) -> str | None:
        return None

    @bot_token.setter
    def bot_token(self, value: str | None) -> None:
        pass
