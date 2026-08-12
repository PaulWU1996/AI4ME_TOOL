import os
import shutil
import requests
import json
from celery import Celery
from urllib.parse import urlparse, parse_qs
import glob
from consts import (
    redis_host,
    redis_port,
    shared_path,
    visual_api_url,
    audio_api_url,
    transcript_api_url,
)
from utils import (
    start_service,
    ensure_api_key,
    extract_flat_captions,
    stop_service,
    save_to_disk,
    get_speaker_turn_boundary_ms,
    load_json_file
)

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
            response = requests.post(analyze_url, headers=headers, files={"video": f}, timeout=6000)

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
            "chunks": payload.get("visual_result"),  # visual segment boundaries for chunk splitting
        }

        response = requests.post(audio_api_url, json=audio_payload, timeout=1800)

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

        result_template.update({"success": True, "output": outputs, "video_name": file_name})
        print(f"[Audio Worker] Success: Received {len(result_template['output'])} items.")

        file_name_no_ext = os.path.splitext(file_name)[0]
        save_to_disk(job_id, f"{file_name_no_ext}_audio_output.json", outputs)

    except Exception as e:
        print(f"[Audio Worker] Error: {str(e)}")
        result_template["error"] = str(e)
    finally:
        stop_service("audioservice")
    return result_template

@app.task(name="tasks.finalize_results")
def finalize_results(job_id, job_type="full", callback_url=None):

    workspace = os.path.join(shared_path, job_id)

    audio_files = glob.glob(os.path.join(workspace, "*_audio_output.json"))
    visual_files = glob.glob(os.path.join(workspace, "*_visual_output.json"))
    summarise_files = glob.glob(os.path.join(workspace, "*_summarise_output.json"))
    extent_files = glob.glob(os.path.join(workspace, "*_extent_output.json"))

    audio_data = load_json_file(audio_files[0]) if audio_files else None
    visual_data = load_json_file(visual_files[0]) if visual_files else None
    summarise_data = load_json_file(summarise_files[0]) if summarise_files else None
    extent_data = load_json_file(extent_files[0]) if extent_files else None

    if audio_files:
        video_name = os.path.basename(audio_files[0]).replace("_audio_output.json", "")
    elif visual_files:
        video_name = os.path.basename(visual_files[0]).replace("_visual_output.json", "")
    elif summarise_files:
        video_name = os.path.basename(summarise_files[0]).replace("_summarise_output.json", "")
    elif extent_files:
        video_name = os.path.basename(extent_files[0]).replace("_extent_output.json", "")
    else:
        video_name = None

    job_success = {
        "full": audio_data is not None and visual_data is not None,
        "audio_only": audio_data is not None,
        "visual_only": visual_data is not None,
        "summarise": summarise_data is not None,
        "speaker-extent-summarise": extent_data is not None and summarise_data is not None,
        "utterance-extent-summarise": extent_data is not None and summarise_data is not None,
    }.get(job_type, False)

    with open(os.path.join(workspace, "task_info.txt"), "w") as f:
        f.write(f"Job ID: {job_id}\n")
        f.write(f"Video Name: {video_name}\n")
        f.write(f"Audio Files: {audio_files}\n")
        f.write(f"Visual Files: {visual_files}\n")
        f.write(f"Summarise Files: {summarise_files}\n")
        f.write(f"Extent Files: {extent_files}\n")
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
        "video_name": video_name,
        "audio_result": audio_data,
        "visual_result": visual_data,
        "summarise_result": summarise_data,
        "extent_result": extent_data,
        "status": "success" if job_success else "failed",
    }

    if callback_url:
        try:
            requests.post(callback_url, json=final_output, timeout=30)
            print(f"[Callback]: final_output sent to: {callback_url}")
        except Exception as e:
            print(f"[Callback Warning] Failed to send callback: {str(e)}")

    if not job_success:
        raise RuntimeError(f"{job_type} failed: {summarise_data}.")

    return final_output


@app.task(name="tasks.process_summarise")
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

        file_name_no_ext = os.path.splitext(os.path.basename(payload.get("file_path")))[0]
        save_to_disk(job_id, f"{file_name_no_ext}_summarise_output.json", result)
        print("[Summarise Worker] Success.")

    except Exception as e:
        print(f"[Summarise Worker] Error: {str(e)}")
        result_template["error"] = str(e)
    finally:
        stop_service("transcriptservice")

    return {**payload, "summarise_result": result_template}


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

    transcript = load_json_file(file_path)
    segments = transcript.get("segments", [])

    text = " ".join(
        seg.get("text", "").strip() 
        for seg in segments 
        if seg.get("text", "").strip()
    )

    txt_path = os.path.splitext(file_path)[0] + ".txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"[Transcript to Text] Saved text to {txt_path}")
    return {**payload, "file_path": txt_path}
