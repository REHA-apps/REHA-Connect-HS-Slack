from __future__ import annotations  # noqa: D100

from typing import Any, ClassVar

from app.connectors.common.base import BaseChannel
from app.db.records import Provider


class ChannelRegistry:
    """Registry for managing and instantiating communication channels.

    This decouples domain services from specific platform implementations,
    allowing for easy extension to new providers (Slack, HubSpot, WhatsApp, etc.).
    """

    _registry: ClassVar[dict[Provider, type[BaseChannel]]] = {}

    @classmethod
    def register(cls, provider: Provider, channel_cls: type[BaseChannel]) -> None:
        """Registers a channel implementation for a specific provider."""
        cls._registry[provider] = channel_cls

    @classmethod
    def get_channel(cls, provider: Provider, **kwargs: Any) -> BaseChannel:
        """Instantiates and returns the registered channel for the given provider."""
        channel_cls = cls._registry.get(provider)
        if not channel_cls:
            raise ValueError(f"No channel registered for provider: {provider}")

        return channel_cls(**kwargs)


# Import channels and register them
def initialize_registry() -> None:
    """Initialize the ChannelRegistry with known channel implementations.

    .. deprecated::
        Use ``setup_connectors()`` from ``app.connectors`` instead.
        ``setup_connectors()`` registers both channel classes *and* router
        manifests in a single call, making this function redundant.

        This shim is retained for test-fixture backwards compatibility only
        and will be removed in a future cleanup sprint.
    """
    import warnings

    warnings.warn(
        "initialize_registry() is deprecated. Call setup_connectors() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from app.connectors.slack.slack_channel import SlackChannel
    from app.providers.hubspot.channel import HubSpotChannel

    ChannelRegistry.register(Provider.SLACK, SlackChannel)
    ChannelRegistry.register(Provider.HUBSPOT, HubSpotChannel)
