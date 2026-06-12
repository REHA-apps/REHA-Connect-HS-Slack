# tests/test_storage_service.py
"""Tests for StorageService: caching, invalidation, cross-cache pre-population."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from app.db.records import IntegrationRecord, Provider
from app.db.services.integration_storage import IntegrationStorage
from app.db.services.workspace_storage import WorkspaceStorage
from app.db.storage_service import StorageService


def _make_integration(
    workspace_id: str = "ws-1",
    provider: Provider = Provider.SLACK,
    **kwargs,
) -> IntegrationRecord:
    return IntegrationRecord(
        id=kwargs.get("id", "integ-1"),
        workspace_id=workspace_id,
        provider=provider,
        credentials=kwargs.get("credentials", {"slack_bot_token": "xoxb-test"}),
        metadata=kwargs.get("metadata", {"slack_team_id": "T123"}),
    )


@pytest.fixture
def storage():
    """StorageService with mocked Supabase repos."""
    svc = StorageService.__new__(StorageService)
    svc.client = MagicMock()

    svc.integration_svc = IntegrationStorage.__new__(IntegrationStorage)
    svc.integration_svc.client = MagicMock()
    svc.integration_svc.client.fetch_single = AsyncMock(return_value=None)
    svc.integration_svc.integrations = MagicMock()
    svc.integration_svc.integrations.fetch_single = AsyncMock()
    svc.integration_svc.integrations.fetch_many = AsyncMock()
    svc.integration_svc.integrations.upsert = AsyncMock()
    svc.integration_svc.integrations.delete = AsyncMock()
    svc.integration_svc.integrations.update = AsyncMock()
    svc.integration_svc.integrations.count = AsyncMock()
    setattr(svc.integration_svc, "_aesgcm", None)

    svc.workspace_svc = WorkspaceStorage.__new__(WorkspaceStorage)
    svc.workspace_svc.client = MagicMock()
    svc.workspace_svc.client.fetch_single = AsyncMock(return_value=None)
    svc.workspace_svc.workspaces = MagicMock()
    svc.workspace_svc.workspaces.fetch_single = AsyncMock()
    svc.workspace_svc.workspaces.fetch_many = AsyncMock()
    svc.workspace_svc.workspaces.upsert = AsyncMock()
    svc.workspace_svc.workspaces.delete = AsyncMock()

    # Bridge the actual calls to sub-services
    svc.get_integration = svc.integration_svc.get_integration
    svc.upsert_integration = svc.integration_svc.upsert_integration
    svc.get_integration_by_slack_team_id = (
        svc.integration_svc.get_integration_by_slack_team_id
    )
    svc.get_integration_by_portal_id = svc.integration_svc.get_integration_by_portal_id

    # Helper repos
    svc.integrations = svc.integration_svc.integrations
    svc.workspaces = svc.workspace_svc.workspaces

    return svc


@pytest_asyncio.fixture(autouse=True)
async def clear_caches():
    """Clear module-level caches between tests."""
    from app.db.services import integration_storage as mod  # noqa: PLC0415

    await mod._record_cache.clear()
    await mod._hubspot_mapping_cache.clear()
    yield
    await mod._record_cache.clear()
    await mod._hubspot_mapping_cache.clear()


# --- get_integration caching ---


@pytest.mark.asyncio
async def test_get_integration_caches_result(storage):
    """Second call should return cached result, not hit DB again."""
    record = _make_integration()
    storage.integrations.fetch_single = AsyncMock(return_value=record)

    r1 = await storage.get_integration("ws-1", Provider.SLACK)
    r2 = await storage.get_integration("ws-1", Provider.SLACK)

    assert r1 is record
    assert r2 is record
    storage.integrations.fetch_single.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_invalidates_cache(storage):
    """Upsert should invalidate the record cache so next get refetches."""
    record = _make_integration()
    storage.integrations.fetch_single = AsyncMock(return_value=record)
    storage.integrations.upsert = AsyncMock(return_value=record)

    # Populate cache
    await storage.get_integration("ws-1", Provider.SLACK)
    assert storage.integrations.fetch_single.await_count == 1

    # Upsert invalidates cache
    await storage.upsert_integration(
        {
            "id": "integ-1",
            "workspace_id": "ws-1",
            "provider": Provider.SLACK,
            "credentials": {},
            "metadata": {"slack_team_id": "T123"},
        }
    )

    # Next get should hit DB again
    await storage.get_integration("ws-1", Provider.SLACK)
    EXPECTED_FETCH_CALLS = 2
    assert storage.integrations.fetch_single.await_count == EXPECTED_FETCH_CALLS


# --- Slack team ID lookup + cross-cache population ---


@pytest.mark.asyncio
async def test_get_by_slack_team_id_populates_both_caches(storage):
    """get_integration_by_slack_team_id should pre-populate the record cache."""
    record = _make_integration()
    # Mock resolution to return ws-1
    storage.integration_svc.client.fetch_single = AsyncMock(return_value={"id": "ws-1"})
    storage.integrations.fetch_single = AsyncMock(return_value=record)

    # First call - fetches and resolves
    r1 = await storage.get_integration_by_slack_team_id("T123")
    assert r1 == record

    # Second call via get_integration should use record cache
    r2 = await storage.get_integration("ws-1", Provider.SLACK)
    assert r2 == record
    # fetch_single called only once (inside r1)
    storage.integrations.fetch_single.assert_awaited_once()


# --- Portal ID lookup ---


@pytest.mark.asyncio
async def test_get_by_portal_id(storage):
    record = _make_integration(
        provider=Provider.HUBSPOT, metadata={"portal_id": "P456"}
    )
    # HubSpot lookup checks workspaces first (via client.fetch_single), then fallback
    # To test the fallback, we make fetch_single return None
    storage.integration_svc.client.fetch_single = AsyncMock(return_value=None)
    # Fallback uses integrations.fetch_single
    storage.integrations.fetch_single = AsyncMock(return_value=record)

    result = await storage.get_integration_by_portal_id("P456")
    assert result == record
    assert result.provider == Provider.HUBSPOT


@pytest.mark.asyncio
async def test_upsert_invalidates_mapping_caches(storage):
    """Upserting an integration with a Slack team ID should invalidate the
    mapping cache.
    """
    record = _make_integration()
    # Mock resolution to return ws-1
    storage.integration_svc.client.fetch_single = AsyncMock(return_value={"id": "ws-1"})
    storage.integrations.fetch_single = AsyncMock(return_value=record)
    storage.integrations.upsert = AsyncMock(return_value=record)

    # Populate both caches
    await storage.get_integration_by_slack_team_id("T123")

    from app.db.services.integration_storage import (
        _hubspot_mapping_cache,  # noqa: PLC0415
    )

    assert await _hubspot_mapping_cache.get("map:slack:T123") == "ws-1"

    # Upsert with same mapping
    await storage.upsert_integration(
        {
            "workspace_id": "ws-1",
            "provider": Provider.SLACK,
            "metadata": {"slack_team_id": "T123"},
        }
    )

    # Mapping cache should be invalidated (None or must be refetched)
    assert await _hubspot_mapping_cache.get("map:slack:T123") is None


@pytest.mark.asyncio
async def test_mapping_miss_self_healing(storage):
    """If mapping exists but record is missing in DB, mapping should be purged."""
    from app.db.services.integration_storage import (
        _hubspot_mapping_cache,  # noqa: PLC0415
    )

    # Manually seed a stale mapping
    await _hubspot_mapping_cache.set("map:slack:T-stale", "ws-stale")

    # Storage.get_integration will return None (mocked)
    storage.integrations.fetch_single = AsyncMock(return_value=None)

    # Try resolver
    # Need to mock the fallback metadata lookup as well
    storage.integration_svc.client.fetch_single = AsyncMock(return_value=None)
    storage.integrations.fetch_single = AsyncMock(return_value=None)

    result = await storage.get_integration_by_slack_team_id("T-stale")

    assert result is None
    # Slack mapping for 'T-stale' should have been invalidated
    assert await _hubspot_mapping_cache.get("map:slack:T-stale") is None
