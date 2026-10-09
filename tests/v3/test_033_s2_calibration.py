"""Work 033 S2: adaptive calibration (interfaces.md 2.8, 3.8, 7.5, 8.2).

Covers Q-11 (adaptive repeats rule, summary classes, A/A discordance), the approval, identity,
budget and state rules of `CalibrationService`, the pure `select_cases` rule of IC-15 and its use by
`EvaluationService` through a real calibration summary. The world builder comes from
`test_033_s2_service.py`.
"""

import dataclasses
from types import SimpleNamespace

import pytest
from test_033_s2_service import (
    BUDGET,
    Scripted,
    fault,
    hold,
    make_world,
    policy,
    subset,
)

from amplai_foundry.evaluation import versions
from amplai_foundry.evaluation.calibration import (
    CalibrationService,
    select_cases,
    validate_summary,
)
from amplai_foundry.evaluation.sequential import noise_band, wilson
from amplai_foundry.evaluation.service import NOT_RUN_EVIDENCE
from amplai_foundry.runtime.contracts.identity import digest, now
from amplai_foundry.runtime.errors import Hold, RuntimeFault

Z95 = 1.959963984540054
CELLS = ("cell-a", "cell-b")
# `Scripted(..., tokens=(5, 5))` reports 10 tokens per trial; a trial settles as an overrun only
# when its tokens exceed the reservation (`meta_harness/budget.py` settle), so 10 is exactly enough.
TRIAL_TOKENS = 10


@pytest.fixture
def w(tmp_path):
    with make_world(tmp_path) as world:
        yield world


def outcome(role, case_id, repeat):
    """Cell A and B on six tasks; the cells disagree on val-00, tie at 0.5..0.6 on val-01."""
    cell = role.removesuffix("-var")
    if role.endswith("-var"):
        return False
    a_pattern, b_pattern = [True, False, True, False, True], [False, True, True, False, True]
    table = {
        "dev-00": {"cell-a": True, "cell-b": True},
        "dev-01": {"cell-a": False, "cell-b": False},
        "val-00": {"cell-a": True, "cell-b": False},
        "val-02": {"cell-a": True, "cell-b": True},
        "val-03": {"cell-a": True, "cell-b": True},
    }
    if case_id == "val-01":
        return (a_pattern if cell == "cell-a" else b_pattern)[repeat]
    return table[case_id][cell]


def make(w, *, variants=False, evaluator=True):
    """Corpus of two development and four validation tasks, one composition per cell."""
    corpus_ref, cases = w.corpus(dev=2, val=4)
    comps = {"cell-a": w.composition("cell-a"), "cell-b": w.composition("cell-b")}
    roles = dict(comps)
    refs = {cell: [ref] for cell, ref in comps.items()}
    if variants:
        roles["cell-a-var"] = w.composition("cell-a-var")
        refs["cell-a"].append(roles["cell-a-var"])
    return SimpleNamespace(
        corpus_ref=corpus_ref,
        cases=cases,
        comps=comps,
        roles=roles,
        refs=refs,
        ev=w.evaluator(corpus_ref) if evaluator else None,
    )


def plan_for(w, c, **over):
    plan = {
        "schema": "amplai.calibration-plan.v1",
        "scope": w.scope.wire(),
        "cells": list(CELLS),
        "composition_refs": c.refs,
        "corpus_ref": c.corpus_ref,
        "splits": ["development", "validation"],
        "case_ids": [x["case_id"] for x in c.cases],
        "initial_repeats": 1,
        "adaptive": {
            "max_repeats": 5,
            "rule": "disagree_or_borderline_v1",
            "borderline": [0.2, 0.9],
        },
        "budget": BUDGET,
        "max_parallel": 1,
        "frozen_at": now(),
    }
    plan.update(over)
    if "approval_ref" not in plan:
        plan["approval_ref"] = w.m.approve("experiment.execute", digest(plan))
    return plan


def service(w, policy_value="default"):
    """A calibration service whose per-trial reservation fits the scripted usage. The
    MetaReference executor policy reserves 0 tokens, so the scripted 10-token trials would
    settle as overruns and stop the run after one trial."""
    meta = w.m
    chosen = tokens_policy(w, TRIAL_TOKENS) if policy_value == "default" else policy_value
    return CalibrationService(
        meta.d.store,
        meta.d.contracts,
        meta.d.artifacts,
        approval_check=meta.check,
        executor_id="local-arithmetic-qualification",
        executor_policy=chosen,
    )


def tokens_policy(w, tokens):
    return dataclasses.replace(w.m.eval.executor_policy, max_trial_tokens=tokens)


