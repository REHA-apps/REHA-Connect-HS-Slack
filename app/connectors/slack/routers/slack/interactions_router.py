# app/api/slack/interactions_router.py  # noqa: D100
from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response

from app.connectors.slack.services.service import InteractionService
from app.core.dependencies import get_integration_service
from app.core.logging import get_corr_id, get_logger, triple_key_context
from app.core.security.provider_signature import verify_provider_signature
from app.domains.crm.integration_service import IntegrationService

router = APIRouter(prefix="/slack", tags=["slack-interactions"])
logger = get_logger("slack.interactions")


@router.post("/interactions", dependencies=[Depends(verify_provider_signature)])
async def slack_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    corr_id: str = Depends(get_corr_id),
    integration_service: IntegrationService = Depends(get_integration_service),
) -> Response:
    """Handles Slack interactivity callbacks (button clicks, modal submissions).

    Thin wrapper that delegates domain logic (modals, shortcuts, interactions)
    to the InteractionService.
    """
    # 1. Parse payload
    form = await request.form()
    payload_str = form.get("payload")
    if not payload_str:
        logger.error("Missing payload in Slack interaction")
        raise HTTPException(status_code=400, detail="Missing payload")

    try:
        payload = json.loads(str(payload_str))
        logger.info("Received interaction type: %s", payload.get("type"))
    except Exception as exc:
        logger.error("Failed to parse Slack interaction payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # 2. Resolve target team/integration (single lookup — forwarded to fast-path)
    team_id = str(payload.get("team", {}).get("id", ""))
    integration = await integration_service.get_integration_by_slack_team_id(team_id)

    if not integration:
        logger.warning("No Slack integration found for team_id=%s", team_id)
        return Response(status_code=200)

    # 3. Build InteractionService with lazy-init stubs.
    # HubSpotService / AIService are only needed for background tasks (view_submission,
    # command processing) — NOT for fast-path modal opens.  Constructing them here
    # would waste ~50-150ms before views.open is called and expire the trigger_id.
    # We defer their creation to inside dispatch_interaction when required.
    from app.domains.ai.service import AIService

    interaction_svc = InteractionService(
        ai=None,  # type: ignore[arg-type]  # lazy — set before background dispatch
        integration_service=integration_service,
        ai_factory=lambda: AIService(corr_id=corr_id),
    )

    # 4. Delegate to central dispatcher in the service
    slack_ts = payload.get("container", {}).get("thread_ts") or payload.get(
        "message", {}
    ).get("ts")
    portal_id = integration.metadata.get("portal_id")

    with triple_key_context(slack_ts=slack_ts, portal_id=portal_id):
        return await interaction_svc.dispatch_interaction(
            payload=payload,
            integration=integration,
            background_tasks=background_tasks,
            corr_id=corr_id,
        )
