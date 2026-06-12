from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.core.models.ui import UnifiedCard
from app.db.records import IntegrationRecord
from app.domains.common.sdk.context import UnifiedContext


@runtime_checkable
class MessagingService(Protocol):
    """Protocol for platform-specific messaging implementations (Slack, etc.)."""

    async def send_card(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        channel_id: str,
        card_builder: Any,
        **kwargs: Any,
    ) -> bool:
        """Sends a structured card to the messaging platform."""
        ...

    async def refresh_and_update_card(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        channel_id: str,
        response_url: str | None,
        text: str,
    ) -> None:
        """Refreshes CRM data and updates an existing message/card."""
        ...


@runtime_checkable
class UIAdapter(Protocol):
    """Protocol defining how the SDK interacts with platform-specific UI elements."""

    async def show_loading(
        self, context: UnifiedContext, title: str, integration: IntegrationRecord
    ) -> str | None:
        """Opens a loading indicator or modal."""
        ...

    async def update_modal(
        self,
        context: UnifiedContext,
        view_or_card: dict[str, Any] | UnifiedCard,
        title: str,
        integration: IntegrationRecord,
        metadata: str | None = None,
    ) -> bool:
        """Updates an existing modal with final content."""
        ...

    async def open_modal(
        self,
        context: UnifiedContext,
        view_or_card: dict[str, Any] | UnifiedCard,
        title: str,
        integration: IntegrationRecord,
        metadata: str | None = None,
    ) -> str | None:
        """Opens a new modal window."""
        ...

    async def send_card(
        self,
        context: UnifiedContext,
        card: UnifiedCard,
        integration: IntegrationRecord,
        messaging_service: MessagingService,
        **kwargs: Any,
    ) -> bool:
        """Renders and sends a UnifiedCard to the platform."""
        ...

    async def send_message(
        self,
        context: UnifiedContext,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        """Sends a message back to the user (DM or Channel).."""
        ...
