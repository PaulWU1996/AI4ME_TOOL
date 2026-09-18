#!/usr/bin/env python3
"""End-to-end run of the real controller/worker/redis stack against mock
analysis services.

Everything is real except the four GPU services: real FastAPI, real Celery,
real Redis, real Docker-socket container orchestration, real shared volume.
The analysis services are mocks/service.py — so a full pipeline that would
take ~20 GPU-minutes finishes in seconds and can be made to fail on demand.

    ./venv/bin/python tests/e2e/run_e2e.py            # all scenarios
    ./venv/bin/python tests/e2e/run_e2e.py --keep     # leave the stack up
    ./venv/bin/python tests/e2e/run_e2e.py -k legacy  # one scenario

Exit code is 0 only if every scenario passed.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMPOSE_FILE = os.path.join("tests", "e2e", "docker-compose.mock.yml")
PROJECT = "ai4me_mock"
CONTROLLER = "http://localhost:19000"
CALLBACK_SINK = "http://localhost:19005"
VISUAL_SINK = "http://localhost:19001"
AUDIO_SINK = "http://localhost:19002"
# As the worker sees it, on the compose network.
CALLBACK_URL = "http://callbacksink:8000/callback"
WORKFLOWS_DIR = os.path.join(REPO, "tests", "e2e", "workflows")
REGISTRY = os.path.join(WORKFLOWS_DIR, "registry.json")
# tests/e2e/-scoped, not the repo-root data/ and shared/ -- those are
# production's real job workspace and cached visual API key
# (docker-compose.mock.yml has the full reasoning). Matching that here is
# what actually makes it not collide: this is where the mock stack's
# volumes point, and what the scenarios below read/write directly.
DATA_DIR = os.path.join(REPO, "tests", "e2e", "data")
SHARED_DIR = os.path.join(REPO, "tests", "e2e", "shared")

CORE_SERVICES = ["redis", "controller", "worker", "callbacksink"]
MOCK_SERVICES = ["visualservice", "audioservice", "transcriptservice", "taggingservice"]
# compose()'s -s/service-name arguments above are compose-file keys, safely
# project-scoped by PROJECT regardless of what a container is actually
# named. These are the container_name values docker-compose.mock.yml
# actually gives them -- required wherever this script talks to the Docker
# daemon directly (docker inspect / docker rm) rather than through
# `compose(...)`, since that bypasses project scoping and a stale literal
# name here reaches whatever container that name belongs to on the host,
# mock or not.
MOCK_CONTAINER_NAMES = {name: f"mock-{name}" for name in MOCK_SERVICES}

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


# --------------------------------------------------------------------------
# Shell / compose helpers
# --------------------------------------------------------------------------

def run(cmd, check=True, capture=False, env=None):
    printable = " ".join(cmd)
    print(f"{DIM}$ {printable}{RESET}")
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(
        cmd, cwd=REPO, check=False, env=merged,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )
    if check and result.returncode != 0:
        if capture and result.stdout:
            print(result.stdout)
        raise SystemExit(f"command failed ({result.returncode}): {printable}")
    return result


def compose(*args, check=True, capture=False, env=None):
    return run(
        ["docker", "compose", "-f", COMPOSE_FILE, "--project-directory", ".", "-p", PROJECT, *args],
        check=check, capture=capture, env=env,
    )


def container_state(name):
    result = run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name],
        check=False, capture=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "absent"


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def http(method, url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except urllib.error.URLError as e:
        return None, str(e)
    except OSError as e:
        # Connection reset / refused while uvicorn is still binding.
        return None, str(e)


def wait_for_controller(timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http("GET", f"{CONTROLLER}/status/probe-not-a-real-job", timeout=5)
        if status is not None:          # any HTTP answer means uvicorn is serving
            return
        time.sleep(1)
    raise SystemExit("controller never came up on :19000")


def poll_job(job_id, timeout=180):
    """Poll /status until the job leaves the running states."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, body = http("GET", f"{CONTROLLER}/status/{job_id}", timeout=10)
        if status == 200 and isinstance(body, dict):
            last = body
            if body.get("is_ready"):
                return body
        time.sleep(1)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s; last status: {last}")