def run_plan(w, c, *, svc=None, parallel=1, executor=None, **over):
    svc = svc or service(w)
    plan = plan_for(w, c, **over)
    plan_ref = svc.freeze(w.m.reviewer, plan)
    executor = executor or Scripted(w, c.roles, outcome, tokens=(5, 5))
    summary_ref = svc.run(w.m.reviewer, plan_ref, executor, parallel=parallel)
    store = w.m.d.store
    return SimpleNamespace(
        svc=svc,
        plan=plan,
        plan_ref=plan_ref,
        summary_ref=summary_ref,
        summary=store.get(w.scope, "calibration-summary", summary_ref),
        head=store.head(w.scope, "calibration-run", plan_ref["id"]),
        trials=lambda: [
            store.get(w.scope, "calibration-trial", r)
            for r in store.head(w.scope, "calibration-run", plan_ref["id"])["data"]["trial_refs"]
        ],
        executor=executor,
    )


# --- Q-11: adaptive repeats, classes, A/A discordance ------------------------------------------


def runs_of(r, cell, case_id):
    v1 = r.plan["composition_refs"][cell][0]
    return [t for t in r.trials() if t["composition_ref"] == v1 and t["task_id"] == case_id]


def test_q11_one_run_per_task_and_cell_then_repeats_only_where_cells_disagree(w):
    r = run_plan(w, make(w))
    expected_runs = {
        "dev-00": 1, "dev-01": 1, "val-00": 5, "val-01": 5, "val-02": 1, "val-03": 1,
    }  # fmt: skip
    for cell in CELLS:
        for case_id, n in expected_runs.items():
            runs = runs_of(r, cell, case_id)
            assert len(runs) == n, (cell, case_id)
            assert sorted(t["repeat"] for t in runs) == list(range(n))
    assert len(r.trials()) == 28
    assert r.head["state"] == "done" and r.head["data"]["stop_reason"] is None
    assert r.head["data"]["repeats_added"] == {"val-00": 8, "val-01": 8}
    assert r.summary["cells"]["cell-a"]["tasks"]["val-00"]["runs"] == 5


def test_q11_borderline_continues_after_the_cells_stop_disagreeing(w):
    # val-01: round one differs (T vs F); after two runs both cells sit at 0.5, inside
    # [0.2, 0.9], so the rule keeps adding repeats until max_repeats (5).
    r = run_plan(w, make(w))
    for cell in CELLS:
        rates = [
            t["success"] for t in sorted(runs_of(r, cell, "val-01"), key=lambda t: t["repeat"])
        ]
        assert len(rates) == 5
    a = r.summary["cells"]["cell-a"]["tasks"]["val-01"]
    assert (a["runs"], a["passes"], a["pass_rate"]) == (5, 3, 0.6)


@pytest.mark.parametrize("max_repeats,runs", [(1, 1), (2, 2), (3, 3), (5, 5)])
def test_q11_max_repeats_caps_every_task_and_cell(w, max_repeats, runs):
    adaptive = {
        "max_repeats": max_repeats,
        "rule": "disagree_or_borderline_v1",
        "borderline": [0.2, 0.9],
    }
    r = run_plan(w, make(w), adaptive=adaptive)
    for cell in CELLS:
        assert len(runs_of(r, cell, "val-00")) == runs
        assert len(runs_of(r, cell, "dev-00")) == 1


def test_q11_a_single_cell_never_repeats_because_one_run_is_never_borderline(w):
    c = make(w)
    r = run_plan(w, c, cells=["cell-a"], composition_refs={"cell-a": c.refs["cell-a"]})
    assert len(r.trials()) == 6
    assert r.head["data"]["repeats_added"] == {}
    assert r.summary["cells"]["cell-a"]["aa_discordance"] is None
    assert r.summary["cells"]["cell-a"]["noise_band_at"] is None


def test_q11_summary_classes_and_lists(w):
    r = run_plan(w, make(w))
    a = {k: v["class"] for k, v in r.summary["cells"]["cell-a"]["tasks"].items()}
    b = {k: v["class"] for k, v in r.summary["cells"]["cell-b"]["tasks"].items()}
    assert a == {
        "dev-00": "saturated", "dev-01": "unsolved", "val-00": "saturated",
        "val-01": "informative", "val-02": "saturated", "val-03": "saturated",
    }  # fmt: skip
    assert b["val-00"] == "unsolved" and b["val-01"] == "informative" and b["dev-01"] == "unsolved"
    assert r.summary["saturated_everywhere"] == ["dev-00", "val-02", "val-03"]
    assert r.summary["informative_any"] == ["val-01"]
    assert "flaky_grading" not in a.values()
    row = r.summary["cells"]["cell-a"]["tasks"]["val-01"]
    assert row["wilson_95"] == pytest.approx(list(wilson(3, 5, Z95)))


