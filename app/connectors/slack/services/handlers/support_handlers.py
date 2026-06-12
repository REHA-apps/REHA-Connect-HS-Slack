from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from app.core.logging import get_logger
from app.db.records import IntegrationRecord
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils import action_ids
from app.utils.constants import (
    SLACK_ERROR_ICON,
    SLACK_SUCCESS_ICON,
)

from .base import (
    InteractionHandler,
    UnifiedContext,
    interaction_handler,
)

logger = get_logger("support_handlers")


class SupportHandlers(InteractionHandler):
    @interaction_handler(action_ids.OPEN_SUPPORT_TICKET_MODAL)
    async def _handle_open_support_ticket_modal(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Handles opening the contact support modal."""
        if not context.view_id:
            logger.error("Missing view_id for support modal")
            return

        modal = messaging_service.cards.build_support_ticket_modal(
            metadata=context.private_metadata
        )
        client = await self.integration_service.get_slack_client(integration)
        await client.views_update(view_id=context.view_id, view=modal)

    @interaction_handler(action_ids.SUPPORT_TICKET_SUBMISSION)
    async def _handle_support_ticket_submission(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Processes the support ticket modal submission and creates a ticket
        in the developer's support portal.
        """
        view = payload.get("view", {})
        user_id = context.user_id

        if not user_id:
            logger.error("Missing user_id in support ticket submission")
            return

        # 1. Extract form values
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)
        hs_associations = None

        # 2. Add user context from Slack
        real_name = "Unknown Slack User"
        email = "no-email@slack.com"
        try:
            import asyncio

            slack_client, support_client = await asyncio.gather(
                self.integration_service.get_slack_client(integration),
                self.crm.get_support_client(),
            )
            user_info = await slack_client.users_info(user=user_id)

            if user_info.get("ok"):
                user = user_info.get("user", {})
                profile = user.get("profile", {})
                real_name = profile.get("real_name") or user.get(
                    "name", "Unknown Slack User"
                )  # noqa: E501
                email = profile.get("email") or "no-email@slack.com"

                # Append user details to description
                current_description = properties.get("content", "")
                properties["content"] = (
                    f"{current_description}\n\n"
                    f"--- Requester Context ---\n"
                    f"Slack Name: {real_name}\n"
                    f"Slack Email: {email}\n"
                    f"Slack User ID: {user_id}\n"
                    f"Workspace ID: {integration.workspace_id}"
                )
                # Map internal properties
                properties["hs_ticket_priority"] = properties.get(
                    "hs_ticket_priority", "medium"
                ).upper()

            # 4. Create HubSpot Support Ticket directly in REHA portal
            try:
                # 4.1 Sync Contact to Support Portal (Search by email)
                contact_results = await support_client.search_objects(
                    "contacts", query_string=email, properties=["email"]
                )

                if contact_results:
                    support_contact_id = contact_results[0]["id"]
                else:
                    name_parts = real_name.split(" ", 1)
                    new_contact = await support_client.create_object(
                        "contacts",
                        {
                            "email": email,
                            "firstname": name_parts[0],
                            "lastname": name_parts[1] if len(name_parts) > 1 else "",
                            "hs_lead_status": "NEW",
                        },
                    )
                    support_contact_id = new_contact["id"]

                # 4.2 Create Support Ticket
                ticket_props = {
                    "subject": f"Slack Support: {properties.get('subject', 'No Subject')}",
                    "content": properties.get("content", ""),
                    "hs_pipeline": "0",  # Support Pipeline
                    "hs_pipeline_stage": "1",  # New
                    "hs_ticket_priority": properties.get(
                        "hs_ticket_priority", "MEDIUM"
                    ).upper(),
                    "hs_ticket_category": properties.get(
                        "hs_ticket_category", "GENERAL"
                    ).upper(),
                }

                # Atomic association to the requester contact
                hs_associations = [
                    {
                        "to": {"id": support_contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 16,  # ticket_to_contact
                            }
                        ],
                    }
                ]

                ticket = await support_client.create_object(
                    "tickets", ticket_props, associations=hs_associations
                )
                ticket_id = ticket["id"]
                logger.info("Created support ticket #%s in REHA portal", ticket_id)

                # 5. Notify user of success
                await slack_client.chat_postMessage(
                    channel=user_id,
                    text=(
                        f"{SLACK_SUCCESS_ICON} *Support Ticket Created!* (Ref: #{ticket_id})\n"
                        "Your request has been routed directly to our HubSpot Helpdesk. "
                        "One of our engineers will reach out to you shortly."
                    ),
                )
            except Exception as hs_exc:
                logger.error(
                    "Failed to create native HubSpot support ticket: %s", hs_exc
                )
                # Fallback to Email if HubSpot fails (Legacy Path)
                from app.domains.common.email_service import EmailService

                email_service = EmailService()
                await email_service.send_support_email(
                    subject=f"Slack Support [FALLBACK]: {properties.get('subject', 'No Subject')}",
                    content=properties.get("content", ""),
                    from_email=email,
                    from_name=real_name,
                    to_email="support@rehaapps.com",
                    metadata={
                        "Slack User ID": user_id,
                        "Workspace ID": integration.workspace_id,
                        "Priority": properties.get("hs_ticket_priority", "MEDIUM"),
                    },
                )

                await slack_client.chat_postMessage(
                    channel=user_id,
                    text=(
                        f"{SLACK_SUCCESS_ICON} *Support Request Sent!* \n"
                        "Your message has been delivered to our support team. "
                        "We'll review it and get back to you via email as soon as possible."
                    ),
                )
        except Exception as exc:
            logger.exception("Support ticket creation failed")
            try:
                slack_client = await self.integration_service.get_slack_client(
                    integration
                )  # noqa: E501
                await slack_client.chat_postMessage(
                    channel=user_id,
                    text=f"{SLACK_ERROR_ICON} *Submission Failed:* {str(exc)}",
                )
            except Exception:
                pass