def submit(path, job_type="full", **extra):
    status, body = http("POST", f"{CONTROLLER}/process", {"path": path, "job_type": job_type, **extra})
    assert status == 200, f"/process returned {status}: {body}"
    return body["job_id"]


# --------------------------------------------------------------------------
# Fixtures (generated, never committed as binaries)
# --------------------------------------------------------------------------

def write_fixtures():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(SHARED_DIR, "api-data"), exist_ok=True)

    video = os.path.join(DATA_DIR, "mock_video.mp4")
    with open(video, "wb") as f:
        # Not a real MP4 — nothing in the mock stack decodes it. It only has
        # to exist, be copyable onto the shared volume, and be non-trivially
        # sized so the multipart upload path is genuinely exercised.
        f.write(b"\x00\x00\x00\x18ftypmp42" + (b"MOCKVIDEO" * 4096))

    transcript = os.path.join(DATA_DIR, "mock_transcript.json")
    segments = []
    for i in range(6):
        segments.append({
            "startMs": i * 4000,
            "endMs": (i + 1) * 4000,
            "text": f"Mock utterance number {i} for the transcript pipeline.",
            "speaker": f"S{i % 2}",
        })
    with open(transcript, "w") as f:
        json.dump({"segments": segments}, f, indent=2)

    return video, transcript


def reset_registry():
    """Return the tracked registry to empty and delete the workflow files a
    run registered, so an e2e run leaves no diff behind in the repo."""
    with open(REGISTRY, "w") as f:
        json.dump({}, f)
        f.write("\n")

    seeded = {
        "full_1.0.json", "full_2.0.json", "summarise_1.0.json", "audio_only_1.0.json",
        "visual_only_1.0.json", "tagging_1.0.json",
        "speaker-extent-summarise_1.0.json", "utterance-extent-summarise_1.0.json",
    }
    for entry in os.listdir(WORKFLOWS_DIR):
        if entry.endswith(".json") and entry != "registry.json" and entry not in seeded:
            os.remove(os.path.join(WORKFLOWS_DIR, entry))


def clean_api_key():
    """The worker caches its visual-service key in data/api.key, which
    survives a shared-volume wipe. Clearing it keeps runs reproducible."""
    key_path = os.path.join(DATA_DIR, "api.key")
    if os.path.exists(key_path):
        os.remove(key_path)


def clean_shared():
    for entry in os.listdir(SHARED_DIR):
        full = os.path.join(SHARED_DIR, entry)
        if entry == "api-data":
            continue
        shutil.rmtree(full, ignore_errors=True) if os.path.isdir(full) else os.remove(full)


def set_mock_control(spec):
    """Write the mock services' runtime control file onto the shared volume.

    Injecting behaviour by env var does not work here: the worker cold-starts
    these containers itself from the compose file, so a manually created
    container carrying MOCK_*_FAIL_MODE is thrown away and replaced by a
    default one the moment a job runs. A file on the shared volume survives,
    and the mocks re-read it per request.
    """
    path = os.path.join(SHARED_DIR, "mock_control.json")
    if spec:
        with open(path, "w") as f:
            json.dump(spec, f)
    elif os.path.exists(path):
        os.remove(path)


def worker_log_mark():
    """Length of the worker log right now, to slice later output against."""
    return len(compose("logs", "--no-log-prefix", "worker", capture=True).stdout or "")


def worker_log_since(mark):
    return (compose("logs", "--no-log-prefix", "worker", capture=True).stdout or "")[mark:]


def workspace_files(job_id):
    workspace = os.path.join(SHARED_DIR, job_id)
    return sorted(os.listdir(workspace)) if os.path.isdir(workspace) else []


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

SCENARIOS = []


def scenario(name, description):
    def decorate(fn):
        SCENARIOS.append((name, description, fn))
        return fn
    return decorate


