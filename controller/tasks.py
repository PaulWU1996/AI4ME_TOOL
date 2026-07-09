from celery import Celery

import os

redis_host = os.getenv('REDIS_HOST', '127.0.0.1') # 默认用本地，允许通过环境变量修改
redis_port = os.getenv('REDIS_PORT', '6379')

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

@app.task(name="tasks.download_file")
def download_file(path, job_id, prompts=None):
    pass

@app.task(name="tasks.process_audio")
def process_audio(payload):
    pass

@app.task(name="tasks.process_visual")
def process_visual(payload):
    pass

@app.task(name="tasks.finalize_results")
def finalize_results(job_id, callback_url=None):
    pass

@app.task(name="tasks.process_summarise")
def process_summarise(payload):
    pass