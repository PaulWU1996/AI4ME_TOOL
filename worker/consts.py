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