@scenario("legacy-full", "job_type=full with an empty registry -> legacy build_chain()")
def scenario_legacy_full(ctx):
    reset_registry()
    job_id = submit(ctx["video"], job_type="full")
    result = poll_job(job_id)

    assert result["status"] == "SUCCESS", result
    data = result["data"]
    assert data["status"] == "success", data
    assert len(data["visual_result"]) == 3, data["visual_result"]
    assert len(data["audio_result"]) == 3, data["audio_result"]
    assert data["video_name"] == "mock_video"

    files = workspace_files(job_id)
    assert "mock_video_visual_output.json" in files, files
    assert "mock_video_audio_output.json" in files, files
    assert "task_info.txt" in files, files
    assert "mock_video.mp4" not in files, "finalize should have cleaned the video up"

    ctx["legacy_shape"] = sorted(data.keys())
    return f"chain finished, workspace has {len(files)} artefacts"


@scenario("dag-full", "the same job_type=full, now routed through the DAG engine")
def scenario_dag_full(ctx):
    with open(os.path.join(WORKFLOWS_DIR, "full_1.0.json")) as f:
        workflow = json.load(f)
    status, body = http("POST", f"{CONTROLLER}/workflows", workflow)
    assert status == 200, f"register failed {status}: {body}"
    assert body["latest"] == "1.0", body

    job_id = submit(ctx["video"], job_type="full")
    result = poll_job(job_id)

    assert result["status"] == "SUCCESS", result
    data = result["data"]

    # One /status contract: the DAG returns finalize's merged output, exactly
    # as the legacy chain does.
    assert data["status"] == "success", data
    assert data["video_name"] == "mock_video"
    assert len(data["visual_result"]) == 3, data["visual_result"]
    assert len(data["audio_result"]) == 3, data["audio_result"]

    files = workspace_files(job_id)
    assert "mock_video_visual_output.json" in files, files
    assert "mock_video_audio_output.json" in files, files

    # Per-node envelopes are not lost, just moved off the wire and onto disk.
    assert "dag_run.json" in files, files
    with open(os.path.join(SHARED_DIR, job_id, "dag_run.json")) as f:
        summary = json.load(f)
    assert summary["order"] == ["download", "visual", "audio", "final"], summary["order"]
    assert set(summary["nodes"]) == {"download", "visual", "audio", "final"}
    assert all(e["status"] == "success" for e in summary["nodes"].values()), summary

    ctx["dag_shape"] = sorted(data.keys())
    return "DAG finished; /status matched the legacy shape, nodes on disk"


@scenario("status-shape-matches", "the legacy and DAG paths return the same /status body")
def scenario_status_shape(ctx):
    legacy, dag = ctx.get("legacy_shape"), ctx.get("dag_shape")
    assert legacy and dag, "run legacy-full and dag-full first"
    assert legacy == dag, f"paths still disagree: legacy {legacy} vs DAG {dag}"
    return f"both paths return {legacy}"


@scenario("dag-service-declared", "a workflow declaring `service` starts and stops it exactly once")
def scenario_service_declared(ctx):
    with open(os.path.join(WORKFLOWS_DIR, "full_2.0.json")) as f:
        workflow = json.load(f)
    status, body = http("POST", f"{CONTROLLER}/workflows", workflow)
    assert status == 200, f"register failed {status}: {body}"

    marker = worker_log_mark()
    job_id = submit(ctx["video"], job_type="full", version="2.0")
    result = poll_job(job_id)

    assert result["status"] == "SUCCESS", result
    assert result["data"]["status"] == "success", result["data"]

    # The engine brackets the node, and the task body brackets its own work.
    # Counted from the worker log below.
    log = worker_log_since(marker)
    stops = log.count("[Service Manager] Stopping visualservice")
    # The engine brackets the node and the task body brackets its own work.
    # dag/readiness.py's re-entrant lease collapses the two into one cycle.
    assert stops == 1, f"expected exactly one stop of visualservice, saw {stops}"
    return "engine and task brackets nested into a single start/stop"


