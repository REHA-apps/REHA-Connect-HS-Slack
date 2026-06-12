# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from app.db.records import IntegrationRecord
    from app.domains.crm.integration_service import IntegrationService


from app.connectors.slack.slack_renderer import SlackRenderer
from app.core.logging import get_logger
from app.domains.ai.service import AIService
from app.domains.crm.service import CRMService
from app.domains.crm.ui import CardBuilder

logger = get_logger("slack.channel.service")

from app.domains.messaging.base import MessagingService  # noqa: E402

from .mixins.card_messaging import CardMessagingMixin  # noqa: E402
from .mixins.channel_resolution import ChannelResolutionMixin  # noqa: E402
from .mixins.core_messaging import CoreMessagingMixin  # noqa: E402
from .mixins.event_handlers import SlackEventsMixin  # noqa: E402


class SlackMessagingService(
    CoreMessagingMixin,
    CardMessagingMixin,
    ChannelResolutionMixin,
    SlackEventsMixin,
    MessagingService,
):
    """Unified Slack Messaging Service using composed mixins."""

    def __init__(
        self,
        corr_id: str | None = None,
        integration_service: IntegrationService | None = None,
        slack_integration: IntegrationRecord | None = None,
        crm: CRMService | None = None,
        ai: AIService | None = None,
        portal_id: int | None = None,
    ) -> None:
        """Initialize the SlackMessagingService."""
        self.corr_id = corr_id or "system"
        self.integration_service = integration_service  # type: ignore
        self.slack_integration = slack_integration
        self.portal_id = portal_id

        # Local per-request services
        self.crm = crm or CRMService(corr_id)
        self.ai = ai or AIService(corr_id)

        # Unified card builder
        self.cards = CardBuilder()

        # 1.1 Architecture fix: resolve renderer via ConnectorRegistry so future
        # connectors (WhatsApp, Teams) can register their own without subclassing.
        from app.connectors.registry import ConnectorRegistry

        manifest = ConnectorRegistry.get_connector("slack")
        renderer_cls = (
            manifest.renderer if manifest and manifest.renderer else SlackRenderer
        )
        self.slack_renderer = cast("SlackRenderer", renderer_cls())
