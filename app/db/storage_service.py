from __future__ import annotations

from typing import Any

from app.db.records import (
    AIKeywordRecord,
    AIScoreRecord,
    GhostingHeartbeatRecord,
    IntegrationRecord,
    Provider,
    ScheduledDigestRecord,
    ScoringConfigRecord,
    ThreadMappingRecord,
    UserMappingRecord,
    WorkspaceRecord,
)
from app.db.repository import SupabaseRepository
from app.db.services.base import BaseStorage
from app.db.services.integration_storage import IntegrationStorage
from app.db.services.workspace_storage import WorkspaceStorage


class StorageService(BaseStorage):
    """Refactored storage service utilizing specialized domain sub-services.

    Acts as a facilitator for persistence operations while delegating core
    logic to specialized components like WorkspaceStorage and IntegrationStorage.
    """

    def __init__(self, corr_id: str | None = None) -> None:
        super().__init__(corr_id=corr_id)

        # Domain Sub-Services
        self.workspace_svc = WorkspaceStorage(client=self.client, corr_id=self.corr_id)
        self.integration_svc = IntegrationStorage(
            client=self.client, corr_id=self.corr_id
        )
        from app.db.services.idempotency_storage import IdempotencyStorage

        self.idempotency_svc = IdempotencyStorage(
            client=self.client, corr_id=self.corr_id
        )

        # Legacy/Shared Repositories (To be extracted in Phase 2)
        self.thread_mappings = SupabaseRepository[ThreadMappingRecord](
            client=self.client, table="thread_mappings", model=ThreadMappingRecord
        )
        self.scoring_configs = SupabaseRepository[ScoringConfigRecord](
            client=self.client, table="scoring_configs", model=ScoringConfigRecord
        )
        self.ai_scores = SupabaseRepository[AIScoreRecord](
            client=self.client, table="ai_scores", model=AIScoreRecord
        )
        self.user_mappings = SupabaseRepository[UserMappingRecord](
            client=self.client, table="user_mappings", model=UserMappingRecord
        )
        self.ai_keywords = SupabaseRepository[AIKeywordRecord](
            client=self.client, table="intent_keywords", model=AIKeywordRecord
        )
        self.ghosting_heartbeats = SupabaseRepository[GhostingHeartbeatRecord](
            client=self.client,
            table="ghosting_heartbeats",
            model=GhostingHeartbeatRecord,
        )
        from app.db.records import ScheduledDigestRecord

        self.scheduled_digests = SupabaseRepository[ScheduledDigestRecord](
            client=self.client,
            table="scheduled_digests",
            model=ScheduledDigestRecord,
        )

        # Backward Compatibility Aliases (CR-Refactor)
        self.integrations = self.integration_svc.integrations
        self.workspaces = self.workspace_svc.workspaces

    # --- Workspace Delegation ---
    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        return await self.workspace_svc.get_workspace(workspace_id)

    async def upsert_workspace(self, **payload: Any) -> WorkspaceRecord:
        return await self.workspace_svc.upsert_workspace(**payload)

    async def start_trial_workspace(self, **kwargs: Any) -> WorkspaceRecord:
        return await self.workspace_svc.start_trial_workspace(**kwargs)

    async def increment_usage_metrics(self, **kwargs: Any) -> WorkspaceRecord | None:
        return await self.workspace_svc.increment_usage_metrics(**kwargs)

    # --- Integration Delegation ---
    async def get_integration(
        self, workspace_id: str, provider: Provider, slack_user_id: str | None = None
    ) -> IntegrationRecord | None:
        return await self.integration_svc.get_integration(
            workspace_id, provider, slack_user_id
        )

    async def check_integration_exists(
        self, workspace_id: str, provider: Provider
    ) -> bool:
        return await self.integration_svc.check_integration_exists(
            workspace_id, provider
        )

    async def get_integration_count_by_slack_team_id(self, team_id: str) -> int:
        return await self.integration_svc.get_integration_count_by_slack_team_id(
            team_id
        )

    async def upsert_integration(self, payload: dict[str, Any]) -> IntegrationRecord:
        return await self.integration_svc.upsert_integration(payload)

    async def get_integration_by_portal_id(
        self, portal_id: str
    ) -> IntegrationRecord | None:
        return await self.integration_svc.get_integration_by_portal_id(portal_id)

    async def get_integration_by_slack_team_id(
        self, team_id: str
    ) -> IntegrationRecord | None:
        return await self.integration_svc.get_integration_by_slack_team_id(team_id)

    async def list_integrations_by_slack_team_id(
        self, team_id: str
    ) -> list[IntegrationRecord]:
        return await self.integration_svc.list_integrations_by_slack_team_id(team_id)

    async def list_integrations_for_workspace(
        self, workspace_id: str
    ) -> list[IntegrationRecord]:
        return await self.integration_svc.list_integrations_for_workspace(workspace_id)

    # --- User Mapping Delegation ---
    async def get_user_mapping(
        self, workspace_id: str, hubspot_owner_id: int
    ) -> UserMappingRecord | None:
        """Fetches a single user mapping for a workspace by owner ID."""
        return await self.user_mappings.fetch_single(
            {"workspace_id": workspace_id, "hubspot_owner_id": hubspot_owner_id}
        )

    async def get_all_user_mappings(self, workspace_id: str) -> list[UserMappingRecord]:
        """Fetches all user mappings for a workspace."""
        return await self.user_mappings.fetch_many({"workspace_id": workspace_id})

    async def upsert_user_mapping(self, payload: dict[str, Any]) -> UserMappingRecord:
        """Saves a user mapping record, handling conflicts on workspace+owner."""
        return await self.user_mappings.upsert(
            payload, on_conflict="workspace_id,hubspot_owner_id"
        )

    async def update_integration(
        self, workspace_id: str, provider: Provider, credentials: dict[str, Any]
    ) -> IntegrationRecord:
        """Backward compatibility helper for partial token updates."""
        # Find existing
        existing = await self.get_integration(workspace_id, provider)
        if not existing:
            raise ValueError(f"No integration found for {workspace_id} {provider}")

        # Merge credentials
        updated_creds = dict(existing.credentials)
        updated_creds.update(credentials)

        # Upsert
        return await self.upsert_integration(
            {
                "workspace_id": workspace_id,
                "provider": provider,
                "credentials": updated_creds,
            }
        )

    # --- Mapping & Logic (Direct Repository Access) ---
    async def get_thread_mapping(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        channel_id: str | None = None,
    ) -> ThreadMappingRecord | None:
        """Resolves a thread mapping with multi-tenant safety."""
        filters = {
            "workspace_id": workspace_id,
            "object_type": object_type,
            "object_id": object_id,
        }
        if channel_id:
            filters["channel_id"] = channel_id
        return await self.thread_mappings.fetch_single(filters)

    async def save_thread_mapping(
        self, record: ThreadMappingRecord
    ) -> ThreadMappingRecord:
        return await self.thread_mappings.upsert(record.to_supabase())

    async def delete_thread_mapping(
        self, workspace_id: str, object_type: str, object_id: str
    ) -> None:
        """Removes a thread mapping for a specific object."""
        await self.thread_mappings.delete(
            {
                "workspace_id": workspace_id,
                "object_type": object_type,
                "object_id": object_id,
            }
        )

    async def get_ai_intent_keywords(self, category: str) -> dict[str, list[str]]:
        """Retrieves and groups intent keywords for AI analysis."""
        records = await self.ai_keywords.fetch_many({"category": category})
        results: dict[str, list[str]] = {}
        for r in records:
            if r.category not in results:
                results[r.category] = []
            results[r.category].append(r.keyword)
        return results

    async def ensure_scoring_config(self, workspace_id: str) -> ScoringConfigRecord:
        """Fetches or creates the default AI scoring configuration for a workspace."""
        config = await self.scoring_configs.fetch_single({"workspace_id": workspace_id})
        if not config:
            # Create default (Repo upsert handles defaults defined in ScoringConfigRecord)
            config = await self.scoring_configs.upsert(
                {"workspace_id": workspace_id}, on_conflict="workspace_id"
            )
        return config

    async def delete_workspace_cascade(self, workspace_id: str) -> None:
        """Handles full uninstallation of all workspace data."""
        await self.client.rpc("delete_workspace_cascade", {"ws_id": workspace_id})

    # Note: Other minor helper methods like delete_all_* can remain here or
    # be called via self.integrations.delete(...) directly in the services.

    async def list_integrations(
        self, workspace_id: str, provider: str = "slack", limit: int = 50
    ) -> list[IntegrationRecord]:
        """Paginated alias for list_integrations_for_workspace."""
        integrations = await self.list_integrations_for_workspace(workspace_id)
        if provider:
            integrations = [i for i in integrations if i.provider == provider]
        return integrations[:limit]

    async def get_thread_mapping_by_ts(
        self, workspace_id: str, channel_id: str, thread_ts: str
    ) -> ThreadMappingRecord | None:
        data = await self.client.fetch_single(
            "thread_mappings",
            {
                "workspace_id": workspace_id,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
            },
        )
        return ThreadMappingRecord(**data) if data else None

    async def upsert_thread_mapping(
        self, record: dict[str, Any] | ThreadMappingRecord
    ) -> bool:
        if isinstance(record, dict):
            record_dict = record
        else:
            record_dict = record.model_dump(exclude_none=True)

        workspace_id = record_dict.get("workspace_id")
        object_type = record_dict.get("object_type")
        object_id = record_dict.get("object_id")

        if not workspace_id or not object_type or not object_id:
            return False

        # Atomic upsert to prevent race conditions during concurrent Lambda invocations
        data = await self.client.upsert(
            "thread_mappings",
            record_dict,
            on_conflict="workspace_id,object_type,object_id",
        )

        return data is not None

    async def delete_user_mapping(
        self, workspace_id: str, provider_user_id: str
    ) -> bool:
        res = await self.client.delete(
            "user_mappings",
            {"workspace_id": workspace_id, "provider_user_id": provider_user_id},
        )
        return bool(res)

    async def list_due_digests(self) -> list[ScheduledDigestRecord]:
        """Fetch digests that are due (filtering logic should be applied upstream based on cron and timezone)."""
        return await self.scheduled_digests.fetch_many({})

    async def upsert_scheduled_digest(
        self, payload: dict[str, Any]
    ) -> ScheduledDigestRecord:
        """Create or update a scheduled digest configuration."""
        return await self.scheduled_digests.upsert(payload, on_conflict="id")

    async def list_scheduled_digests_for_workspace(
        self, workspace_id: str
    ) -> list[ScheduledDigestRecord]:
        """Fetch all scheduled digests for a workspace."""
        return await self.scheduled_digests.fetch_many({"workspace_id": workspace_id})

    async def delete_scheduled_digest(self, digest_id: str) -> bool:
        """Delete a scheduled digest configuration."""
        res = await self.client.delete("scheduled_digests", {"id": digest_id})
        return bool(res)
