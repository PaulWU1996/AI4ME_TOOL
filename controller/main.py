import json
import os

from fastapi import Body, FastAPI, HTTPException
from celery import uuid, signature
from celery.result import AsyncResult
from tasks import app as celery_app
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone

from dag.parser import Parser

app = FastAPI()

SUPPORTED_JOB_TYPES = ["full", "audio_only", "visual_only", "summarise", "speaker-extent-summarise", "utterance-extent-summarise", "tagging"]
MAX_ETA_SECONDS = 3600  # set to visibility_timeout value

# Registered DAG workflows dispatch through tasks.execute_workflow instead of
# the legacy build_chain() below. WORKFLOWS_PATH is a volume shared between
# the controller and worker containers; registry.json maps a workflow's own
# `workflow.name` to its file, and is updated by POST /workflows.
WORKFLOWS_PATH = os.getenv("WORKFLOWS_PATH", "/app/workflows")
REGISTRY_PATH = os.path.join(WORKFLOWS_PATH, "registry.json")


def load_registry() -> dict:
    if not os.path.exists(REGISTRY_PATH):
        return {}
    with open(REGISTRY_PATH, "r") as f:
        return json.load(f)


def save_registry(registry: dict):
    os.makedirs(WORKFLOWS_PATH, exist_ok=True)
    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2)


class ProcessRequest(BaseModel):
    path: str
    callback_url: Optional[str] = None
    prompts: Optional[str] = None
    job_type: str = "full"
    version: Optional[str] = None  # pin a specific registered workflow version; defaults to latest
    run_at_ms: Optional[int] = None


def build_chain(request: ProcessRequest, job_id: str):
    download = signature(
        "tasks.download_file",
        args=[request.path, job_id],
        kwargs={"prompts": request.prompts},
        immutable=True,
    )
    summarise = signature("tasks.process_summarise")
    tagging = signature("tasks.process_tags")
    transcript_to_text = signature("tasks.transcript_to_text")
    finalize = signature(
        "tasks.finalize_results",
        args=[job_id],
        kwargs={"job_type": request.job_type, "callback_url": request.callback_url},
        immutable=True,
    ).set(task_id=job_id)

    chains = {
        "full": (
            download
            | signature("tasks.process_visual")
            | signature("tasks.process_audio")
            | finalize
        ),
        "audio_only": (
            download 
            | signature("tasks.process_audio") 
            | finalize
        ),
        "visual_only": (
            download 
            | signature("tasks.process_visual") 
            | finalize
        ),
        "summarise": (
            download 
            | transcript_to_text
            | summarise 
            | tagging
            | finalize
        ),
        "speaker-extent-summarise": (
            download 
            | signature("tasks.speaker_extent") 
            | transcript_to_text 
            | summarise 
            | tagging
            | finalize
        ),
        "utterance-extent-summarise": (
            download 
            | signature("tasks.segment_extent") 
            | transcript_to_text 
            | summarise 
            | tagging
            | finalize
        ),
        "tagging": (
            download 
            | transcript_to_text 
            | tagging 
            | finalize
        ),
    }
    return chains.get(request.job_type)


def build_dag_workflow(request: ProcessRequest, job_id: str, workflow_path: str):
    return signature(
        "tasks.execute_workflow",
        kwargs={
            "workflow_path": workflow_path,
            "job_id": job_id,
            "path": request.path,
            "prompts": request.prompts,
            "job_type": request.job_type,
            "callback_url": request.callback_url,
        },
        immutable=True,
    ).set(task_id=job_id)


