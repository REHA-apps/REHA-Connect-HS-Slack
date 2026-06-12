from fastapi import APIRouter, Request  # noqa: D100
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.core.security.state_validator import generate_and_store_state
from app.db.records import Provider

router = APIRouter(prefix="/slack", tags=["slack.install"])


@router.get("/install")
async def install_slack(request: Request, state: str | None = None):
    # This ensures that even if started from Slack, we can carry an
    # existing workspace_id (state) if it exists.
    signed_state = generate_and_store_state(request, Provider.SLACK, state)

    oauth_url = (
        "https://slack.com/oauth/v2/authorize"
        f"?client_id={settings.SLACK_CLIENT_ID}"
        f"&scope={settings.SLACK_SCOPES_ENCODED}"
        f"&redirect_uri={settings.SLACK_REDIRECT_URI}"
        f"&state={signed_state}"
    )
    # MUST be a 302 for Slack Marketplace "Direct Install"
    return RedirectResponse(url=oauth_url, status_code=302)
