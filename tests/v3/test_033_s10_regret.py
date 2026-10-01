"""Work 033 S10: regret of decisions, rule-table fitting from stored trials and the `amplai meta
decider` / `amplai meta judge` command groups (interfaces.md §6.5, §6.8, §3.7, §12.1, `plan.md`
§8.1).

Contract read: §6.8 (a decision's regret is measured on tasks where every option was measured for
the
same cell: best = highest success rate, ties by lowest tokens per solved task; `success_regret =
s_best - s_chosen`, and when equal `cost_regret = cost_chosen - cost_best`; coverage = the share of
decisions not made by the prior; for L6 also the escalation rate and the tokens spent on
escalations), §6.5 (rows come from development-split trial-metrics only; a fitted table becomes the
next version of a class B `decider` component naming it), §12.1 (the command groups are modules of
`runtime/meta_commands/` registered by discovery).

Real: store, `regret`, `measured_from_rows`, `rows_from_trial_metrics`, `RuleTable`, the
`meta_commands.deciders` functions (`fit`, `show`, `regret_of`, `goal_decisions`) over a store, the
Typer registration of both groups, and one real goal run for `goal_decisions`. Stand-ins (named):
the
deployment object the command functions take is a namespace of the real store, scope and proposer
identity; trials are written as their writer shapes them (no driver runs).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from test_033_s10_deciders import (
    CELL,
    OTHER,
    Env,
    Ref,
    build_world,
    fault,
    hold,
    layers_of,
    rows,
    wrong_first,
)
from typer.testing import CliRunner

from amplai_foundry.meta_harness import deciders
from amplai_foundry.meta_harness.deciders import (
    DecisionRow,
    OptionEstimate,
    measured_from_rows,
    regret,
)
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.meta_commands import deciders as decider_commands
from amplai_foundry.runtime.meta_commands import register_all

DRAFT = {
    "task_class": "logic_change",
    "risk": "low",
    "in_scope": ["app.py"],
    "acceptance": [{"statement": "value() returns 2", "verifier": "suite"}],
}


def estimate(option: str, rate: float, per_solved: float | None, n: int = 4) -> OptionEstimate:
    cost = None if per_solved is None else per_solved * rate
    return OptionEstimate(option, n, round(rate * n), rate, (0.0, 1.0), cost, per_solved)


def decision(
    task: str,
    chosen: str,
    options: tuple[str, ...] = ("repair_loop", "single"),
    *,
    layer: str = "L2",
    prior: bool = False,
    ineligible: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A stored harness-decision as the regret input shapes it (plus its trial's task id)."""
    return {
        "task_id": task,
        "layer": layer,
        "chosen": chosen,
        "used_prior": prior,
        "options": [{"option": o, "eligible": o not in ineligible} for o in options],
    }


MEASURED = {
    "t1": {
        "repair_loop": estimate("repair_loop", 1.0, 200.0),
        "single": estimate("single", 0.5, 200.0),
    },
    "t2": {
        "repair_loop": estimate("repair_loop", 1.0, 200.0),
        "single": estimate("single", 1.0, 100.0),
    },
    "t3": {
        "repair_loop": estimate("repair_loop", 0.25, 800.0),
        "single": estimate("single", 0.75, 300.0),
    },
}


# ======================================================================================
# 1. regret (§6.8)
# ======================================================================================
def test_success_regret_is_the_best_rate_minus_the_chosen_rate() -> None:
    out = regret([decision("t1", "single")], MEASURED)
    (one,) = out["per_decision"]
    assert one["best"] == "repair_loop" and one["chosen"] == "single"
    assert one["success_regret"] == 0.5 and one["cost_regret"] is None  # only when equal
    assert out["success_regret_mean"] == 0.5 and out["cost_regret_mean"] is None


def test_cost_regret_counts_only_when_the_success_is_equal() -> None:
    out = regret([decision("t2", "repair_loop")], MEASURED)
    (one,) = out["per_decision"]
    # equal success: the best is the cheaper per solved task (single, 100); chosen cost 200
    assert one["best"] == "single" and one["success_regret"] == 0.0
    assert one["cost_regret"] == 100.0 and out["cost_regret_mean"] == 100.0
    best = regret([decision("t2", "single")], MEASURED)["per_decision"][0]
    assert best["success_regret"] == 0.0 and best["cost_regret"] == 0.0  # the best has none


