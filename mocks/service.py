"""Mock stand-in for the four on-demand analysis services.

One image, four roles (MOCK_ROLE): visual, audio, transcript, tagging.
Stdlib only — no pip install, so the image builds in seconds and starts in
milliseconds instead of loading a multi-GB model onto a GPU.

Contracts reproduced here are the ones worker/tasks.py and worker/utils.py
actually depend on, nothing more:

  visual      GET  /health/          -> 200
              POST /generate         -> {"api_key": ...}   (X-Admin-Key)
              POST /analyze          -> XML VideoAnalysis  (X-API-Key, multipart)
  audio       GET  /health/          -> 200
              POST /process_audio/   -> {"output": [{start, end, caption}]}
  transcript  GET  /health           -> 200
              POST /process/         -> summary JSON
  tagging     GET  /health           -> 200
              POST /process/         -> tags JSON

Knobs, so a test can provoke behaviour a real service would take an hour to
show you:

  MOCK_ROLE            visual | audio | transcript | tagging
  MOCK_PORT            default 8000
  MOCK_STARTUP_DELAY   seconds before /health starts returning 200
                       (simulates model load; the worker's health poll waits)
  MOCK_LATENCY         seconds each work endpoint sleeps before answering
  MOCK_FAIL_MODE       "" | error500 | timeout | badbody | unhealthy
  MOCK_SEGMENTS        how many segments/captions to fabricate (default 3)
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROLE = os.getenv("MOCK_ROLE", "visual")
PORT = int(os.getenv("MOCK_PORT", "8000"))
STARTUP_DELAY = float(os.getenv("MOCK_STARTUP_DELAY", "0"))
LATENCY = float(os.getenv("MOCK_LATENCY", "0"))
FAIL_MODE = os.getenv("MOCK_FAIL_MODE", "")
SEGMENTS = int(os.getenv("MOCK_SEGMENTS", "3"))

# Runtime control file on the shared volume. The worker cold-starts these
# containers itself, from the compose file, so anything injected as an env
# var on a manually-created container is lost the moment the worker recreates
# it. A file on the shared volume survives that, and is re-read per request.
CONTROL_PATH = os.getenv("MOCK_CONTROL_PATH", "/app/tmp/mock_control.json")

BOOTED_AT = time.monotonic()
CALLS = []          # every request, so a test can assert what the worker sent
CALLS_LOCK = threading.Lock()


def log(message):
    print(f"[mock:{ROLE}] {message}", flush=True)


def control(key, default):
    """Per-request override from the shared control file, falling back to the
    env var baked in at container creation."""
    try:
        with open(CONTROL_PATH) as f:
            return json.load(f).get(ROLE, {}).get(key, default)
    except (FileNotFoundError, ValueError, AttributeError):
        return default


def fail_mode():
    return control("fail_mode", FAIL_MODE)


def latency():
    return float(control("latency", LATENCY))


def is_ready():
    if fail_mode() == "unhealthy":
        return False
    return (time.monotonic() - BOOTED_AT) >= float(control("startup_delay", STARTUP_DELAY))


# --------------------------------------------------------------------------
# Canned payloads
# --------------------------------------------------------------------------

def visual_xml():
    """Shape consumed by worker/utils.py:extract_flat_captions()."""
    segments = "".join(
        "<Segment>"
        f"<StartTime>{i * 5.0}</StartTime>"
        f"<EndTime>{(i + 1) * 5.0}</EndTime>"
        f"<Description>Mock visual description for segment {i}.</Description>"
        "</Segment>"
        for i in range(SEGMENTS)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<VideoAnalysis><Segments>{segments}</Segments></VideoAnalysis>"
    )


def audio_json():
    """worker/tasks.py:process_audio reads entry["start"/"end"/"caption"] and
    splits start/end on "," — so the SRT-style millisecond suffix matters."""
    def stamp(seconds):
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d},000"

    return {
        "output": [
            {
                "start": stamp(i * 5),
                "end": stamp((i + 1) * 5),
                "caption": f"Mock audio caption for segment {i}.",
            }
            for i in range(SEGMENTS)
        ]
    }


def transcript_json(body):
    return {
        "summary": "Mock summary of the transcript.",
        "job_id": (body or {}).get("job_id"),
        "prompts_seen": (body or {}).get("prompts"),
        "model": "mock-llm",
    }


def tagging_json(body):
    return {
        "tags": ["mock-tag-a", "mock-tag-b", "mock-tag-c"][:max(1, SEGMENTS)],
        "job_id": (body or {}).get("job_id"),
        "model": "mock-tagger",
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockAI4ME/1.0"

    # -- plumbing ----------------------------------------------------------

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return b""
        remaining, chunks = length, []
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send(self, status, payload=b"", content_type="application/json"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, body):
        with CALLS_LOCK:
            CALLS.append({
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body_bytes": len(body),
                "at": time.time(),
            })

    def log_message(self, fmt, *args):
        log(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # -- failure injection -------------------------------------------------

    def _maybe_fail(self):
        """Return True if the request was answered by a failure injection."""
        mode = fail_mode()
        if mode == "error500":
            self._send(500, {"detail": "mock injected failure"})
            return True
        if mode == "timeout":
            log("fail_mode=timeout — hanging, never responding")
            time.sleep(3600)
            return True
        if mode == "badbody":
            self._send(200, "this is not the json you are looking for", "text/plain")
            return True
        return False

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        path = self.path.rstrip("/") or "/"
        if path == "/health":
            if is_ready():
                return self._send(200, {"status": "ok", "role": ROLE})
            return self._send(503, {"status": "starting", "role": ROLE})
        if path == "/__calls":            # test-only introspection
            with CALLS_LOCK:
                return self._send(200, CALLS)
        return self._send(404, {"detail": f"no GET route {self.path}"})

    def do_POST(self):
        body = self._read_body()
        self._record(body)
        path = self.path.rstrip("/") or "/"

        delay = latency()
        if delay:
            time.sleep(delay)

        # Visual service ---------------------------------------------------
        if path == "/generate":
            if not self.headers.get("X-Admin-Key"):
                return self._send(401, {"detail": "X-Admin-Key required"})
            return self._send(200, {"api_key": "mock-api-key-0123456789"})

        if path == "/analyze":
            if self._maybe_fail():
                return
            if not self.headers.get("X-API-Key"):
                return self._send(401, {"detail": "X-API-Key required"})
            log(f"/analyze received {len(body)} bytes of multipart video")
            return self._send(200, visual_xml(), "application/xml")

        # Audio service ----------------------------------------------------
        if path == "/process_audio":
            if self._maybe_fail():
                return
            try:
                parsed = json.loads(body or b"{}")
            except ValueError:
                return self._send(400, {"detail": "body was not JSON"})
            log(f"/process_audio video_path={parsed.get('video_path')} "
                f"chunks={len(parsed.get('chunks') or [])}")
            return self._send(200, audio_json())

        # Callback sink -----------------------------------------------------
        if path == "/callback":
            # finalize_results POSTs its merged output here when a job
            # declares callback_url. Recorded like everything else, so a test
            # can read it back from /__calls.
            log(f"/callback received {len(body)} bytes")
            return self._send(200, {"received": True})

        # Transcript / tagging services -------------------------------------
        if path == "/process":
            if self._maybe_fail():
                return
            try:
                parsed = json.loads(body or b"{}")
            except ValueError:
                parsed = {}
            payload = transcript_json(parsed) if ROLE == "transcript" else tagging_json(parsed)
            return self._send(200, payload)

        return self._send(404, {"detail": f"no POST route {self.path}"})


def main():
    log(f"starting on :{PORT} "
        f"(startup_delay={STARTUP_DELAY}s latency={LATENCY}s fail_mode={FAIL_MODE or 'none'})")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
