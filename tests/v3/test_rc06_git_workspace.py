"""Work 018 S2 — git commit workspace and host-computed patch (D-068, EX-002)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.contracts.identity import canonical
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.sandbox.git_workspace import (
    BASE_MEDIA,
    PATCH_BINDING,
    PATCH_MEDIA,
    GitWorkspaceManager,
)

NODE = {"produces": [{"name": "change", "media_type": PATCH_MEDIA, "required": True}]}


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "app"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("def greet():\n    return 'hi'\n")
    (r / "README.md").write_text("app\n")
    (r / ".github").mkdir()
    (r / ".github" / "ci.yml").write_text("on: push\n")
    (r / ".gitignore").write_text("build/\n")
    (r / "data.bin").write_bytes(bytes(range(256)))
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    # the operator's own uncommitted work must survive every workspace operation
    (r / "README.md").write_text("app\nlocal edit\n")
    return r


@pytest.fixture
def mgr(deployment: Any, tmp_path: Path, repo: Path) -> GitWorkspaceManager:
    return GitWorkspaceManager(tmp_path / "work", deployment.artifacts, {"app": repo})


def checkout_state(repo: Path) -> tuple[str, str, str]:
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "status", "--porcelain"),
        (repo / "README.md").read_text(),
    )


def test_materialize_is_the_commit_with_dotfiles_and_without_git(
    deployment: Any, mgr: GitWorkspaceManager, repo: Path
) -> None:
    before = checkout_state(repo)
    base = mgr.base_snapshot(deployment.scope, "app")
    ws = mgr.materialize(deployment.scope, "run-1", base)
    assert (ws / ".github" / "ci.yml").read_text() == "on: push\n"
    assert (ws / ".gitignore").exists() and not (ws / ".git").exists()
    # the committed bytes, not the operator's uncommitted edit
    assert (ws / "README.md").read_text() == "app\n"
    assert checkout_state(repo) == before


def test_patch_carries_edits_adds_deletes_binary_and_skips_ignored(
    deployment: Any, mgr: GitWorkspaceManager, repo: Path
) -> None:
    before = checkout_state(repo)
    base = mgr.base_snapshot(deployment.scope, "app")
    ws = mgr.materialize(deployment.scope, "run-1", base)
    (ws / "src" / "app.py").write_text("def greet():\n    return 'hello'\n")
    (ws / "src" / "new.py").write_text("X = 1\n")
    (ws / ".github" / "ci.yml").unlink()
    (ws / "data.bin").write_bytes(bytes(reversed(range(256))))
    (ws / "build").mkdir()
    (ws / "build" / "out.txt").write_text("ignored\n")
    out = mgr.collect(deployment.scope, ws, {"change": PATCH_BINDING}, NODE, process_stopped=True)
    patch = deployment.artifacts.read(deployment.scope, out["change"])
    assert b"return 'hello'" in patch and b"src/new.py" in patch and b"GIT binary patch" in patch
    assert b".github/ci.yml" in patch and b"build/out.txt" not in patch
    # the same patch rebuilds the same tree on a fresh copy of the base
    fresh = mgr.materialize(deployment.scope, "run-2", base)
    subprocess.run(["git", "apply", "--binary", "-"], input=patch, cwd=fresh, check=True)
    assert (fresh / "src" / "app.py").read_text() == (ws / "src" / "app.py").read_text()
    assert (fresh / "data.bin").read_bytes() == (ws / "data.bin").read_bytes()
    assert not (fresh / ".github" / "ci.yml").exists() and (fresh / "src" / "new.py").exists()
    assert checkout_state(repo) == before


def test_an_agent_symlink_never_leaks_its_target(
    deployment: Any, mgr: GitWorkspaceManager, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VALUE\n")
    base = mgr.base_snapshot(deployment.scope, "app")
    ws = mgr.materialize(deployment.scope, "run-1", base)
    os.symlink(secret, ws / "leak")
    out = mgr.collect(deployment.scope, ws, {"change": PATCH_BINDING}, NODE, process_stopped=True)
    patch = deployment.artifacts.read(deployment.scope, out["change"])
    assert b"TOP-SECRET-VALUE" not in patch and b"new file mode 120000" in patch


def test_collect_trusts_only_a_stopped_process_and_the_patch_port(
    deployment: Any, mgr: GitWorkspaceManager
) -> None:
    base = mgr.base_snapshot(deployment.scope, "app")
    ws = mgr.materialize(deployment.scope, "run-1", base)
    with pytest.raises(Hold, match="before process stop"):
        mgr.collect(deployment.scope, ws, {"change": PATCH_BINDING}, NODE, process_stopped=False)
    with pytest.raises(Hold) as exc:
        mgr.collect(deployment.scope, ws, {"change": "out.json"}, NODE, process_stopped=True)
    assert exc.value.code == "OUTPUT_BINDINGS"
    json_node = {
        "produces": [{"name": "change", "media_type": "application/json", "required": True}]
    }
    with pytest.raises(Hold) as exc:
        mgr.collect(
            deployment.scope, ws, {"change": PATCH_BINDING}, json_node, process_stopped=True
        )
    assert exc.value.code == "OUTPUT_BINDINGS"


def test_descriptor_cannot_name_an_unregistered_repo_or_a_drifted_tree(
    deployment: Any, mgr: GitWorkspaceManager, repo: Path
) -> None:
    base = mgr.base_snapshot(deployment.scope, "app")
    value = __import__("json").loads(deployment.artifacts.read(deployment.scope, base))
    other = {**value, "repo": "elsewhere"}
    ref = deployment.artifacts.admit(deployment.scope, canonical(other), BASE_MEDIA)
    with pytest.raises(Hold) as exc:
        mgr.materialize(deployment.scope, "run-1", ref)
    assert exc.value.code == "GIT_REPO_UNKNOWN"
    drift = {**value, "tree": "0" * 40}
    ref = deployment.artifacts.admit(deployment.scope, canonical(drift), BASE_MEDIA)
    with pytest.raises(Hold) as exc:
        mgr.materialize(deployment.scope, "run-2", ref)
    assert exc.value.code == "GIT_BASE_DRIFT"


def test_workspace_is_never_reused_and_patches_are_bounded(
    deployment: Any, tmp_path: Path, repo: Path
) -> None:
    mgr = GitWorkspaceManager(
        tmp_path / "work", deployment.artifacts, {"app": repo}, max_patch_bytes=64
    )
    base = mgr.base_snapshot(deployment.scope, "app")
    ws = mgr.materialize(deployment.scope, "run-1", base)
    with pytest.raises(Hold, match="not automatically reused"):
        mgr.materialize(deployment.scope, "run-1", base)
    (ws / "big.txt").write_text("x" * 4096)
    with pytest.raises(Hold) as exc:
        mgr.collect(deployment.scope, ws, {"change": PATCH_BINDING}, NODE, process_stopped=True)
    assert exc.value.code == "PATCH_QUOTA"


def test_checkpoint_roundtrip_and_drift(deployment: Any, mgr: GitWorkspaceManager) -> None:
    base = mgr.base_snapshot(deployment.scope, "app")
    ws = mgr.materialize(deployment.scope, "run-1", base)
    (ws / "src" / "app.py").write_text("changed\n")
    checkpoint = mgr.snapshot(deployment.scope, ws)
    assert mgr.assert_matches(deployment.scope, ws, checkpoint)
    resumed = mgr.materialize(deployment.scope, "run-2", checkpoint)
    assert (resumed / "src" / "app.py").read_text() == "changed\n"
    (ws / "src" / "app.py").write_text("changed again\n")
    with pytest.raises(Hold, match="checkpoint"):
        mgr.assert_matches(deployment.scope, ws, checkpoint)


def test_registered_repo_must_be_a_real_checkout(deployment: Any, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(Hold) as exc:
        GitWorkspaceManager(tmp_path / "work", deployment.artifacts, {"x": plain})
    assert exc.value.code == "GIT_REPO"
