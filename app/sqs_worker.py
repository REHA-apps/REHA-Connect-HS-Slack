"""SQS worker handler for processing async Slack interactions on AWS Lambda.

This module consumes messages from the SQS queue and routes them to the
InteractionService. This replaces the FastAPI BackgroundTasks approach
which freezes on AWS Lambda.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, TypedDict


class SQSRecord(TypedDict, total=False):
    """Standard AWS SQS Message structure."""

    messageId: str
    receiptHandle: str
    body: str
    attributes: dict[str, str]
    messageAttributes: dict[str, dict[str, str]]
    md5OfBody: str
    eventSource: str
    eventSourceARN: str
    awsRegion: str


class SQSEvent(TypedDict, total=False):
    """Standard AWS SQS Event payload."""

    Records: list[SQSRecord]


from app.connectors import setup_connectors
from app.connectors.slack.services.service import InteractionService  # noqa: E402
from app.core.logging import get_logger, log_context  # noqa: E402
from app.db.records import Provider  # noqa: E402
from app.db.storage_service import StorageService  # noqa: E402
from app.domains.ai.service import AIService  # noqa: E402
from app.domains.crm.integration_service import IntegrationService  # noqa: E402
from app.domains.messaging.slack.service import SlackMessagingService  # noqa: E402

setup_connectors()

logger = get_logger(__name__)


async def _process_sqs_message(body_str: str) -> None:  # noqa: PLR0911
    """Parses and processes a single SQS message body."""
    try:
        data = json.loads(body_str)
    except json.JSONDecodeError:
        logger.error("Failed to parse SQS message body: %s", body_str)
        return

    payload = data.get("payload") or data
    workspace_id = data.get("workspace_id")
    corr_id = data.get("corr_id", "sqs-worker")
    view_id = data.get("view_id")
    task_type = data.get("task_type", data.get("type", "handle_interaction"))

    if not workspace_id:
        logger.error("SQS message missing workspace_id: %s", data)
        return

    with log_context(corr_id):
        logger.info("Processing SQS background task for workspace_id=%s", workspace_id)

        # Re-hydrate services
        storage = StorageService(corr_id=corr_id)
        integration_service = IntegrationService(corr_id=corr_id, storage=storage)

        slack_required_tasks = {
            "handle_interaction",
            "handle_shortcut_modal",
            "upgrade_nudge",
            "slack_command",
            "slack_event_link_shared",
            "slack_event_app_home_opened",
            "slack_message_redaction",
            "slack_message_unfurl",
            "slack_threaded_reply_sync",
            "slack_celebration_dm",
            "execute_digest",
        }

        integration = None

        if task_type in slack_required_tasks:
            # Re-hydrate the IntegrationRecord
            # (optional for some tasks like hubspot_webhook)
            integration = await integration_service.get_integration(
                workspace_id=workspace_id, provider=Provider.SLACK
            )
            if not integration:
                logger.error(
                    "Could not find Slack integration for workspace_id=%s, "
                    "aborting Slack task: %s",
                    workspace_id,
                    task_type,
                )
                return

        messaging_service = None
        if integration:
            messaging_service = SlackMessagingService(
                corr_id=corr_id,
                integration_service=integration_service,
                slack_integration=integration,
            )

        interaction_svc = InteractionService(
            ai=None,
            integration_service=integration_service,
            ai_factory=lambda: AIService(corr_id=corr_id),
        )

        # Dispatch
        try:
            if task_type == "handle_interaction":
                if integration is None:
                    logger.error(
                        "Integration unexpectedly None for task_type=%s", task_type
                    )
                    return
                if messaging_service is None:
                    logger.error(
                        "Messaging service unexpectedly None for task_type=%s",
                        task_type,
                    )
                    return
                await interaction_svc.handle_interaction(
                    payload=payload,
                    integration=integration,
                    messaging_service=messaging_service,
                    corr_id=corr_id,
                    view_id=view_id,
                )
            elif task_type == "handle_shortcut_modal":
                if integration is None:
                    logger.error(
                        "Integration unexpectedly None for task_type=%s", task_type
                    )
                    return
                if view_id is None:
                    logger.error(
                        "view_id unexpectedly None for task_type=%s", task_type
                    )
                    return
                await interaction_svc._handle_shortcut_modal_background(
                    payload=payload,
                    integration=integration,
                    view_id=view_id,
                )
            elif task_type == "upgrade_nudge":
                feature_id = data.get("feature_id", "")
                if integration is None:
                    logger.error(
                        "Integration unexpectedly None for task_type=%s", task_type
                    )
                    return
                await interaction_svc._update_view_with_upgrade_nudge(
                    integration=integration,
                    view_id=view_id,
                    feature_id=feature_id,
                )
            elif task_type == "slack_command":
                # Re-hydrate CommandService for slash command execution.
                # The payload contains all parameters needed to resume the search.
                from app.connectors.slack.services.command_service import CommandService

                if integration is None:
                    logger.error(
                        "Integration unexpectedly None for task_type=%s", task_type
                    )
                    return
                cmd_svc = CommandService(
                    corr_id=corr_id,
                    integration=integration,
                    integration_service=integration_service,
                )
                sub_cmd = payload.get("sub_cmd", "universal")
                query = payload.get("query", "")
                channel_id = payload.get("channel_id", "")
                response_url = payload.get("response_url", "")
                object_type = payload.get("object_type", "universal")
                user_id = payload.get("user_id", "")

                if sub_cmd == "report":
                    await cmd_svc._send_report_command(workspace_id, response_url)
                else:
                    await cmd_svc.messaging_service.search_and_send(
                        workspace_id,
                        query,
                        channel_id,
                        response_url,
                        object_type,
                        corr_id,
                        user_id,
                    )
            elif task_type == "hubspot_webhook":
                from app.domains.crm.notification_service import NotificationService

                service = NotificationService(corr_id=corr_id, storage=storage)
                events = data.get("events", [])

                await service.process_event_batch(events, storage)
            elif task_type == "slack_event_link_shared":
                channel = payload.get("channel")
                ts = payload.get("ts")
                links = payload.get("links", [])
                user_id = payload.get("user_id")
                unfurl_id = payload.get("unfurl_id")
                source = payload.get("source")

                if messaging_service is None:
                    logger.error(
                        "Messaging service unexpectedly None for task_type=%s",
                        task_type,
                    )
                    return
                await messaging_service.handle_link_shared(
                    workspace_id=workspace_id,
                    channel=channel,
                    ts=ts,
                    links=links,
                    user_id=user_id,
                    unfurl_id=unfurl_id,
                    source=source,
                )
            elif task_type == "slack_event_app_home_opened":
                user_id = payload.get("user_id")
                if messaging_service is None:
                    logger.error(
                        "Messaging service unexpectedly None for task_type=%s",
                        task_type,
                    )
                    return
                await messaging_service.handle_app_home_opened(user_id=user_id)
            elif task_type == "slack_message_redaction":
                event = payload.get("event")
                if messaging_service is None:
                    logger.error(
                        "Messaging service unexpectedly None for task_type=%s",
                        task_type,
                    )
                    return
                await messaging_service.handle_message_redaction(
                    workspace_id=workspace_id,
                    event=event,
                )
            elif task_type == "slack_message_unfurl":
                channel = payload.get("channel")
                ts = payload.get("ts")
                links = payload.get("links", [])

                if messaging_service is None:
                    logger.error(
                        "Messaging service unexpectedly None for task_type=%s",
                        task_type,
                    )
                    return
                await messaging_service.handle_link_shared(
                    workspace_id=workspace_id,
                    channel=channel,
                    ts=ts,
                    links=links,
                )
            elif task_type == "slack_threaded_reply_sync":
                channel = payload.get("channel")
                thread_ts = payload.get("thread_ts")
                message_ts = payload.get("message_ts")
                text = payload.get("text", "")
                user = payload.get("user", "")

                if messaging_service is None:
                    logger.error(
                        "Messaging service unexpectedly None for task_type=%s",
                        task_type,
                    )
                    return
                await messaging_service.handle_threaded_reply(
                    workspace_id=workspace_id,
                    channel=channel,
                    thread_ts=thread_ts,
                    message_ts=message_ts,
                    text=text,
                    user=user,
                )
            elif task_type == "slack_celebration_dm":
                slack_user_id = payload.get("slack_user_id")
                if messaging_service is None:
                    logger.error(
                        "Messaging service unexpectedly None for task_type=%s",
                        task_type,
                    )
                    return
                await messaging_service.send_celebration_dm(slack_user_id=slack_user_id)
            elif task_type == "execute_digest":
                from app.domains.crm.hubspot.digest_service import DigestService

                digest_id = data.get("digest_id")
                template_id = data.get("template_id")
                target_channel = data.get("target_channel")

                # The integration we fetched earlier is the Slack integration.
                # To execute the digest, we need the HubSpot client.
                hubspot_integration = await integration_service.get_integration(
                    workspace_id=workspace_id, provider=Provider.HUBSPOT
                )
                if not hubspot_integration:
                    logger.error(
                        "No HubSpot integration found for digest_id=%s in workspace=%s",
                        digest_id,
                        workspace_id,
                    )
                    return

                from app.providers.hubspot.client import HubSpotClient

                hubspot_client = HubSpotClient(
                    corr_id=corr_id,
                    access_token=str(hubspot_integration.access_token),
                    refresh_token=hubspot_integration.refresh_token,
                    expires_at=hubspot_integration.expires_at,
                    portal_id=hubspot_integration.portal_id,
                )

                digest_svc = DigestService(storage=storage, corr_id=corr_id)
                blocks_payload = await digest_svc.get_digest_payload(
                    template_id, hubspot_client
                )

                if messaging_service is None:
                    logger.error("Messaging service unexpectedly None for digest task")
                    return

                blocks = blocks_payload.get("blocks", [])

                await messaging_service.send_message(
                    workspace_id=workspace_id,
                    channel=target_channel,
                    text="Your Scheduled Digest",
                    blocks=blocks,
                )
            else:
                logger.warning("Unknown SQS task_type: %s", task_type)

            logger.info(
                "Successfully processed SQS background task (type=%s).", task_type
            )
        except Exception as e:
            logger.exception("Error processing SQS background task: %s", e)
            # Re-raise to let SQS know the message failed (for DLQ/Retries)
            raise


async def _process_batch(records: list[SQSRecord]) -> list[dict[str, str]]:
    """Process a batch of SQS messages concurrently and report failures."""
    failed_items: list[dict[str, str]] = []

    async def _safe_process(record: SQSRecord) -> None:
        try:
            body = record.get("body")
            if body:
                await _process_sqs_message(body)
        except Exception as e:
            logger.exception("Failed processing SQS msg: %s", e)
            message_id = record.get("messageId", "")
            if message_id:
                failed_items.append({"itemIdentifier": message_id})

    await asyncio.gather(*[_safe_process(r) for r in records])
    return failed_items


def handler(event: SQSEvent, context: Any) -> dict[str, Any]:
    """AWS Lambda entry point for SQS events.

    Args:
        event: The SQS event payload.
        context: The Lambda runtime context.

    """
    logger.info("SQS worker triggered with %d records", len(event.get("Records", [])))

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    failed_items = loop.run_until_complete(_process_batch(event.get("Records", [])))

    return {"batchItemFailures": failed_items}
