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
    CHANGE_MEDIA,
    PATCH_BINDING,
    GitWorkspaceManager,
)

NODE = {"produces": [{"name": "change", "media_type": CHANGE_MEDIA, "required": True}]}


def patch_of(deployment: Any, mgr: GitWorkspaceManager, out: dict[str, Any]) -> bytes:
    raw = deployment.artifacts.read(deployment.scope, out["change"])
    return mgr.read_change(deployment.scope, raw)[1]


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
    assert (ws / ".gitignore").exists()
    # the copy's .git is a fresh repository: no remote, one commit, none of the original history
    assert git(ws, "remote") == "" and git(ws, "rev-list", "--count", "HEAD").strip() == "1"
    assert git(ws, "rev-parse", "HEAD").strip() != git(repo, "rev-parse", "HEAD").strip()
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
    patch = patch_of(deployment, mgr, out)
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
    patch = patch_of(deployment, mgr, out)
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


def test_copies_are_git_checkouts_whose_head_is_what_is_judged(
    deployment: Any, tmp_path: Path, repo: Path
) -> None:
    # found by the first real run: repo tests read committed files through git, and a copy
    # without .git made the untouched base fail its own suite
    mgr = GitWorkspaceManager(tmp_path / "w", deployment.artifacts, {"app": repo})
    base = mgr.base_snapshot(deployment.scope, "app")
    ws = mgr.materialize(deployment.scope, "run-1", base)
    assert git(ws, "show", "HEAD:README.md") == "app\n"
    assert git(ws, "status", "--porcelain") == ""
    (ws / "src" / "app.py").write_text("def greet():\n    return 'hello'\n")
    out = mgr.collect(deployment.scope, ws, {"change": PATCH_BINDING}, NODE, process_stopped=True)
    patch = patch_of(deployment, mgr, out)
    assert b".git/" not in patch and b"return 'hello'" in patch
    raw = deployment.artifacts.read(deployment.scope, out["change"])
    judged, _ = mgr.materialize_change(deployment.scope, "run-2", raw)
    assert "return 'hello'" in git(judged, "show", "HEAD:src/app.py")
    assert git(judged, "status", "--porcelain") == ""


def test_registered_repo_must_be_a_real_checkout(deployment: Any, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(Hold) as exc:
        GitWorkspaceManager(tmp_path / "work", deployment.artifacts, {"x": plain})
    assert exc.value.code == "GIT_REPO"


def test_archive_members_are_checked_without_the_311_4_data_filter(
    monkeypatch: pytest.MonkeyPatch, deployment: Any, mgr: GitWorkspaceManager, repo: Path
) -> None:
    import tarfile

    # given: the worker image's Python 3.11.2 has no tarfile.data_filter (found in the container)
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    real = tarfile.TarFile.extractall

    def no_filter(self: tarfile.TarFile, *args: Any, **kwargs: Any) -> None:
        assert "filter" not in kwargs
        real(self, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extractall", no_filter)
    os.symlink("src/app.py", repo / "inside")
    git(repo, "add", "inside")
    git(repo, "commit", "-q", "-m", "inside link")
    base = mgr.base_snapshot(deployment.scope, "app")
    ws = mgr.materialize(deployment.scope, "run-1", base)
    assert (ws / "inside").is_symlink()
    # an escaping link in the base commit is refused before anything is written
    os.symlink("../../outside", repo / "escape")
    git(repo, "add", "escape")
    git(repo, "commit", "-q", "-m", "escape link")
    with pytest.raises(Hold) as exc:
        mgr.materialize(deployment.scope, "run-2", mgr.base_snapshot(deployment.scope, "app"))
    assert exc.value.code == "ARCHIVE_LINK"
