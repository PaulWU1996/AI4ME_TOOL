import os
import time
import shutil
import requests
import xmltodict
import json
from celery import Celery
from urllib.parse import urlparse
import subprocess
import docker
import glob

# --- Config Settings ---
compose_file = os.getenv("COMPOSE_FILE", "/app/docker-compose.yml")
project_dir = os.getenv("COMPOSE_PROJECT_DIR")

redis_host = os.getenv("REDIS_HOST", "127.0.0.1")
redis_port = os.getenv("REDIS_PORT", "6379")

# Service URL
audio_host = os.getenv("AUDIO_HOST", "localhost")
audio_port = os.getenv("AUDIO_PORT", "9002")
visual_host = os.getenv("VISUAL_HOST", "localhost")
visual_port = os.getenv("VISUAL_PORT", "9001")
visual_api_admin_key = os.getenv("ADMIN_KEY", "ai4me_admin_password")
transcript_host = os.getenv("TRANSCRIPT_HOST", "localhost")
transcript_port = os.getenv("TRANSCRIPT_PORT", "9003")


audio_api_url = f"http://{audio_host}:{audio_port}/process_audio/"
visual_api_url = f"http://{visual_host}:{visual_port}"
transcript_api_url = f"http://{transcript_host}:{transcript_port}/process/"

shared_path = os.getenv("SHARED_PATH", "/app/tmp")
api_key_path = os.getenv("API_KEY_PATH", "/app/data")

HEALTH_CHECK_TIMEOUT = 330
HEALTH_CHECK_INTERVAL = 60

docker_client = docker.from_env()

# --- Celery ---
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


# --- Download Task ---
@app.task(
    name="tasks.download_file",
    bind=True,
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=60,
)
def download_file(self, path, job_id, prompts=None):
    output_dir = os.path.join(shared_path, job_id)
    os.makedirs(output_dir, exist_ok=True)
    try:
        parsed = urlparse(path)
        filename = os.path.basename(parsed.path)
        if not os.path.splitext(filename)[1]:
            ext = {"transcript": "txt", "audio": "wav"}.get(filename)
            if ext:
                filename = f"{filename}.{ext}"
        dest = os.path.join(output_dir, filename)

        if parsed.scheme == "s3":
            import boto3

            s3 = boto3.client("s3")
            s3.download_file(parsed.netloc, parsed.path.lstrip("/"), dest)
        elif parsed.scheme in ("http", "https"):
            with requests.get(path, stream=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
        elif os.path.exists(path):
            shutil.copy2(path, dest)
        else:
            raise ValueError(f"Unsupported or missing path: {path}")

        print(f"[Downloader] File ready at {dest}")
        return {"file_path": dest, "job_id": job_id, "prompts": prompts}
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


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


@app.task(name="tasks.process_visual")
def process_visual(payload):
    # file_path: /app/tmp/{task_id}/{filename}
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]
    file_name = os.path.basename(file_path)
    visual_result = None

    start_service("visualservice", max_retries=1)
    try:
        api_key = ensure_api_key()
        if not api_key:
            raise RuntimeError("Failed to obtain API key for visual service")

        print(f"[Visual Worker] Starting Task: {file_path}")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        headers = {"X-API-Key": api_key}
        analyze_url = visual_api_url + "/analyze"
        # TODO: pass prompts to the service
        with open(file_path, "rb") as f:
            response = requests.post(
                analyze_url, headers=headers, files={"video": f}, timeout=6000
            )

        response.raise_for_status()
        visual_result = extract_flat_captions(response.text)

        file_name_no_ext = os.path.splitext(file_name)[0]
        save_to_disk(job_id, f"{file_name_no_ext}_visual_output.json", visual_result)
        print(f"[Visual Worker] Success: {len(visual_result)} segments.")

    except Exception as e:
        print(f"[Visual Worker] Error: {str(e)}")
    finally:
        stop_service("visualservice")

    # pass visual chunks forward so process_audio can use them for chunk splitting
    return {**payload, "visual_result": visual_result}


