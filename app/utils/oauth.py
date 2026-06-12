from app.core.config import settings  # noqa: D100


def build_hubspot_oauth_url(state: str) -> str:
    """Centralized helper to build the HubSpot OAuth authorization URL."""
    return (
        "https://app.hubspot.com/oauth/authorize"
        f"?client_id={settings.HUBSPOT_CLIENT_ID}"
        f"&redirect_uri={settings.HUBSPOT_REDIRECT_URI}"
        f"&scope={settings.HUBSPOT_SCOPES_ENCODED}"
        f"&state={state}"
    )


def build_slack_install_url(state: str) -> str:
    """Centralized helper to build the Slack OAuth installation URL."""
    return (
        "https://slack.com/oauth/v2/authorize"
        f"?client_id={settings.SLACK_CLIENT_ID}"
        f"&scope={settings.SLACK_SCOPES_STR}"
        f"&user_scope={settings.SLACK_USER_SCOPES}"
        f"&redirect_uri={settings.SLACK_REDIRECT_URI}"
        f"&state={state}"
    )
