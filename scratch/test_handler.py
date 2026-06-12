import asyncio

from app.db.records import IntegrationRecord
from app.domains.ai.service import AIService
from app.domains.crm.integration_service import IntegrationService


async def test():
    corr_id = "test-corr-id"
    integration_service = IntegrationService()
    ai_service = AIService(corr_id=corr_id)

    # Mock integration
    integration = IntegrationRecord(
        id="test",
        workspace_id="test",
        slack_team_id="T123",
        slack_bot_token="xoxb-test",
        metadata={"portal_id": "123"},
    )

    payload = {
        "type": "block_actions",
        "actions": [{"action_id": "open_support_ticket_modal", "value": "test"}],
        "view": {"id": "V123", "type": "modal"},
    }

    # Check if handler gets resolved
    import app.connectors.slack.services.handlers.registry as reg
    from app.domains.messaging.slack.service import SlackMessagingService

    msg_svc = SlackMessagingService("T123")

    registry = reg.InteractionRegistry(
        corr_id=corr_id,
        crm=None,
        ai=ai_service,
        integration_service=integration_service,
    )

    handler = registry.get_handler(payload, action_id="open_support_ticket_modal")
    print("Handler found:", handler)


if __name__ == "__main__":
    asyncio.run(test())
