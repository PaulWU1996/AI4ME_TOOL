import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from celery import Celery
from consts import (
    python_call_timeout,
    python_script_root,
    redis_host,
    redis_port,
    shared_path,
)
from utils import (
    load_json_file,
    save_to_shared_disk,
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


# Extensions for extensionless URLs, keyed by the `?format=` query param,
# then by the bare filename as a fallback.
_FORMAT_EXTENSIONS = {"json": "json", "text": "txt"}
_NAME_EXTENSIONS = {"transcript": "txt", "audio": "wav"}

def _resolve_filename(parsed):
    if parsed.scheme == "dash":
        return f"{parsed.netloc}.wav"
    filename = os.path.basename(parsed.path)
    if os.path.splitext(filename)[1]:
        return filename
    format_param = parse_qs(parsed.query).get("format", [None])[0]
    ext = _FORMAT_EXTENSIONS.get(format_param) or _NAME_EXTENSIONS.get(filename)
    return f"{filename}.{ext}" if ext else filename


def _fetch(path, parsed, dest):
    if parsed.scheme == "s3":
        import boto3

        boto3.client("s3").download_file(parsed.netloc, parsed.path.lstrip("/"), dest)
    elif parsed.scheme in ("http", "https"):
        with requests.get(path, stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.writelines(r.iter_content(8192))
    elif os.path.exists(path):
        shutil.copy2(path, dest)
    else:
        raise ValueError(f"Unsupported or missing path: {path}")


@app.task(name="tasks.download_file", bind=True, autoretry_for=(Exception,),
          max_retries=3, retry_backoff=1.0, retry_backoff_max=60.0)
def download_file(self, path, job_id, prompts=None):
    output_dir = os.path.join(shared_path, job_id)
    os.makedirs(output_dir, exist_ok=True)
    try:
        parsed = urlparse(path)
        filename = _resolve_filename(parsed)
        dest = os.path.join(output_dir, filename)
        _fetch(path, parsed, dest)
    except Exception as e:
        shutil.rmtree(output_dir, ignore_errors=True)
        print("download_file failed: ", e)

    print(f"[FileDownloader] File ready at {dest}")
    return {
        "file_path": dest,
        "file_name": filename,
        "video_path": f"{job_id}/{filename}",  # update down stream services to use file path instead
        "shared_path": shared_path,
        "job_id": job_id,
        "prompts": prompts,
    }

@app.task(
    name="tasks.media_selector",
    bind=True,
    autoretry_for=(Exception,),
    max_retries=3,
    retry_backoff=1.0,
    retry_backoff_max=60.0,
)
def media_selector(
    job_id: str,
    programme_id: str,
    start_ms: int | None,
    duration_ms: int | None,
    prompts=None,
):
    filename = f'{programme_id}.wav'
    output_dir = os.path.join(shared_path, job_id)
    dest = os.path.join(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)
    from .stream_decoder import fetch_dash_stream_audio

    try:
        fetch_dash_stream_audio(
            programme_id, 
            start_ms or 0, 
            duration_ms or sys.maxsize, 
            output_dir
        )
    except Exception as e:
        print("media_selector failed: ", e)

    print(f"[DashDownloader] File ready at {dest}")
    return {
        "file_path": dest,
        "file_name": filename,
        "video_path": f"{job_id}/{filename}",  # update down stream services to use file path instead
        "shared_path": shared_path,
        "job_id": job_id,
        "prompts": prompts,
        "storage_id": programme_id,
    }

@app.task(name="tasks.http_call")
def http_call(payload, url, method="POST", headers=None, timeout=60, file_field=None,
              file_path_key="file_path", service=None, body=None, merge=False, save=None):
    """Generic service call backing `driver: "http"` workflow nodes.

    The predecessor result arrives positionally as `payload`. With
    `file_field` set, the file at `payload[file_path_key]` is uploaded as
    multipart/form-data; otherwise the payload is sent as a JSON body with
    the optional static `body` dict merged on top (e.g. to pin a per-node
    `job_type`).
    Failures raise, so a failed node aborts the chain.

    `save` persists the parsed response to disk as
    `{basename(file_path)}_{save}.json` (e.g. "summarise_output"), and
    `merge` returns `{**payload, **response}` so a later node in the chain
    keeps `job_id`/`prompts`/`file_path` from the workflow context.
    """
    job_id = payload.get("job_id", "unknown_job")

    if file_field:
        local_path = payload.get(file_path_key)
        if not local_path:
            raise ValueError(
                f"http_call: no '{file_path_key}' in payload to upload as '{file_field}'."
            )
        with open(local_path, "rb") as f:
            response = requests.request(
                method, url,
                files={file_field: (os.path.basename(local_path), f)},
                headers=headers or {}, timeout=timeout,
            )
    else:
        request_body = {**payload, **(body or {})}
        response = requests.request(
            method, url, json=request_body, headers=headers or {}, timeout=timeout,
        )
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError:
        return response.text

    # TODO: remove this and encapsulate in task
    saved_path = None
    if save:
        filename = f"{job_id}_{save}.json"
        save_to_shared_disk(job_id, filename, result)
        saved_path = os.path.join(shared_path, job_id, filename)

    output = {**payload, **result} if merge else result
    if save:
        output["save_path"] = saved_path
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

# remove this and instead each stage manages storage internally or with an optional callback_url (to push status) and internal storage call
@app.task(name="tasks.finalize_results")
def finalize_results(job_id, job_type="full", callback_url=None, expects=None):
    """Merge a job's outputs, write task_info.txt, and report success.

    `expects` is required and names which results must be present for the
    job to count as a success, e.g. ["audio", "visual"]. A DAG workflow
    declares it on the finalize node:

        {"id": "final", "task": "finalize_results",
         "kwargs": {"expects": ["audio", "visual"]}, "depends_on": [...]}

    Without `expects` there is no way to know what "done" means for a
    workflow, so the job fails at the final node even though every other
    node had succeeded.
    """

    workspace = os.path.join(shared_path, job_id)
    gemma_labels = ("visual", "audio", "transcript")
    audio_files = glob.glob(os.path.join(workspace, "*_audio_output.json"))
    visual_files = glob.glob(os.path.join(workspace, "*_visual_output.json"))
    summarise_files = glob.glob(os.path.join(workspace, "*_summarise_output.json"))
    extent_files = glob.glob(os.path.join(workspace, "*_extent_output.json"))
    tagging_files = glob.glob(os.path.join(workspace, "*_tagging_output.json"))
    gemma_files = {
        label: glob.glob(os.path.join(workspace, f"*_gemma_{label}_output.json"))
        for label in gemma_labels
    }

    audio_data = load_json_file(audio_files[0]) if audio_files else None
    visual_data = load_json_file(visual_files[0]) if visual_files else None
    summarise_data = load_json_file(summarise_files[0]) if summarise_files else None
    extent_data = load_json_file(extent_files[0]) if extent_files else None
    tagging_data = load_json_file(tagging_files[0]) if tagging_files else None

    gemma_data = {
        label: load_json_file(files[0])
        for label, files in gemma_files.items()
        if files
    }

    if audio_files:
        file_name = os.path.basename(audio_files[0]).replace("_audio_output.json", "")
    elif visual_files:
        file_name = os.path.basename(visual_files[0]).replace("_visual_output.json", "")
    elif summarise_files:
        file_name = os.path.basename(summarise_files[0]).replace("_summarise_output.json", "")
    elif extent_files:
        file_name = os.path.basename(extent_files[0]).replace("_extent_output.json", "")
    elif tagging_files:
        file_name = os.path.basename(tagging_files[0]).replace("_tagging_output.json", "")
    elif gemma_files["visual"]:
        file_name = os.path.basename(gemma_files["visual"][0]).replace("_gemma_visual_output.json", "")
    elif gemma_files["audio"]:
        file_name = os.path.basename(gemma_files["audio"][0]).replace("_gemma_audio_output.json", "")
    elif gemma_files["transcript"]:
        file_name = os.path.basename(gemma_files["transcript"][0]).replace("_gemma_transcript_output.json", "")
    else:
        file_name = None

    produced = {
        "audio": audio_data,
        "visual": visual_data,
        "summarise": summarise_data,
        "extent": extent_data,
        "tagging": tagging_data,
        "gemma": gemma_data if len(gemma_data) == len(gemma_labels) else None,
    }

    if expects is None:
        raise ValueError(
            f"job_type '{job_type}' has no 'expects' — the workflow's finalize node "
            f"must declare kwargs.expects, e.g. "
            f'"kwargs": {{"expects": {sorted(produced)}}}.'
        )

    unknown = [name for name in expects if name not in produced]
    if unknown:
        raise ValueError(
            f"finalize_results: unknown expects entries {unknown}; "
            f"choose from {sorted(produced)}."
        )

    missing = [name for name in expects if produced[name] is None]
    job_success = not missing

    with open(os.path.join(workspace, "task_info.txt"), "w") as f:
        f.write(f"Job ID: {job_id}\n")
        f.write(f"Video Name: {file_name}\n")
        f.write(f"Audio Files: {audio_files}\n")
        f.write(f"Visual Files: {visual_files}\n")
        f.write(f"Summarise Files: {summarise_files}\n")
        f.write(f"Extent Files: {extent_files}\n")
        f.write(f"Tagging Files: {tagging_files}\n")
        f.write(f"Gemma Files: {gemma_files}\n")
        f.write(f"Expects: {expects}\n")
        f.write(f"Missing: {missing}\n")
        f.write(f"Status: {'Success' if job_success else 'Partial/Failed'}\n")

    if job_success:
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
        "video_name": file_name,
        "audio_result": audio_data,
        "visual_result": visual_data,
        "summarise_result": summarise_data,
        "extent_result": extent_data,
        "tagging_result": tagging_data,
        "gemma_result": gemma_data,
        "status": "success" if job_success else "failed",
    }

    if callback_url:
        try:
            requests.post(callback_url, json=final_output, timeout=30)
            print(f"[Callback]: final_output sent to: {callback_url}")
        except Exception as e:
            print(f"[Callback Warning] Failed to send callback: {e!s}")

    if not job_success:
        raise RuntimeError(
            f"{job_type} failed: expected {expects}, missing {missing}."
        )

    return final_output
