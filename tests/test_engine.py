"""dag/engine.py — orchestration, payload plumbing, failure policy, lifecycle.

Every node's work is a stub function in a fake `tasks` module, so the whole
engine runs with no Celery, no Redis, no Docker and no filesystem.
"""
import types

import pytest

from dag.engine import (
    DAGEngine,
    PayloadConflictError,
    TaskExecutionError,
    UnknownTaskError,
)
from dag.envelope import failure, success


# ==========================================================================
# Pre-flight validation — must reject before any node has side effects
# ==========================================================================

def test_unknown_driver_is_rejected(stub_tasks, make_engine):
    stub_tasks.add("noop")
    engine = make_engine([{"id": "a", "task": "noop", "driver": "carrier-pigeon", "depends_on": []}])

    with pytest.raises(UnknownTaskError, match="Unknown driver 'carrier-pigeon'"):
        engine.execute()


def test_node_without_func_or_task_is_rejected(stub_tasks, make_engine):
    engine = make_engine([{"id": "a", "depends_on": [], "kwargs": {}}])
    with pytest.raises(UnknownTaskError, match="has no 'func'/'task'"):
        engine.execute()


def test_unimportable_module_is_rejected(make_engine):
    engine = make_engine([{"id": "a", "func": "f", "module": "no_such_module_xyz", "depends_on": []}])
    with pytest.raises(UnknownTaskError, match="Cannot import module"):
        engine.execute()


def test_missing_function_is_rejected(stub_tasks, make_engine):
    engine = make_engine([{"id": "a", "task": "typoed_name", "depends_on": []}])
    with pytest.raises(UnknownTaskError, match="No function 'typoed_name' in module 'tasks'"):
        engine.execute()


def test_http_node_without_url_is_rejected(make_engine):
    engine = make_engine([{"id": "a", "driver": "http", "depends_on": []}])
    with pytest.raises(UnknownTaskError, match="has no 'url' attribute"):
        engine.execute()


def test_download_without_a_path_anywhere_is_rejected(stub_tasks, make_engine):
    stub_tasks.add("download_file")
    engine = make_engine([{"id": "d", "task": "download_file", "depends_on": []}])
    with pytest.raises(UnknownTaskError, match="has no 'path'"):
        engine.execute()


def test_bad_deployment_mode_is_caught_at_preflight(stub_tasks, make_engine, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "nomad")
    stub_tasks.add("noop")
    engine = make_engine([{"id": "a", "task": "noop", "service": "audioservice", "depends_on": []}])

    with pytest.raises(UnknownTaskError, match="Unknown DEPLOYMENT_MODE"):
        engine.execute()


def test_validation_runs_before_any_node_executes(stub_tasks, make_engine, recorder):
    """The documented promise of _validate_task_names: a typo three nodes
    deep must not let the first two nodes cause real side effects first."""
    stub_tasks.add("good")
    engine = make_engine([
        {"id": "a", "task": "good", "depends_on": []},
        {"id": "b", "task": "good", "depends_on": ["a"]},
        {"id": "c", "task": "typo", "depends_on": ["b"]},
    ])

    with pytest.raises(UnknownTaskError):
        engine.execute()

    assert recorder.of_kind("call") == []


def test_validation_also_guards_parallel_execution(stub_tasks, make_engine, recorder):
    stub_tasks.add("good")
    engine = make_engine([
        {"id": "a", "task": "good", "depends_on": []},
        {"id": "b", "task": "typo", "depends_on": ["a"]},
    ])

    with pytest.raises(UnknownTaskError):
        engine.execute_parallel()

    assert recorder.of_kind("call") == []


# ==========================================================================
# Construction
# ==========================================================================

def test_invalid_on_failure_mode_is_rejected_at_construction(make_dag):
    parsed = make_dag([{"id": "a", "task": "noop", "depends_on": []}])
    with pytest.raises(ValueError, match="Unknown on_failure mode 'retry'"):
        DAGEngine(parsed.dag, job_id="j", on_failure="retry")


