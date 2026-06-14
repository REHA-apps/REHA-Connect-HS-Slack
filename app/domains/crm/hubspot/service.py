# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from app.core.config import settings
from app.core.exceptions import IntegrationNotFoundError
from app.core.logging import get_logger
from app.db.records import Provider
from app.db.storage_service import StorageService

if TYPE_CHECKING:
    from app.db.records import IntegrationRecord
from app.domains.crm.base import BaseCRMService
from app.providers.hubspot.client import HubSpotClient
from app.utils.cache import AsyncTTL
from app.utils.helpers import (
    _HS_NAME_TO_TYPE_ID,
    HS_ACTIVITY_ASSOCS,
    get_hub_host,
    normalize_object_type,
    pluralize_hs_type,
)
from app.utils.html import sanitize_for_hubspot, strip_html

from .models import (
    HubSpotCompany,
    HubSpotContact,
    HubSpotDeal,
    HubSpotObject,
    HubSpotOwner,
    HubSpotTicket,
)

logger = get_logger("hubspot.service")


class HubSpotService(BaseCRMService):
    """Domain service coordinating high-level HubSpot business logic.

    Attributes:
        _PIPELINES_CACHE (AsyncTTL): Class-level cache for deal pipelines.
        _OWNERS_CACHE (AsyncTTL): Class-level cache for HubSpot owners.

    """

    _PIPELINES_CACHE = AsyncTTL(ttl=3600)  # Cache for 1 hour
    _OWNERS_CACHE = AsyncTTL(ttl=3600)  # Cache for 1 hour
    _ENGAGEMENTS_CACHE = AsyncTTL(ttl=300)  # Cache engagements for 5 mins
    _ASSOCIATIONS_CACHE = AsyncTTL(ttl=300)  # Cache associations for 5 mins
    _OBJECT_MEMO_CACHE = AsyncTTL(ttl=15)  # Local memo for webhook bursts

    # Canonical property lists for batch-reading associated CRM objects.
    # Shared by get_associated_objects() and get_all_associations().
    _ASSOC_PROPS: dict[str, tuple[list[str], str]] = {
        "contact": (
            [
                "firstname",
                "lastname",
                "email",
                "phone",
                "lifecyclestage",
                "jobtitle",
                "hs_persona",
            ],
            "contact",
        ),
        "deal": (
            ["dealname", "amount", "pipeline", "dealstage", "hs_forecast_amount"],
            "deal",
        ),
        "company": (["name", "domain", "industry"], "company"),
        "ticket": (["subject", "hs_ticket_priority", "hs_ticket_category"], "ticket"),
    }

    def __init__(
        self,
        corr_id: str | None = None,
        *,
        storage: StorageService | None = None,
        slack_ts: str | None = None,
    ) -> None:
        self.corr_id = corr_id or "system"
        self.slack_ts = slack_ts
        self.storage = storage or StorageService(corr_id=self.corr_id)
        # Per-instance client cache: avoids re-fetching tokens from
        # Supabase on every method call within the same request.
        self._client_cache: dict[str, HubSpotClient] = {}

        # Per-instance integration cache to avoid N+1 lookups for metadata
        self._integration_cache: dict[str, Any] = {}

    # Client lifecycle
    async def get_client(
        self,
        workspace_id: str,
        slack_user_id: str | None = None,
    ) -> HubSpotClient:
        """Retrieves an authenticated HubSpotClient for the given workspace/user.

        If slack_user_id is provided, it attempts to fetch a user-specific
        HubSpot token. This is used for authenticated unfurling to prevent
        unauthorized data access.
        """
        cache_key = f"{workspace_id}:{slack_user_id}" if slack_user_id else workspace_id
        if cache_key in self._client_cache:
            return self._client_cache[cache_key]

        # 1. Fetch Integration Record (Cached via StorageService)
        integration = None
        if slack_user_id:
            integration = await self.storage.get_integration(
                workspace_id, Provider.HUBSPOT, slack_user_id=slack_user_id
            )
            if integration:
                logger.debug(
                    "Resolved user-specific HubSpot client for %s", slack_user_id
                )

        # 2. Fallback to workspace-level master token if no user token
        if not integration:
            integration = await self.storage.get_integration(
                workspace_id=workspace_id,
                provider=Provider.HUBSPOT,
            )

        if not integration:
            raise IntegrationNotFoundError(
                f"No HubSpot integration for workspace {workspace_id}"
            )

        # 3. Fetch Workspace for Portal ID (Triple-Key Trace requirement)
        workspace = await self.storage.get_workspace(workspace_id)
        portal_id = integration.portal_id if integration else None

        # Fallback: If workspace doesn't have portal_id, use the one from integration metadata
        if not portal_id:
            portal_id = integration.metadata.get("portal_id")

        return await self.get_client_from_integration(
            integration,
            portal_id=portal_id,
            slack_ts=self.slack_ts,
        )

    async def get_support_client(self) -> HubSpotClient:
        """Retrieves a client for the REHA Support Portal using Private App credentials.

        This client uses the dedicated support portal token defined in settings,
        allowing the app to route support requests directly to REHA's internal
        HubSpot Helpdesk.
        """
        token = settings.HUBSPOT_SUPPORT_ACCESS_TOKEN.get_secret_value()
        portal_id = settings.HUBSPOT_SUPPORT_PORTAL_ID

        if not token:
            logger.warning(
                "HUBSPOT_SUPPORT_ACCESS_TOKEN not configured; support tickets will fail"
            )
            raise ValueError("Support portal credentials missing")

        return HubSpotClient(
            corr_id=self.corr_id,
            access_token=token,
            refresh_token=None,  # Private Apps don't use refresh tokens
            portal_id=portal_id,
            slack_ts=self.slack_ts,
        )

    async def get_client_from_integration(
        self,
        integration: IntegrationRecord,
        portal_id: str | None = None,
        slack_ts: str | None = None,
    ) -> HubSpotClient:
        """Builds an authenticated HubSpotClient directly from an integration record."""
        hub_domain = integration.metadata.get("hub_domain")

        client = HubSpotClient(
            access_token=integration.access_token or "",
            refresh_token=integration.refresh_token,
            hub_domain=hub_domain,
            corr_id=self.corr_id,
            portal_id=portal_id,
            slack_ts=slack_ts,
            expires_at=integration.expires_at,
        )

        async def _handle_refresh(
            new_at: str, new_rt: str | None, new_expires_at: int | None
        ) -> None:
            # Note: We update the specific integration ID found (bridged or native)
            await self.persist_tokens(
                workspace_id=integration.workspace_id,
                new_access=new_at,
                new_refresh=new_rt,
                new_expires_at=new_expires_at,
            )

        async def _handle_revocation() -> None:
            from app.domains.crm.integration_service import IntegrationService

            service = IntegrationService(self.corr_id, storage=self.storage)
            await service.uninstall_workspace(
                integration.workspace_id, trigger_hubspot_uninstall=False
            )

        client.on_token_refresh = _handle_refresh
        client.on_token_revoked = _handle_revocation
        return client

    # Persistence logic
    async def persist_tokens(
        self,
        workspace_id: str,
        new_access: str,
        new_refresh: str | None,
        new_expires_at: int | None = None,
    ) -> None:
        """Persists rotated tokens to storage."""
        integration = await self.storage.get_integration(workspace_id, Provider.HUBSPOT)
        if not integration:
            logger.error(
                "No integration found to persist tokens for workspace %s", workspace_id
            )
            return

        creds = dict(integration.credentials or {})
        creds["access_token"] = new_access
        if new_refresh:
            creds["refresh_token"] = new_refresh
        if new_expires_at:
            creds["expires_at"] = new_expires_at

        await self.storage.upsert_integration(
            {
                "id": integration.id,
                "workspace_id": workspace_id,
                "provider": Provider.HUBSPOT,
                "credentials": creds,
                "metadata": integration.metadata,
            }
        )

    # Cache Control
    async def get_object_count(
        self,
        workspace_id: str,
        object_type: str,
        filters: list[dict[str, Any]] | None = None,
    ) -> int:
        """Retrieves the total count of objects matching the given filters."""
        client = await self.get_client(workspace_id)
        payload: dict[str, Any] = {
            "limit": 0,
            "filterGroups": [{"filters": filters}] if filters else [],
        }
        try:
            resp = await client.request(
                "POST",
                f"crm/objects/{settings.HUBSPOT_API_VERSION}/{pluralize_hs_type(object_type)}/search",
                json=payload,
                description=f"HubSpot: Count {object_type}",
            )
            return int(resp.get("total", 0))
        except Exception as e:
            logger.warning("Failed to fetch count for %s: %s", object_type, e)
            return 0

    async def get_open_deals_count(self, workspace_id: str) -> int:
        """Counts deals that are not in a closed stage."""
        return await self.get_object_count(
            workspace_id,
            "deal",
            filters=[
                {"propertyName": "hs_is_closed", "operator": "NEQ", "value": "true"}
            ],
        )

    async def get_open_tickets_count(self, workspace_id: str) -> int:
        """Counts tickets that are not in a closed stage."""
        return await self.get_object_count(
            workspace_id,
            "ticket",
            filters=[
                {"propertyName": "hs_is_closed", "operator": "NEQ", "value": "true"}
            ],
        )

    async def invalidate_object_caches(
        self, workspace_id: str, object_type: str, object_id: str
    ) -> None:
        """Programmatically clears cached engagements and associations for a record.

        Used when HubSpot webhooks indicate that a record has been modified,
        ensuring subsequent Slack searches return fresh data.
        """
        norm_type = normalize_object_type(object_type)
        eng_key = f"engagements:{workspace_id}:{norm_type}:{object_id}"
        assoc_key = f"associations:{workspace_id}:{norm_type}:{object_id}"

        logger.debug(
            "Invalidating HubSpot object caches for workspace_id=%s type=%s id=%s",
            workspace_id,
            norm_type,
            object_id,
        )

        await asyncio.gather(
            self._ENGAGEMENTS_CACHE.invalidate(eng_key),
            self._ASSOCIATIONS_CACHE.invalidate(assoc_key),
        )

    # Domain operations
    async def search(
        self,
        *,
        workspace_id: str,
        object_type: str,
        query: str,
    ) -> Sequence[Mapping[str, Any]]:
        """Unified HubSpot search entry point."""
        object_type = normalize_object_type(object_type)

        # 1. Handle Universal Search (Multi-Object)
        if object_type == "universal":
            # Search all primary object types in parallel for a true "Google for CRM" experience
            results_list = await asyncio.gather(
                self.search_contacts(workspace_id, query),
                self._search_by_type(workspace_id, "deals", query),
                self._search_by_type(workspace_id, "companies", query),
                self._search_by_type(workspace_id, "tickets", query),
                self._search_by_type(workspace_id, "tasks", query),
                return_exceptions=True,  # Partial failure: one type failing won't abort all others
            )

            # Label results and filter out any exception results gracefully
            type_labels = ["contact", "deal", "company", "ticket", "task"]
            contacts, deals, companies, tickets, tasks = [
                (
                    cast(list[dict[str, Any]], r)
                    if not isinstance(r, BaseException)
                    else []
                )
                for r in results_list
            ]
            for exc_result, label in zip(results_list, type_labels):
                if isinstance(exc_result, BaseException):
                    logger.warning(
                        "Universal search: %s search failed gracefully: %s",
                        label,
                        exc_result,
                    )

            for r in contacts:
                r["type"] = "contact"
            for r in deals:
                r["type"] = "deal"
            for r in companies:
                r["type"] = "company"
            for r in tickets:
                r["type"] = "ticket"
            for r in tasks:
                r["type"] = "task"

            # Combine and return
            all_results = contacts + deals + companies + tickets + tasks
            return await self.inject_urls(workspace_id, all_results)

        # 2. Handle Specific Specialized Search
        match object_type:
            case "contacts" | "contact" | "leads" | "lead":
                results = await self.search_contacts(workspace_id, query)
                url_segment = "contact"
            case "deals" | "deal":
                results = await self._search_by_type(workspace_id, "deals", query)
                url_segment = "deal"
            case "companies" | "company":
                results = await self._search_by_type(workspace_id, "companies", query)
                url_segment = "company"
            case "tickets" | "ticket":
                results = await self._search_by_type(workspace_id, "tickets", query)
                url_segment = "ticket"
            case "tasks" | "task":
                results = await self._search_by_type(workspace_id, "tasks", query)
                url_segment = "task"
            case _:
                logger.error("Unknown HubSpot object_type=%s", object_type)
                return []

        return await self.inject_urls(workspace_id, results, url_segment)

    async def inject_urls(
        self,
        workspace_id: str,
        results: list[Any],
        object_type: str | None = None,
    ) -> list[HubSpotObject]:
        """Injects `hs_url` deep links and standardizes `type` on a list of results."""
        if not results:
            return results

        # Try instance cache first, then storage (which has its own cache)
        integration = self._integration_cache.get(workspace_id)
        if not integration:
            integration = await self.storage.get_integration(
                workspace_id, Provider.HUBSPOT
            )
            if integration:
                self._integration_cache[workspace_id] = integration

        portal_id = integration.portal_id if integration else None

        hub_host = get_hub_host(integration.metadata if integration else None)

        # 3.4 Performance: Pre-normalize object_type once if provided
        normalized_provided_type = (
            normalize_object_type(object_type.lower()) if object_type else None
        )

        for r in results:
            if normalized_provided_type:
                obj_type = normalized_provided_type
            else:
                raw_type = str(r.get("type") or "contact")
                obj_type = normalize_object_type(raw_type.lower())
            r["type"] = obj_type
            object_id = r.get("id")
            active_portal_id = str(
                r.get("portalId") or r.get("portal_id") or portal_id or ""
            )

            if active_portal_id and object_id and not r.get("hs_url"):
                # Use parent record for deep-linking activities (Engagements)
                parent_id = r.get("_parent_id")
                parent_type = r.get("_parent_type")

                # Smart resolution from associations if explicit parent info is missing
                if not parent_id and r.get("associations"):
                    parent_id, parent_type = self._resolve_parent_from_associations(
                        r["associations"]
                    )

                if parent_id and parent_type:
                    p_norm = normalize_object_type(parent_type)
                    p_type_id = _HS_NAME_TO_TYPE_ID.get(p_norm, "0-1")
                    r["hs_url"] = (
                        f"https://{hub_host}/contacts/{active_portal_id}/record/{p_type_id}/{parent_id}?engagement={object_id}"
                    )
                elif obj_type == "task":
                    r["hs_url"] = (
                        f"https://{hub_host}/tasks/{active_portal_id}/task/{object_id}"
                    )
                else:
                    type_id = _HS_NAME_TO_TYPE_ID.get(obj_type, "0-1")
                    # Use type ID directly if it's already one (e.g. custom object)
                    if obj_type.startswith("0-") or (
                        "-" in obj_type and obj_type.split("-")[0].isdigit()
                    ):
                        type_id = obj_type

                    r["hs_url"] = (
                        f"https://{hub_host}/contacts/{active_portal_id}/record/{type_id}/{object_id}"
                    )

        return results

    def _resolve_parent_from_associations(
        self, associations: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Resolves the best parent record (Contact/Deal/Company/Ticket) from metadata."""
        # Priority order defined in HS_ACTIVITY_ASSOCS
        for p_type_plural in HS_ACTIVITY_ASSOCS:
            p_data = associations.get(p_type_plural) or {}
            p_results = p_data.get("results", [])
            if p_results:
                return p_results[0]["id"], p_type_plural
        return None, None

    async def search_contacts(
        self, workspace_id: str, query: str
    ) -> list[HubSpotContact]:
        client = await self.get_client(workspace_id)
        results = await client.search_contacts(query)
        return cast(list[HubSpotContact], results)

    async def _search_by_type(
        self, workspace_id: str, object_type: str, query: str
    ) -> list[HubSpotObject]:
        client = await self.get_client(workspace_id)
        results = await client.search_objects(
            object_type,
            query_string=query,
            properties=HubSpotClient.search_props_for(object_type),
        )
        return cast(list[HubSpotObject], results)

    async def create_contact(
        self, workspace_id: str, properties: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        client = await self.get_client(workspace_id)
        return await client.create_contact(properties)

    async def find_or_create_contact_by_email(
        self,
        workspace_id: str | None,
        email: str,
        name: str | None = None,
        client: HubSpotClient | None = None,
    ) -> str:
        """Finds a contact by email or creates a new one if not found."""
        if not client:
            if not workspace_id:
                raise ValueError("Either workspace_id or client must be provided")
            client = await self.get_client(workspace_id)

        results = await client.search_contacts(email)
        if results:
            return str(results[0]["id"])

        props = {"email": email}
        if name:
            if " " in name:
                first, last = name.split(" ", 1)
                props["firstname"] = first
                props["lastname"] = last
            else:
                props["firstname"] = name

        contact = await client.create_contact(props)
        return str(contact["id"])

    async def create_task(
        self, workspace_id: str, properties: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        client = await self.get_client(workspace_id)
        return await client.create_task(properties)

    async def create_ticket(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
        associations: list[dict[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        client = await self.get_client(workspace_id)
        return await client.create_object(
            "tickets", properties, associations=associations
        )

    async def create_support_ticket(
        self,
        properties: Mapping[str, Any],
        associations: list[dict[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        """Creates a support ticket in the developer's portal."""
        client = await self.get_support_client()
        return await client.create_object(
            "tickets", properties, associations=associations
        )

    async def associate_object(
        self,
        workspace_id: str,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
    ) -> None:
        """Associate two CRM objects using HubSpot CRM v4 default associations."""
        client = await self.get_client(workspace_id)
        from_type = pluralize_hs_type(from_type)
        to_type = pluralize_hs_type(to_type)

        await client.request(
            "PUT",
            f"crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}",
            description=f"HubSpot: Associate {from_type} → {to_type}",
        )

    async def get_pipelines(
        self,
        workspace_id: str,
        object_type: str = "deals",
    ) -> list[dict[str, Any]]:
        """Fetch CRM pipelines for the given object type (deals or tickets) with a shared TTL cache."""
        key = f"pipelines:{workspace_id}:{object_type}"

        async def _fetch() -> list[dict[str, Any]]:
            client = await self.get_client(workspace_id)
            res = await client.request("GET", f"crm/v3/pipelines/{object_type}")
            if not res:
                return []
            return res.get("results", []) if isinstance(res, dict) else []

        return await self._PIPELINES_CACHE.get_or_fetch(key, _fetch)

    async def get_batch_objects(
        self,
        workspace_id: str,
        object_type: str,
        object_ids: list[str],
        properties: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch multiple CRM objects of the same type in a single batch read call."""
        if not object_ids:
            return []
        client = await self.get_client(workspace_id)
        return await client.batch_read(object_type, object_ids, properties=properties)

    async def _get_record(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        properties: list[str],
        associations: list[str] | None = None,
    ) -> HubSpotObject | None:
        """Internal helper for consistent CRM object retrieval."""
        client = await self.get_client(workspace_id=workspace_id)
        result = await client.get_object(
            object_type,
            object_id,
            properties=properties,
            associations=associations,
        )
        if result:
            results = await self.inject_urls(workspace_id, [result], object_type)
            res = results[0]
            res["workspace_id"] = workspace_id
            return res
        return None

    async def get_contact(
        self,
        workspace_id: str,
        object_id: str,
        associations: list[str] | None = None,
    ) -> HubSpotContact | None:
        """Retrieves a single contact from HubSpot."""
        result = await self._get_record(
            workspace_id=workspace_id,
            object_type="contact",
            object_id=object_id,
            properties=[
                "firstname",
                "lastname",
                "email",
                "phone",
                "jobtitle",
                "hs_persona",
                "hubspot_owner_id",
            ],
            associations=associations,
        )
        return cast(HubSpotContact | None, result)

    async def get_object_engagements(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        ignore_cache: bool = False,
        slack_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetches all engagements associated with a CRM object."""
        key = f"engagements:{workspace_id}:{object_type}:{object_id}"

        async def _fetch():
            client = await self.get_client(workspace_id=workspace_id)
            hs_type = pluralize_hs_type(object_type)
            engagements = []
            entities = {
                "notes": ["hs_note_body", "hs_timestamp"],
                "emails": [
                    "hs_email_subject",
                    "hs_email_text",
                    "hs_email_html",
                    "hs_email_direction",
                    "hs_automated_email_id",
                    "hs_timestamp",
                ],
                "meetings": [
                    "hs_meeting_title",
                    "hs_meeting_body",
                    "hs_meeting_start_time",
                    "hs_meeting_end_time",
                    "hs_meeting_outcome",
                ],
                "calls": [
                    "hs_call_title",
                    "hs_call_body",
                    "hs_call_status",
                    "hs_timestamp",
                ],
                "tasks": [
                    "hs_task_subject",
                    "hs_task_body",
                    "hs_task_status",
                    "hs_task_priority",
                    "hs_timestamp",
                ],
            }

            async def _fetch_entity(entity_type: str, props: list[str]):
                try:
                    singular_entity = normalize_object_type(entity_type)
                    assoc_ids = await client.get_associations(
                        hs_type, object_id, singular_entity
                    )
                    if not assoc_ids:
                        return []
                    details = await client.batch_read(
                        entity_type, assoc_ids, properties=props
                    )
                    for d in details:
                        d["_engagement_type"] = entity_type
                        # Parent tracking for deep-links
                        d["_parent_id"] = object_id
                        d["_parent_type"] = hs_type
                    return details
                except Exception as e:
                    logger.error(
                        "Failed to fetch %s engagements for %s %s: %s",
                        entity_type,
                        object_type,
                        object_id,
                        e,
                    )
                    return []

            tasks = [
                _fetch_entity(entity_type, props)
                for entity_type, props in entities.items()
            ]
            results = await asyncio.gather(*tasks)
            for r in results:
                engagements.extend(r)

            return engagements

        if ignore_cache:
            return await _fetch()

        return await self._ENGAGEMENTS_CACHE.get_or_fetch(key, _fetch)

    async def get_deal(
        self,
        workspace_id: str,
        object_id: str,
        associations: list[str] | None = None,
    ) -> HubSpotDeal | None:
        """Retrieves a single deal from HubSpot."""
        result = await self._get_record(
            workspace_id=workspace_id,
            object_type="deal",
            object_id=object_id,
            properties=[
                "dealname",
                "amount",
                "hs_forecast_amount",
                "pipeline",
                "dealstage",
                "closedate",
                "hubspot_owner_id",
            ],
            associations=associations,
        )
        return cast(HubSpotDeal | None, result)

    async def get_company(
        self,
        workspace_id: str,
        object_id: str,
        include_associations: bool = True,
        associations: list[str] | None = None,
    ) -> HubSpotCompany | None:
        """Retrieves a single company with optional associations."""
        res = await self._get_record(
            workspace_id=workspace_id,
            object_type="company",
            object_id=object_id,
            properties=[
                "name",
                "domain",
                "industry",
                "num_associated_contacts",
                "num_associated_deals",
                "hs_analytics_num_page_views",
                "hs_analytics_num_visits",
                "hubspot_owner_id",
            ],
            associations=associations,
        )
        if res and include_associations:
            # 1. Extract IDs from prefetched associations if available
            # HubSpot v3 'associations' parameter returns
            # { 'associations': { 'contacts': { 'results': [...] } } }
            assoc_map = res.get("associations") or {}

            def _get_ids(target: str) -> list[str] | None:
                # HubSpot V3 associations results can use singular or plural keys
                singular = normalize_object_type(target)
                data = assoc_map.get(target) or assoc_map.get(singular)
                if data is None:
                    return None  # Signal that we didn't prefetch this type
                results = data.get("results", [])
                return [r["id"] for r in results]

            # 2. Fetch associations concurrently (leverages _ASSOCIATIONS_CACHE)
            # We pass prefetched_ids to skip the 'get_associations' lookup trip
            contacts_task = self.get_associated_objects(
                workspace_id,
                "company",
                object_id,
                "contact",
                prefetched_ids=_get_ids("contacts"),
            )
            deals_task = self.get_associated_objects(
                workspace_id,
                "company",
                object_id,
                "deal",
                prefetched_ids=_get_ids("deals"),
            )
            tickets_task = self.get_associated_objects(
                workspace_id,
                "company",
                object_id,
                "ticket",
                prefetched_ids=_get_ids("tickets"),
            )
            leads_task = self.get_associated_objects(
                workspace_id,
                "company",
                object_id,
                "lead",
                prefetched_ids=_get_ids("leads"),
            )

            assoc_results = await asyncio.gather(
                contacts_task,
                deals_task,
                tickets_task,
                leads_task,
                return_exceptions=True,
            )

            # Attach them to the structure AIService expects
            assoc_data = {
                "contacts": (
                    assoc_results[0]
                    if not isinstance(assoc_results[0], Exception)
                    else []
                ),
                "deals": (
                    assoc_results[1]
                    if not isinstance(assoc_results[1], Exception)
                    else []
                ),
                "tickets": (
                    assoc_results[2]
                    if not isinstance(assoc_results[2], Exception)
                    else []
                ),
                "leads": (
                    assoc_results[3]
                    if not isinstance(assoc_results[3], Exception)
                    else []
                ),
            }
            res["associated_objects"] = cast(dict[str, list[Any]], assoc_data)

        return cast(HubSpotCompany | None, res)

    async def get_object(
        self,
        *,
        workspace_id: str,
        object_type: str,
        object_id: str,
        associations: list[str] | None = None,
        ignore_cache: bool = False,
    ) -> HubSpotObject | None:
        """Dynamic entry point to fetch any HubSpot object by type.

        Includes a short-term memo cache to skip redundant fetches during bursts.
        """
        norm_type = normalize_object_type(object_type)
        key = f"memo:{workspace_id}:{norm_type}:{object_id}"

        async def _fetch():  # noqa: PLR0912
            result: HubSpotObject | None = None
            match norm_type:
                case "contact":
                    result = cast(
                        HubSpotObject | None,
                        await self.get_contact(workspace_id, object_id, associations),
                    )
                case "company":
                    result = cast(
                        HubSpotObject | None,
                        await self.get_company(
                            workspace_id,
                            object_id,
                            include_associations=True,
                            associations=associations,
                        ),
                    )
                case "deal":
                    result = cast(
                        HubSpotObject | None,
                        await self.get_deal(workspace_id, object_id, associations),
                    )
                case "ticket":
                    result = cast(
                        HubSpotObject | None,
                        await self.get_ticket(workspace_id, object_id),
                    )
                case "task":
                    result = await self.get_task(workspace_id, object_id)
                case "meeting":
                    result = await self.get_meeting(workspace_id, object_id)
                case "note":
                    result = await self.get_note(workspace_id, object_id)
                case "call":
                    result = await self.get_call(workspace_id, object_id)
                case "email":
                    result = await self.get_email(workspace_id, object_id)
                case "lead":
                    result = await self.get_lead(workspace_id, object_id)
                case "conversation" | "thread":
                    client = await self.get_client(workspace_id)
                    thread = await client.get_inbox_thread(object_id)
                    result = cast(HubSpotObject | None, thread)
                case _:
                    logger.error("Unknown object_type=%s for get_object", object_type)

            if result:
                result["type"] = norm_type
            return result

        if ignore_cache:
            return await _fetch()

        return await self._OBJECT_MEMO_CACHE.get_or_fetch(key, _fetch)

    async def get_ticket(
        self,
        workspace_id: str,
        object_id: str,
        associations: list[str] | None = None,
    ) -> HubSpotTicket | None:
        """Retrieves a single ticket from HubSpot."""
        result = await self._get_record(
            workspace_id=workspace_id,
            object_type="ticket",
            object_id=object_id,
            properties=[
                "subject",
                "content",
                "hs_pipeline",
                "hs_pipeline_stage",
                "hs_ticket_priority",
                "hubspot_owner_id",
            ],
            associations=associations,
        )
        return cast(HubSpotTicket | None, result)

    async def get_task(self, workspace_id: str, object_id: str) -> HubSpotObject | None:
        """Retrieves a single task from HubSpot."""
        result = await self._get_record(
            workspace_id=workspace_id,
            object_type="task",
            object_id=object_id,
            properties=[
                "hs_task_subject",
                "hs_task_body",
                "hs_task_status",
                "hs_task_priority",
                "hs_task_type",
                "hs_timestamp",
                "hubspot_owner_id",
            ],
        )
        return result

    async def get_meeting(
        self, workspace_id: str, object_id: str
    ) -> HubSpotObject | None:
        """Retrieves a single meeting engagement from HubSpot.

        Args:
            workspace_id: The internal workspace identifier.
            object_id: The HubSpot meeting object ID.

        """
        return await self._get_record(
            workspace_id=workspace_id,
            object_type="meeting",
            object_id=object_id,
            properties=[
                "hs_meeting_title",
                "hs_meeting_body",
                "hs_meeting_start_time",
                "hs_meeting_end_time",
                "hs_meeting_outcome",
            ],
            associations=HS_ACTIVITY_ASSOCS,
        )

    async def get_note(self, workspace_id: str, object_id: str) -> HubSpotObject | None:
        """Retrieves a single note engagement from HubSpot.

        Args:
            workspace_id: The internal workspace identifier.
            object_id: The HubSpot note object ID.

        """
        return await self._get_record(
            workspace_id=workspace_id,
            object_type="note",
            object_id=object_id,
            properties=["hs_note_body", "hs_timestamp"],
            associations=HS_ACTIVITY_ASSOCS,
        )

    async def get_call(self, workspace_id: str, object_id: str) -> HubSpotObject | None:
        """Retrieves a single call engagement from HubSpot.

        Args:
            workspace_id: The internal workspace identifier.
            object_id: The HubSpot call object ID.

        """
        return await self._get_record(
            workspace_id=workspace_id,
            object_type="call",
            object_id=object_id,
            properties=[
                "hs_call_title",
                "hs_call_body",
                "hs_call_status",
                "hs_call_disposition",
                "hs_call_duration_milliseconds",
                "hs_timestamp",
                "hubspot_owner_id",
            ],
            associations=HS_ACTIVITY_ASSOCS,
        )

    async def get_email(
        self, workspace_id: str, object_id: str
    ) -> HubSpotObject | None:
        """Retrieves a single email engagement from HubSpot.

        Args:
            workspace_id: The internal workspace identifier.
            object_id: The HubSpot email object ID.

        """
        return await self._get_record(
            workspace_id=workspace_id,
            object_type="email",
            object_id=object_id,
            properties=[
                "hs_email_subject",
                "hs_email_text",
                "hs_email_html",
                "hs_timestamp",
            ],
            associations=HS_ACTIVITY_ASSOCS,
        )

    async def get_lead(self, workspace_id: str, object_id: str) -> HubSpotObject | None:
        """Retrieves a single lead from HubSpot.

        Args:
            workspace_id: The internal workspace identifier.
            object_id: The HubSpot lead object ID.

        """
        return await self._get_record(
            workspace_id=workspace_id,
            object_type="lead",
            object_id=object_id,
            properties=["hs_lead_status", "firstname", "lastname", "company"],
        )

    async def get_ticket_thread_id(
        self, workspace_id: str, ticket_id: str
    ) -> str | None:
        """Retrieves the conversation thread ID associated with a Helpdesk ticket."""
        client = await self.get_client(workspace_id)
        try:
            # Check v4 associations from tickets to conversations
            res = await client.request(
                "GET", f"/crm/v4/objects/tickets/{ticket_id}/associations/conversations"
            )
            results = res.get("results", [])
            if results:
                # The ID of the associated conversation/thread
                return str(results[0]["toObjectId"])
        except Exception as e:
            logger.debug("No conversation thread found for ticket %s: %s", ticket_id, e)
        return None

    async def add_conversation_message(
        self,
        workspace_id: str,
        thread_id: str,
        content: str,
        sender_email: str | None = None,
        is_internal: bool = True,
    ) -> dict[str, Any]:
        """Injects a message into a Helpdesk Conversation thread.

        If is_internal is True, posts an Internal Comment (COMMENT).
        If is_internal is False, posts an Outbound Message (MESSAGE).
        """
        client = await self.get_client(workspace_id)

        # Determine actor
        # If sending an outbound message to a customer, it usually requires a known senderActorId
        # For an internal comment, we can often just provide the actor ID or use a generic one if we can't find it.
        # As a fallback for Slack bot logic, A- (Agent) prefix is used for users.
        actor_id = None
        if sender_email:
            try:
                # Try to find the user's HubSpot actor ID
                users_res = await client.request("GET", "/settings/v3/users/")
                users = users_res.get("results", [])
                user = next((u for u in users if u.get("email") == sender_email), None)
                if user:
                    actor_id = f"A-{user['id']}"
            except Exception as e:
                logger.warning("Failed to resolve actor_id for %s: %s", sender_email, e)

        payload: dict[str, Any] = {
            "type": "COMMENT" if is_internal else "MESSAGE",
            "text": content,
        }

        if actor_id:
            payload["senderActorId"] = actor_id

        # To send an actual outbound MESSAGE, HubSpot often requires channelAccountId and channelId.
        # But for COMMENT, it's typically accepted with just the text and actor.
        # If it's outbound MESSAGE, we try without channel ID first, but it might fail without it.
        try:
            res = await client.request(
                "POST",
                f"/conversations/v3/conversations/threads/{thread_id}/messages",
                json=payload,
            )
            return res
        except Exception as e:
            logger.error("Failed to post message to thread %s: %s", thread_id, e)
            raise

    async def create_note(
        self,
        *,
        workspace_id: str,
        content: str,
        associated_id: str,
        associated_type: str,
        continuous: bool = False,
    ) -> dict[str, Any]:
        """Creates a note in HubSpot and associates it with a CRM object.

        Special case for tasks: Appends the note to the task body as tasks don't
        support direct note associations via API.
        """
        if associated_type.lower() == "task":
            logger.info(
                "Redirecting create_note to update_task for task_id=%s", associated_id
            )
            # Fetch existing task to get current body
            task = await self.get_task(workspace_id, associated_id)
            if not task:
                logger.warning(
                    "Task %s not found for comment redirection", associated_id
                )
                raise IntegrationNotFoundError(f"Task {associated_id} not found")

            props = task.get("properties", {})
            current_body = props.get("hs_task_body") or ""
            logger.debug("Existing task body length: %d", len(current_body))

            timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
            # Format: append new comment with concise separation
            header = f"\n--- Slack ({timestamp}) ---"

            if current_body:
                updated_body = (
                    f"{current_body.rstrip()}\n{header}\n"
                    f"{sanitize_for_hubspot(content)}"
                )
            else:
                updated_body = f"{header.lstrip()}\n{sanitize_for_hubspot(content)}"

            logger.info(
                "Updating task %s with new comment. Total length: %d",
                associated_id,
                len(updated_body),
            )

            try:
                res = await self.update_object(
                    workspace_id=workspace_id,
                    object_type="task",
                    object_id=associated_id,
                    properties={"hs_task_body": updated_body},
                )
                logger.info("Task %s updated successfully", associated_id)
                return res
            except Exception as e:
                logger.error(
                    "Failed to update task %s body: %s", associated_id, e, exc_info=True
                )
                raise

        client = await self.get_client(workspace_id)

        if continuous and normalize_object_type(associated_type) == "ticket":
            try:
                note_ids = await client.get_associations(
                    "ticket", associated_id, "notes"
                )
                if note_ids:
                    last_note_ids = note_ids[-10:]
                    last_notes = await client.batch_read(
                        "notes",
                        last_note_ids,
                        properties=["hs_note_body", "hs_createdate"],
                    )

                    now_date_str = datetime.now(UTC).strftime("%Y-%m-%d")
                    matching_note = None
                    for n in reversed(last_notes):
                        props = n.get("properties", {})
                        body = props.get("hs_note_body", "")
                        createdate = props.get("hs_createdate", "")
                        if (
                            "reha-continuous-slack-log" in body
                            and createdate.startswith(now_date_str)
                        ):
                            matching_note = n
                            break

                    if matching_note:
                        existing_body = (
                            matching_note.get("properties", {}).get("hs_note_body")
                            or ""
                        )
                        if len(existing_body) < 50000:
                            new_content = f"<div class='hs-continuous-log-entry' style='margin-top: 12px;'>{sanitize_for_hubspot(content)}</div>"
                            new_body = f"{existing_body}{new_content}"
                            note_obj = await client.update_object(
                                "notes",
                                str(matching_note["id"]),
                                {
                                    "hs_note_body": new_body,
                                    "hs_timestamp": int(
                                        datetime.now(UTC).timestamp() * 1000
                                    ),
                                },
                            )
                            # Invalidate cache
                            eng_key = (
                                f"engagements:{workspace_id}:ticket:{associated_id}"
                            )
                            await self._ENGAGEMENTS_CACHE.invalidate(eng_key)
                            return note_obj
            except Exception as e:
                logger.warning(
                    "Failed to lookup continuous note log for ticket=%s: %s",
                    associated_id,
                    e,
                )

        # 1. Create the note object first (properties only, no in-line associations)
        # Note: We use creation properties directly via create_object
        body = sanitize_for_hubspot(content)
        if continuous:
            body = f'<span class="reha-continuous-slack-log" style="display:none;"></span>{body}'

        note = await client.create_object(
            "notes",
            {
                "hs_note_body": body,
                "hs_timestamp": int(datetime.now(UTC).timestamp() * 1000),
            },
        )

        # 2. Add association separately (more robust and consistent with create_task)
        if associated_id and associated_type:
            try:
                await self.associate_object(
                    workspace_id=workspace_id,
                    from_type="note",
                    from_id=str(note["id"]),
                    to_type=normalize_object_type(associated_type),
                    to_id=associated_id,
                )
            except Exception:
                logger.warning(
                    "Note created (id=%s) but association to %s (id=%s) failed",
                    note["id"],
                    associated_type,
                    associated_id,
                )
        return note

    async def create_email_activity(
        self,
        *,
        workspace_id: str,
        html_content: str,
        subject: str = "Email Reply from REHA Connect",
        associated_id: str | None = None,
        associated_type: str | None = None,
        sender_email: str | None = None,
        sender_name: str | None = None,
    ) -> dict[str, Any]:
        """Creates an email engagement activity in HubSpot and associates it."""
        client = await self.get_client(workspace_id)
        plain_text = strip_html(html_content)
        import html

        # CONTINUOUS LOG LOGIC: For tickets, check if there's a recent email with the same subject
        if (
            associated_id
            and associated_type
            and normalize_object_type(associated_type) == "ticket"
        ):
            try:
                email_ids = await client.get_associations(
                    "ticket", associated_id, "emails"
                )
                if email_ids:
                    # Get the last 10 emails (assuming ascending order, take the tail)
                    last_email_ids = email_ids[-10:]
                    last_emails = await client.batch_read(
                        "emails",
                        last_email_ids,
                        properties=[
                            "hs_email_subject",
                            "hs_email_html",
                            "hs_email_text",
                            "hs_createdate",
                        ],
                    )

                    # Group logs by day so we don't have infinite scrolling blocks
                    now_date_str = datetime.now(UTC).strftime("%Y-%m-%d")

                    # Search backwards for the matching subject and current day
                    matching_email = None
                    for e in reversed(last_emails):
                        props = e.get("properties", {})
                        if props.get("hs_email_subject") == subject:
                            createdate = props.get("hs_createdate", "")
                            # HubSpot timestamps are usually ISO format: 2026-06-09T06:43:45Z
                            if createdate.startswith(now_date_str):
                                matching_email = e
                                break

                    if matching_email:
                        existing_html = (
                            matching_email.get("properties", {}).get("hs_email_html")
                            or ""
                        )
                        existing_text = (
                            matching_email.get("properties", {}).get("hs_email_text")
                            or ""
                        )

                        # HubSpot's rich text properties generally max out at 65,536 characters.
                        # If the thread gets too long, we simply ignore the matching email and
                        # fall through to create a new one!
                        if len(existing_html) < 50000:
                            # Wrap the new content in a div to prevent HubSpot's email thread truncation heuristic from hiding it
                            new_content = f"<div class='hs-continuous-log-entry' style='margin-top: 12px;'>{sanitize_for_hubspot(html_content)}</div>"

                            if "</body>" in existing_html.lower():
                                import re

                                new_html = re.sub(
                                    r"(?i)</body>",
                                    f"{new_content}</body>",
                                    existing_html,
                                )
                            else:
                                new_html = f"{existing_html}{new_content}"

                            new_text = f"{existing_text}\n\n{plain_text}"

                            update_props = {
                                "hs_email_html": new_html,
                                "hs_email_text": html.escape(new_text),
                                "hs_timestamp": int(
                                    datetime.now(UTC).timestamp() * 1000
                                ),
                            }
                            if sender_email or sender_name:
                                import json

                                headers = {"from": {}}
                                if sender_email:
                                    headers["from"]["email"] = sender_email
                                if sender_name:
                                    headers["from"]["firstName"] = sender_name
                                update_props["hs_email_headers"] = json.dumps(headers)

                            email_obj = await client.update_object(
                                "emails", str(matching_email["id"]), update_props
                            )

                            # Ensure the updated email is associated with any newly added contacts on the ticket
                            if (
                                associated_id
                                and associated_type
                                and normalize_object_type(associated_type)
                                in ("ticket", "deal", "company")
                            ):
                                contacts = await self.get_associated_objects(
                                    workspace_id=workspace_id,
                                    from_object_type=associated_type,
                                    object_id=associated_id,
                                    to_object_type="contact",
                                )
                                for contact in contacts:
                                    await self.associate_object(
                                        workspace_id=workspace_id,
                                        from_type="email",
                                        from_id=str(matching_email["id"]),
                                        to_type="contact",
                                        to_id=str(contact["id"]),
                                    )

                            # Invalidate the engagements cache so the updated email appears in search cards immediately
                            eng_key = (
                                f"engagements:{workspace_id}:ticket:{associated_id}"
                            )
                            await self._ENGAGEMENTS_CACHE.invalidate(eng_key)

                            return email_obj
            except Exception as e:
                logger.warning(
                    "Failed to lookup continuous email log for ticket=%s: %s. Proceeding with standard creation.",
                    associated_id,
                    e,
                )

        # Standard Creation
        create_props = {
            "hs_email_html": sanitize_for_hubspot(html_content),
            "hs_email_text": html.escape(plain_text),
            "hs_email_subject": subject,
            "hs_email_direction": "EMAIL",
            "hs_email_status": "SENT",
            "hs_timestamp": int(datetime.now(UTC).timestamp() * 1000),
        }
        if sender_email or sender_name:
            import json

            headers = {"from": {}}
            if sender_email:
                headers["from"]["email"] = sender_email
            if sender_name:
                headers["from"]["firstName"] = sender_name
            create_props["hs_email_headers"] = json.dumps(headers)

        email_obj = await client.create_object(
            "emails",
            create_props,
        )
        if associated_id and associated_type:
            try:
                await self.associate_object(
                    workspace_id=workspace_id,
                    from_type="email",
                    from_id=str(email_obj["id"]),
                    to_type=normalize_object_type(associated_type),
                    to_id=associated_id,
                )

                # Invalidate cache for new engagement
                eng_key = f"engagements:{workspace_id}:{normalize_object_type(associated_type)}:{associated_id}"
                await self._ENGAGEMENTS_CACHE.invalidate(eng_key)

                # If associating with a ticket/deal/company, also try to associate with its contacts
                # so it doesn't show up as "Unknown Contact" in the timeline.
                if normalize_object_type(associated_type) in (
                    "ticket",
                    "deal",
                    "company",
                ):
                    contacts = await self.get_associated_objects(
                        workspace_id=workspace_id,
                        from_object_type=associated_type,
                        object_id=associated_id,
                        to_object_type="contact",
                    )
                    for contact in contacts:
                        await self.associate_object(
                            workspace_id=workspace_id,
                            from_type="email",
                            from_id=str(email_obj["id"]),
                            to_type="contact",
                            to_id=str(contact["id"]),
                        )
            except Exception as e:
                logger.warning(
                    "Email activity created (id=%s) but association to %s (id=%s) failed: %s",
                    email_obj["id"],
                    associated_type,
                    associated_id,
                    e,
                )
        return email_obj

    async def publish_app_event(
        self,
        workspace_id: str,
        event_template_id: str,
        object_type: str,
        object_id: str,
        properties: dict[str, str],
    ) -> None:
        """Logs a custom app event to a record's timeline."""
        try:
            client = await self.get_client(workspace_id)
            await client.create_app_event(
                event_template_id=event_template_id,
                object_id=object_id,
                tokens=properties,
            )
        except Exception as e:
            logger.warning("Failed to publish app event: %s", e)

    async def send_thread_reply(
        self, workspace_id: str, thread_id: str, text: str
    ) -> dict[str, Any]:
        client = await self.get_client(workspace_id)
        return await client.create_inbox_message(thread_id=thread_id, text=text)

    async def get_associated_objects(
        self,
        workspace_id: str,
        from_object_type: str,
        object_id: str,
        to_object_type: str,
        prefetched_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Generic method to fetch and batch read any associated object type."""
        from_object_type = normalize_object_type(from_object_type)
        to_object_type = normalize_object_type(to_object_type)

        key = f"assoc:{workspace_id}:{from_object_type}:{object_id}:{to_object_type}"

        async def _fetch():
            client = await self.get_client(workspace_id)
            # API endpoints use plural forms (contacts, companies, deals, tickets)
            plural_to = pluralize_hs_type(to_object_type)

            props, target_name = self._ASSOC_PROPS.get(
                to_object_type, (["hs_object_id"], to_object_type)
            )

            if prefetched_ids is not None:
                assoc_ids = prefetched_ids
            else:
                assoc_ids = await client.get_associations(
                    from_object_type, object_id, to_object_type
                )

            if not assoc_ids:
                return []
            objects = await client.batch_read(
                plural_to, assoc_ids[:100], properties=props
            )
            for obj in objects:
                obj["type"] = target_name
            return await self.inject_urls(workspace_id, objects, target_name)

        return await self._ASSOCIATIONS_CACHE.get_or_fetch(key, _fetch)

    async def get_all_associations(
        self, workspace_id: str, object_type: str, object_id: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch all primary associations for an object.
        Consolidates multiple association ID lookups into a single
        object fetch to improve performance.
        """
        targets = ["contacts", "companies", "deals", "tickets"]
        hs_type = normalize_object_type(object_type)
        if hs_type in targets:
            targets.remove(hs_type)
        if hs_type == "contact" and "contacts" in targets:
            targets.remove("contacts")

        target_map = {t: t for t in targets}
        obj = await self.get_object(
            workspace_id=workspace_id,
            object_type=hs_type,
            object_id=object_id,
            associations=targets,
        )

        if not obj:
            return {t: [] for t in targets}

        # Check for associations in both raw HubSpot structure and our cached structure
        raw_assocs = obj.get("associations") or {}
        cached_assocs = obj.get("associated_objects") or {}

        if not raw_assocs and not cached_assocs:
            return {t: [] for t in targets}

        async def _fetch_details(plural_type: str):
            client = await self.get_client(workspace_id)
            # Try to get IDs from raw associations or cached associations
            ids = []
            singular = normalize_object_type(plural_type)
            if plural_type in raw_assocs or singular in raw_assocs:
                data = raw_assocs.get(plural_type) or raw_assocs.get(singular) or {}
                results = data.get("results", [])
                ids = [str(r["id"]) for r in results]
            elif plural_type in cached_assocs or singular in cached_assocs:
                # Our cached structure stores full objects
                cached_data = cached_assocs.get(plural_type) or []
                objects = cached_data or cached_assocs.get(singular) or []
                ids = [str(o["id"]) for o in objects]

            if not ids:
                # Fallback: manual ID lookup if not pre-fetched
                ids = await client.get_associations(hs_type, object_id, singular)

            if not ids:
                return []

            singular = normalize_object_type(plural_type)
            props, target_name = self._ASSOC_PROPS.get(
                singular, (["hs_object_id"], singular)
            )

            objects = await client.batch_read(plural_type, ids[:100], properties=props)
            for o in objects:
                o["type"] = target_name
            return await self.inject_urls(workspace_id, objects, target_name)

        tasks = {}
        for target, plural in target_map.items():
            tasks[target] = _fetch_details(plural)

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        final_associations = {t: [] for t in targets}
        for target, res in zip(tasks.keys(), results, strict=False):
            if isinstance(res, Exception):
                logger.error(f"Failed to fetch {target} details: {res}")
            else:
                final_associations[target] = cast(list[dict[str, Any]], res)

        return final_associations

    async def get_owners(self, workspace_id: str) -> list[dict[str, Any]]:
        """Fetches all HubSpot owners with caching."""
        cache_key = f"owners:{workspace_id}"

        async def _fetch():
            client = await self.get_client(workspace_id)
            return await client.get_owners()

        results = await self._OWNERS_CACHE.get_or_fetch(cache_key, _fetch)
        return cast(list[dict[str, Any]], results)

    async def get_owner(
        self, workspace_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        """Fetches a single owner, falling back to direct API if not in cache."""
        owners = await self.get_owners(workspace_id)
        owner = next((o for o in owners if o["id"] == owner_id), None)

        if owner:
            return owner

        logger.info(
            "HubSpot owner %s not in cache for workspace %s. Fetching directly.",
            owner_id,
            workspace_id,
        )
        client = await self.get_client(workspace_id)
        new_owner = await client.get_owner(owner_id)

        if new_owner:
            cache_key = f"owners:{workspace_id}"
            updated_owners = list(owners)
            updated_owners.append(new_owner)
            await self._OWNERS_CACHE.set(cache_key, updated_owners)
            return new_owner

        return None

    async def resolve_owner_display_name(
        self, workspace_id: str, owner_id: str | None
    ) -> str | None:
        """Consolidated helper to fetch and format an owner name."""
        if not owner_id:
            return None
        try:
            owner = await self.get_owner(workspace_id, owner_id)
            return self.format_owner_name(owner)
        except Exception:
            logger.warning(
                "Failed to resolve owner %s for workspace %s", owner_id, workspace_id
            )
            return None

    def format_owner_name(self, owner: HubSpotOwner | dict[str, Any] | None) -> str:
        """Standardizes owner display name from First/Last or Email."""
        if not owner:
            return "Unknown Owner"

        first = owner.get("firstName", "")
        last = owner.get("lastName", "")
        name = f"{first} {last}".strip()

        return name or owner.get("email") or "Unknown Owner"

    async def get_owner_by_email(
        self, workspace_id: str, email: str | None
    ) -> dict[str, Any] | None:
        """Resolves an owner record from an email address."""
        if not email:
            return None

        owners = await self.get_owners(workspace_id)
        return next(
            (o for o in owners if (o.get("email") or "").lower() == email.lower()),
            None,
        )

    async def enrich_task(
        self, workspace_id: str, task: dict[str, Any]
    ) -> dict[str, Any]:
        props = task.get("properties", {})
        context: dict[str, Any] = {
            "owner_name": "Unassigned",
            "contacts": [],
            "companies": [],
        }
        owner_id = props.get("hubspot_owner_id")
        if owner_id:
            owner = await self.get_owner(workspace_id, owner_id)
            context["owner_name"] = self.format_owner_name(owner)

        assoc_contacts = await self.get_associated_objects(
            workspace_id, "tasks", task["id"], "contacts"
        )
        for c in assoc_contacts:
            c_props = c.get("properties", {})
            name = (
                f"{c_props.get('firstname', '')} {c_props.get('lastname', '')}".strip()
            )
            context["contacts"].append(
                name or c_props.get("email") or "Unknown Contact"
            )

        assoc_companies = await self.get_associated_objects(
            workspace_id, "tasks", task["id"], "companies"
        )
        for c in assoc_companies:
            c_props = c.get("properties", {})
            context["companies"].append(
                c_props.get("name") or c_props.get("domain") or "Unknown Company"
            )
        return context

    async def get_contact_meetings(
        self, workspace_id: str, contact_id: str
    ) -> list[dict[str, Any]]:
        client = await self.get_client(workspace_id)
        meeting_ids = await client.get_associations("contact", contact_id, "meeting")
        if not meeting_ids:
            return []
        meetings = await client.batch_read(
            "meetings",
            meeting_ids[:100],
            properties=[
                "hs_meeting_title",
                "hs_meeting_start_time",
                "hs_meeting_end_time",
                "hs_meeting_outcome",
            ],
        )
        for meeting in meetings:
            meeting["type"] = "meeting"
        return meetings

    async def create_meeting(
        self,
        workspace_id: str,
        properties: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        client = await self.get_client(workspace_id)
        associated_id = kwargs.get("associated_id")
        associated_type = kwargs.get("associated_type")

        # 1. Create meeting object first (no in-line associations)
        meeting = await client.create_object("meetings", properties)

        # 2. Add association separately (more robust)
        if associated_id and associated_type:
            try:
                await self.associate_object(
                    workspace_id=workspace_id,
                    from_type="meeting",
                    from_id=str(meeting["id"]),
                    to_type=normalize_object_type(associated_type),
                    to_id=associated_id,
                )
            except Exception:
                logger.warning(
                    "Meeting created (id=%s) but association to %s (id=%s) failed",
                    meeting["id"],
                    associated_type,
                    associated_id,
                )

        return meeting

    async def update_object(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        properties: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Generic update for any HubSpot object type."""
        client = await self.get_client(workspace_id)
        return await client.update_object(
            object_type=normalize_object_type(object_type),
            object_id=object_id,
            properties=properties,
        )

    async def get_object_with_client(
        self,
        client: Any,
        object_type: str,
        object_id: str,
    ) -> Mapping[str, Any] | None:
        """Fetch an object using a pre-authenticated client."""
        # Force cast to the specialized client type
        hs_client = cast(HubSpotClient, client)
        return await hs_client.get_object(
            object_type=normalize_object_type(object_type),
            object_id=object_id,
        )

    async def uninstall_app(self, workspace_id: str) -> None:
        client = await self.get_client(workspace_id)
        await client.uninstall_app()
