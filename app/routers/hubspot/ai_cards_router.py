# app/api/hubspot/ai_cards_router.py  # noqa: D100
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.dependencies import get_ai_service, get_hubspot_service
from app.core.logging import get_corr_id, get_logger
from app.domains.ai.service import AIContactAnalysis, AIService
from app.domains.crm.hubspot.service import HubSpotService
from app.routers.hubspot.webhook_router import verify_hubspot_signature

router = APIRouter(prefix="/hubspot/ai", tags=["hubspot-ai"])
logger = get_logger("hubspot.ai")


@router.get("/contact-analysis")
async def contact_analysis(
    request: Request,
    objectId: str,
    portalId: str,
    corr_id: str = Depends(get_corr_id),
    hubspot: HubSpotService = Depends(get_hubspot_service),
    ai: AIService = Depends(get_ai_service),
    _sig: None = Depends(verify_hubspot_signature),
):
    """Generates AI insights for a contact, with robust signature verification and fallbacks (CR-22, CR-24)."""
    if not portalId:
        raise HTTPException(400, "Missing portalId")

    contact = await hubspot.get_contact(portalId, objectId)
    if not contact:
        raise HTTPException(404, "Contact not found")

    engagements = await hubspot.get_object_engagements(portalId, "contact", objectId)

    try:
        analysis = await ai.analyze_contact(contact, engagements=engagements)
    except Exception as exc:
        # CR-22: Fallback to static insights if AI engine fails
        logger.error("AI Analysis failed for contact %s: %s (CR-22)", objectId, exc)
        analysis = AIContactAnalysis(
            insight="*Basic Insights (AI Engine Offline)*\nRecently active contact. Review last touchpoint for context.",
            score=50,
            score_reason="Static fallback score during engine instability.",
            next_best_action="📥 **Manual Review:** Continue normal workflow.",
            next_action_reason="AI insights currently unavailable.",
        )

    return {
        "analysis": analysis.model_dump()
        if hasattr(analysis, "model_dump")
        else analysis.__dict__
    }
