"""D-128 (operator decision 2026-10-14): calibration overrun, resume, imports and accounting.

interfaces.md "Clarification: Calibration Resume (D-128)". Real: store, budget ledgers,
CalibrationService, EvaluationService, BudgetService, pricing. Stand-ins: the scripted executors
of the S2 tests, whose receipts here also carry the reuse-key facts the trial executor records
(``harness_sha``, ``model_snapshot``, ``corpus_version``), and a ``facts`` function standing in
for ``LocalTrialExecutor.facts``.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_033_s2_calibration import CELLS, evidence_of, make, outcome, plan_for, runs_of
from test_033_s2_service import BUDGET, Scripted, fault, hold, make_world, policy
from test_033_s11_stages import CELL as STAGE_CELL
from test_033_s11_stages import build_world
from test_033_unknown_usage_charge import UnknownUsage

from amplai_foundry.evaluation import calibration, pricing
from amplai_foundry.evaluation.calibration import CalibrationService
from amplai_foundry.evaluation.service import NOT_RUN_EVIDENCE
from amplai_foundry.meta_harness.budget import EvolutionBudget
from amplai_foundry.runtime.budgets.service import BudgetService
from amplai_foundry.runtime.contracts.identity import canonical, digest
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import meta_ops

TRIAL_TOKENS = 10  # the scripted trials report 5 + 5 tokens
SHA = "0ee15f080276a21d8fa03952348efa67bb4f9aa2"
SNAPSHOT = {"provider_model_id": "claude-sonnet-5", "driver_version": "2.1.292",
            "image": "localhost:5000/amplai-worker@sha256:" + "b" * 64}  # fmt: skip


@pytest.fixture
def w(tmp_path):
    with make_world(tmp_path) as world:
        yield world


class Facts:
    """What a trial would run on now (``LocalTrialExecutor.facts``); tests change ``value``."""

    def __init__(self) -> None:
        self.value: dict[str, Any] = {
            "harness_sha": SHA, "model_snapshot": dict(SNAPSHOT), "corpus_version": "2.1.0",
        }  # fmt: skip

    def __call__(self, composition: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(self.value))


class WithFacts:
    """An executor whose receipts also record the facts current when the trial ran."""

    def __init__(self, w, inner, facts: Facts) -> None:
        self.w, self.inner, self.facts = w, inner, facts
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, composition, case, repeat, mode):
        observation = self.inner(composition, case, repeat, mode)
        self.calls.append(self.inner.calls[-1])
        d = self.w.m.d
        receipt = json.loads(d.artifacts.read(d.scope, observation.artifact_refs[0], trusted=True))
        receipt.update(self.facts(composition, case))
        ref = d.artifacts.admit(d.scope, canonical(receipt), "application/json", trust="verifier")
        return dataclasses.replace(observation, artifact_refs=(ref,))


def steady(role, case_id, repeat):
    """``outcome`` without its repeat pattern: a task's result does not change between runs."""
    return outcome(role, case_id, 0)


def make_service(w, facts=None, *, ceiling=TRIAL_TOKENS, weigher=None):
    meta = w.m
    return CalibrationService(
        meta.d.store, meta.d.contracts, meta.d.artifacts,
        approval_check=meta.check, executor_id="local-arithmetic-qualification",
        executor_policy=dataclasses.replace(meta.eval.executor_policy, max_trial_tokens=ceiling),
        facts=facts, ceiling_tokens=weigher,
    )  # fmt: skip


def head(w, plan_ref):
    return w.m.d.store.head(w.scope, "calibration-run", plan_ref["id"])


def trials(w, plan_ref):
    store = w.m.d.store
    return [
        store.get(w.scope, "calibration-trial", r) for r in head(w, plan_ref)["data"]["trial_refs"]
    ]


def allocations(w, plan_ref):
    root = "calibration:" + plan_ref["id"]
    return w.m.d.store.head(w.scope, "meta-budget", root)["data"]["allocations"]


def summaries(w, plan_ref):
    rows = [
        (ref, value)
        for ref, value in w.m.d.store.list_objects(w.scope, "calibration-summary")
        if ref["id"] == "calsum-" + plan_ref["id"]
    ]
    return sorted(rows, key=lambda row: row[0]["revision"])


# --- 1. the per-trial ceiling: recorded, not a stop; judged cost-weighted -----------------------