@app.task(name="tasks.process_audio")
def process_audio(payload):  # change filepath to dict inputs
    """
    payload = {
        file_path:
        job_id:
        prompts:
    }
    """
    result_template = {
        "type": "audio",
        "success": False,
        "video_name": None,
        "output": None,
        "error": None,
    }

    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]
    file_name = os.path.basename(file_path)

    start_service("audioservice", max_retries=1)
    try:
        print(f"[Audio Worker] Starting Task: {file_path}")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Physical file check failed: {file_path}")

        audio_payload = {
            "video_path": f"{job_id}/{file_name}",
            "prompts": payload["prompts"],
            "chunks": payload.get(
                "visual_result"
            ),  # visual segment boundaries for chunk splitting
        }

        response = requests.post(audio_api_url, json=audio_payload, timeout=1800)

        if response.status_code != 200:
            try:
                err_detail = response.json().get("detail", response.text)
            except:
                err_detail = response.text
            raise Exception(
                f"Audio Service Error ({response.status_code}): {err_detail}"
            )

        service_data = response.json()

        outputs = []
        for entry in service_data.get("output", []):
            item = {
                "start": entry["start"].split(",")[0],
                "end": entry["end"].split(",")[0],
                "caption": entry["caption"],
            }
            outputs.append(item)

        result_template.update(
            {"success": True, "output": outputs, "video_name": file_name}
        )
        print(
            f"[Audio Worker] Success: Received {len(result_template['output'])} items."
        )

        file_name_no_ext = os.path.splitext(file_name)[0]
        save_to_disk(job_id, f"{file_name_no_ext}_audio_output.json", outputs)

    except Exception as e:
        print(f"[Audio Worker] Error: {str(e)}")
        result_template["error"] = str(e)
    finally:
        stop_service("audioservice")
    return result_template


def load_json_file(file_path):
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


@app.task(name="tasks.finalize_results")
def finalize_results(job_id, job_type="full", callback_url=None):

    workspace = os.path.join(shared_path, job_id)

    audio_files = glob.glob(os.path.join(workspace, "*_audio_output.json"))
    visual_files = glob.glob(os.path.join(workspace, "*_visual_output.json"))
    summarise_files = glob.glob(os.path.join(workspace, "*_summarise_output.json"))

    audio_data = load_json_file(audio_files[0]) if audio_files else None
    visual_data = load_json_file(visual_files[0]) if visual_files else None
    summarise_data = load_json_file(summarise_files[0]) if summarise_files else None

    if audio_files:
        video_name = os.path.basename(audio_files[0]).replace("_audio_output.json", "")
    elif visual_files:
        video_name = os.path.basename(visual_files[0]).replace(
            "_visual_output.json", ""
        )
    elif summarise_files:
        video_name = os.path.basename(summarise_files[0]).replace(
            "_summarise_output.json", ""
        )
    else:
        video_name = None

    all_success = {
        "full": audio_data is not None and visual_data is not None,
        "audio_only": audio_data is not None,
        "visual_only": visual_data is not None,
        "summarise": summarise_data is not None,
    }.get(job_type, False)

    with open(os.path.join(workspace, "task_info.txt"), "w") as f:
        f.write(f"Job ID: {job_id}\n")
        f.write(f"Video Name: {video_name}\n")
        f.write(f"Audio Files: {audio_files}\n")
        f.write(f"Visual Files: {visual_files}\n")
        f.write(f"Summarise Files: {summarise_files}\n")
        f.write(f"Status: {'Success' if all_success else 'Partial/Failed'}\n")

    if all_success:
        # Clean up only all successful case to preserve data for debugging in failure cases
        for f in os.listdir(workspace):
            if not f.endswith(".json") and not f.endswith(".txt"):
                try:
                    os.remove(os.path.join(workspace, f))
                    print(f"[Cleanup] Removed intermediate file: {f}")
                except Exception as e:
                    print(f"[Cleanup Warning] Retained file: {e}")

    final_output = {
        "job_id": job_id,
        "video_name": video_name,
        "audio_result": audio_data,
        "visual_result": visual_data,
        "summarise_result": summarise_data,
        "status": "success" if all_success else "partial/failed", # more detailed failed 
    }

    if callback_url:
        try:
            requests.post(callback_url, json=final_output, timeout=30)
        except Exception as e:
            print(f"[Callback Warning] Failed to send callback: {str(e)}")

    if not all_success:
        raise RuntimeError(
            f"Pipeline incomplete — audio: {'ok' if audio_data else 'missing'}, "
            f"visual: {'ok' if visual_data else 'missing'}, "
            f"summarise: {'ok' if summarise_data else 'missing'}"
        )
    return final_output


@app.task(name="tasks.summarise")
def process_summarise(payload):
    job_id = payload.get("job_id")
    result_template = {
        "type": "summarise",
        "success": False,
        "output": None,
        "error": None,
    }

    try:
        start_service("transcriptservice", max_retries=1)
        print(f"[Summarise Worker] Starting Task: {job_id}")

        response = requests.post(
            transcript_api_url,
            json={
                "job_id": job_id,
                "job_type": "script",
                "prompts": payload.get("prompts"),
            },
            timeout=1800,
        )
        response.raise_for_status()

        result = response.json()
        result_template.update({"success": True, "output": result})

        file_name_no_ext = os.path.splitext(os.path.basename(payload.get("file_path")))[
            0
        ]
        save_to_disk(job_id, f"{file_name_no_ext}_summarise_output.json", result)
        print("[Summarise Worker] Success.")

    except Exception as e:
        print(f"[Summarise Worker] Error: {str(e)}")
        result_template["error"] = str(e)
    finally:
        stop_service("transcriptservice")

    return {**payload, "summarise_result": result_template}
