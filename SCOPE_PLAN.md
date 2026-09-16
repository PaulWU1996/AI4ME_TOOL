# DAG execution — scope &amp; status

Consolidates and replaces `TOMORROW_PLAN.md` and `dag/DRIVER_UNIFICATION_SCOPE.md`
(both folded in below, marked done). One place to track what's implemented,
what's scoped-but-not-started, and what blocks what.

## Status overview

| Item | Status |
|---|---|
| 1. Driver dispatch unification | **Done** |
| 2. Standard task envelope (steps 1-3) | **Done** |
| 2b. `settings.on_failure` actually wired (bonus fix found along the way) | **Done** |
| 3. Service lifecycle + auth generalization | **Lifecycle done** — readiness (§3b) + leases (§8) shipped, task bodies migrated off `start_service`/`stop_service`; `auth` field/injection still not started |
| 4. Retire `build_chain()` / legacy Celery chains | **Not started** — blocks the rest of #2, and most of #3 |
| Live `docker-compose up --build` verification | **Done against mocks** — 2026-09-16, §7. Found and fixed a `PYTHONPATH` bug that had made the DAG path unrunnable. Not yet run against the real GPU services |
| `execute_parallel()` enablement | **Unblocked on occupancy** — leases shipped (§8). Still gated on an aggregate VRAM feasibility check before it is switched on |
| 5. Multi-instance service pooling / orchestrator migration | **Explicitly deferred, not scoped** — decided 2026-09-09, see note below |
| 3b. Deployment-mode split: readiness strategy (single-host vs multi-host) | **Done** — implemented 2026-09-11, see §3b below |
| 6. Docker-free unit suite for `dag/` | **Done** — 2026-09-16, 124 tests; see §6 for the 8 gaps it surfaced |
| 7. Mock-service e2e stack | **Done** — 2026-09-16, 16 scenarios; see §7 and §9 |
| 8. Gaps A-I closed | **Done** — 2026-09-16, see §8 |
| 9. Node-level retry + coverage of every job type | **Done** — 2026-09-16, see §9 |

---

## Decision: small scope first, pooling/orchestrator later (2026-09-09)

Raised while discussing #3: today's `start_service`/`stop_service`
(`worker/utils.py`) + `scripts/start_services.py`'s static/measured
resource pre-check is, honestly, a hand-rolled, lightweight orchestrator —
scoped to **one named container per service** (`docker-compose.yml`'s fixed
`container_name`, static `AUDIO_HOST`/`VISUAL_HOST` env vars). Moving
lifecycle calls into the engine (#3) doesn't remove that limitation, it
just relocates which file does the hand-rolling — genuine concurrent
multi-instance scaling (several `audioservice` containers, dynamically
named, load-balanced across jobs) would need real pooling/allocation logic,
or a real orchestrator (Kubernetes + GPU device plugin, Nomad) doing
scheduling instead of us.

**Decided:** keep lifecycle management in-house for now (no orchestrator
migration), and scope only #3 (the small, in-house lifecycle/auth
generalization) as near-term work. Multi-instance pooling and/or a real
orchestrator migration is explicitly **not** being scoped right now — it's
a materially larger, separate decision (infrastructure/ops cost, not just
code) to revisit once the current single-instance-per-service model is
actually the bottleneck. #3's engine-level lifecycle wrapper is a
reasonable foundation to build a pool allocator on top of *later*, if that
direction is chosen — but §3 alone does not solve pooling, and shouldn't be
mistaken for progress toward it.

---

## 1. Driver dispatch unification — done

Collapsed the old `TASK_REGISTRY` hardcoded dict into a generic `driver`-based
dispatch, so existing and future pipeline steps are addressed the same way.

**What it looks like now:**
- `dag/envelope.py` — `success()`/`failure()`/`as_envelope()`, the shared
  `{status, data, error}` result shape.
- `dag/drivers/python.py` — resolves `module`/`func` (or legacy `task`) via
  `importlib`, calls it either `func(inputs)` (payload style, default) or
  `func(**inputs)` (kwargs style), wraps the result in an envelope.
- `dag/drivers/http.py` — generic external call (`url`/`method`/`headers`/
  `timeout`), for a step with no Python wrapper at all. Also always returns
  an envelope.
