"""Gemma video analysis pipeline and model lifecycle for the transcription service.

Holds every step that turns a video file into narrative / audio-narrative /
transcript segments: frame extraction, audio extraction, prompt assembly,
generation and JSON recovery. The FastAPI router in `app.routers.process`
drives it, and `analyze_with_gemma.py` drives it from the command line, so both
entry points share one implementation.

Sampling knobs (frame counts, chunk spans, generation parameters) come from the
environment via `Settings`; the request supplies only the job's own parameters.
"""

from __future__ import annotations

import io
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import librosa
import numpy as np
import torch
from PIL import Image
from tqdm import tqdm
from transformers import AutoModelForImageTextToText, AutoProcessor

PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "analysis"

AUDIO_CAPABLE_MODELS = {
    "google/gemma-4-E2B-it",
    "google/gemma-4-E4B-it",
}

_MISSING = object()


class AnalysisError(RuntimeError):
    """Analysis failed for a reason that is not the caller's fault."""


class InvalidVideoError(AnalysisError):
    """The supplied video cannot be opened or decoded."""


@dataclass
class Settings:
    model_id: str
    audio_model_id: str
    hf_token: Optional[str]
    frames_per_chunk: int
    chunk_seconds: float
    max_chunks: int
    max_total_frames: int
    audio_max_seconds: float
    max_shots: int
    max_new_tokens: int
    temperature: float
    top_p: float
    do_sample: bool

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            model_id=os.environ.get("MODEL_ID", "google/gemma-4-E4B-it"),
            audio_model_id=os.environ.get("AUDIO_MODEL_ID", "google/gemma-4-E4B-it"),
            hf_token=os.environ.get("HF_TOKEN") or None,
            frames_per_chunk=_env_int("FRAMES_PER_CHUNK", 16),
            chunk_seconds=_env_float("CHUNK_SECONDS", 30.0),
            max_chunks=_env_int("MAX_CHUNKS", 8),
            max_total_frames=_env_int("MAX_TOTAL_FRAMES", 96),
            audio_max_seconds=_env_float("AUDIO_MAX_SECONDS", 30.0),
            max_shots=_env_int("MAX_SHOTS", 0),
            max_new_tokens=_env_int("MAX_NEW_TOKENS", 3000),
            temperature=_env_float("TEMPERATURE", 0.45),
            top_p=_env_float("TOP_P", 0.9),
            do_sample=_env_bool("DO_SAMPLE", True),
        )


