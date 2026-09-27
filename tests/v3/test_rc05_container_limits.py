"""Real memory / pids limit probes for the ContainerSandbox argv.

Runs only with the ``container`` marker on a host where ``scripts/sandbox_up.sh`` produced
``deployment/local-container.json``. The sandbox argv (``--memory``, ``--pids-limit``) is run
against the pinned worker image: an allocation past the memory limit is OOM killed, a fork
storm past the pids limit is refused while the container keeps running, and in-limit controls
succeed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox

REPO = Path(__file__).resolve().parents[2]
CONTAINER_PROFILE_PATH = REPO / "deployment" / "local-container.json"
# colima shares only $HOME with the VM, so bind-mounted workspaces must live under it.
PROBE_ROOT = Path.home() / ".amplai-sandbox-probes"
PIDS = 32

pytestmark = pytest.mark.container

MEM = "import sys\nchunks = [b'x' * (1024 * 1024) for _ in range(int(sys.argv[1]))]\nprint('ok')\n"
FORK = (
    "import os, sys, time, signal\n"
    "kids, err = [], None\n"
    "for _ in range(int(sys.argv[1])):\n"
    "    try:\n"
    "        pid = os.fork()\n"
    "    except OSError as exc:\n"
    "        err = repr(exc)\n"
    "        break\n"
    "    if pid == 0:\n"
    "        time.sleep(60)\n"
    "        os._exit(0)\n"
    "    kids.append(pid)\n"
    "print('forked', len(kids))\n"
    "print('error', err)\n"
    "for pid in kids:\n"
    "    os.kill(pid, signal.SIGKILL)\n"
    "    os.waitpid(pid, 0)\n"
    "print('alive')\n"
)


def _box() -> ContainerSandbox:
    if not CONTAINER_PROFILE_PATH.is_file():
        raise AssertionError("deployment/local-container.json missing; run scripts/sandbox_up.sh")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise AssertionError("docker engine not reachable; run scripts/sandbox_up.sh")
    p = json.loads(CONTAINER_PROFILE_PATH.read_text())
    return ContainerSandbox(
        ContainerProfile(p["image"], uid=p["uid"], gid=p["gid"], memory="64m", pids=PIDS)
    )


@pytest.fixture
def ws(request: pytest.FixtureRequest) -> Iterator[Path]:
    PROBE_ROOT.mkdir(mode=0o700, exist_ok=True)
    path = PROBE_ROOT / request.node.name.split("[")[0]
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _run(argv: list[str], ws: Path, name: str) -> tuple[subprocess.CompletedProcess[str], Any]:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    command = _box().command(argv, ws, name)
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=120)
        inspect = subprocess.run(
            ["docker", "container", "inspect", name], capture_output=True, text=True, timeout=15
        )
        state = json.loads(inspect.stdout)[0]["State"]
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return out, state


def test_allocation_within_memory_limit_succeeds(ws: Path) -> None:
    out, state = _run(["python3", "-c", MEM, "16"], ws, "amplai-probe-limits-mem-ok")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
    assert state["OOMKilled"] is False


def test_allocation_over_memory_limit_is_oom_killed(ws: Path) -> None:
    out, state = _run(["python3", "-c", MEM, "512"], ws, "amplai-probe-limits-mem-oom")
    assert out.returncode == 137, out.stdout + out.stderr
    assert state["OOMKilled"] is True
    assert "ok" not in out.stdout


def test_forks_within_pids_limit_succeed(ws: Path) -> None:
    out, _ = _run(["python3", "-c", FORK, "8"], ws, "amplai-probe-limits-pids-ok")
    assert out.returncode == 0, out.stderr
    assert "forked 8\nerror None\nalive" in out.stdout


def test_fork_storm_over_pids_limit_is_refused_and_container_survives(ws: Path) -> None:
    out, _ = _run(["python3", "-c", FORK, "200"], ws, "amplai-probe-limits-pids-over")
    assert out.returncode == 0, out.stderr
    lines = dict(line.split(" ", 1) for line in out.stdout.splitlines() if " " in line)
    assert int(lines["forked"]) < PIDS
    assert "Resource temporarily unavailable" in lines["error"]
    assert out.stdout.splitlines()[-1] == "alive"
    assert subprocess.run(["docker", "info"], capture_output=True).returncode == 0
