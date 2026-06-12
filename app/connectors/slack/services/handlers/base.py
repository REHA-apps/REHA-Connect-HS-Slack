from __future__ import annotations  # noqa: D100

from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

from app.core.logging import get_logger
from app.db.records import Provider
from app.domains.common.sdk.context import UnifiedContext
from app.domains.common.sdk.handler import BaseInteractionHandler

if TYPE_CHECKING:
    from app.connectors.slack.slack_channel import SlackChannel
    from app.core.models.ui import ModalMetadata, UnifiedCard
    from app.db.records import IntegrationRecord
    from app.domains.messaging.slack.service import SlackMessagingService

from app.domains.crm.ui.card_builder import CardBuilder
from app.utils.constants import SLACK_ERROR_ICON

logger = get_logger("base_handler")

T = TypeVar("T")


def interaction_handler(
    *actions: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a method as a handler for specific actions or callback IDs."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        existing = getattr(func, "__interaction_actions__", [])
        setattr(func, "__interaction_actions__", list(set(existing + list(actions))))
        return func

    return decorator


def with_slack_error_handling(
    action_name: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to catch exceptions, log with traceback, and notify user via Slack."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            # Extract basic context for the context manager
            payload = kwargs.get("payload") or (args[0] if args else {})
            messaging_service = kwargs.get("messaging_service")

            async with slack_error_handling(
                action_name=action_name,
                payload=payload,
                messaging_service=messaging_service,
                user_id=kwargs.get("user_id"),
                response_url=kwargs.get("response_url"),
            ):
                return await func(self, *args, **kwargs)

        return wrapper

    return decorator


def require_feature(
    feature_id: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to gate interaction handlers behind a feature flag."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            integration = kwargs.get("integration")
            messaging_service = kwargs.get("messaging_service")
            trigger_id = kwargs.get("trigger_id")
            view_id = kwargs.get("view_id")

            if integration and messaging_service:
                has_access = await self.integration_service.check_feature_access(
                    integration.workspace_id, feature_id
                )
                if not has_access:
                    return await self._handle_gated_click(
                        feature_id=feature_id,
                        trigger_id=trigger_id,
                        integration=integration,
                        messaging_service=messaging_service,
                        view_id=view_id,
                        response_url=kwargs.get("response_url"),
                    )
            return await func(self, *args, **kwargs)

        return wrapper

    return decorator


@asynccontextmanager
async def slack_error_handling(
    action_name: str,
    payload: Mapping[str, Any],
    messaging_service: SlackMessagingService | None = None,
    user_id: str | None = None,
    response_url: str | None = None,
):
    """Context manager to catch exceptions and notify user via Slack."""
    try:
        yield
    except Exception as exc:
        logger.exception("Failed to %s", action_name)
        if not response_url and isinstance(payload, dict):
            response_url = payload.get("response_url")
        if not user_id and isinstance(payload, dict):
            user_id = payload.get("user", {}).get("id")

        if response_url and messaging_service:
            try:
                await messaging_service.send_via_response_url(
                    response_url=str(response_url),
                    text=f"{SLACK_ERROR_ICON} Failed to {action_name}: {str(exc)}",
                )
            except Exception as inner_exc:
                logger.error("Failed to send error to response_url: %s", inner_exc)
        elif user_id and messaging_service:
            try:
                slack_channel = await messaging_service.get_slack_channel()
                client = cast("SlackChannel", slack_channel).get_slack_client()
                await client.chat_postMessage(
                    channel=str(user_id),
                    text=f"{SLACK_ERROR_ICON} Failed to {action_name}: {str(exc)}",
                )
            except Exception as inner_exc:
                logger.error("Failed to post error message to user: %s", inner_exc)


class InteractionHandler(BaseInteractionHandler):
    async def handle(
        self,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        **kwargs: Any,
    ) -> Any:
        """Legacy Slack handle method. Resolves context and dispatches to SDK handle_interaction."""
        context = UnifiedContext.from_slack_payload(
            payload, workspace_id=integration.workspace_id, **kwargs
        )
        return await self.handle_interaction(
            context=context,
            payload=payload,
            integration=integration,
            messaging_service=messaging_service,
            **kwargs,
        )

    async def _show_loading(
        self,
        trigger_id: str,
        title: str,
        integration: IntegrationRecord,
        *,
        user_id: str = "",
    ) -> str | None:
        """Legacy helper: Opens a loading modal via the UI adapter."""
        # Create a transient context for legacy compatibility
        context = UnifiedContext(
            platform=Provider.SLACK,
            user_id=user_id,
            workspace_id=integration.workspace_id,
            trigger_id=trigger_id,
        )
        return await self.ui.show_loading(context, title=title, integration=integration)

    async def _update_modal(
        self,
        view_id: str,
        view_or_card: dict[str, Any] | UnifiedCard,
        title: str,
        integration: IntegrationRecord,
        metadata: str | None = None,
        *,
        user_id: str = "",
    ) -> bool:
        """Legacy helper: Updates a modal via the UI adapter."""
        context = UnifiedContext(
            platform=Provider.SLACK,
            user_id=user_id,
            workspace_id=integration.workspace_id,
            view_id=view_id,
        )
        return await self.ui.update_modal(
            context,
            view_or_card=view_or_card,
            title=title,
            integration=integration,
            metadata=metadata,
        )

    async def _open_modal(
        self,
        trigger_id: str | None,
        view_or_card: dict[str, Any] | UnifiedCard,
        title: str,
        integration: IntegrationRecord,
        metadata: str | None = None,
        *,
        user_id: str = "",
    ) -> str | None:
        """Legacy helper: Opens a modal via the UI adapter."""
        context = UnifiedContext(
            platform=Provider.SLACK,
            user_id=user_id,
            workspace_id=integration.workspace_id,
            trigger_id=trigger_id,
        )
        return await self.ui.open_modal(
            context,
            view_or_card=view_or_card,
            title=title,
            integration=integration,
            metadata=metadata,
        )

    async def _resolve_primary_contact(
        self,
        workspace_id: str,
        object_type: str,
        object_id: str,
        context_label: str = "action",
    ) -> tuple[str, str]:
        """Resolves an engagement to its primary contact.

        If ``object_type`` is an engagement type (call, note, email, meeting),
        looks up the associated contact and returns its type and ID.
        Otherwise returns the inputs unchanged.

        Returns:
            A (object_type, object_id) tuple — potentially redirected to a contact.

        """
        if object_type not in ("call", "note", "email", "meeting"):
            return object_type, object_id
        try:
            assoc_contacts = await self.crm.get_associated_objects(
                workspace_id=workspace_id,
                from_object_type=object_type,
                object_id=object_id,
                to_object_type="contact",
            )
            if assoc_contacts:
                resolved_id = assoc_contacts[0]["id"]
                logger.info(
                    "Resolved %s association to contact_id=%s for %s",
                    object_type,
                    resolved_id,
                    context_label,
                )
                return "contact", resolved_id
        except Exception:
            logger.warning(
                "Failed to resolve %s associations for %s",
                object_type,
                context_label,
            )
        return object_type, object_id

    async def _publish_timeline_event(
        self,
        *,
        workspace_id: str,
        object_type: str,
        object_id: str,
        message_body: str,
        channel_id: str | None,
        payload: Mapping[str, Any],
    ) -> None:
        """Publishes a custom timeline event to HubSpot if configured.

        Silently skips if ``HUBSPOT_MESSAGE_TEMPLATE_ID`` is not set.
        """
        from app.core.config import settings

        if not settings.HUBSPOT_MESSAGE_TEMPLATE_ID:
            return
        try:
            user_payload = payload.get("user", {})
            sender_name = (
                user_payload.get("real_name")
                or user_payload.get("name")
                or f"User {user_payload.get('id', 'Unknown')}"
            )
            await self.crm.publish_app_event(
                workspace_id=workspace_id,
                event_template_id=settings.HUBSPOT_MESSAGE_TEMPLATE_ID,
                object_type=object_type,
                object_id=object_id,
                properties={
                    "message_body": message_body,
                    "channel_name": (f"<#{channel_id}>" if channel_id else "DM"),
                    "sender_name": sender_name,
                },
            )
        except Exception as exc:
            logger.warning("Failed to publish custom timeline event: %s", exc)

    async def _handle_gated_click(
        self,
        feature_id: str,
        trigger_id: str | None,
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        view_id: str | None = None,
        response_url: str | None = None,
    ) -> None:
        """Shows the upgrade nudge modal when a gated feature is clicked.

        Args:
            feature_id: The ID of the feature they tried to access.
            trigger_id: The trigger ID from Slack to open a modal.
            integration: The integration record.
            messaging_service: The messaging service for Slack API calls.
            view_id: The view ID of an existing modal to update.
            response_url: Fallback URL to send ephemeral message if modal fails.

        """
        # Fetch portal_id from workspace (Slack integration doesn't store it)
        workspace = await self.integration_service.storage.get_workspace(
            integration.workspace_id
        )
        portal_id = workspace.portal_id if workspace else None
        builder = CardBuilder()
        modal = builder.build_upgrade_nudge_modal(
            feature_name=feature_id,
            portal_id=portal_id,
            workspace_id=integration.workspace_id,
        )
        client = await self.integration_service.get_slack_client(integration)

        try:
            if view_id:
                await client.views_update(view_id=view_id, view=modal)
                return
            elif trigger_id:
                await client.views_open(trigger_id=trigger_id, view=modal)
                return
        except Exception as exc:
            logger.warning(
                "Failed to open/update gated modal, falling back to ephemeral: %s", exc
            )

        if response_url:
            from app.core.config import settings

            await messaging_service.send_via_response_url(
                response_url=response_url,
                text=(
                    f"{feature_id.replace('_', ' ').title()} features require "
                    "a Professional Plan. "
                    f"<{settings.PRICING_URL}|Upgrade to Pro> to continue."
                ),
            )

    def _parse_modal_metadata(self, metadata: str) -> ModalMetadata:
        """Parses Slack modal metadata string into a typed ModalMetadata object.

        Handles both JSON-serialized metadata and legacy colon-separated strings.
        Ensures robust parsing to prevent hidden KeyErrors and improves readability.

        Args:
            metadata (str): The raw metadata string from Slack view payload.

        Returns:
            ModalMetadata: A typed representation of the metadata fields.

        """
        import json

        from app.core.models.ui import ModalMetadata

        if not metadata:
            return ModalMetadata()
        try:
            return ModalMetadata.model_validate_json(metadata)
        except Exception:
            pass
        try:
            raw = json.loads(metadata)
            return ModalMetadata(**raw)
        except Exception:
            pass
        parts = metadata.split(":")
        if len(parts) >= 2:
            if parts[0] in ("deal", "contact", "company", "task", "ticket"):
                return ModalMetadata(object_type=parts[0], object_id=parts[1])
            return ModalMetadata(deal_id=parts[0], stage_id=parts[1])
        return ModalMetadata(deal_id=metadata)
