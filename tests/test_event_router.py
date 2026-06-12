# tests/test_event_router.py  # noqa: D100
from unittest.mock import AsyncMock, patch

import pytest

from app.domains.crm.event_router import EventRouter


@pytest.mark.asyncio
async def test_route_contact_update(corr_id, integration_service, slack_integration):
    router = EventRouter(
        corr_id,
        integration_service=integration_service,
        slack_integration=slack_integration,
    )

    fake_contact = {
        "id": "123",
        "type": "contact",
        "properties": {"firstname": "Alice"},
    }

    with patch.object(
        router.messaging_service, "send_card", new=AsyncMock()
    ) as mock_send:
        await router.route_contact_update(
            workspace_id="test_workspace",
            contact=fake_contact,
            channel="#general",
        )

        mock_send.assert_awaited_once()
