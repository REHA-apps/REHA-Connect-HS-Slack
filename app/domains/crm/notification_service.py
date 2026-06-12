# ruff: noqa: E501  # noqa: D100
from __future__ import annotations  # noqa: I001

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from app.core.logging import get_logger
from app.db.records import PlanTier, Provider
from app.db.storage_service import StorageService
from app.domains.ai.service import AIService
from app.domains.crm.hubspot.ghosting_monitor import GhostingMonitor
from app.domains.crm.hubspot.service import HubSpotService
from app.domains.crm.integration_service import IntegrationService
from app.utils.cache import AsyncTTL
from app.utils.helpers import normalize_object_type

from .notifications.growth_campaigns import GrowthCampaignsMixin
from .notifications.heuristics import NotificationHeuristicsMixin

logger = get_logger("notification.service")

# Global debounce cache to prevent duplicate rapid webhooks (creation + propertyChange)
_recent_notifications = AsyncTTL[bool](ttl=60)

# Cache to track seen event IDs for idempotency (5 minute window)
_seen_event_ids = AsyncTTL[bool](ttl=300)

# Global semaphore to limit concurrent notification processing workers.
# This prevents resource exhaustion and API rate-limiting during bursts.
_SEMAPHORE = asyncio.Semaphore(20)


class NotificationService(NotificationHeuristicsMixin, GrowthCampaignsMixin):
    """Processes HubSpot webhook events and sends proactive AI notifications using mixins."""

    AI_SCORE_THRESHOLD = 80

    def __init__(
        self,
        corr_id: str | None = None,
        *,
        storage: StorageService | None = None,
        integration_service: IntegrationService | None = None,
        ai: AIService | None = None,
    ):
        self.corr_id = corr_id or "system"
        self.storage = storage or StorageService(corr_id=self.corr_id)
        self.hubspot = HubSpotService(self.corr_id, storage=self.storage)
        self.integration_service = integration_service or IntegrationService(
            self.corr_id, storage=self.storage
        )

        self.ai = ai or AIService(self.corr_id)

    async def process_event_batch(
        self, events: list[dict[str, Any]], storage: StorageService
    ) -> None:
        """Process a batch of HubSpot webhook events concurrently with idempotency."""

        async def _process_one(event: dict[str, Any]) -> None:
            try:
                event_id = event.get("eventId")
                if event_id:
                    is_new = await storage.idempotency_svc.mark_processed(
                        str(event_id), "hubspot"
                    )
                    if not is_new:
                        logger.info("Ignoring duplicate HubSpot event: %s", event_id)
                        return
                await self.handle_event(event)
            except Exception as exc:
                logger.error(
                    "Failed to process event from batch (corr_id=%s): %s",
                    self.corr_id,
                    exc,
                    exc_info=True,
                )

        # Concurrent batch — bounded by _SEMAPHORE(20) inside handle_event
        await asyncio.gather(*(_process_one(e) for e in events))

    async def handle_event(self, event: dict[str, Any]) -> None:  # noqa: PLR0911, PLR0912, PLR0915
        """Process a single HubSpot webhook event with concurrency protection and idempotency."""
        event_id = event.get("eventId")
        if event_id:
            if await _seen_event_ids.get(str(event_id)):
                logger.debug("Ignoring duplicate HubSpot event: %s", event_id)
                return
            await _seen_event_ids.set(str(event_id), True)

        async with _SEMAPHORE:
            await self._handle_event_logic(event)

    async def _handle_event_logic(self, event: dict[str, Any]) -> None:  # noqa: PLR0911, PLR0912, PLR0915
        """Internal logic for processing a single event."""
        sub_type = event.get("subscriptionType", "")
        object_id = str(event.get("objectId", ""))

        portal_id = str(event.get("portalId"))

        # 1. Resolve Workspace
        integration = await self.storage.get_integration_by_portal_id(portal_id)
        if not integration:
            logger.warning("No integration found for portalId=%s", portal_id)
            return

        workspace_id = integration.workspace_id

        # 1c. Automated Growth/Momentum Reminder check (Trial Day 4)
        # This occurs on every webhook hit to ensure reliable delivery during trial activity.
        await self._send_growth_reminder(workspace_id)

        # 1a. Handle HubSpot GDPR/Privacy Deletion events
        # Required for Right to be Forgotten (GDPR)
        if sub_type == "privacy.deletion":
            user_id = event.get("userId")
            owner_id = event.get("objectId")  # owner.deletion uses objectId

            logger.info(
                "Processing GDPR privacy deletion for owner_id=%s or user_id=%s",
                owner_id,
                user_id,
            )
            if owner_id:
                deleted = await self.storage.delete_user_mapping(
                    workspace_id, str(owner_id)
                )
                if deleted:
                    logger.info(
                        "Successfully purged user mapping for GDPR compliance (owner=%s)",
                        owner_id,
                    )
            return

        # Handle HubSpot uninstallation event
        if sub_type == "app.deleted":
            logger.info("Processing HubSpot uninstall for portalId=%s", portal_id)
            await self.integration_service.uninstall_hubspot(workspace_id)
            logger.info("HubSpot integration removed for workspace_id=%s", workspace_id)
            return

        # 2. Check notifications_enabled flag (set via SettingsPage)
        # Parallelize with is_pro check — both are independent DB lookups.
        slack_integ_check, is_pro = await asyncio.gather(
            self.storage.get_integration(workspace_id, Provider.SLACK),
            self.integration_service.is_pro_workspace(workspace_id),
        )
        if slack_integ_check:
            notifs_enabled = slack_integ_check.metadata.get(
                "notifications_enabled", True
            )
            if not notifs_enabled:
                logger.debug(
                    "Notifications disabled for workspace %s — skipping event %s",
                    workspace_id,
                    sub_type,
                )
                return

        # 2. Determine Object Type
        obj_type = self._map_subscription_to_type(sub_type, event)
        if not obj_type:
            logger.debug(
                "Skipping unhandled subscription type: %s (objectTypeId: %s)",
                sub_type,
                event.get("objectTypeId"),
            )
            return

        # 2b. Suppress note/email echo notifications.
        # Notes created by this app (e.g. from Slack thread sync) fire
        # webhooks back. Allowing them would create infinite notification
        # loops: Slack reply → HubSpot Note → webhook → Slack notification.
        if obj_type in ("note", "email"):
            logger.debug(
                "Suppressing %s webhook to prevent bi-directional sync loop "
                "(objectId=%s)",
                obj_type,
                object_id,
            )
            return

        # 3. Invalidate Cache for modified records
        # This ensures Slack searches and future notifications are accurate.
        if obj_type in ("contact", "deal", "company", "ticket", "task", "lead"):
            await self.hubspot.invalidate_object_caches(
                workspace_id=workspace_id, object_type=obj_type, object_id=object_id
            )
            await self.ai.invalidate_recap_cache(
                workspace_id=workspace_id, object_id=object_id
            )

        # 3. Fetch full object from HubSpot for context
        # (Creation settling delay is now handled asynchronously via SQS DelaySeconds)
        is_creation = "creation" in sub_type.lower()

        obj = await self.hubspot.get_object(
            workspace_id=workspace_id,
            object_type=obj_type,
            object_id=object_id,
            ignore_cache=True,
        )

        if not obj:
            logger.warning(
                "Could not fetch %s %s (corr_id=%s)", obj_type, object_id, self.corr_id
            )
            return

        # Mitigate webhook race condition: if propertyChange beats creation,
        # but the object was created < 60s ago, treat it as a creation event.
        if not is_creation:
            props = obj.get("properties", {})
            created_str = props.get("createdate") or props.get("hs_createdate")
            if created_str:
                try:
                    from datetime import UTC, datetime

                    created_dt = datetime.fromisoformat(
                        str(created_str).replace("Z", "+00:00")
                    )
                    if (datetime.now(UTC) - created_dt).total_seconds() < 60:
                        is_creation = True
                        logger.debug("Forced is_creation=True due to recent createdate")
                except Exception:
                    pass

        # Log owner ID for DM routing diagnostics
        owner_id = obj.get("properties", {}).get("hubspot_owner_id")
        logger.info(
            "Fetched %s %s (owner_id=%s) for analysis", obj_type, object_id, owner_id
        )

        # 0. Redundant Task Suppression - Ensure generic tasks
        # never trigger proactive cards
        # We allow CALL, MEETING, and EMAIL tasks as they represent
        # important logged activities.
        if obj_type == "task" and "creation" in sub_type.lower():
            props = obj.get("properties", {})
            task_type = props.get("hs_task_type")
            priority = props.get("hs_task_priority")

            # Notify for Call/Meeting/Email tasks OR High/Urgent Priority tasks
            if task_type in ["CALL", "MEETING", "EMAIL"] or str(priority).upper() in (
                "HIGH",
                "URGENT",
            ):
                logger.debug(
                    "Allowing task creation notification (type=%s, priority=%s)",
                    task_type,
                    priority,
                )
            else:
                logger.debug(
                    "Skipping generic task creation notification "
                    "(object_id=%s, type=%s, priority=%s)",
                    object_id,
                    task_type,
                    priority,
                )
                return

        # Inject portalId for deep linking
        if isinstance(obj, dict):
            obj["portalId"] = portal_id

        # 4. Perform AI Analysis
        analysis = await self.ai.analyze_polymorphic(obj, obj_type)

        # 5. Check Notification Threshold
        # Always notify on creation events so new tickets/contacts/deals
        # are never silently dropped, regardless of priority.
        # We use a case-insensitive check and also allow high-value interactions
        # like Calls, Meetings, and Emails to notify on logging (property changes).
        if not is_creation and not self._should_notify(obj, analysis, event):
            logger.debug(
                "Skipping notification for %s %s (logic: below relevant threshold)",
                obj_type,
                object_id,
            )
            return

        # 5b. Debounce Duplicate Webhooks
        # HubSpot often fires multiple events simultaneously
        # (e.g. object.creation + object.propertyChange).
        debounce_key = f"notif_debounce:{workspace_id}:{obj_type}:{object_id}"
        if await _recent_notifications.get(debounce_key):
            logger.debug(
                "Skipping notification for %s %s (debounced recent update)",
                obj_type,
                object_id,
            )
            return

        # Record this notification to debounce subsequent rapid duplicates (60s TTL)
        await _recent_notifications.set(debounce_key, True)

        # 6. Resolve Target Slack Integration + Workspace concurrently.
        # Both lookups are independent — parallelising saves 150-300ms per notification.
        target_integ = None
        if is_pro:
            all_integrations, workspace = await asyncio.gather(
                self.storage.list_integrations(workspace_id, Provider.SLACK),
                self.storage.get_workspace(workspace_id),
            )

            # Heuristic: Match 'hs_territory' against 'routing_key' in metadata
            territory = obj.get("properties", {}).get("hs_territory")
            if territory:
                for integ in all_integrations:
                    if integ.metadata.get("routing_key") == territory:
                        target_integ = integ
                        logger.debug(
                            "Routed notification to Slack team_id=%s for territory=%s",
                            integ.metadata.get("slack_team_id"),
                            territory,
                        )
                        break

            if not target_integ and slack_integ_check:
                # Default to primary active integration
                target_integ = slack_integ_check
                logger.debug(
                    "No territory match; defaulted to primary Slack team_id=%s",
                    target_integ.metadata.get("slack_team_id"),
                )
        else:
            # Starter/Professional: Single workspace logic — parallelise with workspace fetch
            target_integ, workspace = await asyncio.gather(
                self.storage.get_integration(workspace_id, Provider.SLACK),
                self.storage.get_workspace(workspace_id),
            )

        if not target_integ:
            logger.warning(
                "No target Slack integration found for workspace %s, cannot notify",
                workspace_id,
            )
            return

        # Resolve target Messaging Service
        from app.domains.messaging.factory import get_messaging_service

        # Use factory to get the appropriate service (Slack, WhatsApp, Teams)
        messaging_service = await get_messaging_service(
            workspace_id=workspace_id,
            storage=self.storage,
            corr_id=self.corr_id,
            integration_record=target_integ,
        )

        if not messaging_service:
            logger.warning(
                "No target messaging provider found for workspace %s, cannot notify",
                workspace_id,
            )
            return

        logger.info(
            "Sending proactive notification via %s for %s %s",
            type(messaging_service).__name__,
            obj_type,
            object_id,
        )

        thread_ts = None
        mapping = None
        pipelines = None
        channel = None

        if obj_type == "deal":
            pipelines = await self.hubspot.get_pipelines(workspace_id, "deals")

        if is_pro and obj_type in ("ticket", "task"):
            # 1. Resolve target channel
            explicit_channel = (
                target_integ.metadata.get("triage_channel_id")
                if obj_type == "ticket"
                else None
            )
            channel = await messaging_service._resolve_channel(
                workspace_id, explicit_channel, obj=obj
            )  # type: ignore
            if channel:
                # 2. Look up existing thread mapping
                mapping = await self.storage.get_thread_mapping(
                    workspace_id=workspace_id,
                    object_type=obj_type,
                    object_id=object_id,
                    channel_id=channel,
                )
                if mapping:
                    thread_ts = mapping.thread_ts
                    logger.debug(
                        "Found existing thread mapping thread_ts=%s", thread_ts
                    )
                else:
                    # Check for dedicated support channel mapping
                    root_mapping = await self.storage.get_thread_mapping_by_ts(
                        workspace_id=workspace_id,
                        channel_id=channel,
                        thread_ts="CHANNEL_ROOT",
                    )
                    if root_mapping:
                        thread_ts = "CHANNEL_ROOT"
                        logger.debug("Routing to support channel via CHANNEL_ROOT")

        # 6a. Tier Gating: Enforce notification caps for Free Tier
        # workspace was already fetched above in the concurrent gather — no extra DB call.
        if workspace and workspace.plan == PlanTier.FREE:
            # Hard cap at 20 notifications per month for the Free version
            if (workspace.notification_count_monthly or 0) >= 20:  # noqa: PLR2004
                logger.info(
                    "Suppressed notification for Free workspace %s (Limit: 20/mo reached)",
                    workspace_id,
                )
                return

        # 6. Send Slack Notification (Update in-place if thread exists)
        if thread_ts and not is_creation and thread_ts != "CHANNEL_ROOT":
            # Update the original card in place to avoid spam
            logger.debug(
                "Updating existing Slack card ts=%s for %s %s",
                thread_ts,
                obj_type,
                object_id,
            )
            from app.domains.messaging.slack.service import SlackMessagingService

            slack_svc = messaging_service  # type: ignore[assignment]
            if not isinstance(slack_svc, SlackMessagingService):
                logger.warning("update_message not supported on %s", type(slack_svc))
                sent_ts = None
            else:
                unified_card = slack_svc.cards.build(
                    obj,
                    analysis,
                    is_pro=is_pro,
                    pipelines=pipelines,
                    include_actions=True,
                )
                rendered = slack_svc.slack_renderer.render(unified_card)

                # Add Notification Context Header
                rendered["blocks"].insert(
                    0,
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "⚡ *REHA Connect | HubSpot Record Updated*",
                            }
                        ],
                    },
                )

                await slack_svc.update_message(
                    workspace_id=workspace_id,
                    channel=channel,  # type: ignore
                    ts=thread_ts,
                    blocks=rendered["blocks"],
                    text=f"Updated {obj_type.capitalize()}: {unified_card.title}",
                )
                sent_ts = thread_ts

                if obj_type == "ticket":
                    try:
                        changes = []
                        if (
                            isinstance(event, dict)
                            and event.get("subscriptionType") == "ticket.propertyChange"
                        ):
                            prop_name = event.get("propertyName", "")
                            prop_val = event.get("propertyValue", "")
                            if prop_name:
                                changes.append(f"*{prop_name}* changed to `{prop_val}`")

                        update_text = "🔄 The ticket has been updated in HubSpot."
                        if changes:
                            update_text += "\n" + "\n".join(changes)

                        await slack_svc.send_message(
                            workspace_id=workspace_id,
                            channel=channel,
                            thread_ts=thread_ts,
                            text=update_text,
                        )
                    except Exception as e:
                        logger.error("Failed to post threaded ticket update: %s", e)
        else:
            sent_ts = await messaging_service.send_card(
                workspace_id=workspace_id,
                obj=obj,
                channel=channel,
                analysis=analysis,
                is_pro=is_pro,
                thread_ts=thread_ts,
                pipelines=pipelines,
                is_notification=True,
                is_creation=is_creation,
            )

        # 6c. Trigger Live Ghosting Monitor for Tickets (Pro Only)
        if obj_type == "ticket" and sent_ts and is_pro:
            # For Ghosting, we treat the HubSpot update as a 'Customer Message'
            # because it requires an Agent response in Slack.
            owner_id = obj.get("properties", {}).get("hubspot_owner_id")
            slack_user_id = None
            if owner_id:
                try:
                    mapping_record = await self.storage.get_user_mapping(
                        workspace_id, int(owner_id)
                    )
                    if mapping_record:
                        slack_user_id = mapping_record.slack_user_id
                except Exception as e:
                    logger.warning(
                        "Could not map owner %s for ghosting: %s", owner_id, e
                    )

            await GhostingMonitor.get_instance().notify_customer_message(
                workspace_id=workspace_id,
                thread_ts=sent_ts if not thread_ts else thread_ts,
                agent_user_id=slack_user_id,
            )

        if sent_ts:
            await _recent_notifications.set(debounce_key, True)
            # 6b. Increment Usage Metrics
            # Triggers monthly reset if needed and updates total record sync count.
            await self.storage.increment_usage_metrics(
                workspace_id=workspace_id, is_notification=True
            )
        else:
            # Even if card isn't 'sent' (e.g. error), increment sync counter for activity
            await self.storage.increment_usage_metrics(
                workspace_id=workspace_id, is_notification=False
            )

        # 7. Persist new thread mapping if this was the first message
        # Note: SlackMessagingService already handles this for 'ticket' types
        # during send_card, so we only need to handle 'task' or other types here.
        if is_pro and obj_type == "task" and sent_ts and not thread_ts and channel:
            await self.storage.upsert_thread_mapping(
                {
                    "workspace_id": workspace_id,
                    "object_type": obj_type,
                    "object_id": object_id,
                    "channel_id": channel,
                    "thread_ts": sent_ts,
                }
            )
            logger.debug(
                "Stored new thread mapping for %s thread_ts=%s", obj_type, sent_ts
            )

        # 8. Clean up thread mapping when a ticket is closed
        if obj_type == "ticket" and event.get("propertyName") in (
            "hs_pipeline_stage",
            "hs_ticket_status",
        ):
            props = obj.get("properties", {}) if isinstance(obj, dict) else {}
            ticket_status = str(props.get("hs_ticket_status") or "").upper()

            if ticket_status == "CLOSED":
                deleted = await self.storage.delete_thread_mapping(
                    workspace_id, "ticket", object_id
                )
                if deleted:
                    logger.info(
                        "Cleaned up %d thread mapping(s) for closed ticket %s",
                        deleted,
                        object_id,
                    )
                    # Post a closure notice in the existing thread
                    if mapping and mapping.thread_ts:
                        try:
                            ch = mapping.channel_id
                            await messaging_service.send_message(
                                workspace_id=workspace_id,
                                channel=ch,
                                thread_ts=mapping.thread_ts,
                                text=(
                                    "🔒 This ticket has been closed. "
                                    "Thread sync has been disabled."
                                ),
                            )
                        except Exception as exc:
                            logger.warning(
                                "Failed to post closure notice: %s",
                                exc,
                            )

    def _map_subscription_to_type(
        self, sub_type: str, event: dict[str, Any] | None = None
    ) -> str | None:
        # Standard subscription type format:
        # 'ticket.creation', 'deal.propertyChange', etc.
        type_map = {
            "contact": "contact",
            "deal": "deal",
            "company": "company",
            "ticket": "ticket",
            "task": "task",
            "meeting": "meeting",
            "conversation": "conversation",
            "lead": "lead",
            "call": "call",
            "note": "note",
            "email": "email",
            "owner": "owner",
        }
        for key, val in type_map.items():
            if key in sub_type:
                return val

        # HubSpot CRM Events format: 'object.creation', 'object.propertyChange'
        # Object type is identified via objectTypeId in the event payload.
        if sub_type.startswith("object.") and event:
            object_type_id = str(event.get("objectTypeId", ""))
            return normalize_object_type(object_type_id)

        return None
