# Sentinel Lambda — DistilBERT Sentiment Inference
#
# ARM64 image to match main Lambda architecture (eu-west-1, arm64).
# Build with --provenance=false (same constraint as main Lambda):
#
#   docker buildx build --provenance=false \
#     -f functions/sentiment/Dockerfile \
#     -t reha-connect-sentiment:latest .
#
# Model is baked at build time → no network calls on cold start.

FROM public.ecr.aws/lambda/python:3.12-arm64

WORKDIR ${LAMBDA_TASK_ROOT}

# Install uv — consistent with project toolchain
RUN pip install uv

# Copy dependency manifests (only pyproject.toml + lock needed)
COPY pyproject.toml uv.lock README.md ./

# Install ONLY the [ml] optional group — no main app deps needed
# This keeps the image lean: transformers + torch + supabase client only
RUN uv sync --frozen --no-dev --extra ml

# Add venv site-packages to PYTHONPATH (same pattern as Dockerfile.lambda)
ENV PYTHONPATH=${LAMBDA_TASK_ROOT}/.venv/lib/python3.12/site-packages

# Bake DistilBERT weights into image at build time
# Cold start = loading weights from disk (~1-2s), not downloading (~30-60s)
ARG SENTIMENT_MODEL_NAME="lxyuan/distilbert-base-multilingual-cased-sentiments-student"
RUN .venv/bin/python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download( \
    repo_id='${SENTIMENT_MODEL_NAME}', \
    local_dir='/app/models', \
    ignore_patterns=['*.bin', '*.h5', '*.msgpack'] \
)"

ENV BAKED_MODEL_NAME=${SENTIMENT_MODEL_NAME}
ENV SENTIMENT_MODEL_NAME=${SENTIMENT_MODEL_NAME}

# Offline mode — never attempt external network calls at inference time
ENV TRANSFORMERS_OFFLINE=1

# Copy only the handler — NOT the full app (keeps image small)
COPY functions/sentiment/handler.py ./handler.py

CMD ["handler.lambda_handler"]