def test_on_failure_is_read_from_workflow_settings(make_engine):
    engine = make_engine(
        [{"id": "a", "task": "noop", "depends_on": []}],
        settings={"on_failure": "continue"},
    )
    assert engine.on_failure == "continue"


# ==========================================================================
# Payload plumbing
# ==========================================================================

def test_predecessor_output_flows_into_the_successor(stub_tasks, make_engine, recorder):
    stub_tasks.add("download_file", returns={"file_path": "/app/tmp/j/v.mp4"})
    stub_tasks.add("process_visual", returns={"visual_result": {"ok": True}})

    engine = make_engine(
        [
            {"id": "d", "task": "download_file", "depends_on": []},
            {"id": "v", "task": "process_visual", "depends_on": ["d"]},
        ],
        job_inputs={"path": "http://v/x.mp4"},
    )
    engine.execute()

    assert recorder.calls("process_visual")[0]["args"] == ({"file_path": "/app/tmp/j/v.mp4"},)


def test_disjoint_predecessor_payloads_merge(stub_tasks, make_engine, recorder):
    stub_tasks.add("root", returns={"file_path": "/x"})
    stub_tasks.add("left", returns={"file_path": "/x", "audio": "a.json"})
    stub_tasks.add("right", returns={"file_path": "/x", "visual": "v.json"})
    stub_tasks.add("sink")

    engine = make_engine([
        {"id": "r", "task": "root", "depends_on": []},
        {"id": "l", "task": "left", "depends_on": ["r"]},
        {"id": "v", "task": "right", "depends_on": ["r"]},
        {"id": "s", "task": "sink", "depends_on": ["l", "v"]},
    ])
    engine.execute()

    assert recorder.calls("sink")[0]["args"][0] == {
        "file_path": "/x", "audio": "a.json", "visual": "v.json",
    }


def test_predecessors_agreeing_on_a_key_is_fine(stub_tasks, make_engine, recorder):
    stub_tasks.add("root", returns={"job": "j"})
    stub_tasks.add("passthrough", returns={"job": "j"})
    stub_tasks.add("sink")

    engine = make_engine([
        {"id": "r", "task": "root", "depends_on": []},
        {"id": "a", "task": "passthrough", "depends_on": ["r"]},
        {"id": "b", "task": "passthrough", "depends_on": ["r"]},
        {"id": "s", "task": "sink", "depends_on": ["a", "b"]},
    ])
    engine.execute()

    assert recorder.calls("sink")[0]["args"][0] == {"job": "j"}


def test_predecessors_disagreeing_on_a_key_raises(stub_tasks, make_engine):
    stub_tasks.add("left", returns={"file_path": "/one"})
    stub_tasks.add("right", returns={"file_path": "/two"})
    stub_tasks.add("sink")

    engine = make_engine([
        {"id": "l", "task": "left", "depends_on": []},
        {"id": "r", "task": "right", "depends_on": []},
        {"id": "s", "task": "sink", "depends_on": ["l", "r"]},
    ])

    with pytest.raises(PayloadConflictError, match="predecessors disagree on 'file_path'"):
        engine.execute()


def test_failed_predecessor_contributes_no_payload(stub_tasks, make_engine, recorder):
    stub_tasks.add("ok_task", returns={"good": 1})
    stub_tasks.add("bad_task", raises=RuntimeError("down"))
    stub_tasks.add("sink")

    engine = make_engine(
        [
            {"id": "a", "task": "ok_task", "depends_on": []},
            {"id": "b", "task": "bad_task", "depends_on": []},
            {"id": "s", "task": "sink", "depends_on": ["a", "b"]},
        ],
        settings={"on_failure": "continue"},
    )
    engine.execute()

    assert recorder.calls("sink")[0]["args"][0] == {"good": 1}


