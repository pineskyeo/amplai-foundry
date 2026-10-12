"""Planner and operator text is never cut (operator decision 2026-10-12, option A).

Pilot calibration 10 (corpus 2.1.0): 16 tasks had objectives over 4000 characters, and six feature
tasks failed in every cell because ``_clean_draft`` cut the objective at 4000 and the reader/CLI
rules at its end never reached the implementer. Text now reaches the contract, the workgraph and
the prompt whole, up to the frozen 3.0.0 wire bounds (contract objective 8192, node objective and
item text 4096); a longer value Holds with a coded error instead of being cut.

Stand-ins as in the rc06 rig (fixed planner, host "containers").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution import text_bounds
from amplai_foundry.runtime.execution.strategy_runner import StrategyChoice
from rc06_rig import rig_two_apps, rig_with_codex, submit, with_permissions

REPO = Path(__file__).resolve().parents[2]
TASKS = REPO / "specs" / "033-harness-taxonomy" / "corpus" / "tasks"


def _long(n: int, tail: str = "END-OF-OBJECTIVE") -> str:
    """``n`` characters whose last characters are ``tail`` (a cut would lose it)."""
    return "o" * (n - len(tail)) + tail


def _plan(rig: Any, text: str = "make value return 2") -> tuple[str, dict[str, Any]]:
    goal = submit(rig, text)
    return goal, rig.service.plan(goal)


def _contract_and_graph(rig: Any, plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    d = rig.d
    contract = d.store.get(d.scope, "goal-contract", plan["contract_ref"])
    graph = d.store.get(d.scope, "workgraph", plan["graph_ref"])
    return contract, graph


def _held(call: Any) -> Hold:
    with pytest.raises(Hold) as held:
        call()
    return held.value


# -- one app: the contract objective up to 8192 ------------------------------------------------


def test_a_7000_character_objective_reaches_the_contract_and_the_prompt_whole(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    objective = _long(7000)
    rig.planner.draft_value["objective"] = objective
    _goal, plan = _plan(rig)
    contract, graph = _contract_and_graph(rig, plan)
    assert contract["objective"] == objective
    assert plan["draft"]["objective"] == objective
    assert plan["work_items"][0]["objective"] == objective
    # the node bound is 4096: the node names the objective by reference, never a cut prefix
    (node,) = graph["nodes"]
    assert len(node["objective"]) <= text_bounds.NODE_OBJECTIVE
    assert "7000 characters" in node["objective"] and not node["objective"].startswith("ooo")
    assert text_bounds.full_objective(plan, node) == objective
    prompt = loop.prompt(contract, plan, None)
    assert "Objective: " + objective + "\n" in prompt


def test_an_objective_of_exactly_8192_characters_is_kept(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    rig.planner.draft_value["objective"] = _long(8192)
    _goal, plan = _plan(rig)
    contract, _graph = _contract_and_graph(rig, plan)
    assert contract["objective"] == _long(8192)


def test_an_objective_over_8192_characters_holds_and_is_not_cut(
    deployment: Any, tmp_path: Path
) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    rig.planner.draft_value["objective"] = "  " + _long(8193) + "  "
    goal = submit(rig)
    held = _held(lambda: rig.service.plan(goal))
    assert held.code == "PLANNING_OBJECTIVE_LENGTH"
    assert held.details == {"field": "objective", "length": 8193, "limit": 8192}
    assert "split" in held.message


def test_an_objective_within_the_node_bound_is_the_node_objective(
    deployment: Any, tmp_path: Path
) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    rig.planner.draft_value["objective"] = _long(4096)
    _goal, plan = _plan(rig)
    _contract, graph = _contract_and_graph(rig, plan)
    assert graph["nodes"][0]["objective"] == _long(4096)


@pytest.mark.parametrize(
    ("key", "value", "field"),
    [
        ("acceptance", [{"statement": "a" * 4097, "verifier": "check"}], "acceptance[0].statement"),
        ("non_goals", ["ok", "n" * 4097], "non_goals[1]"),
        ("constraints", ["c" * 4097], "constraints[0]"),
        ("assumptions", ["s" * 4097], "assumptions[0]"),
        ("in_scope", ["i" * 4097], "in_scope[0]"),
        ("questions", ["q" * 4097], "questions[0]"),
        ("summary", "m" * 4097, "summary"),
    ],
)
def test_other_draft_text_over_4096_holds(
    deployment: Any, tmp_path: Path, key: str, value: Any, field: str
) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    rig.planner.draft_value[key] = value
    goal = submit(rig)
    held = _held(lambda: rig.service.plan(goal))
    assert held.code == "PLANNING_TEXT_LENGTH"
    assert held.details == {"field": field, "length": 4097, "limit": 4096}


def test_draft_text_of_exactly_4096_characters_is_kept(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    statement = _long(4096, "STATEMENT-END")
    constraint = _long(4096, "CONSTRAINT-END")
    rig.planner.draft_value["acceptance"] = [{"statement": statement, "verifier": "check"}]
    rig.planner.draft_value["constraints"] = [constraint]
    _goal, plan = _plan(rig)
    contract, _graph = _contract_and_graph(rig, plan)
    assert contract["acceptance"][0]["statement"] == statement
    prompt = loop.prompt(contract, plan, None)
    assert statement in prompt and constraint in prompt


# -- the affected corpus tasks (regression) ----------------------------------------------------


@pytest.mark.parametrize(
    "task_id",
    ["feature-warehouse-transfers", "feature-customer-returns", "feature-price-lists"],
)
def test_an_affected_corpus_task_keeps_its_closing_rules_in_the_prompt(
    deployment: Any, tmp_path: Path, task_id: str
) -> None:
    """The real shape: the objective's last lines (the reader/CLI rules) were lost to the cut."""
    meta = json.loads((TASKS / task_id / "task.json").read_text())
    objective = meta["objective"].strip()
    assert 4000 < len(objective) <= text_bounds.CONTRACT_OBJECTIVE
    tail = objective[-300:]
    assert tail not in objective[:4000]  # the cut would have lost it
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    rig.planner.draft_value["objective"] = meta["objective"]
    _goal, plan = _plan(rig)
    contract, _graph = _contract_and_graph(rig, plan)
    assert contract["objective"] == objective
    prompt = loop.prompt(contract, plan, None)
    assert "Objective: " + objective in prompt and tail in prompt


