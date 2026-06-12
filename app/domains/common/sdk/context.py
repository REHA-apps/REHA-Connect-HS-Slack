from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.db.records import Provider


@dataclass
class UnifiedContext:
    """Provider-agnostic context for messaging platform interactions.

    Standardizes fields from Slack, WhatsApp, Teams, etc. into a common
    representation for domain logic.
    """

    platform: Provider
    user_id: str
    workspace_id: str  # Internal UUID or provider-specific ID
    channel_id: str | None = None
    response_url: str | None = None
    trigger_id: str | None = None
    action_id: str | None = None
    value: str | None = None
    view_id: str | None = None
    private_metadata: str | None = None
    corr_id: str | None = None

    @classmethod
    def from_slack_payload(
        cls, payload: Mapping[str, Any], workspace_id: str, **kwargs: Any
    ) -> UnifiedContext:
        """Parses a Slack interaction payload into a UnifiedContext."""
        user_id = payload.get("user", {}).get("id", "")
        # team_id is usually synonymous with workspace_id in Slack
        channel_id = payload.get("channel", {}).get("id") or kwargs.get("channel_id")
        response_url = payload.get("response_url") or kwargs.get("response_url")
        trigger_id = payload.get("trigger_id") or kwargs.get("trigger_id")

        # Extract action_id and value from block_actions
        action_id = kwargs.get("action_id")
        value = kwargs.get("value")

        actions = payload.get("actions", [])
        if actions and isinstance(actions, list):
            action_id = action_id or actions[0].get("action_id")
            value = value or actions[0].get("value")

        corr_id = kwargs.get("corr_id")

        view_id = kwargs.get("view_id")
        private_metadata = None
        view = payload.get("view")
        if view and isinstance(view, dict):
            view_id = view_id or view.get("id")
            private_metadata = view.get("private_metadata")
            if private_metadata:
                try:
                    meta = json.loads(private_metadata)
                    if isinstance(meta, dict):
                        if meta.get("channel_id"):
                            channel_id = meta.get("channel_id")
                        if meta.get("response_url"):
                            response_url = meta.get("response_url")
                except (json.JSONDecodeError, TypeError):
                    pass

        return cls(
            platform=Provider.SLACK,
            user_id=user_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            response_url=response_url,
            trigger_id=trigger_id,
            action_id=action_id,
            value=value,
            view_id=view_id,
            private_metadata=private_metadata,
            corr_id=corr_id,
        )

    def get_entity_id(self) -> str | None:
        """Extracts the numeric entity ID from either action_id or value."""
        raw_context = self.action_id or self.value or ""
        parts = raw_context.split(":")
        extracted = next((p for p in parts if p.isdigit()), None)
        if extracted:
            return extracted
        if len(parts) > 1:
            return parts[-1]
        return None

    def build_metadata(self, **extra: Any) -> str:
        """Builds a JSON metadata string for modal private_metadata."""
        data: dict[str, Any] = {
            "channel_id": self.channel_id,
            "response_url": self.response_url,
        }
        data.update(extra)
        return json.dumps(data)
