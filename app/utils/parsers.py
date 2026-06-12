from __future__ import annotations  # noqa: D100

import shlex
from collections.abc import Iterable, Mapping
from typing import Any

from app.core.logging import get_logger
from app.db.protocols import SupabaseRow

logger = get_logger("utils.parsers")


# Slack command parsing
def parse_slack_command_text(
    text: str,
    *,
    corr_id: str | None = None,
) -> dict[str, str]:
    """Parses raw Slack slash command text into key-value pairs.

    Uses shell-style splitting to handle quoted values.

    Args:
        text: The raw command text (e.g., 'email=foo@bar.com').
        corr_id: Optional correlation ID for logging.

    Returns:
        A dictionary of parsed key-value pairs.

    """
    try:
        parts = shlex.split(text)
        parsed = {
            key: value for key, value in (p.split("=", 1) for p in parts if "=" in p)
        }
        logger.debug("Parsed Slack command text: %s", parsed)
        return parsed

    except Exception as exc:
        logger.error("Failed to parse Slack command text: %s", exc)
        return {}


# Type coercion helpers
def coerce_to_str_dict(
    data: Mapping[str, Any],
) -> dict[str, str | None]:
    """Normalizes a dictionary by converting all values to strings or None.

    Args:
        data: The source dictionary.

    Returns:
        A new dictionary with string-coerced values.

    """
    result = {
        key: (str(value) if value is not None else None) for key, value in data.items()
    }

    logger.debug("Coerced mapping to str dict: %s", result)

    return result


def to_int(value: str | int | None) -> int | None:
    """Convert value to int, returning None on failure.

    Args:
        value: The string, integer, or None value to convert.

    Returns:
        The integer value or None.

    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Database validation helpers
def validate_supabase_row(
    data: SupabaseRow,
    required: Iterable[str],
) -> None:
    """Ensures that a Supabase database row contains all required fields.

    Args:
        data: The record retrieved from Supabase.
        required: List of mandatory column names.

    Raises:
        ValueError: If any required field is missing.

    """
    missing = [key for key in required if key not in data]
    if missing:
        logger.error("Supabase row missing required fields: %s", missing)
        raise ValueError(f"Missing required fields: {missing}")

    logger.debug("Supabase row validated successfully")


def parse_reha_task_command(text: str) -> dict[str, Any]:
    """Parses advanced parameters from /reha task command text.

    Supports mentions (@user), relative due dates (today, tomorrow), and subjects.

    Args:
        text: The raw command text.

    Returns:
        A dictionary containing subject, slack_user_id, and due_date.

    """
    import re
    from datetime import UTC, datetime, timedelta

    result: dict[str, Any] = {
        "subject": text,
        "slack_user_id": None,
        "due_date": None,
    }

    # 1. Extract Slack Mention (@user)
    mention_match = re.search(r"<@([A-Z0-9]+)>", text)
    if mention_match:
        result["slack_user_id"] = mention_match.group(1)
        text = text.replace(mention_match.group(0), "").strip()

    # 2. Extract Relative Due Dates
    now = datetime.now(UTC)
    date_patterns = {
        r"\btoday\b": now,
        r"\btomorrow\b": now + timedelta(days=1),
        r"\bnext week\b": now + timedelta(weeks=1),
    }

    for pattern, dt in date_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            result["due_date"] = dt
            text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
            break

    # 3. Clean up subject
    # Remove extra spaces caused by parameter extraction
    result["subject"] = re.sub(r"\s+", " ", text).strip()

    return result


def extract_text_from_blocks(blocks: list[dict[str, Any]] | None) -> str:
    """Recursively extracts all meaningful text from Slack Block Kit blocks.

    Processes sections, context blocks, rich_text, etc.

    Args:
        blocks: A list of Slack Block objects.

    Returns:
        A concatenated string of all text found in the blocks.

    """
    if not blocks:
        return ""

    text_parts = []

    def _process_element(element: dict[str, Any]) -> None:
        etype = element.get("type", "")

        # Direct text objects
        if etype in {"plain_text", "mrkdwn"}:
            t = element.get("text", "").strip()
            if t:
                text_parts.append(t)
            return

        # Blocks with nested text fields
        if t := element.get("text"):
            if isinstance(t, str):
                text_parts.append(t.strip())
            elif isinstance(t, dict):
                _process_element(t)

        # Blocks with fields (e.g., section fields)
        if fields := element.get("fields"):
            for field in fields:
                _process_element(field)

        # Container blocks (context, actions, etc.)
        if elements := element.get("elements"):
            for child in elements:
                _process_element(child)

    for block in blocks:
        _process_element(block)

    return " ".join(text_parts).strip()


def extract_state_values(view: dict[str, Any]) -> dict[str, Any]:
    """Flattens a Slack view state payload into a flat dictionary of action_id -> value.

    Extracts text input, date selections, and static/external select options.

    Args:
        view: The Slack view payload dictionary.

    Returns:
        A flat dictionary where keys are action_ids and values are the extracted input.

    """
    state_values = view.get("state", {}).get("values", {})
    properties = {}

    for _, actions in state_values.items():
        for action_id, action_data in actions.items():
            value = action_data.get("value")

            if value is None:
                value = action_data.get("selected_date")

            if value is None:
                value = action_data.get("selected_time")

            if value is None:
                selected_option = action_data.get("selected_option")
                if selected_option:
                    value = selected_option.get("value")

            if value is not None:
                properties[action_id] = value

    return properties
