# tests/test_card_builder.py
"""Tests for CardBuilder: block structure validation for each CRM object type."""

import pytest

from app.connectors.slack.slack_renderer import SlackRenderer
from app.core.models.ui import UnifiedCard
from app.domains.ai.service import (
    AICompanyAnalysis,
    AIContactAnalysis,
    AIDealAnalysis,
    AITaskAnalysis,
    AITicketAnalysis,
)
from app.domains.crm.ui.card_builder import CardBuilder
from app.providers.hubspot.renderer import HubSpotRenderer


@pytest.fixture
def builder():
    return CardBuilder()


@pytest.fixture
def contact_analysis():
    return AIContactAnalysis(
        insight="High-value lead.",
        score=85,
        score_reason="Frequent visitor with qualified lifecycle.",
        next_best_action="Schedule a demo.",
        next_action_reason="Contact shows buying signals.",
        engagement_factors="High visit count plus MQL status.",
    )


@pytest.fixture
def deal_analysis():
    return AIDealAnalysis(
        insight="Enterprise deal in negotiation.",
        risk="Open",
        next_best_action="Follow up with decision maker.",
        score=75,
        score_reason="High value deal.",
    )


@pytest.fixture
def company_analysis():
    return AICompanyAnalysis(
        insight="Acme Corp with 5 contacts and 3 deals.",
        health="Active",
        next_best_action="Schedule account review.",
    )


@pytest.fixture
def ticket_analysis():
    return AITicketAnalysis(
        insight="Login broken — High priority.",
        urgency="High",
        next_best_action="Assign to engineering.",
    )


@pytest.fixture
def task_analysis():
    return AITaskAnalysis(
        insight="Follow up call pending.",
        status_label="Not Started",
        next_best_action="Schedule call.",
    )


def _assert_valid_unified_card(result):
    """Helper: verify result is a UnifiedCard IR."""
    assert isinstance(result, UnifiedCard), f"Expected UnifiedCard, got {type(result)}"
    assert result.title
    assert result.emoji


# --- Contact ---


def test_build_contact(builder, contact_analysis):
    contact = {
        "id": "123",
        "properties": {
            "firstname": "Alice",
            "lastname": "Smith",
            "email": "alice@example.com",
            "company": "Acme Corp",
        },
    }
    result = builder.build_contact(contact, contact_analysis)
    _assert_valid_unified_card(result)


# --- Deal ---


def test_build_deal(builder, deal_analysis):
    deal = {
        "id": "456",
        "properties": {
            "dealname": "Enterprise Contract",
            "amount": "50000",
            "dealstage": "negotiation",
        },
    }
    result = builder.build_deal(deal, deal_analysis)
    _assert_valid_unified_card(result)


# --- Company ---


def test_build_company(builder, company_analysis):
    company = {
        "id": "789",
        "properties": {
            "name": "Acme Corp",
            "domain": "acme.com",
            "num_associated_contacts": "5",
        },
    }
    result = builder.build_company(company, company_analysis)
    _assert_valid_unified_card(result)


# --- Ticket ---


def test_build_ticket(builder, ticket_analysis):
    ticket = {
        "id": "101",
        "properties": {
            "subject": "Login broken",
            "hs_pipeline_stage": "1",
            "hs_ticket_priority": "HIGH",
        },
    }
    result = builder.build_ticket(ticket, ticket_analysis)
    _assert_valid_unified_card(result)


# --- Task ---


def test_build_task(builder, task_analysis):
    task = {
        "id": "202",
        "properties": {
            "hs_task_subject": "Follow up",
            "hs_task_status": "NOT_STARTED",
        },
    }
    result = builder.build_task(task, task_analysis)
    _assert_valid_unified_card(result)


# --- Polymorphic build ---


def test_build_dispatches_contact(builder, contact_analysis):
    obj = {
        "id": "123",
        "type": "contact",
        "properties": {"firstname": "Test"},
    }
    result = builder.build(obj, contact_analysis)
    _assert_valid_unified_card(result)


def test_build_dispatches_deal(builder, deal_analysis):
    obj = {
        "id": "456",
        "type": "deal",
        "properties": {"dealname": "Test Deal"},
    }
    result = builder.build(obj, deal_analysis)
    _assert_valid_unified_card(result)


# --- Empty and modal ---


def test_build_empty(builder):
    result = builder.build_empty("No results found.")
    _assert_valid_unified_card(result)


def test_renderer_slack(builder, contact_analysis):
    contact = {"id": "123", "properties": {"firstname": "Alice"}}
    unified_card = builder.build_contact(contact, contact_analysis)

    renderer = SlackRenderer()
    slack_payload = renderer.render(unified_card)

    assert "blocks" in slack_payload
    assert len(slack_payload["blocks"]) > 0


def test_renderer_hubspot(builder, contact_analysis):
    contact = {"id": "123", "properties": {"firstname": "Alice"}}
    unified_card = builder.build_contact(contact, contact_analysis)

    renderer = HubSpotRenderer()
    hubspot_payload = renderer.render("123", unified_card)

    assert hubspot_payload["objectId"] == "123"
    assert hubspot_payload["title"] == "Alice"
    assert "metrics" in hubspot_payload
