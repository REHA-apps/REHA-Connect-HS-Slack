"""P3 tests: support ticket, token revocation, associate_object v4 path."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.domains.crm.hubspot.service import HubSpotService
from app.providers.hubspot.client import HubSpotClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> HubSpotClient:
    return HubSpotClient(
        corr_id="test",
        access_token="at-test",
        refresh_token="rt-test",
    )


@pytest.fixture
def service() -> HubSpotService:
    svc = HubSpotService(corr_id="test")
    svc.storage = MagicMock()
    return svc


# ---------------------------------------------------------------------------
# Test 1 — create_support_ticket raises when token is missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_support_ticket_raises_without_token(monkeypatch):
    """Missing HUBSPOT_SUPPORT_ACCESS_TOKEN must raise ValueError, not silently fail."""
    from app.core.config import settings

    monkeypatch.setattr(
        settings,
        "HUBSPOT_SUPPORT_ACCESS_TOKEN",
        SecretStr(""),
    )
    svc = HubSpotService(corr_id="test")

    with pytest.raises(ValueError, match="Support portal credentials missing"):
        await svc.create_support_ticket({"subject": "test ticket"})


# ---------------------------------------------------------------------------
# Test 2 — token revocation callback fires on invalid_grant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_token_revocation_callback_on_invalid_grant(client):
    """_refresh_token must call on_token_revoked when HubSpot returns invalid_grant."""
    # Mock the refresh endpoint to return 400 invalid_grant
    respx.post("https://api.hubapi.com/oauth/v1/token").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token has been revoked",
            },
        )
    )

    revocation_called = False

    async def on_revoked() -> None:
        nonlocal revocation_called
        revocation_called = True

    client.on_token_revoked = on_revoked

    result = await client._refresh_token()

    assert result is None, "Should return None on failed refresh"
    assert revocation_called, "on_token_revoked callback must have been called"


# ---------------------------------------------------------------------------
# Test 3 — associate_object uses CRM v4 PUT endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_associate_object_uses_v4_put_endpoint(service):
    """associate_object must issue a PUT to the CRM v4 association default endpoint."""
    # Mock the v4 PUT endpoint
    route = respx.put(
        "https://api.hubapi.com/crm/v4/objects/contacts/123/associations/default/deals/456"
    ).mock(return_value=httpx.Response(200, json={}))

    # Wire up a mock client
    mock_client = HubSpotClient(
        corr_id="test",
        access_token="at-test",
        refresh_token=None,
    )

    async def _mock_get_client(workspace_id: str, **kwargs):
        return mock_client

    service.get_client = _mock_get_client

    await service.associate_object(
        workspace_id="ws-test",
        from_type="contact",
        from_id="123",
        to_type="deal",
        to_id="456",
    )

    assert route.called, "PUT to CRM v4 default association endpoint was not called"
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Test 4 — search_props_for classmethod returns correct properties
# ---------------------------------------------------------------------------


def test_search_props_for_contacts():
    """search_props_for classmethod must return the canonical property list."""
    props = HubSpotClient.search_props_for("contacts")
    assert "email" in props
    assert "firstname" in props


def test_search_props_for_unknown_type():
    """search_props_for must return empty list for unknown types."""
    assert HubSpotClient.search_props_for("unknown_object") == []