def test_an_overrun_judged_by_the_weighed_count_is_recorded_and_the_run_goes_on(w):
    c = make(w)
    weigher = lambda trial: {"basis": "raw", "reason": "no_price"}  # noqa: E731
    svc = make_service(w, weigher=weigher, ceiling=9)  # 10 raw tokens against a ceiling of 9
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c))
    svc.run(w.m.reviewer, plan_ref, Scripted(w, c.roles, outcome, tokens=(5, 5)))
    h = head(w, plan_ref)
    assert h["state"] == "done" and h["data"]["stop_reason"] is None
    stored = trials(w, plan_ref)
    assert len(stored) == 28  # every trial of the adaptive rule ran (test_q11)
    assert all(t["ceiling_overrun"] is True for t in stored)
    assert stored[0]["ceiling_tokens"] == {
        "tokens": 10, "basis": "raw", "price_table_id": None, "reason": "no_price",
    }  # fmt: skip
    # META_PRIOR_OVERRUN does not block a calibration root after a recorded overrun
    assert all(
        a["overrun"] is True and a["tokens"] == 10 for a in allocations(w, plan_ref).values()
    )
    assert {o["trial_id"] for o in h["data"]["overruns"]} == {t["trial_id"] for t in stored}
    _, summary = summaries(w, plan_ref)[-1]
    assert summary["warnings"][0]["code"] == "TRIAL_OVER_CEILING"
    assert summary["warnings"][0]["ceiling"] == 9


def test_the_ceiling_is_judged_by_the_price_weighted_count_and_the_ledger_keeps_raw_tokens(w):
    c = make(w)
    weighed = {"tokens": 3, "basis": "price_weighted", "price_table_id": "2026-09-30"}
    svc = make_service(w, weigher=lambda trial: dict(weighed), ceiling=9)
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c))
    svc.run(w.m.reviewer, plan_ref, Scripted(w, c.roles, outcome, tokens=(5, 5)))
    stored = trials(w, plan_ref)
    assert not any(t["ceiling_overrun"] for t in stored)
    assert stored[0]["ceiling_tokens"] == {**weighed, "reason": None}
    assert stored[0]["input_tokens"] + stored[0]["output_tokens"] == 10  # `tokens` unchanged
    allocation = next(iter(allocations(w, plan_ref).values()))
    assert allocation["tokens"] == 10 and allocation["judged_tokens"] == 3
    assert allocation["overrun"] is False
    assert "overruns" not in head(w, plan_ref)["data"]
    assert "warnings" not in summaries(w, plan_ref)[-1][1]


def test_the_uncached_equivalent_of_the_4_7m_token_sonnet_trial_is_about_0_75m():
    # caltrial-12b6e71f (calplan-7c314b2f, 2026-10-12): run usage detail of its one run; it
    # settled 4,703,518 tokens against the 4M ceiling and stopped the calibration
    row = pricing.load_tables()[0].models["claude-sonnet-5"]
    detail = {"provider": "claude", "input_includes_cache": False, "fields": {
        "input_tokens": 84, "cache_read_input_tokens": 4514585,
        "cache_creation_input_tokens": 108217, "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 108217, "output_tokens": 80632}}  # fmt: skip
    total, notes = pricing.uncached_equivalent(row, detail=detail, usage=None)
    # 84 + 4,514,585 x 0.1 + 108,217 x 2 + 80,632 = 748,608.5; the input side alone 667,976.5
    assert total == pytest.approx(748608.5) and notes == []
    assert total < 4_000_000 < 84 + 4514585 + 108217 + 80632
    # without a breakdown every input token counts 1.0 (an upper bound)
    total, notes = pricing.uncached_equivalent(
        row, detail=None, usage={"input_tokens": 100, "output_tokens": 5}
    )
    assert total == 105 and notes == ["no_cache_breakdown"]
    assert pricing.uncached_equivalent(row, detail=None, usage={"input_tokens": None}) is None


def test_an_experiment_root_still_refuses_after_an_overrun():
    """Stage experiments keep META_PRIOR_OVERRUN (the default ``prior_overrun_blocks``)."""

    class Fake:
        def __init__(self):
            self.heads = {}
            self.epoch = 1

        def head(self, scope, kind, oid, db=None):
            return self.heads[oid]

        def cas(self, db, scope, kind, oid, version, state, data):
            self.heads[oid] = {"row_version": version + 1, "state": state, "data": data}

        def clock(self):
            return 0.0

    store = Fake()
    budget = dict(BUDGET)
    over = {"status": "settled", "tokens": 11, "cost": 0, "overrun": True}
    store.heads["root"] = {"row_version": 1, "data": {
        "limits": budget, "experiment_refs": [], "started_epoch": 0.0,
        "allocations": {"a": over}}}  # fmt: skip
    ledger = EvolutionBudget(store)  # type: ignore[arg-type]
    hold("META_PRIOR_OVERRUN", ledger.reserve, None, None, "root", "b", tokens=10, cost=0)
    ledger.reserve(None, None, "root", "b", tokens=10, cost=0, prior_overrun_blocks=False)
    assert store.heads["root"]["data"]["allocations"]["b"]["status"] == "reserved"


# --- 3. accounting: a trial that ran nothing is charged 0 --------------------------------------


def not_run_executor(w, c):
    scripted = Scripted(w, c.roles, outcome, tokens=(5, 5))

    def executor(composition, case, repeat, mode):
        if case["case_id"] == "val-00":
            exc = Hold("TRIAL_BUSY", "held before any goal")
            setattr(exc, NOT_RUN_EVIDENCE, evidence_of(goal_id=None, planner_mode=None))
            raise exc
        return scripted(composition, case, repeat, mode)

    return executor


def test_a_calibration_trial_that_ran_nothing_settles_at_zero_tokens(w):
    c = make(w)
    svc = make_service(w)
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c))
    svc.run(w.m.reviewer, plan_ref, not_run_executor(w, c))
    held = [t for t in trials(w, plan_ref) if t["task_id"] == "val-00"]
    assert held and all(t["outcome_missing"] == "not_run" for t in held)
    assert all(t["charged_tokens"] == 0 for t in held)
    ledger = allocations(w, plan_ref)
    assert all(ledger[t["trial_id"]]["tokens"] == 0 for t in held)
    assert all(ledger[t["trial_id"]]["status"] == "settled" for t in held)


