from __future__ import annotations  # noqa: D100, I001

import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler

from app.core.config import settings
from app.core.logging import CorrelationAdapter, get_logger
from app.utils.helpers import HTTPClient

logger = get_logger("slack.client")

# Global shared aiohttp session for all Slack workspaces
_global_slack_session: aiohttp.ClientSession | None = None


def get_shared_slack_session() -> aiohttp.ClientSession:
    """Returns a singleton aiohttp session for multiplexing all Slack traffic."""
    global _global_slack_session  # noqa: PLW0603
    if _global_slack_session is None or _global_slack_session.closed:
        connector = aiohttp.TCPConnector(limit=100, keepalive_timeout=30)
        _global_slack_session = aiohttp.ClientSession(connector=connector)
    return _global_slack_session


async def close_shared_slack_session() -> None:
    """Closes the shared Slack session on Lambda shutdown."""
    global _global_slack_session  # noqa: PLW0603
    if _global_slack_session and not _global_slack_session.closed:
        await _global_slack_session.close()


class SlackClient:
    """Wrapper around Slack's AsyncWebClient with Unified Session Management (2026.03).

    Rules Applied:
        - Uses global httpx session pooling for "warm pipes" (Frankfurt-to-Edge).
        - Implements 'Triple-Key' trace: [Corr-ID] -> [Slack-TS] -> [Portal-ID].
        - Native adaptive retries for high-density scalability.
    """

    def __init__(
        self,
        corr_id: str,
        bot_token: str,
        refresh_token: str | None = None,
        expires_at: int | None = None,
        portal_id: int | None = None,
    ) -> None:
        self.corr_id = corr_id
        self.bot_token = bot_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.portal_id = portal_id
        self.log = CorrelationAdapter(logger, corr_id)

        # Callback: (new_token, new_refresh, new_expires) -> Awaitable[None]
        self.on_token_refresh: (
            Callable[[str, str | None, int | None], Awaitable[None]] | None
        ) = None

        # 2026.03: Lock provider for shared identity rotation
        self.refresh_lock_provider: (
            Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]] | None
        ) = None

        # 2026.03: Adaptive retries for high-density scalability
        # We inject a global shared aiohttp session to multiplex connections
        # across all workspaces within this Lambda container, preventing OOM/bloat.
        self._client = AsyncWebClient(
            token=bot_token, session=get_shared_slack_session()
        )
        self._client.retry_handlers.append(
            AsyncRateLimitErrorRetryHandler(max_retry_count=2)
        )

    def _triple_key_msg(self, msg: str, slack_ts: str | None = None) -> str:
        """Formats the 2026.03 Triple-Key trace for Frankfurt-to-Dublin observability."""  # noqa: E501
        ts = slack_ts or "no-ts"
        portal = self.portal_id or "no-portal"
        return f"[{self.corr_id}] -> [{ts}] -> [{portal}] | {msg}"

    async def _refresh_if_needed(self) -> None:
        """Refreshes the token if it is expired or about to expire."""
        if not self.refresh_token:
            return

        now = int(time.time())
        if not self.expires_at or (now + 300 < self.expires_at):
            return

        self.log.info(
            self._triple_key_msg("Slack token expiring soon; attempting refresh")
        )  # noqa: E501

        async def _do_refresh() -> dict[str, Any]:
            data = {
                "client_id": settings.SLACK_CLIENT_ID,
                "client_secret": settings.SLACK_CLIENT_SECRET.get_secret_value(),
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }

            http = HTTPClient.get_client(corr_id=self.corr_id)
            resp = await http.post("https://slack.com/api/oauth.v2.access", data=data)
            resp.raise_for_status()
            payload = resp.json()

            if not payload.get("ok"):
                error = payload.get("error", "unknown_refresh_error")
                self.log.error(
                    self._triple_key_msg(f"Slack token refresh failed: {error}")
                )  # noqa: E501
                raise RuntimeError(f"Slack token refresh failed: {error}")

            expires_in = payload.get("expires_in")
            return {
                "access_token": payload["access_token"],
                "refresh_token": payload.get("refresh_token"),
                "expires_at": int(time.time()) + expires_in if expires_in else None,
            }

        try:
            # Use the injected lock provider if available (2026.03 Identity Guard)
            if self.refresh_lock_provider:
                result = await self.refresh_lock_provider(_do_refresh)
            else:
                result = await _do_refresh()
        except RuntimeError as exc:
            # Refresh token is stale/revoked (e.g. after re-install).
            # Clear it so we stop retrying and proceed with the existing access
            # token. If that token is also expired, the Slack SDK will surface a
            # clean token_expired error (caught in __getattr__) rather than
            # leaving a modal hanging silently.
            self.log.error(
                self._triple_key_msg(
                    f"Refresh token is invalid; clearing it and proceeding with "
                    f"existing access token. Re-install the app to fix permanently. "
                    f"Error: {exc}"
                )
            )
            self.refresh_token = None
            return

        self.bot_token = result["access_token"]
        self.refresh_token = result.get("refresh_token")
        self.expires_at = result.get("expires_at")

        self._client.token = self.bot_token
        self.log.info(self._triple_key_msg("Slack token refreshed successfully"))

        if self.on_token_refresh:
            await self.on_token_refresh(
                self.bot_token, self.refresh_token, self.expires_at
            )

    async def chat_postMessage(self, **kwargs) -> Any:
        await self._refresh_if_needed()
        channel = kwargs.get("channel")
        self.log.debug(
            self._triple_key_msg(f"Attempting Slack chat.postMessage channel={channel}")
        )  # noqa: E501

        # Override timeout for high-priority UI updates if needed
        # (Default pooled session is 10s, can be overridden here)
        return await self._client.chat_postMessage(**kwargs)

    async def users_info(self, **kwargs) -> Any:
        await self._refresh_if_needed()
        return await self._client.users_info(**kwargs)

    async def chat_update(self, **kwargs: Any) -> Any:
        await self._refresh_if_needed()
        return await self._client.chat_update(**kwargs)

    async def chat_unfurl(self, **kwargs: Any) -> Any:
        await self._refresh_if_needed()
        return await self._client.chat_unfurl(**kwargs)

    async def chat_postEphemeral(self, **kwargs: Any) -> Any:
        await self._refresh_if_needed()
        return await self._client.chat_postEphemeral(**kwargs)

    async def views_update(self, **kwargs: Any) -> Any:
        await self._refresh_if_needed()
        return await self._client.views_update(**kwargs)

    def __getattr__(self, name: str) -> Any:
        from slack_sdk.errors import SlackApiError

        attr = getattr(self._client, name)
        if callable(attr):

            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                await self._refresh_if_needed()
                method: Any = attr
                try:
                    return await method(*args, **kwargs)
                except SlackApiError as e:
                    if e.response.get("error") == "token_expired":
                        self.log.info(
                            self._triple_key_msg(
                                "Token expired unexpectedly; forcing refresh and retry"
                            )
                        )  # noqa: E501
                        self.expires_at = 1
                        await self._refresh_if_needed()
                        return await method(*args, **kwargs)

                    from app.core.exceptions import SlackAPIError

                    error_code = e.response.get("ok") is False and e.response.get(
                        "error"
                    )
                    raise SlackAPIError(
                        message=f"Slack API error: {error_code or str(e)}",
                        status_code=502,
                    ) from e

            return wrapper
        return attr
