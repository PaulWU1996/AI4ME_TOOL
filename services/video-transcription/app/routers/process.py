import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.services import gemma_runner, mongodb

logger = logging.getLogger(__name__)

router = APIRouter()

# One GPU, one request at a time. `/health` deliberately does not take this lock,
# so readiness stays answerable while a long analysis holds it.
_analysis_lock = asyncio.Lock()


def _shared_path() -> Path:
    return Path(os.environ.get("SHARED_VOLUME_PATH", "/shared"))


class ProcessRequest(BaseModel):
    job_id: str
    job_type: Literal["gemma"] = "gemma"
    video_path: str
    prompts: Optional[str] = None
    language: str = "en"
    clip_start: float | None = None
    clip_end: float | None = None
    shot_detection: Literal["detect", "test"] | None = None
    storage_type: Literal["file_system", "mongodb"] = "file_system"
    storage_id: str | None = None


class NarrativeSegment(BaseModel):
    start: str
    end: str
    caption: str


class TranscriptSegment(BaseModel):
    start: str
    end: str
    transcript: str


class ModelsInfo(BaseModel):
    primary_model: str
    audio_analysis_model: str
    primary_supports_audio: bool


class ProcessResponse(BaseModel):
    job_id: str
    job_type: Literal["gemma"]
    narrative: list[NarrativeSegment]
    audio_narrative: list[NarrativeSegment]
    transcript: list[TranscriptSegment]
    audio_detected: bool
    video_duration_seconds: float
    models: ModelsInfo
    processing_time_ms: int


def _resolve_video(job_id: str, video_path: str) -> Path:
    """Resolve `video_path` inside the job's shared directory, or explain why not.

    `video_path` is relative to the shared volume root and conventionally
    `"{job_id}/{filename}"` — the same shape the orchestrator's audio service
    receives. It must still land inside this job's directory, so one job can
    never reach another job's media.
    """
    if not video_path.strip():
        logger.warning("video_path empty | job_id=%s", job_id)
        raise HTTPException(status_code=422, detail="video_path is empty")

    requested = Path(video_path)
    if requested.is_absolute():
        logger.warning("video_path absolute | job_id=%s video_path=%s", job_id, video_path)
        raise HTTPException(
            status_code=422,
            detail=(
                f"video_path must be relative to the shared volume root "
                f"({_shared_path()}), got an absolute path: {video_path}"
            ),
        )

    shared_root = _shared_path().resolve()
    job_dir = (shared_root / job_id).resolve()
    resolved = (shared_root / requested).resolve()
    if not resolved.is_relative_to(job_dir):
        logger.warning(
            "video_path outside job dir | job_id=%s video_path=%s resolved=%s job_dir=%s",
            job_id,
            video_path,
            resolved,
            job_dir,
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"video_path must be '{job_id}/<filename>' so it stays inside the job "
                f"directory, got: {video_path}"
            ),
        )
    if not resolved.exists():
        logger.warning("video not found | job_id=%s path=%s", job_id, resolved)
        raise HTTPException(
            status_code=404,
            detail=f"video not found for job {job_id}: {video_path}",
        )
    if not resolved.is_file():
        logger.warning("video_path not a file | job_id=%s path=%s", job_id, resolved)
        raise HTTPException(
            status_code=422,
            detail=f"video_path is not a file: {video_path}",
        )
    return resolved


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_outputs(job_id: str, video_stem: str, response: ProcessResponse, result: dict) -> Path:
    job_dir = _shared_path() / job_id
    _write_json(job_dir / "output.json", response.model_dump())
    _write_json(job_dir / f"{video_stem}_gemma_visual_output.json", result["narrative"])
    _write_json(job_dir / f"{video_stem}_gemma_audio_output.json", result["audio_narrative"])
    _write_json(job_dir / f"{video_stem}_gemma_transcript_output.json", result["transcript"])
    return job_dir


@router.post("/process", response_model=ProcessResponse)
async def process(req: ProcessRequest):
    logger.info(
        "job received | job_id=%s job_type=%s language=%s video_path=%s",
        req.job_id,
        req.job_type,
        req.language,
        req.video_path,
    )

    video_path = _resolve_video(req.job_id, req.video_path)

    if not gemma_runner.is_ready():
        logger.error("model not loaded | job_id=%s", req.job_id)
        raise HTTPException(status_code=503, detail="Model is not loaded yet")

    if req.storage_type == "mongodb":
        try:
            await run_in_threadpool(mongodb.ensure_available)
        except Exception as exc:
            logger.error("mongo unavailable | job_id=%s error=%s", req.job_id, exc)
            raise HTTPException(
                status_code=503,
                detail=f"MongoDB storage unavailable, analysis not started: {exc}",
            )

    logger.info("analysis start | job_id=%s path=%s", req.job_id, video_path)

    try:
        async with _analysis_lock:
            result = await run_in_threadpool(
                gemma_runner.analyze,
                video_path,
                req.prompts,
                req.language,
                req.clip_start,
                req.clip_end,
                req.shot_detection,
            )
    except gemma_runner.InvalidVideoError as exc:
        logger.error("invalid video | job_id=%s error=%s", req.job_id, exc)
        raise HTTPException(status_code=422, detail=str(exc))
    except gemma_runner.AnalysisError as exc:
        logger.error("analysis failed | job_id=%s error=%s", req.job_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logger.exception("analysis crashed | job_id=%s", req.job_id)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}")

    empty = [name for name in ("narrative", "audio_narrative", "transcript") if not result[name]]
    if empty:
        logger.error("empty analysis result | job_id=%s missing=%s", req.job_id, empty)
        raise HTTPException(
            status_code=500,
            detail=f"Model produced no {', '.join(empty)} for this video",
        )

    logger.info(
        "analysis done | job_id=%s shots=%d ms=%d",
        req.job_id,
        len(result["narrative"]),
        result["processing_time_ms"],
    )

    response = ProcessResponse(job_id=req.job_id, job_type=req.job_type, **result)

    if req.storage_type == "mongodb":
        doc = {
            "full_output": response.model_dump(),
            "narrative": result["narrative"],
            "audio_narrative": result["audio_narrative"],
            "transcript": result["transcript"],
        }
        try:
            await run_in_threadpool(mongodb.store_obj, req.storage_id or req.job_id, doc)
        except Exception as exc:
            logger.exception("mongo store failed | job_id=%s", req.job_id)
            job_dir = _write_outputs(req.job_id, video_path.stem, response, result)
            raise HTTPException(
                status_code=503,
                detail=(f"MongoDB store failed ({exc}); analysis preserved in {job_dir}"),
            )
        logger.info("stored in mongodb | job_id=%s id=%s", req.job_id, req.storage_id or req.job_id)
    else:
        job_dir = _write_outputs(req.job_id, video_path.stem, response, result)
        logger.info("output written | job_id=%s dir=%s", req.job_id, job_dir)

    return response


@router.get("/health")
async def health():
    if not gemma_runner.is_ready():
        raise HTTPException(
            status_code=503,
            detail={"status": "degraded", "model_ready": False, **gemma_runner.model_status()},
        )
    return {"status": "ok", "model_ready": True, **gemma_runner.model_status()}
