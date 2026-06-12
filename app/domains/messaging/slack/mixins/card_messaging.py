from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from slack_sdk.errors import SlackApiError

from app.connectors.slack.slack_channel import SlackChannel
from app.core.exceptions import IntegrationNotFoundError
from app.core.logging import get_logger
from app.core.models.ui import UnifiedCard
from app.domains.crm.hubspot.service import HubSpotService
from app.domains.crm.integration_service import IntegrationService
from app.domains.crm.notification_service import _recent_notifications

logger = get_logger("slack.messaging")


class CardMessagingMixin:
    """Mixin for Slack messaging capabilities."""

    if TYPE_CHECKING:
        corr_id: str
        integration_service: IntegrationService
        from slack_sdk.errors import SlackApiError

        from app.connectors.slack.slack_channel import SlackChannel
        from app.connectors.slack.slack_renderer import SlackRenderer
        from app.core.exceptions import IntegrationNotFoundError
        from app.domains.ai.service import AIService
        from app.domains.crm.service import CRMService
        from app.domains.crm.ui.card_builder import CardBuilder

        crm: CRMService
        ai: AIService
        cards: CardBuilder
        slack_renderer: SlackRenderer

        async def send_via_response_url(
            self,
            response_url: str,
            text: str,
            blocks: list[dict[str, Any]] | None = None,
            replace_original: bool = False,
        ) -> bool: ...
        async def send_message(self, **kwargs: Any) -> dict[str, Any]: ...
        async def get_slack_channel(self) -> Any: ...
        async def _resolve_channel(
            self,
            workspace_id: str,
            channel_id: str | None,
            obj: Mapping[str, Any] | None = None,
            is_system_alert: bool = False,
        ) -> str | None: ...
        async def _initialize_ticket_thread(
            self,
            *,
            workspace_id: str,
            object_id: str,
            channel: str,
            sent_ts: str,
        ) -> None: ...

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
        response_url: str | None = None,
        include_actions: bool = True,
        is_notification: bool = False,
        is_creation: bool = False,
        show_thinking: bool = False,
    ) -> str | None:
        """Builds and dispatches a rich CRM object card with AI insights to Slack.

        In Platform 2026.03, this supports 'Thinking Steps' to provide
        transparency during long analysis tasks.
        """
        # 1. Resolve channel early for thinking state
        resolved_channel = channel
        if not resolved_channel:
            resolved_channel = await self._resolve_channel(
                workspace_id, channel, obj=obj
            )

        sent_ts = None

        # 2. Handle Thinking Steps (2026.03 Pattern)
        if show_thinking:
            thinking_card = UnifiedCard(
                title="Analyzing Record...",
                emoji="🧠",
                thinking_steps=[
                    {"label": "Fetching CRM Context", "status": "completed"},
                    {"label": "Applying Heuristic Engine 2.0", "status": "in_progress"},
                    {"label": "Generating Insights", "status": "pending"},
                ],
            )
            rendered_thinking = self.slack_renderer.render(thinking_card)
            resp = await self.send_message(
                workspace_id=workspace_id,
                channel=resolved_channel,
                blocks=rendered_thinking["blocks"],
                text="REHA Connect is analyzing...",
                thread_ts=thread_ts,
            )
            sent_ts = str(resp.get("ts")) if resp else None

        # 3. Perform AI analysis (if not provided)
        if analysis is None:
            obj_type = str(obj.get("type") or "contact")
            analysis = await self.ai.analyze_polymorphic(obj, obj_type)

        # 4. Build Unified IR
        unified_card = self.cards.build(
            obj,
            cast(Any, analysis),
            is_pro=is_pro,
            pipelines=pipelines,
            include_actions=include_actions,
        )

        # 5. Render for Slack
        rendered = self.slack_renderer.render(unified_card)
        if is_notification:
            status_text = (
                "HubSpot Record Created" if is_creation else "HubSpot Record Updated"
            )
            rendered["blocks"].insert(
                0,
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"⚡ *REHA Connect | {status_text}*",
                        }
                    ],
                },
            )

        # 6. Send or update Slack message
        if response_url:
            await self.send_via_response_url(
                response_url=response_url,
                blocks=rendered["blocks"],
                text=unified_card.title or "CRM Object Detail",
                replace_original=True if sent_ts else False,  # noqa: SIM210
            )
        elif sent_ts:
            # Update the thinking message
            client = await self.get_slack_channel()
            # We need to use chat.update directly as send_message doesn't support it yet
            slack_client = cast("SlackChannel", client).get_slack_client()
            await slack_client.chat_update(
                channel=resolved_channel,
                ts=sent_ts,
                text=unified_card.title or "CRM Object Detail",
                blocks=rendered["blocks"],
            )
        else:
            resp = await self.send_message(
                workspace_id=workspace_id,
                channel=resolved_channel,
                blocks=rendered["blocks"],
                thread_ts=thread_ts,
                text=unified_card.title or "CRM Object Detail",
            )
            sent_ts = str(resp.get("ts")) if resp and resp.get("ts") else None

        # 7. Auto-initialize thread for tickets in Pro workspaces (only if open)
        is_closed = False
        if str(obj.get("type") or "contact") == "ticket":
            stage = str(obj.get("properties", {}).get("hs_pipeline_stage", "")).lower()
            is_closed = stage == "4" or "closed" in stage

        if (
            sent_ts
            and not thread_ts
            and resolved_channel
            and is_pro
            and str(obj.get("type") or "contact") == "ticket"
            and not is_closed
        ):
            await self._initialize_ticket_thread(
                workspace_id=workspace_id,
                object_id=str(obj.get("id") or ""),
                channel=resolved_channel,
                sent_ts=sent_ts,
            )

        return sent_ts

    async def build_reports_card(self, workspace_id: str) -> UnifiedCard:
        """Constructs a high-level reporting dashboard card for Slack."""
        if not self.integration_service:
            raise IntegrationNotFoundError("Integration service not available")

        workspace = await self.integration_service.storage.get_workspace(workspace_id)
        if not workspace:
            raise IntegrationNotFoundError("Workspace not found")

        # Fetch Real-Time CRM Metrics

        hs_service = HubSpotService(self.corr_id)

        open_deals, open_tickets = await asyncio.gather(
            hs_service.get_open_deals_count(workspace_id),
            hs_service.get_open_tickets_count(workspace_id),
        )

        return self.cards.build_reports_card(
            workspace_id=workspace_id,
            sync_count=workspace.total_sync_count or 0,
            notification_count=workspace.notification_count_monthly or 0,
            portal_id=workspace.portal_id,
            open_deals=open_deals,
            open_tickets=open_tickets,
        )

    async def refresh_and_update_card(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        channel_id: str | None = None,
        response_url: str | None = None,
        text: str = "Record updated.",
        **template_kwargs: Any,
    ) -> None:
        """Centralizes the standard UI refresh cycle:
        1. Wait for HubSpot consistency (Safety Buffer)
        2. Fetch object
        3. Run AI Analysis
        4. Build UI Card
        5. Render & Send via Slack (Response URL or channel injection)
        """
        # 0. Prevent Webhook Echo Duplicates by setting global debounce cache BEFORE sleep
        try:
            debounce_key = f"notif_debounce:{workspace_id}:{object_type}:{object_id}"
            await _recent_notifications.set(debounce_key, True)
            logger.debug(
                "Silenced outbound webhook notifications for %s %s",
                object_type,
                object_id,
            )
        except Exception as deb_err:
            logger.warning(
                "Failed to debounce webhook echo for %s %s: %s",
                object_type,
                object_id,
                deb_err,
            )

        # 1. Safety Buffer: Prevent Read-After-Write lag from HubSpot
        delay = float(os.getenv("HS_CONSISTENCY_DELAY_S", "1.5"))
        await asyncio.sleep(delay)

        # 2. Fetch Object
        obj = await self.crm.hubspot.get_object(
            workspace_id=workspace_id,
            object_type=object_type,
            object_id=object_id,
            ignore_cache=True,
        )
        if not obj:
            if channel_id:
                await self.send_message(
                    workspace_id=workspace_id,
                    channel=channel_id,
                    text=f"Error: Could not reload {object_type} after update.",
                )
            return

        # 2. Analyze
        analysis = await self.ai.analyze_polymorphic(obj, object_type)
        is_pro = True
        if self.integration_service:
            is_pro = await self.integration_service.is_pro_workspace(workspace_id)

        kwargs = {**template_kwargs}
        if object_type == "deal" and "pipelines" not in kwargs:
            kwargs["pipelines"] = await self.crm.hubspot.get_pipelines(
                workspace_id, "deals"
            )
        elif object_type == "ticket" and "pipelines" not in kwargs:
            kwargs["pipelines"] = await self.crm.hubspot.get_pipelines(
                workspace_id, "tickets"
            )

        # 3. Build unified card
        unified_card = self.cards.build(obj, analysis, is_pro=is_pro, **kwargs)
        rendered = self.slack_renderer.render(unified_card)

        # 4. Render and Send
        if response_url:
            await self.send_via_response_url(
                response_url=response_url,
                replace_original=True,
                blocks=rendered["blocks"],
                text=text,
            )
        elif channel_id:
            await self.send_message(
                workspace_id=workspace_id,
                channel=channel_id,
                blocks=rendered["blocks"],
                text=text,
            )

    async def update_view(
        self,
        bot_token: str,
        view_id: str | None,
        view: dict[str, Any],
    ) -> None:
        """Updates a Slack view (modal) with new content."""
        if not view_id:
            logger.warning("Attempted to update view without view_id")
            return
        try:
            channel = await self.get_slack_channel()
            client = cast("SlackChannel", channel).get_slack_client()
            await client.views_update(view_id=view_id, view=view)
        except SlackApiError as exc:
            logger.error("Failed to update Slack view %s: %s", view_id, exc)
            raise

    async def send_record_insights(
        self,
        *,
        workspace_id: str,
        channel: str | None = None,
        user_email: str | None = None,
        analysis: Any,
    ) -> None:
        """Sends record insights/recap to Slack or user DM."""
        unified_card = self.cards.build_record_insights(analysis)
        rendered = self.slack_renderer.render(unified_card)

        # Try sending to the defined channel (or fallback to workspace default)
        result = await self.send_message(
            workspace_id=workspace_id,
            channel=channel,
            **rendered,  # This correctly passes 'blocks' or 'attachments' if present
        )

        # If sending failed (e.g., no default channel resolved)
        # and we have their email, DM them
        if not result and user_email:
            logger.warning(
                "Primary channel delivery failed, attempting to DM user %s.", user_email
            )
            channel_inst = await self.get_slack_channel()
            slack_user_id = await channel_inst.get_user_by_email(user_email)
            if slack_user_id:
                await channel_inst.send_dm(
                    user_id=slack_user_id,
                    text="Your HubSpot CRM Insights",
                    blocks=rendered["blocks"],
                )
            else:
                logger.warning(
                    "Could not resolve Slack user ID for email %s.", user_email
                )
