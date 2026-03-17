import os
import subprocess
import uuid
import boto3
from fastapi import FastAPI, BackgroundTasks

app = FastAPI()
s3_client = boto3.client('s3')

# Download Path from S3
TEMP_DIR = "/tmp/media_processing"
os.makedirs(TEMP_DIR, exist_ok=True)


def download(task_id, s3_key,s3_bucket):
    """download function

    Args:
        task_id (str): _description_
        s3_key (str): _description_
        s3_bucket (str): _description_
    """
    local_input = os.path.join(TEMP_DIR,f"{task_id}_{os.path.basename(s3_key)}")
    s3_client.download_file(s3_bucket, s3_key, local_input)

def process_task(task_id: str, s3_bucket: str, s3_key:str, algo_type: str):
    """Workflow

    download -> preprocess -> call -> post-process

    Args:
        task_id (str): _description_
        s3_bucket (str): _description_
        s3_key (str): _description_
        algo_type (str): _description_
    """
    
    download(task_id=task_id, s3_key=s3_key, s3_bucket=s3_bucket)

