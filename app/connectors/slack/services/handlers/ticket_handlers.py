from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from app.connectors.slack.slack_channel import SlackChannel

from slack_sdk.errors import SlackApiError

from app.connectors.slack.ui.modal_builder import ModalBuilder
from app.core.exceptions import HubSpotAPIError
from app.core.logging import get_logger
from app.db.records import IntegrationRecord, ThreadMappingRecord
from app.domains.common.audit_service import AuditService
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils import action_ids
from app.utils.constants import SLACK_SUCCESS_ICON

from .base import (
    InteractionHandler,
    UnifiedContext,
    interaction_handler,
    require_feature,
    with_slack_error_handling,
)

logger = get_logger("ticket_handlers")


async def handle_ticket_creation(  # noqa: PLR0915
    *,
    crm: Any,
    integration_service: Any,
    integration: IntegrationRecord,
    properties: dict[str, Any],
    association: str | None,
    messaging_service: SlackMessagingService,
    user_id: str,
) -> None:
    """Creates a HubSpot ticket in the customer's own CRM workspace.

    This is the domain logic for the generic "Create Record → Ticket" flow
    triggered from ``CoreModalHandlers._handle_create_record_interaction``.
    Separated here so ``SupportHandlers`` owns the ticket domain and
    ``CoreModalHandlers`` stays a thin dispatcher.

    Responsibilities:
    - Resolves the submitting Slack user to a HubSpot contact (find-or-create).
    - Atomically creates the ticket with contact + optional CRM object associations.
    - Provisions a private Slack channel for the ticket and invites the user.
    - Posts a control panel card and a success DM.

    Args:
        crm: The active ``CRMService`` instance.
        integration_service: The integration service for Slack client retrieval.
        integration: The workspace integration record.
        properties: Form values extracted from the Slack modal state.
        association: Optional ``"<type>:<id>"`` string to associate the ticket with.
        messaging_service: Slack messaging service for API calls.
        user_id: The Slack user ID who submitted the modal.

    """
    subject = properties.get("subject", "Support Ticket")
    hs_associations: list[dict[str, Any]] = []

    # Resolve Slack user as the ticket contact BEFORE creation (atomic bridge)
    try:
        logger.info(
            "Attempting to resolve Slack user %s for atomic ticket bridging", user_id
        )
        channel_inst = await messaging_service.get_slack_channel()
        slack_client = cast("SlackChannel", channel_inst).get_slack_client()
        user_info = await slack_client.users_info(user=user_id)

        if user_info.get("ok"):
            profile = user_info.get("user", {}).get("profile", {})
            email = profile.get("email")
            name = profile.get("real_name")

            if email:
                contact_id = await crm.find_or_create_contact_by_email(
                    integration.workspace_id, email, name
                )
                hs_associations.append(
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 16,  # ticket_to_contact
                            }
                        ],
                    }
                )
                logger.info(
                    "Queued atomic association: contact_id=%s for email=%s",
                    contact_id,
                    email,
                )
            else:
                logger.warning(
                    "No email found for Slack user %s (scope issue?)", user_id
                )
    except Exception as bridge_err:
        logger.warning("Failed to prepare ticket-contact bridge: %s", bridge_err)

    # Add existing association (e.g. from a Company/Deal card)
    if association:
        assoc_type, assoc_id = association.split(":")
        type_id = (
            18 if assoc_type == "company" else 28
        )  # ticket_to_company / ticket_to_deal
        hs_associations.append(
            {
                "to": {"id": assoc_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": type_id,
                    }
                ],
            }
        )

    # Create ticket with ALL associations atomically
    ticket = await crm.create_ticket(
        integration.workspace_id, properties, associations=hs_associations
    )
    ticket_id = ticket["id"]
    logger.info(
        "SUCCESS: Created ticket %s with %d associations",
        ticket_id,
        len(hs_associations),
    )

    from app.domains.crm.notification_service import _recent_notifications

    await _recent_notifications.set(
        f"notif_debounce:{integration.workspace_id}:ticket:{ticket_id}", True
    )

    # Post ticket to triage channel (or fallback to user DM)
    channel_inst = await messaging_service.get_slack_channel()
    slack_client = cast("SlackChannel", channel_inst).get_slack_client()

    triage_channel_id = integration.metadata.get("triage_channel_id")
    target_channel = triage_channel_id or user_id

    builder = ModalBuilder()
    control_panel_blocks = builder.build_ticket_control_panel(ticket_id, subject)

    try:
        post_resp = await slack_client.chat_postMessage(
            channel=target_channel,
            text=f"🎫 Ticket #{ticket_id} created!",
            blocks=control_panel_blocks,
        )
        message_ts = post_resp.get("ts")

        # Store channel-to-ticket mapping for sync
        from app.db.records import ThreadMappingRecord

        if message_ts:
            await integration_service.storage.save_thread_mapping(
                ThreadMappingRecord(
                    workspace_id=integration.workspace_id,
                    object_type="ticket",
                    object_id=ticket_id,
                    channel_id=target_channel,
                    thread_ts=message_ts,
                )
            )

        if target_channel != user_id:
            success_msg = (
                f"{SLACK_SUCCESS_ICON} Ticket created! View it here: "
                f"<#{target_channel}>"
            )
            await slack_client.chat_postMessage(channel=user_id, text=success_msg)

    except Exception as e:
        logger.error("Failed to post ticket to triage channel: %s", e)
        if target_channel != user_id:
            # Fallback to user if triage channel fails
            await slack_client.chat_postMessage(
                channel=user_id,
                text=f"🎫 Ticket #{ticket_id} created! (Failed to post to triage channel)",
                blocks=control_panel_blocks,
            )


