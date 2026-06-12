from __future__ import annotations  # noqa: D100

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BaseInteractionRegistry(Protocol):
    """Platform-agnostic Protocol for interaction handler registries.

    Any connector (Slack, WhatsApp, …) that routes user interactions to
    specific handlers must provide a class that satisfies this Protocol.

    Using a Protocol (structural subtyping) means concrete registries do NOT
    need to explicitly inherit from this class — they only need to implement
    the ``get_handler`` method with a compatible signature.

    ``@runtime_checkable`` allows ``isinstance(obj, BaseInteractionRegistry)``
    guards in factories or tests without requiring explicit subclassing.
    """

    def get_handler(
        self,
        payload: Mapping[str, Any],
        action_id: str | None = None,
    ) -> Any:
        """Return the handler responsible for *payload*, or ``None`` if unmatched.

        Args:
            payload: The raw interaction payload from the messaging platform.
            action_id: Optional pre-extracted action / callback identifier used
                for fast prefix-based routing.

        Returns:
            A platform-specific handler instance, or ``None`` if no handler is
            registered for the given action_id / payload type.

        """
        ...
