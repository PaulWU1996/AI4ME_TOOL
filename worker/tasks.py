import json
import subprocess
import sys
from pathlib import Path

import requests
from celery import Celery
from consts import (
    python_call_timeout,
    python_script_root,
    redis_host,
    redis_port,
)

app = Celery(
    "tasks",
    broker=f"redis://{redis_host}:{redis_port}/0",
    backend=f"redis://{redis_host}:{redis_port}/0",
)

app.conf.update(
    broker_transport_options={"visibility_timeout": 3600},
    result_expires=86400,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    result_persistent=True,
    task_reject_on_worker_lost=True,
)

def report_progress(job_id, stage, message):
    app.backend.store_result(
        job_id,
        {"job_id": job_id, "stage": stage, "message": message},
        state="PROGRESS",
    )

@app.task(name="tasks.http_call")
def http_call(payload, url, method="POST", headers=None, timeout=60, 
              body=None, merge=False):
    """Generic service call backing `driver: "http"` workflow nodes.

    The predecessor result arrives positionally as `payload`.
    Failures raise, so a failed node aborts the chain.

    `save` persists the parsed response to disk as
    `{basename(file_path)}_{save}.json` (e.g. "summarise_output"), and
    `merge` returns `{**payload, **response}` so a later node in the chain
    keeps `job_id`/`prompts`/`file_path` from the workflow context.
    """
    job_id = payload.get("job_id", "unknown_job")

    request_body = {**payload, **(body or {})}
    response = requests.request(
        method, url, json=request_body, headers=headers or {}, timeout=timeout,
    )
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError:
        return response.text

    output = {**payload, **result} if merge else result
        
    report_progress(job_id, "unknown", "complete")
    return output


def _task_script_path(script):
    if not isinstance(script, str) or not script:
        raise ValueError("python_call: 'script' must be a non-empty string.")
    root = Path(python_script_root).resolve()
    path = (root / script).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"python_call: script '{script}' is not inside {root}.")
    return path

@app.task(name="tasks.python_call")
def python_call(payload, script, params=None, timeout=python_call_timeout, merge=False):
    if not isinstance(payload, dict):
        raise TypeError("python_call: payload must be a mapping.")
    if params is not None and not isinstance(params, dict):
        raise ValueError("python_call: params must be a mapping.")

    path = _task_script_path(script)
    request = {**payload, **(params or {})}
    completed = subprocess.run(
        [sys.executable, str(path)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.stderr.strip():
        print(f"[python_call] {script}: {completed.stderr.strip()}")
    if completed.returncode != 0:
        raise RuntimeError(
            f"python_call: {script} exited with {completed.returncode}: {completed.stderr.strip()}"
        )

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"python_call: {script} did not return JSON.") from exc

    if merge:
        if not isinstance(result, dict):
            raise ValueError("python_call: merge requires a JSON object result.")
        return {**payload, **result}
    return result
