"""Real container containment probes (design/10 §5, driver qualification MANDATORY set).

Runs only with the ``container`` marker on a host where ``deployment/local-container.json``
names an image pinned by registry digest and the docker engine answers. Nothing here is a
production qualification; it is the local measurement that turns "inconclusive" into a
recorded pass or fail for filesystem/egress/cancel probes.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox

REPO = Path(__file__).resolve().parents[2]
PROFILE_PATH = REPO / "deployment" / "local-container.json"
# colima shares only $HOME with the VM, so bind-mounted workspaces must live under it.
PROBE_ROOT = Path.home() / ".amplai-sandbox-probes"

pytestmark = pytest.mark.container


def _profile() -> ContainerProfile:
    if not PROFILE_PATH.is_file():
        raise AssertionError("deployment/local-container.json missing; run scripts/sandbox_up.sh")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise AssertionError("docker engine not reachable; run scripts/sandbox_up.sh")
    p = json.loads(PROFILE_PATH.read_text())
    return ContainerProfile(
        p["image"],
        uid=p["uid"],
        gid=p["gid"],
        memory=p["memory"],
        cpus=p["cpus"],
        pids=p["pids"],
        network=p["network"],
    )


@pytest.fixture
def ws(request):
    PROBE_ROOT.mkdir(mode=0o700, exist_ok=True)
    path = PROBE_ROOT / request.node.name.split("[")[0]
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _run(
    argv: list[str], workspace: Path, name: str, timeout: int = 60
) -> subprocess.CompletedProcess[bytes]:
    box = ContainerSandbox(_profile())
    command = box.command(argv, workspace, name)
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_image_is_pinned_by_registry_digest_and_runs_as_unprivileged_uid(ws):
    out = _run(["id", "-u"], ws, "amplai-probe-uid")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == b"65534"
    assert "@sha256:" in _profile().image


def test_filesystem_containment_root_is_read_only_and_workspace_is_the_only_writable_bind(ws):
    out = _run(["sh", "-c", "echo x > /usr/local/bin/evil; echo rc=$?"], ws, "amplai-probe-ro")
    assert b"rc=1" in out.stdout or b"rc=2" in out.stdout, out.stdout + out.stderr
    out = _run(["sh", "-c", "echo hello > /workspace/out.txt; echo rc=$?"], ws, "amplai-probe-ws")
    assert b"rc=0" in out.stdout, out.stdout + out.stderr
    assert (ws / "out.txt").read_text() == "hello\n"
    out = _run(["sh", "-c", "ls /Users /root 2>&1; echo rc=$?"], ws, "amplai-probe-home")
    assert b"No such file" in out.stdout or b"rc=2" in out.stdout


def test_egress_containment_network_none_blocks_all_traffic(ws):
    out = _run(
        [
            "sh",
            "-c",
            "curl -sS --max-time 5 https://api.anthropic.com/ >/dev/null 2>&1; echo rc=$?",
        ],
        ws,
        "amplai-probe-egress",
        timeout=30,
    )
    assert b"rc=0" not in out.stdout, out.stdout + out.stderr


def test_cancel_tree_terminates_the_container_and_its_children(ws):
    box = ContainerSandbox(_profile())
    name = "amplai-probe-cancel"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    command = box.command(["sh", "-c", "sleep 300 & sleep 300 & wait"], ws, name)
    proc = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    try:
        for _ in range(100):
            ps = subprocess.run(
                ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
            )
            if name in ps.stdout:
                break
            time.sleep(0.1)
        assert name in ps.stdout, "container did not start"
        subprocess.run(["docker", "kill", "--signal", "SIGTERM", name], capture_output=True)
        subprocess.run(["docker", "wait", name], capture_output=True, timeout=30)
        ps = subprocess.run(
            ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        assert name not in ps.stdout
        inside = subprocess.run(["docker", "top", name], capture_output=True)
        assert inside.returncode != 0  # no process tree survives the container
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_agent_binaries_are_present_and_report_versions(ws):
    for binary in ("claude", "codex"):
        out = _run([binary, "--version"], ws, f"amplai-probe-{binary}")
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip(), binary
