# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.models.ui import UnifiedCard

from app.domains.crm.ui.mixins.action_modals import ActionModalsMixin
from app.domains.crm.ui.mixins.ai_cards import InsightsCardsMixin
from app.domains.crm.ui.mixins.components import ComponentsMixin
from app.domains.crm.ui.mixins.gating_mixins import GatingMixin
from app.domains.crm.ui.mixins.list_cards import ListCardsMixin
from app.domains.crm.ui.mixins.object_cards import ObjectCardsMixin

MAX_LIST_DISPLAY = 25
MAX_OWNERS_DISPLAY = 100


class CardBuilder(
    ObjectCardsMixin,
    InsightsCardsMixin,
    ListCardsMixin,
    ActionModalsMixin,
    GatingMixin,
    ComponentsMixin,
):
    """Unified utility for building platform-agnostic CRM and AI insight cards.

    Rules Applied:
        - Returns UnifiedCard IR.
        - Centralizes rendering logic for Contacts, Deals, Companies, Tickets,
          and Tasks.
    """

    def build_reports_card(
        self,
        workspace_id: str,
        sync_count: int,
        notification_count: int,
        portal_id: str | None = None,
        open_deals: int = 0,
        open_tickets: int = 0,
    ) -> UnifiedCard:
        """Generates a performance reporting dashboard for Slack."""
        from app.core.models.ui import CardAction, UnifiedCard

        actions = []
        if portal_id:
            actions.append(
                CardAction(
                    label="📈 View HubSpot Dashboards",
                    action_type="url",
                    value="open_dashboards",
                    url=f"https://app.hubspot.com/reports-dashboard/{portal_id}",
                )
            )

        metrics = [
            ("Open Deals", str(open_deals)),
            ("Open Tickets", str(open_tickets)),
            ("Records Shared", str(sync_count)),
            ("Notifications (Mo)", str(notification_count)),
        ]

        return UnifiedCard(
            title="📊 HubSpot Sync Report",
            subtitle=f"Workspace: {workspace_id}",
            metrics=metrics,
            content="Your HubSpot-to-Slack integration is running smoothly. Click below to view your full CRM analytics in HubSpot.",
            actions=actions,
        )

    def build_app_home_view(
        self,
        workspace: Any | None = None,
        integration: Any | None = None,
        scheduled_digests: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Provides a dynamic Home tab dashboard layout for the App Home view.

        Args:
            workspace: The WorkspaceRecord with plan and usage stats.
            integration: The HubSpot integration status.
            scheduled_digests: Optional list of active ScheduledDigestRecords.

        Returns:
            dict[str, Any]: The Slack Home tab view payload.

        """
        # 1. Resolve Sync Status
        plan_name = "Trial"
        notification_usage = "0 / 20"
        total_syncs = 0

        if workspace:
            plan_name = str(workspace.plan).upper()
            total_syncs = workspace.total_sync_count or 0
            limit = 20 if workspace.plan != "pro" else "∞"
            notification_usage = (
                f"{workspace.notification_count_monthly or 0} / {limit}"
            )

        connection_status = "✅ Connected" if integration else "❌ Not Connected"

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🏠 Welcome to REHA Connect",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Search HubSpot contacts, companies, deals, tickets, and tasks "
                        "directly from Slack. Access CRM data seamlessly without "
                        "switching apps!"
                    ),
                },
            },
            {"type": "divider"},
            # --- SYNC STATUS SECTION ---
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📊 Your CRM Sync Status",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Connection*: {connection_status}"},
                    {"type": "mrkdwn", "text": f"*Current Plan*: `{plan_name}`"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Monthly Notifications*: {notification_usage}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Lifetime Records Shared*: {total_syncs}",
                    },
                ],
            },
            {"type": "divider"},
            # --- SCHEDULED DIGESTS SECTION ---
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📬 Scheduled Digests",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Automate your CRM reporting. Send formatted digests (like Stale Deals or New Leads) directly to your Slack channels on a recurring schedule.",
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Create Digest",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": "open_create_digest_modal",
                },
            },
        ]

        if scheduled_digests:
            options = []
            for digest in scheduled_digests:
                template_display = str(digest.template_id).replace("_", " ").title()
                channel_display = (
                    f"#{digest.target_channel}"
                    if digest.target_channel
                    else "Unknown Channel"
                )

                # Truncate text if needed to comply with Slack's 75 char limit for option text
                option_text = (
                    f"{template_display} ({channel_display}) • {digest.cron_expression}"
                )
                if len(option_text) > 75:
                    option_text = option_text[:72] + "..."

                options.append(
                    {
                        "text": {
                            "type": "plain_text",
                            "text": option_text,
                            "emoji": True,
                        },
                        "value": str(digest.id),
                    }
                )

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Select an active schedule to delete:*",
                    },
                    "accessory": {
                        "type": "static_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Choose a schedule...",
                            "emoji": True,
                        },
                        "options": options,
                        "action_id": "delete_scheduled_digest",
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Delete Schedule?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": "Are you sure you want to permanently delete this scheduled digest?",
                            },
                            "confirm": {"type": "plain_text", "text": "Delete"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                            "style": "danger",
                        },
                    },
                }
            )

        blocks.append({"type": "divider"})
        blocks.extend(
            [
                # --- COMMANDS SECTION ---
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "⚡ Available Commands",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "- `/reha [query]` - *Universal Search*\n"
                            "- `/reha contact <query>` - Specifically search People\n"
                            "- `/reha company <query>` - Specifically search Businesses\n"
                            "- `/reha deal <query>` - Specifically search Deals\n"
                            "- `/reha lead <query>` - Specifically search Leads\n"
                            "- `/reha ticket <query>` - Manage Support Tickets\n"
                            "- `/reha task <query>` - Track & Manage Tasks\n"
                            "- `/reha report` - View workspace sync stats\n"
                            "- `/reha help` - Show all available commands"
                        ),
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "💡 *Quick Tip*: You can quickly create new HubSpot records "
                            "from any message using the `Create HubSpot Record` message shortcut."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🎫 Contact Support",
                            },
                            "style": "primary",
                            "action_id": "open_support_ticket_modal",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "📖 View Documentation",
                            },
                            "url": "https://rehaapps.com/support.html?app=hubspot-slack",
                            "action_id": "view_docs",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "⚙️ Disconnect REHA Connect",
                            },
                            "style": "danger",
                            "action_id": "confirm_disconnect_hubspot",
                        },
                    ],
                },
            ]
        )

        return {"type": "home", "blocks": blocks}
