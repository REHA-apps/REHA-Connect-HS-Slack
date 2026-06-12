from __future__ import annotations  # noqa: D100

from typing import Any

from slack_sdk.models.blocks import (
    ActionsBlock,
    ButtonElement,
    ConfirmObject,
    DividerBlock,
    MarkdownTextObject,
    Option,
    PlainTextObject,
    SectionBlock,
    StaticSelectElement,
)
from slack_sdk.models.views import View

from app.connectors.slack.ui.mixins.slack_components import SlackComponentsMixin

MAX_LIST_DISPLAY = 25
MAX_OWNERS_DISPLAY = 100


class ModalBuilder(SlackComponentsMixin):
    """Builder for Slack Modals (Views)."""

    def build_type_selection(self, callback_id: str) -> dict[str, Any]:
        """Builds the initial modal to select the object type."""
        view = View(
            type="modal",
            callback_id=callback_id,
            title=PlainTextObject(text="Create HubSpot Record"),
            close=PlainTextObject(text="Cancel"),
            blocks=[
                SectionBlock(
                    text=MarkdownTextObject(
                        text="Select the type of record you want to create:",
                    ),
                ),
                ActionsBlock(
                    elements=[
                        StaticSelectElement(
                            action_id="select_object_type",
                            placeholder=PlainTextObject(text="Choose type..."),
                            options=[
                                Option(
                                    text=PlainTextObject(text="Company"),
                                    value="company",
                                ),
                                Option(
                                    text=PlainTextObject(text="Contact"),
                                    value="contact",
                                ),
                                Option(text=PlainTextObject(text="Deal"), value="deal"),
                                Option(text=PlainTextObject(text="Lead"), value="lead"),
                                Option(text=PlainTextObject(text="Task"), value="task"),
                                Option(
                                    text=PlainTextObject(text="Ticket"), value="ticket"
                                ),
                            ],
                        )
                    ],
                ),
            ],
        )
        return view.to_dict()

    def build_creation_modal(  # noqa: PLR0912, PLR0915
        self,
        object_type: str,
        callback_id: str,
        pipelines: list[dict[str, Any]] | None = None,
        owners: list[dict[str, Any]] | None = None,
        metadata: str | None = None,
    ) -> dict[str, Any]:
        """Builds the creation form modal for the specified object type."""
        blocks = []

        # Title
        title_text = f"Create {object_type.capitalize()}"

        # --- Contact Fields ---
        if object_type == "contact":
            blocks.extend(
                [
                    self._input("Email", "email", placeholder="alice@example.com"),
                    self._input("First Name", "firstname"),
                    self._input("Last Name", "lastname"),
                    self._input("Job Title", "jobtitle", optional=True),
                    self._input("Phone", "phone", optional=True),
                ]
            )

        # --- Deal Fields ---
        elif object_type == "deal":
            blocks.append(self._input("Deal Name", "dealname"))

            # Pipeline/Stage
            if pipelines:
                # Default to first pipeline
                pipeline_options = [(p["label"], p["id"]) for p in pipelines]
                blocks.append(self._select("Pipeline", "pipeline", pipeline_options))

                # Stages for first pipeline (simplified for now, ideally dynamic)
                stages = pipelines[0].get("stages", [])
                stage_options = [(s["label"], s["id"]) for s in stages]
                if stage_options:
                    blocks.append(self._select("Stage", "dealstage", stage_options))
            else:
                blocks.append(self._input("Pipeline ID", "pipeline", optional=True))
                blocks.append(self._input("Stage ID", "dealstage", optional=True))

            blocks.append(
                self._input("Amount", "amount", placeholder="1000.00", optional=True)
            )
            blocks.append(self._datepicker("Close Date", "closedate"))

            if owners:
                owner_options = [(o["email"], o["id"]) for o in owners[:100]]
                blocks.append(
                    self._select(
                        "Deal Owner", "hubspot_owner_id", owner_options, optional=True
                    )
                )

        # --- Lead Fields ---
        elif object_type == "lead":
            blocks.extend(
                [
                    self._input("First Name", "firstname"),
                    self._input("Last Name", "lastname"),
                    self._input(
                        "Email",
                        "email",
                        placeholder="lead@example.com",
                        optional=True,
                    ),
                    self._select(
                        "Lead Source",
                        "hs_analytics_source",
                        [
                            ("Website", "DIRECT_TRAFFIC"),
                            ("LinkedIn", "SOCIAL_MEDIA"),
                            ("Referral", "REFERRALS"),
                            ("Other", "OTHER"),
                        ],
                        optional=True,
                    ),
                    self._select(
                        "Lead Status",
                        "hs_lead_status",
                        [
                            ("New", "NEW"),
                            ("Contacted", "OPEN"),
                            ("Qualified", "IN_PROGRESS"),
                            ("Unqualified", "UNQUALIFIED"),
                        ],
                        optional=True,
                    ),
                    # HubSpot requires a primary contact or company association.
                    # If an existing record is selected here it takes priority;
                    # otherwise the backend will find-or-create a contact from
                    # the email field above.
                    self._association_select_input(
                        label="Link to Contact or Company",
                        placeholder="Search existing contact or company...",
                    ),
                ]
            )

        # --- Company Fields ---
        elif object_type == "company":
            blocks.extend(
                [
                    self._input("Company Name", "name"),
                    self._input("Domain", "domain", placeholder="example.com"),
                    self._input("Industry", "industry", optional=True),
                    self._input(
                        "Company Size",
                        "numberofemployees",
                        placeholder="e.g. 500",
                        optional=True,
                    ),
                    self._input("City", "city", optional=True),
                ]
            )

        # --- Task Fields ---
        elif object_type == "task":
            blocks.append(self._input("Subject", "hs_task_subject"))
            blocks.append(
                self._select(
                    "Type",
                    "hs_task_type",
                    [
                        ("To-Do", "TODO"),
                        ("Call", "CALL"),
                        ("Email", "EMAIL"),
                    ],
                )
            )
            blocks.append(
                self._select(
                    "Priority",
                    "hs_task_priority",
                    [
                        ("🔴 High", "HIGH"),
                        ("🟡 Medium", "MEDIUM"),
                        ("🔵 Low", "LOW"),
                    ],
                    initial_option="MEDIUM",
                )
            )
            blocks.append(self._datepicker("Due Date", "hs_task_due_date"))
            blocks.append(
                self._input(
                    "Description", "hs_task_body", multiline=True, optional=True
                )
            )

            # Killer Feature: Association Dropdown (External Select)
            blocks.append(self._association_select_input())

            if owners:
                owner_options = [(o["email"], o["id"]) for o in owners[:100]]
                blocks.append(
                    self._select(
                        "Assigned To", "hubspot_owner_id", owner_options, optional=True
                    )
                )

        # --- Ticket Fields ---
        elif object_type == "ticket":
            blocks.append(
                self._input(
                    "Ticket Subject",
                    "subject",
                    placeholder="Short summary of the issue",
                )
            )
            blocks.append(
                self._input(
                    "Description",
                    "content",
                    placeholder="Describe your problem in detail...",
                    multiline=True,
                )
            )
            blocks.append(
                self._select(
                    "Category",
                    "hs_ticket_category",
                    [
                        ("Billing", "BILLING_ISSUE"),
                        ("Technical Support", "PRODUCT_ISSUE"),
                        ("Feature Request", "FEATURE_REQUEST"),
                        ("General Inquiry", "GENERAL_INQUIRY"),
                    ],
                )
            )
            blocks.append(
                self._select(
                    "Priority Level",
                    "hs_ticket_priority",
                    [
                        ("🔴 High", "HIGH"),
                        ("🟡 Medium", "MEDIUM"),
                        ("🔵 Low", "LOW"),
                    ],
                    initial_option="MEDIUM",
                )
            )
            blocks.append(DividerBlock())
            if owners:
                owner_options = [(o["email"], o["id"]) for o in owners[:100]]
                blocks.append(
                    self._select(
                        "Assigned To", "hubspot_owner_id", owner_options, optional=True
                    )
                )

            # Association (optional)
            blocks.append(self._association_select_input())

            if pipelines:
                pipeline_options = [(p["label"], p["id"]) for p in pipelines]
                blocks.append(self._select("Pipeline", "hs_pipeline", pipeline_options))

                # Default to first pipeline's stages if available
                stages = pipelines[0].get("stages", [])
                stage_options = [(s["label"], s["id"]) for s in stages]
                if stage_options:
                    blocks.append(
                        self._select(
                            "Ticket Status", "hs_pipeline_stage", stage_options
                        )
                    )

            blocks.append(
                self._select(
                    "Source",
                    "source_type",
                    [
                        ("Chat", "CHAT"),
                        ("Email", "EMAIL"),
                        ("Form", "FORM"),
                    ],
                    initial_option="CHAT",
                )
            )

        view = View(
            type="modal",
            callback_id=f"{callback_id}:{object_type}",
            private_metadata=metadata or "",
            title=PlainTextObject(text=title_text),
            blocks=blocks,
            submit=PlainTextObject(text="Create"),
            close=PlainTextObject(text="Cancel"),
        )
        return view.to_dict()

    def build_ticket_reply_modal(
        self, ticket_id: str, subject: str, private_metadata: str | None = None
    ) -> dict[str, Any]:
        """Builds a modal to send an outbound reply to the customer."""
        blocks = [
            SectionBlock(
                text=MarkdownTextObject(
                    text=f"Replying to ticket *#{ticket_id}: {subject}*\n\nThis message will be sent to the customer via email.",
                )
            ),
            self._input(
                "Your Message",
                "reply_content",
                multiline=True,
                placeholder="Type your response to the customer here...",
            ),
        ]
        view = View(
            type="modal",
            callback_id="submit_ticket_reply",
            private_metadata=private_metadata or ticket_id,
            title=PlainTextObject(text="Reply to Customer"),
            blocks=blocks,
            submit=PlainTextObject(text="Send Reply"),
            close=PlainTextObject(text="Cancel"),
        )
        return view.to_dict()

    def build_ticket_control_panel(
        self, ticket_id: str, subject: str
    ) -> list[dict[str, Any]]:
        """Constructs the Control Panel message for a new ticket channel."""
        blocks = [
            SectionBlock(
                text=MarkdownTextObject(
                    text=(
                        f"🎫 *Ticket Control Panel*"
                        f"\n*ID:* {ticket_id}"
                        f"\n*Subject:* {subject}"
                        "\n\nUse the buttons below"
                        " to manage this ticket."
                    ),
                ),
            ),
            DividerBlock(),
            ActionsBlock(
                block_id=f"ticket_actions:{ticket_id}",
                elements=[
                    ButtonElement(
                        text=PlainTextObject(text="Reply to Customer"),
                        style="primary",
                        action_id="ticket_reply",
                        value=ticket_id,
                    ),
                    ButtonElement(
                        text=PlainTextObject(text="Close 🔒"),
                        action_id="ticket_close",
                        value=ticket_id,
                        confirm=ConfirmObject(
                            title=PlainTextObject(text="Are you sure?"),
                            text=PlainTextObject(
                                text=("This will close the ticket in HubSpot."),
                            ),
                            confirm=PlainTextObject(text="Close Ticket"),
                            deny=PlainTextObject(text="Cancel"),
                        ),
                    ),
                    ButtonElement(
                        text=PlainTextObject(text="Claim 🙋"),
                        action_id="ticket_claim",
                        value=ticket_id,
                    ),
                ],
            ),
        ]
        return [block.to_dict() for block in blocks]
