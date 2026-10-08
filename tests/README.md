# DAG test suite

Unit tests for `dag/` that run with **no Docker, no Redis, no Celery and no
GPU** — the whole pipeline's orchestration layer, exercised in ~1.5 seconds.

## Running

```bash
python3 -m venv venv
./venv/bin/pip install -r tests/requirements-dev.txt
./venv/bin/python -m pytest          # everything
./venv/bin/python -m pytest -m "not slow"   # skip the concurrency timing tests
./venv/bin/python -m pytest -m gap          # just the known-gap tests
```

## How the isolation works

`worker/Dockerfile` does `COPY worker/ .` then `COPY dag/ ./dag`, so inside
the worker image `utils`, `consts` and `tasks` are **top-level modules on
sys.path**, not members of a package. `dag/readiness.py` and
`dag/drivers/python.py` import them under exactly those names.

The fixtures in `conftest.py` reproduce that layout by installing stub
modules into `sys.modules` under the same top-level names, so the real
import statements resolve without the real Docker SDK. `http`-driver and
multi-host readiness tests run against a real loopback `ThreadingHTTPServer`
rather than a monkeypatched `requests`, so the actual network path executes.

## Known-gap tests

Tests marked `@pytest.mark.gap` are paired with `xfail(strict=True)`: they
assert the *desired* behaviour and currently fail, so they stay quiet. The
day someone closes the gap, `strict=True` turns the unexpected pass into a
**failure**, forcing the marker and the companion "current behaviour" test
to be removed rather than silently rotting.

| Gap | Test |
|---|---|
| Duplicate task ids silently merge into one node | `test_parser.py::test_duplicate_task_id_should_be_rejected` |
| A missing `utils` escapes as `ModuleNotFoundError`, bypassing `on_failure` | `test_readiness.py::test_single_host_missing_utils_should_be_a_service_not_ready_error` |
| No lease/refcount around a shared service | `test_readiness.py::test_readiness_should_expose_occupancy_tracking` |
| Concurrent nodes start the same service twice | `test_engine.py::test_shared_service_should_be_started_once_for_concurrent_nodes` |
| A finished sibling stops the container the other is still using | `test_engine.py::test_shared_service_should_not_be_stopped_while_still_in_use` |

## Not covered here

Needs a live stack, deferred to production testing: real `start_service`
container orchestration, `scripts/start_services.py`'s `nvidia-smi`/`free -m`
measured pass, Celery/Redis task routing, and the analysis services' actual
HTTP contracts.
