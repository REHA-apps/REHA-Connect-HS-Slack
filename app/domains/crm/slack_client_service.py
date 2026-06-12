"""Slack client resolution service.

Extracted from ``IntegrationService`` (H-01 Phase 3) to satisfy the
Single Responsibility Principle.  All Slack token/client resolution
logic — identity pivoting, handshake caching, and token persistence —
lives here.

``IntegrationService`` retains thin delegation wrappers for backwards
compatibility with all existing call sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from app.core.exceptions import IntegrationNotFoundError
from app.core.logging import get_logger
from app.db.records import IntegrationRecord, Provider
from app.utils.cache import AsyncTTL

if TYPE_CHECKING:
    from app.db.storage_service import StorageService
    from app.providers.slack.client import SlackClient

logger = get_logger("crm.slack_client_service")


class SlackClientService:
    """Resolves authenticated Slack clients for any integration record.

    Handles the cross-workspace identity-bridge pattern:
    - HubSpot records that reference a linked Slack workspace.
    - Shell Slack records that point to a parent identity with real tokens.
    - Same-workspace co-located HubSpot + Slack installs.

    Results are cached for 1 hour per integration ID to prevent redundant
    API handshakes during high-volume Slack event bursts.

    Dependencies are injected to avoid circular imports and allow testing.
    """

    # Shared class-level TTL cache — 1 hour handshake cache per integration.
    # ClassVar so it is not re-created per instance.
    _slack_client_cache: ClassVar[AsyncTTL] = AsyncTTL(max_size=1000, ttl=3600)

    def __init__(self, storage: StorageService, corr_id: str) -> None:
        """Initialise with required dependencies.

        Args:
            storage: Active ``StorageService`` instance.
            corr_id: Correlation ID for request tracing.

        """
        self.storage = storage
        self.corr_id = corr_id

    async def get_messaging_client(self, integration: IntegrationRecord) -> Any:
        """Resolve the platform-specific channel client for ``integration``.

        Dispatches to the correct provider implementation.  Use this method
        in new platform connectors (WhatsApp, Teams) instead of calling
        platform-specific helpers directly.

        Args:
            integration: The integration record to resolve a client for.

        Raises:
            NotImplementedError: If the provider is not yet supported.

        """
        provider = Provider(integration.provider)
        match provider:
            case Provider.SLACK:
                return await self.get_slack_client(integration)
            case _:
                raise NotImplementedError(
                    f"get_messaging_client not implemented for provider: {provider}"
                )

    async def get_slack_client(self, integration: IntegrationRecord) -> SlackClient:
        """Resolve an authenticated ``SlackClient`` for ``integration``.

        Supports the Identity Bridge pattern:

        1. Direct Slack record with credentials → use as-is.
        2. HubSpot record or shell Slack record with ``linked_slack_workspace_id``
           → pivot to the linked parent and borrow its credentials.
        3. HubSpot record without a link → fall back to a co-located Slack
           record in the same workspace.

        Results are cached for 1 hour to silence redundant handshakes.

        Args:
            integration: The source integration record (Slack or HubSpot).

        Raises:
            IntegrationNotFoundError: If no valid Slack identity can be resolved.

        """
        # 1. Fast-Path Source Cache Lookup: Bypass any DB pivots if we already resolved this integration
        source_cache_key = f"slack_source_{integration.id}"
        cached_client = await self._slack_client_cache.get(source_cache_key)
        if cached_client:
            return cached_client

        metadata = integration.metadata or {}
        linked_id = metadata.get("linked_slack_workspace_id")
        target_integration = integration

        is_hubspot = integration.provider == Provider.HUBSPOT
        is_empty_slack = (
            integration.provider == Provider.SLACK
            and not integration.credentials.get("access_token")
        )

        # Identity Pivot: borrow credentials from linked parent identity
        if (is_hubspot or is_empty_slack) and linked_id:
            logger.debug(
                "Identity Pivot: workspace=%s (EmptySlack=%s) bridging to parent Identity=%s",
                integration.workspace_id,
                is_empty_slack,
                linked_id,
            )
            identity_integration = await self.storage.get_integration(
                linked_id, Provider.SLACK
            )
            if identity_integration and identity_integration.credentials.get(
                "access_token"
            ):
                target_integration = identity_integration
                logger.debug(
                    "Identity Pivot Success: Resolved parent credentials from %s",
                    linked_id,
                )
            else:
                logger.warning(
                    "Identity Pivot failed: Linked parent %s missing or empty creds",
                    linked_id,
                )
        elif is_hubspot and not linked_id:
            # HubSpot-first install or co-located Slack
            same_ws_slack = await self.storage.get_integration(
                integration.workspace_id, Provider.SLACK
            )
            if same_ws_slack and same_ws_slack.credentials.get("access_token"):
                logger.debug(
                    "Identity Pivot (same-workspace): HubSpot workspace=%s resolved "
                    "Slack credentials from co-located record",
                    integration.workspace_id,
                )
                target_integration = same_ws_slack
            else:
                logger.warning(
                    "Identity Pivot (same-workspace) failed: No Slack record found in "
                    "workspace=%s. Linkage missing in metadata.",
                    integration.workspace_id,
                )

        # Safety Guard: never use HubSpot credentials as Slack tokens
        if target_integration.provider != Provider.SLACK:
            logger.error(
                "CRITICAL IDENTITY MISMATCH: get_slack_client resolved a non-Slack "
                "provider (%s) for workspace=%s. Check identity bridging.",
                target_integration.provider,
                integration.workspace_id,
            )
            raise IntegrationNotFoundError(
                f"No valid Slack identity found for {integration.workspace_id}"
            )

        # Handshake Cache: prevent redundant token lookups in burst traffic
        cache_key = f"slack_{target_integration.id}"
        cached_client = await self._slack_client_cache.get(cache_key)
        if cached_client:
            # Backport to source cache key for future direct hits
            source_cache_key = f"slack_source_{integration.id}"
            await self._slack_client_cache.set(source_cache_key, cached_client)
            return cached_client

        from app.connectors.slack.token_service import (
            get_slack_client as resolve_client,
        )

        client = await resolve_client(
            integration=target_integration,
            corr_id=self.corr_id,
            storage=self.storage,
        )

        # Final Handshake Validation: fail early on bad token format
        token = client.bot_token
        if not token or not token.startswith("xox"):
            logger.critical(
                "IDENTITY BRIDGE FAILURE: Resolved invalid token format for "
                "workspace=%s (Provider=%s). Metadata keys: %s",
                integration.workspace_id,
                target_integration.provider,
                list(metadata.keys()),
            )
        else:
            logger.debug(
                "Identity Handshake Successful: ws=%s, prefix=%s, len=%s",
                integration.workspace_id,
                token[:4],
                len(token),
            )

        await self._slack_client_cache.set(cache_key, client)
        source_cache_key = f"slack_source_{integration.id}"
        await self._slack_client_cache.set(source_cache_key, client)
        return client

    async def update_slack_tokens(
        self,
        workspace_id: str,
        access_token: str,
        refresh_token: str | None,
        expires_at: int | None,
    ) -> None:
        """Persist rotated Slack tokens, routing to the parent identity owner.

        When a workspace is a child identity (has ``linked_slack_workspace_id``
        in metadata), the token is saved to the parent to maintain a single
        source of truth for credentials.

        Args:
            workspace_id: The workspace whose tokens have been rotated.
            access_token: The new bot access token.
            refresh_token: The new refresh token, if present.
            expires_at: Unix timestamp when ``access_token`` expires.

        """
        target_workspace_id = workspace_id

        integration = await self.storage.get_integration(workspace_id, Provider.SLACK)
        if integration:
            linked_id = (integration.metadata or {}).get("linked_slack_workspace_id")
            if linked_id:
                logger.debug(
                    "Redirecting Slack token update to parent Identity workspace=%s",
                    linked_id,
                )
                target_workspace_id = linked_id

        from app.connectors.slack.token_service import update_slack_tokens as _update

        await _update(
            storage=self.storage,
            workspace_id=target_workspace_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )

    async def invalidate_client_cache(self, integration_id: str) -> None:
        """Clear the handshake cache for a specific integration ID."""
        await self._slack_client_cache.invalidate(f"slack_source_{integration_id}")
        await self._slack_client_cache.invalidate(f"slack_{integration_id}")
