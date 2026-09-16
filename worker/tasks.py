import os
import shutil
import requests
from celery import Celery
from urllib.parse import urlparse, parse_qs
import glob
from consts import (
    redis_host,
    redis_port,
    shared_path,
    visual_api_url,
    audio_api_url,
    summarise_api_url,
    tagging_api_url,
    transcript_text_file,
    VISUAL_REQUEST_TIMEOUT,
    AUDIO_REQUEST_TIMEOUT,
    SCRIPT_REQUEST_TIMEOUT,
)
from utils import (
    ensure_api_key,
    extract_flat_captions,
    save_to_disk,
    get_speaker_turn_boundary_ms,
    load_json_file
)
# Service lifecycle goes through the lease in dag/readiness.py rather than
# calling utils.start_service/stop_service directly. The lease is
# reference-counted and re-entrant, so when a task runs as a DAG node whose
# workflow also declares `service`, the engine's bracket and this one nest
# into a single start/stop instead of cycling the container twice.
from dag.engine import DAGEngine
from dag.parser import Parser
from dag.readiness import ensure_ready, release

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
            query_params = parse_qs(parsed.query)
            format_param = query_params.get("format", [None])[0]
            if format_param == "json":
                filename = f"{filename}.json"
            elif format_param == "text":
                filename = f"{filename}.txt"
            else:
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


@app.task(name="tasks.process_visual")
def process_visual(payload):
    # file_path: /app/tmp/{task_id}/{filename}
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]
    file_name = os.path.basename(file_path)
    visual_result = None

    ensure_ready("visualservice")
    try:
        api_key = ensure_api_key()
        if not api_key:
            raise RuntimeError("Failed to obtain API key for visual service")

        print(f"[Visual Worker] Starting Task: {file_path}")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        analyze_url = visual_api_url + "/analyze"

        def post_analyze(key):
            # TODO: pass prompts to the service
            with open(file_path, "rb") as f:
                return requests.post(
                    analyze_url,
                    headers={"X-API-Key": key},
                    files={"video": f},
                    timeout=VISUAL_REQUEST_TIMEOUT,
                )

        response = post_analyze(api_key)

        if response.status_code in (401, 403):
            # The cached key is stale -- the service has forgotten or rotated
            # it. Without this the worker would present the same dead key on
            # every future job, and visual analysis would never recover.
            print(f"[Visual Worker] API key rejected ({response.status_code}); regenerating.")
            api_key = ensure_api_key(force=True)
            if not api_key:
                raise RuntimeError("Failed to regenerate API key for visual service")
            response = post_analyze(api_key)

        response.raise_for_status()
        visual_result = extract_flat_captions(response.text)

        file_name_no_ext = os.path.splitext(file_name)[0]
        save_to_disk(job_id, f"{file_name_no_ext}_visual_output.json", visual_result)
        print(f"[Visual Worker] Success: {len(visual_result)} segments.")
    finally:
        release("visualservice")

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
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]
    file_name = os.path.basename(file_path)

    ensure_ready("audioservice")
    try:
        print(f"[Audio Worker] Starting Task: {file_path}")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Physical file check failed: {file_path}")

        audio_payload = {
            "video_path": f"{job_id}/{file_name}",
            "prompts": payload["prompts"],
            "chunks": payload.get("visual_result"),  # visual segment boundaries for chunk splitting
        }

        response = requests.post(audio_api_url, json=audio_payload, timeout=AUDIO_REQUEST_TIMEOUT)

        if response.status_code != 200:
            try:
                err_detail = response.json().get("detail", response.text)
            except:
                err_detail = response.text
            raise Exception(f"Audio Service Error ({response.status_code}): {err_detail}")

        service_data = response.json()

        outputs = []
        for entry in service_data.get("output", []):
            item = {
                "start": entry["start"].split(",")[0],
                "end": entry["end"].split(",")[0],
                "caption": entry["caption"],
            }
            outputs.append(item)

        print(f"[Audio Worker] Success: Received {len(outputs)} items.")

        file_name_no_ext = os.path.splitext(file_name)[0]
        save_to_disk(job_id, f"{file_name_no_ext}_audio_output.json", outputs)
    finally:
        release("audioservice")

    return {
        **payload,
        "type": "audio",
        "success": True,
        "video_name": file_name,
        "output": outputs,
        "error": None,
    }





