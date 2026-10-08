#!/usr/bin/env bash
set -euo pipefail

if [ -z "${HF_TOKEN:-}" ]; then
  echo "ERROR: HF_TOKEN environment variable is not set" >&2
  echo "       The Gemma weights are gated and cannot be pulled anonymously." >&2
  exit 1
fi

# Report the compute backend so a job that is mysteriously slow can be explained
if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null 2>&1; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
  echo "GPU detected: ${GPU_NAME} — model will load onto the GPU"
else
  echo "WARNING: no GPU detected — the model will load onto CPU and inference will be very slow" >&2
fi

echo "Model: ${MODEL_ID:-google/gemma-4-E4B-it} (audio fallback: ${AUDIO_MODEL_ID:-google/gemma-4-E4B-it})"
echo "Loading model before opening the port; /health stays 503 until it is ready."

# The lifespan hook does the loading, so the port only opens once weights are
# resident. Uvicorn is exec'd so it receives signals directly.
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers "${UVICORN_WORKERS:-1}" \
  --log-level "${UVICORN_LOG_LEVEL:-info}"
