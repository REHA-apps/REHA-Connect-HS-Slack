from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.db.records import PlanTier, WorkspaceRecord
from app.db.repository import SupabaseRepository
from app.db.services.base import BaseStorage


class WorkspaceStorage(BaseStorage):
    """Storage service for workspace-level operations (billing, limits, trials)."""

    def __init__(self, client=None, corr_id=None) -> None:
        super().__init__(client, corr_id)
        self.workspaces = SupabaseRepository[WorkspaceRecord](
            client=self.client,
            table="workspaces",
            model=WorkspaceRecord,
        )

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        """Retrieves a workspace record by its unique identifier.

        Args:
            workspace_id: The UUID of the workspace to retrieve.

        Returns:
            The workspace record if found, otherwise None.

        """
        return await self.workspaces.fetch_single({"id": workspace_id})

    async def get_workspace_by_stripe_customer_id(
        self, customer_id: str
    ) -> WorkspaceRecord | None:
        """Retrieves a workspace record by its Stripe customer ID."""
        return await self.workspaces.fetch_single({"stripe_customer_id": customer_id})

    async def upsert_workspace(self, **payload: Any) -> WorkspaceRecord:
        # Filter None values
        payload = {k: v for k, v in payload.items() if v is not None}

        # "Claim" the slack_team_id: if another workspace already holds this team_id,
        # clear it there first to avoid the unique_slack_team_id constraint violation.
        # This handles the multi-portal case where a user connects a second HubSpot
        # portal using the same Slack workspace.
        slack_team_id = payload.get("slack_team_id")
        current_id = payload.get("id")
        if slack_team_id and current_id:
            existing = await self.workspaces.fetch_single(
                {"slack_team_id": slack_team_id}
            )
            if existing and existing.id != current_id:
                from app.core.logging import get_logger

                _log = get_logger("workspace_storage")
                _log.info(
                    "Clearing slack_team_id=%s from workspace=%s before claiming it for workspace=%s",
                    slack_team_id,
                    existing.id,
                    current_id,
                )
                await self.workspaces.update(
                    {"id": existing.id},
                    {"slack_team_id": None},
                )

        return await self.workspaces.upsert(payload)

    async def start_trial_workspace(
        self,
        workspace_id: str,
        primary_email: str | None = None,
        portal_id: str | None = None,
        slack_team_id: str | None = None,
        trial_days: int = 7,
    ) -> WorkspaceRecord:
        # Check if workspace already exists to enforce Trial Retention (180-Day Rule)
        existing = await self.get_workspace(workspace_id)
        if existing:
            # Preserve existing billing and trial state, just update identity mappings
            return await self.upsert_workspace(
                id=workspace_id,
                primary_email=primary_email,
                portal_id=portal_id,
                slack_team_id=slack_team_id,
            )

        now = datetime.now(UTC)
        return await self.upsert_workspace(
            id=workspace_id,
            primary_email=primary_email,
            portal_id=portal_id,
            slack_team_id=slack_team_id,
            plan=PlanTier.TRIAL,
            subscription_status="trialing",
            trial_ends_at=now + timedelta(days=trial_days),
            install_date=now,
        )

    async def increment_usage_metrics(
        self, workspace_id: str, is_notification: bool = False
    ) -> WorkspaceRecord | None:
        """Increments workspace usage metrics atomically via RPC or fallback logic.

        Args:
            workspace_id: The UUID of the workspace.
            is_notification: Whether this increment is for a notification event.

        Returns:
            The updated workspace record, or None if the workspace was not found.

        """
        # Atomic RPC fallback logic moved here...
        try:
            result = await self.client.rpc(
                "increment_workspace_usage",
                {
                    "p_workspace_id": workspace_id,
                    "p_is_notification": is_notification,
                },
            )
            if result:
                data = result[0] if isinstance(result, list) else result
                return WorkspaceRecord(**data)
        except Exception:
            pass

        # Fallback to non-atomic read-modify-write
        workspace = await self.get_workspace(workspace_id)
        if not workspace:
            return None

        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "id": workspace_id,
            "total_sync_count": (workspace.total_sync_count or 0) + 1,
        }

        last_reset = workspace.last_limit_reset
        should_reset = last_reset is None or (
            now.year != last_reset.year or now.month != last_reset.month
        )

        if should_reset:
            payload["notification_count_monthly"] = 1 if is_notification else 0
            payload["last_limit_reset"] = now
        elif is_notification:
            payload["notification_count_monthly"] = (
                workspace.notification_count_monthly or 0
            ) + 1

        return await self.workspaces.upsert(payload)
