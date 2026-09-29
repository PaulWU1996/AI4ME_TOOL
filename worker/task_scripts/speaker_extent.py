import json
import os
import sys

from utils import get_speaker_turn_boundary_ms, get_transcript, save_to_shared_disk


def run(payload):
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]

    transcript, segments = get_transcript(file_path)
    start = get_speaker_turn_boundary_ms(segments, 0, "forward")
    end = get_speaker_turn_boundary_ms(segments, len(segments) - 1, "backward")

    if start > end:
        start = segments[0]["startMs"]
        end = segments[-1]["endMs"]

    transcript["segments"] = [
        segment
        for segment in segments
        if segment["endMs"] > start and segment["startMs"] < end
    ]

    stem = os.path.splitext(os.path.basename(file_path))[0]
    trimmed_name = f"{stem}_trimmed.json"
    save_to_shared_disk(job_id, trimmed_name, transcript)
    save_to_shared_disk(job_id, f"{stem}_extent_output.json", {"start": start, "end": end})

    return {
        "file_path": os.path.join(os.path.dirname(file_path), trimmed_name),
        "start": start,
        "end": end,
    }


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin))))
