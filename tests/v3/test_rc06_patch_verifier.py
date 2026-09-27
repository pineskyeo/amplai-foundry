"""Work 018 S5 — a change is verified on a fresh base copy with its patch applied (EX-003)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.contracts.identity import canonical
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox
from amplai_foundry.sandbox.git_workspace import CHANGE_MEDIA, PATCH_BINDING, GitWorkspaceManager
from amplai_foundry.verification.runtime.patch_commands import (
    NonEmptyChangeCheck,
    PatchCommandVerifier,
)

NODE = {"produces": [{"name": "change", "media_type": CHANGE_MEDIA, "required": True}]}
TEST = ["python3", "-c", "import app, sys; sys.exit(0 if app.value() == 2 else 1)"]


def git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
    subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=True)


class HostSandbox:
    """Runs the argv on the host in the workspace. A protocol fixture, not a sandbox."""

    class profile:
        network = "none"

    def __init__(self) -> None:
        self.workspaces: list[Path] = []

    def command(self, argv: list[str], workspace: Path, name: str, **_: Any) -> list[str]:
        self.workspaces.append(Path(workspace))
        script = (
            "import os,subprocess,sys;os.chdir(sys.argv[1]);sys.exit(subprocess.call(sys.argv[2:]))"
        )
        return [sys.executable, "-c", script, str(workspace), *argv]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "app"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("def value():\n    return 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


@pytest.fixture
def mgr(deployment: Any, tmp_path: Path, repo: Path) -> GitWorkspaceManager:
    return GitWorkspaceManager(tmp_path / "work", deployment.artifacts, {"app": repo})


def change(deployment: Any, mgr: GitWorkspaceManager, body: str | None) -> bytes:
    base = mgr.base_snapshot(deployment.scope, "app")
    ws = mgr.materialize(deployment.scope, "run-" + str(abs(hash(body)))[:8], base)
    if body is not None:
        (ws / "app.py").write_text(body)
    out = mgr.collect(deployment.scope, ws, {"change": PATCH_BINDING}, NODE, process_stopped=True)
    raw: bytes = deployment.artifacts.read(deployment.scope, out["change"])
    return raw


def verifier(deployment: Any, mgr: GitWorkspaceManager, sandbox: Any, **kw: Any) -> Any:
    return PatchCommandVerifier(
        "unit", TEST, workspaces=mgr, scope=deployment.scope, sandbox=sandbox, **kw
    )


def test_the_patch_is_judged_on_a_fresh_base_copy(
    deployment: Any, mgr: GitWorkspaceManager
) -> None:
    sandbox = HostSandbox()
    good = change(deployment, mgr, "def value():\n    return 2\n")
    bad = change(deployment, mgr, "def value():\n    return 3\n")
    ok = verifier(deployment, mgr, sandbox)(good)
    no = verifier(deployment, mgr, sandbox)(bad)
    assert (ok.outcome, ok.exit_code) == ("pass", 0)
    assert (no.outcome, no.exit_code) == ("fail", 1)
    # every run used its own copy under the manager root, removed afterwards
    assert len(set(sandbox.workspaces)) == 2
    assert all(w.parent == mgr.root and not w.exists() for w in sandbox.workspaces)


def test_a_patch_that_does_not_apply_fails_without_running(
    deployment: Any, mgr: GitWorkspaceManager
) -> None:
    sandbox = HostSandbox()
    raw = json.loads(change(deployment, mgr, "def value():\n    return 2\n"))
    broken = b"--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-nothing here\n+x\n"
    raw["patch"] = deployment.artifacts.admit(deployment.scope, broken, "text/x-diff")
    raw["patch_bytes"] = len(broken)
    result = verifier(deployment, mgr, sandbox)(canonical(raw))
    assert result.outcome == "fail" and "does not apply" in result.reason
    assert sandbox.workspaces == []


def test_a_hanging_command_is_inconclusive(deployment: Any, mgr: GitWorkspaceManager) -> None:
    hang = change(deployment, mgr, "import time\ntime.sleep(30)\ndef value():\n    return 2\n")
    result = verifier(deployment, mgr, HostSandbox(), timeout_seconds=1)(hang)
    assert result.outcome == "inconclusive"


def test_the_verifier_refuses_a_networked_sandbox(
    deployment: Any, mgr: GitWorkspaceManager
) -> None:
    class Networked(HostSandbox):
        class profile:
            network = "amplai-internal"

    with pytest.raises(RuntimeFault):
        verifier(deployment, mgr, Networked())


def test_an_empty_change_fails_the_global_check(deployment: Any, mgr: GitWorkspaceManager) -> None:
    empty = change(deployment, mgr, None)
    full = change(deployment, mgr, "def value():\n    return 2\n")
    refs = {
        "n1": {"change": deployment.artifacts.admit(deployment.scope, full, CHANGE_MEDIA)},
    }
    check = NonEmptyChangeCheck(mgr, deployment.scope)
    assert check({}, {}, refs).outcome == "pass"
    refs["n2"] = {"change": deployment.artifacts.admit(deployment.scope, empty, CHANGE_MEDIA)}
    assert check({}, {}, refs).outcome == "fail"


@pytest.mark.container
def test_real_container_verifier_with_network_none(
    deployment: Any, tmp_path: Path, repo: Path
) -> None:
    root = Path.home() / ".amplai-sandbox-probes" / "verifier-test"
    subprocess.run(["rm", "-rf", str(root)], check=False)
    mgr = GitWorkspaceManager(root, deployment.artifacts, {"app": repo})
    image = json.loads(Path("deployment/local-container.json").read_text())["image"]
    sandbox = ContainerSandbox(ContainerProfile(image, network="none"))
    good = change(deployment, mgr, "def value():\n    return 2\n")
    net = ["python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), 3)"]
    assert verifier(deployment, mgr, sandbox)(good).outcome == "pass"
    probe = PatchCommandVerifier(
        "net", net, workspaces=mgr, scope=deployment.scope, sandbox=sandbox
    )(good)
    assert probe.outcome == "fail"  # no route out of a network=none verifier
