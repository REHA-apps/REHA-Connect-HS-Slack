"""Centralized logging configuration for REHA Connect.

Provides structured JSON formatting and per-request correlation ID tracing.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from fastapi import Request

# Bootstrap Configuration:
# To avoid cyclic imports (config.py imports logging), we pull basic env flags
# directly from os.getenv here. This allows the logger to be initialized
# before the full Settings class is instantiated.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
ENV = os.getenv("ENV", "dev").lower()
USE_JSON_LOGS = (
    os.getenv("USE_JSON_LOGS", "true" if ENV == "prod" else "false").lower() == "true"
)
DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(corr_id)s - %(message)s"

# Global context for correlation IDs and 2026.03 Triple-Key tracing
corr_id_ctx: ContextVar[str] = ContextVar("corr_id", default="none")
slack_ts_ctx: ContextVar[str] = ContextVar("slack_ts", default="none")
portal_id_ctx: ContextVar[str] = ContextVar("portal_id", default="none")


class JsonFormatter(logging.Formatter):
    """Custom logging formatter that outputs logs in structured JSON format.

    Enriches logs with correlation IDs, Slack timestamps, and HubSpot portal IDs
    for cross-platform traceability.
    """

    def format(self, record: logging.LogRecord) -> str:
        corr_id = getattr(record, "corr_id", corr_id_ctx.get())

        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "corr_id": corr_id,
        }

        # 2026.03 Triple-Key enrichment
        slack_ts = slack_ts_ctx.get("none")
        if slack_ts != "none":
            log_data["slack_ts"] = slack_ts

        portal_id = portal_id_ctx.get("none")
        if portal_id != "none":
            log_data["portal_id"] = portal_id

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data)


@contextmanager
def log_context(corr_id: str):
    """Context manager that sets the correlation ID for the current context."""
    token = corr_id_ctx.set(corr_id)
    try:
        yield
    finally:
        corr_id_ctx.reset(token)


@contextmanager
def triple_key_context(
    corr_id: str | None = None,
    slack_ts: str | None = None,
    portal_id: str | None = None,
):
    """Context manager for the 2026.03 Triple-Key tracing protocol."""
    tokens = []
    if corr_id:
        tokens.append((corr_id_ctx, corr_id_ctx.set(corr_id)))
    if slack_ts:
        tokens.append((slack_ts_ctx, slack_ts_ctx.set(slack_ts)))
    if portal_id:
        tokens.append((portal_id_ctx, portal_id_ctx.set(str(portal_id))))

    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def run_task_with_context(
    corr_id: str,
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Wraps a background task in log_context to maintain correlation IDs."""
    with log_context(corr_id):
        await func(*args, **kwargs)


class ContextFilter(logging.Filter):
    """Logging filter that injects the current correlation ID into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.corr_id = corr_id_ctx.get()
        return True


class AccessLogFilter(logging.Filter):
    """Filters out routine health check logs from access logs to reduce noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Filter out common health check paths to reduce noise in production
        skip_paths = ["/health", "/ready", "/live", "/api/health"]
        return not any(path in msg for path in skip_paths)


_configured_loggers: set[str] = set()

# Silence noisy external HTTP loggers (used by Supabase/httpx)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Configures and retrieves a named logger with context-aware traceability."""
    logger = logging.getLogger(name)

    # If it's a uvicorn logger or doesn't have handlers yet, configure it
    if name in _configured_loggers and not name.startswith("uvicorn"):
        return logger

    if not logger.handlers or name.startswith("uvicorn"):
        # Clear existing handlers for uvicorn loggers to avoid duplicate/default logs
        if name.startswith("uvicorn"):
            logger.handlers = []

        logger.setLevel(LOG_LEVEL)

        handler = logging.StreamHandler(sys.stdout)
        handler.addFilter(ContextFilter())

        # Apply AccessLogFilter specifically to web access logs
        if name == "uvicorn.access":
            handler.addFilter(AccessLogFilter())

        formatter = (
            JsonFormatter() if USE_JSON_LOGS else logging.Formatter(DEFAULT_FORMAT)
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        # Add FileHandler for persistent logs that can be viewed via /api/debug/logs.
        # Skip on Lambda: /var/task is read-only and CloudWatch captures stdout.
        if not os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
            log_file = os.path.join(os.getcwd(), "reha_app.log")
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.addFilter(ContextFilter())
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        # Prevent propagation to root logger
        logger.propagate = False
        _configured_loggers.add(name)

    return logger


def setup_uvicorn_logging() -> None:
    """Configures Uvicorn loggers to use our custom filters and formatters.

    This ensures that routine health checks are filtered out of the access logs
    and that all logs follow the application's structured format.
    """
    # Force configuration of uvicorn loggers via our get_logger factory
    get_logger("uvicorn")
    get_logger("uvicorn.access")
    get_logger("uvicorn.error")


class CorrelationAdapter(logging.LoggerAdapter):
    """Logging adapter that injects a correlation ID into every log record.

    Used throughout the application for per-request traceability.
    """

    def __init__(self, logger: logging.Logger, corr_id: str, **kwargs: Any) -> None:
        self.corr_id = corr_id
        super().__init__(logger, {"corr_id": corr_id, **kwargs})

    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        extra = kwargs.get("extra", {})
        extra["corr_id"] = self.corr_id
        kwargs["extra"] = extra
        return msg, kwargs


async def get_corr_id(request: Request) -> str:
    """FastAPI dependency that returns the current correlation ID.

    Checks the context variable first (set by LogContextMiddleware),
    then falls back to the request header, and finally generates a new UUID.
    """
    # 1. Already set by middleware for this request
    ctx_value = corr_id_ctx.get("none")
    if ctx_value != "none":
        return ctx_value

    # 2. From incoming request header
    corr_id = request.headers.get("X-Correlation-ID")
    if corr_id:
        return corr_id

    # 3. Generate new (should rarely happen — middleware runs first)
    return str(uuid.uuid4())


def get_corr_id_from_scope(scope: Mapping[str, Any]) -> str:
    """Extract the correlation ID from a raw ASGI scope.

    Used by the pure-ASGI ``LogContextMiddleware`` which does not
    have a Starlette ``Request`` object.

    """
    headers = scope.get("headers", [])
    # Headers in scope are list of (bytes, bytes)
    for key, value in headers:
        if key.lower() == b"x-correlation-id":
            return value.decode()
    return str(uuid.uuid4())
