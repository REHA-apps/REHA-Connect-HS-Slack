from __future__ import annotations  # noqa: D100

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.base_client import BaseClient
from app.core.config import settings
from app.core.exceptions import HubSpotAPIError, RateLimitError
from app.core.logging import CorrelationAdapter, get_logger
from app.utils.transformers import to_hubspot_timestamp

logger = get_logger("hubspot.client")

type TokenRefreshCallback = Callable[[str, str | None, int | None], Awaitable[None]]
type TokenRevocationCallback = Callable[[], Awaitable[None]]


class HubSpotClient(BaseClient):
    """Asynchronous HTTP client for interacting with the HubSpot CRM v3 API.

    Handles OAuth token refreshing, providing typed convenience methods for
    common CRM object operations with correlation-aware logging.
    """

    # HubSpot Note Association Type IDs (Note → Object)
    _NOTE_ASSOC_TYPE_IDS: dict[str, int] = {
        "contact": 202,
        "deal": 214,
        "company": 190,
        "ticket": 228,
        "task": 204,
    }

    # HubSpot Meeting Association Type IDs (Meeting → Object)
    # 200: Meeting to Contact
    # 212: Meeting to Deal
    # 188: Meeting to Company
    # 226: Meeting to Ticket
    _MEETING_ASSOC_TYPE_IDS: dict[str, int] = {
        "contact": 200,
        "deal": 212,
        "company": 188,
        "ticket": 226,
        "task": 216,
    }

    # Centralized search properties registry
    _SEARCH_PROPS: dict[str, list[str]] = {
        "contacts": [
            "email",
            "firstname",
            "lastname",
            "company",
            "lifecyclestage",
            "hs_analytics_num_visits",
            "hs_additional_emails",
            "phone",
            "mobilephone",
        ],
        "deals": ["dealname", "amount", "dealstage", "pipeline"],
        "leads": [
            "firstname",
            "lastname",
            "email",
            "company",
            "lifecyclestage",
            "hs_lead_status",
            "phone",
            "hubspotscore",
        ],
        "companies": [
            "name",
            "domain",
            "industry",
            "city",
            "state",
            "country",
            "lifecyclestage",
            "num_associated_contacts",
            "num_associated_deals",
            "hs_analytics_num_page_views",
            "hs_analytics_num_visits",
            "lastmodifieddate",
            "hs_lastmodifieddate",
        ],
        "tickets": [
            "subject",
            "content",
            "hs_pipeline_stage",
            "hs_ticket_priority",
            "createdate",
            "hs_ticket_category",
        ],
        "tasks": [
            "hs_task_subject",
            "hs_task_body",
            "hs_task_status",
            "hs_task_priority",
            "hs_task_type",
            "hs_timestamp",
        ],
        "meetings": [
            "hs_meeting_title",
            "hs_meeting_body",
            "hs_meeting_start_time",
            "hs_meeting_end_time",
            "hs_meeting_outcome",
        ],
    }

    # Centralized detail properties registry (preserved from individual methods)
    _DETAIL_PROPS: dict[str, list[str]] = {
        "contacts": [
            "firstname",
            "lastname",
            "email",
            "phone",
            "lifecyclestage",
            "company",
            "hs_analytics_num_visits",
            "hubspot_owner_id",
        ],
        "deals": [
            "dealname",
            "amount",
            "pipeline",
            "dealstage",
            "hs_next_step",
            "hubspot_owner_id",
        ],
        "companies": [
            "name",
            "domain",
            "industry",
            "num_associated_contacts",
            "num_associated_deals",
            "hs_analytics_num_page_views",
            "hs_analytics_num_visits",
            "hubspot_owner_id",
        ],
        "tickets": [
            "subject",
            "content",
            "hs_pipeline_stage",
            "hs_ticket_priority",
            "hs_ticket_category",
        ],
        "leads": [
            "hs_lead_status",
            "firstname",
            "lastname",
            "company",
        ],
        "tasks": [
            "hs_task_subject",
            "hs_task_body",
            "hs_task_status",
            "hs_task_priority",
            "hs_task_type",
            "hs_timestamp",
            "hubspot_owner_id",
        ],
        "notes": ["hs_note_body", "hs_timestamp"],
        "calls": ["hs_call_title", "hs_call_body", "hs_call_status", "hs_timestamp"],
        "emails": [
            "hs_email_subject",
            "hs_email_text",
            "hs_email_html",
            "hs_timestamp",
        ],
        "meetings": [
            "hs_meeting_title",
            "hs_meeting_body",
            "hs_meeting_start_time",
            "hs_meeting_end_time",
            "hs_meeting_outcome",
        ],
    }

    @classmethod
    def search_props_for(cls, object_type: str) -> list[str]:
        """Returns the canonical property list for a given CRM object type."""
        return cls._SEARCH_PROPS.get(object_type, [])

    def __init__(
        self,
        corr_id: str,
        access_token: str,
        refresh_token: str | None,
        hub_domain: str | None = None,
        portal_id: str | None = None,
        slack_ts: str | None = None,
        expires_at: int | None = None,
    ) -> None:
        self.corr_id = corr_id
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.hub_domain = hub_domain or "api.hubapi.com"
        self.portal_id = portal_id
        self.slack_ts = slack_ts
        self.expires_at = expires_at
        self._refresh_lock = asyncio.Lock()

        # 4.5 Performance: Consolidate host logic into helper
        from app.utils.helpers import get_hub_api_host

        base_host = get_hub_api_host(self.hub_domain)

        # Optional callback: (new_access_token, new_refresh_token) -> None
        self.on_token_refresh: TokenRefreshCallback | None = None

        self.on_token_revoked: TokenRevocationCallback | None = None

        super().__init__(
            base_url=base_host,
            headers=self._headers(
                access_token,
                corr_id=corr_id,
                portal_id=portal_id,
                slack_ts=slack_ts,
            ),
            corr_id=corr_id,
        )

        self.log = CorrelationAdapter(logger, self.corr_id)

    # API request orchestration
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        description: str | None = None,
    ) -> Any:
        """Makes an HTTP request to the HubSpot API, handling token refresh if needed.

        Args:
            method (str): The HTTP method (e.g., "GET", "POST").
            path (str): The API endpoint path.
            params (Mapping[str, Any] | None): Optional query parameters.
            json (Mapping[str, Any] | None): Optional JSON body for the request.
            data (Mapping[str, Any] | None): Optional form data for the request.
            description (str | None): Optional human-readable name for logging.

        Returns:
            Any: The JSON response from the API, or None for 404 GET requests.

        Raises:
            httpx.HTTPStatusError: If the request fails with a non-2xx status code
                                   after potential token refresh.

        """
        # 1. Proactive Refresh: If token expires in < 5 mins, refresh now
        if self.expires_at and self.refresh_token:
            if (self.expires_at - int(time.time())) < 300:  # 5 minute window
                async with self._refresh_lock:
                    # Double-check inside lock to prevent redundant refreshes
                    time_left = self.expires_at - int(time.time())
                    if time_left < 300:
                        self.log.info(
                            "Proactive HubSpot refresh triggered (expires in %ds)",
                            time_left,
                        )
                        await self._do_refresh()

        try:
            return await self._raw_request(
                method,
                path,
                params=params,
                json=json,
                data=data,
                description=description,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401 and self.refresh_token:
                # Use a lock to ensure only one refresh happens
                # in high-concurrency bursts.
                async with self._refresh_lock:
                    # Double-check: token may have already been refreshed
                    # by another concurrent request (double-check pattern).
                    if (
                        exc.request.headers.get("Authorization")
                        != f"Bearer {self.access_token}"
                    ):
                        self.log.debug(
                            "Token already refreshed by another task; retrying request"
                        )
                        return await self._raw_request(
                            method,
                            path,
                            params=params,
                            json=json,
                            data=data,
                            description=description,
                        )

                    self.log.info("HubSpot token expired; attempting reactive refresh")
                    await self._do_refresh()
                    return await self._raw_request(
                        method,
                        path,
                        params=params,
                        json=json,
                        data=data,
                        description=description,
                    )

            if exc.response.status_code == 429:
                retry_after = exc.response.headers.get("Retry-After", "10")
                raise RateLimitError(
                    "HubSpot rate limit exceeded",
                    details={"retry_after": int(retry_after)},
                ) from exc

            raise HubSpotAPIError(
                message=f"HubSpot API error: {exc.response.text}",
                status_code=exc.response.status_code,
            ) from exc

    # Low-level request handling
    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        description: str | None = None,
    ) -> Any:
        """Executes a raw HTTP request to the HubSpot API.

        Args:
            method (str): The HTTP method (e.g., "GET", "POST").
            path (str): The API endpoint path. Can be a full URL or relative path.
            params (Mapping[str, Any] | None): Optional query parameters.
            json (Mapping[str, Any] | None): Optional JSON body for the request.
            data (Mapping[str, Any] | None): Optional form data for the request.
            description (str | None): Optional human-readable name for logging.

        Returns:
            Any: The JSON response from the API, or None for 404 GET requests.

        Raises:
            httpx.HTTPStatusError: If the request fails with a non-2xx status code.
            ValueError: If the response is not valid JSON.

        """
        if path.startswith("http"):
            url = path
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"

        # Execute request via BaseClient (handles retries, masking, and logging)

        response = await super().request(
            method=method,
            path=path,
            params=params,
            json=json,
            data=data,
            description=description or f"HubSpot {method} {path[:30]}",
        )

        if method.upper() == "GET" and response.status_code == 404:
            self.log.debug("HubSpot GET %s returned 404 (None)", url)
            return None

        if response.status_code >= 400:
            if "timeline/events" in url:
                # Timeline events frequently error if templates aren't fully set up.
                # We log as warning/info instead of error to avoid noise in logs.
                msg = f"HubSpot result (status={response.status_code}): {response.text}"
                if response.status_code >= 500:
                    self.log.warning(msg)
                else:
                    self.log.info(msg)
            elif response.status_code == 401:
                self.log.debug("HubSpot 401 Unauthorized (likely expired token)")
            else:
                self.log.error(
                    "HubSpot error %s: %s", response.status_code, response.text
                )
            response.raise_for_status()

        if response.status_code == 204:
            return {}

        try:
            return response.json()
        except ValueError:
            self.log.error("Invalid JSON response from HubSpot: %s", response.text)
            raise

    async def _do_refresh(self) -> None:
        """Internal helper to orchestrate token refresh and persistence."""
        import time

        new_tokens = await self._refresh_token()
        if new_tokens:
            new_at = new_tokens["access_token"]
            new_rt = new_tokens.get("refresh_token")
            expires_in = new_tokens.get("expires_in", 1800)  # Default 30m if missing
            new_expires_at = int(time.time()) + expires_in

            # Update in-memory state
            self.access_token = new_at
            if new_rt:
                self.refresh_token = new_rt
            self.expires_at = new_expires_at

            # Update headers for immediate re-use
            self.headers = self._headers(
                new_at,
                corr_id=self.corr_id,
                portal_id=self.portal_id,
                slack_ts=self.slack_ts,
            )

            # Notify persistence listeners (Service layer)
            if self.on_token_refresh:
                try:
                    result = self.on_token_refresh(new_at, new_rt, new_expires_at)
                    if isinstance(result, Awaitable):
                        await result
                except Exception as exc:
                    self.log.error(
                        "on_token_refresh callback raised an exception: %s",
                        exc,
                        exc_info=True,
                    )
        else:
            # Add a short backoff (10s) to prevent spamming the refresh endpoint
            # if the token is permanently invalid or HubSpot is temporarily down.
            self.log.warning("Token refresh failed. Backing off for 10 seconds.")
            self.expires_at = int(time.time()) + 10

    # OAuth token refresh
    async def _refresh_token(self) -> dict[str, Any] | None:
        """Attempts to refresh the HubSpot access token using the refresh token.

        Returns:
            dict[str, Any] | None: The raw OAuth response payload or None.

        """
        from app.utils.helpers import get_hub_api_host

        base_host = get_hub_api_host(self.hub_domain)
        url = f"{base_host}/oauth/v1/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": settings.HUBSPOT_CLIENT_ID,
            "client_secret": settings.HUBSPOT_CLIENT_SECRET.get_secret_value(),
            "refresh_token": self.refresh_token,
        }

        client = self.get_client()

        try:
            resp = await client.post(url, data=data)
        except Exception as exc:
            self.log.error("HubSpot refresh request failed: %s", exc, exc_info=True)
            return None

        if resp.status_code != 200:
            self.log.error(
                "HubSpot token refresh failed: status=%s body=%s",
                resp.status_code,
                resp.text,
            )

            # Detect revocation (user uninstalled app from HubSpot portal)
            if resp.status_code == 400:
                try:
                    error_data = resp.json()
                    if error_data.get("error") == "invalid_grant":
                        self.log.warning(
                            "HubSpot token revoked (invalid_grant); triggering callback"
                        )
                        if self.on_token_revoked:
                            result = self.on_token_revoked()
                            if isinstance(result, Awaitable):
                                await result
                except Exception as exc:
                    self.log.error(
                        "Failed to parse HubSpot refresh error: %s", exc, exc_info=True
                    )

            return None

        payload = resp.json()
        if "access_token" not in payload:
            self.log.error("HubSpot refresh response missing access_token")
            return None

        return payload

    async def get_token_info(self) -> dict[str, Any]:
        """Fetch information about the current access token (user, scopes, etc.).
        Endpoint: GET /oauth/v1/access-tokens/{token}
        """
        return await self.request(
            "GET",
            f"oauth/v1/access-tokens/{self.access_token}",
            description="HubSpot: Get Token Info",
        )

    # ---------------------------------------------------------
    # Headers
    # ---------------------------------------------------------
    @staticmethod
    def _headers(
        token: str,
        corr_id: str | None = None,
        portal_id: str | None = None,
        slack_ts: str | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Hubspot-Api-Version": settings.HUBSPOT_API_VERSION,
            "User-Agent": f"REHA Connect/{settings.APP_VERSION} (HubSpot Marketplace)",
        }
        if corr_id:
            headers["X-Correlation-ID"] = corr_id
        if portal_id:
            headers["X-Hubspot-Portal-Id"] = portal_id
        if slack_ts:
            headers["X-Slack-Thread-Ts"] = slack_ts
        return headers

    # Generic CRM operations
    async def create_object(
        self,
        object_type: str,
        properties: Mapping[str, Any],
        associations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Generic object creation using the 2026.03 date-based API."""
        payload: dict[str, Any] = {"properties": properties}
        if associations:
            payload["associations"] = associations

        return await self.request(
            "POST",
            f"crm/objects/{settings.HUBSPOT_API_VERSION}/{object_type}",
            json=payload,
            description=f"HubSpot: Create {object_type}",
        )

    async def create_note(
        self,
        content: str,
        associated_id: str,
        associated_type: str,
    ) -> dict[str, Any]:
        """Creates a CRM note and associates it with a contact, deal, or company."""
        type_id = self._NOTE_ASSOC_TYPE_IDS.get(associated_type.lower(), 202)  # noqa: PLR2004

        properties = {
            "hs_note_body": content,
            "hs_timestamp": to_hubspot_timestamp(
                datetime.now(UTC), corr_id=self.corr_id
            ),
        }

        associations = [
            {
                "to": {"id": associated_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": type_id,
                    }
                ],
            }
        ]

        return await self.create_object("notes", properties, associations)

    async def create_app_event(
        self,
        event_template_id: str,
        object_id: str,
        tokens: dict[str, str],
    ) -> dict[str, Any]:
        """Posts a custom app event (timeline event) to a specific CRM object."""
        payload = {
            "eventTemplateId": event_template_id,
            "objectId": object_id,
            "tokens": tokens,
            "extraData": {"source": "Slack Connector App"},
        }

        # Use the existing authenticated request wrapper
        # Note: 2026-03 unified Timeline into the 'events' API.
        # We pin to v3 to maintain payload compatibility with the old timeline extensions.
        return await self.request("POST", "crm/timeline/v3/events", json=payload)

    async def get_object(
        self,
        object_type: str,
        object_id: str,
        properties: list[str] | None = None,
        associations: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Generic object retrieval with optional properties and associations."""
        path = f"objects/{object_type}/{object_id}"  # noqa: F841
        params: dict[str, Any] = {}
        if properties:
            params["properties"] = ",".join(properties)
        if associations:
            params["associations"] = ",".join(associations)

        return await self.request(
            "GET",
            f"crm/objects/{settings.HUBSPOT_API_VERSION}/{object_type}/{object_id}",
            params=params,
            description=f"HubSpot: Get {object_type} {object_id}",
        )

    async def update_object(
        self,
        object_type: str,
        object_id: str,
        properties: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Updates a CRM object using the 2026.03 date-based API."""
        return await self.request(
            "PATCH",
            f"crm/objects/{settings.HUBSPOT_API_VERSION}/{object_type}/{object_id}",
            json={"properties": properties},
            description=f"HubSpot: Update {object_type} {object_id}",
        )

    async def search_objects(
        self,
        object_type: str,
        filters: list[dict[str, Any]] | None = None,
        filter_groups: list[dict[str, Any]] | None = None,
        properties: list[str] | None = None,
        limit: int = 5,
        query_string: str | None = None,
    ) -> list[dict[str, Any]]:
        """Generic CRM 2026.03 search."""
        # If query_string is provided, use it for broad "query" search (smart match)
        # Note: If filters/filterGroups are also provided, HubSpot API might
        # ignore query or combine them.
        # But for our use case, we usually want EITHER smart query OR specific filters.

        groups = []
        if filter_groups:
            groups = filter_groups
        elif filters:
            groups = [{"filters": filters}]

        payload: dict[str, Any] = {
            "limit": limit,
            "properties": properties or [],
        }

        if query_string:
            payload["query"] = query_string
        elif groups:
            payload["filterGroups"] = groups

        # If neither query nor filters provided, empty search.
        # Search API requires at least one condition usually.
        # But an empty query string "" might return recent.

        try:
            resp = await self.request(
                "POST",
                f"crm/objects/{settings.HUBSPOT_API_VERSION}/{object_type}/search",
                json=payload,
                description=f"HubSpot: Search {object_type}",
            )
        except Exception as exc:
            # Gracefully handle 403 (missing scopes) and other API errors
            exc_str = str(exc)
            if "403" in exc_str or "MISSING_SCOPES" in exc_str:
                self.log.warning(
                    "Missing HubSpot scopes for %s search — skipping. "
                    "Re-install the app to grant required permissions.",
                    object_type,
                )
                return []
            raise
        return resp.get("results", [])

    # Convenience object helpers
    async def get_contact(
        self, object_id: str, associations: list[str] | None = None
    ) -> dict[str, Any] | None:
        return await self.get_object(
            "contacts",
            object_id,
            properties=self._DETAIL_PROPS["contacts"],
            associations=associations,
        )

    async def get_deal(
        self, object_id: str, associations: list[str] | None = None
    ) -> dict[str, Any] | None:
        return await self.get_object(
            "deals",
            object_id,
            properties=self._DETAIL_PROPS["deals"],
            associations=associations,
        )

    async def get_company(
        self, object_id: str, associations: list[str] | None = None
    ) -> dict[str, Any] | None:
        return await self.get_object(
            "companies",
            object_id,
            properties=self._DETAIL_PROPS["companies"],
            associations=associations,
        )

    async def create_contact(self, properties: Mapping[str, Any]) -> dict[str, Any]:
        return await self.create_object("contacts", properties)

    async def create_task(self, properties: Mapping[str, Any]) -> dict[str, Any]:
        return await self.create_object("tasks", properties)

    # Contact search logic
    async def search_contacts(self, query: str) -> list[dict[str, Any]]:
        q = query.strip().lower()

        # 1. Try CRM search first
        results = await self.search_objects(
            "contacts", query_string=q, properties=self._SEARCH_PROPS["contacts"]
        )

        if results:
            return results

        # 2. Fallback: identity profile lookup (email → contactId)
        try:
            identity_resp = await self.request(
                "GET",
                f"crm/objects/{settings.HUBSPOT_API_VERSION}/contacts/{q}",
                params={"idProperty": "email"},
                description="HubSpot: Email to ID Lookup",
            )
            return [identity_resp] if identity_resp else []
        except Exception:
            return []

    # Deal search logic
    async def search_deals(self, query: str) -> list[dict[str, Any]]:
        return await self.search_objects(
            "deals", query_string=query, properties=self._SEARCH_PROPS["deals"]
        )

    # Lead search logic
    async def search_leads(self, query: str) -> list[dict[str, Any]]:
        return await self.search_objects(
            "leads", query_string=query, properties=self._SEARCH_PROPS["leads"]
        )

    # Company search logic
    async def search_companies(self, query: str) -> list[dict[str, Any]]:
        return await self.search_objects(
            "companies", query_string=query, properties=self._SEARCH_PROPS["companies"]
        )

    async def get_pipelines(self, object_type: str) -> list[dict[str, Any]]:
        """Fetch pipelines for a specific object type (deals, tickets)."""
        data = await self.request(
            "GET",
            f"crm/pipelines/{settings.HUBSPOT_API_VERSION}/{object_type}",
            description=f"HubSpot: Get {object_type} Pipelines",
        )
        if not data:
            return []
        return data.get("results", [])

    async def get_deal_pipelines(self) -> list[dict[str, Any]]:
        """Fetch all deal pipelines and their stages."""
        return await self.get_pipelines("deals")

    async def update_deal(
        self, object_id: str, properties: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Update properties of a deal."""
        return await self.request(
            "PATCH",
            f"crm/objects/{settings.HUBSPOT_API_VERSION}/deals/{object_id}",
            json={"properties": properties},
            description=f"HubSpot: Update Deal {object_id}",
        )

    async def update_contact(
        self, object_id: str, properties: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Update properties of a contact."""
        return await self.request(
            "PATCH",
            f"crm/objects/2026-03/contacts/{object_id}",
            json={"properties": properties},
            description=f"HubSpot: Update Contact {object_id}",
        )

    async def update_company(
        self, object_id: str, properties: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Update properties of a company."""
        return await self.request(
            "PATCH",
            f"crm/objects/2026-03/companies/{object_id}",
            json={"properties": properties},
            description=f"HubSpot: Update Company {object_id}",
        )

    # Ticket search logic
    async def search_tickets(self, query: str) -> list[dict[str, Any]]:
        return await self.search_objects(
            "tickets", query_string=query, properties=self._SEARCH_PROPS["tickets"]
        )

    # Task search logic
    async def search_tasks(self, query: str) -> list[dict[str, Any]]:
        return await self.search_objects(
            "tasks", query_string=query, properties=self._SEARCH_PROPS["tasks"]
        )

    # Files/Documents search logic
    async def search_files(self, query: str) -> list[dict[str, Any]]:
        """Search for files/documents."""
        # Files search API is limited. We'll try to find by name if possible,
        # otherwise we might need to rely on list and filter (inefficient).
        # Official endpoint: GET /files/v3/files/search
        # HubSpot Files API search is not well-documented for content search.
        # Raise explicitly until a proper implementation is added.
        raise NotImplementedError(
            "search_files is not yet implemented — "
            "HubSpot Files API search support is limited"
        )

    async def get_ticket(
        self, object_id: str, associations: list[str] | None = None
    ) -> dict[str, Any] | None:
        return await self.get_object(
            "tickets",
            object_id,
            properties=self._DETAIL_PROPS["tickets"],
            associations=associations,
        )

    async def get_note(self, object_id: str) -> dict[str, Any] | None:
        return await self.get_object(
            "notes",
            object_id,
            properties=self._DETAIL_PROPS["notes"],
        )

    async def get_call(self, object_id: str) -> dict[str, Any] | None:
        return await self.get_object(
            "calls",
            object_id,
            properties=self._DETAIL_PROPS["calls"],
        )

    async def get_email(self, object_id: str) -> dict[str, Any] | None:
        return await self.get_object(
            "emails",
            object_id,
            properties=self._DETAIL_PROPS["emails"],
        )

    async def get_lead(self, object_id: str) -> dict[str, Any] | None:
        return await self.get_object(
            "leads",
            object_id,
            properties=self._DETAIL_PROPS["leads"],
        )

    async def get_owners(self) -> list[dict[str, Any]]:
        """Fetch all owners."""
        data = await self.request(
            "GET",
            f"crm/owners/{settings.HUBSPOT_API_VERSION}",
            description="HubSpot: Get All Owners",  # noqa: E501
        )
        return data.get("results", [])

    async def get_owner(self, owner_id: str) -> dict[str, Any] | None:
        """Fetch a single owner by ID."""
        try:
            return await self.request(
                "GET",
                f"crm/owners/{settings.HUBSPOT_API_VERSION}/{owner_id}",
                description=f"HubSpot: Get Owner {owner_id}",
            )
        except Exception:
            self.log.error("Failed to fetch HubSpot owner %s", owner_id)
            return None

    async def get_task(self, object_id: str) -> dict[str, Any] | None:
        return await self.get_object(
            "tasks",
            object_id,
            properties=self._DETAIL_PROPS["tasks"],
        )

    async def get_associations(
        self,
        from_object_type: str,
        object_id: str,
        to_object_type: str,
    ) -> list[str]:
        """Fetch associated object IDs via CRM v4 Associations API."""
        # Ensure regional domain is used for v4
        base_host = self.hub_domain
        if not base_host.startswith("http"):
            base_host = f"https://{base_host}"
        base_host = base_host.rstrip("/")

        url = (
            f"{base_host}/crm/objects/2026-03/"
            f"{from_object_type}/{object_id}/associations/{to_object_type}"
        )
        resp = await self.request(
            "GET", url, description=f"HubSpot: Get {from_object_type} Associations"
        )
        results = resp.get("results", [])
        return [str(r.get("toObjectId", r.get("id", ""))) for r in results]

    async def batch_read(
        self,
        object_type: str,
        object_ids: list[str],
        properties: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch multiple CRM objects in a single batch read call.

        Uses POST /crm/objects/2026-03/{objectType}/batch/read to avoid
        N+1 individual GET requests.
        """
        if not object_ids:
            return []

        payload: dict[str, Any] = {
            "inputs": [{"id": oid} for oid in object_ids],
        }
        if properties:
            payload["properties"] = properties

        try:
            resp = await self.request(
                "POST",
                f"crm/objects/{settings.HUBSPOT_API_VERSION}/{object_type}/batch/read",
                json=payload,
                description=f"HubSpot: Batch Read {object_type} ({len(object_ids)})",
            )
            return resp.get("results", [])
        except Exception as exc:
            self.log.error(
                "Batch read failed for %s (%d ids): %s",
                object_type,
                len(object_ids),
                exc,
            )
            return []

    async def get_account_details(self) -> dict[str, Any]:
        """Fetch account details, including portalId."""
        # Verified endpoint: GET /account-info/v3/details
        return await self.request(
            "GET",
            f"account-info/{settings.HUBSPOT_API_VERSION}/details",
            description="HubSpot: Get Account Details",
        )

    async def uninstall_app(self) -> None:
        """Uninstalls the app from the HubSpot account using the current session.
        Endpoint: DELETE /appinstalls/v3/external-install
        """
        await self.request(
            "DELETE",
            "https://api.hubapi.com/appinstalls/v3/external-install",
            description="HubSpot: Uninstall App",
        )

    # -----------------------------
    # Conversations Inbox API
    # -----------------------------
    async def get_inbox_thread(self, thread_id: str) -> dict[str, Any]:
        """Fetch a conversation thread by ID."""
        return await self.request(
            "GET",
            f"conversations/conversations/{settings.HUBSPOT_API_VERSION}/threads/{thread_id}",
            description=f"HubSpot: Get Inbox Thread {thread_id}",
        )

    async def create_inbox_message(
        self,
        thread_id: str,
        text: str,
        sender_actor_id: str | None = None,
        sender_type: str = "Responded by Bot",
        channel_id: str | None = None,
        channel_account_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a message to a conversation thread.

        Args:
            thread_id: The ID of the thread.
            text: The message content.
            sender_actor_id: (Optional) The specific actor ID sending the message.

        Note:
            Creating a message usually requires an Actor.
            If generic, we might need to find a fallback actor.
            The standard endpoint is POST
                /conversations/v3/conversations/threads/{threadId}/messages

        """
        # Construction of the payload is complex for Conversations.
        # Minimal payload:
        # {
        #   "type": "MESSAGE",
        #   "text": "...",
        #   "richText": "...",
        #   "senderActorId": "..."
        # }
        # senderActorId is usually required if type is MESSAGE.

        payload: dict[str, Any] = {
            "type": "MESSAGE",
            "text": text,
            "richText": f"<p>{text}</p>",  # Basic HTML support
        }

        if sender_actor_id:
            payload["senderActorId"] = sender_actor_id

        # If no senderActorId, HubSpot API might reject or assign to default?
        # We'll see. If it fails, we might need to fetch actors first.

        return await self.request(
            "POST",
            f"conversations/conversations/{settings.HUBSPOT_API_VERSION}/threads/{thread_id}/messages",
            json=payload,
            description=f"HubSpot: Post Inbox Message {thread_id}",
        )

    async def get_meeting(
        self,
        object_id: str,
        properties: list[str] | None = None,
    ) -> dict[str, Any] | None:
        return await self.get_object(
            "meetings",
            object_id,
            properties=properties or self._DETAIL_PROPS["meetings"],
        )

    async def get_meetings(
        self,
        object_id: str,
        properties: list[str] | None = None,
    ) -> dict[str, Any] | None:
        return await self.get_meeting(object_id, properties)

    async def search_meetings(self, query: str) -> list[dict[str, Any]]:
        return await self.search_objects(
            "meetings", query_string=query, properties=self._SEARCH_PROPS["meetings"]
        )
