from __future__ import annotations  # noqa: D100

import asyncio
import datetime
from collections.abc import Mapping, Sequence
from typing import Any, cast

from postgrest.types import CountMethod
from supabase import AsyncClient, AsyncClientOptions, create_async_client

from app.core.config import settings
from app.core.logging import CorrelationAdapter, get_logger

logger = get_logger("supabase")

# Module-level Supabase connection singleton.
# Avoids creating a new connection per request (~100-300ms savings).
_async_supabase_singleton: AsyncClient | None = None
_supabase_init_lock = asyncio.Lock()


async def get_async_client() -> AsyncClient:
    """Return the shared Supabase AsyncClient, creating it on first use."""
    global _async_supabase_singleton  # noqa: PLW0603

    # Fast path - no lock overhead if already initialized
    if _async_supabase_singleton is not None:
        return _async_supabase_singleton
    async with _supabase_init_lock:
        # Double-check pattern inside the lock
        if _async_supabase_singleton is None:
            import os

            from httpx import Limits

            url = settings.SUPABASE_URL.unicode_string()
            logger.debug("Initializing new Supabase AsyncClient singleton")

            # Pool limits come from env vars so each Lambda function (API, Sentiment)
            # can be tuned independently without code changes.
            # NOTE: These are httpx HTTP connection limits to the Supabase REST API
            # (PostgREST over HTTPS). They do NOT count against the Supabase Free Tier's
            # 60 direct Postgres connection limit — the REST API manages its own pool
            # server-side.
            pool_limits = Limits(
                max_connections=int(os.getenv("SUPABASE_MAX_CONNECTIONS", "50")),
                max_keepalive_connections=int(
                    os.getenv("SUPABASE_KEEPALIVE_CONNECTIONS", "20")
                ),
                keepalive_expiry=30,
            )

            from httpx import AsyncClient as HttpxAsyncClient

            http_client = HttpxAsyncClient(limits=pool_limits)

            options = AsyncClientOptions(
                postgrest_client_timeout=30,
                storage_client_timeout=30,
                httpx_client=http_client,
            )

            _async_supabase_singleton = await create_async_client(
                url, settings.SUPABASE_KEY.get_secret_value(), options=options
            )
    return _async_supabase_singleton


async def reset_supabase_client() -> None:
    """Clears and closes the Supabase singleton."""
    global _async_supabase_singleton  # noqa: PLW0603
    if _async_supabase_singleton is not None:
        logger.warning("Resetting Supabase client singleton")
        _async_supabase_singleton = None


def _serialize_payload(obj: Any) -> Any:
    """Recursively serialize datetime objects for Supabase JSON columns."""
    match obj:
        case dict():
            return {k: _serialize_payload(v) for k, v in obj.items()}
        case list():
            return [_serialize_payload(item) for item in obj]
        case datetime.datetime() | datetime.date():
            return obj.isoformat()
        case _:
            return obj


class SupabaseClient:
    """Native asynchronous wrapper for Supabase Python SDK.

    Eliminates thread pool overhead by using AsyncClient directly.
    """

    def __init__(self, *, corr_id: str | None = None) -> None:
        self.corr_id = corr_id or "supabase"
        self.log = CorrelationAdapter(logger, self.corr_id)

    async def fetch_single(
        self,
        table: str,
        filters: Mapping[str, Any],
        *,
        select: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetches a single record matching the filters."""
        client = await get_async_client()
        query = client.table(table).select(",".join(select) if select else "*")

        for key, value in filters.items():
            query = query.eq(key, value)

        query = query.limit(1)

        resp = await query.execute()
        data = resp.data

        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        return cast(dict[str, Any], data[0])

    async def fetch_many(
        self,
        table: str,
        filters: Mapping[str, Any],
        *,
        select: Sequence[str] | None = None,
        order_by: tuple[str, str] | None = None,
        limit: int | None = None,
        count_only: bool = False,
    ) -> Sequence[dict[str, Any]] | int:
        """Fetches multiple records or just the count."""
        client = await get_async_client()
        query = client.table(table).select(
            ",".join(select) if select else "*",
            count=CountMethod.exact if count_only else None,
        )

        for key, value in filters.items():
            query = query.eq(key, value)

        if order_by:
            col, direction = order_by
            query = query.order(col, desc=(direction.lower() == "desc"))

        if limit:
            query = query.limit(limit)

        if count_only:
            resp = await query.execute()
            return resp.count if resp.count is not None else 0

        resp = await query.execute()
        return cast(Sequence[dict[str, Any]], resp.data or [])

    async def insert(
        self,
        table: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Inserts a new record into the database."""
        client = await get_async_client()
        serialized_payload = _serialize_payload(dict(payload))
        resp = await client.table(table).insert(serialized_payload).execute()

        data = resp.data
        if isinstance(data, list) and len(data) > 0:
            val = data[0]
            if isinstance(val, dict):
                return cast(dict[str, Any], val)
            return cast(dict[str, Any], {"value": val})
        return cast(dict[str, Any], data)

    async def upsert(
        self,
        table: str,
        payload: Mapping[str, Any],
        *,
        on_conflict: str = "id",
        ignore_duplicates: bool = False,
    ) -> dict[str, Any] | None:
        """Upserts a record into the database."""
        client = await get_async_client()
        serialized_payload = _serialize_payload(dict(payload))
        resp = (
            await client.table(table)
            .upsert(
                serialized_payload,
                on_conflict=on_conflict,
                ignore_duplicates=ignore_duplicates,
            )
            .execute()
        )

        data = resp.data
        if isinstance(data, list) and len(data) > 0:
            val = data[0]
            if isinstance(val, dict):
                return cast(dict[str, Any], val)
            return None
        return cast(dict[str, Any], data) if isinstance(data, dict) else None

    async def update(
        self,
        table: str,
        filters: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Updates records matching the filters."""
        client = await get_async_client()
        serialized_payload = _serialize_payload(dict(payload))
        update_query = client.table(table).update(serialized_payload)
        for key, value in filters.items():
            update_query = update_query.eq(key, value)

        resp = await update_query.execute()

        data = resp.data
        if isinstance(data, list) and len(data) > 0:
            val = data[0]
            if isinstance(val, dict):
                return cast(dict[str, Any], val)
            return None
        return cast(dict[str, Any], data) if isinstance(data, dict) else None

    async def delete(
        self,
        table: str,
        filters: Mapping[str, Any],
    ) -> int:
        """Deletes records matching the filters."""
        client = await get_async_client()
        query = client.table(table).delete()
        for key, value in filters.items():
            query = query.eq(key, value)

        resp = await query.execute()
        return len(resp.data or [])

    async def count(
        self,
        table: str,
        filters: Mapping[str, Any],
    ) -> int:
        """Return count of rows matching filters using native head=True."""
        client = await get_async_client()
        resp = (
            await client.table(table)
            .select("*", count=CountMethod.exact)
            .match(dict(filters))
            .execute()
        )
        return resp.count or 0

    async def rpc(self, fn_name: str, params: dict[str, Any]) -> Any:
        """Executes a remote PostgreSQL function via Supabase RPC."""
        client = await get_async_client()
        resp = await client.rpc(fn_name, params).execute()
        return resp.data
