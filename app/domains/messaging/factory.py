from __future__ import annotations  # noqa: D100

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.db.storage_service import StorageService
    from app.domains.messaging.base import MessagingService

from app.connectors.registry import registry
from app.core.logging import get_logger
from app.db.records import Provider

logger = get_logger("messaging.factory")


async def get_messaging_service(
    workspace_id: str,
    storage: StorageService,
    corr_id: str | None = None,
    integration_record: Any = None,
) -> MessagingService | None:
    """Dynamically resolves and instantiates the messaging service for a workspace.

    Args:
        workspace_id: The identifier for the workspace.
        storage: Storage service for database lookups.
        corr_id: Optional correlation ID for tracing.
        integration_record: Optional explicit integration record to use
            (e.g., for territory-based mapping).

    Returns:
        The instantiated MessagingService or None.

    """
    target_integration = integration_record
    active_provider: Provider | None = (
        Provider(target_integration.provider) if target_integration else None
    )

    if not target_integration:
        integrations = await storage.list_integrations(workspace_id)
        # Priority order: Slack > Teams > WhatsApp
        priority = [Provider.SLACK, Provider.TEAMS, Provider.WHATSAPP]

        for p in priority:
            match = next((i for i in integrations if i.provider == p), None)
            if match:
                active_provider = p
                target_integration = match
                break

    if not active_provider or not target_integration:
        logger.warning("No messaging provider found for workspace_id=%s", workspace_id)
        return None

    # Resolve manifest
    manifest = registry.get_connector(active_provider.value)
    if not manifest or not manifest.channel_service:
        logger.error(
            "Connector manifest or channel_service missing for provider=%s",
            active_provider,
        )
        return None

    # Instantiate the service
    # Note: We assume the service constructor follows the standard pattern:
    # (corr_id, integration_service, integration_record)
    from app.domains.crm.integration_service import IntegrationService

    integration_service = IntegrationService(corr_id=corr_id, storage=storage)

    try:
        service_cls = manifest.channel_service
        return service_cls(
            corr_id=corr_id,
            integration_service=integration_service,
            **{f"{active_provider.value}_integration": target_integration},
        )
    except Exception as e:
        logger.error(
            "Failed to instantiate messaging service for %s: %s",
            active_provider,
            e,
            exc_info=True,
        )
        return None
