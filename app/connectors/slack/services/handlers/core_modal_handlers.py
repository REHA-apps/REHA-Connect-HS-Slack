from __future__ import annotations  # noqa: D100

import json
from collections.abc import Mapping
from typing import Any

from app.connectors.slack.ui.modal_builder import ModalBuilder
from app.core.logging import get_logger
from app.db.records import IntegrationRecord
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils import action_ids
from app.utils.constants import (
    CREATE_RECORD_CALLBACK_ID,
    SLACK_ERROR_ICON,
)

from .base import (
    InteractionHandler,
    UnifiedContext,
    interaction_handler,
    require_feature,
    slack_error_handling,
)
from .task_handlers import handle_task_creation
from .ticket_handlers import handle_ticket_creation

# HubSpot-defined association type IDs for Lead object (from → to)
_LEAD_TO_PRIMARY_CONTACT_TYPE_ID = 578  # Lead → Primary Contact
_LEAD_TO_PRIMARY_COMPANY_TYPE_ID = 610  # Lead → Primary Company

logger = get_logger("core_modal_handlers")


def _format_hubspot_error(exc: Exception, object_type: str) -> str:
    """Convert a raw HubSpot API exception into a readable Slack message."""
    raw = str(exc)

    # Attempt to extract the embedded JSON payload from the error message
    try:
        # HubSpotAPIError messages look like: "HubSpot API error: {...}"
        json_start = raw.find("{")
        if json_start != -1:
            payload = json.loads(raw[json_start:])
            category = payload.get("category", "")

            if category == "MISSING_SCOPES":
                return (
                    f":lock: *Permission Required*\n"
                    f"REHA Connect doesn't have permission to create *{object_type.title()}* records in your HubSpot portal.\n\n"
                    f"*Fix:* Reconnect the app to grant the missing permissions:"
                    f" go to *Settings → Integrations → REHA Connect* and click *Reconnect*."
                )

            # Generic HubSpot API error — show just the human message, not the JSON blob
            human_message = payload.get("message", "")
            if human_message:
                # Catch plan tier restrictions for the Lead object
                if object_type == "lead" and (
                    "access" in human_message.lower()
                    or "enabled" in human_message.lower()
                ):
                    return (
                        ":information_source: *HubSpot Plan Restriction*\n"
                        "Your HubSpot plan does not seem to include access to the *Lead* object.\n\n"
                        "The Lead object is a premium feature (Sales Hub Professional+). "
                        "You can create a *Contact* or *Deal* instead."
                    )

                return f":warning: *Failed to create {object_type.title()}*\n{human_message}"
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback for non-JSON errors
    return f":warning: *Failed to create {object_type.title()}*\nAn unexpected error occurred. Please try again or contact support."


