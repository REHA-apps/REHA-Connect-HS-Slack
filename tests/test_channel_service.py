# tests/test_channel_service.py  # noqa: D100
from unittest.mock import AsyncMock, patch

import pytest

from app.domains.messaging.slack.service import SlackMessagingService as ChannelService


@pytest.mark.asyncio
async def test_send_slack_card(corr_id, integration_service, slack_integration):
    service = ChannelService(
        corr_id,
        integration_service=integration_service,
        slack_integration=slack_integration,
    )

    fake_obj = {
        "id": "123",
        "type": "contact",
        "properties": {"firstname": "Alice"},
    }

    with patch.object(
        service, "send_message", new=AsyncMock(return_value={"ts": "123"})
    ) as mock_send:
        await service.send_card(
            workspace_id="test_workspace",
            obj=fake_obj,
            channel="#general",
        )

        mock_send.assert_awaited_once()
