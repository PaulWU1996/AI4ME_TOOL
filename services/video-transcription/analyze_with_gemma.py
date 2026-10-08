#!/usr/bin/env python3
"""Command line front end for the Gemma video analysis pipeline.

The pipeline itself lives in `app/services/gemma_runner.py`, shared with the
FastAPI service. This wrapper exists so the analysis can still be run directly
on a file, e.g. inside the container or during development.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.services import gemma_runner


def parse_args() -> argparse.Namespace:
    settings = gemma_runner.get_settings()
    parser = argparse.ArgumentParser(
        description="Analyze a video with Gemma and emit JSON to stdout"
    )
    parser.add_argument("video_path", help="Path to the input video file")
    parser.add_argument(
        "--model-id",
        default=settings.model_id,
        help="Primary model id (E4B default for lower memory usage)",
    )
    parser.add_argument(
        "--audio-model-id",
        default=settings.audio_model_id,
        help="Fallback audio-capable Gemma model used if --model-id lacks audio input",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=settings.frames_per_chunk,
        help="Frames sampled per chunk",
    )
    parser.add_argument(
        "--chunk-seconds",
        type=float,
        default=settings.chunk_seconds,
        help="Target chunk span in seconds for long clips",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=settings.max_chunks,
        help="Maximum chunks sampled across one shot",
    )
    parser.add_argument(
        "--max-total-frames",
        type=int,
        default=settings.max_total_frames,
        help="Hard cap on total sampled frames passed to model",
    )
    parser.add_argument(
        "--max-shots",
        type=int,
        default=settings.max_shots,
        help="Maximum shots analysed; 0 = no limit",
    )
    parser.add_argument(
        "--audio-max-seconds",
        type=float,
        default=settings.audio_max_seconds,
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
        choices=["detect", "test"],
        help="Split the video into shots before analysis",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Output language, e.g. 'en', 'cy'",
    )
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
    return parser.parse_args()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    video_path = Path(args.video_path).expanduser().resolve()
    if not video_path.exists():
        raise RuntimeError(f"Input video not found: {video_path}")
    if not video_path.is_file():
        raise RuntimeError(f"Input path is not a file: {video_path}")

    gemma_runner.apply_overrides(
        model_id=args.model_id,
        audio_model_id=args.audio_model_id,
        hf_token=args.hf_token or os.environ.get("HF_TOKEN"),
        frames_per_chunk=args.frames,
        chunk_seconds=args.chunk_seconds,
        max_chunks=args.max_chunks,
        max_total_frames=args.max_total_frames,
        max_shots=args.max_shots,
        audio_max_seconds=args.audio_max_seconds,
    )

    result = gemma_runner.analyze(
        video_path,
        prompts=args.overarching_narrative,
        language=args.language,
        clip_start=args.clip_start,
        clip_end=args.clip_end,
        shot_detection=args.shot_detection,
    )

    for key in ("narrative", "audio_narrative", "transcript"):
        if key in result:
            print(json.dumps(result[key], ensure_ascii=False, indent=2))

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        if "narrative" in result:
            write_json(Path(str(output_path) + "_gemma_visual_output.json"), result["narrative"])
        write_json(Path(str(output_path) + "_gemma_audio_output.json"), result["audio_narrative"])
        write_json(Path(str(output_path) + "_gemma_transcript_output.json"), result["transcript"])

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except gemma_runner.AnalysisError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
