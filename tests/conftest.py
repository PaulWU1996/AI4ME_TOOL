"""Shared fixtures for the Docker-free DAG suite.

Nothing here starts a container, talks to Redis, or imports Celery. The
worker-side modules that `dag/` reaches for lazily (`utils`, `consts`) and
the python driver's default target module (`tasks`) are installed as stub
modules in `sys.modules`, which is exactly how they resolve inside the
worker image — see worker/Dockerfile's `COPY worker/ .` flattening, which
puts them on sys.path as top-level modules rather than inside a package.
"""
import json
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


# --------------------------------------------------------------------------
# Call recording
# --------------------------------------------------------------------------

class Recorder:
    """Thread-safe ordered log of everything the stubs were asked to do."""

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []

    def log(self, kind, name, **detail):
        with self._lock:
            self.events.append({"kind": kind, "name": name, "at": time.monotonic(), **detail})

    def of_kind(self, kind):
        return [e for e in self.events if e["kind"] == kind]

    def names(self, kind=None):
        return [e["name"] for e in self.events if kind is None or e["kind"] == kind]

    def calls(self, name):
        return [e for e in self.events if e["kind"] == "call" and e["name"] == name]

    def call_args(self, name):
        """kwargs of the single call to `name` (fails loudly if not exactly one)."""
        matched = self.calls(name)
        assert len(matched) == 1, f"expected exactly one call to {name!r}, got {len(matched)}"
        return matched[0]["kwargs"]


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture(autouse=True)
def reset_service_leases():
    """dag/readiness.py's lease table is module-global. Without this, a test
    that acquires a service and does not release it would leave a holder
    behind, and the next test to want that service would block until
    LEASE_TIMEOUT (an hour) rather than failing."""
    from dag import readiness

    readiness.reset_leases()
    yield
    readiness.reset_leases()


@pytest.fixture(autouse=True)
def short_lease_timeout(monkeypatch):
    """Tests that deliberately exhaust a service's slots should fail in
    milliseconds, not wait out the production-sized hour."""
    monkeypatch.setenv("LEASE_TIMEOUT", "2")


# --------------------------------------------------------------------------
# Stub modules
# --------------------------------------------------------------------------

class StubModule:
    """A module object installed in sys.modules, populated per-test."""

    def __init__(self, name, recorder):
        self.name = name
        self.recorder = recorder
        self.module = types.ModuleType(name)

    def add(self, func_name, *, returns=None, raises=None, delay=0.0, fn=None):
        """Register a callable that records its invocation.

        returns: value to return (default: a small dict naming the task)
        raises:  exception instance to raise instead of returning
        delay:   seconds to sleep mid-call, to make concurrency observable
        fn:      full override; still recorded, but computes its own result
        """
        recorder = self.recorder

        def stub(*args, **kwargs):
            recorder.log("call", func_name, args=args, kwargs=kwargs, phase="enter")
            if delay:
                time.sleep(delay)
            try:
                if raises is not None:
                    raise raises
                if fn is not None:
                    return fn(*args, **kwargs)
                return {"ran": func_name} if returns is None else returns
            finally:
                recorder.log("return", func_name)

        stub.__name__ = func_name
        setattr(self.module, func_name, stub)
        return stub

    def set(self, attr_name, value):
        setattr(self.module, attr_name, value)
        return value


@pytest.fixture
def install_module(recorder):
    """Factory installing a stub module under a top-level name, restoring
    whatever was in sys.modules afterwards."""
    installed = []

    def _install(name):
        stub = StubModule(name, recorder)
        previous = sys.modules.get(name)
        installed.append((name, previous))
        sys.modules[name] = stub.module
        return stub

    yield _install

    for name, previous in reversed(installed):
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


@pytest.fixture
def stub_tasks(install_module):
    """The python driver's default target module (engine defaults module to
    'tasks'), so workflow JSON needs no 'module' attribute."""
    return install_module("tasks")


@pytest.fixture
def stub_utils(install_module):
    """worker/utils.py's start_service/stop_service, as dag/readiness.py's
    single_host strategy imports them."""
    stub = install_module("utils")
    stub.add("start_service")
    stub.add("stop_service")
    return stub


@pytest.fixture
def stub_consts(install_module):
    """worker/consts.py's SERVICE_HEALTH_URLS, for multi_host readiness."""
    stub = install_module("consts")
    stub.set("SERVICE_HEALTH_URLS", {})
    return stub


# --------------------------------------------------------------------------
# Workflow / DAG construction
# --------------------------------------------------------------------------

@pytest.fixture
def workflow_file(tmp_path):
    """Write a workflow dict to disk and return its path."""
    counter = {"n": 0}

    def _write(spec, name=None):
        counter["n"] += 1
        path = tmp_path / (name or f"workflow_{counter['n']}.json")
        path.write_text(json.dumps(spec, indent=2))
        return str(path)

    return _write


@pytest.fixture
def make_dag(workflow_file):
    """Build a DAG from a task list, going through the real Parser so tests
    stay declarative and exercise the same path production does."""
    from dag.parser import Parser

    def _make(tasks, settings=None, workflow=None):
        spec = {
            "workflow": workflow or {"name": "test", "version": "1.0"},
            "settings": settings or {},
            "tasks": tasks,
        }
        return Parser(workflow_file(spec))

    return _make


@pytest.fixture
def make_engine(make_dag):
    """Construct a DAGEngine over an inline task list."""
    from dag.engine import DAGEngine

    def _make(tasks, *, settings=None, job_id="job-1", **kwargs):
        parsed = make_dag(tasks, settings=settings)
        kwargs.setdefault("on_failure", parsed.settings.get("on_failure", "stop"))
        return DAGEngine(parsed.dag, job_id=job_id, **kwargs)

    return _make


# --------------------------------------------------------------------------
# Loopback HTTP server (for the http driver + multi_host readiness)
# --------------------------------------------------------------------------

class LoopbackServer:
    def __init__(self, url, state):
        self.url = url
        self._state = state

    def respond(self, *, status=200, body=None, content_type="application/json"):
        """Install a canned response for every subsequent request."""
        payload = b"" if body is None else (
            json.dumps(body).encode() if content_type == "application/json"
            else str(body).encode()
        )

        def handler(request):
            request.send_response(status)
            request.send_header("Content-Type", content_type)
            request.send_header("Content-Length", str(len(payload)))
            request.end_headers()
            request.wfile.write(payload)

        self._state["handler"] = handler

    def echo(self, status=200):
        """Echo the request back as JSON: method, path, headers, parsed body."""
        def handler(request):
            length = int(request.headers.get("Content-Length") or 0)
            raw = request.rfile.read(length) if length else b""
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = raw.decode()
            payload = json.dumps({
                "method": request.command,
                "path": request.path,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": parsed,
            }).encode()
            request.send_response(status)
            request.send_header("Content-Type", "application/json")
            request.send_header("Content-Length", str(len(payload)))
            request.end_headers()
            request.wfile.write(payload)

        self._state["handler"] = handler

    @property
    def requests(self):
        return self._state["requests"]


@pytest.fixture
def http_server():
    state = {"handler": None, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"   # no keep-alive: one request, one thread

        def _dispatch(self):
            state["requests"].append({"method": self.command, "path": self.path})
            handler = state["handler"]
            if handler is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            handler(self)

        do_GET = do_POST = do_PUT = do_DELETE = _dispatch

        def log_message(self, *args):  # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # poll_interval well under serve_forever's 0.5s default, or every
    # teardown pays half a second waiting for shutdown() to be noticed.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield LoopbackServer(f"http://127.0.0.1:{server.server_address[1]}", state)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def closed_port():
    """A port nothing is listening on, for connection-refused paths."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
