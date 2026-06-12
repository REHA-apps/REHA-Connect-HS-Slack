# ruff: noqa: E501  # noqa: D100
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

    from app.db.records import IntegrationRecord

from app.core.config import settings
from app.core.exceptions import IntegrationNotFoundError
from app.core.logging import get_logger, run_task_with_context
from app.domains.ai.service import AIService
from app.domains.common.audit_service import AuditService
from app.domains.crm.hubspot.service import HubSpotService
from app.domains.crm.integration_service import IntegrationService
from app.domains.messaging.slack.service import SlackMessagingService
from app.utils.constants import SUB_COMMANDS

logger = get_logger("slack.command.service")

# SQS client and publishing are imported from app.utils.sqs_helpers


_COMMAND_USAGE = {
    "main": (
        "Usage: `/reha <sub-command> <query>`\n\n"
        "Available sub-commands:\n"
        "• `contact`: Search HubSpot contacts (email or name)\n"
        "• `deal`: Search HubSpot deals\n"
        "• `company`: Search HubSpot companies\n"
        "• `ticket`: Search HubSpot tickets\n"
        "• `lead`: Search HubSpot leads\n"
        "• `task`: Create/Search HubSpot tasks (Pro)\n"
        "• `report`: View HubSpot dashboards and reporting\n"
        "• `help`: Show this help message\n\n"
        "💡 *Universal Search*: Type `/reha [any text]` to search across all CRM objects simultaneously."
    ),
    "contact": "Usage: `/reha contact <name or email>`",
    "lead": "Usage: `/reha lead <name or email>`",
    "company": "Usage: `/reha company <company name or domain>`",
    "deal": "Usage: `/reha deal <deal name>`",
    "ticket": "Usage: `/reha ticket <subject or ID>`",
    "task": "Usage: `/reha task <task name>`",
    "report": "Usage: `/reha report` to view dashboards.",
}


