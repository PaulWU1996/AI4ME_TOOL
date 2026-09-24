# DAG parser & canvas composer

Reference doc for `dag/parser.py` and `dag/compose.py`: what each module
does today. The old custom execution engine (`dag/engine.py`,
`dag/envelope.py`, `dag/drivers/`) was deleted when orchestration moved to
a controller-built Celery canvas — Celery's `chain`/`group` primitives are
the DAG runtime now.

## `dag/parser.py` — validate the workflow JSON

- Builds a `networkx.DiGraph` from a workflow's `tasks` list; `depends_on`
  becomes edges, validated against declared ids and checked for cycles.
- Retains every top-level field on a task object (`driver`, `url`, `func`,
  `module`, `kwargs`, `service`, `retries`, ...) as node attributes, with
  the legacy nested `attributes` dict merged first so a top-level field
  wins on conflict.
- Rejects a duplicate task `id` outright (`ValueError`) instead of letting
  two tasks silently merge into one node.
- Retains `workflow`/`settings` top-level metadata on the `Parser`
  instance (`.metadata`, `.settings`).

## `dag/compose.py` — turn the DAG into a Celery canvas (runs in controller)

- `topological_layers(dag)`: assigns every node to exactly one layer with
  `layer[n] = 0` for roots, else `1 + max(layer[p] for p in n's
  predecessors)`. Single assignment means each task appears in exactly one
  step; every predecessor sits in a strictly earlier layer, so running the
  layers top-to-bottom in a `chain` satisfies every dependency.
- `build_task_map(parser, job_context)`: maps each node to a Celery task
  signature.
  - A node declaring `call: "kwargs"` (`download_file`, `finalize_results`)
    becomes an *immutable* signature carrying the job-context slice named in
    its `inject` list (`path`, `prompts`, `job_id`, `job_type`,
    `callback_url`) merged over its static `kwargs`; immutable so a
    predecessor's result is never passed positionally. `requires` fails the
    build fast if any injected key is missing.
  - A node declaring `driver: "http"` collapses to the generic
    `tasks.http_call` task with the call parameters baked in (url, method,
    headers, timeout, file upload field, and the `service` whose lifecycle
    the worker brackets around the request); the predecessor's result arrives
    positionally as the request payload. Three optional fields shape a
    node's role in the chain: `body` merges static JSON overrides into the
    payload (e.g. pin a per-node `job_type`), `save` persists the parsed
    response to disk as `{basename(file_path)}_{save}.json`, and `merge`
    passes `{**payload, **response}` downstream so later nodes keep the
    workflow context (`job_id`/`prompts`/`file_path`).
  - Every other node becomes a positional signature — the old
    merged-predecessor-payload convention is Celery's own argument passing
    now: a node after a single predecessor receives that result; a node
    after a group receives the aggregated result list.
- `build_canvas(dag, task_map)`: one `chain(*steps)`; a layer with more
  than one node becomes a `group` (parallel siblings), a single-node layer
  is just the task. Celery auto-upgrades a group followed by another step
  to a chord, so fan-in needs no hand-built chord.
- Retry is not composed here: it lives on the worker tasks themselves
  (`download_file` carries `autoretry_for=(Exception,)`, `max_retries=3`,
  backoff), matching the transient-`download` `retries` the workflows
  declare.

## `/status` contract

A workflow's `terminal: true` node's output is what `/status` returns (via
`finalize_results`, matching the shape the legacy chains returned). The
per-node envelope map the old engine wrote to `{job_id}/dag_run.json` is
gone; node-level detail lives in worker logs.

## Registered workflows

Eight workflows exercise this today:

- `full` — `download` → `visual` → `audio` → `final` (pure chain; audio
  depends on visual), expects audio + visual.
- `full_http` — `download` → parallel `group(visual, audio)` via
  `tasks.http_call`, no terminal node (the group result is the `/status`
  result).
- `audio_only`, `visual_only`, `tagging`, `summarise`,
  `speaker-extent-summarise`, `utterance-extent-summarise` — chain
  equivalents re-added to restore the legacy `job_type` names.