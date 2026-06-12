# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

import json
import time
from functools import cached_property
from typing import TYPE_CHECKING, Any

from app.domains.billing.tier_service import (  # noqa: F401 (re-exported)
    Feature,
    TierService,
)
from app.domains.crm.base import BaseCRMService
from app.domains.crm.slack_client_service import SlackClientService
from app.domains.crm.uninstall_service import UninstallService
from app.utils.cache import AsyncTTL

if TYPE_CHECKING:
    from app.domains.crm.hubspot.service import HubSpotService
    from app.providers.slack.client import SlackClient

from app.connectors.common.registry import ChannelRegistry
from app.core.exceptions import IntegrationNotFoundError
from app.core.logging import get_logger
from app.db.records import IntegrationRecord, PlanTier, Provider
from app.db.storage_service import StorageService

logger = get_logger("integration.service")


class IntegrationService:
    """Domain service for managing workspace-provider integrations and OAuth
    lifecycles.

    Integration record caching is handled by StorageService's AsyncTTL caches.
    This service maintains only a tier cache for derived PlanTier lookups.
    """

    _team_id_cache = AsyncTTL(max_size=1000, ttl=600)  # 10 min mapping cache

    def __init__(
        self,
        corr_id: str | None = None,
        *,
        storage: StorageService | None = None,
    ) -> None:
        """Initializes the IntegrationService."""
        self.corr_id = corr_id or "system"
        self.storage = storage or StorageService(self.corr_id)
        self.hubspot_channel = ChannelRegistry.get_channel(
            Provider.HUBSPOT, corr_id=corr_id
        )

    @cached_property
    def tier(self) -> TierService:
        """Lazy-loaded TierService for all billing / plan-gate concerns.

        Extracted from IntegrationService (H-01 Phase 1) to reduce the
        God Class surface area.  All callers that go through
        ``IntegrationService.*`` continue to work unchanged via the
        delegation wrappers below.
        """
        return TierService(storage=self.storage)

    @cached_property
    def uninstall(self) -> UninstallService:
        """Lazy-loaded UninstallService for workspace teardown concerns.

        Extracted from IntegrationService (H-01 Phase 2).  All callers
        that go through ``IntegrationService.uninstall_*`` continue to
        work without modification via the delegation wrappers below.
        """
        return UninstallService(
            storage=self.storage,
            corr_id=self.corr_id,
            tier_service=self.tier,
        )

    @cached_property
    def slack_client_service(self) -> SlackClientService:
        """Lazy-loaded SlackClientService for Slack token/client resolution.

        Extracted from IntegrationService (H-01 Phase 3).  All callers
        that go through ``IntegrationService.get_slack_client()`` etc.
        continue to work without modification via the delegation wrappers below.
        """
        return SlackClientService(storage=self.storage, corr_id=self.corr_id)

    @cached_property
    def hubspot_service(self) -> HubSpotService:
        """Lazy-loaded to avoid circular imports and unnecessary initialization."""
        from app.domains.crm.hubspot.service import HubSpotService

        return HubSpotService(self.corr_id, storage=self.storage)

    def get_active_crm_service(
        self, workspace_id: str, corr_id: str, slack_ts: str | None = None
    ) -> BaseCRMService:
        """Dynamically resolve and instantiate the active CRM service for a workspace.

        This decouples Slack interaction handlers from specifically knowing about
        HubSpot. In the future, this will branch based on the workspace's active CRM
        provider record, returning the correct ``BaseCRMService`` implementation.
        """
        from app.domains.crm.hubspot.service import HubSpotService

        return HubSpotService(corr_id=corr_id, storage=self.storage, slack_ts=slack_ts)

    async def _resolve_workspace_from_oauth(
        self,
        portal_id: str | None,
        state: str | None,
        primary_email: str | None = None,
    ) -> tuple[str, str | None]:
        """Helper to resolve or create a workspace during OAuth callback.

        Handles HubSpot-first, Slack-first, and re-installation scenarios
        while enforcing isolation per HubSpot Portal ID.
        """
        if not portal_id:
            logger.error("OAuth update missing platform_id/portal_id")
            raise ValueError("OAuth update missing platform_id/portal_id")

        # 1. Migration/Persistence Check: Is this portal already integrated?
        existing_anywhere = await self.storage.get_integration_by_portal_id(portal_id)
        if existing_anywhere:
            # Re-use existing workspace to maintain continuity for current users
            workspace_id = existing_anywhere.workspace_id
            metadata = existing_anywhere.metadata or {}
            linked_slack_id = metadata.get("linked_slack_workspace_id")

            # IDENTITY PIVOT HEALING: If we are in a Slack-first install flow (state exists)
            # and it points to a Slack workspace, we must update the link!
            if state:
                slack_integration = await self.get_integration_by_slack_team_id(state)
                if (
                    slack_integration
                    and slack_integration.workspace_id != linked_slack_id
                ):
                    linked_slack_id = slack_integration.workspace_id
                    logger.info(
                        "Healing identity pivot: updating linked_slack_id to %s",
                        linked_slack_id,
                    )
                    # Create/update the Slack shell record pointing to the new parent
                    await self._upsert_slack_shell_integration(
                        workspace_id,
                        linked_slack_id,
                        slack_integration.metadata.get("slack_team_id"),
                    )

            logger.debug(
                "Portal %s already linked to workspace=%s (Parent=%s); re-using",
                portal_id,
                workspace_id,
                linked_slack_id,
            )
            return workspace_id, linked_slack_id

        # 2. Forced Isolation for New Portals: Use the Portal ID as the primary key
        # Prefixing with 'hs_' to ensure it doesn't conflict with legacy Slack IDs
        workspace_id = f"hs_{portal_id}"
        logger.info("Isolating new HubSpot portal to workspace_id=%s", workspace_id)

        # 3. Resolve parent context (Slack integration) from state if it exists
        slack_integration = None
        if state:
            # 2026.03: Robust Resolution - Try direct workspace lookup first
            slack_integration = await self.get_integration(state, Provider.SLACK)

            # Fallback to Slack Team ID lookup if state is a raw team identifier
            if not slack_integration:
                slack_integration = await self.get_integration_by_slack_team_id(state)
                if slack_integration:
                    logger.info("Resolved Slack context via Team ID lookup: %s", state)
            else:
                logger.info("Resolved Slack context via direct Workspace ID: %s", state)

        # 4. Initialize the isolated workspace
        slack_team_id = (
            slack_integration.metadata.get("slack_team_id")
            if slack_integration
            else None
        )
        await self.storage.start_trial_workspace(
            workspace_id=workspace_id,
            primary_email=primary_email,
            portal_id=portal_id,
            slack_team_id=slack_team_id,
        )

        # 5. Link Sharing: If a Slack integration was found in the context, LINK it
        linked_slack_id = None
        if slack_integration:
            linked_slack_id = slack_integration.workspace_id
            if linked_slack_id != workspace_id:
                logger.info(
                    "Linking isolated workspace %s to Slack team identity in workspace %s",
                    workspace_id,
                    linked_slack_id,
                )
                # Create a shell integration for Slack that pointed to the parent identity
                await self._upsert_slack_shell_integration(
                    workspace_id,
                    linked_slack_id,
                    slack_integration.metadata.get("slack_team_id"),
                )

        return workspace_id, linked_slack_id

    async def _upsert_slack_shell_integration(
        self, workspace_id: str, linked_slack_id: str, slack_team_id: str | None
    ) -> None:
        await self.storage.upsert_integration(
            {
                "workspace_id": workspace_id,
                "provider": Provider.SLACK,
                "credentials": {},
                "metadata": {
                    "linked_slack_workspace_id": linked_slack_id,
                    "slack_team_id": slack_team_id,
                },
            }
        )

    async def get_integration(
        self,
        workspace_id: str,
        provider: Provider,
        slack_user_id: str | None = None,
    ) -> IntegrationRecord | None:
        """Fetches an integration record.

        Delegates to StorageService which maintains its own AsyncTTL cache
        with proper invalidation on upserts, deletes, and token updates.

        Args:
            workspace_id: The workspace to fetch for.
            provider: The specific provider (Slack/HubSpot).
            slack_user_id: Optional Slack user ID for individual token lookups.

        Returns:
            The decrypted integration record if found, otherwise None.

        """
        return await self.storage.get_integration(workspace_id, provider, slack_user_id)

    async def get_all_integrations(self, workspace_id: str) -> list[IntegrationRecord]:
        """Fetches all integrations associated with a specific workspace (CR-15)."""
        return await self.storage.list_integrations_for_workspace(workspace_id)

    async def get_integration_by_slack_team_id(
        self,
        team_id: str,
    ) -> IntegrationRecord | None:
        """Retrieves an integration record by Slack team ID.

        Delegates to StorageService and wraps with a 10min local cache to
        prevent redundant DB calls during Slack event bursts.

        Args:
            team_id: The team ID from Slack.

        Returns:
            The integration record if found, else None.
        """  # noqa: D413
        cached = await self._team_id_cache.get(team_id)
        if cached:
            if cached == "MISSING":
                return None
            return cached

        rec = await self.storage.get_integration_by_slack_team_id(team_id)
        if rec and rec.provider == Provider.SLACK:
            await self._team_id_cache.set(team_id, rec)
            return rec

        # Cache negative hits to prevent repeated DB calls on invalid/uninstalled teams
        await self._team_id_cache.set(team_id, "MISSING")
        return None

    async def is_hubspot_connected_anywhere(self, workspace_id: str) -> bool:
        """Determines if HubSpot is connected for this identity cluster."""
        hubspot = await self.resolve_hubspot_integration(workspace_id)
        return hubspot is not None and bool(hubspot.credentials.get("access_token"))

    async def resolve_hubspot_integration(
        self, workspace_id: str, _depth: int = 0
    ) -> IntegrationRecord | None:
        """Resolves the active HubSpot integration for an identity cluster.

        Supports sibling 'jumps' between Slack identities and HubSpot portals,
        enforcing the 2026.03 Identity Bridge pattern.

        Args:
            workspace_id: The workspace to resolve from.
            _depth: Internal recursion depth guard (max 5 hops). Do not pass externally.

        """
        if _depth > 5:
            logger.error(
                "Identity resolution depth exceeded (>5 hops) for workspace %s — "
                "possible circular identity chain; stopping traversal.",
                workspace_id,
            )
            return None

        # 1. Direct hit (Same workspace)
        local = await self.get_integration(workspace_id, Provider.HUBSPOT)
        if local and local.credentials.get("access_token"):
            return local

        # 2. Check for linked parent/identity
        slack_local = await self.get_integration(workspace_id, Provider.SLACK)
        if not slack_local:
            return None

        metadata = slack_local.metadata or {}
        linked_id = metadata.get("linked_slack_workspace_id")
        if linked_id and linked_id != workspace_id:
            logger.debug("Pivoting to linked identity parent: %s", linked_id)
            return await self.resolve_hubspot_integration(linked_id, _depth + 1)

        # 3. God View: Search for siblings that share the same Slack Team ID
        team_id = metadata.get("slack_team_id")
        if team_id:
            # Find all integrations in the cluster
            cluster = await self.storage.list_integrations_by_slack_team_id(team_id)
            for rec in cluster:
                if rec.workspace_id != workspace_id:
                    # Look for HubSpot in this sibling workspace
                    hubspot = await self.get_integration(
                        rec.workspace_id, Provider.HUBSPOT
                    )
                    if hubspot and hubspot.credentials.get("access_token"):
                        logger.debug(
                            "Resolved sibling HubSpot integration via Team ID %s (Workspace=%s)",
                            team_id,
                            rec.workspace_id,
                        )
                        return hubspot

        return None

    async def resolve_default_channel(self, workspace_id: str) -> str:
        """Determine the default Slack channel for this workspace.

        Priority:
        1. integration.default_channel (if stored)
        2. integration.slack_team_id (fallback)
        """
        integration = await self.get_integration(workspace_id, provider=Provider.SLACK)
        if not integration:
            raise IntegrationNotFoundError(
                f"No Slack integration found for workspace {workspace_id}"
            )

        # 1. Explicit default channel
        default_channel = getattr(integration, "channel_id", None)
        if default_channel:
            return default_channel

        # 2. Check metadata
        metadata = getattr(integration, "metadata", {}) or {}
        if "channel_id" in metadata:
            return metadata["channel_id"]

        # 3. Fallback: No channel found
        raise IntegrationNotFoundError(
            f"No default Slack channel configured for workspace {workspace_id}"
        )

    # ------------------------------------------------------------------
    # Tier & feature-gate — delegated to TierService (H-01 Phase 1)
    # These wrappers preserve the existing public API so all call sites
    # work without modification.
    # ------------------------------------------------------------------

    async def get_tier(self, workspace_id: str) -> PlanTier:
        """Return the effective plan tier for a workspace.

        Delegates to ``TierService.get_tier()``.
        """
        return await self.tier.get_tier(workspace_id)

    async def is_pro_workspace(self, workspace_id: str) -> bool:
        """Return True if the workspace is on the PRO tier.

        Delegates to ``TierService.is_pro_workspace()``.
        """
        return await self.tier.is_pro_workspace(workspace_id)

    async def is_at_least_tier(
        self, workspace_id: str, required_tier: PlanTier
    ) -> bool:
        """Return True if the workspace meets or exceeds ``required_tier``.

        Delegates to ``TierService.is_at_least_tier()``.
        """
        return await self.tier.is_at_least_tier(workspace_id, required_tier)

    async def check_feature_access(
        self, workspace_id: str, feature_id: str | Feature
    ) -> bool:
        """Return True if the workspace has access to ``feature_id``.

        Delegates to ``TierService.check_feature_access()``.
        """
        return await self.tier.check_feature_access(workspace_id, feature_id)

    # OAuth lifecycle
    async def handle_hubspot_oauth_callback(
        self, code: str, state: str | None, slack_user_id: str | None = None
    ) -> str:
        """Processes the HubSpot OAuth callback for both Slack-first and
        HubSpot-first flows.

        Args:
            code: Authorization code from HubSpot.
            state: Context identifier (Slack team ID or Workspace ID).

        Returns:
            The resolved or created workspace ID.

        """
        logger.info("Exchanging HubSpot OAuth code")
        token = await self.hubspot_channel.exchange_token(code)

        # 0. Extract slack_user_id if state contains it (encoded JSON)
        if not slack_user_id and state and state.startswith("{"):
            try:
                ctx = json.loads(state)
                slack_user_id = ctx.get("slack_user_id")
            except Exception:
                pass

        # 2. Enrich token info (domain, email)
        primary_email = None
        try:
            from app.providers.hubspot.client import HubSpotClient

            temp_client = HubSpotClient(
                corr_id=self.corr_id,
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                hub_domain=getattr(token, "hub_domain", None),
            )
            token_info = await temp_client.get_token_info()
            if token_info:
                primary_email = token_info.get("user")
                logger.info(
                    "Captured primary_email=%s from HubSpot token info",
                    primary_email,
                )
        except Exception as e:
            logger.warning("Could not fetch HubSpot token info: %s", e)

        # 3. Resolve Workspace
        workspace_id, linked_slack_id = await self._resolve_workspace_from_oauth(
            portal_id=token.portal_id,
            state=state,
            primary_email=primary_email,
        )

        # Calculate absolute expiration time (Unix timestamp)
        expires_at = int(time.time()) + getattr(token, "expires_in", 1800)

        # 4. Save HubSpot integration
        hs_payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "provider": Provider.HUBSPOT,
            "credentials": {
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "expires_at": expires_at,
            },
            "metadata": {
                "portal_id": token.portal_id,
                "hub_domain": getattr(token, "hub_domain", None),
                "is_user_token": bool(slack_user_id),
                "linked_slack_workspace_id": linked_slack_id,
            },
        }

        # Determine the integration ID (Primary key)
        if slack_user_id:
            # Individual user token
            hs_payload["id"] = f"user_{slack_user_id}"
            logger.info(
                "Saving individual HubSpot token for Slack user=%s", slack_user_id
            )
        else:
            # Default workspace-level token
            # On re-install, include existing ID to merge instead of duplicating
            existing_hs = await self.storage.get_integration(
                workspace_id, Provider.HUBSPOT
            )
            if existing_hs:
                hs_payload["id"] = existing_hs.id

        await self.storage.upsert_integration(hs_payload)
        logger.info("HubSpot integration saved for workspace_id=%s", workspace_id)

        # 5. Success-First Onboarding: Send a single definitive welcome message to Slack
        if linked_slack_id:
            try:
                from app.domains.messaging.slack.service import SlackMessagingService

                # 1. Fetch sibling slack record to get installer identity
                slack_rec = await self.get_integration(linked_slack_id, Provider.SLACK)

                if slack_rec:
                    # 2. Initialize service with the concrete record
                    slack_svc = SlackMessagingService(
                        corr_id=self.corr_id,
                        integration_service=self,
                        slack_integration=slack_rec,
                    )
                    # Target the specific user who installed the app (Installer DM)
                    metadata = slack_rec.metadata or {}
                    installer_id = metadata.get("authed_user_id")

                    if not installer_id:
                        # No installer DM target available — skip the welcome message
                        # rather than calling resolve_default_channel which always raises
                        # IntegrationNotFoundError for brand-new workspaces.
                        logger.debug(
                            "Skipping success DM: no authed_user_id in Slack record for %s",
                            linked_slack_id,
                        )
                    else:
                        await slack_svc.send_welcome_message(
                            workspace_id=linked_slack_id,
                            channel=installer_id,
                            is_update=True,  # Sends the success blocks
                        )
                    logger.debug(
                        "Sent final success welcome message to Slack installer=%s",
                        installer_id,
                    )

            except Exception as e:
                logger.warning("Failed to deliver success welcome message: %s", e)

        return workspace_id

    async def register_integration(
        self,
        provider: Provider,
        platform_id: str,
        credentials: dict[str, Any],
        metadata: dict[str, Any],
        state: str | None = None,
    ) -> str:
        """Generic method to register or update an integration for a workspace.

        Args:
            provider: The provider being registered (e.g. SLACK, HUBSPOT, WHATSAPP).
            platform_id: The unique ID from the platform (e.g., Slack team_id).
            credentials: The OAuth or API credentials dictionary.
            metadata: Provider-specific metadata.
            state: Optional workspace_id to bind the integration to
                an existing workspace.

        """
        existing = None
        if provider == Provider.SLACK:
            existing = await self.storage.get_integration_by_slack_team_id(platform_id)
        # Note: Additional provider ID lookups can be added here as needed.

        if state:
            workspace = await self.storage.get_workspace(state)
            if workspace:
                workspace_id = workspace.id
                logger.info("Resolved workspace via state=%s", workspace_id)

                # --- Conflict Resolution (Merging) ---
                if (
                    provider == Provider.SLACK
                    and existing
                    and existing.workspace_id != workspace_id
                ):
                    logger.info(
                        "Merging new state workspace %s into existing %s",
                        workspace_id,
                        existing.workspace_id,
                    )
                    state_integrations = (
                        await self.storage.list_integrations_for_workspace(workspace_id)
                    )
                    for integ in state_integrations:
                        payload_dict = integ.model_dump(exclude_none=True)
                        payload_dict["workspace_id"] = existing.workspace_id

                        # If the target workspace already has an integration for this provider,
                        # we must block the connection to prevent accidental overwrites.
                        target_existing = await self.storage.get_integration(
                            existing.workspace_id, integ.provider
                        )
                        if target_existing:
                            # Clean up the state workspace before crashing to prevent zombie records
                            try:
                                await self.storage.integration_svc.integrations.delete(
                                    {"id": integ.id}
                                )
                                await self.storage.workspace_svc.workspaces.delete(
                                    {"id": workspace_id}
                                )
                            except Exception as e:
                                logger.warning(
                                    "Failed to clean up state workspace during conflict abort: %s",
                                    e,
                                )

                            raise ValueError(
                                "Portal Conflict: Your Slack workspace is already connected to HubSpot. "
                                "Please visit the REHA Connect App Home in Slack and click 'Uninstall REHA Connect' to remove your current portal before connecting a new one."
                            )

                        await self.storage.upsert_integration(payload_dict)

                    # Delete the empty state workspace to clean up
                    try:
                        await self.storage.workspace_svc.workspaces.delete(
                            {"id": workspace_id}
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to delete merged state workspace %s: %s",
                            workspace_id,
                            e,
                        )

                    # Continue using the established workspace
                    workspace_id = existing.workspace_id
            else:
                workspace = await self.storage.upsert_workspace(workspace_id=state)
                workspace_id = workspace.id
                logger.warning("Invalid state=%s; created new workspace", state)
        elif existing:
            workspace_id = existing.workspace_id
            logger.info("Reusing existing workspace=%s", workspace_id)
        else:
            # 180-Day Rule: Check if a workspace already exists for this external ID (retention policy)
            resolved_ws_id = (
                await self.storage.integration_svc._resolve_external_to_workspace(
                    platform_id, provider
                )
            )

            workspace = await self.storage.get_workspace(resolved_ws_id)
            if workspace:
                workspace_id = workspace.id
                logger.info(
                    "Found retained workspace=%s. Preserving trial state.", workspace_id
                )
            else:
                workspace = await self.storage.start_trial_workspace(
                    workspace_id=resolved_ws_id,
                    slack_team_id=platform_id if provider == Provider.SLACK else None,
                )
                workspace_id = workspace.id
                logger.info("Created new trial workspace=%s", workspace_id)

        # 2026.03 Hardening: Always ensure the workspace record has the current slack_team_id
        if provider == Provider.SLACK:
            await self.storage.upsert_workspace(
                id=workspace_id, slack_team_id=platform_id
            )

        # Merge metadata on re-install to preserve settings
        if existing and existing.metadata:
            metadata = {**dict(existing.metadata), **metadata}

        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "provider": provider,
            "credentials": credentials,
            "metadata": metadata,
        }

        # Safe diagnostic trace
        if provider == Provider.SLACK:
            token_val = credentials.get("access_token")
            token_prefix = str(token_val)[:4] if token_val else "NONE"
            logger.debug(
                "Final Persistence Payload: workspace_id=%s, token_prefix=%s, cred_keys=%s",
                workspace_id,
                token_prefix,
                list(credentials.keys()),
            )

        if existing:
            payload["id"] = existing.id
            if provider == Provider.SLACK:
                await self.slack_client_service.invalidate_client_cache(existing.id)

        await self.storage.upsert_integration(payload)
        logger.debug(
            "%s integration saved workspace_id=%s", provider.value, workspace_id
        )
        return workspace_id

    # ------------------------------------------------------------------
    # Uninstall lifecycle — delegated to UninstallService (H-01 Phase 2)
    # ------------------------------------------------------------------

    async def uninstall_workspace(
        self,
        workspace_id: str,
        trigger_hubspot_uninstall: bool = True,
        trigger_slack_uninstall: bool = True,
    ) -> None:
        """Fully uninstall all integrations for a workspace.

        Delegates to ``UninstallService.uninstall_workspace()``.
        """
        await self.uninstall.uninstall_workspace(
            workspace_id,
            trigger_hubspot_uninstall=trigger_hubspot_uninstall,
            trigger_slack_uninstall=trigger_slack_uninstall,
        )

    async def invalidate_tier_cache(self, workspace_id: str) -> None:
        """Clear the cached plan tier for a workspace.

        Delegates to ``TierService.invalidate_tier_cache()``.
        """
        await self.tier.invalidate_tier_cache(workspace_id)

    async def uninstall_hubspot(self, workspace_id: str) -> None:
        """Uninstall only the HubSpot integration.

        Delegates to ``UninstallService.uninstall_hubspot()``.
        """
        await self.uninstall.uninstall_hubspot(workspace_id)

    async def uninstall_slack(self, workspace_id: str) -> None:
        """Uninstall only the Slack integration.

        Delegates to ``UninstallService.uninstall_slack()``.
        """
        await self.uninstall.uninstall_slack(workspace_id)

    # ------------------------------------------------------------------
    # Slack client resolution — delegated to SlackClientService (H-01 Phase 3)
    # ------------------------------------------------------------------

    async def get_messaging_client(self, integration: IntegrationRecord) -> Any:
        """Resolve the platform-specific channel client for ``integration``.

        Delegates to ``SlackClientService.get_messaging_client()``.
        """
        return await self.slack_client_service.get_messaging_client(integration)

    async def get_slack_client(self, integration: IntegrationRecord) -> SlackClient:
        """Resolve an authenticated Slack client for ``integration``.

        Delegates to ``SlackClientService.get_slack_client()``.
        """
        return await self.slack_client_service.get_slack_client(integration)

    async def update_slack_tokens(
        self,
        workspace_id: str,
        access_token: str,
        refresh_token: str | None,
        expires_at: int | None,
    ) -> None:
        """Persist rotated Slack tokens to the correct identity owner.

        Delegates to ``SlackClientService.update_slack_tokens()``.
        """
        await self.slack_client_service.update_slack_tokens(
            workspace_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )
