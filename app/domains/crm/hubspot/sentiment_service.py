# app/domains/crm/hubspot/sentiment_service.py  # noqa: D100
import asyncio
import os

# Transformers will be imported lazily to support environments without ML dependencies
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("hubspot.sentiment")

# C-02: When set, async Lambda pattern is active; local inference is skipped.
# Set to the full Lambda ARN (e.g. arn:aws:lambda:eu-west-1:...:function:reha-connect-sentiment)
_SENTIMENT_LAMBDA_ARN: str | None = os.environ.get("SENTIMENT_LAMBDA_ARN")


class SentimentService:
    """Sovereign ML Sentiment Service.

    Processes text locally to provide sentiment scores without
    external data exposure.
    """

    _instance: "SentimentService | None" = None
    _initialized: bool
    _classifier: "Any | None"
    _init_lock: "Any"
    _async_client: "Any | None"
    corr_id: str

    _model_name = os.environ.get(
        "SENTIMENT_MODEL_NAME",
        "lxyuan/distilbert-base-multilingual-cased-sentiments-student",
    )
    _baked_model_name = os.environ.get("BAKED_MODEL_NAME")

    @property
    def _cache_dir(self) -> str:
        # If the requested model matches the baked model, load from the baked path.
        if self._baked_model_name and self._model_name == self._baked_model_name:
            return "/app/models"
        # Otherwise, fallback to the mutable lambda disk path for downloading.
        return settings.DISK_PATH

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            import threading

            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._classifier = None
            cls._instance._async_client = None
            cls._instance._init_lock = threading.Lock()
        return cls._instance

    async def async_initialize(self):
        """Asynchronously initializes the model in a background thread."""
        if self._initialized:
            return

        loop = asyncio.get_running_loop()
        # Loading heavy models is CPU/IO intensive; run in executor to avoid blocking the loop  # noqa: E501
        await loop.run_in_executor(None, self._initialize)

    def _initialize(self):
        """Loads the model locally in the Frankfurt-hardened perimeter."""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            logger.info(
                "Initializing Sovereign Sentiment Engine (Model: %s, Cache Dir: %s)",
                self._model_name,
                self._cache_dir,
            )

            try:
                # Create cache dir if it doesn't exist
                if not os.path.exists(self._cache_dir):
                    os.makedirs(self._cache_dir)

                # Initialize pipeline (Local CPU)
                # This handles downloading on first run and caching locally
                from transformers import pipeline  # type: ignore

                kwargs: dict[str, Any] = {"cache_dir": self._cache_dir}
                self._classifier = pipeline(  # type: ignore
                    "sentiment-analysis",  # pyright: ignore[reportArgumentType]
                    model=self._model_name,
                    device=-1,  # Force CPU
                    model_kwargs=kwargs,
                )

                # Pre-load NLTK data in a persistent local directory
                # to avoid cold-start latency on first inference.
                try:
                    import nltk

                    # Use a local project directory for NLTK data
                    nltk_data_path = os.path.join(os.getcwd(), "nltk_data")
                    if not os.path.exists(nltk_data_path):
                        os.makedirs(nltk_data_path)

                    if nltk_data_path not in nltk.data.path:
                        nltk.data.path.append(nltk_data_path)

                    try:
                        nltk.data.find("tokenizers/punkt")
                    except LookupError:
                        logger.info("Downloading NLTK punkt to persistent directory...")
                        nltk.download("punkt", download_dir=nltk_data_path, quiet=True)
                except Exception as e:
                    logger.warning("Failed to pre-load NLTK data: %s", e)

                self._initialized = True
                logger.info("Sentiment Engine initialized successfully.")
            except ImportError:
                logger.info(
                    "Sentiment Engine bypassed: transformers package is not installed."
                )
                self._classifier = None
                self._initialized = True  # Prevent repeated attempts
            except Exception as e:
                logger.error("Failed to initialize Sentiment Engine: %s", e)
                self._classifier = None

    async def analyze_sentiment(self, text: str) -> float:
        """Analyzes text sentiment and returns a normalized score.

        Returns:
            float: A score from -1.0 (Very Negative) to 1.0 (Very Positive).
                  Returns 0.0 if analysis fails.
        """  # noqa: D413
        results = await self.analyze_sentiment_batch([text])
        return results[0] if results else 0.0

    async def analyze_sentiment_batch(self, texts: list[str]) -> list[float]:
        """Analyzes multiple texts in a single batch for efficiency.

        C-02: When ``SENTIMENT_LAMBDA_ARN`` is set, delegates to
        ``AsyncSentimentClient`` (cache-first, fire-and-forget pattern).
        Falls back to local DistilBERT inference when unset (dev/offline).
        """
        # --- C-02: Async Lambda fast path ---
        if _SENTIMENT_LAMBDA_ARN:
            from app.domains.ai.async_sentiment import AsyncSentimentClient

            if self._async_client is None:
                self._async_client = AsyncSentimentClient(
                    corr_id=getattr(self, "corr_id", "system")
                )
            else:
                self._async_client.corr_id = getattr(self, "corr_id", "system")
            return await self._async_client.analyze_sentiment_batch(texts)

        # --- Local inference (dev / offline fallback) ---
        if not self._initialized:
            await self.async_initialize()

        if not self._classifier or not texts:
            return [0.0] * len(texts)

        try:
            # Truncate all texts to fit model max length (512 tokens ~ 2000 chars)
            # 1000 chars is safe and covers most HubSpot notes/emails
            truncated_texts = [t[:1000] for t in texts if t]
            if not truncated_texts:
                return [0.0] * len(texts)

            # Process in batch
            results = self._classifier(truncated_texts)

            scores = []
            result_idx = 0
            for t in texts:
                if not t:
                    scores.append(0.0)
                    continue

                res = results[result_idx]
                label = res.get("label", "NEUTRAL").upper()
                score = res.get("score", 0.0)

                if label == "POSITIVE":
                    scores.append(float(score))
                elif label == "NEGATIVE":
                    scores.append(-float(score))
                else:
                    scores.append(0.0)
                result_idx += 1

            return scores
        except Exception as e:
            logger.error("Batch sentiment analysis failed: %s", e)
            return [0.0] * len(texts)
        finally:
            # Force garbage collection after heavy batch processing
            import gc

            gc.collect()
