from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from app.core.logging import get_logger
from app.core.models.ui import CardAction, UnifiedCard
from app.domains.ai.service import (
    AIAppointmentAnalysis,
    AICommunicationAnalysis,
    AICompanyAnalysis,
    AIContactAnalysis,
    AIConversationAnalysis,
    AIDealAnalysis,
    AIEngagementAnalysis,
    AILeadAnalysis,
    AITaskAnalysis,
    AITicketAnalysis,
)
from app.utils.constants import HS_CALL_OUTCOME_CONNECTED
from app.utils.helpers import normalize_object_type

from .components import ComponentsMixin

MAX_LIST_DISPLAY = 25
MAX_OWNERS_DISPLAY = 100

logger = get_logger("crm.ui.object_cards")


class ObjectCardsMixin(ComponentsMixin):
    """Mixin for building standard CRM object cards."""

    def _build_generic_card(
        self,
        *,
        obj: Mapping[str, Any],
        is_pro: bool,
        title: str,
        subtitle: str,
        emoji: str,
        metrics: list[tuple[str, str]],
        content: str,
        status: str | None = None,
        next_action: str | None = None,
        actions: list[CardAction] | None = None,
        include_actions: bool = True,
        sla_expires_at: int | None = None,
        timer_label: str | None = None,
        table_data: list[dict[str, Any]] | None = None,
        table_total_count: int | None = None,
        required_highlights: list[str] | None = None,
        thinking_steps: list[dict[str, str]] | None = None,
        pulse_score: int | None = None,
        baseline_score: int | None = None,
        pipeline_stages: list[dict[str, Any]] | None = None,
    ) -> UnifiedCard:
        """Centralized factory for all UnifiedCard instances."""
        return UnifiedCard(
            title=title,
            subtitle=subtitle,
            emoji=emoji,
            badge="FREE VERSION" if not is_pro else "PRO TIER",
            status=status,
            metrics=metrics,
            content=content,
            secondary_content=(
                [("Next Best Action", next_action)] if next_action else []
            ),
            actions=actions or [] if include_actions else [],
            sla_expires_at=sla_expires_at,
            timer_label=timer_label,
            table_data=table_data,
            table_total_count=table_total_count,
            required_highlights=required_highlights or [],
            thinking_steps=thinking_steps,
            pulse_score=pulse_score,
            baseline_score=baseline_score,
            pipeline_stages=pipeline_stages,
        )

    def _get_standard_actions(
        self,
        obj_type: str,
        obj_id: str,
        hs_url: str | None,
        is_pro: bool,
        extra_actions: list[CardAction] | None = None,
    ) -> list[CardAction]:
        """Provides consistent CRM actions (Note/Task/Meeting) with Pro gating."""
        actions = extra_actions or []

        # Tasks are handled as terminal actions, so we skip the generic CRM utilities
        if obj_type != "task":
            actions.extend(
                [
                    CardAction(
                        label="Schedule Meeting",
                        action_type="modal",
                        value=f"schedule_meeting:{obj_type}:{obj_id}",
                        is_gated=not is_pro,
                    ),
                    CardAction(
                        label="Add Note",
                        action_type="modal",
                        value=f"add_note:{obj_type}:{obj_id}",
                        is_gated=not is_pro,
                    ),
                    CardAction(
                        label="Add Task",
                        action_type="modal",
                        value=f"add_task:{obj_type}:{obj_id}",
                        is_gated=not is_pro,
                    ),
                ]
            )

        actions.append(
            CardAction(
                label="Open in HubSpot",
                action_type="url",
                value="open_hubspot",
                url=hs_url or "https://app.hubspot.com",
            )
        )
        return actions

    def build_contact(
        self,
        obj: Mapping[str, Any],
        analysis: AIContactAnalysis,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard representation for a HubSpot Contact."""
        props = obj["properties"]
        name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
        email = props.get("email", "unknown@example.com")
        job_title = props.get("jobtitle")

        metrics = [
            ("Email", email),
            ("Phone", str(props.get("phone") or "N/A")),
            ("Lifecycle", str(props.get("lifecyclestage") or "N/A")),
            ("Profile Score", str(analysis.score)),
            ("REHA Pulse", str(analysis.pulse_score)),
        ]

        # Extra contact-specific navigation
        extra = [
            CardAction(
                label="View Deals",
                action_type="callback",
                value=f"view_contact_deals:{obj['id']}",
            ),
            CardAction(
                label="View Meetings",
                action_type="callback",
                value=f"view_contact_meetings:{obj['id']}",
            ),
            CardAction(
                label="View Company",
                action_type="callback",
                value=f"view_contact_company:{obj['id']}",
            ),
        ]

        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=name or email,
            subtitle=f"Contact | {job_title}" if job_title else "Contact",
            emoji="👤",
            status=getattr(analysis, "status", None),
            metrics=metrics,
            content=analysis.insight,
            next_action=analysis.next_best_action,
            actions=self._get_standard_actions(
                "contact", obj["id"], obj.get("hs_url"), is_pro, extra
            ),
            include_actions=include_actions,
            pulse_score=getattr(analysis, "pulse_score", None),
            baseline_score=getattr(analysis, "baseline_score", None),
        )

    def build_lead(
        self,
        obj: Mapping[str, Any],
        analysis: AILeadAnalysis | AIContactAnalysis,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard representation for a HubSpot Lead."""
        props = obj["properties"]
        name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
        email = props.get("email", "unknown@example.com")

        metrics = [
            ("Email", email),
            ("Status", str(props.get("hs_lead_status") or "N/A")),
            ("Source", str(props.get("hs_analytics_source") or "N/A")),
            ("Profile Score", str(analysis.score)),
            ("REHA Pulse", str(getattr(analysis, "pulse_score", 50))),
        ]

        extra = [
            CardAction(
                label="Update Budget",
                action_type="modal",
                value=f"update_forecast_amount:{obj['id']}",
                is_gated=not is_pro,
            ),
            CardAction(
                label="Reassign Owner",
                action_type="modal",
                value=f"reassign_owner:contact:{obj['id']}",
                is_gated=not is_pro,
            ),
        ]

        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=f"Lead: {name or email}",
            subtitle="HubSpot Lead",
            emoji="👔",
            status=getattr(analysis, "status", None),
            metrics=metrics,
            content=analysis.insight,
            next_action=analysis.next_best_action,
            actions=self._get_standard_actions(
                "lead", obj["id"], obj.get("hs_url"), is_pro, extra
            ),
            include_actions=include_actions,
        )

    def build_company(
        self,
        obj: Mapping[str, Any],
        analysis: AICompanyAnalysis,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard representation for a HubSpot Company."""
        props = obj["properties"]
        name = props.get("name", "Unnamed Company")
        domain = props.get("domain")

        metrics = [
            ("Domain", f"{domain}" if domain else "N/A"),
            ("Industry", str(props.get("industry") or "N/A")),
            ("Size", str(props.get("numberofemployees") or "N/A")),
            ("Contacts", str(props.get("num_associated_contacts") or "0")),
            ("Deals", str(props.get("num_associated_deals") or "0")),
            ("Page Views", str(props.get("hs_analytics_num_page_views") or "0")),
            ("Sessions", str(props.get("hs_analytics_num_visits") or "0")),
            ("Health", analysis.health),
            ("REHA Pulse", str(analysis.pulse_score)),
        ]

        extra = [
            CardAction(
                label="View Deals",
                action_type="callback",
                value=f"view_company_deals:{obj['id']}",
            ),
            CardAction(
                label="View Contacts",
                action_type="callback",
                value=f"view_contacts:{obj['id']}",
            ),
        ]

        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=name,
            subtitle="Company",
            emoji="🏢",
            status=getattr(analysis, "status", None),
            metrics=metrics,
            content=analysis.insight,
            next_action=analysis.next_best_action,
            actions=self._get_standard_actions(
                "company", obj["id"], obj.get("hs_url"), is_pro, extra
            ),
            include_actions=include_actions,
            pulse_score=getattr(analysis, "pulse_score", None),
            baseline_score=getattr(analysis, "baseline_score", None),
        )

    def build_deal(  # noqa: PLR0912, PLR0915
        self,
        obj: Mapping[str, Any],
        analysis: AIDealAnalysis,
        pipelines: list[dict[str, Any]] | None = None,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard representation for a HubSpot Deal."""
        props = obj["properties"]
        name = props.get("dealname", "Unnamed Deal")
        current_stage_id = props.get("dealstage")
        pipeline_id = props.get("pipeline")

        # Resolve stage name and build options
        stage_label = "Unknown"
        pipeline_label = "Unknown"
        stage_options = []
        pipeline_stages_list = []

        default_stages = [
            ("Appointment Scheduled", "appointmentscheduled"),
            ("Qualified To Buy", "qualifiedtobuy"),
            ("Presentation Scheduled", "presentationscheduled"),
            ("Decision Maker Bought-In", "decisionmakerboughtin"),
            ("Contract Sent", "contractsent"),
            ("Closed Won", "closedwon"),
            ("Closed Lost", "closedlost"),
        ]

        resolved_from_pipeline = False
        if pipelines and pipeline_id:
            pipeline = next((p for p in pipelines if p["id"] == pipeline_id), None)
            if pipeline:
                resolved_from_pipeline = True
                pipeline_label = pipeline.get("label", pipeline_id)
                for stage in pipeline.get("stages", []):
                    label = stage["label"]
                    if len(label) > 72:  # noqa: PLR2004
                        label = label[:72] + "..."
                    stage_id = stage["id"]
                    stage_options.append((label, stage_id))
                    is_current = stage_id == current_stage_id
                    if is_current:
                        stage_label = label
                    pipeline_stages_list.append(
                        {"label": label, "is_current": is_current}
                    )

        if not resolved_from_pipeline:
            # Fallback to standard sales pipeline stages
            pipeline_label = "Sales Pipeline"
            for label, stage_id in default_stages:
                stage_options.append((label, stage_id))
                is_current = False
                if current_stage_id:
                    norm_current = (
                        str(current_stage_id).lower().replace(" ", "").replace("_", "")
                    )
                    if (
                        norm_current == stage_id
                        or norm_current
                        == label.lower().replace(" ", "").replace("_", "")
                    ):
                        is_current = True
                        stage_label = label
                pipeline_stages_list.append({"label": label, "is_current": is_current})

            if stage_label == "Unknown" and current_stage_id:
                stage_label = str(current_stage_id)

        # Map emojis to stages
        stage_emojis = {
            "appointmentscheduled": "📅",
            "qualifiedtobuy": "✅",
            "presentationscheduled": "🖥️",
            "decisionmakerboughtin": "🤝",
            "contractsent": "📝",
            "closedwon": "🟢",
            "closedlost": "🔴",
            "discovery": "🔍",
            "negotiation": "🟡",
        }
        emoji_prefix = stage_emojis.get(stage_label.lower().replace(" ", ""), "🔹")
        display_stage = f"{emoji_prefix} {stage_label}"

        currency = props.get("deal_currency_code")
        currency_symbols = {
            "USD": "$",
            "EUR": "€",
            "GBP": "£",
            "JPY": "¥",
            "AUD": "A$",
            "CAD": "C$",
            "CHF": "CHF ",
            "INR": "₹",
        }
        currency_prefix = (
            currency_symbols.get(currency, f"{currency} ") if currency else "$"
        )

        try:
            val = float(props.get("amount") or 0)
            fmt_amount = (
                f"{currency_prefix}{val:,.2f}"
                if val % 1 != 0
                else f"{currency_prefix}{int(val):,}"
            )
        except (ValueError, TypeError):
            fmt_amount = f"{currency_prefix}{props.get('amount') or '0'}"

        close_date_raw = props.get("closedate")
        close_date_str = "N/A"
        if close_date_raw:
            try:
                dt = datetime.fromisoformat(close_date_raw.replace("Z", "+00:00"))
                close_date_str = dt.strftime("%Y-%m-%d")
            except Exception:
                close_date_str = str(close_date_raw)

        extra = []
        if stage_options:
            extra.append(
                CardAction(
                    label="Update Stage",
                    action_type="select",
                    value=f"update_deal_stage:{obj['id']}",
                    options=stage_options,
                    is_gated=not is_pro,
                )
            )

        extra.extend(
            [
                CardAction(
                    label="Change Close Date",
                    action_type="datepicker",
                    value=f"update_deal_closedate:{obj['id']}",
                    is_gated=not is_pro,
                ),
                CardAction(
                    label="Log Next Step",
                    action_type="modal",
                    value=f"log_next_step:{obj['id']}",
                    is_gated=not is_pro,
                ),
                CardAction(
                    label="Update Deal Type",
                    action_type="modal",
                    value=f"update_deal_type:{obj['id']}",
                    is_gated=not is_pro,
                ),
                CardAction(
                    label="Calculator",
                    action_type="modal",
                    value=f"open_calculator:{obj['id']}",
                    is_gated=not is_pro,
                ),
                CardAction(
                    label="Reassign Owner",
                    action_type="modal",
                    value=f"reassign_owner:deal:{obj['id']}",
                    is_gated=not is_pro,
                ),
            ]
        )

        # 2026.03: Data Table for Line Items (Truncated to top 5)
        table_data = None
        table_total_count = None
        line_items = (
            (obj.get("associations") or {}).get("line_items", {}).get("results", [])
        )  # noqa: E501

        if line_items:
            table_total_count = len(line_items)
            # Sort by amount (desc) or name if amount missing
            sorted_items = sorted(
                line_items,
                key=lambda x: float((x.get("properties") or {}).get("amount") or 0),
                reverse=True,
            )
            top_5 = sorted_items[:5]
            table_data = []
            for item in top_5:
                iprops = item.get("properties") or {}
                table_data.append(
                    {
                        "Name": iprops.get("name") or "Item",
                        "Qty": str(iprops.get("quantity") or "1"),
                        "Amount": f"{currency_prefix}{float(iprops.get('amount') or 0):,.2f}",
                    }
                )

        # 2026.03: Data Hygiene Check (Required Field Highlighting)
        required_highlights = []
        if analysis.score < 100:  # noqa: PLR2004
            # Dynamic highlighting based on HEURISTIC_CRITICAL_FIELDS for deals
            critical_fields = ["amount", "closedate", "dealstage"]
            for field in critical_fields:
                if not props.get(field):
                    required_highlights.append(field)

        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=name,
            subtitle="Deal",
            emoji="💰",
            metrics=[
                ("Pipeline", f"{pipeline_label}"),
                ("Stage", display_stage),
                ("Amount", fmt_amount),
                ("Close Date", close_date_str),
                ("Risk", f"{analysis.risk}"),
                ("Profile Score", str(analysis.score)),
                ("REHA Pulse", str(analysis.pulse_score)),
            ],
            content=analysis.insight,
            next_action=analysis.next_best_action,
            actions=self._get_standard_actions(
                "deal", obj["id"], obj.get("hs_url"), is_pro, extra
            ),
            include_actions=include_actions,
            table_data=table_data,
            table_total_count=table_total_count,
            required_highlights=required_highlights,
            pipeline_stages=pipeline_stages_list,
        )

    def build_ticket(
        self,
        obj: Mapping[str, Any],
        analysis: AITicketAnalysis,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard representation for a HubSpot Ticket."""
        props = obj["properties"]
        subject = props.get("subject") or "Untitled Ticket"
        ticket_id = obj.get("id")
        created = props.get("createdate")
        # Calculate open days
        open_days = "0d"
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                delta = datetime.now(UTC) - created_dt
                open_days = f"{max(0, delta.days)}d"
            except Exception:
                pass

        category = props.get("hs_ticket_category")
        category_label = (
            str(category).title().replace("_", " ") if category else "General Inquiry"
        )

        metrics = [
            (
                "Status",
                analysis.ticket_status.replace("_", " ").title()
                if analysis.ticket_status
                else "Open",
            ),
            ("SLA Status", analysis.sla_label),
            ("Type", category_label),
            ("Open", open_days),
            ("Urgency", analysis.urgency),
            ("REHA Pulse", str(analysis.pulse_score)),
        ]

        # SLA Logic (2026.3 Release)
        sla_expires_at = None
        timer_label = None
        if is_pro and created:
            try:
                # 4 hour SLA window (Standard Support Tier)
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                expiry_ts = int(created_dt.timestamp()) + (4 * 3600)

                # Only show timer for tickets that haven't breached or are in warning
                if expiry_ts > int(datetime.now(UTC).timestamp()):
                    sla_expires_at = expiry_ts
                    timer_label = "SLA Response Window"
            except Exception:
                pass

        extra = [
            CardAction(
                label="Reply to Customer ✉️",
                action_type="modal",
                value=f"ticket_reply:{obj['id']}",
                is_gated=not is_pro,
            ),
            CardAction(
                label="Ticket Summary",
                action_type="modal",
                value=f"record_recap:ticket:{obj['id']}",
                is_gated=not is_pro,
            ),
        ]

        # Resolve standard HubSpot ticket stages
        raw_stage = str(props.get("hs_pipeline_stage", ""))
        stage_lower = raw_stage.lower()
        is_closed = stage_lower == "4" or "closed" in stage_lower

        default_ticket_stages = {
            "1": "New",
            "2": "Waiting on contact",
            "3": "Waiting on us",
            "4": "Closed",
        }
        stage_label = (
            default_ticket_stages.get(raw_stage, raw_stage) if raw_stage else "Unknown"
        )

        # Only show Claim and Close buttons if the ticket is open
        if not is_closed:
            extra.extend(
                [
                    CardAction(
                        label="Claim 🙋",
                        action_type="button",
                        value=f"ticket_claim:{obj['id']}",
                        is_gated=not is_pro,
                    ),
                    CardAction(
                        label="Close 🔒",
                        action_type="button",
                        value=f"ticket_close:{obj['id']}",
                        is_gated=not is_pro,
                    ),
                ]
            )

        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=f"Ticket #{ticket_id}: {subject}",
            subtitle=f"Ticket • Stage: {stage_label}",
            emoji="🎫",
            metrics=metrics,
            content=analysis.insight,
            next_action=analysis.next_best_action,
            actions=self._get_standard_actions(
                "ticket", obj["id"], obj.get("hs_url"), is_pro, extra
            ),
            include_actions=include_actions,
            sla_expires_at=sla_expires_at,
            timer_label=timer_label,
            pulse_score=getattr(analysis, "pulse_score", None),
            baseline_score=getattr(analysis, "baseline_score", None),
        )

    def build_task(
        self,
        obj: Mapping[str, Any],
        analysis: AITaskAnalysis,
        context: dict[str, Any] | None = None,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard representation for a HubSpot Task."""
        props = obj.get("properties") or {}
        subject = props.get("hs_task_subject") or "Untitled Task"
        status = props.get("hs_task_status", "NOT_STARTED")
        priority = props.get("hs_task_priority") or "—"
        task_type = props.get("hs_task_type") or "Task"

        # Format due date
        due_date = "No Due Date"
        ts = props.get("hs_timestamp")
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts) / 1000)
                due_date = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                pass

        owner_name = (
            (context or {}).get("owner_name")
            or props.get("hubspot_owner_id")
            or "Unassigned"
        )

        status_options = [
            ("Not Started", "NOT_STARTED"),
            ("In Progress", "IN_PROGRESS"),
            ("Waiting", "WAITING"),
            ("Completed", "COMPLETED"),
            ("Deferred", "DEFERRED"),
        ]

        priority_options = [("Low", "LOW"), ("Medium", "MEDIUM"), ("High", "HIGH")]

        extra = [
            CardAction(
                label="Complete Task",
                action_type="button",
                value=f"update_task_status:{obj['id']}:COMPLETED",
                is_gated=not is_pro,
            ),
            CardAction(
                label="Update Status",
                action_type="select",
                value=f"update_task_status:{obj['id']}",
                options=status_options,
                selected_option=status,
                is_gated=not is_pro,
            ),
            CardAction(
                label="Update Priority",
                action_type="select",
                value=f"update_task_priority:{obj['id']}",
                options=priority_options,
                selected_option=priority,
                is_gated=not is_pro,
            ),
        ]

        display_type = str(task_type.title() if task_type else "Task")
        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=subject,
            subtitle=f"{display_type} • Status: {status}",
            emoji="✅",
            metrics=[
                ("Due", due_date),
                ("Priority", priority),
                ("Assigned To", owner_name),
                ("Label", analysis.status_label),
            ],
            content=self._strip_html(
                props.get("hs_task_body") or "No details provided."
            ),
            next_action=analysis.next_best_action,
            actions=self._get_standard_actions(
                "task", obj["id"], obj.get("hs_url"), is_pro, extra
            ),
            include_actions=include_actions,
        )

    def build_conversation(
        self,
        obj: Mapping[str, Any],
        analysis: AIConversationAnalysis,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard for a Conversation Thread."""
        t_id = obj.get("id", "Unknown")
        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=f"Conversation #{t_id}",
            subtitle=f"Status: {analysis.status}",
            emoji="💬",
            metrics=[("Status", analysis.status)],
            content=analysis.insight,
            actions=[
                CardAction(
                    label="Reply in Inbox",
                    action_type="url",
                    value="reply",
                    url=f"https://app.hubspot.com/live-messages/{obj.get('portalId')}/inbox/{t_id}",
                )
            ]
            if include_actions
            else [],
            include_actions=include_actions,
        )

    def build_communication(
        self,
        obj: Mapping[str, Any],
        analysis: AICommunicationAnalysis,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard for a HubSpot comm (SMS/WhatsApp/FB Messenger)."""
        props = obj.get("properties") or {}
        channel = analysis.channel
        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=f"{channel} Message",
            subtitle=f"Communication • {channel}",
            emoji="💬",
            metrics=[
                ("Channel", channel),
                ("Direction", str(props.get("hs_communication_logged_from") or "N/A")),
            ],
            content=analysis.insight,
            next_action=analysis.next_best_action,
            actions=self._get_standard_actions(
                "communication", obj.get("id", ""), obj.get("hs_url"), is_pro
            ),
            include_actions=include_actions,
        )

    def build_appointment(
        self,
        obj: Mapping[str, Any],
        analysis: AIAppointmentAnalysis,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard for a HubSpot Appointment."""
        props = obj.get("properties") or {}
        name = props.get("hs_appointment_name") or "Appointment"
        start = props.get("hs_appointment_start_time", "N/A")
        end = props.get("hs_appointment_end_time", "N/A")

        metrics = [
            ("Status", analysis.status_label),
            ("Start", str(start)[:16] if start != "N/A" else "N/A"),
            ("End", str(end)[:16] if end != "N/A" else "N/A"),
        ]

        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=name,
            subtitle=f"Appointment • {analysis.status_label}",
            emoji="📅",
            metrics=metrics,
            content=analysis.insight,
            next_action=analysis.next_best_action,
            actions=self._get_standard_actions(
                "appointment", obj.get("id", ""), obj.get("hs_url"), is_pro
            ),
            include_actions=include_actions,
        )

    def build_engagement(
        self,
        obj: Mapping[str, Any],
        analysis: AIEngagementAnalysis,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        """Builds a UnifiedCard for a HubSpot Engagement (note, call, email, etc)."""
        props = obj.get("properties") or {}
        etype = analysis.engagement_type.title()

        date_str = "No Date"
        ts = props.get("hs_timestamp")
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts) / 1000)
                date_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                pass

        emoji = (
            "📝"
            if etype.lower() == "note"
            else "☎️"
            if etype.lower() == "call"
            else "📧"
            if etype.lower() == "email"
            else "🤝"
        )

        metrics = [
            ("Type", etype),
            ("Date", date_str),
        ]

        if etype.lower() == "call":
            disposition = props.get("hs_call_disposition")
            if disposition:
                # Common HubSpot Call Outcome GUIDs
                outcome_map = {
                    "f240bbac-87c9-4f6e-bf70-924b57d47db7": "Connected",
                    "f240bbac-87c9-4f6e-90ed-7c79c7830593": "Connected",
                    HS_CALL_OUTCOME_CONNECTED: "Connected",  # noqa: F821
                    "a4c4dbcd-ec71-46ab-b92c-6395b09efed0": "No answer",
                    "73a0d17f-1163-4015-bdd5-ec830791da20": "Left voicemail",
                    "b70498a9-4628-444a-a035-77be6c0ea367": "Busy",
                    "626771d2-0cb5-45ec-9742-5fbd4eb81d11": "Wrong number",
                    "f1577901-443b-47e9-a477-8d07f35a4d13": "Left message",
                }
                outcome_label = outcome_map.get(disposition, str(disposition))
                metrics.append(("Outcome", outcome_label))

            duration_ms = props.get("hs_call_duration_milliseconds")
            if duration_ms:
                try:
                    duration_sec = int(duration_ms) // 1000
                    minutes = duration_sec // 60
                    seconds = duration_sec % 60
                    metrics.append(("Duration", f"{minutes}m {seconds}s"))
                except (ValueError, TypeError):
                    pass

        actions = self._get_standard_actions(
            analysis.engagement_type.lower(),
            obj.get("id", ""),
            obj.get("hs_url"),
            is_pro,
        )

        # Filter out generic actions for calls to keep cards clean
        if etype.lower() == "call":
            actions = [
                a
                for a in actions
                if a.label not in ("Schedule Meeting", "Add Task", "Add Note")
            ]

        return self._build_generic_card(
            obj=obj,
            is_pro=is_pro,
            title=f"{etype} Logged",
            subtitle=f"Engagement • {etype}",
            emoji=emoji,
            metrics=metrics,
            content=analysis.insight,
            next_action=analysis.next_best_action,
            actions=actions,
            include_actions=include_actions,
        )

    def _build_from_legacy_heuristics(
        self,
        obj: Mapping[str, Any],
        analysis: Any,
        include_actions: bool = True,
        is_pro: bool = False,
    ) -> UnifiedCard:
        props = obj.get("properties", {})

        if "dealname" in props:
            return self.build_deal(
                obj,
                cast(AIDealAnalysis, analysis),
                include_actions=include_actions,
                is_pro=is_pro,
            )

        if "domain" in props:
            return self.build_company(
                obj,
                cast(AICompanyAnalysis, analysis),
                include_actions=include_actions,
                is_pro=is_pro,
            )

        if "subject" in props:
            return self.build_ticket(
                obj,
                cast(AITicketAnalysis, analysis),
                include_actions=include_actions,
                is_pro=is_pro,
            )

        if "hs_task_subject" in props:
            return self.build_task(
                obj,
                cast(AITaskAnalysis, analysis),
                include_actions=include_actions,
                is_pro=is_pro,
            )

        lifecycle = (props.get("lifecyclestage") or "").lower()
        if lifecycle == "lead":
            return self.build_lead(
                obj,
                cast(AIContactAnalysis, analysis),
                include_actions=include_actions,
                is_pro=is_pro,
            )

        return self.build_contact(
            obj,
            cast(AIContactAnalysis, analysis),
            include_actions=include_actions,
            is_pro=is_pro,
        )

    def build(  # noqa: PLR0911, PLR0912
        self,
        obj: Mapping[str, Any],
        analysis: AIContactAnalysis
        | AICompanyAnalysis
        | AIDealAnalysis
        | AITicketAnalysis
        | AITaskAnalysis
        | AILeadAnalysis
        | AICommunicationAnalysis
        | AIAppointmentAnalysis
        | AIConversationAnalysis
        | AIEngagementAnalysis,
        pipelines: list[dict[str, Any]] | None = None,
        task_context: dict[str, Any] | None = None,
        *,
        is_pro: bool = False,
        include_actions: bool = True,
    ) -> UnifiedCard:
        """Unified entry point for building any CRM object card as a UnifiedCard IR."""
        # 2026.3 Fix: Use explicit obj_type from metadata or fallback to standard field
        raw_type = str(obj.get("type") or obj.get("hs_object_type") or "")
        obj_type = normalize_object_type(raw_type)

        # Type-safe dispatching — is_pro is forwarded to each builder
        # so Pro users get ungated actions and the PRO TIER badge.

        if isinstance(analysis, AIDealAnalysis) and obj_type == "deal":
            return self.build_deal(
                obj,
                analysis,
                pipelines,
                include_actions=include_actions,
                is_pro=is_pro,
            )

        if isinstance(analysis, AITaskAnalysis) and obj_type == "task":
            return self.build_task(
                obj,
                analysis,
                task_context,
                include_actions=include_actions,
                is_pro=is_pro,
            )

        if isinstance(analysis, AIContactAnalysis):
            if obj_type == "contact":
                return self.build_contact(obj, analysis, include_actions, is_pro)
            if obj_type == "lead":
                return self.build_lead(obj, analysis, include_actions, is_pro)

        if isinstance(analysis, AILeadAnalysis) and obj_type == "lead":
            return self.build_lead(obj, analysis, include_actions, is_pro)

        if isinstance(analysis, AICompanyAnalysis) and obj_type == "company":
            return self.build_company(obj, analysis, include_actions, is_pro)

        if isinstance(analysis, AITicketAnalysis) and obj_type == "ticket":
            return self.build_ticket(obj, analysis, include_actions, is_pro)

        if isinstance(analysis, AIConversationAnalysis) and obj_type in (
            "conversation",
            "thread",
        ):
            return self.build_conversation(obj, analysis, include_actions, is_pro)

        if (
            isinstance(analysis, AICommunicationAnalysis)
            and obj_type == "communication"
        ):
            return self.build_communication(obj, analysis, include_actions, is_pro)

        if isinstance(analysis, AIAppointmentAnalysis) and obj_type == "appointment":
            return self.build_appointment(obj, analysis, include_actions, is_pro)

        if isinstance(analysis, AIEngagementAnalysis):
            # Engagement covers many sub-types
            return self.build_engagement(obj, analysis, include_actions, is_pro)

        # Fallback to legacy heuristics if types don't match or for unknown types
        logger.warning(
            "Type mismatch or unknown type in build: obj_type=%s, analysis_type=%s",
            obj_type,
            type(analysis).__name__,
        )
        return self._build_from_legacy_heuristics(
            obj, analysis, include_actions=include_actions, is_pro=is_pro
        )
