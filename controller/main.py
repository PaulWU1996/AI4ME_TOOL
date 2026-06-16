from fastapi import FastAPI, HTTPException
from tasks import download_file, process_audio, process_visual, finalize_results
from celery import uuid
from celery.result import AsyncResult
from tasks import app as celery_app
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

class ProcessRequest(BaseModel):
    path: str
    callback_url: Optional[str] = None
    prompts: Optional[str] = None

@app.post("/process")
async def start_pipeline(request: ProcessRequest):

    job_id = uuid()

    try:
        (
            download_file.si(request.path, job_id, prompts=request.prompts) |
            process_visual.s() |
            process_audio.s() |
            finalize_results.si(job_id, callback_url=request.callback_url).set(task_id=job_id)
        ).apply_async()

        return {"status": "submitted", "job_id": job_id}

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