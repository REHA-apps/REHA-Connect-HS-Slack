# ruff: noqa: E501  # noqa: D100
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.logging import get_logger
from app.core.security.hubspot_signature import verify_hubspot_signature
from app.db.records import PlanTier
from app.db.storage_service import StorageService
from app.domains.common.audit_service import AuditService
from app.domains.crm.integration_service import IntegrationService
from app.domains.crm.user_mapping_service import UserMappingService

router = APIRouter(prefix="/user-mappings", tags=["user_mappings"])
logger = get_logger("api.user_mappings")


def get_storage() -> StorageService:
    return StorageService(corr_id="api-user-mappings")


def get_mapping_service(
    storage: StorageService = Depends(get_storage),
) -> UserMappingService:
    return UserMappingService(corr_id="api-user-mappings", storage=storage)


def get_integration_service(
    storage: StorageService = Depends(get_storage),
) -> IntegrationService:
    return IntegrationService(corr_id="api-user-mappings", storage=storage)


async def verify_hubspot_idor(
    request: Request,
    storage: StorageService = Depends(get_storage),
) -> str:
    """Verifies portalId from hubspot.fetch and securely returns the workspace_id."""
    portal_id = request.query_params.get("portalId")
    if not portal_id:
        logger.error("IDOR Check failed: Missing portalId query param")
        raise HTTPException(
            status_code=403, detail="Missing portalId context for IDOR verification"
        )

    integration = await storage.get_integration_by_portal_id(portal_id)
    if not integration:
        logger.error(f"IDOR Check failed: portalId {portal_id} not registered.")
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to access data for this workspace.",
        )

    return integration.workspace_id


@router.get("/", dependencies=[Depends(verify_hubspot_signature)])
async def get_mappings(
    workspace_id: str = Depends(verify_hubspot_idor),
    service: UserMappingService = Depends(get_mapping_service),
) -> dict[str, Any]:
    """Get all user mappings for a given workspace, enriched with names."""
    result = await service.get_enriched_mappings(workspace_id)
    return result


@router.post("/sync", dependencies=[Depends(verify_hubspot_signature)])
async def sync_mappings(
    workspace_id: str = Depends(verify_hubspot_idor),
    service: UserMappingService = Depends(get_mapping_service),
) -> dict[str, Any]:
    """Trigger a manual background sync of HubSpot owners to Slack users."""
    stats = await service.sync_workspace(workspace_id)
    return {"status": "success", "stats": stats}


@router.put("/manual", dependencies=[Depends(verify_hubspot_signature)])
async def update_manual_mapping(
    request: Request,
    payload: dict[str, Any],
    workspace_id: str = Depends(verify_hubspot_idor),
    storage: StorageService = Depends(get_storage),
) -> dict[str, Any]:
    """Manually link or unlink a HubSpot owner to a Slack user."""
    owner_id = payload.get("hubspot_owner_id")
    slack_id = payload.get("slack_user_id")

    if not owner_id:
        raise HTTPException(status_code=400, detail="Missing hubspot_owner_id")

    # Tier Gating: Enforce "1 Manual Mapping" limit for Free Tier
    integration_service = get_integration_service(storage)
    tier = await integration_service.get_tier(workspace_id)

    if tier == PlanTier.FREE:
        existing = await storage.get_all_user_mappings(workspace_id)
        manual_mappings = [m for m in existing if m.mapping_status == "manual"]

        # Check if we are trying to create a NEW manual mapping beyond the limit
        is_updating_existing = any(
            m.hubspot_owner_id == int(owner_id) for m in manual_mappings
        )

        if len(manual_mappings) >= 1 and not is_updating_existing and slack_id:
            logger.info(
                "Blocked manual mapping for Free workspace %s (Limit: 1 reached)",
                workspace_id,
            )
            raise HTTPException(
                status_code=403,
                detail="Free tier is limited to 1 manual user mapping. Please upgrade to Pro for unlimited sync.",
            )

    await storage.upsert_user_mapping(
        {
            "workspace_id": workspace_id,
            "hubspot_owner_id": int(owner_id),
            "slack_user_id": slack_id,
            "mapping_status": "manual",
        }
    )

    # Security Audit Log
    audit = AuditService(corr_id="api-user-mappings")
    await audit.log_action(
        action="user_mapping_update",
        workspace_id=workspace_id,
        request=request,
        metadata={
            "hubspot_owner_id": owner_id,
            "slack_user_id": slack_id,
            "type": "manual_link" if slack_id else "manual_unlink",
        },
    )

    return {"status": "success"}
