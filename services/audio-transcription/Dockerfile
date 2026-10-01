# CPU-only: the transcription scripts are stdlib json work, so no CUDA base is
# needed here (unlike audioservice/visualservice, which load model weights).
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dependencies first, so the layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The scripts this app wraps import each other flatly (`from transcript import
# ...`), so /app must stay on sys.path for the whole process lifetime -- not just
# while uvicorn imports the app module, which is what PYTHONPATH buys here.
COPY . /app/

# Mount point for the shared volume, so the image also runs standalone.
RUN mkdir -p /app/tmp

EXPOSE 8000

CMD ["uvicorn", "router:app", "--host", "0.0.0.0", "--port", "8000"]