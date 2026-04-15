from celery import Celery
import os
import time
import shutil
import requests

redis_host = os.getenv('REDIS_HOST', '127.0.0.1') # 默认用本地，允许通过环境变量修改
redis_port = os.getenv('REDIS_PORT', '6379')
audio_host = os.getenv('AUDIO_HOST',"localhost")
audio_port = os.getenv('AUDIO_PORT', '9001')
visual_host = os.getenv('VISUAL_HOST',"localhost")
visual_port = os.getenv('VISUAL_PORT', '9051')

audio_api_url = f"http://{audio_host}:{audio_port}/predict"
visual_api_url = f"http://{visual_host}:{visual_port}/predict"

app = Celery('tasks', 
             broker=f'redis://{redis_host}:{redis_port}/0', 
             backend=f'redis://{redis_host}:{redis_port}/0')

app.conf.update(
    broker_transport_options={'visibility_timeout': 3600},  # 1小时的任务可见性超时
    result_expires=86400,  # 24小时的结果过期时间
    worker_prefetch_multiplier=1,  # 每个 worker 一次只取一个任务
    task_acks_late=True,  # 任务执行完成后才发送 ack
    result_persistent=True,  # 结果持久化到 Redis
)

@app.task(name="tasks.process_visual")
def process_visual(file_path):
    result_template = {
        "type": "visual",
        "success": False,
        "output": None,
        "error": None
    }

    try:

        print(f"[Visual Worker] Task Received, Start to Load: {file_path}")
        
        if not os.path.exists(file_path):
            print(f"[Error]: File {file_path} not found.")

        # 模拟调用 Service 3 的 FastAPI
        # response = requests.post("http://service3_visual:9001/predict", json={"path": file_path})
        # return response.json()
        time.sleep(25) # 模拟处理时间

        result_template.update(
            {
                "success": True,
                "output": {}
            }
        )
    except Exception as e:
        print(f"[Error]: Exception occurred while processing visual task: {str(e)}")
        result_template.update(
            {
                "error": str(e)
            }
        )

    return result_template



@app.task(name="tasks.process_audio")
def process_audio(file_path):
    result_template = {
        "type": "audio",
        "success": False,
        "output": None,
        "error": None
    }

    try:
        print(f"[Audio Worker] Task Received, Start to Load: {file_path}")
        
        if not os.path.exists(file_path):
            return "ERROR: File Not Found"

        response = requests.post(

        )

        time.sleep(10)
        result_template.update(
            {
                "success": True,
                "output": {}
            }
        )

    except Exception as e:
        print(f"[Error]: Exception occurred while processing audio task: {str(e)}")
        result_template.update(
            {
                "error": str(e)
            }     )
    
    return result_template


@app.task(name="tasks.finalize_results")
def finalize_results(raw_results, job_id):
    """
    raw_results: [audio_dict, visual_dict] 自动注入的结果列表
    job_id: 你的文件夹 UUID
    """
    # 1. 结果分拣 (方案一：基于 type 字段)
    final_output = {
        "visual": {"success": False, "error": "Missing"},
        "audio": {"success": False, "error": "Missing"}
    }
    
    for res in raw_results:
        if isinstance(res, dict) and "type" in res:
            final_output[res["type"]] = res

    # 2. 【自动化核心】清理物理路径
    # 只要这个函数被触发，说明 AI 镜像已经处理完文件了
    workspace = os.path.join("/app/tmp", job_id)
    if os.path.exists(workspace):
        try:
            import shutil
            shutil.rmtree(workspace)
            print(f"🧹 [Auto-Cleanup] Job {job_id} workspace removed.")
        except Exception as e:
            print(f"⚠️ [Cleanup Error] {e}")

    # 3. 返回最终合并后的数据，供 Controller 接口读取
    return final_output