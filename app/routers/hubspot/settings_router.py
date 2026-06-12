# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_corr_id, get_storage_service
from app.core.logging import get_logger
from app.db.records import Provider
from app.db.storage_service import StorageService

router = APIRouter(prefix="/hubspot/settings", tags=["hubspot-settings"])
logger = get_logger("hubspot.settings")


async def _resolve_workspace_id(portal_id: str | int, storage: StorageService) -> str:
    """Look up the internal workspace_id from a HubSpot portal ID.

    Uses the existing get_integration_by_portal_id method which queries the
    HubSpot integration record by its metadata portal_id field.
    """
    portal_id_str = str(portal_id)
    hs_integration = await storage.get_integration_by_portal_id(portal_id_str)
    if not hs_integration:
        logger.warning("No HubSpot integration found for portal_id=%s", portal_id_str)
        raise HTTPException(
            status_code=404,
            detail=f"No HubSpot integration found for portal_id={portal_id_str}",
        )
    return hs_integration.workspace_id


class SettingsPayload(BaseModel):
    portal_id: str | int
    channel: str
    triage_channel: str | None = None
    notifs_enabled: bool
    notification_mode: str = "channel"  # "channel" or "dm"
    admin_fallback_enabled: bool = True

    # Heuristic Engine Config
    persona_keywords: str | None = None
    sla_threshold_hours: int | None = None


class SetupChannelPayload(BaseModel):
    portal_id: str | int


@router.post("/save")
async def save_settings(
    payload: SettingsPayload,
    corr_id: str = Depends(get_corr_id),
    storage: StorageService = Depends(get_storage_service),
) -> dict:
    """Save Slack connector settings for a workspace.

    Accepts the HubSpot portal_id (available in UI Extension context) and
    resolves the internal workspace_id via the HubSpot integration record.
    Settings are stored in the Slack integration's metadata field.
    """
    logger.info("Saving settings for portal_id=%s", payload.portal_id)

    workspace_id = await _resolve_workspace_id(payload.portal_id, storage)

    integration = await storage.get_integration(workspace_id, Provider.SLACK)
    if not integration:
        raise HTTPException(status_code=404, detail="Slack integration not found")

    # Validate notification_mode
    valid_modes = ("channel", "dm")
    mode = (
        payload.notification_mode
        if payload.notification_mode in valid_modes
        else "channel"
    )

    metadata = dict(integration.metadata or {})
    metadata.update(
        {
            "channel_id": payload.channel,
            "triage_channel_id": payload.triage_channel,
            "notifications_enabled": payload.notifs_enabled,
            "notification_mode": mode,
            "admin_fallback_enabled": payload.admin_fallback_enabled,
        }
    )

    # Store heuristic settings directly in metadata JSONB
    # (avoids dependency on scoring_configs table columns)
    if payload.persona_keywords is not None:
        metadata["persona_keywords"] = payload.persona_keywords
    if payload.sla_threshold_hours is not None:
        metadata["sla_threshold_hours"] = payload.sla_threshold_hours

    await storage.upsert_integration(
        {
            "id": integration.id,
            "workspace_id": workspace_id,
            "provider": Provider.SLACK,
            "credentials": integration.credentials,
            "metadata": metadata,
        }
    )

    logger.info(
        "Settings saved: channel=%s triage_channel=%s notifs=%s mode=%s",
        payload.channel,
        payload.triage_channel,
        payload.notifs_enabled,
        mode,
    )
    return {"success": True}


