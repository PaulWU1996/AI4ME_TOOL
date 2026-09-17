# Test & deployment plan

Where this project stands after real-GPU validation (2026-09-17,
`SCOPE_PLAN.md` §11) and what's left before the DAG path can replace the
legacy chains in production. Companion to `GPU_TEST_RUNBOOK.md` (how to
re-run the validation) and `SCOPE_PLAN.md` (the detailed, dated log of what
shipped and why).

## Where things stand

| Layer | Status |
|---|---|
| Orchestration logic (`dag/`, `worker/tasks.py`) | Unit-tested (155 tests) + mock e2e-tested (21 scenarios) + real-GPU-tested (Phases 1, 2, 3, 5) |
| Real service contracts (`visualservice`, `audioservice`) | Verified against actual responses, two real bugs found and fixed in the external images, two fixed in this repo's own config |
| `full_pipeline` (production DAG template) | Verified end-to-end, legacy-vs-DAG output byte-identical, service leases confirmed to collapse to one start/stop |
| `full_pipeline_http` (new, concurrent) | Verified working, genuine concurrency proven — but a standalone proof-of-concept, not wired into production job types |
| `summarise`/`tagging` job types | Untested this round — `transcriptservice`/`taggingservice` images unavailable on this host |
| Multi-host deployment mode | Simulated only, never run against a real second host |

## Near-term (next session, no new infra needed)

1. **Get `transcriptservice`/`taggingservice` images loaded and run Phase 4** of `GPU_TEST_RUNBOOK.md` — same class of risk applies (mock contracts vs. real ones) and is currently completely unverified for these two services.
2. **Re-run `capture_contracts.py`** whenever `narrative-api` or `audioservice` gets a new build — both real bugs this session found (`audio_description` KeyError, the `rmtree` scope bug) were silent (200 OK, no error) and could regress without anyone noticing. This is a five-minute check that would have caught both immediately.
3. **Correct `mocks/service.py`** against the now-confirmed real contracts (the runbook's own "definition of done" item still open): the mocks currently encode `worker/tasks.py`'s beliefs, which this session showed were right about payload shapes but blind to failure modes that return `200` with a broken body. Worth adding a mock scenario for "200 but semantically empty/error" to catch this class of bug in the unit/e2e suites going forward, rather than only on real hardware.
4. **Decide on the 30-second audio-segmentation boundary bug** (any video landing within ~1s of a multiple of 30s crashes `audioservice`'s whisper encoder on the trailing chunk) — either request an upstream fix, or add defensive handling on the worker side (e.g., detect and merge a near-empty trailing segment before sending it).

## Medium-term (real design work, not just running the stack again)

5. **Retrofit `process_visual`/`process_audio` onto the `service`-attribute + engine-level lifecycle mechanism** (`SCOPE_PLAN.md` §3). This is the actual prerequisite for enabling `execute_parallel()` on the *production* `full_pipeline` template — §11's proof-of-concept (`full_pipeline_http`) validated the mechanism and the GPU-pinning approach, but the real pipeline's nodes still self-manage their own `start_service`/`stop_service`/`ensure_api_key()` calls internally, invisible to `DAGEngine`. Blocked on `build_chain()` retirement for the reasons already logged in §3's "sequencing problem."
6. **Add a coded aggregate-VRAM feasibility check**, not just a manually-verified GPU pinning. §11 hand-verified that `audioservice` (GPU 0) and `visualservice` (GPU 1) coexist safely on *this* host's specific 2×A5000 layout; that reasoning doesn't travel to a different host without someone re-deriving it. `config/services.json` already carries real measured `vram_mb` per service (§11) — the missing piece is `DAGEngine` (or a pre-flight step) summing a generation's coldstart services' VRAM against host capacity before deciding to run them concurrently, falling back to sequential if it doesn't fit, per the design already sketched in §3.
7. **Make GPU device assignment configurable, not hardcoded.** `docker-compose.yml`'s `device_ids: ["0"]` / `["1"]` are specific to this host's 2×A5000+1×T400 layout. A host with a different GPU mix (count, size, or a single GPU) needs different values, and nothing currently detects or validates that at startup — a service could silently land on the wrong device again on a new machine, reproducing exactly the bug §11 spent most of its time on. Consider folding device selection into `config/services.json` (already the per-service resource registry) and having `scripts/start_services.py`'s pre-check validate it against `nvidia-smi` output, the same way it already validates aggregate VRAM.
8. **`build_chain()` retirement** (`SCOPE_PLAN.md`, cross-cutting section) — still not started, and now blocks more than it did before: envelope migration step 4, the rest of §3, and the production `execute_parallel()` rollout (item 5 above) all wait on it.

## Deployment considerations

- **Keepalive mode is now empirically safe** for `audioservice`+`visualservice` together on this host (§11), but only *after* the GPU-pinning fix — deploying keepalive on a different host without first confirming its GPU layout the same way would risk reproducing the exact silent-corruption bug this session found (HTTP 200, valid output shape, wrong content).
- **The worker-restart-after-config-change gotcha is real and easy to hit operationally**, not just during interactive testing: any deploy process that edits `docker-compose.yml`, `.env`, or writes `service_modes.json` without also force-recreating the worker container will silently leave it running on stale config. Worth a deploy-time check (e.g., compare the worker's `docker inspect ... StartedAt` against the config files' mtimes) rather than relying on operators remembering the caveat.
- **`scripts/start_services.py` needs `pip install docker` on the host** running it — currently undocumented; add to a setup doc or a `scripts/requirements.txt` before anyone else tries to run it cold.
- **Multi-host mode (`DEPLOYMENT_MODE=multi_host`) has never been run against an actual second host.** Everything validated this session was single-host. Before deploying across hosts, at minimum: confirm the shared volume assumption (`SHARED_PATH`, `API_KEY_PATH`) either holds via a real shared filesystem or is replaced by the same "treat as external context" approach §11 used for the http driver's API key.
- **External-image regression risk**: both `narrative-api` and `audioservice` bugs found this session were silent by design (the services caught their own internal exceptions and returned success-shaped output). Any future image update to either should be re-verified with `capture_contracts.py` before being trusted in production, not assumed compatible because it loads and passes health checks.
