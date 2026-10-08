# End-to-end tests against mock services

The real controller, worker, Redis, Celery, shared volume and Docker-socket
container orchestration — with only the four GPU analysis services replaced
by [`mocks/service.py`](../../mocks/service.py). A full pipeline that needs
~20 GPU-minutes and a machine with an NVIDIA card finishes here in about ten
seconds on a laptop, and can be made to fail on demand.

```bash
./venv/bin/python tests/e2e/run_e2e.py            # build images, run all scenarios, tear down
./venv/bin/python tests/e2e/run_e2e.py --no-build # reuse existing images
./venv/bin/python tests/e2e/run_e2e.py -k dag     # only scenarios matching "dag"
./venv/bin/python tests/e2e/run_e2e.py --keep     # leave the stack up to poke at
```

Everything runs on shifted ports (controller `19000`, redis `16379`, mocks
`19001`-`19004`) under compose project `ai4me_mock`, so it never collides
with a real stack.

## What the mocks implement

Only the contracts `worker/tasks.py` and `worker/utils.py` actually depend
on — no more:

| Role | Endpoints |
|---|---|
| visual | `GET /health/`, `POST /generate` (X-Admin-Key → api key), `POST /analyze` (X-API-Key, multipart → `VideoAnalysis` XML) |
| audio | `GET /health/`, `POST /process_audio/` → `{"output": [{start, end, caption}]}` |
| transcript | `GET /health`, `POST /process/` → summary JSON |
| tagging | `GET /health`, `POST /process/` → tags JSON |

Plus `GET /__calls`, which returns every request the service has received —
useful when you want to assert what the worker actually sent.

### They are strict, not permissive

A mock that accepts whatever it is handed cannot catch the bug you most want
caught: the worker sending a correctly-shaped but wrongly-named field, which
a real FastAPI endpoint would reject. So the mocks validate and refuse:

- `/generate` requires a matching `X-Admin-Key` and a `client_name`, and
  issues a *tracked* key, persisted to the shared volume so it survives the
  worker recreating the container.
- `/analyze` returns **401** for a missing key or one this service never
  issued, and **422** for a body that is not multipart or has no part named
  `video`.
- `/process_audio/` requires `video_path` (non-empty string), `prompts`, and
  `chunks` (list or null); **422** otherwise.
- `/process/` requires `job_id`, `job_type` and `prompts`.

Set `MOCK_LENIENT=1` to fall back to accept-anything behaviour.

The `contract-enforced` scenario proves the guard is awake rather than
vacuous: it runs a real job (the worker's own requests must pass), then
sends deliberately malformed requests and asserts each is refused.

This still cannot prove the *real* services have these shapes — only that
the worker keeps sending what it believes they are. But it turns a silent
regression into a failing test. It already earned its keep: tightening the
key check surfaced that `ensure_api_key()` returned its cached key forever,
so a rotated or forgotten key would have failed every visual job
permanently, with no recovery path.

## Injecting failure

The worker cold-starts these containers itself, from the compose file, so a
container you create by hand with `MOCK_*_FAIL_MODE` set is thrown away and
replaced with a default one the moment a job runs. Behaviour is therefore
injected through a **control file on the shared volume**
(`shared/mock_control.json`), which the mocks re-read on every request and
which survives container recreation:

```json
{ "audio": { "fail_mode": "error500" },
  "visual": { "latency": 4, "startup_delay": 6 } }
```

`fail_mode` is one of `error500`, `timeout`, `badbody`, `unhealthy`.
`run_e2e.py` writes and clears this via `set_mock_control()`.

A fifth mock, `callbacksink`, runs permanently rather than on-demand: it
receives `finalize_results`' callback POSTs, which arrive *after* the
analysis services have been stopped again.

## Why the compose file is image-only

`tests/e2e/docker-compose.mock.yml` gives every service an `image:` and no
`build:`. The worker starts on-demand services by shelling out to
`docker compose -f <this file>` from *inside its own container*, where the
repo's build contexts do not exist. Image-only means that inner invocation
never needs them. `run_e2e.py` builds the three images up front.

## Scenarios

| Scenario | What it pins down |
|---|---|
| `legacy-full` | `job_type=full` with an empty registry → legacy `build_chain()` |
| `dag-full` | the *same* request, now routed through `DAGEngine` |
| `status-shape-differs` | gap B: the two paths return incompatible `/status` bodies |
| `dag-service-declared` | gap D: a workflow declaring `service` cycles the container twice per node |
| `dag-summarise` | the 5-node transcript pipeline |
| `failure-propagates` | a service 500 becomes a `FAILURE`, not a hung job |
| `slow-service-tolerated` | injected latency rides through cleanly |
| `slow-startup-health` | the worker waits out a slow-to-become-healthy cold start |
| `coldstart-cycle` | containers really are started and stopped over the Docker socket |
| `keepalive-mode` | `service_modes.json` keeps a container resident across a job |
| `bad-workflow-rejected` | `POST /workflows` refuses a cycle and commits no partial file |
| `retry-parity` | a DAG node retries a transient failure, as the legacy chain's `autoretry_for` does |
| `callback-delivered` | `finalize_results` POSTs its merged output to `callback_url` |
| `all-job-types` | all 5 remaining legacy job types run as DAGs, incl. `speaker_extent`/`segment_extent` |
| `custom-name-needs-expects` | a workflow under a name `build_chain()` never knew about runs clean |
| `contract-enforced` | the mocks refuse malformed requests, and the worker's own requests pass |
| `stale-api-key-recovered` | a 401 on a cached key triggers regeneration instead of failing forever |
| `badbody-surfaces` | a non-JSON response fails the job and writes no output |
| `unhealthy-service` | a service that never goes healthy fails the node rather than hanging |
| `hung-service` | a service that accepts the connection and never answers hits the request timeout |
| `unknown-task-preflight` | a typo'd task fails before `download_file` touches the disk |

## What this found that unit tests could not

`ModuleNotFoundError: No module named 'dag'` on **every** DAG job. Celery's
app loader puts the working directory on `sys.path` only while importing the
app module, then removes it — so `tasks.py`'s module-level imports resolve,
but the lazy `from dag.engine import DAGEngine` inside `execute_workflow`
does not. The DAG path had never executed under a real Celery worker. Fixed
by `ENV PYTHONPATH=/app` in both Dockerfiles.
