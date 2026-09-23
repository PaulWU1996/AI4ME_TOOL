# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI4ME_PROJ is a containerized media processing pipeline that runs parallel audio and visual analysis on video files, aggregating results via a REST API. Built for BBC creative teams.

## Commands

### Start the full stack
```bash
# Load external service images first (one-time setup)
docker load -i audioservice.tar
docker load -i narrative-api.tar

# Build and run all services (all on-demand services cold-start per job, default)
./scripts/start.sh

# Keep specific GPU services resident across jobs (see "Service Modes" below)
./scripts/start.sh --keepalive audioservice,transcriptservice
```

### Rebuild a single service after code changes
```bash
docker-compose up --build controller
docker-compose up --build worker
```

### Test the API locally
```bash
# Submit a job (JSON body, not query params)
curl -X POST http://localhost:9000/process \
  -H 'Content-Type: application/json' \
  -d '{"path": "/app/data/video.mp4", "job_type": "full"}'

# Register a DAG workflow template (name + version come from the body)
curl -X POST http://localhost:9000/workflows \
  -H 'Content-Type: application/json' \
  --data-binary @workflows/full_1.0.json

# Poll for status
curl http://localhost:9000/status/<job_id>

# Test with public URL
curl -X POST http://localhost:9000/process \
  -H 'Content-Type: application/json' \
  -d '{"path": "https://www.w3schools.com/html/mov_bbb.mp4"}' 
```

### One-time directory setup (required before first run)
```bash
mkdir -p ./shared/api-data
mkdir -p ./data
mkdir -p ./weights/AFWhisper/sound_tower
mkdir -p ./weights/PALUniEncRdFc3Llama31_8B_s2/checkpoint-final
mkdir -p ./weights/models
```

## Architecture

### Services (docker-compose.yml)

| Service | Port | Role |
|---|---|---|
| `redis` | 6379, 8001 | Celery broker + backend; Redis Insight GUI on 8001 |
| `controller` | 9000 | FastAPI — accepts client requests, enqueues Celery tasks |
| `worker` | — | Celery worker — executes analysis tasks, manages on-demand containers |
| `audioservice` | 9002 | On-demand (profile: `on-demand`) — AF-Whisper audio model |
| `visualservice` | 9001 | On-demand (profile: `on-demand`) — Narrative API visual model |
| `autoheal` | — | Restarts unhealthy containers automatically |

### Request Lifecycle

```
POST /process (controller)   # JSON body: {path, job_type, prompts, callback_url, version}
  ↓
job_type must be a registered workflow (workflows/registry.json, added via POST /workflows)
  └─ yes -> tasks.execute_workflow  (DAG engine, dag/engine.py)
  ↓
Engine dispatches nodes in topological order (sequential by default, or parallel
per settings.parallel):
  ├─ download_file: S3 / HTTP(S) / local → /app/tmp/{job_id}/
  ├─ process_visual: starts visualservice container → /analyze (XML→JSON)
  └─ process_audio: starts audioservice container → /process_audio/
  ↓
finalize_results: merge outputs, write task_info.txt, cleanup video
  ↓
GET /status/{job_id} returns results (or callback_url receives them)
```

### Key Design Decisions