def test_static_kwargs_are_layered_onto_the_payload(stub_tasks, make_engine, recorder):
    stub_tasks.add("root", returns={"file_path": "/x"})
    stub_tasks.add("sink")

    engine = make_engine([
        {"id": "r", "task": "root", "depends_on": []},
        {"id": "s", "task": "sink", "depends_on": ["r"], "kwargs": {"threshold": 0.7}},
    ])
    engine.execute()

    assert recorder.calls("sink")[0]["args"][0] == {"file_path": "/x", "threshold": 0.7}


def test_static_kwargs_conflicting_with_predecessor_output_raises(stub_tasks, make_engine):
    stub_tasks.add("root", returns={"threshold": 0.1})
    stub_tasks.add("sink")

    engine = make_engine([
        {"id": "r", "task": "root", "depends_on": []},
        {"id": "s", "task": "sink", "depends_on": ["r"], "kwargs": {"threshold": 0.9}},
    ])

    with pytest.raises(PayloadConflictError, match="static kwargs disagree"):
        engine.execute()


def test_non_dict_task_output_is_not_merged(stub_tasks, make_engine, recorder):
    stub_tasks.add("stringy", returns="just a string")
    stub_tasks.add("sink")

    engine = make_engine([
        {"id": "a", "task": "stringy", "depends_on": []},
        {"id": "s", "task": "sink", "depends_on": ["a"]},
    ])
    engine.execute()

    assert recorder.calls("sink")[0]["args"][0] == {}


# ==========================================================================
# NON_PAYLOAD_TASKS — nodes fed job context instead of predecessor payload
# ==========================================================================

def test_download_file_receives_job_context_as_kwargs(stub_tasks, make_engine, recorder):
    stub_tasks.add("download_file")

    engine = make_engine(
        [{"id": "d", "task": "download_file", "depends_on": []}],
        job_id="job-42",
        job_inputs={"path": "s3://bucket/v.mp4", "prompts": "describe it"},
    )
    engine.execute()

    assert recorder.call_args("download_file") == {
        "path": "s3://bucket/v.mp4", "job_id": "job-42", "prompts": "describe it",
    }


def test_runtime_job_inputs_override_static_kwargs(stub_tasks, make_engine, recorder):
    stub_tasks.add("download_file")

    engine = make_engine(
        [{"id": "d", "task": "download_file", "depends_on": [],
          "kwargs": {"path": "/template/default.mp4"}}],
        job_inputs={"path": "/runtime/actual.mp4"},
    )
    engine.execute()

    assert recorder.call_args("download_file")["path"] == "/runtime/actual.mp4"


def test_static_kwargs_path_is_used_when_the_request_has_none(stub_tasks, make_engine, recorder):
    stub_tasks.add("download_file")

    engine = make_engine(
        [{"id": "d", "task": "download_file", "depends_on": [],
          "kwargs": {"path": "/template/default.mp4"}}],
    )
    engine.execute()

    assert recorder.call_args("download_file")["path"] == "/template/default.mp4"


def test_finalize_receives_only_job_context_not_predecessor_payload(
    stub_tasks, make_engine, recorder
):
    """finalize_results reads its inputs by globbing the job workspace
    (worker/tasks.py:202-208), not from the DAG payload — so the engine
    deliberately hands it job context only."""
    stub_tasks.add("process_visual", returns={"visual_result": {"big": "blob"}})
    stub_tasks.add("finalize_results", returns={"done": True})

    engine = make_engine(
        [
            {"id": "v", "task": "process_visual", "depends_on": []},
            {"id": "f", "task": "finalize_results", "depends_on": ["v"]},
        ],
        job_id="job-7", job_type="visual_only", callback_url="http://cb/done",
    )
    engine.execute()

    assert recorder.call_args("finalize_results") == {
        "job_id": "job-7", "job_type": "visual_only", "callback_url": "http://cb/done",
    }


def test_non_payload_dispatch_keys_off_task_name_not_node_id(stub_tasks, make_engine, recorder):
    """The node may be called anything; what selects the job-context branch
    is the resolved task name."""
    stub_tasks.add("download_file")

    engine = make_engine(
        [{"id": "fetch-the-video", "task": "download_file", "depends_on": []}],
        job_inputs={"path": "/v.mp4"},
    )
    engine.execute()

    assert "job_id" in recorder.call_args("download_file")


