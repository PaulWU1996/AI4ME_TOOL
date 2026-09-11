"""Per-node service readiness, per SCOPE_PLAN.md section 3b.

Two deployment modes, one interface: `ensure_ready(service_name)` either
returns (the service is usable) or raises `ServiceNotReadyError` — which
becomes a normal node failure in dag/engine.py, subject to the workflow's
own `on_failure` setting. `release(service_name)` is the symmetric
"I'm done with it" call, made after the node's driver runs.

Worker-specific imports (`utils`, `consts`) are done lazily, inside the
functions that need them, not at module top — this file is part of the
shared dag/ package (copied into both the controller and worker Docker
images), and the controller image has neither the Docker SDK nor
worker/utils.py available.
"""
import os


class ServiceNotReadyError(Exception):
    """Raised when a node's declared service isn't usable and nothing more
    can be done about it here (multi-host mode, or single-host coldstart
    recovery itself failing)."""


def _single_host_ensure_ready(service_name):
    # Docker-socket-based: checks/starts a container on this same host.
    # Self-healing — this is start_service's existing coldstart-or-verify
    # behavior, unchanged, just invoked generically instead of inline in
    # a task function's body.
    from utils import start_service

    try:
        start_service(service_name)
    except Exception as e:
        raise ServiceNotReadyError(f"'{service_name}' failed to become ready: {e}") from e


def _single_host_release(service_name):
    from utils import stop_service

    stop_service(service_name)


def _multi_host_ensure_ready(service_name):
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


def _multi_host_release(service_name):
    # Nothing to release — multi-host mode never started anything.
    pass


_ENSURE_READY_STRATEGIES = {
    "single_host": _single_host_ensure_ready,
    "multi_host": _multi_host_ensure_ready,
}

_RELEASE_STRATEGIES = {
    "single_host": _single_host_release,
    "multi_host": _multi_host_release,
}


def _deployment_mode():
    mode = os.getenv("DEPLOYMENT_MODE", "single_host")
    if mode not in _ENSURE_READY_STRATEGIES:
        raise ValueError(
            f"Unknown DEPLOYMENT_MODE '{mode}'. Choose from: {list(_ENSURE_READY_STRATEGIES)}"
        )
    return mode


def validate_mode():
    """Raise if DEPLOYMENT_MODE is set to something unrecognized. Exposed
    separately from ensure_ready so callers can validate cheaply/early
    (e.g. a pre-flight pass) without needing a real service_name yet."""
    _deployment_mode()


def ensure_ready(service_name):
    """Ensure `service_name` is usable before a node that declared it runs.

    Deployment mode is picked via the DEPLOYMENT_MODE env var
    ("single_host", default, or "multi_host"). Single-host self-heals
    (starts the container if needed); multi-host only checks reachability
    and raises if it's not up — see SCOPE_PLAN.md section 3b for why
    that's a deliberate scope boundary, not a gap.
    """
    _ENSURE_READY_STRATEGIES[_deployment_mode()](service_name)


def release(service_name):
    """Symmetric counterpart to ensure_ready — call after the node's driver
    has run, regardless of success/failure (a finally block)."""
    _RELEASE_STRATEGIES[_deployment_mode()](service_name)
