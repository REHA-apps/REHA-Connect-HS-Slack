from __future__ import annotations  # noqa: D100

from typing import Any

# Simple in-memory de-duplication for unfurls (channel:ts)
LATEST_UNFURLS: set[str] = set()
DEDUPE_LIMIT = 100


def extract_links_from_blocks(blocks: list[dict[str, Any]]) -> list[str]:
    """Recursively extract all type: link URLs from Slack blocks."""
    links = []

    def traverse(item: Any):
        if isinstance(item, list):
            for i in item:
                traverse(i)
        elif isinstance(item, dict):
            if item.get("type") == "link" and item.get("url"):
                links.append(item["url"])
            for value in item.values():
                traverse(value)

    traverse(blocks)
    return list(set(links))


def mark_unfurled(key: str):
    """Mark a message as unfurled and maintain cache size."""
    LATEST_UNFURLS.add(key)
    if len(LATEST_UNFURLS) > DEDUPE_LIMIT:
        # Simple FIFO-ish cleanup
        overflow = list(LATEST_UNFURLS)[-DEDUPE_LIMIT:]
        LATEST_UNFURLS.clear()
        LATEST_UNFURLS.update(overflow)


def is_already_unfurled(key: str) -> bool:
    """Check if this message has already been processed for unfurling."""
    return key in LATEST_UNFURLS
