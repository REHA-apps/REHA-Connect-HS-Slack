from __future__ import annotations  # noqa: D100

from datetime import datetime
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, field_validator

from app.db.protocols import SupabaseRow
from app.utils.parsers import validate_supabase_row


class BaseRecord(BaseModel):
    """Base Pydantic model for all Supabase database records.

    Rules Applied:
        - Enforces immutability (frozen=True) for domain data integrity.
        - Provides standardized constructors and serialization helpers (Supabase).
        - Automatically handles timestamp normalization.
    """

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    # Override in subclasses
    required_fields: ClassVar[set[str]] = set()

    # Timestamp normalization
    @field_validator("created_at", "updated_at", mode="before", check_fields=False)
    def parse_timestamps(cls, v: str | datetime | None) -> datetime | None:
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

    # Validation helpers
    @classmethod
    def validate_required_fields(cls, data: SupabaseRow) -> None:
        if cls.required_fields:
            validate_supabase_row(data, list(cls.required_fields))

    # Record constructors
    @classmethod
    def from_supabase(cls, data: SupabaseRow) -> Self:
        cls.validate_required_fields(data)
        return cls(**data)

    @classmethod
    def from_optional_supabase(cls, data: SupabaseRow | None) -> Self | None:
        if data is None:
            return None
        return cls.from_supabase(data)

    # Serialization helpers
    def to_supabase(self) -> dict[str, Any]:
        """Convert record back to a Supabase-friendly dict."""
        data = self.model_dump()
        for key in ["created_at", "updated_at"]:
            if key in data and data[key] is None:
                del data[key]
        return data

    def dict_for_update(self, exclude: set[str] | None = None) -> dict[str, Any]:
        """Return a dict suitable for update operations.

        Args:
            exclude: Fields to exclude. Defaults to {"id", "created_at"} if
                not provided. Pass an explicit empty set to include all fields.

        """
        effective_exclude = exclude if exclude is not None else {"id", "created_at"}
        return self.model_dump(exclude=effective_exclude)

    # Utility helpers
    @property
    def primary_key(self) -> str:
        """Return the primary key (default: id)."""
        return getattr(self, "id", "")  # pyright: ignore

    def copy_with(self, **updates: Any) -> Self:
        """Immutable update helper."""
        return self.model_copy(update=updates)

    def __repr__(self) -> str:
        """Lightweight repr showing only the primary key to avoid full serialization."""
        pk = getattr(self, "id", None) or getattr(self, "workspace_id", "?")
        return f"{self.__class__.__name__}(pk={pk!r})"
