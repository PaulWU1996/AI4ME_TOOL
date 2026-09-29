import json
import os
import sys

from utils import get_transcript, save_to_shared_disk


def run(payload):
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]

    _, segments = get_transcript(file_path)
    start = segments[0].get("startMs", 0)
    end = segments[-1].get("endMs", 0)

    if start > end:
        start = end

    stem = os.path.splitext(os.path.basename(file_path))[0]
    save_to_shared_disk(job_id, f"{stem}_extent_output.json", {"start": start, "end": end})

    return {"start": start, "end": end}


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin))))