@app.task(name="tasks.finalize_results")
def finalize_results(job_id, job_type="full", callback_url=None, expects=None):
    """Merge a job's outputs, write task_info.txt, and report success.

    `expects` names which results must be present for the job to count as a
    success, e.g. ["audio", "visual"]. A DAG workflow declares it on the
    finalize node:

        {"id": "final", "task": "finalize_results",
         "kwargs": {"expects": ["audio", "visual"]}, "depends_on": [...]}

    When it is omitted, the legacy per-job_type table below is used, so the
    hardcoded chains in controller/main.py keep their exact semantics. A
    workflow registered under a name that is not one of those legacy job
    types must declare `expects` — otherwise there is no way to know what
    "done" means for it, and the job used to fail at the final node even
    though every other node had succeeded.
    """

    workspace = os.path.join(shared_path, job_id)

    audio_files = glob.glob(os.path.join(workspace, "*_audio_output.json"))
    visual_files = glob.glob(os.path.join(workspace, "*_visual_output.json"))
    summarise_files = glob.glob(os.path.join(workspace, "*_summarise_output.json"))
    extent_files = glob.glob(os.path.join(workspace, "*_extent_output.json"))
    tagging_files = glob.glob(os.path.join(workspace, "*_tagging_output.json"))

    audio_data = load_json_file(audio_files[0]) if audio_files else None
    visual_data = load_json_file(visual_files[0]) if visual_files else None
    summarise_data = load_json_file(summarise_files[0]) if summarise_files else None
    extent_data = load_json_file(extent_files[0]) if extent_files else None
    tagging_data = load_json_file(tagging_files[0]) if tagging_files else None

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
    else:
        file_name = None

    produced = {
        "audio": audio_data,
        "visual": visual_data,
        "summarise": summarise_data,
        "extent": extent_data,
        "tagging": tagging_data,
    }

    LEGACY_EXPECTATIONS = {
        "full": ["audio", "visual"],
        "audio_only": ["audio"],
        "visual_only": ["visual"],
        "summarise": ["summarise", "tagging"],
        "speaker-extent-summarise": ["extent", "summarise", "tagging"],
        "utterance-extent-summarise": ["extent", "summarise", "tagging"],
        "tagging": ["tagging"],
    }

    if expects is None:
        expects = LEGACY_EXPECTATIONS.get(job_type)
    if expects is None:
        raise ValueError(
            f"job_type '{job_type}' has no built-in success criteria. Declare "
            f"kwargs.expects on the finalize node of its workflow, e.g. "
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
        "status": "success" if job_success else "failed",
    }

    if callback_url:
        try:
            requests.post(callback_url, json=final_output, timeout=30)
            print(f"[Callback]: final_output sent to: {callback_url}")
        except Exception as e:
            print(f"[Callback Warning] Failed to send callback: {str(e)}")

    if not job_success:
        raise RuntimeError(
            f"{job_type} failed: expected {expects}, missing {missing}."
        )

    return final_output



def run_service_task(
    payload: dict,
    task_type: str,
    service_name: str,
    log_tag: str,
    file_suffix: str,
    result_key: str,
    api_url: str,
) -> dict:
    """Helper function to execute script processing tasks with shared service

    management, HTTP requesting, and output saving.
    """
    job_id = payload.get("job_id")
    result_template = {
        "type": task_type,
        "success": False,
        "output": None,
        "error": None,
    }

    try:
        ensure_ready(service_name)
        print(f"{log_tag} Starting Task: {job_id}")

        response = requests.post(
            api_url,
            json={
                "job_id": job_id,
                "job_type": "script",
                "prompts": payload.get("prompts"),
            },
            timeout=SCRIPT_REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        result = response.json()
        result_template.update({"success": True, "output": result})

        file_path = payload.get("file_path", "")
        file_name_no_ext = os.path.splitext(os.path.basename(file_path))[0]
        save_to_disk(job_id, f"{file_name_no_ext}_{file_suffix}.json", result)
        print(f"{log_tag} Success.")

    except Exception as e:
        print(f"{log_tag} Error: {str(e)}")
        result_template["error"] = str(e)
    finally:
        release(service_name)

    return {**payload, result_key: result_template}


@app.task(name="tasks.process_summarise")
def process_summarise(payload):
    return run_service_task(
        payload=payload,
        task_type="summarise",
        service_name="transcriptservice",
        log_tag="[Summarise Worker]",
        file_suffix="summarise_output",
        result_key="summarise_result",
        api_url=summarise_api_url
    )


@app.task(name="tasks.process_tags")
def process_tags(payload):
    return run_service_task(
        payload=payload,
        task_type="tags",
        service_name="taggingservice",
        log_tag="[Tagging Worker]",
        file_suffix="tagging_output",
        result_key="tagging_result",
        api_url=tagging_api_url
    )


@app.task(name="tasks.speaker_extent")
def speaker_extent(payload):
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]

    transcript = load_json_file(file_path)

    segments = transcript.get("segments", [])
    if not segments:
        raise ValueError("No segments found in transcript")

    start_speaker_ms = get_speaker_turn_boundary_ms(segments, 0, "forward")
    end_speaker_ms = get_speaker_turn_boundary_ms(segments, len(segments) - 1, "backward")

    if start_speaker_ms > end_speaker_ms:
        start_speaker_ms = segments[0]["startMs"]
        end_speaker_ms = segments[len(segments) - 1]["endMs"]

    transcript["segments"] = [
        seg
        for seg in segments
        if seg["endMs"] > start_speaker_ms and seg["startMs"] < end_speaker_ms
    ]

    file_name = os.path.basename(file_path)
    file_name_no_ext = os.path.splitext(file_name)[0]
    trimmed_file_name = f"{file_name_no_ext}_trimmed.json"
    save_to_disk(job_id, trimmed_file_name, transcript)

    extent_result = {"start": start_speaker_ms, "end": end_speaker_ms}
    save_to_disk(job_id, f"{file_name_no_ext}_extent_output.json", extent_result)

    trimmed_file_path = os.path.join(os.path.dirname(file_path), trimmed_file_name)
    print(f"[Speaker Extent] Trimmed to speaker range: {start_speaker_ms}ms - {end_speaker_ms}ms")
    return {
        **payload,
        "file_path": trimmed_file_path,
        "start": start_speaker_ms,
        "end": end_speaker_ms,
    }


