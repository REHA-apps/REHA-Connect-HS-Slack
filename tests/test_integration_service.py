"""Minimal smoke tests for IntegrationService — critical paths.

Addresses M-09 from the production code review (empty test file).
Focuses on the key domain logic that has zero coverage:
- Feature access gate with Feature enum and string backwards-compat
- Tier resolution boundaries
- Identity resolution recursion guard
- is_at_least_tier TRIAL handling (M-05 fix)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.records import PlanTier, Provider
from app.domains.crm.integration_service import Feature, IntegrationService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_service() -> IntegrationService:
    """Build an IntegrationService with a fully mocked StorageService."""
    storage = MagicMock()
    storage.get_workspace = AsyncMock(return_value=None)
    storage.get_integration = AsyncMock(return_value=None)
    storage.get_integration_by_portal_id = AsyncMock(return_value=None)
    storage.list_integrations_by_slack_team_id = AsyncMock(return_value=[])

    with patch(
        "app.connectors.common.registry.ChannelRegistry.get_channel",
        return_value=MagicMock(),
    ):
        svc = IntegrationService(corr_id="test", storage=storage)

    return svc


# ---------------------------------------------------------------------------
# Feature Access Gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_access_with_enum_value_pro_workspace():
    """Feature.AI_INSIGHTS must be granted for a PRO workspace."""
    svc = _make_service()
    # After H-01: check_feature_access routes through self.tier internally,
    # so mock must target the TierService instance.
    svc.tier.is_pro_workspace = AsyncMock(return_value=True)

    result = await svc.check_feature_access("ws-1", Feature.AI_INSIGHTS)

    assert result is True
    svc.tier.is_pro_workspace.assert_awaited_once_with("ws-1")


@pytest.mark.asyncio
async def test_feature_access_with_enum_value_free_workspace():
    """Feature.AI_INSIGHTS must be denied for a FREE workspace."""
    svc = _make_service()
    svc.is_pro_workspace = AsyncMock(return_value=False)

    result = await svc.check_feature_access("ws-1", Feature.AI_INSIGHTS)

    assert result is False


@pytest.mark.asyncio
async def test_feature_access_with_string_backwards_compat():
    """String feature IDs must still work (backwards-compat)."""
    svc = _make_service()
    svc.tier.is_pro_workspace = AsyncMock(return_value=True)

    result = await svc.check_feature_access("ws-1", "ai_insights")

    assert result is True


@pytest.mark.asyncio
async def test_feature_access_unknown_feature_defaults_to_true():
    """An unknown feature ID must default to True (free feature)."""
    svc = _make_service()
    svc.is_pro_workspace = AsyncMock(return_value=False)

    result = await svc.check_feature_access("ws-1", "nonexistent_feature")

    # is_pro_workspace should NOT be called — unknown = free = granted
    assert result is True
    svc.is_pro_workspace.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tier Resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tier_returns_free_for_unknown_workspace():
    """Non-existent workspace must resolve to FREE tier."""
    svc = _make_service()
    svc.storage.get_workspace = AsyncMock(return_value=None)

    tier = await svc.get_tier("unknown-ws")

    assert tier == PlanTier.FREE


@pytest.mark.asyncio
async def test_get_tier_returns_pro_for_active_subscription():
    """Active Stripe subscription must resolve to PRO tier."""
    svc = _make_service()
    workspace = MagicMock()
    workspace.subscription_status = "active"
    workspace.plan = PlanTier.FREE  # plan field doesn't matter when status is active
    workspace.trial_ends_at = None
    workspace.install_date = None
    workspace.created_at = None
    svc.storage.get_workspace = AsyncMock(return_value=workspace)

    tier = await svc.get_tier("ws-active")

    assert tier == PlanTier.PRO


@pytest.mark.asyncio
async def test_get_tier_returns_pro_within_7_day_trial():
    """Workspace installed 3 days ago must be in PRO trial."""
    svc = _make_service()
    workspace = MagicMock()
    workspace.subscription_status = "inactive"
    workspace.plan = PlanTier.FREE
    workspace.trial_ends_at = None
    workspace.install_date = datetime.now(UTC) - timedelta(days=3)
    workspace.created_at = None
    svc.storage.get_workspace = AsyncMock(return_value=workspace)

    tier = await svc.get_tier("ws-trial")

    assert tier == PlanTier.PRO


@pytest.mark.asyncio
async def test_get_tier_returns_free_after_trial_expires():
    """Workspace installed 10 days ago must fall back to FREE."""
    svc = _make_service()
    workspace = MagicMock()
    workspace.subscription_status = "inactive"
    workspace.plan = PlanTier.FREE
    workspace.trial_ends_at = None
    workspace.install_date = datetime.now(UTC) - timedelta(days=10)
    workspace.created_at = None
    svc.storage.get_workspace = AsyncMock(return_value=workspace)

    tier = await svc.get_tier("ws-expired")

    assert tier == PlanTier.FREE


# ---------------------------------------------------------------------------
# is_at_least_tier — TRIAL handling (M-05 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_at_least_tier_trial_grants_pro_workspace():
    """PRO workspace must satisfy a TRIAL-level requirement."""
    svc = _make_service()
    # After H-01: is_at_least_tier routes through self.tier.get_tier internally.
    svc.tier.get_tier = AsyncMock(return_value=PlanTier.PRO)

    result = await svc.is_at_least_tier("ws-1", PlanTier.TRIAL)

    assert result is True


@pytest.mark.asyncio
async def test_is_at_least_tier_trial_denies_free_workspace():
    """FREE workspace must not satisfy a TRIAL-level requirement."""
    svc = _make_service()
    svc.get_tier = AsyncMock(return_value=PlanTier.FREE)

    result = await svc.is_at_least_tier("ws-1", PlanTier.TRIAL)

    assert result is False


@pytest.mark.asyncio
async def test_is_at_least_tier_free_always_true():
    """FREE requirement must always return True regardless of tier."""
    svc = _make_service()
    svc.get_tier = AsyncMock(return_value=PlanTier.FREE)

    result = await svc.is_at_least_tier("ws-1", PlanTier.FREE)

    assert result is True
    svc.get_tier.assert_not_awaited()  # short-circuits before DB lookup


# ---------------------------------------------------------------------------
# Identity Bridge — Recursion Guard (H-05 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_hubspot_integration_stops_at_depth_5():
    """Circular identity chain must stop at depth=5 and return None."""
    svc = _make_service()

    # No direct HubSpot integration
    svc.storage.get_integration = AsyncMock(return_value=None)

    # Slack integration always returns linked_slack_workspace_id pointing back
    slack_mock = MagicMock()
    slack_mock.provider = Provider.SLACK
    slack_mock.metadata = {
        "linked_slack_workspace_id": "ws-parent",
        "slack_team_id": "T123",
    }
    slack_mock.credentials = {}

    async def get_integration_side_effect(workspace_id: str, provider: Provider):
        if provider == Provider.HUBSPOT:
            return None  # Never found
        return slack_mock  # Always return the Slack record pointing back

    svc.storage.get_integration = AsyncMock(side_effect=get_integration_side_effect)
    svc.storage.list_integrations_by_slack_team_id = AsyncMock(return_value=[])

    with patch.object(svc, "get_integration", wraps=svc.storage.get_integration):
        result = await svc.resolve_hubspot_integration("ws-start", _depth=0)

    # Must return None instead of raising RecursionError
    assert result is None


# ---------------------------------------------------------------------------
# Install-based Trial Expiration (Regression Tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tier_install_based_trial_active_within_7_days():
    """Local install trial with status trialing must be PRO within the 7-day window."""
    svc = _make_service()
    workspace = MagicMock()
    workspace.subscription_status = "trialing"
    workspace.plan = PlanTier.TRIAL
    workspace.trial_ends_at = datetime.now(UTC) + timedelta(days=3)
    workspace.install_date = datetime.now(UTC) - timedelta(days=4)
    workspace.created_at = None
    svc.storage.get_workspace = AsyncMock(return_value=workspace)

    tier = await svc.get_tier("ws-trial-active")

    assert tier == PlanTier.PRO


@pytest.mark.asyncio
async def test_get_tier_install_based_trial_expired():
    """Local install trial with status trialing must be FREE after expiration date."""
    svc = _make_service()
    workspace = MagicMock()
    workspace.subscription_status = "trialing"
    workspace.plan = PlanTier.TRIAL
    workspace.trial_ends_at = datetime.now(UTC) - timedelta(days=1)
    workspace.install_date = datetime.now(UTC) - timedelta(days=8)
    workspace.created_at = None
    svc.storage.get_workspace = AsyncMock(return_value=workspace)

    tier = await svc.get_tier("ws-trial-expired")

    assert tier == PlanTier.FREE
