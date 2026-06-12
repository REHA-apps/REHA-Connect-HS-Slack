from unittest.mock import AsyncMock, MagicMock, patch  # noqa: D100

import pytest

from app.connectors.registry import registry
from app.db.records import PlanTier, Provider
from app.domains.ai.service import AIService, AITaskAnalysis, AITicketAnalysis
from app.domains.crm.notification_service import NotificationService


# Mock Hubspot properties for creating test objects
def create_ticket(priority="HIGH", stage="new"):
    return {
        "id": "1",
        "type": "ticket",
        "properties": {
            "subject": "Test Ticket",
            "hs_ticket_priority": priority,
            "hs_pipeline_stage": stage,
            "hs_url": "http://hubspot.com/ticket/1",
        },
    }


def create_task(priority="HIGH", status="NOT_STARTED"):
    return {
        "id": "2",
        "type": "task",
        "properties": {
            "hs_task_subject": "Test Task",
            "hs_task_priority": priority,
            "hs_task_status": status,
            "hs_url": "http://hubspot.com/task/2",
        },
    }


@pytest.mark.asyncio
async def test_ai_ticket_analysis():
    ai = AIService("test-ai")
    ticket = create_ticket(priority="HIGH")

    analysis = await ai.analyze_polymorphic(ticket, "ticket")

    assert isinstance(analysis, AITicketAnalysis)
    assert analysis.urgency == "Critical"
    assert "Respond within 4h." in analysis.next_best_action


@pytest.mark.asyncio
async def test_ai_task_analysis():
    ai = AIService("test-ai")
    task = create_task(status="WAITING")

    analysis = await ai.analyze_polymorphic(task, "task")

    assert isinstance(analysis, AITaskAnalysis)
    assert analysis.status_label == "Pending"
    assert "Start task" in analysis.next_best_action


@pytest.mark.asyncio
async def test_notification_service_high_priority_ticket():
    """Test that high priority tickets trigger a notification."""
    with (
        patch("app.domains.crm.notification_service.StorageService") as MockStorage,
        patch("app.domains.crm.notification_service.HubSpotService") as MockHubSpot,
        patch(
            "app.domains.messaging.slack.service.SlackMessagingService"
        ) as MockChannel,
    ):
        # Setup Registry
        registry.register("slack", channel_service=MockChannel)

        # Setup Mocks
        mock_storage = MockStorage.return_value
        mock_storage.get_integration_by_portal_id = AsyncMock(
            return_value=MagicMock(workspace_id="ws1")
        )
        mock_slack_integ = MagicMock(provider=Provider.SLACK, workspace_id="ws1")
        mock_storage.get_integration = AsyncMock(
            return_value=mock_slack_integ
        )  # Slack integration exists
        mock_storage.get_workspace = AsyncMock(
            return_value=MagicMock(plan=PlanTier.PRO)
        )
        mock_storage.get_thread_mapping = AsyncMock(return_value=None)
        mock_storage.get_thread_mapping_by_ts = AsyncMock(return_value=None)
        mock_storage.upsert_thread_mapping = AsyncMock()
        mock_storage.list_integrations = AsyncMock(return_value=[mock_slack_integ])
        mock_storage.increment_usage_metrics = AsyncMock()

        # Mock GhostingMonitor storage
        from app.domains.crm.hubspot.ghosting_monitor import GhostingMonitor

        GhostingMonitor.get_instance().storage = mock_storage
        mock_storage.ghosting_heartbeats.upsert = AsyncMock()

        mock_hubspot = MockHubSpot.return_value
        mock_hubspot.invalidate_object_caches = AsyncMock()
        # Return a High Priority Ticket
        mock_hubspot.get_object = AsyncMock(return_value=create_ticket(priority="HIGH"))

        mock_channel = MockChannel.return_value
        mock_channel.send_card = AsyncMock(return_value="1715587200.123456")
        mock_channel.send_message = AsyncMock()
        mock_channel._resolve_channel = AsyncMock(return_value="#general")

        service = NotificationService("test-corr-id")

        event = {
            "portalId": 12345,
            "objectId": 100,
            "subscriptionType": "ticket.creation",
        }

        await service.handle_event(event)

        # Verify notification was sent
        mock_channel.send_card.assert_called_once()
        call_args = mock_channel.send_card.call_args
        assert call_args.kwargs["obj"]["properties"]["hs_ticket_priority"] == "HIGH"


@pytest.mark.asyncio
async def test_notification_service_low_priority_ticket_skipped():
    """Test that low priority tickets do NOT trigger a notification."""
    with (
        patch("app.domains.crm.notification_service.StorageService") as MockStorage,
        patch("app.domains.crm.notification_service.HubSpotService") as MockHubSpot,
        patch(
            "app.domains.messaging.slack.service.SlackMessagingService"
        ) as MockChannel,
    ):
        # Setup Registry
        registry.register("slack", channel_service=MockChannel)

        mock_storage = MockStorage.return_value
        mock_storage.get_integration_by_portal_id = AsyncMock(
            return_value=MagicMock(workspace_id="ws1")
        )
        mock_storage.get_integration = AsyncMock(return_value=None)
        mock_storage.get_workspace = AsyncMock(
            return_value=MagicMock(plan=PlanTier.PRO)
        )
        mock_storage.list_integrations = AsyncMock(return_value=[])
        mock_storage.get_thread_mapping = AsyncMock(return_value=None)
        mock_storage.get_thread_mapping_by_ts = AsyncMock(return_value=None)
        mock_storage.upsert_thread_mapping = AsyncMock()

        mock_hubspot = MockHubSpot.return_value
        mock_hubspot.invalidate_object_caches = AsyncMock()
        # Return a Low Priority Ticket
        mock_hubspot.get_object = AsyncMock(return_value=create_ticket(priority="LOW"))

        mock_channel = MockChannel.return_value
        mock_channel.send_card = AsyncMock(return_value="1715587200.123456")
        mock_channel.send_message = AsyncMock()
        mock_channel._resolve_channel = AsyncMock(return_value="#general")

        service = NotificationService("test-corr-id")

        event = {
            "portalId": 12345,
            "objectId": 101,
            "subscriptionType": "ticket.creation",
        }

        await service.handle_event(event)

        # Verify NO notification was sent
        mock_channel.send_card.assert_not_called()
