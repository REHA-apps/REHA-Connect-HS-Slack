from __future__ import annotations  # noqa: D100

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from app.core.logging import get_logger
from app.db.protocols import SupabaseModel
from app.db.supabase_client import SupabaseClient

_PII_KEYS = {"credentials", "email", "access_token", "refresh_token", "client_secret"}


def _mask_pii(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strips sensitive keys from a dictionary for safe logging."""
    return {k: "***" if k in _PII_KEYS else v for k, v in payload.items()}


logger = get_logger("supabase.repo")


class SupabaseRepository[R: SupabaseModel]:
    """Generic asynchronous repository for typed CRUD operations on Supabase tables.

    Attributes:
        client: The underlying database client.
        table: The name of the table this repository manages.
        model: The Pydantic model for record validation and conversion.

    """

    def __init__(
        self,
        client: SupabaseClient,
        table: str,
        model: type[R],
    ) -> None:
        self.client = client
        self.table = table
        self.model = model

    # Fetching operations
    async def fetch_single(
        self,
        filters: Mapping[str, Any],
        *,
        select: Sequence[str] | None = None,
    ) -> R | None:
        """Fetches a single record and converts it to the model.

        Args:
            filters: Equivalence filters.
            select: Column selection override.

        Returns:
            The model instance or None if not found.

        """
        logger.debug("fetch_single(%s, filters=%s)", self.table, _mask_pii(filters))

        row = await self.client.fetch_single(self.table, filters, select=select)
        if row is None:
            return None

        return self.model.from_supabase(row)

    async def fetch_many(
        self,
        filters: Mapping[str, Any],
        *,
        select: Sequence[str] | None = None,
        order_by: tuple[str, str] | None = None,
        limit: int | None = None,
    ) -> list[R]:
        """Fetches multiple records and converts them to models."""
        rows = await self.client.fetch_many(
            self.table,
            filters,
            select=select,
            order_by=order_by,
            limit=limit,
            count_only=False,
        )
        if not isinstance(rows, list):
            raise TypeError(
                f"fetch_many on table '{self.table}' returned unexpected type "
                f"{type(rows)!r}; expected list."
            )
        return [self.model.from_supabase(r) for r in rows]

    # Mutation operations
    async def insert(self, payload: Mapping[str, Any]) -> R:
        """Inserts a record and returns the validated model.

        Args:
            payload: Row data.

        Returns:
            The created model instance.

        """
        logger.debug("insert(%s): %s", self.table, _mask_pii(payload))

        row = await self.client.insert(self.table, payload)
        return self.model.from_supabase(row)

    async def upsert(self, payload: Mapping[str, Any], *, on_conflict: str = "id") -> R:
        """Upserts a record and returns the validated model.

        Args:
            payload: Row data.
            on_conflict: Primary/Unique key column(s) for conflict resolution.

        Returns:
            The upserted model instance.

        Raises:
            RuntimeError: If upsert returns no data.

        """
        logger.debug("upsert(%s): %s", self.table, _mask_pii(payload))

        payload_dict = dict(payload)

        if "updated_at" in getattr(self.model, "model_fields", {}):
            payload_dict["updated_at"] = datetime.now(UTC).isoformat()

        row = await self.client.upsert(
            self.table, payload_dict, on_conflict=on_conflict
        )
        if row is None:
            raise RuntimeError(
                f"Supabase upsert returned None for table={self.table}, "
                f"payload={payload}"
            )
        return self.model.from_supabase(row)

    async def update(
        self,
        filters: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> R | None:
        """Updates records and returns the first updated model.

        Args:
            filters: Filters identifying the row.
            payload: New values.

        Returns:
            The updated model or None if not found.

        """
        logger.debug(
            "update(%s, filters=%s, payload=%s)",
            self.table,
            _mask_pii(filters),
            _mask_pii(payload),
        )

        payload_dict = dict(payload)
        if "updated_at" in getattr(self.model, "model_fields", {}):
            payload_dict["updated_at"] = datetime.now(UTC).isoformat()

        row = await self.client.update(self.table, filters, payload_dict)
        if row is None:
            return None

        return self.model.from_supabase(row)

    # Deletion operations
    async def delete(self, filters: Mapping[str, Any]) -> int:
        """Deletes records matching the given filters.

        Args:
            filters: Filters identifying the rows to delete.

        Returns:
            The number of records deleted.

        """
        logger.debug("delete(%s, filters=%s)", self.table, filters)
        return await self.client.delete(self.table, filters)

    # Utility operations
    async def exists(self, filters: Mapping[str, Any]) -> bool:
        """Checks if a record exists matching the given filters.

        Uses an optimized selection to minimize data transfer.

        Args:
            filters: Filters identifying the record.

        Returns:
            True if at least one record matches, otherwise False.

        """
        row = await self.client.fetch_single(self.table, filters, select=["id"])
        return row is not None

    async def first_or_none(
        self,
        filters: Mapping[str, Any],
        *,
        order_by: tuple[str, str] | None = None,
    ) -> R | None:
        """Fetches the first record matching the filters, or None if not found.

        Args:
            filters: Equivalence filters.
            order_by: Tuple of (column, direction).

        Returns:
            The first model instance or None.

        """
        rows = await self.fetch_many(filters, order_by=order_by, limit=1)
        return rows[0] if rows else None

    async def count(self, filters: Mapping[str, Any]) -> int:
        """Returns the number of rows matching the given filters.

        Args:
            filters: Equivalence filters.

        Returns:
            Total count of matching rows.

        """
        return await self.client.count(self.table, filters)

    async def list_all(self, *, limit: int = 1000) -> list[R]:
        """Fetch all records from the table up to the given limit.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of model instances.

        """
        rows = await self.client.fetch_many(self.table, {}, limit=limit)
        rows_list = cast(list[dict[str, Any]], rows)
        return [self.model.from_supabase(r) for r in rows_list]