- `dag/engine.py` — `DAGEngine.execute_node` reads a node's `driver`
  attribute (default `"python"`, so existing `{"task": "..."}`-only JSON
  keeps working unmodified — see `workflows/full_pipeline_1.0.json`). Builds
  `inputs` differently depending on whether the target is payload-shaped or
  not (`NON_PAYLOAD_TASKS = {"download_file", "finalize_results"}`, kept as
  an explicit set rather than threading job context generically through
  node attributes — this was the one open design question, resolved this
  way during implementation).
- `dag/parser.py`'s `Parser` now also retains `workflow`/`settings`
  top-level JSON metadata (`.metadata`, `.settings`) instead of discarding
  them — previously only `tasks` survived parsing.

**How to add a new driver-dispatched node:**
- `"python"` (default): `{"func": "my_func", "module": "some.module"}` (or
  the legacy `{"task": "my_func"}`, defaulting `module` to `"tasks"`).
- `"http"`: `{"driver": "http", "url": "https://...", "method": "POST"}`.

**Verified:** syntax compile; simulated controller-image layout (`main.py`
imports cleanly, never touches `dag/engine.py`/`dag/drivers/*`); simulated
worker-image layout with stub `tasks.py` — full `DAGEngine.execute()` dry
run against the real `workflows/full_pipeline_1.0.json`: success path,
a deliberately failing node (`TaskExecutionError`, downstream nodes
correctly skipped), an unknown-task pre-flight rejection (zero side
effects), and `on_failure="stop"` vs `"continue"` both behaving correctly.

---

## 2. Standard task envelope — done (steps 1-3 of 4)

Every driver call now returns `{"status": "success"|"error", "data": {...},
"error": str|None}`.

**Why:** `process_audio` didn't spread `payload` in its return (silently
dropped upstream keys), and `process_visual` swallowed its own exceptions
internally (printed + returned `visual_result: None` instead of raising) —
failures looked identical to empty-but-successful results.

**What shipped:**
1. `drivers/*` always return envelope-shaped results — `as_envelope()`
   auto-wraps any raw dict a not-yet-migrated task still returns.
2. This **subsumed** the originally-planned "dual-shape handling in
   `DAGEngine`" — that responsibility ended up living entirely in the
   driver layer, so `dag/engine.py` has no dedicated dual-shape code; it
   just reads `envelope["data"]` uniformly.
3. `process_visual`/`process_audio` in `worker/tasks.py` now let exceptions
   propagate instead of swallowing them; `process_audio` now spreads
   `**payload` like every other task. **Confirmed with user before making
   this change:** since `build_chain()` isn't retired yet and calls these
   same functions, a real audio/visual failure now halts that legacy chain
   before `finalize_results` too — a live behavior change to the
   still-active legacy path, accepted deliberately.

**Not touched:** `process_summarise`, `process_tagging`, `speaker_extent`,
`segment_extent`, `transcript_to_text`, `download_file`, `finalize_results`
— not known to have the same silent-failure pattern, out of scope for this
pass.

**Step 4, not started:** retire `build_chain()`, then drop any remaining
raw-dict tolerance and require the envelope strictly.

---

## 3. Service lifecycle + auth generalization — scoped, not started

### Why this matters beyond tidiness

`execute_parallel()` exists (`dag/engine.py`) but isn't called yet — see
`worker/tasks.py`'s `execute_workflow`, which deliberately uses `execute()`
instead, with a comment explaining why. The reason: running independent
nodes concurrently could try to launch more coldstart GPU containers
(`audioservice`/`visualservice`, per `config/services.json`) than the host
can actually run at once. The agreed fix (see prior discussion) is a
resource-feasibility check — before dispatching a generation in parallel,
sum the declared `vram_mb`/`ram_mb` of that generation's *coldstart*
services against host capacity (mirroring the static pre-check
`scripts/start_services.py` already does), and fall back to sequential
(`max_workers=1`) for that generation if it doesn't fit. Keepalive services
are exempt — they're already resident singletons, so concurrent calls to
them don't risk a container-launch race.

