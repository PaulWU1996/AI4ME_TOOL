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
  └─ yes -> build_workflow_canvas() (dag/compose.py) -> .apply_async()
  ↓
A single Celery chain(*steps), one step per topological layer — sibling
nodes in a layer become a group (so they run in parallel); a group followed
by a later layer is auto-upgraded to a chord (fan-in):
  ├─ download_file: S3 / HTTP(S) / local → /app/tmp/{job_id}/   (immutable)
  ├─ process_visual: starts visualservice container → /analyze (XML→JSON)
  ├─ process_audio: starts audioservice container → /process_audio/
  └─ finalize_results (chord callback, immutable): merge outputs, write task_info.txt, cleanup video
  ↓
GET /status/{job_id} returns results (or callback_url receives them)
```

### Key Design Decisions

- **`download_file` lives in the worker** (not controller) so the downloaded file lands on the shared volume accessible to the analysis tasks.
- **Parallelism is expressed by the DAG structure.** The composer puts sibling nodes of a layer in a `group` — Celery runs them concurrently and auto-upgrades the group to a chord where a later layer fans in. Today only `full_http`'s two http nodes are siblings and run in parallel; the default `full` is a pure chain (its `audio` node depends on `visual`). The aggregate VRAM feasibility caveat that blocked parallel execution under the old engine now applies whenever a layer holds two GPU-backed nodes; nothing enforces host VRAM, so keep GPU nodes in separate layers or on one service unless capacity is known.
- **Scale is per-node, and the queue is the entire serialization story.** There are no locks anywhere. The deployment unit is one worker owning its node's on-demand service containers (`worker/utils.py` `start_service`/`stop_service`). A worker runs one task at a time (`concurrency=1`), and no other worker touches its containers, so a task's start/work/stop bracket can never race another task's. Add capacity by replicating the node — every node runs identical worker code, single host and multi host alike. The one rule that makes this hold: one worker per node's instances; do not point multiple workers at a shared instance (that would need coordination at the service boundary — deliberately out of scope).
- **Service lifecycle is a per-worker concern.** Each task brackets its own service work with `start_service(service)` before and `stop_service(service)` in a `finally` (no-op for keepalive services). `start_service` is probe-first — an already-healthy container (keepalive resident, or still warm from the previous task in a sequential chain) is reused as-is; otherwise it cold-starts via `docker compose up -d` and polls Docker health, and `stop_service` tears it down. There is no lease, no `SERVICE_CONCURRENCY`, no `DEPLOYMENT_MODE`: starting a container and arbitrating between workers were two different concerns, and only the first is needed.
- **Node inputs are declared, not inferred.** The composer never keys off a task's name to decide how to call it. A `call: "kwargs"` node (`download_file`, `finalize_results`) becomes an *immutable* signature carrying a slice of the job context (`path`, `prompts`, `job_id`, `job_type`, `callback_url`) named in its `inject` list (plus static `kwargs`); immutable so a predecessor's payload is never passed positionally. Every other node becomes a positional signature — the old merged-predecessor-payload convention becomes Celery's own argument passing: a node after a single predecessor receives that result; a node after a group receives the aggregated result list. The workflow's summary node is flagged `terminal: true` — that node's output is the `/status` result.
- **`finalize_results` success criteria are declarative and required.** A workflow's finalize node must declare `kwargs.expects` (e.g. `["audio", "visual"]`) — without it there's no way to know what "done" means, and the job fails at the final node even though every other node succeeded.
- **Retries are per node, in the worker, not per Celery task.** The whole workflow runs as one flat Celery chain, so a Celery-level retry of the canvas would re-run every node — including the expensive GPU ones — to recover from one transient download. Instead only `download_file` (the lone node declaring `retries` in the workflows) carries `autoretry_for=(Exception,)` / backoff on its task decorator, so a retry re-runs just that node, never its already-finished successors. The values are hardcoded on the task (`max_retries=3`, `retry_backoff=1.0`, `retry_backoff_max=60.0`) to match the workflow's declared `retries`/`retry_backoff`; the composer does not read retry settings.
- **One `/status` contract.** A workflow's `terminal: true` node's output is what `/status` returns (via `finalize_results`, matching the shape the legacy chains returned). The per-node envelope logging the old engine wrote to `{job_id}/dag_run.json` is gone with the engine — node-level debug detail now lives in the worker logs.
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
- `dag/compose.py` — translates a registered workflow into a Celery `chain` of layer groups (runs in the controller)
- `dag/parser.py` — validates a workflow JSON and exposes its DAG
- `worker/tasks.py` — All processing logic: `download_file`, `process_visual`, `process_audio`, `finalize_results`, `http_call`, plus service management helpers
- `docker-compose.yml` — Single source of truth for service wiring, volumes, and networking

## Testing

No test suite is checked in (the old unit/e2e suites were stripped for a
clean production branch). Verification is done ad-hoc with a throwaway venv
outside the repo:

- **Build smoke tests** — import `dag.compose`, build the canvas for every
  workflow JSON in `workflows/registry.json`, assert each canvas is a chain
  of the expected layer groups; import `controller.main` and `worker.tasks`
  and confirm task registration and resolver wiring.
- **Live fan-in test** — with the installed online Python, run a
  Celery worker against a throwaway Redis and dispatch a
  `chain(group(...), finalize)` canvas generated by the same layering code
  to prove group-parallelism, chord fan-in, and payload propagation.

The mocks encode what `worker/tasks.py` *believes* the service contracts are.
Verifying those against the real services is the one thing local smoke tests
can't do — see `docs/GPU_TEST_RUNBOOK.md` for the session that does, plus
`scripts/gpu_preflight.sh` (read-only environment check) and
`scripts/capture_contracts.py` (captures the real services' responses and
checks every assumption `worker/tasks.py` makes against them).