def test_an_experiment_trial_that_ran_nothing_settles_at_zero_tokens(w):
    s = w.setup(val=4)
    w.m.eval.executor_policy = dataclasses.replace(w.m.eval.executor_policy, max_trial_tokens=100)
    plan = w.experiment(s, policy(s.ev))
    ref = w.freeze(plan)
    scripted = Scripted(w, w.roles(s), tokens=(5, 5))

    def executor(composition, case, repeat, mode):
        if case["case_id"] == "val-00":
            exc = Hold("TRIAL_BUSY", "held before any goal")
            setattr(exc, NOT_RUN_EVIDENCE, evidence_of(goal_id=None, planner_mode=None))
            raise exc
        return scripted(composition, case, repeat, mode)

    report_ref = w.run(ref, executor)
    held = [t for t in w.trials(report_ref) if t["task_id"] == "val-00"]
    assert held and all(t["charged_tokens"] == 0 for t in held)
    root = w.budget(plan["proposal_ref"]["id"])["data"]["allocations"]
    assert all(root[t["trial_id"]]["tokens"] == 0 for t in held)


# --- 2a. resume (same plan) --------------------------------------------------------------------


def stopped_run(w, c, facts, *, unknown=None):
    """A run stopped by its provider-failure streak: val-00 and val-01 report no usage."""
    svc = make_service(w, facts)
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c))
    unknown = unknown or (lambda role, cid, rep: cid in ("val-00", "val-01"))
    first = WithFacts(w, UnknownUsage(w, c.roles, outcome, tokens=(5, 5), unknown=unknown), facts)
    svc.run(w.m.reviewer, plan_ref, first)
    assert head(w, plan_ref)["data"]["stop_reason"] == "provider_failures"
    return svc, plan_ref


def test_resume_keeps_graded_trials_reruns_the_rest_and_writes_a_new_summary_revision(w):
    c, facts = make(w), Facts()
    svc, plan_ref = stopped_run(w, c, facts)
    before = head(w, plan_ref)["data"]["trial_refs"]
    graded = [
        r for r, t in zip(before, trials(w, plan_ref), strict=True) if t["success"] is not None
    ]
    assert len(before) == 8 and len(graded) == 4  # dev-00, dev-01 in both cells
    (_, partial), = summaries(w, plan_ref)  # fmt: skip
    assert partial["partial"] is True and partial["run_segment"] == 0
    assert calibration.complete_summaries(w.m.d.store, w.scope) == []

    second = WithFacts(w, Scripted(w, c.roles, outcome, tokens=(5, 5)), facts)
    svc.resume(w.m.reviewer, plan_ref, second)
    h = head(w, plan_ref)
    assert h["state"] == "done" and h["data"]["stop_reason"] is None
    assert h["data"]["trial_refs"][:4] == graded  # kept, never re-run
    assert len(h["data"]["superseded_trial_refs"]) == 4
    assert [(r["kept"], r["superseded"]) for r in h["data"]["resumes"]] == [(4, 4)]
    assert not any(cid.startswith("dev-") and rep == 0 for _, cid, rep in second.calls)
    # the same runs per (cell, task) as an uninterrupted run (test_q11)
    r = SimpleNamespace(plan={"composition_refs": c.refs}, trials=lambda: trials(w, plan_ref))
    expected = {"dev-00": 1, "dev-01": 1, "val-00": 5, "val-01": 5, "val-02": 1, "val-03": 1}
    for cell in CELLS:
        for case_id, n in expected.items():
            assert sorted(t["repeat"] for t in runs_of(r, cell, case_id)) == list(range(n))
    rows = summaries(w, plan_ref)
    assert [ref["revision"] for ref, _ in rows] == [1, 2]
    assert rows[-1][1]["partial"] is False and rows[-1][1]["run_segment"] == 1
    assert calibration.complete_summaries(w.m.d.store, w.scope) == [rows[-1]]
    assert rows[-1][1]["cells"]["cell-a"]["tasks"]["val-01"]["runs"] == 5


