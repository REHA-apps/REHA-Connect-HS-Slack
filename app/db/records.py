from __future__ import annotations  # noqa: D100

from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator

from app.db.base_record import BaseRecord


class Provider(StrEnum):
    SLACK = "slack"
    HUBSPOT = "hubspot"
    WHATSAPP = "whatsapp"
    GMAIL = "gmail"
    TEAMS = "teams"


class ProviderSchema(BaseModel):
    """Metadata registry for platform-specific database mappings."""

    provider: Provider
    external_id_key: str  # The column name in 'workspaces' or 'integrations'
    metadata_id_key: str  # The key inside the JSONB metadata field
    id_prefix: str  # Prefix for generating internal IDs (e.g., 'hs_')


CRM_SCHEMAS: dict[Provider, ProviderSchema] = {
    Provider.HUBSPOT: ProviderSchema(
        provider=Provider.HUBSPOT,
        external_id_key="portal_id",
        metadata_id_key="portal_id",
        id_prefix="hs_",
    ),
    Provider.SLACK: ProviderSchema(
        provider=Provider.SLACK,
        external_id_key="slack_team_id",
        metadata_id_key="slack_team_id",
        id_prefix="sl_",
    ),
    Provider.TEAMS: ProviderSchema(
        provider=Provider.TEAMS,
        external_id_key="teams_tenant_id",
        metadata_id_key="teams_tenant_id",
        id_prefix="ms_",
    ),
    Provider.WHATSAPP: ProviderSchema(
        provider=Provider.WHATSAPP,
        external_id_key="whatsapp_phone_number_id",
        metadata_id_key="whatsapp_phone_number_id",
        id_prefix="wa_",
    ),
}


class PlanTier(StrEnum):
    FREE = "free"
    PRO = "pro"
    TRIAL = "trial"

    @classmethod
    def from_string(cls, value: str | None) -> PlanTier:
        """Converts a string to a PlanTier, defaulting to FREE."""
        if not value:
            return cls.FREE
        try:
            return cls(value.lower())
        except ValueError:
            return cls.FREE


class WorkspaceRecord(BaseRecord):
    """Persistence model representing a workspace (e.g., a company or Slack team).

    Rules Applied:
        - Requires a unique string 'id' as the primary identifier.
    """

    required_fields: ClassVar[set[str]] = {"id"}

    id: str
    primary_email: str | None = None
    portal_id: str | None = None
    slack_team_id: str | None = None
    teams_tenant_id: str | None = None
    whatsapp_phone_number_id: str | None = None
    subscription_id: str | None = None
    subscription_status: str | None = "inactive"  # 'active', 'inactive', 'trialing'
    stripe_customer_id: str | None = None
    plan: PlanTier = PlanTier.FREE
    trial_ends_at: datetime | None = None
    install_date: datetime | None = None
    notification_count_monthly: int = 0
    total_sync_count: int = 0
    last_limit_reset: datetime | None = None
    sent_day4_reminder: bool = False

    # Optional metadata
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("portal_id", mode="before")
    @classmethod
    def coerce_to_string(cls, v: Any) -> str | None:
        if v is None:
            return None
        return str(v)


