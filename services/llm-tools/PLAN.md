# Project Plan

## 1. Goal

A containerized FastAPI worker that an external orchestrator calls to convert transcripts into shortform content (a catchy title + bullet-point summary). The orchestrator drops a `transcript.txt` on a shared volume, sends a job ID, and this service writes `output.json` back to the same volume. "Done for v1" means the container builds, receives a job, and returns a valid title + summary via a locally-running Ollama model.

## 2. Constraints

- **Language / framework:** Python, FastAPI, async httpx — no ML frameworks (Ollama's HTTP API is the only LLM interface)
- **Deployment target:** Docker container; orchestrated externally (no Kubernetes assumption)
- **Must integrate with:** external orchestrator via shared filesystem volume + HTTP; Ollama for LLM inference
- **Non-negotiables:**
  - Ollama runs inside the same container (not a sidecar)
  - Models are never bundled in the image — bind-mounted from host at `/root/.ollama/models`
  - Stateless: no database, no in-memory queue; all state is files on the shared volume
  - CPU-only safe (no GPU assumed); single Uvicorn worker + single Ollama instance

## 3. Architecture

```
Orchestrator
    │  1. Writes  shared/{job_id}/transcript.txt
    │  2. POST /process  {"job_id": "..."}
    │  3. Reads   shared/{job_id}/output.json
    ▼
┌──────────────────────────────────────────────┐
│          transcript-processor container        │
│                                              │
│  FastAPI :8000                               │
│    └── POST /process                         │
│    └── GET  /health                          │
│    └── GET  /jobs/{job_id}        (Phase 2)  │
│    └── GET  /metrics              (Phase 4)  │
│                                              │
│  Ollama daemon :11434 (localhost only)       │
│    └── /root/.ollama/models  (bind :ro)      │
│                                              │
│  /shared  (bind rw)                          │
│    └── {job_id}/transcript.txt  ← read       │
│    └── {job_id}/status.json     ← write      │
│    └── {job_id}/output.json     ← write      │
└──────────────────────────────────────────────┘
```

- **Data model:** flat files only. `transcript.txt` → plain text in, `output.json` → `{job_id, title, summary[], model, processing_time_ms}` out. `status.json` → `{status, started_at/finished_at/error}`.
- **Third-party services:** Ollama (self-hosted, inside container); no external APIs.

## 4. Directory / repo structure

```
app/
  main.py              # FastAPI app, logging config
  routers/
    process.py         # POST /process, GET /health, GET /jobs/{job_id}
    metrics.py         # GET /metrics  (Phase 4)
  services/
    ollama_client.py   # async httpx → Ollama /api/generate, retry logic
  prompts/
    transcript.txt     # summary mode prompt  (Phase 1)
    action_items.txt   # action_items mode    (Phase 3)
    qa_pairs.txt       # qa_pairs mode        (Phase 3)
scripts/
  entrypoint.sh        # start ollama, wait, verify model, exec uvicorn
Dockerfile
docker-compose.yml
.env.example
requirements.txt
```

## 5. Milestones (phased, not a flat task list)

### Phase 1 — Foundation ✅ COMPLETE
- FastAPI container with Ollama co-located
- Job-based shared volume I/O (`transcript.txt` → `output.json`)
- Single processing mode: title + bullet summary
- `POST /process`, `GET /health`
- `Dockerfile`, `docker-compose.yml`, `.env.example`

**Limitation:** orchestrator must hold an open HTTP connection for the full inference duration (15–90 s on CPU).

---

### Phase 2 — Job Lifecycle & Reliability

**Goal:** decouple the orchestrator from inference duration; make failures observable.

- **Status file:** write `shared/{job_id}/status.json` at `processing` → `done` / `error` stages
- **`GET /jobs/{job_id}`:** returns current status from `status.json` so orchestrator can fire-and-forget + poll
- **Retry logic:** wrap `ollama_client.generate()` with up to 3 retries + exponential backoff on `httpx.HTTPError`
- **Structured JSON logging:** `logging.JSONFormatter` in `app/main.py`; log at transcript-read, inference-start, inference-end, output-write

Files touched: `app/routers/process.py`, `app/services/ollama_client.py`, `app/main.py`

---

### Phase 3 — Processing Modes

**Goal:** support multiple output types from the same container.

- **`mode` field** in request (`summary` default, `action_items`, `qa_pairs`)
- **Mode-specific prompt templates** in `app/prompts/`
- **Prompt loader** `_load_prompt(mode)` in `ollama_client.py` (replaces module-level constant, caches in dict)
- **`output.json` gains `mode` field**; `ProcessResponse` uses `model_config = {"extra": "allow"}` to pass through mode-specific fields

| Mode | Output fields |
|---|---|
| `summary` | `title`, `summary: [str]` |
| `action_items` | `title`, `action_items: [{owner, task, due}]` |
| `qa_pairs` | `title`, `qa_pairs: [{question, answer}]` |

Files touched: `app/routers/process.py`, `app/services/ollama_client.py`, `app/prompts/`

---

### Phase 4 — Production Hardening

**Goal:** operational visibility, reduced cold-start latency, safer resource usage.

- **Prometheus metrics** (`app/routers/metrics.py`): `transcript_requests_total` (by mode/status), `inference_duration_seconds` histogram, `ollama_ready` gauge
- **Model warm-up** (`scripts/entrypoint.sh`): run `ollama run "$OLLAMA_MODEL" "hi"` before Uvicorn starts to preload weights into RAM
- **Configurable context window:** `OLLAMA_NUM_CTX` env var (default `4096`) passed to Ollama `options`
- **Resource limits** in `docker-compose.yml`: `deploy.resources.limits` for memory and optional CPU cap

Files touched: `app/routers/metrics.py` (new), `app/main.py`, `app/services/ollama_client.py`, `scripts/entrypoint.sh`, `docker-compose.yml`, `requirements.txt`

---

## 6. Open questions

- Should `POST /process` be fire-and-forget (return 202 immediately, let orchestrator poll) or keep the current synchronous response? Fire-and-forget is friendlier for long transcripts but requires the orchestrator to poll `GET /jobs/{job_id}`.
- Should `action_items.due` be a free-text string or an ISO date? Free-text is safer given LLM output variability.
- Multi-language support: should the `language` field control the model's response language only, or also select a different prompt template per language?

## 7. Out of scope (for v1)

- Authentication / API keys (orchestrator handles this externally)
- Streaming responses (Ollama called with `"stream": false`)
- GPU support / CUDA configuration
- Horizontal scaling / load balancer config
- Transcript chunking for very long inputs (document the `MAX_TRANSCRIPT_CHARS` limit instead)
- Web UI or dashboard
