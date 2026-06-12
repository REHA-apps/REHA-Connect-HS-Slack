from __future__ import annotations  # noqa: D100

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core.config import settings
from app.core.dependencies import get_corr_id, get_integration_service
from app.core.logging import get_logger
from app.db.records import Provider
from app.domains.crm.integration_service import IntegrationService
from app.domains.messaging.slack.service import SlackMessagingService

router = APIRouter(prefix="/hubspot/extensions", tags=["hubspot-extensions"])
logger = get_logger("hubspot.extensions")

MAX_DISPLAY_LENGTH = 100


@router.get("/activity")
async def get_slack_activity(
    objectId: str,
    portalId: str,
    # HubSpot CRM Cards send the object type in hs_object_type
    hs_object_type: str = Query(..., alias="hs_object_type"),
    corr_id: str = Depends(get_corr_id),
    integration_service: IntegrationService = Depends(get_integration_service),
) -> dict[str, Any]:
    """HubSpot CRM Card endpoint that returns the latest Slack activity for an object.
    Gated by Pro plan.
    """
    # 1. Pro plan check
    is_pro = await integration_service.is_pro_workspace(portalId)
    if not is_pro:
        return {
            "results": [
                {
                    "objectId": "pro-only",
                    "title": "Upgrade to Pro",
                    "properties": [
                        {
                            "label": "Feature",
                            "value": "Slack Activity Sync is a Pro feature.",
                        }
                    ],
                }
            ]
        }

    # 2. Resolve Integration & Service
    slack_integration = await integration_service.get_integration(
        workspace_id=portalId,
        provider=Provider.SLACK,
    )
    if not slack_integration:
        return {"results": []}

    messaging_service = SlackMessagingService(
        corr_id=corr_id,
        integration_service=integration_service,
        slack_integration=slack_integration,
    )

    # 3. Look up thread mapping (Simplified: fetch last mapping for this object)
    # Note: In a production app, we'd handle multiple mapping across channels.
    # For now, we use a simple fetch.
    mapping = await integration_service.storage.thread_mappings.fetch_single(
        {
            "workspace_id": portalId,
            "object_type": hs_object_type,
            "object_id": objectId,
        }
    )

    if not mapping:
        return {
            "results": [
                {
                    "objectId": f"no-activity-{objectId}",
                    "title": "No Slack Activity",
                    "properties": [
                        {
                            "label": "Status",
                            "value": "Waiting for first notification...",
                        }
                    ],
                }
            ]
        }

    # 4. Fetch Replies from Slack
    slack_channel = await messaging_service.get_slack_channel()
    replies = await slack_channel.get_thread_replies(
        channel_id=mapping.channel_id,
        thread_ts=mapping.thread_ts,
    )

    if not replies:
        return {"results": []}

    # 5. Format for HubSpot CRM Card
    # We take the latest reply or parent if no replies
    latest = replies[-1]
    text = latest.get("text", "")
    user = latest.get("user", "Unknown")
    ts = latest.get("ts", "")

    # Truncate text
    display_text = (
        (text[:MAX_DISPLAY_LENGTH] + "...") if len(text) > MAX_DISPLAY_LENGTH else text
    )

    # Build Slack deep link
    # https://slack.com/app_redirect?channel=C12345&thread_ts=12345.6789
    slack_url = (
        f"https://slack.com/app_redirect?channel={mapping.channel_id}"
        f"&thread_ts={mapping.thread_ts}"
    )

    return {
        "results": [
            {
                "objectId": mapping.thread_ts,
                "title": "Latest Slack Discussion",
                "link": slack_url,
                "properties": [
                    {"label": "Latest Message", "value": display_text},
                    {"label": "By", "value": f"<@{user}>"},
                    {"label": "Time", "value": ts},
                ],
                "actions": [
                    {
                        "type": "CONFIRMATION",
                        "httpMethod": "POST",
                        "uri": (
                            f"{settings.API_PUBLIC_URL.unicode_string().rstrip('/')}"
                            f"/api/hubspot/actions/ping-owner?objectId={objectId}&portalId={portalId}"
                        ),
                        "label": "Ping Owner",
                        "associatedObjectProperties": [],
                        "confirmationMessage": "Send a Slack DM to the deal owner?",
                        "confirmButtonText": "Send Ping",
                        "cancelButtonText": "Cancel",
                    }
                ],
            }
        ]
    }
