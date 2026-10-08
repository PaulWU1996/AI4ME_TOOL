FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Layer 1 — OS packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    zstd \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Layer 2 — Ollama binary (~150 MB; cached until install script changes)
RUN curl -fsSL https://ollama.com/install.sh | sh

# Layer 3 — Python dependencies (cached until requirements.txt changes)
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

# Layer 4 — Application code (most frequently changed; always last)
COPY app/ ./app/
COPY scripts/ ./scripts/
RUN chmod +x /app/scripts/entrypoint.sh

# Declare mount points so Docker creates them with correct ownership
RUN mkdir -p /shared /root/.ollama/models

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
