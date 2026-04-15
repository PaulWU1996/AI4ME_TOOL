from fastapi import FastAPI, HTTPException
from tasks import process_audio, process_visual, finalize_results
from celery import group, uuid, chord
from celery.result import AsyncResult, GroupResult
from downloader import UniversalDownloader
import os
import shutil
from tasks import app as celery_app

SHARED_PATH = os.getenv("SHARED_PATH", "/app/tmp")

app = FastAPI()
downloader = UniversalDownloader(base_dir=SHARED_PATH)

@app.post("/process")
async def start_pipeline(path: str):

    job_id = uuid() 
    workspace = os.path.join(SHARED_PATH, job_id)

    try:
       
        local_standard_path = downloader.download(input_path=path, task_id=job_id)

        header = [
            process_audio.s(local_standard_path),
            process_visual.s(local_standard_path)
        ]

        callback = finalize_results.s(job_id)
        chord(header)(callback.set(task_id=job_id))
        return {
            "status": "submitted",
            "job_id": job_id,
            "workspace": workspace
        }
    except Exception as e:
        if os.path.exists(workspace): shutil.rmtree(workspace)
        raise HTTPException(status_code=500, detail=str(e))
    

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    result = AsyncResult(job_id, app=celery_app)

    # 检查任务是否存在（针对无效 ID）
    if result.status == 'PENDING' and not result.info:
         raise HTTPException(status_code=404, detail="Task not found or expired")
    
    response = {
        "job_id": job_id,
        "status": result.status, # PENDING, STARTED, SUCCESS, FAILURE
        "is_ready": result.ready(),
        "results": None
    }

    if result.ready():
        if result.successful():
            response["results"] = result.result
        else:
            # 如果任务失败（比如代码崩溃），返回错误信息
            response["results"] = {"error": str(result.result)}
            
    return response