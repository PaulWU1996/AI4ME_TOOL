#!/usr/bin/env bash
# Entrypoint for starting AI4ME_PROJ with resource-aware keepalive selection.
# Usage: ./scripts/start.sh --keepalive audioservice,transcriptservice
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 scripts/start_services.py "$@"
