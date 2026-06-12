from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from app.core.logging import get_logger
from app.db.records import IntegrationRecord
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils import action_ids

from .base import (
    InteractionHandler,
    UnifiedContext,
    interaction_handler,
    require_feature,
    with_slack_error_handling,
)

logger = get_logger("note_handlers")


class NoteHandlers(InteractionHandler):
    @interaction_handler(action_ids.ADD_NOTE_SUBMISSION)
    @with_slack_error_handling("log note")
    async def _handle_add_note_submission(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        view = payload.get("view", {})
        metadata = self._parse_modal_metadata(view.get("private_metadata", ""))
        object_type = metadata.object_type
        object_id = metadata.object_id
        channel_id = metadata.channel_id
        if not object_type or not object_id:
            logger.warning("Missing object context in metadata")
            return
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)
        note_content = properties.get("content", "")
        if not note_content:
            logger.warning("Empty note content submitted")
            return
        # Resolve primary contact if logging a note to an engagement
        object_type, object_id = await self._resolve_primary_contact(
            integration.workspace_id, object_type, object_id, "note"
        )

        await self.crm.create_note(
            workspace_id=integration.workspace_id,
            content=note_content,
            associated_id=object_id,
            associated_type=object_type,
        )

        # Clear cache so subsequent lookups show the new note
        await self.crm.invalidate_object_caches(
            workspace_id=integration.workspace_id,
            object_type=object_type,
            object_id=object_id,
        )
        await self.ai.invalidate_recap_cache(
            workspace_id=integration.workspace_id,
            object_id=object_id,
        )
        await self._publish_timeline_event(
            workspace_id=integration.workspace_id,
            object_type=object_type,
            object_id=object_id,
            message_body=note_content,
            channel_id=channel_id,
            payload=payload,
        )
        return

    @interaction_handler(action_ids.OPEN_ADD_NOTE_MODAL)
    @require_feature("note_logging")
    async def _handle_open_add_note_modal(
        self,
        value: str,
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        trigger_id: str | None,
        **kwargs: Any,
    ) -> None:
        parts = value.split(":")
        if len(parts) < 3:
            logger.warning("Malformed add_note value=%s", value)
            return
        obj_type = parts[1]
        object_id = parts[2]
        view_id = kwargs.get("view_id")
        if not view_id and not trigger_id:
            logger.error("Missing trigger_id and view_id for modal")
            return
        try:
            metadata = context.build_metadata(object_type=obj_type, object_id=object_id)
            modal = messaging_service.cards.build_note_modal(
                obj_type, object_id, metadata=metadata
            )
            if view_id:
                await self._update_modal(
                    view_id=view_id,
                    view_or_card=modal,
                    title="Log note",
                    integration=integration,
                )
            else:
                await self._open_modal(
                    trigger_id=trigger_id,
                    view_or_card=modal,
                    title="Log note",
                    integration=integration,
                )
            logger.debug("Opened add_note modal for object_id=%s", object_id)
        except Exception as exc:
            logger.exception("Failed to open add note modal")
            await messaging_service.send_error(
                exc,
                response_url=context.response_url,
                user_id=str(kwargs.get("payload", {}).get("user", {}).get("id", "")),
                channel_id=context.channel_id,
                integration=integration,
                context="open note modal",
            )
