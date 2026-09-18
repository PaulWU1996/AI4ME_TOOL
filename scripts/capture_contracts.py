#!/usr/bin/env python3
"""Capture the REAL analysis services' HTTP responses, and check them against
what worker/tasks.py assumes.

This is the one thing the mock suite cannot do. `mocks/service.py` encodes
what `worker/tasks.py` *believes* the contracts are; if that belief is wrong,
every test still passes and production still breaks. This script asks the
real services directly, saves the raw responses, and reports any mismatch
against the parsers' actual expectations.

Run it on the GPU machine with the services up:

    docker compose --profile on-demand up -d visualservice audioservice
    python3 scripts/capture_contracts.py --video ./data/sample.mp4

Everything is written to contracts/<timestamp>/ -- commit that directory, so
the mocks can be corrected against evidence rather than assumption.

Read-only with respect to the pipeline: it talks to the services directly and
never enqueues a job.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "visual": "http://localhost:9001",
    "audio": "http://localhost:9002",
    "transcript": "http://localhost:9003",
    "tagging": "http://localhost:9004",
}

# Only the two services loaded from a fixed local tag (`docker load -i
# *.tar`) get freshness tracking. transcript/tagging resolve to an
# ECR ref via AWS_ACCOUNT_ID/AWS_REGION env vars, so "new build" there
# is already visible as a tag/digest change in the registry.
IMAGE_NAMES = {
    "visual": "visualservice:latest",
    "audio": "audioservice:latest",
}


def image_id(image_name):
    """Local image ID (sha256:...) for freshness comparisons, or None."""
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", "--format={{.Id}}", image_name],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if out.returncode != 0:
        return None, out.stderr.strip() or f"exit {out.returncode}"
    return out.stdout.strip(), None

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

findings = []


def note(level, message):
    colour = {"ok": GREEN, "mismatch": RED, "warn": YELLOW}[level]
    print(f"  {colour}{level.upper():8}{RESET} {message}")
    findings.append({"level": level, "message": message})


def request(method, url, *, body=None, headers=None, files=None, timeout=600):
    """Minimal HTTP with optional multipart, so this script needs no deps."""
    headers = dict(headers or {})
    data = None

    if files:
        boundary = f"----capture{uuid.uuid4().hex}"
        parts = []
        for field, (filename, content) in files.items():
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(
                f'Content-Disposition: form-data; name="{field}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode()
            )
            parts.append(content)
            parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:                       # noqa: BLE001 - reported, not raised
        return None, {}, str(e).encode()


def save(outdir, name, content):
    path = os.path.join(outdir, name)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as f:
        f.write(content)
    return path


# --------------------------------------------------------------------------
# Per-service capture + contract check
# --------------------------------------------------------------------------

def capture_visual(base, video_path, admin_key, outdir):
    print(f"\n{DIM}visual @ {base}{RESET}")

    status, _, raw = request("GET", f"{base}/health/", timeout=30)
    save(outdir, "visual_health.txt", f"{status}\n{raw.decode(errors='replace')}")
    note("ok" if status == 200 else "mismatch", f"GET /health/ -> {status} (worker's healthcheck path)")

    status, _, raw = request(
        "POST", f"{base}/api/keys/generate",
        body={"client_name": "client_ai4me", "expire_in_days": 365},
        headers={"X-Admin-Key": admin_key}, timeout=60,
    )
    save(outdir, "visual_generate.json", raw)
    if status != 200:
        note("mismatch", f"POST /api/keys/generate -> {status}; utils.ensure_api_key expects 200. Body: {raw[:200]!r}")
        return
    try:
        key = json.loads(raw).get("api_key")
    except ValueError:
        note("mismatch", "POST /api/keys/generate did not return JSON; ensure_api_key calls .json()")
        return
    if not key:
        note("mismatch", f"/api/keys/generate response has no 'api_key' field: {raw[:200]!r}")
        return
    note("ok", "POST /api/keys/generate returned an 'api_key'")

    with open(video_path, "rb") as f:
        content = f.read()
    status, headers, raw = request(
        "POST", f"{base}/analyze",
        headers={"X-API-Key": key},
        files={"video": (os.path.basename(video_path), content)},
        timeout=6000,
    )
    save(outdir, "visual_analyze.raw", raw)
    if status != 200:
        note("mismatch", f"POST /analyze -> {status}. Body: {raw[:300]!r}")
        return
    note("ok", f"POST /analyze -> 200, {len(raw)} bytes, Content-Type={headers.get('Content-Type')!r}")

    # worker/utils.py:extract_flat_captions expects this exact nesting.
    try:
        import xmltodict
    except ImportError:
        note("warn", "xmltodict not installed here; skipping XML shape check (pip install xmltodict)")
        return
    try:
        data = xmltodict.parse(raw)
    except Exception as e:                        # noqa: BLE001
        note("mismatch", f"/analyze body is not parseable XML: {e}")
        return

    segments = data.get("VideoAnalysis", {}).get("Segments", {})
    if not segments:
        note("mismatch",
             f"expected VideoAnalysis/Segments; got top-level keys {list(data)} — "
             "extract_flat_captions would return []")
        return
    raw_segments = segments.get("Segment", [])
    if isinstance(raw_segments, dict):
        raw_segments = [raw_segments]
    if not raw_segments:
        note("mismatch", "VideoAnalysis/Segments has no Segment entries")
        return
    first = raw_segments[0]
    for field in ("StartTime", "EndTime", "Description"):
        if field not in first:
            note("mismatch", f"Segment is missing {field!r}; extract_flat_captions defaults it. Keys: {list(first)}")
    else:
        note("ok", f"{len(raw_segments)} segments with StartTime/EndTime/Description")


def capture_audio(base, job_id, video_rel, outdir):
    print(f"\n{DIM}audio @ {base}{RESET}")

    status, _, raw = request("GET", f"{base}/health/", timeout=30)
    save(outdir, "audio_health.txt", f"{status}\n{raw.decode(errors='replace')}")
    note("ok" if status == 200 else "mismatch", f"GET /health/ -> {status}")

    payload = {"video_path": video_rel, "prompts": None, "chunks": None}
    status, _, raw = request("POST", f"{base}/process_audio/", body=payload, timeout=1800)
    save(outdir, "audio_process.json", raw)
    save(outdir, "audio_request.json", json.dumps(payload, indent=2))
    if status != 200:
        note("mismatch", f"POST /process_audio/ -> {status}. Body: {raw[:300]!r}")
        return
    try:
        data = json.loads(raw)
    except ValueError:
        note("mismatch", "/process_audio/ did not return JSON; process_audio calls .json()")
        return

    if "output" not in data:
        note("mismatch", f"response has no 'output' key; process_audio reads it. Keys: {list(data)}")
        return
    entries = data["output"]
    if not entries:
        note("warn", "'output' is empty — cannot check entry shape")
        return
    first = entries[0]
    for field in ("start", "end", "caption"):
        if field not in first:
            note("mismatch", f"output entry missing {field!r}; process_audio indexes it. Keys: {list(first)}")
    # process_audio does entry["start"].split(",")[0]
    for field in ("start", "end"):
        value = first.get(field)
        if not isinstance(value, str):
            note("mismatch",
                 f"output[0][{field!r}] is {type(value).__name__}, not str — "
                 'process_audio calls .split(",") on it and would raise')
        elif "," not in value:
            note("warn",
                 f"output[0][{field!r}]={value!r} has no comma; process_audio splits on it "
                 "(harmless, but the SRT millisecond suffix is assumed)")
        else:
            note("ok", f"output[0][{field!r}]={value!r} splits on ',' as expected")


def capture_script_service(name, base, job_id, outdir):
    print(f"\n{DIM}{name} @ {base}{RESET}")

    status, _, raw = request("GET", f"{base}/health", timeout=30)
    save(outdir, f"{name}_health.txt", f"{status}\n{raw.decode(errors='replace')}")
    note("ok" if status == 200 else "mismatch", f"GET /health -> {status}")

    payload = {"job_id": job_id, "job_type": "script", "prompts": None}
    status, _, raw = request("POST", f"{base}/process/", body=payload, timeout=1800)
    save(outdir, f"{name}_process.json", raw)
    if status != 200:
        note("mismatch", f"POST /process/ -> {status}. Body: {raw[:300]!r}")
        return
    try:
        json.loads(raw)
    except ValueError:
        note("mismatch", f"{name} /process/ did not return JSON; run_service_task calls .json()")
        return
    note("ok", f"POST /process/ -> 200, JSON body ({len(raw)} bytes) stored verbatim by run_service_task")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video", help="local video file to POST to /analyze")
    parser.add_argument("--video-rel", help="path as the audio service sees it, e.g. '<job_id>/clip.mp4'")
    parser.add_argument("--job-id", default="contract-capture", help="job_id sent to the script services")
    parser.add_argument("--admin-key", default=os.getenv("ADMIN_KEY", ""), help="X-Admin-Key for /generate")
    parser.add_argument("--only", nargs="*", choices=list(DEFAULTS),
                        help="limit to these services (default: all reachable)")
    for name, url in DEFAULTS.items():
        parser.add_argument(f"--{name}-url", default=url)
    args = parser.parse_args()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(REPO, "contracts", stamp)
    os.makedirs(outdir, exist_ok=True)
    print(f"Capturing to {outdir}")

    wanted = args.only or list(DEFAULTS)

    if "visual" in wanted:
        if not args.video:
            note("warn", "--video not given; skipping /analyze (the most important capture)")
        else:
            capture_visual(args.visual_url, args.video, args.admin_key, outdir)
    if "audio" in wanted:
        if not args.video_rel:
            note("warn", "--video-rel not given; skipping /process_audio/")
        else:
            capture_audio(args.audio_url, args.job_id, args.video_rel, outdir)
    if "transcript" in wanted:
        capture_script_service("transcript", args.transcript_url, args.job_id, outdir)
    if "tagging" in wanted:
        capture_script_service("tagging", args.tagging_url, args.job_id, outdir)

    manifest = {}
    for name in wanted:
        image_name = IMAGE_NAMES.get(name)
        if not image_name:
            continue
        id_, err = image_id(image_name)
        manifest[name] = {"image": image_name, "id": id_}
        if id_:
            note("ok", f"recorded {image_name} id for freshness tracking: {id_[:19]}...")
        else:
            note("warn", f"could not read {image_name}'s id ({err}); "
                          "check_contract_freshness.py won't be able to track it")

    with open(os.path.join(outdir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    with open(os.path.join(outdir, "findings.json"), "w") as f:
        json.dump(findings, f, indent=2)

    mismatches = [f for f in findings if f["level"] == "mismatch"]
    print(f"\n{'=' * 70}")
    print(f"{len(findings)} checks, {len(mismatches)} mismatches. Saved to {outdir}")
    if mismatches:
        print(f"\n{RED}The real services differ from what worker/tasks.py assumes:{RESET}")
        for f in mismatches:
            print(f"  - {f['message']}")
        print("\nFix the parser (or the mock) against the captured bodies in that directory.")
    else:
        print(f"{GREEN}Every assumption in worker/tasks.py held against the real services.{RESET}")
        print("Commit the captured bodies as the reference the mocks are modelled on.")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
