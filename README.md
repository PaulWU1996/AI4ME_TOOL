# CONTAINERIZED MEDIA PROCESSING PIPELINE FOR BBC

---

## 1. PROJECT OVERVIEW

A distributed media processing tool designed for BBC creative teams. It integrates audio and visual modality understanding algorithms into a scalable containerized architecture using **FastAPI**, **Celery**, and **Redis**.

---

## 2. DIRECTORY STRUCTURE

```bash
.
|-- controller
|   |-- main.py         # FastAPI application & API endpoints
|   |-- downloader.py   # Support for S3, URL, and Local file ingestion
|   |-- tasks.py        # Celery task signatures for Producer side
|   |-- Dockerfile      # Python 3.10-slim base
|-- worker
|   |-- tasks.py        # Analysis logic & automated cleanup for Consumer side
|   |-- Dockerfile      # Celery worker configuration
|-- docker-compose.yml  # Service orchestration for Redis, Controller, Worker
|-- shared              # Shared volume for temporary media processing
|   |-- api-data        # Folder storing Sample API request payloads for testing
|-- weights             # Placeholder for AI model weights
|   |-- AFWhisper       
|   |   |-- sound_tower
|   |-- PALUniEncRdFc3Llama31_8B_s2
|   |   |-- checkpoint-final
|   |-- models
|   |-- ollama          # Ollama model weights for transcript service
|-- README.md            # Project documentation (this file) 
```

---

## 3. KEY FEATURES

- **Universal Ingestion**  
  Support for S3, HTTP/HTTPS, and local file paths  

- **Multiple Job Types**  
  Supports `full`, `audio_only`, `visual_only`, and `summarise` pipelines via a single endpoint  

- **Automated Cleanup**  
  The worker automatically deletes `/app/tmp/<job_id>` once processing is finalized to prevent disk overflow  

- **Industrial Stability**  
  Optimized with:
  - Visibility timeout (1 hour)  
  - Strict concurrency limits  
  - Late acknowledgments  
  - Designed for long-running (30min+) AI workloads on GPUs (e.g., A100)

- **Containerized Architecture**  
  Each component (Controller, Worker, Redis) runs in its own Docker container for modularity and scalability

- **Automatic First Aid**  
  The system is designed to handle and recover from common failure scenarios (e.g., task timeouts, worker crashes) without manual intervention

---

## 4. GETTING STARTED

### Prerequisites

- Docker  
- Docker Compose  
- (Optional) AWS credentials for S3 access  

### Deployment

1. Copy the weights folder to the appropriate location following the structure outlined above. (Note: The shared volume and the api-data folder are shown above but you need to mannually create them and put the corresponding place following the stracture above.)

2. Load the Docker images for the audio and visual services, respectively:

```bash
docker load -i audioservice.tar
docker load -i narrative-api.tar
```
- The narrative-api.tar is the image for the visual service and produced by Asmar. (No test on his image yet, but it should work as long as the entrypoint is correct and the model weights are in place).

- The audioservice.tar is the image for the audio service and produced by Tony (audio llm) and Paul (plugin wrapper and docker design). It has been tested and works with the current codebase.

3. Load the Docker image for the transcript service:

```bash
docker load -i transcriptservice.tar
```

4. Start the entire stack using the resource-aware start script:
```bash
# All on-demand services cold-start per job (default, historical behavior)
./scripts/start.sh

# Keep specific GPU services resident across jobs (see "Service Modes" below)
./scripts/start.sh --keepalive audioservice,transcriptservice
```
There will be several services starting up, including Redis, the Controller API, and the Worker. Services not selected with `--keepalive` start on-demand when a job requires them and stop afterward. The Controller and Worker will connect to Redis for task orchestration.

Once compose completed, the TOOL API will be available at:

```
http://localhost:9000
```

---

### Service Modes: Cold-start vs Keepalive

By default, `audioservice`, `visualservice`, and `transcriptservice` are **cold-started**: the worker starts each container only when a job needs it and stops it again once the job finishes. This keeps host resource usage minimal but pays a model-load/health-check cost (up to ~5.5 minutes) on every single job.

If your host has enough spare GPU/RAM capacity, you can instead keep one or more of these services **resident** across jobs ("keepalive"), avoiding the per-job startup cost.

```bash
# Keep audioservice and transcriptservice running; visualservice still cold-starts per job
./scripts/start.sh --keepalive audioservice,transcriptservice
```

How it works (`scripts/start_services.py`, invoked by `scripts/start.sh`):

