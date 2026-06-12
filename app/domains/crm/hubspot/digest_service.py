"""Service for executing and formatting Scheduled Reporting Digests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.db.storage_service import StorageService
from app.providers.hubspot.client import HubSpotClient

logger = get_logger(__name__)


class DigestService:
    """Executes pre-canned HubSpot searches and formats them into Slack Block Kit digests."""

    def __init__(self, storage: StorageService, corr_id: str | None = None) -> None:
        self.storage = storage
        self.corr_id = corr_id

    async def execute_digest(
        self, digest_id: str, hubspot_client: HubSpotClient, slack_channel_id: str
    ) -> dict[str, Any] | None:
        """Executes a scheduled digest by fetching data and formatting a Slack message."""
        # Note: We query the DB here to ensure we have the latest config if it was modified
        # while waiting in SQS.
        # However, for simplicity, we assume the caller has resolved the integration and
        # passed an authenticated HubSpotClient.

        # Currently, storage_service does not have get_scheduled_digest_by_id,
        # but we can fetch it. For now, let's just implement the template logic.
        pass

    async def get_digest_payload(
        self, template_id: str, hubspot_client: HubSpotClient
    ) -> dict[str, Any]:
        """Fetches and formats the digest payload based on the template_id."""
        match template_id:
            case "stale_deals":
                return await self._generate_stale_deals(hubspot_client)
            case "new_leads":
                return await self._generate_new_leads(hubspot_client)
            case "weekly_conversions":
                return await self._generate_weekly_conversions(hubspot_client)
            case "task_roundup":
                return await self._generate_task_roundup(hubspot_client)
            case _:
                logger.warning("Unknown digest template_id: %s", template_id)
                return {
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "Digest configuration is invalid.",
                            },
                        }
                    ]
                }

    async def _generate_stale_deals(self, client: HubSpotClient) -> dict[str, Any]:
        """Finds deals with no activity in the last 14 days and stages are open."""
        fourteen_days_ago = int(
            (datetime.now(UTC) - timedelta(days=14)).timestamp() * 1000
        )

        query = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "notes_last_updated",
                            "operator": "LT",
                            "value": str(fourteen_days_ago),
                        },
                        {
                            "propertyName": "dealstage",
                            "operator": "NOT_IN",
                            "values": ["closedwon", "closedlost"],
                        },
                    ]
                }
            ],
            "properties": [
                "dealname",
                "amount",
                "dealstage",
                "hubspot_owner_id",
                "notes_last_updated",
            ],
            "limit": 10,
        }

        try:
            resp = await client.request(
                "POST", "/crm/v3/objects/deals/search", json=query
            )
            data = resp
            results = data.get("results", [])

            blocks: list[dict[str, Any]] = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Scheduled Digest: Stale Deals",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Here are the top open deals that haven't had any logged activity in over 14 days:",
                    },
                },
                {"type": "divider"},
            ]

            if not results:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "🎉 Good news! All open deals have recent activity.",
                        },
                    }
                )
                return {"blocks": blocks}

            for i, deal in enumerate(results):
                props = deal.get("properties", {})
                name = props.get("dealname", "Unknown Deal")
                amount = props.get("amount", "0")
                try:
                    amount = f"{float(amount):,.2f}"
                except ValueError:
                    pass

                deal_id = deal.get("id")
                portal_id = client.portal_id
                link = (
                    f"https://app.hubspot.com/contacts/{portal_id}/deal/{deal_id}"
                    if portal_id
                    else None
                )
                name_display = f"<{link}|*{name}*>" if link else f"*{name}*"

                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{name_display}\n_No activity in 14 days_",
                        },
                        "fields": [
                            {
                                "type": "mrkdwn",
                                "text": f"*Amount*\n${amount}",
                            },
                            {
                                "type": "mrkdwn",
                                "text": f"*Stage*\n{props.get('dealstage', 'Open')}",
                            },
                        ],
                    }
                )

                if i < len(results) - 1:
                    blocks.append({"type": "divider"})

            return {"blocks": blocks}
        except Exception as e:
            logger.error("Error executing stale deals digest: %s", e)
            return {
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Failed to generate Stale Deals digest due to a CRM error.",
                        },
                    }
                ]
            }

    async def _generate_new_leads(self, client: HubSpotClient) -> dict[str, Any]:
        """Finds contacts created in the last 7 days."""
        seven_days_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp() * 1000)

        query = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "createdate",
                            "operator": "GTE",
                            "value": str(seven_days_ago),
                        }
                    ]
                }
            ],
            "properties": ["firstname", "lastname", "email", "company"],
            "limit": 10,
        }

        try:
            resp = await client.request(
                "POST", "/crm/v3/objects/contacts/search", json=query
            )
            data = resp
            results = data.get("results", [])

            blocks: list[dict[str, Any]] = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🌱 Scheduled Digest: New Leads",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Here are the newest leads from the past 7 days:",
                    },
                },
                {"type": "divider"},
            ]

            if not results:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "No new leads found in the past 7 days.",
                        },
                    }
                )
                return {"blocks": blocks}

            for i, contact in enumerate(results):
                props = contact.get("properties", {})
                name = (
                    f"{props.get('firstname', '')} {props.get('lastname', '')}".strip()
                    or "Unknown"
                )
                email = props.get("email", "No email")
                company = props.get("company", "No company")

                contact_id = contact.get("id")
                portal_id = client.portal_id
                link = (
                    f"https://app.hubspot.com/contacts/{portal_id}/contact/{contact_id}"
                    if portal_id
                    else None
                )
                name_display = f"<{link}|*{name}*>" if link else f"*{name}*"

                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": name_display},
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Email*\n{email}"},
                            {"type": "mrkdwn", "text": f"*Company*\n{company}"},
                        ],
                    }
                )
                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": "Created within the last 7 days"}
                        ],
                    }
                )

                if i < len(results) - 1:
                    blocks.append({"type": "divider"})

            return {"blocks": blocks}
        except Exception as e:
            logger.error("Error executing new leads digest: %s", e)
            return {
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Failed to generate New Leads digest due to a CRM error.",
                        },
                    }
                ]
            }

    async def _generate_weekly_conversions(
        self, client: HubSpotClient
    ) -> dict[str, Any]:
        """Finds deals won in the last 7 days."""
        seven_days_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp() * 1000)

        query = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "closedate",
                            "operator": "GTE",
                            "value": str(seven_days_ago),
                        },
                        {
                            "propertyName": "dealstage",
                            "operator": "EQ",
                            "value": "closedwon",
                        },
                    ]
                }
            ],
            "properties": ["dealname", "amount", "hubspot_owner_id", "closedate"],
            "limit": 10,
            "sorts": [{"propertyName": "amount", "direction": "DESCENDING"}],
        }

        try:
            resp = await client.request(
                "POST", "/crm/v3/objects/deals/search", json=query
            )
            data = resp
            results = data.get("results", [])

            blocks: list[dict[str, Any]] = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🏆 Scheduled Digest: Weekly Conversions",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Here are the top deals won in the past 7 days:",
                    },
                },
                {"type": "divider"},
            ]

            if not results:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "No deals were won in the past 7 days. Better luck next week!",
                        },
                    }
                )
                return {"blocks": blocks}

            for i, deal in enumerate(results):
                props = deal.get("properties", {})
                name = props.get("dealname", "Unknown Deal")
                amount = props.get("amount", "0")
                try:
                    amount = f"{float(amount):,.2f}"
                except ValueError:
                    pass

                deal_id = deal.get("id")
                portal_id = client.portal_id
                link = (
                    f"https://app.hubspot.com/contacts/{portal_id}/deal/{deal_id}"
                    if portal_id
                    else None
                )
                name_display = f"<{link}|*{name}*>" if link else f"*{name}*"

                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{name_display}",
                        },
                        "fields": [
                            {
                                "type": "mrkdwn",
                                "text": f"*Won Amount*\n${amount}",
                            },
                            {
                                "type": "mrkdwn",
                                "text": f"*Owner ID*\n{props.get('hubspot_owner_id', 'Unassigned')}",
                            },
                        ],
                    }
                )

                if i < len(results) - 1:
                    blocks.append({"type": "divider"})

            return {"blocks": blocks}
        except Exception as e:
            logger.error("Error executing weekly conversions digest: %s", e)
            return {
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Failed to generate Weekly Conversions digest due to a CRM error.",
                        },
                    }
                ]
            }

    async def _generate_task_roundup(self, client: HubSpotClient) -> dict[str, Any]:
        """Finds overdue and upcoming tasks for the user."""
        now = int(datetime.now(UTC).timestamp() * 1000)

        query = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "hs_task_status",
                            "operator": "NEQ",
                            "value": "COMPLETED",
                        }
                    ]
                }
            ],
            "properties": [
                "hs_task_subject",
                "hs_task_priority",
                "hs_timestamp",
                "hs_task_status",
            ],
            "limit": 10,
            "sorts": [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}],
        }

        try:
            resp = await client.request(
                "POST", "/crm/v3/objects/tasks/search", json=query
            )
            data = resp
            results = data.get("results", [])

            blocks: list[dict[str, Any]] = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📅 Scheduled Digest: Task Roundup",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Here are your pending tasks:",
                    },
                },
                {"type": "divider"},
            ]

            if not results:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "🎉 You have no pending tasks!",
                        },
                    }
                )
                return {"blocks": blocks}

            for i, task in enumerate(results):
                props = task.get("properties", {})
                subject = props.get("hs_task_subject", "Unknown Task")
                priority = props.get("hs_task_priority", "NONE")
                timestamp = props.get("hs_timestamp")

                status_emoji = "🔴 Overdue"
                due_date_str = "No Date"
                if timestamp:
                    try:
                        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        ts_ms = int(ts.timestamp() * 1000)
                        if ts_ms > now:
                            status_emoji = "🟢 Upcoming"
                        due_date_str = ts.strftime("%b %d, %Y")
                    except ValueError:
                        pass

                task_id = task.get("id")
                portal_id = client.portal_id
                link = (
                    f"https://app.hubspot.com/contacts/{portal_id}/task/{task_id}"
                    if portal_id
                    else None
                )
                subject_display = f"<{link}|*{subject}*>" if link else f"*{subject}*"

                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": subject_display},
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Priority*\n{priority}"},
                            {"type": "mrkdwn", "text": f"*Status*\n{status_emoji}"},
                        ],
                    }
                )
                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": f"Due: {due_date_str}"}
                        ],
                    }
                )

                if i < len(results) - 1:
                    blocks.append({"type": "divider"})

            return {"blocks": blocks}
        except Exception as e:
            logger.error("Error executing task roundup digest: %s", e)
            return {
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Failed to generate Task Roundup digest due to a CRM error.",
                        },
                    }
                ]
            }
