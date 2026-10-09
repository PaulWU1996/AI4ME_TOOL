# AI4ME Video Transcription

A containerized FastAPI service that describes a video with a local multimodal Gemma model. An external orchestrator sends a `job_id` and a `video_path`; the service analyses the clip and returns three time-coded outputs — a visual narrative, an audio narrative, and a best-effort transcript — also writing them to `output.json` on the shared volume.

The model runs **inside the service process** (no separate inference server), and is loaded once at startup so no request pays for weight loading.

## How it works

```
Orchestrator
    │  1. Writes  shared/{job_id}/clip.mp4
    │  2. POST /process  {"job_id": "...", "video_path": "{job_id}/clip.mp4"}
    │  3. Reads   shared/{job_id}/output.json
    ▼
video-transcription container
    ├── FastAPI :8000
    └── Gemma 4 (in-process, GPU via device_map="auto")
```

## Quick start

```bash
# 1. Export a Hugging Face token — the Gemma weights are gated
export HF_TOKEN=hf_xxx

# 2. Build and start — detects GPU and picks the right compose file
./scripts/run.sh up --build

# 3. Poll until ready (model load takes minutes on first run)
curl http://localhost:9004/health

# 4. Drop a video into the shared volume and send it
mkdir -p ./shared/test123
cp /path/to/clip.mp4 ./shared/test123/

curl -X POST http://localhost:9004/process \
  -H 'Content-Type: application/json' \
  -d '{"job_id": "test123", "job_type": "gemma", "video_path": "test123/clip.mp4"}'

# With the journalist's programme brief, in Welsh
curl -X POST http://localhost:9004/process \
  -H 'Content-Type: application/json' \
  -d '{
    "job_id": "test123",
    "video_path": "test123/clip.mp4",
    "prompts": "An episode of a documentary series about wildlife in Wales",
    "language": "cy"
  }'

# Analyse only a window of the video, shot by shot
curl -X POST http://localhost:9004/process \
  -H 'Content-Type: application/json' \
  -d '{"job_id": "test123", "video_path": "test123/clip.mp4", "shot_detection": "detect", "max_shots": 4}'
```

## API

### `POST /process`

| Field | Type | Required | Description |
|---|---|---|---|
| `job_id` | string | yes | Orchestrator-assigned job identity; also names the output directory |
| `video_path` | string | one of `video_path` / `program_id` | Path to the video **relative to the shared volume root**, conventionally `"{job_id}/{filename}"`. Absolute paths are rejected |
| `program_id` | string | one of `video_path` / `program_id` | Programme to fetch DASH audio for; written to `shared/{job_id}/{program_id}.wav` and analysed |
| `start_time_ms` | int | no | Offset into the programme to start fetching audio from (default `0`). Only used with `program_id` |
| `duration_ms` | int | no | How much audio to fetch, in ms. Defaults to the rest of the programme. Only used with `program_id` |
| `job_type` | string | no | Echoed back in the response. Defaults to `"gemma"`; nothing branches on it |
| `prompts` | string | no | The programme's description, given to the model as context |
| `language` | string | no | Output language, e.g. `"en"`, `"cy"` (default `"en"`) |
| `clip_start` | float | no | Start of the window to analyse, in seconds. Defaults to `0` |
| `clip_end` | float | no | End of the window, in seconds. Defaults to the end of the video |
| `shot_detection` | string | no | `"detect"` to split on scene cuts, `"test"` for a fixed 3-shot split. Omit to analyse the whole window as one shot |

**Scenes.** With `program_id`, the service reads the programme's scenes from MongoDB (collection `MONGO_SCENES_COLLECTION`, default `scenes`; `start_time`/`end_time` in programme seconds), keeps those overlapping `[start_time_ms, start_time_ms + duration_ms)`, cuts each one out of the downloaded audio with ffmpeg (`shared/{job_id}/scenes/{scene_id}.wav`) and analyses each separately. With `video_path`, the `clip_start`–`clip_end` window of the file is analysed as a single scene.

**Response (HTTP 200):**
```json
{
  "job_id": "test123",
  "job_type": "gemma",
  "program_id": "m002vqlg",
  "scenes": [
    {
      "scene_id": "scene_0.06_27.18",
      "start_time": 0.06,
      "end_time": 27.18,
      "timeline": [
        {
          "start": 0.06,
          "end": 27.18,
          "transcript": "Here we are, on one of the most rugged coastlines...",
          "audio_narrative": "The presenter introduces the coastline while gulls call overhead."
        }
      ]
    }
  ],
  "models": {
    "primary_model": "google/gemma-4-E4B-it",
    "audio_analysis_model": "google/gemma-4-E4B-it",
    "primary_supports_audio": true
  },
  "processing_time_ms": 18432
}
```

Timeline times are absolute programme seconds. Segments include a `narrative` caption only when the input has video.

With `storage_type: "mongodb"`, each scene is upserted into `gemma_audio_analysis` as `{_id, program_id, scene_id, start_time, end_time, timeline}`, keyed on `(program_id, scene_id)`, so re-running a job replaces the docs instead of duplicating them. Otherwise the payload is written to `shared/{job_id}/output.json`, along with the flattened per-modality files `clip_gemma_visual_output.json`, `clip_gemma_audio_output.json` and `clip_gemma_transcript_output.json` (all scenes concatenated) that `finalize_results` reads. The `_gemma_` infix keeps them clear of the `visualservice` / `audioservice` outputs the orchestrator puts in the same job directory.

**Error responses:**

