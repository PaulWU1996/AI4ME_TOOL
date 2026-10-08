#!/usr/bin/env bash
# Pre-flight checks before the first real-GPU run. Read-only: starts nothing,
# builds nothing, changes nothing. Run from the repo root.
#
#   ./scripts/gpu_preflight.sh
#
# Exit code is 0 only if every BLOCKER passes. WARNs are judgement calls.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

pass=0; warn=0; fail=0
ok()    { printf '  \033[32mOK\033[0m    %s\n' "$1"; pass=$((pass+1)); }
warned(){ printf '  \033[33mWARN\033[0m  %s\n' "$1"; warn=$((warn+1)); }
bad()   { printf '  \033[31mBLOCK\033[0m %s\n' "$1"; fail=$((fail+1)); }
section(){ printf '\n\033[1m%s\033[0m\n' "$1"; }

section "Host"
if command -v nvidia-smi >/dev/null 2>&1; then
  ok "nvidia-smi present: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
  bad "nvidia-smi not found — the GPU services cannot start"
fi

if docker info 2>/dev/null | grep -qi "runtimes:.*nvidia"; then
  ok "nvidia container runtime registered with docker"
else
  warned "nvidia runtime not listed by 'docker info' — GPU passthrough may fail"
fi

docker version >/dev/null 2>&1 && ok "docker daemon reachable" || bad "docker daemon unreachable"
docker compose version >/dev/null 2>&1 && ok "docker compose plugin present" || bad "docker compose plugin missing"

if [ -S /var/run/docker.sock ]; then
  ok "/var/run/docker.sock exists (the worker mounts this to start services)"
else
  bad "/var/run/docker.sock missing — on-demand service start will fail"
fi

section "Environment"
missing_env=0
for var in AWS_ACCOUNT_ID AWS_REGION ECR_REPO; do
  if grep -q "^${var}=" .env 2>/dev/null; then ok ".env defines ${var}"; else bad ".env is missing ${var}"; missing_env=1; fi
done

# ADMIN_KEY is interpolated into BOTH the worker and visualservice. If the var
# is absent, compose sets it to "" on both sides -- which happens to match, so
# the handshake may still work, but nothing here is deliberate.
if grep -q "^AI4ME_ADMIN_PASSWORD=" .env 2>/dev/null; then
  ok ".env defines AI4ME_ADMIN_PASSWORD"
else
  warned "AI4ME_ADMIN_PASSWORD is NOT in .env -> ADMIN_KEY resolves to \"\" for worker AND visualservice."
  warned "  They still match, so /generate may work, but an empty X-Admin-Key header is fragile."
  warned "  Set it in .env on both sides before trusting the key handshake."
fi

section "Images"
for image in audioservice:latest narrative-api:latest; do
  if docker image inspect "$image" >/dev/null 2>&1; then
    ok "$image loaded"
  else
    bad "$image not loaded — run: docker load -i ${image%%:*}.tar"
  fi
done

section "Directories the compose file mounts"
for d in ./shared ./shared/api-data ./data ./workflows ./weights; do
  [ -d "$d" ] && ok "$d exists" || bad "$d missing — see CLAUDE.md 'One-time directory setup'"
done

section "Code state"
if grep -q "PYTHONPATH=/app" worker/Dockerfile && grep -q "PYTHONPATH=/app" controller/Dockerfile; then
  ok "PYTHONPATH fix present in both Dockerfiles"
else
  bad "PYTHONPATH missing from a Dockerfile — the DAG path will not run"
fi

# The published ECR images predate that fix. A plain 'up -d' would pull them.
warned "You MUST build locally: 'docker-compose up --build controller worker'."
warned "  The ECR images predate the PYTHONPATH fix and will fail every DAG job."

for var in HEALTH_CHECK_TIMEOUT HEALTH_CHECK_INTERVAL VISUAL_REQUEST_TIMEOUT AUDIO_REQUEST_TIMEOUT SCRIPT_REQUEST_TIMEOUT; do
  if grep -q "^${var}=" .env 2>/dev/null || [ -n "${!var:-}" ]; then
    warned "${var} is overridden — the mock stack used short values; real GPU services need the defaults"
  fi
done
[ "$warn" -eq 0 ] && true
ok "timeout overrides checked (defaults: health 330s/60s, requests 6000s/1800s)"

if grep -q "concurrency=1" docker-compose.yml; then
  ok "worker runs --concurrency=1 (service leases are in-process and assume this)"
else
  bad "worker concurrency is not 1 — the service leases do not hold across processes"
fi

section "Compose resolves"
if docker compose --env-file .env config >/dev/null 2>&1; then
  ok "docker compose config parses"
  admin=$(docker compose --env-file .env config 2>/dev/null | grep -m1 'ADMIN_KEY:' | sed 's/.*ADMIN_KEY: //')
  [ "$admin" = '""' ] && warned "ADMIN_KEY resolves to an empty string (see above)" || ok "ADMIN_KEY resolves to a non-empty value"
else
  bad "docker compose config failed — fix interpolation before starting anything"
fi

printf '\n\033[1mSummary:\033[0m %d ok, %d warn, %d blocking\n' "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] || printf 'Resolve the blockers before starting the stack.\n'
exit $(( fail > 0 ? 1 : 0 ))