def test_the_best_option_has_the_highest_rate_whatever_it_costs() -> None:
    out = regret([decision("t3", "repair_loop"), decision("t3", "single")], MEASURED)
    low, high = out["per_decision"]
    assert low["best"] == high["best"] == "single"
    assert low["success_regret"] == 0.5 and high["success_regret"] == 0.0


def test_a_task_with_an_unmeasured_option_does_not_count_but_stays_in_the_total() -> None:
    partial = {"t4": {"repair_loop": estimate("repair_loop", 1.0, 100.0)}}  # `single` unmeasured
    out = regret(
        [decision("t4", "repair_loop"), decision("t1", "single"), decision("nope", "single")],
        {**MEASURED, **partial},
    )
    assert out["decisions"] == 3 and out["measured"] == 1
    assert [p["task_id"] for p in out["per_decision"]] == ["t1"]


def test_an_option_the_goal_could_not_run_is_not_required_to_be_measured() -> None:
    measured = {
        "t5": {
            "repair_loop": estimate("repair_loop", 1.0, 100.0),
            "single": estimate("single", 0.5, 100.0),
        }
    }
    options = ("repair_loop", "single", "cascade")
    out = regret([decision("t5", "single", options, ineligible=("cascade",))], measured)
    assert out["measured"] == 1 and out["per_decision"][0]["success_regret"] == 0.5
    # eligible but never measured: the decision cannot be judged
    none = regret([decision("t5", "single", options)], measured)
    assert none["measured"] == 0 and none["success_regret_mean"] is None


def test_coverage_is_the_share_of_decisions_the_prior_did_not_make() -> None:
    decisions = [
        decision("t1", "single", prior=False),
        decision("t2", "single", prior=True),
        decision("t3", "single", prior=True),
        decision("zz", "single", prior=False),
    ]
    out = regret(decisions, MEASURED)
    assert out["decisions"] == 4 and out["coverage"] == 0.5
    assert [p["used_prior"] for p in out["per_decision"]] == [False, True, True]
    assert regret([], MEASURED) == {
        "decisions": 0,
        "measured": 0,
        "coverage": None,
        "success_regret_mean": None,
        "cost_regret_mean": None,
        "per_decision": [],
    }


def test_the_means_are_over_the_measured_decisions() -> None:
    out = regret(
        [decision("t1", "single"), decision("t2", "repair_loop"), decision("t2", "single")],
        MEASURED,
    )
    assert out["measured"] == 3
    assert out["success_regret_mean"] == pytest.approx(0.5 / 3, abs=1e-6)
    assert out["cost_regret_mean"] == pytest.approx(50.0)  # (100 + 0) / 2: equal-success ones only


def test_l6_regret_reports_the_escalation_rate_and_the_tokens_spent_on_escalations() -> None:
    options = deciders.OPTIONS["L6"]
    measured = {
        "a": {o: estimate(o, 0.5, 100.0) for o in options},
        "b": {o: estimate(o, 0.5, 100.0) for o in options},
        "c": {o: estimate(o, 0.5, 100.0) for o in options},
    }
    measured["a"]["escalate"] = OptionEstimate("escalate", 4, 3, 0.75, (0.0, 1.0), 900.0, 1200.0)
    measured["b"]["escalate"] = OptionEstimate("escalate", 4, 3, 0.75, (0.0, 1.0), 400.0, 533.0)
    chosen = [
        decision("a", "escalate", options, layer="L6"),
        decision("b", "escalate", options, layer="L6"),
        decision("c", "retry_feedback", options, layer="L6"),
    ]
    out = regret(chosen, measured)
    assert out["escalation_rate"] == pytest.approx(2 / 3, abs=1e-6)
    assert out["escalation_tokens"] == 1300.0  # the measured attempt cost of each escalation
    assert "escalation_rate" not in regret([decision("t1", "single")], MEASURED)  # L6 only


