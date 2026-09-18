# DAG engine & parser — original work vs. current state

Reference doc for `dag/parser.py` and `dag/engine.py`: what the very first
version did, what changed and why, and what the two modules actually do
today. No findings/bugs here — those live in `SCOPE_PLAN.md` (dated,
numbered sections) and `SCOPE_PLAN.md` §11 (real-GPU validation). This is
purely the architecture, as it stands.

## Original work (`6d3adcf`, 2026-09-07)

The commit that introduced `dag/` at all. Three things define it:

**`Parser.parse()` kept almost nothing.** It copied a task's legacy
`attributes` dict and, if present, the literal `task` field — nothing
else:

```python
for task in tasks:
    node_attributes = dict(task.get('attributes', {}))
    if 'task' in task:
        node_attributes['task'] = task['task']
    self.dag.add_node(task['id'], **node_attributes)
```

No `driver`, `url`, `func`, `module`, `service`, or `retries` field on a
task object ever reached the graph. `workflow`/`settings` top-level JSON
metadata wasn't retained at all — the `Parser` object had no `.metadata`,
no `.settings`. No duplicate-id check, so two tasks sharing an `id` would
silently collapse into one node via networkx's own merge-on-`add_node`
behaviour.

**`DAGEngine` dispatched through a hardcoded registry**, not a driver:

```python
TASK_REGISTRY = {
    "download_file": download_file,
    "process_visual": process_visual,
    "process_audio": process_audio,
    "finalize_results": finalize_results,
    "process_summarise": process_summarise,
    "process_tags": process_tagging,
    "speaker_extent": speaker_extent,
    "segment_extent": segment_extent,
    "transcript_to_text": transcript_to_text,
}
```

A node's `task` string looked itself up in this dict and got called
directly. There was no concept of a step that wasn't a Python function
already imported into `dag/engine.py` — an HTTP-only call, for instance,
had nowhere to live. `dag/drivers/http.py` and `dag/drivers/python.py`
already existed as files in this same commit, empty (0 bytes) — the
driver split was evidently the intended design from day one, just not
built yet.

**No result envelope, no service lifecycle, no retry.** `execute_node`
returned whatever the task function returned, raw. Nothing started or
stopped a GPU container on the engine's behalf — that lived entirely
inside `process_visual`/`process_audio`'s own bodies, invisible to the
graph. A failing node raised immediately; nothing retried it.

**`execute_parallel()` already existed, unused.** The topological-
generations + thread-pool method is present in this very first commit,
byte-for-byte the same algorithm as today. Nothing ever called it —
`execute_workflow` (in `worker/tasks.py`) only ever called `execute()`.
It shipped as a capability with no caller for its entire history until
this session.

## What changed, and when

