from __future__ import annotations

import inspect
from abc import ABC
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.db.records import IntegrationRecord
from app.domains.common.sdk.context import UnifiedContext
from app.domains.common.sdk.ui_adapter import UIAdapter

if TYPE_CHECKING:
    from app.domains.ai.service import AIService
    from app.domains.crm.base import BaseCRMService
    from app.domains.crm.integration_service import IntegrationService

logger = get_logger("sdk.handler")


class BaseInteractionHandler(ABC):
    """Platform-agnostic base for handling CRM interactions.

    Orchestrates data retrieval and AI analysis while delegating
    UI rendering to a provider-specific UIAdapter.
    """

    def __init__(
        self,
        corr_id: str,
        crm: BaseCRMService,
        ai: AIService,
        integration_service: IntegrationService,
        ui: UIAdapter,
    ):
        self.corr_id = corr_id
        self.crm = crm
        self.ai = ai
        self.integration_service = integration_service
        self.ui = ui

        # Automatically register methods decorated with @interaction_handler
        self._action_routes: dict[str, Callable[..., Any]] = {}
        for _, method in inspect.getmembers(self, inspect.ismethod):
            actions = getattr(method, "__interaction_actions__", [])
            for action in actions:
                self._action_routes[action] = method

    async def handle_interaction(
        self,
        context: UnifiedContext,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        **kwargs: Any,
    ) -> Any:
        """Dispatches an interaction to the appropriate registered method."""
        action_id = context.action_id
        if not action_id:
            # Fallback for platform-specific callback IDs (e.g. Slack modals)
            action_id = payload.get("view", {}).get("callback_id")

        if not action_id:
            logger.warning("No action identifier found for routing")
            return None

        for registered_action, method in self._action_routes.items():
            if action_id == registered_action or action_id.startswith(
                f"{registered_action}:"
            ):
                logger.debug(
                    "SDK: Routed action_id=%s to %s",
                    action_id,
                    method.__name__,
                )
                return await method(
                    context=context,
                    payload=payload,
                    integration=integration,
                    **kwargs,
                )

        logger.warning(
            "SDK: No route found for action_id=%s. Registered: %s",
            action_id,
            list(self._action_routes.keys()),
        )
        return None
