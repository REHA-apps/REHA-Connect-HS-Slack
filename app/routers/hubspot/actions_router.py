import asyncio  # noqa: D100
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import (
    get_ai_service,
    get_hubspot_service,
    get_integration_service,
    get_slack_messaging_service,
    get_workspace_id,
)
from app.core.exceptions import IntegrationNotFoundError
from app.core.logging import get_corr_id, get_logger
from app.domains.ai.service import AIService
from app.domains.crm.hubspot.service import HubSpotService
from app.domains.crm.integration_service import IntegrationService
from app.domains.messaging.base import MessagingService
from app.utils.helpers import normalize_object_type

router = APIRouter(prefix="/hubspot/actions", tags=["hubspot-actions"])
logger = get_logger("hubspot.actions")


@router.post("/send-record-insights-to-slack")
async def send_record_insights_to_slack(
    object_id: Annotated[str, Query(..., alias="objectId")],
    hs_object_type: Annotated[str, Query(..., alias="hs_object_type")],
    workspace_id: Annotated[str, Depends(get_workspace_id)],
    hubspot: Annotated[HubSpotService, Depends(get_hubspot_service)],
    ai: Annotated[AIService, Depends(get_ai_service)],
    messaging_service: Annotated[
        MessagingService, Depends(get_slack_messaging_service)
    ],
    user_email: Annotated[str | None, Query(alias="userEmail")] = None,
    channel: Annotated[str | None, Query()] = None,
) -> dict[str, str]:
    """Analyse a HubSpot record and post insights to Slack.

    Args:
        object_id: The HubSpot CRM object ID.
        hs_object_type: The HubSpot object type (e.g., ``0-1`` for contacts).
        user_email: Optional user email to send the DM to.
        channel: Optional Slack channel override.
        workspace_id: Internal workspace ID resolved from ``portalId``.
        hubspot: HubSpot service (injected).
        ai: Record insights service (injected).
        messaging_service: Ready-to-use Slack messaging service (injected).

    """
    try:
        # Fetch object, engagements, and associations in parallel
        obj, engagements, associated_objects = await asyncio.gather(
            hubspot.get_object(
                workspace_id=workspace_id,
                object_type=hs_object_type,
                object_id=object_id,
            ),
            hubspot.get_object_engagements(workspace_id, hs_object_type, object_id),
            hubspot.get_all_associations(workspace_id, hs_object_type, object_id),
        )
        if not obj:
            raise HTTPException(404, f"Record not found for id {object_id}")

        # Resolved Owner details
        owner_id = obj.get("properties", {}).get("hubspot_owner_id")
        owner_name = await hubspot.resolve_owner_display_name(workspace_id, owner_id)

        analysis = await ai.analyze_polymorphic(
            obj,
            hs_object_type,
            engagements=engagements,
            associated_objects=associated_objects,
            owner_name=owner_name,
        )

        await messaging_service.send_record_insights(
            workspace_id=workspace_id,
            channel=channel,
            user_email=user_email,
            analysis=analysis,
        )

        return {"status": "ok"}
    except IntegrationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Integration not found: {exc.message}. "
                "Please ensure you have authorised both HubSpot and Slack."
            ),
        )


@router.post("/ping-owner")
async def ping_owner(
    object_id: str = Query(..., alias="objectId"),
    hs_object_type: str = Query(..., alias="hs_object_type"),
    user_email: str | None = Query(None, alias="userEmail"),
    corr_id: str = Depends(get_corr_id),
    workspace_id: str = Depends(get_workspace_id),
    hubspot: HubSpotService = Depends(get_hubspot_service),
    integration_service: IntegrationService = Depends(get_integration_service),
    messaging_service: MessagingService = Depends(get_slack_messaging_service),
) -> dict[str, str]:
    """Send a Slack DM to the record's assigned HubSpot owner.

    This is a Pro-tier feature. The Pro check is enforced server-side
    using the resolved workspace_id.

    Args:
        object_id: The HubSpot CRM object ID.
        hs_object_type: The HubSpot object type (e.g., ``0-1`` for contacts).
        corr_id: Correlation ID for structured logging.
        workspace_id: Internal workspace ID resolved from ``portalId``.
        hubspot: HubSpot service (injected).
        integration_service: Integration service (injected).
        messaging_service: Ready-to-use Slack messaging service (injected).

    Raises:
        HTTPException 403: Workspace is not on the Pro plan.
        HTTPException 404: Record, owner, or Slack user not found.
        HTTPException 400: Record has no owner assigned.

    """
    try:
        # Pro plan check
        is_pro = await integration_service.is_pro_workspace(workspace_id)
        if not is_pro:
            raise HTTPException(
                403, "Ping Owner is a Pro feature. Please upgrade your plan."
            )

        # Fetch the record
        obj = await hubspot.get_object(
            workspace_id=workspace_id,
            object_type=hs_object_type,
            object_id=object_id,
        )
        if not obj:
            raise HTTPException(404, f"{hs_object_type} not found")

        owner_id = obj.get("properties", {}).get("hubspot_owner_id")
        if not owner_id:
            raise HTTPException(400, "No owner assigned to this HubSpot record.")

        # Resilient Owner Lookup (Handles new team members immediately)
        owner = await hubspot.get_owner(workspace_id, owner_id)
        if not owner or not owner.get("email"):
            raise HTTPException(404, "Owner details or email not found in HubSpot.")

        owner_email = owner["email"]
        record_url = obj.get("hs_url", "https://app.hubspot.com")
        display_name = normalize_object_type(hs_object_type).capitalize()

        # Resolve names
        target_owner_name = hubspot.format_owner_name(owner)
        requester_name = "A team member"
        if user_email:
            requester = await hubspot.get_owner_by_email(workspace_id, user_email)
            if requester:
                requester_name = hubspot.format_owner_name(requester)
            else:
                requester_name = user_email

        # Natural wording if pinging yourself (e.g. during testing)
        is_self_ping = False
        if user_email and owner_email:
            is_self_ping = owner_email.lower() == user_email.lower()
        requester_label = "You are" if is_self_ping else f"*{requester_name}* is"

        ping_text = (
            f"Hi *{target_owner_name}*! {requester_label} requesting your attention on "
            f"this record in HubSpot:\n*{display_name}*: {record_url}"
        )

        sent = await messaging_service.send_dm(user_email=owner_email, text=ping_text)
        if not sent:
            raise HTTPException(
                404, f"Could not find a Slack user matching email: {owner_email}"
            )

        return {"status": "ok", "message": f"Ping sent to {owner_email}"}

    except IntegrationNotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
