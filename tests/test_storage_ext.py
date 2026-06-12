# tests/test_storage_ext.py
"""Extended tests for StorageService and Repository: list() and intent keywords."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.records import AIKeywordRecord
from app.db.storage_service import StorageService


@pytest.fixture
def storage():
    svc = StorageService(corr_id="test")
    svc.ai_keywords = MagicMock()
    svc.ai_keywords.fetch_many = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_get_ai_intent_keywords(storage):
    """Verify that get_ai_intent_keywords calls repository.list()."""
    mock_keywords = [AIKeywordRecord(id="1", category="risk", keyword="cancel")]
    storage.ai_keywords.fetch_many.return_value = mock_keywords

    result = await storage.get_ai_intent_keywords("test_key")
    assert "risk" in result
    assert "cancel" in result["risk"]
    storage.ai_keywords.fetch_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_repository_list():
    """Verify that SupabaseRepository.list() calls fetch_many and maps to models."""
    from app.db.records import AIKeywordRecord
    from app.db.repository import SupabaseRepository

    mock_client = MagicMock()
    mock_client.fetch_many = AsyncMock(
        return_value=[
            {"id": "1", "category": "risk", "keyword": "churn", "priority": 5}
        ]
    )

    repo = SupabaseRepository[AIKeywordRecord](
        table="ai_keywords", model=AIKeywordRecord, client=mock_client
    )

    results = await repo.list(limit=10)
    assert len(results) == 1
    assert isinstance(results[0], AIKeywordRecord)
    assert results[0].keyword == "churn"
    mock_client.fetch_many.assert_awaited_once_with("ai_keywords", {}, limit=10)
