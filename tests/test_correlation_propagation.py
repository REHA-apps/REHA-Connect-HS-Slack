# tests/test_correlation_propagation.py  # noqa: D100
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient, Response

from app.core.logging import corr_id_ctx, get_logger
from app.core.middleware import LogContextMiddleware
from app.db.storage_service import StorageService
from app.utils.helpers import HTTPClient

logger = get_logger("test.propagation")
# Ensure logs propagate to root for caplog capture
logger.propagate = True


@pytest.fixture
def app():
    _app = FastAPI()
    _app.add_middleware(LogContextMiddleware)

    @_app.get("/propagate")
    async def propagate_endpoint(request: Request):
        # 1. Log something
        logger.info("Inside endpoint")

        # 2. Call DB (StorageService)
        # We get corr_id from context now
        corr_id = corr_id_ctx.get()
        storage = StorageService(corr_id)
        await storage.get_workspace("ws-123")

        # 3. Call External API
        client = HTTPClient.get_client(corr_id=corr_id)
        await client.get("https://api.external.com/data")

        return {"status": "ok"}

    return _app


@pytest.mark.asyncio
@respx.mock
async def test_correlation_propagation_e2e(app, caplog):
    caplog.set_level(logging.INFO)

    # Mock external API to verify headers
    route = respx.get("https://api.external.com/data").mock(
        return_value=Response(200, json={})
    )

    # Mock StorageService dependencies
    # We need to mock SupabaseClient so it doesn't try to connect,
    # and its fetch_single must be an AsyncMock.
    mock_async_client = MagicMock()
    mock_async_client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"id": "ws-123"}])
    )

    with patch(
        "app.db.supabase_client.get_async_client",
        new=AsyncMock(return_value=mock_async_client),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Send request with custom corr_id
            custom_id = "test-corr-123"
            resp = await client.get(
                "/propagate", headers={"X-Correlation-Id": custom_id}
            )

            assert resp.status_code == httpx.codes.OK
            assert resp.headers["x-correlation-id"] == custom_id

            # 1. Verify External API call header
            assert route.calls.last.request.headers["x-correlation-id"] == custom_id

            # 2. Verify Log entries in caplog
            # The ContextFilter injects 'corr_id' into the record.
            # We iterate over records and check for our log message AND the
            # corr_id attribute.
            found_log = False
            for record in caplog.records:
                if "Inside endpoint" in record.message:
                    # Depending on how the filter is attached, it might be in
                    # 'corr_id' or 'extra'
                    # Our ContextFilter sets record.corr_id
                    if getattr(record, "corr_id", None) == custom_id:
                        found_log = True
                        break

            if not found_log:
                # Debugging aid: print what we found
                ids = [getattr(r, "corr_id", "MISSING") for r in caplog.records]
                msgs = [r.message for r in caplog.records]
                print(f"Captured Logs: {list(zip(msgs, ids))}")

            assert found_log, (
                f"Log with message 'Inside endpoint' and corr_id='{custom_id}' "
                "not found."
            )

            # 3. Verify DB call was made (table chain was invoked)
            mock_async_client.table.assert_called()
