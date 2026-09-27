"""Work 019 S1 (A) — what happened to each draft PR (R1, D-078).

Real git publication to a bare remote (as in test_rc06_publish); the PR *state* reader is a
stand-in for ``gh pr view`` (the real ``gh`` read is exercised on PRs #12-#14 in the real runs).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from amplai_foundry.evaluation.observatory import Observatory
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.outcomes import PullRequestTracker
from amplai_foundry.runtime.execution.publish import GitPublisher
from rc06_rig import approved, git, rig_with_codex


class States:
    """Stand-in for the operator's ``gh pr view``: a scripted PR state."""

    def __init__(self) -> None:
        self.value: dict[str, Any] = {"state": "OPEN", "headRefOid": None}
        self.fail = False
        self.calls: list[str] = []

    def __call__(self, repo: Path, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if self.fail:
            raise Hold("PR_STATE", "simulated gh outage")
        return dict(self.value)


def published(deployment: Any, tmp_path: Path) -> tuple[Any, Any, str, dict[str, Any]]:
    box: dict[str, GitPublisher] = {}
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right", publisher=lambda g: box["p"](g))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(rig.repo, "remote", "add", "origin", str(remote))
    git(rig.repo, "push", "-q", "origin", "main")
    box["p"] = GitPublisher(rig.service, pr_creator=lambda *_: "https://example.invalid/pr/7")
    goal = approved(rig)
    record = loop.run_goal(goal)
    assert record["status"] == "published"
    return rig, loop, goal, record["publication"]


def events(rig: Any, goal: str) -> list[str]:
    rows = rig.d.store.conn.execute(
        "SELECT event_type FROM events WHERE aggregate_id=? AND event_type LIKE 'publication.%' "
        "ORDER BY seq",
        (goal,),
    ).fetchall()
    return [r["event_type"] for r in rows]


def outcomes(rig: Any) -> dict[str, Any]:
    value: dict[str, Any] = Observatory(rig.d.store).summary(rig.d.scope)["publication_outcomes"]
    return value


def test_an_open_pr_is_recorded_without_a_transition(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, goal, publication = published(deployment, tmp_path)
    states = States()
    states.value["headRefOid"] = publication["commit"]
    PullRequestTracker(rig.service, reader=states).sync()
    assert events(rig, goal) == ["publication.opened"]
    outcome = rig.service.plan_record(goal)["publication_outcome"]
    assert outcome["state"] == "OPEN" and outcome["revised"] is False
    assert outcomes(rig) == {
        "opened": 1, "undecided": 1, "merged": 0, "closed": 0, "revised": 0,
        "acceptance_rate": None, "revised_share": 0.0,
    }  # fmt: skip


def test_a_merge_is_recorded_once(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, goal, publication = published(deployment, tmp_path)
    states = States()
    states.value = {"state": "MERGED", "headRefOid": publication["commit"]}
    tracker = PullRequestTracker(rig.service, reader=states)
    tracker.sync()
    tracker.sync()
    assert events(rig, goal) == ["publication.opened", "publication.merged"]
    assert outcomes(rig)["acceptance_rate"] == 1.0 and outcomes(rig)["undecided"] == 0


def test_human_commits_then_close_count_as_revised_and_rejected(
    deployment: Any, tmp_path: Path
) -> None:
    rig, _loop, goal, _publication = published(deployment, tmp_path)
    states = States()
    states.value = {"state": "OPEN", "headRefOid": "f" * 40}  # someone pushed on the branch
    tracker = PullRequestTracker(rig.service, reader=states)
    tracker.sync()
    states.value = {"state": "CLOSED", "headRefOid": "f" * 40}
    tracker.sync()
    assert events(rig, goal) == ["publication.opened", "publication.revised", "publication.closed"]
    got = outcomes(rig)
    assert got["closed"] == 1 and got["revised"] == 1
    assert got["acceptance_rate"] == 0.0 and got["revised_share"] == 1.0


def test_an_unreadable_pr_stays_unknown(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, goal, _publication = published(deployment, tmp_path)
    states = States()
    states.fail = True
    PullRequestTracker(rig.service, reader=states).sync()
    assert "publication_outcome" not in rig.service.plan_record(goal)
    assert outcomes(rig)["acceptance_rate"] is None and outcomes(rig)["undecided"] == 1


def test_the_loop_syncs_outcomes_when_due(deployment: Any, tmp_path: Path) -> None:
    rig, loop, goal, publication = published(deployment, tmp_path)
    states = States()
    states.value = {"state": "MERGED", "headRefOid": publication["commit"]}
    now = [1000.0]
    loop.tracker = PullRequestTracker(
        rig.service, reader=states, clock=lambda: now[0], interval_seconds=600
    )
    loop.idle_tick()
    assert states.calls == [publication["pr_url"]]
    loop.idle_tick()  # not due yet
    assert len(states.calls) == 1
    now[0] += 601
    loop.idle_tick()
    assert len(states.calls) == 1  # merged is final: nothing left to read
    assert events(rig, goal)[-1] == "publication.merged"