@pytest.mark.parametrize(
    ("field", "value"),
    [("harness_sha", "1" * 40),
     ("model_snapshot", {**SNAPSHOT, "driver_version": "2.1.300"}),
     ("model_snapshot", {**SNAPSHOT, "image": "localhost:5000/amplai-worker@sha256:" + "c" * 64}),
     ("corpus_version", "2.2.0")],
)  # fmt: skip
def test_resume_refuses_a_behaviour_affecting_difference_with_the_differences(w, field, value):
    c, facts = make(w), Facts()
    svc, plan_ref = stopped_run(w, c, facts)
    facts.value[field] = value
    second = WithFacts(w, Scripted(w, c.roles, outcome), facts)
    caught = hold("CALIBRATION_RESUME_CHANGED", svc.resume, w.m.reviewer, plan_ref, second)
    differences = caught.details["differences"]
    assert {d["field"] for d in differences} == {field}
    assert {d["cell_id"] for d in differences} == set(CELLS)
    assert all(d["now"] == value for d in differences)
    assert second.calls == [] and head(w, plan_ref)["state"] == "stopped"


def test_resume_refuses_an_unknown_harness_sha_and_another_evaluator_version(w):
    c, facts = make(w), Facts()
    facts.value["harness_sha"] = "unknown"  # §8.5: a key without the harness sha is never reused
    svc, plan_ref = stopped_run(w, c, facts)
    caught = hold("CALIBRATION_RESUME_CHANGED", svc.resume, w.m.reviewer, plan_ref,
                  WithFacts(w, Scripted(w, c.roles, outcome), facts))  # fmt: skip
    assert {d["field"] for d in caught.details["differences"]} == {"harness_sha"}
    facts.value["harness_sha"] = SHA
    store = w.m.d.store
    with store.tx() as db:  # a run recorded under another evaluator version
        h = store.head(w.scope, "calibration-run", plan_ref["id"], db=db)
        other = {**h["data"]["evaluator_version_ref"], "id": "evaluator-1"}
        store.cas(db, w.scope, "calibration-run", plan_ref["id"], h["row_version"], "stopped",
                  {**h["data"], "evaluator_version_ref": other})  # fmt: skip
    caught = hold("CALIBRATION_RESUME_CHANGED", svc.resume, w.m.reviewer, plan_ref,
                  WithFacts(w, Scripted(w, c.roles, outcome), facts))  # fmt: skip
    fields = {d["field"] for d in caught.details["differences"]}
    assert "evaluator_version_ref" in fields


def test_resume_needs_a_stopped_run_settled_allocations_and_wall_time_left(w, monkeypatch):
    c, facts = make(w), Facts()
    svc = make_service(w, facts)
    frozen = svc.freeze(w.m.reviewer, plan_for(w, c))
    hold("CALIBRATION_RESUME_STATE", svc.resume, w.m.reviewer, frozen, Scripted(w, c.roles))
    svc.run(w.m.reviewer, frozen, WithFacts(w, Scripted(w, c.roles, outcome), facts))
    hold("CALIBRATION_RESUME_STATE", svc.resume, w.m.reviewer, frozen, Scripted(w, c.roles))

    # an unknown effect leaves its allocation unknown: reconcile it first (IC-18)
    c2 = make(w)
    raising = Scripted(w, c2.roles, outcome, tokens=(5, 5),
                       raises=lambda role, cid, rep: cid == "dev-01")  # fmt: skip
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c2))
    svc.run(w.m.reviewer, plan_ref, WithFacts(w, raising, facts))
    assert head(w, plan_ref)["data"]["stop_reason"] == "safety_or_unknown_effect"
    caught = hold("CALIBRATION_RESUME_UNRESOLVED", svc.resume, w.m.reviewer, plan_ref,
                  Scripted(w, c2.roles))  # fmt: skip
    assert [a["status"] for a in caught.details["allocations"]] == ["unknown"]

    # the approved wall time runs from the freeze; a resume never resets it
    c3 = make(w)
    _, stopped = stopped_run(w, c3, facts)
    store = w.m.d.store
    started = store.head(w.scope, "meta-budget", "calibration:" + stopped["id"])["data"]
    monkeypatch.setattr(
        store, "clock", lambda: started["started_epoch"] + BUDGET["max_wall_seconds"]
    )
    hold("META_WALL_BUDGET", svc.resume, w.m.reviewer, stopped, Scripted(w, c3.roles))


def test_resume_without_executor_facts_is_refused(w):
    c, facts = make(w), Facts()
    _, plan_ref = stopped_run(w, c, facts)
    blind = make_service(w, None)
    hold("CALIBRATION_FACTS_UNAVAILABLE", blind.resume, w.m.reviewer, plan_ref,
         Scripted(w, c.roles, outcome))  # fmt: skip


# --- 2b, 2c. imports into a new plan -----------------------------------------------------------


def import_policy(cells=CELLS, **over):
    value = {"window_hours": 72, "drift_share": 0.1, "drift_min_per_cell": 4,
             "drift_reference": {cell: None for cell in cells}}  # fmt: skip
    value.update(over)
    return value


