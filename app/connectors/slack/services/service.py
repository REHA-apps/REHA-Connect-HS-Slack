from __future__ import annotations  # noqa: D100

import json
import time
from collections.abc import Mapping
from typing import Any

# SQS client is managed inside app.utils.sqs_helpers — no direct boto3 needed here
from fastapi import BackgroundTasks
from fastapi.responses import Response

from app.connectors.common.interaction_registry import BaseInteractionRegistry
from app.connectors.slack.services.handlers.registry import InteractionRegistry
from app.connectors.slack.ui import ModalBuilder
from app.core.config import settings
from app.core.logging import get_logger
from app.db.records import IntegrationRecord
from app.domains.ai.service import AIService
from app.domains.billing.tier_service import Feature
from app.domains.crm.integration_service import IntegrationService
from app.domains.crm.ui.card_builder import CardBuilder
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils.constants import CREATE_RECORD_CALLBACK_ID

logger = get_logger("interaction.service")

# SQS client and publishing are imported from app.utils.sqs_helpers


# Static mapping from action_id prefix to feature gate.
# Built once at module load — never recreated per request.
_ACTION_FEATURE_MAP: dict[str, str] = {
    "open_add_note_modal": Feature.NOTE_LOGGING.value,
    "open_add_task_modal": Feature.TASK_LOGGING.value,
    "open_schedule_meeting_modal": Feature.MEETING_SCHEDULER.value,
    "open_record_recap_modal": Feature.AI_INSIGHTS.value,
    "post_to_channel": Feature.TICKET_SYNC.value,
}

# Derived inverse mapping from feature gate to action_id.
_FEATURE_ACTION_MAP: dict[str, str] = {v: k for k, v in _ACTION_FEATURE_MAP.items()}