def test_q11_aa_discordance_noise_band_and_rates(w):
    r = run_plan(w, make(w))
    for cell in CELLS:
        row = r.summary["cells"][cell]
        # tasks with two or more runs: val-00 (same outcome twice) and val-01 (T then F / F then T)
        assert row["aa_discordance"] == 0.5
        assert row["noise_band_at"] == pytest.approx(
            {str(n): noise_band(0.5, n) for n in (16, 20, 30)}
        )
    a = r.summary["cells"]["cell-a"]
    assert a["pass_rate"] == pytest.approx((1 + 0 + 1 + 0.6 + 1 + 1) / 6)
    assert a["pass_k"] == {"k": 1, "rate": pytest.approx(5 / 6)}
    assert r.summary["cells"]["cell-b"]["pass_k"] == {"k": 1, "rate": pytest.approx(3 / 6)}
    low, high = a["pass_rate_interval"]
    assert 0 <= low <= a["pass_rate"] <= high <= 1
    assert a["tokens_per_solved"] == pytest.approx(140 / 11)
    assert a["median_seconds"] is not None and a["median_seconds"] >= 0


def test_q11_variants_run_in_the_same_slots_but_stay_out_of_the_summary(w):
    plain = run_plan(w, make(w))
    r = run_plan(w, make(w, variants=True))
    trials = r.trials()
    assert len(trials) == 42
    variant = r.plan["composition_refs"]["cell-a"][1]
    own = [t for t in trials if t["composition_ref"] == variant]
    assert len(own) == 14 and {t["cell_id"] for t in own} == {"cell-a"}
    assert sorted((t["task_id"], t["repeat"]) for t in own) == sorted(
        (t["task_id"], t["repeat"]) for t in runs_all_v1(r, "cell-a")
    )
    for cell in CELLS:
        assert r.summary["cells"][cell]["tasks"] == plain.summary["cells"][cell]["tasks"]


def runs_all_v1(r, cell):
    v1 = r.plan["composition_refs"][cell][0]
    return [t for t in r.trials() if t["composition_ref"] == v1]


def test_q11_trial_records_carry_the_split_cell_and_binding(w):
    r = run_plan(w, make(w))
    by_task = {t["task_id"]: t for t in r.trials()}
    assert by_task["dev-00"]["split"] == "development"
    assert by_task["val-02"]["split"] == "validation"
    t = by_task["dev-00"]
    assert t["schema"] == "amplai.calibration-trial.v1"
    assert t["calibration_plan_ref"] == r.plan_ref and t["mode"] == "sandbox_rerun"
    assert "experiment_ref" not in t and t["cell_id"] in CELLS
    assert t["executor_id"] == "local-arithmetic-qualification"


def test_q11_parallel_calibration_has_the_same_trials_and_summary(w):
    serial = run_plan(w, make(w))
    c = make(w)
    executor = Scripted(w, c.roles, outcome, tokens=(5, 5), delay=0.01)
    executor.prime(c.cases, 5)
    par = run_plan(w, c, parallel=4, executor=executor, max_parallel=4)

    def keys(r):
        return sorted((t["cell_id"], t["task_id"], t["repeat"]) for t in r.trials())

    assert keys(serial) == keys(par)
    for cell in CELLS:
        assert par.summary["cells"][cell]["tasks"] == serial.summary["cells"][cell]["tasks"]


# --- budget: adaptive repeats run "while budget remains" ---------------------------------------


def tight(tokens):
    return {**BUDGET, "max_tokens": tokens}


def test_budget_ending_the_adaptive_phase_is_a_normal_finish(w):
    # 12 first-round trials of 10 tokens, then room for two repeats only.
    svc = service(w, tokens_policy(w, 10))
    r = run_plan(w, make(w), svc=svc, budget=tight(145))
    assert len(r.trials()) == 14
    assert r.head["state"] == "done"
    assert r.head["data"]["stop_reason"] == "META_TOKEN_BUDGET"
    assert r.summary["cells"]["cell-a"]["tasks"]["val-00"]["runs"] == 2


def test_budget_ending_the_first_round_is_a_stop_with_unknown_classes(w):
    svc = service(w, tokens_policy(w, 10))
    r = run_plan(w, make(w), svc=svc, budget=tight(50))
    assert len(r.trials()) == 5
    assert r.head["state"] == "stopped"
    assert r.head["data"]["stop_reason"] == "META_TOKEN_BUDGET"
    classes = {v["class"] for v in r.summary["cells"]["cell-b"]["tasks"].values()}
    assert "unknown" in classes


