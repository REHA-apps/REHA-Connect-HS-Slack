from __future__ import annotations  # noqa: D100

import asyncio
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import (
    get_ai_service,
    get_hubspot_service,
    get_integration_service,
    get_workspace_id,
)
from app.core.logging import get_logger
from app.domains.ai.service import AIService
from app.domains.crm.hubspot.service import HubSpotService
from app.domains.crm.integration_service import IntegrationService
from app.domains.crm.ui import CardBuilder
from app.providers.hubspot.renderer import HubSpotRenderer

router = APIRouter(prefix="/hubspot/ui-extension", tags=["hubspot-ui-extension"])
logger = get_logger("hubspot.ui_extension")


@router.get("/insight")
async def get_insight(
    object_id: str = Query(..., alias="objectId"),
    hs_object_type: str = Query(..., alias="hs_object_type"),
    workspace_id: str = Depends(get_workspace_id),
    integration_service: IntegrationService = Depends(get_integration_service),
    hubspot: HubSpotService = Depends(get_hubspot_service),
    ai: AIService = Depends(get_ai_service),
) -> dict[str, Any]:
    """Return the UnifiedCard IR for rendering inside the HubSpot React sidebar.

    Fetches the CRM object, recent engagements, and Pro plan status in parallel,
    then runs AI analysis and renders the result to the HubSpot UI Extensions
    JSON format consumed by MirrorCard.tsx.
    """
    logger.info("Fetching UI extension insight for %s %s", hs_object_type, object_id)

    (obj, engagements), is_pro = await asyncio.gather(
        asyncio.gather(
            hubspot.get_object(
                workspace_id=workspace_id,
                object_type=hs_object_type,
                object_id=object_id,
            ),
            hubspot.get_object_engagements(workspace_id, hs_object_type, object_id),
        ),
        integration_service.is_pro_workspace(workspace_id),
    )
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")

    owner_name = None
    owner_id = obj.get("properties", {}).get("hubspot_owner_id")
    if owner_id:
        try:
            owners = await hubspot.get_owners(workspace_id)
            owner = next((o for o in owners if str(o["id"]) == str(owner_id)), None)
            if owner:
                owner_name = (
                    f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
                )
        except Exception:
            logger.warning("Failed to fetch owner for record %s", object_id)

    analysis = await ai.analyze_polymorphic(
        obj,
        hs_object_type,
        engagements=engagements,
        associated_objects=None,
        owner_name=owner_name,
        format_engagements=False,
    )

    unified_card = CardBuilder().build(obj, cast(Any, analysis), is_pro=is_pro)
    return HubSpotRenderer().render(object_id, unified_card, object_type=hs_object_type)


@router.get("/insight/public")
async def get_insight_public(
    object_id: str = Query(..., alias="objectId"),
    hs_object_type: str = Query(..., alias="hs_object_type"),
    portal_id: str = Query(..., alias="portalId"),
    hubspot: HubSpotService = Depends(get_hubspot_service),
    ai: AIService = Depends(get_ai_service),
) -> dict[str, Any]:
    """Insight endpoint for the Support Integration (static-auth) HubSpot app.

    Does NOT require a CRM Connectors OAuth IntegrationRecord in the database.
    Uses HUBSPOT_SUPPORT_ACCESS_TOKEN (server-side private app token) to fetch
    the CRM object directly from HubSpot's API via get_support_client().

    The Support Integration app uses ``type: static`` auth so it may be
    installed on portals that have no corresponding OAuth record.  This
    endpoint bridges that gap without touching the main integration flow.
    """
    logger.info(
        "Public insight for portal=%s %s %s", portal_id, hs_object_type, object_id
    )

    try:
        client = await hubspot.get_support_client()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Server not configured with a support access token: {exc}",
        ) from exc

    # Fetch the ticket object directly
    obj = await client.get_object(hs_object_type, object_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found in HubSpot")

    # Best-effort engagements (notes/emails/calls associated with the ticket)
    engagements: list[dict[str, Any]] = []
    try:
        from app.utils.helpers import normalize_object_type, pluralize_hs_type

        hs_plural = pluralize_hs_type(hs_object_type)
        for entity_type, props in {
            "notes": ["hs_note_body", "hs_timestamp"],
            "emails": ["hs_email_subject", "hs_email_direction", "hs_timestamp"],
            "calls": ["hs_call_title", "hs_call_body", "hs_timestamp"],
        }.items():
            try:
                assoc_ids = await client.get_associations(
                    hs_plural, object_id, normalize_object_type(entity_type)
                )
                if assoc_ids:
                    items = await client.batch_read(
                        entity_type, assoc_ids, properties=props
                    )
                    for item in items or []:
                        item["_engagement_type"] = entity_type
                    engagements.extend(items or [])
            except Exception:
                pass
    except Exception:
        pass  # Engagements are bonus — never block the card

    analysis = await ai.analyze_polymorphic(
        obj,
        hs_object_type,
        engagements=engagements,
        associated_objects=None,
        format_engagements=False,
    )

    # Public endpoint: treat callers as free tier (no workspace DB lookup)
    unified_card = CardBuilder().build(obj, cast(Any, analysis), is_pro=False)
    return HubSpotRenderer().render(object_id, unified_card, object_type=hs_object_type)