# -- several apps: a work item's own objective up to 4096 --------------------------------------


def _two_app_goal(rig: Any, text: str = "value 2 end to end") -> str:
    submitted = rig.d.goals.submit(
        rig.actors.service, text=text, target_hints=["app", "consumer"], key="two-" + text[:8]
    )
    goal: str = submitted["goal_id"]
    return goal


def test_a_work_item_objective_of_4096_reaches_its_node_and_the_prompt(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _container, _consumer = rig_two_apps(deployment, tmp_path)
    part = _long(4096, "PART-END")
    rig.planner.multi_value["work_items"][0]["objective"] = part
    plan = rig.service.plan(_two_app_goal(rig))
    contract, graph = _contract_and_graph(rig, plan)
    node = next(n for n in graph["nodes"] if n["node_id"] == "node-app")
    assert node["objective"] == part
    assert f"This part of the goal, in app: {part}\n" in loop.prompt(
        contract, plan, None, node=node
    )


def test_a_work_item_objective_over_4096_holds(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _container, _consumer = rig_two_apps(deployment, tmp_path)
    rig.planner.multi_value["work_items"][1]["objective"] = "p" * 4097
    goal = _two_app_goal(rig)
    held = _held(lambda: rig.service.plan(goal))
    assert held.code == "PLANNING_OBJECTIVE_LENGTH"
    assert held.details == {"field": "work_items[1].objective", "length": 4097, "limit": 4096}


def test_a_work_item_without_an_objective_carries_the_whole_long_goal(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _container, _consumer = rig_two_apps(deployment, tmp_path)
    objective = _long(6000)
    rig.planner.multi_value["objective"] = objective
    rig.planner.multi_value["work_items"][0]["objective"] = ""
    plan = rig.service.plan(_two_app_goal(rig))
    contract, graph = _contract_and_graph(rig, plan)
    node = next(n for n in graph["nodes"] if n["node_id"] == "node-app")
    assert len(node["objective"]) <= text_bounds.NODE_OBJECTIVE
    assert plan["work_items"][0]["objective"] == objective
    prompt = loop.prompt(contract, plan, None, node=node)
    assert "Objective: " + objective in prompt
    assert f"This part of the goal, in app: {objective}\n" in prompt


def test_a_work_item_text_over_4096_holds(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _container, _consumer = rig_two_apps(deployment, tmp_path)
    rig.planner.multi_value["work_items"][0]["in_scope"] = ["s" * 4097]
    goal = _two_app_goal(rig)
    held = _held(lambda: rig.service.plan(goal))
    assert held.code == "PLANNING_TEXT_LENGTH"
    assert held.details["field"] == "work_items[0].in_scope[0]"


# -- strategy parts (workgraph_split) -------------------------------------------------------------


def _split_draft(part_objective: str) -> dict[str, Any]:
    acceptance = [{"statement": "value() returns 2", "verifier": "check"}]
    return {
        "objective": "value() returns 2",
        "in_scope": ["app.py"],
        "acceptance": acceptance,
        "parts": [
            {"objective": part_objective, "in_scope": ["app.py"], "acceptance": acceptance},
            {"objective": "second part", "in_scope": ["app.py"], "acceptance": acceptance},
        ],
    }


SPLIT = StrategyChoice("workgraph_split", {"max_nodes": 4}, {}, (), None)


def test_a_split_part_keeps_its_whole_objective(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    part = _long(4096, "PART-END")
    items, _apps = rig.service.strategy_runner().items(_split_draft(part), SPLIT, "app")
    assert items[0]["objective"] == part


def test_a_split_part_over_the_node_bound_holds(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    runner = rig.service.strategy_runner()
    held = _held(lambda: runner.items(_split_draft("p" * 4097), SPLIT, "app"))
    assert held.code == "PLANNING_OBJECTIVE_LENGTH"
    assert held.details == {"field": "parts[0].objective", "length": 4097, "limit": 4096}


# -- the node objective by reference --------------------------------------------------------------


def test_node_objective_names_a_long_objective_by_reference() -> None:
    long = _long(5000)
    named = text_bounds.node_objective(long, "node-app.int")
    assert len(named) <= text_bounds.NODE_OBJECTIVE and "node-app.int" in named
    assert text_bounds.node_objective("short", "node-app") == "short"
    plan = {"work_items": [{"app": "app", "node_id": "node-app.int", "objective": long}]}
    assert text_bounds.full_objective(plan, {"node_id": "node-app.int", "objective": named}) == long
    # a plan without the item keeps the node's own text
    assert text_bounds.full_objective({}, {"node_id": "node-x", "objective": "own"}) == "own"


# -- operator steering and replanning -------------------------------------------------------------


def test_a_steering_message_or_replan_reason_over_4096_holds(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    goal, _plan_record = _plan(rig)
    operator = with_permissions(rig.operator, "goal.steer")
    held = _held(lambda: loop.steer(operator, goal, "s" * 4097))
    assert held.code == "STEER_TEXT_LENGTH"
    assert held.details == {"field": "steering message", "length": 4097, "limit": 4096}
    held = _held(lambda: loop.replan(operator, goal, "r" * 4097))
    assert held.code == "REPLAN_REASON_LENGTH"
    assert held.details == {"field": "replan reason", "length": 4097, "limit": 4096}
    # within the bound the request goes on to its own checks (no attempt runs: not running)
    held = _held(lambda: loop.steer(operator, goal, "s" * 4096))
    assert held.code == "STEER_NOT_RUNNING"
