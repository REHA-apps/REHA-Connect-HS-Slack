from __future__ import annotations  # noqa: D100

from typing import Any

from app.core.config import settings
from app.core.security.state_validator import encode_state


class GatingMixin:
    """Mixin for handling Pro-tier gating UI elements and modals."""

    def build_upgrade_nudge_modal(
        self,
        feature_name: str,
        portal_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Builds a Slack modal nudging the user to upgrade to Professional.

        Args:
            feature_name: The name of the feature they tried to access.
            portal_id: Optional HubSpot portal ID for the upgrade link.
            workspace_id: Optional Slack workspace ID for the upgrade link.

        Returns:
            A Slack modal payload.

        """
        feature_display = feature_name.replace("_", " ").title()

        feature_benefits = {
            "note_logging": (
                "• *Unlimited Notes*: Capture every detail and sync instantly.\n"
                "• *AI Summaries*: Automatically summarize long meetings.\n"
                "• *Full Context*: Access history right within Slack."
            ),
            "meeting_scheduler": (
                "• *Instant Booking*: Let clients book time directly from chat.\n"
                "• *Calendar Sync*: Real-time availability checks.\n"
                "• *Reminders*: Automated nudges to reduce no-shows."
            ),
            "task_logging": (
                "• *Unlimited Tasks*: Track every follow-up seamlessly.\n"
                "• *Smart Routing*: Auto-assign to the right owner.\n"
                "• *Due Dates*: Stay on top of your pipeline."
            ),
            "ai_insights": (
                "• *Thread Recap*: Distill 50+ messages into actionable bullets.\n"
                "• *Sentiment Analysis*: Know the mood before you reply.\n"
                "• *Next Best Action*: AI suggests your next move."
            ),
            "ticket_sync": (
                "• *Two-Way Sync*: Updates flow between Slack and HubSpot.\n"
                "• *Team Swarming*: Collaborate on tickets natively.\n"
                "• *Faster Resolution*: Claim and close directly from chat."
            ),
            "pricing_calculator": (
                "• *Instant Quotes*: Generate accurate pricing instantly.\n"
                "• *Custom Discounts*: Handle complex pricing models.\n"
                "• *Share Proposals*: Send quotes directly in Slack."
            ),
            "view_contact_deals": (
                "• *Full Pipeline visibility*: See associated deals instantly.\n"
                "• *Revenue Insights*: Understand contact's monetary impact.\n"
                "• *Deal Velocity*: Track how fast deals are moving."
            ),
        }

        default_benefits = (
            "• *CRM Insights*: Get deep record context and history.\n"
            "• *Advanced Tools*: Access pricing calculators/schedulers.\n"
            "• *Unlimited Activity*: Log unlimited notes and tasks."
        )

        benefits_text = feature_benefits.get(feature_name, default_benefits)

        return {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Upgrade to Pro", "emoji": True},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"✨ *{feature_display}* is a Professional feature.\n\n"
                            "Unlock advanced automation, premium CRM insights, "
                            "and deep CRM integrations by upgrading your workspace."
                        ),
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": benefits_text,
                    },
                },
                {"type": "divider"},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "💎 Upgrade Now",
                                "emoji": True,
                            },
                            "style": "primary",
                            # Use centralized settings with context parameters.
                            # portal_id and workspace_id are base64-encoded to
                            # avoid triggering Cloudflare WAF on raw IDs like hs_148238284.
                            "url": (
                                f"{settings.PRICING_URL}"
                                f"?portal_id={encode_state(portal_id)}"
                                f"&state={encode_state(workspace_id)}"
                                if portal_id and workspace_id
                                else str(settings.PRICING_URL)
                            ),
                            "action_id": "upgrade_link_click",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Contact Sales",
                                "emoji": True,
                            },
                            "value": "sales_inquiry",
                            "action_id": "open_support_ticket_modal",
                        },
                    ],
                },
            ],
        }

    def _apply_gating_to_button(
        self,
        button: dict[str, Any],
        is_pro: bool,
        feature_id: str | None = None,
    ) -> None:
        """Modifies a Slack Block Kit button to visually indicate Pro gating.

        If `is_pro` is True, the button remains unaffected.
        If `is_pro` is False, a lock emoji is prepended to the button text,
        and the `action_id` is prefixed or changed so a gating modal can intercept it.

        Args:
            button: The Slack button block dict.
            is_pro: Whether the workspace has a Pro subscription.
            feature_id: Identifier for the gated capability (e.g., 'object_creation').

        """
        if is_pro:
            return  # No transformation needed for Pro workspaces

        # It's a free workspace, visual lockdown
        original_text = button.get("text", {}).get("text", "Action")
        button["text"]["text"] = f"✨ {original_text}"

        # If it's gated, change the action_id so our service can intercept
        # and show the modal
        if feature_id:
            button["action_id"] = f"gated_feature_click:{feature_id}"