- **`download_file` lives in the worker** (not controller) so the downloaded file lands on the shared volume accessible to the analysis tasks.
- **Audio and visual run sequentially today.** The `<em>node</em>` chain is a sequential one by default — nothing in the current default workflows runs them concurrently. `DAGEngine.execute_parallel()` exists and is safe with respect to service occupancy (see leases below), but is not enabled by default: it still needs an aggregate VRAM feasibility check, since a per-service lease does not stop two *different* GPU services being jointly resident beyond host capacity.
- **Service leases (`dag/readiness.py`):** `ensure_ready`/`release` are reference-counted, re-entrant per thread, and capped by a per-service `concurrency` (default 1, overridable with the `SERVICE_CONCURRENCY` env var as JSON). The container starts on the first holder and stops on the last, so the engine's bracket around a node declaring `service` and the task body's own bracket nest into a single start/stop rather than cycling the container twice. A queued waiter inherits a running service instead of it being stopped and cold-started again. These are in-process locks: they cover threads in one worker process, not multiple workers or hosts.
- **Node inputs are declared, not inferred.** The engine never keys off a task's name to decide how to call it. Every node defaults to the merged-predecessor-payload convention; a node opts out with `call: "kwargs"` and reads a slice of the job context (`path`, `prompts`, `job_id`, `job_type`, `callback_url`) named in its `inject` list, with `requires` failing the node fast if any such key is missing. The workflow's summary node is flagged `terminal: true` — that node's output is the `/status` result.
- **`finalize_results` success criteria are declarative and required.** A workflow's finalize node must declare `kwargs.expects` (e.g. `["audio", "visual"]`) — without it there's no way to know what "done" means, and the job fails at the final node even though every other node succeeded.
- **Retries are per node, in the engine, not per Celery task.** `tasks.execute_workflow` is a *single* Celery task covering the whole DAG, so a Celery-level retry would re-run every node — including the expensive GPU ones — to recover from one transient download. Celery's `@app.task(autoretry_for=...)` also never engages on the DAG path at all, because the python driver calls a task's function directly rather than dispatching it. A workflow sets `settings.retries` as a default and overrides it per node with a `retries` attribute; `settings.retry_backoff`/`retry_backoff_max` control the doubling delay. A retry re-attempts the whole bracket including service acquisition, so a node whose service failed to start gets a fresh cold start — and its side effects run again, so only declare retries on nodes that tolerate that.
- **One `/status` contract.** A workflow's `terminal: true` node's merged output is what `/status` returns (via `finalize_results`, matching the shape the legacy chains returned). Jobs additionally write per-node envelopes to `{job_id}/dag_run.json` in the workspace, so node-level detail is available for debugging without changing the wire shape.
- **The visual API key self-heals.** `ensure_api_key()` caches the key in `API_KEY_PATH/api.key`, but `process_visual` regenerates it and retries once on a 401/403. Without that, a service that had forgotten or rotated its keys — its store is `shared/api-data`, which any volume reset wipes — would reject the cached key on every future job forever.
- **`PYTHONPATH=/app` is required in both images.** Celery's app loader puts the working directory on `sys.path` only while importing the app module, then removes it — so any import that happens later (inside a task body) fails without it.
- **On-demand containers:** the worker dynamically starts/stops on-demand services (`audioservice`, `visualservice`, `transcriptservice`) via the Docker Python SDK using the host Docker socket (`/var/run/docker.sock`). Health checks poll for 330s before timing out.
- **Service modes (cold-start vs keepalive):** `scripts/start.sh` (see "Service Modes" below) resolves, per service, whether it cold-starts per job (default, historical behavior) or stays resident ("keepalive") across jobs. The resolved selection is written to `shared/service_modes.json`, which `worker/utils.py` reads once at import — `start_service`/`stop_service` skip the start/stop cycle for any service marked `keepalive`, falling back to normal cold-start recovery if a keepalive container isn't actually healthy.
- **Task reliability settings** in both `controller/main.py` and `worker/tasks.py`: `task_acks_late=True`, `task_reject_on_worker_lost=True`, prefetch=1, visibility timeout=1h. These are required for long-running GPU workloads.
- **Workspace per job:** each job gets `/app/tmp/{job_id}/` on the shared volume. On success, the raw video is deleted; JSON outputs and `task_info.txt` are retained.

### Shared Volume

`./shared` (host) ↔ `/app/tmp` (containers). All services mount this same path so files written by the worker are readable by audio/visual services without copying.

## Environment Variables

**Controller** (`controller/`):
- `REDIS_HOST`, `REDIS_PORT` — broker connection
- `SHARED_PATH` — workspace root (default `/app/tmp`)

