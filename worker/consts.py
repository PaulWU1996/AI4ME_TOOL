import json
import os

# Alot of this should be defined either internaly to the workers container or in the workflow definition.

redis_host = os.getenv("REDIS_HOST", "127.0.0.1")
redis_port = os.getenv("REDIS_PORT", "6379")

# Service URL
audio_host = os.getenv("AUDIO_HOST", "localhost")
audio_port = os.getenv("AUDIO_PORT", "9002")
visual_host = os.getenv("VISUAL_HOST", "localhost")
visual_port = os.getenv("VISUAL_PORT", "9001")
visual_api_admin_key = os.getenv("ADMIN_KEY", "ai4me_admin_password")


audio_api_url = f"http://{audio_host}:{audio_port}/process_audio/"
visual_api_url = f"http://{visual_host}:{visual_port}"

shared_path = os.getenv("SHARED_PATH", "/app/tmp")
api_key_path = os.getenv("API_KEY_PATH", "/app/data")

# --- Config Settings ---
compose_file = os.getenv("COMPOSE_FILE", "/app/docker-compose.yml")
project_dir = os.getenv("COMPOSE_PROJECT_DIR")
service_modes_path = os.getenv("SERVICE_MODES_PATH", "/app/tmp/service_modes.json")

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

transcript_text_file = "transcript.txt"

# How long start_service() waits for a container to report healthy, and how
# often it re-checks. Defaults are sized for GPU services loading multi-GB
# weights; env-overridable so a mock stack (tests/e2e/) can poll in seconds
# instead of spending a minute per cold start.
HEALTH_CHECK_TIMEOUT = int(os.getenv("HEALTH_CHECK_TIMEOUT", "330"))
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))

# How long to wait on a service's HTTP response before giving up. Sized for
# real GPU inference; env-overridable so a hung service is testable in
# seconds rather than half an hour. A service that accepts a connection and
# then never answers is a realistic failure, and without a reachable timeout
# the worker blocks on it for the full budget.
VISUAL_REQUEST_TIMEOUT = int(os.getenv("VISUAL_REQUEST_TIMEOUT", "6000"))
AUDIO_REQUEST_TIMEOUT = int(os.getenv("AUDIO_REQUEST_TIMEOUT", "1800"))