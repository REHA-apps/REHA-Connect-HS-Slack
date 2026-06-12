# app/core/dependencies.py
"""Per-request dependency factories for FastAPI's Depends() system.

FastAPI caches Depends() results per-request, so calling get_storage_service
from multiple services in the same handler returns the SAME instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, HTTPException, Query

from app.core.logging import get_corr_id
from app.db.records import IntegrationRecord, Provider
from app.db.storage_service import StorageService

if TYPE_CHECKING:
    from app.domains.ai.service import AIService
    from app.domains.crm.hubspot.service import HubSpotService
    from app.domains.crm.integration_service import IntegrationService
    from app.domains.crm.service import CRMService
    from app.domains.messaging.base import MessagingService


# Request-safe singleton for StorageService.
# Correlation IDs are tracked via ContextVars (corr_id_ctx), so one instance
# can safely serve concurrent requests with their own tracing context.
_STORAGE_SVC_SINGLETON: StorageService | None = None


def get_storage_service() -> StorageService:
    """Provides a singleton StorageService instance reused across warm starts.

    Note: Correlation IDs are handled via ContextVars, so the singleton
    remains safe for concurrent requests.
    """
    global _STORAGE_SVC_SINGLETON
    if _STORAGE_SVC_SINGLETON is None:
        _STORAGE_SVC_SINGLETON = StorageService()
    return _STORAGE_SVC_SINGLETON


def get_ai_service(
    corr_id: str = Depends(get_corr_id),
) -> AIService:
    """One AIService per request."""
    from app.domains.ai.service import AIService

    return AIService(corr_id)


def get_integration_service(
    corr_id: str = Depends(get_corr_id),
    storage: StorageService = Depends(get_storage_service),
) -> IntegrationService:
    """One IntegrationService per request, sharing StorageService."""
    from app.domains.crm.integration_service import IntegrationService

    return IntegrationService(corr_id, storage=storage)


def get_crm_service(
    corr_id: str = Depends(get_corr_id),
    storage: StorageService = Depends(get_storage_service),
) -> CRMService:
    """One CRMService per request, sharing StorageService."""
    from app.domains.crm.service import CRMService

    return CRMService(corr_id, storage=storage)


def get_hubspot_service(
    corr_id: str = Depends(get_corr_id),
    storage: StorageService = Depends(get_storage_service),
) -> HubSpotService:
    """One HubSpotService per request, sharing StorageService."""
    from app.domains.crm.hubspot.service import HubSpotService

    return HubSpotService(corr_id, storage=storage)


async def get_hubspot_integration(
    portal_id: str = Query(..., alias="portalId"),
    integration_service: IntegrationService = Depends(get_integration_service),
) -> IntegrationRecord:
    """Resolves a HubSpot portalId to a full IntegrationRecord and caches it.

    FastAPI caches Depends() results per-request, so this fetch only happens ONCE
    even if multiple dependencies (like get_workspace_id and
    get_slack_messaging_service) require it.

    Args:
        portal_id: The HubSpot portal identifier.
        integration_service: Per-request integration service.

    Returns:
        The verified HubSpot IntegrationRecord.

    Raises:
        HTTPException 404: No integration found for this portal.

    """
    integration = await integration_service.storage.get_integration_by_portal_id(
        portal_id
    )
    if not integration:
        raise HTTPException(
            status_code=404,
            detail=f"No integration found for HubSpot portal {portal_id}. "
            "Please ensure the app is installed and authorised.",
        )
    return integration


async def get_workspace_id(
    hubspot_integration: IntegrationRecord = Depends(get_hubspot_integration),
) -> str:
    """Returns the internal workspace_id from the cached HubSpot integration.

    Args:
        hubspot_integration: Resolved HubSpot integration record.

    Returns:
        The internal workspace identifier.

    """
    return hubspot_integration.workspace_id


async def get_slack_messaging_service(
    corr_id: str = Depends(get_corr_id),
    workspace_id: str = Depends(get_workspace_id),
    integration_service: IntegrationService = Depends(get_integration_service),
) -> MessagingService:
    """Build the Slack MessagingService for the requesting workspace.

    Looks up the registered Slack connector and instantiates its
    channel_service with the workspace's Slack integration credentials.

    Args:
        corr_id: Request correlation ID for structured logging.
        workspace_id: Internal workspace identifier (resolved from portalId).
        integration_service: Per-request integration service.

    Returns:
        A ready-to-use MessagingService backed by Slack.

    Raises:
        HTTPException 404: Slack integration not found for this workspace.
        HTTPException 500: Slack connector not registered.

    """
    from app.connectors.registry import registry  # deferred to avoid circular import

    slack_integration = await integration_service.get_integration(
        workspace_id=workspace_id,
        provider=Provider.SLACK,
    )
    if not slack_integration:
        raise HTTPException(
            status_code=404,
            detail="Slack integration not found for this workspace.",
        )

    manifest = registry.get_connector("slack")
    if not manifest or not manifest.channel_service:
        raise HTTPException(
            status_code=500,
            detail="Slack messaging service is not registered.",
        )

    return cast(Any, manifest.channel_service)(
        corr_id=corr_id,
        integration_service=integration_service,
        slack_integration=slack_integration,
    )
