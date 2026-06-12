# tests/test_hubspot_client.py
"""Tests for HubSpotClient: token refresh, association IDs, HTTP error handling."""

import httpx
import pytest
import respx

from app.providers.hubspot.client import HubSpotClient


@pytest.fixture
def client():
    return HubSpotClient(
        corr_id="test",
        access_token="at-test",
        refresh_token="rt-test",
    )


# --- Association type IDs ---


def test_note_assoc_type_ids_completeness():
    """_NOTE_ASSOC_TYPE_IDS covers all standard CRM object types."""
    expected_keys = {"contact", "deal", "company", "ticket", "task"}
    assert set(HubSpotClient._NOTE_ASSOC_TYPE_IDS.keys()) == expected_keys


def test_note_assoc_type_ids_are_ints():
    for key, val in HubSpotClient._NOTE_ASSOC_TYPE_IDS.items():
        assert isinstance(val, int), f"{key} should map to int, got {type(val)}"


# --- HTTP error handling ---


@pytest.mark.asyncio
@respx.mock
async def test_get_returns_none_on_404(client):
    """GET returning 404 should return None, not raise."""
    respx.get("https://api.hubapi.com/objects/contacts/123").mock(
        return_value=httpx.Response(httpx.codes.NOT_FOUND)
    )
    result = await client._raw_request("GET", "/objects/contacts/123")
    assert result is None


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_400(client):
    """Non-404 errors should raise HTTPStatusError."""
    respx.get("https://api.hubapi.com/objects/contacts/bad").mock(
        return_value=httpx.Response(
            httpx.codes.BAD_REQUEST, json={"message": "Bad request"}
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client._raw_request("GET", "/objects/contacts/bad")


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_500(client):
    """Server errors should raise."""
    respx.post("https://api.hubapi.com/objects/contacts").mock(
        return_value=httpx.Response(
            httpx.codes.INTERNAL_SERVER_ERROR, json={"message": "Server error"}
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client._raw_request("POST", "/objects/contacts", json={"properties": {}})


# --- Token refresh ---


@pytest.mark.asyncio
@respx.mock
async def test_token_refresh_on_401(client):
    """401 triggers token refresh and retries the original request."""
    # First call: 401
    route = respx.get("https://api.hubapi.com/objects/contacts/1")
    route.side_effect = [
        httpx.Response(httpx.codes.UNAUTHORIZED),
        httpx.Response(httpx.codes.OK, json={"id": "1"}),
    ]

    # Mock the refresh endpoint
    respx.post("https://api.hubapi.com/oauth/v1/token").mock(
        return_value=httpx.Response(
            httpx.codes.OK,
            json={
                "access_token": "at-new",
                "refresh_token": "rt-new",
            },
        )
    )

    callback_called = False

    async def on_refresh(at, rt, expires_at):
        nonlocal callback_called
        callback_called = True

    client.on_token_refresh = on_refresh

    result = await client.request("GET", "/objects/contacts/1")
    assert result == {"id": "1"}
    assert client.access_token == "at-new"
    assert callback_called
