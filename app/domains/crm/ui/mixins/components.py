from __future__ import annotations  # noqa: D100

from typing import Any

from app.core.models.ui import UnifiedCard
from app.utils.html import strip_html


class ComponentsMixin:
    """Generic UI utilities for the CRM domain."""

    def _strip_html(self, text: str) -> str:
        """Remove HTML tags from text."""
        return strip_html(text)

    def build_empty(self, message: str) -> UnifiedCard:
        return UnifiedCard(
            title="Notification",
            emoji="🔍",
            content=message,
        )

    def _input(
        self,
        label: str,
        action_id: str,
        placeholder: str = "",
        initial_value: str = "",
        multiline: bool = False,
        optional: bool = False,
    ) -> Any:
        """Generic input builder (overridden by connectors)."""
        return {
            "type": "input",
            "label": label,
            "action_id": action_id,
            "placeholder": placeholder,
            "initial_value": initial_value,
            "multiline": multiline,
            "optional": optional,
        }

    def _select(
        self,
        label: str,
        action_id: str,
        options: list[tuple[str, str]],
        initial_option: str | None = None,
        optional: bool = False,
    ) -> Any:
        """Generic select builder (overridden by connectors)."""
        return {
            "type": "select",
            "label": label,
            "action_id": action_id,
            "options": options,
            "initial_option": initial_option,
            "optional": optional,
        }

    def _datepicker(
        self,
        label: str,
        action_id: str,
        initial_date: str | None = None,
        optional: bool = False,
    ) -> Any:
        """Generic datepicker builder (overridden by connectors)."""
        return {
            "type": "datepicker",
            "label": label,
            "action_id": action_id,
            "initial_date": initial_date,
            "optional": optional,
        }
