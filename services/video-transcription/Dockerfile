# Base image: Python 3.12 on Debian slim
FROM mcr.microsoft.com/azureml/openmpi5.0-cuda12.6-ubuntu24.04 as base

# Always non-interactive apt and sane Python defaults
ENV DEBIAN_FRONTEND=noninteractive \
	PIP_NO_CACHE_DIR=1 \
	PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1

# System dependencies required by OpenCV and MediaPipe
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
	git \
	ffmpeg \
	libsndfile1 \
	libgl1 \
	libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/AI4ME_TOOL/gemma

# Install Python deps first (better caching)
COPY requirements.txt /tmp/gemma-requirements.txt
RUN python -m pip install --upgrade pip \
 && pip install --no-cache-dir -r /tmp/gemma-requirements.txt

# Keep the script available when the repository is not bind-mounted.
COPY analyze_with_gemma.py ./analyze_with_gemma.py

# Make the repository root available to imports and preserve host-relative paths.
ENV PYTHONPATH=/workspace

# Default to an interactive shell; override with `docker run ... <cmd>` or compose
CMD ["bash"]

