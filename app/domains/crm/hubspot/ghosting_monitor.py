from __future__ import annotations  # noqa: D100

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any  # noqa: F401

from app.core.logging import get_logger
from app.db.records import (  # noqa: F401
    GhostingHeartbeatRecord,
    IntegrationRecord,
    Provider,
)
from app.db.storage_service import StorageService

logger = get_logger("hubspot.ghosting")


class GhostingMonitor:
    """Real-time monitor for 'Ghosting' detection in HubSpot-Slack threads.

    Rules Applied:
        - Uses database-backed heartbeats to survive Lambda container recycling.
        - Synchronizes across multiple instances via Supabase.
    """  # noqa: D208

    _instance: GhostingMonitor | None = None
    _pending_alerts: dict[str, asyncio.Task[None]] = {}

    def __init__(self, corr_id: str = "ghosting-monitor"):
        self.corr_id = corr_id
        self.storage = StorageService(corr_id=corr_id)

    @classmethod
    def get_instance(cls) -> GhostingMonitor:
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    async def notify_customer_message(
        self, workspace_id: str, thread_ts: str, agent_user_id: str | None = None
    ) -> None:
        """Called when a customer sends a message in a HubSpot conversation."""
        key = f"{workspace_id}:{thread_ts}"

        # 1. Persist to DB (AWS Lambda / Horizontal scaling safety)
        heartbeat = GhostingHeartbeatRecord(
            workspace_id=workspace_id,
            thread_ts=thread_ts,
            agent_user_id=agent_user_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )
        await self.storage.ghosting_heartbeats.upsert(
            heartbeat.to_supabase(), on_conflict="workspace_id,thread_ts"
        )

        # 2. Start local timer (Fast-path if container stays alive)
        if key in self._pending_alerts:
            return

        logger.info("Starting persistent ghosting monitor for thread %s", thread_ts)

        task = asyncio.create_task(
            self._ghosting_timer(workspace_id, thread_ts, agent_user_id)
        )
        self._pending_alerts[key] = task
        task.add_done_callback(lambda _: self._pending_alerts.pop(key, None))

    async def notify_agent_reply(self, workspace_id: str, thread_ts: str) -> None:
        """Called when an agent replies in the Slack thread."""
        key = f"{workspace_id}:{thread_ts}"

        # 1. Clear from DB
        await self.storage.ghosting_heartbeats.delete(
            {"workspace_id": workspace_id, "thread_ts": thread_ts}
        )

        # 2. Cancel local timer
        task = self._pending_alerts.pop(key, None)
        if task:
            logger.info(
                "Agent responded in thread %s. Ghosting alert cancelled.", thread_ts
            )  # noqa: E501
            task.cancel()

    async def shutdown(self) -> None:
        """Gracefully cancels all pending monitor tasks."""
        if not self._pending_alerts:
            return

        logger.info(
            "GhostingMonitor: Cancelling %d pending alerts", len(self._pending_alerts)
        )
        tasks = list(self._pending_alerts.values())
        for task in tasks:
            task.cancel()

        # Wait for all tasks to acknowledge cancellation
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending_alerts.clear()

    async def _ghosting_timer(
        self, workspace_id: str, thread_ts: str, agent_user_id: str | None
    ) -> None:
        """Internal timer that triggers the alert after 60s."""
        try:
            await asyncio.sleep(60)

            # If we reached here, it means the task wasn't cancelled by an agent reply.
            logger.warning(
                "Ghosting detected! No response in thread %s after 60s.", thread_ts
            )  # noqa: E501
            await self._trigger_alert(workspace_id, thread_ts, agent_user_id)
        except asyncio.CancelledError:
            # Task was cancelled correctly
            pass
        except Exception as e:
            logger.error("Error in ghosting timer: %s", e, exc_info=True)

    async def _trigger_alert(
        self, workspace_id: str, thread_ts: str, agent_user_id: str | None
    ) -> None:
        """Sends a High-Urgency DM to the agent."""
        # 1. Resolve agent to DM
        target_user = agent_user_id

        if not target_user:
            # Fallback: Try to find the ticket owner from thread mapping or HubSpot
            # (Note: For brevity, we'll try to find any active integration)
            integration = await self.storage.get_integration(
                workspace_id, Provider.SLACK
            )  # noqa: E501
            if integration:
                target_user = integration.metadata.get("admin_user_id")

        if not target_user:
            logger.error(
                "No target agent found for ghosting alert in workspace %s", workspace_id
            )  # noqa: E501
            return

        # 2. Prepare Alert Card
        from app.domains.messaging.factory import get_messaging_service

        messaging_service = await get_messaging_service(
            workspace_id=workspace_id, storage=self.storage, corr_id=self.corr_id
        )

        if not messaging_service:
            return

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "🚨 *HIGH URGENCY: Ghosting Detected*",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "A customer is waiting for a response in HubSpot Chat! "
                        "It's been over 60 seconds since their last message."
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Thread 🧵"},
                        "url": f"https://app.slack.com/archives/{thread_ts}",  # Placeholder  # noqa: E501
                        "action_id": "view_ghosting_thread",
                    }
                ],
            },
        ]

        await messaging_service.send_message(
            workspace_id=workspace_id,
            channel=target_user,
            text="🚨 High Urgency: Customer is waiting for a response!",
            blocks=blocks,
        )

        # Mark as triggered in DB to avoid double-alerts if the worker picks it up
        await self.storage.ghosting_heartbeats.update(
            {"workspace_id": workspace_id, "thread_ts": thread_ts},
            {"alert_triggered": True},
        )
        logger.info("Ghosting DM sent to agent %s", target_user)

    async def process_stale_heartbeats(self) -> int:
        """Background worker task to process expired heartbeats across all instances.

        Returns:
            Number of alerts triggered.

        """
        now = datetime.now(UTC)
        # Fetch expired heartbeats that haven't been triggered
        stale = await self.storage.ghosting_heartbeats.fetch_many(
            {"alert_triggered": False},
            order_by=("expires_at", "asc"),
        )

        expired = [h for h in stale if h.expires_at <= now]
        if not expired:
            return 0

        for heartbeat in expired:
            logger.warning(
                "Stale ghosting heartbeat detected for %s. Triggering alert.",
                heartbeat.thread_ts,
            )

        # Concurrent alert firing — stateless per heartbeat
        await asyncio.gather(
            *(
                self._trigger_alert(
                    h.workspace_id,
                    h.thread_ts,
                    h.agent_user_id,
                )
                for h in expired
            ),
            return_exceptions=True,
        )

        count = len(expired)

        # Cleanup old triggered heartbeats (older than 1 hour)
        try:
            from app.db.supabase_client import get_async_client

            db_client = await get_async_client()
            await (
                db_client.table("ghosting_heartbeats")
                .delete()
                .eq("alert_triggered", True)
                .lt("created_at", str(now - timedelta(hours=1)))
                .execute()
            )
        except Exception as e:
            logger.warning("Failed to cleanup old ghosting heartbeats: %s", e)
        return count
