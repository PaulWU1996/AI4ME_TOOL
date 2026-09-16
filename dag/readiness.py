"""Per-node service readiness and occupancy, per SCOPE_PLAN sections 3b and 8.

Two deployment modes, one interface: `ensure_ready(service_name)` either
returns (the service is usable and this caller holds a slot on it) or raises
`ServiceNotReadyError` — which becomes a normal node failure in
dag/engine.py, subject to the workflow's own `on_failure` setting.
`release(service_name)` is the symmetric "I'm done with it" call, made after
the node's driver runs.

Worker-specific imports (`utils`, `consts`) are done lazily, inside the
functions that need them, not at module top — this file is part of the
shared dag/ package (copied into both the controller and worker Docker
images), and the controller image has neither the Docker SDK nor
worker/utils.py available.

## Leases

`ensure_ready`/`release` are reference-counted per service. The container is
started on the 0 -> 1 transition and stopped on the 1 -> 0 transition, so
two callers that both need the same service produce **one** start and **one**
stop, and neither can tear the container down while the other is still
mid-request.

That matters in two places at once:

  * `dag/engine.py` brackets a node that declares `service`, while the task
    body in `worker/tasks.py` brackets its own work. Before leases those two
    brackets fought: the task's `finally` stopped the container, then the
    engine stopped it again. Now they nest.
  * `DAGEngine.execute_parallel()` runs independent nodes concurrently. Two
    siblings sharing a service used to start it twice and let the first one
    to finish stop it under the second.

A per-service `concurrency` limit (default 1) additionally throttles how many
callers may hold a service at once, so a single-model GPU server is not
handed concurrent requests. Callers past the limit block until a slot frees
or `LEASE_TIMEOUT` elapses.

**Scope:** these are in-process locks. They cover threads inside one worker
process, which is what `execute_parallel()` and today's `--concurrency=1`
Celery worker need. They do *not* coordinate across worker processes or
hosts; that needs a Redis lock, and is the pooling work deferred in
SCOPE_PLAN's 2026-09-09 decision.
"""
import json
import os
import threading


class ServiceNotReadyError(Exception):
    """Raised when a node's declared service isn't usable and nothing more
    can be done about it here (multi-host mode, coldstart recovery itself
    failing, or no free slot before the lease timeout)."""


# --------------------------------------------------------------------------
# Deployment-mode strategies
# --------------------------------------------------------------------------

def _single_host_start(service_name):
    # Docker-socket-based: checks/starts a container on this same host.
    # Self-healing — this is start_service's existing coldstart-or-verify
    # behavior, unchanged, just invoked generically instead of inline in
    # a task function's body.
    try:
        from utils import start_service
    except ImportError as e:
        # The layout this depends on is worker/Dockerfile's `COPY worker/ .`,
        # which puts utils.py on sys.path as a top-level module. If that ever
        # changes, fail as a ServiceNotReadyError rather than letting a bare
        # ImportError escape past dag/engine.py's handler and bypass the
        # workflow's on_failure policy entirely.
        raise ServiceNotReadyError(
            f"single_host mode needs worker/utils.py importable as 'utils' "
            f"(service '{service_name}'): {e}"
        ) from e

    try:
        start_service(service_name)
    except Exception as e:
        raise ServiceNotReadyError(f"'{service_name}' failed to become ready: {e}") from e


def _single_host_stop(service_name):
    try:
        from utils import stop_service
    except ImportError as e:
        raise ServiceNotReadyError(
            f"single_host mode needs worker/utils.py importable as 'utils' "
            f"(service '{service_name}'): {e}"
        ) from e

    stop_service(service_name)


def _multi_host_start(service_name):
    # No Docker socket, no start authority — purely "is it reachable right
    # now." An HTTP GET to the service's documented health path (the same
    # paths already used by docker-compose.yml's own healthcheck blocks),
    # not the Docker-container health check single-host mode uses.
    import requests
    from consts import SERVICE_HEALTH_URLS

    health_url = SERVICE_HEALTH_URLS.get(service_name)
    if not health_url:
        raise ServiceNotReadyError(f"No health URL configured for '{service_name}'.")

    try:
        response = requests.get(health_url, timeout=10)
        response.raise_for_status()
    except Exception as e:
        raise ServiceNotReadyError(f"'{service_name}' is not reachable at {health_url}: {e}") from e


def _multi_host_stop(service_name):
    # Nothing to release — multi-host mode never started anything.
    pass


_START_STRATEGIES = {
    "single_host": _single_host_start,
    "multi_host": _multi_host_start,
}

_STOP_STRATEGIES = {
    "single_host": _single_host_stop,
    "multi_host": _multi_host_stop,
}


def _deployment_mode():
    mode = os.getenv("DEPLOYMENT_MODE", "single_host")
    if mode not in _START_STRATEGIES:
        raise ValueError(
            f"Unknown DEPLOYMENT_MODE '{mode}'. Choose from: {list(_START_STRATEGIES)}"
        )
    return mode


def validate_mode():
    """Raise if DEPLOYMENT_MODE is set to something unrecognized. Exposed
    separately from ensure_ready so callers can validate cheaply/early
    (e.g. a pre-flight pass) without needing a real service_name yet."""
    _deployment_mode()


# --------------------------------------------------------------------------
# Lease bookkeeping
# --------------------------------------------------------------------------

DEFAULT_CONCURRENCY = 1
DEFAULT_LEASE_TIMEOUT = 3600.0     # matches Celery's visibility_timeout

_condition = threading.Condition()
_leases = {}     # service -> {"holders": int, "ready": bool, "starting": bool, "error": str|None}


def _state(service_name):
    return _leases.setdefault(
        service_name,
        {"holders": 0, "waiters": 0, "owners": {}, "ready": False, "starting": False,
         "error": None},
    )


