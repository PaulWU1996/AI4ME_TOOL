import os
import time
import requests
import xmltodict
import json
import subprocess
import docker

from consts import (
    compose_file,
    project_dir,
    HEALTH_CHECK_TIMEOUT,
    HEALTH_CHECK_INTERVAL,
    api_key_path,
    shared_path,
    visual_api_url,
    visual_api_admin_key,
)


docker_client = docker.from_env()


# --- Service Container Management ---
def _compose(service_name, *args):
    cmd = ["docker", "compose", "-f", compose_file]
    if project_dir:
        cmd += ["--project-directory", project_dir]
    cmd += list(args) + [service_name]
    subprocess.run(cmd, check=True)


def start_service(service_name, max_retries=1):
    for attempt in range(max_retries + 1):
        if attempt == 0:
            _compose(service_name, "up", "-d")
        else:
            _compose(service_name, "restart")

        # health check
        elapsed = 0
        while elapsed < HEALTH_CHECK_TIMEOUT:
            container = docker_client.containers.get(service_name)
            container.reload()
            health = container.attrs.get("State", {}).get("Health", {}).get("Status")
            if health == "healthy":
                return
            time.sleep(HEALTH_CHECK_INTERVAL)
            elapsed += HEALTH_CHECK_INTERVAL
        else:
            if attempt < max_retries:
                print(
                    f"[{service_name}] Health check failed after {HEALTH_CHECK_TIMEOUT}s. Retrying ({attempt}/{max_retries})..."
                )
            else:
                print(
                    f"[{service_name}] Health check failed after {HEALTH_CHECK_TIMEOUT}s. No more retries left."
                )

    _compose(service_name, "stop")
    raise RuntimeError(f"[Service Manager] {service_name} failed to become healthy!")


def stop_service(service_name):
    print(f"[Service Manager] Stopping {service_name}")
    _compose(service_name, "stop")


# --- Support functions ---
def save_to_disk(job_id, filename, data):
    output_dir = os.path.join(shared_path, job_id)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def ensure_api_key(
    api_dir=api_key_path, admin_key=visual_api_admin_key
):  # change the api_dir
    key_file_path = os.path.join(api_dir, "api.key")

    if os.path.exists(key_file_path):
        with open(key_file_path, "r") as f:
            existing_key = f.read().strip()
            if existing_key:
                print(f"[Key Manager] API key already exists: {existing_key}")
                return existing_key
    else:
        print(
            f"[Key Manager] API key file not found. Creating new key at {key_file_path}"
        )
        gen_url = visual_api_url + "/generate"
        headers = {"X-Admin-Key": admin_key, "Content-Type": "application/json"}
        payload = {"client_name": "client_ai4me", "expire_in_days": 365}

        try:
            response = requests.post(gen_url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()

            data = response.json()
            new_key = data.get("api_key")

            if not new_key:
                raise ValueError(
                    f"Failed to obtain API key from visual service: {data}"
                )
            with open(key_file_path, "w") as f:
                f.write(new_key)
            print(f"[Key Manager] Generated and saved new API key: {new_key}")

            os.chmod(key_file_path, 0o644)
            return new_key

        except Exception as e:
            print(f"[Key Manager] Error ensuring API key: {str(e)}")
            return None


def extract_flat_captions(xml_body):
    """
    [{"start": 0.0, "end": 15.0, "captions": "..."}, ...]
    """
    data = xmltodict.parse(xml_body)

    try:
        segments_node = data.get("VideoAnalysis", {}).get("Segments", {})
        raw_segments = segments_node.get("Segment", [])
    except (AttributeError, KeyError):
        return []

    if isinstance(raw_segments, dict):
        raw_segments = [raw_segments]

    return [
        {
            "start": float(seg.get("StartTime", 0)),
            "end": float(seg.get("EndTime", 0)),
            "captions": seg.get("Description", ""),
        }
        for seg in raw_segments
    ]
