"""Sentinel Lambda — ML inference handler for REHA Connect.

C-02: This function is invoked asynchronously (InvocationType='Event')
by the main Lambda when a sentiment cache miss occurs.

Expected event payload
----------------------
{
    "texts":   ["raw text 1", "raw text 2", ...],
    "hashes":  ["sha256_hex_1", "sha256_hex_2", ...],
    "corr_id": "optional-trace-id"
}

The handler runs DistilBERT batch inference, then writes each result
to the Supabase ``sentiment_cache`` table using the pre-computed hash
as the primary key.

Environment variables
---------------------
``SUPABASE_URL``         — Supabase project URL
``SUPABASE_SERVICE_KEY`` — Service-role key (bypasses RLS)
``SENTIMENT_MODEL_NAME`` — HuggingFace model ID (default: distilbert-base-...)
``BAKED_MODEL_NAME``     — Set at Docker build time; enables offline loading
``TRANSFORMERS_OFFLINE`` — Set to "1" at Docker build time
"""

from __future__ import annotations

import gc
import logging
import os

logger = logging.getLogger("sentiment_lambda")
logger.setLevel(logging.INFO)

_CACHE_TABLE = "sentiment_cache"
_MODEL_NAME = os.environ.get(
    "SENTIMENT_MODEL_NAME",
    "lxyuan/distilbert-base-multilingual-cased-sentiments-student",
)
_MODEL_DIR = "/app/models"

# Module-level singleton — survives Lambda warm invocations
_classifier = None
_classifier_loaded = False


def _load_classifier():
    global _classifier, _classifier_loaded  # noqa: PLW0603
    if _classifier_loaded:
        return _classifier

    try:
        from typing import Any, cast

        from transformers import pipeline  # type: ignore

        _pipeline_any = cast(Any, pipeline)
        _classifier = _pipeline_any(
            "sentiment-analysis",
            model=_MODEL_NAME,
            device=-1,  # CPU only
            model_kwargs={"cache_dir": _MODEL_DIR},
        )
        logger.info("DistilBERT loaded from %s", _MODEL_DIR)
    except Exception as exc:
        logger.error("Failed to load DistilBERT: %s", exc)
        _classifier = None
    finally:
        _classifier_loaded = True

    return _classifier


def _run_inference(texts: list[str]) -> list[float]:
    """Run batch inference and return normalised scores [-1, 1]."""
    classifier = _load_classifier()
    if not classifier or not texts:
        return [0.0] * len(texts)

    try:
        truncated = [t[:1000] for t in texts if t]
        if not truncated:
            return [0.0] * len(texts)

        results = classifier(truncated)
        scores = []
        idx = 0
        for t in texts:
            if not t:
                scores.append(0.0)
                continue
            res = results[idx]
            label = res.get("label", "NEUTRAL").upper()
            raw = float(res.get("score", 0.0))
            scores.append(
                raw if label == "POSITIVE" else -raw if label == "NEGATIVE" else 0.0
            )
            idx += 1
        return scores
    except Exception as exc:
        logger.error("Inference failed: %s", exc)
        return [0.0] * len(texts)
    finally:
        gc.collect()


def _write_cache(hashes: list[str], scores: list[float]) -> None:
    """Upsert results into Supabase sentiment_cache."""
    try:
        from supabase import create_client  # type: ignore

        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        client = create_client(url, key)

        rows = [{"id": h, "score": s} for h, s in zip(hashes, scores, strict=True)]
        client.table(_CACHE_TABLE).upsert(rows, on_conflict="id").execute()
        logger.info("Wrote %d scores to sentiment_cache", len(rows))
    except Exception as exc:
        logger.error("Failed to write to sentiment_cache: %s", exc)


def lambda_handler(event: dict, context: object) -> dict:  # noqa: ARG001
    """Lambda entry point.

    Args:
        event: Payload from main Lambda (texts + hashes).
        context: Lambda context (unused).

    Returns:
        Minimal status dict (result is persisted to Supabase, not returned).

    """
    corr_id = event.get("corr_id", "unknown")
    texts: list[str] = event.get("texts", [])
    hashes: list[str] = event.get("hashes", [])

    logger.info(
        "Sentiment Lambda invoked: %d texts (corr_id=%s)",
        len(texts),
        corr_id,
    )

    if not texts or not hashes or len(texts) != len(hashes):
        logger.warning("Invalid payload — texts/hashes mismatch or empty")
        return {"status": "error", "reason": "invalid_payload"}

    scores = _run_inference(texts)
    _write_cache(hashes, scores)

    return {"status": "ok", "processed": len(scores)}
