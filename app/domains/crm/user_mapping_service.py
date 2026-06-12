from __future__ import annotations  # noqa: D100

import asyncio
from typing import Any

from app.connectors.slack.slack_channel import SlackChannel
from app.core.logging import get_logger
from app.db.records import Provider
from app.db.storage_service import StorageService
from app.domains.crm.hubspot.service import HubSpotService

logger = get_logger("user.mapping.service")


class UserMappingService:
    """Service to handle the synchronization of HubSpot owners and Slack users."""

    def __init__(
        self, corr_id: str | None = None, *, storage: StorageService | None = None
    ):
        self.corr_id = corr_id or "system"
        self.storage = storage or StorageService(corr_id=self.corr_id)
        self.hubspot = HubSpotService(self.corr_id, storage=self.storage)

    async def _get_slack_channel(self, workspace_id: str) -> SlackChannel | None:
        """Helper to instantiate a SlackChannel for a workspace, supporting identity bridges."""
        from app.domains.crm.integration_service import IntegrationService

        integ_svc = IntegrationService(self.corr_id, storage=self.storage)

        # 1. Resolve HubSpot integration first (to find bridge context)
        hubspot = await integ_svc.resolve_hubspot_integration(workspace_id)

        # 2. Get the authenticated Slack client using the bridge
        # (This handles the jump to the parent Slack workspace automatically)
        if hubspot:
            try:
                slack_client = await integ_svc.get_slack_client(hubspot)
                return SlackChannel(
                    corr_id=self.corr_id,
                    slack_client=slack_client,
                    portal_id=int(hubspot.metadata.get("portal_id", 0)) or None,
                )
            except Exception as e:
                logger.warning(
                    "Failed to resolve Slack client via bridge for %s: %s",
                    workspace_id,
                    e,
                )

        # 3. Fallback: Direct lookup for native Slack installations
        integration = await self.storage.get_integration(workspace_id, Provider.SLACK)
        if not integration:
            return None

        async def _handle_refresh(
            new_at: str, new_rt: str | None, new_exp: int | None
        ) -> None:
            logger.info(
                "Persisting refreshed Slack tokens for workspace_id=%s", workspace_id
            )
            await self.storage.update_integration(
                workspace_id=workspace_id,
                provider=Provider.SLACK,
                credentials={
                    "access_token": new_at,
                    "refresh_token": new_rt,
                    "expires_at": new_exp,
                },
            )

        return SlackChannel(
            corr_id=self.corr_id,
            bot_token=integration.slack_bot_token,
            refresh_token=integration.refresh_token,
            expires_at=integration.expires_at,
            portal_id=int(integration.portal_id) if integration.portal_id else None,
            on_token_refresh=_handle_refresh,
        )

    async def sync_workspace(self, workspace_id: str) -> dict[str, Any]:  # noqa: PLR0912
        """Perform a full sync of HubSpot owners and Slack users.

        Matches users based on email address. Preserves any manual overrides.

        Args:
            workspace_id: The internal workspace ID.

        Returns:
            A dictionary containing sync statistics.

        """
        logger.info("Starting user mapping sync for workspace_id=%s", workspace_id)

        # 1. Fetch HubSpot Owners
        hs_owners = await self.hubspot.get_owners(workspace_id)
        if not hs_owners:
            logger.warning("No HubSpot owners found for workspace_id=%s", workspace_id)
            return {"status": "no_hubspot_owners"}

        # 2. Fetch Slack Users
        slack_channel = await self._get_slack_channel(workspace_id)
        slack_users_by_email: dict[str, str] = {}

        if slack_channel:
            slack_users = await slack_channel.get_all_users()
            for su in slack_users:
                if su.get("deleted") or su.get("is_bot") or su.get("is_app_user"):
                    continue

                profile = su.get("profile", {})
                email = profile.get("email")
                if email:
                    slack_users_by_email[email.lower()] = su.get("id", "")
        else:
            logger.warning(
                "No Slack integration found for workspace_id=%s", workspace_id
            )

        # 3. Fetch Existing Mappings
        existing_records = await self.storage.get_all_user_mappings(workspace_id)
        existing_map = {r.hubspot_owner_id: r for r in existing_records}

        # 4. Process matches and upsert
        stats = {
            "total_owners": len(hs_owners),
            "new_mapped": 0,
            "updated_mapped": 0,
            "manual_skipped": 0,
            "unmapped": 0,
        }

        for owner in hs_owners:
            owner_id = int(owner.get("id", 0))
            if not owner_id:
                continue

            email = owner.get("email", "")
            slack_id = slack_users_by_email.get(email.lower()) if email else None

            existing = existing_map.get(owner_id)

            # Preserve manual mappings
            if existing and existing.mapping_status == "manual":
                stats["manual_skipped"] += 1
                continue

            # Determine if this constitutes an update or new mapping
            if existing:
                # If nothing changed, skip DB call for efficiency
                if (
                    existing.slack_user_id == slack_id
                    and existing.hubspot_email == email
                ):
                    # Still record them in stats depending on state
                    if slack_id:
                        stats["updated_mapped"] += 1
                    else:
                        stats["unmapped"] += 1
                    continue

                if slack_id:
                    stats["updated_mapped"] += 1
                else:
                    stats["unmapped"] += 1
            elif slack_id:
                stats["new_mapped"] += 1
            else:
                stats["unmapped"] += 1

            # Perform Upsert
            payload = {
                "workspace_id": workspace_id,
                "hubspot_owner_id": owner_id,
                "hubspot_email": email,
                "slack_user_id": slack_id,
                "mapping_status": "auto",
            }
            await self.storage.upsert_user_mapping(payload)

        return stats

    async def get_enriched_mappings(self, workspace_id: str) -> dict[str, Any]:
        """Fetch user mappings and enrich with real names from HubSpot and Slack."""
        # 1. Fetch data in parallel
        mappings = await self.storage.get_all_user_mappings(workspace_id)
        hs_owners_task = self.hubspot.get_owners(workspace_id)
        slack_channel_task = self._get_slack_channel(workspace_id)

        hs_owners, slack_channel = await asyncio.gather(
            hs_owners_task, slack_channel_task
        )

        slack_users = []
        slack_auth_error = False
        if slack_channel:
            try:
                slack_users = await slack_channel.get_all_users()
            except Exception as e:
                logger.error("Slack authentication error during enrichment: %s", e)
                slack_auth_error = True

        # 2. Build lookup maps
        owner_map = {int(o["id"]): o for o in hs_owners}
        slack_map = {u["id"]: u for u in slack_users}

        # 3. Enrich
        enriched = []
        for m in mappings:
            data = m.model_dump()

            # Enrich HubSpot
            owner = owner_map.get(m.hubspot_owner_id)
            if owner:
                fname = owner.get("firstName") or ""
                lname = owner.get("lastName") or ""
                data["hubspot_name"] = f"{fname} {lname}".strip() or owner.get("email")
            else:
                data["hubspot_name"] = f"Unknown Owner ({m.hubspot_owner_id})"

            # Enrich Slack
            if m.slack_user_id:
                slack_user = slack_map.get(m.slack_user_id)
                if slack_user:
                    profile = slack_user.get("profile", {})
                    data["slack_name"] = (
                        profile.get("real_name")
                        or profile.get("display_name")
                        or slack_user.get("name")
                        or m.slack_user_id
                    )
                elif slack_auth_error:
                    data["slack_name"] = f"Disconnected (ID: {m.slack_user_id})"
                else:
                    data["slack_name"] = f"Unknown User ({m.slack_user_id})"
            else:
                data["slack_name"] = "Not Mapped"

            enriched.append(data)

        return {"mappings": enriched, "slack_auth_error": slack_auth_error}
