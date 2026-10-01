"""HTTP API over the transcript-tools scripts.

The scripts in this directory are `driver: "python"` nodes today: the worker
runs one as a subprocess with the predecessor payload on stdin and expects a
single JSON document on stdout. This app exposes the same four operations
over HTTP so a workflow can reach them with `driver: "http"` instead, with
that payload arriving as a JSON body rather than on stdin.

The scripts remain the implementation -- their `run(payload)` is called
unchanged -- so the files written into the shared volume are identical
whichever driver runs them.
"""
import logging
import os
from collections.abc import Callable
from typing import Literal

import segment_extent
import speaker_extent
import transcript_to_text
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

router = APIRouter()

JobType = Literal[
    "transcript_to_text",
    "segment_extent",
    "speaker_extent",
    "semantic_extent",
]


class ProcessRequest(BaseModel):
    """The job payload a workflow node POSTs.

    `extra="ignore"` because `tasks.http_call` forwards the whole predecessor
    payload (`video_path`, `prompts`, ...); only the fields below are consumed.
    """

    model_config = ConfigDict(extra="ignore")

    job_id: str
    job_type: JobType
    file_path: str
    shared_path: str | None = None

    def script_payload(self) -> "ScriptPayload":
        """The payload the scripts read, with the shared root defaulted.

        A node's payload carries the worker's own `shared_path`; the fallback
        keeps the service usable on its own (a plain curl), where the shared
        volume is mounted where this service expects it.
        """
        return ScriptPayload(
            file_path=self.file_path,
            job_id=self.job_id,
            shared_path=self.shared_path or shared_root(),
        )


class ScriptPayload(BaseModel):
    """Exactly the keys a script's `run(payload)` reads."""

    file_path: str
    job_id: str
    shared_path: str


class JobResult(BaseModel):
    """Fields every result carries.

    `extra="forbid"` keeps each job_type's result one exact shape: without it a
    speaker_extent result (which carries `file_path` as well) would also
    validate as a TranscriptTextResult, leaving the response union ambiguous.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    job_type: JobType


class TranscriptTextResult(JobResult):
    """`file_path` now points at the transcript.txt written for this job."""

    file_path: str


class SegmentExtentResult(JobResult):
    """First utterance's span to the last one's, in the transcript's own ms."""

    start: float
    end: float


class SpeakerExtentResult(JobResult):
    """`file_path` now points at the trimmed transcript JSON written for this job."""

    file_path: str
    start: float
    end: float


class SemanticExtentResult(JobResult):
    """Same shape as SpeakerExtentResult, from the semantic chunker's range.

    Separate class rather than a reuse of that one so the OpenAPI schema names
    the job_type that produces it; the fields are identical.
    """

    file_path: str
    start: float
    end: float


def _semantic_extent(payload: dict) -> dict:
    """Run semantic_extent, importing it on first use.

    That module imports chonkie at the top level, and chonkie pulls its
    embedding model on first use. Importing here rather than at the top of
    this file keeps that off the app's startup path, so /health and the other
    three job_types stay fast, and stay up, if the chonkie install or the
    model download ever fails -- the failure then lands on the semantic request
    that needed them instead of the whole service.
    """
    import semantic_extent

    return semantic_extent.run(payload)


JOB_TYPES: dict[JobType, tuple[Callable[[dict], dict], type[JobResult]]] = {
    "transcript_to_text": (transcript_to_text.run, TranscriptTextResult),
    "segment_extent": (segment_extent.run, SegmentExtentResult),
    "speaker_extent": (speaker_extent.run, SpeakerExtentResult),
    "semantic_extent": (_semantic_extent, SemanticExtentResult),
}


def shared_root() -> str:
    """The shared volume root this service reads and writes under."""
    return os.environ.get("SHARED_PATH", "/app/tmp")


@router.post(
    "/process",
    response_model=(
        TranscriptTextResult | SegmentExtentResult | SpeakerExtentResult | SemanticExtentResult
    ),
)
def process(req: ProcessRequest):
    """Run one script over a transcript JSON in the shared volume.

    Each job_type writes its outputs into `{shared_path}/{job_id}/` and returns
    only its own keys -- a segment_extent result deliberately omits `file_path`,
    because `http_call`'s `merge` would let a null here overwrite the video path
    the next node in the chain needs.
    """
    run, result_cls = JOB_TYPES[req.job_type]

    if not os.path.isfile(req.file_path):
        raise HTTPException(status_code=404, detail=f"input file not found: {req.file_path}")

    payload = req.script_payload()
    logger.info(
        "job received | job_id=%s job_type=%s file=%s",
        req.job_id,
        req.job_type,
        payload.file_path,
    )

    try:
        result = run(payload.model_dump())
    except ValueError as exc:
        # The scripts reject a transcript they cannot use: unreadable JSON, or
        # no segments in it.
        logger.warning(
            "transcript rejected | job_id=%s job_type=%s error=%s",
            req.job_id,
            req.job_type,
            exc,
        )
        raise HTTPException(status_code=422, detail=str(exc))
    except KeyError as exc:
        # A segment is missing a field the script needs, e.g. the "speaker"
        # that speaker_extent groups turns by.
        logger.warning(
            "transcript missing field | job_id=%s job_type=%s field=%s",
            req.job_id,
            req.job_type,
            exc,
        )
        raise HTTPException(status_code=422, detail=f"transcript missing required field: {exc}")
    except Exception as exc:
        logger.exception("script failed | job_id=%s job_type=%s", req.job_id, req.job_type)
        raise HTTPException(status_code=500, detail=f"{req.job_type} failed: {exc}")

    logger.info("done | job_id=%s job_type=%s", req.job_id, req.job_type)
    return result_cls(job_id=req.job_id, job_type=req.job_type, **result)


@router.get("/health")
def health():
    shared = shared_root()
    if not os.path.isdir(shared):
        raise HTTPException(status_code=503, detail=f"shared volume not mounted at {shared}")
    return {"status": "ok", "shared_path": shared, "job_types": sorted(JOB_TYPES)}


app = FastAPI(title="AI4ME Audio Transcription", version="0.1.0")
app.include_router(router)