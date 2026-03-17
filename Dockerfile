# Step 1: Base Image with CUDA 12 Support
FROM nvidia/cuda:12.1.0-cudnn8-devel-ubuntu22.04

# Step 2: System Setup & Tools
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    wget ffmpeg git curl libgl1-mesa-glx libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Step 3: Install Miniconda
RUN wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh && \
    bash /tmp/miniconda.sh -b -p /opt/conda && \
    rm /tmp/miniconda.sh
ENV PATH="/opt/conda/bin:$PATH"

# Step 4: Create Conda Environments with PyTorch (CUDA 12.1 build)
COPY visual.txt audio.txt /tmp/

# ENV A: Visual
RUN conda create -n visual python=3.11 -y && \
    /opt/conda/envs/visual/bin/pip install -r /tmp/visual.txt

# ENV B: Audio
RUN conda create -n audio python=3.11 -y && \
    /opt/conda/envs/audio/bin/pip install -r /tmp/audio.txt

# Step 5: Main App Setup (Base env)
RUN pip install fastapi uvicorn boto3 python-multipart

WORKDIR /app
# COPY PROJ（Include main.py, visual_proj, audio_proj）
COPY . .

# Step 6: Final Configuration
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]