from celery import Celery

app = Celery(
    'tasks',
    broker='redis://redis:6379/0',
    backend='redis://redis:6379/0'
)

@app.task
def process_visual(file_path):
    pass

@app.task
def process_audio(file_path):
    pass