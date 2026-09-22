#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tqdm import tqdm

import cv2
import librosa
import numpy as np
import torch
from PIL import Image
from transformers import AutoModelForImageTextToText, AutoProcessor


# from extract_timed_media_list import Event, WINDOW_END, WINDOW_START, load_events


AUDIO_CAPABLE_MODELS = {
    "google/gemma-4-E2B-it",
    "google/gemma-4-E4B-it",
}
AVAILABLE_PROCESSES = ("visual", "audio", "transcript")


@dataclass
class LoadedModel:
    model_id: str
    processor: Any
    model: Any
    supports_audio: bool


def debug_log(message: str) -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    print(f"[{ts}] {message}", file=sys.stderr, flush=True)


def run_cmd_capture(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


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
    proc = run_cmd_capture(cmd)
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def load_model(model_id: str, hf_token: Optional[str]) -> LoadedModel:
    debug_log(f"Loading model '{model_id}'...")
    t0 = time.monotonic()
    processor = AutoProcessor.from_pretrained(model_id, token=hf_token)
    debug_log(f"Processor for '{model_id}' loaded. Loading model weights...")
    model = AutoModelForImageTextToText.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        token=hf_token,
    )
    elapsed = time.monotonic() - t0
    debug_log(f"Model '{model_id}' ready in {elapsed:.1f}s")
    return LoadedModel(
        model_id=model_id,
        processor=processor,
        model=model,
        supports_audio=model_id in AUDIO_CAPABLE_MODELS,
    )


