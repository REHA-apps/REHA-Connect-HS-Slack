"""Stripe Billing Router
=====================
Handles subscription management, checkout sessions, and webhooks
for Stripe-based billing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse

import stripe
from app.core.config import settings
from app.core.dependencies import get_integration_service, get_storage_service
from app.core.logging import get_corr_id, get_logger
from app.core.security.state_validator import (
    decode_state,
    encode_state,
    generate_and_store_state,
)
from app.db.records import PlanTier, Provider
from app.db.storage_service import StorageService
from app.domains.crm.integration_service import IntegrationService
from app.utils.oauth import build_hubspot_oauth_url
from stripe import error

logger = get_logger("billing.router")
router = APIRouter(prefix="/billing", tags=["hubspot-billing"])

# Initialize Stripe
# stripe.api_key = settings.STRIPE_SECRET_KEY.get_secret_value()
stripe.api_key = settings.STRIPE_RESTRICTED_KEY.get_secret_value()


@router.get("/hubspot/trial")
async def hubspot_trial(
    request: Request,
    portal_id: str,
    state: str,
    storage: StorageService = Depends(get_storage_service),
    corr_id: str = Depends(get_corr_id),
) -> RedirectResponse:
    """Handle 7-day trial selection.

    Pre-creates a placeholder workspace record with trial status
    and redirects the user to the HubSpot OAuth authorization page.

    Args:
        request: FastAPI request object.
        portal_id: HubSpot portal ID.
        state: Opaque state string for security and tracking.
        storage: Data storage service.
        corr_id: Correlation ID for tracing.

    Returns:
        A redirect to the HubSpot OAuth URL.

    """
    logger.info("Processing 7-day trial signup for portal_id=%s", portal_id)

    # Decode IDs — they may be base64-encoded by the pricing page
    # to avoid Cloudflare WAF triggers on raw strings like hs_148238284.
    portal_id = decode_state(portal_id)
    state = decode_state(state)

    # Pre-create/update workspace with trial info.
    # workspace_id at this stage is the 'state'
    # (temporary Slack team ID or random UUID).
    await storage.start_trial_workspace(
        workspace_id=state,
        portal_id=portal_id,
    )

    logger.info("Trial workspace initialized. Redirecting to HubSpot OAuth.")

    # Redirect to normal HubSpot OAuth flow
    return RedirectResponse(_hubspot_oauth_url(request, state))


@router.get("/checkout")
async def create_checkout_session(
    request: Request,
    portal_id: str,
    state: str,
    email: str | None = None,
    storage: StorageService = Depends(get_storage_service),
    corr_id: str = Depends(get_corr_id),
) -> RedirectResponse:
    """Create a Stripe Checkout Session for the Pro plan.

    Initializes a Stripe payment session, resolving the internal
    workspace ID and checking for existing active subscriptions to
    prevent double-billing.

    Args:
        request: FastAPI request object.
        portal_id: HubSpot portal ID.
        state: Opaque state string.
        email: Optional initial customer email.
        storage: Data storage service.
        corr_id: Correlation ID for tracing.

    Returns:
        A redirect to the Stripe Checkout hosted page.

    """
    logger.info("Creating Stripe Checkout session for portal_id=%s", portal_id)

    # Decode IDs — they may be base64-encoded by the pricing page
    # to avoid Cloudflare WAF triggers on raw strings like hs_148238284.
    portal_id = decode_state(portal_id)
    state = decode_state(state)

    try:
        # Pre-resolve the workspace ID to prevent duplicate workspaces
        # when upgrading from the UI extension (where state = user_id)
        workspace_id = state
        if portal_id:
            existing = await storage.get_integration_by_portal_id(portal_id)
            if existing:
                workspace_id = existing.workspace_id
                logger.info(
                    "Resolved checkout workspace_id=%s from portal_id=%s",
                    workspace_id,
                    portal_id,
                )

                # Check for active subscription to prevent double-billing
                workspace = await storage.get_workspace(workspace_id)
                if (
                    workspace
                    and workspace.plan == PlanTier.PRO
                    and workspace.subscription_status in ("active", "trialing")
                ):
                    if workspace.subscription_id:
                        # Reactivate the subscription if it was
                        # set to cancel at period end
                        try:
                            sub = stripe.Subscription.retrieve(
                                workspace.subscription_id
                            )
                            if sub and getattr(sub, "cancel_at_period_end", False):
                                stripe.Subscription.modify(
                                    workspace.subscription_id,
                                    cancel_at_period_end=False,
                                )
                                logger.info(
                                    "Reactivated cancel_at_period_end subscription %s",
                                    workspace.subscription_id,
                                )
                        except Exception as e:
                            logger.error(
                                "Failed to check/reactivate Stripe subscription: %s", e
                            )

                    logger.info(
                        "Workspace %s already has active Pro plan, skipping checkout",
                        workspace_id,
                    )
                    return RedirectResponse(_hubspot_oauth_url(request, state))

        checkout_params: dict[str, Any] = {
            "mode": "subscription",
            "payment_method_types": ["card"],
            "line_items": [
                {
                    "price": settings.STRIPE_PRO_PRICE_ID,
                    "quantity": 1,
                }
            ],
            "automatic_tax": {
                "enabled": True,
            },
            "tax_id_collection": {
                "enabled": True,
            },
            "success_url": (
                f"{str(settings.API_BASE_URL).rstrip('/')}"
                "/api/billing/checkout-success"
                f"?portal_id={portal_id}&state={state}"
                "&session_id={CHECKOUT_SESSION_ID}"
            ),
            # Re-encode IDs for the cancel_url since it points back to the
            # external pricing page and would re-trigger Cloudflare WAF.
            "cancel_url": (
                f"{settings.PRICING_URL}"
                f"?portal_id={encode_state(portal_id)}"
                f"&state={encode_state(state)}&canceled=true"
            ),
            "metadata": {
                "portal_id": portal_id,
                # The workspace_id is strictly resolved to prevent duplicated workspaces
                "workspace_id": workspace_id,
                "state": state,
            },
        }

        if email:
            checkout_params["customer_email"] = email

        session: Any = stripe.checkout.Session.create(**checkout_params)
        if not session.url:
            raise ValueError("Stripe session URL is missing")
        return RedirectResponse(session.url)
    except Exception as e:
        logger.error("Failed to create Stripe session: %s", e)
        raise HTTPException(status_code=500, detail="Billing system unavailable")


@router.get("/checkout-success")
async def checkout_success(
    request: Request,
    portal_id: str,
    state: str,
    session_id: str,
    storage: StorageService = Depends(get_storage_service),
    corr_id: str = Depends(get_corr_id),
) -> RedirectResponse:
    """Handle post-payment redirect from Stripe.

    Updates the workspace records to reflect the successful payment
    and redirects the user back to the HubSpot OAuth flow to complete
    installation.

    Args:
        request: FastAPI request object.
        portal_id: HubSpot portal ID.
        state: Opaque state string.
        session_id: The Stripe checkout session ID.
        storage: Data storage service.
        corr_id: Correlation ID for tracing.

    Returns:
        A redirect to the HubSpot OAuth URL.

    """
    logger.info("Checkout success for session=%s", session_id)

    return RedirectResponse(_hubspot_oauth_url(request, state))


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="stripe-signature"),
    storage: StorageService = Depends(get_storage_service),
    integration_service: IntegrationService = Depends(get_integration_service),
    corr_id: str = Depends(get_corr_id),
) -> dict[str, str]:
    """Handle Stripe webhooks for subscription synchronization.

    Processes incoming Stripe events (like payment successes or
    subscription deletions) to keep internal workspace plan statuses
    accurate and up-to-date.

    Args:
        request: FastAPI request object.
        stripe_signature: The signature header from Stripe.
        storage: Data storage service.
        corr_id: Correlation ID for tracing.

    Returns:
        A JSON response indicating successful processing.

    """
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.STRIPE_WEBHOOK_SECRET.get_secret_value()
        )
    except error.SignatureVerificationError as e:
        logger.error("Webhook signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature")

    if not await storage.idempotency_svc.mark_processed(event.id, provider="stripe"):
        return {"status": "ignored"}

    if event.type == "checkout.session.completed":
        session = event["data"]["object"]

        if session["mode"] == "subscription":
            metadata = session.get("metadata", {})
            workspace_id = metadata.get("workspace_id") or metadata.get("state")
            portal_id = metadata.get("portal_id")

            if not workspace_id or not portal_id:
                logger.error(
                    "Missing portal_id or workspace_id/state in session metadata."
                )
                return {"status": "error"}

            await storage.upsert_workspace(
                workspace_id=workspace_id,
                portal_id=portal_id,
                plan=PlanTier.PRO,
                subscription_status="active",
                stripe_customer_id=str(session["customer"]),
                subscription_id=str(session["subscription"]),
            )

            # CR-26: Invalidate cache so user sees Pro features immediately
            await integration_service.invalidate_tier_cache(workspace_id)

            logger.info("Activated PRO for workspace=%s", workspace_id)
    # Handle the event
    elif event.type == "customer.subscription.updated":
        subscription = event["data"]["object"]
        customer_id = str(subscription["customer"])
        status = str(subscription["status"])  # active, past_due, canceled, trialing

        workspace = await storage.workspace_svc.get_workspace_by_stripe_customer_id(
            customer_id
        )
        if workspace:
            await storage.upsert_workspace(
                workspace_id=workspace.id, subscription_status=status
            )
            # CR-26: Sync cache
            await integration_service.invalidate_tier_cache(workspace.id)

            logger.info(
                "Synced subscription status for workspace_id=%s status=%s",
                workspace.id,
                status,
            )

    elif event.type == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = str(subscription["customer"])

        workspace = await storage.workspace_svc.get_workspace_by_stripe_customer_id(
            customer_id
        )
        if workspace:
            await storage.upsert_workspace(
                workspace_id=workspace.id,
                plan=PlanTier.FREE,
                subscription_status="canceled",
            )
            # CR-26: Sync cache
            await integration_service.invalidate_tier_cache(workspace.id)

            logger.info(
                "Downgraded workspace_id=%s to free due to subscription deletion",
                workspace.id,
            )

    return {"status": "success"}


def _hubspot_oauth_url(request: Request, state: str) -> str:
    signed_state = generate_and_store_state(request, Provider.HUBSPOT, state)
    return build_hubspot_oauth_url(signed_state)
