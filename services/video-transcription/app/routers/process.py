import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, model_validator

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
    # Either a file already on the shared volume, or a programme to fetch audio for.
    video_path: str | None = None # rename to media_path
    programme_id: str | None = None
    start_ms: int | None = None
    duration_ms: int | None = None
    prompts: Optional[str] = None
    language: str = "en"
    clip_start: float | None = None
    clip_end: float | None = None
    shot_detection: Literal["detect", "test"] | None = None
    storage_type: Literal["file_system", "mongodb"] = "file_system"
    storage_id: str | None = None

    @model_validator(mode="after")
    def _one_source(self):
        if bool(self.video_path) == bool(self.programme_id):
            raise ValueError("Provide exactly one of `video_path` or `programme_id`")
        return self


class TimelineSegment(BaseModel):
    start: float
    end: float
    transcript: str
    audio_narrative: str
    narrative: str | None = None  # absent for audio-only input


class SceneResult(BaseModel):
    scene_id: str
    start_time: float
    end_time: float
    timeline: list[TimelineSegment]


class ModelsInfo(BaseModel):
    primary_model: str
    audio_analysis_model: str
    primary_supports_audio: bool


class ProcessResponse(BaseModel):
    job_id: str
    job_type: Literal["gemma"]
    programme_id: str | None = None
    scenes: list[SceneResult]
    models: ModelsInfo
    processing_time_ms: int


def _download_audio(programme_id: str, output_path: Path, start_ms: int | None, duration_ms: int | None) -> Path:
    from app.stream_decoder import fetch_dash_stream_audio

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fetch_dash_stream_audio(
        programme_id,
        start_time_ms=start_ms or 0,
        look_ahead_ms=duration_ms or sys.maxsize,
        output_path=str(output_path),
    )
    if not output_path.is_file():
        raise RuntimeError(f"No audio written for programme {programme_id}")
    logger.info("audio ready | programme_id=%s path=%s", programme_id, output_path)
    return output_path


