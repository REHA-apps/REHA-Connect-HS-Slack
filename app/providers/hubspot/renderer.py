from __future__ import annotations  # noqa: D100

from typing import Any

from app.core.logging import get_logger
from app.core.models.ui import UnifiedCard

logger = get_logger("hubspot.renderer")


class HubSpotRenderer:
    """Converts a UnifiedCard IR into a modern React UI Extension JSON response.

    Emits only insight-unique data that complements (not duplicates) the native
    HubSpot sidebar.  Basic contact/company/deal properties are already
    visible in the record header and association cards.
    """

    # Metrics worth showing — everything else duplicates the native sidebar
    _INSIGHT_METRIC_KEYS = frozenset(
        {
            "Profile Score",  # contacts, leads
            "Rolling Pulse",  # sentiment window
            "Historical Baseline",
            "Pulse Score",
            "Sentiment Score",
            "REHA Pulse",
            "Heuristic Score",  # legacy alias — keep for safety
            "Risk",  # deals
            "Health",  # companies
            "Urgency",  # tickets
            # "Priority" intentionally excluded — tickets show this natively in HubSpot
            "SLA Status",  # unique AI-computed SLA health context
            # "Stage" intentionally excluded — deals/tasks show this natively in HubSpot
            "Assigned To",
            "Due",
            "Label",  # tasks
        }
    )

    def render(
        self,
        object_id: str,
        card: UnifiedCard,
        object_type: str = "contact",
    ) -> dict[str, Any]:
        # Only keep metrics that add insight value
        insight_metrics = [
            m
            for m in (card.metrics or [])
            if m[0] in self._INSIGHT_METRIC_KEYS or m[0] == "Lead Score"
        ]

        return {
            "objectId": object_id,
            "title": card.title or "Record Insights",
            "subtitle": card.subtitle,
            "emoji": card.emoji,
            "badge": card.badge,
            "content": card.content,
            "metrics": insight_metrics,
            "secondary_content": card.secondary_content,
        }