| Status | Condition |
|---|---|
| 404 | The video does not exist under the job directory, or no scenes exist for `program_id` in the window |
| 502 | Audio could not be fetched for `program_id` |
| 422 | `video_path` is empty, absolute, not a file, or resolves outside the job directory |
| 422 | The video cannot be opened or decoded |
| 500 | The model returned no narrative / audio narrative / transcript, or analysis failed |
| 503 | The model is not loaded yet (container still warming up), or MongoDB is unreachable |

### `GET /health`

```json
{
  "status": "ok",
  "model_ready": true,
  "model": "google/gemma-4-E4B-it",
  "audio_model": "google/gemma-4-E4B-it",
  "loaded_models": ["google/gemma-4-E4B-it"]
}
```

Returns HTTP 503 until the model is resident. The port only opens after the lifespan hook finishes loading, so poll this after a restart or a model change.

## Prompt structure

The prompt is assembled at request time from two files in `app/prompts/analysis/`:

| File | Editable | Purpose |
|---|---|---|
| `transcript.txt` | Yes | Requirements: the producer persona, what to describe, and the `{language}` slot |
| `output_structure.txt` | No — always fixed | The exact JSON the model must return |

`transcript.txt` is rendered with `str.format_map({"language": ...})`; `output_structure.txt` is concatenated after it and never rendered, so the JSON braces need no escaping and the response shape stays stable no matter what `prompts` contains. The `prompts` field is **not** a requirements override as it is in `llm-tools` — it is the programme description, and is passed as context ahead of the frames.

## Configuration

All values are set in `docker-compose.yml`.

| Variable | Default | Notes |
|---|---|---|
| `HF_TOKEN` | — | **Required.** The Gemma weights are gated; the entrypoint fails fast without it |
| `MODEL_ID` | `google/gemma-4-E4B-it` | Primary model |
| `AUDIO_MODEL_ID` | `google/gemma-4-E4B-it` | Loaded only if the primary cannot take audio input |
| `SHARED_VOLUME_PATH` | `/shared` | Container-side path — matches the `./shared` mount |
| `FRAMES_PER_CHUNK` | `16` | Frames sampled per chunk of a shot |
| `CHUNK_SECONDS` | `30` | Target chunk span for long shots |
| `MAX_CHUNKS` | `8` | Maximum chunks sampled across one shot |
| `MAX_TOTAL_FRAMES` | `96` | Hard cap on frames sent to the model |
| `AUDIO_MAX_SECONDS` | `30` | Maximum audio seconds sent to the model |
| `MAX_SHOTS` | `0` | Cap on shots analysed; `0` = no limit. Truncation is logged |
| `MAX_NEW_TOKENS` | `3000` | Generation length per shot |
| `TEMPERATURE` / `TOP_P` / `DO_SAMPLE` | `0.45` / `0.9` / `true` | Sampling parameters |

Sampling knobs are read once per process at first use; changing them needs a restart.

## GPU support

Use `scripts/run.sh` instead of `docker compose` directly — it detects GPU availability and picks the right compose configuration:

```bash
./scripts/run.sh up --build   # GPU mode if nvidia-smi found, CPU mode otherwise
./scripts/run.sh down         # any other docker compose subcommand works too
```

It prints which mode was chosen at startup, and merges `docker-compose.gpu.yml` onto `docker-compose.yml` when a GPU is found.

**GPU prerequisite**: [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) must be installed on the host.

The weights are **not** bundled in the image — they are cached on the host and bind-mounted from `${HF_HOME:-~/.cache/huggingface}`.

**When adding this to the orchestrator's `docker-compose.yml`, pin the GPU by `device_ids` rather than `count: all`.** The orchestrator already runs an 18 GB audio model on GPU 0 and a 4 GB visual model on GPU 1; a third `count: all` model can land on either and OOM. Its `deploy.resources.reservations.devices` block is also where `config/services.json` gets a matching entry so `scripts/start_services.py` can schedule it.

## Logging

Logs go to stdout and are captured by Docker:

```bash
docker logs -f video-transcription          # follow live
docker logs --tail 50 video-transcription   # last 50 lines
docker logs video-transcription 2>&1 | grep "job_id=test123"
```

A happy-path request produces:
```
INFO  | job received   | job_id=test123 job_type=gemma language=en video_path=test123/clip.mp4
INFO  | analysis start | job_id=test123 path=/shared/test123/clip.mp4
INFO  | analysis done  | job_id=test123 shots=1 ms=18432
INFO  | output written | job_id=test123 dir=/shared/test123
```

The pipeline also writes timestamped progress to stderr (`Extracting audio at 16kHz…`, `Generation complete in 41.2s`) — useful when a job is slow and the log line alone tells you nothing.

## Command line

`analyze_with_gemma.py` runs the same pipeline on a file directly, with no HTTP layer:

```bash
python analyze_with_gemma.py /path/to/clip.mp4 \
  --overarching-narrative "A documentary about football" \
  --shot-detection detect \
  --output /tmp/result          # writes /tmp/result_gemma_{visual,audio,transcript}_output.json
```

`--help` lists every flag. Defaults come from the same environment variables the service reads.

## Notes

- **Serial processing**: one job at a time, enforced by a lock in the router. A second request waits rather than failing. Scale by running multiple replicas.
- **Inference is slow**: seconds per shot on GPU, minutes on CPU, and longer the more frames and shots are sampled. The caller's HTTP timeout must exceed it — the orchestrator's existing workflow nodes use `1800`.
- **No authentication**: the orchestrator handles this externally.
- **`/health` never blocks**, even while a job holds the analysis lock.
