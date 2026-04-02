from celery import Celery
import os
import time

app = Celery('tasks', 
             broker='redis://redis:6379/0', 
             backend='redis://redis:6379/0')

@app.task(name="tasks.process_visual") # 必须与 Controller 的 name 完全一致
def process_visual(file_path):
    print(f"🎬 [Visual Worker] 收到任务，开始读取: {file_path}")
    
    # 检查文件物理存在（确保 Volume 挂载成功）
    if not os.path.exists(file_path):
        print(f"❌ 错误: 文件 {file_path} 不存在")
        return "ERROR: File Not Found"

    # 模拟调用 Service 3 的 FastAPI
    # response = requests.post("http://service3_visual:9001/predict", json={"path": file_path})
    # return response.json()

    time.sleep(2) # 模拟处理
    return f"SUCCESS: Visual processed {file_path}"

@app.task(name="tasks.process_audio") # 必须一致
def process_audio(file_path):
    print(f"🎵 [Audio Worker] 收到任务，开始读取: {file_path}")
    
    if not os.path.exists(file_path):
        return "ERROR: File Not Found"

    # 模拟调用 Service 4 的 FastAPI
    # response = requests.post("http://service4_audio:9002/predict", json={"path": file_path})
    # return response.json()

    time.sleep(2)
    return f"SUCCESS: Audio processed {file_path}"