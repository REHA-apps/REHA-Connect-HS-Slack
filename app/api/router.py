import datetime  # noqa: D100
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.public.contact_router import router as contact_router
from app.core.logging import get_logger
from app.domains.billing.router import router as billing_router
from app.domains.crm.user_mappings_router import router as user_mappings_router

# Routers are initialized dynamically via the lifespan startup block in main.py

api_router = APIRouter()


logger = get_logger("api.router")


@api_router.get("/health")
@api_router.head("/health")
async def health_check(deep: bool = False) -> Any:
    """Liveness check for Render.

    Setting deep=true will verify database connectivity, which is avoided
    during startup to prevent port scan timeouts on restricted infrastructure.
    """
    response: dict[str, Any] = {
        "status": "ok",
        "app": "crm-connectors",
        "timestamp": str(datetime.datetime.now(datetime.UTC)),
    }

    if not deep:
        return response

    try:
        from app.db.supabase_client import SupabaseClient

        db = SupabaseClient(corr_id="health-check")
        # Use a lightweight existence check rather than a full COUNT(*) scan
        # to avoid timeout on large tables.
        row = await db.fetch_single("workspaces", {}, select=["id"])
        response.update(
            {
                "database": "connected",
                "metrics": {"has_workspaces": row is not None},
            }
        )
        return response
    except Exception as e:
        logger.exception("Deep health check failed")
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "disconnected",
                "detail": str(e),
                "type": type(e).__name__,
            },
        )


# Public pages
api_router.include_router(contact_router)
api_router.include_router(billing_router)

# Internal Domain Endpoints
api_router.include_router(user_mappings_router)

# Note: Connector-specific routers are now included in main.py lifespan to avoid import-time side effects
