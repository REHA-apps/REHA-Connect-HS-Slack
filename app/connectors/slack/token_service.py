"""Slack Token Service
===================
Manages Slack bot-token lifecycle: construction,
rotation callbacks, and credential persistence.

Extracted from IntegrationService so that core CRM
logic never imports Slack-specific modules.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.db.records import IntegrationRecord, Provider

if TYPE_CHECKING:
    from app.db.storage_service import StorageService
    from app.providers.slack.client import SlackClient

logger = get_logger("slack.token_service")

# 2026.03: Global lock registry to prevent Identity rotation collisions
_REFRESH_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


async def get_slack_client(
    integration: IntegrationRecord,
    *,
    corr_id: str,
    storage: StorageService,
) -> SlackClient:
    """Build a rotation-aware SlackClient for the given integration.

    Args:
        integration: The record containing Slack credentials.
        corr_id: Correlation ID for logging / tracing.
        storage: StorageService for persisting rotated tokens.

    Returns:
        A SlackClient with automatic token refresh logic.

    """
    credentials = integration.credentials
    bot_token = credentials.get("access_token") or credentials.get("slack_bot_token")
    refresh_token = credentials.get("refresh_token")
    expires_at = credentials.get("expires_at")

    from app.providers.slack.client import SlackClient

    client = SlackClient(
        corr_id=corr_id,
        bot_token=str(bot_token),
        refresh_token=refresh_token,
        expires_at=expires_at,
    )

    # identity_id is the source of truth for the lock (parent ID if linked)
    identity_id = integration.workspace_id
    metadata = integration.metadata or {}
    if linked_id := metadata.get("linked_slack_workspace_id"):
        identity_id = linked_id

    # Set callback for token rotation
    async def on_refresh(
        access_token: str, refresh_token: str | None, expires_at: int | None
    ) -> None:
        try:
            await update_slack_tokens(
                storage=storage,
                workspace_id=integration.workspace_id,  # Original workspace ID for logging  # noqa: E501
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
            )
        except Exception as exc:
            logger.error(
                "Failed to persist rotated Slack tokens for workspace=%s: %s",
                integration.workspace_id,
                exc,
            )

    # 2026.03: Inject identity-aware locking into the client's refresh path
    async def locked_refresh(refresh_logic: Any) -> Any:
        lock = _REFRESH_LOCKS.get(identity_id)
        if lock is None:
            lock = asyncio.Lock()
            _REFRESH_LOCKS[identity_id] = lock

        async with lock:
            # Re-fetch credentials inside the lock to see if another process
            # just refreshed while we were waiting
            current = await storage.get_integration(identity_id, Provider.SLACK)
            if current and current.credentials:
                creds = current.credentials
                exp = creds.get("expires_at")
                import time

                # If another process already updated it, just adopt the new token
                if exp and int(time.time()) + 300 < exp:
                    logger.info(
                        "Shared Lock: Token already refreshed by sibling portal "
                        "for identity=%s",
                        identity_id,
                    )
                    return {
                        "access_token": creds.get("access_token"),
                        "refresh_token": creds.get("refresh_token"),
                        "expires_at": exp,
                    }

            return await refresh_logic()

    client.on_token_refresh = on_refresh
    client.refresh_lock_provider = locked_refresh
    return client


async def update_slack_tokens(
    *,
    storage: StorageService,
    workspace_id: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: int | None,
) -> None:
    """Persist rotated Slack tokens into the integration record.

    Identity-Aware: Automatically redirects updates to the parent workspace
    if the current workspace is a linked isolated portal.
    """
    target_workspace_id = workspace_id

    # 1. Identity Resolution: Ensure we save to the parent owner
    integration = await storage.get_integration(workspace_id, Provider.SLACK)
    if integration:
        metadata = integration.metadata or {}
        linked_id = metadata.get("linked_slack_workspace_id")
        if linked_id:
            logger.info(
                "Redirecting Slack token update to parent Identity workspace=%s",
                linked_id,
            )  # noqa: E501
            target_workspace_id = linked_id

    # Refresh the integration for the target to get the latest ID
    if target_workspace_id != workspace_id:
        integration = await storage.get_integration(target_workspace_id, Provider.SLACK)

    if not integration:
        logger.warning(
            "Could not find integration record for Slack token update workspace=%s",
            target_workspace_id,
        )  # noqa: E501
        return

    # Merge new tokens and remove legacy key
    new_creds = {**integration.credentials}
    new_creds.pop("slack_bot_token", None)
    new_creds["access_token"] = access_token
    new_creds["refresh_token"] = refresh_token
    new_creds["expires_at"] = expires_at

    await storage.upsert_integration(
        {
            "id": integration.id,
            "workspace_id": target_workspace_id,
            "provider": Provider.SLACK,
            "credentials": new_creds,
            "metadata": integration.metadata,
        }
    )
    logger.info(
        "Slack tokens rotated and persisted for identity=%s", target_workspace_id
    )  # noqa: E501
