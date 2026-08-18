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

# Audio only
curl -X POST "http://localhost:9000/process" \
  -H "Content-Type: application/json" \
  -d '{"path": "/app/data/video.mp4", "job_type": "audio_only"}'

# Summarise (transcript analysis)
curl -X POST "http://localhost:9000/process" \
  -H "Content-Type: application/json" \
  -d '{"path": "http://localhost:8000/49794ede-.../transcript?start=1781183300&end=1781183700", "job_type": "summarise", "prompts": "summarise key summarise"}'

# With callback and prompts
curl -X POST "http://localhost:9000/process" \
  -H "Content-Type: application/json" \
  -d '{"path": "https://example.com/video.mp4", "job_type": "full", "callback_url": "https://your-server/callback", "prompts": "describe the scene"}'
```

**Request fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `path` | string | required | Media source: local path, HTTP/HTTPS URL, or S3 URI |
| `job_type` | string | `"full"` | `full` / `audio_only` / `visual_only` / `summarise` |
| `prompts` | string | null | Custom analysis prompt passed to services |
| `callback_url` | string | null | Webhook to POST results to on completion |

Returns a `job_id` immediately. If `callback_url` is provided, results are also POSTed there when complete.

Once the request is received, the Controller will:
1. Validate `job_type` and generate a unique `job_id`
2. Build the appropriate Celery chain and enqueue it
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

The pipeline uses **Celery Chains** — tasks within a job execute sequentially. Multiple jobs run in parallel across the queue.

**Job types and their chains:**

| `job_type` | Chain | Success condition |
|---|---|---|
| `full` | `download_file` → `process_visual` → `process_audio` → `finalize_results` | both audio + visual present |
| `audio_only` | `download_file` → `process_audio` → `finalize_results` | audio present |
| `visual_only` | `download_file` → `process_visual` → `finalize_results` | visual present |
| `summarise` | `download_file` → `process_summarise` | transcript service returns result |

For `full`, `audio_only`, and `visual_only`, `finalize_results` runs last: it merges output JSON files from the shared volume, writes `task_info.txt`, deletes the raw video on success, and optionally POSTs to `callback_url`.

For `transcript`, `process_summarise` is the terminal task. It calls `transcriptservice` with the `job_id`, which locates the downloaded file on the shared volume directly. The result is stored in Redis under `job_id` and retrievable via `/status/{job_id}`.

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
│  FastAPI — generates job_id, builds chain, calls .apply_async()     │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ enqueue chain
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
│  Per-job sequential chain:                                          │
│                                                                     │
│  ① download_file                                                    │
│     S3 / HTTP(S) / local → /app/tmp/{job_id}/{filename}            │
│     returns: {file_path, job_id, prompts}                           │
│            │                                                        │
│            ▼                                                        │
│  ② process_visual  (skipped for audio_only / summarise)              │
│     starts visualservice → POST /analyze (upload video)            │
│     parses XML → flat caption segments                              │
│     saves {name}_visual_output.json                                 │
│     stops visualservice                                             │
│     returns: {file_path, job_id, prompts, visual_result}           │
│            │                                                        │
│            ▼                                                        │
│  ③ process_audio  (skipped for visual_only / summarise)              │
│     starts audioservice → POST /process_audio/                     │
│     passes visual_result as chunk boundaries                        │
│     saves {name}_audio_output.json                                  │
│     stops audioservice                                              │
│            │                                                        │
│            ▼                                                        │
│  ② process_summarise  (summarise only, replaces audio + visual)      │ 
│     starts transcriptservice → POST /process/                       │
│     service locates file via job_id on shared volume                │
│     stops transcriptservice                                         │
│     stores result in Redis under job_id  ← terminal task           │
│            │                                                        │
│            ▼                                                        │
│  ④ finalize_results  (full / audio_only / visual_only only)        │
│     merges audio + visual JSON from shared volume                   │
│     evaluates success per job_type                                  │
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
│    ├── video_visual_output.json   (full / visual_only)              │
│    ├── video_audio_output.json    (full / audio_only)               │
│    ├── summarise_output.json      (summarise)                       │
│    └── task_info.txt              (full / audio_only / visual_only) │
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