@scenario("dag-summarise", "the transcript pipeline: download -> text -> summarise -> tag")
def scenario_summarise(ctx):
    with open(os.path.join(WORKFLOWS_DIR, "summarise_1.0.json")) as f:
        workflow = json.load(f)
    status, body = http("POST", f"{CONTROLLER}/workflows", workflow)
    assert status == 200, f"register failed {status}: {body}"

    job_id = submit(ctx["transcript"], job_type="summarise")
    result = poll_job(job_id)

    assert result["status"] == "SUCCESS", result
    data = result["data"]
    assert data["status"] == "success", data
    assert data["summarise_result"] is not None, data
    assert data["tagging_result"] is not None, data

    files = workspace_files(job_id)
    assert "transcript.txt" in files, files
    assert "transcript_summarise_output.json" in files, files
    assert "transcript_tagging_output.json" in files, files
    return "5-node transcript DAG finished"


@scenario("retry-parity", "a DAG node retries a transient failure like the legacy chain does")
def scenario_retry(ctx):
    """Celery's @app.task(autoretry_for=...) never engages on the DAG path —
    the python driver calls the task's function directly. And a Celery-level
    retry of execute_workflow would re-run the whole DAG to recover from one
    transient download. So retry lives in DAGEngine, per node.
    """
    with open(os.path.join(WORKFLOWS_DIR, "full_1.0.json")) as f:
        workflow = json.load(f)
    assert workflow["tasks"][0]["retries"] == 3, "download node should declare retries"

    marker = worker_log_mark()
    job_id = submit("/app/data/absent_file.mp4", job_type="full")
    result = poll_job(job_id)

    assert result["status"] == "FAILURE", result
    log = worker_log_since(marker)
    attempts = log.count("attempt ")
    assert attempts == 3, f"expected 3 logged retry attempts before giving up, saw {attempts}"
    assert "retrying in" in log
    return f"node retried {attempts}x with backoff, then failed the job"


@scenario("callback-delivered", "finalize_results POSTs its output to callback_url")
def scenario_callback(ctx):
    before = len(http("GET", f"{CALLBACK_SINK}/__calls")[1] or [])

    job_id = submit(ctx["video"], job_type="full", callback_url=CALLBACK_URL)
    result = poll_job(job_id)
    assert result["status"] == "SUCCESS", result

    status, calls = http("GET", f"{CALLBACK_SINK}/__calls")
    assert status == 200, (status, calls)
    delivered = [c for c in calls if c["path"] == "/callback"]
    assert len(delivered) == before + 1, f"expected one new callback, saw {len(delivered) - before}"
    assert delivered[-1]["body_bytes"] > 100, delivered[-1]
    return "callback POSTed to the sink with the merged output"


@scenario("all-job-types", "every legacy job_type also runs as a registered DAG")
def scenario_all_job_types(ctx):
    """5 of the 7 job types had only ever run as legacy chains. These are the
    same pipelines expressed as workflows, including the two extent tasks
    that nothing had executed at all."""
    cases = [
        ("audio_only", ctx["video"], ["audio_result"]),
        ("visual_only", ctx["video"], ["visual_result"]),
        ("tagging", ctx["transcript"], ["tagging_result"]),
        ("speaker-extent-summarise", ctx["transcript"],
         ["extent_result", "summarise_result", "tagging_result"]),
        ("utterance-extent-summarise", ctx["transcript"],
         ["extent_result", "summarise_result", "tagging_result"]),
    ]
    for name, path, expected_keys in cases:
        with open(os.path.join(WORKFLOWS_DIR, f"{name}_1.0.json")) as f:
            workflow = json.load(f)
        status, body = http("POST", f"{CONTROLLER}/workflows", workflow)
        assert status == 200, f"{name}: register failed {status}: {body}"

        job_id = submit(path, job_type=name)
        result = poll_job(job_id)
        assert result["status"] == "SUCCESS", f"{name}: {result}"
        data = result["data"]
        assert data["status"] == "success", f"{name}: {data}"
        for key in expected_keys:
            assert data[key] is not None, f"{name}: {key} missing from {sorted(data)}"

    return f"{len(cases)} job types ran as DAGs, incl. speaker_extent and segment_extent"