def test_budget_too_small_for_one_trial_dispatches_nothing(w):
    c = make(w)
    svc = service(w, tokens_policy(w, 10))
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c, budget=tight(5)))
    executor = Scripted(w, c.roles, outcome)
    hold("META_TOKEN_BUDGET", svc.run, w.m.reviewer, plan_ref, executor)
    assert executor.calls == []
    head = w.m.d.store.head(w.scope, "calibration-run", plan_ref["id"])
    assert head["state"] == "stopped" and head["data"]["trial_refs"] == []


def test_root_budget_is_keyed_by_the_plan_id(w):
    r = run_plan(w, make(w))
    root = w.m.d.store.head(w.scope, "meta-budget", "calibration:" + r.plan_ref["id"])
    assert len(root["data"]["allocations"]) == 28
    assert root["data"]["experiment_refs"] == [r.plan_ref]
    assert r.plan_ref["id"].startswith("calplan-")


# --- identity, approval and state --------------------------------------------------------------


def test_freeze_needs_experiment_approve_and_refuses_a_proposer(w):
    c = make(w)
    svc, reviewer = service(w), w.m.reviewer
    plan = plan_for(w, c)
    nobody = dataclasses.replace(reviewer, permissions=frozenset({"corpus.read"}))
    fault("FORBIDDEN", svc.freeze, nobody, plan)
    proposer = dataclasses.replace(reviewer, permissions=reviewer.permissions | {"harness.propose"})
    hold("CALIBRATION_PROPOSER", svc.freeze, proposer, plan)
    ref = svc.freeze(reviewer, plan)
    hold("CALIBRATION_PROPOSER", svc.run, proposer, ref, Scripted(w, c.roles, outcome))
    fault("FORBIDDEN", svc.run, nobody, ref, Scripted(w, c.roles, outcome))


def test_the_operator_approval_binds_the_exact_plan(w):
    c = make(w)
    svc = service(w)
    wrong = w.m.approve("experiment.execute", "sha256:" + "0" * 64)
    hold("DEMO_APPROVAL", svc.freeze, w.m.reviewer, plan_for(w, c, approval_ref=wrong))
    other_action = w.m.approve("canary.execute", digest({"x": 1}))
    hold("DEMO_APPROVAL", svc.freeze, w.m.reviewer, plan_for(w, c, approval_ref=other_action))


def test_a_revoked_approval_stops_a_frozen_plan_before_any_spending(w):
    c = make(w)
    svc = service(w)
    plan = plan_for(w, c)
    ref = svc.freeze(w.m.reviewer, plan)
    w.m.approvals[digest(plan["approval_ref"])]["revoked"] = True
    executor = Scripted(w, c.roles, outcome)
    hold("DEMO_APPROVAL", svc.run, w.m.reviewer, ref, executor)
    assert executor.calls == []
    root = w.m.d.store.head(w.scope, "meta-budget", "calibration:" + ref["id"])
    assert root["data"]["allocations"] == {}
    assert w.m.d.store.head(w.scope, "calibration-run", ref["id"])["state"] == "frozen"


def test_revocation_during_the_run_stops_at_the_next_guard(w):
    c = make(w)
    svc = service(w)
    plan = plan_for(w, c)
    ref = svc.freeze(w.m.reviewer, plan)
    inner = Scripted(w, c.roles, outcome)

    def revoking(composition, case, repeat, mode):
        result = inner(composition, case, repeat, mode)
        w.m.approvals[digest(plan["approval_ref"])]["revoked"] = True
        return result

    svc.run(w.m.reviewer, ref, revoking)
    head = w.m.d.store.head(w.scope, "calibration-run", ref["id"])
    assert head["state"] == "stopped" and head["data"]["stop_reason"] == "DEMO_APPROVAL"
    assert len(head["data"]["trial_refs"]) == 1


def test_the_kill_switch_holds_freeze_and_run(w):
    c = make(w)
    svc = service(w)
    ref = svc.freeze(w.m.reviewer, plan_for(w, c))
    with w.m.d.store.tx() as db:
        w.m.d.store.cas(db, w.scope, "runtime-control", "kill", 0, "enabled", {"enabled": True})
    hold("KILL_SWITCH", svc.run, w.m.reviewer, ref, Scripted(w, c.roles, outcome))
    hold("KILL_SWITCH", svc.freeze, w.m.reviewer, plan_for(w, c, frozen_at="2026-10-02T00:00:00Z"))


def test_a_plan_runs_once_and_freezes_idempotently(w):
    c = make(w)
    svc = service(w)
    plan = plan_for(w, c)
    ref = svc.freeze(w.m.reviewer, plan)
    assert svc.freeze(w.m.reviewer, plan) == ref
    hold("CALIBRATION_STATE", svc.summarize, w.scope, ref)
    executor = Scripted(w, c.roles, outcome)
    summary_ref = svc.run(w.m.reviewer, ref, executor)
    hold("CALIBRATION_STATE", svc.run, w.m.reviewer, ref, executor)
    assert svc.summarize(w.scope, ref) == summary_ref
    assert summary_ref["id"] == "calsum-" + ref["id"]


