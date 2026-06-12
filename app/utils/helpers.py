from __future__ import annotations  # noqa: D100

from functools import lru_cache
from typing import Any, Final

import httpx
from fastapi import Request

from app.core.logging import CorrelationAdapter, corr_id_ctx, get_logger

logger = get_logger("utils.http")


# HubSpot Object Type IDs (Singular → ID)
HS_CONTACT_TYPE_ID: Final[str] = "0-1"
HS_COMPANY_TYPE_ID: Final[str] = "0-2"
HS_DEAL_TYPE_ID: Final[str] = "0-3"
HS_TICKET_TYPE_ID: Final[str] = "0-5"
HS_NOTE_TYPE_ID: Final[str] = "0-9"
HS_TASK_TYPE_ID: Final[str] = "0-27"
HS_MEETING_TYPE_ID: Final[str] = "0-47"
HS_CALL_TYPE_ID: Final[str] = "0-48"
HS_EMAIL_TYPE_ID: Final[str] = "0-49"
HS_LEAD_TYPE_ID: Final[str] = "0-136"

_HS_TYPE_MAP: Final[dict[str, str]] = {
    HS_CONTACT_TYPE_ID: "contact",
    HS_COMPANY_TYPE_ID: "company",
    HS_DEAL_TYPE_ID: "deal",
    "0-4": "note",
    HS_TICKET_TYPE_ID: "ticket",
    HS_NOTE_TYPE_ID: "note",
    HS_TASK_TYPE_ID: "task",
    "0-46": "task",
    HS_MEETING_TYPE_ID: "meeting",
    HS_CALL_TYPE_ID: "call",
    HS_EMAIL_TYPE_ID: "email",
    "0-13": "lead",
    HS_LEAD_TYPE_ID: "lead",
    "0-18": "communication",
}

_HS_NAME_TO_TYPE_ID: Final[dict[str, str]] = {
    "contact": HS_CONTACT_TYPE_ID,
    "company": HS_COMPANY_TYPE_ID,
    "deal": HS_DEAL_TYPE_ID,
    "ticket": HS_TICKET_TYPE_ID,
    "note": HS_NOTE_TYPE_ID,
    "task": HS_TASK_TYPE_ID,
    "meeting": HS_MEETING_TYPE_ID,
    "call": HS_CALL_TYPE_ID,
    "email": HS_EMAIL_TYPE_ID,
    "lead": HS_LEAD_TYPE_ID,
}

# Standard associations to fetch for engagements (Calls/Meetings/Notes)
HS_ACTIVITY_ASSOCS: Final[list[str]] = ["contacts", "deals", "companies", "tickets"]


def get_hub_host(metadata: dict[str, Any] | None) -> str:
    """Determine the HubSpot app host based on integration metadata (US vs EU).

    Args:
        metadata: Integration metadata containing hub_domain.

    Returns:
        The HubSpot app domain (e.g. app.hubspot.com or app-eu1.hubspot.com).

    """
    if not metadata:
        return "app.hubspot.com"
    hub_domain = metadata.get("hub_domain")
    if hub_domain and "eu1" in str(hub_domain).lower():
        return "app-eu1.hubspot.com"
    return "app.hubspot.com"


def get_hub_api_host(hub_domain: str | None) -> str:
    """Determine the HubSpot API host based on hub_domain.

    Args:
        hub_domain: The raw domain string (e.g. api-eu1.hubapi.com).

    Returns:
        The normalized API host (e.g. https://api-eu1.hubapi.com).

    """
    host = hub_domain or "api.hubapi.com"
    if not host.startswith("http"):
        host = f"https://{host}"
    return host.rstrip("/")


# Centralized singular → plural mapping for HubSpot API endpoints.
_PLURAL_MAP_BASE: dict[str, str] = {
    "contact": "contacts",
    "company": "companies",
    "deal": "deals",
    "ticket": "tickets",
    "task": "tasks",
    "note": "notes",
    "meeting": "meetings",
    "call": "calls",
    "email": "emails",
    "lead": "leads",
    "communication": "communications",
}

# Include numeric HubSpot type IDs (e.g. "0-1" → "contacts") in one pass.
_PLURAL_MAP: Final[dict[str, str]] = {
    **_PLURAL_MAP_BASE,
    **{
        type_id: _PLURAL_MAP_BASE[singular]
        for type_id, singular in _HS_TYPE_MAP.items()
        if singular in _PLURAL_MAP_BASE
    },
}


