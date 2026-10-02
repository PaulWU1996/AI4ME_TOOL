import json
import logging
import os
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import ollama_client

logger = logging.getLogger(__name__)

router = APIRouter()


def _shared_path() -> Path:
    return Path(os.environ.get("SHARED_VOLUME_PATH", "/shared"))


class ProcessRequest(BaseModel):
    job_id: str
    job_type: Literal["summary", "tagging"] = "summary"
    file_name: str
    prompts: Optional[str] = None
    language: str = "en"


class SummaryResponse(BaseModel):
    job_id: str
    title: str
    summary: str
    model: str
    processing_time_ms: int


class TaggingResponse(BaseModel):
    job_id: str
    tags: list[str]
    model: str
    processing_time_ms: int


JOB_TYPES: dict[str, tuple[Path, type[SummaryResponse] | type[TaggingResponse]]] = {
    "summary": (Path("prompts", "summary"), SummaryResponse),
    "tagging": (Path("prompts", "tagging"), TaggingResponse),
}


def validate_result(job_type: str, result: dict) -> dict:
    if job_type == "summary":
        if not isinstance(result.get("title"), str) or not result["title"].strip():
            raise ValueError("Model returned no valid 'title'")
        if not isinstance(result.get("summary"), str) or not result["summary"].strip():
            raise ValueError("Model returned no valid 'summary'")
    elif job_type == "tagging":
        tags = result.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("Model returned invalid 'tags'; expected a list of strings")
    return result


@router.post("/process", response_model=SummaryResponse | TaggingResponse)
async def process_text(req: ProcessRequest):
    logger.info(
        "job received | file_name=%s job_id=%s job_type=%s language=%s",
        req.file_name,
        req.job_id,
        req.job_type,
        req.language,
    )

    if req.job_type not in JOB_TYPES:
        logger.warning("rejected job_type | job_id=%s job_type=%s", req.job_id, req.job_type)
        raise HTTPException(
            status_code=422,
            detail=f"job_type '{req.job_type}' is not handled by this service",
        )

    text_path = _shared_path() / req.job_id / req.file_name

    if not text_path.exists():
        logger.warning("text not found | job_id=%s path=%s", req.job_id, text_path)
        raise HTTPException(
            status_code=404,
            detail=f"text not found for job {req.job_id}",
        )

    text = text_path.read_text(encoding="utf-8").strip()

    if not text:
        logger.warning("text is empty | job_id=%s", req.job_id)
        raise HTTPException(status_code=422, detail=f"{req.file_name} is empty")

    logger.info("text read | job_id=%s chars=%d", req.job_id, len(text))

    if not await ollama_client.is_ready():
        logger.error("ollama unavailable | job_id=%s", req.job_id)
        raise HTTPException(status_code=503, detail="Ollama service unavailable")

    logger.info("ollama call start | job_id=%s", req.job_id)

    prompts_dir, response_cls = JOB_TYPES[req.job_type]

    try:
        result = await ollama_client.generate(
            text, req.language, custom_prompt=req.prompts, prompts_dir=prompts_dir
        )
        validated = validate_result(req.job_type, result)
    except ValueError as exc:
        logger.error("ollama parse error | job_id=%s error=%s", req.job_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info("ollama call done | job_id=%s ms=%d", req.job_id, result["processing_time_ms"])

    output = {"job_id": req.job_id, **{k: v for k, v in validated.items() if k != "job_id"}}

    output_path = _shared_path() / req.job_id / "output.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("output written | job_id=%s path=%s", req.job_id, output_path)

    response = response_cls(
        job_id=req.job_id,
        **{k: v for k, v in validated.items() if k != "job_id"},
    )
    return response


@router.get("/health")
async def health():
    ready = await ollama_client.is_ready()
    model = os.environ.get("OLLAMA_MODEL", "")
    if not ready:
        raise HTTPException(
            status_code=503,
            detail={"status": "degraded", "ollama_ready": False, "model": model},
        )
    return {"status": "ok", "ollama_ready": True, "model": model}
