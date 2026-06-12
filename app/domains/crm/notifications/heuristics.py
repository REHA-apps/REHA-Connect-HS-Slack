from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.db.storage_service import StorageService
from app.domains.ai.service import AIService
from app.domains.crm.hubspot.service import HubSpotService
from app.domains.crm.integration_service import IntegrationService
from app.utils.constants import HS_CALL_OUTCOME_CONNECTED

logger = get_logger("crm.notifications")


class NotificationHeuristicsMixin:
    """Mixin for Notification capabilities."""

    if TYPE_CHECKING:
        corr_id: str
        storage: StorageService
        hubspot: HubSpotService
        integration_service: IntegrationService
        ai: AIService

    def _should_notify(  # noqa: PLR0911, PLR0912
        self, obj: Mapping[str, Any], analysis: Any, event: dict[str, Any]
    ) -> bool:
        """Determine if a notification should be sent.

        Uses event fields or AI analysis to decide.
        """
        sub_type = event.get("subscriptionType", "")
        obj_type = self._map_subscription_to_type(sub_type, event) or ""

        # 1. High-Value Activity Logging
        # If a task or call is being logged (body/disposition change), always notify.
        if obj_type == "task":
            task_type = obj.get("properties", {}).get("hs_task_type")
            if task_type in ["CALL", "MEETING", "EMAIL"]:
                # Notify if body or status or priority changes
                if event.get("propertyName") in [
                    "hs_task_body",
                    "hs_task_status",
                    "hs_task_priority",
                ]:
                    return True
        elif obj_type == "call":
            # Any property change on a call object is usually significant
            # (body, disposition, etc.)
            return True
        elif obj_type == "meeting":
            return True

        # 2. Ticket priority changed to HIGH or URGENT — always notify.
        if event.get("propertyName") == "hs_ticket_priority" and str(
            event.get("propertyValue", "")
        ).upper() in ("HIGH", "URGENT"):
            return True

        # 2. Ticket pipeline stage changed — always notify.
        if event.get("propertyName") == "hs_pipeline_stage":
            return True

        # 3. Deal stage changed — always notify.
        if event.get("propertyName") == "dealstage":
            return True

        # 4. Task status or priority changed — always notify.
        if event.get("propertyName") in ("hs_task_status", "hs_task_priority"):
            # If priority changed, only notify if it's HIGH or URGENT
            if event.get("propertyName") == "hs_task_priority":
                if str(event.get("propertyValue", "")).upper() in ("HIGH", "URGENT"):
                    return True
                return False  # Suppress low-priority changes
            return True

        # 3. AI-driven: Deals with High/Critical risk
        if hasattr(analysis, "risk"):
            if analysis.risk in ["High", "Critical"]:
                return True

        # 4. AI-driven: Tickets with High/Critical urgency
        if hasattr(analysis, "urgency"):
            if analysis.urgency in ["High", "Critical"]:
                return True

        # 5. AI-driven: Contacts with score >= threshold
        if hasattr(analysis, "score"):
            try:
                if int(analysis.score) >= self.AI_SCORE_THRESHOLD:
                    return True
            except (ValueError, TypeError):
                pass

        # 6. Conversations: always notify
        if hasattr(analysis, "status") and "Conversation" in str(
            getattr(analysis, "summary", "")
        ):
            return True

        # 7. Calls: notify if outcome is "Connected"
        if event.get("objectTypeId") == "0-48" or "call" in str(
            event.get("subscriptionType", "")
        ):
            # Check property change for disposition
            if event.get("propertyName") == "hs_call_disposition":
                val = str(event.get("propertyValue", "")).lower()
                # Typical "Connected" dispositions often contain the word 'connected'
                # or are specific system IDs. We'll be broad here.

                if "connected" in val or val == HS_CALL_OUTCOME_CONNECTED:
                    return True

        # Default: suppress to keep signal-to-noise ratio high
        return False
