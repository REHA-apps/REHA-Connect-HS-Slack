"""AWS Lambda entry point — routes between FastAPI (HTTP) and scheduled maintenance.

EventBridge Scheduler invokes this handler with a JSON payload like
``{"task": "billing"}`` or ``{"task": "ghosting"}``.  All other invocations
(API Gateway, Lambda Function URL) are forwarded to the Mangum/FastAPI handler.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from mangum import Mangum  # type: ignore

from app.main import app

logger = logging.getLogger(__name__)

# Reused across warm invocations (Mangum wraps the full FastAPI ASGI app).
_mangum_handler = Mangum(app, lifespan="auto")


def handler(event: dict[str, Any], context: Any) -> Any:
    """Unified Lambda entry point.

    Routes EventBridge Scheduler events to the maintenance scheduler and
    all other events (HTTP via API Gateway / Function URL) to FastAPI via Mangum.

    Args:
        event: The raw Lambda event dict.
        context: The Lambda runtime context object.

    """
    # ------------------------------------------------------------------ #
    # EventBridge Scheduler events contain a "task" key in the payload.   #
    # They do NOT contain "httpMethod", "requestContext", or "version".   #
    # ------------------------------------------------------------------ #
    is_http = (
        "httpMethod" in event  # API Gateway REST
        or "requestContext" in event  # API Gateway HTTP / Function URL
        or event.get("version") in ("1.0", "2.0")  # Payload format versions
    )

    if not is_http:
        # Check for SQS Events
        if "Records" in event and len(event["Records"]) > 0:
            if event["Records"][0].get("eventSource") == "aws:sqs":
                from app.sqs_worker import SQSEvent
                from app.sqs_worker import handler as sqs_handler

                logger.info("Routing to SQS worker")
                return sqs_handler(cast(SQSEvent, event), context)

        # Treat as a scheduled maintenance event
        from lambda_scheduler import handler as scheduler_handler

        logger.info("Routing to scheduler — event: %s", event)
        return scheduler_handler(event, context)

    # Standard HTTP traffic → FastAPI via Mangum
    return _mangum_handler(event, context)
