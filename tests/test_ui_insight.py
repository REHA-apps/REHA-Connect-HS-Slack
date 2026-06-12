import asyncio  # noqa: D100

from app.db.storage_service import StorageService
from app.domains.ai.service import AIService
from app.domains.crm.hubspot.service import HubSpotService
from app.domains.crm.integration_service import IntegrationService
from app.domains.crm.ui.card_builder import CardBuilder


async def main():
    corr_id = "test-ui"
    portalId = "147910822"
    objectId = "716708562113"  # The ID of Brian Halligan from earlier
    hs_object_type = "contact"

    storage = StorageService(corr_id=corr_id)
    hubspot = HubSpotService(corr_id=corr_id, storage=storage)
    ai = AIService(corr_id=corr_id)
    integration = IntegrationService(corr_id=corr_id, storage=storage)

    try:
        # 1. Fetch Object
        obj = await hubspot.get_object(
            workspace_id=portalId,
            object_type=hs_object_type,
            object_id=objectId,
        )
        if not obj:
            print("Object not found")
            return

        # 2. Run Analysis
        engagements = await hubspot.get_object_engagements(
            portalId, hs_object_type, objectId
        )

        analysis = await ai.analyze_polymorphic(
            obj, hs_object_type, engagements=engagements
        )
        if analysis is None:
            print("Analysis returned None")
            return

        # 3. Build Unified IR
        is_pro = await integration.is_pro_workspace(portalId)
        builder = CardBuilder()
        unified_card = builder.build(obj, analysis, is_pro=is_pro)  # type: ignore

        print(unified_card.model_dump())
    except Exception as e:
        print(f"FAILED WITH EXCEPTION: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
