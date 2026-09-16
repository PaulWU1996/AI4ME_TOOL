"""dag/drivers/python.py — resolve a callable by module + name and call it."""
import pytest

from dag.drivers import python as python_driver
from dag.envelope import success


def test_calls_target_with_payload_by_default(stub_tasks, recorder):
    stub_tasks.add("process_visual", returns={"visual_result": {"ok": True}})

    env = python_driver.run({"func": "process_visual"}, {"file_path": "/x"})

    assert env == success({"visual_result": {"ok": True}})
    assert recorder.calls("process_visual")[0]["args"] == ({"file_path": "/x"},)


def test_call_kwargs_expands_inputs(stub_tasks, recorder):
    stub_tasks.add("download_file", returns={"file_path": "/x"})

    env = python_driver.run(
        {"func": "download_file", "call": "kwargs"},
        {"path": "http://v/x.mp4", "job_id": "j1", "prompts": None},
    )

    assert env["status"] == "success"
    call = recorder.calls("download_file")[0]
    assert call["args"] == ()
    assert call["kwargs"] == {"path": "http://v/x.mp4", "job_id": "j1", "prompts": None}


def test_legacy_task_attribute_is_used_when_func_is_absent(stub_tasks, recorder):
    stub_tasks.add("process_audio")
    env = python_driver.run({"task": "process_audio"}, {})
    assert env["status"] == "success"
    assert recorder.calls("process_audio")


def test_func_wins_over_legacy_task_attribute(stub_tasks, recorder):
    stub_tasks.add("winner")
    stub_tasks.add("loser")
    python_driver.run({"func": "winner", "task": "loser"}, {})
    assert recorder.calls("winner")
    assert not recorder.calls("loser")


def test_explicit_module_attribute_is_honoured(install_module, recorder):
    other = install_module("other_pipeline")
    other.add("special")

    env = python_driver.run({"module": "other_pipeline", "func": "special"}, {})

    assert env["status"] == "success"
    assert recorder.calls("special")


# --------------------------------------------------------------------------
# Failure paths — all return a failure envelope, none raise
# --------------------------------------------------------------------------

def test_missing_func_returns_failure_envelope():
    env = python_driver.run({}, {})
    assert env["status"] == "error"
    assert "requires a 'func'" in env["error"]


def test_unimportable_module_returns_failure_envelope():
    env = python_driver.run({"module": "no_such_module_xyz", "func": "f"}, {})
    assert env["status"] == "error"
    assert "Cannot resolve python driver target" in env["error"]


def test_missing_attribute_returns_failure_envelope(stub_tasks):
    env = python_driver.run({"func": "not_defined"}, {})
    assert env["status"] == "error"
    assert "tasks.not_defined" in env["error"]


def test_target_raising_becomes_a_failure_envelope(stub_tasks):
    stub_tasks.add("process_audio", raises=RuntimeError("audio service exploded"))

    env = python_driver.run({"func": "process_audio"}, {})

    assert env["status"] == "error"
    assert env["error"] == "audio service exploded"
    assert env["data"] is None


def test_target_raising_a_bare_exception_still_yields_a_string_error(stub_tasks):
    stub_tasks.add("f", raises=KeyError("file_path"))
    env = python_driver.run({"func": "f"}, {})
    assert env["status"] == "error"
    assert "file_path" in env["error"]


# --------------------------------------------------------------------------
# Envelope normalisation
# --------------------------------------------------------------------------

def test_raw_dict_return_is_wrapped_once(stub_tasks):
    stub_tasks.add("f", returns={"file_path": "/x"})
    env = python_driver.run({"func": "f"}, {})
    assert env["data"] == {"file_path": "/x"}
    assert env["data"].get("status") is None       # not double-wrapped


def test_already_enveloped_return_is_passed_through(stub_tasks):
    stub_tasks.add("f", returns={"status": "error", "data": None, "error": "declined"})

    env = python_driver.run({"func": "f"}, {})

    assert env["status"] == "error"
    assert env["error"] == "declined"


def test_none_return_is_a_success_envelope_with_null_data(stub_tasks):
    stub_tasks.add("f", returns=False)   # falsy but not None
    assert python_driver.run({"func": "f"}, {})["data"] is False


@pytest.mark.parametrize("call_mode", ["payload", None, "anything-else"])
def test_only_the_literal_kwargs_mode_expands(stub_tasks, recorder, call_mode):
    stub_tasks.add("f")
    attrs = {"func": "f"}
    if call_mode is not None:
        attrs["call"] = call_mode

    python_driver.run(attrs, {"a": 1})

    assert recorder.calls("f")[0]["args"] == ({"a": 1},)
