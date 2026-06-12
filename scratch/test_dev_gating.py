import asyncio

from app.db.storage_service import StorageService
from app.domains.billing.tier_service import TierService


async def main():
    storage = StorageService()
    tier_service = TierService(storage)
    # Clear cache
    await tier_service._tier_cache.clear()

    # Query the two dev workspaces
    ws_hs = "hs_148238284"
    ws_slack = "T0AT1MJ1L64"

    tier_hs = await tier_service.get_tier(ws_hs)
    tier_slack = await tier_service.get_tier(ws_slack)

    print(f"Workspace {ws_hs} tier: {tier_hs}")
    print(f"Workspace {ws_slack} tier: {tier_slack}")


if __name__ == "__main__":
    asyncio.run(main())
