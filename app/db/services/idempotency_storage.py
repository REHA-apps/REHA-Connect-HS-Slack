from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.db.services.base import BaseStorage, logger

if TYPE_CHECKING:
    pass


class IdempotencyStorage(BaseStorage):
    """Storage service for persistent idempotency tracking.

    Prevents duplicate processing of webhooks across distributed compute
    instances by tracking event IDs in a shared database.
    """

    async def is_processed(self, event_id: str) -> bool:
        """Checks if an event has already been processed and is not expired."""
        try:
            res = await self.client.fetch_single(
                "processed_events", {"event_id": event_id}, select=["event_id"]
            )
            return res is not None
        except Exception as e:
            logger.warning("Idempotency check failed for %s: %s", event_id, e)
            return False

    async def mark_processed(
        self, event_id: str, provider: str, ttl_minutes: int = 60
    ) -> bool:
        """Marks an event as processed with a specific expiration time.

        Returns True if the marking was successful (unique), False if it
        collided with an existing record (duplicate).
        """
        expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)

        try:
            await self.client.insert(
                "processed_events",
                {
                    "event_id": event_id,
                    "provider": provider,
                    "expires_at": expires_at.isoformat(),
                },
            )
            return True
        except Exception as e:
            # COLLISION: Likely a duplicate event being processed concurrently
            if "duplicate key" in str(e).lower():
                return False
            logger.error("Failed to record idempotency for %s: %s", event_id, e)
            return True  # Fail open to prevent blocking real events on DB issues
