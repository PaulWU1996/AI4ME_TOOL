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
    # 现在直接查询汇总任务的状态
    result = AsyncResult(job_id, app=celery_app)

    if not result:
        raise HTTPException(status_code=505, detail="Task not found")
    
    is_ready = result.ready()
    
    return {
        "job_id": job_id,
        "is_ready": is_ready,
        "status": result.status,
        # 只有 Ready 为 True 时，result.result 才是由 finalize_results 汇总好的字典
        "results": result.result if is_ready else None
    }