# CONTAINERIZED MEDIA PROCESSING PIPELINE FOR BBC

---

## 1. PROJECT OVERVIEW

A distributed media processing tool designed for BBC creative teams. It integrates audio and visual modality understanding algorithms into a scalable containerized architecture using **FastAPI**, **Celery**, and **Redis**.

---

## 2. DIRECTORY STRUCTURE

```bash
.
|-- controller
|   |-- main.py         # FastAPI application & API endpoints
|   |-- downloader.py   # Support for S3, URL, and Local file ingestion
|   |-- tasks.py        # Celery task signatures for Producer side
|   |-- Dockerfile      # Python 3.10-slim base
|-- worker
|   |-- tasks.py        # Analysis logic & automated cleanup for Consumer side
|   |-- Dockerfile      # Celery worker configuration
|-- docker-compose.yml  # Service orchestration for Redis, Controller, Worker
|-- shared              # Shared volume for temporary media processing
|   |-- api-data        # Folder storing Sample API request payloads for testing
|-- weights             # Placeholder for AI model weights
|   |-- AFWhisper       
|   |   |-- sound_tower
|   |-- PALUniEncRdFc3Llama31_8B_s2
|   |   |-- checkpoint-final
|   |-- models
|-- init.sh              # Initialization script for setting up the environment for visual service 
|-- README.md            # Project documentation (this file) 
```

---

## 3. KEY FEATURES

- **Universal Ingestion**  
  Support for S3, HTTP/HTTPS, and local file paths  

- **Parallel Processing**  
  Uses Celery Chords to run heavy audio and visual analysis simultaneously  

- **Automated Cleanup**  
  The worker automatically deletes `/app/tmp/<job_id>` once processing is finalized to prevent disk overflow  

- **Industrial Stability**  
  Optimized with:
  - Visibility timeout (1 hour)  
  - Strict concurrency limits  
  - Late acknowledgments  
  - Designed for long-running (30min+) AI workloads on GPUs (e.g., A100)

- **Containerized Architecture**  
  Each component (Controller, Worker, Redis) runs in its own Docker container for modularity and scalability

- **Automatic First Aid**  
  The system is designed to handle and recover from common failure scenarios (e.g., task timeouts, worker crashes) without manual intervention

---

## 4. GETTING STARTED

### Prerequisites

- Docker  
- Docker Compose  
- (Optional) AWS credentials for S3 access  

### Deployment

1. Copy the weights folder to the appropriate location following the structure outlined above. (Note: The shared volume and the api-data folder are shown above but you need to mannually create them and put the corresponding place following the stracture above.)

2. Load the Docker images for the audio and visual services, respectively:

```bash
docker load -i audioservice.tar
docker load -i narrative-api.tar
```
- The narrative-api.tar is the image for the visual service and produced by Asmar. (No test on his image yet, but it should work as long as the entrypoint is correct and the model weights are in place).

- The audioservice.tar is the image for the audio service and produced by Tony (audio llm) and Paul (plugin wrapper and docker design). It has been tested and works with the current codebase.

3. Start the entire stack using Docker Compose:
```bash
docker-compose up --build
```
There will be several services starting up, including Redis, the Controller API, the Worker API, the audio service and the visual service. The Controller and Worker will connect to Redis for task orchestration.

Once compose completed, the TOOL API will be available at:

```
http://localhost:9000
```

---

### API Usage

#### Start Processing

- **Endpoint:** `POST /process?path={media_path}&callback_url={optional_callback}`

```bash
curl -X POST "http://localhost:9000/process?path=/app/data/video.mp4"
```

Returns a `job_id` used for tracking the asynchronous workflow. If `callback_url` is provided, results will be sent there once processing is complete. Otherwise, results can be retrieved via the status endpoint.


The `path` parameter supports:
- Local file paths (e.g., `/app/data/video.mp4`)
- HTTP/HTTPS URLs (e.g., `https://example.com/video.mp4`)
- S3 paths (e.g., `s3://bucket/key` - requires AWS credentials to be configured in the Controller environment) [Currently not tested with S3, but the downloader.py has the logic to support S3 download using boto3, so it should work as long as the AWS credentials are properly set up in the Controller container]

Once the request is received, the Controller will:
1. Create a unique `job_id` and corresponding temporary workspace at `/app/tmp/{job_id}`
2. Download the media file into this workspace using `downloader.py`
3. Dispatch Celery tasks for audio and visual processing in parallel
4. Monitor task completion and aggregate results once all tasks are finished
5. Clean up the temporary workspace to free disk space
6. Return combined results to the client or send them to the provided `callback_url` if specified

Note: The outputs (audio and visual analysis results, as well the task info) will be saved in the shared volume workspace under `/app/tmp/{job_id}/` before being returned to the client or sent to the callback URL. You can also check the outputs on the host machine by navigating to the corresponding directory in the shared volume (e.g., `/your/path/to/shared_vol/{job_id}/`) while the processing is still running or after it has completed. This can be useful for debugging or verifying intermediate results.


---

#### Check Status & Get Results

- **Endpoint:** `GET /status/{job_id}`

```bash
curl http://localhost:9000/status/<your_job_id>
```

Returns combined JSON results once `is_ready` is `true`.

---

## 5. TASK ORCHESTRATION DETAILS

The pipeline utilizes **Celery Chords**:

