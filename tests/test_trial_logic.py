import asyncio  # noqa: D100
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

# Add the project root to sys.path
sys.path.append(os.getcwd())

from app.connectors.common.registry import ChannelRegistry
from app.db.records import PlanTier, WorkspaceRecord
from app.db.storage_service import StorageService
from app.domains.ai.service import AIContactAnalysis, AIDealAnalysis
from app.domains.crm.integration_service import IntegrationService
from app.domains.crm.ui.card_builder import CardBuilder


async def test_trial_logic():
    print("Testing Trial Logic...")

    # Mock ChannelRegistry to avoid ValueError
    ChannelRegistry.get_channel = MagicMock(return_value=MagicMock())

    storage = MagicMock(spec=StorageService)
    service = IntegrationService("test-corr-id", storage=storage)

    # Tier cache is in TierService now, which is a cached_property on service
    await service.tier._tier_cache.clear()

    recent_date = datetime.now(UTC) - timedelta(days=6)
    workspace_recent = WorkspaceRecord(
        id="ws-recent",
        install_date=recent_date,
        plan=PlanTier.TRIAL,
        subscription_status="trialing",
    )
    storage.get_workspace = AsyncMock(return_value=workspace_recent)

    tier_recent = await service.get_tier("ws-recent")
    print(f"WS (6 days old) Tier: {tier_recent} (Expected: PlanTier.PRO)")
    if tier_recent != PlanTier.PRO:
        print(f"DEBUG: install_date={recent_date}")
        assert tier_recent == PlanTier.PRO

    # 2. Test FREE (after 7 days)
    await service.tier._tier_cache.clear()
    old_date = datetime.now(UTC) - timedelta(days=8)
    workspace_old = WorkspaceRecord(
        id="ws-old",
        install_date=old_date,
        plan=PlanTier.TRIAL,
        subscription_status="trialing",
    )
    storage.get_workspace = AsyncMock(return_value=workspace_old)

    tier_old = await service.get_tier("ws-old")
    print(f"WS (8 days old) Tier: {tier_old} (Expected: PlanTier.FREE)")
    assert tier_old == PlanTier.FREE


async def test_ui_restrictions():
    print("\nTesting UI Restrictions...")
    builder = CardBuilder()

    # Mock data
    obj = {
        "id": "123",
        "properties": {
            "firstname": "John",
            "lastname": "Doe",
            "email": "john@example.com",
        },
    }
    analysis = MagicMock(spec=AIContactAnalysis)
    analysis = MagicMock(spec=AIContactAnalysis)
    analysis.score = 85
    analysis.pulse_score = 90
    analysis.insight = "Insight"
    analysis.next_best_action = "Action"

    # 1. Pro Card (Should have buttons)
    pro_card = builder.build_contact(obj, analysis, is_pro=True)
    print(f"Pro Card Buttons Count: {len(pro_card.actions)} (Expected: > 0)")
    assert len(pro_card.actions) > 0

    # 2. Free Card (Should have buttons but they are LOCK symbols)
    free_card = builder.build_contact(obj, analysis, is_pro=False)
    # CardBuilder marks gated actions in the IR
    print(f"Free Card Buttons Count: {len(free_card.actions)} (Expected: > 0)")
    # Instead of assert 0, we assert they are present but logically gated in the IR
    assert len(free_card.actions) > 0
    # The CardBuilder IR itself should mark them as is_gated
    assert any(a.is_gated for a in free_card.actions)

    # 3. Deal Card (Special case with local actions list)
    deal_obj = {
        "id": "456",
        "properties": {
            "dealname": "Big Deal",
            "amount": 1000,
            "dealstage": "negotiation",
        },
    }
    deal_analysis = MagicMock(spec=AIDealAnalysis)
    deal_analysis.risk = "High"
    deal_analysis.score = 70
    deal_analysis.deal_health = "High"
    deal_analysis.pulse_score = 75
    deal_analysis.momentum_score = 10
    deal_analysis.insight = "Deal Summary"
    deal_analysis.next_best_action = "Follow up"

    free_deal_card = builder.build_deal(deal_obj, deal_analysis, is_pro=False)
    print(
        f"Free Deal Card Buttons Count: {len(free_deal_card.actions)} (Expected: > 0)"
    )
    assert len(free_deal_card.actions) > 0
    assert any(a.is_gated for a in free_deal_card.actions)


if __name__ == "__main__":
    asyncio.run(test_trial_logic())
    asyncio.run(test_ui_restrictions())
    print("\nAll tests passed!")
