"""app/utils/sqs_helpers.py — SQS publishing helpers.

This module wraps boto3 SQS so callers can fall back to local background
tasks when no queue URL is configured (development) without importing boto3
at module-level (keeps the app boot fast and test-friendly).

Usage::

    published = publish_to_sqs(
        queue_url=settings.SQS_SLACK_WEBHOOK_QUEUE_URL,
        workspace_id=workspace_id,
        corr_id=corr_id,
        task_type="hubspot_webhook",
        payload={"events": events},
    )
    if not published:
        background_tasks.add_task(...)
"""

from __future__ import annotations

import json
from typing import Any

from app.core.logging import get_logger

logger = get_logger("utils.sqs_helpers")

# Module-level lazy SQS client — created once per process
_sqs_client: Any = None


def _get_sqs_client() -> Any:
    """Return a cached boto3 SQS client, creating it on first call."""
    global _sqs_client  # noqa: PLW0603
    if _sqs_client is None:
        try:
            import boto3  # type: ignore[import-untyped]

            _sqs_client = boto3.client("sqs")
        except Exception as exc:  # pragma: no cover
            logger.warning("boto3 not available — SQS publishing disabled: %s", exc)
            _sqs_client = None
    return _sqs_client


def publish_to_sqs(
    *,
    queue_url: str | None,
    workspace_id: str,
    corr_id: str,
    task_type: str,
    payload: dict[str, Any],
    delay_seconds: int = 0,
) -> bool:
    """Publish a task message to an SQS queue.

    Returns ``True`` if the message was sent successfully, ``False`` when
    SQS is unavailable or no queue URL is configured so callers can fall
    back to local ``BackgroundTasks``.

    Parameters
    ----------
    queue_url:
        The SQS queue URL.  When ``None`` or empty the function returns
        ``False`` immediately (useful for local development with no queue).
    workspace_id:
        Tenant identifier; stored as a message attribute for routing.
    corr_id:
        Correlation / trace ID forwarded to the worker for log continuity.
    task_type:
        Logical task name (e.g. ``"hubspot_webhook"``, ``"slack_message_redaction"``).
    payload:
        Arbitrary JSON-serialisable dict that the worker will receive as the
        message body.
    delay_seconds:
        SQS delivery delay in seconds (0–900).  Defaults to 0.

    """
    if not queue_url:
        return False

    client = _get_sqs_client()
    if client is None:
        return False

    message_body = json.dumps(
        {
            "task_type": task_type,
            "workspace_id": workspace_id,
            "corr_id": corr_id,
            **payload,
        }
    )

    kwargs: dict[str, Any] = {
        "QueueUrl": queue_url,
        "MessageBody": message_body,
        "MessageAttributes": {
            "workspace_id": {
                "DataType": "String",
                "StringValue": workspace_id,
            },
            "task_type": {
                "DataType": "String",
                "StringValue": task_type,
            },
            "corr_id": {
                "DataType": "String",
                "StringValue": corr_id,
            },
        },
    }

    if delay_seconds > 0:
        kwargs["DelaySeconds"] = min(delay_seconds, 900)

    try:
        response = client.send_message(**kwargs)
        message_id = response.get("MessageId", "?")
        logger.debug(
            "SQS message published (task=%s, workspace=%s, msg_id=%s, delay=%ds)",
            task_type,
            workspace_id,
            message_id,
            delay_seconds,
        )
        return True
    except Exception as exc:
        logger.warning(
            "SQS publish failed (task=%s, workspace=%s): %s — falling back to local",
            task_type,
            workspace_id,
            exc,
        )
        return False
