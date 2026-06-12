from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel

from app.core.dependencies import get_integration_service
from app.core.logging import get_corr_id, get_logger
from app.db.records import Provider
from app.domains.crm.integration_service import IntegrationService
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils.helpers import HS_CONTACT_TYPE_ID, normalize_object_type

logger = get_logger("hubspot.workflow-actions")
router = APIRouter(prefix="/integrations/hubspot", tags=["hubspot-workflow-actions"])


class HubSpotWorkflowActionPayload(BaseModel):
    callbackId: str | None = None
    origin: Mapping[str, Any]
    object: Mapping[str, Any]
    fields: Mapping[str, Any]


@router.post("/workflow-action")
async def handle_workflow_action(  # noqa: PLR0912, PLR0915
    payload: HubSpotWorkflowActionPayload = Body(...),
    corr_id: str = Depends(get_corr_id),
    integration_service: IntegrationService = Depends(get_integration_service),
):
    """Handles a custom workflow action execution request from HubSpot.
    Specifically designed for the 'Send Slack Message' action.
    """
    portal_id = str(payload.origin.get("portalId"))

    message_text = payload.fields.get("message_text")
    workspace_id = payload.fields.get("workspace_id")

    if not workspace_id:
        # portal_id (e.g. 147910822) is NOT the workspace_id.
        # Resolve the real workspace_id via the HubSpot integration record.
        hs_integration = await integration_service.storage.get_integration_by_portal_id(
            portal_id
        )
        if not hs_integration:
            return {
                "status": "ok",
                "message": f"No HubSpot integration found for portal_id={portal_id}",
            }
        workspace_id = hs_integration.workspace_id

    slack_integration = await integration_service.get_integration(
        workspace_id=workspace_id,
        provider=Provider.SLACK,
    )
    if not slack_integration:
        return {"status": "ok", "message": "Slack not connected for this workspace"}

    # Resolve channel: use workflow field value first;
    # fall back to the default channel saved in the settings page.
    channel_id = payload.fields.get("channel_id") or slack_integration.metadata.get(
        "channel_id", ""
    )

    # Initialize MessagingService to handle Slack dispatch
    messaging_service = SlackMessagingService(
        corr_id=corr_id,
        integration_service=integration_service,
        slack_integration=slack_integration,
    )

    # Resolve channel name → ID if a human-readable name was given
    target_id = channel_id
    is_slack_id = (
        channel_id
        and len(channel_id) >= 9  # noqa: PLR2004
        and channel_id[0] in ("C", "G", "D", "U")
        and channel_id[1:].isalnum()
        and channel_id[1:].isupper()
    )
    if channel_id and not is_slack_id:
        slack_channel = await messaging_service.get_slack_channel()
        resolved_id = await slack_channel.resolve_channel_name(channel_id)
        if resolved_id:
            target_id = resolved_id

    # 4. For engagements (emails, calls, notes), fetch and append the body
    # to provide context if not already present in message_text.
    obj_type = payload.object.get("objectType")
    obj_id = str(payload.object.get("objectId") or "")

    full_obj = None

    if obj_type and obj_id:
        normalized_obj_type = normalize_object_type(obj_type)
        # Map object types to engagement names as understood by hubspot_service
        engagement_map = {
            "email": "emails",
            "call": "calls",
            "note": "notes",
        }
        hs_type = engagement_map.get(normalized_obj_type)
        if hs_type:
            try:
                if hs_type == "emails":
                    full_obj = await integration_service.hubspot_service.get_email(
                        workspace_id, obj_id
                    )
                elif hs_type == "calls":
                    full_obj = await integration_service.hubspot_service.get_call(
                        workspace_id, obj_id
                    )
                elif hs_type == "notes":
                    full_obj = await integration_service.hubspot_service.get_note(
                        workspace_id, obj_id
                    )
            except Exception as e:
                logger.warning(
                    "Failed to fetch engagement body for workflow action: %s", e
                )
        elif normalized_obj_type == "contact":
            try:
                # If contact-based, fetch latest engagement to provide context.
                # Use ignore_cache=True because the workflow trigger happens
                # immediately after the engagement is logged.
                engs = await integration_service.hubspot_service.get_object_engagements(
                    workspace_id, "contacts", obj_id, ignore_cache=True
                )
                if engs:
                    # Robust timestamp parsing to handle Unix ms or ISO strings
                    def _parse_ts(e: dict[str, Any]) -> float:
                        ts_val = e.get("properties", {}).get("hs_timestamp")
                        if not ts_val:
                            return 0.0
                        try:
                            # Try as numeric first (HubSpot often uses milliseconds)
                            return float(ts_val)
                        except (ValueError, TypeError):
                            try:
                                # ISO string. Clean up 'Z' and normalize.
                                ts_str = str(ts_val).replace("Z", "+00:00")
                                dt = datetime.fromisoformat(ts_str)
                                return dt.timestamp() * 1000  # Convert to ms
                            except Exception:
                                return 0.0

                    # Sort by timestamp descending
                    engs.sort(key=_parse_ts, reverse=True)

                    # Prioritization: Look for the latest Email or Note specifically
                    # within a reasonable window (e.g., if multiple exist).
                    best_match = None
                    for e in engs[:5]:  # Check top 5 latest
                        etype = e.get("_engagement_type")
                        if etype in ("emails", "notes", "calls"):
                            best_match = e
                            break

                    full_obj = best_match or engs[0]
            except Exception as e:
                logger.warning(
                    "Failed to fetch associated engagements for contact: %s", e
                )

        if full_obj:
            props = full_obj.get("properties") or {}
            # Detect automated marketing emails
            is_automated = bool(props.get("hs_automated_email_id"))

            body = (
                props.get("hs_email_text")
                or props.get("hs_email_html")
                or props.get("hs_note_body")
                or props.get("hs_call_body")
                or props.get("hs_meeting_body")
                or props.get("hs_task_body")
                or ""
            )

            if body:
                display_body = body.strip()

                # Clean up marketing footers (Unsubscribe / Preferences)
                if "Prefer fewer emails from me?" in display_body:
                    display_body = display_body.split("Prefer fewer emails from me?")[
                        0
                    ].strip()
                elif "Unsubscribe" in display_body:
                    display_body = display_body.split("Unsubscribe")[0].strip()

                # Best Practice: Suppress body for automated marketing blasts
                if is_automated:
                    display_body = (
                        "_Automated marketing email (content hidden to reduce noise)_"
                    )
                elif len(display_body) > 500:  # noqa: PLR2004
                    # Shorten truncation to 500 for better Slack hygiene
                    display_body = display_body[:497] + "..."

                # Construct direct HubSpot deep link if portal_id is available
                hs_link = ""
                record_id = obj_id
                if portal_id and record_id:
                    # Default to contact record view with interaction focus
                    hs_link = (
                        f"https://app.hubspot.com/contacts/{portal_id}/record/{HS_CONTACT_TYPE_ID}/"
                        f"{record_id}?interaction={full_obj.get('id', '')}"
                    )

                detail_block = f"*Included Details:*\n{display_body}"
                if hs_link:
                    detail_block += f"\n\n<{hs_link}|View in HubSpot>"

                if message_text:
                    message_text += f"\n\n{detail_block}"
                else:
                    message_text = detail_block

    # Send message to Slack
    resp = await messaging_service.send_message(
        workspace_id=workspace_id,
        channel=target_id,
        text=message_text,
    )

    # If the message was sent successfully and we have object context from HubSpot,
    # map the thread so users can reply natively in Slack.
    if resp and resp.get("ts"):
        obj_type = payload.object.get("objectType")
        obj_id = payload.object.get("objectId")
        if obj_type and obj_id:
            # Determine source context for reply prefix
            source = "workflow"
            if full_obj:
                eng_type = full_obj.get("_engagement_type", "")
                normalized_type = normalize_object_type(obj_type)
                if eng_type == "emails" or normalized_type == "email":
                    source = "email"
                elif eng_type == "calls" or normalized_type == "call":
                    source = "call"
                elif eng_type == "notes" or normalized_type == "note":
                    source = "note"

            logger.info(
                "Storing thread mapping for workflow action obj=%s:%s source=%s",
                obj_type,
                obj_id,
                source,
            )
            await integration_service.storage.upsert_thread_mapping(
                {
                    "workspace_id": workspace_id,
                    "object_type": obj_type.lower(),
                    "object_id": str(obj_id),
                    "channel_id": target_id,
                    "thread_ts": str(resp.get("ts")),
                    "source": source,
                }
            )

    return {"status": "ok"}