class CommandService:
    """Broker service for processing and delegating Slack slash commands.

    Handles parsing of sub-commands, permission gating (Pro vs Free),
    and coordination between HubSpot and Slack messaging services.
    """

    def __init__(
        self,
        corr_id: str,
        integration: IntegrationRecord,
        *,
        ai: AIService | None = None,
        integration_service: IntegrationService | None = None,
    ) -> None:
        self.corr_id = corr_id
        self.integration = integration

        # Shared per-request dependencies
        self.ai = ai or AIService(corr_id)

        _integration_service = integration_service or IntegrationService(corr_id)

        # Share StorageService from the integration_service to avoid split-brain caches
        self.hubspot = HubSpotService(corr_id, storage=_integration_service.storage)

        self.messaging_service = SlackMessagingService(
            corr_id=corr_id,
            integration_service=_integration_service,
            slack_integration=integration,
        )

    def _publish_to_sqs(
        self,
        workspace_id: str,
        corr_id: str,
        task_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """Publishes a slash command payload to SQS for reliable Lambda execution.

        Returns True if successfully published, False otherwise (e.g. dev mode).
        """
        from app.utils.sqs_helpers import publish_to_sqs

        return publish_to_sqs(
            queue_url=settings.SQS_SLACK_WEBHOOK_QUEUE_URL,
            workspace_id=workspace_id,
            corr_id=corr_id,
            task_type=task_type,
            payload=payload,
        )

    async def handle_slack_command(  # noqa: PLR0911, PLR0912
        self,
        *,
        command: str | None,
        text: str,
        workspace_id: str,
        response_url: str,
        channel_id: str,
        user_id: str = "",
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Entry point for all /reha slash commands."""
        # 2. Command Validation
        is_pro = False
        if self.messaging_service and self.messaging_service.integration_service:
            is_pro = await self.messaging_service.integration_service.is_pro_workspace(
                workspace_id
            )

        # 2. Command Validation
        if not command or command != "/reha":
            return {"response_type": "ephemeral", "text": f"Unknown command: {command}"}

        # 3. Parse Sub-command and Query
        parts = text.strip().split(" ", 1)
        sub_cmd = parts[0].lower() if parts else ""
        query = parts[1].strip() if len(parts) > 1 else ""

        # 4. Handle Special Sub-commands (Help, Report)
        if sub_cmd in ("help", "h"):
            return self._usage_for("/reha")

        if sub_cmd in ("report", "reports", "stats"):
            sqs_payload = {
                "sub_cmd": "report",
                "workspace_id": workspace_id,
                "response_url": response_url,
                "channel_id": channel_id,
                "user_id": user_id,
                "query": "",
                "object_type": "report",
            }
            if not self._publish_to_sqs(
                workspace_id=workspace_id,
                corr_id=self.corr_id,
                task_type="slack_command",
                payload=sqs_payload,
            ):
                asyncio.create_task(
                    run_task_with_context(
                        self.corr_id,
                        self._send_report_command,
                        workspace_id,
                        response_url,
                    )
                )
            return {"response_type": "ephemeral", "text": "Fetching HubSpot reports..."}

        # 5. Handle Specialized CRM Searches
        if sub_cmd in SUB_COMMANDS:
            if not query:
                return self._usage_for(sub_cmd)

            cfg = SUB_COMMANDS[sub_cmd]
            object_type = cfg["object_type"]
            prefix = cfg["prefix"]

            # (Note: Interactive task actions like creation/updating are gated at the UI component level)

            # Schedule specialized search — SQS in prod, create_task in dev
            self._publish_or_run_search_command(
                sub_cmd,
                workspace_id,
                query,
                channel_id,
                response_url,
                object_type,
                user_id,
            )
            return {"response_type": "ephemeral", "text": f"{prefix} for *{query}*..."}

        # 6. Default: Universal Multi-Object Search (if query exists) or Usage
        # If the first word wasn't a keyword, we treat the WHOLE text as a search query
        all_text = text.strip()
        if not all_text:
            return self._usage_for("/reha")

        # Universal search — SQS in prod, create_task in dev
        self._publish_or_run_search_command(
            "universal",
            workspace_id,
            all_text,
            channel_id,
            response_url,
            "universal",
            user_id,
        )

        # Security Audit: Log Search Query (fire-and-forget, always in-process).
        # asyncio.create_task() is intentional here — audit logging must never block
        # the command response. The inner try/except ensures any DB failure is captured
        # in structured logs under this request's corr_id, not silently dropped to stderr.
        audit = AuditService(corr_id=self.corr_id)
        _corr_id = self.corr_id

        async def _log_audit() -> None:
            try:
                await audit.log_action(
                    action="crm_search",
                    workspace_id=workspace_id,
                    actor_id=user_id,
                    metadata={
                        "query": all_text,
                        "object_type": "universal",
                    },
                )
            except Exception as exc:
                logger.warning(
                    "Audit log failed for crm_search (corr_id=%s): %s",
                    _corr_id,
                    exc,
                )

        asyncio.create_task(_log_audit())

        return {
            "response_type": "ephemeral",
            "text": f"Searching via REHA for *{all_text}*...",
        }

    async def _send_report_command(self, workspace_id: str, response_url: str) -> None:
        """Background task to fetch and send HubSpot reports."""
        try:
            reports_card = await self.messaging_service.build_reports_card(workspace_id)
            if reports_card:
                payload = self.messaging_service.slack_renderer.render(reports_card)
                await self.messaging_service.send_via_response_url(
                    response_url=response_url,
                    blocks=payload.get("blocks"),
                    text="HubSpot Performance Report",
                )
        except IntegrationNotFoundError:
            logger.warning(
                "Report generation failed: HubSpot not connected for workspace %s",
                workspace_id,
            )
            await self.messaging_service.send_via_response_url(
                response_url=response_url,
                text="❌ *HubSpot not connected.* Please connect your HubSpot account first.",
            )
        except Exception:
            logger.exception("Failed to send reports command")

    def _usage_for(self, command: str) -> dict[str, str]:
        text = _COMMAND_USAGE.get(command, _COMMAND_USAGE["main"])
        if command not in {"main", "/reha"}:
            text = f"{text}\nTry `/reha help` for all options."

        return {"response_type": "ephemeral", "text": text}

    def _publish_or_run_search_command(
        self,
        sub_cmd: str,
        workspace_id: str,
        query: str,
        channel_id: str,
        response_url: str,
        object_type: str,
        user_id: str,
    ) -> None:
        sqs_payload = {
            "sub_cmd": sub_cmd,
            "workspace_id": workspace_id,
            "query": query,
            "channel_id": channel_id,
            "response_url": response_url,
            "object_type": object_type,
            "user_id": user_id,
        }
        if not self._publish_to_sqs(
            workspace_id=workspace_id,
            corr_id=self.corr_id,
            task_type="slack_command",
            payload=sqs_payload,
        ):
            import asyncio

            from app.core.logging import run_task_with_context

            asyncio.create_task(
                run_task_with_context(
                    self.corr_id,
                    self.messaging_service.search_and_send,
                    workspace_id,
                    query,
                    channel_id,
                    response_url,
                    object_type,
                    self.corr_id,
                    user_id,
                )
            )