@app.post("/workflows")
async def register_workflow(workflow: dict = Body(...)):
    """Validate and permanently register a DAG workflow template version, so
    it can be referenced as a `job_type` (optionally pinned to a `version`)
    in POST /process afterwards.

    A name may hold multiple registered versions; `latest` tracks whichever
    was registered most recently and is what /process uses when a request
    doesn't pin a specific version. This applies to built-in job_type names
    too (e.g. "full") — build_chain()'s hardcoded chains are being phased
    out, so registering a DAG version under a built-in name is intentional:
    it becomes the default for that name once it's `latest`, and the legacy
    chain remains reachable only for requests with no matching registry
    entry at all.
    """
    meta = workflow.get("workflow") or {}
    name = meta.get("name")
    version = meta.get("version")
    if not name:
        raise HTTPException(status_code=400, detail="workflow.name is required.")
    if not version:
        raise HTTPException(status_code=400, detail="workflow.version is required.")

    registry = load_registry()
    entry = registry.get(name, {"latest": None, "versions": {}})
    if version in entry["versions"]:
        raise HTTPException(
            status_code=400,
            detail=f"Workflow '{name}' version '{version}' is already registered.",
        )

    os.makedirs(WORKFLOWS_PATH, exist_ok=True)
    dest_path = os.path.join(WORKFLOWS_PATH, f"{name}_{version}.json")
    tmp_path = dest_path + ".tmp"

    with open(tmp_path, "w") as f:
        json.dump(workflow, f, indent=2)

    try:
        Parser(tmp_path)  # validates acyclic + fully-declared dependencies
    except Exception as e:
        os.remove(tmp_path)
        raise HTTPException(status_code=400, detail=f"Invalid workflow: {e}")

    os.replace(tmp_path, dest_path)  # commit only once validated

    entry["versions"][version] = {"path": dest_path}
    entry["latest"] = version
    registry[name] = entry
    save_registry(registry)

    return {"status": "registered", "name": name, "version": version, "latest": entry["latest"]}


@app.post("/process")
async def start_pipeline(request: ProcessRequest):
    registry = load_registry()
    workflow_versions = registry.get(request.job_type)
    workflow_entry = None

    if workflow_versions is not None:
        resolved_version = request.version or workflow_versions.get("latest")
        workflow_entry = workflow_versions.get("versions", {}).get(resolved_version)
        if workflow_entry is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown version '{resolved_version}' for workflow '{request.job_type}'. "
                f"Available: {list(workflow_versions.get('versions', {}).keys())}",
            )
    elif request.version is not None:
        raise HTTPException(
            status_code=400,
            detail=f"job_type '{request.job_type}' has no registered versions to pin.",
        )

    if request.job_type not in SUPPORTED_JOB_TYPES and workflow_entry is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported job_type '{request.job_type}'. Choose from: {SUPPORTED_JOB_TYPES + list(registry.keys())}",
        )

    job_id = uuid()
    async_kwargs = {}

    if request.run_at_ms is not None:
        run_at_seconds = request.run_at_ms / 1000.0
        eta_dt = datetime.fromtimestamp(run_at_seconds, tz=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        diff_seconds = (eta_dt - now_dt).total_seconds()

        if diff_seconds > MAX_ETA_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"run_at_ms timestamp exceeds the maximum allowed delay of {MAX_ETA_SECONDS} seconds.",
            )

        if diff_seconds > 0:
            print(f"[Controller] Task set to run at: {eta_dt.isoformat()}")
            async_kwargs["eta"] = eta_dt

    try:
        if workflow_entry is not None:
            build_dag_workflow(request, job_id, workflow_entry["path"]).apply_async(**async_kwargs)
        else:
            build_chain(request, job_id).apply_async(**async_kwargs)
        return {"status": "submitted", "job_id": job_id, "job_type": request.job_type}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    result = AsyncResult(job_id, app=celery_app)

    if result.status == "PENDING" and not result.info:
        raise HTTPException(status_code=404, detail="Task not found or expired")

    response = {
        "job_id": job_id,
        "status": result.status,  # PENDING, STARTED, SUCCESS, FAILURE
        "is_ready": result.ready(),
        "data": None,
    }

    if result.ready():
        if result.successful():
            response["data"] = result.result
            response["message"] = "Task completed successfully"
        else:
            response["status"] = "FAILURE"
            response["message"] = str(result.result)

    return response
