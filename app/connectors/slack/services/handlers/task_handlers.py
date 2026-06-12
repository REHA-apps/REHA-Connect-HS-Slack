from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from datetime import UTC, datetime
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
    slack_error_handling,
)

logger = get_logger("task_handlers")


async def handle_task_creation(
    *,
    crm: Any,
    integration: IntegrationRecord,
    properties: dict[str, Any],
    association: str | None,
) -> None:
    """Creates a HubSpot task from modal properties and optionally associates it.

    This is the domain logic for the generic "Create Record → Task" flow
    triggered from ``CoreModalHandlers._handle_create_record_interaction``.
    Separated here so ``TaskHandlers`` owns the task domain and ``CoreModalHandlers``
    stays a thin dispatcher.

    Args:
        crm: The active ``CRMService`` instance.
        integration: The workspace integration record.
        properties: Form values extracted from the Slack modal state.
        association: Optional ``"<type>:<id>"`` string to associate the task with.

    """
    from app.utils.transformers import to_hubspot_timestamp

    if "hs_task_due_date" in properties:
        date_str = properties.pop("hs_task_due_date")
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
            properties["hs_timestamp"] = str(to_hubspot_timestamp(dt))
        except ValueError:
            pass
    if "hs_timestamp" not in properties:
        properties["hs_timestamp"] = str(to_hubspot_timestamp(datetime.now(UTC)))

    hubspot_client = await crm.get_client(integration.workspace_id)
    task = await hubspot_client.create_object("tasks", properties)
    task_id = task["id"]

    if association:
        assoc_type, assoc_id = association.split(":")
        await crm.associate_object(
            workspace_id=integration.workspace_id,
            from_type="task",
            from_id=task_id,
            to_type=assoc_type,
            to_id=assoc_id,
        )
        # Clear cache so subsequent lookups show the new task
        await crm.invalidate_object_caches(
            workspace_id=integration.workspace_id,
            object_type=assoc_type,
            object_id=assoc_id,
        )


class TaskHandlers(InteractionHandler):
    @interaction_handler(action_ids.ADD_TASK_SUBMISSION)
    async def _handle_add_task_submission(
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
        response_url = metadata.response_url
        if not object_type or not object_id:
            logger.warning("Missing object context in metadata")
            return
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)

        subject = properties.get("hs_task_subject", "")
        body = str(properties.get("hs_task_body", ""))
        task_type = properties.get("hs_task_type", "TODO")
        priority = properties.get("hs_task_priority", "NONE")

        if not subject:
            logger.warning("Empty task subject submitted")
            return

        async with slack_error_handling(
            "create Task",
            payload,
            messaging_service,
            response_url=response_url,
        ):
            # 1. Create task
            task = await self.crm.create_task(
                workspace_id=integration.workspace_id,
                properties={
                    "hs_task_subject": subject,
                    "hs_task_body": body,
                    "hs_task_type": task_type,
                    "hs_task_priority": priority,
                    "hs_task_status": "WAITING",
                    "hs_timestamp": int(datetime.now(UTC).timestamp() * 1000),
                },
            )
            # 2. Associate task with object
            task_id = str(task["id"])
            
            from app.domains.crm.notification_service import _recent_notifications
            await _recent_notifications.set(
                f"notif_debounce:{integration.workspace_id}:task:{task_id}", True
            )
            await self.crm.associate_object(
                workspace_id=integration.workspace_id,
                from_type="task",
                from_id=task_id,
                to_type=object_type,
                to_id=object_id,
            )

            # Clear cache so subsequent lookups show the new task
            await self.crm.invalidate_object_caches(
                workspace_id=integration.workspace_id,
                object_type=object_type,
                object_id=object_id,
            )

            # 3. Publish to feed if possible
            await self._publish_timeline_event(
                workspace_id=integration.workspace_id,
                object_type=object_type,
                object_id=object_id,
                message_body=f"Task created: {subject}",
                channel_id=channel_id,
                payload=payload,
            )
        return

    @interaction_handler(action_ids.OPEN_ADD_TASK_MODAL)
    async def _handle_open_add_task_modal(
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
            logger.warning("Malformed add_task value=%s", value)
            return
        obj_type = parts[1]
        object_id = parts[2]
        view_id = kwargs.get("view_id")
        if not view_id and not trigger_id:
            logger.error("Missing trigger_id and view_id for modal")
            return
        try:
            metadata = context.build_metadata(object_type=obj_type, object_id=object_id)
            modal = messaging_service.cards.build_add_task_modal(
                obj_type, object_id, metadata=metadata
            )
            if view_id:
                await self._update_modal(view_id, modal, "Create task", integration)
            else:
                await self._open_modal(trigger_id, modal, "Create task", integration)
            logger.debug("Opened add_task modal for object_id=%s", object_id)
        except Exception as exc:
            logger.exception("Failed to open add task modal")
            await messaging_service.send_error(
                exc,
                response_url=context.response_url,
                user_id=str(kwargs.get("payload", {}).get("user", {}).get("id", "")),
                channel_id=context.channel_id,
                integration=integration,
                context="open task modal",
            )
