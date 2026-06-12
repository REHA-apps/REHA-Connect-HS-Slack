from __future__ import annotations  # noqa: D100

from typing import Any


class AppError(Exception):
    """Base category for all application-specific errors."""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int = 500,
    ):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.status_code = status_code


class IntegrationError(AppError):
    """Base for errors related to workspace/provider integrations."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, details, status_code=400)


class IntegrationNotFoundError(IntegrationError):
    """Raised when an integration record is expected but not found."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, details)
        self.status_code = 404


class AuthenticationError(AppError):
    """Raised when authentication or token exchange fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, details, status_code=401)


class RateLimitError(AppError):
    """Raised when an external API throttles our requests."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, details, status_code=429)


class CRMObjectNotFoundError(AppError):
    """Raised when a HubSpot/CRM object is not found."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, details, status_code=404)


class ExternalAPIError(AppError):
    """Base for errors raised when an external platform API call fails."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message, status_code=status_code)


class HubSpotAPIError(ExternalAPIError):
    """Raised when an external HubSpot API call fails."""


class SlackAPIError(ExternalAPIError):
    """Raised when an external Slack API call fails."""


class AIServiceError(AppError):
    """Raised when AI analysis or heuristics fail."""
