from __future__ import annotations

from typing import Any

from app.db.records import CRM_SCHEMAS, IntegrationRecord, Provider
from app.db.repository import SupabaseRepository
from app.db.services.base import BaseStorage, logger
from app.utils.cache import AsyncTTL

# Module-level caches for global lookup efficiency
_record_cache = AsyncTTL[IntegrationRecord | None](ttl=300)
_hubspot_mapping_cache = AsyncTTL[str](ttl=300)


class IntegrationStorage(BaseStorage):
    """Storage service for managing OAuth integrations and platform-specific tokens."""

    def __init__(self, client=None, corr_id=None) -> None:
        super().__init__(client, corr_id)
        self.integrations = SupabaseRepository[IntegrationRecord](
            client=self.client,
            table="integrations",
            model=IntegrationRecord,
        )
        self._id_resolution_cache: dict[str, str] = {}

    async def get_integration(
        self,
        workspace_id: str,
        provider: Provider,
        slack_user_id: str | None = None,
    ) -> IntegrationRecord | None:
        """Fetches an integration record with caching and identity bridging.

        Handles the resolution of HubSpot portal IDs to internal workspace IDs
        and manages decryption of sensitive credentials.

        Args:
            workspace_id: The UUID of the workspace (or numeric portal ID).
            provider: The integration provider (e.g., HubSpot, Slack).
            slack_user_id: Optional Slack user ID for user-scoped tokens.

        Returns:
            The decrypted integration record if found, otherwise None.

        """
        # 1. Identity Resolution (External ID -> Workspace ID)
        schema = CRM_SCHEMAS.get(provider)
        if schema and workspace_id.isdigit() and provider == Provider.HUBSPOT:
            workspace_id = await self._resolve_external_to_workspace(
                workspace_id, provider
            )

        # 2. Shared/Master Token Fetch
        cache_key = f"integ:{workspace_id}:{provider}:{slack_user_id or 'shared'}"

        async def fetch() -> IntegrationRecord | None:
            # Try direct lookup first (explicit column select — avoids full-row serialization)
            filters: dict[str, Any] = {
                "workspace_id": workspace_id,
                "provider": provider,
            }
            if slack_user_id:
                filters["id"] = f"user_{slack_user_id}"

            res = await self.integrations.fetch_single(filters)
            if res:
                return res

            # Fallback: Identity Bridge (Slack workspace → linked HubSpot integration)
            if provider == Provider.HUBSPOT and not workspace_id.startswith("hs_"):
                logger.debug(
                    "Attempting identity bridge fallback for Slack workspace %s",
                    workspace_id,
                )
                bridge_filters: dict[str, Any] = {
                    "provider": Provider.HUBSPOT,
                    "metadata->>linked_slack_workspace_id": workspace_id,
                }
                return await self.integrations.fetch_single(bridge_filters)

            return None

        record = await _record_cache.get_or_fetch(cache_key, fetch)
        return self._decrypt_integration(record)

    async def get_integration_by_external_id(
        self, external_id: str, provider: Provider
    ) -> IntegrationRecord | None:
        """Finds an integration record by its provider-specific external ID."""
        workspace_id = await self._resolve_external_to_workspace(external_id, provider)

        # If mapping found, use it
        if workspace_id != external_id:
            res = await self.get_integration(workspace_id, provider)
            # Self-healing: if we have a mapping but no record, purge the mapping
            if not res:
                cache_key = f"map:{provider}:{external_id}"
                await _hubspot_mapping_cache.invalidate(cache_key)
            return res

        # Fallback: direct metadata search on integrations table (needed for bootstrap/migration)
        schema = CRM_SCHEMAS.get(provider)
        if not schema:
            return None

        res = await self.integrations.fetch_single(
            {f"metadata->>{schema.metadata_id_key}": external_id}
        )
        return self._decrypt_integration(res)

    async def get_integration_by_portal_id(
        self, portal_id: str
    ) -> IntegrationRecord | None:
        """Legacy helper for HubSpot-specific lookups. (Deprecated in favor of get_integration_by_external_id)"""
        return await self.get_integration_by_external_id(portal_id, Provider.HUBSPOT)

    async def get_integration_by_slack_team_id(
        self, team_id: str
    ) -> IntegrationRecord | None:
        """Finds a Slack integration by its Slack Team ID (from metadata)."""
        return await self.get_integration_by_external_id(team_id, Provider.SLACK)

    async def list_integrations_by_slack_team_id(
        self, team_id: str
    ) -> list[IntegrationRecord]:
        """Finds all integrations linked to a Slack Team ID."""
        schema = CRM_SCHEMAS[Provider.SLACK]
        res = await self.integrations.fetch_many(
            {f"metadata->>{schema.metadata_id_key}": team_id}
        )
        return [self._decrypt_integration(r) for r in res] if res else []  # type: ignore

    async def _resolve_external_to_workspace(
        self, external_id: str, provider: Provider
    ) -> str:
        """Resolves a provider-specific external ID to its internal workspace_id."""
        schema = CRM_SCHEMAS.get(provider)
        if not schema:
            return external_id

        cache_key = f"map:{provider}:{external_id}"
        cached = await _hubspot_mapping_cache.get(
            cache_key
        )  # Re-using hubspot cache for now
        if cached:
            return cached

        # DB Lookup using the schema-defined column
        res = await self.client.fetch_single(
            "workspaces", {schema.external_id_key: external_id}, select=["id"]
        )
        if res:
            wid = res["id"]
            await _hubspot_mapping_cache.set(cache_key, wid)
            return wid
        return external_id

    async def check_integration_exists(
        self, workspace_id: str, provider: Provider
    ) -> bool:
        """Checks if an integration exists without downloading the payload."""
        row = await self.integrations.fetch_single(
            {"workspace_id": workspace_id, "provider": provider},
            select=["id"],
        )
        return row is not None

    async def get_integration_count_by_slack_team_id(self, team_id: str) -> int:
        """Returns the number of integrations linked to a Slack Team ID."""
        schema = CRM_SCHEMAS[Provider.SLACK]
        return await self.integrations.count(
            {f"metadata->>{schema.metadata_id_key}": team_id}
        )

    def _decrypt_integration(
        self, record: IntegrationRecord | None
    ) -> IntegrationRecord | None:
        if not record or not self._aesgcm:
            return record

        creds = dict(record.credentials)
        if "access_token" in creds:
            creds["access_token"] = self._decrypt_token(creds["access_token"])
        if "refresh_token" in creds:
            creds["refresh_token"] = self._decrypt_token(creds["refresh_token"])

        return record.model_copy(update={"credentials": creds})

    async def upsert_integration(self, payload: dict[str, Any]) -> IntegrationRecord:
        """Creates or updates an integration record with automatic credential encryption.

        Args:
            payload: The raw integration data, including credentials.

        Returns:
            The saved and decrypted integration record.

        """
        if "credentials" in payload:
            creds = payload["credentials"].copy()
            if "access_token" in creds:
                creds["access_token"] = self._encrypt_token(creds["access_token"])
            if "refresh_token" in creds:
                creds["refresh_token"] = self._encrypt_token(creds["refresh_token"])
            payload["credentials"] = creds

        res = await self.integrations.upsert(payload)

        # Determine the user suffix for the cache key
        user_suffix = "shared"
        if res.id and res.id.startswith("user_"):
            user_suffix = res.id.replace("user_", "")

        await _record_cache.invalidate(
            f"integ:{res.workspace_id}:{res.provider}:{user_suffix}"
        )

        # Invalidate mapping cache if metadata changed
        schema = CRM_SCHEMAS.get(res.provider)
        if schema and res.metadata:
            external_id = res.metadata.get(schema.metadata_id_key)
            if external_id:
                await _hubspot_mapping_cache.invalidate(
                    f"map:{res.provider}:{external_id}"
                )

        return self._decrypt_integration(res)  # type: ignore

    async def delete_integration(
        self,
        workspace_id: str,
        provider: Provider,
        slack_user_id: str | None = None,
    ) -> None:
        """Deletes an integration and systematically invalidates all associated caches.

        Args:
            workspace_id: The UUID of the workspace.
            provider: The integration provider to delete.
            slack_user_id: Optional Slack user ID for user-scoped tokens.

        """
        # 1. Fetch before deletion to get metadata for mapping cache invalidation
        integration = await self.get_integration(workspace_id, provider, slack_user_id)

        # 2. Delete from DB
        filters = {"workspace_id": workspace_id, "provider": provider.value}
        if slack_user_id:
            filters["id"] = f"user_{slack_user_id}"
        await self.integrations.delete(filters)

        # 3. Invalidate caches
        user_suffix = slack_user_id or "shared"
        await _record_cache.invalidate(f"integ:{workspace_id}:{provider}:{user_suffix}")

        if integration and integration.metadata:
            schema = CRM_SCHEMAS.get(provider)
            if schema and schema.metadata_id_key in integration.metadata:
                ext_id = integration.metadata[schema.metadata_id_key]
                if ext_id:
                    await _hubspot_mapping_cache.invalidate(f"map:{provider}:{ext_id}")

    async def list_integrations_for_workspace(
        self, workspace_id: str
    ) -> list[IntegrationRecord]:
        """Lists all integration records associated with a workspace (CR-15)."""
        res = await self.integrations.fetch_many({"workspace_id": workspace_id})
        return [self._decrypt_integration(r) for r in res] if res else []  # type: ignore