@scenario("failure-propagates", "a failing service becomes a FAILURE, not a hung job")
def scenario_failure(ctx):
    set_mock_control({"audio": {"fail_mode": "error500"}})
    try:
        job_id = submit(ctx["video"], job_type="full")
        result = poll_job(job_id)
        assert result["status"] == "FAILURE", result
        assert "500" in str(result.get("message", "")), result
        return "an audio 500 surfaced as a FAILURE on /status"
    finally:
        set_mock_control({})


@scenario("slow-service-tolerated", "a service slower than the poll interval still completes")
def scenario_slow(ctx):
    set_mock_control({"visual": {"latency": 4}, "audio": {"latency": 4}})
    try:
        started = time.time()
        job_id = submit(ctx["video"], job_type="full")
        result = poll_job(job_id)
        elapsed = time.time() - started
        assert result["status"] == "SUCCESS", result
        assert elapsed >= 8, f"expected the injected latency to show up, took {elapsed:.1f}s"
        return f"8s of injected service latency rode through cleanly ({elapsed:.1f}s total)"
    finally:
        set_mock_control({})


@scenario("slow-startup-health", "the worker waits out a service that is slow to become healthy")
def scenario_slow_startup(ctx):
    for name in ("visualservice", "audioservice"):
        compose("stop", name, check=False)
        compose("rm", "-f", name, check=False)
    set_mock_control({"visual": {"startup_delay": 6}})
    try:
        job_id = submit(ctx["video"], job_type="full")
        result = poll_job(job_id)
        assert result["status"] == "SUCCESS", result
        return "cold start blocked on the health poll until the service was ready"
    finally:
        set_mock_control({})


@scenario("contract-enforced", "the mocks reject what the real services would reject")
def scenario_contract(ctx):
    """The mocks validate request shape rather than accepting anything, so a
    regression in how worker/tasks.py builds a request fails here instead of
    only against the real GPU stack.

    First prove the worker's real requests pass, then prove the guard is
    actually awake by sending it deliberately malformed ones.
    """
    job_id = submit(ctx["video"], job_type="full")
    result = poll_job(job_id)
    assert result["status"] == "SUCCESS", f"worker's own requests were rejected: {result}"

    # Keep the services up so they can be probed directly.
    compose("up", "-d", "visualservice", "audioservice")
    time.sleep(4)

    probes = [
        ("no api key", "POST", f"{VISUAL_SINK}/analyze", None, 401),
        ("bad audio body", "POST", f"{AUDIO_SINK}/process_audio/", {"nope": 1}, 422),
        ("audio missing chunks", "POST", f"{AUDIO_SINK}/process_audio/",
         {"video_path": "j/v.mp4", "prompts": None}, 422),
        ("audio wrong chunks type", "POST", f"{AUDIO_SINK}/process_audio/",
         {"video_path": "j/v.mp4", "prompts": None, "chunks": "not-a-list"}, 422),
    ]
    try:
        for label, method, url, body, expected in probes:
            status, response = http(method, url, body, timeout=10)
            assert status == expected, f"{label}: expected {expected}, got {status} {response}"

        # And a well-formed audio request is still accepted, so the guard is
        # not simply rejecting everything.
        status, response = http("POST", f"{AUDIO_SINK}/process_audio/",
                                {"video_path": "j/v.mp4", "prompts": None, "chunks": None})
        assert status == 200, (status, response)
        assert "output" in response, response
    finally:
        for name in ("visualservice", "audioservice"):
            compose("stop", name, check=False)
            compose("rm", "-f", name, check=False)

    return f"{len(probes)} malformed requests rejected, well-formed one accepted"