def source_run(w, c, facts, *, executor=None, svc=None):
    svc = svc or make_service(w, facts)
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c))
    executor = executor or WithFacts(w, Scripted(w, c.roles, steady, tokens=(5, 5)), facts)
    svc.run(w.m.reviewer, plan_ref, executor)
    return svc, plan_ref


def importing_plan(w, c, svc, sources, *, policy_value=None, **over):
    """A plan of ``c`` that imports the eligible trials of ``sources`` (as the operator's
    ``calibrate --import-from`` composes it), approved with its imports."""
    plan = plan_for(w, c, **over)
    del plan["approval_ref"]
    plan["import_policy"] = policy_value or import_policy()
    found = svc.eligible_imports(w.scope, plan, sources)
    plan["imports"] = found["imports"]
    plan["approval_ref"] = w.m.approve("experiment.execute", digest(plan))
    return plan, found


def test_imported_trials_carry_provenance_and_a_zero_charge_and_fill_round_one(w):
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    plan, found = importing_plan(w, c, svc, [source])
    # every round-1 trial of the source; its adaptive repeats are not round 1
    assert found["imported"] == 12
    assert {e["reason"] for e in found["excluded"]} <= {"not_round_one"}
    assert found["cases"] == [x["case_id"] for x in c.cases]
    plan_ref = svc.freeze(w.m.reviewer, plan)
    executor = WithFacts(w, Scripted(w, c.roles, steady, tokens=(5, 5)), facts)
    svc.run(w.m.reviewer, plan_ref, executor)
    h = head(w, plan_ref)
    assert h["state"] == "done"
    # round 1 came from the source; only the drift sample (4 tasks per cell) ran at repeat 1
    assert not any(rep == 0 for _, _, rep in executor.calls)
    for cell in CELLS:
        assert h["data"]["drift_check"][cell]["status"] == "pass"
        assert len(h["data"]["drift_check"][cell]["sample"]) == 4
    imported = [t for t in trials(w, plan_ref) if "imported_from" in t]
    assert len(imported) == 12
    source_ids = {t["trial_id"] for t in trials(w, source)}
    ledger = allocations(w, plan_ref)
    for t in imported:
        provenance = t["imported_from"]
        assert provenance["plan_ref"] == source and provenance["trial_id"] in source_ids
        assert provenance["finished_at"] == t["finished_at"] and provenance["key_digest"]
        assert t["calibration_plan_ref"] == plan_ref and t["repeat"] == 0
        allocation = ledger[t["trial_id"]]
        assert allocation["tokens"] == 0 and allocation["status"] == "settled"
        assert allocation["imported"] == provenance
    _, summary = summaries(w, plan_ref)[-1]
    assert summary["imports"] == {"sources": [source], "imported": 12, "pooled": 12, "dropped": []}
    assert summary["partial"] is False


def test_a_key_mismatch_is_excluded_and_a_plan_that_imports_it_is_refused(w):
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    facts.value["model_snapshot"] = {**SNAPSHOT, "driver_version": "2.1.300"}
    plan = plan_for(w, c)
    plan["import_policy"] = import_policy()
    found = svc.eligible_imports(w.scope, plan, [source])
    assert found["imports"] == [] and found["imported"] == 0
    mismatched = [e for e in found["excluded"] if e["reason"] == "key_mismatch"]
    assert len(mismatched) == 12 and {tuple(e["fields"]) for e in mismatched} == {
        ("model_snapshot",)
    }
    # a plan that lists them anyway is refused at freeze
    source_refs = [r for r, t in zip(head(w, source)["data"]["trial_refs"], trials(w, source),
                                     strict=True) if t["repeat"] == 0]  # fmt: skip
    del plan["approval_ref"]
    plan["imports"] = [{"plan_ref": source, "trial_refs": source_refs}]
    plan["approval_ref"] = w.m.approve("experiment.execute", digest(plan))
    caught = hold("CALIBRATION_IMPORT", svc.freeze, w.m.reviewer, plan)
    assert {e["reason"] for e in caught.details["excluded"]} == {"key_mismatch"}


def test_only_cases_complete_in_every_cell_and_composition_are_imported(w):
    c, facts = make(w), Facts()
    # val-02 of cell-b has no graded outcome in the source run
    unknown = lambda role, cid, rep: (role, cid) == ("cell-b", "val-02")  # noqa: E731
    inner = UnknownUsage(w, c.roles, steady, tokens=(5, 5), unknown=unknown)
    svc, source = source_run(w, c, facts, executor=WithFacts(w, inner, facts))
    plan = plan_for(w, c)
    plan["import_policy"] = import_policy()
    found = svc.eligible_imports(w.scope, plan, [source])
    assert "val-02" not in found["cases"] and len(found["cases"]) == 5
    reasons = {(e["reason"]) for e in found["excluded"] if e["trial_id"] is not None}
    assert {"partial_case", "not_graded"} <= reasons
    # a plan with a variant the source never ran imports nothing (no case is complete)
    v = make(w, variants=True)
    _, source_v = source_run(w, v, facts, svc=svc)
    other = [v.refs["cell-a"][0], w.composition("new")]
    variant_plan = plan_for(w, v, composition_refs={**v.refs, "cell-a": other})
    variant_plan["import_policy"] = import_policy()
    found = svc.eligible_imports(w.scope, variant_plan, [source_v])
    assert found["imported"] == 0
    assert {e["reason"] for e in found["excluded"]} >= {"partial_case", "composition_mismatch"}


