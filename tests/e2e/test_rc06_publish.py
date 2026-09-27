"""Work 018 S8 — publish a verified change as branch + draft PR, never touching the checkout.

Real git: a bare repository is the ``origin`` remote and pushes really happen. The PR step is a
recording stand-in for ``gh pr create`` (the real ``gh`` call is exercised in the real runs).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.errors import Conflict, Hold
from amplai_foundry.runtime.execution.publish import GitPublisher
from rc06_rig import approved, checkout, git, rig_with_codex


class Prs:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail_first = fail_first

    def __call__(self, repo: Path, head: str, base: str, title: str, body: str) -> str:
        if self.fail_first and not self.calls:
            self.calls.append(("failed", head, base))
            raise Hold("PR_CREATE", "simulated gh outage")
        self.calls.append((title, head, base))
        return "https://example.invalid/pr/1"


def setup(deployment: Any, tmp_path: Path, prs: Prs) -> tuple[Any, Any, GitPublisher, Path]:
    publisher_box: dict[str, GitPublisher] = {}
    rig, loop, _ = rig_with_codex(
        deployment, tmp_path, "right", publisher=lambda g: publisher_box["p"](g)
    )
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(rig.repo, "remote", "add", "origin", str(remote))
    git(rig.repo, "push", "-q", "origin", "main")
    # the operator is busy on another branch with uncommitted work
    git(rig.repo, "checkout", "-q", "-b", "feature")
    (rig.repo / "app.py").write_text("def value():\n    return 99  # wip\n")
    publisher = GitPublisher(rig.service, pr_creator=prs)
    publisher_box["p"] = publisher
    return rig, loop, publisher, remote


def test_verified_goal_becomes_a_pushed_branch_and_one_draft_pr(
    deployment: Any, tmp_path: Path
) -> None:
    prs = Prs()
    rig, loop, publisher, remote = setup(deployment, tmp_path, prs)
    before = checkout(rig.repo)
    main = git(rig.repo, "rev-parse", "main").strip()
    goal = approved(rig)
    record = loop.run_goal(goal)
    assert record["status"] == "published"
    pub = record["publication"]
    branch = "refs/heads/" + pub["branch"]
    assert git(remote, "rev-parse", branch).strip() == pub["commit"]
    assert git(remote, "rev-parse", pub["commit"] + "^").strip() == main
    assert "return 2" in git(remote, "show", pub["commit"] + ":app.py")
    assert prs.calls == [(prs.calls[0][0], pub["branch"], "main")] and pub["pr_url"]
    # idempotent: a second call neither pushes nor opens another PR
    assert publisher(goal)["commit"] == pub["commit"] and len(prs.calls) == 1
    # the operator's checkout (branch, HEAD, uncommitted edit) and main never moved
    assert checkout(rig.repo) == before
    assert git(rig.repo, "rev-parse", "main").strip() == main
    assert (rig.repo / "app.py").read_text() == "def value():\n    return 99  # wip\n"


def test_an_interrupted_publication_resumes_without_a_second_push(
    deployment: Any, tmp_path: Path
) -> None:
    prs = Prs(fail_first=True)
    rig, loop, publisher, _ = setup(deployment, tmp_path, prs)
    goal = approved(rig)
    record = loop.run_goal(goal)
    assert record["status"] == "verified" and record["publication"]["error"] == "PR_CREATE"
    assert rig.d.store.head(rig.d.scope, "publication", goal)["state"] == "pushed"
    done = publisher(goal)
    assert done["pr_url"] and [c[0] for c in prs.calls].count("failed") == 1
    assert len([c for c in prs.calls if c[0] != "failed"]) == 1


def test_publish_refuses_unverified_revoked_or_diverged_goals(
    deployment: Any, tmp_path: Path
) -> None:
    prs = Prs()
    rig, loop, publisher, _ = setup(deployment, tmp_path, prs)
    goal = approved(rig)
    with pytest.raises(Hold) as exc:
        publisher(goal)
    assert exc.value.code == "PUBLISH_NOT_VERIFIED"
    loop.publisher = None
    assert loop.run_goal(goal)["status"] == "verified"
    rig.service.revoke(rig.operator, goal)
    with pytest.raises(Hold) as exc:
        publisher(goal)
    assert exc.value.code == "PUBLISH_REVOKED"


def test_a_remote_branch_at_another_commit_is_never_overwritten(
    deployment: Any, tmp_path: Path
) -> None:
    prs = Prs()
    rig, loop, publisher, remote = setup(deployment, tmp_path, prs)
    goal = approved(rig)
    loop.publisher = None
    loop.run_goal(goal)
    other = git(rig.repo, "rev-parse", "main").strip()
    git(rig.repo, "push", "-q", "origin", f"{other}:refs/heads/amplai/{goal}")
    with pytest.raises(Conflict) as exc:
        publisher(goal)
    assert exc.value.code == "PUBLISH_BRANCH_EXISTS" and prs.calls == []
    assert git(remote, "rev-parse", f"refs/heads/amplai/{goal}").strip() == other