@scenario("stale-api-key-recovered", "a rejected API key is regenerated rather than failing forever")
def scenario_stale_key(ctx):
    """ensure_api_key() used to return the cached key unconditionally, so if
    the visual service ever forgot or rotated its keys -- its store lives in
    shared/api-data, which any volume reset wipes -- the worker would present
    the same dead key on every future job and visual analysis would never
    recover. Found by tightening the mock to reject keys it never issued.
    """
    # Prime the cache, then poison it with a key the service will not accept.
    key_path = os.path.join(DATA_DIR, "api.key")
    with open(key_path, "w") as f:
        f.write("stale-key-from-a-previous-deployment")

    marker = worker_log_mark()
    job_id = submit(ctx["video"], job_type="full")
    result = poll_job(job_id)

    assert result["status"] == "SUCCESS", f"worker did not recover from a stale key: {result}"
    log = worker_log_since(marker)
    assert "API key rejected" in log, "expected the 401 recovery path to be taken"
    with open(key_path) as f:
        assert f.read().strip() != "stale-key-from-a-previous-deployment", "key was not replaced"
    return "401 on a stale key triggered regeneration and the job completed"


@scenario("badbody-surfaces", "a service returning non-JSON fails the job instead of corrupting it")
def scenario_badbody(ctx):
    """A real service returning an HTML error page or a truncated response is
    a realistic failure the parser had never seen."""
    set_mock_control({"audio": {"fail_mode": "badbody"}})
    try:
        job_id = submit(ctx["video"], job_type="full")
        result = poll_job(job_id)
        assert result["status"] == "FAILURE", result
        files = workspace_files(job_id)
        assert not any(f.endswith("_audio_output.json") for f in files), \
            f"a corrupt response produced an audio output file: {files}"
        return "non-JSON response failed the job and wrote no output"
    finally:
        set_mock_control({})


@scenario("unhealthy-service", "a service that never becomes healthy fails the node")
def scenario_unhealthy(ctx):
    for name in ("visualservice",):
        compose("stop", name, check=False)
        compose("rm", "-f", name, check=False)
    set_mock_control({"visual": {"fail_mode": "unhealthy"}})
    try:
        job_id = submit(ctx["video"], job_type="full")
        result = poll_job(job_id, timeout=240)
        assert result["status"] == "FAILURE", result
        message = str(result.get("message", "")).lower()
        assert "visualservice" in message, result
        return "cold start gave up and failed the node rather than hanging"
    finally:
        set_mock_control({})
        compose("stop", "visualservice", check=False)
        compose("rm", "-f", "visualservice", check=False)


@scenario("hung-service", "a service that accepts the connection and never answers")
def scenario_hung(ctx):
    """Without a reachable request timeout the worker blocks for the full
    30-minute budget on this. VISUAL/AUDIO_REQUEST_TIMEOUT make the give-up
    point configurable; the mock stack sets them to 8s."""
    set_mock_control({"audio": {"fail_mode": "timeout"}})
    try:
        started = time.time()
        job_id = submit(ctx["video"], job_type="full")
        result = poll_job(job_id, timeout=180)
        elapsed = time.time() - started
        assert result["status"] == "FAILURE", result
        assert elapsed < 120, f"took {elapsed:.0f}s — the request timeout did not fire"
        return f"gave up on the hung service after {elapsed:.0f}s"
    finally:
        set_mock_control({})
        compose("stop", "audioservice", check=False)
        compose("rm", "-f", "audioservice", check=False)


@scenario("coldstart-cycle", "the worker really starts and stops containers over the Docker socket")
def scenario_coldstart(ctx):
    for name in ("visualservice", "audioservice"):
        compose("stop", name, check=False)
        compose("rm", "-f", name, check=False)
    assert container_state(MOCK_CONTAINER_NAMES["visualservice"]) == "absent", \
        "expected no visual container before the job"

    job_id = submit(ctx["video"], job_type="full")
    result = poll_job(job_id)
    assert result["status"] == "SUCCESS", result

    for name in ("visualservice", "audioservice"):
        state = container_state(MOCK_CONTAINER_NAMES[name])
        assert state in ("exited", "absent"), f"{name} left in state {state!r} after the job"
    return "containers cold-started on demand and were stopped afterwards"


