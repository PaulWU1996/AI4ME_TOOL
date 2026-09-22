#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILES="-f docker-compose.yml"

if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null 2>&1; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
  echo "GPU detected: ${GPU_NAME} — starting in GPU mode"
  COMPOSE_FILES="${COMPOSE_FILES} -f docker-compose.gpu.yml"
else
  echo "No GPU detected — starting in CPU mode"
fi

exec docker compose ${COMPOSE_FILES} "$@"
