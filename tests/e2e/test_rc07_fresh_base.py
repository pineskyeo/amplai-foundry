"""D-086 — a plan starts from the remote base branch fetched now, never from a stale clone.

Found on the real product: the app clone's local ``main`` was never pulled, so goals planned on
an old commit (a draft PR then conflicted with main, and the planner could not see a decision
merged since). Stand-ins as in the rc06 rig; the remote is a local bare repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.errors import Hold
from rc06_rig import git, rig_with_codex, submit


def advance(remote: Path, tmp_path: Path) -> str:
    """Another client pushes a commit to the remote's main; returns the new head."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    (other / "notes.txt").write_text("landed on main elsewhere\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "elsewhere")
    git(other, "push", "-q", "origin", "main")
    return git(remote, "rev-parse", "main").strip()


def with_remote(rig: Any, tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    # -b main: a runner whose git defaults to master would leave the bare HEAD dangling
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git(rig.repo, "remote", "add", "origin", str(remote))
    git(rig.repo, "push", "-q", "origin", "main")
    return remote


def test_a_plan_starts_from_the_remote_branch_fetched_now(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    remote = with_remote(rig, tmp_path)
    head = advance(remote, tmp_path)
    local = git(rig.repo, "rev-parse", "main").strip()
    status = git(rig.repo, "status", "--porcelain", "--branch")
    assert head != local
    plan = rig.service.plan(submit(rig))
    assert plan["base_commit"] == head
    # the operator's branch and checkout (main is checked out here) are not moved
    assert git(rig.repo, "rev-parse", "main").strip() == local
    assert git(rig.repo, "status", "--porcelain", "--branch") == status


def test_an_unreachable_remote_stops_the_plan(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    git(rig.repo, "remote", "add", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(Hold) as held:
        rig.service.plan(submit(rig))
    assert held.value.code == "BASE_FETCH"


def test_a_repository_without_the_remote_plans_on_its_local_branch(
    deployment: Any, tmp_path: Path
) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    plan = rig.service.plan(submit(rig))
    assert plan["base_commit"] == git(rig.repo, "rev-parse", "main").strip()
