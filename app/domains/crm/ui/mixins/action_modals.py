from __future__ import annotations  # noqa: D100

from typing import Any

from app.core.models.ui import CardAction, UnifiedCard
from app.domains.ai.service import (
    AIThreadSummary,
)

from .components import ComponentsMixin

MAX_LIST_DISPLAY = 25
MAX_OWNERS_DISPLAY = 100


class ActionModalsMixin(ComponentsMixin):
    def _input(
        self,
        label: str,
        action_id: str,
        placeholder: str = "",
        initial_value: str = "",
        multiline: bool = False,
        optional: bool = False,
    ) -> dict[str, Any]:
        """Slack-compatible input block generation."""
        element: dict[str, Any] = {
            "type": "plain_text_input",
            "action_id": action_id,
            "multiline": multiline,
        }
        if placeholder:
            element["placeholder"] = {"type": "plain_text", "text": placeholder[:150]}
        if initial_value:
            element["initial_value"] = initial_value

        return {
            "type": "input",
            "block_id": f"block_{action_id}",
            "element": element,
            "label": {"type": "plain_text", "text": label[:150]},
            "optional": optional,
        }

    def _select(
        self,
        label: str,
        action_id: str,
        options: list[tuple[str, str]],
        initial_option: str | None = None,
        optional: bool = False,
    ) -> dict[str, Any]:
        """Slack-compatible static select input block generation."""
        slack_options = []
        initial = None
        for lbl, val in options:
            opt = {
                "text": {"type": "plain_text", "text": lbl[:75]},
                "value": val,
            }
            slack_options.append(opt)
            if initial_option == val:
                initial = opt

        element: dict[str, Any] = {
            "type": "static_select",
            "action_id": action_id,
            "options": slack_options,
            "placeholder": {"type": "plain_text", "text": "Select..."},
        }
        if initial:
            element["initial_option"] = initial

        return {
            "type": "input",
            "block_id": f"block_{action_id}",
            "element": element,
            "label": {"type": "plain_text", "text": label[:150]},
            "optional": optional,
        }

    def build_card_modal(
        self,
        card: UnifiedCard,
        title: str = "Details",
        metadata: str | None = None,
    ) -> dict:
        """Wraps a UnifiedCard into a Slack Modal payload.

        Args:
            card (UnifiedCard): The unified card data structure to render.
            title (str, optional): The title of the modal. Defaults to "Details".
            metadata (str, optional): JSON metadata to persist in the view.

        Returns:
            dict: A Slack modal payload containing the rendered card blocks.

        """
        from app.connectors.slack.slack_renderer import SlackRenderer

        renderer = SlackRenderer()
        payload = renderer.render(card)

        res = {
            "type": "modal",
            "title": {"type": "plain_text", "text": title[:24]},
            "blocks": payload["blocks"],
            "close": {"type": "plain_text", "text": "Close"},
        }
        if metadata:
            res["private_metadata"] = metadata
        return res

    def build_meeting_modal(
        self,
        object_id: str,
        object_type: str = "contact",
        metadata: str | None = None,
    ) -> dict:
        """Builds the Slack Modal for scheduling a meeting in HubSpot."""
        return {
            "type": "modal",
            "callback_id": "schedule_meeting_modal",
            "private_metadata": metadata or f"{object_type}:{object_id}",
            "title": {"type": "plain_text", "text": "Schedule Meeting"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "title_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "title_input",
                        "placeholder": {"type": "plain_text", "text": "Meeting Title"},
                    },
                    "label": {"type": "plain_text", "text": "Title"},
                },
                {
                    "type": "input",
                    "block_id": "date_block",
                    "element": {
                        "type": "datepicker",
                        "action_id": "date_input",
                        "placeholder": {"type": "plain_text", "text": "Select date"},
                    },
                    "label": {"type": "plain_text", "text": "Date"},
                },
                {
                    "type": "input",
                    "block_id": "time_block",
                    "element": {
                        "type": "timepicker",
                        "action_id": "time_input",
                        "placeholder": {"type": "plain_text", "text": "Select time"},
                    },
                    "label": {"type": "plain_text", "text": "Time"},
                },
                {
                    "type": "input",
                    "block_id": "body_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "body_input",
                        "multiline": True,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "What is this meeting about?",
                        },
                    },
                    "label": {"type": "plain_text", "text": "Description"},
                    "optional": True,
                },
            ],
        }

    def build_loading_modal(self, title: str = "Loading...") -> dict:
        """Builds a simple loading modal payload."""
        return {
            "type": "modal",
            "title": {"type": "plain_text", "text": title[:24]},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "⏳ *Fetching data from HubSpot...* Please wait.",
                    },
                }
            ],
        }

    def build_error_modal(self, message: str, title: str = "Error") -> dict:
        """Builds a simple error modal payload."""
        return {
            "type": "modal",
            "title": {"type": "plain_text", "text": title[:24]},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⚠️ {message}",
                    },
                }
            ],
            "close": {"type": "plain_text", "text": "Close"},
        }

    def build_update_deal_type_modal(
        self, deal_id: str, current_value: str = "", metadata: str | None = None
    ) -> dict:
        """Builds the Slack Modal for updating a deal's Deal Type."""
        return {
            "type": "modal",
            "callback_id": "update_deal_type_modal",
            "private_metadata": metadata or deal_id,
            "title": {"type": "plain_text", "text": "Update Deal Type"},
            "submit": {"type": "plain_text", "text": "Update"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                self._select(
                    "Deal Type",
                    "deal_type_input",
                    [
                        ("New Business", "newbusiness"),
                        ("Existing Business", "existingbusiness"),
                    ],
                )
            ],
        }

    def build_note_modal(
        self, object_type: str, object_id: str, metadata: str | None = None
    ) -> dict:
        """Builds the Slack Modal for logging a note to HubSpot."""
        return {
            "type": "modal",
            "callback_id": "add_note_modal",
            "private_metadata": metadata or f"{object_type}:{object_id}",
            "title": {"type": "plain_text", "text": "Log a Note"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "note_input",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "content",
                        "multiline": True,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "What happened with this record?",
                        },
                    },
                    "label": {"type": "plain_text", "text": "Note Content"},
                }
            ],
        }

    def build_add_task_modal(
        self, object_type: str, object_id: str, metadata: str | None = None
    ) -> dict:
        """Builds the Slack Modal for creating a new Task in HubSpot."""
        return {
            "type": "modal",
            "callback_id": "add_task_modal",
            "private_metadata": metadata or f"{object_type}:{object_id}",
            "title": {"type": "plain_text", "text": "Create Task"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "task_subject_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "hs_task_subject",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Task title or subject",
                        },
                    },
                    "label": {"type": "plain_text", "text": "Subject"},
                },
                {
                    "type": "input",
                    "block_id": "task_body_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "hs_task_body",
                        "multiline": True,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Detailed description of the task...",
                        },
                    },
                    "label": {"type": "plain_text", "text": "Notes / Body"},
                    "optional": True,
                },
                self._select(
                    "Task Type",
                    "hs_task_type",
                    [
                        ("To-Do", "TODO"),
                        ("Call", "CALL"),
                        ("Email", "EMAIL"),
                    ],
                ),
                self._select(
                    "Priority",
                    "hs_task_priority",
                    [
                        ("Low", "LOW"),
                        ("Medium", "MEDIUM"),
                        ("High", "HIGH"),
                    ],
                ),
            ],
        }

    def build_post_mortem_modal(
        self, deal_id: str, stage_id: str, metadata: str | None = None
    ) -> dict:
        """Builds the Win/Loss post-mortem modal."""
        is_won = "won" in stage_id.lower()
        title = "Closed Won Details" if is_won else "Closed Lost Details"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Almost there!* Please provide details for this deal "
                        "status change."
                    ),
                },
            }
        ]

        if is_won:
            blocks.append(
                self._input(
                    "Closed Won Reason",
                    "closed_won_reason",
                    placeholder="What was the key factor in winning?",
                )
            )
        else:
            blocks.append(
                self._select(
                    "Closed Lost Reason",
                    "closed_lost_reason",
                    [
                        ("Price", "price"),
                        ("Product Fit", "product_fit"),
                        ("Lost to Competitor", "competitor"),
                        ("Project Shelved", "shelved"),
                    ],
                )
            )

        return {
            "type": "modal",
            "callback_id": "post_mortem_submission",
            "private_metadata": metadata or f"{deal_id}:{stage_id}",
            "title": {"type": "plain_text", "text": title},
            "submit": {"type": "plain_text", "text": "Save & Close"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": blocks,
        }

    def build_pricing_calculator_modal(
        self, deal_id: str, current_amount: float = 0.0, metadata: str | None = None
    ) -> dict:
        """Builds the pricing calculator modal."""
        return {
            "type": "modal",
            "callback_id": "calculator_submission",
            "private_metadata": metadata or deal_id,
            "title": {"type": "plain_text", "text": "Deal Calculator"},
            "submit": {"type": "plain_text", "text": "Calculate & Update"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Current Amount: `${current_amount:,}`",
                    },
                },
                self._input("Quantity", "quantity", placeholder="10"),
                self._input("Unit Price", "unit_price", placeholder="100.00"),
                self._input("Discount %", "discount_percent", placeholder="15"),
            ],
        }

    def build_next_step_enforcement_modal(
        self, deal_id: str, stage_id: str, metadata: str | None = None
    ) -> dict:
        """Forces a Next Step input before stage change."""
        return {
            "type": "modal",
            "callback_id": "next_step_enforcement_submission",
            "private_metadata": metadata or f"{deal_id}:{stage_id}",
            "title": {"type": "plain_text", "text": "Next Step Required"},
            "submit": {"type": "plain_text", "text": "Update Status"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "📈 *Stage Change Enforcement*\nYour manager requires a "
                            "'Next Step' to be set before moving this deal forward."
                        ),
                    },
                },
                self._input(
                    "Next Step",
                    "next_step",
                    placeholder="e.g. Schedule final demo with CTO",
                ),
            ],
        }

    def build_log_next_step_modal(
        self, deal_id: str, metadata: str | None = None
    ) -> dict:
        """Standalone modal to log a Next Step for a deal without requiring a stage change."""
        return {
            "type": "modal",
            "callback_id": "log_next_step_submission",
            "private_metadata": metadata or deal_id,
            "title": {"type": "plain_text", "text": "Log Next Step"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "📋 *What's the next step for this deal?*",
                    },
                },
                self._input(
                    "Next Step",
                    "next_step",
                    placeholder="e.g. Schedule final demo with CTO",
                ),
            ],
        }

    def build_reassign_modal(
        self, object_id: str, owners: list[dict], metadata: str | None = None
    ) -> dict:
        """Builds modal to reassign owner."""
        owner_options = [(o["email"], o["id"]) for o in owners[:100]]
        return {
            "type": "modal",
            "callback_id": "reassign_owner_submission",
            "private_metadata": metadata or object_id,
            "title": {"type": "plain_text", "text": "Reassign Owner"},
            "submit": {"type": "plain_text", "text": "Reassign"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                self._select("Select New Owner", "hubspot_owner_id", owner_options),
            ],
        }

    def build_record_recap_modal(
        self,
        object_type: str,
        object_id: str,
        summary: AIThreadSummary,
        metadata: str | None = None,
    ) -> dict[str, Any]:
        """Builds the Record Recap review modal."""
        # Clean summary for Slack
        recap_text = summary.summary
        key_points_text = "\n".join([f"• {p}" for p in summary.key_points])

        # Dynamic naming based on object type
        label = "Summary" if object_type == "ticket" else "Deep Recap"

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{label} for {object_type.capitalize()} #{object_id}*",
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Summary:*\n{recap_text}"},
            },
        ]

        if key_points_text:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Key Points:*\n{key_points_text}",
                    },
                }
            )

        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Sentiment: *{summary.sentiment}*"}
                ],
            }
        )

        return {
            "type": "modal",
            "callback_id": "record_recap_submission_modal",
            "private_metadata": metadata or f"{object_type}:{object_id}",
            "title": {"type": "plain_text", "text": f"Review {label}"},
            "submit": {"type": "plain_text", "text": "Save to HubSpot"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": blocks,
        }

    def build_disambiguation(self, options: list[dict]) -> UnifiedCard:
        actions = []
        for o in options:
            name = (
                o["properties"].get("name")
                or o["properties"].get("dealname")
                or o["properties"].get("subject")
                or o["properties"].get("hs_task_subject")
                or "Unknown"
            )
            actions.append(
                CardAction(
                    label=f"Select {name}",
                    action_type="callback",
                    value=f"select:{o.get('type')}:{o['id']}",
                )
            )

        return UnifiedCard(
            title="Which one did you mean?",
            emoji="❓",
            actions=actions,
        )

    def build_support_ticket_modal(
        self,
        metadata: str | None = None,
        initial_category: str = "medium",
        initial_ticket_category: str = "GENERAL_INQUIRY",
    ) -> dict:
        """Builds the Slack Modal for raising a support ticket to the developer."""
        return {
            "type": "modal",
            "callback_id": "support_ticket_submission",
            "private_metadata": metadata or "support",
            "title": {"type": "plain_text", "text": "Contact Support"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "👋 *Need help?* Fill out the details below and we'll get back to you via email as soon as possible.",  # noqa: E501
                    },
                },
                self._input(
                    "Subject",
                    "subject",
                    placeholder="Short summary of the issue",
                ),
                self._input(
                    "Description",
                    "content",
                    placeholder="Provide as much detail as possible...",
                    multiline=True,
                ),
                self._select(
                    "Priority",
                    "hs_ticket_priority",
                    [
                        ("Low", "low"),
                        ("Medium", "medium"),
                        ("High", "high"),
                    ],
                    initial_option="medium",
                ),
                self._select(
                    "Category",
                    "hs_ticket_category",
                    [
                        ("Bug / Product Issue", "PRODUCT_ISSUE"),
                        ("Feature Request", "FEATURE_REQUEST"),
                        ("Billing / Sales", "BILLING_ISSUE"),
                        ("General Question", "GENERAL_INQUIRY"),
                    ],
                    initial_option=initial_ticket_category,
                ),
            ],
        }

    def build_confirm_disconnect_modal(
        self,
        portal_id: str,
        is_last_portal: bool = True,  # Kept for backward compatibility but ignored
        metadata: str | None = None,
        is_slack_only: bool = False,
    ) -> dict:
        """Builds the confirmation modal for completely uninstalling REHA Connect.

        Args:
            portal_id: The ID of the HubSpot portal being disconnected.
            is_last_portal: Legacy param; REHA enforces a strict 1-to-1 mapping.
            metadata: Context to persist (usually workspace_id).
            is_slack_only: Whether the app is only installed in Slack and not HubSpot.
        """  # noqa: D413
        title = "Uninstall REHA Connect"

        if is_slack_only:
            body = (
                "🔌 *Disconnect REHA Connect?*\n\n"
                "REHA Connect is currently installed only in Slack and not connected to HubSpot. "
                "Would you like to completely uninstall the app from this Slack workspace?"
            )
        else:
            body = (
                f"🔌 *Disconnect HubSpot Portal {portal_id}?*\n\n"
                "Would you like to completely uninstall REHA Connect from Slack? "
                "(*Note:* This won't delete any existing HubSpot or Slack data; it only removes REHA Connect's connection data)."
            )

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": body},
            }
        ]

        # Use an actions block for the final choice
        actions = [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Uninstall REHA Connect",
                },
                "style": "danger",
                "action_id": "execute_universal_uninstall",
                "value": portal_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Cancel"},
                "action_id": "cancel_uninstall",
            },
        ]

        blocks.append({"type": "actions", "elements": actions})

        return {
            "type": "modal",
            "callback_id": "confirm_disconnect_modal",
            "private_metadata": metadata or portal_id,
            "title": {"type": "plain_text", "text": title[:24]},
            "blocks": blocks,
            "close": {"type": "plain_text", "text": "Close"},
        }
