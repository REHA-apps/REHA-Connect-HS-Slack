# tests/test_slack_connector.py  # noqa: D100
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.slack.slack_channel import SlackChannel as SlackConnector
from app.core.models.channel import OutboundMessage


@pytest.mark.asyncio
async def test_slack_connector_send_message(corr_id):
    with patch("app.providers.slack.client.AsyncWebClient") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.chat_postMessage = AsyncMock(
            return_value=MagicMock(data={"ok": True})
        )

        connector = SlackConnector(
            corr_id,
            bot_token="xoxb-test",
        )

        msg = OutboundMessage(
            workspace_id="test_workspace",
            destination="C12345678",
            text="Hello",
            provider_metadata={"blocks": None},
        )

        await connector.send_message(msg)

        mock_instance.chat_postMessage.assert_awaited_once()
