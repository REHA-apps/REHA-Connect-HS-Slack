from __future__ import annotations  # noqa: D100

import asyncio
import random
import re
from collections.abc import Mapping
from typing import Any

import httpx

from app.core.logging import CorrelationAdapter, get_logger

logger = get_logger("base_client")


class BaseClient:
    """Base asynchronous HTTP client for external service integrations.

    Utilizes a shared httpx.AsyncClient for connection pooling, integrations
    with CorrelationAdapter, and implementing automatic exponential backoff.
    """

    # No per-class _client; all subclasses share the global HTTPClient pool.
    # This is intentional: a single pool avoids duplicate connection management
    # across HubSpotClient, SlackClient, etc. on Lambda warm invocations.

    def __init__(
        self,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        corr_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.corr_id = corr_id or "client_unknown"
        self.log = CorrelationAdapter(logger, self.corr_id)

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        """Retrieves the shared httpx AsyncClient singleton.

        Delegates to the global HTTPClient pool (helpers.py) to ensure all
        HTTP subclients share a single connection pool with keepalive connections.
        This reduces cold-start time and prevents pool fragmentation across
        HubSpotClient, SlackClient, and other BaseClient subclasses.

        Returns:
            The shared AsyncClient instance.

        """
        from app.utils.helpers import (
            HTTPClient,  # noqa: PLC0415 (avoids circular import)
        )

        return HTTPClient.get_client()

    def _build_url(self, path: str) -> str:
        """Constructs a full URL from a relative path or returns the URL as is."""
        if path.startswith("http"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _mask_url(self, url: str) -> str:
        """Masks sensitive query parameters in a URL string."""
        # Mask access_token and other common sensitive keys
        masked = re.sub(r"(access_token=)[^&]+", r"\1***", url)
        # Add more masking if needed (e.g. client_secret)
        masked = re.sub(r"(client_secret=)[^&]+", r"\1***", masked)
        return masked

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        description: str | None = None,
    ) -> httpx.Response:
        """Generic request wrapper with retry logic and error handling.

        Args:
            method: HTTP verb (GET, POST, etc.).
            path: API endpoint path relative to the base URL.
            params: URL query parameters.
            json: JSON body payload.
            data: Form data payload.
            description: Optional human-readable name for logging.

        Returns:
            The httpx.Response object.

        """
        url = self._build_url(path)
        client = self.get_client()
        max_retries = 4

        # Resource name for logging
        display_url = self._mask_url(url)
        resource = f"[{description}]" if description else display_url

        for attempt in range(max_retries + 1):
            self.log.debug("HTTP %s %s (attempt %s)", method, resource, attempt)

            try:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=self.headers,
                    params=params,
                    json=json,
                    data=data,
                )

                status = response.status_code

                # ---------------------------------------------------------
                # Check for retryable HTTP statuses (429 or 5xx)
                # ---------------------------------------------------------
                is_retryable = status == 429 or 500 <= status < 600

                if is_retryable and attempt < max_retries:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            delay = 1.0  # Fallback
                        self.log.warning(
                            "Rate limit or server error (%d) on %s. Retrying in %ss",
                            status,
                            resource,
                            delay,
                        )
                    else:
                        base = 0.5 * (2**attempt)
                        delay = base * random.uniform(0.8, 1.2)
                        self.log.warning(
                            "Retrying %s %s due to %d (delay %.2fs)",
                            method,
                            resource,
                            status,
                            delay,
                        )

                    await asyncio.sleep(delay)
                    continue

                # Log completion (even if it's an error, as it's the final attempt)
                self.log.debug("HTTP %s %s returned %d", method, resource, status)
                return response

            except httpx.RequestError as exc:
                # Network issues → retry with backoff
                if attempt < max_retries:
                    base = 0.5 * (2**attempt)
                    delay = base * random.uniform(0.8, 1.2)
                    self.log.warning(
                        "Network error during %s %s: %s (retrying in %.2fs)",
                        method,
                        resource,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    self.log.error(
                        "Max retries exceeded for %s %s due to network error",
                        method,
                        resource,
                    )
                    raise

        # This should theoretically be unreachable due to returning or raising in loop
        raise httpx.RequestError(f"Request failed after {max_retries} retries")

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Convenience method for GET requests, returning parsed JSON."""
        response = await self.request(
            "GET", path, params=params, description=description
        )
        response.raise_for_status()
        return response.json()

    async def post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Convenience method for POST requests, returning parsed JSON."""
        response = await self.request(
            "POST", path, json=json, data=data, description=description
        )
        response.raise_for_status()
        return response.json()
