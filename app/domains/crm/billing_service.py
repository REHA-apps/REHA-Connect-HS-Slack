from __future__ import annotations  # noqa: D100

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.logging import get_logger
from app.db.records import Provider, WorkspaceRecord
from app.db.storage_service import StorageService
from app.domains.messaging.slack.service import SlackMessagingService

if TYPE_CHECKING:
    pass

logger = get_logger("billing.service")

_TRIAL_REMINDER_DAYS_LEFT = 2  # "Day 5" = 2 days remaining in 7-day trial


class BillingService:
    """Service for managing subscription lifecycles, trial expiries, and reminders."""

    def __init__(
        self, corr_id: str | None = None, storage: StorageService | None = None
    ) -> None:
        self.corr_id = corr_id or "system"
        self.storage = storage or StorageService(corr_id=self.corr_id)

    async def check_trial_expiries(self) -> None:
        """Scans all workspaces for expired trials and downgrades them to free."""
        # 1. Handle Expiries (trial_ends_at < now)
        now = datetime.now(UTC)
        # We'd need a way to filter by trial_ends_at in storage_service.
        # For now, let's assume we list trialing workspaces.
        # Note: listing all might be expensive if many, but fine for now.

        # Fetch only workspaces currently on a trial plan
        workspaces = await self.storage.workspaces.fetch_many({"plan": "trial"})

        tasks = []
        for ws in workspaces:
            if ws.trial_ends_at and ws.trial_ends_at < now:
                tasks.append(self._downgrade_expired_trial(ws))
            elif ws.trial_ends_at:
                days_left = (ws.trial_ends_at - now).days
                if days_left == _TRIAL_REMINDER_DAYS_LEFT:
                    tasks.append(self._send_trial_reminder(ws, days_left))

        if tasks:
            logger.info(
                "Executing %d billing maintenance tasks in parallel", len(tasks)
            )  # noqa: E501
            await asyncio.gather(*tasks)

    async def _downgrade_expired_trial(self, workspace: WorkspaceRecord) -> None:
        logger.info(
            "Trial expired for workspace_id=%s. Downgrading to free.", workspace.id
        )
        await self.storage.upsert_workspace(
            workspace_id=workspace.id, plan="free", subscription_status="inactive"
        )
        # TODO: notify user via Slack?

    async def _send_trial_reminder(
        self, workspace: WorkspaceRecord, days_left: int
    ) -> None:
        logger.info(
            "Sending trial reminder to workspace_id=%s (%s days left)",
            workspace.id,
            days_left,
        )

        # Fetch Slack integration to send message
        slack_integ = await self.storage.get_integration(workspace.id, Provider.SLACK)
        if not slack_integ:
            logger.warning(
                "No Slack integration found for workspace_id=%s, cannot send reminder",
                workspace.id,
            )
            return

        # Initialize MessagingService
        from app.domains.crm.integration_service import IntegrationService

        integration_service = IntegrationService(self.corr_id, storage=self.storage)
        messaging_service = SlackMessagingService(
            corr_id=self.corr_id,
            integration_service=integration_service,
            slack_integration=slack_integ,
        )

        message = (
            f"🔔 *Plan Reminder*: Your 7-day Pro trial expires in {days_left} days. "
            "Upgrade now to keep your advanced CRM notifications active! "
            f"Visit <{settings.PRICING_URL}"
            f"?portal_id={workspace.portal_id}"
            f"&state={workspace.id}|Pricing> to subscribe."
        )

        # Resolve default channel or use metadata
        channel = slack_integ.metadata.get("channel_id")
        if not channel:
            # Fallback to general lookup or specific discovery
            logger.warning(
                "No default channel set for reminder in workspace_id=%s", workspace.id
            )
            return

        await messaging_service.send_message(
            workspace_id=workspace.id, channel=channel, text=message
        )
        logger.info("Trial reminder sent successfully to channel=%s", channel)
