"""The standard task-result envelope (SCOPE_PLAN section 2)."""
import pytest

from dag.envelope import ENVELOPE_KEYS, as_envelope, failure, is_envelope, success


def test_success_shape():
    assert success({"a": 1}) == {"status": "success", "data": {"a": 1}, "error": None}


def test_success_accepts_non_dict_data():
    assert success("plain text")["data"] == "plain text"
    assert success(None)["data"] is None


def test_failure_shape_and_string_coercion():
    env = failure(RuntimeError("boom"))
    assert env["status"] == "error"
    assert env["data"] is None
    assert env["error"] == "boom"


def test_failure_accepts_a_plain_string():
    assert failure("nope")["error"] == "nope"


@pytest.mark.parametrize("value", [
    {"status": "success", "data": {}, "error": None},
    {"status": "error", "data": None, "error": "x"},
    {"status": "success", "data": {}, "error": None, "extra": "allowed"},
])
def test_is_envelope_accepts_envelope_shapes(value):
    assert is_envelope(value) is True


@pytest.mark.parametrize("value", [
    {"status": "success"},                      # partial
    {"status": "success", "data": {}},          # partial
    {"data": {}, "error": None},                # no status
    "a string",
    None,
    42,
    ["status", "data", "error"],
])
def test_is_envelope_rejects_everything_else(value):
    assert is_envelope(value) is False


def test_envelope_keys_are_the_documented_three():
    assert ENVELOPE_KEYS == {"status", "data", "error"}


def test_as_envelope_passes_through_an_existing_envelope():
    env = success({"file_path": "/app/tmp/j/v.mp4"})
    assert as_envelope(env) is env          # identity: no re-wrapping


def test_as_envelope_wraps_a_raw_task_return():
    """worker/tasks.py functions still return bare dicts until migrated."""
    wrapped = as_envelope({"file_path": "/x", "prompts": None})
    assert wrapped["status"] == "success"
    assert wrapped["data"] == {"file_path": "/x", "prompts": None}


def test_as_envelope_does_not_double_wrap_an_error_envelope():
    env = failure("already failed")
    assert as_envelope(env) is env
    assert as_envelope(env)["status"] == "error"


def test_as_envelope_wraps_a_partial_dict_rather_than_trusting_it():
    """A dict that merely has a 'status' key is data, not an envelope —
    important because service responses often carry their own 'status'."""
    service_response = {"status": "completed", "segments": []}
    wrapped = as_envelope(service_response)
    assert wrapped["status"] == "success"
    assert wrapped["data"] == service_response
