from __future__ import annotations  # noqa: D100

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.db.records import IntegrationRecord


class BaseCRMService(ABC):
    """Abstract base class for all CRM domain services.

    Defines a product-complete interface covering every CRM object type and
    action exposed to users through the Slack UI. Concrete implementations
    (HubSpotService, SalesforceService, etc.) must satisfy this contract so
    that interaction handlers never need to cast ``self.crm`` to a concrete type.
    """

    # ── Generic Object Operations ──────────────────────────────────────────────

    @abstractmethod
    async def get_object(
        self,
        *,
        workspace_id: str,
        object_type: str,
        object_id: str,
        ignore_cache: bool = False,
    ) -> Mapping[str, Any] | None:
        """Fetch any CRM object by type and ID."""
        pass

    @abstractmethod
    async def update_object(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        properties: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Generic update for any CRM object type."""
        pass

    @abstractmethod
    async def search(
        self,
        *,
        workspace_id: str,
        object_type: str,
        query: str,
    ) -> Sequence[Mapping[str, Any]]:
        """Search across any CRM object type by a free-text query."""
        pass

    @abstractmethod
    async def get_object_engagements(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        ignore_cache: bool = False,
        slack_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetches all engagements associated with a CRM object."""
        pass

    # ── Contacts ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_contact(
        self,
        workspace_id: str,
        object_id: str,
        associations: list[str] | None = None,
    ) -> Mapping[str, Any] | None:
        """Retrieve a single contact by ID."""
        pass

    @abstractmethod
    async def create_contact(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Create a new contact in the CRM."""
        pass

    # ── Deals ──────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_deal(
        self,
        workspace_id: str,
        object_id: str,
        associations: list[str] | None = None,
    ) -> Mapping[str, Any] | None:
        """Retrieve a single deal by ID."""
        pass

    # ── Companies ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_company(
        self,
        workspace_id: str,
        object_id: str,
        include_associations: bool = True,
        associations: list[str] | None = None,
    ) -> Mapping[str, Any] | None:
        """Retrieve a single company by ID."""
        pass

    # ── Tickets & Conversations ────────────────────────────────────────────────

    @abstractmethod
    async def create_ticket(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
        associations: list[dict[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        """Create a new support ticket in the CRM."""
        pass

    @abstractmethod
    async def get_ticket_thread_id(
        self, workspace_id: str, ticket_id: str
    ) -> str | None:
        """Retrieves the conversation thread ID associated with a Helpdesk ticket."""
        pass

    @abstractmethod
    async def add_conversation_message(
        self,
        workspace_id: str,
        thread_id: str,
        content: str,
        sender_email: str | None = None,
        is_internal: bool = True,
    ) -> dict[str, Any]:
        """Injects a message into a Helpdesk Conversation thread."""
        pass

    # ── Notes ──────────────────────────────────────────────────────────────────

    @abstractmethod
    async def create_note(
        self,
        *,
        workspace_id: str,
        content: str,
        associated_id: str,
        associated_type: str,
        continuous: bool = False,
    ) -> dict[str, Any]:
        """Create a note/activity associated with any CRM object."""
        pass

    @abstractmethod
    async def create_email_activity(
        self,
        *,
        workspace_id: str,
        html_content: str,
        subject: str = "Email Reply from REHA Connect",
        associated_id: str | None = None,
        associated_type: str | None = None,
    ) -> dict[str, Any]:
        """Create an email activity/reply associated with a CRM object."""
        pass

    # ── Tasks ──────────────────────────────────────────────────────────────────

    @abstractmethod
    async def create_task(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Create a new task in the CRM."""
        pass

    # ── Meetings ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def create_meeting(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create/schedule a meeting in the CRM."""
        pass

    @abstractmethod
    async def get_contact_meetings(
        self, workspace_id: str, contact_id: str
    ) -> list[dict[str, Any]]:
        """Retrieve meetings associated with a contact."""
        pass

    # ── Pipelines ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_pipelines(
        self,
        workspace_id: str,
        object_type: str = "deals",
    ) -> list[dict[str, Any]]:
        """List all pipelines for the given object type (e.g., deals, tickets)."""
        pass

    # ── Associations ───────────────────────────────────────────────────────────

    @abstractmethod
    async def associate_object(
        self,
        workspace_id: str,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
    ) -> None:
        """Create an association between two CRM objects."""
        pass

    @abstractmethod
    async def get_associated_objects(
        self,
        workspace_id: str,
        from_object_type: str,
        object_id: str,
        to_object_type: str,
        prefetched_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch and batch read any associated object type."""
        pass

    # ── Timeline Events ────────────────────────────────────────────────────────

    @abstractmethod
    async def publish_app_event(
        self,
        workspace_id: str,
        event_template_id: str,
        object_type: str,
        object_id: str,
        properties: dict[str, str],
    ) -> None:
        """Logs a custom app event to a record's timeline."""
        pass

    # ── Client Access ──────────────────────────────────────────────────────────

    @abstractmethod
    async def get_client(
        self,
        workspace_id: str,
        slack_user_id: str | None = None,
    ) -> Any:
        """Fetch the low-level authenticated provider API client."""
        pass

    @abstractmethod
    async def get_support_client(self) -> Any:
        """Retrieves a client for the Support Portal."""
        pass

    @abstractmethod
    async def get_object_with_client(
        self,
        client: Any,
        object_type: str,
        object_id: str,
    ) -> Mapping[str, Any] | None:
        """Fetch an object using a pre-authenticated client (avoids re-auth)."""
        pass

    @abstractmethod
    async def get_client_from_integration(
        self,
        integration: IntegrationRecord,
        portal_id: str | None = None,
        slack_ts: str | None = None,
    ) -> Any:
        """Build an authenticated client directly from an integration record."""
        pass

    @abstractmethod
    async def get_owners(self, workspace_id: str) -> list[dict[str, Any]]:
        """List all active owners/agents in the CRM workspace."""
        pass

    @abstractmethod
    async def find_or_create_contact_by_email(
        self, workspace_id: str, email: str, name: str | None = None
    ) -> str:
        """Find contact by email, or create a new one if not found. Returns the contact ID."""
        pass

    @abstractmethod
    async def invalidate_object_caches(
        self, workspace_id: str, object_type: str, object_id: str
    ) -> None:
        """Programmatically clears cached engagements and associations for a record."""
        pass
