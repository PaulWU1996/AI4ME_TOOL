#!/usr/bin/env python3
"""Detect whether audioservice/visualservice have moved since the last
verified `capture_contracts.py` run, without needing to remember to check.

Both real bugs found during real-GPU testing (the `audio_description`
KeyError, the `rmtree` scope bug) were silent -- HTTP 200, no error -- so a
new image build can regress a contract `worker/tasks.py` relies on with
nothing failing loudly. `capture_contracts.py` is the check that would catch
it, but only if someone remembers to re-run it after every image update.

This script closes that gap for the two locally-loaded images
(audioservice:latest, visualservice:latest -- see IMAGE_NAMES in
capture_contracts.py): it compares their current local image ID against the
ID recorded in the most recent `contracts/<timestamp>/manifest.json`, and
tells you which ones changed since that capture.

Read-only: runs `docker image inspect`, touches nothing else.

    python3 scripts/check_contract_freshness.py

Exit code 0 means every trackable image matches its last verified capture.
Non-zero means at least one has moved (or was never captured) --
re-run capture_contracts.py before trusting that service's contract.
"""
import glob
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def image_id(image_name):
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


def latest_manifest():
    """Most recent contracts/<timestamp>/manifest.json, by timestamp order."""
    candidates = sorted(glob.glob(os.path.join(REPO, "contracts", "*", "manifest.json")))
    if not candidates:
        return None, None
    path = candidates[-1]
    stamp = os.path.basename(os.path.dirname(path))
    with open(path) as f:
        return stamp, json.load(f)


def main():
    stamp, manifest = latest_manifest()
    if manifest is None:
        print(f"{RED}No contracts/<timestamp>/manifest.json found.{RESET}")
        print("Run scripts/capture_contracts.py at least once and commit its output.")
        return 1

    print(f"Comparing against contracts/{stamp}/manifest.json\n")

    stale = 0
    for name, entry in manifest.items():
        image_name, expected = entry["image"], entry["id"]
        current, err = image_id(image_name)
        if current is None:
            print(f"  {RED}MISSING {RESET} {image_name}: not loaded locally ({err})")
            stale += 1
        elif expected is None:
            print(f"  {YELLOW}UNKNOWN {RESET} {image_name}: no id was recorded at capture time")
            stale += 1
        elif current != expected:
            print(f"  {RED}STALE   {RESET} {image_name}: {expected[:19]}... -> {current[:19]}... "
                  f"(changed since {stamp})")
            stale += 1
        else:
            print(f"  {GREEN}FRESH   {RESET} {image_name}: matches {stamp}")

    print()
    if stale:
        print(f"{RED}{stale} image(s) have moved since the last capture.{RESET}")
        print("Re-run: python3 scripts/capture_contracts.py --video ... --video-rel ...")
        print("(see docs/GPU_TEST_RUNBOOK.md Phase 2)")
    else:
        print(f"{GREEN}All tracked images match their last verified capture.{RESET}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
