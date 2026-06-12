from __future__ import annotations  # noqa: D100

from collections.abc import Iterable, Iterator
from typing import Any, Protocol, Self, TypedDict

# JSON Types
type JSONValue = str | int | float | bool | None | dict[str, Any] | list[Any]
type JSONDict = dict[str, JSONValue]
type SupabaseList = list[JSONDict]


# Protocol definitions
class SupabaseRow(Protocol):
    """A minimal protocol describing a row returned by Supabase."""

    def __getitem__(self, key: str, /) -> Any: ...
    def keys(self) -> Iterable[str]: ...
    def __iter__(self) -> Iterator[str]: ...
    def __len__(self) -> int: ...


class SupabaseRowDict(TypedDict, total=False):
    """A TypedDict version of a Supabase row.
    Useful for autocomplete and static analysis.

    Subclasses can extend this for specific tables.
    """

    id: Any
    created_at: Any
    updated_at: Any


class SupabaseModel(Protocol):
    """Protocol for models that can be constructed from a Supabase row."""

    @classmethod
    def from_supabase(cls, data: SupabaseRow) -> Self: ...


class SupportsSingle(Protocol):
    """Protocol for Supabase queries that support `.single()`."""

    def single(self) -> Any: ...


class SupportsMaybeSingle(Protocol):
    """Protocol for Supabase queries that support `.maybe_single()`."""

    def maybe_single(self) -> Any: ...


class SupportsExecute(Protocol):
    """Protocol for RPC calls or filters that support `.execute()`."""

    def execute(self) -> Any: ...
