from __future__ import annotations  # noqa: D100

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any


class MessagingService(ABC):
    """Abstract base class for all messaging services (Slack, WhatsApp, Teams).

    Ensures a consistent interface for sending basic messages and rich CRM
    entity notifications across different platforms.
    """

    def __init__(
        self,
        corr_id: str | None = None,
        integration_service: Any = None,  # Avoid circular import
        **kwargs: Any,
    ) -> None:
        self.corr_id = corr_id or "system"
        self.integration_service = integration_service

    @abstractmethod
    async def send_message(
        self,
        *,
        workspace_id: str,
        channel: str | None,
        text: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        metadata: Mapping[str, Any] | None = None,
        thread_ts: str | None = None,
        unfurl_links: bool = True,
    ) -> Mapping[str, Any] | None:
        """Sends a generic message to the specified destination."""
        pass

    @abstractmethod
    async def send_card(
        self,
        *,
        workspace_id: str,
        obj: Mapping[str, Any],
        channel: str | None = None,
        analysis: Any = None,
        is_pro: bool = False,
        thread_ts: str | None = None,
        pipelines: list[dict[str, Any]] | None = None,
        is_notification: bool = False,
        is_creation: bool = False,
    ) -> str | None:
        """Sends a rich CRM object card to the specified destination."""
        pass

    @abstractmethod
    async def send_record_insights(
        self,
        *,
        workspace_id: str,
        channel: str | None = None,
        user_email: str | None = None,
        analysis: Any,
    ) -> None:
        """Sends AI insights for a specific record to a channel or DM."""
        pass

    @abstractmethod
    async def send_dm(
        self,
        *,
        user_id: str | None = None,
        user_email: str | None = None,
        text: str,
    ) -> bool:
        """Sends a direct message to a user by ID or email."""
        pass

    @abstractmethod
    async def update_view(
        self,
        bot_token: str,
        view_id: str | None,
        view: dict[str, Any],
    ) -> None:
        """Updates an existing UI view (e.g. Slack modal)."""
        pass

    @abstractmethod
    async def _resolve_channel(
        self,
        workspace_id: str,
        channel_id: str | None,
        obj: Mapping[str, Any] | None = None,
        is_system_alert: bool = False,
    ) -> str | None:
        """Resolves the target channel/destination for a message."""
        pass

    async def on_agent_reply(self, workspace_id: str, thread_ts: str) -> None:
        """Lifecycle hook called when a human agent replies in a thread.

        Override in platform-specific subclasses to integrate with monitoring
        systems (e.g. GhostingMonitor for HubSpot). The default is a no-op so
        connectors that don't need it (e.g. WhatsApp) don't require an override.

        Args:
            workspace_id: The internal workspace identifier.
            thread_ts: The platform-specific thread timestamp/ID.

        """