def test_the_summary_pins_the_running_evaluator_version(w):
    r = run_plan(w, make(w))
    value = r.summary
    assert value["schema"] == "amplai.calibration-summary.v1"
    assert value["plan_ref"] == r.plan_ref
    assert value["evaluator_version_ref"] == versions.current_version_ref(w.m.d.store, w.scope)
    assert set(value) == {
        "schema", "scope", "plan_ref", "cells", "saturated_everywhere", "informative_any",
        "evaluator_version_ref", "summarized_at",
    }  # fmt: skip


def test_no_evaluator_version_means_no_spending(w):
    c = make(w, evaluator=False)
    svc = service(w)
    ref = svc.freeze(w.m.reviewer, plan_for(w, c))
    executor = Scripted(w, c.roles, outcome)
    hold("EVALUATOR_UNQUALIFIED", svc.run, w.m.reviewer, ref, executor)
    assert executor.calls == []
    assert w.m.d.store.head(w.scope, "calibration-run", ref["id"])["state"] == "frozen"


def test_the_executor_must_be_qualified_for_sandbox_reruns(w):
    c = make(w)
    plan = plan_for(w, c)
    base = w.m.eval.executor_policy
    static_only = dataclasses.replace(base, modes=frozenset({"static"}))
    svc = service(w, static_only)
    ref = svc.freeze(w.m.reviewer, plan)
    hold("EXECUTOR_MODE", svc.run, w.m.reviewer, ref, Scripted(w, c.roles, outcome))
    none = service(w, None)
    hold("QUALIFIED_EXECUTOR_REQUIRED", none.run, w.m.reviewer, ref, Scripted(w, c.roles, outcome))


def test_an_unknown_effect_stops_the_run_and_the_task_is_unknown(w):
    c = make(w)
    executor = Scripted(w, c.roles, outcome, raises=lambda role, cid, rep: cid == "val-00")
    r = run_plan(w, c, executor=executor)
    assert r.head["state"] == "stopped"
    assert r.head["data"]["stop_reason"] == "safety_or_unknown_effect"
    assert r.summary["cells"]["cell-a"]["tasks"]["val-00"]["class"] == "unknown"
    assert r.summary["cells"]["cell-a"]["tasks"]["val-03"]["class"] == "unknown"
    assert r.summary["cells"]["cell-a"]["tasks"]["dev-00"]["class"] == "saturated"


def holding_executor(w, c, evidence, code="TRIAL_VERIFIER"):
    """val-00 holds ``code`` (TRIAL_VERIFIER: the caltrial-ac33... shape); ``evidence`` (None:
    none) is what the executor attaches as ``NOT_RUN_EVIDENCE``."""
    scripted = Scripted(w, c.roles, outcome, tokens=(5, 5))

    def executor(composition, case, repeat, mode):
        if case["case_id"] == "val-00":
            exc = Hold(code, "held before the run")
            if evidence is not None:
                setattr(exc, NOT_RUN_EVIDENCE, evidence)
            raise exc
        return scripted(composition, case, repeat, mode)

    return executor


def evidence_of(goal_id="goal-held", planner_mode="fixed", base_checks=0, runs=(), dispatches=()):
    return {"goal_id": goal_id, "planner_mode": planner_mode, "base_checks": base_checks,
            "runs": list(runs), "dispatches": list(dispatches)}  # fmt: skip


@pytest.mark.parametrize(
    ("code", "evidence"),
    [("TRIAL_VERIFIER", evidence_of()),
     ("TRIAL_TASK", evidence_of()),
     ("TRIAL_BUSY", evidence_of(goal_id=None, planner_mode=None)),
     # before a goal exists the call itself starts no base check; a concurrent trial's count
     ("COMPOSITION_PIN", evidence_of(goal_id=None, planner_mode=None, base_checks=2))],
)  # fmt: skip
def test_a_hold_before_any_goal_ran_is_a_missing_outcome_with_its_code(w, code, evidence):
    # operator decision 2026-10-09: the Hold proves that no process started (a fixed draft's
    # Hold with no base check during the call, or a closed pre-goal Hold): no uncertain effect,
    # the run continues
    c = make(w)
    r = run_plan(w, c, executor=holding_executor(w, c, evidence, code))
    held = [t for t in r.trials() if t["task_id"] == "val-00"]
    assert held and all(t["error_type"] == "Hold" for t in held)
    trial = held[0]
    assert trial["error_code"] == code
    assert trial["unknown_effects"] == 0 and trial["success"] is None
    assert trial["not_run"] == {"goal_id": evidence["goal_id"], "runs": 0, "dispatches": 0}
    assert trial["outcome_missing"] == "not_run"
    assert trial["charged_tokens"] == TRIAL_TOKENS  # decision (B): usage unknown, reservation
    store = w.m.d.store
    assert store.head(w.scope, "calibration-trial", trial["trial_id"])["state"] == "observed"
    assert r.head["state"] == "done" and r.head["data"]["stop_reason"] is None
    root = store.head(w.scope, "meta-budget", "calibration:" + r.plan_ref["id"])
    assert root["data"]["allocations"][trial["trial_id"]]["status"] != "unknown"
    assert r.summary["cells"]["cell-a"]["tasks"]["val-00"]["class"] == "unknown"  # no outcome
    assert r.summary["cells"]["cell-a"]["tasks"]["dev-00"]["class"] == "saturated"


