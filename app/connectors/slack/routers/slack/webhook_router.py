# app/api/slack/webhook_router.py  # noqa: D100
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.connectors.slack.services.command_service import CommandService
from app.core.dependencies import get_integration_service
from app.core.logging import get_corr_id, get_logger
from app.core.security.slack_signature import verify_slack_signature
from app.domains.crm.integration_service import IntegrationService

router = APIRouter(prefix="/slack", tags=["slack-webhooks"])
logger = get_logger("slack.webhooks")


@router.post("/commands")
async def slack_commands(
    request: Request,
    background_tasks: BackgroundTasks,
    corr_id: str = Depends(get_corr_id),
    integration_service: IntegrationService = Depends(get_integration_service),
) -> dict[str, Any]:
    logger.info("Received Slack command request: %s", corr_id)
    body = await request.body()
    await verify_slack_signature(request.headers, body, corr_id=corr_id)

    form = await request.form()
    command = str(form.get("command", "")).strip()
    text = str(form.get("text", "")).strip()
    team_id = str(form.get("team_id", ""))
    response_url = str(form.get("response_url", ""))
    channel_id = str(form.get("channel_id", "")).strip()
    user_id = str(form.get("user_id", "")).strip()

    if not command:
        return {"text": "Unknown command."}

    # Fetch integration ONCE (integration_service injected via Depends)
    integration = await integration_service.get_integration_by_slack_team_id(team_id)

    if not integration:
        logger.warning(
            "No integration found for team_id=%s — returning friendly response", team_id
        )
        return {
            "response_type": "ephemeral",
            "text": (
                "⚠️ *REHA Connect is not fully set up yet.*\n"
                "If you just connected your HubSpot portal, please wait a moment and try again. "
                "If the issue persists, reinstall the app from the HubSpot App Marketplace."
            ),
        }
    command_service = CommandService(corr_id, integration=integration)

    try:
        result = await command_service.handle_slack_command(
            command=command,
            text=text,
            workspace_id=integration.workspace_id,
            response_url=response_url,
            channel_id=channel_id,
            user_id=user_id,
            background_tasks=background_tasks,
        )
        return result or {"text": "Command executed."}

    except Exception as exc:
        logger.error("Slack command failed: %s", exc)
        raise HTTPException(status_code=500, detail="Command failed")
