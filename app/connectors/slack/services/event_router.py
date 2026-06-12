from typing import Any  # noqa: D100, I001
from app.core.logging import get_logger
from app.domains.messaging.slack.service import SlackMessagingService

logger = get_logger("slack.event_router")


class SlackEventRouter:
    """Routes incoming Slack events to their appropriate handlers in the messaging service."""  # noqa: E501

    def __init__(self, messaging_service: SlackMessagingService):
        self.messaging_service = messaging_service
        self.corr_id = messaging_service.corr_id
        self.integration_service = messaging_service.integration_service
        self.slack_integration = messaging_service.slack_integration

    async def dispatch_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        background_tasks: Any,
    ) -> dict[str, Any]:
        """Central dispatcher for Slack Events.

        Handles Link Unfurling, Uninstall, and Threaded Replies by delegating
        to specific handlers in the SlackMessagingService, often backgrounding
        them for low-latency response.
        """
        event = payload.get("event", {})
        team_id = payload.get("team_id")
        workspace_id = (
            self.slack_integration.workspace_id if self.slack_integration else None
        )

        logger.debug("Dispatching Slack event: type=%s team_id=%s", event_type, team_id)

        # 1. Handle Uninstall
        if event_type == "app_uninstalled":
            logger.info("Slack integration removed for team_id=%s", team_id)
            if not team_id or not self.integration_service:
                return {"status": "error", "message": "Invalid request"}

            storage = self.integration_service.storage
            integrations = await storage.list_integrations_by_slack_team_id(team_id)
            for integration in integrations:
                # Security Audit Log per workspace
                from app.domains.common.audit_service import AuditService

                audit = AuditService(corr_id=self.corr_id)
                await audit.log_action(
                    action="slack_uninstall",
                    workspace_id=integration.workspace_id,
                    metadata={"slack_team_id": team_id, "source": "slack_event"},
                )

                await self.integration_service.uninstall_slack(integration.workspace_id)
                logger.info(
                    "Slack integration removed for workspace_id=%s", workspace_id
                )
            return {"ok": True}

        # 2. Handle Link Shared (Unfurling)
        if event_type == "link_shared":
            links = event.get("links", [])
            channel = event.get("channel")
            ts = event.get("message_ts")
            user_id = event.get("user")
            # 2026 Composer Unfurl Metadata
            unfurl_id = event.get("unfurl_id")
            source = event.get("source")

            if links and channel and workspace_id:
                from app.core.config import settings
                from app.utils.sqs_helpers import publish_to_sqs

                published = publish_to_sqs(
                    queue_url=settings.SQS_SLACK_WEBHOOK_QUEUE_URL,
                    workspace_id=workspace_id,
                    corr_id=self.corr_id,
                    task_type="slack_event_link_shared",
                    payload={
                        "channel": channel,
                        "ts": ts,
                        "links": links,
                        "user_id": user_id,
                        "unfurl_id": unfurl_id,
                        "source": source,
                    },
                )
                if not published:
                    from app.core.logging import run_task_with_context

                    background_tasks.add_task(
                        run_task_with_context,
                        self.corr_id,
                        self.messaging_service.handle_link_shared,
                        workspace_id=workspace_id,
                        channel=channel,
                        ts=ts,
                        links=links,
                        user_id=user_id,
                        unfurl_id=unfurl_id,
                        source=source,
                    )
            return {"ok": True}

        # 3. Handle Message (Replies / Sniffing)
        if event_type == "message":
            # Delegate to specialized message handler logic
            await self.messaging_service._handle_message_event(event, background_tasks)
            return {"ok": True}

        # 4. Handle App Home
        if event_type == "app_home_opened":
            user_id = event.get("user")
            if user_id and workspace_id:
                from app.core.config import settings
                from app.utils.sqs_helpers import publish_to_sqs

                published = publish_to_sqs(
                    queue_url=settings.SQS_SLACK_WEBHOOK_QUEUE_URL,
                    workspace_id=workspace_id,
                    corr_id=self.corr_id,
                    task_type="slack_event_app_home_opened",
                    payload={
                        "user_id": user_id,
                    },
                )
                if not published:
                    from app.core.logging import run_task_with_context

                    background_tasks.add_task(
                        run_task_with_context,
                        self.corr_id,
                        self.messaging_service.handle_app_home_opened,
                        user_id=user_id,
                    )
            return {"ok": True}

        return {"ok": True}
