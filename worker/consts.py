import json
import os

redis_host = os.getenv("REDIS_HOST", "127.0.0.1")
redis_port = os.getenv("REDIS_PORT", "6379")

shared_path = os.getenv("SHARED_PATH", "/app/tmp")
api_key_path = os.getenv("API_KEY_PATH", "/app/data")
python_script_root = os.getenv("PYTHON_SCRIPT_ROOT", "/app/services")
python_call_timeout = int(os.getenv("PYTHON_CALL_TIMEOUT", "300"))

# --- Config Settings ---
compose_file = os.getenv("COMPOSE_FILE", "/app/docker-compose.yml")
project_dir = os.getenv("COMPOSE_PROJECT_DIR")

# start_service()/stop_service() pass a service's compose-file key (e.g.
# "visualservice") straight to `docker container get()` too, assuming
# container_name == the compose key -- true in production, where it's set
# explicitly to match. A second stack sharing that same key (tests/e2e's
# mock compose file) can't also set container_name to the same literal
# string without colliding with production's real container on the host
# Docker daemon. This lets such a stack tell the worker "look up container
# X for logical service Y" instead, without changing the compose key
# `_compose()` needs. Empty/unset (the production default) preserves
# today's exact behavior: container_name is assumed to equal service_name.
SERVICE_CONTAINER_NAMES = json.loads(os.getenv("SERVICE_CONTAINER_NAMES", "{}"))

# How long start_service() waits for a container to report healthy, and how
# often it re-checks. Defaults are sized for GPU services loading multi-GB
# weights; env-overridable so a mock stack (tests/e2e/) can poll in seconds
# instead of spending a minute per cold start.
HEALTH_CHECK_TIMEOUT = int(os.getenv("HEALTH_CHECK_TIMEOUT", "330"))
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))