**That check requires the engine to know which service a node needs.**
Today that's impossible without either running the node or reading its
Python source — `start_service("visualservice", ...)` is just a line of
code inside `process_visual`, invisible to `DAGEngine`. So generalizing
service lifecycle out of the function body and into something the engine
can see isn't just cleanup — it's the literal prerequisite for
`execute_parallel()` ever being safe to enable.

### Design (agreed direction)

**Where lifecycle/auth config lives:** `config/services.json` — the
existing per-service registry (already the single source of truth for
`vram_mb`/`ram_mb`/`gpu`/`supports_keepalive`/`compose_service`). Extend it
with an `auth` field:

```json
"visualservice": {
  "compose_service": "visualservice",
  "vram_mb": 6000,
  "ram_mb": 4000,
  "gpu": true,
  "supports_keepalive": true,
  "auth": {"type": "api_key_header", "header_name": "X-API-Key", "key_provider": "ensure_api_key"}
},
"audioservice": {
  "compose_service": "audioservice",
  "vram_mb": 8000,
  "ram_mb": 4000,
  "gpu": true,
  "supports_keepalive": true,
  "auth": {"type": "none"}
}
```

Chosen over declaring auth per-node in the workflow JSON: auth is a
property of the *service*, not of any particular pipeline calling it — a
workflow author shouldn't have to redeclare how to authenticate to
`visualservice` every time they reference it. `key_provider` names a
resolvable callable (same resolution mechanism `drivers/python.py` already
uses) so the existing `ensure_api_key()` bootstrap logic
(`ADMIN_KEY`/`API_KEY_PATH`-based, in `worker/utils.py`) stays reusable
rather than forcing every service's auth to be literally identical — only
uniformly *invoked*.

**Where it's referenced:** a new optional node attribute, `"service"`:

```json
{"id": "visual", "task": "process_visual", "service": "visualservice", "depends_on": ["download"]}
```

When present, before calling the driver, `DAGEngine`/the driver layer would:
1. Look up `config/services.json[service]` for `compose_service`, resource
   needs, `auth`.
2. Call `start_service(compose_service, ...)` before, `stop_service(...)`
   after (`finally`) — driver-agnostic, so even an `"http"`-driver node
   calling a coldstart service would get correct lifecycle management,
   which is impossible today (the generic http driver has zero lifecycle
   awareness).
3. If `auth.type != "none"`, resolve `auth.key_provider` and inject the
   credential appropriately (header for `"http"`, some equivalent for
   `"python"`) — **exact mechanics still an open item**, since today's
   `ensure_api_key()` is called and used inline within `process_visual`'s
   own body, not passed in as a parameter.

### The sequencing problem (why this can't ship the way #1/#2 did)

Unlike driver unification and the envelope, this **cannot be done as a
pure addition without breaking something**, because of `build_chain()`:

- If the engine-level wrapper starts/stops a service **and** the existing
  function body (`process_visual`) *also* still calls `start_service`/
  `stop_service` internally, a DAG-dispatched node would double-manage the
  container — calling `start_service` twice, or `stop_service` on an
  already-stopped container. Broken.
- If the function bodies get stripped of their internal lifecycle calls
  *now*, `build_chain()` (still live, still calling these same functions
  directly) loses lifecycle management entirely, since it never goes
  through `DAGEngine`. Also broken.

**Resolution, consistent with how #1/#2 were sequenced:** build the
mechanism (config schema, `service` attribute, engine-level wrapper) as new,
additive code — but apply it only to **newly written task functions that
don't self-manage lifecycle**, not to `process_visual`/`process_audio`.
Those two keep their internal `start_service`/`stop_service`/
`ensure_api_key()` calls, and their nodes simply don't set `"service"` in
the workflow JSON, exactly as today. Retrofitting them (removing the
internal calls, adding `"service"` to their nodes) waits until `build_chain()`
is retired — same blocker as envelope migration step 4.

**Honest limitation this implies:** the resource-feasibility check for
`execute_parallel()` only *fully* protects the pipeline once
`process_visual`/`process_audio` are retrofitted, since those are the only
nodes actually contending for coldstart GPU capacity today. The mechanism
can be built and validated against new nodes earlier, but `execute_parallel()`
shouldn't be considered safe to enable for the *existing* `full_pipeline`
template until the retrofit happens.

