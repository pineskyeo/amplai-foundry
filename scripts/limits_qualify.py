"""Measure memory / pids / cpus limits of the ContainerSandbox argv on a real engine.

Every probe runs the pinned worker image through ``ContainerSandbox.command`` (the same argv
the runtime uses) with a small bounded profile, keeps the raw stdout/stderr/inspect output as
an artifact, and records the observed outcome. Nothing is marked pass unless it was observed.

    .venv/bin/python scripts/limits_qualify.py          # write local-limits-qualification.json
    .venv/bin/python scripts/limits_qualify.py --check  # re-probe, exit 1 unless all pass
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox

REPO = Path(__file__).resolve().parents[1]
CONTAINER = REPO / "deployment" / "local-container.json"
OUT = REPO / "deployment" / "local-limits-qualification.json"
ARTIFACTS = REPO / "deployment" / "limits-qualification-artifacts"
# colima shares only $HOME with the VM, so the bind-mounted workspace must live under it.
PROBE_ROOT = Path.home() / ".amplai-sandbox-probes" / "limits"

MEMORY = "64m"
PIDS = 32
CPUS = 0.5

MEM_SCRIPT = (
    "import sys\n"
    "n = int(sys.argv[1])\n"
    "chunks = []\n"
    "for i in range(n):\n"
    "    chunks.append(b'x' * (1024 * 1024))\n"
    "print('allocated_mib', n, flush=True)\n"
)
FORK_SCRIPT = (
    "import os, sys, time, signal\n"
    "n = int(sys.argv[1])\n"
    "kids = []\n"
    "err = None\n"
    "for i in range(n):\n"
    "    try:\n"
    "        pid = os.fork()\n"
    "    except OSError as exc:\n"
    "        err = repr(exc)\n"
    "        break\n"
    "    if pid == 0:\n"
    "        time.sleep(60)\n"
    "        os._exit(0)\n"
    "    kids.append(pid)\n"
    "print('forked', len(kids), flush=True)\n"
    "print('fork_error', err, flush=True)\n"
    "for pid in kids:\n"
    "    os.kill(pid, signal.SIGKILL)\n"
    "    os.waitpid(pid, 0)\n"
    "print('alive_after', True, flush=True)\n"
)


def load_profile() -> ContainerProfile:
    p = json.loads(CONTAINER.read_text())
    return ContainerProfile(
        p["image"], uid=p["uid"], gid=p["gid"], memory=MEMORY, cpus=CPUS, pids=PIDS
    )


def run_probe(box: ContainerSandbox, name: str, argv: list[str], ws: Path) -> dict[str, Any]:
    """Run one container (no --rm so it can be inspected), save raw output, remove it."""
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    command = box.command(argv, ws, name)
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        inspect = subprocess.run(
            ["docker", "container", "inspect", name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    info = json.loads(inspect.stdout)[0] if inspect.returncode == 0 else {}
    host_config = info.get("HostConfig", {})
    raw: dict[str, Any] = {
        "command": command,
        "rc": run.returncode,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "inspect_state": info.get("State"),
        "inspect_host_config": {
            k: host_config.get(k)
            for k in ("Memory", "MemorySwap", "NanoCpus", "PidsLimit", "Init", "ReadonlyRootfs")
        },
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / f"{name}.json"
    path.write_text(json.dumps(raw, indent=2) + "\n")
    raw["artifact"] = str(path.relative_to(REPO))
    return raw


def _line(stdout: str, key: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith(key + " "):
            return line[len(key) + 1 :]
    return None


def probes(box: ContainerSandbox, ws: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    py = ["python3", "-c"]

    def record(name: str, outcome: str, observed: dict[str, Any], raw: dict[str, Any]) -> None:
        out.append(
            {"name": name, "outcome": outcome, "observed": observed, "artifact": raw["artifact"]}
        )

    # memory positive control: 16 MiB under a 64m limit must succeed.
    raw = run_probe(box, "amplai-limits-mem-ok", [*py, MEM_SCRIPT, "16"], ws)
    state = raw["inspect_state"] or {}
    obs: dict[str, Any] = {
        "rc": raw["rc"],
        "oom_killed": state.get("OOMKilled"),
        "stdout": raw["stdout"].strip(),
    }
    ok = raw["rc"] == 0 and state.get("OOMKilled") is False and "allocated_mib 16" in raw["stdout"]
    record("memory_within_limit_succeeds", "pass" if ok else "fail", obs, raw)

    # memory: 512 MiB against 64m (default swap 2x = 128m) must be OOM killed.
    raw = run_probe(box, "amplai-limits-mem-oom", [*py, MEM_SCRIPT, "512"], ws)
    state = raw["inspect_state"] or {}
    obs = {
        "rc": raw["rc"],
        "oom_killed": state.get("OOMKilled"),
        "memory_bytes": raw["inspect_host_config"]["Memory"],
        "memory_swap_bytes": raw["inspect_host_config"]["MemorySwap"],
        "stdout_tail": raw["stdout"][-200:],
        "stderr_tail": raw["stderr"][-200:],
    }
    if raw["rc"] == 137 and state.get("OOMKilled") is True:
        outcome = "pass"
    elif "allocated_mib 512" in raw["stdout"]:
        outcome = "fail"
    else:
        outcome = "inconclusive"  # killed/failed but not attributable to the OOM killer
    record("memory_over_limit_oom_killed", outcome, obs, raw)

    # pids positive control: 8 forks under a 32 limit must all succeed.
    raw = run_probe(box, "amplai-limits-pids-ok", [*py, FORK_SCRIPT, "8"], ws)
    obs = {
        "rc": raw["rc"],
        "forked": _line(raw["stdout"], "forked"),
        "fork_error": _line(raw["stdout"], "fork_error"),
    }
    ok = raw["rc"] == 0 and obs["forked"] == "8" and obs["fork_error"] == "None"
    record("pids_within_limit_succeeds", "pass" if ok else "fail", obs, raw)

    # pids: 200 forks against a 32 limit must fail and leave container and engine alive.
    raw = run_probe(box, "amplai-limits-pids-over", [*py, FORK_SCRIPT, "200"], ws)
    info = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
    forked = _line(raw["stdout"], "forked")
    obs = {
        "rc": raw["rc"],
        "forked": forked,
        "fork_error": _line(raw["stdout"], "fork_error"),
        "container_alive_after": _line(raw["stdout"], "alive_after"),
        "engine_alive_after_rc": info.returncode,
        "pids_limit": raw["inspect_host_config"]["PidsLimit"],
    }
    capped = forked is not None and forked.isdigit() and int(forked) < PIDS
    ok = (
        raw["rc"] == 0
        and capped
        and obs["fork_error"] not in (None, "None")
        and obs["container_alive_after"] == "True"
        and info.returncode == 0
    )
    record("pids_over_limit_fork_refused", "pass" if ok else "fail", obs, raw)

    # cpus: --cpus reflected in NanoCpus (configuration only, no load measurement).
    raw = run_probe(box, "amplai-limits-cpus", ["true"], ws)
    nano = raw["inspect_host_config"]["NanoCpus"]
    obs = {"rc": raw["rc"], "nano_cpus": nano, "expected": int(CPUS * 1e9)}
    record("cpus_configured", "pass" if nano == int(CPUS * 1e9) else "fail", obs, raw)
    out.append(
        {
            "name": "cpus_throttled_under_load",
            "outcome": "inconclusive",
            "observed": {"reason": "CPU throttling under load was not measured"},
            "artifact": None,
        }
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    profile = load_profile()
    box = ContainerSandbox(profile)
    if PROBE_ROOT.exists():
        shutil.rmtree(PROBE_ROOT)
    PROBE_ROOT.mkdir(parents=True, mode=0o700)
    try:
        results = probes(box, PROBE_ROOT)
    finally:
        shutil.rmtree(PROBE_ROOT, ignore_errors=True)
    version = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    ).stdout.strip()
    doc = {
        "schema_version": "1.0",
        "kind": "container_limits_qualification",
        "checked_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": {"platform": platform.platform(), "runtime": "colima (lima VM) on macOS"},
        "engine": "docker",
        "docker_version": version,
        "image": profile.image,
        "profile": {"memory": MEMORY, "pids": PIDS, "cpus": CPUS},
        "probes": results,
    }
    print(json.dumps(doc, indent=2))
    if not args.check:
        OUT.write_text(json.dumps(doc, indent=2) + "\n")
    required = [p for p in results if p["name"] != "cpus_throttled_under_load"]
    return 0 if all(p["outcome"] == "pass" for p in required) else 1


if __name__ == "__main__":
    sys.exit(main())