@pytest.mark.parametrize(
    ("code", "evidence"),
    [("TRIAL_VERIFIER", None),
     ("TRIAL_VERIFIER", evidence_of(runs=["run-1"], dispatches=["dispatch-1"])),
     ("TRIAL_VERIFIER", evidence_of(runs=["run-1"])),
     ("TRIAL_VERIFIER", {"runs": []}),
     ("TRIAL_VERIFIER", {"goal_id": "goal-held", "runs": [], "dispatches": []}),  # no counts
     ("TRIAL_VERIFIER", "nothing ran"),
     # a base check started during the call (product.py base_check: docker kill unconfirmed)
     ("TRIAL_VERIFIER", evidence_of(base_checks=1)),
     # the IC-18 review finding: a real planner turn timed out (readonly_turn.py docker kill)
     ("PLANNER_TIMEOUT", evidence_of(planner_mode="real")),
     ("PLANNER_FAILED", evidence_of(planner_mode="real")),
     ("TRIAL_VERIFIER", evidence_of(planner_mode="real")),
     ("PLANNER_TIMEOUT", evidence_of()),  # not a fixed draft's code
     ("TRIAL_VERIFIER", evidence_of(goal_id=None, planner_mode=None)),  # not a pre-goal code
     ("TARGET_UNKNOWN", evidence_of())],  # raised late in plan() too (graph compiler)
)  # fmt: skip
def test_a_hold_without_evidence_that_nothing_ran_stays_an_unknown_effect(w, code, evidence):
    # IC-18 fail closed: no or malformed evidence, a run, dispatch or base check in it, a real
    # planner, or a code outside the closed sets
    c = make(w)
    r = run_plan(w, c, executor=holding_executor(w, c, evidence, code))
    (trial,) = [t for t in r.trials() if t["task_id"] == "val-00"]
    assert trial["error_type"] == "Hold" and trial["error_code"] == code
    assert trial["unknown_effects"] == 1 and trial["success"] is None
    assert "not_run" not in trial and "outcome_missing" not in trial
    store = w.m.d.store
    assert store.head(w.scope, "calibration-trial", trial["trial_id"])["state"] == "unknown"
    assert r.head["state"] == "stopped"
    assert r.head["data"]["stop_reason"] == "safety_or_unknown_effect"
    root = store.head(w.scope, "meta-budget", "calibration:" + r.plan_ref["id"])
    assert root["data"]["allocations"][trial["trial_id"]]["status"] == "unknown"


# --- plan validation (2.8) -------------------------------------------------------------------

ADAPTIVE = {"max_repeats": 5, "rule": "disagree_or_borderline_v1", "borderline": [0.2, 0.9]}


def refused_plans(w, c):
    other = w.composition("unrelated")
    return {
        "extra field": {"extra": 1},
        "schema": {"schema": "amplai.calibration-plan.v2"},
        "initial repeats": {"initial_repeats": 2},
        "max repeats": {"adaptive": {**ADAPTIVE, "max_repeats": 6}},
        "zero repeats": {"adaptive": {**ADAPTIVE, "max_repeats": 0}},
        "rule": {"adaptive": {**ADAPTIVE, "rule": "always"}},
        "borderline order": {"adaptive": {**ADAPTIVE, "borderline": [0.9, 0.2]}},
        "borderline range": {"adaptive": {**ADAPTIVE, "borderline": [0.2, 1.5]}},
        "adaptive field": {"adaptive": {**ADAPTIVE, "seed": 1}},
        "holdout split": {"splits": ["holdout"]},
        "holdout among splits": {"splits": ["development", "holdout"]},
        "duplicate split": {"splits": ["development", "development"]},
        "attempts": {"budget": {**BUDGET, "max_attempts": 11}},
        "wall": {"budget": {**BUDGET, "max_wall_seconds": 604801}},
        "currency": {"budget": {**BUDGET, "currency": "usd"}},
        "budget field": {"budget": {**BUDGET, "extra": 1}},
        "parallel": {"max_parallel": 5},
        "no cases": {"case_ids": []},
        "no cells": {"cells": [], "composition_refs": {}},
        "duplicate cells": {"cells": ["cell-a", "cell-a"]},
        "refs of other cells": {"composition_refs": {"cell-a": c.refs["cell-a"]}},
        "duplicate compositions": {
            "composition_refs": {"cell-a": [c.comps["cell-a"]] * 2, "cell-b": c.refs["cell-b"]}
        },
        "composition kind": {
            "composition_refs": {"cell-a": [c.corpus_ref], "cell-b": c.refs["cell-b"]}
        },
        "corpus kind": {"corpus_ref": other},
        "frozen_at": {"frozen_at": 5},
    }