---

## 3b. Deployment-mode split: readiness strategy — done (2026-09-11)

Refines §3 rather than replacing it. Came out of asking "if we removed
lifecycle from the functions, wouldn't the orchestrator be cleaner?" — the
honest answer was that today's `start_service`/`stop_service` is itself a
hand-rolled, single-host-only orchestrator (fixed `container_name` per
service, Docker socket access), and a real fix depends on what "deployment"
even means going forward. Decided: split explicitly into two deployment
modes, rather than trying to make one lifecycle model serve both.

**Single-host** (today's model, unchanged): worker has Docker socket
access, checks health, and — if not ready — calls `start_service` to
coldstart it (or it's already up via `keepalive`). Self-healing.

**Multi-host** (new): worker never starts or stops anything, no Docker
socket dependency. Still checks health/availability before use (agreed:
this check stays even without start/stop authority) — but if a service
isn't ready, that's just a node failure, no self-heal attempt. This
reuses the `on_failure` mechanism already built in §"2b" for free: an
unreachable multi-host service becomes a normal `TaskExecutionError`,
subject to the workflow's own `"stop"`/`"continue"` setting — no new
failure-handling machinery needed.

**Where readiness checking lives, mapped onto two scheduling levels:**
- **Job/DAG level** (`DAGEngine`) — untouched. Still just walks
  generations and calls `execute_node` → `driver.run()`, with zero
  awareness of lifecycle/readiness. This is "scheduler" in the sense of
  deciding *what runs when* across the graph — unaffected by this split.
- **Task/module level** (the drivers) — this is where "is my target ready"
  belongs, since it's specific to one call, not the graph shape. Design:
  a small pluggable interface, e.g. `ensure_ready(service_name)`, that
  either returns cleanly or raises — called by a driver just before it
  actually dispatches (the HTTP POST, or the Python call that itself makes
  one). Two implementations (single-host / multi-host); which one is active
  is picked by one config switch, not baked into driver code.

**Config:** one new value, e.g. `DEPLOYMENT_MODE=single_host` (default) or
`multi_host` — same env-var-driven pattern as `SHARED_PATH`/`WORKFLOWS_PATH`.
User has no strong preference between one repo with a mode toggle vs. two
separate deployment profiles; leaning toward one repo unless the two
strategies turn out to diverge more than expected — they're small and
swappable as scoped, not a deep fork.

**Service URL resolution:** deliberately kept as simple as possible for v1
— a static URL per service (same shape as today's `AUDIO_HOST`/`VISUAL_HOST`,
or a flat `url` field in `config/services.json`), no discovery/registry
mechanism. Multi-host just means that URL might point off-host instead of
at a sibling container — the resolution mechanism doesn't change, only
where it happens to point. Explicitly deferred: anything smarter
(service discovery, DNS-based resolution, a registry) if/when it's
actually needed.

**Relationship to §3 and §5:** this doesn't replace §3's config/`"service"`-
attribute design — it sits on top of it. §3's `start_service`/`stop_service`
wrapping *is* the single-host readiness strategy; multi-host is a second,
simpler implementation of the same interface. Still blocked on the same
`build_chain()` sequencing issue as §3 for retrofitting `process_visual`/
`process_audio` specifically — see §3's "sequencing problem" section,
unchanged by this addition. Still explicitly not about pooling (§5) —
multi-host mode assumes a service is reachable, it doesn't decide *which*
instance among several, or scale anything.

**What actually shipped:**
- `dag/readiness.py` (new) — `ensure_ready(service_name)` /
  `release(service_name)`, each dispatching to a single-host or multi-host
  strategy function based on `DEPLOYMENT_MODE` (env var, default
  `single_host`), validated eagerly (`validate_mode()`, `ValueError` on an
  unrecognized value) rather than failing silently into a default.
  Worker-specific imports (`utils`, `consts`) are lazy, inside the strategy
  functions — this file is part of the shared `dag/` package copied into
  both Docker images, and the controller image has neither the Docker SDK
  nor `worker/utils.py`.
  - **Single-host:** calls the existing `start_service`/`stop_service`
    (`worker/utils.py`) unchanged — same Docker-socket-based, container
    `State.Health.Status` check, coldstart-recovery-on-unhealthy behavior
    as today. Resolved the "what does health check mean generically" open
    question by *not* generalizing it — single-host keeps its existing
    Docker-specific check as-is.
  - **Multi-host:** a plain `requests.get(health_url, timeout=10)` against
    a new `SERVICE_HEALTH_URLS` dict in `worker/consts.py` (paths lifted
    directly from `docker-compose.yml`'s own `healthcheck` blocks per
    service). No start attempt — a failed check raises
    `ServiceNotReadyError` immediately.
- `dag/engine.py` — `execute_node` reads a new optional `"service"` node
  attribute; if present, calls `ensure_ready` before `driver.run(...)` and
  `release` after (`finally`, so it runs even if the driver call raises). A
  failed `ensure_ready` becomes a `failure()` envelope, flowing through the
  exact same `on_failure` branch as any other node failure — confirming
  the "no new failure-handling machinery needed" point above.
  `_validate_task_names` also pre-flight-checks `DEPLOYMENT_MODE` validity
  for any node declaring `"service"`, consistent with every other
  pre-flight check in that function.
- **Resolved, the interface's call site:** decided to live in `execute_node`
  itself (wrapping the driver call), not inside each driver — keeps
  `drivers/python.py`/`drivers/http.py` unaware of lifecycle entirely,
  which was the whole point of generalizing it out of task functions in
  the first place.

**Bug found and fixed along the way — not scoped, discovered while testing
this:** `dag/parser.py`'s `Parser.parse()` only ever copied a task's
`"attributes"` dict and the explicit `"task"` field into node attributes.
Every other top-level JSON field — `driver`, `url`, `func`, `module`,
`kwargs`, and now `service` — was silently dropped and never reached
`DAGEngine` at all. This had apparently never been exercised end-to-end
before (only `{"task": "..."}`-only nodes, like every node in
`workflows/full_pipeline_1.0.json`, had ever actually been parsed and
run). Fixed: `Parser.parse()` now copies every top-level task field except
the structural ones (`id`, `depends_on`, `attributes`) into node
attributes, with the legacy nested `attributes` dict merged first so a
top-level field wins on conflict. Regression-checked against
`full_pipeline_1.0.json` — unaffected, since it only ever used `task`.

**Verified:** syntax compile; simulated controller-image import path
(unaffected, `dag/readiness.py` has no heavy imports at module top);
worker-image dry run covering: a node with no `"service"` (unaffected,
existing behavior), single-host mode (`start_service`/`stop_service`
called, as stubbed), multi-host mode with a deliberately unreachable
health URL (node correctly fails, `on_failure="continue"` lets the DAG
proceed past it), and an invalid `DEPLOYMENT_MODE` (caught pre-flight,
zero nodes executed). **Not verified:** a live run — same gap as
everything else in this document.

**Still open, per the sequencing already documented in §3:** no existing
node uses `"service"` yet — `process_visual`/`process_audio` still
self-manage lifecycle internally and aren't retrofitted, still blocked on
`build_chain()` retirement. This mechanism is ready for the *next* new
task function that needs a service, exactly as intended.

---

## Cross-cutting: `build_chain()` retirement

Referenced as a blocker three separate times above (envelope step 4, most
of #3, and by extension `execute_parallel()`'s full safety). Worth treating
as its own tracked piece of work rather than an incidental footnote —
whoever picks this up next should read `controller/main.py`'s
`build_chain()`/`build_dag_workflow()`/`/process` routing logic first, since
retiring it means every `job_type` needs either a registered DAG workflow
or an equivalent, and the routing logic that currently falls back to
`build_chain()` needs to change accordingly.

## Verification gap

Nothing in this document has been run against a live `docker-compose up
--build` stack with real Redis/Celery/Docker-orchestrated services. All
verification so far is syntax checks, simulated image-layout imports, and
dry runs with stub functions standing in for the real ones. This is the
natural next step whenever it's convenient to run the full stack — and
arguably should happen before relying further on anything in section 3.

---

## 6. Docker-free unit suite — done (2026-09-16)

`tests/` covers the whole of `dag/` with no Docker, Redis, Celery or GPU:
**124 passing, 5 xfailed, ~1.5s.** See `tests/README.md` for how the
isolation works and how to run it.

The trick is that `worker/Dockerfile`'s `COPY worker/ .` flattening makes
`utils`, `consts` and `tasks` **top-level modules** inside the image, so the
suite reproduces that layout by installing stubs into `sys.modules` under
the same names — the real import statements in `dag/readiness.py` and
`dag/drivers/python.py` run unmodified. The `http` driver and multi-host
readiness run against a real loopback `ThreadingHTTPServer`, not a
monkeypatched `requests`.

Also removed: the old top-level `test_parser.py`, which imported
`worker.dag.parser` (the package moved to top-level `dag/`), read a
`full_workflow.json` that does not exist, and wrapped everything in a bare
`try/except` — so it could never fail. `__pycache__` was tracked in git and
is now ignored.

### Gaps found while writing it

Each is pinned by a test marked `@pytest.mark.gap` + `xfail(strict=True)`,
so closing the gap turns the test into a failure and forces the marker out.

| # | Gap | Pinned by |
|---|---|---|
| A | `registry.json` registers `full_pipeline`; `SUPPORTED_JOB_TYPES` has `full`. Different strings, so no request takes the DAG path by default | — (routing, no test yet) |
| B | `/status` returns finalize's merged output on the legacy path but the whole node→envelope dict on the DAG path. A client cannot parse both | — |
| C | No node in `full_pipeline_1.0.json` declares `service`, so §3b's readiness layer is unreachable from the only registered workflow | `test_parser.py::test_shipped_workflow_declares_no_service_anywhere` |
| D | Adding `service` to that workflow double-manages the container: engine starts it, `worker/tasks.py:103` starts it again, the task's `finally` stops it, then the engine stops it | — |
| E | No lease/refcount: two concurrent nodes sharing a service start it twice, and the first to finish stops it **while the other is still mid-request** | `test_engine.py::test_shared_service_should_{be_started_once,not_be_stopped_while_still_in_use}` |
| F | `from utils import start_service` escapes as `ModuleNotFoundError` if the Dockerfile layout ever changes, bypassing `dag/engine.py:185` and the workflow's `on_failure` entirely | `test_readiness.py::test_single_host_missing_utils_should_be_a_service_not_ready_error` |
| G | Duplicate task ids silently merge into one node — a task vanishes with no diagnostic | `test_parser.py::test_duplicate_task_id_should_be_rejected` |
| H | `CLAUDE.md` describes a Celery **chord** running audio and visual in parallel. `build_chain()` is a sequential `chain`; nothing in the codebase has ever run them in parallel | — |

**E is the blocker for `execute_parallel()`**, and is narrower than the
resource-feasibility check named in the status table above: a per-service
lease stops one service being doubly occupied, but does *not* stop two
different GPU services being jointly resident beyond host VRAM. Leases are
necessary, not sufficient.

Note on H: `execute_parallel()` is therefore not restoring lost
parallelism — it would be adding parallelism this system has never had.

### Still needs a live stack

Real `start_service` orchestration, `scripts/start_services.py`'s
`nvidia-smi`/`free -m` measured pass, Celery/Redis routing, and the
analysis services' actual HTTP contracts. Unchanged from the section above.

---

## 7. Mock-service end-to-end stack — done (2026-09-16)

`tests/e2e/` runs the **real** controller, worker, Redis, Celery, shared
volume and Docker-socket orchestration, with only the four GPU analysis
services swapped for `mocks/service.py` (stdlib HTTP, no weights, no GPU).
**12 scenarios, ~2.5 minutes, on a laptop.** See `tests/e2e/README.md`.

### The finding that justified the whole exercise

Every DAG job failed with:

```
ModuleNotFoundError: No module named 'dag'
  File "/app/tasks.py", line 461, in execute_workflow
    from dag.engine import DAGEngine
```

Celery's app loader puts the working directory on `sys.path` only *while it
imports the app module*, then takes it back off. So `tasks.py`'s
module-level imports resolve at boot, but the **lazy** import inside
`execute_workflow` runs later, when `/app` is no longer importable. The
package was sitting right there in the image the whole time.

**The DAG path had never once executed under a real Celery worker.** Every
"verified" claim in sections 1, 2, 2b, 3b above was verified by simulation
against a code path that could not run. The legacy `build_chain()` path was
unaffected and worked first try.

Fixed with `ENV PYTHONPATH=/app` in `worker/Dockerfile` and
`controller/Dockerfile`. Worth noting the stale comment at
`worker/tasks.py:459` justifying the lazy import as circular-import
avoidance: since the driver-dispatch work in §1, `dag.engine` no longer
imports task functions at all, so that import could simply move to module
scope and fail fast at worker boot instead of mid-job.

### Gaps confirmed empirically

- **Gap D** (double-managed containers) is real and measured: a workflow node
  declaring `service` stops `visualservice` **twice** for a single node —
  once in the task body's `finally`, once in the engine's. Scenario
  `dag-service-declared`.
- **Gap B** (incompatible `/status` shapes) demonstrated side by side from
  the same request: legacy returns
  `[audio_result, extent_result, job_id, status, summarise_result,
  tagging_result, video_name, visual_result]`; the DAG returns
  `[audio, download, final, visual]`. Scenario `status-shape-differs`.
- **Gap A** is what makes `dag-full` work at all: the workflow must be
  registered under the built-in name `full`, not a new name, or `/process`
  never routes to the DAG. Worse, `finalize_results`'s `job_success` map is
  keyed by the legacy `job_type` names, so a workflow registered under a
  novel name fails at the final node even when every other node succeeded.

### New gap I: task name vs function name

`build_chain()` refers to the tagging step as `tasks.process_tags` (the
Celery task name), but the function is `process_tagging`. The python driver
resolves a **Python attribute**, not a Celery task name, so a workflow JSON
copied from `build_chain()` fails pre-flight with `UnknownTaskError`. Only
this one task diverges. Either rename the function or teach the driver to
fall back to the Celery registry.

### Also changed

`HEALTH_CHECK_TIMEOUT` / `HEALTH_CHECK_INTERVAL` in `worker/consts.py` are
now env-overridable (defaults unchanged at 330s/60s). Without that every mock
cold start cost a full 60-second poll interval.

### Still not covered

The real services' actual HTTP contracts and payload shapes, real model
latency and VRAM behaviour, and `scripts/start_services.py`'s
`nvidia-smi`/`free -m` measured pass. The mocks encode what `worker/tasks.py`
*believes* the contracts are — if that belief is wrong, only the real
services will say so.

---

## 8. Gaps A-I closed — done (2026-09-16)

Everything §6 and §7 surfaced, except the real-service HTTP contracts, which
need the GPU stack. **140 unit tests, 13 e2e scenarios, all passing.**

### The central move: leases (gaps D and E were one problem)

`dag/readiness.py`'s `ensure_ready`/`release` are now **reference-counted,
re-entrant per thread, and concurrency-capped per service**:

- **Refcount** — the container starts on the first holder and stops on the
  last, so two concurrent nodes sharing a service produce one start and one
  stop, and neither can stop it while the other is mid-request. *(gap E)*
- **Re-entrancy** — a task body bracketing its own work *inside*
  `dag/engine.py`'s bracket is the same logical holder, not a second one.
  Without this, a node whose workflow declares `service` would deadlock
  against itself at concurrency 1. This is what let `worker/tasks.py` migrate
  off `start_service`/`stop_service` while the legacy chains keep working
  unchanged. *(gap D)*
- **Waiter-aware release** — a queued caller inherits a running service
  rather than it being stopped and cold-started again between two nodes that
  both want it.
- **Concurrency limit** — default 1, overridable via `SERVICE_CONCURRENCY`.
  `config/services.json` carries the declarative value; the env var is what
  reaches the worker today, since that file is not mounted into the image.

Still in-process only: threads in one worker process, not across workers or
hosts. Cross-process needs a Redis lock, which is the pooling work deferred
on 2026-09-09.

### The rest

| Gap | Fix |
|---|---|
| A | `finalize_results` takes `expects` (e.g. `["audio","visual"]`) from the finalize node's `kwargs`, falling back to the legacy per-`job_type` table. A workflow under a novel name now runs clean instead of failing at the last node. `DAGEngine` passes static kwargs to job-context tasks to make this reachable |
| B | One `/status` contract: both paths return `finalize_results`'s merged output. Per-node envelopes go to `{job_id}/dag_run.json` instead of onto the wire, via `DAGEngine.terminal_result()` / `run_summary()` |
| C | `workflows/full_pipeline_1.0.json` declares `service` on its visual and audio nodes, so the readiness layer is actually reachable from the shipped pipeline |
| D | Solved by re-entrant leases, above |
| E | Solved by refcounted leases, above |
| F | `_single_host_start`/`_single_host_stop` wrap the `utils` import and raise `ServiceNotReadyError`, so a layout change cannot bypass `on_failure` |
| G | `Parser` rejects duplicate task ids instead of letting networkx silently merge two tasks into one |
| H | CLAUDE.md corrected: `build_chain()` is a sequential *chain*, not a chord; `/process` takes a JSON body, not query params. Leases, `expects`, the `/status` contract and the new env vars documented |
| I | The tagging function is renamed `process_tags` to match its Celery task name, so a workflow copied from `build_chain()` resolves |

### Verification

Three unit tests that were `xfail(strict=True)` gap markers flipped to XPASS
the moment the fix landed and forced their own rewrite — which is what the
strict marker was for. No `gap` markers remain.

### What is left

- **#4, `build_chain()` retirement.** Now genuinely optional rather than
  blocking: `/status` shapes match, and any `job_type` can be expressed as a
  registered workflow. Retiring it means shipping a workflow per legacy
  `job_type` and deleting the fallback branch in `controller/main.py`.
- **`auth` field/injection** (the remainder of §3).
- **`execute_parallel()`** — needs the aggregate VRAM check, not a lease.
- **Cross-process leases** — only if a second worker or `--concurrency>1`
  ever arrives.
- **The real services' HTTP contracts.** The mocks encode what
  `worker/tasks.py` believes they are. Only the GPU stack can confirm it.

---

## 9. Node-level retry, and coverage for the rest — done (2026-09-16)

### Gap J: the DAG path had silently lost retries

`download_file` carries `autoretry_for=(Exception,), max_retries=3,
retry_backoff=True`. Same missing file, both routes:

```
LEGACY  download_file  retry in 1s -> retry in 0s -> retry in 1s -> raised
DAG     execute_workflow raised TaskExecutionError immediately
```

Celery's autoretry only engages when a task is *dispatched*. The python
driver resolves the function and calls it directly, so the body runs but the
retry machinery never does — the exact case the retry exists for (a flaky S3
or HTTP fetch) failed the whole job on first attempt. The trap generalises:
**task decorators are inert on the DAG path.**

Fixed in `DAGEngine`, not on the Celery task, and deliberately so:
`tasks.execute_workflow` is one Celery task covering the entire DAG, so a
Celery-level retry would re-run every node — including the GPU ones — to
recover from one transient download. Retry has to be per node to be useful.

- `settings.retries` sets a workflow default, a node's `retries` attribute
  overrides it, default 0 (unchanged behaviour).
- `settings.retry_backoff` / `retry_backoff_max`: doubling delay, capped.
- A retry re-attempts the whole bracket **including service acquisition**,
  so a node whose service failed to start gets a fresh cold start rather
  than the same broken container.
- Deterministic failures (`PayloadConflictError`) are not retried.
- `retries` is validated at pre-flight alongside task names.

The shipped workflows declare `retries: 3` on their download node, restoring
parity with the legacy chain.

### Coverage closed

5 of the 7 job types had only ever run as legacy chains, and
`speaker_extent`/`segment_extent` had never executed at all. All seven now
run as registered DAG workflows in `tests/e2e/workflows/`, and
`callback_url` delivery is verified against a permanently-running mock sink.

### Still not covered

`s3://` downloads, `run_at_ms` ETA scheduling, `DEPLOYMENT_MODE=multi_host`
end-to-end, `execute_parallel()` in production, and the real services' HTTP
contracts. Also worth remembering that the mocks were written *from*
`worker/tasks.py`, so they prove the code self-consistent, not correct: if
`process_audio` misparses a real response, the mock returns exactly what the
parser expects.
