from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.connectors.slack.slack_channel import SlackChannel  # noqa: PLC0415
from app.core.exceptions import IntegrationNotFoundError
from app.core.logging import get_logger
from app.core.models.channel import OutboundMessage
from app.domains.crm.integration_service import IntegrationService

logger = get_logger("slack.messaging")


class CoreMessagingMixin:
    """Mixin for Slack messaging capabilities."""

    if TYPE_CHECKING:
        corr_id: str
        integration_service: IntegrationService

        async def get_slack_channel(self) -> Any: ...

        async def _resolve_channel(
            self,
            workspace_id: str,
            channel_id: str | None,
            obj: Mapping[str, Any] | None = None,
            is_system_alert: bool = False,
        ) -> str | None: ...

    async def send_message(
        self,
        *,
        workspace_id: str,
        channel: str | None,
        blocks: list[dict[str, Any]] | None = None,
        text: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        thread_ts: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        unfurl_links: bool = True,
    ) -> Mapping[str, Any] | None:
        """Dispatches a generic message or Block Kit payload to Slack."""
        channel_inst = await self.get_slack_channel()
        try:
            channel = await self._resolve_channel(workspace_id, channel)
        except IntegrationNotFoundError:
            logger.warning(
                "Configuration missing: No Slack channel resolved. Skipping message."
            )
            return

        if not channel:
            return None

        if thread_ts == "CHANNEL_ROOT":
            thread_ts = None

        message = OutboundMessage(
            workspace_id=workspace_id,
            destination=channel,
            text=text,
            provider_metadata={
                "blocks": blocks,
                "attachments": attachments,
                "thread_ts": thread_ts,
            },
        )

        return await channel_inst.send_message(message)

    async def update_message(
        self,
        *,
        workspace_id: str,
        channel: str,
        ts: str,
        text: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> Mapping[str, Any] | None:
        """Updates an existing Slack message in place."""
        channel_inst = await self.get_slack_channel()
        assert isinstance(channel_inst, SlackChannel), "Expected SlackChannel instance"
        return await channel_inst.update_message(
            channel_id=channel,
            ts=ts,
            text=text,
            blocks=blocks,
        )

    async def send_via_response_url(
        self,
        response_url: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        replace_original: bool = False,
    ) -> bool:
        """Sends a response to a Slack slash command or interaction using the
        response_url.

        # noqa: E501
        """
        channel = await self.get_slack_channel()
        return await channel.send_via_response_url(
            response_url=response_url,
            text=text,
            blocks=blocks,
            replace_original=replace_original,
        )

    async def send_dm(
        self,
        *,
        user_id: str | None = None,
        user_email: str | None = None,
        text: str,
    ) -> bool:
        """Sends a direct message to a user by ID or email."""
        try:
            channel_inst = await self.get_slack_channel()
            if not user_id and user_email:
                user_id = await channel_inst.get_user_by_email(user_email)

            if not user_id:
                logger.warning("Could not resolve Slack user ID.")
                return False

            await channel_inst.send_dm(user_id=user_id, text=text)
            return True
        except Exception as exc:
            logger.error("Failed to send DM: %s", exc, exc_info=True)
            return False

    async def send_error(
        self,
        exc: Exception,
        *,
        response_url: str | None = None,
        user_id: str | None = None,
        channel_id: str | None = None,
        integration: Any = None,
        context: str = "open modal",
    ) -> None:
        """Centralizes Slack error notification routing (Response URL, Ephemeral, or DM)."""
        error_msg = f"❌ Failed to {context}: {str(exc)}"
        if response_url:
            await self.send_via_response_url(response_url=response_url, text=error_msg)
            return

        if not user_id or not integration:
            logger.warning("Cannot send slack error without user_id and integration")
            return

        try:
            if not self.integration_service:
                return

            client = await self.integration_service.get_slack_client(integration)
            if not client:
                return

            if channel_id:
                await client.chat_postEphemeral(
                    channel=channel_id, user=user_id, text=error_msg
                )
            else:
                await client.chat_postMessage(channel=user_id, text=error_msg)
        except Exception as fallback_exc:
            logger.error(
                "Failed to route localized slack error %s: %s", user_id, fallback_exc
            )
