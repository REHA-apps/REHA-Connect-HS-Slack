from __future__ import annotations  # noqa: D100

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


from app.core.logging import get_logger
from app.db.records import IntegrationRecord
from app.domains.crm.notification_service import _recent_notifications
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils import action_ids

from .base import (
    InteractionHandler,
    UnifiedContext,
    interaction_handler,
    require_feature,
    with_slack_error_handling,
)

logger = get_logger("action_handlers")


class ActionButtonHandler(InteractionHandler):
    @interaction_handler(action_ids.UPDATE_DEAL_STAGE)
    @with_slack_error_handling(action_ids.UPDATE_DEAL_STAGE)
    @require_feature("deal_stage")
    async def _handle_update_deal_stage(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        channel_id = context.channel_id
        deal_id = context.get_entity_id()
        if not deal_id:
            logger.warning(
                "Malformed update_deal_stage context=%s",
                context.action_id or context.value,
            )
            return

        actions = payload.get("actions", [])
        if not actions:
            return
        selected_option = actions[0].get("selected_option")
        if not selected_option:
            return
        new_stage_id = selected_option.get("value")

        # Set debounce key IMMEDIATELY before any awaits, so the HubSpot webhook
        # echo (which can arrive within 1-2 seconds) is suppressed.
        await _recent_notifications.set(
            f"notif_debounce:{integration.workspace_id}:deal:{deal_id}", True
        )

        is_pro = await self.integration_service.is_pro_workspace(
            integration.workspace_id
        )
        if is_pro:
            deal = await self.crm.get_deal(
                workspace_id=integration.workspace_id, object_id=deal_id
            )
            props = deal.get("properties", {}) if deal else {}
            response_url = context.response_url
            metadata = json.dumps(
                {
                    "deal_id": deal_id,
                    "stage_id": new_stage_id,
                    "channel_id": channel_id,
                    "response_url": response_url,
                }
            )
            if "won" in new_stage_id.lower() or "lost" in new_stage_id.lower():
                modal = messaging_service.cards.build_post_mortem_modal(
                    deal_id, new_stage_id, metadata=metadata
                )
                if context.trigger_id:
                    await self._open_modal(
                        trigger_id=context.trigger_id,
                        view_or_card=modal,
                        title="Update Deal",
                        integration=integration,
                        user_id=context.user_id or "",
                    )
                return
            if not props.get("hs_next_step"):
                modal = messaging_service.cards.build_next_step_enforcement_modal(
                    deal_id, new_stage_id, metadata=metadata
                )
                if context.trigger_id:
                    await self._open_modal(
                        trigger_id=context.trigger_id,
                        view_or_card=modal,
                        title="Next Steps",
                        integration=integration,
                        user_id=context.user_id or "",
                    )
                return

        await self.crm.update_object(
            workspace_id=integration.workspace_id,
            object_type="deal",
            object_id=deal_id,
            properties={"dealstage": new_stage_id},
        )
        await messaging_service.refresh_and_update_card(
            workspace_id=integration.workspace_id,
            object_type="deal",
            object_id=deal_id,
            channel_id=channel_id,
            response_url=context.response_url,
            text=f"Deal stage updated to {new_stage_id}",
        )

    @interaction_handler("update_deal_closedate")
    @with_slack_error_handling("update_deal_closedate")
    @require_feature("deal_closedate")
    async def _handle_update_deal_closedate(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        channel_id = context.channel_id
        deal_id = context.get_entity_id()
        if not deal_id:
            logger.warning(
                "Malformed update_deal_closedate context=%s",
                context.action_id or context.value,
            )
            return

        actions = payload.get("actions", [])
        if not actions:
            return
        selected_date = actions[0].get("selected_date")
        if not selected_date:
            return

        # Set debounce key IMMEDIATELY before any awaits
        from app.domains.crm.notification_service import _recent_notifications

        await _recent_notifications.set(
            f"notif_debounce:{integration.workspace_id}:deal:{deal_id}", True
        )

        try:
            dt = datetime.strptime(selected_date, "%Y-%m-%d").replace(tzinfo=UTC)
            hs_timestamp = str(int(dt.timestamp() * 1000))
        except ValueError:
            hs_timestamp = selected_date

        await self.crm.update_object(
            workspace_id=integration.workspace_id,
            object_type="deal",
            object_id=deal_id,
            properties={"closedate": hs_timestamp},
        )
        await messaging_service.refresh_and_update_card(
            workspace_id=integration.workspace_id,
            object_type="deal",
            object_id=deal_id,
            channel_id=channel_id,
            response_url=context.response_url,
            text=f"Deal close date updated to {selected_date}",
        )

    @interaction_handler(action_ids.UPDATE_TASK_STATUS)
    @with_slack_error_handling(action_ids.UPDATE_TASK_STATUS)
    async def _handle_update_task_status(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        channel_id = context.channel_id
        task_id = context.get_entity_id()
        if not task_id:
            logger.warning(
                "Malformed update_task_status context=%s",
                context.action_id or context.value,
            )
            return
        actions = payload.get("actions", [])
        if not actions:
            return

        new_status = ""
        action = actions[0]
        if action.get("type") == "static_select":
            selected_option = action.get("selected_option")
            if not selected_option:
                return
            new_status = selected_option.get("value")
        elif action.get("type") == "button":
            new_status = (
                "COMPLETED"  # Default for button click if we add a 'Complete' button
            )

        if not new_status:
            return

        await _recent_notifications.set(
            f"notif_debounce:{integration.workspace_id}:task:{task_id}", True
        )

        await self.crm.update_object(
            workspace_id=integration.workspace_id,
            object_type="task",
            object_id=task_id,
            properties={"hs_task_status": new_status},
        )
        await messaging_service.refresh_and_update_card(
            workspace_id=integration.workspace_id,
            object_type="task",
            object_id=task_id,
            channel_id=channel_id,
            response_url=context.response_url,
            text=f"Task status updated to {new_status}",
        )

    @interaction_handler(action_ids.UPDATE_TASK_PRIORITY)
    @with_slack_error_handling(action_ids.UPDATE_TASK_PRIORITY)
    async def _handle_update_task_priority(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        channel_id = context.channel_id
        task_id = context.get_entity_id()
        if not task_id:
            logger.warning(
                "Malformed update_task_priority context=%s",
                context.action_id or context.value,
            )
            return

        actions = payload.get("actions", [])
        if not actions:
            return

        new_priority = ""
        action = actions[0]
        if action.get("type") == "static_select":
            selected_option = action.get("selected_option")
            if not selected_option:
                return
            new_priority = selected_option.get("value")

        if not new_priority:
            return

        await _recent_notifications.set(
            f"notif_debounce:{integration.workspace_id}:task:{task_id}", True
        )

        await self.crm.update_object(
            workspace_id=integration.workspace_id,
            object_type="task",
            object_id=task_id,
            properties={"hs_task_priority": new_priority},
        )
        await messaging_service.refresh_and_update_card(
            workspace_id=integration.workspace_id,
            object_type="task",
            object_id=task_id,
            channel_id=channel_id,
            response_url=context.response_url,
            text=f"Task priority updated to {new_priority}",
        )

    @interaction_handler(action_ids.OPEN_IN_HUBSPOT)
    async def _handle_open_in_hubspot(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """No-op: Handled by Slack URL button directly.
        Registered to avoid 'No handler found' warnings.
        """
        logger.info("Open in HubSpot click tracked for audit: %s", context.value)

    @interaction_handler(action_ids.GATED_FEATURE_CLICK)
    @with_slack_error_handling(action_ids.GATED_FEATURE_CLICK)
    async def _handle_gated_feature_click(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Shows the upgrade nudge modal when a gated feature is clicked."""
        action_id = kwargs.get("action_id", "")
        feature_id = action_id.split(":")[1] if ":" in action_id else action_id
        trigger_id = context.trigger_id
        view_id = kwargs.get("view_id")
        await self._handle_gated_click(
            feature_id=feature_id,
            trigger_id=trigger_id,
            integration=integration,
            messaging_service=messaging_service,
            view_id=view_id,
            response_url=context.response_url,
        )

    @interaction_handler(action_ids.UPGRADE_LINK_CLICK)
    async def _handle_upgrade_link_click(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """No-op: Slack URL buttons open in browser directly."""

    @interaction_handler(action_ids.CONTACT_SALES_CLICK)
    async def _handle_contact_sales_click(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """No-op: Slack URL buttons open in browser directly."""

    @interaction_handler(action_ids.CONFIRM_DISCONNECT_HUBSPOT)
    @with_slack_error_handling("confirm_disconnect")
    async def _handle_confirm_disconnect(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Shows the Smart Cleanup confirmation modal."""
        if not context.trigger_id:
            return

        team_id = payload.get("team", {}).get("id")
        if not team_id:
            return

        # Run both DB lookups in parallel to minimise time before views_open.
        # Slack's trigger_id expires after ~3 seconds; sequential awaits risk
        # expiry when the database is under load.
        integrations, workspace = await asyncio.gather(
            self.integration_service.storage.list_integrations_by_slack_team_id(
                team_id
            ),
            self.integration_service.storage.get_workspace(integration.workspace_id),
        )

        # Portals are integrations where workspace_id starts with 'hs_'
        portals = [i for i in integrations if i.workspace_id.startswith("hs_")]
        is_last_portal = len(portals) <= 1
        portal_id = workspace.portal_id if workspace else "Unknown"

        # 3. Build and open the modal
        modal = messaging_service.cards.build_confirm_disconnect_modal(
            portal_id=str(portal_id),
            is_last_portal=is_last_portal,
            metadata=integration.workspace_id,
            is_slack_only=len(portals) == 0,
        )

        client = await self.integration_service.get_slack_client(integration)

        try:
            await client.views_open(
                trigger_id=context.trigger_id,
                view=modal,
            )
        except Exception as e:
            from slack_sdk.errors import SlackApiError

            err = getattr(e, "__cause__", e)
            if (
                isinstance(err, SlackApiError)
                and err.response.get("error") == "invalid_auth"
            ):
                logger.warning(
                    "invalid_auth encountered; invalidating client cache and retrying"
                )
                await self.integration_service.slack_client_service.invalidate_client_cache(
                    integration.id
                )
                client = await self.integration_service.get_slack_client(integration)
                await client.views_open(
                    trigger_id=context.trigger_id,
                    view=modal,
                )
            else:
                raise

    @interaction_handler(action_ids.EXECUTE_UNIVERSAL_UNINSTALL)
    @with_slack_error_handling(action_ids.EXECUTE_UNIVERSAL_UNINSTALL)
    async def _handle_universal_uninstall(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Performs a full wipe of HubSpot and Slack connections (Smart Cleanup)."""
        workspace_id = context.private_metadata or integration.workspace_id
        logger.info(
            "Universal uninstall triggered from UI for workspace=%s", workspace_id
        )  # noqa: E501

        # Update the view first to say it's done. Once we uninstall, the Slack token
        # is revoked and we can no longer make API calls to update the view!
        # Use resolved client to handle shell records (Identity Bridge)
        client = await self.integration_service.get_slack_client(integration)
        try:
            await client.views_update(
                view_id=context.view_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Uninstalled"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    "✅ *App uninstalled from both HubSpot and Slack.* "
                                    "Your data has been safely cleared."
                                ),
                            },
                        }
                    ],
                    "close": {"type": "plain_text", "text": "Finish"},
                },
            )
        except Exception as e:
            logger.warning("Could not update view before uninstall: %s", e)

        # Trigger full uninstallation pipeline
        await self.integration_service.uninstall_workspace(workspace_id)

    @interaction_handler(action_ids.EXECUTE_HUBSPOT_ONLY_UNINSTALL)
    @with_slack_error_handling(action_ids.EXECUTE_HUBSPOT_ONLY_UNINSTALL)
    async def _handle_hubspot_only_uninstall(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Performs a selective HubSpot wipe (preserving Slack identity)."""
        workspace_id = context.private_metadata or integration.workspace_id
        logger.info("HubSpot-only uninstall triggered for workspace=%s", workspace_id)

        await self.integration_service.uninstall_hubspot(workspace_id)

        # Use resolved client to handle shell records (Identity Bridge)
        client = await self.integration_service.get_slack_client(integration)
        await client.views_update(
            view_id=context.view_id,
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "HubSpot Disconnected"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "🧹 *HubSpot portal disconnected.* Your data has been "
                                "scoured, but Slack remains ready for other portal "
                                "connections."
                            ),
                        },
                    }
                ],
                "close": {"type": "plain_text", "text": "Finish"},
            },
        )

    @interaction_handler(action_ids.CANCEL_UNINSTALL)
    async def _handle_cancel_uninstall(
        self,
        **kwargs: Any,
    ) -> None:
        """No-op: Closed via Slack's built-in modal mechanisms."""
        pass