# ==========================================================================
# Failure policy
# ==========================================================================

def test_on_failure_stop_halts_and_skips_downstream(stub_tasks, make_engine, recorder):
    stub_tasks.add("boom", raises=RuntimeError("service exploded"))
    stub_tasks.add("downstream")

    engine = make_engine([
        {"id": "a", "task": "boom", "depends_on": []},
        {"id": "b", "task": "downstream", "depends_on": ["a"]},
    ])

    with pytest.raises(TaskExecutionError, match="service exploded"):
        engine.execute()

    assert not recorder.calls("downstream")


def test_on_failure_continue_keeps_walking(stub_tasks, make_engine, recorder):
    stub_tasks.add("boom", raises=RuntimeError("service exploded"))
    stub_tasks.add("downstream")

    engine = make_engine(
        [
            {"id": "a", "task": "boom", "depends_on": []},
            {"id": "b", "task": "downstream", "depends_on": ["a"]},
        ],
        settings={"on_failure": "continue"},
    )
    results = engine.execute()

    assert recorder.calls("downstream")
    assert results["a"]["status"] == "error"
    assert results["b"]["status"] == "success"


def test_results_are_envelopes_keyed_by_node_id(stub_tasks, make_engine):
    stub_tasks.add("t", returns={"k": "v"})
    engine = make_engine([
        {"id": "first", "task": "t", "depends_on": []},
        {"id": "second", "task": "t", "depends_on": ["first"]},
    ])

    results = engine.execute()

    assert set(results) == {"first", "second"}
    assert results["first"] == success({"k": "v"})


def test_already_executed_nodes_are_not_rerun(stub_tasks, make_engine, recorder):
    stub_tasks.add("t")
    engine = make_engine([{"id": "a", "task": "t", "depends_on": []}])

    engine.execute()
    engine.execute()

    assert len(recorder.calls("t")) == 1


# ==========================================================================
# Service lifecycle bracket
# ==========================================================================

@pytest.fixture
def spy_lifecycle(monkeypatch, recorder):
    """Replace engine-level ensure_ready/release with recorders."""
    def make(name, raises=None):
        def spy(service_name):
            recorder.log("lifecycle", name, service=service_name)
            if raises:
                raise raises
        return spy

    def _install(ensure_raises=None):
        monkeypatch.setattr("dag.engine.ensure_ready", make("ensure_ready", ensure_raises))
        monkeypatch.setattr("dag.engine.release", make("release"))

    return _install


def test_service_node_brackets_the_driver_call(stub_tasks, make_engine, recorder, spy_lifecycle):
    spy_lifecycle()
    stub_tasks.add("process_visual")

    engine = make_engine([
        {"id": "v", "task": "process_visual", "service": "visualservice", "depends_on": []},
    ])
    engine.execute()

    assert recorder.names() == ["ensure_ready", "process_visual", "process_visual", "release"]
    assert recorder.of_kind("lifecycle")[0]["service"] == "visualservice"


def test_node_without_service_touches_no_lifecycle(stub_tasks, make_engine, recorder, spy_lifecycle):
    spy_lifecycle()
    stub_tasks.add("t")

    make_engine([{"id": "a", "task": "t", "depends_on": []}]).execute()

    assert recorder.of_kind("lifecycle") == []


def test_release_runs_even_when_the_driver_raises(
    stub_tasks, make_engine, recorder, spy_lifecycle, monkeypatch
):
    """Drivers normally return failure envelopes rather than raising, so the
    `finally` is only reachable via a driver bug — test it anyway, since it
    is the thing standing between a bug and a permanently occupied GPU."""
    spy_lifecycle()

    exploding = types.SimpleNamespace(run=lambda attrs, inputs: (_ for _ in ()).throw(RuntimeError("driver bug")))
    monkeypatch.setitem(__import__("dag.engine", fromlist=["DRIVERS"]).DRIVERS, "exploding", exploding)

    engine = make_engine([
        {"id": "a", "driver": "exploding", "task": "t", "service": "audioservice", "depends_on": []},
    ])

    with pytest.raises(RuntimeError, match="driver bug"):
        engine.execute()

    assert recorder.names("lifecycle") == ["ensure_ready", "release"]


