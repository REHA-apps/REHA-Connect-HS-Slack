# tests/test_ai_service.py
"""Tests for AIService: scoring, null fields, all object types, intent detection."""

import pytest

from app.core.logging import corr_id_ctx, log_context
from app.domains.ai.service import (
    AICompanyAnalysis,
    AIContactAnalysis,
    AIDealAnalysis,
    AIService,
    AITaskAnalysis,
    AITicketAnalysis,
)


@pytest.fixture
def ai():
    return AIService("test")


# --- Logging Context ---


def test_corr_id_in_context():
    with log_context("custom-corr"):
        assert corr_id_ctx.get() == "custom-corr"


def test_default_corr_id():
    assert corr_id_ctx.get() == "none"


# --- Contact scoring ---


def test_score_normalization_clamp(ai):
    """Score must be 0-100 even with maxed-out features."""
    props = {
        "hs_analytics_num_visits": "9999",
        "lifecyclestage": "salesqualifiedlead",
        "company": "Acme Corp",
        "email": "test@test.com",
    }
    score = ai.generate_score(props)
    MAX_SCORE = 100
    assert 0 <= score <= MAX_SCORE


def test_score_zero_for_empty_props(ai):
    """Contact with no data scores 0."""
    score = ai.generate_score({})
    assert score == 0


@pytest.mark.asyncio
async def test_null_fields_handled(ai):
    """None/missing properties don't crash."""
    contact = {"properties": {}}
    # In the refactored service, we call analyze_contact for insights
    result = await ai.analyze_contact(contact)
    assert isinstance(result.insight, str)


@pytest.mark.asyncio
async def test_generate_contact_insight_with_engagements(ai):
    """Engagements are properly formatted into the insight string."""
    contact = {"properties": {"firstname": "John"}}
    engagements = [
        {
            "_engagement_type": "meetings",
            "properties": {
                "hs_meeting_title": "Test Meeting",
                "hs_timestamp": "2026-02-27T07:00:00Z",
            },
            "id": "111",
        }
    ]
    result = await ai.analyze_contact(contact, engagements=engagements)
    assert "Test Meeting" in result.insight
    assert "*Recent Engagements*:" in result.insight
    assert "Meeting" in result.insight


def test_extreme_visits_value(ai):
    """Very large visit count doesn't overflow."""
    props = {"hs_analytics_num_visits": "999999999"}
    score = ai.generate_score(props)
    MAX_SCORE = 100
    assert 0 <= score <= MAX_SCORE


# --- Contact analysis ---


@pytest.mark.asyncio
async def test_analyze_contact(ai):
    contact = {
        "properties": {
            "firstname": "Alice",
            "lastname": "Smith",
            "email": "alice@example.com",
        }
    }
    result = await ai.analyze_contact(contact)
    assert isinstance(result, AIContactAnalysis)
    assert result.insight
    assert result.score is not None
    assert result.insight


# --- Deal analysis ---


@pytest.mark.asyncio
async def test_analyze_deal(ai):
    deal = {
        "properties": {
            "dealname": "Enterprise Contract",
            "amount": "50000",
            "dealstage": "contractsent",
        }
    }
    result = await ai.analyze_deal(deal)
    assert isinstance(result, AIDealAnalysis)
    assert "Enterprise Contract" in result.insight
    assert result.risk == "Open"


@pytest.mark.asyncio
async def test_analyze_deal_closed_won(ai):
    deal = {"properties": {"dealname": "Won Deal", "dealstage": "closedwon"}}
    result = await ai.analyze_deal(deal)
    assert result.risk == "Won"


@pytest.mark.asyncio
async def test_analyze_deal_closed_lost(ai):
    deal = {"properties": {"dealname": "Lost Deal", "dealstage": "closedlost"}}
    result = await ai.analyze_deal(deal)
    assert result.risk == "Lost"


# --- Company analysis ---


@pytest.mark.asyncio
async def test_analyze_company_active(ai):
    # Active/Healthy needs 2 factors (visits > 10, contacts >= 2, deals >= 1)
    props = {"name": "Normal Co", "hs_analytics_num_visits": "11"}
    assoc = {"contacts": [{}, {}]}
    company = {"properties": props}
    result = await ai.analyze_company(company, associated_objects=assoc)
    assert result.health == "Healthy"


@pytest.mark.asyncio
async def test_analyze_company_dormant(ai):
    company = {"properties": {"name": "Dormant Corp", "num_associated_deals": "0"}}
    result = await ai.analyze_company(company)
    assert isinstance(result, AICompanyAnalysis)
    assert result.health == "At Risk"


@pytest.mark.asyncio
async def test_analyze_company_strategic(ai):
    # Strong needs 3 factors
    props = {"name": "Big Corp", "hs_analytics_num_visits": "20"}
    assoc = {"contacts": [{}, {}], "deals": [{}]}
    company = {"properties": props}
    result = await ai.analyze_company(company, associated_objects=assoc)
    assert result.health == "Strong"


# --- Ticket analysis ---


@pytest.mark.asyncio
async def test_analyze_ticket(ai):
    ticket = {
        "properties": {
            "subject": "Login broken",
            "hs_pipeline_stage": "1",
            "hs_ticket_priority": "HIGH",
        }
    }
    result = await ai.analyze_ticket(ticket)
    assert isinstance(result, AITicketAnalysis)
    assert result.insight


@pytest.mark.asyncio
async def test_analyze_task(ai):
    task = {
        "properties": {
            "hs_task_subject": "Follow up",
            "hs_task_status": "NOT_STARTED",
        }
    }
    result = await ai.analyze_task(task)
    assert isinstance(result, AITaskAnalysis)
    assert result.insight


# --- Polymorphic dispatch ---


@pytest.mark.asyncio
async def test_analyze_polymorphic_contact(ai):
    obj = {"properties": {"firstname": "Test"}}
    result = await ai.analyze_polymorphic(obj, "contact")
    assert isinstance(result, AIContactAnalysis)


@pytest.mark.asyncio
async def test_analyze_polymorphic_deal(ai):
    obj = {"properties": {"dealname": "Deal"}}
    result = await ai.analyze_polymorphic(obj, "deal")
    assert isinstance(result, AIDealAnalysis)


@pytest.mark.asyncio
async def test_analyze_polymorphic_unknown_fallback(ai):
    obj = {"properties": {}}
    result = await ai.analyze_polymorphic(obj, "unknown_type")
    assert isinstance(result, AIContactAnalysis)


@pytest.mark.asyncio
async def test_analyze_polymorphic_malformed_input(ai):
    """None or empty dicts should fallback to safe error object instead of crashing."""
    # 1. Null object
    r1 = await ai.analyze_polymorphic(None, "contact")
    assert r1.score == 0
    assert "unavailable" in r1.insight.lower()

    # 2. Completely empty dict (missing properties)
    r2 = await ai.analyze_polymorphic({}, "contact")
    assert r2.score == 0

    # 3. Non-dict properties
    r3 = await ai.analyze_polymorphic({"properties": None}, "contact")
    assert r3.score == 0


# --- Intent detection is handled via smarter search now ---
# If specific intent detection is needed, we should re-implement it.
# For now, we'll comment out these tests if the method is gone.
