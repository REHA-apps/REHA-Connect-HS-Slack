from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from app.core.logging import get_logger
from app.db.records import IntegrationRecord
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils import action_ids

from .base import (
    InteractionHandler,
    UnifiedContext,
    interaction_handler,
    require_feature,
    slack_error_handling,
)

logger = get_logger("deal_modal_handlers")


class DealModalHandlers(InteractionHandler):
    @interaction_handler(action_ids.UPDATE_DEAL_TYPE_SUBMISSION)
    async def _handle_update_deal_type_submission(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        view = payload.get("view", {})
        metadata = self._parse_modal_metadata(view.get("private_metadata", ""))
        deal_id = metadata.deal_id
        response_url = metadata.response_url
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)
        deal_type = properties.get("deal_type_input", "")
        if not deal_id:
            logger.warning("Missing deal_id in metadata for update_deal_type_modal")
            return
        from app.domains.crm.notification_service import _recent_notifications

        await _recent_notifications.set(
            f"notif_debounce:{integration.workspace_id}:deal:{deal_id}", True
        )
        async with slack_error_handling(
            "update Deal Type",
            payload,
            messaging_service,
            response_url=response_url,
        ):
            await self.crm.update_object(
                workspace_id=integration.workspace_id,
                object_type="deal",
                object_id=deal_id,
                properties={"dealtype": deal_type},
            )
            await messaging_service.refresh_and_update_card(
                workspace_id=integration.workspace_id,
                object_type="deal",
                object_id=deal_id,
                channel_id=metadata.channel_id,
                response_url=response_url,
                text=f"Deal type updated to {deal_type}",
            )
        return

    @interaction_handler(action_ids.POST_MORTEM_SUBMISSION)
    async def _handle_post_mortem_submission(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        view = payload.get("view", {})
        metadata = self._parse_modal_metadata(view.get("private_metadata", ""))
        deal_id = metadata.deal_id
        stage_id = metadata.stage_id
        response_url = metadata.response_url
        from app.utils.parsers import extract_state_values

        properties_extracted = extract_state_values(view)
        properties = {"dealstage": stage_id}
        won_reason = properties_extracted.get("closed_won_reason", "")
        lost_reason = properties_extracted.get("closed_lost_reason", "")
        if won_reason:
            properties["closed_won_reason"] = won_reason
        if lost_reason:
            properties["closed_lost_reason"] = lost_reason
        if not deal_id:
            logger.warning("Missing deal_id for post_mortem_submission")
            return
        from app.domains.crm.notification_service import _recent_notifications

        await _recent_notifications.set(
            f"notif_debounce:{integration.workspace_id}:deal:{deal_id}", True
        )
        async with slack_error_handling(
            "record post-mortem",
            payload,
            messaging_service,
            response_url=response_url,
        ):
            await self.crm.update_object(
                workspace_id=integration.workspace_id,
                object_type="deal",
                object_id=deal_id,
                properties=properties,
            )
            note = f"Post-Mortem for {stage_id}: "
            if won_reason:
                note += f"Won Reason: {won_reason}. "
            if lost_reason:
                note += f"Lost Reason: {lost_reason}."
            if not deal_id:
                logger.warning("Missing deal_id for post_mortem_note")
            else:
                await self.crm.create_note(
                    workspace_id=integration.workspace_id,
                    content=note,
                    associated_id=deal_id,
                    associated_type="deal",
                )

                # Clear cache so subsequent lookups show the new note
                await self.crm.invalidate_object_caches(
                    workspace_id=integration.workspace_id,
                    object_type="deal",
                    object_id=deal_id,
                )

            await messaging_service.refresh_and_update_card(
                workspace_id=integration.workspace_id,
                object_type="deal",
                object_id=deal_id,
                channel_id=metadata.channel_id,
                response_url=response_url,
                text=f"Deal stage updated to {stage_id}",
            )
        return

    @interaction_handler(action_ids.CALCULATOR_SUBMISSION)
    async def _handle_calculator_submission(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        view = payload.get("view", {})
        metadata = self._parse_modal_metadata(view.get("private_metadata", ""))
        deal_id = metadata.deal_id
        response_url = metadata.response_url
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)
        if not deal_id:
            logger.warning("Missing deal_id in metadata for calculator_submission")
            return

        from app.domains.crm.notification_service import _recent_notifications

        await _recent_notifications.set(
            f"notif_debounce:{integration.workspace_id}:deal:{deal_id}", True
        )
        async with slack_error_handling(
            "calculate deal amount",
            payload,
            messaging_service,
            response_url=response_url,
        ):
            qty = float(properties.get("quantity", "1") or "1")
            price = float(properties.get("unit_price", "0") or "0")
            disc = float(properties.get("discount_percent", "0") or "0")
            total = qty * price * (1 - disc / 100)

            await self.crm.update_object(
                workspace_id=integration.workspace_id,
                object_type="deal",
                object_id=deal_id,
                properties={"amount": str(total)},
            )
            await messaging_service.refresh_and_update_card(
                workspace_id=integration.workspace_id,
                object_type="deal",
                object_id=deal_id,
                channel_id=metadata.channel_id,
                response_url=response_url,
                text="Deal amount updated.",
            )
        return

    @interaction_handler(action_ids.NEXT_STEP_ENFORCEMENT_SUBMISSION)
    async def _handle_next_step_enforcement_submission(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        view = payload.get("view", {})
        metadata = self._parse_modal_metadata(view.get("private_metadata", ""))
        deal_id = metadata.deal_id
        stage_id = metadata.stage_id
        response_url = metadata.response_url
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)
        next_step = properties.get("next_step", "")
        if not deal_id:
            logger.warning("Missing deal_id in metadata for next_step_enforcement")
            return
        from app.domains.crm.notification_service import _recent_notifications

        await _recent_notifications.set(
            f"notif_debounce:{integration.workspace_id}:deal:{deal_id}", True
        )
        async with slack_error_handling(
            "enforce next step",
            payload,
            messaging_service,
            response_url=response_url,
        ):
            try:
                await self.crm.update_object(
                    workspace_id=integration.workspace_id,
                    object_type="deal",
                    object_id=deal_id,
                    properties={"dealstage": stage_id, "hs_next_step": next_step},
                )
            except Exception as exc:
                if "PROPERTY_DOESNT_EXIST" in str(exc) or "VALIDATION_ERROR" in str(
                    exc
                ):
                    logger.warning(
                        "Property hs_next_step failed, falling back to dealstage only"
                    )
                    await self.crm.update_object(
                        workspace_id=integration.workspace_id,
                        object_type="deal",
                        object_id=deal_id,
                        properties={"dealstage": stage_id},
                    )
                else:
                    raise exc

            await messaging_service.refresh_and_update_card(
                workspace_id=integration.workspace_id,
                object_type="deal",
                object_id=deal_id,
                channel_id=metadata.channel_id,
                response_url=response_url,
                text=f"Deal stage updated to {stage_id}",
            )

        return

    @interaction_handler(action_ids.OPEN_UPDATE_DEAL_TYPE_MODAL)
    @require_feature("deal_type")
    async def _handle_open_update_deal_type_modal(
        self,
        value: str,
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        trigger_id: str | None,
        **kwargs: Any,
    ) -> None:
        parts = value.split(":")
        if len(parts) < 2:
            logger.warning("Malformed update_deal_type value=%s", value)
            return
        deal_id = parts[1]
        if not trigger_id:
            return
        view_id = await self._show_loading(trigger_id, "Loading...", integration)
        try:
            metadata = context.build_metadata(deal_id=deal_id)
            deal = await self.crm.get_deal(
                workspace_id=integration.workspace_id, object_id=deal_id
            )
            current_value = (
                (deal.get("properties") or {}).get("dealtype", "") if deal else ""
            )
            modal = messaging_service.cards.build_update_deal_type_modal(
                deal_id, current_value, metadata=metadata
            )
            if view_id:
                await self._update_modal(
                    view_id, modal, "Update Deal Type", integration
                )
            else:
                await self._open_modal(
                    trigger_id, modal, "Update Deal Type", integration
                )
        except Exception:
            logger.exception("Failed to open deal type modal")

    @interaction_handler(action_ids.OPEN_CALCULATOR)
    @require_feature("pricing_calculator")
    async def _handle_open_calculator_modal(
        self,
        value: str,
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        trigger_id: str | None,
        **kwargs: Any,
    ) -> None:
        """Fetch deal and open calculator modal."""
        parts = value.split(":")
        if len(parts) < 2:
            return
        deal_id = parts[1]
        if not trigger_id:
            return
        view_id = await self._show_loading(
            trigger_id, "Fetching Deal Details...", integration
        )
        try:
            deal = await self.crm.get_deal(
                workspace_id=integration.workspace_id, object_id=deal_id
            )
            if not deal:
                logger.warning("Deal not found for id=%s", deal_id)
                return
            props = deal.get("properties") or {}
            amount_str = props.get("amount", "0")
            amount = float(amount_str) if amount_str else 0.0
            metadata = context.build_metadata(deal_id=deal_id)
            modal = messaging_service.cards.build_pricing_calculator_modal(
                deal_id, amount, metadata=metadata
            )
            if view_id:
                await self._update_modal(view_id, modal, "Deal Calculator", integration)
            else:
                await self._open_modal(
                    trigger_id, modal, "Deal Calculator", integration
                )
        except Exception as exc:
            logger.exception("Failed to open calculator modal")
            response_url = context.response_url
            if response_url:
                await messaging_service.send_via_response_url(
                    response_url=response_url,
                    text=f"❌ Failed to open calculator modal: {str(exc)}",
                )

    @interaction_handler(action_ids.LOG_NEXT_STEP)
    @require_feature("deal_next_step")
    async def _handle_open_log_next_step_modal(
        self,
        value: str,
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        trigger_id: str | None,
        **kwargs: Any,
    ) -> None:
        """Opens a standalone modal to log a Next Step on a deal."""
        parts = value.split(":")
        if len(parts) < 2:
            logger.warning("Malformed log_next_step value=%s", value)
            return
        deal_id = parts[1]
        if not trigger_id:
            return
        view_id = await self._show_loading(trigger_id, "Loading...", integration)
        try:
            metadata = context.build_metadata(deal_id=deal_id)
            modal = messaging_service.cards.build_log_next_step_modal(
                deal_id, metadata=metadata
            )
            if view_id:
                await self._update_modal(view_id, modal, "Log Next Step", integration)
            else:
                await self._open_modal(trigger_id, modal, "Log Next Step", integration)
        except Exception:
            logger.exception("Failed to open log_next_step modal")

    @interaction_handler(action_ids.LOG_NEXT_STEP_SUBMISSION)
    async def _handle_log_next_step_submission(
        self,
        *,
        payload: Mapping[str, Any],
        integration: IntegrationRecord,
        messaging_service: SlackMessagingService,
        context: UnifiedContext,
        **kwargs: Any,
    ) -> None:
        """Saves the Next Step to HubSpot without changing the deal stage."""
        view = payload.get("view", {})
        metadata = self._parse_modal_metadata(view.get("private_metadata", ""))
        deal_id = metadata.deal_id
        response_url = metadata.response_url
        from app.utils.parsers import extract_state_values

        properties = extract_state_values(view)
        next_step = properties.get("next_step", "").strip()
        if not deal_id:
            logger.warning("Missing deal_id in metadata for log_next_step_submission")
            return
        if not next_step:
            return

        from app.domains.crm.notification_service import _recent_notifications

        await _recent_notifications.set(
            f"notif_debounce:{integration.workspace_id}:deal:{deal_id}", True
        )
        async with slack_error_handling(
            "log next step",
            payload,
            messaging_service,
            response_url=response_url,
        ):
            try:
                await self.crm.update_object(
                    workspace_id=integration.workspace_id,
                    object_type="deal",
                    object_id=deal_id,
                    properties={"hs_next_step": next_step},
                )
            except Exception as exc:
                if "PROPERTY_DOESNT_EXIST" in str(exc) or "VALIDATION_ERROR" in str(
                    exc
                ):
                    logger.warning(
                        "Property hs_next_step doesn't exist, logging as note instead"
                    )
                    await self.crm.create_note(
                        workspace_id=integration.workspace_id,
                        content=f"Next Step: {next_step}",
                        associated_id=deal_id,
                        associated_type="deal",
                    )
                else:
                    raise

            await messaging_service.refresh_and_update_card(
                workspace_id=integration.workspace_id,
                object_type="deal",
                object_id=deal_id,
                channel_id=metadata.channel_id,
                response_url=response_url,
                text=f"Next step logged: {next_step}",
            )