1. **Registry** — `config/services.json` declares each on-demand service's estimated `vram_mb`/`ram_mb` and whether it supports keepalive. Add a new on-demand service by adding one entry here.
2. **Static pre-check** — before touching Docker, sums the declared resource estimates for your `--keepalive` selection and compares against detected host GPU/RAM capacity (`nvidia-smi`, `free -m`). If the selection is obviously too large, the script aborts immediately with no containers started.
3. **Measured pass** — starts each keepalive-selected service one at a time, waits for it to become healthy, and measures its *actual* VRAM delta. If real cumulative usage would exceed a safety margin of host capacity, the script stops what it started and aborts, reporting the real numbers. On success, the measured values are written back into `config/services.json` so future runs use observed reality instead of stale estimates.
4. The resolved mode selection is written to `shared/service_modes.json`. The worker reads this file once at startup — `start_service`/`stop_service` skip the start/stop cycle for any service marked `keepalive`, and automatically fall back to normal cold-start recovery if a keepalive container isn't actually healthy when a job needs it.

Services omitted from `--keepalive` default to `coldstart` (today's default behavior).

**Note:** `scripts/start_services.py` runs on the host (not inside a container) — it needs `docker`, the `docker` Python package, and (for GPU services) `nvidia-smi` available on the host running `docker compose`.

---

### API Usage

#### Start Processing

- **Endpoint:** `POST /process` (JSON body)

```bash
# Full pipeline (default)
curl -X POST "http://localhost:9000/process" \
  -H "Content-Type: application/json" \
  -d '{"path": "/app/data/video.mp4"}'

# Runs a specific registered workflow (register first, see §5)
curl -X POST "http://localhost:9000/process" \
  -H "Content-Type: application/json" \
  -d '{"path": "/app/data/video.mp4", "job_type": "full"}'

# With callback and prompts
curl -X POST "http://localhost:9000/process" \
  -H "Content-Type: application/json" \
  -d '{"path": "https://example.com/video.mp4", "job_type": "full", "callback_url": "https://your-server/callback", "prompts": "describe the scene"}'
```

**Request fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `path` | string | required | Media source: local path, HTTP/HTTPS URL, or S3 URI |
| `job_type` | string | `"full"` | Name of a registered DAG workflow (see `POST /workflows`) |
| `prompts` | string | null | Custom analysis prompt passed to services |
| `callback_url` | string | null | Webhook to POST results to on completion |

Returns a `job_id` immediately. If `callback_url` is provided, results are also POSTed there when complete.

Once the request is received, the Controller will:
1. Look up the registered workflow for `job_type` (optionally pinned by `version`) and generate a unique `job_id`
2. Translate the workflow into a single Celery `chain` of layer groups (`dag/compose.py`) and enqueue it
3. Return `{"status": "submitted", "job_id": "...", "job_type": "..."}` immediately

Note: The outputs (audio and visual analysis results, as well the task info) will be saved in the shared volume workspace under `/app/tmp/{job_id}/` before being returned to the client or sent to the callback URL. You can also check the outputs on the host machine by navigating to the corresponding directory in the shared volume (e.g., `/your/path/to/shared_vol/{job_id}/`) while the processing is still running or after it has completed. This can be useful for debugging or verifying intermediate results.


---

#### Check Status & Get Results

- **Endpoint:** `GET /status/{job_id}`

```bash
curl http://localhost:9000/status/<your_job_id>
```

Returns combined JSON results once `is_ready` is `true`.

---

## 5. TASK ORCHESTRATION DETAILS

Jobs dispatch through **registered DAG workflows**. `POST /workflows` validates and permanently registers a workflow template (name + version from its body); a subsequent `POST /process` with `job_type=<name>` runs the workflow's `latest` version (or the one pinned by `version`) by translating it into a single Celery canvas — a `chain` of topological layers, sibling nodes as a parallel `group` (`dag/compose.py`, running in the controller).

```bash
# Register a workflow template, then run it
curl -X POST http://localhost:9000/workflows \
  -H 'Content-Type: application/json' \
  --data-binary @workflows/full_1.0.json

curl -X POST http://localhost:9000/process \
  -H 'Content-Type: application/json' \
  -d '{"path": "/app/data/video.mp4", "job_type": "full"}'
```

Eight workflow templates ship pre-registered in `workflows/registry.json`
(mount `./workflows` into the containers at `/app/workflows`):

- `full` — `download` → `visual` → `audio` → `final` — expects audio + visual
- `full_http` — `download` → parallel http `visual`/`audio`, no terminal node
- `audio_only` — `download` → `audio` → `final`
- `visual_only` — `download` → `visual` → `final`
- `tagging` — `download` → `transcript` → `tagging` → `final`
- `summarise` — `download` → `transcript` → `summarise` → `tagging` → `final`
- `speaker-extent-summarise` — adds `extent` (speaker) before `transcript`
- `utterance-extent-summarise` — adds `extent` (segment) before `transcript`

A workflow template declares its nodes with `id`, `task` (a function in `worker/tasks.py`, or the HTTP driver via `url`), `depends_on`, and optional `service` / `retries`. Node input is declarative, never keyed on a task's name:

| Node attribute | Meaning |
|---|---|
| `call: "kwargs"` | Node reads a job-context slice instead of the merged predecessor payload (`inject` below). Default is the merged-payload convention. |
| `inject: [...]` | For `call: "kwargs"` nodes — which job-context keys (`path`, `prompts`, `job_id`, `job_type`, `callback_url`, ...) to pass in; job context wins over template `kwargs` defaults. |
| `requires: [...]` | For `call: "kwargs"` nodes — keys that must resolve at pre-flight, or the job fails fast. |
| `terminal: true` | The node whose output is the job's `/status` result (a workflow's summary/finalize step). |
| `service` / `retries` | On-demand service this node's worker starts/stops around the node (per-worker lifecycle manager in `worker/utils.py`); node-level retry count (honored on `download_file` by its task-level `autoretry_for`). |

