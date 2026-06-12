"""Interaction handlers for Scheduled Digests."""

from __future__ import annotations

from typing import Any

from app.connectors.slack.services.handlers.base import (
    InteractionHandler,
    UnifiedContext,
    interaction_handler,
)
from app.core.logging import get_logger
from app.domains.crm.ui.mixins.digest_modals import DigestModalsMixin

logger = get_logger(__name__)


class DigestHandlers(InteractionHandler, DigestModalsMixin):
    """Handlers for digest-related actions."""

    @interaction_handler("submit_create_digest")
    async def _handle_submit_create_digest(
        self,
        *,
        payload: dict[str, Any],
        integration: Any,
        messaging_service: Any,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Processes the creation of a new scheduled digest."""
        from app.db.storage_service import StorageService

        storage = StorageService(corr_id=self.corr_id)

        hubspot_integration = (
            await self.integration_service.resolve_hubspot_integration(
                integration.workspace_id
            )
        )
        target_workspace_id = (
            hubspot_integration.workspace_id
            if hubspot_integration
            else integration.workspace_id
        )

        await self.handle_create_digest_submission(
            workspace_id=target_workspace_id, payload=payload, storage=storage
        )

    @interaction_handler("delete_scheduled_digest")
    async def _handle_delete_scheduled_digest(
        self,
        *,
        value: str,
        payload: dict[str, Any],
        messaging_service: Any,
        **kwargs: Any,
    ) -> None:
        """Processes the deletion of a scheduled digest."""
        from app.db.storage_service import StorageService

        storage = StorageService(corr_id=self.corr_id)

        digest_id = value

        if digest_id:
            await storage.delete_scheduled_digest(digest_id)
            user_id = payload.get("user", {}).get("id")
            if user_id:
                # Refresh App Home
                await messaging_service.handle_app_home_opened(user_id)
