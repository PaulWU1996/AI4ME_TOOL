FROM python:3.10-slim

# Install docker.io and curl (needed to download the binary)
RUN apt-get update && apt-get install -y \
    docker.io \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Download and install the docker compose plugin
RUN mkdir -p /usr/libexec/docker/cli-plugins/ && \
    curl -SL "https://github.com/docker/compose/releases/download/v2.24.5/docker-compose-linux-$(uname -m)" -o /usr/libexec/docker/cli-plugins/docker-compose && \
    chmod +x /usr/libexec/docker/cli-plugins/docker-compose

WORKDIR /app

# Celery's app loader puts the working directory on sys.path only while it
# imports the app module, then removes it again -- so a lazy `import dag`
# inside a task body (worker/tasks.py:461) fails at run time even though the
# package sits right there in /app. Setting PYTHONPATH makes /app importable
# for the whole process lifetime, not just during app discovery.
ENV PYTHONPATH=/app

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/ .
COPY dag/ ./dag

CMD ["celery", "-A", "tasks", "worker", "--loglevel=info", "--concurrency=1"]