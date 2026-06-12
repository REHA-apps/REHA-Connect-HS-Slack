"""Billing and plan tier domain service.

Extracted from ``IntegrationService`` (H-01 Phase 1) to satisfy the
Single Responsibility Principle.  All workspace tier lookups, feature
gates, and trial-period logic live here so ``IntegrationService`` is no
longer responsible for billing concerns.

``IntegrationService`` retains thin delegation wrappers for backwards
compatibility with all existing call sites.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger
from app.db.records import PlanTier
from app.utils.cache import AsyncTTL

logger = get_logger("billing.tier_service")

# Default in-memory TTL for tier lookups (5 minutes)
_TIER_CACHE_TTL = 300


class Feature(StrEnum):
    """Feature gate identifiers for PRO-tier access control.

    Use these enum values instead of raw strings when calling
    ``TierService.check_feature_access()`` or
    ``IntegrationService.check_feature_access()``.
    """

    PRICING_CALCULATOR = "pricing_calculator"
    MEETING_SCHEDULER = "meeting_scheduler"
    AI_INSIGHTS = "ai_insights"
    NOTE_LOGGING = "note_logging"
    TASK_LOGGING = "task_logging"
    TICKET_SYNC = "ticket_sync"
    WIN_LOSS_POST_MORTEM = "win_loss_post_mortem"
    DEAL_STAGE = "deal_stage"
    DEAL_CLOSEDATE = "deal_closedate"
    DEAL_TYPE = "deal_type"
    DEAL_NEXT_STEP = "deal_next_step"
    REASSIGN_OWNER = "reassign_owner"


# All currently registered features are PRO-gated.
# To introduce a FREE feature in the future, remove it from this set.
_PRO_FEATURES: frozenset[Feature] = frozenset(Feature)


class TierService:
    """Handles all workspace plan-tier and feature-gate concerns.

    Responsibilities:
    - Retrieve the effective ``PlanTier`` for a workspace (accounting for
      active subscriptions, explicit trial end dates, and the 7-day
      install-based trial grace period).
    - Gate access to PRO features via ``check_feature_access()``.
    - Invalidate cached tier data when subscriptions change.

    Dependencies:
    - ``StorageService`` is passed in to avoid circular imports and to
      allow easy mocking in tests.
    """

    # Shared class-level TTL cache — one entry per workspace_id.
    # ClassVar so it is not re-created per instance (survives Lambda warm invocations).
    _tier_cache = AsyncTTL(max_size=1000, ttl=_TIER_CACHE_TTL)

    def __init__(self, storage: Any) -> None:
        """Initialise with a StorageService instance.

        Args:
            storage: A ``StorageService`` (or compatible protocol) used for
                workspace lookups.  Typed as ``Any`` to avoid the heavy
                import chain at module load time.

        """
        self.storage = storage

    async def get_tier(self, workspace_id: str) -> PlanTier:  # noqa: PLR0911
        """Return the effective plan tier for a workspace.

        Resolution order:
        1. In-memory TTL cache (5 min)
        2. Active Stripe subscription (``active`` / ``trialing`` status)
        3. Manual PRO plan override (``workspace.plan == PlanTier.PRO``)
        4. Explicit ``trial_ends_at`` date (absolute source of truth when set)
        5. 7-day install-based trial grace period (fallback when no explicit date)
        6. ``FREE`` otherwise

        Args:
            workspace_id: The workspace to check.

        Returns:
            The resolved ``PlanTier``.

        """
        cached = await self._tier_cache.get(workspace_id)
        if cached:
            return cached

        workspace = await self.storage.get_workspace(workspace_id)
        if not workspace:
            await self._tier_cache.set(workspace_id, PlanTier.FREE)
            return PlanTier.FREE

        # 1. Active subscription check
        # 'trialing' is a valid Pro status from Stripe/external providers
        # 'pro' is our internal flag for legacy or manual overrides
        # We must exclude local install-based trials (PlanTier.TRIAL) so their expiration check isn't bypassed.
        is_active_stripe_sub = (
            workspace.subscription_status in ("active", "trialing")
            and workspace.plan != PlanTier.TRIAL
        )
        if is_active_stripe_sub or workspace.plan == PlanTier.PRO:
            logger.debug(
                "Workspace %s is Pro via status/plan: %s / %s",
                workspace_id,
                workspace.subscription_status,
                workspace.plan,
            )
            await self._tier_cache.set(workspace_id, PlanTier.PRO)
            return PlanTier.PRO

        # 2. Trial check
        target_now = datetime.now(UTC)

        # Check explicit trial end date first — absolute source of truth when set.
        # Overrides installation-based defaults and allows manual trial expiry.
        if workspace.trial_ends_at:
            ends_at = workspace.trial_ends_at
            if ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=UTC)

            if target_now <= ends_at:
                await self._tier_cache.set(workspace_id, PlanTier.PRO)
                return PlanTier.PRO
            else:
                await self._tier_cache.set(workspace_id, PlanTier.FREE)
                return PlanTier.FREE

        # Fallback to 7-day default from installation ONLY if trial_ends_at is missing
        install_date = workspace.install_date or workspace.created_at
        if install_date:
            if install_date.tzinfo is None:
                install_date = install_date.replace(tzinfo=UTC)

            if target_now <= install_date + timedelta(days=7):
                await self._tier_cache.set(workspace_id, PlanTier.PRO)
                return PlanTier.PRO

        await self._tier_cache.set(workspace_id, PlanTier.FREE)
        return PlanTier.FREE

    async def is_pro_workspace(self, workspace_id: str) -> bool:
        """Return True if the workspace is on the PRO tier.

        Args:
            workspace_id: The workspace to check.

        """
        tier = await self.get_tier(workspace_id)
        return tier == PlanTier.PRO

    async def is_at_least_tier(
        self, workspace_id: str, required_tier: PlanTier
    ) -> bool:
        """Return True if the workspace meets or exceeds ``required_tier``.

        Tier order: FREE < TRIAL < PRO.

        Args:
            workspace_id: The workspace to check.
            required_tier: The minimum tier required.

        """
        if required_tier == PlanTier.FREE:
            return True

        tier = await self.get_tier(workspace_id)

        if required_tier == PlanTier.TRIAL:
            # TRIAL or PRO both satisfy a TRIAL requirement
            return tier in (PlanTier.TRIAL, PlanTier.PRO)

        if required_tier == PlanTier.PRO:
            return tier == PlanTier.PRO

        return False

    async def check_feature_access(
        self, workspace_id: str, feature_id: str | Feature
    ) -> bool:
        """Return True if the workspace has access to ``feature_id``.

        Accepts either a ``Feature`` enum value or its string equivalent for
        backwards compatibility with existing callers.

        Args:
            workspace_id: The workspace to check.
            feature_id: A ``Feature`` enum value or its string equivalent
                (e.g. ``Feature.AI_INSIGHTS`` or ``'ai_insights'``).

        """
        # Normalise string callers — allows gradual migration to Feature enum
        try:
            feature = Feature(feature_id)
        except ValueError:
            # Unknown feature — default to accessible (free feature)
            return True

        if feature in _PRO_FEATURES:
            return await self.is_pro_workspace(workspace_id)

        return True

    async def invalidate_tier_cache(self, workspace_id: str) -> None:
        """Clear the cached plan tier for ``workspace_id``.

        Call this after any subscription change (upgrade, downgrade, cancel)
        to ensure the next tier lookup reflects the new state.

        Args:
            workspace_id: The workspace whose tier cache to purge.

        """
        await self._tier_cache.invalidate(workspace_id)
        logger.debug("Invalidated tier cache for workspace %s", workspace_id)
