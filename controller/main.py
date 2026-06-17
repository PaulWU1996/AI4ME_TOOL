from fastapi import FastAPI, HTTPException
from tasks import download_file, process_audio, process_visual, finalize_results, process_moment
from celery import uuid
from celery.result import AsyncResult
from tasks import app as celery_app
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

SUPPORTED_JOB_TYPES = ["full", "audio_only", "visual_only", "moments"]

class ProcessRequest(BaseModel):
    path: str
    callback_url: Optional[str] = None
    prompts: Optional[str] = None
    job_type: str = "full"

def build_chain(request: ProcessRequest, job_id: str):
    download = download_file.si(request.path, job_id, prompts=request.prompts)
    finalize = finalize_results.si(job_id, callback_url=request.callback_url).set(task_id=job_id)

    chains = {
        "full": (
            download | process_visual.s() | process_audio.s() | finalize
        ),
        "audio_only": (
            download | process_audio.s() | finalize
        ),
        "visual_only": (
            download | process_visual.s() | finalize
        ),
        "moments": (
            download | process_moment.s() | finalize
        ),
    }
    return chains.get(request.job_type)

@app.post("/process")
async def start_pipeline(request: ProcessRequest):
    if request.job_type not in SUPPORTED_JOB_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported job_type '{request.job_type}'. Choose from: {SUPPORTED_JOB_TYPES}")

    job_id = uuid()

    try:
        build_chain(request, job_id).apply_async()
        return {"status": "submitted", "job_id": job_id, "job_type": request.job_type}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    result = AsyncResult(job_id, app=celery_app)

    if result.status == 'PENDING' and not result.info:
         raise HTTPException(status_code=404, detail="Task not found or expired")
    
    response = {
        "job_id": job_id,
        "status": result.status, # PENDING, STARTED, SUCCESS, FAILURE
        "is_ready": result.ready(),
        "data": None
    }

    if result.ready():
        if result.successful():
            response["data"] = result.result
            response["message"] = "Task completed successfully"
        else:
            response["status"] = "FAILURE"
            response["message"] = str(result.result) 
            
    return response