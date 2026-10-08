# Stdlib-only mock service: no pip install, no model weights, no GPU.
FROM python:3.11-slim

WORKDIR /app
COPY service.py .

# curl is only here so the compose healthcheck can use the same CMD form the
# real services use; the service itself needs nothing beyond the stdlib.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV MOCK_ROLE=visual MOCK_PORT=8000
EXPOSE 8000
CMD ["python", "-u", "service.py"]
