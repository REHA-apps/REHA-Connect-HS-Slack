from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.slack.services.service import InteractionService
from app.db.records import IntegrationRecord
from app.domains.crm.integration_service import (
    IntegrationService as DomainIntegrationService,
)


@pytest.fixture
def mock_integration_service():
    svc = MagicMock(spec=DomainIntegrationService)
    svc.is_pro_workspace = AsyncMock(return_value=False)
    svc.check_feature_access = AsyncMock(return_value=False)
    # mock slack client
    mock_slack_client = AsyncMock()
    mock_slack_client.views_open = AsyncMock(return_value={"view": {"id": "v-123"}})
    svc.get_slack_client = AsyncMock(return_value=mock_slack_client)
    return svc


@pytest.fixture
def interaction_service(mock_integration_service):
    # Mock bot token resolution in InteractionService
    with patch.object(
        InteractionService, "_resolve_bot_token", return_value="xoxb-mock-token"
    ):
        yield InteractionService(ai=None, integration_service=mock_integration_service)


@pytest.mark.asyncio
async def test_free_view_action_not_gated(
    interaction_service, mock_integration_service
):
    """Actions like view_contact_deals should NOT be gated by Pro tier checks."""
    payload = {
        "type": "block_actions",
        "trigger_id": "trig-123",
        "team": {"id": "team-123"},
        "actions": [{"action_id": "view_contact_deals", "value": "contact:123"}],
    }
    integration = MagicMock(spec=IntegrationRecord)
    integration.workspace_id = "ws-123"
    integration.slack_bot_token = "xoxb-mock"

    background_tasks = MagicMock()

    # Call handle_fast_path_action
    response = await interaction_service.handle_fast_path_action(
        payload=payload,
        corr_id="test-corr",
        background_tasks=background_tasks,
        integration=integration,
    )

    assert response is not None
    assert response.status_code == 200

    # check_feature_access should NOT have been called because it is a free action
    mock_integration_service.check_feature_access.assert_not_awaited()

    # The background task should have been added to run handle_interaction, not upgrade_nudge
    background_tasks.add_task.assert_called_once()
    # Check that the added task is the handle_interaction wrapper, not upgrade nudge
    task_args = background_tasks.add_task.call_args[0]
    assert "handle_interaction" in task_args[2].__name__


@pytest.mark.asyncio
async def test_gated_note_action_is_gated(
    interaction_service, mock_integration_service
):
    """Actions like open_add_note_modal must be gated by Pro tier checks."""
    payload = {
        "type": "block_actions",
        "trigger_id": "trig-123",
        "team": {"id": "team-123"},
        "actions": [{"action_id": "open_add_note_modal", "value": "contact:123"}],
    }
    integration = MagicMock(spec=IntegrationRecord)
    integration.workspace_id = "ws-123"
    integration.slack_bot_token = "xoxb-mock"

    background_tasks = MagicMock()

    # Call handle_fast_path_action
    response = await interaction_service.handle_fast_path_action(
        payload=payload,
        corr_id="test-corr",
        background_tasks=background_tasks,
        integration=integration,
    )

    assert response is not None
    assert response.status_code == 200

    # check_feature_access MUST be called with feature_id="note_logging"
    mock_integration_service.check_feature_access.assert_awaited_once_with(
        "ws-123", "note_logging"
    )

    # Since check_feature_access returned False (mocked), upgrade_nudge should be added as a task
    background_tasks.add_task.assert_called_once()
    task_args = background_tasks.add_task.call_args[0]
    assert "upgrade_nudge" in task_args[2].__name__


@pytest.mark.asyncio
async def test_gated_feature_click_on_pro_workspace_rewrites_action_id(
    interaction_service, mock_integration_service
):
    """If a gated feature click action is received for a Pro workspace, it should rewrite action_id to original action."""
    # Mock check_feature_access to return True (workspace has access / is Pro)
    mock_integration_service.check_feature_access.return_value = True

    payload = {
        "type": "block_actions",
        "trigger_id": "trig-123",
        "team": {"id": "team-123"},
        "actions": [
            {
                "action_id": "gated_feature_click:task_logging",
                "value": "task:contact:123",
            }
        ],
    }
    integration = MagicMock(spec=IntegrationRecord)
    integration.workspace_id = "ws-123"
    integration.slack_bot_token = "xoxb-mock"

    background_tasks = MagicMock()

    # Call handle_fast_path_action (which determines if we should route to background task)
    response = await interaction_service.handle_fast_path_action(
        payload=payload,
        corr_id="test-corr",
        background_tasks=background_tasks,
        integration=integration,
    )

    assert response is not None
    assert response.status_code == 200

    # Since check_feature_access returned True, it should schedule handle_interaction background task
    background_tasks.add_task.assert_called_once()
    task_args = background_tasks.add_task.call_args[0]
    assert "handle_interaction" in task_args[2].__name__

    # Now let's test handle_interaction directly to verify action_id rewriting
    # Mock registry and handler
    mock_handler = AsyncMock()
    interaction_service.ai = MagicMock()

    with patch(
        "app.connectors.slack.services.service.InteractionRegistry"
    ) as MockRegistry:
        mock_registry = MockRegistry.return_value
        mock_registry.get_handler.return_value = mock_handler

        messaging_service = MagicMock()

        await interaction_service.handle_interaction(
            payload=payload,
            integration=integration,
            messaging_service=messaging_service,
            corr_id="test-corr",
        )

        # Verify action_id was rewritten to 'open_add_task_modal'
        mock_registry.get_handler.assert_called_once_with(
            payload, action_id="open_add_task_modal"
        )
        assert payload["actions"][0]["action_id"] == "open_add_task_modal"