def test_measured_options_are_raw_rates_with_a_wilson_interval_and_tokens() -> None:
    data = [
        *rows("single", 4, 3, {}, tokens=100),
        *rows("repair_loop", 2, 2, {}, tokens=300),
        DecisionRow("single-x", {}, "single", None, 999, 1.0),  # unknown outcome: left out
    ]
    renamed = [
        DecisionRow("t1", r.features, r.option, r.success, r.tokens, r.seconds) for r in data
    ]
    out = measured_from_rows(renamed)
    single, repair = out["t1"]["single"], out["t1"]["repair_loop"]
    assert (single.n, single.successes, single.posterior_success) == (4, 3, 0.75)
    assert single.cost_per_attempt == 100.0 and single.cost_per_solved == pytest.approx(133.333)
    assert 0.3 < single.interval[0] < 0.75 < single.interval[1] <= 1.0  # Wilson 95 %
    assert (repair.n, repair.posterior_success, repair.cost_per_solved) == (2, 1.0, 300.0)
    # an option that never solved has no cost per solved task
    nothing = measured_from_rows([DecisionRow("t9", {}, "single", False, 50, 1.0)])
    assert nothing["t9"]["single"].cost_per_solved is None


# ======================================================================================
# 2. regret and fitting from stored trials (§6.5, §6.8)
# ======================================================================================
def dep_of(env: Env, service: Any = None) -> Any:
    """What the command functions take of a deployment: store, scope, proposer, service."""
    return SimpleNamespace(
        store=env.store,
        scope=env.scope,
        meta_local=SimpleNamespace(proposer=env.proposer),
        service=service,
    )


@pytest.fixture
def env(deployment: Any) -> Env:
    return Env(deployment)


def trial(
    env: Env,
    task: str,
    option: str,
    success: bool,
    *,
    split: str = "development",
    cell: str = CELL,
    domain: str = "bug",
    chosen_by: Ref | None = None,
    source: str = "experiment",
    decision_options: tuple[str, ...] = ("repair_loop", "single"),
) -> Ref:
    """A trial that ran `option` at L2: its goal plan holds the L2 decision (made by the
    decider `chosen_by` or the prior) and its metrics row names the strategy."""
    goal = f"goal-{task}-{option}-{split}-{cell}-{source}"
    made = env.decide(
        chosen_by,
        "L2",
        decision_options,
        option,
        {"domain": domain},
        cell=cell,
    )
    assert made.option == option
    env.plan_head(goal, [made.record_ref], draft=DRAFT)
    return env.metrics(
        task=task,
        split=split,
        cell=cell,
        domain=domain,
        success=success,
        strategy=option,
        goal=goal,
        source=source,
        tokens=(60, 40) if option == "single" else (160, 140),
    )


def test_regret_of_measures_decisions_against_what_the_same_tasks_showed(env: Env) -> None:
    for t in ("t1", "t2", "t3"):
        trial(env, t, "repair_loop", True)  # repair_loop solves every task (300 tokens)
        trial(env, t, "single", t == "t3")  # single solves only t3 (100 tokens)
    out = decider_commands.regret_of(dep_of(env), "L2", [CELL])
    assert out["layer"] == "L2" and out["cells"] == [CELL]
    assert out["decisions"] == 6 and out["measured"] == 6
    assert out["coverage"] == 0.0  # every decision here was the prior's
    by = {(p["task_id"], p["chosen"]): p for p in out["per_decision"]}
    assert (
        by[("t1", "single")]["success_regret"] == 1.0
        and by[("t1", "single")]["best"] == "repair_loop"
    )
    assert by[("t1", "repair_loop")]["success_regret"] == 0.0
    # t3: both solved it; single is cheaper per solved task (100 vs 300)
    assert by[("t3", "repair_loop")]["cost_regret"] == 200.0
    assert by[("t3", "single")]["cost_regret"] == 0.0
    assert out["success_regret_mean"] == pytest.approx(2 / 6, abs=1e-6)


def test_regret_reads_development_trials_of_the_named_cells_only(env: Env) -> None:
    trial(env, "t1", "repair_loop", True)
    trial(env, "t1", "single", False)
    # a validation calibration trial and a holdout trial where `single` wins: never measured
    trial(env, "t1", "single", True, split="validation", source="calibration")
    trial(env, "t1", "single", True, split="holdout")
    trial(env, "t1", "single", True, cell=OTHER)  # another cell's trials
    out = decider_commands.regret_of(dep_of(env), "L2", [CELL])
    assert out["decisions"] == 2
    chosen = {p["chosen"]: p for p in out["per_decision"]}
    assert chosen["single"]["success_regret"] == 1.0  # still the 0/1 development measurement
    both = decider_commands.regret_of(dep_of(env), "L2", [CELL, OTHER])
    assert both["decisions"] == 3  # the other cell's development trial counts there


