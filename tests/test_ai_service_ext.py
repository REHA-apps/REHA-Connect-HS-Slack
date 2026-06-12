# tests/test_ai_service_ext.py
"""Extended tests for AIService: sentence formatting and keyword loading."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domains.ai.service import AIService


@pytest.fixture
def ai():
    return AIService("test")


def test_format_sentence(ai):
    """Test heuristic sentence cleaning."""
    assert ai._format_sentence("hello world") == "Hello world."
    assert ai._format_sentence("Already capitalized.") == "Already capitalized."
    assert ai._format_sentence("ends with bang!") == "Ends with bang!"
    assert ai._format_sentence("") == ""


@pytest.mark.asyncio
async def test_load_dynamic_keywords_delegation(ai):
    """Test that keywords are loaded via StorageService."""
    ai.storage = MagicMock()
    ai.storage.get_ai_intent_keywords = AsyncMock(return_value={"risk": ["cancel"]})

    result = await ai._load_dynamic_keywords()
    assert result == {"risk": ["cancel"]}
    ai.storage.get_ai_intent_keywords.assert_awaited_once_with("global_intents")
