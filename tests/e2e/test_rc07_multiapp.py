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
from amplai_foundry.runtime.git_host import PullRequestState
from amplai_foundry.runtime.git_host.github import GitHubHost, run_process
from rc06_rig import RecordingGitHub, git, local_github, rig_two_apps


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
        rig.service,
        git_host=local_github(),
        pr_creator=prs,
        pr_linker=lambda repo, url, body: comments.append((url, body)),
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

    def reader(repo: Path, url: str) -> PullRequestState:
        return PullRequestState(answers[url], head_commit=heads[url])

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


def test_the_link_comment_is_recorded_before_it_is_posted(deployment: Any, tmp_path: Path) -> None:
    box: dict[str, GitPublisher] = {}
    rig, loop, _container, consumer = rig_two_apps(
        deployment, tmp_path, publisher=lambda g: box["p"](g)
    )
    remote(rig.repo, tmp_path, "app")
    remote(consumer, tmp_path, "consumer")
    created: list[str] = []
    posted: list[str] = []
    seen: list[bool] = []

    def prs(repo: Path, head: str, base: str, title: str, body: str) -> str:
        created.append(title)
        return f"https://example.invalid/pr/{len(created)}"

    def linker(repo: Path, url: str, body: str) -> None:
        record = box["p"]._record(f"{goal}:app")
        seen.append(bool(record and record["data"].get("linked")))
        if not posted and len(seen) == 1:
            raise RuntimeError("gh is down")  # the first comment fails
        posted.append(url)

    box["p"] = GitPublisher(rig.service, git_host=local_github(), pr_creator=prs, pr_linker=linker)
    goal = two_app_goal(rig)
    rig.service.plan(goal)
    rig.service.approve(rig.operator, goal)
    record = loop.run_goal(goal)
    # the failed comment leaves the goal verified with the error, the link recorded as not made
    assert record["status"] == "verified" and record["publication"]["error"] == "RuntimeError"
    assert box["p"]._record(f"{goal}:app")["data"]["linked"] is False  # type: ignore[index]
    # a retry reuses both PRs and comments once; it was recorded before it was posted
    again = box["p"](goal)
    assert len(created) == 2 and posted == ["https://example.invalid/pr/1"]
    assert seen == [True, True] and again["publications"]["app"]["linked"] is True
    box["p"](goal)
    assert posted == ["https://example.invalid/pr/1"]  # never twice


def test_a_remote_on_another_host_stops_every_app_before_any_push(
    deployment: Any, tmp_path: Path
) -> None:
    """Work 034 OD-15 (D-130, PR #93 review P3-d): the publisher checks every app's remote host
    first, so the first app is not pushed when the second app's remote is elsewhere."""
    box: dict[str, GitPublisher] = {}
    rig, loop, _container, consumer = rig_two_apps(
        deployment, tmp_path, publisher=lambda g: box["p"](g)
    )
    app_remote = remote(rig.repo, tmp_path, "app")
    consumer_remote = remote(consumer, tmp_path, "consumer")

    def run(
        args: list[str], cwd: Path, env: dict[str, str] | None, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        if args[:3] == ["git", "remote", "get-url"]:
            same = Path(cwd).resolve() == rig.repo.resolve()
            url = "https://github.com/o/app.git" if same else "git@elsewhere.example:o/c.git"
            return subprocess.CompletedProcess(args, 0, (url + "\n").encode(), b"")
        return run_process(args, cwd, env, timeout)

    box["p"] = GitPublisher(
        rig.service,
        git_host=GitHubHost(run=run),
        pr_creator=lambda *_: "https://github.com/o/app/pull/1",
        pr_linker=lambda *_: None,
    )
    goal = two_app_goal(rig)
    rig.service.plan(goal)
    rig.service.approve(rig.operator, goal)
    record = loop.run_goal(goal)
    assert record["status"] == "verified"
    assert record["publication"]["error"] == "GIT_HOST_MISMATCH"
    for bare in (app_remote, consumer_remote):
        assert git(bare, "for-each-ref", "refs/heads/amplai").strip() == ""  # nothing pushed


def test_the_production_pr_path_opens_and_links_one_pr_per_app(
    deployment: Any, tmp_path: Path
) -> None:
    """Work 034 OD-15 (PR #93 regression review P2-a): no PR stand-in injected. Every app's
    remote is checked first, then each app is pushed, gets its draft PR through the module, and
    the first PR is linked to the second with one comment."""
    box: dict[str, GitPublisher] = {}
    rig, loop, _container, consumer = rig_two_apps(
        deployment, tmp_path, publisher=lambda g: box["p"](g)
    )
    remote(rig.repo, tmp_path, "app")
    remote(consumer, tmp_path, "consumer")
    host = RecordingGitHub()
    box["p"] = GitPublisher(rig.service, git_host=GitHubHost(run=host))
    goal = two_app_goal(rig)
    rig.service.plan(goal)
    rig.service.approve(rig.operator, goal)
    record = loop.run_goal(goal)
    assert record["status"] == "published", record.get("publication")
    parts = record["publication"]["publications"]
    assert [p["branch"] for p in parts.values()] == [
        f"amplai/{goal}-app",
        f"amplai/{goal}-consumer",
    ]
    lookups = host.of("git", "remote", "get-url")
    assert set(map(tuple, lookups)) == {("git", "remote", "get-url", "--push", "--all", "origin")}
    assert len(lookups) == 6  # 2 pre-push checks, then push and PR for each app
    first_push = host.calls.index(host.of("git", "push")[0])
    assert host.calls[:first_push].count(lookups[0]) == 3  # both apps checked before any push
    creates = host.of("gh", "pr", "create")
    assert [c[3:8] for c in creates] == [
        ["--draft", "--head", f"amplai/{goal}-app", "--base", "main"],
        ["--draft", "--head", f"amplai/{goal}-consumer", "--base", "main"],
    ]
    assert host.of("gh", "pr", "comment") == [
        ["gh", "pr", "comment", "https://github.com/o/r/pull/1", "--body",
         f"Part of AMPLAI goal `{goal}` with: https://github.com/o/r/pull/2"],
    ]  # fmt: skip
    assert parts["app"]["linked"] is True
