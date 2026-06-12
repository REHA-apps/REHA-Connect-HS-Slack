# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

from typing import Any

from fastapi import Request

from app.core.logging import get_logger
from app.db.supabase_client import SupabaseClient
from app.utils.helpers import get_client_country, get_client_ip

logger = get_logger("audit.service")


class AuditService:
    """Service for recording security auditing events in Supabase.

    Captures actor metadata, Cloudflare-aware client IPs, geographic data, and action details.
    """

    def __init__(self, corr_id: str | None = None) -> None:
        self.corr_id = corr_id or "system"
        self.client = SupabaseClient(corr_id=self.corr_id)

    async def log_action(
        self,
        *,
        action: str,
        workspace_id: str | None = None,
        actor_id: str | None = None,
        request: Request | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Asynchronously records an audit event to the database.

        Can be awaited directly or passed to FastAPI's BackgroundTasks.
        """
        client_ip = "unknown"
        user_agent = "unknown"
        country_code = "XX"

        if request:
            client_ip = get_client_ip(request)
            user_agent = request.headers.get("User-Agent", "unknown")
            country_code = get_client_country(request)

        payload = {
            "workspace_id": workspace_id,
            "actor_id": actor_id or "system",
            "action": action,
            "client_ip": client_ip,
            "country_code": country_code,
            "user_agent": user_agent,
            "metadata": metadata or {},
        }

        try:
            # We use direct client.insert since AuditService doesn't
            # need a high-level repository (it's write-only mostly)
            await self.client.insert("audit_logs", payload)
            logger.info(
                "Audit Log Created: action=%s, ip=%s, country=%s, workspace=%s",
                action,
                client_ip,
                country_code,
                workspace_id,
            )
        except Exception as e:
            logger.error("Failed to create audit log: %s", e, exc_info=True)

    async def prune_stale_logs(self, days_to_keep: int = 90) -> int:
        """Invokes the DB-side pruning function to enforce retention policies.

        Returns:
            The number of logs deleted.

        """
        try:
            # Call the 'prune_audit_logs' function via RPC
            count = await self.client.rpc(
                "prune_audit_logs", {"days_to_keep": days_to_keep}
            )
            logger.debug("Successfully pruned audit logs. Rows deleted: %s", count)
            return count or 0
        except Exception as e:
            logger.error("Failed to prune audit logs: %s", e)
            return 0
