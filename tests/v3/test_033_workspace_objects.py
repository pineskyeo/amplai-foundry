"""Operator decision (D), 2026-10-08: a trial workspace holds no git object beyond the base commit.

interfaces.md clarification "Answer Lookup And Workspace Objects (2026-10-08)". The real bench base
repo ``~/.amplai/repos/amplai-bench-app`` (read 2026-10-08) has one commit and 32 unreachable blobs
(73 objects, 41 reachable); a workspace materialized from it held exactly the 41 objects of the
base commit's tree plus its own commit, and ``git fsck --unreachable`` found nothing. These tests
pin that with a source repository that also has a later commit, a branch, a tag, a reflog and
dangling blobs. Real ``GitWorkspaceManager`` and git; no container.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.sandbox.git_workspace import GitWorkspaceManager

SOLUTION = "def lint_orders(path):\n    return 'the answer'\n"


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_OPTIONAL_LOCKS": "0"}  # fmt: skip
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, input=stdin, capture_output=True, text=True, check=True
    ).stdout


def odb(repo: Path) -> set[str]:
    out = git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    return set(out.split())


def objects_of(repo: Path, rev: str) -> set[str]:
    return {line.split()[0] for line in git(repo, "rev-list", "--objects", rev).splitlines()}


@pytest.fixture
def source(tmp_path: Path) -> dict[str, Any]:
    """A base commit, then history an agent must never see: a later commit with the solution on
    another branch and a tag, an amended commit (reflog only) and dangling blobs."""
    r = tmp_path / "bench"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "stockroom").mkdir()
    (r / "stockroom" / "orders.py").write_text("def read(path):\n    return []\n")
    (r / "README.md").write_text("bench\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    base = git(r, "rev-parse", "HEAD").strip()
    git(r, "checkout", "-q", "-b", "solutions")
    (r / "stockroom" / "lint.py").write_text(SOLUTION)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "solution")
    git(r, "tag", "v-solution")
    (r / "stockroom" / "lint.py").write_text(SOLUTION + "# amended\n")
    git(r, "commit", "-q", "-a", "--amend", "-m", "solution amended")
    later = git(r, "rev-parse", "HEAD").strip()
    git(r, "checkout", "-q", "main")
    dangling = git(r, "hash-object", "-w", "--stdin", stdin="a dangling answer\n").strip()
    return {"repo": r, "base": base, "later": later, "dangling": dangling}


@pytest.fixture
def mgr(deployment: Any, tmp_path: Path, source: dict[str, Any]) -> GitWorkspaceManager:
    return GitWorkspaceManager(tmp_path / "work", deployment.artifacts, {"bench": source["repo"]})


def test_the_source_has_history_beyond_the_base(source: dict[str, Any]) -> None:
    r = source["repo"]
    # base, the tagged solution and its amended commit on the branch
    assert git(r, "rev-list", "--all", "--count").strip() == "3"
    assert source["dangling"] in git(r, "fsck", "--unreachable", "--no-reflogs")


def test_a_workspace_holds_only_the_base_commits_objects(
    deployment: Any, mgr: GitWorkspaceManager, source: dict[str, Any]
) -> None:
    r = source["repo"]
    ws = mgr.materialize(deployment.scope, "run-1", mgr.base_snapshot(deployment.scope, "bench"))
    head = git(ws, "rev-parse", "HEAD").strip()
    tree_objects = objects_of(r, source["base"]) - {source["base"]}
    # exactly the base tree's objects and the workspace's own commit: nothing else in the odb
    assert odb(ws) == tree_objects | {head}
    assert git(ws, "rev-parse", "HEAD^{tree}") == git(r, "rev-parse", source["base"] + "^{tree}")
    assert git(ws, "fsck", "--unreachable", "--no-reflogs", "--full") == ""
    assert git(ws, "rev-list", "--all", "--count").strip() == "1"
    assert git(ws, "for-each-ref", "--format=%(refname)").split() == ["refs/heads/amplai"]
    assert not list((ws / ".git" / "objects" / "pack").iterdir())
    assert not (ws / ".git" / "objects" / "info" / "alternates").exists()
    # no object of the later commit, the tag, the amended commit or the dangling blob
    hidden = objects_of(r, "--all") - objects_of(r, source["base"])
    hidden |= {source["dangling"], *odb(r)} - objects_of(r, source["base"])
    assert hidden and not hidden & odb(ws)
    probe = subprocess.run(["git", "cat-file", "-e", source["later"]], cwd=ws, capture_output=True)
    assert probe.returncode != 0
    assert SOLUTION not in "".join(p.read_text() for p in ws.rglob("*.py"))


def test_a_repair_copy_adds_no_object(
    deployment: Any, mgr: GitWorkspaceManager, source: dict[str, Any]
) -> None:
    """A later attempt starts from the base plus the previous attempt's patch, applied as
    uncommitted changes (``git apply`` without the index writes no object)."""
    base = mgr.base_snapshot(deployment.scope, "bench")
    first = mgr.materialize(deployment.scope, "run-2", base)
    (first / "stockroom" / "orders.py").write_text("def read(path):\n    return [1]\n")
    repair = mgr.snapshot(deployment.scope, first)
    ws = mgr.materialize(deployment.scope, "run-3", repair)
    assert "return [1]" in (ws / "stockroom" / "orders.py").read_text()
    head = git(ws, "rev-parse", "HEAD").strip()
    assert odb(ws) == (objects_of(source["repo"], source["base"]) - {source["base"]}) | {head}
    assert git(ws, "fsck", "--unreachable", "--no-reflogs") == ""