def test_a_malformed_plan_is_a_calibration_plan_fault_before_anything_is_stored(w):
    c = make(w)
    svc = service(w)
    for name, over in refused_plans(w, c).items():
        error = fault("CALIBRATION_PLAN", svc.freeze, w.m.reviewer, plan_for(w, c, **over))
        assert error.outcome == "rejected", name
    assert w.m.d.store.list_objects(w.scope, "calibration-plan") == []


def test_a_plan_of_another_scope_is_refused(w):
    c = make(w)
    other = {**w.scope.wire(), "project_id": "someone-else"}
    fault("SCOPE_MISMATCH", service(w).freeze, w.m.reviewer, plan_for(w, c, scope=other))


def test_the_pinned_cases_must_be_every_case_of_the_splits_in_corpus_order(w):
    c = make(w)
    svc = service(w)
    ids = [x["case_id"] for x in c.cases]
    for changed in (ids[:-1], ids[::-1], [*ids, "val-09"], [ids[1], ids[0], *ids[2:]]):
        hold("SAMPLING_CHANGED", svc.freeze, w.m.reviewer, plan_for(w, c, case_ids=changed))
    only_val = [i for i in ids if i.startswith("val")]
    ref = svc.freeze(w.m.reviewer, plan_for(w, c, splits=["validation"], case_ids=only_val))
    assert ref["id"].startswith("calplan-")


def test_the_holdout_is_never_read_by_a_calibration(w):
    corpus_ref, cases = w.corpus(val=2, hold_n=3)
    comps = {"cell-a": w.composition("cell-a")}
    w.evaluator(corpus_ref)
    ids = [x["case_id"] for x in cases if x["split"] != "holdout"]
    plan = {
        "schema": "amplai.calibration-plan.v1", "scope": w.scope.wire(),
        "cells": ["cell-a"], "composition_refs": {"cell-a": [comps["cell-a"]]},
        "corpus_ref": corpus_ref, "splits": ["validation"], "case_ids": ids,
        "initial_repeats": 1, "adaptive": ADAPTIVE, "budget": BUDGET, "max_parallel": 1,
        "frozen_at": now(),
    }  # fmt: skip
    plan["approval_ref"] = w.m.approve("experiment.execute", digest(plan))
    ref = service(w).freeze(w.m.reviewer, plan)
    executor = Scripted(w, comps, lambda role, cid, rep: True)
    service(w).run(w.m.reviewer, ref, executor)
    assert {call[1] for call in executor.calls} == {"val-00", "val-01"}
    with pytest.raises(RuntimeFault) as gone:
        w.m.d.store.head(w.scope, "holdout-use", corpus_ref["id"])
    assert gone.value.code == "NOT_FOUND"


# --- select_cases (IC-15, pure) ----------------------------------------------------------------

CASES = [
    {"case_id": f"c{i}", "task_class": d} for i, d in enumerate("abacba")
]  # a: c0 c2 c5, b: c1 c4, c: c3


def domain(case):
    return case["task_class"]


def summary_of(classes, cell="x"):
    return {"cells": {cell: {"tasks": {k: {"class": v} for k, v in classes.items()}}}}


def pick(rule="all_v1", summary=None, cell="x", max_tasks=10, cases=CASES):
    return select_cases(
        cases, rule=rule, summary=summary, cell_id=cell, max_tasks=max_tasks, domain_of=domain
    )


def test_select_all_round_robins_over_domains_and_returns_corpus_order():
    assert pick(max_tasks=4) == ["c0", "c1", "c2", "c3"]
    assert pick(max_tasks=2) == ["c0", "c1"]
    assert pick(max_tasks=10) == [f"c{i}" for i in range(6)]
    assert pick(max_tasks=1) == ["c0"]


