"""Work 019 S4 (D) — one goal across two apps: dependency order, integration, linked PRs.

Stand-ins as in the rc06 rig (fixed planner, host "containers"); real git for both repos and
their bare remotes. The real two-app goal is recorded under specs/019-v3-completion/runs/.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.evaluation.observatory import Observatory
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.outcomes import PullRequestTracker
from amplai_foundry.runtime.execution.publish import GitPublisher
from rc06_rig import git, rig_two_apps


def two_app_goal(rig: Any, text: str = "value 2 end to end") -> str:
    submitted = rig.d.goals.submit(
        rig.actors.service, text=text, target_hints=["app", "consumer"], key="two-" + text[:8]
    )
    goal: str = submitted["goal_id"]
    return goal


def remote(repo: Path, tmp_path: Path, name: str) -> Path:
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    return bare


def test_two_apps_run_in_order_verify_together_and_publish_linked_prs(
    deployment: Any, tmp_path: Path
) -> None:
    box: dict[str, GitPublisher] = {}
    rig, loop, container, consumer = rig_two_apps(
        deployment, tmp_path, publisher=lambda g: box["p"](g)
    )
    remote(rig.repo, tmp_path, "app")
    remote(consumer, tmp_path, "consumer")
    created: list[tuple[str, str]] = []
    comments: list[tuple[str, str]] = []

    def prs(repo: Path, head: str, base: str, title: str, body: str) -> str:
        created.append((title, body))
        return f"https://example.invalid/pr/{len(created)}"

    box["p"] = GitPublisher(
        rig.service, pr_creator=prs, pr_linker=lambda repo, url, body: comments.append((url, body))
    )
    goal = two_app_goal(rig)
    plan = rig.service.plan(goal)
    assert plan["apps"] == ["app", "consumer"] and len(plan["work_items"]) == 2
    assert set(rig.planner.calls[-1]["saw_bases"]) == {"app", "consumer"}
    d = rig.d
    graph = d.store.get(d.scope, "workgraph", plan["graph_ref"])
    by_id = {n["node_id"]: n for n in graph["nodes"]}
    assert by_id["node-consumer"]["depends_on"] == ["node-app"]
    assert [c["from_node"] for c in by_id["node-consumer"]["consumes"]] == ["node-app"]
    rig.service.approve(rig.operator, goal)
    record = loop.run_goal(goal)
    assert record["status"] == "published"
    assert [(a["app"], a["outcome"]) for a in record["attempts"]] == [
        ("app", "pass"), ("consumer", "pass")
    ]  # fmt: skip
    # the consumer's agent saw the verified producer change
    assert "+    return 2" in container.prompts[1] and "is already verified" in container.prompts[1]
    assert "+    return 2" not in container.prompts[0]
    # one draft PR per app, linked both ways
    assert [t for t, _ in created] == [
        "[AMPLAI] value() returns 2 and the consumer renders it (app)",
        "[AMPLAI] value() returns 2 and the consumer renders it (consumer)",
    ]
    assert "https://example.invalid/pr/1" in created[1][1]
    assert comments == [
        ("https://example.invalid/pr/1", f"Part of AMPLAI goal `{goal}` with: https://example.invalid/pr/2")
    ]  # fmt: skip
    assert set(record["publication"]["publications"]) == {"app", "consumer"}
    assert git(consumer, "rev-parse", "HEAD").strip() == git(consumer, "rev-parse", "main").strip()

    # the goal's outcome is the aggregate of its PRs
    answers = {"https://example.invalid/pr/1": "MERGED", "https://example.invalid/pr/2": "OPEN"}
    commits = {a: p["commit"] for a, p in record["publication"]["publications"].items()}
    heads = {
        "https://example.invalid/pr/1": commits["app"],
        "https://example.invalid/pr/2": commits["consumer"],
    }

    def reader(repo: Path, url: str) -> dict[str, Any]:
        return {"state": answers[url], "headRefOid": heads[url]}

    tracker = PullRequestTracker(rig.service, reader=reader)
    tracker.sync()
    assert rig.service.plan_record(goal)["publication_outcome"]["state"] == "OPEN"
    answers["https://example.invalid/pr/2"] = "MERGED"
    tracker.sync()
    outcome = Observatory(d.store).summary(d.scope)["publication_outcomes"]
    assert outcome["merged"] == 1 and outcome["acceptance_rate"] == 1.0


def test_a_failed_integration_fails_the_goal_with_its_reason(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _container, _consumer = rig_two_apps(deployment, tmp_path, "multi-bad")
    goal = two_app_goal(rig)
    rig.service.plan(goal)
    rig.service.approve(rig.operator, goal)
    record = loop.run_goal(goal)
    # each app passed its own suite; together they do not
    assert [a["outcome"] for a in record["attempts"]] == ["pass", "pass"]
    assert record["status"] == "failed" and rig.published == []
    assert "INTEGRATION_FAILED" in record["reason"]
    assert "integration both exited 1" in record["reason"]
    assert rig.d.store.head(rig.d.scope, "goal", goal)["state"] == "failed"


def test_a_design_goal_names_exactly_one_app(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _container, _consumer = rig_two_apps(deployment, tmp_path)
    submitted = rig.d.goals.submit(
        rig.actors.service, text="design both", mode="design",
        target_hints=["app", "consumer"], key="design-two",
    )  # fmt: skip
    with pytest.raises(Hold) as held:
        rig.service.plan(submitted["goal_id"])
    assert held.value.code == "DESIGN_ONE_APP"


def test_a_work_item_cannot_come_after_itself_or_an_unknown_app(
    deployment: Any, tmp_path: Path
) -> None:
    rig, _loop, _container, _consumer = rig_two_apps(deployment, tmp_path)
    rig.planner.multi_value["work_items"][0]["after"] = ["nowhere"]
    with pytest.raises(Hold) as held:
        rig.service.plan(two_app_goal(rig, "bad order goal"))
    assert held.value.code == "PLANNING_ORDER"
