import asyncio  # noqa: D100
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.exceptions import AppError, HubSpotAPIError, SlackAPIError
from app.core.logging import get_corr_id, get_logger, log_context, setup_uvicorn_logging
from app.core.middleware import LogContextMiddleware, SecurityGuardMiddleware
from app.utils.helpers import HTTPClient

logger = get_logger("app.main")

# Silence noisy health checks and standardize uvicorn logs
setup_uvicorn_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: PLR0915
    """Manages the application lifecycle.

    Handles startup initialization (logging, background tasks, registry)
    and ensuring shared HTTP clients are closed on shutdown.

    Args:
        app: The FastAPI application instance.

    """
    with log_context("startup"):
        logger.info("Application starting up")

        try:
            # 1. Validate configuration
            settings.validate_all()

            # Background maintenance workers have been moved to lambda_scheduler.py
            # which is triggered by EventBridge to prevent Lambda freezes.

            # Initialize all connectors (channel registry + routers) in one step.
            # setup_connectors() is the single registration point for all platforms.
            from app.connectors import setup_connectors
            from app.connectors.registry import registry

            setup_connectors()

            # Register dynamic connector routers to the main app instance
            # This must happen after setup_connectors() populates the registry
            for connector_router in registry.get_all_routers():
                app.include_router(connector_router, prefix="/api")

            logger.info("Startup complete")

            # Warm up the Sentiment Engine in the background so the first
            # /reha search doesn't block on a 30-second model cold-start.
            # This fires-and-forgets; the model will be ready within seconds.
            async def _warmup_sentiment():
                try:
                    from app.domains.crm.hubspot.sentiment_service import (
                        SentimentService,
                    )

                    await SentimentService().async_initialize()
                except Exception as exc:
                    logger.warning(
                        "Sentiment Engine warm-up failed (non-fatal): %s", exc
                    )

            asyncio.create_task(_warmup_sentiment())
        except Exception as e:
            logger.error("Critical error during startup lifespan: %s", e, exc_info=True)
            # Web server still starts to respond to health checks

    yield

    with log_context("shutdown"):
        logger.info("Shutting down")

        # Shutdown Ghosting Monitor
        if settings.ENABLE_BACKGROUND_WORKERS:
            from app.domains.crm.hubspot.ghosting_monitor import GhostingMonitor

            await GhostingMonitor.get_instance().shutdown()

        await HTTPClient.close(corr_id="shutdown")

        from app.providers.slack.client import close_shared_slack_session

        await close_shared_slack_session()

        logger.info("Shutdown complete")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(SecurityGuardMiddleware)
app.add_middleware(LogContextMiddleware)
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    if settings.is_prod
    else ["*"],  # noqa: E501
)

_CORS_ORIGINS: dict[str, list[str]] = {
    "dev": ["*"],
    "staging": [
        "https://app.hubspot.com",
        "https://app-eu1.hubspot.com",
    ],
    "prod": [
        "https://app.hubspot.com",
        "https://app-eu1.hubspot.com",
        "https://rehaapps.com",
        "https://www.rehaapps.com",
    ],
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS.get(settings.ENV, _CORS_ORIGINS["prod"]),
    allow_credentials=True,
    allow_methods=["GET", "POST"] if settings.is_prod else ["*"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Correlation-ID",
        "X-Hubspot-Signature",
        "X-Hubspot-Signature-V3",
        "X-Hubspot-Request-Timestamp",
        "X-Slack-Signature",
        "X-Slack-Request-Timestamp",
    ]
    if settings.is_prod
    else ["*"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET_KEY.get_secret_value(),
    session_cookie="reha_session",
    same_site="lax",
    https_only=settings.is_prod,
)


@app.get("/api/debug/logs")
async def get_debug_logs(lines: int = 200):
    """Retrieves the last N lines from the local log file for troubleshooting.

    Gated to dev environments only for security. Uses asyncio.to_thread to
    avoid blocking the event loop on file I/O.
    """
    if not settings.is_dev:
        raise HTTPException(
            status_code=403, detail="Debug logs only available in development"
        )

    import os

    log_file = os.path.join(os.getcwd(), "reha_app.log")
    if not os.path.exists(log_file):
        return {"error": "Log file not found"}

    def _read_tail(path: str, n: int) -> list[str]:
        with open(path) as f:  # noqa: PTH123
            return f.readlines()[-n:]

    try:
        content = await asyncio.to_thread(_read_tail, log_file, lines)
        return {"logs": content}
    except Exception as e:
        return {"error": str(e)}


@app.exception_handler(AppError)
async def app_exception_handler(request: Request, exc: AppError) -> JSONResponse:
    """Handles custom domain exceptions and returns structured JSON responses."""
    corr_id = await get_corr_id(request)
    status_code = exc.status_code

    with log_context(corr_id):
        logger.warning(
            "%s: %s (Status: %d)", exc.__class__.__name__, exc.message, status_code
        )

    return JSONResponse(
        status_code=status_code,
        content={
            "error": exc.__class__.__name__,
            "message": exc.message,
            "correlation_id": corr_id,
        },
    )


@app.exception_handler(HubSpotAPIError)
async def hubspot_exception_handler(
    request: Request, exc: HubSpotAPIError
) -> JSONResponse:
    """Specialized handler for HubSpot connectivity issues."""
    corr_id = await get_corr_id(request)
    logger.error("HubSpot Connectivity Error [%s]: %s", corr_id, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "HubSpotError",
            "message": "A connection error occurred with the HubSpot API.",
            "correlation_id": corr_id,
        },
    )


@app.exception_handler(SlackAPIError)
async def slack_exception_handler(request: Request, exc: SlackAPIError) -> JSONResponse:
    """Specialized handler for Slack connectivity issues."""
    corr_id = await get_corr_id(request)
    logger.error("Slack Connectivity Error [%s]: %s", corr_id, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "SlackError",
            "message": "A connection error occurred with the Slack API.",
            "correlation_id": corr_id,
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler to capture all unhandled errors.

    Logs the error with correlation ID and returns a clean JSON response.

    Args:
        request: The incoming request.
        exc: The unhandled exception.

    Returns:
        A 500 JSON response.

    """
    corr_id = await get_corr_id(request)

    with log_context(corr_id):
        logger.error("Unhandled exception: %s", exc, exc_info=True)

    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "detail": str(exc) if settings.DEBUG else "An unexpected error occurred.",
            "correlation_id": corr_id,
        },
    )


app.include_router(api_router, prefix="/api")


@app.get("/")
@app.head("/")
async def root() -> dict[str, str]:
    """Basic health check endpoint to verify the service is running.

    Returns:
        A mapping containing the application name.

    """
    return {"message": f"{settings.APP_NAME} is running"}


# Force uvicorn reload

# Trigger reload

# Trigger reload 2

# Trigger reload 3

# Trigger reload 4

# Trigger reload 5

# Trigger reload 6
