from __future__ import annotations  # noqa: D100

from typing import Any

from slack_sdk.models.blocks import (
    ActionsBlock,
    ButtonElement,
    ContextBlock,
    DatePickerElement,
    DividerBlock,
    HeaderBlock,
    MarkdownTextObject,
    Option,
    PlainTextObject,
    SectionBlock,
    StaticSelectElement,
)

from app.core.models.ui import CardAction, UnifiedCard


class SlackRenderer:
    """Converts a UnifiedCard IR into Slack Block Kit payload."""

    class _RawBlock:
        def __init__(self, d: dict[str, Any]) -> None:
            self.d = d

        def to_dict(self) -> dict[str, Any]:
            return self.d

    # Coordination maps for gated features
    ACTION_MAP = {
        "log_note": "note_logging",
        "add_task": "task_logging",
        "schedule_meeting": "meeting_scheduler",
        "pricing_calculator": "pricing_calculator",
        "win_loss": "win_loss_post_mortem",
        "send_ai": "ai_insights",
        "update_deal_stage": "deal_stage",
        "update_deal_closedate": "deal_closedate",
        "log_next_step": "deal_next_step",
        "update_task_status": "task_logging",
        "update_task_priority": "task_logging",
        "ticket_reply": "ticket_reply",
        "record_recap": "record_recap",
        "ticket_transcript": "ticket_transcript",
        "ticket_claim": "ticket_claim",
        "ticket_close": "ticket_close",
    }

    FEATURE_MAP = {
        "note_logging": "note_logging",
        "task_logging": "task_logging",
        "meeting_scheduler": "meeting_scheduler",
        "pricing_calculator": "pricing_calculator",
        "win_loss_post_mortem": "win_loss_post_mortem",
        "ai_insights": "ai_insights",
        "deal_stage": "deal_stage",
        "deal_closedate": "deal_closedate",
        "deal_next_step": "deal_next_step",
        "deal_type": "deal_type",
        "reassign_owner": "reassign_owner",
        "ticket_reply": "ticket_reply",
        "record_recap": "record_recap",
        "ticket_transcript": "ticket_transcript",
        "ticket_claim": "ticket_claim",
        "ticket_close": "ticket_close",
    }

    def render(  # noqa: PLR0912, PLR0915
        self,
        card: UnifiedCard,
        is_unfurl: bool = False,
    ) -> dict[str, Any]:
        """Renders a UnifiedCard into Slack Block Kit format."""
        blocks: list[Any] = []

        # Map diagnostic status to Slack color sidebars (Show, Don't Tell)
        color_map = {
            "healthy": "#2EB67D",  # Slack Green
            "warning": "#ECB22E",  # Slack Yellow
            "critical": "#E01E5A",  # Slack Red
        }
        status_color = color_map.get(card.status or "", "#D1D2D3")  # Default Grey

        if is_unfurl:
            # For link previews, we MUST use pure legacy attachments (no 'blocks' key).
            # If you mix 'blocks' inside 'attachments', Slack either squashes the text
            # or truncates it at 5 lines. Pure legacy attachments render perfectly.
            legacy_fields = []
            if card.metrics:
                for k, v in card.metrics:
                    legacy_fields.append(
                        {"title": k or "Field", "value": v or "—", "short": True}
                    )

            text_parts = []
            if card.subtitle:
                text_parts.append(f"_{card.subtitle}_")

            # Subtle visual separator
            text_parts.append("━━━━━━━━━━━━━━━━━━━━━")

            if card.content:
                text_parts.append(card.content)
            for label, sec_text in card.secondary_content:
                text_parts.append(f"\n*{label}*\n{sec_text}")

            attachment = {
                "color": status_color,
                "fallback": f"HubSpot Record: {card.title}",
                "title": f"{card.emoji} {card.title}" if card.emoji else card.title,
                "text": "\n\n".join(text_parts),
                "fields": legacy_fields,
            }
            # Slack automatically appends 'Added by [App Name]',
            # so we skip our custom footer to avoid redundancy.

            return {"attachments": [attachment]}

        # --- BLOCK KIT PATH (For standard messages, not unfurls) ---

        # 1. Title/Header
        if card.title:
            blocks.append(self._header(card.title, card.emoji))

        # 2. Strong Subtitle
        if card.subtitle:
            blocks.append(
                SectionBlock(text=MarkdownTextObject(text=f"*{card.subtitle}*"))
            )

        # 2.3 Visual Pipeline (For Deals)
        if card.pipeline_stages:
            blocks.append(self._pipeline(card.pipeline_stages))

        # Guaranteed visual separator
        blocks.append(DividerBlock())

        # 2.5. Sentiment Pulse (Rolling vs Baseline)
        if card.pulse_score is not None and card.baseline_score is not None:
            blocks.append(self._sentiment_pulse(card.pulse_score, card.baseline_score))
            blocks.append(DividerBlock())

        # 3. Pulse & Metrics
        if card.metrics:
            blocks.append(self._fields(card.metrics))

        # SLA Timer — rendered as a plain mrkdwn section (Slack has no native `timer` block type)
        if card.sla_expires_at:
            status_emoji = (
                "🔴"
                if card.status == "critical"
                else ("🟡" if card.status == "warning" else "🟢")
            )
            label = card.timer_label or "SLA Response Window"
            # Use Slack date formatting: <!date^epoch^{date_short_pretty} at {time}|fallback>
            try:
                epoch = int(card.sla_expires_at)
                from datetime import UTC, datetime

                fallback_dt = datetime.fromtimestamp(epoch, tz=UTC).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                date_str = (
                    f"<!date^{epoch}^{{date_short_pretty}} at {{time}}|{fallback_dt}>"
                )
            except Exception:
                date_str = str(card.sla_expires_at)

            timer_block = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{status_emoji} *{label}:* {date_str}",
                },
            }
            blocks.append(self._RawBlock(timer_block))  # type: ignore

        # Primary Content
        if card.content:
            blocks.append(self._markdown(card.content))

        # Divider before extra details
        if card.secondary_content or card.metrics:
            blocks.append(DividerBlock())

        # Secondary Content
        for label, text in card.secondary_content:
            blocks.append(self._markdown(f"{label}:\n{text}"))

        # Actions
        actions_list = list(card.actions)
        if card.table_total_count and card.table_total_count > 5:
            actions_list.append(
                CardAction(
                    label=f"🔍 View All {card.table_total_count} Items",
                    action_type="callback",
                    value=f"expand_table:{card.title}",
                )
            )

        if actions_list:
            blocks.extend(self._actions(actions_list))

        # Slack 2.0: Table Block (2026.03)
        if card.table_data:
            blocks.append(self._table(card.table_data))

        # Slack 2.0: Thinking Steps (Plan Block)
        if card.thinking_steps:
            blocks.append(self._plan(card.thinking_steps))

        # Footer
        if card.footer:
            blocks.append(self._context(card.footer))

        # Standardize Block Kit payload
        dict_blocks = [b.to_dict() if hasattr(b, "to_dict") else b for b in blocks]

        # Wrap blocks in an attachment to enable the lateral color bar (for standard messages)  # noqa: E501
        # Note: Slack's unfurl API prefers/requires pure blocks over nested ones.
        fallback_text = f"HubSpot Record: {card.title or 'Unknown'}"
        if card.subtitle:
            fallback_text += f" - {card.subtitle}"

        return {
            "blocks": dict_blocks,
            "attachments": [
                {
                    "fallback": fallback_text,
                    "color": status_color,
                    "blocks": dict_blocks,
                }
            ],
        }

    def _header(self, text: str, emoji: str | None = None) -> HeaderBlock:
        prefix = f"{emoji} " if emoji else ""
        safe_text = (text or "HubSpot Record").strip() or "HubSpot Record"
        return HeaderBlock(
            text=PlainTextObject(text=f"{prefix}{safe_text}", emoji=True),
        )

    def _markdown(self, text: str) -> SectionBlock:
        safe_text = (text or "No details provided.").strip() or "No details provided."
        return SectionBlock(text=MarkdownTextObject(text=safe_text))

    def _fields(self, fields: list[tuple[str, str]]) -> SectionBlock:
        field_objects = []
        for label, value in fields:
            safe_label = (label or "Field").strip() or "Field"
            safe_value = (value or "—").strip() or "—"
            field_objects.append(
                MarkdownTextObject(text=f"*{safe_label}:*\n{safe_value}")
            )

        return SectionBlock(fields=field_objects)

    def _context(self, text: str) -> ContextBlock:
        safe_text = (text or "REHA Connection").strip() or "REHA Connection"
        return ContextBlock(elements=[MarkdownTextObject(text=safe_text)])

    def _sentiment_pulse(self, pulse: int, baseline: int) -> SectionBlock:
        """Renders a rolling sentiment pulse vs historical baseline."""
        # Determine status indicators for pulse
        if pulse >= 70:  # noqa: PLR2004
            pulse_icon = "🟢 📈"
            pulse_text = "Strong Momentum"
        elif pulse >= 40:  # noqa: PLR2004
            pulse_icon = "🟡 ➡️"
            pulse_text = "Stable"
        else:
            pulse_icon = "🔴 📉"
            pulse_text = "Declining"

        # Determine status indicator for baseline
        if baseline >= 70:  # noqa: PLR2004
            base_icon = "🟢 📊"
        elif baseline >= 40:  # noqa: PLR2004
            base_icon = "🟡 📊"
        else:
            base_icon = "🔴 📊"

        return SectionBlock(
            fields=[
                MarkdownTextObject(
                    text=f"*Rolling Pulse:*\n{pulse_icon} {pulse}% ({pulse_text})"
                ),
                MarkdownTextObject(
                    text=f"*Historical Baseline:*\n{base_icon} {baseline}%"
                ),
            ]
        )

    def _pipeline(self, stages: list[dict[str, Any]]) -> ContextBlock:
        """Renders an emoji-based rich text breadcrumb of pipeline stages."""
        parts = []
        found_current = False

        for stage in stages:
            label = stage["label"]
            if stage["is_current"]:
                parts.append(f"🎯 *{label}*")
                found_current = True
            elif not found_current:
                parts.append(f"✅ {label}")
            else:
                parts.append(f"⏳ {label}")

        # Join with arrows, but keep it clean
        full_text = "  →  ".join(parts)
        # Truncate if too long for Slack context
        if len(full_text) > 500:  # noqa: PLR2004
            # Try to show current and surrounding
            current_idx = next((i for i, s in enumerate(stages) if s["is_current"]), 0)
            start = max(0, current_idx - 1)
            end = min(len(stages), current_idx + 2)
            subset = parts[start:end]
            full_text = "..." + "  →  ".join(subset) + "..."

        return ContextBlock(elements=[MarkdownTextObject(text=full_text)])

    def _actions(self, actions: list[CardAction]) -> list[ActionsBlock]:  # noqa: PLR0912
        elements: list[Any] = []
        for action in actions:
            if action.action_type == "select" and action.options:
                # Render a static select menu
                placeholder_text = action.label
                if len(placeholder_text) > 75:  # noqa: PLR2004
                    placeholder_text = placeholder_text[:72] + "..."

                options = []
                for opt_label, value in action.options:
                    if len(opt_label) > 75:  # noqa: PLR2004
                        label_text = opt_label[:72] + "..."
                    else:
                        label_text = opt_label
                    options.append(
                        Option(
                            text=PlainTextObject(text=label_text),
                            value=value,
                        )
                    )

                select = StaticSelectElement(
                    placeholder=PlainTextObject(text=placeholder_text),
                    action_id=action.value,  # ensure this is unique!
                    options=options,
                )

                # Set initial option if provided
                if action.selected_option:
                    initial = next(
                        (opt for opt in options if opt.value == action.selected_option),
                        None,
                    )
                    if initial:
                        select.initial_option = initial

                # Gating logic for select menus
                if action.is_gated:
                    select.placeholder = PlainTextObject(
                        text=f"🔒 {placeholder_text}", emoji=True
                    )
                    # For select menus, we can't easily prevent selection,
                    # but we can redirect the action_id so the handler blocks it.
                    action_id_prefix = next(
                        (
                            prefix
                            for prefix in self.ACTION_MAP
                            if action.value.startswith(prefix)
                        ),
                        None,
                    )
                    if action_id_prefix:
                        feature_name = self.FEATURE_MAP.get(
                            self.ACTION_MAP[action_id_prefix],
                            self.ACTION_MAP[action_id_prefix],
                        )
                        select.action_id = (
                            f"gated_feature_click:{feature_name}:{action.value}"
                        )

                elements.append(select)
                continue
            elif action.action_type == "datepicker":
                placeholder_text = action.label
                if action.is_gated:
                    placeholder_text = f"🔒 {placeholder_text}"

                dp = DatePickerElement(
                    action_id=action.value,
                    placeholder=PlainTextObject(text=placeholder_text, emoji=True),
                )
                if action.initial_date:
                    dp.initial_date = action.initial_date

                if action.is_gated:
                    # Gating redirection
                    action_id_prefix = next(
                        (
                            prefix
                            for prefix in self.ACTION_MAP
                            if action.value.startswith(prefix)
                        ),
                        None,
                    )
                    if action_id_prefix:
                        feature_name = self.FEATURE_MAP.get(
                            self.ACTION_MAP[action_id_prefix],
                            self.ACTION_MAP[action_id_prefix],
                        )
                        dp.action_id = (
                            f"gated_feature_click:{feature_name}:{action.value}"
                        )

                elements.append(dp)
                continue

            # Render a button (default)
            button_text = action.label
            if action.is_gated:
                button_text = f"🔒 {button_text}"

            if len(button_text) > 75:  # noqa: PLR2004
                button_text = button_text[:72] + "..."

            button = ButtonElement(
                text=PlainTextObject(text=button_text),
                value=action.value,
                url=action.url if action.action_type == "url" else None,
            )

            # Map UnifiedCard actions to Slack action_ids for dispatch.
            action_map = {
                "view:": "view_object",
                "select:": "select_object",
                "add_note": "open_add_note_modal",
                "view_contact_deals": "view_contact_deals",
                "view_contact_company": "view_contact_company",
                "view_company_deals": "view_company_deals",
                "view_deals": "view_deals",
                "view_contacts": "view_contacts",
                "view_contact_meetings": "view_contact_meetings",
                "schedule_meeting": "open_schedule_meeting_modal",
                "update_lead_source": "open_update_lead_source_modal",
                "update_deal_type": "open_update_deal_type_modal",
                "update_deal_stage": "update_deal_stage",
                "update_deal_closedate": "update_deal_closedate",
                "log_next_step": "log_next_step",
                "update_task_status": "update_task_status",
                "update_task_priority": "update_task_priority",
                "update_forecast_amount": "open_update_forecast_amount_modal",
                "add_task": "open_add_task_modal",
                "record_recap": "open_record_recap_modal",
                "reassign_owner": "reassign_owner",
                "open_hubspot": "open_in_hubspot",
                "post_to_channel": "post_to_channel",
                "ticket_transcript": "ticket_transcript",
                "ticket_reply": "ticket_reply",
                "ticket_claim": "ticket_claim",
                "ticket_close": "ticket_close",
                "open_calculator": "open_calculator",
            }

            action_id_prefix = next(
                (prefix for prefix in action_map if action.value.startswith(prefix)),
                None,
            )

            if action.is_gated and action_id_prefix:
                # Map internal action id to the pro feature name
                feature_map = {
                    "open_add_note_modal": "note_logging",
                    "open_add_task_modal": "task_logging",
                    "open_record_recap_modal": "ai_insights",
                    "open_calculator": "pricing_calculator",
                    "open_schedule_meeting_modal": "meeting_scheduler",
                    "ticket_transcript": "ticket_sync",
                    "ticket_claim": "ticket_sync",
                    "ticket_close": "ticket_sync",
                    "update_task_status": "task_logging",
                    "update_task_priority": "task_logging",
                    "update_deal_stage": "task_logging",
                }
                feature_name = feature_map.get(
                    action_map[action_id_prefix], action_map[action_id_prefix]
                )
                button.action_id = f"gated_feature_click:{feature_name}:{action.value}"
            elif action_id_prefix:
                mapped_prefix = action_map[action_id_prefix]
                # Avoid double-prefixing if the value already has it
                if action.value.startswith(mapped_prefix):
                    button.action_id = action.value
                else:
                    button.action_id = f"{mapped_prefix}:{action.value}"

            elements.append(button)

        # Slack strictly limits ActionsBlock to 5 elements.
        blocks: list[ActionsBlock] = []
        for i in range(0, len(elements), 5):
            blocks.append(ActionsBlock(elements=elements[i : i + 5]))

        return blocks

    def _table(self, rows: list[dict[str, Any]]) -> Any:
        """Renders the 2026.03 Table block using the Header-less array spec.
        The first row functions as the visual header.
        """
        if not rows:
            return DividerBlock()

        # 2026.03 Pattern: First row is the header
        header_keys = list(rows[0].keys())
        table_rows = [[{"type": "raw_text", "text": k} for k in header_keys]]

        for row in rows:
            table_rows.append(
                [{"type": "raw_text", "text": str(v)} for v in row.values()]
            )

        # Structural settings for the 2-column CRM standard
        column_settings = [
            {"is_wrapped": True, "align": "left"},  # Col 0: Property/Variable
            {"align": "right"},  # Col 1: Value/Metric
        ]

        table_block = {
            "type": "table",
            "column_settings": column_settings,
            "rows": table_rows,
        }
        return self._raw_block(table_block)

    def _plan(self, tasks: list[dict[str, str]]) -> Any:
        """Renders the 2026.03 Plan block for thinking transparency.
        Uses the high-fidelity 'task_card' for detailed AI step visibility.
        """
        plan_block = {
            "type": "plan",
            "title": "Analysis Progress",
            "tasks": [
                {
                    "type": "task_card",
                    "label": task["label"],
                    "status": task.get("status", "pending"),
                    "description": task.get("description", ""),
                }
                for task in tasks
            ],
        }
        return self._raw_block(plan_block)

    def _raw_block(self, block_dict: dict[str, Any]) -> Any:
        """Helper to inject raw block dicts for cutting-edge SDK features."""  # noqa: D202

        return self._RawBlock(block_dict)
