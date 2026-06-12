# app/api/slack/oauth_router.py  # noqa: D100
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse  # noqa: F401

from app.connectors.common.registry import ChannelRegistry
from app.core.config import settings
from app.core.dependencies import get_integration_service
from app.core.logging import get_corr_id, get_logger
from app.core.security.state_validator import (
    decode_state_context,
    encode_state_context,
    generate_and_store_state,
    verify_and_clear_state,
)
from app.db.records import Provider
from app.domains.crm.integration_service import IntegrationService
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils.constants import ErrorCode
from app.utils.oauth import build_hubspot_oauth_url
from app.utils.ui import render_success_page

router = APIRouter(prefix="/slack/oauth", tags=["slack-oauth"])

logger = get_logger("slack.oauth")


@router.get("/callback")
async def slack_oauth_callback(  # noqa: PLR0912, PLR0915
    request: Request,
    code: str = Query(...),
    state: str | None = Query(default=None),
    corr_id: str = Depends(get_corr_id),
    integration_service: IntegrationService = Depends(get_integration_service),
) -> Any:
    """Slack OAuth callback."""
    logger.info("Received Slack OAuth callback code=%s state=%s", code, state)

    # 1. CSRF Protection: Verify signed state
    if not state:
        logger.warning("Missing state in OAuth callback")
        raise HTTPException(status_code=400, detail="Security error: Missing state")

    workspace_context_raw = verify_and_clear_state(request, Provider.SLACK, state)
    if workspace_context_raw is None:
        logger.warning("Invalid or expired state: %s", state)
        raise HTTPException(
            status_code=400,
            detail="Security error: Invalid or expired state",
        )

    # 2. Extract context (Decodes JSON state if present)
    target_workspace_id = None
    return_url = None

    context = decode_state_context(workspace_context_raw)
    if context:
        target_workspace_id = context.get("workspace_id")
        return_url = context.get("return_url")
    else:
        # Fallback for simple string state
        target_workspace_id = workspace_context_raw

    # 2. handle Slack OAuth errors
    error = request.query_params.get("error")
    if error:
        logger.warning("Slack OAuth error=%s", error)
        raise HTTPException(status_code=400, detail=f"Slack OAuth error: {error}")

    try:
        # Get Slack Channel and exchange code for token
        slack_channel = ChannelRegistry.get_channel(Provider.SLACK, corr_id=corr_id)
        token = await slack_channel.exchange_token(code)

        team_id = token.team_id
        if not team_id:
            raise ValueError("Slack token missing team_id")

        team_name = token.raw.get("team", {}).get("name", "")

        credentials = {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at,
        }
        metadata = {
            "slack_team_id": team_id,
            "slack_team_name": team_name,
            "authed_user_id": token.raw.get("authed_user", {}).get("id"),
        }

        # Safe diagnostic trace (Identity Bridge Hardening)
        token_prefix = str(token.access_token)[:4] if token.access_token else "NONE"
        logger.info(
            "Slack OAuth Handshake: team_id=%s, token_prefix=%s, has_refresh=%s",
            team_id,
            token_prefix,
            bool(token.refresh_token),
        )

        workspace_id = await integration_service.register_integration(
            provider=Provider.SLACK,
            platform_id=team_id,
            credentials=credentials,
            metadata=metadata,
            state=target_workspace_id,
        )

        slack_integration = await integration_service.get_integration(
            workspace_id, Provider.SLACK
        )

        # Bridge to HubSpot if missing (Smart Sibling-Aware Check)
        is_connected = await integration_service.is_hubspot_connected_anywhere(
            workspace_id
        )  # noqa: E501
        if not is_connected:
            installer_id = metadata.get("authed_user_id")
            logger.info(
                "Sending proactive welcome message to Slack installer=%s", installer_id
            )  # noqa: E501
            try:
                messaging_service = SlackMessagingService(  # noqa: F841
                    corr_id=corr_id,
                    integration_service=integration_service,
                    slack_integration=slack_integration,
                )

                # Success-First Onboarding: We no longer send the welcome message here.
                # It will be sent as a confirmation once HubSpot is also connected.
                # resp = await messaging_service.send_welcome_message(
                #    workspace_id=workspace_id, channel=installer_id
                # )
                # if resp and resp.get("ok"):
                #     ... (persistence logic)
                pass

            except Exception as e:
                logger.error(
                    "Failed to prepare onboarding context: %s", e, exc_info=True
                )  # noqa: E501

            logger.info("Rendering HubSpot connection bridge page")
            # Preserve return_url through the HubSpot bridge so the
            # marketplace redirect destination is not lost.
            bridge_context: dict[str, str] = {"workspace_id": workspace_id}
            if return_url:
                bridge_context["return_url"] = return_url
            signed_state = generate_and_store_state(
                request, Provider.HUBSPOT, encode_state_context(bridge_context)
            )

            oauth_url = build_hubspot_oauth_url(signed_state)

            from fastapi.responses import RedirectResponse

            return RedirectResponse(url=oauth_url)

        # If we reach here, BOTH platforms are connected (HubSpot was installed first).
        # We must send the final success welcome message now.
        installer_id = metadata.get("authed_user_id")
        if installer_id:
            logger.info(
                "HubSpot already connected. Sending final welcome message to Slack installer=%s",
                installer_id,
            )
            try:
                messaging_service = SlackMessagingService(
                    corr_id=corr_id,
                    integration_service=integration_service,
                    slack_integration=slack_integration,
                )
                await messaging_service.send_welcome_message(
                    workspace_id=workspace_id,
                    channel=installer_id,
                    is_update=True,
                )
            except Exception as e:
                logger.warning(
                    "Failed to deliver success welcome message during Slack auth: %s", e
                )

        open_in_slack_url = None
        if team_id and settings.SLACK_APP_ID:
            open_in_slack_url = f"https://slack.com/app_redirect?app={settings.SLACK_APP_ID}&team={team_id}"

        from fastapi.responses import RedirectResponse

        if return_url:
            logger.info("Directly redirecting to return_url: %s", return_url)
            return RedirectResponse(url=return_url)

        if open_in_slack_url:
            logger.info("Directly redirecting back to Slack: %s", open_in_slack_url)
            return RedirectResponse(url=open_in_slack_url)

        return render_success_page(
            title="Connection Successful",
            message=(
                "Slack has been linked successfully. "
                "Your cross-platform CRM integration is now active."
            ),
            workspace_id=workspace_id,
            open_in_slack_url=open_in_slack_url,
        )

    except HTTPException:
        raise

    except Exception as exc:
        logger.error("Slack OAuth callback failed: %s", exc, exc_info=True)
        
        if isinstance(exc, ValueError) and "Portal Conflict" in str(exc):
            from app.utils.ui import render_error_page
            return render_error_page(
                title="Connection Blocked",
                message=str(exc)
            )
            
        raise HTTPException(
            status_code=ErrorCode.INTERNAL_ERROR, detail="Slack OAuth failed"
        ) from exc