class InteractionService:
    """Refactored InteractionService that delegates to specific handlers.

    This service serves as the main entry point for Slack interactions,
    handling high-throughput 'fast-path' operations synchronously within
    the 3s window and delegating longer tasks to background handlers.
    """

    def __init__(
        self,
        ai: AIService | None,
        integration_service: IntegrationService,
        ai_factory: Any = None,
    ) -> None:
        self.ai = ai
        self.integration_service = integration_service
        self._ai_factory = ai_factory

    def _ensure_services(self) -> None:
        """Materialise AI services on first use (lazy init)."""
        if self.ai is None and self._ai_factory:
            self.ai = self._ai_factory()

    def _publish_to_sqs(
        self,
        workspace_id: str,
        corr_id: str,
        task_type: str,
        payload: Mapping[str, Any],
        view_id: str | None = None,
        feature_id: str | None = None,
    ) -> bool:
        """Publishes the interaction payload to SQS for reliable Lambda execution."""
        from app.utils.sqs_helpers import publish_to_sqs

        full_payload = dict(payload)
        if view_id:
            full_payload["view_id"] = view_id
        if feature_id:
            full_payload["feature_id"] = feature_id

        return publish_to_sqs(
            queue_url=settings.SQS_SLACK_WEBHOOK_QUEUE_URL,
            workspace_id=workspace_id,
            corr_id=corr_id,
            task_type=task_type,
            payload=full_payload,
        )

    async def dispatch_interaction(
        self,
        payload: dict[str, Any],
        integration: IntegrationRecord,
        background_tasks: BackgroundTasks,
        corr_id: str,
    ) -> Response:
        # 2026.03: Capture Portal ID for Triple-Key tracing
        from app.core.logging import portal_id_ctx

        portal_id = str(integration.metadata.get("portal_id", "none"))
        portal_id_ctx.set(portal_id)

        interaction_type = payload.get("type")

        # 0. Fast-path: External select suggestions (block_suggestion)
        # Slack fires this when the user types into an ExternalDataSelectElement.
        # Must respond synchronously with {"options": [...]} within 3 seconds.
        if interaction_type == "block_suggestion":
            try:
                self._ensure_services()
                messaging_service = self._make_messaging_service(corr_id, integration)
                options_resp = await self.handle_suggestion(
                    payload=payload,
                    integration=integration,
                    messaging_service=messaging_service,
                    corr_id=corr_id,
                    action_id=payload.get("action_id", "association_search"),
                    value=payload.get("value", ""),
                )
                if options_resp is None:
                    options_resp = {"options": []}
                return Response(
                    content=json.dumps(options_resp),
                    media_type="application/json",
                )
            except Exception as e:
                logger.error(
                    "block_suggestion handler failed (corr_id=%s): %s", corr_id, e
                )
                return Response(
                    content=json.dumps({"options": []}),
                    media_type="application/json",
                )

        # 1. Fast-path: Actions that open modals (Block Actions)
        if interaction_type == "block_actions":
            try:
                response = await self.handle_fast_path_action(
                    payload, corr_id, background_tasks, integration=integration
                )
                if response:
                    return response
            except Exception as e:
                logger.error("Fast-path modal open failed (corr_id=%s): %s", corr_id, e)
                return Response(status_code=200)

        # 2. Fast-path: Shortcuts / Message Actions
        # CRITICAL: views_open MUST be called within 3 seconds of the trigger.
        # We bypass get_slack_client() (which does DB pivots + token resolution)
        # and use a raw AsyncWebClient with the token already on the integration
        # record. This saves ~500ms-2s and prevents expired_trigger_id errors.
        if interaction_type in ("shortcut", "message_action"):
            callback_id = payload.get("callback_id")
            if callback_id in (
                CREATE_RECORD_CALLBACK_ID,
                "create_hubspot_record_message",
            ):
                trigger_id = payload.get("trigger_id")
                if trigger_id:
                    try:
                        from slack_sdk.web.async_client import AsyncWebClient

                        from app.domains.crm.ui.card_builder import (
                            CardBuilder as DomainCardBuilder,
                        )
                        from app.providers.slack.client import get_shared_slack_session

                        # Fast-path: use token directly from the already-resolved
                        # integration record — no DB pivots, no network calls.
                        bot_token = integration.slack_bot_token
                        if not bot_token:
                            # Fallback: fall through to the full client resolution
                            # (acceptable — only happens on edge-case identity bridges)
                            full_client = (
                                await self.integration_service.get_slack_client(
                                    integration
                                )
                            )
                            raw_client = full_client._client
                        else:
                            raw_client = AsyncWebClient(
                                token=bot_token,
                                session=get_shared_slack_session(),
                            )

                        loading_modal = DomainCardBuilder().build_loading_modal(
                            title="Loading..."
                        )
                        resp = await raw_client.views_open(
                            trigger_id=trigger_id, view=loading_modal
                        )
                        view_id = resp.get("view", {}).get("id") if resp else None

                        if view_id:
                            # Offload the rest to background (tier check, modal build, views_update)
                            if not (
                                settings.SQS_SLACK_WEBHOOK_QUEUE_URL
                                and self._publish_to_sqs(
                                    workspace_id=integration.workspace_id,
                                    corr_id=corr_id,
                                    task_type="handle_shortcut_modal",
                                    payload=payload,
                                    view_id=view_id,
                                )
                            ):
                                background_tasks.add_task(
                                    self._handle_shortcut_modal_background,
                                    payload,
                                    integration,
                                    view_id,
                                )
                    except Exception as e:
                        logger.error("Failed to open loading modal for shortcut: %s", e)
            return Response(status_code=200)

        # 3. Default: Process in background (including view_submission)
        try:
            # Materialise AI services only when needed for background work
            self._ensure_services()
            messaging_service = self._make_messaging_service(corr_id, integration)

            self._dispatch_interaction(
                payload, integration, messaging_service, background_tasks, corr_id
            )
        except Exception:
            logger.exception(
                "Failed to dispatch Slack interaction (corr_id=%s)", corr_id
            )

        # Immediate response for submissions to avoid "double submit"
        if interaction_type == "view_submission":
            return Response(
                content=json.dumps({"response_action": "clear"}),
                media_type="application/json",
            )

        return Response(status_code=200)

    def _dispatch_interaction(
        self,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        background_tasks: BackgroundTasks,
        corr_id: str,
    ) -> None:
        from app.core.logging import run_task_with_context

        if not (
            settings.SQS_SLACK_WEBHOOK_QUEUE_URL
            and self._publish_to_sqs(
                workspace_id=integration.workspace_id,
                corr_id=corr_id,
                task_type="handle_interaction",
                payload=payload,
            )
        ):
            background_tasks.add_task(
                run_task_with_context,
                corr_id,
                self.handle_interaction,
                payload,
                integration,
                messaging_service,
                corr_id,
            )

    async def handle_interaction(
        self,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        corr_id: str,
        **extra_kwargs: Any,
    ) -> Any:
        """Main entry point for Slack interactions."""
        # Ensure AI services are materialised if we're in background task
        self._ensure_services()

        # 2026.03: Resolve slack_ts for Triple-Key Trace
        slack_ts = (
            payload.get("container", {}).get("thread_ts")
            or payload.get("message", {}).get("ts")
            or payload.get("container", {}).get("message_ts")
        )

        # Dynamically resolve the active CRM service instead of hardcoding HubSpot
        if self.ai is None:
            raise RuntimeError("Services not initialized before handle_interaction")

        crm_service = self.integration_service.get_active_crm_service(
            workspace_id=integration.workspace_id, corr_id=corr_id, slack_ts=slack_ts
        )

        registry: BaseInteractionRegistry = InteractionRegistry(
            corr_id=corr_id,
            crm=crm_service,
            ai=self.ai,
            integration_service=self.integration_service,
        )

        interaction_type = str(payload.get("type", ""))

        # Determine unique routing keys
        action_id = None
        actions = payload.get("actions", [])
        if actions:
            action_id = str(actions[0].get("action_id", ""))
        elif interaction_type == "view_submission":
            action_id = str(payload.get("view", {}).get("callback_id", ""))
        elif interaction_type in {"shortcut", "message_action"}:
            action_id = str(payload.get("callback_id", ""))

        # If a gated feature click was intercepted but the workspace is actually Pro,
        # rewrite the action ID to the original feature action so it routes to the correct handler.
        if action_id and action_id.startswith("gated_feature_click:"):
            feature_str = action_id.split(":")[-1]
            is_pro = await self.integration_service.check_feature_access(
                integration.workspace_id, feature_str
            )
            if is_pro:
                mapped_action = _FEATURE_ACTION_MAP.get(feature_str)
                if mapped_action:
                    logger.info(
                        "Rewriting gated feature click action_id=%s to mapped_action=%s for PRO workspace=%s",
                        action_id,
                        mapped_action,
                        integration.workspace_id,
                    )
                    action_id = mapped_action
                    if actions:
                        actions[0]["action_id"] = mapped_action

        handler = registry.get_handler(payload, action_id=action_id)

        if not handler:
            logger.warning(
                "No handler found for interaction: %s (action_id=%s)",
                interaction_type,
                action_id,
            )
            return None

        # Prepare kwargs for the handler
        kwargs: dict[str, Any] = {
            "action_id": action_id,
            "corr_id": corr_id,
        }

        if actions:
            kwargs["value"] = str(
                actions[0].get("value")
                or (actions[0].get("selected_option") or {}).get("value")
                or ""
            )
            kwargs["trigger_id"] = str(payload.get("trigger_id", ""))
            kwargs["response_url"] = str(payload.get("response_url", ""))
            kwargs["channel_id"] = str(payload.get("channel", {}).get("id", ""))

        # If channel details are missing (e.g. action from a modal),
        # try to extract from view metadata
        view = payload.get("view")
        if view and isinstance(view, dict):
            private_metadata = view.get("private_metadata")
            if private_metadata:
                try:
                    meta = json.loads(private_metadata)
                    if isinstance(meta, dict):
                        # Prioritize metadata for modals as top-level might be
                        # stale/missing
                        if meta.get("channel_id"):
                            kwargs["channel_id"] = meta.get("channel_id")
                        if meta.get("response_url"):
                            kwargs["response_url"] = meta.get("response_url")
                except (json.JSONDecodeError, TypeError):
                    pass

        # Merge in any extra kwargs from background tasks (e.g., view_id)
        kwargs.update(extra_kwargs)

        return await handler.handle(
            payload=payload,
            integration=integration,
            messaging_service=messaging_service,
            **kwargs,
        )

    async def handle_suggestion(
        self,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        corr_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Handles real-time search suggestions via SuggestionHandler."""
        from app.connectors.slack.services.handlers.object_handlers import (
            SuggestionHandler,
        )
        from app.connectors.slack.services.slack_ui_adapter import SlackUIAdapter

        self._ensure_services()
        assert self.ai is not None, "AIService not initialized"

        crm_service = self.integration_service.get_active_crm_service(
            workspace_id=integration.workspace_id, corr_id=corr_id
        )

        handler = SuggestionHandler(
            corr_id=corr_id,
            crm=crm_service,
            ai=self.ai,
            integration_service=self.integration_service,
            ui=SlackUIAdapter(integration_service=self.integration_service),
        )

        return await handler.handle(
            payload=payload,
            integration=integration,
            messaging_service=messaging_service,
            **kwargs,
        )

    def _make_messaging_service(
        self, corr_id: str, integration: Any
    ) -> SlackMessagingService:
        from app.domains.messaging.slack.service import SlackMessagingService

        return SlackMessagingService(
            corr_id,
            integration_service=self.integration_service,
            slack_integration=integration,
        )

    async def handle_fast_path_action(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        payload: dict[str, Any],
        corr_id: str,
        background_tasks: BackgroundTasks | None = None,
        integration: IntegrationRecord | None = None,
    ) -> Response | None:
        """Handle high-priority interaction block actions within the 3s window.

        Analyzes the action_id and performs immediate UI feedback, such as
        opening modals or acknowledging ticket state changes, before
        potentially dispatching longer tasks to background workers.

        Args:
            payload: Parameters from the Slack interaction webhook.
            corr_id: Correlation ID for tracing.
            background_tasks: FastAPI background task manager.
            integration: Pre-resolved Slack integration (avoids a redundant DB lookup).

        Returns:
            Success response to Slack if handled, otherwise None.

        """
        # Track execution time for 3s window monitoring
        start_time = time.time()

        actions = payload.get("actions", [])
        action_id = str(actions[0].get("action_id", "")) if actions else ""

        if not action_id.startswith(
            (
                "open_add_note_modal",
                "open_schedule_meeting_modal",
                "open_add_task_modal",
                "open_record_recap_modal",
                "ticket_close",
                "ticket_claim",
                "ticket_reply",
                "view_",
                "post_to_channel",
                "gated_feature_click",
                "open_support_ticket_modal",
                "connect_hubspot",
                "open_create_digest_modal",
            )
        ):
            return None

        # Short-circuit for simple URL buttons that don't need server-side logic
        if action_id == "connect_hubspot":
            return Response(status_code=200)

        trigger_id = payload.get("trigger_id")
        value = str(actions[0].get("value", ""))

        # Validation: AI Recap/Modals expect colon parts, ticket actions expect raw ID
        is_ticket_action = action_id in ("ticket_close", "ticket_claim")
        if (
            not is_ticket_action
            and "ticket_reply" not in action_id
            and ":" not in value
            and "post_to_channel" not in action_id
            and "open_support_ticket_modal" not in action_id
            and "open_create_digest_modal" not in action_id
        ):
            return None

        # Use the pre-resolved integration from the router to avoid a second DB lookup.
        # Fall back to fetching by team_id if not provided (e.g. called standalone).
        if integration is None:
            team_id = str(payload.get("team", {}).get("id", ""))
            integration = (
                await self.integration_service.get_integration_by_slack_team_id(team_id)
            )
            if not integration:
                logger.warning(
                    "Fast-path: No integration found for team_id=%s (corr_id=%s)",
                    team_id,
                    corr_id,
                )
                return None

        bot_token = await self._resolve_bot_token(integration)
        if not bot_token:
            return None

        from app.domains.billing.tier_service import (
            Feature,  # noqa: F811 (re-import for local compat)
        )

        # Determine feature_id for tier-gating (None means the action is free and not gated)
        feature_id = None
        if "gated_feature_click" in action_id:
            feature_str = action_id.split(":")[-1]
            try:
                feature_id = Feature(feature_str).value
            except ValueError:
                pass
        else:
            for key, mapped_feature in _ACTION_FEATURE_MAP.items():
                if key in action_id:
                    feature_id = mapped_feature
                    break

        # Build modal args
        parts = value.split(":") if ":" in value else []
        object_id = parts[-1] if parts else value
        obj_type = parts[1] if len(parts) > 2 else "contact"  # noqa: PLR2004
        # Build metadata
        channel_id = payload.get("channel", {}).get("id")
        response_url = payload.get("response_url")
        meta_dict = {
            "object_id": object_id,
            "object_type": obj_type,
            "contact_id": object_id if obj_type == "contact" else None,
        }
        if channel_id:
            meta_dict["channel_id"] = channel_id
        if response_url:
            meta_dict["response_url"] = response_url

        metadata = json.dumps(meta_dict)

        cards = CardBuilder()

        # Handle all modal-opening actions using the synchronous loading modal + background worker flow.
        # The trigger_id is consumed by views_open within ~50ms.  The feature gate check (which may
        # require a DB round-trip on a cache miss) runs AFTER views_open so it can never expire the
        # trigger.  Non-pro workspaces receive a views_update upgrade-nudge from the background task.
        is_modal_action = action_id.startswith(
            (
                "open_add_note_modal",
                "open_schedule_meeting_modal",
                "open_add_task_modal",
                "open_record_recap_modal",
                "view_",
                "gated_feature_click",
                "open_create_digest_modal",
                "ticket_reply",
            )
        )

        messaging_service = self._make_messaging_service(corr_id, integration)

        # Special-case: the support ticket form needs no CRM data — open it
        # directly within the 3-second synchronous window, skipping the
        # loading-modal → background-task → views_update round-trip that
        # was causing silent hangs.
        if action_id.startswith("open_support_ticket_modal") and trigger_id:
            try:
                client = await self.integration_service.get_slack_client(integration)
                initial_category = (
                    "BILLING_ISSUE" if value == "sales_inquiry" else "GENERAL_INQUIRY"
                )
                support_modal = cards.build_support_ticket_modal(
                    initial_ticket_category=initial_category
                )
                is_from_modal = bool(
                    payload.get("view")
                    and payload.get("view", {}).get("type") == "modal"
                )
                if is_from_modal:
                    await client.views_push(trigger_id=trigger_id, view=support_modal)
                else:
                    await client.views_open(trigger_id=trigger_id, view=support_modal)
                logger.info(
                    "Fast-path: opened support ticket modal directly (corr_id=%s)",
                    corr_id,
                )
            except Exception:
                logger.exception(
                    "Failed to open support ticket modal (action_id=%s, corr_id=%s)",
                    action_id,
                    corr_id,
                )
            return Response(status_code=200)

        if action_id == "open_create_digest_modal" and trigger_id:
            try:
                client = await self.integration_service.get_slack_client(integration)
                # Call users.info to get timezone
                user_id = payload.get("user", {}).get("id")
                user_info = await client.users_info(user=user_id)
                user_tz = (
                    user_info.get("user", {}).get("tz", "UTC")
                    if user_info.get("ok")
                    else "UTC"
                )

                from app.domains.crm.ui.mixins.digest_modals import DigestModalsMixin

                mixin = DigestModalsMixin()
                digest_modal = await mixin.build_create_digest_modal(
                    user_timezone=user_tz
                )

                await client.views_open(trigger_id=trigger_id, view=digest_modal)
                logger.info("Fast-path: opened digest modal (corr_id=%s)", corr_id)
            except Exception:
                logger.exception("Failed to open digest modal")
            return Response(status_code=200)

        if is_modal_action and background_tasks and trigger_id:
            logger.info(
                "Fast-path: opening loading modal for trigger=%s (action_id=%s)",
                trigger_id[:8],
                action_id,
            )
            try:
                from app.domains.crm.ui.card_builder import (
                    CardBuilder as DomainCardBuilder,
                )

                domain_builder = DomainCardBuilder()
                title = "Loading..."
                if action_id.startswith("open_record_recap_modal"):
                    title = "Summarizing Thread..."

                loading_modal = domain_builder.build_loading_modal(title=title)

                # 1. Consume trigger_id immediately — safe from latency/expiry
                client = await self.integration_service.get_slack_client(integration)

                is_from_modal = bool(
                    payload.get("view")
                    and payload.get("view", {}).get("type") == "modal"
                )
                if is_from_modal:
                    resp = await client.views_push(
                        trigger_id=trigger_id, view=loading_modal
                    )
                else:
                    resp = await client.views_open(
                        trigger_id=trigger_id, view=loading_modal
                    )

                view_id = resp.get("view", {}).get("id") if resp else None

                if view_id:
                    from app.core.logging import run_task_with_context

                    # 2. Gate check runs after trigger is consumed — cache miss is safe now
                    is_pro = True
                    if feature_id:
                        is_pro = await self.integration_service.check_feature_access(
                            integration.workspace_id, feature_id
                        )

                    if not is_pro:
                        # Show upgrade nudge by updating the already-open loading modal
                        if not (
                            settings.SQS_SLACK_WEBHOOK_QUEUE_URL
                            and self._publish_to_sqs(
                                workspace_id=integration.workspace_id,
                                corr_id=corr_id,
                                task_type="upgrade_nudge",
                                payload=payload,
                                view_id=view_id,
                                feature_id=feature_id,
                            )
                        ):
                            background_tasks.add_task(
                                run_task_with_context,
                                corr_id,
                                self._update_view_with_upgrade_nudge,
                                integration,
                                view_id,
                                feature_id,
                            )
                    elif not (
                        settings.SQS_SLACK_WEBHOOK_QUEUE_URL
                        and self._publish_to_sqs(
                            workspace_id=integration.workspace_id,
                            corr_id=corr_id,
                            task_type="handle_interaction",
                            payload=payload,
                            view_id=view_id,
                        )
                    ):
                        background_tasks.add_task(
                            run_task_with_context,
                            corr_id,
                            self.handle_interaction,
                            payload,
                            integration,
                            messaging_service,
                            corr_id,
                            view_id=view_id,
                        )
            except Exception:
                logger.exception(
                    "Failed to open loading modal for action_id=%s", action_id
                )
            return Response(status_code=200)

        # Non-modal fast-path actions (ticket_close, ticket_claim, post_to_channel)
        # are gated inside their respective background handlers.

        # Handle Immediate Background Actions (Close, Delete, Claim, Post to Channel)
        is_immediate_background_action = is_ticket_action or action_id.startswith(
            "post_to_channel"
        )

        if is_immediate_background_action and background_tasks:
            logger.info(
                "Fast-path: dispatching background action %s (corr_id=%s)",
                action_id,
                corr_id,
            )
            from app.core.logging import run_task_with_context

            self._dispatch_interaction(
                payload, integration, messaging_service, background_tasks, corr_id
            )
            return Response(status_code=200)

        # Regular modals synchronous fallback (when background_tasks or trigger_id is not present, e.g. in legacy tests)
        if action_id.startswith("open_add_note_modal"):
            modal = cards.build_note_modal(obj_type, object_id, metadata=metadata)
        elif action_id.startswith("open_add_task_modal"):
            modal = cards.build_add_task_modal(obj_type, object_id, metadata=metadata)
        elif action_id.startswith("open_schedule_meeting_modal"):
            modal = cards.build_meeting_modal(
                object_id, object_type=obj_type, metadata=metadata
            )
        elif action_id.startswith("open_support_ticket_modal"):
            modal = cards.build_support_ticket_modal(metadata=metadata)
        else:
            # Catch-all for other modal types or whitelisted actions
            logger.warning(
                "No specific fast-path UI handler for action_id=%s (corr_id=%s)",
                action_id,
                corr_id,
            )
            return Response(status_code=200)

        if trigger_id:
            logger.info(
                "Fast-path synchronous fallback: opening modal for trigger=%s",
                trigger_id[:8],
            )
            try:
                client = await self.integration_service.get_slack_client(integration)
                is_from_modal = bool(
                    payload.get("view")
                    and payload.get("view", {}).get("type") == "modal"
                )
                if is_from_modal:
                    await client.views_push(trigger_id=trigger_id, view=modal)
                else:
                    await client.views_open(trigger_id=trigger_id, view=modal)
                logger.info("Modal opened for object_id=%s", object_id)
            except Exception:
                logger.exception("Failed to open modal in fallback path")
        else:
            logger.error("Missing trigger_id for fast-path modal fallback")

        elapsed = time.time() - start_time
        logger.info(
            "Fast-path handled action_id=%s in %.3fs (corr_id=%s)",
            action_id,
            elapsed,
            corr_id,
        )
        return Response(status_code=200)

    async def _resolve_bot_token(self, integration: IntegrationRecord) -> str | None:
        """Resolve bot token, utilizing Identity Bridge if needed."""
        token = integration.slack_bot_token
        if not token:
            try:
                client = await self.integration_service.get_slack_client(integration)
                token = client.bot_token
            except Exception as exc:
                logger.warning(
                    "Failed to resolve Slack bot token via Identity Bridge: %s", exc
                )
        return token

    async def _update_view_with_upgrade_nudge(
        self,
        integration: IntegrationRecord,
        view_id: str,
        feature_id: str,
    ) -> None:
        """Update an open loading modal to display the upgrade nudge.

        Called from the background task when a non-pro workspace triggers a
        gated modal action.  The loading modal is already open (trigger_id was
        consumed in the fast-path), so we update it in-place rather than
        sending an ephemeral message.

        Args:
            integration: The Slack integration record for the workspace.
            view_id: The Slack view ID of the open loading modal to replace.
            feature_id: The feature gate identifier (used for the nudge copy).

        """
        try:
            workspace = await self.integration_service.storage.get_workspace(
                integration.workspace_id
            )
            portal_id = integration.portal_id if "integration" in locals() and integration else "Unknown" if workspace else None
            builder = CardBuilder()
            modal = builder.build_upgrade_nudge_modal(
                feature_name=feature_id,
                portal_id=portal_id,
                workspace_id=integration.workspace_id,
            )
            client = await self.integration_service.get_slack_client(integration)
            await client.views_update(view_id=view_id, view=modal)
        except Exception:
            logger.exception(
                "Failed to show upgrade nudge for view_id=%s (feature=%s)",
                view_id,
                feature_id,
            )

    async def _handle_shortcut_modal_background(
        self,
        payload: dict[str, Any],
        integration: IntegrationRecord,
        view_id: str,
    ) -> None:
        """Background worker for high-priority Slack shortcuts.

        Performs validation (subscription tier checks) and updates the initial
        loading modal with the actual form or an upgrade nudge.
        """
        try:
            client = await self.integration_service.get_slack_client(integration)

            # Tier check for Pro actions (Create)
            is_pro = await self.integration_service.is_pro_workspace(
                integration.workspace_id
            )
            if not is_pro:
                try:
                    workspace = await self.integration_service.storage.get_workspace(
                        integration.workspace_id
                    )
                    portal_id = integration.portal_id if "integration" in locals() and integration else "Unknown" if workspace else None
                    from app.domains.crm.ui.card_builder import CardBuilder

                    builder = CardBuilder()
                    modal = builder.build_upgrade_nudge_modal(
                        feature_name="create_record",
                        portal_id=portal_id,
                        workspace_id=integration.workspace_id,
                    )
                    await client.views_update(view_id=view_id, view=modal)
                except Exception:
                    logger.exception("Failed to update to upgrade modal")
                return

            # Build modal
            modals = ModalBuilder()
            modal = modals.build_type_selection(CREATE_RECORD_CALLBACK_ID)

            channel_id = payload.get("channel", {}).get("id")
            response_url = payload.get("response_url")
            meta_dict = {}
            if channel_id:
                meta_dict["channel_id"] = channel_id
            if response_url:
                meta_dict["response_url"] = response_url

            if meta_dict:
                modal["private_metadata"] = json.dumps(meta_dict)

            try:
                await client.views_update(view_id=view_id, view=modal)
            except Exception:
                logger.exception("Failed to update to global create modal")
        except Exception as e:
            logger.error("Failed executing background shortcut worker: %s", e)
