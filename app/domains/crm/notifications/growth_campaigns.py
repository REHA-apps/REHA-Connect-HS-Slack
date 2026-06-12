from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.db.records import Provider
from app.db.storage_service import StorageService
from app.domains.ai.service import AIService
from app.domains.crm.hubspot.service import HubSpotService
from app.domains.crm.integration_service import IntegrationService
from app.domains.messaging.factory import get_messaging_service

logger = get_logger("crm.notifications")


class GrowthCampaignsMixin:
    """Mixin for Notification capabilities."""

    if TYPE_CHECKING:
        corr_id: str
        storage: StorageService
        hubspot: HubSpotService
        integration_service: IntegrationService
        ai: AIService

    async def _is_day4_reminder_due(self, workspace_id: str) -> bool:
        """Checks if the workspace is eligible for the Day 4 Momentum DM."""
        workspace = await self.storage.get_workspace(workspace_id)
        if not workspace or workspace.sent_day4_reminder:
            return False

        install_date = workspace.install_date or workspace.created_at
        if not install_date:
            return False

        if install_date.tzinfo is None:
            install_date = install_date.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        age_days = (now - install_date).days

        return 4 <= age_days <= 5  # noqa: PLR2004

    async def _send_growth_reminder(self, workspace_id: str) -> None:
        """Sends the Momentum DM to the workspace admin."""
        if not await self._is_day4_reminder_due(workspace_id):
            return

        logger.debug("Triggering Day 4 Momentum DM for workspace %s", workspace_id)

        target_integ = await self.storage.get_integration(workspace_id, Provider.SLACK)
        if not target_integ:
            return

        messaging_service = await get_messaging_service(
            workspace_id=workspace_id,
            storage=self.storage,
            corr_id=self.corr_id,
            integration_record=target_integ,
        )
        if not messaging_service:
            return

        workspace = await self.storage.get_workspace(workspace_id)
        if not workspace:
            return

        # Resolve admin for the DM
        admin_id = target_integ.metadata.get("admin_user_id")
        if not admin_id:
            # Fallback to resolving from owner mapping if available
            mappings = await self.storage.get_all_user_mappings(workspace_id)
            if mappings:
                admin_id = mappings[0].slack_user_id

        if not admin_id:
            return

        count = workspace.total_sync_count or 0
        message = (
            f"🚀 *Record-Breaking Week for your CRM!*\n\n"
            f"You've interacted with *{count} CRM records* since installing REHA Connect. "
            f"Your team is moving faster than ever.\n\n"
            f"Ready to unlock Unlimited Notifications and Bi-directional sync? "
            f"Upgrade to Pro today and keep the momentum alive! ⚡️"
        )

        # Send private DM to admin
        await messaging_service.send_message(
            workspace_id=workspace_id,
            channel=admin_id,
            text=message,
            unfurl_links=False,
        )

        # Mark as sent to prevent duplicate reminders
        await self.storage.upsert_workspace(
            workspace_id=workspace_id, sent_day4_reminder=True
        )
        logger.debug("Momentum DM sent successfully to admin %s", admin_id)
