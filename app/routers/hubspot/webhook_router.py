# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.core.config import settings
from app.core.dependencies import get_integration_service, get_storage_service
from app.core.logging import get_logger, triple_key_context
from app.core.security.hubspot_signature import verify_hubspot_signature
from app.db.storage_service import StorageService
from app.domains.common.audit_service import AuditService
from app.domains.crm.integration_service import IntegrationService
from app.domains.crm.notification_service import NotificationService
from app.utils.sqs_helpers import publish_to_sqs

router = APIRouter(prefix="/hubspot/webhooks", tags=["hubspot_webhooks"])
logger = get_logger("hubspot.webhooks")


@router.post("")
async def handle_hubspot_events(
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_hubspot_signature),
) -> dict[str, str]:
    """Receives and processes HubSpot webhook events asynchronously."""
    try:
        events = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(events, list):
        # HubSpot sends an array of events
        events = [events]

    # Use a shared correlation ID for this batch of events
    corr_id = f"hs-hook-{uuid.uuid4().hex[:8]}"

    logger.info("Accepted %d HubSpot events (corr_id=%s)", len(events), corr_id)

    # Offload processing to background task to satisfy HubSpot 3s timeout
    portal_id = str(events[0].get("portalId")) if events else None

    # 2026.03 Pattern: SQS-First queueing or local dev background tasks fallback
    queue_url = (
        settings.SQS_HUBSPOT_WEBHOOK_QUEUE_URL or settings.SQS_SLACK_WEBHOOK_QUEUE_URL
    )
    workspace_id = f"hs_{portal_id}" if portal_id else "hubspot"

    # Determine if we need to delay the SQS message for HubSpot creation settling
    has_creation_event = any(
        "creation" in str(e.get("subscriptionType", "")).lower() for e in events
    )
    delay_seconds = 1 if has_creation_event else 0

    published = publish_to_sqs(
        queue_url=queue_url,
        workspace_id=workspace_id,
        corr_id=corr_id,
        task_type="hubspot_webhook",
        delay_seconds=delay_seconds,
        payload={
            "events": events,
            "corr_id": corr_id,
            "portal_id": portal_id,
        },
    )

    if not published:
        with triple_key_context(corr_id=corr_id, portal_id=portal_id):
            background_tasks.add_task(
                _process_webhook_batch, events, corr_id, portal_id, delay_seconds
            )

    return {"status": "accepted", "corr_id": corr_id}


async def _process_webhook_batch(
    events: list[dict],
    corr_id: str,
    portal_id: str | None = None,
    delay_seconds: int = 0,
) -> None:
    """Helper to process a batch of events in the background."""
    import asyncio

    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    with triple_key_context(corr_id=corr_id, portal_id=portal_id):
        storage = get_storage_service()
        service = NotificationService(corr_id=corr_id)

        # Process events. NotificationService.process_event_batch handles idempotency
        # and loops through each event internally.
        await service.process_event_batch(events, storage)


@router.post("/uninstall")
async def handle_app_uninstall(
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_hubspot_signature),
    storage: StorageService = Depends(get_storage_service),
    integration_service: IntegrationService = Depends(get_integration_service),
) -> dict[str, str]:
    """Called by HubSpot when a user uninstalls the app from their portal.

    Required for HubSpot Marketplace listing.
    Immediately downgrades the workspace to FREE and cancels the subscription.
    Configure this URL in your HubSpot developer portal under
    App Settings → Uninstall URL.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    portal_id = str(payload.get("portalId", ""))

    if not portal_id:
        logger.warning("Uninstall webhook received without portalId: %s", payload)
        return {"status": "ignored"}

    logger.info("App uninstall received for portal_id=%s", portal_id)

    # Resolve internal workspace_id from portal_id
    integration = await storage.get_integration_by_portal_id(portal_id)
    if not integration:
        logger.info(
            "Uninstall already processed or integration missing for portal_id=%s",
            portal_id,
        )
        return {"status": "already_processed"}

    workspace_id = integration.workspace_id

    # 1. Trigger robust uninstall pipeline handling both Stripe cancellation and Data destruction
    await integration_service.uninstall_hubspot(workspace_id)

    # 2. Security Audit Log (Synchronous for reliable compliance on Lambda)
    corr_id = f"uninstall-{uuid.uuid4().hex[:8]}"
    audit = AuditService(corr_id=corr_id)
    await audit.log_action(
        action="hubspot_uninstall",
        workspace_id=workspace_id,
        request=request,
        metadata={"portal_id": portal_id, "source": "hubspot_webhook"},
    )

    logger.info(
        "Workspace %s app uninstall processed via global pipeline (portal_id=%s)",
        workspace_id,
        portal_id,
    )

    return {"status": "uninstalled"}
