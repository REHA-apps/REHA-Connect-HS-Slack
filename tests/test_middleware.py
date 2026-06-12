# tests/test_middleware.py
"""Tests for CorrelationIdMiddleware: corr_id generation, propagation, health skip."""

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core.middleware import LogContextMiddleware as CorrelationIdMiddleware


@pytest.fixture
def app():
    _app = FastAPI()
    _app.add_middleware(CorrelationIdMiddleware)

    @_app.get("/test")
    async def test_endpoint(request: Request):
        return {"corr_id": request.state.corr_id}

    @_app.get("/health")
    async def health():
        return {"status": "ok"}

    return _app


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_generates_corr_id(client):
    """Response contains X-Correlation-Id even when none is sent."""
    resp = await client.get("/test")
    assert resp.status_code == httpx.codes.OK
    assert "x-correlation-id" in resp.headers
    # LogContextMiddleware generates UUID-style IDs (no prefix)
    assert len(resp.headers["x-correlation-id"]) > 8


@pytest.mark.asyncio
async def test_propagates_incoming_corr_id(client):
    """Provided X-Correlation-Id header is echoed back."""
    resp = await client.get("/test", headers={"X-Correlation-Id": "my-custom-id"})
    assert resp.headers["x-correlation-id"] == "my-custom-id"
    assert resp.json()["corr_id"] == "my-custom-id"


@pytest.mark.asyncio
async def test_health_check_skips_logging(client):
    """/health returns 200 and still sets corr_id on state."""
    resp = await client.get("/health")
    assert resp.status_code == httpx.codes.OK
    assert resp.json() == {"status": "ok"}