class IntegrationRecord(BaseRecord):
    """Unified persistence model for all integration installations (Slack, HubSpot).

    Rules Applied:
        - Utilizes generic JSONB 'credentials' and 'metadata' fields for flexibility.
        - Links a provider integration to a specific workspace ID.
    """

    required_fields: ClassVar[set[str]] = {"id", "workspace_id", "provider"}

    id: str
    workspace_id: str
    provider: Provider

    # Flexible storage for all platforms
    # Supabase jsonb columns
    credentials: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Optional metadata
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Convenience helpers
    def is_slack(self) -> bool:
        return self.provider == Provider.SLACK

    def is_teams(self) -> bool:
        return self.provider == Provider.TEAMS

    def is_hubspot(self) -> bool:
        return self.provider == Provider.HUBSPOT

    # Credential access helpers
    @property
    def access_token(self) -> str | None:
        return self.credentials.get("access_token")

    @property
    def refresh_token(self) -> str | None:
        return self.credentials.get("refresh_token")

    @property
    def expires_at(self) -> int | None:
        return self.credentials.get("expires_at")

    @property
    def slack_bot_token(self) -> str | None:
        return self.credentials.get("access_token") or self.credentials.get(
            "slack_bot_token"
        )

    @property
    def teams_bot_token(self) -> str | None:
        return self.credentials.get("access_token") or self.credentials.get(
            "teams_bot_token"
        )

    @property
    def portal_id(self) -> str | None:
        return self.metadata.get("portal_id")

    @property
    def slack_team_id(self) -> str | None:
        return self.metadata.get("slack_team_id")

    @property
    def teams_tenant_id(self) -> str | None:
        return self.metadata.get("teams_tenant_id")

    @property
    def channel_id(self) -> str | None:
        return self.metadata.get("channel_id")


class ThreadMappingRecord(BaseRecord):
    """Maps a CRM object to its corresponding Slack thread."""

    required_fields: ClassVar[set[str]] = {
        "workspace_id",
        "object_type",
        "object_id",
        "channel_id",
        "thread_ts",
    }

    workspace_id: str
    object_type: str
    object_id: str
    channel_id: str
    thread_ts: str
    source: str | None = None  # e.g. "email", "workflow", "search", "notification"


class ScoringConfigRecord(BaseRecord):
    required_fields: ClassVar[set[str]] = {"workspace_id"}

    workspace_id: str

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

    max_score: int = 100

    # Heuristic 2.0: Admin Settings
    persona_keywords: str = "vp,director,head,chief,founder,partner,lead,principal"
    sla_threshold_hours: int = 4

    # Registry for fields critical to heuristics (e.g. {"deal": ["amount", "closedate"]})  # noqa: E501
    heuristic_requirements: dict[str, list[str]] = Field(default_factory=dict)

    created_at: datetime | None = None
    updated_at: datetime | None = None


class AIScoreRecord(BaseRecord):
    required_fields: ClassVar[set[str]] = {
        "workspace_id",
        "object_type",
        "object_id",
    }

    workspace_id: str
    object_type: str
    object_id: str

    score: int
    score_reason: str
    next_action: str

    updated_at: datetime | None = None


class UserMappingRecord(BaseRecord):
    """Maps a HubSpot owner to a messaging platform user."""

    required_fields: ClassVar[set[str]] = {
        "workspace_id",
        "hubspot_owner_id",
    }

    workspace_id: str
    hubspot_owner_id: int
    hubspot_email: str | None = None
    slack_user_id: str | None = None
    teams_user_id: str | None = None
    mapping_status: str = "auto"

    updated_at: datetime | None = None


class AIKeywordRecord(BaseRecord):
    """Dynamic AI Intent keywords stored in Supabase (matches intent_keywords table)."""

    required_fields: ClassVar[set[str]] = {"category", "keyword"}

    id: str | None = None
    category: str  # 'risk', 'commercial', 'action'
    keyword: str
    priority_weight: int = 1
    created_at: datetime | None = None


class GhostingHeartbeatRecord(BaseRecord):
    """Persistence model for tracking active customer response timers.

    Rules Applied:
        - Used to coordinate ghosting alerts across ephemeral Lambda instances.
    """

    required_fields: ClassVar[set[str]] = {"workspace_id", "thread_ts"}

    workspace_id: str
    thread_ts: str
    agent_user_id: str | None = None
    expires_at: datetime
    alert_triggered: bool = False
    created_at: datetime | None = None


class ScheduledDigestRecord(BaseRecord):
    """Configuration for an automated scheduled reporting digest."""

    required_fields: ClassVar[set[str]] = {
        "workspace_id",
        "target_channel",
        "cron_expression",
        "timezone",
        "template_id",
    }

    id: str | None = None
    workspace_id: str
    target_channel: str
    cron_expression: str
    timezone: str
    template_id: str
    query_config: dict[str, Any] = Field(default_factory=dict)
    last_run_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
