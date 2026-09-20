"""Real egress sidecar probes (design/10 §5, EGR-02 acceptance).

Runs only with the ``container`` marker on a host where the egress sidecar
(``amplai-egress-proxy`` on network ``amplai-internal``) is up. Confirms the deny-by-default
enforced egress network: direct egress fails, an allowlisted CONNECT tunnels through the
sidecar, a non-allowlisted CONNECT is refused with 403, the sidecar logs the deny decision,
and the recorded qualification file is a real pass matching the current profile digest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from amplai_foundry.runtime.errors import Hold
from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox
from amplai_foundry.sandbox.egress import (
    REQUIRED_PROBES,
    EgressProfile,
    load_qualification,
    require_qualified,
)

REPO = Path(__file__).resolve().parents[2]
CONTAINER_PROFILE_PATH = REPO / "deployment" / "local-container.json"
EGRESS_PROFILE_PATH = REPO / "deployment" / "local-egress.json"
EGRESS_QUALIFICATION_PATH = REPO / "deployment" / "local-egress-qualification.json"
# colima shares only $HOME with the VM, so bind-mounted workspaces must live under it.
PROBE_ROOT = Path.home() / ".amplai-sandbox-probes"

pytestmark = pytest.mark.container


def _profile() -> ContainerProfile:
    if not CONTAINER_PROFILE_PATH.is_file():
        raise AssertionError("deployment/local-container.json missing; run scripts/sandbox_up.sh")
    if not EGRESS_PROFILE_PATH.is_file():
        raise AssertionError("deployment/local-egress.json missing; run scripts/sandbox_up.sh")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise AssertionError("docker engine not reachable; run scripts/sandbox_up.sh")
    p = json.loads(CONTAINER_PROFILE_PATH.read_text())
    egress = EgressProfile.load(EGRESS_PROFILE_PATH)
    ref = load_qualification(EGRESS_QUALIFICATION_PATH)
    return ContainerProfile(
        p["image"],
        uid=p["uid"],
        gid=p["gid"],
        memory=p["memory"],
        cpus=p["cpus"],
        pids=p["pids"],
        network=egress.network,
        network_qualification_ref=ref,
        egress=egress,
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


def test_egress_qualification_file_is_pass_and_matches_the_profile_digest():
    egress = EgressProfile.load(EGRESS_PROFILE_PATH)
    ref = load_qualification(EGRESS_QUALIFICATION_PATH)
    assert ref.get("outcome") == "pass"
    assert ref.get("egress_profile_digest") == egress.digest()
    require_qualified(egress, ref)  # must not raise
    passed = {c["name"] for c in ref.get("checks", []) if c.get("outcome") == "pass"}
    assert passed == set(REQUIRED_PROBES)


def test_sandbox_argv_on_egress_network_carries_proxy_env_and_hardening(ws):
    box = ContainerSandbox(_profile())
    argv = box.command(["true"], ws, "amplai-probe-egress-argv")
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "amplai-internal"
    assert "--env" in argv
    assert "HTTPS_PROXY=http://amplai-egress-proxy:3128" in argv
    assert "--init" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv


def test_direct_egress_from_internal_network_is_denied(ws):
    out = _run(
        [
            "sh",
            "-c",
            "env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy "
            "curl -sS --max-time 5 https://api.anthropic.com/ ; echo rc=$?",
        ],
        ws,
        "amplai-probe-egress-direct",
        timeout=30,
    )
    assert b"rc=0" not in out.stdout, out.stdout + out.stderr


def test_allowlisted_target_tunnels_through_the_sidecar(ws):
    out = _run(
        [
            "curl",
            "-sS",
            "--max-time",
            "10",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "https://api.anthropic.com/",
        ],
        ws,
        "amplai-probe-egress-allowed",
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    status = out.stdout.strip()
    assert len(status) == 3 and status.isdigit(), out.stdout + out.stderr


def test_non_allowlisted_target_is_refused_with_403(ws):
    out = _run(
        [
            "curl",
            "-sS",
            "--max-time",
            "10",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "https://example.com/",
        ],
        ws,
        "amplai-probe-egress-denied",
        timeout=30,
    )
    assert out.returncode == 56, out.stdout + out.stderr
    assert b"403" in out.stderr, out.stdout + out.stderr


def test_sidecar_logs_the_deny_decision(ws):
    since = str(int(time.time()) - 1)  # anchor the log read; --tail races with other probes
    out = _run(
        [
            "curl",
            "-sS",
            "--max-time",
            "10",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "https://example.com/",
        ],
        ws,
        "amplai-probe-egress-logdeny",
        timeout=30,
    )
    assert out.returncode == 56, out.stdout + out.stderr
    logs = subprocess.run(
        ["docker", "logs", "--since", since, "amplai-egress-proxy"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    found = False
    for line in (logs.stdout + logs.stderr).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("target") == "example.com:443" and record.get("decision") == "deny":
            found = True
            break
    assert found, logs.stdout + logs.stderr


def test_sandbox_without_qualification_ref_cannot_join_the_egress_network():
    egress = EgressProfile.load(EGRESS_PROFILE_PATH)
    profile = ContainerProfile(
        json.loads(CONTAINER_PROFILE_PATH.read_text())["image"],
        network=egress.network,
        egress=egress,
        network_qualification_ref=None,
    )
    with pytest.raises(Hold) as excinfo:
        ContainerSandbox(profile)
    assert excinfo.value.code == "EGRESS_UNQUALIFIED"
