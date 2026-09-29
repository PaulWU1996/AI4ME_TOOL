import json
import os
import sys

from consts import transcript_text_file
from utils import get_transcript, save_to_shared_disk


def run(payload):
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]

    _, segments = get_transcript(file_path)
    text = " ".join(
        segment.get("text", "").strip()
        for segment in segments
        if segment.get("text", "").strip()
    )

    save_to_shared_disk(job_id, transcript_text_file, text)
    return {"file_path": os.path.join(os.path.dirname(file_path), transcript_text_file)}


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin))))