For example, `workflows/full_1.0.json`:

```json
{ "id": "download", "task": "download_file", "call": "kwargs",
  "inject": ["path", "prompts", "job_id"], "requires": ["path"] },
{ "id": "visual", "task": "process_visual", "service": "visualservice",
  "depends_on": ["download"] },
{ "id": "audio", "task": "process_audio", "service": "audioservice",
  "depends_on": ["visual"] },
{ "id": "final", "task": "finalize_results", "call": "kwargs",
  "inject": ["job_id", "job_type", "callback_url"], "terminal": true,
  "kwargs": { "expects": ["audio", "visual"] },
  "depends_on": ["visual", "audio"] }
```

`finalize_results` merges the job's output JSON files from the shared volume, writes `task_info.txt`, deletes the raw video on success, and optionally POSTs to `callback_url`. Its success criteria are declarative via `kwargs.expects`; a workflow must declare `expects`, or the job fails at the final node.

The full workflow is demonstrated in the following diagram:

```
┌─────────────────────────────────────────────────────────────────────┐
│                           CLIENT                                    │
│  POST /process  {path, job_type, prompts, callback_url}             │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ returns immediately
                            │ {"status":"submitted","job_id":"..."}
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     CONTROLLER  :9000                               │
│  FastAPI — looks up registered workflow, generates job_id, builds   │
│  the Celery canvas (dag/compose.py), enqueues via .apply_async()    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ enqueue chain(*layers)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     REDIS  :6379                                    │
│  Celery broker + result backend                                     │
│                                                                     │
│  Queue:  [job_A] [job_B] [job_C] ...   ← jobs parallel in queue    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ worker picks up one job at a time
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      WORKER  (Celery)                               │
│                                                                     │
│  One chain(*steps), one step per topological layer. Sibling nodes   │
│  in a layer form a group and run in parallel (full_http's two http  │
│  nodes today); a group followed by a later layer is auto-upgraded   │
│  to a chord (fan-in). The default full workflow is a pure chain:    │
│  download → visual → audio → final.                                 │
│                                                                     │
│  ① download_file   (immutable kwargs)                              │
│     S3 / HTTP(S) / local → /app/tmp/{job_id}/{filename}            │
│            │                                                        │
│            ▼                                                        │
│  ② process_visual  (starts visualservice → /analyze)                │
│     saves {name}_visual_output.json                                 │
│            │                                                        │
│            ▼                                                        │
│  ③ process_audio  (starts audioservice → /process_audio/)           │
│     saves {name}_audio_output.json                                  │
│            │                                                        │
│            ▼                                                        │
│  ④ finalize_results  (immutable, terminal node)                    │
│     merges audio + visual JSON from shared volume                   │
│     evaluates success per kwargs.expects                            │
│     writes task_info.txt                                            │
│     deletes raw video on success                                    │
│     POST callback_url (if provided)                                 │
│     stores final_output in Redis under job_id                       │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
              ┌─────────────┴──────────────┐
              │                            │
              ▼                            ▼
  GET /status/{job_id}           callback_url  ← POST final_output
  polls Redis AsyncResult
  returns data when ready

┌─────────────────────────────────────────────────────────────────────┐
│                   SHARED VOLUME  ./shared → /app/tmp                │
│                                                                     │
│  /app/tmp/{job_id}/                                                 │
│    ├── video.mp4                  (deleted on success)              │
│    ├── video_visual_output.json                                     │
│    ├── video_audio_output.json                                      │
│    └── task_info.txt                                                │
└─────────────────────────────────────────────────────────────────────┘
```