class CoreModalHandlers(InteractionHandler):
    @interaction_handler(action_ids.REASSIGN_OWNER_SUBMISSION)
    @require_feature("reassign_owner")
    async def _handle_reassign_owner_submission(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        view = payload.get("view", {})
        metadata = view.get("private_metadata", "")
        try:
            meta = json.loads(metadata)
            object_type = meta.get("object_type")
            object_id = meta.get("object_id")
            meta.get("channel_id")
            response_url = meta.get("response_url")
        except Exception:
            PARTS_MIN_LEN = 2
            parts = str(metadata).split(":")
            if len(parts) >= PARTS_MIN_LEN:
                object_type, object_id = (parts[0], parts[1])
            else:
                object_type, object_id = ("deal", parts[0])
            response_url = None
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)
        new_owner_id = properties.get("hubspot_owner_id", "")
        async with slack_error_handling(
            "reassign owner", payload, messaging_service, response_url=response_url
        ):
            target_type = (
                object_type if object_type in ("contact", "lead", "task") else "deal"
            )
            await self.crm.update_object(
                workspace_id=integration.workspace_id,
                object_type=target_type,
                object_id=object_id,
                properties={"hubspot_owner_id": new_owner_id},
            )

    @interaction_handler(CREATE_RECORD_CALLBACK_ID, "create_hubspot_record_message")
    async def _handle_create_record_interaction(  # noqa: PLR0912, PLR0915
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Handle both the Shortcut opening and the Record Submission."""
        interaction_type = payload.get("type", "")

        # 1. Opening the generic "Choose Type" modal from a shortcut
        if interaction_type in {"shortcut", "message_action"}:
            trigger_id = context.trigger_id
            if not trigger_id:
                return
            modal = ModalBuilder().build_type_selection(CREATE_RECORD_CALLBACK_ID)
            client = await self.integration_service.get_slack_client(integration)
            await client.views_open(trigger_id=trigger_id, view=modal)
            return

        # 2. Submission of the creation form
        view = payload.get("view", {})
        callback_id = view.get("callback_id", "")
        parts = callback_id.split(":")
        if len(parts) < 2:
            return
        object_type = parts[1]
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)
        association = properties.pop("association_search", None)
        metadata = view.get("private_metadata")
        channel_id = None
        response_url = None
        if metadata:
            try:
                meta = json.loads(metadata)
                channel_id = meta.get("channel_id")
                response_url = meta.get("response_url")
            except Exception:
                pass

        if object_type == "task":
            await self._handle_task_submission(
                integration=integration,
                properties=properties,
                association=association,
                messaging_service=messaging_service,
                response_url=response_url,
                channel_id=channel_id,
                user_id=context.user_id,
                context=context,
            )
            return

        if object_type == "ticket":
            await self._handle_ticket_submission(
                integration=integration,
                properties=properties,
                association=association,
                messaging_service=messaging_service,
                user_id=context.user_id,
                context=context,
            )
            return

        if object_type == "lead":
            await self._handle_lead_submission(
                integration=integration,
                properties=properties,
                association=association,
                messaging_service=messaging_service,
                user_id=context.user_id,
                context=context,
            )
            return

        hubspot_client = await self.crm.get_client(integration.workspace_id)
        try:
            result = await hubspot_client.create_object(object_type, properties)
            object_id = result.get("id")
            logger.debug("Created %s: %s", object_type, object_id)

            if object_id:
                from app.domains.crm.notification_service import _recent_notifications

                await _recent_notifications.set(
                    f"notif_debounce:{integration.workspace_id}:{object_type}:{object_id}",
                    True,
                )
        except Exception as exc:
            logger.exception("Failed to create object")
            user_id = context.user_id
            if user_id:
                client = await self.integration_service.get_slack_client(integration)
                await client.chat_postMessage(
                    channel=user_id,
                    text=_format_hubspot_error(exc, object_type),
                )

    async def _handle_lead_submission(
        self,
        integration: IntegrationRecord,
        properties: dict[str, Any],
        association: str | None,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        user_id: str | None,
    ) -> None:
        """Create a HubSpot Lead with a required primary-contact association.

        HubSpot's Lead object mandates either a LEAD_TO_PRIMARY_CONTACT or
        LEAD_TO_PRIMARY_COMPANY association at creation time (VALIDATION_ERROR
        otherwise).  We resolve this by finding-or-creating a contact from the
        submitted email, then passing the association in the atomic create call.
        If an explicit ``association`` was selected in the modal (type:id), we
        use that instead (or additionally) to satisfy the requirement.
        """
        hubspot_client = await self.crm.get_client(integration.workspace_id)
        hs_associations: list[dict[str, Any]] = []

        # 1. Try to fulfil the requirement from the optional association picker
        if association and ":" in association:
            assoc_type, assoc_id = association.split(":", 1)
            if assoc_type == "contact":
                hs_associations.append(
                    {
                        "to": {"id": assoc_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": _LEAD_TO_PRIMARY_CONTACT_TYPE_ID,
                            }
                        ],
                    }
                )
            elif assoc_type == "company":
                hs_associations.append(
                    {
                        "to": {"id": assoc_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": _LEAD_TO_PRIMARY_COMPANY_TYPE_ID,
                            }
                        ],
                    }
                )

        # 2. If no contact/company association yet, resolve from the email field
        if not hs_associations:
            email = properties.get("email", "").strip()
            if email:
                try:
                    firstname = properties.get("firstname", "")
                    lastname = properties.get("lastname", "")
                    full_name = " ".join(filter(None, [firstname, lastname])) or None
                    contact_id = await self.crm.find_or_create_contact_by_email(
                        integration.workspace_id, email, full_name
                    )
                    hs_associations.append(
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": _LEAD_TO_PRIMARY_CONTACT_TYPE_ID,
                                }
                            ],
                        }
                    )
                    logger.info(
                        "Resolved contact_id=%s for lead email=%s",
                        contact_id,
                        email,
                    )
                except Exception as contact_err:
                    logger.warning(
                        "Could not resolve contact for lead email=%s: %s",
                        email,
                        contact_err,
                    )

        if not hs_associations:
            # Cannot satisfy HubSpot's requirement — surface a clear error
            if user_id:
                client = await self.integration_service.get_slack_client(integration)
                await client.chat_postMessage(
                    channel=user_id,
                    text=(
                        ":warning: *Failed to create Lead*\n"
                        "A contact or company association is required. "
                        "Please provide an email address or use the "
                        "*Associate with Record* field to link to an existing contact or company."
                    ),
                )
            return

        try:
            # Strip email, firstname, lastname from lead properties — leads don't
            # have these properties; they live on the associated contact.
            # Instead, HubSpot leads use `hs_lead_name`.
            lead_properties = {
                k: v
                for k, v in properties.items()
                if k not in ("email", "firstname", "lastname")
            }

            # Construct hs_lead_name if not provided
            if "hs_lead_name" not in lead_properties:
                fname = properties.get("firstname", "")
                lname = properties.get("lastname", "")
                lead_name = " ".join(filter(None, [fname, lname]))
                if lead_name:
                    lead_properties["hs_lead_name"] = lead_name

            result = await hubspot_client.create_object(
                "lead", lead_properties, associations=hs_associations
            )
            object_id = result.get("id")
            logger.debug("Created lead: %s", object_id)
            if object_id:
                from app.domains.crm.notification_service import _recent_notifications

                await _recent_notifications.set(
                    f"notif_debounce:{integration.workspace_id}:lead:{object_id}",
                    True,
                )
        except Exception as exc:
            logger.exception("Failed to create lead")
            if user_id:
                client = await self.integration_service.get_slack_client(integration)
                await client.chat_postMessage(
                    channel=user_id,
                    text=_format_hubspot_error(exc, "lead"),
                )

    async def _handle_task_submission(
        self,
        integration: IntegrationRecord,
        properties: dict[str, Any],
        association: str | None,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        response_url: str | None,
        channel_id: str | None,
        user_id: str | None,
    ) -> None:
        """Delegate task creation to the TaskHandlers domain function."""
        await handle_task_creation(
            crm=self.crm,
            integration=integration,
            properties=properties,
            association=association,
        )

    async def _handle_ticket_submission(
        self,
        integration: IntegrationRecord,
        properties: dict[str, Any],
        association: str | None,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        user_id: str,
    ) -> None:
        """Delegate CRM ticket creation to the SupportHandlers domain function."""
        try:
            await handle_ticket_creation(
                crm=self.crm,
                integration_service=self.integration_service,
                integration=integration,
                properties=properties,
                association=association,
                messaging_service=messaging_service,
                user_id=user_id,
            )
        except Exception as exc:
            logger.exception("Ticket submission failed")
            if user_id:
                try:
                    client = await self.integration_service.get_slack_client(
                        integration
                    )
                    await client.chat_postMessage(
                        channel=user_id,
                        text=f"{SLACK_ERROR_ICON} Failed to create ticket: {str(exc)}",
                    )
                except Exception:
                    pass

    @interaction_handler(action_ids.REASSIGN_OWNER)
    async def _handle_open_reassign_modal(
        self,
        value: str,
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        trigger_id: str | None,
        **kwargs: Any,
    ) -> None:
        """Fetch owners and open reassign modal."""
        parts = value.split(":")
        if len(parts) < 2:
            return
        PARTS_OBJECT_ID_INDEX = 2
        PARTS_TYPE_INDEX = 1
        PARTS_REQ_LEN = 3
        if len(parts) >= PARTS_REQ_LEN:
            obj_type = parts[PARTS_TYPE_INDEX]
            object_id = parts[PARTS_OBJECT_ID_INDEX]
        else:
            obj_type = "deal"
            object_id = parts[1]
        if not trigger_id:
            return
        view_id = await self._show_loading(trigger_id, "Loading Owners...", integration)
        try:
            hubspot_client = await self.crm.get_client(integration.workspace_id)
            owners = await hubspot_client.get_owners()
            metadata = context.build_metadata(object_type=obj_type, object_id=object_id)
            modal = messaging_service.cards.build_reassign_modal(
                f"{obj_type}:{object_id}", owners, metadata=metadata
            )
            if view_id:
                await self._update_modal(view_id, modal, "Reassign Owner", integration)
            else:
                await self._open_modal(trigger_id, modal, "Reassign Owner", integration)
        except Exception as exc:
            logger.exception("Failed to open reassign modal")
            response_url = context.response_url
            if response_url:
                await messaging_service.send_via_response_url(
                    response_url=response_url,
                    text=f"❌ Failed to open reassign modal: {str(exc)}",
                )
