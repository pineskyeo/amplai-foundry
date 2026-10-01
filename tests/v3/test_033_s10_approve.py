"""Work 033 S10: approval re-checks a decided composition (IC-22), router parts refused at plan
time,
the `append_system_prompt` validator and decider component content (interfaces.md §0 IC-22, §6.1 L3,
§2.2 `driver_options` / `decider`, the Clarifications after W0-W1, W2 part 1 and S9/S11).

Contract read: IC-22 (`plan` records an L3-decided composition with `record["composition"]
["decision_ref"]`; `approve` re-checks it through the pin path - `pin=chosen["ref"]`, `pin_allowed`,
the eligibility filter unchanged - and checks that the decision record's `chosen` cell is that
composition's cell; a composition without a decision keeps today's re-selection); the W0-W1
clarification "plan-time router check" (refusal moves before the planner turn, S4/S10); the
W2-part-1
clarification "`append_system_prompt` validator refusing a leading `-` or NUL (S10)".

Real: the rc06 product rig with both Codex and Claude cells (host-process stand-ins for their
containers), `LocalExecutionService.plan` / `approve`, the active release (what promotion writes),
`ManifestService.materialize`, the real `ExecutionLoop` and the protected verification. Stand-ins
(named): the "containers" run a script on the host; the router that carries an L3 decider is made
the
active release's router by writing a signed release directly, as promotion does.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from test_033_s10_deciders import (
    CELL,
    OTHER,
    Env,
    Ref,
    build_world,
    candidate,
    decisions_of,
    env_of,
    fault,
    hold,
    l3_world,
    layers_of,
    only,
    plan_free,
    rows,
    wrong_first,
)

from amplai_foundry.meta_harness import deciders
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import policies


@pytest.fixture
def env(deployment: Any) -> Env:
    return Env(deployment)


def saved(w: Any, goal: str, **changes: Any) -> dict[str, Any]:
    """The plan record with changes made to it (the way a stored plan of an earlier build or a
    tampered one would look), written back."""
    record = {**w.record(goal), **changes}
    w.service._save_plan(goal, record, None)
    return dict(record)


def with_composition(w: Any, goal: str, **changes: Any) -> None:
    saved(w, goal, composition={**w.record(goal)["composition"], **changes})


# ======================================================================================
# 1. IC-22: a goal whose L3 decision chose the second cell is approved
# ======================================================================================
def test_a_goal_whose_l3_decision_chose_the_second_cell_is_approved_and_runs_there(
    deployment: Any, tmp_path: Path
) -> None:
    w, _decider, _made, _table = l3_world(deployment, tmp_path)
    goal = plan_free(w)
    plan = w.record(goal)
    assert plan["composition"]["cell_id"] == OTHER  # the router's first eligible cell is Codex
    assert plan["composition"]["decision_ref"] == plan["decisions"][2]
    assert plan["composition"]["pinned"] is False
    approved = w.service.approve(
        w.rig.operator, goal
    )  # without IC-22 this holds COMPOSITION_CHANGED
    assert approved["status"] == "approved"
    profile = w.rig.d.store.head(w.rig.d.scope, "goal", goal)["data"]["profile"]
    assert profile["driver_profile_ref"]["id"].startswith("claude-cli-")  # fixed from here on
    assert profile["composition_ref"] == plan["composition"]["ref"]
    done = w.loop.run_goal(goal)
    assert done["status"] == "published" and w.rig.published == [goal]
    claude = w.extra["claude_box"]
    assert claude.prompts and w.agents.calls == []  # the decided cell ran, not the first one
    # decisions at and after dispatch are for the cell the goal runs on; those at plan time for the
    # cell the plan started from (§2.5 `cell_id`: the harness cell the decision is for)
    cells = {d["layer"]: d["cell_id"] for d in decisions_of(w, goal)}
    assert cells["L3"] == CELL and cells["L4"] == cells["L5"] == cells["L7"] == OTHER
    assert layers_of(w, goal)[:4] == ["L1", "L2", "L3", "L8"]


def test_the_decided_composition_is_rechecked_through_the_pin_path(
    deployment: Any, tmp_path: Path
) -> None:
    """`approve` pins the decided composition (so it cannot be re-selected to Codex), and the pin
    must still be an installed composition or its candidate (`pin_allowed`)."""
    w, _decider, _made, _table = l3_world(deployment, tmp_path)
    goal = plan_free(w)
    plan = w.record(goal)
    again = w.service.select_composition(
        w.service.apps["app"], plan["composition"]["task_class"], pin=plan["composition"]["ref"]
    )
    assert again["ref"] == plan["composition"]["ref"] and again["cell_id"] == OTHER
    # an arbitrary composition that is no cell's installed composition or candidate is refused
    stray = w.service.apps["app"].compositions[CELL]
    store = w.rig.d.store
    foreign = {
        **store.get(w.rig.d.scope, "harness-composition", stray),
        "composition_id": "app-unrelated",
    }
    with store.tx() as db:
        ref = store.put(db, w.rig.d.scope, "harness-composition", "app-unrelated", 1, foreign)
    with_composition(w, goal, ref=ref)
    with pytest.raises(Hold) as held:
        w.service.approve(w.rig.operator, goal)
    assert (
        held.value.code == "COMPOSITION_PIN"
    )  # pin_allowed: not a cell's composition or candidate
    assert w.record(goal)["status"] == "awaiting_approval"


def test_a_composition_without_a_decision_keeps_todays_reselection(
    deployment: Any, tmp_path: Path
) -> None:
    w, _decider, _made, _table = l3_world(deployment, tmp_path)
    goal = plan_free(w)
    with_composition(w, goal, decision_ref=None)  # as a plan from before deciders: no decision
    held = hold("COMPOSITION_CHANGED", w.service.approve, w.rig.operator, goal)
    assert held.details == {"planned": "claude-cli", "now": "codex-cli"}  # re-selected: Codex
    assert w.record(goal)["status"] == "awaiting_approval"


def test_a_decision_that_names_another_cell_layer_or_nothing_holds_the_approval(
    deployment: Any, tmp_path: Path
) -> None:
    w, _decider, _made, _table = l3_world(deployment, tmp_path)
    goal = plan_free(w)
    plan = w.record(goal)
    e = env_of(w)
    # (1) an L3 decision that chose the router's cell, not this composition's
    other_cell = e.decide(
        None, "L3", (CELL, OTHER), CELL, {"task_class": "logic_change"}, production=True
    )
    with_composition(w, goal, decision_ref=other_cell.record_ref)
    held = hold("COMPOSITION_CHANGED", w.service.approve, w.rig.operator, goal)
    assert held.details["decided"] == CELL and held.details["planned"] == OTHER
    # (2) a decision of another layer (the L2 decision of the same plan) is no L3 decision
    l2_ref = plan["decisions"][1]
    assert only(w, goal, "L2")["layer"] == "L2"
    with_composition(w, goal, decision_ref=l2_ref)
    hold("COMPOSITION_CHANGED", w.service.approve, w.rig.operator, goal)
    # (3) a decision record that does not exist is a fault, never an approval
    missing = {"id": "decision-nope", "revision": 1, "digest": "sha256:" + "0" * 64}
    with_composition(w, goal, decision_ref=missing)
    with pytest.raises(RuntimeFault):
        w.service.approve(w.rig.operator, goal)
    assert w.record(goal)["status"] == "awaiting_approval"
    # the untouched plan still approves
    with_composition(w, goal, decision_ref=plan["composition"]["decision_ref"])
    assert w.service.approve(w.rig.operator, goal)["status"] == "approved"


def test_a_cell_changed_since_planning_holds_the_decided_goals_approval(
    deployment: Any, tmp_path: Path
) -> None:
    """The pin path runs `select_composition`, which reads the app's router from the active release:
    the operator disables the Claude model after planning and the deployment re-installs, so the
    release's candidates and the installed Claude composition no longer name one router. The
    approval holds (ROUTER_INCONSISTENT) instead of approving the decided composition. A
    candidate of the disabled cell cannot be re-derived (`COMPOSITION_UNQUALIFIED`), so the
    eligibility filter alone cannot be isolated here; it is the same call the pin path always
    made."""
    from amplai_foundry.runtime.execution.codex import install_driver_profile
    from amplai_foundry.runtime.execution.product import AppConfig, app_capabilities
    from rc06_rig import claude_inputs

    w, _decider, _made, _table = l3_world(deployment, tmp_path)
    goal = plan_free(w)
    assert w.record(goal)["composition"]["cell_id"] == OTHER
    d = w.rig.d
    w.service.drivers["claude-cli"] = install_driver_profile(
        d.store, d.scope, claude_inputs(tmp_path, enabled=False), app_capabilities("app")
    )
    w.service.install(AppConfig("app", w.rig.repo, w.service.apps["app"].config.verifiers))
    held = hold("ROUTER_INCONSISTENT", w.service.approve, w.rig.operator, goal)
    assert held.details["routers"]
    assert w.record(goal)["status"] == "awaiting_approval"


def test_a_pinned_composition_is_approved_as_before(deployment: Any, tmp_path: Path) -> None:
    w, _decider, made, _table = l3_world(deployment, tmp_path)
    goal = w.plan(made[CELL])  # an experiment arm or canary goal
    assert w.record(goal)["composition"]["pinned"] is True
    assert w.service.approve(w.rig.operator, goal)["status"] == "approved"


def test_v1_goals_approve_exactly_as_before(deployment: Any, tmp_path: Path) -> None:
    w = build_world(deployment, tmp_path, two=True)
    goal = plan_free(w)
    plan = w.record(goal)
    assert (
        plan["composition"]["cell_id"] == CELL and plan["composition"].get("decision_ref") is None
    )
    assert w.service.approve(w.rig.operator, goal)["status"] == "approved"
    assert w.loop.run_goal(goal)["status"] == "published"
    assert w.agents.prompts and not w.extra["claude_box"].prompts


# ======================================================================================
# 2. plan-time refusal of router parts the loop cannot honour (before the planner turn)
# ======================================================================================
def refused(w: Any, composition: Ref, *needles: str) -> RuntimeFault:
    """Planning with this composition is refused with COMPONENT_CONTENT before the planner turn,
    leaving no plan record, and the details name each needle."""
    from rc06_rig import submit

    w.extra["goals"] = n = w.extra.get("goals", 0) + 1
    goal = submit(w.rig, f"({n}) a goal the router refuses")
    with pytest.raises(RuntimeFault) as caught:
        w.service.plan(goal, composition=composition)
    err = caught.value
    assert err.code == "COMPONENT_CONTENT" and not isinstance(err, Hold)
    details = " | ".join(str(p) for p in err.details)
    for needle in needles:
        assert needle in details, (needle, details)
    assert w.rig.planner.calls == []  # the planner (a model turn) never ran
    with pytest.raises(RuntimeFault) as missing:
        w.service.plan_record(goal)
    assert missing.value.code == "NOT_FOUND"  # and no plan was saved
    return err


def test_an_interpretation_other_than_v1_is_refused_before_the_planner_turn(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path)
    for instruction, form in (("ask_first", "v1"), ("assume_and_state", "v1"), ("v1", "steps")):
        interpretation = w.comps.version(
            "interpretation", planner_instruction=instruction, contract_form=form
        )
        refused(w, candidate(w, interpretation=interpretation), "interpretation")


def test_route_policy_roles_and_a_foreign_route_order_are_refused(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path)
    roles = w.comps.version("route_policy", roles={"reviewer": ["codex-cli"]})
    refused(w, candidate(w, route_policy=roles), "roles")
    order = w.comps.version("route_policy", order={"*": ["codex-cli"]})  # the app's has three cells
    refused(w, candidate(w, route_policy=order), "route_policy order")


def test_a_decider_whose_parts_cannot_run_is_refused_at_plan_time(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path)
    e = env_of(w)
    # an L6 feature on an L2 decider: the layer's decision point never records it
    foreign = e.decider("L2", e.method(["attempt"], name="fm"), None, name="foreign")
    refused(w, candidate(w, L2=foreign), "decider L2", "attempt")
    # a decider of one layer in another layer's slot is refused by the manifest itself
    with pytest.raises(RuntimeFault) as caught:
        base = w.comps.manifests.of_composition(w.comps.base_ref)
        w.comps.manifests.change(base, L3=foreign)
    assert caught.value.code == "MANIFEST_SLOT"
    # a table fitted for another layer on an L3 decider
    method = e.method(["task_class"], name="lm")
    table = e.table(
        "L2",
        rows("repair_loop", 12, 12, {"task_class": "x"}),
        features=("task_class",),
        options=("repair_loop", "single"),
        method=method,
    )
    wrong_table = e.decider("L3", method, table, name="wrongt")
    refused(w, candidate(w, L3=wrong_table), "fitted for L2")
    # a judge estimator on a layer that has no judge question
    judged = e.method(["questions"], name="jm", estimator="judge_v1")
    judge = e.register(
        "judge_model", "j2", {"judge": "llm_cell", "cell": CELL, "question_types": ["yes_no"]}
    )
    refused(w, candidate(w, L2=e.decider("L2", judged, None, judge=judge, name="j2d")), "L1 only")


def test_a_clean_router_is_not_refused(deployment: Any, tmp_path: Path) -> None:
    w = build_world(deployment, tmp_path)
    v1 = w.comps.version("interpretation")  # the v1 text and contract form
    assert w.service.plan(_goal(w), composition=candidate(w, interpretation=v1))["status"] == (
        "awaiting_approval"
    )


def _goal(w: Any) -> str:
    from rc06_rig import submit

    return str(submit(w.rig, "a clean router"))


def test_the_loop_still_holds_a_goal_planned_before_the_refusal_existed(
    deployment: Any, tmp_path: Path
) -> None:
    """A plan record whose composition's router has another interpretation than the plan was
    drafted with (v1 here). Approval now holds it (COMPOSITION_CHANGED); a plan approved before
    that check existed is held by the loop before any claim (`_router_unsupported`), so no agent
    turn starts."""
    w = build_world(deployment, tmp_path)
    clean = candidate(w, interpretation=w.comps.version("interpretation"))
    ask_first = w.comps.version("interpretation", planner_instruction="ask_first")
    unsupported = candidate(w, interpretation=ask_first)
    # 1. approval holds the changed composition
    goal = w.plan(clean)
    with_composition(w, goal, ref=unsupported, pinned=True)  # tampered: an old plan's shape
    with pytest.raises(Hold) as caught:
        w.service.approve(w.rig.operator, goal)
    assert caught.value.code == "COMPOSITION_CHANGED"
    assert "interpretation" in caught.value.message
    # 2. a plan approved before the check existed: approved clean, then its record changed
    old = w.plan(clean)
    w.service.approve(w.rig.operator, old)
    with_composition(w, old, ref=unsupported, pinned=True)
    assert w.record(old)["status"] == "approved"
    held = w.loop.run_goal(old)
    assert held["status"] == "held" and held["attempts"] == []
    assert "COMPONENT_CONTENT" in held["reason"]
    assert "interpretation of the plan differs from the composition router" in held["reason"]
    assert w.agents.calls == [] and w.rig.published == []


def test_the_loop_holds_deciders_l4_to_l8_whose_parts_cannot_run_before_any_claim(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path)
    e = env_of(w)
    # `strategy` is not an L6 feature (the failure point knows attempts, tokens and the like)
    bad = e.decider("L6", e.method(["strategy"], name="l6bad"), None, name="l6bad")
    goal = w.approved(candidate(w, L6=bad))
    held = w.loop.run_goal(goal)
    assert held["status"] == "held" and held["attempts"] == [] and w.agents.calls == []
    assert "decider L6" in held["reason"] and "strategy" in held["reason"]
    # the same for a table fitted for another layer on an L7 decider
    method = e.method(["changed_files"], name="l7m")
    table = e.table(
        "L6",
        rows("retry_feedback", 12, 12, {"attempt": 1}),
        features=("attempt",),
        options=("retry_feedback", "stop"),
        method=e.method(["attempt"], name="l6m"),
    )
    wrong = e.decider("L7", method, table, name="l7wrong")
    held = w.loop.run_goal(w.approved(candidate(w, L7=wrong)))
    assert held["status"] == "held" and "fitted for L6" in held["reason"]
    assert w.agents.calls == [] and w.rig.published == []


# ======================================================================================
# 3. `append_system_prompt` (§2.2 driver_options; W2 part 1 clarification)
# ======================================================================================
NULLS: dict[str, Any] = {
    "claude": {"max_turns": None, "append_system_prompt": None, "allowed_tools": None},
    "codex": {"config": []},
}


def driver_options(prompt: Any) -> dict[str, Any]:
    value = copy.deepcopy(NULLS)
    value["claude"]["append_system_prompt"] = prompt
    return value


@pytest.mark.parametrize(
    "prompt",
    [
        "-",
        "-x",
        "--help",
        "--dangerously-skip-permissions",
        "-p injected",
        "ok\x00",
        "\x00",
        "a\x00b",
    ],
)
def test_an_append_system_prompt_that_is_a_flag_or_holds_nul_is_refused(prompt: str) -> None:
    err = fault(
        "COMPONENT_CONTENT", policies.validate_content, "driver_options", driver_options(prompt)
    )
    assert "append_system_prompt" in err.message and "'-'" in err.message and "NUL" in err.message


def test_a_leading_dash_is_refused_by_name_before_the_allowlist(env: Env) -> None:
    fault("COMPONENT_CONTENT", env.register, "driver_options", "dash", driver_options("-x"))
    # an ordinary text still meets the allowlist (no Claude option is measured yet, §14 Q13):
    # it is refused too, but for that reason, not this one
    plain = fault(
        "COMPONENT_CONTENT",
        policies.validate_content,
        "driver_options",
        driver_options("Write tests first."),
    )
    assert "ALLOWLIST" in plain.message and "NUL" not in plain.message


def test_the_all_null_driver_options_stay_valid_and_other_refusals_are_unchanged(env: Env) -> None:
    policies.validate_content("driver_options", copy.deepcopy(NULLS))
    env.register("driver_options", "null", copy.deepcopy(NULLS))
    fault(
        "COMPONENT_CONTENT",
        policies.validate_content,
        "driver_options",
        {**copy.deepcopy(NULLS), "codex": {"config": [["model_reasoning_effort", "high"]]}},
    )
    too_long = driver_options("x" * 2001)
    fault("COMPONENT_CONTENT", policies.validate_content, "driver_options", too_long)
    # a non-string is not a flag test's business: it is still not a valid prompt
    fault("COMPONENT_CONTENT", policies.validate_content, "driver_options", driver_options(5))


# ======================================================================================
# 4. component content of the decider kinds (§2.2): one more look from the plan
# ======================================================================================
def test_a_valid_decider_of_every_layer_installs_and_a_goal_still_plans(
    deployment: Any, tmp_path: Path
) -> None:
    """A decider with no table at every layer reads as the prior: the goal plans and approves, and
    each decision is the prior's and names its decider (§2.5)."""
    w = build_world(deployment, tmp_path, wrong_first)  # one failure, so L6 decides too
    e = env_of(w)
    limits_ref = w.service._budget_policy(w.comps.base_ref).refs["limits"]
    null = e.register("driver_options", "all", copy.deepcopy(NULLS))
    slots: dict[str, Ref] = {}
    for layer in deciders.LAYERS:
        features = list(deciders.FEATURES[layer][:1])
        method = e.method(features, name=f"all{layer}")
        options = {"L5": [null], "L8": [limits_ref]}.get(layer)
        slots[layer] = e.decider(layer, method, None, options=options, name=f"all{layer}")
    goal = w.approved(candidate(w, **slots))
    record = w.loop.run_goal(goal)
    assert record["status"] == "published"
    made = decisions_of(w, goal)
    assert {d["layer"] for d in made} == set(deciders.LAYERS)
    assert all(d["used_prior"] is True and d["decider_ref"] is not None for d in made)
    assert all(d["method_ref"] is not None and d["table_ref"] is None for d in made)
