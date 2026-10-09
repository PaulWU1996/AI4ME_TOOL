# Media Processing Orchestrator

A distributed workflow orchestrator. A **FastAPI**
controller registers DAG workflow templates and dispatches each job as a
Celery canvas; a **Celery** worker executes the nodes against HTTP
services or local Python scripts, coordinated through **Redis**.

---

## 1. DIRECTORY STRUCTURE

```bash
.
|-- controller
|   |-- main.py         # FastAPI app: /workflows, /process, /status
|   |-- tasks.py        # Celery app configuration (broker + backend)
|   |-- Dockerfile
|   |-- requirements.txt
|-- worker
|   |-- tasks.py        # Celery tasks: http_call, python_call
|   |-- consts.py       # Environment configuration
|   |-- Dockerfile
|   |-- requirements.txt
|-- dag
|   |-- parser.py       # Workflow JSON -> validated DAG (networkx)
|   |-- compose.py      # DAG -> Celery chain-of-groups canvas
|-- workflows
|   |-- registry.json   # job_type -> versions -> file paths
|   |-- *.json          # Registered workflow templates
|-- scripts             # ECR build/push helpers
|-- docs                # Module-level reference documentation
|-- shared              # Job workspace, mounted at /app/tmp
|-- docker-compose.yml  # redis, controller, worker, autoheal
```

---

## 2. GETTING STARTED

### Prerequisites

- Docker + Docker Compose
- AWS credentials (only for building/pushing images to ECR)

### Environment

Create a `.env` in the repo root:

```bash
AWS_ACCOUNT_ID=123456789012
AWS_REGION=eu-west-1
ECR_REPO=moments
```

### Run

```bash
docker compose up -d
```

The API is then available at `http://localhost:9000`. Redis listens on
`6379` (Redis Insight GUI on `8001`).

---

## 3. API

### `POST /workflows` — register a workflow template

The request body **is** the workflow document (see §4). It is validated
(acyclic, all `depends_on` declared, unique ids), written to
`workflows/<name>_<version>.json`, and recorded in `registry.json`.

```bash
curl -X POST http://localhost:9000/workflows \
  -H "Content-Type: application/json" \
  --data-binary @workflows/content-avoidance_1.0.json
```

A name may hold several versions; `latest` always points at the most
recently registered one. Re-registering an existing name+version is a
`400`.

### `POST /process` — submit a job

```bash
curl -X POST http://localhost:9000/process \
  -H "Content-Type: application/json" \
  -d '{
    "job_type": "content-avoidance",
    "payload": {"programme_id": "m002vqlg", "start_ms": 0, "duration_ms": 60000}
  }'
```

| Field | Type | Required | Description |
|---|---|---|---|
| `payload` | object | yes | Input passed to the first node of the workflow |
| `job_type` | string | yes | Name of a registered workflow |
| `version` | string | no | Pin a version; defaults to `latest` |
| `run_at_ms` | int | no | Epoch ms to schedule the job (max 1h ahead) |

Extra top-level fields are allowed and become part of the job context
available to nodes declaring `call: "kwargs"`.

Returns immediately:

```json
{"status": "submitted", "job_id": "…", "job_type": "content-avoidance"}
```

### `GET /status/{job_id}` — poll for the result

```bash
curl http://localhost:9000/status/<job_id>
```

```json
{
  "job_id": "…",
  "status": "SUCCESS",
  "is_ready": true,
  "data": { … },
  "message": "Task completed successfully"
}
```

`status` is a Celery state (`PENDING`, `PROGRESS`, `STARTED`,
`SUCCESS`, `FAILURE`). Unknown or expired ids return `404`. `data`
holds the final node's return value once the job is ready.

---

## 4. WORKFLOW FORMAT

A workflow is a JSON document:

```json
{
  "workflow": { "name": "content-avoidance", "version": "1.0" },
  "tasks": [ … ]
}
```

Each entry in `tasks` declares a node:

| Field | Meaning |
|---|---|
| `id` | Unique node id |
| `depends_on` | Ids of predecessor nodes |
| `driver` | `"http"` or `"python"` (see below) |
| `call` | `"kwargs"` to read a slice of the job context instead of the predecessor's result |
| `inject` | For `call: "kwargs"` — job-context keys to pass in |
| `requires` | For `call: "kwargs"` — keys that must be present or the build fails |
| `kwargs` | Static arguments merged under the injected keys |

### HTTP driver

Runs the generic `tasks.http_call` task. The predecessor's result
arrives positionally as the request payload.

```json
{
  "id": "audio-transcription",
  "driver": "http",
  "url": "http://localhost:9004/process",
  "method": "POST",
  "headers": {},
  "timeout": 300,
  "body": { "storage_type": "mongodb" },
  "merge": true
}
```

`body` is merged into the payload for this call only. With
`merge: true` the response is merged over the payload
(`{**payload, **response}`) so downstream nodes keep `job_id` and the
rest of the context; otherwise the response replaces it.

### Python driver

Runs the generic `tasks.python_call` task: a script below the worker's
script root, fed the predecessor result as JSON on stdin, expected to
print one JSON document to stdout.

```json
{
  "id": "transcript",
  "driver": "python",
  "script": "transcript-tools/transcript_to_text.py",
  "params": { "key": "value" },
  "timeout": 300,
  "merge": true,
  "depends_on": ["download"]
}
```

`script` must be a relative path inside `PYTHON_SCRIPT_ROOT` (no `..`).
`params` overrides payload keys, `timeout` bounds the subprocess, and
`merge` preserves the payload for the next node. The script runs as a
bare subprocess — it imports nothing from the worker, so everything it
needs must arrive in the payload.

> **Note:** no service scripts are shipped in this repo yet; the worker
> image does not currently contain a `/app/services` tree, so `python`
> driver nodes need that directory mounted/added first.

### Job context

The job context is the `/process` request (`payload`, `job_type`,
`version`, `run_at_ms` plus any extra fields) merged with `job_id`.
Nodes using `call: "kwargs"` read from it; everything else receives the
predecessor's result positionally.

---

## 5. EXECUTION MODEL

`dag/parser.py` validates the workflow into a `networkx.DiGraph`;
`dag/compose.py` turns it into a single Celery canvas:

1. Every node is assigned to exactly one layer
   (`layer = 1 + max(predecessors' layers)`, roots at 0).
2. Layers are chained in order; a layer with several nodes becomes a
   parallel `group`, a single-node layer is the task itself.

Running the chain top-to-bottom guarantees dependencies finish before a
node starts, and Celery upgrades a group followed by another step to a
fan-in automatically.

```
POST /process ──> CONTROLLER ──> REDIS ──> WORKER
                  builds the       queue      executes nodes:
                  canvas from      jobs       http_call / python_call
                  the workflow
                        │
                        ▼
               GET /status/{job_id}  <──  Celery result backend
```

---

## 6. DEPLOYMENT

Images are built and pushed to ECR, then referenced by
`docker-compose.yml`:

```bash
./scripts/deploy-all.sh          # both images
./scripts/deploy-controller.sh   # or individually
./scripts/deploy-worker.sh
docker compose up -d
```

`scripts/lib-ecr.sh` holds the shared helpers (env loading, ECR login,
repository creation, build+push).

---

## 7. DOCUMENTATION

- `docs/DAG_ENGINE_PARSER.md` — reference for `dag/parser.py` and
  `dag/compose.py`
