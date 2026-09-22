# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

A containerized FastAPI service that an external orchestrator calls to process transcripts. Given a `job_id`, it reads `$SHARED_VOLUME_PATH/{job_id}/transcript.txt` from a shared volume, runs the text through a locally-running Ollama LLM, and returns a catchy title + bullet-point summary — also writing the result to `$SHARED_VOLUME_PATH/{job_id}/output.json`.

Ollama runs **inside the same container** as the FastAPI app (not a separate service). LLM models are **not bundled in the image** — they are bind-mounted from the host at `/root/.ollama/models`.

## Build and run

```bash
# First-time setup
cp .env.example .env          # fill in OLLAMA_MODEL and volume paths

# Build and start
docker compose up --build

# Check readiness (poll until ollama_ready: true)
curl http://localhost:8000/health

# Send a job (transcript must already exist at SHARED_VOLUME_PATH_HOST/<job_id>/transcript.txt)
curl -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{"job_id": "test123"}'
```

There is no test suite yet. Manual verification steps are in [PLAN.md](PLAN.md#verification).

## Architecture

```
POST /process  →  routers/process.py  →  services/ollama_client.py  →  Ollama HTTP API (localhost:11434)
GET  /health   →  routers/process.py  →  services/ollama_client.py
```

**Startup sequence** (`scripts/entrypoint.sh`):
1. `ollama serve &` — starts daemon in background
2. Polls `localhost:11434` until ready
3. Verifies `$OLLAMA_MODEL` exists on the mounted volume (attempts pull + fails fast if missing)
4. `exec uvicorn app.main:app` — replaces shell so Uvicorn receives signals directly

**Request flow** (`routers/process.py`):
1. Resolves `$SHARED_VOLUME_PATH/{job_id}/transcript.txt` — 404 if missing
2. Checks `MAX_TRANSCRIPT_CHARS` limit (0 = no limit)
3. Rejects `job_type` other than `script` — 422
4. Pings Ollama readiness — 503 if down
5. Looks up the mode registry (`MODES`) by `req.mode` — prompts dir + response model
6. Calls `ollama_client.generate()` — async `httpx` POST to `/api/generate`, timeout 120s
7. Validates the returned JSON shape per mode (`_validate_result`) — 500 if malformed
8. Writes `output.json` to the shared volume and returns the response model

**Prompt** (`app/prompts/{mode}/transcript.txt`, e.g. `summary/` or `tagging/`): loaded at request time inside `_build_prompt`. Uses `str.format_map` with `{language}` slot. Double-braces (`{{`, `}}`) are literal `{}` escapes for format_map. The output JSON schema lives in `app/prompts/{mode}/output_structure.txt`. Adding a new mode = adding one entry to `MODES` in `routers/process.py` plus the two prompt files.

## Key environment variables

| Var | Notes |
|---|---|
| `OLLAMA_MODEL` | Required. Model tag, e.g. `llama3.2:3b`. Must be present on the mounted volume. |
| `OLLAMA_MODELS_PATH` | Host path mounted read-only to `/root/.ollama/models` inside the container. |
| `SHARED_VOLUME_PATH_HOST` | Host path for the shared job volume (compose only). |
| `SHARED_VOLUME_PATH` | Path inside the container (default `/shared`). |
| `MAX_TRANSCRIPT_CHARS` | Per-request char limit; `0` = no limit. Read at request time (not startup). |

## Constraints to keep in mind

- **Single-process by design**: one Uvicorn worker, one Ollama instance. Requests are serial. Scale horizontally, not vertically.
- **CPU-only**: no GPU assumed. Inference on a 3B model takes 15–90 s depending on hardware. The orchestrator's HTTP timeout must exceed 120 s.
- **No authentication**: the orchestrator handles auth externally.
- **No streaming**: Ollama is called with `"stream": false`. The full response is buffered before returning.
