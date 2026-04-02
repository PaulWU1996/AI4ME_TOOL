from celery import Celery

app = Celery(
    'tasks',
    broker='redis://redis:6379/0',
    backend='redis://redis:6379/0'
)

app.conf.update(
    broker_transport_options={'visibility_timeout': 3600},  # 1小时的任务可见性超时
    result_expires=86400,  # 24小时的结果过期时间
    worker_prefetch_multiplier=1,  # 每个 worker 一次只取一个任务
    task_acks_late=True,  # 任务执行完成后才发送 ack
    result_persistent=True,  # 结果持久化到 Redis
)

@app.task
def process_visual(file_path):
    pass

@app.task
def process_audio(file_path):
    pass

@app.task(name="tasks.finalize_results")
def finalize_results(raw_results, job_id):
    pass