class TicketHandlers(InteractionHandler):
    @interaction_handler("ticket_reply")
    @with_slack_error_handling("ticket_reply")
    @require_feature("ticket_sync")
    async def _handle_ticket_reply(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        view_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Opens a modal to reply to the customer."""
        if not context.value or not view_id:
            logger.warning("Missing ticket_id or view_id for ticket_reply")
            return
        ticket_id = context.get_entity_id()
        if not ticket_id:
            return

        crm_client = await self.crm.get_client(integration.workspace_id)
        ticket = await crm_client.get_object("tickets", ticket_id)
        subject = (
            ticket.get("properties", {}).get("subject", "Support Ticket")
            if ticket
            else "Support Ticket"
        )

        import json

        from app.connectors.slack.ui.modal_builder import ModalBuilder

        channel_id = payload.get("channel", {}).get("id")
        thread_ts = (
            payload.get("container", {}).get("thread_ts")
            or payload.get("message", {}).get("ts")
            or payload.get("container", {}).get("message_ts")
        )

        metadata_dict = {"t": ticket_id}
        if channel_id and thread_ts:
            metadata_dict["c"] = channel_id
            metadata_dict["ts"] = thread_ts

        builder = ModalBuilder()
        reply_modal = builder.build_ticket_reply_modal(
            ticket_id, subject, json.dumps(metadata_dict)
        )

        slack_channel = await messaging_service.get_slack_channel()
        slack_client = cast("SlackChannel", slack_channel).get_slack_client()
        await slack_client.views_update(view_id=view_id, view=reply_modal)

    @interaction_handler("submit_ticket_reply")
    @with_slack_error_handling("submit_ticket_reply")
    @require_feature("ticket_sync")
    async def _handle_submit_ticket_reply(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Sends an outbound reply to the customer via HubSpot."""
        private_metadata = payload.get("view", {}).get("private_metadata")
        if not private_metadata:
            return

        import json

        try:
            meta = json.loads(private_metadata)
            ticket_id = meta.get("t")
            channel_id = meta.get("c")
            thread_ts = meta.get("ts")
        except json.JSONDecodeError:
            ticket_id = private_metadata
            channel_id = None
            thread_ts = None

        if not ticket_id:
            return

        values = payload.get("view", {}).get("state", {}).get("values", {})
        reply_content = None
        for block_id, actions in values.items():
            if "reply_content" in actions:
                reply_content = actions["reply_content"].get("value")
                break

        if not reply_content:
            return

        user_id = payload.get("user", {}).get("id")

        # 1. Fetch HubSpot user mapping to get their email signature / info
        user_mapping = (
            await self.integration_service.storage.user_mappings.fetch_single(
                {"workspace_id": integration.workspace_id, "slack_user_id": user_id}
            )
        )
        agent_name = "Support Agent"
        agent_email = None
        if user_mapping and user_mapping.hubspot_email:
            agent_email = user_mapping.hubspot_email
            agent_name = (
                user_mapping.hubspot_email.split("@")[0].replace(".", " ").title()
            )

        import time
        from datetime import datetime

        offset_hours = (
            int(time.timezone / -3600)
            if time.localtime().tm_isdst == 0
            else int(time.altzone / -3600)
        )
        sign = "+" if offset_hours > 0 else ""
        # 2. Sync reply to HubSpot
        crm_client = await self.crm.get_client(integration.workspace_id)

        # Check if the ticket is part of a Helpdesk Conversation Thread
        thread_id = await self.crm.get_ticket_thread_id(
            integration.workspace_id, ticket_id
        )

        if thread_id:
            # Inject outbound email directly into Helpdesk thread
            await self.crm.add_conversation_message(
                workspace_id=integration.workspace_id,
                thread_id=thread_id,
                content=reply_content,
                sender_email=agent_email,
                is_internal=False,
            )
        else:
            # Fallback to CRM Note
            now_str = datetime.now().strftime(f"%Y-%m-%d %H:%M GMT{sign}{offset_hours}")
            formatted_reply = f"[{now_str}] {agent_name}: {reply_content}"
            await self.crm.create_note(
                workspace_id=integration.workspace_id,
                associated_id=ticket_id,
                associated_type="ticket",
                content=formatted_reply,
                continuous=True,
            )

        # Invalidate cache
        await cast("Any", self.crm).invalidate_object_caches(
            integration.workspace_id, "ticket", ticket_id
        )

        logger.info(
            "Outbound reply sent for ticket_id=%s by user=%s", ticket_id, user_id
        )

        # 3. Post reply to Slack thread if context is available
        if channel_id and thread_ts:
            slack_channel = await messaging_service.get_slack_channel()
            slack_client = cast("Any", slack_channel).get_slack_client()

            # Use block quote for the reply content
            quoted_reply = "> " + reply_content.replace("\n", "\n> ")
            await slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"✉️ *{agent_name} replied to customer:*\n{quoted_reply}",
            )

    @interaction_handler(action_ids.TICKET_CLOSE)
    @with_slack_error_handling(action_ids.TICKET_CLOSE)
    @require_feature("ticket_sync")
    async def _handle_ticket_close(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Closes the HubSpot ticket and archives the Slack channel."""
        if not context.value or not context.user_id or not context.channel_id:
            return
        ticket_id = context.get_entity_id()
        if not ticket_id:
            logger.warning(
                "Missing ticket_id for close action: context=%s", context.value
            )
            return
        user_id = context.user_id
        channel_id = context.channel_id
        crm_client = await self.crm.get_client(integration.workspace_id)
        await crm_client.update_object(
            object_type="tickets",
            object_id=ticket_id,
            properties={"hs_pipeline_stage": "4"},
        )
        slack_channel = await messaging_service.get_slack_channel()
        slack_client = cast("SlackChannel", slack_channel).get_slack_client()
        try:
            await slack_client.chat_postMessage(
                channel=channel_id,
                text=f"🔒 Ticket closed by <@{user_id}>.",
            )

            # Audit Log
            audit = AuditService(corr_id=self.corr_id)
            await audit.log_action(
                action=action_ids.TICKET_CLOSE,
                workspace_id=integration.workspace_id,
                actor_id=user_id,
                metadata={"ticket_id": ticket_id, "channel_id": channel_id},
            )
        except SlackApiError as exc:
            if exc.response.get("error") not in ["channel_not_found", "is_archived"]:
                raise

    @interaction_handler(action_ids.TICKET_TRANSCRIPT)
    @with_slack_error_handling(action_ids.TICKET_TRANSCRIPT)
    async def _handle_ticket_transcript(  # noqa: PLR0912
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Generates and logs a conversation transcript to HubSpot."""
        ticket_id = context.get_entity_id()
        channel_id = context.channel_id
        workspace_id = integration.workspace_id

        if not ticket_id or not channel_id:
            logger.warning("Missing context for transcript: ticket=%s", ticket_id)
            return

        # 1. Resolve or Create thread mapping
        mapping = await self._ensure_thread_mapping(
            workspace_id, ticket_id, channel_id, payload
        )

        # 2. Fetch history
        channel_inst = await messaging_service.get_slack_channel()
        slack_client = cast("SlackChannel", channel_inst).get_slack_client()
        messages = await self._fetch_slack_history(
            slack_client, channel_id, mapping, payload
        )

        if not messages:
            if context.response_url:
                await messaging_service.send_via_response_url(
                    response_url=context.response_url,
                    text="⚠️ No messages found to transcript.",
                )
            return

        # 3. Format and Log
        full_transcript = await self._format_transcript(
            slack_client, messaging_service, ticket_id, messages
        )
        await self.crm.create_note(
            workspace_id=workspace_id,
            content=full_transcript,
            associated_id=ticket_id,
            associated_type="ticket",
        )

        # Clear object and AI caches so the new transcript appears immediately
        await self.crm.invalidate_object_caches(
            workspace_id=workspace_id,
            object_type="ticket",
            object_id=ticket_id,
        )
        await self.ai.invalidate_recap_cache(
            workspace_id=workspace_id, object_id=ticket_id
        )

        # 4. Success Feedback
        if context.response_url:
            await messaging_service.send_via_response_url(
                response_url=context.response_url,
                text=f"✅ Transcript successfully logged to Ticket #{ticket_id}.",
            )

    async def _ensure_thread_mapping(
        self,
        workspace_id: str,
        ticket_id: str,
        channel_id: str,
        payload: Mapping[str, Any],
    ) -> Any:
        """Ensures a thread mapping exists, creating one if necessary (Lazy Mapping)."""
        mapping = await self.integration_service.storage.get_thread_mapping(
            workspace_id=workspace_id,
            object_type="ticket",
            object_id=ticket_id,
            channel_id=channel_id,
        )
        if not mapping:
            msg_payload = payload.get("message", {})
            effective_ts = msg_payload.get("thread_ts") or msg_payload.get("ts")
            if effective_ts:
                mapping = await self.integration_service.storage.save_thread_mapping(
                    ThreadMappingRecord(
                        workspace_id=workspace_id,
                        object_type="ticket",
                        object_id=ticket_id,
                        channel_id=channel_id,
                        thread_ts=effective_ts,
                        source="search",
                    )
                )
        return mapping

    async def _fetch_slack_history(
        self,
        slack_client: Any,
        channel_id: str,
        mapping: Any,
        payload: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Fetches message history from Slack, handling dedicated vs threaded channels."""
        is_dedicated = mapping and mapping.thread_ts == "CHANNEL_ROOT"
        msg_payload = payload.get("message", {})
        target_ts = msg_payload.get("thread_ts") or msg_payload.get("ts")

        async def _do_fetch() -> list[dict[str, Any]]:
            if is_dedicated:
                resp = await slack_client.conversations_history(
                    channel=channel_id, limit=100
                )
            else:
                try:
                    resp = await slack_client.conversations_replies(
                        channel=channel_id, ts=target_ts, limit=100
                    )
                except Exception as e:
                    if "thread_not_found" in str(e):
                        resp = await slack_client.conversations_history(
                            channel=channel_id,
                            latest=target_ts,
                            inclusive=True,
                            limit=1,
                        )
                    else:
                        raise
            return resp.get("messages", [])

        try:
            return await _do_fetch()
        except Exception as e:
            if "not_in_channel" in str(e):
                logger.info("Bot not in channel %s, attempting to join...", channel_id)
                try:
                    await slack_client.conversations_join(channel=channel_id)
                    return await _do_fetch()
                except Exception as join_err:
                    logger.warning(
                        "Failed to join channel %s: %s", channel_id, join_err
                    )
                    raise ValueError(
                        "The REHA Connect bot must be explicitly invited to this channel (e.g. `/invite @REHA Connect`) to read the transcript."
                    ) from e
            logger.error("Failed to fetch Slack history: %s", e)
            raise

    async def _format_transcript(
        self,
        slack_client: Any,
        messaging_service: SlackMessagingService,
        ticket_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """Formats raw Slack messages into a professional human-readable transcript."""
        transcript_lines = [
            f"--- TICKET TRANSCRIPT (ID: {ticket_id}) ---",
            f"Generated At: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "-------------------------------------------",
            "",
        ]

        # Pre-collect unique user IDs to batch resolve names once

        unique_user_ids: set[str] = set()
        for msg in messages:
            uid = msg.get("user") or msg.get("bot_id") or "System"
            unique_user_ids.add(str(uid))

        # Resolve all names in parallel — eliminates N+1 users.info calls
        name_tasks = {
            uid: messaging_service._get_slack_user_name(slack_client, uid)
            for uid in unique_user_ids
        }
        resolved = await asyncio.gather(*name_tasks.values(), return_exceptions=True)
        user_name_cache: dict[str, str] = {
            uid: (name if isinstance(name, str) else uid)
            for uid, name in zip(name_tasks.keys(), resolved)
        }

        for msg in reversed(messages):
            bot_id = msg.get("bot_id")
            user_id = msg.get("user") or bot_id or "System"
            text = msg.get("text") or ""
            if not text and msg.get("blocks"):
                text = extract_text_from_blocks(msg.get("blocks"))

            if not text or text == "[Attachment/Rich Content]":
                continue

            # Filtering logic
            skip_patterns = [
                "New CRM update",
                "CRM Object Detail",
                "Ticket Control Panel",
                "Reminder: Use the Transcript",
                "Transcript successfully logged",
            ]
            if bot_id and any(p in text for p in skip_patterns):
                continue

            user_name = user_name_cache.get(str(user_id), str(user_id))
            if "REHA Connect" in user_name:
                continue

            ts = float(msg.get("ts", 0))
            timestamp = datetime.fromtimestamp(ts, UTC).strftime("%H:%M")
            transcript_lines.append(f"[{timestamp}] {user_name}: {text}")

        return "\n".join(transcript_lines)

    @interaction_handler(action_ids.TICKET_CLAIM)
    @with_slack_error_handling(action_ids.TICKET_CLAIM)
    @require_feature("ticket_sync")
    async def _handle_ticket_claim(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Assigns the HubSpot ticket to the claiming Slack user."""
        if not context.value or not context.user_id or not context.channel_id:
            return
        ticket_id = context.get_entity_id()
        if not ticket_id:
            return
        user_id = context.user_id
        channel_id = context.channel_id
        slack_channel = await messaging_service.get_slack_channel()
        slack_client = cast("SlackChannel", slack_channel).get_slack_client()
        user_info = await slack_client.users_info(user=user_id)
        email = user_info.get("user", {}).get("profile", {}).get("email")
        if not email:
            raise HubSpotAPIError("Could not resolve email for Slack user.")
        owners = await self.crm.get_owners(integration.workspace_id)
        hs_owner = next((o for o in owners if o.get("email") == email), None)
        if not hs_owner:
            raise HubSpotAPIError(f"No HubSpot owner found for email {email}")
        await self.crm.update_object(
            workspace_id=integration.workspace_id,
            object_type="ticket",
            object_id=ticket_id,
            properties={"hubspot_owner_id": hs_owner["id"]},
        )
        await slack_client.chat_postMessage(
            channel=channel_id,
            text=f"🙋\u200d♂️ <@{user_id}> has claimed this ticket.",
        )

        # Audit Log
        audit = AuditService(corr_id=self.corr_id)
        await audit.log_action(
            action=action_ids.TICKET_CLAIM,
            workspace_id=integration.workspace_id,
            actor_id=user_id,
            metadata={"ticket_id": ticket_id, "channel_id": channel_id},
        )


def extract_text_from_blocks(blocks: list[dict[str, Any]] | None) -> str:
    """Recursively extracts text from Slack blocks."""
    if not blocks:
        return ""
    text_parts = []
    for block in blocks:
        b_type = block.get("type")
        if b_type == "rich_text":
            for element in block.get("elements", []):
                if element.get("type") == "rich_text_section":
                    for sub_element in element.get("elements", []):
                        if "text" in sub_element:
                            text_parts.append(sub_element["text"])
        elif b_type == "section" and "text" in block:
            text_parts.append(block["text"].get("text", ""))
    return " ".join(text_parts).strip()