def test_unready_service_becomes_a_node_failure(stub_tasks, make_engine, recorder, spy_lifecycle):
    from dag.readiness import ServiceNotReadyError

    spy_lifecycle(ensure_raises=ServiceNotReadyError("'audioservice' failed to become ready"))
    stub_tasks.add("process_audio")

    engine = make_engine(
        [{"id": "a", "task": "process_audio", "service": "audioservice", "depends_on": []}],
        settings={"on_failure": "continue"},
    )
    results = engine.execute()

    assert results["a"]["status"] == "error"
    assert "failed to become ready" in results["a"]["error"]
    assert not recorder.calls("process_audio")     # driver never ran


def test_unready_service_does_not_release(stub_tasks, make_engine, recorder, spy_lifecycle):
    """Documents today's asymmetry: release is only reached via the driver
    branch, so a failed ensure_ready leaves no release call. Correct while
    ensure_ready is all-or-nothing; revisit when it acquires a lease, since
    a partial acquisition would then need unwinding."""
    from dag.readiness import ServiceNotReadyError

    spy_lifecycle(ensure_raises=ServiceNotReadyError("nope"))
    stub_tasks.add("t")

    make_engine(
        [{"id": "a", "task": "t", "service": "audioservice", "depends_on": []}],
        settings={"on_failure": "continue"},
    ).execute()

    assert recorder.names("lifecycle") == ["ensure_ready"]


# ==========================================================================
# Parallel execution
# ==========================================================================

@pytest.mark.slow
def test_independent_siblings_run_concurrently(stub_tasks, make_engine, recorder):
    stub_tasks.add("root", returns={"file_path": "/x"})
    stub_tasks.add("slow_left", returns={"file_path": "/x", "l": 1}, delay=0.2)
    stub_tasks.add("slow_right", returns={"file_path": "/x", "r": 1}, delay=0.2)

    engine = make_engine([
        {"id": "r", "task": "root", "depends_on": []},
        {"id": "l", "task": "slow_left", "depends_on": ["r"]},
        {"id": "v", "task": "slow_right", "depends_on": ["r"]},
    ])
    engine.execute_parallel()

    entered = [e["at"] for e in recorder.of_kind("call") if e["name"].startswith("slow_")]
    returned = [e["at"] for e in recorder.of_kind("return") if e["name"].startswith("slow_")]
    assert max(entered) < min(returned), "siblings did not overlap — they ran sequentially"


@pytest.mark.slow
def test_generations_are_ordered(stub_tasks, make_engine, recorder):
    stub_tasks.add("first", returns={"a": 1}, delay=0.05)
    stub_tasks.add("second", returns={"a": 1}, delay=0.05)

    engine = make_engine([
        {"id": "x", "task": "first", "depends_on": []},
        {"id": "y", "task": "second", "depends_on": ["x"]},
    ])
    engine.execute_parallel()

    first_return = [e["at"] for e in recorder.of_kind("return") if e["name"] == "first"][0]
    second_enter = [e["at"] for e in recorder.of_kind("call") if e["name"] == "second"][0]
    assert first_return < second_enter


def test_parallel_produces_the_same_results_as_sequential(stub_tasks, make_engine):
    tasks = [
        {"id": "r", "task": "root", "depends_on": []},
        {"id": "l", "task": "left", "depends_on": ["r"]},
        {"id": "v", "task": "right", "depends_on": ["r"]},
        {"id": "s", "task": "sink", "depends_on": ["l", "v"]},
    ]
    stub_tasks.add("root", returns={"p": "/x"})
    stub_tasks.add("left", returns={"p": "/x", "l": 1})
    stub_tasks.add("right", returns={"p": "/x", "r": 1})
    stub_tasks.add("sink", returns={"done": True})

    sequential = make_engine(tasks).execute()
    parallel = make_engine(tasks).execute_parallel()

    assert sequential == parallel