def test_imports_honour_the_time_window_between_source_finish_and_freeze(w):
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    late = (datetime.now(UTC) + timedelta(hours=73)).isoformat().replace("+00:00", "Z")
    plan = plan_for(w, c, frozen_at=late)
    plan["import_policy"] = import_policy()
    found = svc.eligible_imports(w.scope, plan, [source])
    assert found["imported"] == 0
    assert {e["reason"] for e in found["excluded"]} == {"outside_window", "not_round_one"}
    early = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    plan["frozen_at"] = early  # a source trial finished after the freeze
    assert svc.eligible_imports(w.scope, plan, [source])["imported"] == 0
    plan["frozen_at"] = late
    plan["import_policy"] = import_policy(window_hours=80)
    assert svc.eligible_imports(w.scope, plan, [source])["imported"] == 12


def test_a_failed_drift_check_drops_that_cells_imports_and_runs_them_fresh(w):
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    plan, _ = importing_plan(w, c, svc, [source])
    plan_ref = svc.freeze(w.m.reviewer, plan)

    def drifted(role, case_id, repeat):
        # cell-a now fails what it solved and solves what it failed; cell-b is unchanged
        value = steady(role, case_id, repeat)
        return (not value) if role == "cell-a" else value

    executor = WithFacts(w, Scripted(w, c.roles, drifted, tokens=(5, 5)), facts)
    svc.run(w.m.reviewer, plan_ref, executor)
    h = head(w, plan_ref)
    drift = h["data"]["drift_check"]
    assert drift["cell-a"]["status"] == "fail" and drift["cell-a"]["flips"] == 4
    assert drift["cell-b"]["status"] == "pass" and drift["cell-b"]["flips"] == 0
    assert len(drift["cell-a"]["dropped"]) == 6
    pooled = trials(w, plan_ref)
    assert not any("imported_from" in t and t["cell_id"] == "cell-a" for t in pooled)
    assert sum("imported_from" in t for t in pooled) == 6  # cell-b's
    # cell-a's imported tasks ran fresh: the drift sample's re-runs count as their round 1, the
    # rest ran at repeat 0
    sample = set(drift["cell-a"]["sample"])
    fresh_a = {(cid, rep) for role, cid, rep in executor.calls if role == "cell-a"}
    assert {cid for cid, rep in fresh_a} == {x["case_id"] for x in c.cases}
    assert {cid for cid, rep in fresh_a if rep == 1} >= sample  # the drift re-runs
    # the tasks outside the sample ran at repeat 0 in round 1
    assert {cid for cid, rep in fresh_a if rep == 0} >= {x["case_id"] for x in c.cases} - sample
    assert all(t["cell_id"] != "cell-a" or "imported_from" not in t for t in pooled)
    _, summary = summaries(w, plan_ref)[-1]
    assert summary["drift_check"]["cell-a"]["status"] == "fail"
    assert summary["imports"]["pooled"] == 6 and len(summary["imports"]["dropped"]) == 6


def test_drift_verdict_uses_the_cells_aa_discordance():
    # no measured discordance: any flip fails; four agreeing pairs pass
    assert calibration.drift_verdict(4, [False] * 4, None) == ("pass", 0.0)
    assert calibration.drift_verdict(4, [True, False, False, False], None)[0] == "fail"
    # a flaky cell (A/A discordance 0.5) tolerates one flip in four
    assert calibration.drift_verdict(4, [True, False, False, False], 0.5)[0] == "pass"
    assert calibration.drift_verdict(4, [True] * 4, 0.3)[0] == "fail"
    # fewer than half the sample compared is inconclusive (its imports are dropped too)
    assert calibration.drift_verdict(4, [False], None) == ("inconclusive", None)
    assert calibration.drift_verdict(4, [], 0.2) == ("inconclusive", None)


