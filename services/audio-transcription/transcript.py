from utils import load_json_file

transcript_text_file = "transcript.txt"


def get_transcript(file_path):
    transcript = load_json_file(file_path)
    if not transcript:
        raise ValueError("Failed to load transcript")

    segments = transcript.get("segments", [])
    if not segments:
        raise ValueError("No segments found in transcript")

    return transcript, segments


def get_speaker_turn_boundary_ms(segments: list[dict], index: int, search_direction: str) -> int:
    speaker = segments[index]["speaker"]
    step = 1 if search_direction == "forward" else -1
    i = index

    while 0 <= i + step < len(segments) and segments[i + step]["speaker"] == speaker:
        i += step

    return segments[i]["endMs"] if search_direction == "forward" else segments[i]["startMs"]
