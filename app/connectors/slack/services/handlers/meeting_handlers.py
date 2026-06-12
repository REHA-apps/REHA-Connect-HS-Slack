from __future__ import annotations  # noqa: D100

import json
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from app.core.logging import get_logger
from app.db.records import IntegrationRecord
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

logger = get_logger("meeting_handlers")


class MeetingHandlers(InteractionHandler):
    @interaction_handler(action_ids.OPEN_SCHEDULE_MEETING_MODAL, "schedule_meeting")
    @require_feature("meeting_scheduler")
    async def _handle_open_meeting_modal(
        self,
        value: str,
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        trigger_id: str | None,
        **kwargs: Any,
    ) -> None:
        """Open meeting modal for contact."""
        parts = value.split(":")
        if len(parts) >= 3:
            obj_type = parts[1]
            obj_id = parts[2]
        elif len(parts) == 2:
            obj_type = "contact"
            obj_id = parts[1]
        else:
            return
        view_id = kwargs.get("view_id")
        if not view_id:
            if not trigger_id:
                return
            view_id = await self._show_loading(trigger_id, "Loading...", integration)
        try:
            metadata = context.build_metadata(object_id=obj_id, object_type=obj_type)
            modal = messaging_service.cards.build_meeting_modal(
                obj_id, object_type=obj_type, metadata=metadata
            )
            if view_id:
                await self._update_modal(
                    view_id, modal, "Schedule Meeting", integration
                )
            else:
                await self._open_modal(
                    trigger_id, modal, "Schedule Meeting", integration
                )
        except Exception as exc:
            logger.exception("Failed to open meeting modal")
            response_url = context.response_url
            if response_url:
                await messaging_service.send_via_response_url(
                    response_url=response_url,
                    text=f"{SLACK_ERROR_ICON} Failed to open meeting modal: {str(exc)}",
                )

    @interaction_handler(action_ids.SCHEDULE_MEETING_SUBMISSION)
    async def _handle_schedule_meeting_submission(  # noqa: PLR0912, PLR0915
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Process the schedule meeting modal submission."""
        view = payload.get("view", {})
        metadata = view.get("private_metadata", "")
        try:
            meta = json.loads(metadata)
            object_id = meta.get("object_id") or meta.get("contact_id")
            object_type = meta.get("object_type") or "contact"
            channel_id = meta.get("channel_id")
            response_url = meta.get("response_url")
            logger.info(
                "Parsed meeting submission context: object_type=%s object_id=%s",
                object_type,
                object_id,
            )
        except Exception:
            object_id = str(metadata)
            object_type = "contact"
            channel_id = None
            response_url = None
            logger.info(
                "Parsed meeting submission (fallback): object_type=%s id=%s",
                object_type,
                object_id,
            )
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)
        title = properties.get("title_input", "")
        date_str = properties.get("date_input", "")
        time_str = properties.get("time_input", "")
        body = properties.get("body_input", "")
        if not title or not date_str or (not time_str):
            logger.warning("Incomplete meeting data submitted")
            return

        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            int(dt.timestamp() * 1000)
        except Exception:
            logger.exception("Failed to parse meeting date/time")
            return
        from datetime import timedelta

        properties = {
            "hs_meeting_title": title,
            "hs_meeting_body": body or "Scheduled via Slack",
            "hs_meeting_start_time": int(dt.timestamp() * 1000),
            "hs_meeting_end_time": int((dt + timedelta(minutes=30)).timestamp() * 1000),
            "hs_timestamp": int(dt.timestamp() * 1000),
        }

        # Resolve primary contact if scheduling from an engagement
        object_type, object_id = await self._resolve_primary_contact(
            integration.workspace_id, object_type, object_id, "meeting"
        )

        try:
            result = await self.crm.create_meeting(
                workspace_id=integration.workspace_id,
                properties=properties,
                associated_id=object_id,
                associated_type=object_type,
            )

            # Clear cache so subsequent lookups show the new meeting
            await self.crm.invalidate_object_caches(
                workspace_id=integration.workspace_id,
                object_type=object_type,
                object_id=object_id,
            )

            meeting_id = result.get("id", "unknown")
            logger.debug("Meeting successfully created meeting_id=%s", meeting_id)
            user_id = context.user_id
            if user_id:
                pass
            pass
        except Exception as exc:
            logger.exception("Failed to create meeting")
            user_id = context.user_id
            if user_id:
                error_msg = f"{SLACK_ERROR_ICON} Failed to schedule meeting: {str(exc)}"
                success = False
                if response_url:
                    success = await messaging_service.send_via_response_url(
                        response_url=response_url, text=error_msg
                    )
                if not success:
                    client = await self.integration_service.get_slack_client(
                        integration
                    )
                    if channel_id:
                        try:
                            await client.chat_postEphemeral(
                                channel=str(channel_id), user=user_id, text=error_msg
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            await client.chat_postMessage(
                                channel=user_id, text=error_msg
                            )
                        except Exception:
                            pass