@router.get("/load")
async def load_settings(
    portal_id: str,
    corr_id: str = Depends(get_corr_id),
    storage: StorageService = Depends(get_storage_service),
) -> dict:
    """Load Slack connector settings for a workspace.

    Accepts portal_id as a query param:
    ``GET /api/hubspot/settings/load?portal_id=<id>``
    """
    logger.info("Loading settings for portal_id=%s", portal_id)

    workspace_id = await _resolve_workspace_id(portal_id, storage)

    integration = await storage.get_integration(workspace_id, Provider.SLACK)
    if not integration:
        # Fallback to creating a trial workspace if only HubSpot is connected
        # (Allows settings to be viewed even if Slack isn't fully set up)
        workspace = await storage.get_workspace(workspace_id)
        if not workspace:
            raise HTTPException(status_code=404, detail="Workspace not found")

        return {
            "channel": "",
            "notifs_enabled": True,
            "notification_mode": "channel",
            "admin_fallback_enabled": True,
            "channel_name": "",
            "plan": str(workspace.plan).upper(),
            "notification_usage": f"{workspace.notification_count_monthly} / {20 if workspace.plan != 'pro' else '∞'}",
            "total_syncs": workspace.total_sync_count or 0,
        }

    from app.domains.crm.integration_service import IntegrationService

    integ_svc = IntegrationService(corr_id, storage=storage)

    workspace = await storage.get_workspace(workspace_id)
    metadata = integration.metadata or {}

    # 2026.03 Identity Bridge enrichment: Fetch the Slack Team Name from the parent identity
    workspace_name = metadata.get("slack_team_name")
    if not workspace_name:
        try:
            # Try to resolve via identity bridge
            hubspot = await integ_svc.resolve_hubspot_integration(workspace_id)
            if hubspot:
                # Get the Slack integration linked to this HubSpot portal
                parent_slack = await integ_svc.get_integration_by_slack_team_id(
                    hubspot.metadata.get("slack_team_id") or ""
                )
                if parent_slack:
                    workspace_name = parent_slack.metadata.get("slack_team_name")
        except Exception:
            pass

    plan_name = str(workspace.plan).upper() if workspace else "TRIAL"
    limit = 20 if (not workspace or workspace.plan != "pro") else "∞"
    usage = f"{workspace.notification_count_monthly if workspace else 0} / {limit}"
    total_syncs = workspace.total_sync_count if workspace else 0

    response = {
        "channel": metadata.get("channel_id", ""),
        "triage_channel": metadata.get("triage_channel_id", ""),
        "notifs_enabled": metadata.get("notifications_enabled", True),
        "notification_mode": metadata.get("notification_mode", "channel"),
        "admin_fallback_enabled": metadata.get("admin_fallback_enabled", True),
        "channel_name": metadata.get("notification_channel_name", ""),
        "triage_channel_name": metadata.get("triage_channel_name", ""),
        "workspace_name": workspace_name or "Connected Workspace",
        "plan": plan_name,
        "notification_usage": usage,
        "total_syncs": total_syncs,
    }

    # Load Heuristic Settings — prefer metadata JSONB, fall back to scoring_config row
    default_keywords = "vp,director,head,chief,founder,partner,lead,principal"
    default_sla = 4

    if "persona_keywords" in metadata or "sla_threshold_hours" in metadata:
        # Already stored in metadata (new path)
        response["persona_keywords"] = metadata.get(
            "persona_keywords", default_keywords
        )
        response["sla_threshold_hours"] = metadata.get(
            "sla_threshold_hours", default_sla
        )
    else:
        # Legacy: try scoring_configs table row
        try:
            h_config = await storage.ensure_scoring_config(workspace_id)
            if h_config:
                response["persona_keywords"] = h_config.persona_keywords
                response["sla_threshold_hours"] = h_config.sla_threshold_hours
            else:
                response["persona_keywords"] = default_keywords
                response["sla_threshold_hours"] = default_sla
        except Exception:
            # Column may not exist yet — return defaults gracefully
            response["persona_keywords"] = default_keywords
            response["sla_threshold_hours"] = default_sla

    return response


@router.post("/setup-channel")
async def setup_notification_channel(
    payload: SetupChannelPayload,
    corr_id: str = Depends(get_corr_id),
    storage: StorageService = Depends(get_storage_service),
) -> dict:
    """One-click setup for the #reha-connect-alerts notification channel.

    Implements the "Safe Create" pattern:
    1. Checks if ``reha-connect-alerts`` already exists in Slack.
    2. If not, creates a public channel.
    3. Invites the bot to the channel.
    4. Saves the channel_id as the default in integration metadata.
    """
    logger.info("Setting up notification channel for portal_id=%s", payload.portal_id)

    workspace_id = await _resolve_workspace_id(payload.portal_id, storage)

    try:
        from app.connectors.slack.channel_setup_service import (
            ensure_notification_channel,
        )

        result = await ensure_notification_channel(
            workspace_id=workspace_id,
            storage=storage,
            corr_id=corr_id,
        )
        return {"success": True, **result}
    except RuntimeError as exc:
        logger.error("Channel setup failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
