from __future__ import annotations

from typing import Any

from app.core.models.ui import UnifiedCard
from app.db.records import IntegrationRecord
from app.domains.common.sdk.context import UnifiedContext
from app.domains.common.sdk.ui_adapter import UIAdapter
from app.domains.crm.integration_service import IntegrationService


class SlackUIAdapter(UIAdapter):
    """Slack-specific implementation of the SDK UIAdapter."""

    def __init__(self, integration_service: IntegrationService):
        self.integration_service = integration_service

    async def show_loading(
        self, context: UnifiedContext, title: str, integration: IntegrationRecord
    ) -> str | None:
        if not context.trigger_id:
            return None

        from app.domains.crm.ui.card_builder import CardBuilder

        builder = CardBuilder()
        modal = builder.build_loading_modal(title=title)

        client = await self.integration_service.get_slack_client(integration)
        resp = await client.views_open(trigger_id=context.trigger_id, view=modal)
        return str(resp.get("view", {}).get("id"))

    async def update_modal(
        self,
        context: UnifiedContext,
        view_or_card: dict[str, Any] | UnifiedCard,
        title: str,
        integration: IntegrationRecord,
        metadata: str | None = None,
    ) -> bool:
        if not context.view_id:
            return False

        from app.domains.crm.ui.card_builder import CardBuilder

        builder = CardBuilder()

        if isinstance(view_or_card, dict):
            modal = view_or_card
        else:
            modal = builder.build_card_modal(
                view_or_card, title=title, metadata=metadata
            )

        client = await self.integration_service.get_slack_client(integration)
        await client.views_update(view_id=context.view_id, view=modal)
        return True

    async def open_modal(
        self,
        context: UnifiedContext,
        view_or_card: dict[str, Any] | UnifiedCard,
        title: str,
        integration: IntegrationRecord,
        metadata: str | None = None,
    ) -> str | None:
        if not context.trigger_id:
            return None

        from app.domains.crm.ui.card_builder import CardBuilder

        builder = CardBuilder()

        if isinstance(view_or_card, dict):
            modal = view_or_card
        else:
            modal = builder.build_card_modal(
                view_or_card, title=title, metadata=metadata
            )

        client = await self.integration_service.get_slack_client(integration)
        resp = await client.views_open(trigger_id=context.trigger_id, view=modal)
        return str(resp.get("view", {}).get("id"))

    async def send_card(
        self,
        context: UnifiedContext,
        card: UnifiedCard,
        integration: IntegrationRecord,
        messaging_service: Any,
        **kwargs: Any,
    ) -> bool:
        """Delegates rich card sending to SlackMessagingService."""
        # Note: In a pure SDK implementation, we might use a dedicated SlackRenderer here.
        # For now, we reuse the existing messaging_service.send_card logic.
        await messaging_service.send_card(
            workspace_id=integration.workspace_id,
            obj=kwargs.get("obj"),
            analysis=kwargs.get("analysis"),
            channel=context.channel_id,
            is_pro=kwargs.get("is_pro", False),
            response_url=context.response_url,
        )
        return True

    async def send_message(
        self,
        context: UnifiedContext,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        """Sends a message back to the user via Slack WebClient."""
        from app.db.records import Provider

        integration = await self.integration_service.get_integration(
            workspace_id=context.workspace_id, provider=Provider.SLACK
        )
        if not integration:
            return None

        client = await self.integration_service.get_slack_client(integration)
        resp = await client.chat_postMessage(
            channel=context.channel_id, text=text, blocks=blocks, thread_ts=thread_ts
        )
        return str(resp.get("ts"))