1. **Header Tasks**  
   - `process_audio`  
   - `process_visual`  
   → Dispatched in parallel via Redis queue  

2. **Callback Task**  
   - `finalize_results`  
   → Executes only after all header tasks complete  

3. **Cleanup Phase**  
   - Performs recursive deletion of the temporary video in workspace, the output json files are saved in the same workspace  

The full workflow is demonstrated in the following diagram:

```
Client Request
    |
    v
Controller API (FastAPI)   -> Downloader (S3/URL/Local) -> /shared/{job_id}/video.xxx 
    |
    v
+------------------+
| Celery Chord     |
|------------------|
| Header Tasks     |
| - process_audio  |
| - process_visual |
|------------------|
| Callback Task    |
| - finalize_results|
+------------------+
    |
    v
Worker API (Celery Worker)
    |
    v
+------------------+
| Task Execution   |
| - Audio Analysis |
| - Visual Analysis|
+------------------+
    |
    v
Results Aggregation & Cleanup   -> /shared/{job_id}/ only cotains the output json files, the video file and intermediate files have been deleted 
    |
    v
Client Response / Callback Notification
```

Note: The /shared/{job_id}/ directory will not be automatically deleted by orchestrator (reddis, controller, worker and autoheal). The reason is that we want to keep the output json files for the client and wait confirmation of the final export method (e.g. push to database, save local file, send to callback url).
---

## 6. INDIVIDUAL SERVICE COMPONENT TESTING

For the purpose of testing individual service components (audio and visual service) without Docker Compose, you can use the following commands. 

### 6.1  Audio Service Testing

Start the audio service container with the appropriate environment variables and volume mounts:
```bash
docker run -d \
  --name audioservice \
  --gpus all \
  -e MODEL_PATH="/app/weights/checkpoint-final" \
  -e SHARED_PATH="/app/tmp" \
  -v /your/path/to/PALUniEncRdFc3Llama31_8B_s2/checkpoint-final:/app/weights/checkpoint-final \
  -v /your/path/to/shared_vol:/app/tmp \
  -p 9002:8000 \
  -w /app \
  audioservice:latest \
  python3 -m uvicorn src.audio_entry:app --host 0.0.0.0 --port 8000
```
Once service is ready, send a test request to the audio service:
```bash
curl -X POST "http://localhost:9002/process?path=https://www.w3schools.com/html/mov_bbb.mp4"
```
You can also check the service status by sending a GET request to the status endpoint:
```bash
curl -X POST "http://localhost:9002/health"
```

### 6.2  Visual Service Testing

Docker running command for the visual service.
```bash
# Load image
docker load -i narrative-api.tar

# Create data directory for API keys
mkdir -p ~/narrative-api-data

# Run
docker run -d \
  --runtime=nvidia \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e ADMIN_KEY=your-admin-key \
  --name narrative-api \
  -v /path/to/weights:/app/models \
  -v ~/narrative-api-data:/app/data \
  -p 8000:8000 \
  narrative-api

# Check it is running (wait 1-2 min for model to load)
curl http://localhost:8000/health
```

Once the service is running, you can generate the api-key following:
```bash
curl -X POST http://localhost:8000/api/keys/generate \
  -H "X-Admin-Key: change-me-in-production" \
  -H "Content-Type: application/json" \
  -d '{"client_name": "client-ai4me", "expires_in_days": 365}'
```

Save the api_key value from the response — it is shown only once.

And then you can send a test request to the visual service:
```bash
Analyse a video
curl -X POST http://localhost:8000/analyze \
  -H "X-API-Key: sk_your-api-key" \
  -F "video=@/path/to/video.mp4" \
  --output result.xml
```

Supported formats: mp4, avi, mov, mkv, webm

---

### Appendix: Commands for 
reddis start command:
```
apptainer run --env LC_ALL=C redis.sif \
  redis-server \
  --port 6379 \
  --protected-mode no \
  --save "" \
  --appendonly no \
  --dir /tmp \
  --logfile ""
```

redis check command:
```
apptainer exec redis.sif redis-cli -p 6379 ping
```

controller start command:
```
apptainer exec \
  --env SHARED_PATH="/mnt/fast/nobackup/scratch4weeks/pw0036/Compose/temp_data" \
  --env REDIS_HOST="127.0.0.1" \
  --pwd /app \
  controller.sif \
  uvicorn main:app --host 0.0.0.0 --port 9000
```

worker start command:
```
apptainer exec \
  --env SHARED_PATH="/mnt/fast/nobackup/scratch4weeks/pw0036/Compose/temp_data" \
  --env REDIS_HOST="127.0.0.1" \
  --pwd /app \
  worker.sif \
  celery -A tasks worker --loglevel=info --pool=solo
```

curl test command:
```
curl -X POST "http://127.0.0.1:9000/process?path=https://www.w3schools.com/html/mov_bbb.mp4"
```

```
apptainer run --nv --env MODEL_PATH="/mnt/fast/nobackup/scratch4weeks/pw0036/Compose/weights/PALUniEncRdFc3Llama31_8B_s2/checkpoint-final" --env SHARED_PATH="/mnt/fast/nobackup/scratch4weeks/pw0036/samples"  --pwd app audioservice@1.sif python3 -m uvicorn src.audio_entry:app --host 0.0.0.0 --port 8000
```