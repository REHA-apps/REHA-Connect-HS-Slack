from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from typing import Any

from app.connectors.slack.services.handlers.action_handlers import (
    ActionButtonHandler,
)
from app.connectors.slack.services.handlers.base import InteractionHandler
from app.connectors.slack.services.handlers.core_modal_handlers import CoreModalHandlers
from app.connectors.slack.services.handlers.deal_modal_handlers import DealModalHandlers
from app.connectors.slack.services.handlers.meeting_handlers import MeetingHandlers
from app.connectors.slack.services.handlers.note_handlers import NoteHandlers
from app.connectors.slack.services.handlers.object_handlers import (
    ObjectViewHandler,
)
from app.connectors.slack.services.handlers.recap_handlers import RecapHandlers
from app.connectors.slack.services.handlers.support_handlers import (
    SupportHandlers,
)
from app.connectors.slack.services.handlers.task_handlers import TaskHandlers
from app.connectors.slack.services.slack_ui_adapter import SlackUIAdapter
from app.domains.ai.service import AIService
from app.domains.crm.integration_service import IntegrationService


class InteractionRegistry:
    """Central registry to route Slack interactions to their specific handlers."""

    def __init__(
        self,
        corr_id: str,
        crm: Any,
        ai: AIService,
        integration_service: IntegrationService,
    ):
        self.corr_id = corr_id

        # SDK UI Adapter
        self.ui = SlackUIAdapter(integration_service=integration_service)

        # Initialize handlers with SDK injection
        args = (corr_id, crm, ai, integration_service, self.ui)
        self.object_view = ObjectViewHandler(*args)
        self.action_button = ActionButtonHandler(*args)

        self.notes = NoteHandlers(*args)
        self.tasks = TaskHandlers(*args)
        self.deals = DealModalHandlers(*args)
        self.meetings = MeetingHandlers(*args)
        self.recaps = RecapHandlers(*args)
        self.support = SupportHandlers(*args)
        self.core_modals = CoreModalHandlers(*args)

        from app.connectors.slack.services.handlers.digest_handlers import (
            DigestHandlers,
        )
        from app.connectors.slack.services.handlers.ticket_handlers import (
            TicketHandlers,
        )

        self.digests = DigestHandlers(*args)
        self.tickets = TicketHandlers(*args)

        self._all_handlers = [
            self.object_view,
            self.action_button,
            self.notes,
            self.tasks,
            self.deals,
            self.meetings,
            self.recaps,
            self.support,
            self.core_modals,
            self.digests,
            self.tickets,
        ]

    def get_handler(
        self, payload: Mapping[str, Any], action_id: str | None = None
    ) -> InteractionHandler | None:
        """Determines the appropriate handler for a given payload.

        Uses the SDK's auto-registration to resolve which handler owns a specific
        action_id or callback_id, eliminating the need for manual dispatch tables.
        """
        # Dynamic action-id routing (Block Actions, View Submissions, Shortcuts)
        if action_id:
            # Check both the full action_id and the prefix (colon-separated)
            prefix = action_id.split(":")[0]
            for handler in self._all_handlers:
                if any(
                    registered == action_id or registered == prefix
                    for registered in handler._action_routes
                ):
                    return handler

        return None