@scenario("keepalive-mode", "service_modes.json=keepalive leaves the container running")
def scenario_keepalive(ctx):
    modes_path = os.path.join(SHARED_DIR, "service_modes.json")
    compose("up", "-d", "visualservice", "audioservice")
    time.sleep(4)
    with open(modes_path, "w") as f:
        json.dump({"visualservice": "keepalive", "audioservice": "keepalive"}, f)
    # worker/utils.py reads service_modes.json once at import, so the worker
    # has to be restarted for a mode change to take effect — itself worth
    # knowing, and the reason this scenario restarts it explicitly.
    compose("restart", "worker")
    time.sleep(6)
    try:
        job_id = submit(ctx["video"], job_type="full")
        result = poll_job(job_id)
        assert result["status"] == "SUCCESS", result
        assert container_state(MOCK_CONTAINER_NAMES["visualservice"]) == "running", "keepalive container was stopped"
        assert container_state(MOCK_CONTAINER_NAMES["audioservice"]) == "running", "keepalive container was stopped"
        return "both services stayed resident across the job"
    finally:
        os.remove(modes_path) if os.path.exists(modes_path) else None
        compose("restart", "worker")
        time.sleep(6)
        for name in ("visualservice", "audioservice"):
            compose("stop", name, check=False)
            compose("rm", "-f", name, check=False)


@scenario("bad-workflow-rejected", "POST /workflows refuses a cyclic template and leaves no file")
def scenario_bad_workflow(ctx):
    cyclic = {
        "workflow": {"name": "cyclic_demo", "version": "1.0"},
        "tasks": [
            {"id": "a", "task": "download_file", "depends_on": ["b"]},
            {"id": "b", "task": "process_visual", "depends_on": ["a"]},
        ],
    }
    status, body = http("POST", f"{CONTROLLER}/workflows", cyclic)
    assert status == 400, (status, body)
    assert "cycle" in str(body).lower(), body

    duplicate = {
        "workflow": {"name": "dup_demo", "version": "1.0"},
        "tasks": [
            {"id": "same", "task": "download_file", "depends_on": []},
            {"id": "same", "task": "process_visual", "depends_on": []},
        ],
    }
    status, body = http("POST", f"{CONTROLLER}/workflows", duplicate)
    assert status == 400, (status, body)
    assert "duplicate task id" in str(body).lower(), body

    leftovers = [f for f in os.listdir(WORKFLOWS_DIR) if f.startswith(("cyclic_demo", "dup_demo"))]
    assert leftovers == [], f"temp file left behind: {leftovers}"
    return "cycle and duplicate id both rejected with 400, no partial files"


@scenario("custom-name-needs-expects", "a workflow under a novel name must declare expects")
def scenario_custom_name(ctx):
    """Gap A's teeth: finalize_results used to key its success criteria off
    the legacy job_type names, so a workflow registered under a new name
    failed at the final node even when every other node succeeded."""
    spec = {
        "workflow": {"name": "customflow", "version": "1.0"},
        "settings": {"on_failure": "stop"},
        "tasks": [
            {"id": "download", "task": "download_file", "depends_on": []},
            {"id": "visual", "task": "process_visual", "service": "visualservice",
             "depends_on": ["download"]},
            {"id": "final", "task": "finalize_results",
             "kwargs": {"expects": ["visual"]}, "depends_on": ["visual"]},
        ],
    }
    status, body = http("POST", f"{CONTROLLER}/workflows", spec)
    assert status == 200, (status, body)

    job_id = submit(ctx["video"], job_type="customflow")
    result = poll_job(job_id)

    assert result["status"] == "SUCCESS", result
    assert result["data"]["status"] == "success", result["data"]
    assert result["data"]["visual_result"] is not None
    return "a workflow under a name build_chain() has never heard of ran clean"


