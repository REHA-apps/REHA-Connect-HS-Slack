from __future__ import annotations  # noqa: D100

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from app.connectors.slack.slack_channel import SlackChannel

from app.core.logging import get_logger
from app.db.records import IntegrationRecord
from app.domains.ai.service import AIThreadSummary
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils import action_ids
from app.utils.constants import (
    SLACK_ERROR_ICON,
)

from .base import (
    InteractionHandler,
    UnifiedContext,
    interaction_handler,
    require_feature,
)

logger = get_logger("recap_handlers")


class RecapHandlers(InteractionHandler):
    @interaction_handler(action_ids.OPEN_AI_RECAP_MODAL)
    @interaction_handler(action_ids.OPEN_RECORD_RECAP_MODAL)
    @require_feature("ai_insights")
    async def _handle_open_ai_recap_modal(  # noqa: PLR0912, PLR0915
        self,
        value: str,
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        trigger_id: str | None,
        payload: Mapping[str, Any],
        view_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Fetch thread, summarize, and show review modal."""
        parts = value.split(":")
        if len(parts) < 3:
            return
        obj_type = parts[1]
        obj_id = parts[2]

        # If we didn't get a view_id from fast-path, we must have a trigger_id
        if not view_id and not trigger_id:
            return

        # Only show loading if we don't already have a view_id from the fast-path
        if not view_id:
            view_id = await self._show_loading(
                str(trigger_id), "Summarizing Thread...", integration
            )
        # Dynamic naming based on object type
        label = "Summary" if obj_type == "ticket" else "Deep Recap"

        try:
            workspace_id = integration.workspace_id
            slack_channel = await messaging_service.get_slack_channel()
            storage = self.integration_service.storage

            # 1. Fetch HubSpot Ticket Data (with Hydrated Engagements)
            ticket_data = await self.crm.get_object(
                workspace_id=workspace_id, object_type="ticket", object_id=obj_id
            )
            if not ticket_data:
                logger.warning("Could not find ticket_data for ticket_id=%s", obj_id)
                # Fallback to empty dict or handle error
                ticket_data = cast(Any, {})
            # Use the dedicated engagement fetcher to get actual note/email content
            engagements = await self.crm.get_object_engagements(
                workspace_id=workspace_id, object_type="ticket", object_id=obj_id
            )

            # 2. Fetch Slack Thread/History
            mapping = await storage.thread_mappings.fetch_single(
                {"workspace_id": workspace_id, "object_id": obj_id}
            )

            replies = []
            if mapping:
                if mapping.thread_ts == "CHANNEL_ROOT":
                    replies = await cast(
                        "SlackChannel", slack_channel
                    ).get_channel_history(channel_id=mapping.channel_id)
                else:
                    replies = await cast(
                        "SlackChannel", slack_channel
                    ).get_thread_replies(
                        channel_id=mapping.channel_id, thread_ts=mapping.thread_ts
                    )

            # 3. Hybrid AI Analysis (HubSpot + Slack)
            analysis = await self.ai.analyze_ticket(
                ticket_data,
                engagements=engagements,
                slack_messages=replies,
                object_id=obj_id,
                compact=False,
            )

            # Convert analysis into the expected format for the modal
            summary = AIThreadSummary(
                summary=analysis.insight,
                key_points=[analysis.next_best_action],
                sentiment=analysis.status,
            )

            metadata = context.build_metadata(object_type=obj_type, object_id=obj_id)
            modal = messaging_service.cards.build_record_recap_modal(
                obj_type, obj_id, summary, metadata=metadata
            )
            if view_id:
                await self._update_modal(view_id, modal, label, integration)
            else:
                bot_token = await self._resolve_bot_token(integration)
                await cast("SlackChannel", slack_channel).open_view(
                    bot_token=bot_token,
                    trigger_id=str(trigger_id),
                    view=modal,
                )
        except Exception as exc:
            logger.exception("Failed to open %s modal: %s", label, exc)
            error_msg = f"Failed to open {label} modal: {str(exc)}"
            if view_id:
                modal = messaging_service.cards.build_error_modal(
                    error_msg, title=f"{label} Error"
                )
                await self._update_modal(view_id, modal, f"{label} Error", integration)
            else:
                response_url = context.response_url
                if response_url:
                    await messaging_service.send_via_response_url(
                        response_url=response_url,
                        text=f"{SLACK_ERROR_ICON} {error_msg}",
                    )

    @interaction_handler(action_ids.RECORD_RECAP_SUBMISSION)
    async def _handle_record_recap_submission(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Save the AI summary to HubSpot as a note."""
        view = payload.get("view", {})
        metadata = view.get("private_metadata", "")
        try:
            meta = json.loads(metadata)
            object_type = meta.get("object_type")
            object_id = meta.get("object_id")
            channel_id = meta.get("channel_id")
            response_url = meta.get("response_url")
        except Exception:
            parts = str(metadata).split(":")
            object_type = parts[0] if len(parts) > 0 else ""
            object_id = parts[1] if len(parts) > 1 else ""
            channel_id = None
            response_url = None
        if not object_id or not object_type:
            return
        obj_type = object_type
        obj_id = object_id
        blocks = view.get("blocks", [])
        summary_text = "Deep Recap Summary"
        for block in blocks:
            if block.get("type") == "section" and "*Summary:*" in block.get(
                "text", {}
            ).get("text", ""):
                summary_text = block["text"]["text"]
                break
        # Dynamic naming based on object type
        label = "Summary" if obj_type == "ticket" else "Deep Recap"

        try:
            await self.crm.create_note(
                workspace_id=integration.workspace_id,
                content=f"--- {label.upper()} ---\n{summary_text}",
                associated_id=obj_id,
                associated_type=obj_type,
            )

            # Clear cache so subsequent lookups show the new note
            await self.crm.invalidate_object_caches(
                workspace_id=integration.workspace_id,
                object_type=obj_type,
                object_id=obj_id,
            )
            await self.ai.invalidate_recap_cache(
                workspace_id=integration.workspace_id,
                object_id=obj_id,
            )

            logger.debug("%s saved as note for %s %s", label, obj_type, obj_id)
            user_id = context.user_id
            pass
        except Exception as exc:
            logger.exception("Failed to save %s: %s", label, exc)
            user_id = context.user_id
            if user_id:
                error_msg = f"{SLACK_ERROR_ICON} Failed to save {label}: {str(exc)}"
                if response_url:
                    await messaging_service.send_via_response_url(
                        response_url=response_url, text=error_msg
                    )
                elif channel_id:
                    client = await self.integration_service.get_slack_client(
                        integration
                    )
                    try:
                        await client.chat_postEphemeral(
                            channel=str(channel_id), user=user_id, text=error_msg
                        )
                    except Exception:
                        pass