def _cut_scene_audio(src: Path, dst: Path, start: float, end: float) -> Path:
    """Write `[start, end)` seconds of `src` to `dst` as 16kHz mono wav."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{end - start:.3f}",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as exc:
        raise gemma_runner.AnalysisError("FFmpeg is required to split scenes.") from exc
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="replace").strip()
        raise gemma_runner.AnalysisError(f"FFmpeg scene split failed for {dst.name}: {error}")
    return dst


def _timecode_to_seconds(timecode: str) -> float:
    hours, minutes, seconds = timecode.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _build_timeline(result: dict, offset: float) -> list[dict]:
    """Merge analyze()'s parallel segment lists into one list in programme seconds.

    analyze() emits one entry per analysis window in each list, in the same
    order and with the same timecodes, so they zip by index.
    """
    narratives = result.get("narrative")
    timeline = []
    for index, (transcript, audio) in enumerate(zip(result["transcript"], result["audio_narrative"])):
        segment = {
            "start": round(_timecode_to_seconds(transcript["start"]) + offset, 3),
            "end": round(_timecode_to_seconds(transcript["end"]) + offset, 3),
            "transcript": transcript["transcript"],
            "audio_narrative": audio["caption"],
        }
        if narratives is not None:
            segment["narrative"] = narratives[index]["caption"]
        timeline.append(segment)
    return timeline


def _resolve_media_path(media_path: str) -> Path:
    if Path(media_path).is_absolute():
        raise HTTPException(status_code=422, detail="media_path must be relative to the shared volume")
    root = _shared_path().resolve()
    path = (root / media_path).resolve()
    if not path.is_relative_to(root):
        raise HTTPException(status_code=422, detail="media_path resolves outside the shared volume")
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Media not found: {media_path}")
    if not path.is_file():
        raise HTTPException(status_code=422, detail=f"media_path is not a file: {media_path}")
    return path


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_outputs(job_id: str, media_stem: str, response: ProcessResponse) -> Path:
    """Write `output.json` plus the flat per-modality files `finalize_results` globs for."""
    job_dir = _shared_path() / job_id
    _write_json(job_dir / "output.json", response.model_dump(exclude_none=True))

    segments = [segment for scene in response.scenes for segment in scene.timeline]

    def timecode(segment: TimelineSegment) -> dict:
        return {
            "start": gemma_runner.format_timecode(segment.start),
            "end": gemma_runner.format_timecode(segment.end),
        }

    if any(segment.narrative is not None for segment in segments):
        _write_json(
            job_dir / f"{media_stem}_gemma_visual_output.json",
            [{**timecode(s), "caption": s.narrative or ""} for s in segments],
        )
    _write_json(
        job_dir / f"{media_stem}_gemma_audio_output.json",
        [{**timecode(s), "caption": s.audio_narrative} for s in segments],
    )
    _write_json(
        job_dir / f"{media_stem}_gemma_transcript_output.json",
        [{**timecode(s), "transcript": s.transcript} for s in segments],
    )
    return job_dir


@router.post("/process", response_model=ProcessResponse, response_model_exclude_none=True)
async def process(req: ProcessRequest):
    logger.info(
        "job received | job_id=%s job_type=%s language=%s video_path=%s programme_id=%s start_ms=%s duration_ms=%s",
        req.job_id,
        req.job_type,
        req.language,
        req.video_path,
        req.programme_id,
        req.start_ms,
        req.duration_ms,
    )

    if not gemma_runner.is_ready():
        logger.error("model not loaded | job_id=%s", req.job_id)
        raise HTTPException(status_code=503, detail="Model is not loaded yet")

    # Scene lookup needs Mongo whatever the storage type.
    if req.storage_type == "mongodb" or req.programme_id:
        try:
            await run_in_threadpool(mongodb.ensure_available)
        except Exception as exc:
            logger.error("mongo unavailable | job_id=%s error=%s", req.job_id, exc)
            raise HTTPException(
                status_code=503,
                detail=f"MongoDB unavailable, analysis not started: {exc}",
            )

    # Window of programme time covered by the downloaded audio, in seconds.
    window_start = (req.start_ms or 0) / 1000
    window_end = window_start + req.duration_ms / 1000 if req.duration_ms else None

    if req.programme_id:
        try:
            scenes = await run_in_threadpool(
                mongodb.find_scenes, req.programme_id, window_start, window_end
            )
        except Exception as exc:
            logger.exception("scene lookup failed | job_id=%s programme_id=%s", req.job_id, req.programme_id)
            raise HTTPException(status_code=503, detail=f"Could not fetch scenes for {req.programme_id}: {exc}")
        if not scenes:
            raise HTTPException(
                status_code=404,
                detail=f"No scenes for {req.programme_id} between {window_start}s and {window_end or 'end'}s",
            )
        logger.info("scenes found | job_id=%s count=%d", req.job_id, len(scenes))

        dest = _shared_path() / req.job_id / f"{req.programme_id}.wav"
        try:
            media_path = await run_in_threadpool(
                _download_audio,
                req.programme_id,
                dest,
                req.start_ms,
                req.duration_ms
            )
        except Exception as exc:
            logger.exception("audio download failed | job_id=%s programme_id=%s", req.job_id, req.programme_id)
            raise HTTPException(status_code=502, detail=f"Could not fetch audio for {req.programme_id}: {exc}")
    else:
        media_path = _resolve_media_path(req.video_path or '')
        # No programme, so no scenes: treat the requested window of the file as one scene.
        duration = await run_in_threadpool(gemma_runner.probe_duration_seconds, media_path)
        start = req.clip_start or 0.0
        end = req.clip_end if req.clip_end is not None else duration
        scenes = [{"scene_id": f"scene_{start:.2f}_{end:.2f}", "start_time": start, "end_time": end}]

    logger.info("analysis start | job_id=%s path=%s scenes=%d", req.job_id, media_path, len(scenes))

    t0 = time.monotonic()
    scene_docs = []
    models = None
    try:
        async with _analysis_lock:
            for scene in scenes:
                scene_id = scene["scene_id"]
                if req.programme_id:
                    # Clip to the downloaded window, then cut the scene out of the
                    # wav, which starts at `window_start` in programme time.
                    start = max(float(scene["start_time"]), window_start)
                    end = float(scene["end_time"])
                    if window_end is not None:
                        end = min(end, window_end)
                    if end <= start:
                        continue
                    scene_path = await run_in_threadpool(
                        _cut_scene_audio,
                        media_path,
                        _shared_path() / req.job_id / "scenes" / f"{scene_id}.wav",
                        start - window_start,
                        end - window_start,
                    )
                    # The scene file is the clip, and its timecodes start at 0.
                    clip_start, clip_end, offset = None, None, start
                else:
                    start, end = scene["start_time"], scene["end_time"]
                    scene_path = media_path
                    # analyze() reports timecodes in file time already.
                    clip_start, clip_end, offset = req.clip_start, req.clip_end, 0.0

                logger.info("scene start | job_id=%s scene_id=%s %.2fs-%.2fs", req.job_id, scene_id, start, end)
                result = await run_in_threadpool(
                    gemma_runner.analyze,
                    scene_path,
                    req.prompts,
                    req.language,
                    clip_start,
                    clip_end,
                    req.shot_detection,
                )

                expected = ("narrative", "audio_narrative", "transcript")
                if not result["video_detected"]:
                    expected = ("audio_narrative", "transcript")
                empty = [name for name in expected if not result[name]]
                if empty:
                    logger.error("empty analysis result | job_id=%s scene_id=%s missing=%s", req.job_id, scene_id, empty)
                    raise gemma_runner.AnalysisError(
                        f"Model produced no {', '.join(empty)} for scene {scene_id}"
                    )

                models = result["models"]
                scene_docs.append(
                    {
                        "programme_id": req.programme_id,
                        "scene_id": scene_id,
                        "start_time": start,
                        "end_time": end,
                        "timeline": _build_timeline(result, offset),
                    }
                )
    except gemma_runner.InvalidVideoError as exc:
        logger.error("invalid media | job_id=%s error=%s", req.job_id, exc)
        raise HTTPException(status_code=422, detail=str(exc))
    except gemma_runner.AnalysisError as exc:
        logger.error("analysis failed | job_id=%s error=%s", req.job_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logger.exception("analysis crashed | job_id=%s", req.job_id)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}")

    if not scene_docs:
        raise HTTPException(status_code=404, detail="No scene overlapped the requested window")

    processing_time_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "analysis done | job_id=%s scenes=%d ms=%d",
        req.job_id,
        len(scene_docs),
        processing_time_ms,
    )

    response = ProcessResponse(
        job_id=req.job_id,
        job_type=req.job_type,
        programme_id=req.programme_id,
        scenes=[
            SceneResult(**{k: v for k, v in doc.items() if k != "programme_id"})
            for doc in scene_docs
        ],
        models=models,
        processing_time_ms=processing_time_ms,
    )

    if req.storage_type == "mongodb":
        try:
            await run_in_threadpool(mongodb.store_scene_results, scene_docs)
        except Exception as exc:
            logger.exception("mongo store failed | job_id=%s", req.job_id)
            job_dir = _write_outputs(req.job_id, media_path.stem, response)
            raise HTTPException(
                status_code=503,
                detail=(f"MongoDB store failed ({exc}); analysis preserved in {job_dir}"),
            )
        logger.info("stored in mongodb | job_id=%s scenes=%d", req.job_id, len(scene_docs))
    else:
        job_dir = _write_outputs(req.job_id, media_path.stem, response)
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