@scenario("unknown-task-preflight", "a typo'd task fails before any node runs")
def scenario_preflight(ctx):
    bad = {
        "workflow": {"name": "typo_demo", "version": "1.0"},
        "tasks": [
            {"id": "download", "task": "download_file", "depends_on": []},
            {"id": "oops", "task": "process_vissual", "depends_on": ["download"]},
        ],
    }
    status, body = http("POST", f"{CONTROLLER}/workflows", bad)
    assert status == 200, (status, body)   # the parser cannot know task names

    job_id = submit(ctx["video"], job_type="typo_demo")
    result = poll_job(job_id)

    assert result["status"] == "FAILURE", result
    assert "process_vissual" in str(result.get("message", "")), result

    # The promise of _validate_task_names: nothing ran, so no workspace
    # directory was even created by download_file.
    assert workspace_files(job_id) == [], workspace_files(job_id)
    return "pre-flight rejected the DAG before download_file touched the disk"


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def build_images():
    run(["docker", "build", "-q", "-t", "ai4me-mock-service:latest", "mocks/"])
    run(["docker", "build", "-q", "-f", "controller/Dockerfile", "-t", "ai4me-mock-controller:latest", "."])
    run(["docker", "build", "-q", "-f", "worker/Dockerfile", "-t", "ai4me-mock-worker:latest", "."])


def reclaim_ownership():
    """The mock containers run as root, same as production's images, so any
    file they create through the bind mounts (job workspaces, api.key,
    mock_control.json, ...) comes out root-owned. This script also writes
    directly into DATA_DIR/SHARED_DIR from the host side (write_fixtures,
    set_mock_control, the stale-key scenario, ...) as whatever user is
    running it -- which a root-owned file left by an earlier run then
    blocks with PermissionError. Self-heal by chowning both trees back with
    a one-off container, rather than requiring host-side sudo. Uses the
    already-built mock image so a first run needs no extra pull.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SHARED_DIR, exist_ok=True)
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    for d in (DATA_DIR, SHARED_DIR):
        run(["docker", "run", "--rm", "-v", f"{d}:/target",
             "ai4me-mock-service:latest", "chown", "-R", uid_gid, "/target"],
            check=False, capture=True)


def bring_up():
    compose("up", "-d", *CORE_SERVICES)
    wait_for_controller()


def tear_down():
    compose("down", "-v", "--remove-orphans", check=False)
    for name in MOCK_SERVICES:
        run(["docker", "rm", "-f", MOCK_CONTAINER_NAMES[name]], check=False, capture=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", "--filter", help="only run scenarios whose name contains this")
    parser.add_argument("--keep", action="store_true", help="leave the stack running afterwards")
    parser.add_argument("--no-build", action="store_true", help="skip docker build")
    args = parser.parse_args()

    if not args.no_build:
        build_images()

    # Needs ai4me-mock-service:latest (just above) to chown with, and must
    # run before anything else touches DATA_DIR/SHARED_DIR -- an earlier
    # run's root-owned leftovers (its containers run as root, same as
    # production's images) block this run's own writes otherwise.
    reclaim_ownership()
    video, transcript = write_fixtures()
    ctx = {"video": "/app/data/mock_video.mp4", "transcript": "/app/data/mock_transcript.json"}

    tear_down()
    reset_registry()
    clean_shared()
    clean_api_key()
    bring_up()

    selected = [s for s in SCENARIOS if not args.filter or args.filter in s[0]]
    results = []
    try:
        for name, description, fn in selected:
            print(f"\n{YELLOW}=== {name} ==={RESET} {description}")
            started = time.time()
            try:
                note = fn(ctx)
                results.append((name, True, note, time.time() - started))
                print(f"{GREEN}PASS{RESET} {name} — {note}")
            except Exception as e:
                results.append((name, False, f"{type(e).__name__}: {e}", time.time() - started))
                print(f"{RED}FAIL{RESET} {name} — {type(e).__name__}: {e}")
    finally:
        if not args.keep:
            tear_down()
            reset_registry()     # leave the working tree as we found it
        else:
            print(f"\n{YELLOW}--keep: stack left running. Tear down with:{RESET}")
            print(f"  docker compose -f {COMPOSE_FILE} --project-directory . -p {PROJECT} down -v")

    print(f"\n{'=' * 72}")
    passed = sum(1 for _, ok, _, _ in results if ok)
    for name, ok, note, seconds in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"{mark}  {name:<26} {seconds:>5.1f}s  {note}")
    print(f"{'=' * 72}")
    print(f"{passed}/{len(results)} scenarios passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