def test_regret_counts_the_decisions_a_decider_made_as_coverage(env: Env) -> None:
    method = env.method(["domain"])
    data = [
        *rows("repair_loop", 12, 12, {"domain": "bug"}, tokens=300),
        *rows("single", 12, 3, {"domain": "bug"}, tokens=100),
    ]
    table = env.table(
        "L2", data, features=("domain",), options=("repair_loop", "single"), method=method
    )
    decider = env.decider("L2", method, table)
    for t in ("t1", "t2"):
        trial(env, t, "repair_loop", True, chosen_by=decider)  # decided: not the prior
        trial(env, t, "single", False)  # the prior
    out = decider_commands.regret_of(dep_of(env), "L2", [CELL])
    assert out["decisions"] == 4 and out["coverage"] == 0.5
    decided = [p for p in out["per_decision"] if not p["used_prior"]]
    assert {p["chosen"] for p in decided} == {"repair_loop"}
    assert all(p["success_regret"] == 0.0 for p in decided)


def test_the_fit_command_builds_a_table_of_development_rows_and_versions_the_decider(
    env: Env,
) -> None:
    """§6.5: rows exclude validation calibration trials; the table names the cell it was fitted
    on; the decider component's next version names the table (a class B candidate)."""
    for domain, a_win, b_win in (("bug", 7, 4), ("feature", 2, 2)):
        for i in range(8):
            trial(env, f"{domain}-{i}", "repair_loop", i < a_win, domain=domain)
            trial(env, f"{domain}-{i}", "single", i < b_win, domain=domain)
    for i in range(5):  # calibration also runs validation tasks: these must stay out
        trial(env, f"val-{i}", "repair_loop", True, split="validation", source="calibration")
        trial(env, f"val-{i}", "single", False, split="validation", source="calibration")
    method = env.method(["domain"], name="fitm")
    v1 = env.decider("L2", method, None, name="fitd")
    out = decider_commands.fit(
        dep_of(env),
        layer="L2",
        cells=[CELL],
        decider="decider.fitd",
        method=None,
        pooling_strength=None,
        confidence=0.95,
    )
    assert out["rows"] == 32 and out["options"] == ["repair_loop", "single"]
    assert out["features"] == ["domain"] and out["layer"] == "L2"
    table = env.store.get(env.scope, "decider-table", out["table_ref"])
    assert table["source"]["rows"] == 32
    assert table["source"]["trial_metrics_query"] == {
        "cells": [CELL],
        "since": None,
        "splits": ["development"],
    }
    entry = next(
        c
        for c in table["cells"]
        if c["bucket"] == {"domain": "bug"} and c["option"] == "repair_loop"
    )
    assert (entry["n"], entry["successes"]) == (8, 7)
    assert not any(c["bucket"] == {"domain": "val"} for c in table["cells"])
    assert table["method_ref"] == env.content(v1)["method"]
    # the decider's next version names the table; the first version is untouched
    ref = out["decider_ref"]
    assert (ref["id"], ref["revision"]) == ("decider.fitd", 2)
    version = env.components.get(ref)
    assert version["content"]["table"] == out["table_ref"] and version["surface_class"] == "B"
    assert version["source"] == "operator" and version["parent"] == v1
    assert env.content(v1)["table"] is None
    # `show` lists the layer's tables and deciders
    shown = decider_commands.show(dep_of(env), "L2")
    assert [t["ref"] for t in shown["tables"]] == [out["table_ref"]]
    assert shown["tables"][0]["cells"] == [CELL] and shown["tables"][0]["rows"] == 32
    assert [d["table"] for d in shown["deciders"]] == [None, out["table_ref"]]
    assert decider_commands.show(dep_of(env), "L3") == {"layer": "L3", "tables": [], "deciders": []}
    # a table fitted on CELL is used there and only there. `single` costs a third of
    # `repair_loop` per attempt and is not credibly worse over 16 trials each, so the table picks
    # it; the prior is `repair_loop`, so the two cannot be confused
    fitted = env.content(ref)
    here = deciders.Decider.of(env.store, env.scope, fitted, ref=ref).decide(
        deciders.DecisionContext(
            "L2",
            CELL,
            {"domain": "bug"},
            ("repair_loop", "single"),
            "repair_loop",
            {"goal_id": "g"},
            True,
        )
    )
    there = deciders.Decider.of(env.store, env.scope, fitted, ref=ref).decide(
        deciders.DecisionContext(
            "L2",
            OTHER,
            {"domain": "bug"},
            ("repair_loop", "single"),
            "repair_loop",
            {"goal_id": "g"},
            True,
        )
    )
    assert here.used_prior is False and here.option == "single"
    assert there.used_prior is True and there.option == "repair_loop"


