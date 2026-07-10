from celery import Celery

import os

redis_host = os.getenv('REDIS_HOST', '127.0.0.1')
redis_port = os.getenv('REDIS_PORT', '6379')

app = Celery('tasks',
             broker=f'redis://{redis_host}:{redis_port}/0',
             backend=f'redis://{redis_host}:{redis_port}/0')

app.conf.update(
    broker_transport_options={'visibility_timeout': 3600},
    result_expires=86400,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    result_persistent=True,
)