@dataclass
class LoadedModel:
    model_id: str
    processor: Any
    model: Any
    supports_audio: bool


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, _MISSING)
    if value is _MISSING:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name}={value!r} is not an integer") from exc


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, _MISSING)
    if value is _MISSING:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name}={value!r} is not a number") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, _MISSING)
    if value is _MISSING:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read the environment once per process and cache the result."""
    return Settings.from_env()


def apply_overrides(**overrides) -> Settings:
    """Override cached settings in place. Used by the CLI, which runs once."""
    settings = get_settings()
    for key, value in overrides.items():
        if value is not None:
            setattr(settings, key, value)
    return settings


def debug_log(message: str) -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    print(f"[{ts}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Model lifecycle
# --------------------------------------------------------------------------

_model_cache: Dict[str, LoadedModel] = {}
_model_lock = threading.Lock()


def load_model(model_id: str, hf_token: Optional[str]) -> LoadedModel:
    """Load `model_id`, reusing already-loaded weights for the same id.

    Keyed by id because the audio-capable fallback model is usually the same
    checkpoint as the primary -- without the cache a single job would pay for
    two full weight loads.
    """
    with _model_lock:
        cached = _model_cache.get(model_id)
        if cached is not None:
            debug_log(f"Reusing already-loaded model '{model_id}'.")
            return cached

        debug_log(f"Loading model '{model_id}'...")
        t0 = time.monotonic()
        try:
            processor = AutoProcessor.from_pretrained(model_id, token=hf_token)
            debug_log(f"Processor for '{model_id}' loaded. Loading model weights...")
            model = AutoModelForImageTextToText.from_pretrained(
                model_id,
                torch_dtype=torch.bfloat16,
                device_map="auto",
                token=hf_token,
            )
        except Exception as exc:
            raise AnalysisError(f"Could not load model '{model_id}': {exc}") from exc

        loaded = LoadedModel(
            model_id=model_id,
            processor=processor,
            model=model,
            supports_audio=model_id in AUDIO_CAPABLE_MODELS,
        )
        _model_cache[model_id] = loaded
        debug_log(f"Model '{model_id}' ready in {time.monotonic() - t0:.1f}s")
        return loaded


def warm_up() -> LoadedModel:
    """Load the primary model and the audio model, so no request pays for it."""
    settings = get_settings()
    primary = load_model(settings.model_id, settings.hf_token)
    if settings.audio_model_id != settings.model_id:
        load_model(settings.audio_model_id, settings.hf_token)
    return primary


def is_ready() -> bool:
    settings = get_settings()
    with _model_lock:
        return settings.model_id in _model_cache


def model_status() -> Dict[str, Any]:
    settings = get_settings()
    with _model_lock:
        loaded = sorted(_model_cache)
    return {
        "model": settings.model_id,
        "audio_model": settings.audio_model_id,
        "loaded_models": loaded,
    }


# --------------------------------------------------------------------------
# Media inspection
# --------------------------------------------------------------------------

def ffprobe_has_audio_stream(path: Path) -> bool:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(path),
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True)
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def probe_duration_seconds(video_path: Path) -> float:
    """Return the container duration in seconds, or 0.0 if it cannot be read."""
    cap = cv2.VideoCapture(str(video_path))
    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    finally:
        cap.release()
    if total_frames <= 0 or fps <= 0:
        return 0.0
    return total_frames / fps


def detect_shots(video_path: Path) -> List[Dict[str, Optional[float]]]:
    from scenedetect import detect, ContentDetector

    scene_list = detect(str(video_path), ContentDetector())
    shots: List[Dict[str, Optional[float]]] = []
    for scene in scene_list:
        start = scene[0].get_seconds()
        end = scene[1].get_seconds()
        debug_log(f"Shot {len(shots)}: start={start:.2f}s, end={end:.2f}s")
        shots.append({"start": start, "end": end})
    return shots


# --------------------------------------------------------------------------
# Frame and audio extraction
# --------------------------------------------------------------------------


def extract_frames(
    video_path: Path,
    frames_per_chunk: int,
    chunk_seconds: float,
    max_chunks: int,
    max_total_frames: int,
    clip_start: Optional[float],
    clip_end: Optional[float],
) -> Tuple[List[Image.Image], List[float], float]:
    debug_log(f"Opening video for frame extraction: {video_path}")
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise InvalidVideoError(f"Could not open video: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if total_frames <= 0:
        cap.release()
        raise InvalidVideoError(f"Video appears to have zero frames: {video_path}")

    video_duration_seconds = (total_frames / fps) if fps > 0 else 0.0
    if video_duration_seconds <= 0:
        clip_start_seconds = 0.0
        clip_end_seconds = 0.0
    else:
        clip_start_seconds = 0.0 if clip_start is None else float(clip_start)
        clip_end_seconds = video_duration_seconds if clip_end is None else float(clip_end)

        clip_start_seconds = max(0.0, min(clip_start_seconds, video_duration_seconds))
        clip_end_seconds = max(0.0, min(clip_end_seconds, video_duration_seconds))

    if clip_end_seconds <= clip_start_seconds:
        cap.release()
        raise AnalysisError(
            "Invalid clip time window: "
            f"clip_start={clip_start_seconds:.3f}s, clip_end={clip_end_seconds:.3f}s"
        )

    duration_seconds = clip_end_seconds - clip_start_seconds
    if duration_seconds <= 0:
        num_chunks = 1
    else:
        estimated_chunks = max(1, int(math.ceil(duration_seconds / max(chunk_seconds, 1.0))))
        num_chunks = min(max_chunks, estimated_chunks)

    clip_start_frame = int(clip_start_seconds * fps) if fps > 0 else 0
    clip_end_frame = int(clip_end_seconds * fps) - 1 if fps > 0 else total_frames - 1
    clip_start_frame = max(0, min(clip_start_frame, total_frames - 1))
    clip_end_frame = max(clip_start_frame, min(clip_end_frame, total_frames - 1))

    debug_log(
        "Frame sampling plan: "
        f"clip_start={clip_start_seconds:.2f}s, clip_end={clip_end_seconds:.2f}s, "
        f"duration={duration_seconds:.1f}s, chunks={num_chunks}, "
        f"frames_per_chunk={frames_per_chunk}, max_total_frames={max_total_frames}"
    )

    frames: List[Image.Image] = []
    timestamps: List[float] = []
    seen_indices = set()

    if num_chunks == 1:
        chunk_ranges = [(clip_start_frame, clip_end_frame)]
    else:
        chunk_ranges = []
        for chunk_idx in range(num_chunks):
            start_t = clip_start_seconds + (chunk_idx / num_chunks) * duration_seconds
            end_t = clip_start_seconds + ((chunk_idx + 1) / num_chunks) * duration_seconds
            start_frame = int(start_t * fps)
            end_frame = int(end_t * fps) - 1
            start_frame = max(clip_start_frame, min(start_frame, clip_end_frame))
            end_frame = max(start_frame, min(end_frame, clip_end_frame))
            chunk_ranges.append((start_frame, end_frame))

    for start_idx, end_idx in tqdm(
        chunk_ranges, desc="Extract video chunks", unit="chunk", leave=False
    ):
        sampled_indices = np.linspace(start_idx, end_idx, frames_per_chunk, dtype=int)
        for idx in sampled_indices:
            idx_int = int(idx)
            if idx_int in seen_indices:
                continue
            seen_indices.add(idx_int)
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx_int)
            ret, frame = cap.read()
            if not ret:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(rgb))
            ts = ((idx_int / max(fps, 1e-6)) - clip_start_seconds) if fps > 0 else 0.0
            timestamps.append(ts)

    # Cap context size for multimodal inference cost/latency.
    if len(frames) > max_total_frames:
        keep = np.linspace(0, len(frames) - 1, max_total_frames, dtype=int)
        frames = [frames[i] for i in keep]
        timestamps = [timestamps[i] for i in keep]

    cap.release()
    if not frames:
        raise InvalidVideoError(f"Could not decode sampled frames from: {video_path}")
    debug_log(f"Frame extraction complete: sampled {len(frames)} frames")
    return frames, timestamps, duration_seconds


def extract_audio_for_model(
    video_path: Path,
    start_seconds: Optional[float] = None,
    end_seconds: Optional[float] = None,
    max_seconds: Optional[float] = None,
) -> Optional[np.ndarray]:
    if not ffprobe_has_audio_stream(video_path):
        debug_log("No audio stream detected.")
        return None

    clip_start = 0.0 if start_seconds is None else float(start_seconds)
    if clip_start < 0:
        raise AnalysisError(f"Invalid audio start time: {clip_start}")

    if end_seconds is None:
        clip_duration = None
    else:
        clip_end = float(end_seconds)
        if clip_end <= clip_start:
            raise AnalysisError(
                f"Invalid audio time window: start={clip_start:.3f}s, end={clip_end:.3f}s"
            )
        clip_duration = clip_end - clip_start

    # Gemma audio context is limited, so cap audio duration explicitly.
    if max_seconds is not None:
        cap_seconds = float(max_seconds)
        if cap_seconds <= 0:
            raise AnalysisError(f"Invalid max_seconds value: {cap_seconds}")
        clip_duration = cap_seconds if clip_duration is None else min(clip_duration, cap_seconds)

    duration_msg = "full duration" if clip_duration is None else f"{clip_duration:.1f}s"
    debug_log(
        f"Extracting audio at 16kHz from {clip_start:.2f}s (duration {duration_msg})..."
    )

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(clip_start),
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
    ]
    if clip_duration is not None:
        ffmpeg_cmd.extend(["-t", str(clip_duration)])
    ffmpeg_cmd.extend(["-f", "wav", "pipe:1"])

    try:
        proc = subprocess.run(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise AnalysisError("FFmpeg is required for audio extraction.") from exc

    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="replace").strip()
        raise AnalysisError(f"FFmpeg audio extraction failed: {error}")

    audio_array, _ = librosa.load(io.BytesIO(proc.stdout), sr=16000, mono=True)
    if audio_array is None or audio_array.size == 0:
        debug_log("Audio extraction returned no samples.")
        return None
    debug_log(f"Audio extraction complete: {audio_array.size} samples")
    return audio_array


# --------------------------------------------------------------------------
# Prompt and generation
# --------------------------------------------------------------------------


def build_context(overarching_description: Optional[str], clip_has_audio: bool) -> str:
    """Assemble the per-request context block that precedes the media."""
    lines: List[str] = []
    if overarching_description:
        lines.append(
            "The clip is from a TV programme. "
            f"The programme's description is: {overarching_description}"
        )
    lines.append(
        "Audio input is present." if clip_has_audio else "No audio input is available."
    )
    return "\n".join(lines)


def build_instructions(language: str) -> str:
    """Render the requirements + fixed output schema from `app/prompts/analysis`."""
    requirements = (PROMPTS_DIR / "transcript.txt").read_text(encoding="utf-8")
    output_structure = (PROMPTS_DIR / "output_structure.txt").read_text(encoding="utf-8")
    return (
        requirements.format_map({"language": language}).strip()
        + "\n\n"
        + output_structure.strip()
        + "\n\nProduce the JSON output now."
    )


def build_content(
    frames: List[Image.Image],
    frame_timestamps: List[float],
    context_text: str,
    prompt_text: str,
    audio_array: Optional[np.ndarray],
    supports_audio: bool,
) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": context_text}]
    for frame, ts in zip(frames, frame_timestamps):
        content.append({"type": "image", "image": frame})
        content.append({"type": "text", "text": f"[frame_timestamp={ts:.2f}s]"})

    if audio_array is not None and supports_audio:
        content.append({"type": "audio", "audio": audio_array})

    content.append({"type": "text", "text": prompt_text})
    return content


def run_generation(
    loaded: LoadedModel,
    content: List[Dict[str, Any]],
    max_new_tokens: int,
    temperature: float,
    top_p: float,
    do_sample: bool,
) -> str:
    debug_log("Preparing multimodal prompt tensors...")
    t0 = time.monotonic()
    messages = [{"role": "user", "content": content}]
    inputs = loaded.processor.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_tensors="pt",
        return_dict=True,
    ).to(loaded.model.device)
    prep_elapsed = time.monotonic() - t0
    input_tokens = int(inputs["input_ids"].shape[-1]) if "input_ids" in inputs else -1
    debug_log(
        f"Prompt prepared in {prep_elapsed:.1f}s; input_tokens={input_tokens}. "
        "Starting generation..."
    )

    gen_t0 = time.monotonic()
    with torch.inference_mode():
        outputs = loaded.model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            do_sample=do_sample,
        )
    debug_log(f"Generation complete in {time.monotonic() - gen_t0:.1f}s")

    generated = outputs[0][inputs["input_ids"].shape[-1] :]
    decoded = loaded.processor.decode(generated, skip_special_tokens=True)
    debug_log(f"Decoded output length: {len(decoded)} characters")
    return decoded


def extract_json_object(raw: str) -> Dict[str, Any]:
    debug_log("Extracting JSON object from model response...")
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AnalysisError("Model did not return a JSON object.")
    candidate = raw[start : end + 1]
    try:
        parsed = json.loads(candidate)
        debug_log("JSON parse succeeded without repair.")
        return parsed
    except json.JSONDecodeError:
        pass
    # Fallback: attempt repair for truncated or slightly malformed JSON.
    try:
        from json_repair import repair_json

        repaired = repair_json(candidate, return_objects=True)
        if isinstance(repaired, dict):
            debug_log("JSON parse required repair and succeeded.")
            return repaired
        raise AnalysisError("json_repair did not return a dict.")
    except AnalysisError:
        raise
    except Exception as exc:
        raise AnalysisError(f"Model returned invalid JSON (repair failed): {exc}") from exc


def normalize_audio_analysis_fields(
    analysis_json: Dict[str, Any], clip_has_audio: bool
) -> Dict[str, Any]:
    """Guarantee the two `audio_analysis` keys the response contract promises."""
    if not clip_has_audio:
        analysis_json["audio_analysis"] = {"transcript": "", "audio_narrative": ""}
        return analysis_json

    audio_analysis = analysis_json.get("audio_analysis")
    if not isinstance(audio_analysis, dict):
        audio_analysis = {}
    audio_analysis.setdefault("transcript", "")
    audio_analysis.setdefault("audio_narrative", "")
    analysis_json["audio_analysis"] = audio_analysis
    return analysis_json


# --------------------------------------------------------------------------
# Shot planning
# --------------------------------------------------------------------------


def format_timecode(seconds: Optional[float]) -> str:
    if seconds is None:
        return "00:00:00.000"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02}:{minutes:02}:{secs:06.3f}"


def plan_shots(
    video_path: Path,
    duration_seconds: float,
    clip_start: Optional[float],
    clip_end: Optional[float],
    shot_detection: Optional[str],
    max_shots: int,
) -> List[Dict[str, Optional[float]]]:
    """Resolve the shot list, defaulting to one shot covering the whole window."""
    start = 0.0 if clip_start is None else float(clip_start)
    end = duration_seconds if clip_end is None else float(clip_end)

    if shot_detection == "test":
        shots: List[Dict[str, Optional[float]]] = [
            {"start": 0, "end": 20},
            {"start": 20, "end": 40},
            {"start": 40, "end": None},
        ]
    elif shot_detection == "detect":
        shots = detect_shots(video_path)
        if not shots:
            debug_log("Shot detection found no cuts; falling back to a single shot.")
            shots = [{"start": start, "end": end}]
    else:
        shots = [{"start": start, "end": end}]

    if max_shots and len(shots) > max_shots:
        debug_log(
            f"Shot limit reached: analysing the first {max_shots} of {len(shots)} detected shots."
        )
        shots = shots[:max_shots]

    return shots


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def analyze(
    video_path: Path,
    prompts: Optional[str] = None,
    language: str = "en",
    clip_start: Optional[float] = None,
    clip_end: Optional[float] = None,
    shot_detection: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyse `video_path` and return narrative, audio narrative and transcript.

    Blocking and GPU-bound; callers on an event loop must run it in a thread.
    """
    settings = get_settings()
    t0 = time.monotonic()

    primary = load_model(settings.model_id, settings.hf_token)
    duration_seconds = probe_duration_seconds(video_path)
    clip_has_audio = ffprobe_has_audio_stream(video_path)
    debug_log(f"Duration: {duration_seconds:.2f}s | audio detected: {clip_has_audio}")

    audio_runner = primary
    if clip_has_audio and not primary.supports_audio:
        debug_log(
            "Primary model does not support audio input; loading audio-capable model "
            f"{settings.audio_model_id} for audio-aware analysis."
        )
        audio_runner = load_model(settings.audio_model_id, settings.hf_token)

    shots = plan_shots(
        video_path=video_path,
        duration_seconds=duration_seconds,
        clip_start=clip_start,
        clip_end=clip_end,
        shot_detection=shot_detection,
        max_shots=settings.max_shots,
    )

    context_text = build_context(prompts, clip_has_audio)
    prompt_text = build_instructions(language)

    narratives: List[Dict[str, Any]] = []
    audio_narratives: List[Dict[str, Any]] = []
    transcripts: List[Dict[str, Any]] = []

    for index, shot in enumerate(shots):
        debug_log(f"Analyzing shot {index + 1}/{len(shots)}: {video_path}")
        frames, frame_timestamps, _ = extract_frames(
            video_path,
            frames_per_chunk=settings.frames_per_chunk,
            chunk_seconds=settings.chunk_seconds,
            max_chunks=settings.max_chunks,
            max_total_frames=settings.max_total_frames,
            clip_start=shot["start"],
            clip_end=shot["end"],
        )

        audio_array = extract_audio_for_model(
            video_path,
            start_seconds=shot["start"],
            end_seconds=shot["end"],
            max_seconds=settings.audio_max_seconds,
        )

        content = build_content(
            frames=frames,
            frame_timestamps=frame_timestamps,
            context_text=context_text,
            prompt_text=prompt_text,
            audio_array=audio_array,
            supports_audio=audio_runner.supports_audio,
        )

        raw = run_generation(
            loaded=audio_runner,
            content=content,
            max_new_tokens=settings.max_new_tokens,
            temperature=settings.temperature,
            top_p=settings.top_p,
            do_sample=settings.do_sample,
        )
        analysis = normalize_audio_analysis_fields(
            extract_json_object(raw), clip_has_audio
        )

        timecode = {
            "start": format_timecode(shot["start"]),
            "end": format_timecode(shot["end"] if shot["end"] is not None else duration_seconds),
        }
        audio_analysis = analysis.get("audio_analysis", {})
        narratives.append(
            {**timecode, "caption": analysis.get("narrative", "") or ""}
        )
        audio_narratives.append(
            {**timecode, "caption": audio_analysis.get("audio_narrative", "") or ""}
        )
        transcripts.append(
            {**timecode, "transcript": audio_analysis.get("transcript", "") or ""}
        )

    return {
        "narrative": narratives,
        "audio_narrative": audio_narratives,
        "transcript": transcripts,
        "audio_detected": clip_has_audio,
        "video_duration_seconds": round(duration_seconds, 3),
        "models": {
            "primary_model": primary.model_id,
            "audio_analysis_model": audio_runner.model_id,
            "primary_supports_audio": primary.supports_audio,
        },
        "processing_time_ms": int((time.monotonic() - t0) * 1000),
    }
