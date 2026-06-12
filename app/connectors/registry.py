from __future__ import annotations  # noqa: D100

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

if TYPE_CHECKING:
    from app.connectors.common.base import BaseChannel
    from app.db.records import Provider

from fastapi import APIRouter

from app.core.models.ui import UnifiedCard
from app.domains.messaging.base import MessagingService


class Renderer(Protocol):
    def render(self, card: UnifiedCard, is_unfurl: bool = False) -> dict[str, Any]: ...


@dataclass
class ConnectorManifest:
    """Describes all components of a single platform connector.

    Serves as the single source of truth for connector registration —
    combining channel (OAuth/API), routing, and messaging concerns.
    When ``provider`` and ``channel_cls`` are supplied, the connector
    is automatically registered with ``ChannelRegistry`` so domain
    services can call ``ChannelRegistry.get_channel(provider)`` without
    any additional setup.

    Attributes:
        name: Unique connector identifier (e.g. ``"slack"``, ``"hubspot"``).
        provider: The ``Provider`` enum value for ChannelRegistry lookup.
        channel_cls: The ``BaseChannel`` subclass to instantiate for API calls.
        renderer: Optional card renderer for UI surfaces.
        service: Optional interaction/command service class.
        channel_service: Optional messaging service class.
        routers: FastAPI routers contributed by this connector.

    """

    name: str
    provider: Provider | None = None
    channel_cls: type[BaseChannel] | None = None
    renderer: type[Renderer] | None = None
    service: type[Any] | None = None
    channel_service: type[MessagingService] | None = None
    routers: list[APIRouter] = field(default_factory=list)


class ConnectorRegistry:
    """Central registry for multi-platform connectors (Slack, HubSpot, WhatsApp).

    Allows dynamic discovery and registration of platform-specific components.
    When a manifest declares ``provider`` + ``channel_cls``, the registry
    transparently registers them with ``ChannelRegistry`` so both routing
    and API-call concerns are handled from a single ``register()`` call.

    Adding a new connector (e.g. WhatsApp) requires only one call::

        registry.register(
            name="whatsapp",
            provider=Provider.WHATSAPP,
            channel_cls=WhatsAppChannel,
            routers=[wa_webhook, wa_install],
        )
    """

    _connectors: ClassVar[dict[str, ConnectorManifest]] = {}

    @classmethod
    def register(
        cls,
        name: str,
        *,
        provider: Provider | None = None,
        channel_cls: type[BaseChannel] | None = None,
        renderer: type[Renderer] | None = None,
        service: type[Any] | None = None,
        channel_service: type[MessagingService] | None = None,
        routers: list[APIRouter] | None = None,
    ) -> None:
        """Register a connector manifest and, optionally, its channel class.

        Args:
            name: Unique connector name.
            provider: Optional ``Provider`` enum value. When supplied together
                with ``channel_cls``, the channel is automatically registered
                with ``ChannelRegistry``.
            channel_cls: Optional ``BaseChannel`` subclass for API calls.
            renderer: Optional card renderer class.
            service: Optional interaction service class.
            channel_service: Optional messaging service class.
            routers: Optional list of FastAPI routers for this connector.

        """
        manifest = ConnectorManifest(
            name=name,
            provider=provider,
            channel_cls=channel_cls,
            renderer=renderer,
            service=service,
            channel_service=channel_service,
            routers=routers or [],
        )
        cls._connectors[name] = manifest

        # Auto-register channel with ChannelRegistry when both fields present
        if provider is not None and channel_cls is not None:
            from app.connectors.common.registry import ChannelRegistry

            ChannelRegistry.register(provider, channel_cls)

    @classmethod
    def get_connector(cls, name: str) -> ConnectorManifest | None:
        """Returns the manifest for the named connector, or None if not registered."""
        return cls._connectors.get(name)

    @classmethod
    def get_all_routers(cls) -> list[APIRouter]:
        """Returns all routers contributed by every registered connector."""
        routers: list[APIRouter] = []
        for manifest in cls._connectors.values():
            routers.extend(manifest.routers)
        return routers

    @classmethod
    def all_manifests(cls) -> list[ConnectorManifest]:
        """Returns all registered manifests in registration order."""
        return list(cls._connectors.values())


# Global singleton — import this when you need to register or query connectors
registry = ConnectorRegistry()
