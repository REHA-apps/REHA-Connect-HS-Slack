"""Async fire-and-forget Sentiment Lambda client.

C-02: Extracted from the monolithic main Lambda to keep ML inference
out of the critical request path.

Pattern
-------
1. Hash the input text (SHA-256, first 1000 chars).
2. Check ``sentiment_cache`` Supabase table for a cached result.
3. Cache HIT  → return the stored score immediately (0 ms overhead).
4. Cache MISS → async-invoke the Sentinel Lambda (InvocationType='Event',
   no wait) and return 0.0 (neutral) as a fallback.
5. Sentinel Lambda runs DistilBERT inference in background (~2–5 s),
   writes the result to ``sentiment_cache``.
6. Next card refresh reads the real score from cache.

Pre-warm on webhook
-------------------
Call ``request_async()`` inside the HubSpot webhook handler when a
contact/deal/ticket property changes.  By the time the rep opens the
card the score is already cached → first card load shows real score.

Environment variables
---------------------
``SENTIMENT_LAMBDA_ARN``
    When set (production), the async Lambda pattern is active.
    When unset (local dev / CI), this module is a no-op and
    ``SentimentService`` falls back to local transformers inference.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any, cast

import boto3  # type: ignore

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.db.storage_service import StorageService

logger = get_logger("ai.async_sentiment")

# Populated from env at module import — safe to read once.
SENTIMENT_LAMBDA_ARN: str | None = os.environ.get("SENTIMENT_LAMBDA_ARN")

# Supabase table used for cross-Lambda result sharing.
_CACHE_TABLE = "sentiment_cache"

# Neutral fallback score returned while the Lambda runs in background.
_NEUTRAL = 0.0

# Module-level boto3 client — reused across warm invocations (~15-40ms saved/call).
# Safe: boto3 low-level clients are thread-safe for concurrent read operations.
_lambda_client = boto3.client("lambda") if SENTIMENT_LAMBDA_ARN else None


def _hash_text(text: str) -> str:
    """Return a 32-char hex SHA-256 of the first 1000 chars of *text*."""
    return hashlib.sha256(text[:1000].encode()).hexdigest()[:32]


class AsyncSentimentClient:
    """Cache-first, async-invoke wrapper around the Sentinel Lambda.

    Instantiated as a singleton inside ``SentimentService`` when
    ``SENTIMENT_LAMBDA_ARN`` is set.  All methods are safe to call
    concurrently from multiple async tasks.

    Args:
        storage: Optional ``StorageService`` instance. If not provided,
            one is created lazily on first Supabase access.
        corr_id: Correlation ID for tracing.

    """

    def __init__(
        self, storage: StorageService | None = None, corr_id: str = "system"
    ) -> None:
        self.corr_id = corr_id
        self._storage: StorageService | None = storage

    @property
    def storage(self) -> StorageService:
        """Lazy StorageService — created on first Supabase access if not injected."""
        if self._storage is None:
            from app.db.storage_service import StorageService

            self._storage = StorageService(self.corr_id)
        return self._storage

    # ------------------------------------------------------------------
    # Public API (mirrors SentimentService interface)
    # ------------------------------------------------------------------

    async def analyze_sentiment(self, text: str) -> float:
        """Return a cached sentiment score, firing async inference if needed.

        Args:
            text: Raw text to analyse.

        Returns:
            float in [-1.0, 1.0].  Returns 0.0 (neutral) on cache miss
            while the Sentinel Lambda processes in the background.

        """
        results = await self.analyze_sentiment_batch([text])
        return results[0] if results else _NEUTRAL

    async def analyze_sentiment_batch(self, texts: list[str]) -> list[float]:
        """Batch version — cache-first with async fallback per text.

        Args:
            texts: List of raw strings to analyse.

        Returns:
            Parallel list of float scores.  0.0 for any cache miss.

        """
        if not texts:
            return []

        hashes = [_hash_text(t) for t in texts]
        scores: list[float | None] = [None] * len(texts)

        # 1. Batch cache lookup
        cached = await self._get_cached_batch(hashes)
        miss_indices: list[int] = []

        for i, text_hash in enumerate(hashes):
            if text_hash in cached:
                scores[i] = cached[text_hash]
            else:
                miss_indices.append(i)

        # 2. For misses: fire async Lambda invoke (no wait) + set neutral
        if miss_indices:
            miss_texts = [texts[i] for i in miss_indices]
            miss_hashes = [hashes[i] for i in miss_indices]
            await self.request_async(miss_texts, miss_hashes)
            for i in miss_indices:
                scores[i] = _NEUTRAL

        return [s if s is not None else _NEUTRAL for s in scores]

    async def request_async(self, texts: list[str], hashes: list[str]) -> None:
        """Fire-and-forget invoke of the Sentinel Lambda.

        Use this in webhook handlers to pre-warm the cache before the
        rep opens the card.

        Args:
            texts: Raw texts to send for inference.
            hashes: Pre-computed SHA-256 hashes (one per text).

        """
        if not SENTIMENT_LAMBDA_ARN or _lambda_client is None:
            logger.debug(
                "SENTIMENT_LAMBDA_ARN not set — skipping async sentiment invoke"
            )
            return

        payload = json.dumps(
            {
                "texts": texts,
                "hashes": hashes,
                "corr_id": self.corr_id,
            }
        ).encode()

        try:
            import asyncio

            # InvocationType='Event' = fire-and-forget, returns 202 immediately
            await asyncio.to_thread(
                _lambda_client.invoke,
                FunctionName=SENTIMENT_LAMBDA_ARN,
                InvocationType="Event",
                Payload=payload,
            )
            logger.debug(
                "Async sentiment invoked for %d texts (corr_id=%s)",
                len(texts),
                self.corr_id,
            )
        except Exception as exc:
            # Never crash the caller — this is best-effort pre-warming.
            logger.warning("Async sentiment invoke failed: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_cached_batch(self, hashes: list[str]) -> dict[str, float]:
        """Fetch all cached scores for *hashes* in a single Supabase query.

        Args:
            hashes: List of SHA-256 hex IDs.

        Returns:
            Mapping of hash → score for all cache hits.

        """
        if not hashes:
            return {}

        try:
            from app.db.supabase_client import get_async_client

            db = await get_async_client()
            response = (
                await db.table(_CACHE_TABLE)
                .select("id, score")
                .in_("id", hashes)
                .execute()
            )
            rows = cast(list[dict[str, Any]], response.data or [])
            return {str(row["id"]): float(row["score"]) for row in rows}
        except Exception as exc:
            logger.warning("sentiment_cache read failed: %s", exc)
            return {}