**Worker** (`worker/`):
- `REDIS_HOST`, `REDIS_PORT`
- `AUDIO_HOST`, `AUDIO_PORT` — audio service address
- `VISUAL_HOST`, `VISUAL_PORT` — visual service address
- `SHARED_PATH`
- `ADMIN_KEY` — visual service admin password (for API key bootstrap)
- `API_KEY_PATH` — path where the visual API key is cached
- `COMPOSE_PROJECT_DIR`, `COMPOSE_FILE` — docker-compose context for `_compose()` helper
- `SERVICE_MODES_PATH` — path to the resolved cold-start/keepalive selection written by `scripts/start.sh` (default `/app/tmp/service_modes.json`)
- `DEPLOYMENT_MODE` — `single_host` (default; starts containers over the Docker socket) or `multi_host` (checks reachability only)
- `SERVICE_CONCURRENCY` — JSON object of per-service lease limits, e.g. `{"transcriptservice": 2}` (default 1 each)
- `LEASE_TIMEOUT` — seconds a node waits for a busy service before failing (default 3600, matching the Celery visibility timeout)
- `HEALTH_CHECK_TIMEOUT` / `HEALTH_CHECK_INTERVAL` — container health poll budget and cadence (defaults 330s / 60s)
- `VISUAL_REQUEST_TIMEOUT` / `AUDIO_REQUEST_TIMEOUT` / `SCRIPT_REQUEST_TIMEOUT` — how long to wait on a service's HTTP response before giving up (defaults 6000s / 1800s / 1800s). A service that accepts the connection and then never answers blocks the worker for this long

## Service Modes

`scripts/start_services.py` (invoked via `./scripts/start.sh`) decides, per on-demand service, whether it cold-starts per job or stays resident ("keepalive") across jobs:

1. **Registry** (`config/services.json`) declares each service's estimated `vram_mb`/`ram_mb` and whether it supports keepalive. Adding a new on-demand service = one new entry here.
2. **Static pre-check**: sums declared resource estimates for the `--keepalive` selection against host GPU/RAM capacity (`nvidia-smi`, `free -m`); aborts immediately with no containers touched if it's obviously too much.
3. **Measured pass**: starts each keepalive-selected service one at a time, measures its *actual* VRAM delta, and aborts (stopping what it started) if real cumulative usage exceeds a safety margin of host capacity. Successful runs write the measured deltas back into `config/services.json`, so the registry self-calibrates instead of relying on stale estimates.
4. The resolved mode selection is written to `shared/service_modes.json`, which the worker reads to skip start/stop cycling for keepalive services (falling back to normal cold-start recovery if a keepalive container isn't actually healthy).

Services not passed to `--keepalive` default to `coldstart` (today's behavior — started/stopped per job).

## Code Layout

- `controller/main.py` — FastAPI app, `/process` and `/status/{job_id}` endpoints
- `controller/tasks.py` — Celery task *signatures* (producer side, no logic)
- `worker/tasks.py` — All processing logic: `download_file`, `process_visual`, `process_audio`, `finalize_results`, plus service management helpers
- `docker-compose.yml` — Single source of truth for service wiring, volumes, and networking

## Testing

Two suites, neither needing a GPU:

```bash
python3 -m venv venv && ./venv/bin/pip install -r tests/requirements-dev.txt

./venv/bin/python -m pytest                      # 140 unit tests, ~3s, no Docker at all
./venv/bin/python tests/e2e/run_e2e.py           # 13 scenarios, ~2.5 min, real stack + mock services
```

`tests/` unit-tests `dag/` with stub modules installed in `sys.modules` under
the names the worker image exposes them as. `tests/e2e/` runs the real
controller, worker, Redis, Celery and Docker-socket orchestration against
`mocks/service.py`, a stdlib stand-in for the four GPU services. See
`tests/README.md` and `tests/e2e/README.md`.

The mocks encode what `worker/tasks.py` *believes* the service contracts are.
Verifying those against the real services is the one thing neither suite can
do — see `docs/GPU_TEST_RUNBOOK.md` for the session that does, plus
`scripts/gpu_preflight.sh` (read-only environment check) and
`scripts/capture_contracts.py` (captures the real services' responses and
checks every assumption `worker/tasks.py` makes against them).