def test_import_plans_are_validated_and_bound_by_the_approval(w):
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    plan, _ = importing_plan(w, c, svc, [source])
    for broken in (
        {k: v for k, v in plan.items() if k != "import_policy"},
        {**plan, "import_policy": {**plan["import_policy"], "window_hours": 0}},
        {**plan, "import_policy": {**plan["import_policy"], "drift_reference": {}}},
        {**plan, "imports": []},
        {**plan, "purpose": "drift"},
    ):
        fault("CALIBRATION_PLAN", calibration.validate_calibration_plan, broken)
    # the imports are part of the approved content
    assert calibration.plan_id(plan) != calibration.plan_id(
        {k: v for k, v in plan.items() if k not in ("imports", "import_policy")}
    )
    entry = plan["imports"][0]
    tampered = {**plan, "imports": [{**entry, "trial_refs": entry["trial_refs"][1:]}]}
    with pytest.raises((Hold, RuntimeFault)):
        svc.freeze(w.m.reviewer, tampered)
    # the drift check re-runs a sample: a plan with imports needs max_repeats >= 2
    single, _ = importing_plan(w, c, svc, [source],
                               adaptive={"max_repeats": 1, "rule": "disagree_or_borderline_v1",
                                         "borderline": [0.2, 0.9]})  # fmt: skip
    fault("CALIBRATION_PLAN", svc.freeze, w.m.reviewer, single)


# --- 4. partial summaries are never selected ---------------------------------------------------


def test_a_partial_summary_is_never_complete(w):
    c, facts = make(w), Facts()
    _, plan_ref = stopped_run(w, c, facts)
    ((_, value),) = summaries(w, plan_ref)
    assert value["partial"] is True
    assert calibration.complete_summaries(w.m.d.store, w.scope) == []
    assert not calibration.summary_complete(value, "done", value["plan_ref"])  # marked partial
    unmarked = {k: v for k, v in value.items() if k != "partial"}  # a summary from before D-128
    assert not calibration.summary_complete(unmarked, "stopped", value["plan_ref"])
    assert calibration.summary_complete(unmarked, "done", value["plan_ref"])


# --- 3. accounting: ended runs' reservations settle from the dispatch journal -------------------


def seed_run(store, scope, run_id, state, *, dispatches=1, tokens=20_000_000):
    with store.tx() as db:
        store.cas(db, scope, "run", run_id, 0, state, {"record": {"usage": {"status": "unknown"}}})
        db.execute(
            "INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,NULL)",
            (*scope.keys(), "goal-" + run_id, run_id, tokens, None, 0.0, 1800, "active"),
        )
        for i in range(dispatches):
            db.execute("INSERT INTO worker_dispatch VALUES(?,?,?,?,?,?,?,NULL)",
                       (*scope.keys(), f"dispatch-{run_id}-{i}", run_id, "worker", "sha256:x",
                        "acknowledged"))  # fmt: skip


def journal_of(dispatch_id, *, state="failed", stopped=True, usage=True):
    # the shape of dispatch-b6bd165e's journal (a failed Claude run, usage estimated)
    usage_value = {"cost_microunits": 380872, "currency": "USD", "input_tokens": 519133,
                   "output_tokens": 8525, "source_ref": None, "status": "estimated"}  # fmt: skip
    value = {"dispatch_id": dispatch_id, "state": state, "process_stopped": stopped,
             "usage": usage_value}  # fmt: skip
    if not usage:
        value["usage"] = {"input_tokens": None, "output_tokens": None, "status": "unknown"}
    return value


def test_ended_runs_settle_from_their_journal_usage_and_nothing_else_moves(w):
    store, scope = w.m.d.store, w.scope
    seed_run(store, scope, "run-failed", "failed")
    seed_run(store, scope, "run-nousage", "cancelled")
    seed_run(store, scope, "run-running", "running")
    seed_run(store, scope, "run-alive", "failed")
    seed_run(store, scope, "run-followup", "failed")
    seed_run(store, scope, "run-two", "failed", dispatches=2)
    seed_run(store, scope, "run-nojournal", "failed")
    journals = {
        "dispatch-run-failed-0": journal_of("dispatch-run-failed-0"),
        "dispatch-run-nousage-0": journal_of("dispatch-run-nousage-0", state="cancelled",
                                             usage=False),
        "dispatch-run-running-0": journal_of("dispatch-run-running-0", state="running",
                                             stopped=False),
        "dispatch-run-alive-0": journal_of("dispatch-run-alive-0", stopped=False),
        "dispatch-run-followup-0": journal_of("dispatch-run-followup-0"),
        "dispatch-run-followup-0-f1": journal_of("dispatch-run-followup-0-f1"),
        "dispatch-run-two-0": journal_of("dispatch-run-two-0"),
    }  # fmt: skip
    out = BudgetService(store).settle_ended(scope, journals.get)
    assert out["settled"] == [{"run_id": "run-failed", "dispatch_id": "dispatch-run-failed-0",
                               "tokens": 519133 + 8525, "status": "estimated"}]  # fmt: skip
    assert {s["run_id"]: s["reason"] for s in out["skipped"]} == {
        "run-nousage": "no_usage", "run-alive": "process_not_stopped",
        "run-followup": "multi_turn", "run-two": "dispatches:2", "run-nojournal": "no_journal",
    }  # fmt: skip
    seeded = ("run-failed", "run-nousage", "run-running", "run-alive", "run-followup", "run-two",
              "run-nojournal")  # fmt: skip
    rows = {r["run_id"]: dict(r) for r in store.conn.execute("SELECT * FROM reservations")}
    assert rows["run-failed"]["status"] == "estimated" and rows["run-failed"]["tokens"] == 527658
    assert all(rows[r]["status"] == "active" for r in seeded if r != "run-failed")
    # once settled it is not settled again
    assert BudgetService(store).settle_ended(scope, journals.get)["settled"] == []


