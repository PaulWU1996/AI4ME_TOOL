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
# Submit a job
curl -X POST "http://localhost:9000/process?path=/app/data/video.mp4"

# Poll for status
curl http://localhost:9000/status/<job_id>

# Test with public URL
curl -X POST "http://localhost:9000/process?path=https://www.w3schools.com/html/mov_bbb.mp4"
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
POST /process (controller)
  ↓
Celery Chord dispatched to Redis:
  Header (parallel):
    ├─ download_file → process_visual
    └─ process_audio
  Callback: finalize_results
  ↓
Worker executes tasks:
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
- **Audio and visual tasks run in parallel** via a Celery chord; `finalize_results` is the chord callback and runs only after both complete.
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
