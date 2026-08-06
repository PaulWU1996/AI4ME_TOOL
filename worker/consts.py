import os 

redis_host = os.getenv("REDIS_HOST", "127.0.0.1")
redis_port = os.getenv("REDIS_PORT", "6379")

# Service URL
audio_host = os.getenv("AUDIO_HOST", "localhost")
audio_port = os.getenv("AUDIO_PORT", "9002")
visual_host = os.getenv("VISUAL_HOST", "localhost")
visual_port = os.getenv("VISUAL_PORT", "9001")
visual_api_admin_key = os.getenv("ADMIN_KEY", "ai4me_admin_password")
summarise_host = os.getenv("TRANSCRIPT_HOST", "localhost")
summarise_port = os.getenv("TRANSCRIPT_PORT", "9003")
tagging_host = os.getenv("TAGGING_HOST", "localhost")
tagging_port = os.getenv("TAGGING_PORT", "9004")


audio_api_url = f"http://{audio_host}:{audio_port}/process_audio/"
visual_api_url = f"http://{visual_host}:{visual_port}"
summarise_api_url = f"http://{summarise_host}:{summarise_port}/process/"
tagging_api_url = f"http://{tagging_host}:{tagging_port}/process/"

shared_path = os.getenv("SHARED_PATH", "/app/tmp")
api_key_path = os.getenv("API_KEY_PATH", "/app/data")

# --- Config Settings ---
compose_file = os.getenv("COMPOSE_FILE", "/app/docker-compose.yml")
project_dir = os.getenv("COMPOSE_PROJECT_DIR")

HEALTH_CHECK_TIMEOUT = 330
HEALTH_CHECK_INTERVAL = 60