def test_the_operator_command_reads_the_driver_journal_files(w, tmp_path):
    store, scope = w.m.d.store, w.scope
    seed_run(store, scope, "run-failed", "failed")
    root = tmp_path / "workspaces"
    (root / "journal").mkdir(parents=True)
    path = root / "journal" / "dispatch-run-failed-0.json"
    path.write_bytes(canonical(journal_of("dispatch-run-failed-0")))
    config = SimpleNamespace(workspace_root=str(root))
    dep = SimpleNamespace(store=store, scope=scope, config=config, local=Path)
    out = meta_ops.settle_ended_reservations(dep)
    assert [s["run_id"] for s in out["settled"]] == ["run-failed"]
    # probing the follow-up ids never created a journal lock for them
    assert not any(
        p.name.startswith("dispatch-run-failed-0-") for p in (root / "journal").iterdir()
    )


# --- the operator commands on the product rig (LocalMetaOps) ------------------------------------


@pytest.fixture
def stage_world(tmp_path):
    with build_world(tmp_path) as world:
        executor = world.executor
        executor.facts = lambda composition, case: {
            "harness_sha": executor.harness_sha,
            "model_snapshot": executor.snapshot,
            "corpus_version": executor.corpus_version,
        }
        yield world


def ops_calibrate(ops, **over):
    args = {"max_repeats": 2, "max_trials": 200, "parallel": 1, "max_tokens": 1_000_000,
            "max_wall_seconds": 3600}  # fmt: skip
    args.update(over)
    return ops.calibrate([STAGE_CELL], **args)


def test_calibrate_import_from_announces_imported_and_new_before_it_runs(stage_world):
    w = stage_world
    first = ops_calibrate(w.ops)
    assert first["state"] == "done" and first["imports"] is None
    w.executor.calls.clear()
    announced = []
    second = ops_calibrate(w.ops, import_from=[first["plan_ref"]["id"]], announce=announced.append)
    (shown,) = announced
    assert shown["plan_id"] == second["plan_ref"]["id"]
    imports = shown["imports"]
    assert imports["imported"] == 32 and imports["imported_cases"] == 32  # 14 + 18 cases
    assert imports["new_round_one"] == 0 and imports["drift_reruns"] == 4
    assert imports["window_hours"] == 72 and imports["plan_id"] == second["plan_ref"]["id"]
    assert second["state"] == "done" and second["imports"] == imports
    # only the drift sample ran (round 1 came from the first plan)
    assert len(w.executor.calls) == 4 and {rep for _, _, rep in w.executor.calls} == {1}
    plan = w.store.get(w.scope, "calibration-plan", second["plan_ref"])
    # every task saturated after one run: no repeat 1, so no measured A/A discordance
    assert plan["import_policy"]["drift_reference"] == {STAGE_CELL: None}
    # a stage plan reads the newest complete summary: the importing plan's
    pid = w.propose("a2")
    assert w.ops.stage_runner(pid, cell_id=STAGE_CELL).summary_ref == second["summary_ref"]


def test_calibration_resume_finishes_a_stopped_plan_and_the_stage_plan_waits_for_it(stage_world):
    w = stage_world
    store, scope = w.store, w.scope
    calls = {"n": 0}

    def kill_after_three(role, case_id, repeat):
        calls["n"] += 1
        if calls["n"] == 3:  # the operator's kill switch stops admission
            with store.tx() as db:
                store.cas(db, scope, "runtime-control", "kill", 0, "on", {"enabled": True})
        return True

    w.executor.outcome = kill_after_three
    first = ops_calibrate(w.ops)
    assert first["state"] == "stopped" and first["stop_reason"] == "KILL_SWITCH"
    pid = w.propose("a2")
    # the partial summary is never a stage plan's calibration (the world's own one stays)
    assert w.ops.stage_runner(pid, cell_id=STAGE_CELL).summary_ref != first["summary_ref"]
    with store.tx() as db:
        current = store.head(scope, "runtime-control", "kill", db=db)
        store.cas(db, scope, "runtime-control", "kill", current["row_version"], "off",
                  {"enabled": False})  # fmt: skip
    w.executor.outcome = lambda role, case_id, repeat: True
    w.executor.calls.clear()
    resumed = w.ops.calibration_resume(first["plan_ref"]["id"])
    assert resumed["state"] == "done" and resumed["trials"] == 32
    assert len(w.executor.calls) == 32 - 3
    assert w.ops.stage_runner(pid, cell_id=STAGE_CELL).summary_ref == resumed["summary_ref"]
    assert resumed["summary_ref"]["revision"] == 2
