"""Stripe Billing Router — PARKED / NOT IN USE
============================================
This file has been moved outside the `app/` directory because the app
is transitioning to HubSpot Marketplace Billing for monetization.

It is kept here for reference. Do NOT import this into the FastAPI app.

To re-enable Stripe (e.g. as a secondary billing path):
  1. Move back to: app/connectors/hubspot_slack/routers/hubspot/billing_router.py
  2. Re-register in: app/connectors/__init__.py under hs_billing
  3. Ensure STRIPE_SECRET_KEY, STRIPE_PRO_PRICE_ID, STRIPE_WEBHOOK_SECRET
     are set in .env

Required scopes: None (Stripe is external)
Required env vars:
  - STRIPE_SECRET_KEY
  - STRIPE_PRO_PRICE_ID
  - STRIPE_WEBHOOK_SECRET
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse

import stripe
from app.core.config import settings
from app.core.dependencies import get_storage_service
from app.core.logging import get_corr_id, get_logger
from app.db.records import PlanTier
from app.db.storage_service import StorageService
from stripe import error

logger = get_logger("hubspot.billing")
router = APIRouter(prefix="/billing", tags=["hubspot-billing"])

# Initialize Stripe
stripe.api_key = settings.STRIPE_SECRET_KEY.get_secret_value()


@router.get("/hubspot/trial")
async def hubspot_trial(
    portal_id: str,
    state: str,
    storage: StorageService = Depends(get_storage_service),
    corr_id: str = Depends(get_corr_id),
):
    """Handle 7-day trial selection.

    Creates a placeholder workspace record with trial status
    and redirects to HubSpot OAuth.
    """
    logger.info("Processing 7-day trial signup for portal_id=%s", portal_id)

    # Calculate trial end date (7 days from now)
    trial_ends_at = datetime.now(UTC) + timedelta(days=7)

    # Pre-create/update workspace with trial info.
    # workspace_id at this stage is the 'state'
    # (temporary Slack team ID or random UUID).
    await storage.upsert_workspace(
        workspace_id=state,
        portal_id=portal_id,
        plan=PlanTier.TRIAL,
        subscription_status="trialing",
        trial_ends_at=trial_ends_at,
    )

    logger.info("Trial workspace initialized. Redirecting to HubSpot OAuth.")

    # Redirect to normal HubSpot OAuth flow
    return RedirectResponse(_hubspot_oauth_url(state))


@router.get("/checkout")
async def create_checkout_session(
    portal_id: str,
    state: str,
    email: str,
    corr_id: str = Depends(get_corr_id),
):
    """Create a Stripe Checkout Session for the Pro plan."""
    logger.info("Creating Stripe Checkout session for portal_id=%s", portal_id)

    try:
        session: Any = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[
                {
                    "price": settings.STRIPE_PRO_PRICE_ID,
                    "quantity": 1,
                }
            ],
            customer_email=email,
            automatic_tax={
                "enabled": True,
            },
            tax_id_collection={
                "enabled": True,
            },
            success_url=f"{settings.API_BASE_URL}/api/billing/checkout-success?portal_id={portal_id}&state={state}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"https://rehaapps.se/pricing?portal_id={portal_id}&state={state}&canceled=true",
            metadata={"portal_id": portal_id, "state": state},
        )
        if not session.url:
            raise ValueError("Stripe session URL is missing")
        return RedirectResponse(session.url)
    except Exception as e:
        logger.error("Failed to create Stripe session: %s", e)
        raise HTTPException(status_code=500, detail="Billing system unavailable")


@router.get("/checkout-success")
async def checkout_success(
    portal_id: str,
    state: str,
    session_id: str,
    storage: StorageService = Depends(get_storage_service),
    corr_id: str = Depends(get_corr_id),
):
    """Called by Stripe after successful payment.
    Updates workspace records and redirects to HubSpot OAuth.
    """
    logger.info("Checkout success for session=%s", session_id)

    return RedirectResponse(_hubspot_oauth_url(state))


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="stripe-signature"),
    storage: StorageService = Depends(get_storage_service),
    corr_id: str = Depends(get_corr_id),
):
    """Handle Stripe webhooks to keep subscription status in sync."""
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.STRIPE_WEBHOOK_SECRET.get_secret_value()
        )
    except error.SignatureVerificationError as e:
        logger.error("Webhook signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature")

    if not await storage.store_stripe_event(event.id):
        return {"status": "ignored"}

    if event.type == "checkout.session.completed":
        session = event["data"]["object"]

        if session["mode"] == "subscription":
            workspace_id = session["metadata"]["workspace_id"]
            portal_id = session["metadata"]["portal_id"]

            await storage.upsert_workspace(
                workspace_id=workspace_id,
                portal_id=portal_id,
                plan=PlanTier.PRO,
                subscription_status="active",
                stripe_customer_id=str(session["customer"]),
                subscription_id=str(session["subscription"]),
            )

            logger.info("Activated PRO for workspace=%s", workspace_id)
    # Handle the event
    elif event.type == "customer.subscription.updated":
        subscription = event["data"]["object"]
        customer_id = str(subscription["customer"])
        status = str(subscription["status"])  # active, past_due, canceled, trialing

        workspace = await storage.get_workspace_by_stripe_customer_id(customer_id)
        if workspace:
            await storage.upsert_workspace(
                workspace_id=workspace.id, subscription_status=status
            )
            logger.info(
                "Synced subscription status for workspace_id=%s status=%s",
                workspace.id,
                status,
            )

    elif event.type == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = str(subscription["customer"])

        workspace = await storage.get_workspace_by_stripe_customer_id(customer_id)
        if workspace:
            await storage.upsert_workspace(
                workspace_id=workspace.id,
                plan=PlanTier.FREE,
                subscription_status="canceled",
            )
            logger.info(
                "Downgraded workspace_id=%s to free due to subscription deletion",
                workspace.id,
            )

    return {"status": "success"}


def _hubspot_oauth_url(state: str) -> str:
    return (
        "https://app.hubspot.com/oauth/authorize"
        f"?client_id={settings.HUBSPOT_CLIENT_ID}"
        f"&redirect_uri={settings.HUBSPOT_REDIRECT_URI}"
        f"&scope={settings.HUBSPOT_SCOPES_ENCODED}"
        f"&state={state}"
    )