def extract_frames(
    video_path: Path,
    frames_per_chunk: int,
    chunk_seconds: float,
    max_chunks: int,
    max_total_frames: int,
    clip_start: Optional[float] = None,
    clip_end: Optional[float] = None,
) -> Tuple[List[Image.Image], List[float], float]:
    debug_log(f"Opening video for frame extraction: {video_path}")
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if total_frames <= 0:
        cap.release()
        raise RuntimeError(f"Video appears to have zero frames: {video_path}")

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
        raise RuntimeError(
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

    for chunk_idx, (start_idx, end_idx) in enumerate(
        tqdm(chunk_ranges, desc="Extract video chunks", unit="chunk")
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
        raise RuntimeError(f"Could not decode sampled frames from: {video_path}")
    debug_log(f"Frame extraction complete: sampled {len(frames)} frames")
    return frames, timestamps, duration_seconds


def extract_audio_for_model(
    video_path: Path,
    start_seconds: Optional[float] = None,
    end_seconds: Optional[float] = None,
    max_seconds: Optional[float] = None,
) -> Optional[np.ndarray]:
    debug_log("Checking whether clip has an audio stream...")
    if not ffprobe_has_audio_stream(video_path):
        debug_log("No audio stream detected.")
        return None

    clip_start = 0.0 if start_seconds is None else float(start_seconds)
    if clip_start < 0:
        raise RuntimeError(f"Invalid audio start time: {clip_start}")

    if end_seconds is None:
        clip_duration = None
    else:
        clip_end = float(end_seconds)
        if clip_end <= clip_start:
            raise RuntimeError(
                f"Invalid audio time window: start={clip_start:.3f}s, end={clip_end:.3f}s"
            )
        clip_duration = clip_end - clip_start

    # Gemma 4 audio context is limited, so cap audio duration explicitly when requested.
    if max_seconds is not None:
        cap_seconds = float(max_seconds)
        if cap_seconds <= 0:
            raise RuntimeError(f"Invalid max_seconds value: {cap_seconds}")
        clip_duration = cap_seconds if clip_duration is None else min(clip_duration, cap_seconds)

    duration_msg = "full duration" if clip_duration is None else f"{clip_duration:.1f}s"
    debug_log(
        "Extracting audio at 16kHz "
        f"from {clip_start:.2f}s (duration {duration_msg})..."
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
        raise RuntimeError("FFmpeg is required for audio extraction.") from exc

    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"FFmpeg audio extraction failed: {error}")

    audio_array, _ = librosa.load(
        io.BytesIO(proc.stdout),
        sr=16000,
        mono=True,
    )
    if audio_array is None or audio_array.size == 0:
        debug_log("Audio extraction returned no samples.")
        return None
    debug_log(f"Audio extraction complete: {audio_array.size} samples")
    return audio_array


def build_content(
    frames: List[Image.Image],
    frame_timestamps: List[float],
    prompt_text: str,
    audio_array: Optional[np.ndarray],
    supports_audio: bool,
) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = []
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
    gen_elapsed = time.monotonic() - gen_t0
    debug_log(f"Generation complete in {gen_elapsed:.1f}s")

    generated = outputs[0][inputs["input_ids"].shape[-1] :]
    decoded = loaded.processor.decode(generated, skip_special_tokens=True)
    debug_log(f"Decoded output length: {len(decoded)} characters")
    return decoded


def extract_json_object(raw: str) -> Dict[str, Any]:
    debug_log("Extracting JSON object from model response...")
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("Model did not return a JSON object.")
    candidate = raw[start : end + 1]
    try:
        parsed = json.loads(candidate)
        debug_log("JSON parse succeeded without repair.")
        return parsed
    except json.JSONDecodeError:
        pass
    # Fallback: attempt repair for truncated or slightly malformed JSON
    try:
        from json_repair import repair_json
        repaired = repair_json(candidate, return_objects=True)
        if isinstance(repaired, dict):
            debug_log("JSON parse required repair and succeeded.")
            return repaired
        raise RuntimeError("json_repair did not return a dict.")
    except Exception as exc:
        raise RuntimeError(f"Model returned invalid JSON (repair failed): {exc}") from exc



def remap_legacy_media_path(path: Path, root: Path) -> Optional[Path]:
    if not path.is_absolute():
        return None

    path_posix = path.as_posix()
    for legacy_root in KNOWN_MEDIA_ROOTS:
        legacy_posix = Path(legacy_root).as_posix().rstrip("/")
        if path_posix == legacy_posix or path_posix.startswith(legacy_posix + "/"):
            rel = path_posix[len(legacy_posix) :].lstrip("/")
            return (root / rel).resolve()
    return None


def resolve_media_path(path_value: str, root: Path) -> Optional[Path]:
    p = Path(path_value)
    candidates: List[Path] = []

    if p.is_absolute():
        candidates.append(p)
        remapped = remap_legacy_media_path(p, root)
        if remapped is not None:
            candidates.append(remapped)
    else:
        candidates.append((root / p).resolve())

    seen = set()
    for candidate in candidates:
        key = candidate.as_posix()
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    return None




def load_video_events_from_ordered_list(events_path: Path, root: Path) -> List[Event]:
    if not events_path.exists():
        raise RuntimeError(
            "Ordered events file not found: "
            f"{events_path}. Run extract_timed_media_list.py first, "
            "or pass --rebuild-events-from-metadata."
        )

    with events_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    items = payload.get("events", [])
    if not isinstance(items, list):
        raise RuntimeError(f"Invalid ordered events format in: {events_path}")

    events: List[Event] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("media_type") != "video":
            continue

        path_value = item.get("path")
        source_id = item.get("source_id")
        start_str = item.get("start")
        end_str = item.get("end")
        if not all(isinstance(v, str) for v in [path_value, source_id, start_str, end_str]):
            continue

        abs_path = resolve_media_path(path_value, root)
        if abs_path is None:
            continue

        try:
            start = datetime.fromisoformat(start_str)
            end = datetime.fromisoformat(end_str)
        except ValueError:
            continue

        if end <= start:
            continue
        events.append(
            Event(
                path=abs_path,
                media_type="video",
                source_id=source_id,
                start=start,
                end=end,
            )
        )

    if not events:
        raise RuntimeError(f"No valid video events found in ordered events file: {events_path}")
    return events


def event_metadata(event: Event, root: Path) -> Dict[str, Any]:
    path_str = str(event.path)
    try:
        path_str = str(event.path.relative_to(root))
    except ValueError:
        pass
    return {
        "path": path_str,
        "source_id": event.source_id,
        "event_start": event.start.isoformat(),
        "event_end": event.end.isoformat(),
        "event_duration_seconds": (event.end - event.start).total_seconds(),
    }


def build_analysis_prompt(
    clip_has_audio: bool,
    overarching_description: Optional[str],
    preceding_narrative: Optional[str],
    processes: set[str],
) -> str:
    overarching_description_hint = ""
    if overarching_description:
        overarching_description_hint = (
        f"The clip is from a TV programme. The programme's description is: {overarching_description}\n"
        )
    preceding_narrative_hint = ""
    if preceding_narrative:
        preceding_narrative_hint = (
        f"The description produced by you when describing the preceding events from the episode is: {preceding_narrative}\n"
        )
    audio_requested = bool(processes & {"audio", "transcript"})
    if audio_requested:
        audio_hint = "Audio input is present." if clip_has_audio else "No audio input is available."
    else:
        audio_hint = "Audio analysis is not requested."

    schema: Dict[str, Any] = {}
    if "visual" in processes:
        schema["narrative"] = (
            "detailed description about what happens in the scene, the setting and the people present."
        )
    audio_schema = {}
    if "transcript" in processes:
        audio_schema["transcript"] = "best-effort transcript or empty string"
    if "audio" in processes:
        audio_schema["audio_narrative"] = (
            "detailed description about what happens in the clip, making use of spoken tone and other sounds"
        )
    if audio_schema:
        schema["audio_analysis"] = audio_schema

    return (
        "You are a television producer watching a TV show and logging the details of what happens in the show.\n"
        "You are analyzing one clip extracted from a larger episode.\n"
        f"{overarching_description_hint}"
        f"{preceding_narrative_hint}"
        f"{audio_hint}\n"
        "Return JSON only with this schema:\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n"
        "Rules:"
        " Use British English spelling, grammar and terms. "
    )


def normalize_audio_analysis_fields(analysis_json: Dict[str, Any], clip_has_audio: bool) -> Dict[str, Any]:
    if not clip_has_audio:
        analysis_json["audio_analysis"] = {
            "transcript": "N/A",
            "notable_sounds": ["N/A"],
            "speaker_tone": "N/A",
            "audio_context_notes": "N/A",
        }
        return analysis_json

    aa = analysis_json.get("audio_analysis")
    if not isinstance(aa, dict):
        aa = {}
    aa.setdefault("transcript", "")
    aa.setdefault("notable_sounds", [])
    aa.setdefault("speaker_tone", "")
    aa.setdefault("audio_context_notes", "")
    analysis_json["audio_analysis"] = aa
    return analysis_json

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze one video clip with Gemma and emit JSON to stdout"
    )
    parser.add_argument("video_path", help="Path to the input video file")
    # parser.add_argument(
    #     "--stage",
    #     choices=["all", "analyze", "predict"],
    #     default="all",
    #     help="Run only clip analysis or include backward/forward predictions",
    # )
    parser.add_argument(
        "--model-id",
        default="google/gemma-4-E4B-it",
        help="Primary model id (E4B default for lower memory usage)",
    )
    parser.add_argument(
        "--audio-model-id",
        default="google/gemma-4-E4B-it",
        help="Fallback audio-capable Gemma model used if --model-id lacks audio input",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=16,
        help="Frames sampled per chunk (legacy default was a single 16-frame pass)",
    )
    parser.add_argument(
        "--chunk-seconds",
        type=float,
        default=30.0,
        help="Target chunk span in seconds for long clips",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=8,
        help="Maximum chunks sampled across one clip",
    )
    parser.add_argument(
        "--max-total-frames",
        type=int,
        default=96,
        help="Hard cap on total sampled frames passed to model",
    )
    parser.add_argument(
        "--audio-max-seconds",
        type=float,
        default=30.0,
        help="Max audio seconds to pass to model",
    )
    parser.add_argument(
        "--clip-start",
        type=float,
        default=None,
        help="Optional clip start time in seconds (defaults to 0.0)",
    )
    parser.add_argument(
        "--clip-end",
        type=float,
        default=None,
        help="Optional clip end time in seconds (defaults to end of video)",
    )
    parser.add_argument(
        "--shot-detection",
        type=str,
        default=None,
        help="Optional shot detection",
    )
    parser.add_argument(
        "--shot-threshold",
        type=float,
        default=27.0,
        help="ContentDetector sensitivity; higher = fewer/longer scenes",
    )
    parser.add_argument(
        "--shot-min-scene-len",
        type=float,
        default=2,
        help="Minimum scene length in seconds before a cut boundary is accepted",
    )
    parser.add_argument(
        "--shot-max-scene-seconds",
        type=float,
        default=30,
        help="Optional cap on scene length in seconds; longer scenes are split into equal sub-chunks",
    )
    parser.add_argument("--seed", type=int, default=None, help="Optional seed for prediction style variance")
    parser.add_argument(
        "--overarching-narrative",
        default=None,
        help="Optional journalist-provided narrative hypothesis",
    )
    parser.add_argument(
        "--hf-token",
        default=None,
        help="Optional Hugging Face token. Can also be set in HF_TOKEN env var.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output location for the analysis results.",
    )
    parser.add_argument(
        "--processes",
        nargs="+",
        choices=AVAILABLE_PROCESSES,
        default=list(AVAILABLE_PROCESSES),
        help=(
            "Analysis outputs to produce; selecting audio also enables transcript "
            "(default: visual audio transcript)"
        ),
    )
    return parser.parse_args()


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _split_shot(start_time: float, end_time: float, max_scene_seconds: Optional[float]) -> List[Dict[str, float]]:
    if not max_scene_seconds or (end_time - start_time) <= max_scene_seconds:
        return [{"start": start_time, "end": end_time}]
    chunks = []
    t = start_time
    while t < end_time:
        sub_end = min(t + max_scene_seconds, end_time)
        chunks.append({"start": t, "end": sub_end})
        t = sub_end
    return chunks


def _video_duration_seconds(video_path) -> float:
    cap = cv2.VideoCapture(str(video_path))
    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    finally:
        cap.release()
    return (total_frames / fps) if fps > 0 else 0.0


def detect_shots(video_path, shot_detection=None, threshold=27.0, min_scene_len=0.5, max_scene_seconds=None):
    from scenedetect import detect, ContentDetector
    scene_list = detect(str(video_path), ContentDetector(threshold=threshold, min_scene_len=f"{min_scene_len}s"))

    shots = []
    if not scene_list:
        # No scene boundaries detected; treat the whole video as a single shot.
        duration = _video_duration_seconds(video_path)
        shots.extend(_split_shot(0.0, duration, max_scene_seconds))
    else:
        for scene in scene_list:
            start_time = scene[0].get_seconds()
            end_time = scene[1].get_seconds()
            shots.extend(_split_shot(start_time, end_time, max_scene_seconds))


    #print out all the shots detected
    for i, shot in enumerate(shots):
        print(f"Shot {i}: start={shot['start']:.2f}s, end={shot['end']:.2f}s")
    return shots



def main() -> int:
    args = parse_args()
    selected_processes = set(args.processes)
    if "audio" in selected_processes:
        selected_processes.add("transcript")
    use_audio = bool(selected_processes & {"audio", "transcript"})
    use_visual = "visual" in selected_processes
    video_path = Path(args.video_path).expanduser().resolve()
    if not video_path.exists():
        raise RuntimeError(f"Input video not found: {video_path}")
    if not video_path.is_file():
        raise RuntimeError(f"Input path is not a file: {video_path}")

    hf_token = args.hf_token or os.getenv("HF_TOKEN") or None
    if isinstance(hf_token, str):
        hf_token = hf_token.strip() or None
    primary_model_id = args.model_id
    audio_model_id = args.audio_model_id

    # if args.stage == "predict":
    #     raise RuntimeError("--stage predict is no longer supported in this script; use --stage analyze or --stage all")

    primary = load_model(primary_model_id, hf_token=hf_token)
    debug_log("Loading primary Gemma model...")


    audio_array = extract_audio_for_model(video_path, 0, 5) if use_audio else None
    clip_has_audio = use_audio and audio_array is not None
    debug_log(f"Audio detected: {clip_has_audio}")

    audio_runner = primary
    if clip_has_audio and not primary.supports_audio:
        debug_log(
            "Primary model does not support audio input; "
            f"loading audio-capable model {audio_model_id} for audio-aware analysis..."
        )
        audio_runner = load_model(audio_model_id, hf_token=hf_token)




    shot_list = []
    if args.shot_detection == "detect":
        #Do the shot detection
        shot_list = detect_shots(
            video_path,
            args.shot_detection,
            threshold=args.shot_threshold,
            min_scene_len=args.shot_min_scene_len,
            max_scene_seconds=args.shot_max_scene_seconds,
        )
    elif args.shot_detection == "test":
        shot_list.append({"start": 0, "end": 20})
        shot_list.append({"start": 20, "end": 40})
        shot_list.append({"start": 40, "end": None})
    else:
        shot_list.append({"start": args.clip_start, "end": args.clip_end})  # No shot detection, single shot



    output_narrative_list = []
    output_audio_narrative_list = []
    output_transcript_list = []



    for shot in shot_list:


        debug_log(f"Analyzing clip: {video_path}")
        if use_visual:
            frames, frame_timestamps, decoded_duration = extract_frames(
                video_path,
                frames_per_chunk=args.frames,
                chunk_seconds=args.chunk_seconds,
                max_chunks=args.max_chunks,
                max_total_frames=args.max_total_frames,
                clip_start=shot["start"],
                clip_end=shot["end"],
            )
        else:
            frames, frame_timestamps, decoded_duration = [], [], 0.0

        audio_array = None
        if use_audio:
            audio_array = extract_audio_for_model(
                video_path,
                start_seconds=shot["start"],
                end_seconds=shot["end"],
                max_seconds=args.audio_max_seconds,
            )

        analysis_prompt = build_analysis_prompt(
            clip_has_audio=clip_has_audio,
            overarching_description=None,
            preceding_narrative=None,
            processes=selected_processes,
        )
        debug_log("Generating detailed clip narrative...")
        narrative_content = build_content(
            frames=frames,
            frame_timestamps=frame_timestamps,
            prompt_text=analysis_prompt,
            audio_array=audio_array,
            supports_audio=audio_runner.supports_audio,
        )
        # print(narrative_content)
        # for item in narrative_content:
        #     if item["type"] == "image":
        #         print(f"Image: {item['image'].size}")
            # elif item["type"] == "text":
            #     print(f"Text: {item['text'][:60]}...")
            # elif item["type"] == "audio":
            #     print(f"Audio: {len(item['audio'])} samples")

        analysis_raw = run_generation(
            loaded=audio_runner,
            content=narrative_content,
            max_new_tokens=3000,
            temperature=0.45,
            top_p=0.9,
            do_sample=True,
        )
        analysis_json = extract_json_object(analysis_raw)
        if use_audio:
            analysis_json = normalize_audio_analysis_fields(analysis_json, clip_has_audio)
        debug_log("Analysis JSON normalized.")


        def format_timecode(seconds: float) -> str:
            if seconds is None:
                return "00:00:00.000"
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = seconds % 60
            return f"{hours:02}:{minutes:02}:{secs:06.3f}"

        start_timecode = format_timecode(shot["start"])
        end_timecode = format_timecode(shot["end"])
        audio_analysis = analysis_json.get("audio_analysis", {})

        if use_visual:
            output_narrative: Dict[str, Any] = {
                "start": start_timecode,
                "end": end_timecode,
                "caption": analysis_json.get("narrative", ""),
            }
            print(json.dumps(output_narrative, ensure_ascii=False, indent=2))
            output_narrative_list.append(output_narrative)

        if "audio" in selected_processes:
            output_audio_narrative: Dict[str, Any] = {
                "start": start_timecode,
                "end": end_timecode,
                "caption": audio_analysis.get("audio_narrative", ""),
            }
            print(json.dumps(output_audio_narrative, ensure_ascii=False, indent=2))
            output_audio_narrative_list.append(output_audio_narrative)

        if "transcript" in selected_processes:
            output_transcript: Dict[str, Any] = {
                "start": start_timecode,
                "end": end_timecode,
                "transcript": audio_analysis.get("transcript", ""),
            }
            print(json.dumps(output_transcript, ensure_ascii=False, indent=2))
            output_transcript_list.append(output_transcript)

        # output_payload: Dict[str, Any] = {
        #     "created_at": datetime.utcnow().isoformat() + "Z",
        #     "selected_clip": {
        #         "path": str(video_path),
        #         "clip_decode_duration_seconds": decoded_duration,
        #     },
        #     "frame_timestamps_seconds": [round(t, 3) for t in frame_timestamps],
        #     "audio_used": clip_has_audio and audio_runner.supports_audio,
        #     "audio_detected": clip_has_audio,
        #     "models": {
        #         "primary_model": primary.model_id,
        #         "audio_analysis_model": audio_runner.model_id,
        #         "primary_supports_audio": primary.supports_audio,
        #     },
        #     "analysis": analysis_json,
        # }

        # debug_log("Writing final JSON payload to stdout.")
        # print(json.dumps(output_payload, ensure_ascii=False, indent=2))


        if args.output:
            output_path = Path(args.output).expanduser().resolve()
            if use_visual:
                write_json(Path(str(output_path) + "_gemma_visual_output.json"), output_narrative_list)
            if "audio" in selected_processes:
                write_json(Path(str(output_path) + "_gemma_audio_output.json"), output_audio_narrative_list)
            if "transcript" in selected_processes:
                write_json(Path(str(output_path) + "_gemma_transcript_output.json"), output_transcript_list)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        if use_visual:
            write_json(Path(str(output_path) + "_gemma_visual_output.json"), output_narrative_list)
        if "audio" in selected_processes:
            write_json(Path(str(output_path) + "_gemma_audio_output.json"), output_audio_narrative_list)
        if "transcript" in selected_processes:
            write_json(Path(str(output_path) + "_gemma_transcript_output.json"), output_transcript_list)




    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)