@lru_cache(maxsize=128)
def normalize_object_type(object_type: str) -> str:
    """Normalize a HubSpot object type to its singular form.

    Converts to lowercase, handles pluralization (e.g., 'contacts' ->
    'contact'), and maps internal numerical type IDs (e.g., '0-1' ->
    'contact').

    Args:
        object_type: The raw object type string.

    Returns:
        Normalized singular object type.

    """
    normalized = _HS_TYPE_MAP.get(object_type.lower())
    if normalized:
        return normalized

    # Fallback to string manipulation only for unknown custom types
    return object_type.lower().replace("ies", "y").rstrip("s")


@lru_cache(maxsize=128)
def pluralize_hs_type(object_type: str) -> str:
    """Convert any HubSpot object type to plural API form.

    Handles both singular names and numeric type IDs.

    Args:
        object_type: Raw object type (e.g. 'contact', '0-1').

    Returns:
        Plural API form (e.g. 'contacts').

    """
    key = object_type.lower()
    if key in _PLURAL_MAP:
        return _PLURAL_MAP[key]
    # Already plural or unknown — return as-is
    return key


class HTTPClient:
    """Global asynchronous HTTP client wrapper with centralized configuration.

    Provides a shared httpx.AsyncClient instance with request-scoped
    correlation ID tracking and global logging hooks for traceability.
    """

    _client: httpx.AsyncClient | None = None

    @classmethod
    def get_client(
        cls, *, corr_id: str | None = None, timeout: float | httpx.Timeout = 10.0
    ) -> httpx.AsyncClient:
        """Retrieve or initialize the shared httpx.AsyncClient singleton.

        Ensures that a single client instance is reused across the application
        to optimize performance through connection pooling (warm pipes).

        Args:
            corr_id: Optional correlation ID for initial client setup.
            timeout: Default timeout for the client session (Tiered: 10s default).

        Returns:
            The shared AsyncClient instance.

        """
        # Set context for hooks
        if corr_id:
            corr_id_ctx.set(corr_id)

        log = CorrelationAdapter(logger, corr_id or "no-corr-id")

        if cls._client is None or cls._client.is_closed:
            log.debug("Creating new shared AsyncClient instance (Timeout=%ss)", timeout)

            cls._client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout)
                if isinstance(timeout, int | float)
                else timeout,
                event_hooks={
                    "request": [cls._log_request, cls._inject_headers],
                    "response": [cls._log_response],
                },
                # Maximize throughput for Frankfurt-to-Edge pooling
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
        else:
            log.debug("Reusing existing shared AsyncClient instance")

        return cls._client

    # ---------------------------------------------------------
    # Logging hooks (use corr_id_ctx from app.core.logging)
    # ---------------------------------------------------------
    @staticmethod
    async def _inject_headers(request: httpx.Request) -> None:
        """Injects the request-scoped correlation ID into outbound headers."""
        corr_id = corr_id_ctx.get("no-corr-id")
        if corr_id != "no-corr-id":
            request.headers["X-Correlation-ID"] = corr_id

    @staticmethod
    async def _log_request(request: httpx.Request) -> None:
        """Logs the outbound HTTP request details."""
        logger.debug("HTTP %s %s", request.method, request.url)

    @staticmethod
    async def _log_response(response: httpx.Response) -> None:
        """Logs the inbound HTTP response details."""
        logger.debug(
            "HTTP %s %s → %s",
            response.request.method,
            response.request.url,
            response.status_code,
        )

    @classmethod
    async def close(cls, *, corr_id: str | None = None) -> None:
        """Gracefully shuts down the shared HTTP client and its connection pool.

        Args:
            corr_id (str | None): Correlation ID for shutdown logging.

        Returns:
            None

        """
        log = CorrelationAdapter(logger, corr_id or "no-corr-id")

        if cls._client and not cls._client.is_closed:
            log.info("Closing shared AsyncClient instance")
            await cls._client.aclose()
            cls._client = None
        else:
            log.debug("AsyncClient already closed or not initialized")


def get_client_ip(request: Request) -> str:
    """Retrieve the real client IP address, respecting Cloudflare/Render proxies.

    Priority order:
    1. CF-Connecting-IP (Direct user IP for Cloudflare)
    2. X-Forwarded-For (Standard proxy chain)
    3. request.client.host (Direct connection fallback)
    """
    # 1. Cloudflare specific header
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip

    # 2. Standard proxy header (X-Forwarded-For)
    # This might be a comma-separated list; the first one is the client.
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()

    # 3. Direct connection IP
    if request.client:
        return request.client.host

    return "unknown"


def get_client_country(request: Request) -> str:
    """Retrieve the client country code from Cloudflare headers.

    Returns:
        ISO 3166-1 alpha-2 country code or 'XX' if not available.

    """
    return request.headers.get("CF-IPCountry", "XX")
