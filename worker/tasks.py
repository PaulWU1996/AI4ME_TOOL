from celery import Celery
import os
import time

# 连接到 Redis
app = Celery('tasks', 
             broker='redis://redis:6379/0', 
             backend='redis://redis:6379/0')

@app.task(name="tasks.process_visual")
def process_visual(file_path):
    print(f"🎬 [Visual Worker] 正在读取: {file_path}")
    if os.path.exists(file_path):
        # 模拟处理耗时
        time.sleep(2)
        return f"SUCCESS: Visual processed {os.path.basename(file_path)}"
    return f"FAILED: {file_path} not found"

@app.task(name="tasks.process_audio")
def process_audio(file_path):
    print(f"🎵 [Audio Worker] 正在读取: {file_path}")
    if os.path.exists(file_path):
        time.sleep(2)
        return f"SUCCESS: Audio processed {os.path.basename(file_path)}"
    return f"FAILED: {file_path} not found"