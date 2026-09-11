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
| 3. Service lifecycle + auth generalization | **Partially done** — readiness (§3b) shipped; `auth` field/injection still not started |
| 4. Retire `build_chain()` / legacy Celery chains | **Not started** — blocks the rest of #2, and most of #3 |
| Live `docker-compose up --build` verification | **Never done** — everything below is verified by simulation/dry-run, not a real run |
| `execute_parallel()` enablement | **Deferred** — needs #3's resource-feasibility check first |
| 5. Multi-instance service pooling / orchestrator migration | **Explicitly deferred, not scoped** — decided 2026-09-09, see note below |
| 3b. Deployment-mode split: readiness strategy (single-host vs multi-host) | **Done** — implemented 2026-09-11, see §3b below |

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
