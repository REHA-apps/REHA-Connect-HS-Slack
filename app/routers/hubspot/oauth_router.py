# app/api/hubspot/oauth_router.py  # noqa: D100
# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

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
from app.domains.common.audit_service import AuditService
from app.domains.crm.integration_service import IntegrationService
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils.constants import ErrorCode
from app.utils.ui import render_success_page

router = APIRouter(prefix="/hubspot/oauth", tags=["hubspot-oauth"])
logger = get_logger("hubspot.oauth")


@router.get("/user-auth")
async def hubspot_user_auth(
    request: Request,
    workspace_id: str,
    slack_user_id: str,
    corr_id: str = Depends(get_corr_id),
) -> RedirectResponse:
    """Initiates the HubSpot OAuth flow for an individual Slack user.

    Used for authenticated link unfurling where each user must personally
    authorize HubSpot to reveal record details.
    """
    logger.info(
        "Initiating user-level HubSpot auth for user=%s in workspace=%s",
        slack_user_id,
        workspace_id,
    )

    # Encode user context into a robust dictionary-based state
    context = {"workspace_id": workspace_id, "slack_user_id": slack_user_id}
    context_str = encode_state_context(context)
    signed_state = generate_and_store_state(request, Provider.HUBSPOT, context_str)

    from app.utils.oauth import build_hubspot_oauth_url

    return RedirectResponse(url=build_hubspot_oauth_url(signed_state))


@router.get("/callback")
async def hubspot_oauth_callback(  # noqa: PLR0912, PLR0915
    request: Request,
    background_tasks: BackgroundTasks,
    code: str = Query(...),
    state: str | None = Query(default=None),
    corr_id: str = Depends(get_corr_id),
    integration_service: IntegrationService = Depends(get_integration_service),
) -> Any:
    logger.info("Received HubSpot OAuth callback code=%s state=%s", code, state)

    # 1. CSRF Protection: Verify signed state
    workspace_context_raw = None
    slack_user_id = None
    target_workspace_id = None
    return_url = None

    if state:
        workspace_context_raw = verify_and_clear_state(request, Provider.HUBSPOT, state)
        if workspace_context_raw is None:
            logger.warning("Invalid or expired state: %s", state)
            raise HTTPException(
                status_code=400,
                detail="Security error: Invalid or expired state",
            )

        # 2. Extract context (Robust decoding)
        context = decode_state_context(workspace_context_raw)
        if context:
            target_workspace_id = context.get("workspace_id")
            slack_user_id = context.get("slack_user_id")
            return_url = context.get("return_url")
            install_source = context.get("source")
            logger.info(
                "Decoded Context: workspace=%s user=%s has_return_url=%s source=%s",
                target_workspace_id,
                slack_user_id,
                bool(return_url),
                install_source,
            )
        # Fallback for legacy simple string state
        elif workspace_context_raw.startswith("user_auth:"):
            try:
                parts = workspace_context_raw.split(":")
                target_workspace_id = parts[1]
                slack_user_id = parts[2]
            except Exception:
                logger.error("Malformatted legacy context: %s", workspace_context_raw)
        else:
            target_workspace_id = workspace_context_raw

    error = request.query_params.get("error")
    if error:
        logger.warning("HubSpot OAuth error=%s", error)
        raise HTTPException(status_code=400, detail=f"HubSpot OAuth error: {error}")

    try:
        # Resolve identity and save tokens
        final_workspace_id = await integration_service.handle_hubspot_oauth_callback(
            code=code,
            state=target_workspace_id or state,
            slack_user_id=slack_user_id,
        )

        # 2b. Security Audit Log (Synchronous for reliable compliance on Lambda)
        audit = AuditService(corr_id=corr_id)
        await audit.log_action(
            action="hubspot_install",
            workspace_id=final_workspace_id,
            request=request,
            actor_id=slack_user_id or "system",
            metadata={
                "source": "oauth_callback",
                "portal_id": target_workspace_id or "unknown",
            },
        )

        # 3. Background Celebration DM (Dual-Confirmation)
        if slack_user_id:
            slack_integration = await integration_service.get_integration(
                final_workspace_id, Provider.SLACK
            )
            if slack_integration:
                from app.utils.sqs_helpers import publish_to_sqs

                published = publish_to_sqs(
                    queue_url=settings.SQS_SLACK_WEBHOOK_QUEUE_URL,
                    workspace_id=final_workspace_id,
                    corr_id=corr_id,
                    task_type="slack_celebration_dm",
                    payload={
                        "slack_user_id": slack_user_id,
                    },
                )
                if not published:
                    messaging_service = SlackMessagingService(
                        corr_id=corr_id,
                        integration_service=integration_service,
                        slack_integration=slack_integration,
                    )
                    logger.info("Queueing Celebration DM for user %s", slack_user_id)
                    background_tasks.add_task(
                        messaging_service.send_celebration_dm,
                        slack_user_id=slack_user_id,
                    )

        # Upgrade generic fallback return_url now that we know the real portal_id.
        # final_workspace_id is formatted as 'hs_<portal_id>' for portal-isolated workspaces.
        if return_url and "ecosystem/marketplace/apps" in return_url:
            portal_id_from_ws = final_workspace_id.removeprefix("hs_")
            if portal_id_from_ws and portal_id_from_ws != final_workspace_id:
                return_url = (
                    f"https://app.hubspot.com/integrations-settings/{portal_id_from_ws}"
                )
                logger.info(
                    "Upgraded generic return_url to portal-specific: %s", return_url
                )

        # 4. Success Response / Redirection
        # Bridge to Slack if missing (Cross-Provider Onboarding)
        slack_integration = await integration_service.get_integration(
            final_workspace_id, Provider.SLACK
        )
        if not slack_integration:
            logger.info(
                "Slack missing for workspace %s; rendering bridge", final_workspace_id
            )
            from app.utils.oauth import build_slack_install_url

            # Preserve context (return_url) through the Slack flow
            bridge_context = {"workspace_id": final_workspace_id}
            if return_url:
                bridge_context["return_url"] = return_url

            signed_state = generate_and_store_state(
                request, Provider.SLACK, encode_state_context(bridge_context)
            )
            slack_install_url = build_slack_install_url(signed_state)

            return RedirectResponse(url=slack_install_url)

        if return_url:
            logger.info("Marketplace install detected; redirecting to return_url")
            return RedirectResponse(url=return_url)

        # For manual/handshake flows, show the upgraded Success Page with Deep Link
        slack_team_id = (
            slack_integration.metadata.get("slack_team_id")
            if slack_integration
            else None
        )

        open_in_slack_url = None
        if slack_team_id and settings.SLACK_APP_ID:
            open_in_slack_url = f"https://slack.com/app_redirect?app={settings.SLACK_APP_ID}&team={slack_team_id}"

        if open_in_slack_url:
            logger.info("Directly redirecting to Slack: %s", open_in_slack_url)
            return RedirectResponse(url=open_in_slack_url)

        return render_success_page(
            title="Connection Successful",
            message=(
                f"Individual HubSpot account link for Slack user {slack_user_id} active."
                if slack_user_id
                else "HubSpot has been linked successfully. "
                "Your cross-platform CRM integration is now active."
            ),
            workspace_id=final_workspace_id,
            open_in_slack_url=open_in_slack_url,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("HubSpot OAuth callback failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=ErrorCode.INTERNAL_ERROR, detail="HubSpot OAuth failed"
        ) from exc
