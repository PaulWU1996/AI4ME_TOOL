# Real-GPU test runbook

**The question this session answers:** do the real analysis services behave
the way `worker/tasks.py` believes they do?

Everything else is already tested. 155 unit tests and 21 end-to-end scenarios
cover the orchestration, and they pass. But the mock services were written
*from* `worker/tasks.py`, so they prove the code self-consistent, not correct.
If `extract_flat_captions` misreads the real XML, or the audio service's
timestamps are not the SRT-style strings `process_audio` splits on `","`,
every one of those tests still passes and production still breaks.

Only this session can falsify that. Budget **2-3 hours**.

---

## Before you start

```bash
git pull                       # you want c6ce8f1 or later
./scripts/gpu_preflight.sh
```

It is read-only — starts nothing, builds nothing. Resolve every **BLOCK**
before continuing. Two **WARN**s matter more than they look:

**`AI4ME_ADMIN_PASSWORD` is not in `.env`.** Compose interpolates it into
*both* the worker's `ADMIN_KEY` and visualservice's, so both currently get
`""`. They match, so the key handshake may work by accident — but an empty
`X-Admin-Key` header is fragile, and some proxies drop empty headers. Set it
properly:

```bash
echo 'AI4ME_ADMIN_PASSWORD=<pick-one>' >> .env
```

**You must build locally.** The images in ECR predate the `PYTHONPATH` fix in
`c83ebcd` and will fail *every* DAG job with `ModuleNotFoundError: No module
named 'dag'`. A plain `up -d` pulls them.

```bash
docker compose up --build -d redis controller worker
docker compose logs worker | tail -20      # expect the task list, no tracebacks
```

Pick a **short** video — 30-60 seconds. You are testing contracts, not
throughput, and a long clip turns every iteration into a coffee break.

```bash
cp <your-clip>.mp4 ./data/sample.mp4
```

---

## Phase 1 — Legacy baseline (~15 min)

Establish that the real services work at all, *before* involving the DAG. This
separates "the services behave unexpectedly" from "the DAG is wrong", and you
cannot debug the second while the first is unknown.

`workflows/registry.json` registers `full_pipeline`, not `full`. So on one
stack, with no reconfiguration:

| `job_type` | Route |
|---|---|
| `full` | legacy `build_chain()` |
| `full_pipeline` | DAG engine |

That is the A/B, and it is free.

```bash
curl -X POST http://localhost:9000/process \
  -H 'Content-Type: application/json' \
  -d '{"path": "/app/data/sample.mp4", "job_type": "full"}'

# then poll
curl -s http://localhost:9000/status/<job_id> | python3 -m json.tool
```

**Gate:** `status: SUCCESS`, and `shared/<job_id>/` contains
`sample_visual_output.json`, `sample_audio_output.json`, `task_info.txt`.

If this fails, stop. Nothing below is meaningful, and the failure is in the
services or the environment, not in this week's work.

---

## Phase 2 — Capture the real contracts (~20 min)

**The highest-value step of the day.** Do it while the services are warm.

```bash
docker compose --profile on-demand up -d visualservice audioservice
sleep 60    # they load multi-GB weights; watch: docker ps

python3 scripts/capture_contracts.py \
  --video ./data/sample.mp4 \
  --video-rel "<job_id-from-phase-1>/sample.mp4" \
  --admin-key "$AI4ME_ADMIN_PASSWORD"
```

It talks to the services directly (never enqueues a job), saves every raw
response to `contracts/<timestamp>/`, and checks each assumption
`worker/tasks.py` makes:

- `/generate` returns an `api_key`
- `/analyze` returns XML nesting `VideoAnalysis/Segments/Segment` with
  `StartTime`, `EndTime`, `Description`
- `/process_audio/` returns `output[]` whose `start`/`end` are strings
  containing a comma
- `/process/` returns JSON on both script services

Exit code 0 means every assumption held. Any mismatch is printed with the
captured body next to it.

**Commit `contracts/<timestamp>/` either way.** If the assumptions held, it is
the evidence the mocks are modelled on reality. If they did not, it is exactly
what is needed to correct the parser and the mock — without another GPU
booking.

The tool is self-tested: it reports 11/11 OK against the mock services, so a
mismatch tomorrow is a real difference, not a bug in the tool.

It also writes `contracts/<timestamp>/manifest.json`, recording the local
image ID of `audioservice:latest`/`visualservice:latest` at capture time.
Run `python3 scripts/check_contract_freshness.py` (read-only, no GPU needed)
any time before trusting a job's output — it flags either image if it no
longer matches the last committed capture, which is the signal to come back
and re-run this phase before the drift causes a silent regression like the
ones this session found.

---

## Phase 3 — The same video through the DAG (~20 min)

```bash
curl -X POST http://localhost:9000/process \
  -H 'Content-Type: application/json' \
  -d '{"path": "/app/data/sample.mp4", "job_type": "full_pipeline"}'
```

**Gates, in order of importance:**

