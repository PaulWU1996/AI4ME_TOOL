# Base image: Python 3.12 + CUDA 12.6 on Ubuntu 24.04
FROM mcr.microsoft.com/azureml/openmpi5.0-cuda12.6-ubuntu24.04

# Always non-interactive apt and sane Python defaults
ENV DEBIAN_FRONTEND=noninteractive \
	PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PIP_NO_CACHE_DIR=1

# System dependencies required by OpenCV, MediaPipe and the compose healthcheck
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
	curl \
	git \
	ffmpeg \
	libsndfile1 \
	libgl1 \
	libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better caching)
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt

# Application code (most frequently changed; always last)
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY analyze_with_gemma.py ./analyze_with_gemma.py
RUN chmod +x /app/scripts/entrypoint.sh

# `app.main:app` is imported from inside a task/router body, so the working
# directory must be importable.
ENV PYTHONPATH=/app

# Declare mount points so Docker creates them with correct ownership
RUN mkdir -p /shared /root/.cache/huggingface

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
