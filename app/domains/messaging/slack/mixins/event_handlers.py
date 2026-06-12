from __future__ import annotations

import asyncio
import re
from collections.abc import Coroutine, Mapping
from typing import TYPE_CHECKING, Any, cast

from slack_sdk.errors import SlackApiError

from app.connectors.slack.slack_channel import SlackChannel
from app.connectors.slack.utils import (
    extract_links_from_blocks,
    is_already_unfurled,
    mark_unfurled,
)
from app.core.config import settings
from app.core.exceptions import IntegrationNotFoundError
from app.core.logging import get_logger, run_task_with_context
from app.db.records import (
    IntegrationRecord,
    Provider,
    WorkspaceRecord,
)
from app.domains.crm.hubspot.ghosting_monitor import GhostingMonitor
from app.domains.crm.integration_service import IntegrationService
from app.utils.helpers import normalize_object_type
from app.utils.sqs_helpers import publish_to_sqs

logger = get_logger("slack.messaging")


class SlackEventsMixin:
    """Mixin for Slack messaging capabilities."""

    if TYPE_CHECKING:
        from app.connectors.slack.slack_renderer import SlackRenderer
        from app.domains.ai.service import AIService
        from app.domains.crm.service import CRMService
        from app.domains.crm.ui import CardBuilder

        corr_id: str
        integration_service: IntegrationService
        slack_integration: IntegrationRecord | None
        cards: CardBuilder
        slack_renderer: SlackRenderer
        crm: CRMService
        ai: AIService

        async def get_slack_channel(self) -> Any: ...
        async def send_message(self, **kwargs: Any) -> Any: ...
        async def _initialize_ticket_thread(
            self,
            *,
            workspace_id: str,
            object_id: str,
            channel: str,
            sent_ts: str,
        ) -> None: ...
        async def _resolve_thread_target(
            self,
            workspace_id: str,
            channel: str,
            thread_ts: str | None,
            full_context: str | None = None,
        ) -> tuple[str | None, str | None, str | None, str | None]: ...
        async def _get_slack_user_name(self, client: Any, user: str) -> str: ...
        async def _resolve_channel(
            self,
            workspace_id: str,
            channel_id: str | None,
            obj: Mapping[str, Any] | None = None,
            is_system_alert: bool = False,
        ) -> str | None: ...

    async def on_agent_reply(self, workspace_id: str, thread_ts: str) -> None:
        """Slack override: notifies GhostingMonitor when an agent replies.

        This keeps GhostingMonitor (a HubSpot-domain concern) scoped to the
        Slack connector. Future connectors (WhatsApp, Teams) implement their
        own override or rely on the no-op default from MessagingService.
        """
        await GhostingMonitor.get_instance().notify_agent_reply(
            workspace_id=workspace_id, thread_ts=thread_ts
        )

    async def _handle_message_event(
        self, event: dict[str, Any], background_tasks: Any
    ) -> None:
        """Internal helper to process Slack message events (replies, link sniffing, redaction)."""
        subtype = event.get("subtype")
        thread_ts = event.get("thread_ts")
        ts = event.get("ts")
        blocks = event.get("blocks")
        workspace_id = (
            self.slack_integration.workspace_id if self.slack_integration else None
        )
        if not workspace_id:
            return

        # 0. EEA Compliance: Redaction events (message_deleted / message_changed)
        if subtype in ("message_deleted", "message_changed"):
            published = publish_to_sqs(
                queue_url=settings.SQS_SLACK_WEBHOOK_QUEUE_URL,
                workspace_id=workspace_id,
                corr_id=self.corr_id,
                task_type="slack_message_redaction",
                payload={
                    "event": event,
                },
            )
            if not published:
                background_tasks.add_task(
                    run_task_with_context,
                    self.corr_id,
                    self.handle_message_redaction,
                    workspace_id=workspace_id,
                    event=event,
                )
            return

        # 1. Cancel Ghosting Monitor if any human message is received in a thread
        if not event.get("bot_id") and thread_ts:
            await self.on_agent_reply(workspace_id=workspace_id, thread_ts=thread_ts)

        # Link Sniffing: Trigger manual unfurl for rich blocks
        if blocks and not event.get("bot_id"):
            # Deduplication check
            dedupe_key = f"{event.get('channel')}:{ts}"
            if is_already_unfurled(dedupe_key):
                logger.info("Skipping duplicate sniffed unfurl for %s", dedupe_key)
                return

            extracted_urls = extract_links_from_blocks(blocks)
            hubspot_links = [{"url": u} for u in extracted_urls if "hubspot.com" in u]
            if hubspot_links:
                mark_unfurled(dedupe_key)
                published = publish_to_sqs(
                    queue_url=settings.SQS_SLACK_WEBHOOK_QUEUE_URL,
                    workspace_id=workspace_id,
                    corr_id=self.corr_id,
                    task_type="slack_message_unfurl",
                    payload={
                        "channel": event.get("channel"),
                        "ts": ts,
                        "links": hubspot_links,
                    },
                )
                if not published:
                    background_tasks.add_task(
                        run_task_with_context,
                        self.corr_id,
                        self.handle_link_shared,
                        workspace_id=workspace_id,
                        channel=event.get("channel"),
                        ts=ts,
                        links=hubspot_links,
                    )

        # Threaded Reply Sync — covers both threaded replies and standalone
        # support-channel messages. `thread_ts` is None for standalone messages,
        # which handle_threaded_reply already accepts.
        is_threaded_reply = (
            subtype is None
            and not event.get("bot_id")
            and ts
            and thread_ts is not None
            and thread_ts != ts
        )
        is_standalone_support = (
            subtype is None and not event.get("bot_id") and not thread_ts
        )
        if is_threaded_reply or is_standalone_support:
            effective_thread_ts = thread_ts if is_threaded_reply else None
            published = publish_to_sqs(
                queue_url=settings.SQS_SLACK_WEBHOOK_QUEUE_URL,
                workspace_id=workspace_id,
                corr_id=self.corr_id,
                task_type="slack_threaded_reply_sync",
                payload={
                    "channel": event.get("channel"),
                    "thread_ts": effective_thread_ts,
                    "message_ts": str(ts),
                    "text": event.get("text", ""),
                    "user": event.get("user", ""),
                },
            )
            if not published:
                background_tasks.add_task(
                    run_task_with_context,
                    self.corr_id,
                    self.handle_threaded_reply,
                    workspace_id=workspace_id,
                    channel=event.get("channel"),
                    thread_ts=effective_thread_ts,
                    message_ts=str(ts),
                    text=event.get("text", ""),
                    user=event.get("user", ""),
                )

    async def send_welcome_message(
        self,
        workspace_id: str,
        channel: str | None,
        is_update: bool = False,
        ts: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Sends or updates the initial onboarding welcome message.

        If is_update is True, it removes the 'Connect' button and provides
        a confirmation that HubSpot is now linked.
        """
        # 1. Determine if we already have an existing sibling connection
        has_existing_connection = False
        if not is_update:
            if self.integration_service and self.slack_integration:
                metadata = self.slack_integration.metadata or {}
                team_id = metadata.get("slack_team_id")
                if team_id:
                    integrations = await self.integration_service.storage.list_integrations_by_slack_team_id(
                        team_id
                    )
                    for integration in integrations:
                        if await self.integration_service.is_hubspot_connected_anywhere(
                            integration.workspace_id
                        ):
                            has_existing_connection = True
                            break

        if is_update:
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "✅ *Welcome to REHA Connect!* \n\n"
                            "Your HubSpot account is successfully connected. "
                            "You can now search objects and receive notifications directly in Slack.\n\n"
                            "💡 *Tip:* Use `/reha <name>` to get started.\n"
                            "📌 *Tip:* Please remember to invite the bot (`@REHA Connect`) to your channels.\n"
                            "📅 *Tip:* Configure automated daily and weekly digest reports from the REHA Connect Slack App Home.\n"
                            "⚙️ *Setup Required:* To receive CRM alerts, please configure your Notification Channel and Support Triage Channel in the HubSpot App Settings."
                        ),
                    },
                },
            ]
        elif has_existing_connection:
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "👋 *Welcome back to REHA Connect!* \n\n"
                            "Your HubSpot account is already linked and ready to go! "
                            "You can start searching your CRM directly from any channel where the bot is present.\n\n"
                            "📌 *Tip:* Please remember to invite the bot (@REHA Connect) to your channels.\n"
                            "📅 *Tip:* Configure automated daily and weekly digest reports from the REHA Connect Slack App Home.\n"
                            "⚙️ *Setup Required:* To receive CRM alerts, please configure your Notification Channel and Support Triage Channel in the HubSpot App Settings."
                        ),
                    },
                },
            ]
        else:
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "👋 *Welcome to REHA Connect!* \n\n"
                            "To start searching your CRM directly from Slack, "
                            "you need to connect your HubSpot account.\n\n"
                            "💡 *Tip:* Please remember to invite the bot "
                            "(`@REHA Connect`) to your channels."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Connect HubSpot"},
                            "style": "primary",
                            "url": f"{str(settings.API_BASE_URL).rstrip('/')}/api/hubspot/install?state={workspace_id}",
                            "action_id": "connect_hubspot",
                        }
                    ],
                },
            ]

        if is_update and ts:
            # Resolve channel if not provided
            if not channel:
                channel = await self._resolve_channel(workspace_id, None)

            slack_channel = await self.get_slack_channel()
            slack_client = cast("SlackChannel", slack_channel).get_slack_client()
            return await slack_client.chat_update(
                channel=channel,
                ts=ts,
                text="REHA Connect is ready!",
                blocks=blocks,
            )

        return await self.send_message(
            workspace_id=workspace_id,
            channel=channel,
            blocks=blocks,
            text="Welcome to REHA Connect!",
        )

    async def search_and_send(
        self,
        workspace_id: str,
        query: str,
        channel: str,
        response_url: str,
        object_type: str,
        corr_id: str,
        user_id: str = "",
    ) -> None:
        """Coordinates HubSpot search and sends the best possible Slack result."""
        object_type = normalize_object_type(object_type)
        slack_channel = await self.get_slack_channel()

        # Build a context header showing who triggered the search
        context_blocks: list[dict[str, Any]] = []
        target_display = "HubSpot" if object_type == "universal" else f"{object_type}s"
        if user_id:
            context_blocks = [
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"<@{user_id}> searched {target_display}"
                            f" for *{query}*",
                        }
                    ],
                }
            ]

        # 1. Search HubSpot
        try:
            results = await self.crm.search(
                workspace_id=workspace_id,
                object_type=object_type,
                query=query,
            )
        except IntegrationNotFoundError:
            logger.warning(
                "Search failed: HubSpot not connected for workspace %s", workspace_id
            )
            await slack_channel.send_via_response_url(
                response_url=response_url,
                text="❌ *HubSpot not connected.* Please connect your HubSpot account first.",
            )
            return
        except Exception as exc:
            logger.error("Unexpected error in search_and_send: %s", exc, exc_info=True)
            await slack_channel.send_via_response_url(
                response_url=response_url,
                text=(
                    "⚠️ *An unexpected error occurred* while searching "
                    "HubSpot. Please try again later."
                ),
            )
            return

        if not results:
            # Build a friendly empty state card
            empty_card = self.cards.build_search_results(
                results=[],
                title=(
                    "Universal Search"
                    if object_type == "universal"
                    else f"{object_type.capitalize()} Search"
                ),
            )
            rendered = self.slack_renderer.render(empty_card)

            await slack_channel.send_via_response_url(
                response_url=response_url,
                blocks=context_blocks + rendered["blocks"],
                text=f"No {object_type} found for *{query}*.",
            )
            return

        if len(results) > 1:
            # 2. Multiple results -> Send summary list with "View" buttons
            logger.debug(
                "Multiple results found (%d), sending summary list", len(results)
            )
            title = (
                "Universal Search Results"
                if object_type == "universal"
                else f"{object_type.capitalize()} Search Results"
            )
            summary_card = self.cards.build_search_results(
                results=results[:10],  # Increased limit for universal search
                title=title,
            )
            rendered = self.slack_renderer.render(summary_card)
            await slack_channel.send_via_response_url(
                response_url=response_url,
                blocks=context_blocks + rendered["blocks"],
                text=f"Found multiple results for {query}.",
            )
            return

        # 3. Single result hit -> Perform record analysis
        raw_obj = results[0]
        obj_id = raw_obj.get("id")
        # For universal search, use the specific type injected into the result
        obj_type = str(raw_obj.get("type") or normalize_object_type(object_type))

        # Re-fetch via crm.get_object to ensure enrichment (associations, etc.)
        # and fetch engagements for accurate AI scoring in parallel.
        tasks: list[Coroutine[Any, Any, Any]] = [
            self.crm.get_object(
                workspace_id=workspace_id,
                object_type=obj_type,
                object_id=obj_id or "",
            )
        ]

        # Only fetch engagements for objects that use them for scoring/insights
        fetch_engagements = obj_type in ("contact", "deal", "company", "ticket")
        if fetch_engagements:
            tasks.append(
                self.crm.hubspot.get_object_engagements(
                    workspace_id=workspace_id,
                    object_type=obj_type,
                    object_id=obj_id or "",
                )
            )

        results = await asyncio.gather(*tasks)
        obj = results[0] or raw_obj
        engagements = cast(list[Any], results[1] if len(results) > 1 else [])

        logger.info(
            "Single hit found id=%s type=%s (engagements=%d), "
            "performing Record Insights analysis",
            obj.get("id"),
            obj_type,
            len(engagements),
        )

        analysis = await self.ai.analyze_polymorphic(
            obj, obj_type, engagements=engagements
        )

        # Fetch pipelines if it's a deal
        pipelines = None
        if obj_type == "deal":
            pipelines = await self.crm.hubspot.get_pipelines(workspace_id, "deals")

        # Enrich task if it's a task
        task_context = None
        if obj_type == "task":
            task_context = await self.crm.hubspot.enrich_task(
                workspace_id, cast(dict[str, Any], obj)
            )

        # 4. Build Unified IR
        if self.integration_service:
            is_pro = await self.integration_service.is_pro_workspace(workspace_id)
        else:
            is_pro = False
        unified_card = self.cards.build(
            obj,
            cast(Any, analysis),
            pipelines=pipelines,
            task_context=task_context,
            is_pro=is_pro,
        )
        rendered = self.slack_renderer.render(unified_card)

        sent_ts = None
        if obj_type == "ticket" and is_pro:
            # For tickets, use chat.postMessage to enable threading/sync
            resp = await self.send_message(
                workspace_id=workspace_id,
                channel=channel,
                blocks=context_blocks + rendered["blocks"],
                text=f"Found {object_type}: {obj.get('id')}",
            )
            sent_ts = str(resp.get("ts")) if resp and resp.get("ts") else None
        else:
            await slack_channel.send_via_response_url(
                response_url=response_url,
                blocks=context_blocks + rendered["blocks"],
                text=f"Found {object_type}: {obj.get('id')}",
            )

        # 5. Persist thread mapping and post starter message for tickets
        # (Note: send_card now handles this automatically, but we ensure it for clarity
        # or if sent via response_url which search_and_send uses for contacts/companies)
        is_closed = False
        if obj_type == "ticket":
            stage = str(obj.get("properties", {}).get("hs_pipeline_stage", "")).lower()
            is_closed = stage == "4" or "closed" in stage

        if obj_type == "ticket" and is_pro and sent_ts:
            if not is_closed:
                await self._initialize_ticket_thread(
                    workspace_id=workspace_id,
                    object_id=obj_id or "",
                    channel=channel,
                    sent_ts=sent_ts,
                )

    async def handle_link_shared(  # noqa: PLR0912, PLR0915
        self,
        *,
        workspace_id: str,
        channel: str,
        ts: str | None = None,
        links: list[dict[str, str]],
        user_id: str | None = None,
        unfurl_id: str | None = None,
        source: str | None = None,
    ) -> None:
        """Handles Slack link_shared event by unfurling HubSpot URLs.

        Implements Option A (Strict): If no user-specific HubSpot token is found,
        it prompts the user to authenticate before showing any data.

        Supports 2026 Composer Unfurls (source="composer").
        """
        try:
            pattern = re.compile(
                r"(?:app(?:-[a-z0-9]+)?\.hubspot\.com/contacts/\d+|(?:api\.)?rehaapps\.com/crm)/(?:record/)?([^/?#]+)/(\d+)"
            )

            unfurls = {}
            needs_auth = False
            auth_url = ""

            # 1. Attempt to get a user-specific HubSpot client
            user_client = None
            if user_id:
                # 3. Secure Identity Resolution (Sibling-Aware)
                # Find a HubSpot integration record
                # (User-level or Sibling Workspace-level)
                if self.integration_service:
                    integration = (
                        await self.integration_service.storage.get_integration(
                            workspace_id, Provider.HUBSPOT, slack_user_id=user_id
                        )
                    )
                else:
                    integration = None

                # If no user-specific token is found, try workspace (app-level) token
                # Note: get_integration handles the 'Sibling Jump' to workspace record.
                if not integration:
                    logger.debug(
                        "No user-level token found for %s, "
                        "falling back to workspace identity",
                        user_id,
                    )
                    if self.integration_service:
                        integration = (
                            await self.integration_service.storage.get_integration(
                                workspace_id, Provider.HUBSPOT
                            )
                        )

                if not integration or not integration.credentials.get("access_token"):
                    needs_auth = True
                    logger.debug(
                        "Identity Bridge [MISS]: No token found for "
                        "user=%s in workspace/sibling=%s",
                        user_id,
                        workspace_id,
                    )
                else:
                    logger.debug(
                        "Identity Bridge [SUCCESS]: Found HubSpot token "
                        "for user=%s in workspace/sibling=%s",
                        user_id,
                        integration.workspace_id,
                    )
                    user_client = await self.crm.get_client_from_integration(
                        integration
                    )

            # 1. Pre-process invariant tier status (Resolved once per message)
            is_pro = False
            if self.integration_service:
                is_pro = await self.integration_service.is_pro_workspace(workspace_id)

            # 2. Parallel Unfurling Engine
            # We process all links concurrently to minimize round-trip latency
            async def process_single_link(
                link_data: dict[str, Any],
            ) -> tuple[str, dict[str, Any] | None]:
                url = link_data.get("url", "")
                match = pattern.search(url)
                if not match or not user_client:
                    return url, None

                obj_type = normalize_object_type(match.group(1))
                obj_id = match.group(2)

                # Fetch Object (Fast path for link previews)
                obj = await self.crm.get_object_with_client(
                    client=user_client, object_type=obj_type, object_id=obj_id
                )
                if not obj:
                    return url, None

                if isinstance(obj, dict):
                    obj["type"] = obj_type

                analysis = await self.ai.analyze_polymorphic(obj, obj_type)
                unified_card = self.cards.build(obj, cast(Any, analysis), is_pro=is_pro)
                rendered = self.slack_renderer.render(unified_card, is_unfurl=True)

                # Use the first attachment object directly as the unfurl
                # value for color bar support
                payload = None
                if "attachments" in rendered and rendered["attachments"]:
                    payload = rendered["attachments"][0]
                elif "blocks" in rendered and rendered["blocks"]:
                    payload = {"blocks": rendered["blocks"]}

                return url, payload

            # Execute all unfurls in parallel
            results = await asyncio.gather(
                *[process_single_link(link) for link in links]
            )

            # Map results back to the unfurls dictionary
            for url, payload in results:
                if payload:
                    unfurls[url] = payload

            # 6. Call chat.unfurl
            slack_channel_inst = await self.get_slack_channel()

            unfurl_params: dict[str, Any] = {
                "channel": channel,
                "ts": ts,
            }

            if unfurls:
                unfurl_params["unfurls"] = unfurls

            if needs_auth and user_id:
                # Prepare the user auth prompt URL
                auth_url = (
                    f"{settings.API_BASE_URL_STR}/api/hubspot/oauth/user-auth"
                    f"?workspace_id={workspace_id}&slack_user_id={user_id}"
                )

                # 1. Provide standard user_auth_url to Slack (triggers system prompt)
                # This works perfectly in both Standard and Composer contexts.
                unfurl_params["user_auth_url"] = auth_url

                # 2. Custom ephemeral block (Only for Standard posted links)
                # Note: Composer source does nicht support custom ephemeral messages.
                if source != "composer":
                    try:
                        slack_channel = cast(SlackChannel, slack_channel_inst)
                        client = slack_channel.get_slack_client()
                        await client.chat_postEphemeral(
                            channel=channel,
                            user=user_id,
                            text="Connect to HubSpot",
                            blocks=[
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": (
                                            "🛡️ *REHA Connect* needs to verify "
                                            "your HubSpot access to show this preview."
                                        ),
                                    },
                                    "accessory": {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Connect HubSpot",
                                            "emoji": True,
                                        },
                                        "style": "primary",
                                        "url": auth_url,
                                        "action_id": "auth_hubspot_user",
                                    },
                                }
                            ],
                        )
                    except Exception as ephemeral_exc:
                        logger.warning(
                            "Failed to post custom auth ephemeral: %s", ephemeral_exc
                        )

                logger.debug("Prompted user %s for HubSpot auth for unfurl", user_id)

            if unfurls or (needs_auth and auth_url):
                try:
                    # Capture composer-specific identifiers
                    if source == "composer":
                        unfurl_params["source"] = "composer"
                        unfurl_params["unfurl_id"] = unfurl_id

                    await slack_channel_inst.chat_unfurl(**unfurl_params)
                    logger.info(
                        "Handled link_shared: unfurled=%d, needs_auth=%s, source=%s",
                        len(unfurls),
                        needs_auth,
                        source or "default",
                    )
                except SlackApiError as exc:
                    # Fallback or Error handling
                    if exc.response.get("error") == "cannot_unfurl_url":
                        logger.warning("Slack cannot_unfurl_url fallback triggered")
                        # (Optional fallback posting omitted for brevity)
                    else:
                        logger.error("Slack chat.unfurl failed: %s", exc)

        except Exception as exc:
            logger.error(
                "Failed to handle Slack link_shared event: %s", exc, exc_info=True
            )

    async def handle_threaded_reply(  # noqa: PLR0912
        self,
        *,
        workspace_id: str,
        channel: str,
        thread_ts: str | None,
        message_ts: str,
        text: str,
        user: str,
    ) -> None:
        """Handles a reply in a threaded conversation."""
        try:
            slack_channel = await self.get_slack_channel()
            client = cast("SlackChannel", slack_channel).get_slack_client()

            # 1. Resolve target by mapping first (fast, skip Slack API if matched)
            (
                object_id,
                object_type,
                conversation_thread_id,
                source,
            ) = await self._resolve_thread_target(workspace_id, channel, thread_ts)

            full_context = ""
            if not (object_id or conversation_thread_id):
                # 2. Heuristic fallback: Fetch context to try matching Ticket IDs
                if thread_ts:
                    try:
                        resp = await client.conversations_replies(
                            channel=channel, ts=thread_ts, limit=1, inclusive=True
                        )
                        messages = resp.get("messages", [])
                        if messages:
                            parent = messages[0]
                            full_context = (
                                f"{parent.get('text', '')} "
                                f"{str(parent.get('blocks', []))}"
                            )
                    except Exception as exc:
                        logger.warning(
                            "Could not fetch parent message for context: %s", exc
                        )

                if full_context:
                    (
                        object_id,
                        object_type,
                        conversation_thread_id,
                        source,
                    ) = await self._resolve_thread_target(
                        workspace_id, channel, thread_ts, full_context
                    )

                    if object_id and object_type:
                        if self.integration_service:
                            # Lazy Mapping: Persist thread to avoid future lookup costs
                            await (
                                self.integration_service.storage.upsert_thread_mapping(
                                    {
                                        "workspace_id": workspace_id,
                                        "object_type": object_type,
                                        "object_id": object_id,
                                        "channel_id": channel,
                                        "thread_ts": thread_ts,
                                        "source": "heuristic",
                                    }
                                )
                            )
                        logger.info(
                            "Lazy mapped thread_ts=%s to %s=%s (heuristic)",
                            thread_ts,
                            object_type,
                            object_id,
                        )

            if not (object_id or conversation_thread_id):
                logger.debug(
                    "Could not resolve thread target for channel=%s ts=%s",
                    channel,
                    thread_ts,
                )
                return

            # 3. Resolve user identity and build context-aware prefix
            user_name = await self._get_slack_user_name(client, user)
            source_labels = {
                "email": "📧 Email Thread Reply",
                "call": "📞 Call Thread Reply",
                "note": "📝 Note Thread Reply",
            }
            prefix = source_labels.get(source or "", "💬 Slack Thread Reply")
            import time
            from datetime import datetime

            offset_hours = (
                int(time.timezone / -3600)
                if time.localtime().tm_isdst == 0
                else int(time.altzone / -3600)
            )
            sign = "+" if offset_hours > 0 else ""
            now_str = datetime.now().strftime(f"%Y-%m-%d %H:%M GMT{sign}{offset_hours}")
            reply_log = f"[{now_str}] {user_name}: {text}"

            user_mapping = (
                await self.integration_service.storage.user_mappings.fetch_single(
                    {"workspace_id": workspace_id, "slack_user_id": user}
                )
            )
            sender_email = f"{user_name.lower().replace(' ', '.')}@slack.internal"
            if user_mapping and user_mapping.hubspot_email:
                sender_email = user_mapping.hubspot_email

            # 4. Sync to HubSpot
            if object_id and object_type:
                if object_type == "ticket":
                    thread_id = await self.crm.hubspot.get_ticket_thread_id(
                        workspace_id, object_id
                    )
                    if thread_id:
                        # Inject internal comment directly into Helpdesk thread
                        await self.crm.hubspot.add_conversation_message(
                            workspace_id=workspace_id,
                            thread_id=thread_id,
                            content=text,
                            sender_email=sender_email,
                            is_internal=True,
                        )
                    else:
                        await self.crm.hubspot.create_note(
                            workspace_id=workspace_id,
                            content=reply_log.replace("\n", "<br>"),
                            associated_id=object_id,
                            associated_type=object_type,
                            continuous=True,
                        )
                else:
                    await self.crm.hubspot.create_note(
                        workspace_id=workspace_id,
                        content=reply_log,
                        associated_id=object_id,
                        associated_type=object_type,
                    )
                await self.crm.hubspot.invalidate_object_caches(
                    workspace_id, object_type, object_id
                )
                logger.info("Synced threaded reply to %s %s", object_type, object_id)

                # Slack thread has new messages, so we MUST invalidate
                # the AI recap cache.
                if getattr(self, "ai", None):
                    await self.ai.invalidate_recap_cache(workspace_id, object_id)
            elif conversation_thread_id:
                await self.crm.hubspot.send_thread_reply(
                    workspace_id=workspace_id,
                    thread_id=conversation_thread_id,
                    text=reply_log,
                )
                logger.info(
                    "Synced threaded reply to Conversation %s", conversation_thread_id
                )

            # 5. Confirm sync with reaction
            if object_type != "ticket":
                await client.reactions_add(
                    channel=channel, name="notebook", timestamp=message_ts
                )

        except Exception as exc:
            logger.error("Failed to handle threaded reply: %s", exc, exc_info=True)

    async def handle_app_home_opened(self, user_id: str) -> None:
        """Publishes the dynamic Home tab view when a user opens the App Home."""
        try:
            slack_channel = await self.get_slack_channel()
            client = cast("SlackChannel", slack_channel).get_slack_client()

            # 1. Fetch domain stats for dynamic dashboard
            workspace_id = getattr(self.slack_integration, "workspace_id", None)
            workspace: WorkspaceRecord | None = None
            hubspot_integration: IntegrationRecord | None = None

            scheduled_digests = []
            if (
                workspace_id
                and self.integration_service
                and self.integration_service.storage
            ):
                workspace = await self.integration_service.storage.get_workspace(
                    workspace_id
                )
                # 2. Resolve Integration (Sibling-Aware)
                hubspot_integration = (
                    await self.integration_service.resolve_hubspot_integration(
                        workspace_id
                    )
                )

                if not hubspot_integration:
                    logger.warning(
                        "No HubSpot integration found for App Home for user=%s", user_id
                    )
                else:
                    # Update workspace_id if we jumped to a sibling
                    workspace_id = hubspot_integration.workspace_id
                    workspace = await self.integration_service.storage.get_workspace(
                        workspace_id
                    )

                    # 3. Resolve Transport: get_slack_client handles identity bridging
                    client = await self.integration_service.get_slack_client(
                        hubspot_integration
                    )

                scheduled_digests = await self.integration_service.storage.list_scheduled_digests_for_workspace(
                    workspace_id
                )

            logger.info("Publishing Dynamic App Home view for user=%s", user_id)
            view_payload = self.cards.build_app_home_view(
                workspace=workspace,
                integration=hubspot_integration,
                scheduled_digests=scheduled_digests,
            )

            await client.views_publish(user_id=user_id, view=view_payload)
            logger.info("Successfully published Dynamic App Home for user=%s", user_id)
        except Exception as exc:
            logger.error(
                "Failed to publish App Home view for user=%s: %s", user_id, exc
            )

    async def handle_message_redaction(
        self, workspace_id: str, event: dict[str, Any]
    ) -> None:
        """Handles Slack 'message_deleted' or 'message_changed' (redaction) events.
        Instead of deleting records, we anonymize PII to maintain audit integrity
        in the Triple-Key Trace Protocol (Correlation-ID -> TS -> Portal-ID).
        """  # noqa: D208
        try:
            # 1. Resolve Slack TS (the key for redaction)
            slack_ts = event.get("deleted_ts") or event.get("ts")
            if not slack_ts:
                return

            logger.info(
                "Compliance: Redacting PII for message ts=%s in workspace=%s",
                slack_ts,
                workspace_id,
            )

            # 2. Trigger Database-Side Redaction Protocol (2026.03 EEA Standard)
            # A standard 'delete' call triggers the 'BEFORE DELETE'
            # anonymization trigger in the DB.
            if self.integration_service:
                await self.integration_service.storage.client.delete(
                    "interaction_logs", {"slack_ts": slack_ts}
                )

            # 3. Cancel any pending Ghosting Monitor alerts for this thread
            await self.on_agent_reply(workspace_id, slack_ts)

            logger.info("Compliance: PII successfully scrubbed for ts=%s", slack_ts)

        except Exception as exc:
            logger.error("Failed to handle message redaction: %s", exc, exc_info=True)

    async def send_celebration_dm(
        self,
        slack_user_id: str,
    ) -> None:
        """Sends a proactive welcome DM to a user after successful HubSpot auth."""
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "High five! 🙌 You're all set. "
                        "*REHA Connect* is now verified for your account."
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Try these magic moments:*\n"
                        "• Type `/reha` to find a contact.\n"
                        "• Paste any HubSpot record URL to see a private preview.\n"
                        "• Check your *App Home* for real-time CRM stats."
                    ),
                },
            },
        ]

        try:
            if not self.integration_service or not self.slack_integration:
                return

            client = await self.integration_service.get_slack_client(
                self.slack_integration
            )
            if not client:
                return

            await client.chat_postMessage(
                channel=slack_user_id,
                blocks=blocks,
                text="High five! Your HubSpot account is connected.",
            )
            logger.info("Sent celebration DM to user %s", slack_user_id)
        except Exception as e:
            logger.error(
                "Failed to send celebration DM to user %s: %s", slack_user_id, e
            )
