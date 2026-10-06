import json
import os
import sys
from bisect import bisect_right

from chonkie import SemanticChunker
from transcript import get_transcript
from utils import save_to_shared_disk

MIN_CHUNKS_TO_TRIM = 3
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

def get_semantic_range_ms(segments: list[dict]) -> tuple[int, int]:
    """The ms range between the first and last semantic chunk.

    Lines are joined with newlines so each segment keeps its own chunk
    boundary, and the chunker's character offsets are mapped back to segment
    indices with bisect. The first chunk is dropped whole (its last segment's
    start) and the last chunk is dropped whole (its first segment's end),
    which is what takes the intro and outro off the extent.
    """
    lines = [segment.get("text", "") for segment in segments]
    text = "\n".join(lines)

    # start offset of each line in the joined text
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1  # +1 for "\n"

    chunker = SemanticChunker(
        embedding_model=EMBEDDING_MODEL,
        threshold=0.9,
        chunk_size=1024,
        similarity_window=5,
        # min_sentences_per_chunk=8,   # hard floor: at least 8 lines per chunk
        # min_characters_per_sentence=1,
        delim=["\n"],
    )

    chunks = chunker.chunk(text)

    if len(chunks) < MIN_CHUNKS_TO_TRIM:
        start_ms = segments[0].get("startMs", 0)
        end_ms = segments[-1].get("endMs", 0)
        print(
            f"[Semantic Extent] {len(chunks)} chunk(s) found, "
            f"using the full transcript: {start_ms}ms - {end_ms}ms"
        )
        return min(start_ms, end_ms), end_ms

    first_chunk_last = bisect_right(offsets, chunks[0].end_index - 1) - 1
    last_chunk_first = bisect_right(offsets, chunks[-1].start_index) - 1
    start_ms = segments[first_chunk_last]["startMs"]
    end_ms = segments[last_chunk_first]["endMs"]

    if start_ms > end_ms:
        start_ms = segments[0].get("startMs", 0)
        end_ms = segments[-1].get("endMs", 0)

    print(f"[Semantic Extent] Extracted range: {start_ms}ms - {end_ms}ms")
    return start_ms, end_ms


def run(payload):
    file_path = os.path.normpath(payload["file_path"])
    job_id = payload["job_id"]
    shared_path = payload["shared_path"]

    transcript, segments = get_transcript(file_path)
    start, end = get_semantic_range_ms(segments)

    transcript["segments"] = [
        segment
        for segment in segments
        if segment["endMs"] > start and segment["startMs"] < end
    ]

    stem = os.path.splitext(os.path.basename(file_path))[0]
    trimmed_name = f"{stem}_trimmed.json"
    save_to_shared_disk(shared_path, job_id, trimmed_name, transcript)
    save_to_shared_disk(shared_path, job_id, f"{stem}_extent_output.json", {"start": start, "end": end})

    return {
        "file_path": os.path.join(os.path.dirname(file_path), trimmed_name),
        "start": start,
        "end": end,
    }


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin))))
