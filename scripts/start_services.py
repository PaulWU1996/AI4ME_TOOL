#!/usr/bin/env python3
"""
Resource-aware startup for AI4ME_PROJ.

Selects which on-demand services (audioservice, visualservice, transcriptservice, ...)
run in "keepalive" mode (stay resident across jobs) vs "coldstart" mode (started/stopped
per job, the historical default). Before touching Docker, does a cheap static check
against declared resource estimates in config/services.json; then brings up the
keepalive-selected services one at a time, measuring their real VRAM/RAM delta, and
aborts on the first real overcommit rather than a predicted one. On success, the
measured deltas are written back into the registry so future runs use observed
numbers instead of stale estimates, and the resolved mode selection is written to
shared/service_modes.json for the worker container to read.
"""
import argparse
import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "services.json")
SERVICE_MODES_PATH = os.path.join(REPO_ROOT, "shared", "service_modes.json")
COMPOSE_FILE = os.path.join(REPO_ROOT, "docker-compose.yml")

HEALTH_CHECK_TIMEOUT = 330
HEALTH_CHECK_INTERVAL = 10
SAFETY_MARGIN = 0.9  # only reserve up to 90% of host capacity for keepalive services


def load_registry():
    with open(SERVICES_CONFIG_PATH) as f:
        return json.load(f)


def save_registry(registry):
    with open(SERVICES_CONFIG_PATH, "w") as f:
        json.dump(registry, f, indent=2)
        f.write("\n")


def gpu_capacity_mb():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True,
        ).stdout
        return sum(int(line.strip()) for line in out.splitlines() if line.strip())
    except (FileNotFoundError, subprocess.CalledProcessError):
        return 0


def gpu_used_mb():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True,
        ).stdout
        return sum(int(line.strip()) for line in out.splitlines() if line.strip())
    except (FileNotFoundError, subprocess.CalledProcessError):
        return 0


def ram_capacity_mb():
    try:
        out = subprocess.run(["free", "-m"], check=True, capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith("Mem:"):
                return int(line.split()[1])
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    return 0


def compose(*args):
    subprocess.run(["docker", "compose", "-f", COMPOSE_FILE] + list(args), check=True)


def wait_healthy(container_name, timeout=HEALTH_CHECK_TIMEOUT, interval=HEALTH_CHECK_INTERVAL):
    import docker
    client = docker.from_env()
    elapsed = 0
    while elapsed < timeout:
        try:
            container = client.containers.get(container_name)
            container.reload()
            health = container.attrs.get("State", {}).get("Health", {}).get("Status")
            if health == "healthy":
                return True
        except docker.errors.NotFound:
            pass
        time.sleep(interval)
        elapsed += interval
    return False


def static_precheck(selected, registry, gpu_cap, ram_cap):
    vram_needed = sum(registry[s]["vram_mb"] for s in selected)
    ram_needed = sum(registry[s]["ram_mb"] for s in selected)
    problems = []
    if vram_needed > gpu_cap * SAFETY_MARGIN:
        problems.append(
            f"declared VRAM need {vram_needed}MB exceeds {SAFETY_MARGIN:.0%} of host capacity {gpu_cap}MB"
        )
    if ram_needed > ram_cap * SAFETY_MARGIN:
        problems.append(
            f"declared RAM need {ram_needed}MB exceeds {SAFETY_MARGIN:.0%} of host capacity {ram_cap}MB"
        )
    return problems


def measured_pass(selected, registry, gpu_cap, ram_cap):
    started = []
    for name in selected:
        entry = registry[name]
        compose_service = entry["compose_service"]

        vram_before = gpu_used_mb()

        print(f"[start_services] Starting {name} (keepalive)...")
        compose("--profile", "on-demand", "up", "-d", compose_service)

        if not wait_healthy(compose_service):
            compose("stop", compose_service)
            print(f"[start_services] ABORT: {name} did not become healthy in time.")
            for s in started:
                compose("stop", registry[s]["compose_service"])
            sys.exit(1)

        vram_after = gpu_used_mb()
        vram_delta = max(vram_after - vram_before, 0)

        started.append(name)
        cumulative_vram = sum(registry[s].get("_measured_vram_mb", registry[s]["vram_mb"]) for s in started[:-1]) + vram_delta

        if cumulative_vram > gpu_cap * SAFETY_MARGIN:
            print(
                f"[start_services] ABORT: after starting {name}, measured cumulative VRAM "
                f"{cumulative_vram}MB exceeds {SAFETY_MARGIN:.0%} of host capacity {gpu_cap}MB "
                f"(measured delta for {name}: {vram_delta}MB, declared estimate was {entry['vram_mb']}MB)."
            )
            for s in started:
                compose("stop", registry[s]["compose_service"])
            sys.exit(1)

        entry["_measured_vram_mb"] = vram_delta
        entry["vram_mb"] = vram_delta if vram_delta > 0 else entry["vram_mb"]
        print(f"[start_services] {name} healthy. Measured VRAM delta: {vram_delta}MB.")

    return started


def main():
    parser = argparse.ArgumentParser(description="Start AI4ME_PROJ with per-service keepalive selection.")
    parser.add_argument("--keepalive", default="", help="Comma-separated services to keep running across jobs")
    parser.add_argument("--coldstart", default="", help="Comma-separated services to start/stop per job (default for anything not listed as keepalive)")
    args = parser.parse_args()

    registry = load_registry()
    keepalive = [s.strip() for s in args.keepalive.split(",") if s.strip()]
    coldstart = [s.strip() for s in args.coldstart.split(",") if s.strip()]

    for s in keepalive + coldstart:
        if s not in registry:
            sys.exit(f"[start_services] Unknown service '{s}'. Known services: {', '.join(registry)}")
        if s in keepalive and not registry[s].get("supports_keepalive", False):
            sys.exit(f"[start_services] Service '{s}' does not support keepalive mode.")

    gpu_cap = gpu_capacity_mb()
    ram_cap = ram_capacity_mb()

    if keepalive:
        problems = static_precheck(keepalive, registry, gpu_cap, ram_cap)
        if problems:
            print("[start_services] ABORT: selection fails static pre-check:")
            for p in problems:
                print(f"  - {p}")
            print("Re-select fewer/smaller services, or use coldstart mode instead.")
            sys.exit(1)

        measured_pass(keepalive, registry, gpu_cap, ram_cap)
        save_registry(registry)

    modes = {s: "keepalive" for s in keepalive}
    modes.update({s: "coldstart" for s in registry if s not in keepalive})

    os.makedirs(os.path.dirname(SERVICE_MODES_PATH), exist_ok=True)
    with open(SERVICE_MODES_PATH, "w") as f:
        json.dump(modes, f, indent=2)
        f.write("\n")
    print(f"[start_services] Wrote resolved service modes to {SERVICE_MODES_PATH}: {modes}")

    print("[start_services] Bringing up core stack (redis, controller, worker)...")
    compose("up", "--build", "-d", "redis", "controller", "worker")
    print("[start_services] Done.")


if __name__ == "__main__":
    main()