@pytest.mark.slow
def test_a_failing_node_does_not_cancel_its_running_siblings(stub_tasks, make_engine, recorder):
    """on_failure='stop' raises out of execute_parallel via future.result(),
    but siblings already in flight run to completion — different from
    sequential execution, and worth pinning down explicitly."""
    stub_tasks.add("boom", raises=RuntimeError("fast failure"))
    stub_tasks.add("slow_sibling", delay=0.15)

    engine = make_engine([
        {"id": "a", "task": "boom", "depends_on": []},
        {"id": "b", "task": "slow_sibling", "depends_on": []},
    ])

    with pytest.raises(TaskExecutionError):
        engine.execute_parallel()

    assert recorder.of_kind("return")


# ==========================================================================
# Node-level retry
# ==========================================================================

@pytest.fixture
def flaky(stub_tasks, recorder):
    """A task that fails its first `failures` attempts, then succeeds."""
    def _make(name, failures, result=None):
        state = {"calls": 0}

        def body(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] <= failures:
                raise RuntimeError(f"transient failure {state['calls']}")
            return result if result is not None else {"ran": name}

        stub_tasks.add(name, fn=body)
        return state
    return _make


def test_no_retry_by_default(flaky, make_engine, recorder):
    flaky("download_file", failures=1)
    engine = make_engine(
        [{"id": "d", "task": "download_file", "depends_on": []}],
        job_inputs={"path": "/v.mp4"},
    )
    with pytest.raises(TaskExecutionError):
        engine.execute()
    assert len(recorder.calls("download_file")) == 1


def test_node_retries_until_it_succeeds(flaky, make_engine, recorder):
    flaky("download_file", failures=2, result={"file_path": "/x"})
    engine = make_engine(
        [{"id": "d", "task": "download_file", "retries": 3, "depends_on": []}],
        job_inputs={"path": "/v.mp4"},
        retry_backoff=0,
    )
    results = engine.execute()
    assert len(recorder.calls("download_file")) == 3
    assert results["d"]["status"] == "success"


def test_retries_are_bounded(flaky, make_engine, recorder):
    flaky("t", failures=99)
    engine = make_engine(
        [{"id": "a", "task": "t", "retries": 2, "depends_on": []}],
        retry_backoff=0,
    )
    with pytest.raises(TaskExecutionError):
        engine.execute()
    assert len(recorder.calls("t")) == 3, "expected 1 attempt + 2 retries"


def test_workflow_settings_supply_a_default(flaky, make_engine, recorder):
    flaky("t", failures=1)
    engine = make_engine([{"id": "a", "task": "t", "depends_on": []}], retries=2, retry_backoff=0)
    engine.execute()
    assert len(recorder.calls("t")) == 2


def test_node_attribute_overrides_the_workflow_default(flaky, make_engine, recorder):
    flaky("t", failures=99)
    engine = make_engine(
        [{"id": "a", "task": "t", "retries": 0, "depends_on": []}],
        retries=5, retry_backoff=0,
    )
    with pytest.raises(TaskExecutionError):
        engine.execute()
    assert len(recorder.calls("t")) == 1


def test_retry_reacquires_the_service(stub_tasks, stub_utils, make_engine, recorder):
    """A node whose service failed gets a fresh cold start rather than being
    handed the same broken container."""
    state = {"calls": 0}

    def body(payload):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("service returned garbage")
        return {"ok": True}

    stub_tasks.add("process_audio", fn=body)
    engine = make_engine(
        [{"id": "a", "task": "process_audio", "service": "audioservice",
          "retries": 1, "depends_on": []}],
        retry_backoff=0,
    )
    engine.execute()

    assert len(recorder.calls("start_service")) == 2, "service was not restarted between attempts"
    assert len(recorder.calls("stop_service")) == 2


