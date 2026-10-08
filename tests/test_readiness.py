"""dag/readiness.py — the single_host / multi_host strategy split (SCOPE_PLAN 3b).

No Docker: single_host's `from utils import start_service` resolves against a
stub module installed in sys.modules under the top-level name `utils`, which
is how it resolves in the worker image (worker/Dockerfile flattens worker/
into /app, so utils.py is a top-level module, not a package member).
"""
import sys
import threading
import time

import pytest

from dag import readiness
from dag.readiness import ServiceNotReadyError, ensure_ready, release, validate_mode


@pytest.fixture(autouse=True)
def clean_deployment_mode(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)


# --------------------------------------------------------------------------
# Mode selection
# --------------------------------------------------------------------------

def test_default_mode_is_single_host():
    assert readiness._deployment_mode() == "single_host"


def test_validate_mode_accepts_both_known_modes(monkeypatch):
    for mode in ("single_host", "multi_host"):
        monkeypatch.setenv("DEPLOYMENT_MODE", mode)
        validate_mode()


def test_unknown_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "kubernetes")
    with pytest.raises(ValueError, match="Unknown DEPLOYMENT_MODE 'kubernetes'"):
        validate_mode()


def test_unknown_mode_also_blocks_ensure_ready(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "typo")
    with pytest.raises(ValueError):
        ensure_ready("audioservice")


# --------------------------------------------------------------------------
# single_host
# --------------------------------------------------------------------------

def test_single_host_ensure_ready_starts_the_container(stub_utils, recorder):
    ensure_ready("audioservice")
    assert recorder.calls("start_service")[0]["args"] == ("audioservice",)


def test_single_host_release_stops_the_container(stub_utils, recorder):
    ensure_ready("visualservice")
    release("visualservice")
    assert recorder.calls("stop_service")[0]["args"] == ("visualservice",)


def test_release_without_a_matching_acquire_is_a_noop(stub_utils, recorder):
    """So a `finally` block need not know whether acquisition succeeded."""
    release("visualservice")
    assert not recorder.calls("stop_service")


def test_single_host_start_failure_becomes_service_not_ready(install_module):
    utils = install_module("utils")
    utils.add("start_service", raises=RuntimeError("failed to become healthy!"))
    utils.add("stop_service")

    with pytest.raises(ServiceNotReadyError) as excinfo:
        ensure_ready("audioservice")

    assert "'audioservice' failed to become ready" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_single_host_ensure_ready_is_silent_on_success(stub_utils):
    assert ensure_ready("transcriptservice") is None


# --------------------------------------------------------------------------
# multi_host
# --------------------------------------------------------------------------