@app.task(name="tasks.segment_extent")
def segment_extent(payload):
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]

    transcript = load_json_file(file_path)

    segments = transcript.get("segments", [])
    first = segments[0]
    last = segments[len(segments) - 1]
    start_ms = first.get("startMs", 0)
    end_ms = last.get("endMs", 0)

    if start_ms > end_ms:
        start_ms = end_ms

    file_name = os.path.basename(file_path)
    file_name_no_ext = os.path.splitext(file_name)[0]

    extent_result = {"start": start_ms, "end": end_ms}
    save_to_disk(job_id, f"{file_name_no_ext}_extent_output.json", extent_result)

    print(f"[Segment Extent] Extracted range: {start_ms}ms - {end_ms}ms")
    return {**payload, "start": start_ms, "end": end_ms}


@app.task(name="tasks.transcript_to_text")
def transcript_to_text(payload):
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]

    transcript = load_json_file(file_path)
    segments = transcript.get("segments", [])

    text = " ".join(
        seg.get("text", "").strip() 
        for seg in segments 
        if seg.get("text", "").strip()
    )

    save_to_disk(job_id, transcript_text_file, text)

    txt_path = os.path.join(os.path.dirname(file_path), transcript_text_file)
    print(f"[Transcript to Text] Saved text to {txt_path}")
    return {**payload, "file_path": txt_path}


@app.task(name="tasks.execute_workflow", bind=True)
def execute_workflow(self, workflow_path, job_id, path, prompts=None, job_type="full", callback_url=None):
    """Entry point for DAG-based jobs: parses a workflow JSON template into
    a DAG and runs it as a single Celery job, feeding this request's
    `path`/`prompts` into the DAG as runtime input (job_inputs) rather than
    baking them into the template.
    """
    parser = Parser(workflow_path)
    engine = DAGEngine(
        parser.dag,
        job_id=job_id,
        job_type=job_type,
        callback_url=callback_url,
        job_inputs={"path": path, "prompts": prompts},
        on_failure=parser.settings.get("on_failure", "stop"),
        # Node-level retry. The legacy chains get this from
        # @app.task(autoretry_for=...), which does not engage on the DAG path
        # because the python driver calls the task's function directly — and
        # a Celery-level retry of execute_workflow would re-run the whole DAG
        # rather than the one node that failed.
        retries=parser.settings.get("retries", 0),
        retry_backoff=parser.settings.get("retry_backoff", 1.0),
        retry_backoff_max=parser.settings.get("retry_backoff_max", 60.0),
    )
    # Sequential by default. execute_parallel() is now safe as far as
    # service occupancy goes — dag/readiness.py leases refcount holders and
    # cap concurrency per service — but enabling it still needs an aggregate
    # resource-feasibility check, since a lease stops one service being
    # doubly occupied without stopping two *different* GPU services being
    # jointly resident beyond host VRAM.
    engine.execute()

    # Per-node envelopes go to the workspace rather than into the task
    # result, so /status returns the same shape for DAG jobs as it does for
    # the legacy chains (finalize's merged output) without losing the
    # node-level detail that makes a failed run diagnosable.
    try:
        save_to_disk(job_id, "dag_run.json", engine.run_summary())
    except Exception as e:
        print(f"[DAG] Could not write dag_run.json: {e}")

    return engine.terminal_result()
