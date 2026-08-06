from fastapi import FastAPI, HTTPException
from celery import uuid, signature
from celery.result import AsyncResult
from tasks import app as celery_app
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone

app = FastAPI()

SUPPORTED_JOB_TYPES = ["full", "audio_only", "visual_only", "summarise", "speaker-extent-summarise", "utterance-extent-summarise", "tags"]
MAX_ETA_SECONDS = 3600  # set to visibility_timeout value


class ProcessRequest(BaseModel):
    path: str
    callback_url: Optional[str] = None
    prompts: Optional[str] = None
    job_type: str = "full"
    run_at_ms: Optional[int] = None


def build_chain(request: ProcessRequest, job_id: str):
    download = signature(
        "tasks.download_file",
        args=[request.path, job_id],
        kwargs={"prompts": request.prompts},
        immutable=True,
    )
    summarise = signature("tasks.process_summarise")
    tagging = signature("tasks.process_tags")
    transcript_to_text = signature("tasks.transcript_to_text")
    finalize = signature(
        "tasks.finalize_results",
        args=[job_id],
        kwargs={"job_type": request.job_type, "callback_url": request.callback_url},
        immutable=True,
    ).set(task_id=job_id)

    chains = {
        "full": (
            download
            | signature("tasks.process_visual")
            | signature("tasks.process_audio")
            | finalize
        ),
        "audio_only": (
            download 
            | signature("tasks.process_audio") 
            | finalize
        ),
        "visual_only": (
            download 
            | signature("tasks.process_visual") 
            | finalize
        ),
        "summarise": (
            download 
            | transcript_to_text
            | summarise 
            | tagging
            | finalize
        ),
        "speaker-extent-summarise": (
            download 
            | signature("tasks.speaker_extent") 
            | transcript_to_text 
            | summarise 
            | tagging
            | finalize
        ),
        "utterance-extent-summarise": (
            download 
            | signature("tasks.segment_extent") 
            | transcript_to_text 
            | summarise 
            | tagging
            | finalize
        ),
        "tagging": (
            download 
            | transcript_to_text 
            | tagging 
            | finalize
        ),
    }
    return chains.get(request.job_type)


@app.post("/process")
async def start_pipeline(request: ProcessRequest):
    if request.job_type not in SUPPORTED_JOB_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported job_type '{request.job_type}'. Choose from: {SUPPORTED_JOB_TYPES}",
        )

    job_id = uuid()
    async_kwargs = {}

    if request.run_at_ms is not None:
        run_at_seconds = request.run_at_ms / 1000.0
        eta_dt = datetime.fromtimestamp(run_at_seconds, tz=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        diff_seconds = (eta_dt - now_dt).total_seconds()

        if diff_seconds > MAX_ETA_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"run_at_ms timestamp exceeds the maximum allowed delay of {MAX_ETA_SECONDS} seconds.",
            )

        if diff_seconds > 0:
            print(f"[Controller] Task set to run at: {eta_dt.isoformat()}")
            async_kwargs["eta"] = eta_dt

    try:
        build_chain(request, job_id).apply_async(**async_kwargs)
        return {"status": "submitted", "job_id": job_id, "job_type": request.job_type}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    result = AsyncResult(job_id, app=celery_app)

    if result.status == "PENDING" and not result.info:
        raise HTTPException(status_code=404, detail="Task not found or expired")

    response = {
        "job_id": job_id,
        "status": result.status,  # PENDING, STARTED, SUCCESS, FAILURE
        "is_ready": result.ready(),
        "data": None,
    }

    if result.ready():
        if result.successful():
            response["data"] = result.result
            response["message"] = "Task completed successfully"
        else:
            response["status"] = "FAILURE"
            response["message"] = str(result.result)

    return response