def test_the_fit_command_refuses_what_cannot_be_fitted(env: Env) -> None:
    trial(env, "t1", "repair_loop", True)
    method = env.method(["domain"], name="fitm2")
    env.decider("L3", env.method(["task_class"], name="fitm3"), None, name="l3d")
    common: dict[str, Any] = {
        "layer": "L2",
        "cells": [CELL],
        "decider": None,
        "method": None,
        "pooling_strength": None,
        "confidence": 0.95,
    }
    dep = dep_of(env)
    fault("DECIDER_METHOD", decider_commands.fit, dep, **common)  # neither method nor decider
    fault("DECIDER_LAYER", decider_commands.fit, dep, **{**common, "decider": "decider.l3d"})
    fault("DECIDER_METHOD", decider_commands.fit, dep, **{**common, "method": "decider.l3d"})
    fault("NOT_FOUND", decider_commands.fit, dep, **{**common, "method": "decision_method.none"})
    fault(
        "DECIDER_ROWS",
        decider_commands.fit,
        dep,
        **{**common, "cells": ["no-such-cell"], "method": "decision_method.fitm2"},
    )
    assert method["id"] == "decision_method.fitm2"
    assert list(env.store.list_objects(env.scope, "decider-table")) == []  # nothing was written


def test_fitting_never_reads_validation_or_holdout_rows(env: Env) -> None:
    """The fit takes no split argument: it can only read development, and a direct ask for another
    split holds DECIDER_SPLIT (plan.md §10.3)."""
    for split in ("validation", "holdout"):
        trial(env, f"x-{split}", "repair_loop", True, split=split)
    method = env.method(["domain"], name="fitm4")
    fault(
        "DECIDER_ROWS",
        decider_commands.fit,
        dep_of(env),
        layer="L2",
        cells=[CELL],
        decider=None,
        method="decision_method.fitm4",
        pooling_strength=None,
        confidence=0.95,
    )
    assert method["revision"] == 1
    hold(
        "DECIDER_SPLIT",
        deciders.rows_from_trial_metrics,
        env.store,
        env.scope,
        layer="L2",
        cells=[CELL],
        splits=("validation", "holdout"),
    )


# ======================================================================================
# 3. decisions of a real goal (the `decisions` command) and command registration
# ======================================================================================
def test_the_decisions_command_lists_every_decision_of_a_goal_in_order(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, wrong_first)
    goal = w.approved(w.comps.base_ref)
    w.loop.run_goal(goal)
    env = Env(deployment)
    out = decider_commands.goal_decisions(dep_of(env, w.service), goal)
    assert out["goal_id"] == goal
    assert [d["layer"] for d in out["decisions"]] == layers_of(w, goal)
    assert [d["layer"] for d in out["decisions"]] == [
        "L1",
        "L2",
        "L3",
        "L8",
        "L5",
        "L4",
        "L7",
        "L6",
        "L4",
        "L7",
    ]
    assert all(d["subject"] == {"goal_id": goal} for d in out["decisions"])
    json.dumps(out)  # printable as the command prints it


def test_both_command_groups_are_registered_by_discovery() -> None:
    meta = typer.Typer()
    register_all(meta, lambda call: call())
    runner = CliRunner()

    def help_of(*args: str) -> str:
        output = runner.invoke(meta, [*args, "--help"]).output
        return re.sub(r"\x1b\[[0-9;]*m", "", output)  # the colour codes of the help renderer

    top = help_of()
    assert "decider" in top and "judge" in top
    decider_help = help_of("decider")
    for command in ("fit", "show", "regret", "decisions"):
        assert command in decider_help
    judge_help = help_of("judge")
    for command in ("list", "label", "qualify"):
        assert command in judge_help
    fit_help = help_of("decider", "fit")
    for option in ("--layer", "--cells", "--decider", "--method", "--pooling-strength"):
        assert option in fit_help
    assert "--split" not in fit_help  # a table reads development trials only


def test_fault_codes_of_the_decider_module_are_public_runtime_faults() -> None:
    with pytest.raises(RuntimeFault) as caught:
        deciders.rows_from_trial_metrics(None, None, layer="L0", cells=[])  # type: ignore[arg-type]
    assert caught.value.code == "DECIDER_LAYER"
