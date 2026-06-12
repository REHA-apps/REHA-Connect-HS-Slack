from __future__ import annotations  # noqa: D100

import asyncio
import json
from collections.abc import Coroutine, Mapping
from typing import Any, cast

from app.connectors.slack.ui.modal_builder import ModalBuilder
from app.core.logging import get_logger
from app.db.records import IntegrationRecord
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils import action_ids
from app.utils.constants import CREATE_RECORD_CALLBACK_ID, SLACK_ERROR_ICON

from .base import (
    InteractionHandler,
    UnifiedContext,
    interaction_handler,
    require_feature,
    with_slack_error_handling,
)

logger = get_logger("object_handlers")


class ObjectViewHandler(InteractionHandler):
    @interaction_handler(action_ids.VIEW_OBJECT)
    @with_slack_error_handling("view object")
    async def _handle_view_object(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        value = context.value or ""
        channel_id = context.channel_id
        parts = value.split(":")
        if len(parts) < 3:
            logger.warning("Malformed interaction value=%s", value)
            return
        obj_type = parts[1]
        obj_id = parts[2]
        interaction_type = parts[3] if len(parts) > 3 else "channel"
        # Fetch object and engagements in parallel for accurate scoring
        tasks: list[Coroutine[Any, Any, Any]] = [
            self.crm.get_object(
                workspace_id=integration.workspace_id,
                object_type=obj_type,
                object_id=obj_id,
            )
        ]

        fetch_engagements = obj_type in ("contact", "deal", "company")
        if fetch_engagements:
            tasks.append(
                self.crm.get_object_engagements(
                    workspace_id=integration.workspace_id,
                    object_type=obj_type,
                    object_id=obj_id,
                )
            )

        it = iter(await asyncio.gather(*tasks))
        obj = next(it)
        engagements = next(it, [])

        if not obj:
            logger.warning("Could find HubSpot object type=%s id=%s", obj_type, obj_id)
            return
        is_pro = await self.integration_service.is_pro_workspace(
            integration.workspace_id
        )

        # Priority: view_id from fast-path loading modal, else trigger_id
        trigger_id = context.trigger_id
        view_id = kwargs.get("view_id")

        if not view_id and trigger_id:
            view_id = await self._show_loading(
                trigger_id, "Loading Record...", integration
            )

        try:
            pipelines = None
            if obj_type == "deal":
                hubspot_client = await self.crm.get_client(integration.workspace_id)
                pipelines = await hubspot_client.get_pipelines("deals")

            analysis = await self.ai.analyze_polymorphic(
                obj, obj_type, engagements=engagements
            )
            unified_card = messaging_service.cards.build(
                obj,
                cast(Any, analysis),
                is_pro=is_pro,
                pipelines=pipelines,
                include_actions=(interaction_type != "modal"),
            )

            # Bundle metadata to persist channel context
            metadata = json.dumps(
                {
                    "channel_id": context.channel_id,
                    "response_url": context.response_url,
                    "object_type": obj_type,
                    "object_id": obj_id,
                }
            )

            title = f"{obj_type.capitalize()} Details"

            # Determine display target: channel or modal
            if interaction_type == "channel":
                # If we have a view_id, it means we are coming from a search modal.
                # We want to update that modal with feedback and post the card to
                # the channel.
                if view_id:
                    # 1. Update the existing modal with "Sent to channel" feedback
                    feedback_blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"✅ *Details for {obj_type.capitalize()} "
                                    f"#{obj_id} have been sent to "
                                    f"the channel.*"
                                ),
                            },
                        }
                    ]
                    await self._update_modal(
                        view_id,
                        {
                            "type": "modal",
                            "title": {"type": "plain_text", "text": "Sent to Channel"},
                            "blocks": feedback_blocks,
                        },
                        title,
                        integration,
                    )

                # Ensure object type is preserved as a string for card logic
                if isinstance(obj, dict):
                    obj["type"] = obj_type

                # Determine if we should use response_url or post a new message.
                # For tickets, we force chat.postMessage to enable threading/sync.
                target_response_url = (
                    context.response_url if obj_type != "ticket" else None
                )

                # 2. Post the full rich card to the channel
                await messaging_service.send_card(
                    workspace_id=integration.workspace_id,
                    obj=obj,
                    analysis=analysis,
                    channel=channel_id,
                    is_pro=is_pro,
                    pipelines=pipelines,
                    response_url=target_response_url,
                )
            # interaction_type == "modal"
            elif view_id:
                await self._update_modal(
                    view_id,
                    unified_card,
                    title,
                    integration,
                    metadata=metadata,
                )
            elif trigger_id:
                await self._open_modal(
                    trigger_id=trigger_id,
                    view_or_card=unified_card,
                    title=title,
                    integration=integration,
                    metadata=metadata,
                )
            else:
                # Fallback to channel
                await messaging_service.send_card(
                    workspace_id=integration.workspace_id,
                    obj=obj,
                    analysis=analysis,
                    channel=channel_id,
                    is_pro=is_pro,
                    pipelines=pipelines,
                    response_url=context.response_url,
                )
        except Exception as exc:
            logger.error("Failed to build/show object modal: %s", exc)
            raise

    @interaction_handler(action_ids.POST_TO_CHANNEL)
    @with_slack_error_handling("post to channel")
    @require_feature("ticket_sync")
    async def _handle_post_to_channel(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        value = context.value or context.action_id or ""
        channel_id = context.channel_id
        parts = value.split(":")
        if len(parts) < 3:
            logger.warning("Malformed post_to_channel value=%s", value)
            return
        obj_type = parts[1]
        obj_id = parts[2]

        tasks: list[Coroutine[Any, Any, Any]] = [
            self.crm.get_object(
                workspace_id=integration.workspace_id,
                object_type=obj_type,
                object_id=obj_id,
            )
        ]

        if obj_type in ("contact", "deal", "company"):
            tasks.append(
                self.crm.get_object_engagements(
                    workspace_id=integration.workspace_id,
                    object_type=obj_type,
                    object_id=obj_id,
                )
            )

        results = await asyncio.gather(*tasks)
        obj = results[0]
        engagements = results[1] if len(results) > 1 else []

        if not obj:
            logger.warning("Could find HubSpot object type=%s id=%s", obj_type, obj_id)
            return

        is_pro = await self.integration_service.is_pro_workspace(
            integration.workspace_id
        )

        analysis = await self.ai.analyze_polymorphic(
            obj, obj_type, engagements=engagements
        )

        unified_card = messaging_service.cards.build(
            obj,
            cast(Any, analysis),
            is_pro=is_pro,
        )

        rendered = messaging_service.slack_renderer.render(unified_card)

        # Send public message to channel
        resp = await messaging_service.send_message(
            workspace_id=integration.workspace_id,
            channel=channel_id,
            blocks=rendered["blocks"],
            text=f"{obj_type.capitalize()} #{obj_id}",
        )

        sent_ts = str(resp.get("ts")) if resp and resp.get("ts") else None

        # Persist thread mapping for tickets
        if obj_type == "ticket" and is_pro and sent_ts and channel_id:
            from app.db.records import ThreadMappingRecord

            await self.integration_service.storage.save_thread_mapping(
                ThreadMappingRecord(
                    workspace_id=integration.workspace_id,
                    object_type=obj_type,
                    object_id=obj_id,
                    channel_id=channel_id,
                    thread_ts=sent_ts,
                    source="manual_post",
                )
            )

    @interaction_handler(action_ids.SELECT_OBJECT, action_ids.SELECT_OBJECT_TYPE)
    @with_slack_error_handling("update creation modal")
    async def _handle_select_object_type(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Handle selection of object type in creation modal."""
        actions = payload.get("actions", [])
        if not actions:
            return
        selected_option = actions[0].get("selected_option")
        if not selected_option:
            return
        object_type = selected_option.get("value")
        if not object_type:
            return

        view_id = payload.get("view", {}).get("id")
        if not view_id:
            logger.warning("No view_id found in select_object_type payload")
            return
        private_metadata = payload.get("view", {}).get("private_metadata", "{}")

        logger.info(
            "Updating creation modal: view_id=%s object_type=%s workspace_id=%s",
            view_id,
            object_type,
            integration.workspace_id,
        )

        hubspot_client = await self.crm.get_client(integration.workspace_id)
        pipelines = None
        owners = None

        if object_type in ("deal", "ticket"):
            pipelines, owners = await asyncio.gather(
                hubspot_client.get_pipelines(object_type + "s"),
                hubspot_client.get_owners(),
            )
        elif object_type in ("task", "contact", "company"):
            owners = await hubspot_client.get_owners()

        modals = ModalBuilder()
        modal = modals.build_creation_modal(
            object_type=object_type,
            callback_id=CREATE_RECORD_CALLBACK_ID,
            pipelines=pipelines,
            owners=owners,
        )

        # Merge metadata
        if private_metadata:
            modal["private_metadata"] = private_metadata

        await self._update_modal(
            view_id=view_id,
            view_or_card=modal,
            title=f"Create {object_type.title()}",
            integration=integration,
        )

    @interaction_handler(action_ids.VIEW_CONTACT_COMPANY)
    @with_slack_error_handling("fetch contact's company")
    async def _handle_view_contact_company(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        value = context.value or ""
        channel_id = context.channel_id
        parts = value.split(":")
        if len(parts) < 2:
            logger.warning("Malformed view_contact_company value=%s", value)
            return

        contact_id = parts[1]

        # Priority: view_id from fast-path loading modal, else trigger_id
        trigger_id = context.trigger_id
        view_id = kwargs.get("view_id")

        if not view_id and trigger_id:
            view_id = await self._show_loading(
                trigger_id, "Associated Companies", integration
            )

        # Fetch associations and parent for URL
        companies_task = self.crm.get_associated_objects(
            workspace_id=integration.workspace_id,
            from_object_type="contact",
            object_id=contact_id,
            to_object_type="company",
        )
        contact_task = self.crm.get_object(
            workspace_id=integration.workspace_id,
            object_type="contact",
            object_id=contact_id,
        )
        results_gather = await asyncio.gather(companies_task, contact_task)
        companies, contact = cast(
            "tuple[list[dict[str, Any]], dict[str, Any] | None]", results_gather
        )
        parent_url = contact.get("hs_url") if contact else None
        is_pro = await self.integration_service.is_pro_workspace(
            integration.workspace_id
        )

        metadata = json.dumps(
            {
                "channel_id": context.channel_id,
                "response_url": context.response_url,
            }
        )

        cards = messaging_service.cards
        if not companies:
            card = cards.build_empty("No companies found for this contact.")
        else:
            # Always use list view for associated objects in modals for consistency
            card = cards.build_companies_list(
                companies, is_pro=is_pro, parent_url=parent_url
            )

        success = False
        if view_id:
            success = await self._update_modal(
                view_id,
                card,
                "Associated Companies",
                integration,
                metadata=metadata,
            )
        if not success:
            rendered = messaging_service.slack_renderer.render(card)
            response_url = cast(str, context.response_url)
            if response_url:
                await messaging_service.send_via_response_url(
                    response_url=response_url,
                    text="Associated Companies",
                    blocks=rendered["blocks"],
                )
            else:
                await messaging_service.send_message(
                    workspace_id=integration.workspace_id,
                    text="Associated Companies",
                    blocks=rendered["blocks"],
                    channel=channel_id,
                )

    async def _show_associated_deals(
        self,
        from_object_type: str,
        object_id: str,
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        view_id: str | None,
    ) -> None:
        trigger_id = context.trigger_id

        if not view_id and trigger_id:
            view_id = await self._show_loading(
                trigger_id, "Associated Deals", integration
            )

        # Fetch associations and parent for URL
        deals_task = self.crm.get_associated_objects(
            workspace_id=integration.workspace_id,
            from_object_type=from_object_type,
            object_id=object_id,
            to_object_type="deal",
        )
        parent_task = self.crm.get_object(
            workspace_id=integration.workspace_id,
            object_type=from_object_type,
            object_id=object_id,
        )
        results_gather = await asyncio.gather(deals_task, parent_task)
        deals, parent_obj = cast(
            "tuple[list[dict[str, Any]], dict[str, Any] | None]", results_gather
        )
        parent_url = parent_obj.get("hs_url") if parent_obj else None

        cards = messaging_service.cards
        is_pro = await self.integration_service.is_pro_workspace(
            integration.workspace_id
        )
        if not deals:
            card = cards.build_empty(f"No deals found for this {from_object_type}.")
        else:
            # Always use list view for associated objects in modals for consistency
            card = cards.build_deals_list(deals, is_pro=is_pro, parent_url=parent_url)
        import json

        metadata = json.dumps(
            {
                "channel_id": context.channel_id,
                "response_url": context.response_url,
            }
        )
        success = False
        if view_id:
            success = await self._update_modal(
                view_id,
                card,
                "Associated Deals",
                integration,
                metadata=metadata,
            )
        if not success:
            rendered = messaging_service.slack_renderer.render(card)
            response_url = cast(str, context.response_url)
            if response_url:
                await messaging_service.send_via_response_url(
                    response_url=response_url,
                    text="Associated Deals",
                    blocks=rendered["blocks"],
                )
            else:
                await messaging_service.send_message(
                    workspace_id=integration.workspace_id,
                    text="Associated Deals",
                    blocks=rendered["blocks"],
                    channel=context.channel_id,
                )

    @interaction_handler(action_ids.VIEW_CONTACT_DEALS)
    @with_slack_error_handling("fetch contact's deals")
    async def _handle_view_contact_deals(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        value = context.value or ""
        parts = value.split(":")
        if len(parts) < 2:
            logger.warning("Malformed view_contact_deals value=%s", value)
            return
        contact_id = parts[1]
        await self._show_associated_deals(
            "contact",
            contact_id,
            integration,
            messaging_service,
            context,
            kwargs.get("view_id"),
        )

    @interaction_handler(action_ids.VIEW_COMPANY_DEALS)
    @with_slack_error_handling("fetch associated deals")
    async def _handle_view_company_deals(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        value = context.value or ""
        parts = value.split(":")
        if len(parts) < 2:
            logger.warning("Malformed view_company_deals value=%s", value)
            return
        company_id = parts[1]
        await self._show_associated_deals(
            "company",
            company_id,
            integration,
            messaging_service,
            context,
            kwargs.get("view_id"),
        )

    @interaction_handler(action_ids.VIEW_DEALS)
    @with_slack_error_handling("fetch associated deals")
    async def _handle_view_deals(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        value = context.value or ""
        parts = value.split(":")
        if len(parts) < 2:
            logger.warning("Malformed view_deals value=%s", value)
            return
        contact_id = parts[1]
        await self._show_associated_deals(
            "contact",
            contact_id,
            integration,
            messaging_service,
            context,
            kwargs.get("view_id"),
        )

    @interaction_handler(action_ids.VIEW_CONTACTS)
    @with_slack_error_handling("fetch associated contacts")
    async def _handle_view_contacts(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        value = context.value or ""
        parts = value.split(":")
        if len(parts) < 2:
            logger.warning("Malformed view_contacts value=%s", value)
            return
        company_id = parts[1]
        # Priority: view_id from fast-path loading modal, else trigger_id
        trigger_id = context.trigger_id
        view_id = kwargs.get("view_id")

        if not view_id and trigger_id:
            view_id = await self._show_loading(
                trigger_id, "Associated Contacts", integration
            )

        # Fetch associations and parent for URL
        contacts_task = self.crm.get_associated_objects(
            workspace_id=integration.workspace_id,
            from_object_type="company",
            object_id=company_id,
            to_object_type="contact",
        )
        company_task = self.crm.get_object(
            workspace_id=integration.workspace_id,
            object_type="company",
            object_id=company_id,
        )
        results_gather = await asyncio.gather(contacts_task, company_task)
        contacts, company = cast(
            "tuple[list[dict[str, Any]], dict[str, Any] | None]", results_gather
        )
        parent_url = company.get("hs_url") if company else None
        cards = messaging_service.cards
        is_pro = await self.integration_service.is_pro_workspace(
            integration.workspace_id
        )
        if not contacts:
            card = cards.build_empty("No contacts found for this company.")
        else:
            # Always use list view for associated objects in modals for consistency
            card = cards.build_contacts_list(
                contacts, is_pro=is_pro, parent_url=parent_url
            )
        metadata = json.dumps(
            {
                "channel_id": context.channel_id,
                "response_url": context.response_url,
            }
        )
        success = False
        if view_id:
            success = await self._update_modal(
                view_id,
                card,
                "Associated Contacts",
                integration,
                metadata=metadata,
            )
        if not success:
            rendered = messaging_service.slack_renderer.render(card)
            response_url = cast(str, context.response_url)
            if response_url:
                await messaging_service.send_via_response_url(
                    response_url=response_url,
                    text="Associated Contacts",
                    blocks=rendered["blocks"],
                )
            else:
                await messaging_service.send_message(
                    workspace_id=integration.workspace_id,
                    text="Associated Contacts",
                    blocks=rendered["blocks"],
                    channel=context.channel_id,
                )

    @interaction_handler(action_ids.VIEW_CONTACT_MEETINGS)
    async def _handle_view_contact_meetings(  # noqa: PLR0912
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Fetch and display meetings associated with a contact."""
        value = context.value or ""
        channel_id = context.channel_id
        parts = value.split(":")
        if len(parts) < 2:
            logger.warning("Malformed view_contact_meetings value=%s", value)
            return
        try:
            contact_id = parts[1]
            # Priority: view_id from fast-path loading modal, else trigger_id
            trigger_id = context.trigger_id
            view_id = kwargs.get("view_id")

            if not view_id and trigger_id:
                view_id = await self._show_loading(
                    trigger_id, "Associated Meetings", integration
                )

            # Fetch associations and parent for URL
            meetings_task = self.crm.get_contact_meetings(
                workspace_id=integration.workspace_id, contact_id=contact_id
            )
            contact_task = self.crm.get_object(
                workspace_id=integration.workspace_id,
                object_type="contact",
                object_id=contact_id,
            )
            results_gather = await asyncio.gather(meetings_task, contact_task)
            meetings, contact = cast(
                "tuple[list[dict[str, Any]], dict[str, Any] | None]", results_gather
            )
            parent_url = contact.get("hs_url") if contact else None
            cards = messaging_service.cards
            if not meetings:
                card = cards.build_empty("No meetings found for this contact.")
            else:
                from app.utils.transformers import to_datetime

                is_pro = await self.integration_service.is_pro_workspace(
                    integration.workspace_id
                )
                meetings.sort(
                    key=lambda x: to_datetime(
                        x.get("properties", {}).get("hs_meeting_start_time")
                    ),
                    reverse=True,
                )
                # Note: Build card will handle slicing to latest 5
                card = cards.build_meetings_list(
                    meetings, is_pro=is_pro, parent_url=parent_url
                )
            metadata = json.dumps(
                {
                    "channel_id": context.channel_id,
                    "response_url": context.response_url,
                }
            )
            if view_id:
                await self._update_modal(
                    view_id,
                    card,
                    "Associated Meetings",
                    integration,
                    metadata=metadata,
                )
            elif trigger_id:
                await self._open_modal(
                    trigger_id=trigger_id,
                    view_or_card=card,
                    title="Associated Meetings",
                    integration=integration,
                    metadata=metadata,
                )
            else:
                rendered = messaging_service.slack_renderer.render(card)
                response_url = cast(str, context.response_url)
                await messaging_service.send_via_response_url(
                    response_url=response_url,
                    text="Contact's Meetings",
                    blocks=rendered["blocks"],
                )
        except Exception as exc:
            logger.exception("Failed to view contact meetings")
            response_url = context.response_url
            if response_url:
                await messaging_service.send_via_response_url(
                    response_url=response_url,
                    text=(
                        f"{SLACK_ERROR_ICON} Failed to fetch contact's "
                        f"meetings: {str(exc)}"
                    ),
                )
            else:
                user_id = str(kwargs.get("payload", {}).get("user", {}).get("id", ""))
                if user_id:
                    client = await self.integration_service.get_slack_client(
                        integration
                    )
                    if channel_id:
                        await client.chat_postEphemeral(
                            channel=channel_id,
                            user=user_id,
                            text=(
                                f"{SLACK_ERROR_ICON} Failed to fetch contact's "
                                f"meetings: {str(exc)}"
                            ),
                        )
                    else:
                        try:
                            await client.chat_postMessage(
                                channel=user_id,
                                text=(
                                    f"{SLACK_ERROR_ICON} Failed to fetch contact's "
                                    f"meetings: {str(exc)}"
                                ),
                            )
                        except Exception:
                            pass


class SuggestionHandler(InteractionHandler):
    @interaction_handler(action_ids.ASSOCIATION_SEARCH)
    async def _handle_association_search(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Handles real-time search suggestions for the Association dropdown."""
        action_id = payload.get("action_id")
        value = payload.get("value", "")
        if action_id != "association_search":
            return {"options": []}
        logger.info("Performing association search for query: %s", value)
        if not value or not value.strip():
            return {"options": []}
        try:
            hubspot_client = await self.crm.get_client(integration.workspace_id)

            # Use HubSpot's smart "query" search — matches across all indexed
            # fields (name, email, domain, etc.), same as the HubSpot UI search.
            search_tasks: list[Coroutine[Any, Any, Any]] = [
                hubspot_client.search_objects(
                    "contacts",
                    query_string=value,
                    properties=["firstname", "lastname", "email"],
                    limit=5,
                ),
                hubspot_client.search_objects(
                    "deals",
                    query_string=value,
                    properties=["dealname"],
                    limit=5,
                ),
                hubspot_client.search_objects(
                    "companies",
                    query_string=value,
                    properties=["name", "domain"],
                    limit=5,
                ),
            ]
            search_results = await asyncio.gather(*search_tasks)
            results_tuple = cast(
                "tuple[list[dict[str, Any]], ...]",
                search_results,
            )
            contacts_results, deals_results, companies_results = results_tuple
            options = []
            for obj in contacts_results:
                props = obj["properties"]
                name = (
                    f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
                    or props.get("email", "Unknown")
                )
                options.append(
                    {
                        "text": {"type": "plain_text", "text": f"👤 Contact: {name}"},
                        "value": f"contact:{obj['id']}",
                    }
                )
            for obj in deals_results:
                name = obj["properties"].get("dealname", "Unnamed Deal")
                options.append(
                    {
                        "text": {"type": "plain_text", "text": f"💰 Deal: {name}"},
                        "value": f"deal:{obj['id']}",
                    }
                )
            for obj in companies_results:
                name = obj["properties"].get("name", "Unnamed Company")
                options.append(
                    {
                        "text": {"type": "plain_text", "text": f"🏢 Company: {name}"},
                        "value": f"company:{obj['id']}",
                    }
                )
            return {"options": list(options)[:25]}
        except Exception:
            logger.exception("Failed to fetch search suggestions")
            return {"options": []}
