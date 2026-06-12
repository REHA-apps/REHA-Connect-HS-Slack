from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from typing import Any

from app.core.logging import get_logger
from app.utils.constants import SLACK_ERROR_ICON
from app.utils.helpers import HTTPClient

logger = get_logger("utils.slack")


# Core Slack response_url integration
async def send_slack_response(
    response_url: str,
    content: Mapping[str, Any],
    *,
    corr_id: str | None = None,
) -> None:
    """Dispatches an asynchronous JSON payload to a Slack response_url.

    Args:
        response_url: The specific endpoint provided by Slack for background
            responses.
        content: The message payload (text, blocks, etc.).
        corr_id: Optional correlation ID for tracking.

    """
    client = HTTPClient.get_client(corr_id=corr_id)

    try:
        logger.info("Sending Slack response to response_url")
        resp = await client.post(response_url, json=content)
        resp.raise_for_status()
        logger.info("Slack response sent successfully")

    except Exception as exc:
        logger.error("Failed to send Slack response: %s", exc)


# Specialized error handling
async def send_slack_error(
    response_url: str,
    message: str,
    *,
    corr_id: str | None = None,
) -> None:
    """Sends a standardized error message notification to Slack.

    Args:
        response_url: Target response URL.
        message: The error description.
        corr_id: Correlation ID.

    """
    await send_slack_response(
        response_url,
        {"text": f"{SLACK_ERROR_ICON} {message}"},
        corr_id=corr_id,
    )
