from unittest.mock import AsyncMock, MagicMock

import pytest

from app.connectors.slack.services.handlers.registry import InteractionRegistry
from app.connectors.slack.services.slack_ui_adapter import SlackUIAdapter
from app.core.models.ui import UnifiedCard
from app.domains.common.sdk.context import Provider, UnifiedContext


@pytest.fixture
def mock_services():
    hubspot = MagicMock()
    ai = MagicMock()
    integration_service = MagicMock()
    return hubspot, ai, integration_service


def test_unified_context_from_slack_payload():
    payload = {
        "type": "block_actions",
        "user": {"id": "U123"},
        "team": {"id": "T123"},
        "channel": {"id": "C123"},
        "trigger_id": "trig_123",
        "actions": [{"action_id": "test_action", "value": "test_value"}],
    }
    context = UnifiedContext.from_slack_payload(payload, workspace_id="ws_123")

    assert context.platform == Provider.SLACK
    assert context.user_id == "U123"
    assert context.workspace_id == "ws_123"
    assert context.trigger_id == "trig_123"
    assert context.action_id == "test_action"
    assert context.value == "test_value"


def test_interaction_registry_injection(mock_services):
    hubspot, ai, integration_service = mock_services
    registry = InteractionRegistry("corr_123", hubspot, ai, integration_service)

    assert isinstance(registry.ui, SlackUIAdapter)
    assert registry.object_view.ui == registry.ui
    assert registry.action_button.ui == registry.ui
    assert registry.core_modals.ui == registry.ui


@pytest.mark.asyncio
async def test_base_interaction_handler_routing(mock_services):
    hubspot, ai, integration_service = mock_services
    registry = InteractionRegistry("corr_123", hubspot, ai, integration_service)

    # Mock a handler method
    handler = registry.action_button
    mock_method = AsyncMock()
    setattr(handler, "_handle_test_action", mock_method)

    # Register the action manually for the test
    handler._action_routes["test_action"] = mock_method

    context = UnifiedContext(
        platform=Provider.SLACK,
        user_id="U123",
        workspace_id="ws_123",
        action_id="test_action",
    )

    await handler.handle_interaction(
        context=context,
        payload={},
        integration=MagicMock(),
        messaging_service=MagicMock(),
    )

    mock_method.assert_called_once()


@pytest.mark.asyncio
async def test_slack_ui_adapter_delegation(mock_services):
    _, _, integration_service = mock_services
    adapter = SlackUIAdapter(integration_service=integration_service)

    messaging_service = AsyncMock()
    context = UnifiedContext(
        platform=Provider.SLACK,
        user_id="U123",
        workspace_id="ws_123",
        channel_id="C123",
    )
    card = UnifiedCard(title="Test Card")

    await adapter.send_card(
        context=context,
        card=card,
        integration=MagicMock(),
        messaging_service=messaging_service,
    )

    messaging_service.send_card.assert_called_once()
