from __future__ import annotations


def setup_connectors() -> None:
    """Register all available connectors with the unified ConnectorRegistry.

    This is the **single** registration point for all connectors. It handles:
    - Connector manifests (routers, renderer, service, channel_service)
    - Channel classes for ChannelRegistry (via provider + channel_cls fields)

    Both concerns are registered in one call per connector. Adding a new
    platform (e.g. WhatsApp) means adding one ``registry.register()`` block
    below — nothing else needs to change.

    Called once during application lifespan startup (see ``app/main.py``).
    """
    from app.connectors.registry import registry
    from app.db.records import Provider

    # -------------------------------------------------------------------------
    # HubSpot Connector
    # -------------------------------------------------------------------------
    from app.providers.hubspot.channel import HubSpotChannel
    from app.routers.hubspot.actions_router import router as hs_actions
    from app.routers.hubspot.ai_cards_router import router as hs_ai
    from app.routers.hubspot.extensions_router import router as hs_ext
    from app.routers.hubspot.install_router import router as hs_install
    from app.routers.hubspot.oauth_router import router as hs_oauth
    from app.routers.hubspot.settings_router import router as hs_settings
    from app.routers.hubspot.ui_extension_router import router as hs_ui_ext
    from app.routers.hubspot.webhook_router import router as hs_webhook
    from app.routers.hubspot.workflow_actions_router import router as hs_workflow

    registry.register(
        name="hubspot",
        provider=Provider.HUBSPOT,
        channel_cls=HubSpotChannel,
        routers=[
            hs_oauth,
            hs_ai,
            hs_actions,
            hs_ext,
            hs_webhook,
            hs_workflow,
            hs_install,
            hs_ui_ext,
            hs_settings,
        ],
    )

    # -------------------------------------------------------------------------
    # Slack Connector
    # -------------------------------------------------------------------------
    from app.connectors.slack.routers.slack.events_router import router as slack_events
    from app.connectors.slack.routers.slack.install_router import (
        router as slack_install,
    )
    from app.connectors.slack.routers.slack.interactions_router import (
        router as slack_interactions,
    )
    from app.connectors.slack.routers.slack.oauth_router import router as slack_oauth
    from app.connectors.slack.routers.slack.webhook_router import (
        router as slack_webhook,
    )
    from app.connectors.slack.services.service import InteractionService
    from app.connectors.slack.slack_channel import SlackChannel
    from app.connectors.slack.slack_renderer import SlackRenderer
    from app.domains.messaging.slack.service import SlackMessagingService

    registry.register(
        name="slack",
        provider=Provider.SLACK,
        channel_cls=SlackChannel,
        renderer=SlackRenderer,
        service=InteractionService,
        channel_service=SlackMessagingService,
        routers=[
            slack_install,
            slack_webhook,
            slack_oauth,
            slack_events,
            slack_interactions,
        ],
    )

    # -------------------------------------------------------------------------
    # Future connectors (WhatsApp, Teams, etc.)
    # -------------------------------------------------------------------------
    # registry.register(
    #     name="whatsapp",
    #     provider=Provider.WHATSAPP,
    #     channel_cls=WhatsAppChannel,
    #     routers=[wa_webhook, wa_install],
    # )
