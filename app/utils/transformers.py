from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from app.core.logging import get_logger

logger = get_logger("utils.transformers")


# HubSpot timestamp conversions
@lru_cache(maxsize=1024)
def _cached_to_hubspot_timestamp(dt: datetime) -> int:
    """Pure cached conversion — expects UTC-aware datetime."""
    return int(dt.timestamp() * 1000)


def to_hubspot_iso8601(dt: datetime) -> str:
    """Formats a Python datetime as a HubSpot-compatible ISO 8601 string (UTC).
    HubSpot expects millisecond precision: YYYY-MM-DDTHH:MM:SS.mmmZ
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)

    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_hubspot_timestamp(
    dt: datetime,
    *,
    corr_id: str | None = None,
) -> int:
    """Converts a Python datetime to a HubSpot-compatible Unix millisecond timestamp.

    Args:
        dt: The datetime to convert.
        corr_id: Unused, kept for backward compatibility.

    Returns:
        The resulting millisecond timestamp.

    """
    if dt.tzinfo is None:
        logger.warning("Datetime missing timezone; assuming UTC")
        dt = dt.replace(tzinfo=UTC)

    ts = _cached_to_hubspot_timestamp(dt)
    logger.debug("Converted datetime to HubSpot timestamp: %s -> %s", dt, ts)
    return ts


@lru_cache(maxsize=1024)
def _cached_from_hubspot_timestamp(ms: int) -> datetime:
    """Pure cached conversion without logging."""
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def from_hubspot_timestamp(
    ms: int,
    *,
    corr_id: str | None = None,
) -> datetime:
    """Converts a HubSpot Unix millisecond timestamp to a Python datetime (UTC).

    Args:
        ms: Target millisecond timestamp.
        corr_id: Unused, kept for backward compatibility.

    Returns:
        UTC-aware datetime object.

    """
    dt = _cached_from_hubspot_timestamp(ms)
    logger.debug("Converted HubSpot timestamp to datetime: %s -> %s", ms, dt)
    return dt


# HubSpot object flattening
def flatten_properties(
    hubspot_object: Mapping[str, Any],
    *,
    corr_id: str | None = None,
) -> dict[str, Any]:
    """Flattens HubSpot's nested 'properties' structure into a single-level dict.

    Args:
        hubspot_object: Raw HubSpot object record.
        corr_id: Unused, kept for backward compatibility.

    Returns:
        Flattened object dictionary.

    """
    props = hubspot_object.get("properties", {})
    if not isinstance(props, Mapping):
        logger.warning("HubSpot object properties is not a mapping; returning original")
        return dict(hubspot_object)

    flattened = dict(hubspot_object)
    for key, value in props.items():
        if key in flattened:
            logger.debug("Property %s overwrites top-level key", key)
        flattened[key] = value

    logger.debug("Flattened HubSpot object: %s", flattened)
    return flattened


def to_datetime(value: Any) -> datetime:
    """Converts a HubSpot property value (int milliseconds or ISO 8601 string)
        into a Python datetime object (UTC).

    Args:
        value (Any): The value to convert.

    Returns:
        datetime: UTC-aware datetime object. Defaults to epoch if unparsable.

    """
    if not value:
        return datetime.fromtimestamp(0, tz=UTC)

    # Handle numeric (millisecond timestamps)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)

    if isinstance(value, str):
        # Handle numeric strings
        if value.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)

        # Handle ISO 8601 strings
        try:
            # datetime.fromisoformat handles 'Z' in 3.11+
            # Replacing Z for backwards safety if needed, though we're on 3.12
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except ValueError:
            return datetime.fromtimestamp(0, tz=UTC)

    return datetime.fromtimestamp(0, tz=UTC)