def test_unready_service_is_retried(stub_tasks, make_engine, recorder, monkeypatch):
    from dag.readiness import ServiceNotReadyError

    attempts = {"n": 0}

    def flaky_ensure(service_name):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ServiceNotReadyError("cold start timed out")
        recorder.log("lifecycle", "ensure_ready", service=service_name)

    monkeypatch.setattr("dag.engine.ensure_ready", flaky_ensure)
    monkeypatch.setattr("dag.engine.release", lambda s: recorder.log("lifecycle", "release", service=s))
    stub_tasks.add("process_visual")

    engine = make_engine(
        [{"id": "v", "task": "process_visual", "service": "visualservice",
          "retries": 2, "depends_on": []}],
        retry_backoff=0,
    )
    results = engine.execute()

    assert results["v"]["status"] == "success"
    assert attempts["n"] == 2
    assert recorder.calls("process_visual")


def test_retry_backoff_grows(flaky, make_engine, monkeypatch):
    slept = []
    monkeypatch.setattr("dag.engine.time.sleep", lambda s: slept.append(s))
    flaky("t", failures=3)

    make_engine(
        [{"id": "a", "task": "t", "retries": 3, "depends_on": []}],
        retry_backoff=2, retry_backoff_max=100,
    ).execute()

    assert slept == [2, 4, 8]


def test_retry_backoff_is_capped(flaky, make_engine, monkeypatch):
    slept = []
    monkeypatch.setattr("dag.engine.time.sleep", lambda s: slept.append(s))
    flaky("t", failures=3)

    make_engine(
        [{"id": "a", "task": "t", "retries": 3, "depends_on": []}],
        retry_backoff=10, retry_backoff_max=15,
    ).execute()

    assert slept == [10, 15, 15]


@pytest.mark.parametrize("bad", [-1, 1.5, "3", True, None])
def test_invalid_retries_is_rejected_at_preflight(stub_tasks, make_engine, bad, recorder):
    stub_tasks.add("t")
    engine = make_engine([{"id": "a", "task": "t", "retries": bad, "depends_on": []}])
    with pytest.raises(UnknownTaskError, match="non-negative integer"):
        engine.execute()
    assert recorder.of_kind("call") == []


def test_payload_conflict_is_not_retried(stub_tasks, make_engine, recorder):
    """A conflict is deterministic — retrying it just wastes time."""
    stub_tasks.add("left", returns={"k": 1})
    stub_tasks.add("right", returns={"k": 2})
    stub_tasks.add("sink")

    engine = make_engine(
        [
            {"id": "l", "task": "left", "depends_on": []},
            {"id": "r", "task": "right", "depends_on": []},
            {"id": "s", "task": "sink", "retries": 5, "depends_on": ["l", "r"]},
        ],
        retry_backoff=0,
    )
    with pytest.raises(PayloadConflictError):
        engine.execute()
    assert not recorder.calls("sink")


# ==========================================================================
# Service leases under concurrency
# ==========================================================================

@pytest.fixture
def parallel_service_run(stub_tasks, stub_utils, make_engine, recorder):
    """Two independent nodes that both declare the same coldstart service."""
    def _run():
        stub_tasks.add("left", returns={"l": 1}, delay=0.15)
        stub_tasks.add("right", returns={"r": 1}, delay=0.15)
        engine = make_engine([
            {"id": "l", "task": "left", "service": "audioservice", "depends_on": []},
            {"id": "v", "task": "right", "service": "audioservice", "depends_on": []},
        ])
        engine.execute_parallel()
        return recorder
    return _run


@pytest.mark.slow
def test_shared_service_is_started_once_for_concurrent_nodes(parallel_service_run):
    """The lease refcounts holders, so two siblings needing the same service
    produce one start, not one each."""
    recorder = parallel_service_run()
    assert len(recorder.calls("start_service")) == 1


@pytest.mark.slow
def test_shared_service_is_stopped_once_after_the_last_holder(parallel_service_run):
    recorder = parallel_service_run()
    assert len(recorder.calls("stop_service")) == 1


