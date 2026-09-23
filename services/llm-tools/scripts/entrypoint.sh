#!/usr/bin/env bash
set -euo pipefail

if [ -z "${OLLAMA_MODEL:-}" ]; then
  echo "ERROR: OLLAMA_MODEL environment variable is not set" >&2
  exit 1
fi

# Detect GPU and log which compute backend Ollama will use
if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null 2>&1; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
  echo "GPU detected: ${GPU_NAME} — Ollama will use GPU"
else
  echo "No GPU detected — Ollama will use CPU"
fi

echo "Starting Ollama daemon..."
ollama serve &
OLLAMA_PID=$!

echo "Waiting for Ollama to become ready..."
until curl --silent --fail http://localhost:11434/ > /dev/null 2>&1; do
  sleep 2
done
echo "Ollama is ready."

echo "Verifying model '${OLLAMA_MODEL}' is available on the mounted volume..."
if ! ollama list | grep -qF "${OLLAMA_MODEL}"; then
  echo "WARNING: model '${OLLAMA_MODEL}' not found in mounted volume." >&2
  echo "Attempting to pull (requires internet access)..." >&2
  ollama pull "${OLLAMA_MODEL}" || {
    echo "ERROR: Failed to pull model '${OLLAMA_MODEL}'. Ensure it is pre-populated on the host volume." >&2
    kill "$OLLAMA_PID"
    exit 1
  }
fi
echo "Model '${OLLAMA_MODEL}' is ready."

echo "Starting Uvicorn on port 8000..."
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers "${UVICORN_WORKERS:-1}" \
  --log-level "${UVICORN_LOG_LEVEL:-info}"