1. `status: SUCCESS` — if it is `ModuleNotFoundError: No module named 'dag'`,
   you are running a stale image. Rebuild.
2. The `/status` body has the **same keys** as Phase 1 (`audio_result`,
   `visual_result`, `video_name`, `status`, …). Both paths are supposed to
   return `finalize_results`' merged output.
3. `shared/<job_id>/dag_run.json` exists and every node reads
   `"status": "success"`.
4. **Compare the two runs' outputs.** Same video, two engines — the analysis
   payloads should be equivalent:

```bash
diff <(python3 -m json.tool shared/<legacy_job>/sample_visual_output.json) \
     <(python3 -m json.tool shared/<dag_job>/sample_visual_output.json)
```

A difference here is the single most interesting result of the day and means
the DAG feeds the services something subtly different.

5. Confirm the container was cycled **once**, not twice — the lease should
   collapse the engine's bracket and the task's:

```bash
docker compose logs worker | grep -c "Stopping visualservice"
```

---

## Phase 4 — The other job types (~30 min)

`summarise` and `tagging` need a transcript JSON, not a video. Only run these
if `transcriptservice`/`taggingservice` are available on this machine.

```bash
for jt in audio_only visual_only; do
  curl -X POST http://localhost:9000/process -H 'Content-Type: application/json' \
    -d "{\"path\": \"/app/data/sample.mp4\", \"job_type\": \"$jt\"}"
done
```

These go through `build_chain()` (they are not in the registry). To run them
as DAGs, register the templates the e2e suite already uses:

```bash
for f in tests/e2e/workflows/audio_only_1.0.json tests/e2e/workflows/visual_only_1.0.json; do
  curl -X POST http://localhost:9000/workflows -H 'Content-Type: application/json' --data-binary @$f
done
```

Note those templates declare `expects` and `service`; that is deliberate and
is what makes a workflow under a non-legacy name work at all.

---

## Phase 5 — Lifecycle under real load (~30 min)

The mock services start in milliseconds. These take minutes, which is the
regime the health-check and lease logic was actually written for and has
never been observed in.

1. **Cold start timing.** With no service running, submit a job and time how
   long `start_service` waits. `HEALTH_CHECK_TIMEOUT` defaults to 330s — if a
   real cold start approaches that, raise it *before* it bites in production.
2. **Keepalive.**
   ```bash
   ./scripts/start.sh --keepalive audioservice,visualservice
   ```
   Then run two jobs back to back and confirm the containers stay up and the
   second job skips the cold start entirely. Note `worker/utils.py` reads
   `service_modes.json` **once at import**, so the worker must be restarted
   after changing modes.
3. **Back-to-back jobs.** Submit two jobs in quick succession and confirm the
   second waits for the first rather than both driving the same GPU
   container. Watch `holders` behaviour in the worker log.

---

## If something fails

Capture this before restarting anything — a rebuilt stack loses all of it:

```bash
mkdir -p /tmp/failure && cd /tmp/failure
docker compose logs --no-color worker      > worker.log
docker compose logs --no-color controller  > controller.log
docker logs visualservice                  > visual.log 2>&1
docker logs audioservice                   > audio.log 2>&1
cp -r <repo>/shared/<job_id> ./workspace
```

`shared/<job_id>/dag_run.json` names the failing node and carries its error
envelope; `task_info.txt` records what `finalize_results` expected versus what
it found.

---

## Rollback

The legacy path is untouched by this week's work, and is the default for every
`job_type` that is not in the registry. To disable the DAG entirely:

```bash
echo '{}' > workflows/registry.json
docker compose restart controller
```

Every request then routes through `build_chain()`, exactly as before. Nothing
else needs reverting.

---

## What to watch for, ranked by likelihood

| Risk | Signal | Response |
|---|---|---|
| Stale ECR image without `PYTHONPATH` | `ModuleNotFoundError: No module named 'dag'` | rebuild with `--build` |
| Empty `ADMIN_KEY` rejected by the real narrative-api | 401/403 on `/generate` | set `AI4ME_ADMIN_PASSWORD` in `.env` |
| Real XML nests differently | `visual_result: []`, no error | `contracts/` capture shows the true shape |
| Audio timestamps not comma-separated strings | `AttributeError` on `.split` in `process_audio` | same |
| Real cold start exceeds 330s | node fails with "failed to become healthy" | raise `HEALTH_CHECK_TIMEOUT` |
| Stale `data/api.key` after a volume reset | 401 on `/analyze`, then automatic recovery | already handled — confirm the log says "API key rejected; regenerating" |

---

## Definition of done

- [ ] Phase 1 green — the real services work
- [ ] `contracts/<timestamp>/` captured **and committed**, mismatches or not
- [ ] Phase 3 green, and the two engines' outputs match
- [ ] Any contract mismatch written up, with `mocks/service.py` corrected to match reality

The third box is the one that changes the project's status from "reliable
foundation" to "reliable". The second is the one that keeps tomorrow's work
from having to be repeated.
