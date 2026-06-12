# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, HttpUrl, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.logging import get_logger

logger = get_logger("config")


class Settings(BaseSettings):
    """Centralized application configuration using Pydantic Settings.

    Loads configuration from environment variables and .env files,
    providing validated types for URLs and sensitive credentials used
    across the platform.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        validate_default=True,
    )

    # App settings
    APP_NAME: str = Field(default="CRM Connector")
    APP_VERSION: str = Field(default="1.0.0")

    ENV: Literal["dev", "staging", "prod"] = "dev"

    LOG_LEVEL: str = "INFO"
    DEBUG: bool = False
    USE_JSON_LOGS: bool | None = None
    # Set True on Render/Docker where the process is long-lived.
    # Leave False (default) on AWS Lambda — asyncio tasks freeze between invocations.
    ENABLE_BACKGROUND_WORKERS: bool = False

    # AWS Settings
    SQS_SLACK_WEBHOOK_QUEUE_URL: str | None = Field(default=None)
    SQS_HUBSPOT_WEBHOOK_QUEUE_URL: str | None = Field(default=None)

    ENCRYPTION_KEY: SecretStr = Field(default=SecretStr(""))
    SESSION_SECRET_KEY: SecretStr = Field(default=SecretStr("dev-secret-key-change-me"))

    # Branding & Links
    HOMEPAGE_URL: HttpUrl = Field(default=HttpUrl("https://rehaapps.com"))
    PRICING_URL: HttpUrl = Field(default=HttpUrl("https://rehaapps.com/pricing.html"))
    CONTACT_URL: HttpUrl = Field(default=HttpUrl("https://rehaapps.com/contact.html"))

    # HubSpot settings
    HUBSPOT_CLIENT_ID: str = Field(default="")
    HUBSPOT_CLIENT_SECRET: SecretStr = Field(default=SecretStr(""))
    HUBSPOT_APP_ID: str = Field(default="")
    HUBSPOT_REDIRECT_URI: HttpUrl = Field(default=HttpUrl("http://localhost"))
    HUBSPOT_MESSAGE_TEMPLATE_ID: str = Field(default="")
    HUBSPOT_API_VERSION: str = Field(default="2026-03")

    # Slack settings
    SLACK_CLIENT_ID: str = Field(default="")
    SLACK_CLIENT_SECRET: SecretStr = Field(default=SecretStr(""))
    SLACK_REDIRECT_URI: HttpUrl = Field(default=HttpUrl("http://localhost"))
    SLACK_SIGNING_SECRET: SecretStr = Field(default=SecretStr(""))
    SLACK_BOT_TOKEN: SecretStr = Field(default=SecretStr(""))
    SLACK_APP_ID: str = Field(default="A0ASAS6MNCC")

    SLACK_USER_SCOPES: str = "links:read,links:write"

    # HubSpot Support account settings (Private App)
    HUBSPOT_SUPPORT_ACCESS_TOKEN: SecretStr = Field(default=SecretStr(""))
    HUBSPOT_SUPPORT_PORTAL_ID: str = Field(default="")

    @field_validator(
        "HUBSPOT_SUPPORT_PORTAL_ID", "HUBSPOT_MESSAGE_TEMPLATE_ID", mode="before"
    )
    @classmethod
    def coerce_to_string(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    # Supabase settings
    SUPABASE_URL: HttpUrl = Field(default=HttpUrl("http://localhost"))
    SUPABASE_KEY: SecretStr = Field(default=SecretStr(""))

    # API settings
    API_BASE_URL: HttpUrl = Field(default=HttpUrl("http://localhost"))
    API_PUBLIC_URL: HttpUrl = Field(default=HttpUrl("http://localhost"))
    DISK_PATH: str = Field(default="./models")
    REHA_WEBHOOK_SECRET: SecretStr = Field(default=SecretStr(""))

    # Stripe settings
    STRIPE_SECRET_KEY: SecretStr = Field(default=SecretStr(""))
    STRIPE_WEBHOOK_SECRET: SecretStr = Field(default=SecretStr(""))
    STRIPE_PRO_PRICE_ID: str = Field(default="")
    STRIPE_RESTRICTED_KEY: SecretStr = Field(default=SecretStr(""))

    # Email Settings for Contact Form (SMTP Fallback)
    CONTACT_EMAIL_DESTINATION: str = Field(default="hello@rehaapps.com")

    # SMTP Settings
    SMTP_SERVER: str = Field(default="smtp.gmail.com")
    SMTP_PORT: int = Field(default=587)
    SMTP_USERNAME: str = Field(default="")
    SMTP_PASSWORD: SecretStr = Field(default=SecretStr(""))

    HUBSPOT_SCOPES: tuple[str, ...] = Field(
        default=(
            "oauth",
            "crm.objects.contacts.read",
            "crm.objects.contacts.write",
            "crm.objects.companies.read",
            "crm.objects.companies.write",
            "crm.objects.deals.read",
            "crm.objects.deals.write",
            "crm.objects.leads.read",
            "crm.objects.leads.write",
            "conversations.read",
            "conversations.write",
            "crm.objects.owners.read",
            "tickets",
            "sales-email-read",
        ),
        repr=False,
    )

    SLACK_SCOPES: tuple[str, ...] = Field(
        default=(
            "commands",
            "chat:write",
            "chat:write.public",
            "users:read",
            "users:read.email",
            "team:read",
            "links:read",
            "links:write",
            "app_mentions:read",
            "channels:history",
            "groups:history",
            "im:history",
            "mpim:history",
            "channels:read",
            "groups:read",
            "im:read",
            "mpim:read",
        ),
        repr=False,
    )

    def to_safe_dict(self) -> Mapping[str, Any]:
        """Return a safe, scrubbed snapshot of the configuration.

        Returns:
            Configuration dictionary with sensitive values masked.

        """
        result: dict[str, Any] = {}
        for name in self.model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                result[name] = "***"
            else:
                result[name] = value
        return result

    @computed_field(repr=False)
    @property
    def HUBSPOT_SCOPES_STR(self) -> str:
        return " ".join(self.HUBSPOT_SCOPES)

    @computed_field(repr=False)
    @property
    def SLACK_SCOPES_STR(self) -> str:
        return " ".join(self.SLACK_SCOPES)

    @computed_field(repr=False)
    @property
    def HUBSPOT_SCOPES_ENCODED(self) -> str:
        return self.HUBSPOT_SCOPES_STR.replace(" ", "%20")

    @computed_field(repr=False)
    @property
    def SLACK_SCOPES_ENCODED(self) -> str:
        return self.SLACK_SCOPES_STR.replace(" ", "%20")

    @computed_field
    @property
    def API_BASE_URL_STR(self) -> str:
        """Sanitized version of API_BASE_URL as a string without a trailing slash."""
        return str(self.API_BASE_URL).rstrip("/")

    # ---------------------------------------------------------
    # Environment flags
    # ---------------------------------------------------------
    @computed_field
    @property
    def is_dev(self) -> bool:
        return self.ENV.lower() == "dev"

    @computed_field
    @property
    def is_staging(self) -> bool:
        return self.ENV.lower() == "staging"

    @computed_field
    @property
    def is_prod(self) -> bool:
        return self.ENV.lower() == "prod"

    @computed_field
    @property
    def is_debug(self) -> bool:
        return self.DEBUG or self.is_dev

    @computed_field
    @property
    def use_json_logs(self) -> bool:
        if self.USE_JSON_LOGS is not None:
            return self.USE_JSON_LOGS
        return self.is_prod

    def require_prod_secrets(self) -> None:
        """Validates that all necessary secrets are provided when running in prod.

        Returns:
            None

        Rules Applied:
            - Raises RuntimeError if any SecretStr is empty in a production environment.

        """
        if not self.is_prod:
            return

        if self.DEBUG:
            raise RuntimeError("DEBUG must be False in production")

        missing = []
        for name, field_info in self.model_fields.items():
            if field_info.annotation is SecretStr:
                val = getattr(self, name)
                if not val.get_secret_value():
                    missing.append(name)

        if missing:
            raise RuntimeError(f"Missing required production secrets: {missing}")

        if not self.HUBSPOT_SUPPORT_PORTAL_ID:
            raise RuntimeError(
                "Missing required production secret: HUBSPOT_SUPPORT_PORTAL_ID"
            )

        # CR-28: Enforce session key entropy for production safety
        if len(self.SESSION_SECRET_KEY.get_secret_value()) < 32:  # noqa: PLR2004
            raise RuntimeError(
                "SESSION_SECRET_KEY must be at least 32 characters in production for sufficient entropy (CR-28)."
            )

        # Explicitly check for webhook secret in prod
        if not self.REHA_WEBHOOK_SECRET.get_secret_value() and self.is_prod:
            raise RuntimeError("REHA_WEBHOOK_SECRET must be set in production")

        # Explicitly block weak default session key
        if (
            self.SESSION_SECRET_KEY.get_secret_value() == "dev-secret-key-change-me"
            and self.is_prod
        ):
            raise RuntimeError(
                "SESSION_SECRET_KEY is still using the default development "
                "value in production"
            )

        # Specific safety checks
        encryption_val = self.ENCRYPTION_KEY.get_secret_value()
        if encryption_val and len(encryption_val) < 43:
            logger.warning(
                "ENCRYPTION_KEY is shorter than 43 characters; AES-256 expects a 32-byte base64 encoding."
            )

    _validated: bool = False

    def validate_all(self) -> None:
        """Executes all application-level configuration validation checks.

        Ensures that critical secrets are present in production and that
        security configurations are sane.
        """
        if self._validated:
            return

        self.require_prod_secrets()
        self._validated = True


settings = Settings()
