from fastapi import APIRouter, Request  # noqa: D100
from fastapi.responses import RedirectResponse

from app.core.security.state_validator import (
    encode_state_context,
    generate_and_store_state,
)
from app.db.records import Provider
from app.utils.oauth import build_hubspot_oauth_url

router = APIRouter(prefix="/hubspot", tags=["hubspot-install"])


@router.get("/install")
async def hubspot_install(
    request: Request,
    portalId: str | None = None,
    returnUrl: str | None = None,
    state: str | None = None,
):
    """Entry point for HubSpot Marketplace installs.

    HubSpot may or may not send ``returnUrl``. We always stamp ``source=hubspot``
    into the context so downstream OAuth callbacks know where to redirect after
    the full install chain completes.  A fallback ``return_url`` is synthesised
    from the ``portalId`` when HubSpot does not provide one explicitly.
    """
    context_data: dict[str, str] = {"source": "hubspot"}

    if portalId:
        context_data["portal_id"] = portalId

    # Use HubSpot-provided returnUrl when available; otherwise build a sensible
    # fallback so the user lands back in HubSpot after the full OAuth chain.
    if returnUrl:
        context_data["return_url"] = returnUrl
    elif portalId:
        context_data["return_url"] = (
            f"https://app.hubspot.com/integrations-settings/{portalId}"
        )
    else:
        # Generic fallback: HubSpot's global app integrations directory
        context_data["return_url"] = (
            "https://app.hubspot.com/ecosystem/marketplace/apps"
        )

    # Preserve existing state if passed (e.g., from a custom deep-link)
    if state:
        context_data["workspace_id"] = state

    context_str = encode_state_context(context_data)
    signed_state = generate_and_store_state(request, Provider.HUBSPOT, context_str)

    oauth_url = build_hubspot_oauth_url(signed_state)
    return RedirectResponse(oauth_url)
