from __future__ import annotations  # noqa: D100

from enum import IntEnum
from typing import TypedDict


class ErrorCode(IntEnum):
    SUCCESS = 200
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    NOT_FOUND = 404
    RATE_LIMIT = 429
    INTERNAL_ERROR = 500
    CUSTOM = 600


class CommandConfig(TypedDict):
    object_type: str
    prefix: str


HS_CALL_OUTCOME_CONNECTED = "f3927361-5ccf-4713-9027-f03f072f889e"

SUB_COMMANDS: dict[str, CommandConfig] = {
    "contact": {"object_type": "contacts", "prefix": "Searching contacts"},
    "lead": {"object_type": "leads", "prefix": "Searching leads"},
    "deal": {"object_type": "deals", "prefix": "Searching deals"},
    "company": {"object_type": "companies", "prefix": "Searching companies"},
    "ticket": {"object_type": "tickets", "prefix": "Searching tickets"},
    "task": {"object_type": "tasks", "prefix": "Searching tasks"},
}

CREATE_RECORD_CALLBACK_ID = "create_hubspot_record"
SLACK_ERROR_ICON = "⚠️"  # Professional warning/error indicator
SLACK_SUCCESS_ICON = "✅"
