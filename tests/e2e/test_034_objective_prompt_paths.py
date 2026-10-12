"""Every agent-facing path gets a long objective whole (option A, 2026-10-12; PR #85 review P1).

A workgraph node whose objective is over the node bound (4096) names it by reference
(``text_bounds.node_objective``). That reference sentence must never reach an agent or a PR:
the implementer, the reviewer, the investigator, the chain and merge sections and the PR body
read the full text (``text_bounds.full_objective``). Each test runs a 7000-character goal
objective through one path and checks that the objective's last 300 characters are there and the
reference sentence is not.

Stand-ins as in the Work 033 S9 strategy tests (scripted executor processes, fake read-only
turns) and the rc07 two-app rig (host "containers", bare git remotes).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.execution import text_bounds
from amplai_foundry.runtime.execution.publish import GitPublisher
from e2e.test_033_s9_strategies import (
    APPROVE,
    AUX,
    CHANGES,
    FINDINGS,
    LEAD,
    ORCH,
    PARTS,
    World,
    chain_script,
    make_world,
    orchestrated,
    outcomes,
    reviewed,
)
from rc06_rig import git, rig_two_apps

# the sentence ``node_objective`` puts in a node in place of a long objective
REFERENCE = "over the workgraph node bound"
OBJECTIVE = "Make value() return 2. " + "r" * 6900 + " CLOSING RULES: the CLI prints the value."
TAIL = OBJECTIVE[-300:]


def _long_world(deployment: Any, tmp_path: Path, script: Any) -> World:
    assert len(OBJECTIVE) > 7000 - 100 and TAIL not in OBJECTIVE[:4000]
    w = make_world(deployment, tmp_path, script)
    w.rig.planner.draft_value["objective"] = OBJECTIVE
    return w


def _whole(text: str) -> None:
    assert TAIL in text, "the objective's end did not reach this text"
    assert REFERENCE not in text, "the node's reference sentence reached an agent"


def test_the_implementer_and_the_reviewer_get_the_whole_objective(
    deployment: Any, tmp_path: Path
) -> None:
    w = _long_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = [CHANGES, APPROVE]
    strategy = w.comps.strategy(
        "generator_reviewer", {"reviewer_role": "reviewer", "max_rounds": 2}, **AUX
    )
    goal = w.approved(strategy)
    (node,) = w.nodes(goal)
    assert REFERENCE in node["objective"]  # the graph names it by reference
    record = w.loop.run_goal(goal)
    assert record["status"] == "published" and outcomes(record) == ["pass"]
    _whole(w.agents.prompts[0])  # the implementer
    reviews = w.turns.of("review")
    assert len(reviews) == 2
    for review in reviews:
        _whole(review.prompt)


def test_the_investigator_gets_the_whole_objective(deployment: Any, tmp_path: Path) -> None:
    w = _long_world(deployment, tmp_path, reviewed)
    w.turns.outputs["investigate"] = FINDINGS
    record = w.run(w.comps.strategy("parallel_readonly", {"steps": ["files_to_change"]}, **AUX))
    assert record["status"] == "published"
    (investigator,) = w.turns.of("investigate")
    _whole(investigator.prompt)
    _whole(w.agents.prompts[0])


def test_a_chain_part_gets_the_whole_objective_and_its_already_applied_section(
    deployment: Any, tmp_path: Path
) -> None:
    w = _long_world(deployment, tmp_path, chain_script)
    w.turns.outputs["split"] = {"parts": PARTS}
    record = w.run(w.comps.strategy("workgraph_split", {"max_nodes": 3}, **AUX))
    assert record["status"] == "published" and outcomes(record) == ["pass", "pass"]
    second = w.agents.prompts[1]
    assert "Already applied in this directory" in second
    assert "- Part one: value() returns 2" in second
    for prompt in w.agents.prompts:
        _whole(prompt)


def test_the_integration_node_gets_the_whole_objective_and_its_merged_section(
    deployment: Any, tmp_path: Path
) -> None:
    w = _long_world(deployment, tmp_path, orchestrated)
    w.turns.outputs["lead"] = LEAD
    goal = w.approved(w.comps.strategy("orchestrator", ORCH, **AUX))
    *_parts, integration = w.nodes(goal)
    # "Integrate the merged parts into one change for: <objective>": over the node bound
    assert integration["node_id"] == "node-app.int" and REFERENCE in integration["objective"]
    record = w.loop.run_goal(goal)
    assert record["status"] == "published" and outcomes(record) == ["pass", "pass", "pass"]
    last = w.agents.prompts[2]
    assert "Merged parts (applied in this directory, each verified on its own):" in last
    assert "in app: Integrate the merged parts into one change for: " + OBJECTIVE in last
    for prompt in w.agents.prompts:
        _whole(prompt)


def _remote(repo: Path, tmp_path: Path, name: str) -> None:
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")


def test_the_pr_body_of_a_node_named_by_reference_has_the_whole_objective(
    deployment: Any, tmp_path: Path
) -> None:
    box: dict[str, GitPublisher] = {}
    rig, loop, container, consumer = rig_two_apps(
        deployment, tmp_path, publisher=lambda g: box["p"](g)
    )
    _remote(rig.repo, tmp_path, "app")
    _remote(consumer, tmp_path, "consumer")
    bodies: list[str] = []

    def prs(repo: Path, head: str, base: str, title: str, body: str) -> str:
        bodies.append(body)
        return f"https://example.invalid/pr/{len(bodies)}"

    box["p"] = GitPublisher(rig.service, pr_creator=prs, pr_linker=lambda *a: None)
    # the app's item carries the whole goal: its node names the objective by reference
    rig.planner.multi_value["objective"] = OBJECTIVE
    rig.planner.multi_value["work_items"][0]["objective"] = ""
    submitted = rig.d.goals.submit(
        rig.actors.service, text="long two-app goal", target_hints=["app", "consumer"],
        key="two-long",
    )  # fmt: skip
    goal = submitted["goal_id"]
    plan = rig.service.plan(goal)
    graph = rig.d.store.get(rig.d.scope, "workgraph", plan["graph_ref"])
    node = next(n for n in graph["nodes"] if n["node_id"] == "node-app")
    assert REFERENCE in node["objective"]
    assert text_bounds.full_objective(plan, node) == OBJECTIVE
    rig.service.approve(rig.operator, goal)
    record = loop.run_goal(goal)
    assert record["status"] == "published" and len(bodies) == 2
    app_body = bodies[0]
    assert "This PR: " + OBJECTIVE + " (app)" in app_body
    _whole(app_body)
    for prompt in container.prompts:
        _whole(prompt)
