from __future__ import annotations  # noqa: D100

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.db.records import IntegrationRecord

from app.core.logging import get_logger
from app.db.records import Provider
from app.db.storage_service import StorageService
from app.domains.crm.base import BaseCRMService
from app.domains.crm.hubspot.service import HubSpotService

logger = get_logger("crm.service")


class CRMService(BaseCRMService):
    """Orchestration layer for CRM providers.
    Routes generic CRM requests to the appropriate provider-specific service.
    """

    def __init__(
        self,
        corr_id: str | None = None,
        storage: StorageService | None = None,
        slack_ts: str | None = None,
    ) -> None:
        self.corr_id = corr_id or "system"
        self.slack_ts = slack_ts
        self.storage = storage or StorageService(corr_id=self.corr_id)
        # Initialize provider-specific services
        self.hubspot = HubSpotService(
            corr_id=corr_id, storage=self.storage, slack_ts=slack_ts
        )

    async def get_client(
        self,
        workspace_id: str,
        slack_user_id: str | None = None,
        provider: Provider = Provider.HUBSPOT,
    ) -> Any:
        service = self._resolve_provider_service(provider)
        return await service.get_client(
            workspace_id=workspace_id, slack_user_id=slack_user_id
        )

    async def get_client_from_integration(
        self,
        integration: IntegrationRecord,
        portal_id: str | None = None,
        slack_ts: str | None = None,
    ) -> Any:
        service = self._resolve_provider_service(integration.provider)
        return await service.get_client_from_integration(
            integration, portal_id=portal_id, slack_ts=slack_ts
        )

    async def get_object_with_client(
        self,
        client: Any,
        object_type: str,
        object_id: str,
        provider: Provider = Provider.HUBSPOT,
    ) -> Mapping[str, Any] | None:
        service = self._resolve_provider_service(provider)
        return await service.get_object_with_client(
            client=client, object_type=object_type, object_id=object_id
        )

    def _resolve_provider_service(self, provider: Provider) -> BaseCRMService:
        """Resolve the concrete service for a given provider."""
        if provider == Provider.HUBSPOT:
            return self.hubspot

        # Add future providers here
        raise ValueError(f"Unsupported CRM provider: {provider}")

    async def get_object(
        self,
        *,
        workspace_id: str,
        object_type: str,
        object_id: str,
        provider: Provider = Provider.HUBSPOT,  # Default for now
        ignore_cache: bool = False,
    ) -> Mapping[str, Any] | None:
        service = self._resolve_provider_service(provider)
        return await service.get_object(
            workspace_id=workspace_id,
            object_type=object_type,
            object_id=object_id,
            ignore_cache=ignore_cache,
        )

    async def search(
        self,
        *,
        workspace_id: str,
        object_type: str,
        query: str,
        provider: Provider = Provider.HUBSPOT,
    ) -> Sequence[Mapping[str, Any]]:
        service = self._resolve_provider_service(provider)
        return await service.search(
            workspace_id=workspace_id,
            object_type=object_type,
            query=query,
        )

    async def create_contact(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
        provider: Provider = Provider.HUBSPOT,
    ) -> Mapping[str, Any]:
        service = self._resolve_provider_service(provider)
        return await service.create_contact(workspace_id, properties)

    async def get_object_engagements(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        ignore_cache: bool = False,
        slack_ts: str | None = None,
        provider: Provider = Provider.HUBSPOT,
    ) -> list[dict[str, Any]]:
        service = self._resolve_provider_service(provider)
        return await service.get_object_engagements(
            workspace_id=workspace_id,
            object_type=object_type,
            object_id=object_id,
            ignore_cache=ignore_cache,
            slack_ts=slack_ts,
        )

    async def get_contact_meetings(
        self,
        workspace_id: str,
        contact_id: str,
        provider: Provider = Provider.HUBSPOT,
    ) -> list[dict[str, Any]]:
        service = self._resolve_provider_service(provider)
        return await service.get_contact_meetings(workspace_id, contact_id)

    async def get_support_client(self, provider: Provider = Provider.HUBSPOT) -> Any:
        service = self._resolve_provider_service(provider)
        return await service.get_support_client()

    async def create_task(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
        provider: Provider = Provider.HUBSPOT,
    ) -> Mapping[str, Any]:
        service = self._resolve_provider_service(provider)
        return await service.create_task(workspace_id, properties)

    async def create_note(
        self,
        *,
        workspace_id: str,
        content: str,
        associated_id: str,
        associated_type: str,
        provider: Provider = Provider.HUBSPOT,
    ) -> dict[str, Any]:
        """Create a note associated with an object."""
        service = self._resolve_provider_service(provider)
        return await service.create_note(
            workspace_id=workspace_id,
            content=content,
            associated_id=associated_id,
            associated_type=associated_type,
        )

    async def create_email_activity(
        self,
        *,
        workspace_id: str,
        html_content: str,
        subject: str = "Email Reply from REHA Connect",
        associated_id: str | None = None,
        associated_type: str | None = None,
        provider: Provider = Provider.HUBSPOT,
    ) -> dict[str, Any]:
        """Create an email engagement activity associated with an object."""
        service = self._resolve_provider_service(provider)
        if not hasattr(service, "create_email_activity"):
            raise NotImplementedError(
                f"create_email_activity not implemented for {provider}"
            )
        return await service.create_email_activity(
            workspace_id=workspace_id,
            html_content=html_content,
            subject=subject,
            associated_id=associated_id,
            associated_type=associated_type,
        )

    async def create_meeting(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
        contact_id: str | None = None,
        provider: Provider = Provider.HUBSPOT,
        **kwargs: Any,
    ) -> dict[str, Any]:
        service = self._resolve_provider_service(provider)
        return await service.create_meeting(
            workspace_id=workspace_id,
            properties=properties,
            contact_id=contact_id,
            **kwargs,
        )

    async def update_object(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        properties: Mapping[str, Any],
        provider: Provider = Provider.HUBSPOT,
    ) -> dict[str, Any]:
        service = self._resolve_provider_service(provider)
        return await service.update_object(
            workspace_id=workspace_id,
            object_type=object_type,
            object_id=object_id,
            properties=properties,
        )

    async def get_contact(
        self,
        workspace_id: str,
        object_id: str,
        associations: list[str] | None = None,
        provider: Provider = Provider.HUBSPOT,
    ) -> Mapping[str, Any] | None:
        service = self._resolve_provider_service(provider)
        return await service.get_contact(workspace_id, object_id, associations)

    async def get_deal(
        self,
        workspace_id: str,
        object_id: str,
        associations: list[str] | None = None,
        provider: Provider = Provider.HUBSPOT,
    ) -> Mapping[str, Any] | None:
        service = self._resolve_provider_service(provider)
        return await service.get_deal(workspace_id, object_id, associations)

    async def get_company(
        self,
        workspace_id: str,
        object_id: str,
        include_associations: bool = True,
        associations: list[str] | None = None,
        provider: Provider = Provider.HUBSPOT,
    ) -> Mapping[str, Any] | None:
        service = self._resolve_provider_service(provider)
        return await service.get_company(
            workspace_id, object_id, include_associations, associations
        )

    async def create_ticket(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
        associations: list[dict[str, Any]] | None = None,
        provider: Provider = Provider.HUBSPOT,
    ) -> Mapping[str, Any]:
        service = self._resolve_provider_service(provider)
        return await service.create_ticket(workspace_id, properties, associations)

    async def get_ticket_thread_id(
        self,
        workspace_id: str,
        ticket_id: str,
        provider: Provider = Provider.HUBSPOT,
    ) -> str | None:
        service = self._resolve_provider_service(provider)
        return await service.get_ticket_thread_id(workspace_id, ticket_id)

    async def add_conversation_message(
        self,
        workspace_id: str,
        thread_id: str,
        content: str,
        sender_email: str | None = None,
        is_internal: bool = True,
        provider: Provider = Provider.HUBSPOT,
    ) -> dict[str, Any]:
        service = self._resolve_provider_service(provider)
        return await service.add_conversation_message(
            workspace_id, thread_id, content, sender_email, is_internal
        )

    async def get_pipelines(
        self,
        workspace_id: str,
        object_type: str = "deals",
        provider: Provider = Provider.HUBSPOT,
    ) -> list[dict[str, Any]]:
        service = self._resolve_provider_service(provider)
        return await service.get_pipelines(workspace_id, object_type)

    async def associate_object(
        self,
        workspace_id: str,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        provider: Provider = Provider.HUBSPOT,
    ) -> None:
        service = self._resolve_provider_service(provider)
        await service.associate_object(workspace_id, from_type, from_id, to_type, to_id)

    async def get_associated_objects(
        self,
        workspace_id: str,
        from_object_type: str,
        object_id: str,
        to_object_type: str,
        prefetched_ids: list[str] | None = None,
        provider: Provider = Provider.HUBSPOT,
    ) -> list[dict[str, Any]]:
        service = self._resolve_provider_service(provider)
        return await service.get_associated_objects(
            workspace_id=workspace_id,
            from_object_type=from_object_type,
            object_id=object_id,
            to_object_type=to_object_type,
            prefetched_ids=prefetched_ids,
        )

    async def publish_app_event(
        self,
        workspace_id: str,
        event_template_id: str,
        object_type: str,
        object_id: str,
        properties: dict[str, str],
        provider: Provider = Provider.HUBSPOT,
    ) -> None:
        """Logs a custom app event to a record's timeline."""
        service = self._resolve_provider_service(provider)
        await service.publish_app_event(
            workspace_id=workspace_id,
            event_template_id=event_template_id,
            object_type=object_type,
            object_id=object_id,
            properties=properties,
        )

    async def get_owners(
        self,
        workspace_id: str,
        provider: Provider = Provider.HUBSPOT,
    ) -> list[dict[str, Any]]:
        service = self._resolve_provider_service(provider)
        return await service.get_owners(workspace_id)

    async def invalidate_object_caches(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        provider: Provider = Provider.HUBSPOT,
    ) -> None:
        """Invalidates object cache for a specific provider."""
        service = self._resolve_provider_service(provider)
        await service.invalidate_object_caches(
            workspace_id=workspace_id,
            object_type=object_type,
            object_id=object_id,
        )

    async def find_or_create_contact_by_email(
        self,
        workspace_id: str,
        email: str,
        name: str | None = None,
        provider: Provider = Provider.HUBSPOT,
    ) -> str:
        """Finds or creates a CRM contact by email address."""
        service = self._resolve_provider_service(provider)
        return await service.find_or_create_contact_by_email(
            workspace_id=workspace_id,
            email=email,
            name=name,
        )
