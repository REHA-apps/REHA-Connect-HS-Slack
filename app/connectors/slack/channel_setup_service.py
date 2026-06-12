"""Channel Setup Service — Safe create/reuse of the #reha-connect-alerts channel.

Implements the "Safe Create" pattern:
1. Search for existing ``reha-connect-alerts`` channel.
2. If found, reuse it.
3. If not found, create it (public).
4. Invite the bot.
5. Persist the channel_id in the Slack integration metadata.
"""

from __future__ import annotations

from typing import Any, cast

from app.connectors.common.registry import ChannelRegistry
from app.connectors.slack.slack_channel import SlackChannel
from app.core.logging import get_logger
from app.db.records import Provider
from app.db.storage_service import StorageService

logger = get_logger("slack.channel_setup")

# Hardcoded channel name — not user-modifiable
NOTIFICATION_CHANNEL_NAME = "reha-connect-alerts"


async def ensure_notification_channel(
    workspace_id: str,
    storage: StorageService,
    corr_id: str = "system",
) -> dict[str, Any]:
    """Find or create the ``#reha-connect-alerts`` Slack channel.

    Returns:
        A dict with ``channel_id``, ``channel_name``, and ``created``
        (bool indicating whether a new channel was created).

    Raises:
        RuntimeError: If the Slack integration is missing or the channel
            could not be created/found.
    """  # noqa: D413
    # 1. Get Slack integration
    integration = await storage.get_integration(workspace_id, Provider.SLACK)
    if not integration:
        raise RuntimeError(f"No Slack integration found for workspace {workspace_id}")

    # 2. Build a SlackChannel with valid credentials
    from app.connectors.slack.token_service import (
        get_slack_client as _get_client,
    )

    slack_client = await _get_client(integration, corr_id=corr_id, storage=storage)
    slack_channel = cast(
        SlackChannel,
        ChannelRegistry.get_channel(
            Provider.SLACK,
            bot_token=integration.slack_bot_token,
            refresh_token=integration.refresh_token,
            expires_at=integration.expires_at,
            corr_id=corr_id,
            slack_client=slack_client,
        ),
    )

    # 3. Try to find the channel first (Safe Create — Step 1)
    channel_id = await slack_channel.resolve_channel_name(NOTIFICATION_CHANNEL_NAME)
    created = False

    if not channel_id:
        # 4. Channel doesn't exist — create it (public)
        logger.info("Channel #%s not found, creating it", NOTIFICATION_CHANNEL_NAME)
        new_channel = await slack_channel.create_channel(
            name=NOTIFICATION_CHANNEL_NAME, is_private=False
        )

        if new_channel:
            channel_id = str(new_channel.get("id", ""))
            created = True
        else:
            # Race condition: another call may have created it between
            # our list and create calls. Try resolving again.
            channel_id = await slack_channel.resolve_channel_name(
                NOTIFICATION_CHANNEL_NAME
            )
            if not channel_id:
                raise RuntimeError(
                    f"Failed to create or find #{NOTIFICATION_CHANNEL_NAME}"
                )

    # 5. Invite the bot to the channel
    bot_user_id = await slack_channel.get_bot_user_id()
    if bot_user_id and channel_id:
        await slack_channel.invite_to_channel(channel_id, bot_user_id)

    # 6. Persist the channel_id in integration metadata
    metadata = dict(integration.metadata or {})
    metadata["channel_id"] = channel_id
    metadata["notification_channel_name"] = NOTIFICATION_CHANNEL_NAME

    await storage.upsert_integration(
        {
            "id": integration.id,
            "workspace_id": workspace_id,
            "provider": Provider.SLACK,
            "credentials": integration.credentials,
            "metadata": metadata,
        }
    )

    logger.info(
        "Notification channel configured: #%s (id=%s, created=%s)",
        NOTIFICATION_CHANNEL_NAME,
        channel_id,
        created,
    )

    return {
        "channel_id": channel_id,
        "channel_name": f"#{NOTIFICATION_CHANNEL_NAME}",
        "created": created,
    }