@pytest.fixture
def multi_host(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi_host")


def test_multi_host_ready_when_health_endpoint_returns_200(
    multi_host, stub_consts, http_server
):
    http_server.respond(body={"status": "ok"})
    stub_consts.set("SERVICE_HEALTH_URLS", {"audioservice": http_server.url + "/health/"})

    assert ensure_ready("audioservice") is None
    assert http_server.requests[-1]["path"] == "/health/"


def test_multi_host_uses_a_plain_get(multi_host, stub_consts, http_server):
    http_server.respond(body={})
    stub_consts.set("SERVICE_HEALTH_URLS", {"s": http_server.url + "/health"})

    ensure_ready("s")

    assert http_server.requests[-1]["method"] == "GET"


@pytest.mark.parametrize("status", [500, 503, 404])
def test_multi_host_unhealthy_service_raises(multi_host, stub_consts, http_server, status):
    http_server.respond(status=status, body={})
    stub_consts.set("SERVICE_HEALTH_URLS", {"audioservice": http_server.url + "/health/"})

    with pytest.raises(ServiceNotReadyError, match="is not reachable"):
        ensure_ready("audioservice")


def test_multi_host_unreachable_service_raises(multi_host, stub_consts, closed_port):
    stub_consts.set(
        "SERVICE_HEALTH_URLS",
        {"visualservice": f"http://127.0.0.1:{closed_port}/health/"},
    )

    with pytest.raises(ServiceNotReadyError, match="'visualservice' is not reachable"):
        ensure_ready("visualservice")


def test_multi_host_unconfigured_service_raises(multi_host, stub_consts):
    with pytest.raises(ServiceNotReadyError, match="No health URL configured for 'mystery'"):
        ensure_ready("mystery")


def test_multi_host_release_is_a_noop(multi_host, stub_consts):
    assert release("audioservice") is None


def test_multi_host_release_never_touches_the_docker_side(multi_host, stub_consts):
    """The controller image ships dag/ but has neither worker/utils.py nor
    the Docker SDK — multi_host must not import `utils` even transitively."""
    sys.modules.pop("utils", None)
    release("audioservice")
    assert "utils" not in sys.modules


# --------------------------------------------------------------------------
# Known gaps
# --------------------------------------------------------------------------

def test_single_host_missing_utils_is_a_service_not_ready_error(monkeypatch):
    """single_host's `from utils import start_service` depends on
    worker/Dockerfile's `COPY worker/ .` flattening. If that layout ever
    changes, the failure must still arrive as ServiceNotReadyError, so
    dag/engine.py sees a node failure and the workflow's `on_failure` applies
    — rather than a bare ImportError escaping the engine entirely.
    """
    monkeypatch.delitem(sys.modules, "utils", raising=False)
    with pytest.raises(ServiceNotReadyError, match="importable as 'utils'"):
        ensure_ready("audioservice")


def test_failed_acquisition_leaves_no_holder_behind(monkeypatch):
    """A start that fails must give its slot back, or the service would be
    permanently unacquirable for the life of the process."""
    monkeypatch.delitem(sys.modules, "utils", raising=False)
    with pytest.raises(ServiceNotReadyError):
        ensure_ready("audioservice")
    assert readiness.holders("audioservice") == 0


# --------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------

def test_holders_counts_up_and_down(stub_utils):
    assert readiness.holders("audioservice") == 0
    ensure_ready("audioservice")
    assert readiness.holders("audioservice") == 1
    release("audioservice")
    assert readiness.holders("audioservice") == 0


def test_start_happens_once_per_lease_generation(stub_utils, recorder):
    ensure_ready("audioservice")
    release("audioservice")
    ensure_ready("audioservice")
    release("audioservice")
    assert len(recorder.calls("start_service")) == 2
    assert len(recorder.calls("stop_service")) == 2


def test_nested_acquire_is_reentrant(stub_utils, recorder):
    """A task body bracketing its own work inside dag/engine.py's bracket is
    the same logical holder — it must not take a second slot, and its inner
    release must not stop the container."""
    ensure_ready("visualservice")
    ensure_ready("visualservice")
    assert readiness.hold_depth("visualservice") == 2
    assert readiness.holders("visualservice") == 1

    release("visualservice")
    assert not recorder.calls("stop_service"), "inner release stopped the container"
    assert readiness.holders("visualservice") == 1

    release("visualservice")
    assert len(recorder.calls("stop_service")) == 1
    assert len(recorder.calls("start_service")) == 1


def test_concurrency_limit_blocks_a_second_thread(stub_utils, monkeypatch):
    monkeypatch.setenv("LEASE_TIMEOUT", "0.3")
    ensure_ready("audioservice")
    outcome = {}

    def contender():
        try:
            ensure_ready("audioservice")
            outcome["result"] = "acquired"
        except ServiceNotReadyError as e:
            outcome["result"] = f"blocked: {e}"

    thread = threading.Thread(target=contender)
    thread.start()
    thread.join(timeout=5)

    assert outcome["result"].startswith("blocked"), outcome
    assert "slots held" in outcome["result"]
    release("audioservice")


def test_concurrency_limit_is_configurable(stub_utils, monkeypatch):
    monkeypatch.setenv("SERVICE_CONCURRENCY", '{"transcriptservice": 2}')
    ensure_ready("transcriptservice")
    outcome = {}

    def contender():
        ensure_ready("transcriptservice")
        outcome["holders"] = readiness.holders("transcriptservice")
        release("transcriptservice")

    thread = threading.Thread(target=contender)
    thread.start()
    thread.join(timeout=5)

    assert outcome.get("holders") == 2, outcome
    release("transcriptservice")


def test_a_waiting_thread_inherits_the_running_service(stub_utils, recorder):
    """Handing a service straight to a queued caller instead of stopping and
    cold-starting it again between two nodes that both want it."""
    ensure_ready("audioservice")
    started = threading.Event()
    done = threading.Event()

    def contender():
        started.set()
        ensure_ready("audioservice")
        release("audioservice")
        done.set()

    thread = threading.Thread(target=contender)
    thread.start()
    started.wait(timeout=5)
    time.sleep(0.2)          # let the contender actually reach the wait
    release("audioservice")  # last holder, but a waiter is queued
    done.wait(timeout=5)
    thread.join(timeout=5)

    assert len(recorder.calls("start_service")) == 1, "service was needlessly restarted"
    assert len(recorder.calls("stop_service")) == 1, "service was stopped mid-handover"


def test_reset_leases_clears_state(stub_utils):
    ensure_ready("audioservice")
    assert readiness.holders("audioservice") == 1
    readiness.reset_leases()
    assert readiness.holders("audioservice") == 0