def test_select_informative_keeps_only_that_cells_informative_tasks():
    classes = {"c0": "saturated", "c1": "informative", "c2": "informative", "c3": "unknown",
               "c4": "informative", "c5": "informative"}  # fmt: skip
    summary = summary_of(classes)
    # domains a: c2 c5, b: c1 c4, c: none; first-appearance order a, b, c
    assert pick("informative_v1", summary, max_tasks=3) == ["c1", "c2", "c5"]
    assert pick("informative_v1", summary, max_tasks=10) == ["c1", "c2", "c4", "c5"]
    assert pick("informative_v1", summary, max_tasks=1) == ["c2"]
    assert pick("informative_v1", summary, max_tasks=2) == ["c1", "c2"]
    both = {"cells": {**summary["cells"], "y": {"tasks": {"c3": {"class": "informative"}}}}}
    assert pick("informative_v1", both, cell="y") == ["c3"]
    assert pick("informative_v1", both, cell="x", max_tasks=10) == ["c1", "c2", "c4", "c5"]


def test_select_is_deterministic_and_ignores_the_summary_for_all_v1():
    summary = summary_of({"c0": "informative"})
    assert pick(summary=summary, max_tasks=3) == pick(max_tasks=3)
    assert pick(max_tasks=5) == pick(max_tasks=5)
    assert pick("informative_v1", summary, max_tasks=5) == ["c0"]


def test_select_domain_order_is_first_appearance_not_alphabetical():
    cases = [{"case_id": f"t{i}", "task_class": d} for i, d in enumerate(["z", "a", "z", "a"])]
    assert pick(max_tasks=2, cases=cases) == ["t0", "t1"]
    assert pick(max_tasks=3, cases=cases) == ["t0", "t1", "t2"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rule": "some_v1"},
        {"max_tasks": 0},
        {"max_tasks": True},
        {"max_tasks": "3"},
        {"rule": "informative_v1", "summary": None},
        {"rule": "informative_v1", "summary": {"cells": {}}},
        {"rule": "informative_v1", "summary": {"cells": {"x": {}}}},
        {"rule": "informative_v1", "summary": summary_of({}), "cell": "other"},
        {"cases": [{"case_id": "a", "task_class": "d"}, {"case_id": "a", "task_class": "d"}]},
        {"cases": [{"case_id": "", "task_class": "d"}]},
    ],
)
def test_select_refuses_malformed_inputs(kwargs):
    fault("SAMPLING_PLAN", pick, **kwargs)


# --- summary validation and a real summary driving EvaluationService (IC-15) --------------------


def test_validate_summary_refuses_unknown_classes_and_missing_fields():
    ref = {"id": "x", "revision": 1, "digest": "sha256:" + "a" * 64}
    good = {
        "schema": "amplai.calibration-summary.v1", "scope": {}, "plan_ref": ref,
        "cells": {"c": {"tasks": {"t": {"class": "informative"}}}},
        "saturated_everywhere": [], "informative_any": [], "evaluator_version_ref": ref,
        "summarized_at": "2026-10-01T00:00:00Z",
    }  # fmt: skip
    validate_summary(good)
    bad_class = {**good, "cells": {"c": {"tasks": {"t": {"class": "flaky"}}}}}
    fault("CALIBRATION_SUMMARY", validate_summary, bad_class)
    fault("CALIBRATION_SUMMARY", validate_summary, {k: v for k, v in good.items() if k != "scope"})
    fault("CALIBRATION_SUMMARY", validate_summary, {**good, "plan_ref": "p"})


def test_a_real_summary_selects_the_informative_subset_for_a_versioned_plan(w):
    c = make(w)
    r = run_plan(w, c)
    base, cand = c.comps["cell-a"], c.comps["cell-b"]
    s = SimpleNamespace(
        corpus_ref=c.corpus_ref, cases=c.cases, ev=c.ev, base=base, cand=cand,
        prop=w.proposal(base, cand), ref=None,
    )  # fmt: skip
    cases = [x for x in c.cases if x["split"] == "validation"]
    chosen = select_cases(
        cases,
        rule="informative_v1",
        summary=r.summary,
        cell_id="cell-a",
        max_tasks=12,
        domain_of=lambda case: case["task_class"],
    )
    assert chosen == ["val-01"]
    sampling = subset("informative_v1", 12, cell="cell-a", summary=r.summary_ref)
    plan = w.experiment(s, policy(c.ev), ids=["val-01"], sampling=sampling)
    ref = w.freeze(plan)
    executor = Scripted(w, {"baseline": base, "candidate": cand})
    report_ref = w.run(ref, executor)
    assert {t["task_id"] for t in w.trials(report_ref)} == {"val-01"}
    wrong = w.experiment(s, policy(c.ev), ids=["val-00", "val-01"], sampling=sampling)
    hold("SAMPLING_CHANGED", w.freeze, wrong)
