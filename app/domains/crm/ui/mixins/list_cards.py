from collections.abc import Mapping, Sequence  # noqa: D100
from typing import Any

from app.core.models.ui import CardAction, UnifiedCard
from app.utils.transformers import to_datetime

from .components import ComponentsMixin

MAX_LIST_DISPLAY = 25
MAX_OWNERS_DISPLAY = 100


class ListCardsMixin(ComponentsMixin):
    def build_deals_list(
        self,
        deals: Sequence[Mapping[str, Any]],
        is_pro: bool = False,
        parent_url: str | None = None,
    ) -> UnifiedCard:
        """Build a card showing a list of associated deals."""
        content_parts = []
        display_deals = deals[:5]
        for deal in display_deals:
            props = deal.get("properties", {})
            name = props.get("dealname") or "Unnamed Deal"
            amount = props.get("amount") or "N/A"
            stage = props.get("dealstage") or "unknown"

            # Use link if available
            hs_url = deal.get("hs_url")
            name_text = f"<{hs_url}|{name}>" if hs_url else f"*{name}*"
            content_parts.append(f"{name_text}\nAmount: `{amount}` • Stage: `{stage}`")

        actions = []
        if parent_url and len(deals) > 5:
            actions.append(
                CardAction(
                    label="See More in HubSpot",
                    action_type="url",
                    value="see_more",
                    url=parent_url,
                )
            )

        return UnifiedCard(
            title="Associated Deals",
            emoji="💰",
            badge="FREE VERSION" if not is_pro else "PRO TIER",
            content="\n\n".join(content_parts) if content_parts else "No deals found.",
            actions=actions,
        )

    def build_contacts_list(
        self,
        contacts: Sequence[Mapping[str, Any]],
        is_pro: bool = False,
        parent_url: str | None = None,
    ) -> UnifiedCard:
        """Build a card showing a list of associated contacts."""
        content_parts = []
        display_contacts = contacts[:5]
        for contact in display_contacts:
            props = contact.get("properties", {})
            name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
            email = props.get("email") or "N/A"
            lifecycle = props.get("lifecyclestage") or "—"

            # Use link if available
            hs_url = contact.get("hs_url")
            name_text = (
                f"<{hs_url}|{name or email}>" if hs_url else f"*{name or email}*"
            )
            content_parts.append(
                f"{name_text}\nEmail: `{email}` • Stage: `{lifecycle}`"
            )

        actions = []
        if parent_url and len(contacts) > 5:
            actions.append(
                CardAction(
                    label="See More in HubSpot",
                    action_type="url",
                    value="see_more",
                    url=parent_url,
                )
            )

        return UnifiedCard(
            title="Associated Contacts",
            emoji="👥",
            badge="FREE VERSION" if not is_pro else "PRO TIER",
            content="\n\n".join(content_parts)
            if content_parts
            else "No contacts found.",
            actions=actions,
        )

    def build_companies_list(
        self,
        companies: Sequence[Mapping[str, Any]],
        is_pro: bool = False,
        parent_url: str | None = None,
    ) -> UnifiedCard:
        """Build a card showing a list of associated companies."""
        content_parts = []
        display_companies = companies[:5]
        for company in display_companies:
            props = company.get("properties") or {}
            name = props.get("name") or "Unnamed Company"
            domain = props.get("domain") or "N/A"
            industry = props.get("industry") or "—"

            # Use link if available
            hs_url = company.get("hs_url")
            name_text = f"<{hs_url}|{name}>" if hs_url else f"*🏢 {name}*"
            content_parts.append(
                f"{name_text}\nDomain: `{domain}` • Industry: `{industry}`"
            )

        actions = []
        if parent_url and len(companies) > 5:
            actions.append(
                CardAction(
                    label="See More in HubSpot",
                    action_type="url",
                    value="see_more",
                    url=parent_url,
                )
            )

        return UnifiedCard(
            title="Associated Companies",
            emoji="🏢",
            badge="FREE VERSION" if not is_pro else "PRO TIER",
            content="\n\n".join(content_parts)
            if content_parts
            else "No companies found.",
            actions=actions,
        )

    def build_meetings_list(
        self,
        meetings: Sequence[Mapping[str, Any]],
        is_pro: bool = False,
        parent_url: str | None = None,
    ) -> UnifiedCard:
        """Build a card showing a list of associated meetings."""
        content_parts = []
        display_meetings = meetings[:5]
        for meeting in display_meetings:
            props = meeting.get("properties", {})
            title = props.get("hs_meeting_title") or "Untitled Meeting"

            # Start time
            start_ts = props.get("hs_meeting_start_time")
            start_str = "No time set"
            if start_ts:
                dt = to_datetime(start_ts)
                start_str = dt.strftime("%Y-%m-%d %H:%M")

            outcome = props.get("hs_meeting_outcome", "No outcome")

            # Use link if available
            hs_url = meeting.get("hs_url")
            title_text = f"<{hs_url}|{title}>" if hs_url else f"📅 *{title}*"
            content_parts.append(
                f"{title_text}\nTime: `{start_str}` • Outcome: `{outcome}`"
            )

        actions = []
        if parent_url and len(meetings) > 5:
            actions.append(
                CardAction(
                    label="See More in HubSpot",
                    action_type="url",
                    value="see_more",
                    url=parent_url,
                )
            )

        return UnifiedCard(
            title="Associated Meetings",
            emoji="📅",
            badge="FREE VERSION" if not is_pro else "PRO TIER",
            content="\n\n".join(content_parts)
            if content_parts
            else "No meetings found.",
            actions=actions,
        )

    def build_search_results(
        self,
        results: Sequence[Mapping[str, Any]],
        interaction_type: str = "channel",
        title: str = "Search Results",
        subtitle: str | None = None,
        emoji: str = "🔍",
        content: str = (
            "Multiple results matched your query. Select one to view details:"
        ),
    ) -> UnifiedCard:
        if not results:
            return self.build_empty(
                "No records found! Try creating one with the *+ button* (Shortcuts) "
                "or try a different search term."
            )

        count = len(results)
        actions = []

        # Determine if we have mixed types (Universal Search)
        types_represented = {r.get("type") for r in results if r.get("type")}
        is_mixed = len(types_represented) > 1

        for r in results:
            props = r.get("properties", {})
            obj_type = r.get("type") or "contact"

            # CRM objects use 'properties', CMS objects (like KB) use root attributes
            name = (
                props.get("name")
                or props.get("dealname")
                or props.get("subject")
                or props.get("hs_task_subject")
                or r.get("title")  # For Knowledge Articles
                or "Unknown"
            )

            # Add distinguishing detail so users can tell similar names apart
            detail = (
                props.get("domain")
                or props.get("email")
                or props.get("dealstage")
                or props.get("hs_pipeline_stage")
                or r.get("description")  # For Knowledge Articles
                or ""
            )

            # Map type to emoji for universal search clarity
            type_icons = {
                "contact": "👥",
                "company": "🏢",
                "deal": "💰",
                "ticket": "🎫",
                "task": "✅",
                "lead": "📍",
            }
            icon = type_icons.get(obj_type, "📄")

            label_base = f"{name} ({detail})" if detail else name
            label = f"{icon} {label_base}" if is_mixed else label_base

            # Truncate to 75 chars for Slack button text limit
            if len(label) > 75:  # noqa: PLR2004
                label = label[:72] + "..."

            actions.append(
                CardAction(
                    label=label,
                    action_type="callback",
                    value=(f"view:{obj_type}:{r['id']}:{interaction_type}"),
                )
            )

        return UnifiedCard(
            title=title,
            subtitle=subtitle or f"Found {count} matching records",
            emoji=emoji,
            content=content,
            actions=actions,
        )
