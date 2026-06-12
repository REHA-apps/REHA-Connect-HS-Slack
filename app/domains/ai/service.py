# ruff: noqa: E501
"""AI analysis and scoring services for REHA Connect CRM intelligence engine."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.core.logging import get_logger
from app.db.storage_service import StorageService
from app.domains.crm.hubspot.sentiment_service import SentimentService
from app.utils.cache import AsyncTTL
from app.utils.helpers import normalize_object_type
from app.utils.html import strip_html
from app.utils.parsers import to_int

logger = get_logger("ai.service")

# ==========================================================
# CONFIG
# ==========================================================

QUALIFIED_STAGES = {"marketingqualifiedlead", "salesqualifiedlead"}
FREE_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "protonmail.com",
}
GATEKEEPER_KEYWORDS = ["assistant", "associate", "coordinator"]

# 2026 SaaS Median: 84 days.
VELOCITY_THRESHOLDS = {
    "smb": (15000, 30),
    "mid_market": (50000, 60),
    "enterprise": (100000, 120),
    "default": 84,
}

# CR-21: Safety stops for local sentiment processing
# Prevents OOM/CPU spikes when processing massive conversation threads.
MAX_INPUT_CHARS = 10000
MAX_BATCH_SIZE = 50

_DEFAULT_TICKET_STAGE_MAP: dict[str, str] = {
    "1": "New",
    "2": "Waiting on contact",
    "3": "Waiting on us",
    "4": "Closed",
    "closed": "Closed",
}


@dataclass(frozen=True)
class ScoringConfig:
    visit_threshold_moderate: int = 5
    visit_threshold_high: int = 10
    visit_threshold_very_high: int = 15

    weight_high_visit: int = 30
    weight_moderate_visit: int = 15
    weight_qualified_lifecycle: int = 25
    weight_has_company: int = 10
    weight_has_email: int = 10

    weight_recency_bonus_high: int = 15
    weight_recency_bonus_medium: int = 8
    weight_recency_bonus_low: int = 3

    weight_velocity_bonus: int = 15
    weight_stage_stale_penalty: int = -15

    # Heuristic 2.0: Compound Variables
    weight_decision_maker_bonus: int = 25
    weight_gatekeeper_penalty: int = -15
    weight_business_domain_bonus: int = 15
    weight_no_decision_maker_penalty: int = -30

    max_score: int = 100

    engagement_recent_bonus: int = 10
    engagement_high_activity_bonus: int = 15
    engagement_stale_penalty: int = -10
    deal_recent_activity_risk_reduction: int = -15

    # SLA Thresholds (Seconds)
    sla_threshold_healthy: int = 3600  # 1 hour
    sla_threshold_warning: int = 14400  # 4 hours

    # Heuristic 2.0: Dynamic Settings
    persona_keywords: list[str] = field(
        default_factory=lambda: [
            "vp",
            "director",
            "head",
            "chief",
            "founder",
            "partner",
            "lead",
            "principal",
        ]
    )
    sla_threshold_hours: int = 4

    # Registry for fields critical to heuristics (e.g. {"deal": ["amount", "closedate"]})
    heuristic_requirements: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: Any) -> "ScoringConfig":
        """Build a ScoringConfig from a ``ScoringConfigRecord``.

        Centralises the DB → dataclass mapping so that adding a new weight field
        only requires updating ``ScoringConfigRecord`` and this method — not every
        call site.
        """
        return cls(
            visit_threshold_moderate=record.visit_threshold_moderate,
            visit_threshold_high=record.visit_threshold_high,
            visit_threshold_very_high=record.visit_threshold_very_high,
            weight_high_visit=record.weight_high_visit,
            weight_moderate_visit=record.weight_moderate_visit,
            weight_qualified_lifecycle=record.weight_qualified_lifecycle,
            weight_has_company=record.weight_has_company,
            weight_has_email=record.weight_has_email,
            weight_recency_bonus_high=record.weight_recency_bonus_high,
            weight_recency_bonus_medium=record.weight_recency_bonus_medium,
            weight_recency_bonus_low=record.weight_recency_bonus_low,
            weight_velocity_bonus=record.weight_velocity_bonus,
            weight_stage_stale_penalty=record.weight_stage_stale_penalty,
            max_score=record.max_score,
            persona_keywords=[
                k.strip().lower()
                for k in record.persona_keywords.split(",")
                if k.strip()
            ],
            sla_threshold_hours=record.sla_threshold_hours,
            # Derive SLA warning threshold from hours
            sla_threshold_warning=record.sla_threshold_hours * 3600,
        )


# ==========================================================
# DATA MODELS (Pydantic for validation & serialization)
# ==========================================================


class AIContactAnalysis(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: Literal["contact"] = "contact"
    insight: str
    score: int = 0
    score_reason: str = ""
    next_best_action: str
    next_action_reason: str = ""
    engagement_factors: str = ""
    status: str = "healthy"
    pulse_score: int = 50
    baseline_score: int = 50


class AICompanyAnalysis(BaseModel):
    type: Literal["company"] = "company"
    insight: str
    health: str
    next_best_action: str
    top_actions: list[str] | None = None
    status: str = "healthy"
    pulse_score: int | None = None
    baseline_score: int | None = None


class AIDealAnalysis(BaseModel):
    type: Literal["deal"] = "deal"
    insight: str
    risk: str
    next_best_action: str
    score: int
    score_reason: str = ""
    top_actions: list[str] | None = None
    status: str = "healthy"
    pulse_score: int = 50
    baseline_score: int = 50


class AITicketAnalysis(BaseModel):
    type: Literal["ticket"] = "ticket"
    insight: str
    urgency: str
    next_best_action: str
    status: str = "healthy"
    pulse_score: int = 50
    baseline_score: int = 50
    sla_label: str = "🟢 Within SLA"  # Human-readable SLA context for HubSpot card
    ticket_status: str | None = None  # Add ticket status for Slack card


class AITaskAnalysis(BaseModel):
    type: Literal["task"] = "task"
    insight: str
    status_label: str
    next_best_action: str


class AIConversationAnalysis(BaseModel):
    type: Literal["conversation"] = "conversation"
    insight: str
    status: str
    next_best_action: str


class AIEngagementAnalysis(BaseModel):
    type: Literal["engagement"] = "engagement"
    insight: str
    engagement_type: str
    next_best_action: str


class AIThreadSummary(BaseModel):
    summary: str
    key_points: list[str]
    sentiment: str


class AILeadAnalysis(BaseModel):
    type: Literal["lead"] = "lead"
    insight: str
    status_label: str
    next_best_action: str
    score: int


class AICommunicationAnalysis(BaseModel):
    type: Literal["communication"] = "communication"
    insight: str
    channel: str
    next_best_action: str


class AIAppointmentAnalysis(BaseModel):
    type: Literal["appointment"] = "appointment"
    insight: str
    status_label: str
    next_best_action: str


# ==========================================================
# SERVICE (STATELESS + MULTI-TENANT SAFE)
# ==========================================================


class AIService:
    # 3.5 Performance: Class-level cache for scoring configs to avoid N+1 lookups
    # across multiple analysis requests for the same workspace.
    _config_cache = AsyncTTL[ScoringConfig](ttl=3600, max_size=100)

    def __init__(
        self,
        corr_id: str | None = None,
        *,
        storage: StorageService | None = None,
        sentiment: SentimentService | None = None,
    ) -> None:
        self.corr_id = corr_id or "system"
        self.storage = storage or StorageService(self.corr_id)
        self.sentiment = sentiment or SentimentService(self.corr_id)
        self._recap_cache = AsyncTTL[AIConversationAnalysis](ttl=3600, max_size=500)
        # 1.4 Architecture fix: initialize here so hasattr is never needed
        # and Pyright can see the full attribute lifecycle.
        self._config_cache = AIService._config_cache

    def _sanitize_for_logging(self, text: str) -> str:
        """Masks PII (emails, common names) before logging to prevent leakage (CR-20)."""
        if not text:
            return ""

        # Mask emails
        sanitized = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", "[EMAIL]", text)

        # Truncate for logs
        if len(sanitized) > 500:  # noqa: PLR2004
            return sanitized[:500] + "... [TRUNCATED]"

        return sanitized

    async def invalidate_recap_cache(self, workspace_id: str, object_id: str) -> None:
        """Invalidates the AI recap cache for a given object."""
        cache_key = f"recap:{workspace_id}:{object_id}"
        await self._recap_cache.invalidate(cache_key)

    # ------------------------------------------------------
    # CONFIG (NEVER STORED ON SELF)
    # ------------------------------------------------------

    async def _get_workspace_config(self, workspace_id: str | None) -> ScoringConfig:
        if not workspace_id:
            return ScoringConfig()

        async def _fetch_config() -> ScoringConfig:
            record = await self.storage.ensure_scoring_config(workspace_id)
            return ScoringConfig.from_record(record)

        return await self._config_cache.get_or_fetch(workspace_id, _fetch_config)

    # ------------------------------------------------------
    # FEATURE EXTRACTION
    # ------------------------------------------------------

    def _extract_features(self, props: Mapping[str, Any]) -> dict[str, Any]:
        props = dict(props)
        props["hs_analytics_num_visits"] = (
            to_int(props.get("hs_analytics_num_visits")) or 0
        )

        return {
            "props": props,
            "visits": props["hs_analytics_num_visits"],
            "lifecycle": (props.get("lifecyclestage") or "").lower(),
            "has_company": bool(props.get("company")),
            "has_email": bool(props.get("email")),
        }

    # ------------------------------------------------------
    # SCORING ENGINE
    # ------------------------------------------------------

    def _recency_bonus(self, props: Mapping[str, Any], cfg: ScoringConfig) -> int:
        last = props.get("lastmodifieddate")
        if not last:
            return 0
        try:
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            days = (datetime.now(UTC) - dt).days
            if days <= 2:  # noqa: PLR2004
                return cfg.weight_recency_bonus_high
            if days <= 7:  # noqa: PLR2004
                return cfg.weight_recency_bonus_medium
            if days <= 30:  # noqa: PLR2004
                return cfg.weight_recency_bonus_low
        except Exception:
            return 0
        return 0

    def _velocity_bonus(self, props: Mapping[str, Any], cfg: ScoringConfig) -> int:
        recent = to_int(props.get("recent_visits_7d")) or 0
        lifetime = to_int(props.get("hs_analytics_num_visits")) or 0
        if lifetime == 0:
            return 0
        if recent >= 3 and (recent / lifetime) >= 0.5:  # noqa: PLR2004
            return cfg.weight_velocity_bonus
        return 0

    def generate_score(
        self,
        props: Mapping[str, Any],
        cfg: ScoringConfig | None = None,
    ) -> int:
        if cfg is None:
            cfg = ScoringConfig()
        f = self._extract_features(props)
        score = 0

        # Title & Persona Logic
        title = (props.get("jobtitle") or "").lower()
        if any(kw in title for kw in cfg.persona_keywords):
            score += cfg.weight_decision_maker_bonus
        elif any(kw in title for kw in GATEKEEPER_KEYWORDS):
            score += cfg.weight_gatekeeper_penalty

        # Domain Strength
        email = (props.get("email") or "").lower()
        if email and "@" in email:
            domain = email.split("@")[-1]
            if domain not in FREE_DOMAINS:
                score += cfg.weight_business_domain_bonus

        if f["visits"] >= cfg.visit_threshold_very_high:
            score += cfg.weight_high_visit
        elif f["visits"] >= cfg.visit_threshold_high:
            score += int(cfg.weight_high_visit * 0.8)
        elif f["visits"] >= cfg.visit_threshold_moderate:
            score += cfg.weight_moderate_visit

        if f["lifecycle"] in QUALIFIED_STAGES:
            score += cfg.weight_qualified_lifecycle

        if f["has_company"]:
            score += cfg.weight_has_company

        if f["has_email"]:
            score += cfg.weight_has_email

        score += self._recency_bonus(f["props"], cfg)
        score += self._velocity_bonus(f["props"], cfg)

        return max(0, min(score, cfg.max_score))

    def _days_since(self, date_str: str | None) -> int:
        """Calculates days since an ISO format date string."""
        if not date_str:
            return 0
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return (datetime.now(UTC) - dt).days
        except Exception:
            return 0

    def _calculate_velocity_health(
        self,
        amount: float,
        days_in_stage: int,
    ) -> tuple[str, str]:
        """Maps deal velocity to health tiers based on segment benchmarks."""
        # 1. Determine Tier
        tier = "default"
        ent_threshold = VELOCITY_THRESHOLDS["enterprise"]
        mid_threshold = VELOCITY_THRESHOLDS["mid_market"]

        if isinstance(ent_threshold, tuple) and amount >= ent_threshold[0]:
            tier = "enterprise"
        elif isinstance(mid_threshold, tuple) and amount >= mid_threshold[0]:
            tier = "mid_market"
        elif amount > 0:
            tier = "smb"

        # 2. Compare against threshold
        tier_val = VELOCITY_THRESHOLDS[tier]
        limit = (
            tier_val[1]
            if isinstance(tier_val, tuple)
            else VELOCITY_THRESHOLDS["default"]
            if not isinstance(VELOCITY_THRESHOLDS["default"], tuple)
            else 84
        )

        if days_in_stage > limit * 1.5:  # noqa: PLR2004
            return (
                "critical",
                f"Stale ({days_in_stage}d in stage vs {limit}d {tier} limit)",
            )
        if days_in_stage > limit:
            return "warning", f"Momentum Fade (Exceeded {limit}d {tier} limit)"

        return "healthy", f"On Track ({days_in_stage}d / {limit}d limit)"

    def _stage_staleness_penalty(
        self,
        props: Mapping[str, Any],
        cfg: ScoringConfig,
    ) -> int:
        entered = props.get("hs_date_entered_stage")
        if not entered:
            return 0
        try:
            dt = datetime.fromisoformat(entered.replace("Z", "+00:00"))
            if (datetime.now(UTC) - dt).days > 30:  # noqa: PLR2004
                return cfg.weight_stage_stale_penalty
        except Exception:
            return 0
        return 0

    def _extract_engagement_datetime(
        self,
        engagement: Mapping[str, Any],
    ) -> datetime | None:
        """Safely extract a datetime from any HubSpot engagement shape.
        Supports:
        - CRM v3 (properties.hs_timestamp)
        - Meetings (hs_meeting_start_time)
        - createdate / hs_createdate
        - Legacy engagements API (engagement.timestamp)
        - Milliseconds and seconds
        """
        ts = None

        # CRM v3 structure
        props = engagement.get("properties") or {}
        ts = (
            props.get("hs_timestamp")
            or props.get("hs_meeting_start_time")
            or props.get("createdate")
            or props.get("hs_createdate")
        )

        # Legacy engagement API
        if not ts and "engagement" in engagement:
            ts = engagement.get("engagement", {}).get("timestamp")

        if not ts:
            return None

        try:
            # Milliseconds or seconds
            if isinstance(ts, int | float):
                # Heuristic: ms are 13 digits
                if ts > 10_000_000_000:  # noqa: PLR2004
                    return datetime.fromtimestamp(ts / 1000, tz=UTC)
                return datetime.fromtimestamp(ts, tz=UTC)

            # ISO string
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

        except Exception:
            return None

    def _engagement_metrics(
        self,
        engagements: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        if not engagements:
            return {
                "count_30d": 0,
                "recent": False,
                "last_activity_days": None,
            }

        now = datetime.now(UTC)
        count_30d = 0
        last_seconds: float | None = None

        for e in engagements:
            dt = self._extract_engagement_datetime(e)

            if not dt:
                continue
            delta_seconds = (now - dt).total_seconds()

            if last_seconds is None or delta_seconds < last_seconds:
                last_seconds = delta_seconds

            if delta_seconds <= 2592000:  # 30 days in seconds
                count_30d += 1

        last_days = int(last_seconds / 86400.0) if last_seconds is not None else None

        return {
            "count_30d": count_30d,
            "recent": last_days is not None and last_days <= 7,  # noqa: PLR2004
            "last_activity_days": last_days,
            "last_activity_seconds": last_seconds,
        }

    def _format_engagements(
        self, engagements: Sequence[Mapping[str, Any]] | None, compact: bool = True
    ) -> str:
        """Formats CRM engagements into a readable Slack block.

        Optimized for extensibility via a formatter map.
        """
        if not engagements:
            return ""

        lines = ["\n *Recent Engagements*:\n"]

        # Sort and group logic
        sorted_engs = sorted(
            [
                (dt, e)
                for e in engagements
                if (dt := self._extract_engagement_datetime(e))
            ],
            key=lambda x: x[0],
            reverse=True,
        )

        by_type: dict[str, list[tuple[datetime, Any]]] = {}
        for dt, e in sorted_engs:
            etype = e.get("_engagement_type", "activity")
            if len(by_type.get(etype, [])) < 2:
                by_type.setdefault(etype, []).append((dt, e))

        selected = sorted(
            [item for items in by_type.values() for item in items],
            key=lambda x: x[0],
            reverse=True,
        )

        # Formatter Map for O(1) dispatch
        def _fmt_note(body: str, dt_str: str, is_compact: bool) -> str:
            body = re.sub(r"<[^>]+>", " ", body).strip()
            # Strip bot-attributed lines from transcripts (e.g. [18:15] REHA Connect Dev: ...)
            body = re.sub(
                r"^\[\d{2}:\d{2}\]\s+REHA Connect.*?:.*$",
                "",
                body,
                flags=re.MULTILINE,
            ).strip()

            limit = 60 if is_compact else 1000

            if not is_compact and "--- TICKET TRANSCRIPT" in body:
                if "---" in body:
                    parts = re.split(r"-{10,}", body, maxsplit=1)
                    if len(parts) > 1:
                        body = parts[1].strip()
                else:
                    body = re.sub(
                        r"--- TICKET TRANSCRIPT.*?---", "", body, flags=re.DOTALL
                    ).strip()

            if len(body) > limit:
                body = f"{body[: limit - 3]}..."

            return (
                f"• 📝 *Note* ({dt_str}): '{body}'"
                if body
                else f"• 📝 *Note* ({dt_str})"
            )

        formatters = {
            "meetings": lambda p, d: (
                f"• 📅 *Meeting* ({d}): '{p.get('hs_meeting_title', 'Meeting')}'"
            ),
            "emails": lambda p, d: (
                f"• ✉️ *Email* ({d}): '{p.get('hs_email_subject', 'Email')}'"
            ),
            "calls": lambda p, d: (
                f"• 📞 *Call* ({d}): '{p.get('hs_call_title', 'Call')}'"
            ),
            "tasks": lambda p, d: (
                f"• ✅ *Task* ({d}): '{p.get('hs_task_subject', 'Task')}'"
            ),
            "notes": lambda p, d: _fmt_note(p.get("hs_note_body", ""), d, compact),
        }

        for dt, e in selected:
            etype = e.get("_engagement_type", "activity")
            props = e.get("properties") or {}
            dt_str = dt.strftime("%b %d, %I:%M %p")

            fmt_func = formatters.get(etype)
            if fmt_func:
                lines.append(fmt_func(props, dt_str))
            else:
                lines.append(f"• 📌 *Activity* ({dt_str})")

        return "\n".join(lines)

    def _format_associated_objects(
        self, associated_objects: dict[str, list[dict[str, Any]]] | None
    ) -> str:
        """Format associated CRM objects as text for Slack messages."""
        if not associated_objects:
            return ""

        lines = ["\n *Associations*:\n"]
        assoc_map = associated_objects or {}

        # Contacts
        contacts = list(assoc_map.get("contacts", []))
        for c in contacts[:5]:
            props = c.get("properties") or {}
            name = (
                f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
                or props.get("email", "Contact")
            )
            lines.append(f"• 👤 {name}")

        # Leads
        leads = list(assoc_map.get("leads", []))
        for lead in leads[:5]:
            props = lead.get("properties") or {}
            name = (
                f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
                or props.get("email", "Lead")
            )
            lines.append(f"• 🎯 {name}")

        # Companies
        companies = list(assoc_map.get("companies", []))
        for c in companies[:5]:
            props = c.get("properties") or {}
            name = props.get("name", "Company")
            lines.append(f"• 🏢 {name}")

        # Deals
        deals = list(assoc_map.get("deals", []))
        for d in deals[:5]:
            props = d.get("properties") or {}
            name = props.get("dealname", "Deal")
            amount = props.get("amount") or ""
            lines.append(f"• 💰 {name} ({amount})")

        # Tickets
        tickets = list(assoc_map.get("tickets", []))
        for t in tickets[:5]:
            props = t.get("properties") or {}
            subject = props.get("subject", "Unknown Ticket")
            priority = props.get("hs_ticket_priority", "Normal")
            lines.append(f"• 🎟️ {subject} (Priority: {priority})")

        return "\n".join(lines)

    # ======================================================
    # POLYMORPHIC ENTRY
    # ======================================================

    async def analyze_polymorphic(  # noqa: PLR0911
        self,
        obj: Mapping[str, Any],
        object_type: str,
        **kwargs: Any,
    ) -> (
        AIContactAnalysis
        | AICompanyAnalysis
        | AIDealAnalysis
        | AITicketAnalysis
        | AITaskAnalysis
        | AILeadAnalysis
        | AICommunicationAnalysis
        | AIAppointmentAnalysis
        | AIConversationAnalysis
        | AIEngagementAnalysis
    ):
        """Dispatches to the correct analyzer based on HubSpot object type or ID."""
        object_type = normalize_object_type(object_type)

        # Engagement collection - respect overrides in kwargs
        if obj is None:
            # Safe fallback for tests or malformed webhooks
            return AIContactAnalysis(
                insight="Object data is currently unavailable.",
                score=0,
                score_reason="Null object",
                next_best_action="Investigate manually",
                next_action_reason="Data missing",
                engagement_factors="Object was None",
            )

        engagements = kwargs.pop("engagements", obj.get("engagements") or [])
        associated_objects = kwargs.pop(
            "associated_objects", obj.get("associated_objects") or {}
        )

        match object_type:
            case "contact":
                return await self.analyze_contact(
                    obj,
                    engagements=engagements,
                    associated_objects=associated_objects,
                    **kwargs,
                )
            case "company":
                return await self.analyze_company(
                    obj,
                    engagements=engagements,
                    associated_objects=associated_objects,
                    **kwargs,
                )
            case "deal":
                return await self.analyze_deal(
                    obj,
                    engagements=engagements,
                    associated_objects=associated_objects,
                    **kwargs,
                )
            case "ticket":
                return await self.analyze_ticket(
                    obj,
                    engagements=engagements,
                    associated_objects=associated_objects,
                    **kwargs,
                )
            case "task":
                return await self.analyze_task(obj, **kwargs)
            case "lead":
                return await self.analyze_lead(obj, **kwargs)
            case "communication":
                return await self.analyze_communication(obj, **kwargs)
            case "appointment":
                return await self.analyze_appointment(obj, **kwargs)
            case "conversation":
                return await self.analyze_conversation(obj, **kwargs)
            case "call" | "meeting" | "email" | "note":
                return await self.analyze_engagement(obj, **kwargs)
            case _:
                logger.warning(
                    "Unsupported object type: %s, falling back to contact analysis",
                    object_type,
                )
                return await self.analyze_contact(obj, **kwargs)

    # ======================================================
    async def _preprocess_content(self, text: str) -> str:
        """Cleans and truncates content for AI analysis (Head+Tail method)."""
        if not text:
            return ""

        # 1. Strip HTML tags
        text = re.sub(r"<[^>]+>", " ", text)

        # 2. Basic signature/disclaimer stripping
        sig_patterns = [r"\n--\n", r"\nRegards,", r"\nSincerely,", r"IMPORTANT NOTICE:"]
        for pattern in sig_patterns:
            parts = re.split(pattern, text, flags=re.IGNORECASE)
            if parts:
                text = parts[0]

        # 3. Head + Tail Truncation (Respects DistilBERT 512 token limit)
        words = text.split()
        if len(words) <= 400:  # Roughly 512 tokens
            return " ".join(words)

        # Take first 300 words (Goal/Problem) and last 100 words (Closing Tone)
        head = " ".join(words[:300])
        tail = " ".join(words[-100:])
        return f"{head} ... {tail}"

    async def _calculate_windowed_sentiment(
        self, engagements: Sequence[Mapping[str, Any]], window: tuple[int, int]
    ) -> tuple[float, int]:
        """Calculates aggregated sentiment for a specific engagement window."""
        start, end = window
        subset = engagements[start:end]

        agg = 0.0
        hits = 0

        # Collect content for batch processing
        contents_to_analyze = []
        weights = []

        for e in subset:
            e_type = e.get("_engagement_type")
            e_props = e.get("properties") or {}

            # Filtering: Inbound interactions only (Customer feedback)
            is_inbound = True
            if e_type == "emails":
                is_inbound = (
                    e_props.get("hs_email_direction") or ""
                ).upper() == "INBOUND"
            elif e_type == "calls":
                is_inbound = (
                    e_props.get("hs_call_direction") or ""
                ).upper() == "INBOUND"

            if not is_inbound:
                continue

            content = (
                e_props.get("hs_email_text")
                or e_props.get("hs_note_body")
                or e_props.get("hs_call_body")
                or e_props.get("hs_meeting_body")
                or e_props.get("hs_body_preview")
                or ""
            )

            content = strip_html(content)

            # For transcript notes, strip the auto-generated header and bot lines
            # first so only genuine customer messages are analysed.
            # This prevents false-positive sentiment from the header text while
            # still correctly detecting real negative signals (e.g. "I want to cancel").
            if e_type == "notes" and "--- TICKET TRANSCRIPT" in content:
                # 1. Strip the transcript block header
                content = re.sub(
                    r"--- TICKET TRANSCRIPT \(ID: [^)]+\) ---[\s\S]*?-------------------------------------------",
                    "",
                    content,
                ).strip()
                # 2. Remove REHA Connect bot lines
                content = re.sub(
                    r"^\[\d{2}:\d{2}\]\s+REHA Connect.*?:.*$",
                    "",
                    content,
                    flags=re.MULTILINE,
                ).strip()
                # 3. Strip [HH:MM] Name: prefixes — keep only the message body
                content = re.sub(
                    r"^\[\d{2}:\d{2}\]\s+[^:\n]+:\s*",
                    "",
                    content,
                    flags=re.MULTILINE,
                ).strip()
                # Require at least 20 chars of real customer content to avoid
                # single-word tokens (e.g. "testnog") skewing the sentiment model.
                if len(content) < 20:  # noqa: PLR2004
                    continue

            if content:
                # Weighting: 1.2x for Transcripts/Emails, 0.8x for Notes
                weight = 1.0
                if e_type in {"calls", "emails"}:
                    weight = 1.2
                elif e_type == "notes":
                    weight = 0.8

                clean_content = await self._preprocess_content(content)
                if clean_content:
                    contents_to_analyze.append(clean_content)
                    weights.append(weight)

        if contents_to_analyze:
            scores = await self.sentiment.analyze_sentiment_batch(contents_to_analyze)
            for score, weight in zip(scores, weights):
                agg += score * weight
                hits += 1

        return agg, hits

    async def _get_pulse_and_baseline_sentiment(
        self, engagements: Sequence[Mapping[str, Any]] | None
    ) -> tuple[int, int]:
        """Calculates a rolling pulse (last 10 interactions) and baseline (interactions 11-50) sentiment score."""
        pulse_score = 50
        baseline_score = 50

        if engagements:
            pulse_agg, pulse_hits = await self._calculate_windowed_sentiment(
                engagements, (0, 10)
            )
            if pulse_hits > 0:
                avg_p = pulse_agg / pulse_hits
                pulse_score = int((avg_p + 1) * 50)

            base_agg, base_hits = await self._calculate_windowed_sentiment(
                engagements, (10, 50)
            )
            if base_hits > 0:
                baseline_score = int(((base_agg / base_hits) + 1) * 50)

        return pulse_score, baseline_score

    # CONTACT
    # ======================================================

    async def analyze_contact(
        self,
        obj: Mapping[str, Any],
        engagements: Sequence[Mapping[str, Any]] | None = None,
        associated_objects: dict[str, list[dict[str, Any]]] | None = None,
        include_associations: bool = False,
        format_engagements: bool = True,
        **kwargs: Any,
    ) -> AIContactAnalysis:
        """Analyse a contact and produce an AIContactAnalysis.

        Args:
            format_engagements: When False, engagements are used for score
                bonuses only and are NOT appended to the insight text.
                Useful for compact UI contexts (e.g. HubSpot sidebar card)
                where engagement details are already surfaced elsewhere.

        """
        props = obj.get("properties") or {}
        workspace_id = obj.get("workspace_id")

        cfg = await self._get_workspace_config(workspace_id)

        # 1. Behavioral Scoring (Heuristic Profile Score)
        score = self.generate_score(props, cfg)

        # 2. Advanced Sentiment Diagnostics (Pulse vs Baseline)
        pulse_score, baseline_score = await self._get_pulse_and_baseline_sentiment(
            engagements
        )

        logger.info(
            "Advanced Sentiment for contact=%s: Pulse=%s, Baseline=%s",
            obj.get("id"),
            pulse_score,
            baseline_score,
        )

        metrics = self._engagement_metrics(engagements)
        last_act = metrics.get("last_activity_days")

        # Time-decay: Revert pulse score towards neutral (50) for stale contacts
        if last_act is not None and last_act > 14:
            # Full decay after 60 days (46 days post-grace period)
            decay_factor = max(0.0, 1.0 - ((last_act - 14) / 46.0))
            pulse_score = 50 + int((pulse_score - 50) * decay_factor)

            # Staleness Penalty: A cold contact's pulse should drop below neutral over time.
            # Decays slower than deals or companies (-1 point every 3 days of silence)
            pulse_score = max(10, pulse_score - int((last_act - 14) / 3))

        # Engagement influence
        if metrics["recent"]:
            score += cfg.engagement_recent_bonus

        if metrics["count_30d"] >= 5:  # noqa: PLR2004
            score += cfg.engagement_high_activity_bonus

        last_act = metrics["last_activity_days"]
        if last_act is not None and last_act > 30:  # noqa: PLR2004
            score += cfg.engagement_stale_penalty

        # Final score factored by Sentiment Pulse
        sentiment_mod = (pulse_score - 50) // 2  # -25 to +25 modifier
        score = max(0, min(score + sentiment_mod, cfg.max_score))

        summary = self._contact_summary(props, metrics)

        # Only append engagement text when the caller wants the full narrative
        engagements_text = (
            self._format_engagements(engagements) if format_engagements else ""
        )
        assoc_text = ""
        if include_associations:
            assoc_text = self._format_associated_objects(associated_objects)

        insight = summary + engagements_text + assoc_text

        # Status Determination (Diagnostic Health)
        status = "healthy"
        if last_act is not None and last_act > 21:  # noqa: PLR2004
            status = "critical"
        elif last_act is not None and last_act > 7:  # noqa: PLR2004
            status = "warning"
        elif score >= 70:  # noqa: PLR2004
            status = "healthy"

        # Readable lifecycle label
        stage_labels = {
            "subscriber": "Subscriber",
            "lead": "Lead",
            "marketingqualifiedlead": "MQL",
            "salesqualifiedlead": "SQL",
            "opportunity": "Opportunity",
            "customer": "Customer",
            "evangelist": "Evangelist",
            "other": "Other",
        }
        lifecycle = (props.get("lifecyclestage") or "").lower()
        stage_label = stage_labels.get(lifecycle, lifecycle.title() or "Unknown")

        return AIContactAnalysis(
            insight=insight,
            score=score,
            score_reason=self._contact_reasoning(props, cfg, score),
            next_best_action=self._next_action(props, metrics, score=score),
            next_action_reason=(
                f"Triggered by: {metrics['count_30d']} engagements "
                f"& {stage_label} status."
            ),
            engagement_factors=self._contact_reasoning(props, cfg, score),
            status=status,
            pulse_score=pulse_score,
            baseline_score=baseline_score,
        )

    def _contact_summary(
        self,
        props: Mapping[str, Any],
        metrics: dict[str, Any] | None = None,
    ) -> str:
        name = (
            f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
            or props.get("email")
            or "Contact"
        )
        company = props.get("company")
        visits = to_int(props.get("hs_analytics_num_visits")) or 0
        lifecycle = (props.get("lifecyclestage") or "").lower()

        # Readable lifecycle label
        stage_labels = {
            "subscriber": "Subscriber",
            "lead": "Lead",
            "marketingqualifiedlead": "MQL",
            "salesqualifiedlead": "SQL",
            "opportunity": "Opportunity",
            "customer": "Customer",
            "evangelist": "Evangelist",
            "other": "Other",
        }
        stage_label = stage_labels.get(lifecycle, lifecycle.title() or "Contact")

        # Dynamic identity construction (avoiding "Unknown" artifacts)
        identity = f"{name} ({stage_label})"
        if company and company.lower() not in {"unknown company", "unknown", "n/a", ""}:
            identity += f" at {company}"

        parts = [identity]
        if visits > 0:
            parts.append(f"{visits} visits")

        if metrics:
            count_30d = metrics.get("count_30d", 0)
            last_days = metrics.get("last_activity_days")
            if count_30d:
                parts.append(f"{count_30d} engagements in 30d")
            if last_days is not None:
                if last_days == 0:
                    parts.append("last active today")
                elif last_days == 1:
                    parts.append("last active yesterday")
                else:
                    parts.append(f"last active {last_days}d ago")

        contact_header = parts.pop(0)
        summary = f"*{contact_header}*\n"
        if parts:
            summary += " • ".join(parts) + "\n"

        return summary

    def _contact_reasoning(
        self,
        props: Mapping[str, Any],
        cfg: ScoringConfig,
        total_score: int = 0,
    ) -> str:
        f = self._extract_features(props)
        parts: list[str] = []

        # Visit contribution
        if f["visits"] >= cfg.visit_threshold_very_high:
            parts.append(f"High website intent ({f['visits']} visits)")
        elif f["visits"] >= cfg.visit_threshold_high:
            parts.append(f"Strong website intent ({f['visits']} visits)")
        elif f["visits"] >= cfg.visit_threshold_moderate:
            parts.append(f"Moderate website intent ({f['visits']} visits)")

        if f["lifecycle"] in QUALIFIED_STAGES:
            parts.append("Qualified lifecycle stage")

        if f["has_company"]:
            parts.append("B2B verified (has company)")
        if f["has_email"]:
            parts.append("Contactable profile (has email)")

        recency = self._recency_bonus(f["props"], cfg)
        if recency:
            parts.append("Active momentum (recent update)")

        velocity = self._velocity_bonus(f["props"], cfg)
        if velocity:
            parts.append("Rising interest (velocity bonus)")

        return (
            f"Profile Score {total_score}: " + ", ".join(parts)
            if parts
            else f"Profile Score {total_score}: baseline"
        )

    def _next_action(  # noqa: PLR0911
        self,
        props: Mapping[str, Any],
        metrics: dict[str, Any] | None = None,
        score: int = 0,
    ) -> str:
        f = self._extract_features(props)
        last_days = metrics.get("last_activity_days") if metrics else None
        count_30d = metrics.get("count_30d", 0) if metrics else 0

        # Heuristic 2.0: Data Hygiene (High intent but missing critical info)
        if score > 70:
            missing_fields = []
            if not props.get("jobtitle"):
                missing_fields.append("Job Title")
            if not props.get("industry"):
                missing_fields.append("Industry")
            if missing_fields:
                return (
                    f"🧹 **Data Gap:** High-intent lead is missing {missing_fields[0]}. "
                    "**Update record to ensure proper routing.**"
                )

        # Heuristic 2.0: Champion At Risk
        if props.get("hs_email_changed") == "true" or props.get(
            "hs_job_title_modified"
        ):
            return "🚨 **Champion At Risk:** Role change detected. **Identify new internal stakeholder immediately.**"

        # Stale contact — re-engage
        if last_days is not None and last_days > 14:  # noqa: PLR2004
            return f"👋 **Momentum Fade:** No activity in {last_days} days. **Re-engage with a value-add email.**"

        # High recent activity — capitalize
        if count_30d >= 5:  # noqa: PLR2004
            return f"📈 **High Velocity:** {count_30d} engagements in 30d. **Propose next steps before momentum fades.**"

        # SQL ready for sales
        if f["lifecycle"] == "salesqualifiedlead":
            return "✅ **SQL Ready:** Prospect reached Sales Qualified status. **Schedule discovery call.**"

        # MQL needs nurturing
        if f["lifecycle"] == "marketingqualifiedlead":
            return "🪴 **MQL Nurture:** Marketing Qualified status. **Nurture with targeted content.**"

        # New lead with high visits
        if f["lifecycle"] == "lead" and f["visits"] >= 5:  # noqa: PLR2004
            return f"⚡ **Hot Lead:** {f['visits']} visits + Lead stage. **Schedule intro call within 48h.**"

        # High intent visitor
        if f["visits"] >= 15:  # noqa: PLR2004
            return f"🔥 **Extreme Intent:** {f['visits']} visits detected. **Prioritize immediate follow-up.**"

        from datetime import UTC, datetime

        created_str = props.get("createdate") or props.get("hs_createdate")
        created_days = None
        if created_str:
            try:
                created_dt = datetime.fromisoformat(
                    str(created_str).replace("Z", "+00:00")
                )
                created_days = (datetime.now(UTC) - created_dt).days
            except Exception:
                pass

        if created_days is not None and created_days < 7:
            return "🌱 **New Contact:** Recently added. **Build out profile and qualify intent.**"

        # Recently active but low engagement
        if last_days is not None and last_days <= 2:  # noqa: PLR2004
            return "⏱️ **Recently Active:** Engaged within last 48h. **Send a timely follow-up.**"

        return "📝 **Standard Follow-up:** No high-intent triggers. **Add follow-up task.**"

    # ======================================================
    # COMPANY
    # ======================================================

    async def analyze_company(  # noqa: PLR0912, PLR0915
        self,
        company: Mapping[str, Any],
        engagements: Sequence[Mapping[str, Any]] | None = None,
        associated_objects: dict[str, list[dict[str, Any]]] | None = None,
        include_associations: bool = False,
        format_engagements: bool = True,
        **kwargs: Any,
    ) -> AICompanyAnalysis:
        props = company.get("properties") or {}
        name = props.get("name", "Company")
        visits = to_int(props.get("hs_analytics_num_visits")) or 0
        industry = props.get("industry") or ""
        employees = to_int(props.get("numberofemployees")) or 0

        # Count associated objects
        # Use HubSpot's native rollup properties to ensure consistency with UI metrics
        display_contacts = to_int(props.get("num_associated_contacts")) or 0
        n_deals = to_int(props.get("num_associated_deals")) or 0

        # Fallback to manual object inspection if native rollups are missing
        if display_contacts == 0:
            n_c = len((associated_objects or {}).get("contacts", []))
            n_l = len((associated_objects or {}).get("leads", []))
            display_contacts = n_c + n_l

        if n_deals == 0:
            n_deals = len((associated_objects or {}).get("deals", []))

        # Multi-factor health
        health_score = 0
        missing = []
        if visits > 10:  # noqa: PLR2004
            health_score += 1
        else:
            missing.append("visits")

        if display_contacts >= 1:
            health_score += 1
        else:
            missing.append("contacts")

        if n_deals >= 1:
            health_score += 1
        else:
            missing.append("deals")

        from datetime import UTC, datetime

        created_str = props.get("createdate") or props.get("hs_createdate")
        created_days = None
        if created_str:
            try:
                created_dt = datetime.fromisoformat(
                    str(created_str).replace("Z", "+00:00")
                )
                created_days = (datetime.now(UTC) - created_dt).days
            except Exception:
                pass

        if created_days is not None and created_days < 7:
            health = "New"
            next_action = "🌱 **New Account:** Recently created. **Build out profile and identify stakeholders.**"
        elif health_score >= 3:  # noqa: PLR2004
            health = "Strong"
            # Heuristic 2.0: Expansion Discovery
            if n_deals == 0:
                next_action = "💎 **Expansion Signal:** Support is stable. **Identify upgrade opportunity.**"
            else:
                next_action = "🚀 **Expansion Opportunity:** High engagement & active deals. **Identify new stakeholders.**"
        elif health_score >= 2:  # noqa: PLR2004
            health = "Healthy"
            next_action = "✅ **Account Stable:** Healthy activity levels. **Schedule quarterly business review.**"
        else:
            # Build dynamic missing text
            if not missing:
                missing_text = "activity"
            elif len(missing) == 3:
                missing_text = "visits, contacts, or deals"
            elif len(missing) == 2:
                missing_text = f"{missing[0]} or {missing[1]}"
            else:
                missing_text = missing[0]

            if health_score == 1:
                health = "Needs Attention"
                next_action = f"⚠️ **Weak Presence:** Low {missing_text}. **Re-engage to build account depth.**"
            else:
                health = "At Risk"
                next_action = f"🧊 **Cold Account:** No {missing_text}. **Investigate churn risk or re-prospect.**"

        top = None
        if associated_objects and "contacts" in associated_objects:
            top = await self.top_recommended_actions(
                associated_objects["contacts"],
                company.get("workspace_id"),
            )

        # Build rich summary
        parts = [name]
        if industry:
            parts[0] += f" ({industry})"
        if employees:
            parts.append(f"{employees} employees")
        parts.append(f"{visits} visits")
        parts.append(f"{display_contacts} contacts")
        parts.append(f"{n_deals} active deals")

        company_header = parts.pop(0)
        summary = f"*{company_header}*\n"
        if parts:
            summary += " • ".join(parts) + "\n"
        if include_associations:
            summary += self._format_associated_objects(associated_objects)
        if format_engagements:
            summary += self._format_engagements(engagements)

        # 2. Advanced Sentiment Diagnostics (Pulse vs Baseline)
        pulse_score = 50
        baseline_score = 50

        if engagements:
            pulse_agg, pulse_hits = await self._calculate_windowed_sentiment(
                engagements, (0, 10)
            )
            if pulse_hits > 0:
                avg_p = pulse_agg / pulse_hits
                pulse_score = int((avg_p + 1) * 50)

            base_agg, base_hits = await self._calculate_windowed_sentiment(
                engagements, (10, 50)
            )
            if base_hits > 0:
                baseline_score = int(((base_agg / base_hits) + 1) * 50)

            comp_metrics = self._engagement_metrics(engagements)
            comp_last_act = comp_metrics.get("last_activity_days")
            if comp_last_act is not None and comp_last_act > 14:
                decay_factor = max(0.0, 1.0 - ((comp_last_act - 14) / 46.0))
                pulse_score = 50 + int((pulse_score - 50) * decay_factor)

                # Staleness Penalty: Silence drops sentiment to 'At Risk'
                pulse_score = max(10, pulse_score - int((comp_last_act - 14) / 2))

        # Status determination for the colored sidebar
        status = "healthy"
        if health == "At Risk":
            status = "critical"
        elif health == "Needs Attention":
            status = "warning"

        return AICompanyAnalysis(
            insight=summary,
            health=health,
            next_best_action=next_action,
            top_actions=top,
            status=status,
            pulse_score=pulse_score,
            baseline_score=baseline_score,
        )

    # ======================================================
    # DEAL
    # ======================================================

    async def analyze_deal(  # noqa: PLR0912, PLR0915
        self,
        deal: Mapping[str, Any],
        engagements: Sequence[Mapping[str, Any]] | None = None,
        associated_objects: dict[str, list[dict[str, Any]]] | None = None,
        include_associations: bool = False,
        owner_name: str | None = None,
        format_engagements: bool = True,
        **kwargs: Any,
    ) -> AIDealAnalysis:
        props = deal.get("properties") or {}
        workspace_id = deal.get("workspace_id")
        cfg = await self._get_workspace_config(workspace_id)

        # Basic engagement metrics
        metrics = self._engagement_metrics(engagements)
        last_act = metrics["last_activity_days"]

        # Risk Score (0-100)
        score = 50 + self._stage_staleness_penalty(props, cfg)
        if metrics["recent"]:
            score += cfg.deal_recent_activity_risk_reduction
        if last_act is not None and last_act > 30:  # noqa: PLR2004
            score += 10

        stage = (props.get("dealstage") or "").lower()
        deal_name = props.get("dealname", "Deal")
        amount = props.get("amount") or ""

        # Closing status
        close_date = props.get("closedate")
        close_days = None
        if close_date:
            try:
                close_dt = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
                close_days = (close_dt - datetime.now(UTC)).days
            except Exception:
                pass

        created_str = props.get("createdate") or props.get("hs_createdate")
        created_days = None
        if created_str:
            try:
                created_dt = datetime.fromisoformat(
                    str(created_str).replace("Z", "+00:00")
                )
                created_days = (datetime.now(UTC) - created_dt).days
            except Exception:
                pass

        # Determine risk and next action
        if stage.startswith("closedlost"):
            risk = "Lost"
            next_action = "🔍 **Post-Mortem:** Deal closed-lost. **Document loss reasons and learnings.**"
        elif stage.startswith("closedwon"):
            risk = "Won"
            next_action = (
                "🎉 **Deal Won:** Contract signed. **Handoff to Onboarding team.**"
            )
        elif last_act is not None and last_act > 14:  # noqa: PLR2004
            risk = "Stalling"
            next_action = f"⏳ **Stale Deal:** No activity in {last_act} days. **Re-engage before deal goes cold.**"
        # Heuristic 2.0: Ghosting Trigger
        elif last_act is not None and last_act > 3:  # noqa: PLR2004
            # Assume last activity was outreach since it is being analyzed as a stale risk
            risk = "Silent"
            next_action = "👻 **Ghosting Alert:** No response to outreach. **Try a new channel or stakeholder.**"
        elif close_days is not None and 0 < close_days <= 7:  # noqa: PLR2004
            risk = "Closing Soon"
            next_action = f"🏁 **Closing Window:** {close_days} days until close. **Confirm commitment and finalize terms.**"
        elif close_days is not None and close_days < 0:
            risk = "Overdue"
            next_action = "⚠️ **Overdue:** Close date has passed. **Update timeline or move to Closed-Lost.**"
        elif created_days is not None and created_days < 7:
            risk = "New"
            next_action = "🌱 **New Deal:** Recently opened. **Establish timeline and confirm next steps.**"
        else:
            risk = "Open"
            next_action = "📅 **Pipeline Maintenance:** Deal is healthy. **Ensure next meeting is scheduled.**"

        # Rich summary: Deal Name ($Amount) — Owned by [Name]
        main_parts = [deal_name]
        if amount:
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
            main_parts[0] += f" ({currency_prefix}{amount})"
        if owner_name:
            main_parts.append(f"Owned by {owner_name}")

        extra_parts = []
        if close_days is not None and close_days > 0:
            extra_parts.append(f"closing in {close_days}d")
        elif close_days is not None and close_days < 0:
            extra_parts.append(f"overdue by {abs(close_days)}d")
        if last_act is not None:
            extra_parts.append(f"last activity {last_act}d ago")

        deal_header = main_parts.pop(0)
        summary = f"*{deal_header}*\n"
        if main_parts or extra_parts:
            pieces = main_parts + extra_parts
            summary += " • ".join(pieces) + "\n"

        if include_associations:
            summary += self._format_associated_objects(associated_objects)
        if format_engagements:
            summary += self._format_engagements(engagements)

        # Heuristic 2.0: Velocity & Stakeholder Check
        days_in_stage = self._days_since(props.get("hs_date_entered_stage"))
        amount_val = to_int(props.get("amount")) or 0
        v_status, v_reason = self._calculate_velocity_health(amount_val, days_in_stage)

        # Stakeholder Alignment (Is there a decision maker?)
        has_dm = False
        contacts = (associated_objects or {}).get("contacts", [])
        for c in contacts:
            c_props = c.get("properties") or {}
            title = (c_props.get("jobtitle") or "").lower()
            if any(kw in title for kw in cfg.persona_keywords):
                has_dm = True
                break

        if not has_dm and v_status == "healthy":
            v_status = "warning"
            v_reason += " (No Decision Maker associated)"

        # 2. Advanced Sentiment Diagnostics (Pulse vs Baseline)
        pulse_score, baseline_score = await self._get_pulse_and_baseline_sentiment(
            engagements
        )

        if last_act is not None and last_act > 14:
            decay_factor = max(0.0, 1.0 - ((last_act - 14) / 46.0))
            pulse_score = 50 + int((pulse_score - 50) * decay_factor)

            # Staleness Penalty: Silence on active deals drops sentiment directly
            pulse_score = max(10, pulse_score - int(last_act - 14))

        return AIDealAnalysis(
            insight=summary,
            risk=risk,
            next_best_action=next_action,
            score=max(0, min(score, 100)),
            score_reason=(
                f"Triggered by: {v_reason}. "
                f"Activity: {'recent' if metrics['recent'] else 'stale'}."
            ),
            top_actions=None,
            status=v_status,
            pulse_score=pulse_score,
            baseline_score=baseline_score,
        )

    # ======================================================
    # TICKET
    # ======================================================

    async def analyze_ticket(  # noqa: PLR0912, PLR0915
        self,
        ticket: Mapping[str, Any],
        engagements: Sequence[Mapping[str, Any]] | None = None,
        associated_objects: dict[str, list[dict[str, Any]]] | None = None,
        include_associations: bool = False,
        slack_messages: Sequence[Mapping[str, Any]] | None = None,
        compact: bool = True,
        format_engagements: bool = True,
        **kwargs: Any,
    ) -> AITicketAnalysis:
        props = ticket.get("properties") or {}
        workspace_id = ticket.get("workspace_id")
        cfg = await self._get_workspace_config(workspace_id)

        # 1. Identity & Context
        subject = props.get("subject", "Ticket")
        priority = (props.get("hs_ticket_priority") or "NORMAL").upper()
        category = (props.get("hs_ticket_category") or "GENERAL_INQUIRY").upper()

        # Human-readable mapping
        {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}.get(priority, "Normal")
        category.replace("_", " ").title()

        # 2. Sentiment & SLA Diagnostics
        created_at = props.get("createdate")
        age_days = (
            (
                datetime.now(UTC)
                - datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            ).days
            if created_at
            else 0
        )

        pulse_score = 50
        is_agitated = False
        idle_seconds = None
        if engagements:
            pulse_agg, pulse_hits = await self._calculate_windowed_sentiment(
                engagements, (0, 10)
            )
            if pulse_hits > 0:
                avg_p = pulse_agg / pulse_hits
                pulse_score = int((avg_p + 1) * 50)
                is_agitated = avg_p < -0.4  # noqa: PLR2004

            ticket_metrics = self._engagement_metrics(engagements)

            # SLA-based Pulse Penalty: Stale tickets don't just go neutral; they tank customer sentiment.
            # Use last activity seconds (or fallback to age since creation if no activity)
            idle_seconds = ticket_metrics.get("last_activity_seconds")
            if idle_seconds is None:
                idle_seconds = age_days * 86400

            if idle_seconds > cfg.sla_threshold_warning:
                # Critical Breach: Pulse tanks quickly (e.g., -10 points per hour over SLA)
                hours_over = (idle_seconds - cfg.sla_threshold_warning) / 3600.0
                penalty = int(hours_over * 10)
                pulse_score = max(10, pulse_score - penalty)
            elif idle_seconds > cfg.sla_threshold_healthy:
                # Minor Breach/Warning: Flat penalty
                pulse_score = max(20, pulse_score - 15)

        # 3. Urgency & Action Logic (Consolidated with Heuristic Extraction)
        next_action: str = "📁 **Backlog:** Low priority. Review in next cycle."
        if engagements:
            all_eng_text = " ".join(self._clean_engagement_data(engagements))
            heuristic_action = await self._extract_action_items(all_eng_text)
            if heuristic_action:
                next_action = heuristic_action

        if priority == "HIGH" or (is_agitated and format_engagements):
            urgency = "Critical"
            if not (
                engagements
                and await self._extract_action_items(
                    " ".join(self._clean_engagement_data(engagements))
                )
            ):
                next_action = (
                    "💣 **At-Risk Customer:** Negative sentiment detected. **Escalate immediately.**"
                    if is_agitated
                    else f"🔴 **SLA BREACH:** Open {age_days}d. **Escalate.**"
                    if age_days > 3
                    else "⚡ **Urgent:** High priority. **Respond within 4h.**"
                )
        elif priority == "MEDIUM":
            urgency = "Moderate"
            if not (
                engagements
                and await self._extract_action_items(
                    " ".join(self._clean_engagement_data(engagements))
                )
            ):
                next_action = (
                    f"🟡 **Stale Ticket:** Open {age_days}d. **Follow up.**"
                    if age_days > 7
                    else "🕒 **Standard Triage:** Respond within 24h."
                )
        else:
            urgency = "Low"
            next_action = (
                "🌱 **New Ticket:** Recently opened. **Triage and assign to an agent.**"
                if age_days < 7
                else "📁 **Backlog:** Low priority. Review in next cycle."
            )

        # 4. Content Assembly
        # Suppressed header text (subject, priority, open days) as it is now shown in the card metrics
        summary = ""
        # HubSpot Smart Recap — only for Slack cards, not the HubSpot sidebar
        if engagements and format_engagements:
            cleaned_eng = self._clean_engagement_data(engagements)
            if cleaned_eng:
                hubspot_recap = self._get_smart_summary(cleaned_eng)
                summary += f"\n*HubSpot Recap:*\n{hubspot_recap}\n"

        if include_associations:
            summary += self._format_associated_objects(associated_objects)

        # Still show individual engagements if not in compact mode
        if not compact and format_engagements:
            summary += self._format_engagements(engagements, compact=compact)

        if slack_messages:
            conv_recap = await self.analyze_conversation(
                {"messages": slack_messages}, **kwargs
            )
            summary += f"\n\n--- SLACK CONVERSATION ---\n{conv_recap.insight}"

        if not summary:
            summary = f"*{subject}*"

        # 5. SLA Health Status (2026.03 Compliance)
        # SLA measures time since LAST activity (idle time), not just creation time
        if engagements and idle_seconds is not None:
            sla_seconds = int(idle_seconds)
        else:
            sla_seconds = age_days * 86400
        status = "healthy"
        if sla_seconds > cfg.sla_threshold_warning:
            status = "critical"
        elif sla_seconds > cfg.sla_threshold_healthy:
            status = "warning"

        # Compute human-readable SLA label
        sla_hours_open = sla_seconds / 3600
        time_suffix = "idle" if engagements else "open"

        if sla_seconds > cfg.sla_threshold_warning:
            hours_over = (sla_seconds - cfg.sla_threshold_warning) / 3600
            if hours_over >= 24:  # noqa: PLR2004
                sla_label = f"🔴 SLA Breached ({int(hours_over / 24)}d {int(hours_over % 24)}h over)"
            else:
                sla_label = f"🔴 SLA Breached ({int(hours_over)}h over)"
        elif sla_seconds > cfg.sla_threshold_healthy:
            sla_label = f"🟡 SLA Warning ({int(sla_hours_open)}h {time_suffix})"
        else:
            sla_label = f"🟢 Within SLA ({int(sla_hours_open)}h {time_suffix})"

        # Map standard HubSpot numeric pipeline stages
        raw_stage = str(props.get("hs_pipeline_stage", "Open")).lower()

        ticket_status = _DEFAULT_TICKET_STAGE_MAP.get(
            raw_stage, props.get("hs_pipeline_stage", "Open")
        )

        return AITicketAnalysis(
            insight=summary,
            urgency=urgency,
            next_best_action=next_action,
            pulse_score=pulse_score,
            status=status,
            sla_label=sla_label,
            ticket_status=ticket_status,
        )

    # ======================================================
    # TASK
    # ======================================================

    async def analyze_task(
        self,
        task: Mapping[str, Any],
        format_engagements: bool = True,
        **kwargs: Any,
    ) -> AITaskAnalysis:
        props = task.get("properties") or {}
        status = (props.get("hs_task_status") or "").upper()

        if status == "COMPLETED":
            label = "Done"
            next_action = "✅ **Task Done:** Log outcome and **save final summary.**"
        elif status == "IN_PROGRESS":
            label = "In Progress"
            next_action = (
                "⏳ **In Progress:** Task is active. **Ensure completion by deadline.**"
            )
        else:
            label = "Pending"
            next_action = "📥 **New Task:** Task is unstarted. **Start task to maintain workflow.**"

        return AITaskAnalysis(
            insight=f"*{props.get('hs_task_subject', 'Task')}*\nStatus: {status}",
            status_label=label,
            next_best_action=next_action,
        )

    # ======================================================
    # LEAD
    # ======================================================

    async def analyze_lead(
        self,
        lead: Mapping[str, Any],
        **kwargs: Any,
    ) -> AILeadAnalysis:
        props = lead.get("properties") or {}
        workspace_id = lead.get("workspace_id")
        cfg = await self._get_workspace_config(workspace_id)

        status = (props.get("hs_lead_status") or "NEW").upper()
        name = (
            f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
            or props.get("email", "Unknown Lead")
        )

        if status in {"CONNECTED", "OPEN"}:
            label = "Active"
            next_action = "⏱️ **Recently Active:** Engaged within last 48h. **Follow up within 24 hours.**"
        elif status in {"IN_PROGRESS"}:
            label = "In Progress"
            next_action = "🪴 **MQL Nurture:** Continue nurturing — **check last touchpoint for context.**"
        elif status in {"UNQUALIFIED"}:
            label = "Unqualified"
            next_action = "📁 **Archive:** Unqualified lead. **Archive or reassign to marketing.**"
        else:
            label = "New"
            next_action = "📥 **New Lead:** Record assigned. **Make first contact to establish connection.**"

        score = self.generate_score(props, cfg)
        lead_score = props.get("hubspotscore")

        summary = f"*{name}*\nStatus: {status}"
        if lead_score:
            summary += f" • Score: {lead_score}"

        return AILeadAnalysis(
            insight=summary,
            status_label=label,
            next_best_action=next_action,
            score=score,
        )

    # ======================================================
    # COMMUNICATION (SMS / WhatsApp / FB Messenger)
    # ======================================================

    async def analyze_communication(
        self,
        comm: Mapping[str, Any],
        **kwargs: Any,
    ) -> AICommunicationAnalysis:
        props = comm.get("properties") or {}
        channel = props.get("hs_communication_channel_type") or "Email"
        subject = props.get("hs_communication_subject") or "Communication"

        return AICommunicationAnalysis(
            insight=f"{channel} — {subject}",
            channel=channel,
            next_best_action="Reply via same channel if pending.",
        )

    # ======================================================
    # APPOINTMENT
    # ======================================================

    async def analyze_appointment(
        self,
        appt: Mapping[str, Any],
        **kwargs: Any,
    ) -> AIAppointmentAnalysis:
        props = appt.get("properties") or {}
        name = props.get("hs_appointment_name") or "Appointment"
        status = (props.get("hs_appointment_status") or "SCHEDULED").upper()
        start = props.get("hs_appointment_start_time") or ""

        if status == "COMPLETED":
            label = "Completed"
            next_action = (
                "✅ **Meeting Done:** Log outcome and **send follow-up summary.**"
            )
        elif status == "CANCELLED":
            label = "Cancelled"
            next_action = "📅 **Reschedule:** Meeting was cancelled. **Reschedule if still relevant.**"
        elif status == "NO_SHOW":
            label = "No Show"
            next_action = (
                "🚫 **No Show:** Prospect missed meeting. **Reschedule immediately.**"
            )
        else:
            label = "Scheduled"
            next_action = (
                "🔔 **Upcoming:** Meeting tomorrow. **Send reminder 24h before.**"
            )

        summary = f"{name} — {label}"
        if start:
            summary += f" (starts: {start[:10]})"

        return AIAppointmentAnalysis(
            insight=summary,
            status_label=label,
            next_best_action=next_action,
        )

    # ======================================================
    # CONVERSATION
    # ======================================================

    async def analyze_conversation(
        self,
        conv: Mapping[str, Any],
        **kwargs: Any,
    ) -> AIConversationAnalysis:
        messages = conv.get("messages", [])
        workspace_id = str(kwargs.get("workspace_id") or "default")
        obj_id = str(kwargs.get("object_id") or "unknown")
        cache_key = f"recap:{workspace_id}:{obj_id}"

        # 0. Check cache
        cached = await self._recap_cache.get(cache_key)
        if cached:
            return cached

        # Step 1: Clean and Structure HubSpot Data
        cleaned_msgs = self._clean_engagement_data(messages)
        if not cleaned_msgs:
            return AIConversationAnalysis(
                insight=f"Recap for {obj_id}: No recent messages found.",
                status=conv.get("status", "OPEN"),
                next_best_action="💬 **Active Thread:** Conversation is open.",
            )

        full_text = " ".join(cleaned_msgs)

        # CR-21: Truncate massive threads to prevent OOM during local processing
        if len(full_text) > MAX_INPUT_CHARS:
            logger.warning(
                "Truncating massive conversation thread (len=%d) for stability (CR-21)",
                len(full_text),
            )
            full_text = full_text[:MAX_INPUT_CHARS]

        # Step 2: Language Detection (Manage Expectations)
        lang_note = ""
        try:
            from langdetect import detect

            lang = detect(full_text[:500])
            if lang != "en":
                lang_note = f"\n_Note: Summary provided in [{lang.upper()}] - Sentiment analysis may be less accurate._"
        except Exception:
            pass

        # Step 3: Context-Aware Summarization (LexRank with Narrative Connectors)
        summary_str = self._get_smart_summary(cleaned_msgs)

        # Step 4: Compound Intent Detection (Sentiment + Keywords)
        sentiment_score = 50  # Default neutral
        try:
            sentiment_batch = await self.sentiment.analyze_sentiment_batch([full_text])
            if sentiment_batch:
                sentiment_score = int(sentiment_batch[0] * 100)
        except Exception:
            pass

        next_action = await self._extract_action_items(
            full_text, sentiment_score=sentiment_score
        )

        result = AIConversationAnalysis(
            insight=f"Recap for {obj_id}: {summary_str}{lang_note}",
            status=conv.get("status", "OPEN"),
            next_best_action=next_action or "💬 **Review:** Check thread for details.",
        )

        # Store in cache before returning
        await self._recap_cache.set(cache_key, result)
        return result

    def _clean_engagement_data(
        self, engagements: Sequence[Mapping[str, Any]]
    ) -> list[str]:
        """Sanitizes HubSpot data by mapping properties and stripping noise."""
        cleaned_texts = []

        # HubSpot returns newest first, we reverse for 'Original Issue' identification
        sorted_eng = sorted(
            engagements, key=lambda x: float(x.get("ts") or x.get("hs_timestamp") or 0)
        )

        for eng in sorted_eng:
            # Skip messages from the bot itself to prevent self-summarization
            if eng.get("bot_id") or "REHA Connect" in (eng.get("username") or ""):
                continue

            e_type = str(eng.get("type") or eng.get("_engagement_type") or "").lower()
            props = eng.get("properties") or {}
            raw_text = ""

            if e_type == "notes":
                raw_text = props.get("hs_note_body", "")
            elif e_type == "emails":
                raw_text = props.get("hs_email_text") or props.get("hs_email_html", "")
            elif e_type == "calls":
                raw_text = props.get("hs_call_body", "")
            elif e_type in {"communication", "communications"}:
                raw_text = props.get("hs_communication_body", "")
            else:
                # Fallback for generic interactions
                raw_text = props.get("hs_body_preview") or eng.get("text") or ""

            if not raw_text:
                continue

            # Strip HTML tags but preserve newlines for regex line matching
            clean_text = strip_html(raw_text)
            # Strip ticket transcript header
            clean_text = re.sub(
                r"--- TICKET TRANSCRIPT \(ID: [^)]+\) ---[\s\S]*?-------------------------------------------",
                "",
                clean_text,
            ).strip()
            # Strip bot-attributed lines from transcripts to avoid self-summarization
            clean_text = re.sub(
                r"^\[\d{2}:\d{2}\]\s+REHA Connect.*?:.*$",
                "",
                clean_text,
                flags=re.MULTILINE,
            ).strip()

            # --- Continuous log splitting ---
            if "---" in clean_text and "Added on " in clean_text:
                parts = re.split(
                    r"---\s*Added on \d{4}-\d{2}-\d{2}[^:]+:\s*", clean_text
                )
                for part in parts:
                    part = part.strip()
                    if len(part) > 15:  # noqa: PLR2004
                        cleaned_texts.append(part)
                continue

            # --- Transcript & Continuous Log line splitting ---
            # If this is a transcript-style note or our new continuous log format
            # (contains [time] Name: text lines)
            # split into individual per-message entries so the summarizer treats each
            # message as its own sentence rather than one giant run-on string.
            transcript_line_pattern = re.compile(
                r"^\[(.*?)\]\s+([^:]+):\s*(.+)$", re.MULTILINE
            )
            transcript_lines = transcript_line_pattern.findall(clean_text)
            if transcript_lines:
                for ts_time, speaker, msg_body in transcript_lines:
                    msg_body = msg_body.strip()
                    if not msg_body or len(msg_body) < 3:  # noqa: PLR2004
                        continue
                    # Format as readable attributed sentence
                    entry = f"[{ts_time}] {speaker}: {msg_body}"
                    # Ensure it ends with sentence-terminating punctuation
                    if entry[-1] not in {".", "!", "?"}:
                        entry += "."
                    cleaned_texts.append(entry)
                continue  # Skip the blob — we've already added the lines

            # Skip short system noise
            if clean_text and len(clean_text) > 15:  # noqa: PLR2004
                cleaned_texts.append(clean_text)

        return cleaned_texts

    def _get_smart_summary(self, texts: list[str]) -> str:
        """Generates an extractive summary using LexRank with a Lead-3 bias and Narrative Connectors."""
        try:
            import os

            import nltk
            from sumy.nlp.tokenizers import Tokenizer
            from sumy.parsers.plaintext import PlaintextParser
            from sumy.summarizers.lex_rank import LexRankSummarizer

            # Use the persistent directory initialized in SentimentService
            nltk_data_path = os.path.join(os.getcwd(), "nltk_data")
            if nltk_data_path not in nltk.data.path:
                nltk.data.path.append(nltk_data_path)

            full_conversation = " ".join(texts)

            # CR-20: Prevent PII leakage in debug logs
            logger.debug(
                "Generating smart summary for payload: %s",
                self._sanitize_for_logging(full_conversation),
            )

            parser = PlaintextParser.from_string(
                full_conversation, Tokenizer("english")
            )
            summarizer = LexRankSummarizer()

            # Extract top 2 central sentences
            summary_sentences = summarizer(parser.document, 2)

            # Lead-3 Bias formatting (using Narrative Connectors)
            # Apply 'Clean-Sentence' heuristic to messy extractive text
            first_msg = self._format_sentence(texts[0])
            if len(first_msg) > 150:  # noqa: PLR2004
                first_msg = first_msg[:147] + "..."

            summary = f"📍 *Where it started:*\n> {first_msg}\n\n"

            if summary_sentences:
                # Deduplicate: don't repeat the first message in the ranked summary
                unique_sentences = []
                for s in summary_sentences:
                    clean_s = self._format_sentence(str(s))
                    if clean_s != first_msg and clean_s not in unique_sentences:
                        unique_sentences.append(clean_s)

                if unique_sentences:
                    summary += "🔍 *The Core Issue (Ranked):*\n"
                    for i, clean_s in enumerate(unique_sentences, 1):
                        summary += f"{i}. {clean_s}\n"
                return summary

            return first_msg

        except Exception as e:
            logger.warning("LexRank summarization failed: %s", e)
            # Fallback to last 2 messages
            return " — ".join(texts[-2:])

    async def _extract_action_items(  # noqa: PLR0912
        self, text: str, sentiment_score: int = 50
    ) -> str | None:
        """Heuristic-based extraction using Compound Intents (Sentiment + Keywords)."""
        from rapidfuzz import process

        # Expanded Default Intent Map (Fallback for 2026 Marketplace)
        intent_map = {
            "risk": [
                "cancel",
                "terminate",
                "churn",
                "unhappy",
                "frustrated",
                "bad",
                "garbage",
                "slow",
                "gdpr",
                "legal",
            ],
            "commercial": [
                "upgrade",
                "add seats",
                "more",
                "licenses",
                "quote",
                "invoice",
                "pricing",
                "beta",
                "love this",
            ],
            "action": [
                "call me",
                "meeting",
                "demo",
                "zoom",
                "urgent",
                "asap",
                "critical",
                "blocked",
                "stuck",
                "manager",
            ],
        }

        # Try to load dynamic keywords from Supabase
        dynamic_map = await self._load_dynamic_keywords()
        if dynamic_map:
            intent_map.update(dynamic_map)

        words = text.lower().split()
        NEGATIONS = {"not", "no", "don't", "dont", "won't", "wont", "never"}
        found_signals = []  # List of (priority, alert_msg)

        # Check for highest similarity matches
        for category, keywords in intent_map.items():
            for i, word in enumerate(words):
                # Guard: skip short stop-words — they fuzzy-match anything (e.g. "in" → "terminate")
                if len(word) < 4:  # noqa: PLR2004
                    continue
                match = process.extractOne(word, keywords, score_cutoff=88)
                if match:
                    window = words[max(0, i - 3) : i]
                    if any(neg in window for neg in NEGATIONS):
                        continue

                    # --- COMPOUND LOGIC ---
                    matched_word = match[0]

                    # 🔴 RISK COMPOUNDS
                    if category == "risk":
                        if sentiment_score < 40:  # noqa: PLR2004
                            found_signals.append(
                                (
                                    12,
                                    "🚨 **High-Priority Churn Risk:** Frustrated customer mentioned cancellation or exit.",
                                )
                            )
                        else:
                            found_signals.append(
                                (
                                    10,
                                    "🔴 **Risk Alert:** exit intent or dissatisfaction detected.",
                                )
                            )

                    # 🟢 COMMERCIAL COMPOUNDS
                    elif category == "commercial":
                        if sentiment_score > 70:  # Positive sentiment
                            found_signals.append(
                                (
                                    9,
                                    "🚀 **Expansion Opportunity:** Happy customer interested in expansion or upsell.",
                                )
                            )
                        else:
                            found_signals.append(
                                (
                                    8,
                                    "💰 **Commercial Context:** High-intent signals detected.",
                                )
                            )

                    # 🔵 ACTION COMPOUNDS
                    elif category == "action":
                        if (
                            "urgent" in matched_word
                            or "asap" in matched_word
                            or "critical" in matched_word
                        ):
                            found_signals.append(
                                (
                                    11,
                                    "⚠️ **Urgent Action Required:** Customer mentioned a critical SLA or blocker.",
                                )
                            )
                        else:
                            found_signals.append(
                                (
                                    5,
                                    "🔵 **Pending Action:** Follow-up or meeting requested.",
                                )
                            )

        if found_signals:
            found_signals.sort(key=lambda x: x[0], reverse=True)
            return found_signals[0][1]

        return None

    def _format_sentence(self, text: str) -> str:
        """Heuristic to clean extractive sentences (Capitalization & Punctuation)."""
        if not text:
            return ""

        text = text.strip()
        # Capitalize first letter
        if text and text[0].islower():
            text = text[0].upper() + text[1:]

        # Add period if missing
        if text and text[-1] not in {".", "!", "?"}:
            text += "."

        return text

    async def _load_dynamic_keywords(self) -> dict[str, list[str]] | None:
        """Fetches dynamic AI keywords from Supabase with local TTL caching."""
        try:
            # 1. Delegate to StorageService which handles DB mapping and internal caching
            return await self.storage.get_ai_intent_keywords("global_intents")
        except Exception as e:
            logger.debug("Failed to fetch dynamic keywords: %s", e)
            return None

    # ======================================================
    # ENGAGEMENT
    # ======================================================

    async def analyze_engagement(
        self,
        engagement: Mapping[str, Any],
        **kwargs: Any,
    ) -> AIEngagementAnalysis:
        props = engagement.get("properties") or {}
        etype = (engagement.get("type") or "engagement").lower()

        # HubSpot v3 Engagement Property Mapping
        # Emails: hs_email_subject, hs_email_text
        # Calls: hs_call_title, hs_call_body
        # Notes: (no subject), hs_note_body
        subject = (
            props.get("hs_email_subject")
            or props.get("hs_call_title")
            or props.get("hs_subject")
            or props.get("hs_body_preview")
            or ""
        )

        body = (
            props.get("hs_email_text")
            or props.get("hs_email_html")
            or props.get("hs_call_body")
            or props.get("hs_note_body")
            or ""
        )

        if len(subject) > 80:  # noqa: PLR2004
            subject = subject[:77] + "..."

        summary = f"{etype.title()} — {subject}"
        if body and body.strip() and body not in subject:
            # Clean HTML or truncate
            clean_body = strip_html(body)
            if len(clean_body) > 300:  # noqa: PLR2004
                clean_body = clean_body[:297] + "..."
            summary += f"\n\n{clean_body}"

        return AIEngagementAnalysis(
            insight=summary,
            engagement_type=etype,
            next_best_action="📝 **Activity Logged:** New interaction detected. **Log follow-up if required.**",
        )

    # ======================================================
    # TOP ACTIONS (MULTI-TENANT SAFE)
    # ======================================================

    async def top_recommended_actions(
        self,
        objects: Sequence[Mapping[str, Any]],
        workspace_id: str | None,
    ) -> list[str]:
        cfg = await self._get_workspace_config(workspace_id)

        scored: list[tuple[int, str]] = []

        for obj in objects:
            props = obj.get("properties") or {}
            score = self.generate_score(props, cfg)
            action = self._next_action(props, metrics=None)
            scored.append((score, action))

        scored.sort(key=lambda x: x[0], reverse=True)

        unique = []
        seen = set()

        for _, action in scored:
            if action not in seen:
                unique.append(action)
                seen.add(action)
            if len(unique) == 3:  # noqa: PLR2004
                break

        return unique
