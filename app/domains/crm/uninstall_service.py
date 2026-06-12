"""Workspace uninstallation domain service.

Extracted from ``IntegrationService`` (H-01 Phase 2) to satisfy the
Single Responsibility Principle.  All teardown logic — Stripe subscription
cancellation, provider token revocation, data deletion, and cache purging —
lives here so ``IntegrationService`` is no longer responsible for
uninstallation concerns.

``IntegrationService`` retains thin delegation wrappers for backwards
compatibility with all existing call sites.
"""

from __future__ import annotations

import asyncio
from functools import cached_property
from typing import TYPE_CHECKING, cast

from app.connectors.common.registry import ChannelRegistry
from app.core.logging import get_logger
from app.db.records import Provider

if TYPE_CHECKING:
    from app.connectors.slack.slack_channel import SlackChannel
    from app.db.storage_service import StorageService
    from app.domains.billing.tier_service import TierService
    from app.domains.crm.hubspot.service import HubSpotService

logger = get_logger("crm.uninstall_service")


class UninstallService:
    """Handles all workspace teardown operations across providers.

    Responsibilities:
    - Cancel active Stripe subscriptions (at period end, non-destructive).
    - Revoke HubSpot OAuth tokens via outbound API call.
    - Revoke Slack app installation via outbound API call.
    - Delete all workspace data from Supabase (cascade or manual fallback).
    - Invalidate the tier cache via TierService.

    Dependencies are injected to avoid circular imports and allow testing.
    """

    def __init__(
        self,
        storage: StorageService,
        corr_id: str,
        tier_service: TierService,
    ) -> None:
        """Initialise with required dependencies.

        Args:
            storage: Active ``StorageService`` instance.
            corr_id: Correlation ID for request tracing.
            tier_service: The workspace's ``TierService`` for cache invalidation.

        """
        self.storage = storage
        self.corr_id = corr_id
        self.tier_service = tier_service

    @cached_property
    def hubspot_service(self) -> HubSpotService:
        """Lazy-loaded to avoid circular imports and unnecessary initialisation."""
        from app.domains.crm.hubspot.service import HubSpotService

        return HubSpotService(self.corr_id, storage=self.storage)

    async def uninstall_workspace(
        self,
        workspace_id: str,
        trigger_hubspot_uninstall: bool = True,
        trigger_slack_uninstall: bool = True,
    ) -> None:
        """Fully uninstall all integrations for a workspace and reset state.

        Triggers proactive uninstallation on both Slack and HubSpot if applicable.

        Args:
            workspace_id: The workspace to uninstall.
            trigger_hubspot_uninstall: Whether to revoke the HubSpot token.
            trigger_slack_uninstall: Whether to revoke the Slack token.

        """
        workspace = await self.storage.get_workspace(workspace_id)
        if not workspace:
            logger.info("Workspace %s already uninstalled; skipping.", workspace_id)
            return

        logger.info("Resetting all integrations for workspace_id=%s", workspace_id)

        # 1. Audit log
        from app.domains.common.audit_service import AuditService

        audit = AuditService(corr_id=self.corr_id)
        await audit.log_action(
            action="slack_uninstall",
            workspace_id=workspace_id,
            metadata={"trigger": "proactive_reset"},
        )

        # 2. Cancel Stripe subscription at period end (non-destructive)
        if workspace.subscription_id and workspace.subscription_status in (
            "active",
            "trialing",
        ):
            try:
                import stripe  # lazy — only needed on uninstall path

                await asyncio.to_thread(
                    stripe.Subscription.modify,
                    workspace.subscription_id,
                    cancel_at_period_end=True,
                )
                logger.info(
                    "Set Stripe subscription %s to cancel_at_period_end "
                    "for workspace_id=%s during global uninstall",
                    workspace.subscription_id,
                    workspace_id,
                )
            except Exception as exc:
                logger.error(
                    "Failed to set cancel_at_period_end for workspace_id=%s: %s",
                    workspace_id,
                    exc,
                )

        # 3. Revoke HubSpot token
        if trigger_hubspot_uninstall:
            try:
                hs_integration = await self.storage.get_integration(
                    workspace_id, Provider.HUBSPOT
                )
                if hs_integration:
                    logger.info("Triggering outbound HubSpot uninstallation")
                    await self.hubspot_service.uninstall_app(workspace_id)
            except Exception as exc:
                logger.warning("Outbound HubSpot uninstallation failed: %s", exc)

        # 4. Revoke Slack app installation
        if trigger_slack_uninstall:
            try:
                slack_integration = await self.storage.get_integration(
                    workspace_id, Provider.SLACK
                )
                if slack_integration:
                    from app.domains.crm.integration_service import IntegrationService

                    logger.info("Triggering outbound Slack uninstallation")
                    slack_channel = ChannelRegistry.get_channel(
                        Provider.SLACK, corr_id=self.corr_id
                    )

                    bot_token = slack_integration.slack_bot_token
                    if not bot_token:
                        try:
                            integ_svc = IntegrationService(
                                self.corr_id, storage=self.storage
                            )
                            client = await integ_svc.get_slack_client(slack_integration)
                            bot_token = client.bot_token
                        except Exception as exc:
                            logger.warning(
                                "Failed to resolve Slack bot token via Identity Bridge: %s",
                                exc,
                            )

                    cast("SlackChannel", slack_channel).bot_token = bot_token
                    await cast("SlackChannel", slack_channel).apps_uninstall()
            except Exception as exc:
                logger.warning("Outbound Slack uninstallation failed: %s", exc)

        # 5. Delete all integrations data
        try:
            # We must invalidate caches BEFORE atomic delete
            # Use gather to safely attempt both deletions without failing the entire process
            await asyncio.gather(
                self.storage.integration_svc.delete_integration(
                    workspace_id, Provider.HUBSPOT
                ),
                self.storage.integration_svc.delete_integration(
                    workspace_id, Provider.SLACK
                ),
                return_exceptions=True,
            )

            # Parallel individual table deletes
            await asyncio.gather(
                self.storage.thread_mappings.delete({"workspace_id": workspace_id}),
                self.storage.user_mappings.delete({"workspace_id": workspace_id}),
                return_exceptions=True,
            )
            logger.info("Integrations for %s deleted successfully", workspace_id)
        except Exception as err:
            logger.warning(
                "Uninstallation data cleanup failed: %s.",
                err,
            )

        # 6. Purge tier cache
        await self.tier_service.invalidate_tier_cache(workspace_id)

    async def uninstall_hubspot(self, workspace_id: str) -> None:
        """Uninstall only the HubSpot integration (Slack remains active).

        Called by the HubSpot webhook uninstall endpoint.

        Args:
            workspace_id: The workspace to uninstall HubSpot from.

        """
        logger.info("Uninstalling ONLY HubSpot for workspace=%s", workspace_id)

        # 1. Trigger outbound API uninstall if possible
        try:
            await self.hubspot_service.uninstall_app(workspace_id)
        except Exception as exc:
            logger.warning("Outbound HubSpot uninstallation failed: %s", exc)

        # 2. Delete ONLY the HubSpot integration from the DB
        try:
            await self.storage.integration_svc.delete_integration(
                workspace_id, Provider.HUBSPOT
            )
            logger.info(
                "Deleted HubSpot integration record for workspace=%s", workspace_id
            )
        except Exception as exc:
            logger.error("Failed to delete HubSpot integration record: %s", exc)

        # 3. Purge tier cache
        await self.tier_service.invalidate_tier_cache(workspace_id)

    async def uninstall_slack(self, workspace_id: str) -> None:
        """Uninstall only the Slack integration (HubSpot remains active).

        Called by Slack's app_uninstalled event handler.

        Args:
            workspace_id: The workspace to uninstall Slack from.

        """
        logger.info("Uninstalling ONLY Slack for workspace=%s", workspace_id)

        try:
            # 1. Fetch integration before deletion to get its ID for cache clearing
            from app.db.records import Provider

            integration = await self.storage.integration_svc.get_integration(
                workspace_id, Provider.SLACK
            )
            integration_id = integration.id if integration else None

            # 2. Delete the record
            await self.storage.integration_svc.delete_integration(
                workspace_id, Provider.SLACK
            )
            logger.info(
                "Deleted Slack integration record for workspace=%s", workspace_id
            )

            # 3. Proactively invalidate the in-memory SlackClient cache
            if integration_id:
                from app.domains.crm.slack_client_service import SlackClientService

                await SlackClientService._slack_client_cache.invalidate(
                    f"slack_source_{integration_id}"
                )
                await SlackClientService._slack_client_cache.invalidate(
                    f"slack_{integration_id}"
                )

        except Exception as exc:
            logger.error("Failed to delete Slack integration record: %s", exc)

        # Purge tier cache
        await self.tier_service.invalidate_tier_cache(workspace_id)