| | Original (`6d3adcf`, 09-07) | `bfb1ba9` (09-11) | `c83ebcd` (09-16) | Today (`10dd009`, 09-17) |
|---|---|---|---|---|
| **Parser field retention** | `attributes` + `task` only | — | — | (unchanged since `bfb1ba9`, see below) |
| **Dispatch** | hardcoded `TASK_REGISTRY` dict | `driver` attribute → `dag/drivers/{python,http}.py`, resolved by `importlib` | — | — |
| **Result shape** | raw return value | uniform `{status, data, error}` envelope (`dag/envelope.py`) | — | http driver also returns raw XML/text as envelope `data` when a response isn't JSON |
| **Service lifecycle** | inline in task bodies, invisible to the engine | `dag/readiness.py`: `ensure_ready`/`release`, single-host vs. multi-host strategy behind `DEPLOYMENT_MODE` | refcounted + re-entrant + concurrency-capped leases (fixes double-start/double-stop) | unchanged |
| **Retry** | none | none | per-node, engine-level, with backoff (`settings.retries`/`retry_backoff`) | unchanged |
| **`/status` contract** | N/A (DAG path unrunnable — see `SCOPE_PLAN.md` §7) | N/A | one shape for legacy and DAG (`finalize_results`'s merged output); per-node detail moved to `dag_run.json` | unchanged |
| **`execute_parallel()`** | present, uncalled | present, uncalled | present, uncalled | **called** — `execute_workflow` now honors `settings.parallel: true` |
| **Auth for a driver-called service** | N/A | flagged as open (`SCOPE_PLAN.md` §3: `config/services.json` `auth` field, not implemented) | still open | resolved *for the http driver specifically*: treated as static, externally-provisioned context (a literal header value in the workflow JSON), not something the driver generates or rotates |
| **File upload from a driver** | N/A | http driver: JSON body only | — | added multipart support (`file_field`/`file_path_key`), needed to call `visualservice`'s raw-upload `/analyze` |

`bfb1ba9` is the commit where the driver split, the envelope, and service
readiness went from "files exist" to "implemented and wired through
`dag/engine.py`" — it's also where `SCOPE_PLAN.md` itself was born, as the
running log of this evolution.

## Current status — what `dag/parser.py` and `dag/engine.py` do today

**`dag/parser.py`**
- Builds a `networkx.DiGraph` from a workflow's `tasks` list; `depends_on`
  becomes edges, validated against declared ids and checked for cycles.
- Retains every top-level field on a task object (`driver`, `url`, `func`,
  `module`, `kwargs`, `service`, `retries`, ...) as node attributes, with
  the legacy nested `attributes` dict merged first so a top-level field
  wins on conflict.
- Rejects a duplicate task `id` outright (`ValueError`) instead of letting
  two tasks silently merge into one node.
- Retains `workflow`/`settings` top-level JSON metadata on the `Parser`
  instance (`.metadata`, `.settings`), read by `DAGEngine`'s constructor
  and by `execute_workflow` (e.g. `settings.parallel`, `settings.retries`,
  `settings.on_failure`).

**`dag/engine.py`**
- `_validate_task_names()` pre-flight-checks every node before any node
  runs: driver resolves, python targets import and expose the named
  function, http nodes declare a `url`, `retries` is a valid non-negative
  int, and a node declaring `service` has a valid `DEPLOYMENT_MODE`.
- `execute_node()` reads a node's `driver` (default `"python"`), builds
  its `inputs` (job-context kwargs for `download_file`/`finalize_results`,
  or the merged predecessor payload plus static `kwargs` for everything
  else), and dispatches to `dag/drivers/python.py` or `dag/drivers/http.py`
  — both always return an envelope, so the engine never branches on driver
  type past this point.
- If a node declares `service`, the driver call is wrapped by
  `dag/readiness.py`'s `ensure_ready`/`release` — reference-counted,
  re-entrant per thread, concurrency-capped per service
  (`SERVICE_CONCURRENCY`), so a node's own lifecycle management and the
  engine's bracket collapse into one holder rather than double-managing
  the container.
- A failed node retries up to `retries` times (node attribute, falling
  back to `settings.retries`, default 0) with doubling backoff, each
  retry re-attempting the whole bracket including service acquisition.
  `on_failure` (`"stop"`/`"continue"`, from `settings`) decides whether a
  node's final failure halts the run.
- `execute()` walks nodes in plain topological order, one at a time.
  `execute_parallel()` groups nodes into topological generations and runs
  each generation's nodes concurrently via a thread pool, generations in
  order — same algorithm since the original commit, now actually
  reachable in production via `worker/tasks.py`'s `if
  parser.settings.get("parallel", False)` branch.
- `terminal_result()` returns `finalize_results`'s merged output when a
  workflow has one (matching the legacy chains' `/status` shape); the full
  per-node envelope map otherwise. `run_summary()` (order, executed set,
  per-node envelopes) is written to `{job_id}/dag_run.json` regardless, so
  node-level detail is never lost even when the wire response is the
  merged summary.

**Two registered workflows exercise this today**: `full_pipeline`
(sequential, `process_visual`/`process_audio` as python-driver nodes that
self-manage their own service lifecycle) and `full_pipeline_http`
(`settings.parallel: true`, `visual`/`audio` as sibling http-driver nodes
whose lifecycle is entirely engine-managed via `service`). Both have run
successfully against the real GPU stack.
