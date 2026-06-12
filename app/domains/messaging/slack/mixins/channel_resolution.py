from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from app.connectors.common.base import BaseChannel
from app.connectors.common.registry import ChannelRegistry
from app.core.exceptions import IntegrationNotFoundError
from app.core.logging import get_logger
from app.db.records import (
    Provider,
)
from app.domains.crm.integration_service import IntegrationService
from app.utils.cache import AsyncTTL

logger = get_logger("slack.messaging")

# Compiled Regex Patterns
TICKET_PATTERN = re.compile(r"Ticket #(\d+)")
TICKET_ID_PATTERN = re.compile(r"Ticket ID: (\d+)")
CONVERSATION_PATTERN = re.compile(r"Conversation #(\d+)")

# Global cache for Slack names to avoid redundant API calls
_user_name_cache = AsyncTTL[str](ttl=3600)


class ChannelResolutionMixin:
    """Mixin for Slack messaging capabilities."""

    if TYPE_CHECKING:
        corr_id: str
        portal_id: int | None
        slack_integration: Any
        integration_service: IntegrationService
        from app.connectors.slack.slack_channel import SlackChannel
        from app.domains.crm.service import CRMService

        crm: CRMService

        async def send_message(self, **kwargs: Any) -> dict[str, Any]: ...

    async def get_slack_channel(self) -> BaseChannel:
        if not self.slack_integration:
            raise IntegrationNotFoundError(
                "Slack integration not configured for this workspace"
            )

        if not self.integration_service:
            raise RuntimeError("IntegrationService not initialized")

        client = await self.integration_service.get_slack_client(self.slack_integration)

        return ChannelRegistry.get_channel(
            Provider.SLACK,
            bot_token=client.bot_token,
            refresh_token=self.slack_integration.refresh_token,
            expires_at=self.slack_integration.expires_at,
            corr_id=self.corr_id,
            slack_client=client,
            portal_id=self.portal_id,  # 2026.03: Triple-Key Trace Prop
        )

    async def _resolve_channel(  # noqa: PLR0912
        self,
        workspace_id: str,
        channel_id: str | None,
        obj: Mapping[str, Any] | None = None,
        is_system_alert: bool = False,
    ) -> str | None:
        """Resolves the target Slack channel for a message.

        Priority:
        1. Explicit ``channel`` parameter.
        2. Integration metadata ``channel_id`` (set via Settings Page).
        3. ``resolve_default_channel`` from IntegrationService.
        4. DM fallback (when ``notification_mode`` is ``"dm"``):
           - Record alerts → DM the HubSpot record owner.
           - System alerts → DM the installing admin.
           - If owner can't be mapped → DM the admin with a fallback
             message.
        5. Slack's ``#general`` channel (last resort).
        """
        raw_destination = channel_id
        if not raw_destination:
            # Ensure we have the integration record for metadata-based routing (DMs, etc)
            if not self.slack_integration and self.integration_service:
                self.slack_integration = await self.integration_service.get_integration(
                    workspace_id, Provider.SLACK
                )

            metadata = (
                self.slack_integration.metadata if self.slack_integration else {}
            ) or {}
            notification_mode = metadata.get("notification_mode", "channel")

            # 1. If DM mode is active, try resolving DM target FIRST
            if notification_mode == "dm":
                dm_target = await self._resolve_dm_target(
                    workspace_id=workspace_id,
                    obj=obj,
                    is_system_alert=is_system_alert,
                )
                if dm_target:
                    return dm_target

            # 2. Fallback to explicit channel_id or default channel
            if self.slack_integration and self.slack_integration.metadata.get(
                "channel_id"
            ):
                raw_destination = str(self.slack_integration.metadata["channel_id"])
            else:
                try:
                    if not self.integration_service:
                        raise RuntimeError("IntegrationService not initialized")

                    raw_destination = (
                        await self.integration_service.resolve_default_channel(
                            workspace_id=workspace_id,
                        )
                    )
                except IntegrationNotFoundError:
                    # Last resort: Slack's #general channel
                    client = await self.get_slack_channel()
                    default_id = await client.get_default_channel_id()
                    if default_id:
                        return default_id
                    raise

        if not raw_destination:
            # Don't log warning if we are in DM mode as this is expected
            metadata = (
                self.slack_integration.metadata if self.slack_integration else {}
            ) or {}
            if metadata.get("notification_mode") != "dm":
                logger.warning(
                    "No target Slack channel configured for workspace %s. "
                    "Ensure a channel is selected in the REHA Connect settings page in HubSpot.",
                    workspace_id,
                )
            return None

        # Resolve human-readable channel names like #general into system IDs
        if raw_destination.startswith("#"):
            client = await self.get_slack_channel()
            resolved_id = await client.resolve_channel_name(raw_destination)
            if resolved_id:
                logger.debug(
                    "Resolved channel name %s to ID %s for workspace %s",
                    raw_destination,
                    resolved_id,
                    workspace_id,
                )
                return resolved_id
            else:
                logger.warning(
                    "Could not resolve channel name %s, using as-is", raw_destination
                )

        return raw_destination

    async def _resolve_dm_target(  # noqa: PLR0911
        self,
        workspace_id: str,
        obj: Mapping[str, Any] | None = None,
        is_system_alert: bool = False,
    ) -> str | None:
        """Smart DM routing when no default channel is configured.

        - Record alerts → DM the HubSpot record owner (via email→Slack ID).
        - System alerts → DM the installing admin.
        - Unmapped owner → DM the admin with a fallback notification.

        Returns:
            A Slack user ID to DM, or None if DM mode is not applicable.

        """
        if not self.slack_integration:
            return None

        metadata = self.slack_integration.metadata or {}
        notification_mode = metadata.get("notification_mode", "channel")

        if notification_mode != "dm":
            return None

        admin_user_id = metadata.get("authed_user_id")
        admin_fallback_enabled = metadata.get("admin_fallback_enabled", True)

        # System alerts always go to the installing admin
        if is_system_alert:
            if admin_user_id:
                logger.info(
                    "Routing system alert DM to installing admin %s",
                    admin_user_id,
                )
                return admin_user_id
            logger.warning("DM mode active but no admin user_id stored")
            return None

        # Record alerts → try to find the HubSpot record owner's Slack ID
        if obj:
            owner_id = (obj.get("properties") or {}).get("hubspot_owner_id")
            if owner_id:
                slack_user_id = await self._resolve_owner_slack_id(
                    workspace_id, owner_id
                )
                if slack_user_id:
                    logger.debug(
                        "Routing record DM to owner (hs_owner=%s, slack=%s)",
                        owner_id,
                        slack_user_id,
                    )
                    return slack_user_id

                logger.warning(
                    "Owner %s found on record but no Slack mapping exists. Workspace=%s",
                    owner_id,
                    workspace_id,
                )

                # Owner exists but can't be mapped — send fallback to admin ONLY if enabled
                if admin_user_id and admin_fallback_enabled:
                    await self._send_unmapped_owner_fallback(
                        workspace_id=workspace_id,
                        admin_user_id=admin_user_id,
                        owner_id=owner_id,
                        obj=obj,
                    )
                    return admin_user_id

                # If fallback disabled, stop here to avoid routing to admin
                if not admin_fallback_enabled:
                    logger.debug(
                        "Admin fallback disabled — dropping DM for unmapped owner %s",
                        owner_id,
                    )
                    return None

        # No owner on record — fall back to admin ONLY if enabled
        if admin_user_id and admin_fallback_enabled:
            logger.debug("No record owner found, routing DM to admin %s", admin_user_id)
            return admin_user_id

        return None

    async def _resolve_owner_slack_id(
        self, workspace_id: str, owner_id: str
    ) -> str | None:
        """Maps a HubSpot owner_id to a Slack user ID via the user mappings table."""
        try:
            if not self.integration_service:
                return None
            mapping = await self.integration_service.storage.get_user_mapping(
                workspace_id, int(owner_id)
            )
            if mapping and mapping.slack_user_id:
                return mapping.slack_user_id
            return None
        except Exception as exc:
            logger.warning(
                "Failed to resolve Slack ID from DB for owner %s: %s", owner_id, exc
            )
            return None

    async def _send_unmapped_owner_fallback(
        self,
        workspace_id: str,
        admin_user_id: str,
        owner_id: str,
        obj: Mapping[str, Any],
    ) -> None:
        """Sends a fallback DM to the admin when a record owner isn't mapped."""
        try:
            # Resolve owner name for the message
            async def _fetcher() -> str:
                val = await self.crm.hubspot.resolve_owner_display_name(
                    workspace_id, owner_id
                )
                return val or "HubSpot Owner"

            cache_key = f"owner_name:{workspace_id}:{owner_id}"
            owner_name = await _user_name_cache.get_or_fetch(cache_key, _fetcher)

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"⚠️ A record owned by {owner_name} was updated, "
                            f"but {owner_name} isn't connected to Slack yet."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": f"📍 Map {owner_name} Now",
                            },
                            "url": "https://app.hubspot.com/ecosystem/reha-connect/settings",
                            "action_id": "map_user_btn",
                        }
                    ],
                },
            ]

            slack_channel = await self.get_slack_channel()
            await slack_channel.send_dm(
                user_id=admin_user_id,
                text=(
                    f"A record owned by {owner_name} was updated, "
                    f"but I couldn't find their Slack account."
                ),
                blocks=blocks,
            )
            logger.info(
                "Sent unmapped-owner fallback DM to admin %s for owner %s (%s)",
                admin_user_id,
                owner_id,
                owner_name,
            )
        except Exception as exc:
            logger.warning("Failed to send unmapped-owner fallback to admin: %s", exc)

    async def _initialize_ticket_thread(
        self,
        *,
        workspace_id: str,
        object_id: str,
        channel: str,
        sent_ts: str,
    ) -> None:
        """Helper to initialize HubSpot synchronization for a Slack ticket thread."""
        logger.debug(
            "Initializing ticket thread sync for ticket=%s in channel=%s",
            object_id,
            channel,
        )
        if self.integration_service:
            await self.integration_service.storage.upsert_thread_mapping(
                {
                    "workspace_id": workspace_id,
                    "object_type": "ticket",
                    "object_id": object_id,
                    "channel_id": channel,
                    "thread_ts": sent_ts,
                }
            )

        await self.send_message(
            workspace_id=workspace_id,
            channel=channel,
            thread_ts=sent_ts,
            text="💬 *Thread open!* Any replies you send here will automatically sync to HubSpot.",
        )

    async def _resolve_thread_target(  # noqa: PLR0911
        self,
        workspace_id: str,
        channel: str,
        thread_ts: str | None,
        full_context: str | None = None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Resolves the HubSpot object or conversation targets for a thread.

        Returns: (object_id, object_type, thread_id, source)
        """
        # 1. Look up thread mapping
        if not self.integration_service:
            return None, None, None, None

        mapping = await self.integration_service.storage.get_thread_mapping_by_ts(
            workspace_id=workspace_id,
            channel_id=channel,
            thread_ts=thread_ts or "CHANNEL_ROOT",
        )

        if mapping:
            logger.info(
                "Resolved thread target via mapping for object_id=%s in channel=%s",
                mapping.object_id,
                channel,
            )
            source = getattr(mapping, "source", None)
            if mapping.object_type == "conversation":
                return None, None, mapping.object_id, source
            return mapping.object_id, mapping.object_type, None, source

        if not full_context:
            return None, None, None, None

        # 2. Heuristic fallback for objects
        ticket_match = TICKET_PATTERN.search(full_context)
        if not ticket_match:
            ticket_match = TICKET_ID_PATTERN.search(full_context)

        if ticket_match:
            return ticket_match.group(1), "ticket", None, None

        # 3. Heuristic fallback for conversations
        conversation_match = CONVERSATION_PATTERN.search(full_context)
        if conversation_match:
            return None, None, conversation_match.group(1), None

        return None, None, None, None

    async def _get_slack_user_name(self, client: Any, user: str) -> str:
        """Resolves user's real name via Slack API, falls back to <@id>."""
        cache_key = f"slack_name:{user}"

        async def fetch():
            try:
                user_info_resp = await client.users_info(user=user)
                if user_info_resp and user_info_resp.get("ok"):
                    user_info = dict(user_info_resp.get("user", {}))
                    return (
                        user_info.get("real_name")
                        or user_info.get("name")
                        or f"<@{user}>"
                    )
            except Exception as exc:
                logger.warning("Failed to fetch Slack user info for %s: %s", user, exc)
            return f"<@{user}>"

        return await _user_name_cache.get_or_fetch(cache_key, fetch)

    async def get_user_email(self, user_id: str) -> str | None:
        """Resolves a Slack user ID to their email address with caching."""
        cache_key = f"slack_email:{user_id}"

        async def fetch() -> str:
            # First try Slack client if available
            try:
                slack_channel = await self.get_slack_channel()
                client = cast("Any", slack_channel).get_slack_client()
                user_info_resp = await client.users_info(user=user_id)
                if user_info_resp:
                    val = user_info_resp.get("user", {}).get("profile", {}).get("email")
                    return str(val) if val else ""
            except Exception as exc:
                logger.warning(
                    "Failed to fetch Slack user email for %s: %s", user_id, exc
                )
            return ""

        # Fix: using _user_name_cache for now, but really this is generic async TTL
        res = await _user_name_cache.get_or_fetch(cache_key, fetch)
        return str(res) if res else None
