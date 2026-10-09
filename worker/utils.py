import json
import os
import subprocess

import docker
from consts import (
    compose_file,
    project_dir,
    shared_path,
)

_docker_client = None

def _client():
    """Lazily-bound Docker client.

    Constructed on first use rather than at import so a worker can boot (and
    run non-service tasks) on a node without a reachable Docker socket; the
    connection is only needed when the worker actually manages a container.
    """
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client

# keep this for now since we may write a docker driver
def _compose(service_name, *args):
    cmd = ["docker", "compose", "-f", compose_file]
    if project_dir:
        cmd += ["--project-directory", project_dir]
    cmd += list(args) + [service_name]
    subprocess.run(cmd, check=True)


# --- Support functions ---
def save_to_shared_disk(job_id, filename, data):
    output_dir = os.path.join(shared_path, job_id)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_json_file(file_path) -> dict | None:
    try:
        if not os.path.exists(file_path):
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"[Error] Invalid JSON in {file_path}: {e}")
        return None
    except Exception as e:
        print(f"[Error] Failed to read {file_path}: {e}")
        return None
