"""Pure ASGI middleware for correlation-ID propagation and security.

Replaces BaseHTTPMiddleware to avoid the performance penalty of wrapping
every request body in a ``StreamingResponse``.
"""

from __future__ import annotations

import hmac
import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.logging import get_corr_id_from_scope, get_logger

logger = get_logger("app.middleware")


class LogContextMiddleware:
    """Bind correlation ID and Triple-Key context to the logging context.

    This is a pure ASGI middleware (no ``BaseHTTPMiddleware`` overhead).
    It extracts Slack request timestamps and HubSpot portal IDs for
    cross-platform traceability.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        corr_id = get_corr_id_from_scope(scope)

        # Expose corr_id via request.state.corr_id for route handlers
        scope.setdefault("state", {})["corr_id"] = corr_id

        # Capture Triple-Key tracing context from headers
        slack_ts = "none"
        portal_id = "none"
        for key, value in scope.get("headers", []):
            if key.lower() == b"x-slack-request-timestamp":
                slack_ts = value.decode()
            elif key.lower() == b"x-hubspot-portal-id":
                portal_id = value.decode()

        from app.core.logging import triple_key_context

        async def send_with_corr_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-correlation-id", corr_id.encode()))
                message["headers"] = headers
            await send(message)

        with triple_key_context(
            corr_id=corr_id, slack_ts=slack_ts, portal_id=portal_id
        ):
            await self.app(scope, receive, send_with_corr_id)


class SecurityGuardMiddleware:
    """Validates X-REHA-SECRET header on incoming requests.

    This is a pure ASGI middleware for maximum performance.
    Secures the Lambda Function URL from direct, unauthenticated hits.
    Bypassed in dev mode or for exempt routes like / and /api/health.
    """

    EXEMPT_PATHS = {
        b"/",
        b"/api/health",
        b"/api/oauth/callback",
        b"/api/slack/oauth/callback",
        b"/api/hubspot/webhook",
        b"/api/slack/events",
        b"/api/slack/interactions",
        b"/api/slack/commands",
    }

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "").encode()

        # Always allow through exempt paths (health checks, externally-signed webhooks)
        if path in self.EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # In dev mode, log bypass but still process (don't fully skip the guard)
        if settings.is_dev:
            logger.debug("SecurityGuard bypassed (dev mode): %s", path.decode())
            await self.app(scope, receive, send)
            return

        # 3. Security validation
        expected_secret = settings.REHA_WEBHOOK_SECRET.get_secret_value()

        # Extract secret from headers (headers are a list of tuples)
        provided_secret = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"x-reha-secret":
                provided_secret = value.decode()
                break

        if (
            not expected_secret
            or not provided_secret
            or not hmac.compare_digest(expected_secret, provided_secret)
        ):
            logger.warning(
                "Unauthorized access attempt blocked. Path: %s",
                path.decode(),
            )

            # Return 401 response via direct ASGI messages
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps({"error": "Unauthorized"}).encode(),
                }
            )
            return

        await self.app(scope, receive, send)