def _concurrency_limit(service_name):
    """How many callers may hold this service at once.

    Sourced from the SERVICE_CONCURRENCY env var (a JSON object keyed by
    service name). config/services.json is the declarative home for this
    alongside vram_mb/ram_mb, but it is not mounted into the worker image
    today, so the env var is what actually reaches the worker.

    Default 1: the GPU services host a single model and cannot sensibly take
    concurrent jobs, and the LLM services run one Ollama model apiece.
    """
    try:
        configured = json.loads(os.getenv("SERVICE_CONCURRENCY", "{}"))
        return max(1, int(configured.get(service_name, DEFAULT_CONCURRENCY)))
    except (ValueError, TypeError, AttributeError):
        return DEFAULT_CONCURRENCY


def _lease_timeout():
    try:
        return float(os.getenv("LEASE_TIMEOUT", DEFAULT_LEASE_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_LEASE_TIMEOUT


def _drop_slot(service_name):
    """Give back a slot taken during a failed acquisition."""
    with _condition:
        state = _state(service_name)
        state["holders"] = max(0, state["holders"] - 1)
        state["owners"].pop(threading.get_ident(), None)
        _condition.notify_all()


def holders(service_name):
    """How many callers currently hold `service_name`. For tests and logs."""
    with _condition:
        return _state(service_name)["holders"]


def hold_depth(service_name):
    """How deeply the calling thread has nested its hold on `service_name`."""
    with _condition:
        return _state(service_name)["owners"].get(threading.get_ident(), 0)


def reset_leases():
    """Drop all lease bookkeeping. Tests only — never call this while a job
    is in flight, since it discards the record of who holds what."""
    with _condition:
        _leases.clear()
        _condition.notify_all()


# --------------------------------------------------------------------------
# Public interface
# --------------------------------------------------------------------------

def ensure_ready(service_name, timeout=None):
    """Take a slot on `service_name` and ensure it is usable.

    Starts the service on the first concurrent holder and blocks later
    holders until that start finishes, so no caller is handed a container
    that is still booting. Raises ServiceNotReadyError if no slot frees
    within the lease timeout, or if the start itself fails.

    Every successful call must be matched by exactly one `release()`.
    """
    mode = _deployment_mode()
    limit = _concurrency_limit(service_name)
    deadline = _lease_timeout() if timeout is None else timeout

    me = threading.get_ident()

    with _condition:
        state = _state(service_name)
        if state["owners"].get(me):
            # Re-entrant: this thread already holds the service, so the task
            # body nested inside dag/engine.py's bracket is the same logical
            # holder, not a second one competing for a slot. Without this,
            # a node with concurrency 1 would deadlock against itself.
            state["owners"][me] += 1
            return
        # Registering as a waiter *before* blocking is what lets release()
        # see that someone is queued and skip the stop, so a service handed
        # straight from one node to the next is not needlessly cycled.
        state["waiters"] += 1
        try:
            if not _condition.wait_for(lambda: state["holders"] < limit, deadline):
                raise ServiceNotReadyError(
                    f"'{service_name}' is busy ({state['holders']}/{limit} slots held); "
                    f"no slot freed within {deadline}s."
                )
        finally:
            state["waiters"] -= 1
        state["holders"] += 1
        state["owners"][me] = 1
        if state["ready"]:
            return                       # already up, nothing to start
        if state["starting"]:
            i_start = False
        else:
            state["starting"] = True
            i_start = True

    if i_start:
        try:
            _START_STRATEGIES[mode](service_name)
        except Exception as e:
            with _condition:
                state = _state(service_name)
                state["starting"] = False
                state["ready"] = False
                state["error"] = str(e)
                _condition.notify_all()
            _drop_slot(service_name)
            raise
        with _condition:
            state = _state(service_name)
            state["starting"] = False
            state["ready"] = True
            state["error"] = None
            _condition.notify_all()
        return

    # Someone else is starting it; wait for them to finish, then inherit.
    with _condition:
        state = _state(service_name)
        if not _condition.wait_for(lambda: not state["starting"], deadline):
            failure = f"'{service_name}' did not finish starting within {deadline}s."
            ready = False
        elif state["ready"]:
            return
        else:
            failure = f"'{service_name}' failed to become ready: {state['error']}"
            ready = False
    if not ready:
        _drop_slot(service_name)
        raise ServiceNotReadyError(failure)


def release(service_name):
    """Give back a slot taken by ensure_ready.

    Stops the service only when the last holder lets go, so a caller nested
    inside another caller's bracket (a task body inside dag/engine.py's
    bracket) cannot tear the container down early.

    Safe to call for a service that was never acquired — it is a no-op, so a
    `finally` block does not need to know whether acquisition succeeded.
    """
    mode = _deployment_mode()

    me = threading.get_ident()

    with _condition:
        state = _state(service_name)
        depth = state["owners"].get(me, 0)
        if depth == 0:
            return                      # never acquired here: no-op
        if depth > 1:
            state["owners"][me] = depth - 1
            return                      # inner bracket of a nested hold
        state["owners"].pop(me, None)
        state["holders"] -= 1
        # Hand the service straight to a queued caller instead of stopping
        # and cold-starting it again between two nodes that both want it.
        last = state["holders"] == 0 and state["waiters"] == 0
        if last:
            state["ready"] = False
        _condition.notify_all()

        if not last:
            return
        # Deliberately inside the lock: stopping takes seconds, and letting a
        # new acquirer start the container while the stop is still in flight
        # would race. Blocking here is acceptable for an in-process lease in
        # a --concurrency=1 worker; a cross-process lease would need a real
        # distributed lock instead (see the module docstring).
        _STOP_STRATEGIES[mode](service_name)
