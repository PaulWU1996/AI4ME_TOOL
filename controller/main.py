from fastapi import FastAPI, HTTPException
from tasks import process_audio, process_visual
from celery import group, uuid
from downloader import UniversalDownloader
import os
import shutil

SHARED_PATH = os.getenv("SHARED_PATH", "/app/tmp")

app = FastAPI()
downloader = UniversalDownloader(base_dir=SHARED_PATH)

@app.post("/process")
async def start_pipeline(path: str):
    task_id = uuid()
    workspace = os.path.join(SHARED_PATH, task_id)

    try:

        local_standard_path = downloader.download(input_path=path, task_id=task_id)

        job = group(
            process_audio.s(local_standard_path),
            process_visual.s(local_standard_path)
        )

        result = job.apply_async(task_id=task_id)

        return {
            "status": "submitted",
            "task_id": task_id,
            "workspace": workspace,
            "file_path": local_standard_path
        }

    except Exception as e:
        if os.path.exists(workspace):
            shutil.rmtree(workspace)
        raise HTTPException(status_code=500, detail=f"Pipeline Error: {str(e)}")