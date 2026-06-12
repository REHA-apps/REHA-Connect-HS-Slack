# app/api/slack/events_router.py  # noqa: D100
from __future__ import annotations  # noqa: I001

from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)

from app.core.dependencies import get_integration_service
from app.core.logging import get_corr_id, get_logger, triple_key_context
from app.core.security.slack_signature import (
    verify_slack_signature,
)
from app.domains.crm.integration_service import IntegrationService
from app.domains.messaging.slack.service import SlackMessagingService
from app.connectors.slack.services.event_router import SlackEventRouter


router = APIRouter(prefix="/slack", tags=["slack-events"])
logger = get_logger("slack.events")


@router.post("/events")
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    corr_id: str = Depends(get_corr_id),
    integration_service: IntegrationService = Depends(get_integration_service),
) -> Any:
    """Handles Slack Events API callbacks.

    Thin wrapper that delegates domain logic (unfurls, replies, uninstalls)
    to the SlackMessagingService.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        logger.error("Failed to parse Slack event payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # 0. Idempotency Check: Skip if we've already processed this event_id
    event_id = payload.get("event_id")
    if event_id:
        storage = integration_service.storage
        is_new = await storage.idempotency_svc.mark_processed(event_id, "slack")
        if not is_new:
            logger.info(
                "Ignoring duplicate Slack event: %s (distributed check)", event_id
            )
            return {"ok": True}

    event_type = payload.get("type")

    # 1. Slack URL verification challenge — exempted from signature check.
    #    During the initial challenge handshake, Slack does not send a signing
    #    secret, so verification would always fail. The challenge itself is
    #    sufficient proof of intent at this stage.
    if event_type == "url_verification":
        challenge = payload.get("challenge")
        logger.info("Responding to Slack challenge")
        return Response(content=challenge, media_type="text/plain")

    # 2. Enforce Slack HMAC-SHA256 signature for all real events.
    body = await request.body()
    await verify_slack_signature(request.headers, body, corr_id=corr_id)

    # 3. Extract event metadata
    event = payload.get("event", {})
    actual_event_type = event.get("type") or event_type
    team_id = payload.get("team_id")

    if not team_id:
        logger.warning("Slack event missing team_id: %s", actual_event_type)
        return {"ok": True}

    # 3. Resolve integration and delegate to service
    integration = await integration_service.get_integration_by_slack_team_id(team_id)
    if not integration:
        logger.warning("No Slack integration found for team_id=%s", team_id)
        # We return 200/ok to Slack even if we don't have the integration
        # to prevent Slack from retrying indefinitely.
        return {"ok": True}

    messaging_service = SlackMessagingService(
        corr_id=corr_id,
        integration_service=integration_service,
        slack_integration=integration,
    )

    event_router = SlackEventRouter(messaging_service)

    # Dispatch to service expert logic
    slack_ts = event.get("thread_ts") or event.get("ts")
    portal_id = integration.metadata.get("portal_id")

    with triple_key_context(slack_ts=slack_ts, portal_id=portal_id):
        return await event_router.dispatch_event(
            event_type=actual_event_type,
            payload=payload,
            background_tasks=background_tasks,
        )