@pytest.mark.slow
def test_shared_service_is_never_stopped_while_still_in_use(parallel_service_run):
    """The damaging half of the old gap E: whichever node finished first used
    to call stop_service, tearing down the container the other node was still
    mid-request against."""
    recorder = parallel_service_run()
    last_task_return = max(
        e["at"] for e in recorder.of_kind("return") if e["name"] in {"left", "right"}
    )
    first_stop = min(e["at"] for e in recorder.calls("stop_service"))
    assert first_stop > last_task_return


@pytest.mark.slow
def test_concurrency_limit_serialises_holders(stub_tasks, stub_utils, make_engine, recorder):
    """With the default limit of 1, two nodes sharing a service do not run
    their work concurrently — the second blocks until the first releases."""
    stub_tasks.add("left", returns={"l": 1}, delay=0.15)
    stub_tasks.add("right", returns={"r": 1}, delay=0.15)

    make_engine([
        {"id": "l", "task": "left", "service": "audioservice", "depends_on": []},
        {"id": "v", "task": "right", "service": "audioservice", "depends_on": []},
    ]).execute_parallel()

    first_return = min(e["at"] for e in recorder.of_kind("return") if e["name"] in {"left", "right"})
    last_enter = max(e["at"] for e in recorder.of_kind("call") if e["name"] in {"left", "right"})
    assert first_return <= last_enter, "work overlapped despite a concurrency limit of 1"


@pytest.mark.slow
def test_raising_the_concurrency_limit_allows_overlap(
    stub_tasks, stub_utils, make_engine, recorder, monkeypatch
):
    monkeypatch.setenv("SERVICE_CONCURRENCY", '{"transcriptservice": 2}')
    stub_tasks.add("left", returns={"l": 1}, delay=0.2)
    stub_tasks.add("right", returns={"r": 1}, delay=0.2)

    make_engine([
        {"id": "l", "task": "left", "service": "transcriptservice", "depends_on": []},
        {"id": "v", "task": "right", "service": "transcriptservice", "depends_on": []},
    ]).execute_parallel()

    entered = [e["at"] for e in recorder.of_kind("call") if e["name"] in {"left", "right"}]
    returned = [e["at"] for e in recorder.of_kind("return") if e["name"] in {"left", "right"}]
    assert max(entered) < min(returned), "limit of 2 should have let both run at once"
    assert len(recorder.calls("start_service")) == 1


def test_nested_brackets_start_and_stop_once(stub_tasks, stub_utils, make_engine, recorder):
    """The engine brackets the node while the task body brackets its own work
    (worker/tasks.py does this for process_visual/process_audio). Nesting must
    collapse to a single start/stop, not two."""
    from dag.readiness import ensure_ready, release

    def task_body(payload):
        ensure_ready("visualservice")
        try:
            return {"done": True}
        finally:
            release("visualservice")

    stub_tasks.add("process_visual", fn=task_body)

    make_engine([
        {"id": "v", "task": "process_visual", "service": "visualservice", "depends_on": []},
    ]).execute()

    assert len(recorder.calls("start_service")) == 1
    assert len(recorder.calls("stop_service")) == 1


# ==========================================================================
# Introspection helpers
# ==========================================================================

def test_get_node_status_before_and_after(stub_tasks, make_engine):
    stub_tasks.add("t")
    engine = make_engine([
        {"id": "a", "task": "t", "depends_on": []},
        {"id": "b", "task": "t", "depends_on": ["a"]},
    ])

    before = engine.get_node_status("b")
    assert before["executed"] is False
    assert before["predecessors"] == ["a"]
    assert before["successors"] == []

    engine.execute()
    assert engine.get_node_status("b")["executed"] is True


def test_get_execution_plan_is_in_topological_order(stub_tasks, make_engine):
    stub_tasks.add("t")
    engine = make_engine([
        {"id": "a", "task": "t", "depends_on": []},
        {"id": "b", "task": "t", "depends_on": ["a"]},
        {"id": "c", "task": "t", "depends_on": ["b"]},
    ])

    assert [step["id"] for step in engine.get_execution_plan()] == ["a", "b", "c"]
