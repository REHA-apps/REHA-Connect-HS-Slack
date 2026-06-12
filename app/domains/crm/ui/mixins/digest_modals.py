"""UI Mixin for handling Scheduled Digest configuration modals."""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.db.storage_service import StorageService

logger = get_logger(__name__)


class DigestModalsMixin:
    """Handles the UI generation and form submission for Scheduled Digests."""

    async def build_create_digest_modal(
        self, user_timezone: str = "UTC"
    ) -> dict[str, Any]:
        """Builds the Block Kit modal for creating a new Scheduled Digest."""
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Set up a new Scheduled Digest to receive automated HubSpot reports in Slack.",
                },
            },
            {
                "type": "input",
                "block_id": "digest_template",
                "label": {"type": "plain_text", "text": "Report Template"},
                "element": {
                    "type": "static_select",
                    "action_id": "template_selection",
                    "placeholder": {"type": "plain_text", "text": "Select a report..."},
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Stale Deals (>14 days inactive)",
                            },
                            "value": "stale_deals",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "New Leads (Created this week)",
                            },
                            "value": "new_leads",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Weekly Conversions (Deals won)",
                            },
                            "value": "weekly_conversions",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Task Roundup (Overdue and upcoming tasks)",
                            },
                            "value": "task_roundup",
                        },
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "digest_schedule",
                "label": {"type": "plain_text", "text": "Schedule"},
                "element": {
                    "type": "static_select",
                    "action_id": "schedule_selection",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select frequency...",
                    },
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "Daily at 8:00 AM"},
                            "value": "0 8 * * *",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Weekly on Monday 8:00 AM",
                            },
                            "value": "0 8 * * 1",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Every 15 Minutes (Testing)",
                            },
                            "value": "*/15 * * * *",
                        },
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "digest_timezone",
                "label": {"type": "plain_text", "text": "Timezone"},
                "element": {
                    "type": "static_select",
                    "action_id": "timezone_selection",
                    "initial_option": {
                        "text": {"type": "plain_text", "text": user_timezone},
                        "value": user_timezone,
                    },
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": user_timezone},
                            "value": user_timezone,
                        },
                        {"text": {"type": "plain_text", "text": "UTC"}, "value": "UTC"},
                        {
                            "text": {"type": "plain_text", "text": "America/New_York"},
                            "value": "America/New_York",
                        },
                        {
                            "text": {"type": "plain_text", "text": "Europe/London"},
                            "value": "Europe/London",
                        },
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "digest_channel",
                "label": {"type": "plain_text", "text": "Target Channel"},
                "element": {
                    "type": "channels_select",
                    "action_id": "channel_selection",
                    "placeholder": {"type": "plain_text", "text": "Select a channel"},
                },
            },
        ]

        return {
            "type": "modal",
            "callback_id": "submit_create_digest",
            "title": {"type": "plain_text", "text": "Create Digest"},
            "submit": {"type": "plain_text", "text": "Save Schedule"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": blocks,
        }

    async def handle_create_digest_submission(
        self, workspace_id: str, payload: dict[str, Any], storage: StorageService
    ) -> None:
        """Processes the modal submission and saves the digest to the database."""
        state_values = payload.get("view", {}).get("state", {}).get("values", {})

        template_id = (
            state_values.get("digest_template", {})
            .get("template_selection", {})
            .get("selected_option", {})
            .get("value")
        )
        cron_expression = (
            state_values.get("digest_schedule", {})
            .get("schedule_selection", {})
            .get("selected_option", {})
            .get("value")
        )
        timezone = (
            state_values.get("digest_timezone", {})
            .get("timezone_selection", {})
            .get("selected_option", {})
            .get("value")
        )
        target_channel = (
            state_values.get("digest_channel", {})
            .get("channel_selection", {})
            .get("selected_channel")
        )

        if not all([template_id, cron_expression, timezone, target_channel]):
            logger.error("Incomplete digest configuration.")
            return

        digest_payload = {
            "workspace_id": workspace_id,
            "target_channel": target_channel,
            "cron_expression": cron_expression,
            "timezone": timezone,
            "template_id": template_id,
        }

        await storage.upsert_scheduled_digest(digest_payload)
        logger.info(
            "Successfully created scheduled digest for workspace=%s, template=%s",
            workspace_id,
            template_id,
        )
