from __future__ import annotations  # noqa: D100

from collections.abc import Awaitable, Callable, Mapping
from time import time
from typing import Any, cast

import httpx
from pydantic import Field

from app.connectors.common.base import BaseChannel, BaseOAuthResult
from app.core.config import settings
from app.core.logging import get_logger
from app.core.models.channel import Identity, NormalizedEvent, OutboundMessage
from app.providers.slack.client import SlackClient
from app.utils.helpers import HTTPClient

logger = get_logger("slack.channel")


class SlackOAuthResult(BaseOAuthResult):
    """Slack-specific OAuth metadata."""

    bot_user_id: str
    team_id: str | None = Field(default=...)  # Overrides base field, required


class SlackChannel(BaseChannel):
    """Unified Slack channel implementation.

    Handles infrastructure concerns such as OAuth lifecycle management and
    domain logic including event normalization and outbound messaging.
    """

    channel_name: str = "slack"
    supports_cards: bool = True
    supports_ephemeral: bool = True
    supports_threading: bool = True

    @property
    def bot_token(self) -> str | None:
        return self._bot_token

    @bot_token.setter
    def bot_token(self, value: str | None) -> None:
        self._bot_token = value

    def __init__(
        self,
        corr_id: str,
        bot_token: str | None = None,
        refresh_token: str | None = None,
        expires_at: int | None = None,
        slack_client: SlackClient | None = None,
        portal_id: int | None = None,
        on_token_refresh: Callable[[str, str | None, int | None], Awaitable[None]]
        | None = None,
        **_kwargs: Any,  # Absorb extra kwargs from registry
    ) -> None:
        self.corr_id = corr_id
        self.http_client = HTTPClient.get_client(corr_id=corr_id)
        self._bot_token = bot_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.portal_id = portal_id
        self.on_token_refresh = on_token_refresh
        self.slack_client = slack_client

    def get_slack_client(self) -> SlackClient:
        """Lazily initialize the SlackClient wrapper with unified session pooling."""
        if not self.slack_client:
            self.slack_client = SlackClient(
                corr_id=self.corr_id,
                bot_token=str(self.bot_token),
                refresh_token=self.refresh_token,
                expires_at=self.expires_at,
                portal_id=self.portal_id,  # 2026.03 Triple-Key tracing
            )
            self.slack_client.on_token_refresh = self.on_token_refresh
        return self.slack_client

    # -----------------------------
    # Authentication & Transport
    # -----------------------------
    async def exchange_token(
        self, code: str, redirect_uri: str | None = None
    ) -> SlackOAuthResult:
        """Handles Slack-specific OAuth token exchange."""
        logger.info("Exchanging Slack OAuth code")

        data = {
            "client_id": settings.SLACK_CLIENT_ID,
            "client_secret": settings.SLACK_CLIENT_SECRET.get_secret_value(),
            "code": code,
            "redirect_uri": redirect_uri
            or settings.SLACK_REDIRECT_URI.unicode_string(),
        }

        resp = await self.http_client.post(
            "https://slack.com/api/oauth.v2.access", data=data
        )
        resp.raise_for_status()
        payload = resp.json()

        if not payload.get("ok"):
            error = payload.get("error", "unknown_error")
            logger.error("Slack OAuth failed: %s", error)
            raise RuntimeError(f"Slack OAuth failed: {error}")

        expires_in = payload.get("expires_in")
        expires_at = int(time()) + expires_in if expires_in else None

        return SlackOAuthResult(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=expires_at,
            bot_user_id=payload["bot_user_id"],
            team_id=payload["team"]["id"],
            raw=payload,
        )

    # -----------------------------
    # Inbound Event Normalization
    # -----------------------------
    async def normalize_event(
        self, workspace_id: str, raw_event: Mapping[str, Any]
    ) -> NormalizedEvent:
        """Maps raw Slack webhook data into a standardized NormalizedEvent."""
        user_id = (
            raw_event.get("user")
            or raw_event.get("actor_id")
            or raw_event.get("event", {}).get("user")
            or "unknown"
        )

        ts = (
            raw_event.get("event_ts")
            or raw_event.get("ts")
            or raw_event.get("event", {}).get("ts")
        )

        return NormalizedEvent(
            workspace_id=workspace_id,
            source="slack",
            event_type=raw_event.get("type", "unknown"),
            identity=Identity(
                external_id=str(user_id),
                provider="slack",
                source="slack",
            ),
            payload=dict(raw_event),
            timestamp=str(ts),
        )

    # -----------------------------
    # Identity Resolution
    # -----------------------------
    async def resolve_identity(self, event: NormalizedEvent) -> Identity | None:
        """Slack identity resolution (currently logic is handled upstream)."""
        return None

    # -----------------------------
    # Outbound Communication
    # -----------------------------
    async def send_message(
        self,
        message: OutboundMessage,
        **kwargs: Any,
    ) -> Mapping[str, Any] | None:
        """Sends a message to Slack using chat.postMessage."""
        logger.info("SlackChannel.send_message to destination=%s", message.destination)

        # Use provided bot_token or fallback to kwargs
        client = self.slack_client or self.get_slack_client()

        # Slack-specific validation
        destination = message.destination
        if not destination:
            logger.error("OutboundMessage missing destination")
            return None

        # Resolve human-readable channel names like #general into system IDs
        if destination.startswith("#"):
            resolved_id = await self.resolve_channel_name(destination)
            if not resolved_id:
                logger.error("Could not resolve named channel: %s", destination)
                return None
            destination = resolved_id

        if not (
            destination
            and len(destination) >= 9  # noqa: PLR2004
            and destination[0] in ("C", "G", "D", "U")
            and destination[1:].isalnum()
            and not any(c.islower() for c in destination)
        ):
            logger.error("Invalid Slack channel/destination ID format: %s", destination)
            return None

        fallback_text = message.text or "New CRM update"

        # Extract Slack-specific fields from provider_metadata
        blocks = message.provider_metadata.get("blocks")
        thread_ts = message.provider_metadata.get("thread_ts")
        # user = message.provider_metadata.get("user")  # For ephemeral messages

        resp = await client.chat_postMessage(
            channel=destination,
            text=fallback_text,
            blocks=blocks,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
            **kwargs,
        )
        return cast(dict[str, Any], resp.data) if resp and resp.data else None

    async def update_message(
        self,
        channel_id: str,
        ts: str,
        text: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Mapping[str, Any] | None:
        """Updates an existing message in Slack using chat.update."""
        logger.info("SlackChannel.update_message ts=%s in channel=%s", ts, channel_id)
        client = self.slack_client or self.get_slack_client()

        resp = await client.chat_update(
            channel=channel_id,
            ts=ts,
            text=text or "Updated CRM record",
            blocks=blocks,
            **kwargs,
        )
        return cast(dict[str, Any], resp.data) if resp and resp.data else None

    # -----------------------------
    # Lifecycle Hooks
    # -----------------------------
    async def install(self, payload: Mapping[str, Any]) -> None:
        """Post-install hook for Slack integration."""
        logger.info("Slack install event received (handled upstream)")

    async def uninstall(self, payload: Mapping[str, Any]) -> None:
        """Post-uninstall hook for Slack integration."""
        logger.info("Slack uninstall event received (handled upstream)")

    async def validate_install_payload(self, payload: Mapping[str, Any]) -> None:
        """Validates the Slack OAuth installation payload."""
        if "team" not in payload or "access_token" not in payload:
            logger.error("Invalid Slack install payload")
            raise ValueError("Invalid Slack install payload")

    # -----------------------------
    # Slack-Specific Methods
    # -----------------------------
    async def open_view(
        self, trigger_id: str, view: dict[str, Any], bot_token: str | None = None
    ) -> Any:
        """Opens a Slack modal view."""
        client = self.slack_client
        if bot_token:
            client = SlackClient(corr_id=self.corr_id, bot_token=bot_token)
        elif not client:
            client = self.get_slack_client()

        return await client.views_open(trigger_id=trigger_id, view=view)

    async def trigger_overlay(
        self, trigger_id: str, view: dict[str, Any], bot_token: str | None = None
    ) -> Any:
        """Triggers a 2026.03 Right-Side Overlay (Secondary Surface)."""
        client = self.slack_client
        if bot_token:
            client = SlackClient(corr_id=self.corr_id, bot_token=bot_token)
        elif not client:
            client = self.get_slack_client()

        # Modern Slack API for secondary surfaces
        return await client.views_open(
            trigger_id=trigger_id,
            view={
                **view,
                "type": "modal",
                "is_overlay": True,  # 2026.03 overlay trigger
            },
        )

    async def post_to_response_url(
        self, response_url: str, payload: dict[str, Any]
    ) -> bool:  # noqa: E501
        """Resilient Frankfurt-to-Edge delivery with Exponential Backoff + Jitter.

        Now uses the shared HTTPClient singleton session for pooling.
        """
        import asyncio  # noqa: I001
        import random
        import httpx

        # Borrow the warm singleton session from helpers
        session = HTTPClient.get_client(corr_id=self.corr_id)

        # Max 3 retries over ~2 seconds as per 2026.03 protocol
        for attempt in range(3):
            try:
                # Use a strict 2.0s override for the 'Ack' path
                response = await session.post(response_url, json=payload, timeout=2.0)

                if response.status_code == 200:
                    return True

                if response.status_code == 429:
                    wait = int(response.headers.get("Retry-After", 1))
                    await asyncio.sleep(wait)
                    continue

            except httpx.RequestError:
                # Backoff: 0.2s, 0.4s, 0.8s + small jitter
                wait_time = (0.2 * (2**attempt)) + (random.uniform(0, 0.1))
                await asyncio.sleep(wait_time)

        return False

    async def chat_unfurl(
        self,
        channel: str,
        ts: str,
        unfurls: dict[str, dict[str, Any]] | None = None,
        unfurl_id: str | None = None,
        source: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Provides rich previews for shared links via chat.unfurl.

        Supports 2026 Composer Unfurls by accepting unfurl_id and source.
        """
        params = {
            "channel": channel,
            "ts": ts,
            "unfurls": unfurls or {},
            **kwargs,
        }

        if source:
            params["source"] = source
        if unfurl_id:
            params["unfurl_id"] = unfurl_id

        return await self.get_slack_client().chat_unfurl(**params)

    async def _find_channel(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        label: str = "channel",
    ) -> str | None:
        """Paginate conversations.list and return the first channel ID
        matching predicate.
        """
        client = self.get_slack_client()
        try:
            cursor = None
            while True:
                resp = await client.conversations_list(
                    types="public_channel,private_channel",
                    cursor=cursor,
                    limit=1000,
                    exclude_archived=True,
                )
                if not resp.get("ok"):
                    logger.error(
                        "Slack conversations.list failed: %s", resp.get("error")
                    )
                    break

                for channel in resp.get("channels", []):
                    if predicate(channel):
                        channel_id = str(channel.get("id"))
                        logger.info("Resolved %s to %s", label, channel_id)
                        return channel_id

                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as exc:
            logger.error("Failed to find %s: %s", label, exc, exc_info=True)

        return None

    async def resolve_channel_name(self, name: str) -> str | None:
        """Resolves a channel name (e.g., '#general' or 'general') to a channel ID.

        This uses conversations.list which requires 'channels:read'
        and 'groups:read' scopes.
        """
        clean_name = name.lstrip("#").lower()
        logger.info("Attempting to resolve Slack channel name: %s", clean_name)

        def _match(channel: dict[str, Any]) -> bool:
            if clean_name == "general" and channel.get("is_general"):
                return True
            return channel.get("name") == clean_name

        result = await self._find_channel(_match, label=f"channel '{name}'")
        if result is None:
            logger.warning("Could not resolve Slack channel name: %s", name)
        return result

    async def get_default_channel_id(self) -> str | None:
        """Fetches the workspace's default channel ID (is_general=True)."""
        logger.info("Fetching default Slack channel (is_general=True)")
        return await self._find_channel(
            lambda ch: bool(ch.get("is_general")),
            label="default channel",
        )

    async def create_channel(
        self, name: str, is_private: bool = False
    ) -> dict[str, Any] | None:
        """Creates a new Slack channel.

        Returns the channel object on success, or None on failure.
        Handles the ``name_taken`` error gracefully by returning None
        (caller should use :meth:`resolve_channel_name` first).
        """
        client = self.get_slack_client()
        try:
            resp = await client.conversations_create(name=name, is_private=is_private)
            if resp.get("ok"):
                channel = cast(dict[str, Any], resp.get("channel", {}))
                logger.info(
                    "Created Slack channel '%s' (id=%s)",
                    name,
                    channel.get("id"),
                )
                return channel
            logger.error("conversations.create failed: %s", resp.get("error"))
        except Exception as exc:
            error_str = str(exc)
            if "name_taken" in error_str:
                logger.info("Channel '%s' already exists (name_taken)", name)
            else:
                logger.error(
                    "Failed to create channel '%s': %s",
                    name,
                    exc,
                    exc_info=True,
                )
        return None

    async def invite_to_channel(self, channel_id: str, user_id: str) -> bool:
        """Invites a user (typically the bot) to a channel.

        Returns True on success or if already in the channel.
        """
        client = self.get_slack_client()
        try:
            resp = await client.conversations_invite(channel=channel_id, users=user_id)
            if resp.get("ok"):
                logger.info("Invited user %s to channel %s", user_id, channel_id)
                return True
            logger.warning("conversations.invite failed: %s", resp.get("error"))
        except Exception as exc:
            error_str = str(exc)
            if "already_in_channel" in error_str:
                logger.info("User %s already in channel %s", user_id, channel_id)
                return True
            logger.error(
                "Failed to invite user %s to channel %s: %s",
                user_id,
                channel_id,
                exc,
                exc_info=True,
            )
        return False

    async def get_bot_user_id(self) -> str | None:
        """Resolves the bot's own Slack user ID via ``auth.test``."""
        client = self.get_slack_client()
        try:
            resp = await client.auth_test()
            if resp.get("ok"):
                bot_id = str(resp.get("user_id", ""))
                logger.info("Resolved bot user_id=%s", bot_id)
                return bot_id
        except Exception as exc:
            logger.error("Failed to resolve bot user ID: %s", exc, exc_info=True)
        return None

    async def apps_uninstall(self) -> bool:
        """Uninstalls the app from the workspace using the current bot token.

        Returns:
            bool: True if successful, False otherwise.

        """
        if not self.bot_token:
            logger.error("Cannot uninstall: No bot token provided")
            return False

        try:
            resp = await self.get_slack_client().apps_uninstall(
                client_id=settings.SLACK_CLIENT_ID,
                client_secret=settings.SLACK_CLIENT_SECRET.get_secret_value(),
            )
            if not resp.get("ok"):
                logger.error("Slack apps.uninstall failed: %s", resp.get("error"))
                return False
            logger.info("App successfully uninstalled from Slack workspace")
            return True
        except Exception as exc:
            logger.error("Slack apps.uninstall exception: %s", exc, exc_info=True)
            return False

    async def get_user_by_email(self, email: str) -> str | None:
        """Resolves a Slack user ID by their email address."""
        try:
            resp = await self.get_slack_client().users_lookupByEmail(email=email)
            if resp and resp.get("ok") and "user" in resp:
                user_data = cast(dict[str, Any], resp["user"])
                return str(user_data.get("id"))
        except Exception as exc:
            logger.error(
                "Failed to lookup Slack user by email '%s': %s",
                email,
                exc,
                exc_info=True,
            )
        return None

    async def get_all_users(self) -> list[dict[str, Any]]:
        """Fetches all users from the Slack workspace using pagination."""
        users = []
        cursor = None

        while True:
            try:
                resp = await self.get_slack_client().users_list(
                    cursor=cursor, limit=200
                )
                if not resp.get("ok"):
                    logger.error("Slack users.list failed: %s", resp.get("error"))
                    break

                members = cast(list[dict[str, Any]], resp.get("members", []))
                users.extend(members)

                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            except Exception as exc:
                # 2026.03: Explicitly raise auth errors so service can handle
                # re-auth flow
                from slack_sdk.errors import SlackApiError

                if isinstance(exc, SlackApiError) and exc.response.get("error") in [
                    "token_expired",
                    "invalid_auth",
                    "account_inactive",
                ]:
                    logger.error(
                        "Slack authentication failed during user fetch: %s", exc
                    )
                    raise

                logger.error("Error fetching Slack users: %s", exc, exc_info=True)
                break

        return users

    async def send_dm(
        self,
        user_id: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> Mapping[str, Any] | None:
        """Sends a private DM to a Slack user."""
        logger.info("SlackChannel.send_dm to user_id=%s", user_id)

        message = OutboundMessage(
            workspace_id="DM",  # Arbitrary for DMs
            destination=user_id,
            text=text,
            provider_metadata={"blocks": blocks},
        )
        return await self.send_message(message)

    async def get_thread_replies(
        self, channel_id: str, thread_ts: str
    ) -> list[dict[str, Any]]:
        """Fetches all replies in a Slack thread."""
        try:
            resp = await self.get_slack_client().conversations_replies(
                channel=channel_id,
                ts=thread_ts,
            )
            if resp.get("ok"):
                return cast(list[dict[str, Any]], resp.get("messages", []))
        except Exception as exc:
            logger.error(
                "Failed to fetch thread replies for channel=%s ts=%s: %s",
                channel_id,
                thread_ts,
                exc,
                exc_info=True,
            )
        return []

    async def get_channel_history(
        self, channel_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Fetches history for a Slack channel."""
        try:
            resp = await self.get_slack_client().conversations_history(
                channel=channel_id,
                limit=limit,
            )
            if resp.get("ok"):
                return cast(list[dict[str, Any]], resp.get("messages", []))
        except Exception as exc:
            logger.error(
                "Failed to fetch channel history for channel=%s: %s",
                channel_id,
                exc,
                exc_info=True,
            )
        return []

    async def send_via_response_url(
        self,
        response_url: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        replace_original: bool = False,
    ) -> bool:
        """Sends a response to a Slack slash command using the response_url webhook.

        This is highly reliable as it bypasses channel discovery/membership issues.
        """
        logger.info(
            "SlackChannel.send_via_response_url using %s", response_url[:30] + "..."
        )

        payload = {
            "text": text,
            "replace_original": replace_original,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if blocks:
            payload["blocks"] = blocks

        try:
            resp = await self.http_client.post(response_url, json=payload)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Slack rejected payload: {resp.text} \nPayload was: {payload}"
                )
                raise e
            return True
        except Exception as exc:
            logger.error("Failed to send to Slack response_url: %s", exc, exc_info=True)
            return False