Note: The /shared/{job_id}/ directory will not be automatically deleted by orchestrator (reddis, controller, worker and autoheal). The reason is that we want to keep the output json files for the client and wait confirmation of the final export method (e.g. push to database, save local file, send to callback url).
---

## 6. INDIVIDUAL SERVICE COMPONENT TESTING

For the purpose of testing individual service components (audio and visual service) without Docker Compose, you can use the following commands. 

### 6.1  Audio Service Testing

Start the audio service container with the appropriate environment variables and volume mounts:
```bash
docker run -d \
  --name audioservice \
  --gpus all \
  -e MODEL_PATH="/app/weights/checkpoint-final" \
  -e SHARED_PATH="/app/tmp" \
  -v /your/path/to/PALUniEncRdFc3Llama31_8B_s2/checkpoint-final:/app/weights/checkpoint-final \
  -v /your/path/to/shared_vol:/app/tmp \
  -p 9002:8000 \
  -w /app \
  audioservice:latest \
  python3 -m uvicorn src.audio_entry:app --host 0.0.0.0 --port 8000
```
Once service is ready, send a test request to the audio service:
```bash
curl -X POST "http://localhost:9002/process?path=https://www.w3schools.com/html/mov_bbb.mp4"
```
You can also check the service status by sending a GET request to the status endpoint:
```bash
curl -X POST "http://localhost:9002/health"
```

### 6.2  Visual Service Testing

Docker running command for the visual service.
```bash
# Load image
docker load -i narrative-api.tar

# Create data directory for API keys
mkdir -p ~/narrative-api-data

# Run
docker run -d \
  --runtime=nvidia \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e ADMIN_KEY=your-admin-key \
  --name narrative-api \
  -v /path/to/weights:/app/models \
  -v ~/narrative-api-data:/app/data \
  -p 8000:8000 \
  narrative-api

# Check it is running (wait 1-2 min for model to load)
curl http://localhost:8000/health
```

Once the service is running, you can generate the api-key following:
```bash
curl -X POST http://localhost:8000/api/keys/generate \
  -H "X-Admin-Key: change-me-in-production" \
  -H "Content-Type: application/json" \
  -d '{"client_name": "client-ai4me", "expires_in_days": 365}'
```

Save the api_key value from the response — it is shown only once.

And then you can send a test request to the visual service:
```bash
Analyse a video
curl -X POST http://localhost:8000/analyze \
  -H "X-API-Key: sk_your-api-key" \
  -F "video=@/path/to/video.mp4" \
  --output result.xml
```

Supported formats: mp4, avi, mov, mkv, webm

---

### Appendix: Commands for 
reddis start command:
```
apptainer run --env LC_ALL=C redis.sif \
  redis-server \
  --port 6379 \
  --protected-mode no \
  --save "" \
  --appendonly no \
  --dir /tmp \
  --logfile ""
```

redis check command:
```
apptainer exec redis.sif redis-cli -p 6379 ping
```

controller start command:
```
apptainer exec \
  --env SHARED_PATH="/mnt/fast/nobackup/scratch4weeks/pw0036/Compose/temp_data" \
  --env REDIS_HOST="127.0.0.1" \
  --pwd /app \
  controller.sif \
  uvicorn main:app --host 0.0.0.0 --port 9000
```

worker start command:
```
apptainer exec \
  --env SHARED_PATH="/mnt/fast/nobackup/scratch4weeks/pw0036/Compose/temp_data" \
  --env REDIS_HOST="127.0.0.1" \
  --pwd /app \
  worker.sif \
  celery -A tasks worker --loglevel=info --pool=solo
```

curl test command:
```
curl -X POST "http://127.0.0.1:9000/process" \
  -H "Content-Type: application/json" \
  -d '{"path": "https://www.w3schools.com/html/mov_bbb.mp4"}'
```

```
apptainer run --nv --env MODEL_PATH="/mnt/fast/nobackup/scratch4weeks/pw0036/Compose/weights/PALUniEncRdFc3Llama31_8B_s2/checkpoint-final" --env SHARED_PATH="/mnt/fast/nobackup/scratch4weeks/pw0036/samples"  --pwd app audioservice@1.sif python3 -m uvicorn src.audio_entry:app --host 0.0.0.0 --port 8000
```