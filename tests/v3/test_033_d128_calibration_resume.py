"""D-128 (operator decision 2026-10-14): calibration overrun, resume, imports and accounting.

interfaces.md "Clarification: Calibration Resume (D-128)". Real: store, budget ledgers,
CalibrationService, EvaluationService, BudgetService, pricing. Stand-ins: the scripted executors
of the S2 tests, whose receipts here also carry the reuse-key facts the trial executor records
(``trial_code_digest``, ``model_snapshot``, ``corpus_version``; ``harness_sha`` as provenance),
and a ``facts`` function standing in for ``LocalTrialExecutor.facts``.
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

from amplai_foundry.evaluation import calibration, pricing, versions
from amplai_foundry.evaluation.calibration import CalibrationService
from amplai_foundry.evaluation.service import NOT_RUN_EVIDENCE
from amplai_foundry.meta_harness.budget import EvolutionBudget
from amplai_foundry.runtime.budgets.service import BudgetService
from amplai_foundry.runtime.contracts.identity import canonical, digest
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import meta_ops

TRIAL_TOKENS = 10  # the scripted trials report 5 + 5 tokens
SHA = "0ee15f080276a21d8fa03952348efa67bb4f9aa2"
CODE = "sha256:" + "a" * 64  # the trial code digest (review P1-1)
SNAPSHOT = {"provider_model_id": "claude-sonnet-5", "driver_version": "2.1.292",
            "image": "localhost:5000/amplai-worker@sha256:" + "b" * 64}  # fmt: skip


@pytest.fixture
def w(tmp_path, monkeypatch):
    # the S2 world's root wall budget is 600 s; a resume needs one trial deadline left (1800 s
    # in production, product.Budget), here 60 s
    monkeypatch.setattr(calibration, "TRIAL_WALL_SECONDS", 60)
    with make_world(tmp_path) as world:
        yield world


class Facts:
    """What a trial would run on now (``LocalTrialExecutor.facts``); tests change ``value``."""

    def __init__(self) -> None:
        self.value: dict[str, Any] = {
            "harness_sha": SHA, "trial_code_digest": CODE, "model_snapshot": dict(SNAPSHOT),
            "corpus_version": "2.1.0",
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
    [("trial_code_digest", "sha256:" + "1" * 64),
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


def test_a_new_harness_sha_alone_never_blocks_a_resume(w):
    # review P1-1: harness_sha (the whole checkout) is provenance; the trial code digest decides
    c, facts = make(w), Facts()
    svc, plan_ref = stopped_run(w, c, facts)
    facts.value["harness_sha"] = "2" * 40
    svc.resume(w.m.reviewer, plan_ref, WithFacts(w, Scripted(w, c.roles, outcome), facts))
    assert head(w, plan_ref)["state"] == "done"


def test_resume_refuses_an_unknown_trial_code_and_another_evaluator_version(w):
    c, facts = make(w), Facts()
    facts.value["trial_code_digest"] = None  # a receipt without the trial code is never reused
    svc, plan_ref = stopped_run(w, c, facts)
    caught = hold("CALIBRATION_RESUME_CHANGED", svc.resume, w.m.reviewer, plan_ref,
                  WithFacts(w, Scripted(w, c.roles, outcome), facts))  # fmt: skip
    assert {d["field"] for d in caught.details["differences"]} == {"trial_code_digest"}
    facts.value["trial_code_digest"] = CODE
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

    # review P1-2: the wall budget counts active run time; time between stop and resume does not
    c3 = make(w)
    _, stopped = stopped_run(w, c3, facts)
    store = w.m.d.store
    root = "calibration:" + stopped["id"]
    budget = store.head(w.scope, "meta-budget", root)["data"]
    assert budget["segment_started_epoch"] is None and 0 <= budget["wall_used_seconds"] < 60
    clock = store.clock
    monkeypatch.setattr(store, "clock", lambda: clock() + BUDGET["max_wall_seconds"] * 10)
    second = WithFacts(w, Scripted(w, c3.roles, outcome), facts)
    svc.resume(w.m.reviewer, stopped, second)  # ten wall budgets later: still within it
    assert head(w, stopped)["state"] == "done"
    budget = store.head(w.scope, "meta-budget", root)["data"]
    assert budget["wall_used_seconds"] < BUDGET["max_wall_seconds"]

    # a root whose active time is spent refuses the resume
    c4 = make(w)
    _, spent = stopped_run(w, c4, facts)
    root = "calibration:" + spent["id"]
    with store.tx() as db:
        h = store.head(w.scope, "meta-budget", root, db=db)
        store.cas(
            db,
            w.scope,
            "meta-budget",
            root,
            h["row_version"],
            "active",
            {**h["data"], "wall_used_seconds": float(BUDGET["max_wall_seconds"])},
        )
    hold("META_WALL_BUDGET", svc.resume, w.m.reviewer, spent, Scripted(w, c4.roles))


def test_a_resume_long_after_the_stop_is_refused_as_stale(w):
    # review P1-2: kept trials older than the window would pool with new ones: refused, so the
    # operator imports them into a new plan (with its window and drift check) instead
    c, facts = make(w), Facts()
    svc, plan_ref = stopped_run(w, c, facts)
    store = w.m.d.store
    old = (datetime.now(UTC) - timedelta(hours=73)).isoformat().replace("+00:00", "Z")
    with store.tx() as db:
        h = store.head(w.scope, "calibration-run", plan_ref["id"], db=db)
        store.cas(db, w.scope, "calibration-run", plan_ref["id"], h["row_version"], "stopped",
                  {**h["data"], "stopped_at": old})  # fmt: skip
    caught = hold("CALIBRATION_RESUME_STALE", svc.resume, w.m.reviewer, plan_ref,
                  Scripted(w, c.roles))  # fmt: skip
    assert caught.details["window_hours"] == 72 and caught.details["gap_hours"] >= 73
    svc.resume(w.m.reviewer, plan_ref, WithFacts(w, Scripted(w, c.roles, outcome), facts),
               window_hours=80)  # fmt: skip
    assert head(w, plan_ref)["state"] == "done"


def test_a_resume_needs_one_trial_deadline_of_wall_time_left(w):
    # failure review P2-1: 30 s left against a trial deadline of 60 s (1800 s in production)
    c, facts = make(w), Facts()
    svc, plan_ref = stopped_run(w, c, facts)
    store, root = w.m.d.store, "calibration:" + plan_ref["id"]
    with store.tx() as db:
        h = store.head(w.scope, "meta-budget", root, db=db)
        used = BUDGET["max_wall_seconds"] - 30.0
        store.cas(db, w.scope, "meta-budget", root, h["row_version"], "active",
                  {**h["data"], "wall_used_seconds": used})  # fmt: skip
    caught = hold("META_WALL_BUDGET", svc.resume, w.m.reviewer, plan_ref, Scripted(w, c.roles))
    assert caught.details["trial_wall_seconds"] == 60


def lost_owner(w, svc, c, *, reserved):
    """A run whose owner died 500 s into its segment, its last settled trial 400 s ago; with
    ``reserved`` one allocation is still reserved."""
    store = w.m.d.store
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c))
    root = "calibration:" + plan_ref["id"]
    old = (datetime.now(UTC) - timedelta(seconds=400)).isoformat().replace("+00:00", "Z")
    with store.tx() as db:
        h = store.head(w.scope, "calibration-run", plan_ref["id"], db=db)
        store.cas(db, w.scope, "calibration-run", plan_ref["id"], h["row_version"], "running",
                  {**h["data"], "state": "running", "owner_epoch": store.epoch - 1})  # fmt: skip
        b = store.head(w.scope, "meta-budget", root, db=db)
        allocations = {"done": {"status": "settled", "tokens": 10, "cost": 0, "overrun": False,
                                "issued_at": old, "settled_at": old}}  # fmt: skip
        if reserved:
            allocations["left"] = {"status": "reserved", "tokens": 10, "cost": 0,
                                   "issued_at": old}  # fmt: skip
        store.cas(db, w.scope, "meta-budget", root, b["row_version"], "active",
                  {**b["data"], "segment_started_epoch": store.clock() - 500,
                   "allocations": allocations})  # fmt: skip
    return plan_ref, root


@pytest.mark.parametrize(("reserved", "low", "high"), [(False, 90, 110), (True, 490, 520)])
def test_recover_closes_a_lost_segment_at_now_while_a_trial_is_still_reserved(
    w, reserved, low, high
):
    # failure review P2-1: a reserved trial may have run until now (conservative); otherwise the
    # segment ends at the root's last recorded activity
    c = make(w)
    svc = make_service(w, Facts())
    plan_ref, root = lost_owner(w, svc, c, reserved=reserved)
    svc.recover(w.m.reviewer, plan_ref)
    budget = w.m.d.store.head(w.scope, "meta-budget", root)["data"]
    assert budget["segment_started_epoch"] is None
    assert low <= budget["wall_used_seconds"] <= high


def test_admission_reserves_the_largest_raw_trial_seen_so_the_budget_stops_a_wave_earlier(w):
    # failure review P2-4, the probe: max 10M, parallel 4, ceiling 1M, every trial 4.7M raw.
    # The first wave (4 x 1M reserved) overshoots by itself; after the first 4.7M settles the
    # next admission reserves 4.7M, so nothing beyond the first wave is admitted
    c = make(w)
    svc = make_service(w, Facts(), ceiling=1_000_000)
    budget = {**BUDGET, "max_tokens": 10_000_000, "max_parallel_works": 4}
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c, budget=budget, max_parallel=4))
    executor = Scripted(w, c.roles, outcome, tokens=(4_700_000, 0))
    executor.prime(c.cases, 1)
    svc.run(w.m.reviewer, plan_ref, executor, parallel=4)
    h = head(w, plan_ref)
    assert h["data"]["stop_reason"] == "META_TOKEN_BUDGET" and len(h["data"]["trial_refs"]) == 4
    ledger = allocations(w, plan_ref).values()
    assert all(a["token_ceiling"] == 1_000_000 and a["tokens"] == 4_700_000 for a in ledger)


def test_resume_keeps_the_original_qualification_unless_requalified(w):
    # failure review P3: another per-trial ceiling (or qualification) holds; --requalify records it
    c, facts = make(w), Facts()
    _, plan_ref = stopped_run(w, c, facts)
    other = make_service(w, facts, ceiling=TRIAL_TOKENS * 2)
    second = WithFacts(w, Scripted(w, c.roles, outcome, tokens=(5, 5)), facts)
    caught = hold("CALIBRATION_RESUME_QUALIFICATION", other.resume, w.m.reviewer, plan_ref, second)
    assert caught.details["was"]["max_trial_tokens"] == TRIAL_TOKENS
    assert caught.details["now"]["max_trial_tokens"] == TRIAL_TOKENS * 2
    other.resume(w.m.reviewer, plan_ref, second, requalify=True)
    h = head(w, plan_ref)
    assert h["state"] == "done" and h["data"]["max_trial_tokens"] == TRIAL_TOKENS * 2
    assert h["data"]["resumes"][-1]["requalified"]["was"]["max_trial_tokens"] == TRIAL_TOKENS


def test_imports_are_stored_in_the_transaction_that_starts_the_run(w, monkeypatch):
    # failure review P3: a crash while storing the imports leaves the plan frozen, never running
    # without them
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    plan, _ = importing_plan(w, c, svc, [source])
    plan_ref = svc.freeze(w.m.reviewer, plan)

    def crash(*args, **kwargs):
        raise RuntimeError("process died while importing")

    monkeypatch.setattr(svc.budgets, "record_import", crash)
    with pytest.raises(RuntimeError):
        svc.run(w.m.reviewer, plan_ref, Scripted(w, c.roles, steady))
    h = head(w, plan_ref)
    assert h["state"] == "frozen" and h["data"]["trial_refs"] == []
    assert not [r for r, v in w.m.d.store.list_objects(w.scope, "calibration-trial")
                if v.get("calibration_plan_ref") == plan_ref]  # fmt: skip
    monkeypatch.undo()
    monkeypatch.setattr(calibration, "TRIAL_WALL_SECONDS", 60)
    svc.run(w.m.reviewer, plan_ref, WithFacts(w, Scripted(w, c.roles, steady), facts))
    assert sum("imported_from" in t for t in trials(w, plan_ref)) == 12


def test_resume_without_executor_facts_is_refused(w):
    c, facts = make(w), Facts()
    _, plan_ref = stopped_run(w, c, facts)
    blind = make_service(w, None)
    hold("CALIBRATION_FACTS_UNAVAILABLE", blind.resume, w.m.reviewer, plan_ref,
         Scripted(w, c.roles, outcome))  # fmt: skip


# --- 2b, 2c. imports into a new plan -----------------------------------------------------------


def import_policy(cells=CELLS, **over):
    value = {"window_hours": 72, "drift_share": 0.1, "drift_min_per_cell": 8,
             "drift_reference": {cell: None for cell in cells},
             "drift_reference_basis": {cell: "none" for cell in cells},
             "expected_imported": 0}  # fmt: skip
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
    plan["import_policy"]["expected_imported"] = found["imported"]
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
    # round 1 came from the source; only the drift sample (min 8, here all 6 stable tasks per
    # cell) ran at repeat 1
    assert not any(rep == 0 for _, _, rep in executor.calls)
    for cell in CELLS:
        assert h["data"]["drift_check"][cell]["status"] == "pass"
        assert len(h["data"]["drift_check"][cell]["sample"]) == 6
        assert h["data"]["drift_check"][cell]["unstable_excluded"] == 0
    # review P3: the imported repeat 0 against the drift re-run is no A/A pair
    _, summary = summaries(w, plan_ref)[-1]
    assert all(summary["cells"][cell]["aa_discordance"] is None for cell in CELLS)
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
    plan["import_policy"]["expected_imported"] = len(source_refs)
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
    # review P2-c: manifest variants are never imported; they run fresh in the imported slots
    v = make(w, variants=True)
    _, source_v = source_run(w, v, facts, svc=svc)
    plan, found = importing_plan(w, v, svc, [source_v])
    assert found["imported"] == 12  # the v1 trials of both cells
    assert "variant_not_imported" in {e["reason"] for e in found["excluded"]}
    executor = WithFacts(w, Scripted(w, v.roles, steady, tokens=(5, 5)), facts)
    plan_ref = svc.freeze(w.m.reviewer, plan)
    svc.run(w.m.reviewer, plan_ref, executor)
    variant_r0 = {cid for role, cid, rep in executor.calls if role == "cell-a-var" and rep == 0}
    assert variant_r0 == {x["case_id"] for x in v.cases}
    # a plan with a v1 the source never ran imports nothing for it (no case is complete)
    other = {**v.refs, "cell-a": [w.composition("new")]}
    other_plan = plan_for(w, v, composition_refs=other)
    other_plan["import_policy"] = import_policy()
    found = svc.eligible_imports(w.scope, other_plan, [source_v])
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
    assert drift["cell-a"]["status"] == "fail" and drift["cell-a"]["flips"] == 6
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
    assert calibration.drift_verdict(8, [False] * 3, None) == ("inconclusive", None)
    assert calibration.drift_verdict(4, [], 0.2) == ("inconclusive", None)
    # failure review P2-2: one to three agreeing pairs are no evidence, whatever the sample
    for pairs in (1, 2, 3):
        assert calibration.drift_verdict(pairs, [False] * pairs, None) == ("inconclusive", None)


def test_import_plans_are_validated_and_bound_by_the_approval(w):
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    plan, _ = importing_plan(w, c, svc, [source])
    for broken in (
        {k: v for k, v in plan.items() if k != "import_policy"},
        {**plan, "import_policy": {**plan["import_policy"], "window_hours": 0}},
        {**plan, "import_policy": {**plan["import_policy"], "drift_reference": {}}},
        # review P2-b: the approved count must be the count of listed trials
        {**plan, "import_policy": {**plan["import_policy"], "expected_imported": 11}},
        {
            **plan,
            "import_policy": {
                **plan["import_policy"],
                "drift_reference_basis": {c_: "guess" for c_ in CELLS},
            },
        },
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


def flipping_source(w, c, facts):
    """A source run where val-01 alternates between repeats (``outcome``): unstable there."""
    flipping = WithFacts(w, Scripted(w, c.roles, outcome, tokens=(5, 5)), facts)
    return source_run(w, c, facts, executor=flipping)


def test_the_drift_sample_skips_tasks_that_flipped_in_their_source_run(w):
    # review P2-c: a flip of val-01 in the new run would say nothing about drift; with a floor of
    # 4 the five stable tasks fill the sample and val-01 is never drawn
    c, facts = make(w), Facts()
    svc, source = flipping_source(w, c, facts)
    plan, found = importing_plan(w, c, svc, [source],
                                 policy_value=import_policy(drift_min_per_cell=4))  # fmt: skip
    assert {s_["sample"] for s_ in found["drift_samples"].values()} == {4}
    plan_ref = svc.freeze(w.m.reviewer, plan)
    svc.run(w.m.reviewer, plan_ref, WithFacts(w, Scripted(w, c.roles, outcome), facts))
    drift = head(w, plan_ref)["data"]["drift_check"]
    for cell in CELLS:
        assert "val-01" not in drift[cell]["sample"] and len(drift[cell]["sample"]) == 4
        assert drift[cell]["unstable_excluded"] == 1 and drift[cell]["topped_up"] == []


def test_a_sample_below_its_floor_is_topped_up_from_unstable_non_flaky_tasks(w):
    # failure review P2-2: floor = min(6 imported, 8) = 6, five stable: val-01 tops it up
    c, facts = make(w), Facts()
    svc, source = flipping_source(w, c, facts)
    plan, found = importing_plan(w, c, svc, [source])
    for cell in CELLS:
        expected = {"sample": 6, "floor": 6, "topped_up": 1, "unstable_excluded": 1,
                    "status": "pending"}  # fmt: skip
        assert found["drift_samples"][cell] == expected
    plan_ref = svc.freeze(w.m.reviewer, plan)
    svc.run(w.m.reviewer, plan_ref, WithFacts(w, Scripted(w, c.roles, steady), facts))
    drift = head(w, plan_ref)["data"]["drift_check"]
    assert all(drift[cell]["topped_up"] == ["val-01"] for cell in CELLS)


def test_a_sample_that_cannot_reach_its_floor_makes_the_cell_inconclusive_before_any_run(w):
    # failure review P2-2: the source summary classes val-01 flaky_grading for cell-a, so cell-a
    # cannot top up to its floor: inconclusive, its imports dropped and run fresh
    c, facts = make(w), Facts()
    svc, source = flipping_source(w, c, facts)
    store = w.m.d.store
    (ref, value), = [
        (r, v) for r, v in store.list_objects(w.scope, "calibration-summary")
        if r["id"] == "calsum-" + source["id"]
    ]  # fmt: skip
    cells_ = json.loads(json.dumps(value["cells"]))
    cells_["cell-a"]["tasks"]["val-01"]["class"] = "flaky_grading"
    with store.tx() as db:
        store.put(db, w.scope, "calibration-summary", ref["id"], ref["revision"] + 1,
                  {**value, "cells": cells_})  # fmt: skip
    plan, found = importing_plan(w, c, svc, [source])
    assert found["drift_samples"]["cell-a"]["status"] == "inconclusive"
    assert found["drift_samples"]["cell-b"]["status"] == "pending"
    plan_ref = svc.freeze(w.m.reviewer, plan)
    executor = WithFacts(w, Scripted(w, c.roles, steady, tokens=(5, 5)), facts)
    svc.run(w.m.reviewer, plan_ref, executor)
    drift = head(w, plan_ref)["data"]["drift_check"]
    assert drift["cell-a"]["status"] == "inconclusive"
    assert drift["cell-a"]["reason"] == "sample_below_floor"
    assert len(drift["cell-a"]["dropped"]) == 6
    assert {cid for role, cid, rep in executor.calls if role == "cell-a" and rep == 0} == {
        x["case_id"] for x in c.cases
    }
    assert not any("imported_from" in t and t["cell_id"] == "cell-a" for t in trials(w, plan_ref))


def test_a_resumed_plan_with_imports_finishes_its_drift_check(w):
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    plan, _ = importing_plan(w, c, svc, [source])
    plan_ref = svc.freeze(w.m.reviewer, plan)
    # every drift re-run reports no usage: four in a row stop the run inside the drift check
    inner = UnknownUsage(w, c.roles, steady, tokens=(5, 5), unknown=lambda *a: True)
    svc.run(w.m.reviewer, plan_ref, WithFacts(w, inner, facts))
    h = head(w, plan_ref)
    assert h["data"]["stop_reason"] == "provider_failures"
    assert {e["status"] for e in h["data"]["drift_check"].values()} == {"pending"}
    second = WithFacts(w, Scripted(w, c.roles, steady, tokens=(5, 5)), facts)
    svc.resume(w.m.reviewer, plan_ref, second)
    h = head(w, plan_ref)
    assert h["state"] == "done"
    assert {e["status"] for e in h["data"]["drift_check"].values()} == {"pass"}
    assert sum("imported_from" in t for t in trials(w, plan_ref)) == 12  # still pooled


def evaluator_version(w, version, compatible=None):
    requal = w.m.d.put(
        "evaluator-requalification", "requal-" + version,
        {"schema": "amplai.evaluator-requalification.v1", "scope": w.scope.wire(),
         "evaluator_version": version, "store": "test", "reports": [], "all_equal": True},
    )  # fmt: skip
    return versions.write_version(
        w.m.d.store, w.m.reviewer, corpus_ref=w.ev_ref and w.m.d.store.get(
            w.scope, "evaluator-version", w.ev_ref)["corpus_ref"],
        requalification_ref=requal, version=version, trial_compatible_with=compatible,
    )  # fmt: skip


def test_trial_compatible_with_admits_listed_versions_only_and_is_not_transitive(w):
    # review P2-a: eval-3 declares eval-2's trials compatible; eval-4 declares nothing
    c, facts = make(w), Facts()
    svc, plan_ref = stopped_run(w, c, facts)  # recorded under eval-2
    was = head(w, plan_ref)["data"]["evaluator_version_ref"]
    eval3 = evaluator_version(w, "eval-3", ["eval-2"])
    store = w.m.d.store
    assert versions.current_version_ref(store, w.scope) == eval3
    assert calibration.evaluator_compatible(store, w.scope, was, eval3)
    svc.resume(w.m.reviewer, plan_ref, WithFacts(w, Scripted(w, c.roles, outcome), facts))
    assert head(w, plan_ref)["state"] == "done"
    eval4 = evaluator_version(w, "eval-4")
    assert not calibration.evaluator_compatible(store, w.scope, was, eval4)
    assert not calibration.evaluator_compatible(store, w.scope, eval3, eval4)
    assert not calibration.evaluator_compatible(store, w.scope, None, eval4)
    c2 = make(w)
    _, other = stopped_run(w, c2, facts)  # recorded under eval-4
    with store.tx() as db:
        h = store.head(w.scope, "calibration-run", other["id"], db=db)
        store.cas(db, w.scope, "calibration-run", other["id"], h["row_version"], "stopped",
                  {**h["data"], "evaluator_version_ref": was})  # fmt: skip
    caught = hold("CALIBRATION_RESUME_CHANGED", svc.resume, w.m.reviewer, other,
                  Scripted(w, c2.roles))  # fmt: skip
    assert {d["field"] for d in caught.details["differences"]} == {"evaluator_version_ref"}
    # the record shape: distinct lower eval-N names only
    with pytest.raises(RuntimeFault):
        evaluator_version(w, "eval-5", ["eval-6"])
    with pytest.raises(RuntimeFault):
        evaluator_version(w, "eval-5", ["eval-2", "eval-2"])


def test_a_weigher_failure_of_any_kind_judges_the_raw_tokens(w):
    c = make(w)

    def broken(trial):
        raise ZeroDivisionError("price row with a zero input price")

    svc = make_service(w, weigher=broken)
    plan_ref = svc.freeze(w.m.reviewer, plan_for(w, c))
    svc.run(w.m.reviewer, plan_ref, Scripted(w, c.roles, outcome, tokens=(5, 5)))
    judged = trials(w, plan_ref)[0]["ceiling_tokens"]
    assert judged == {"tokens": 10, "basis": "raw", "price_table_id": None,
                      "reason": "weigher_error:ZeroDivisionError"}  # fmt: skip
    assert head(w, plan_ref)["state"] == "done"


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
            "trial_code_digest": executor.trial_code_digest,
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
    source = [first["plan_ref"]["id"]]
    plans = len(w.objects("calibration-plan"))
    # review P2-b: a dry run previews and approves nothing
    preview = ops_calibrate(w.ops, import_from=source, dry_run=True)
    assert preview["dry_run"] is True and len(w.objects("calibration-plan")) == plans
    imports = preview["imports"]
    assert imports["imported"] == 32 and imports["imported_cases"] == 32  # 14 + 18 cases
    assert imports["new_round_one"] == 0 and imports["drift_reruns_at_most"] == 8
    assert imports["per_cell"][STAGE_CELL]["imported"] == 32 and imports["window_hours"] == 72
    # every task saturated after one run: no source A/A, so the world's complete summary's
    assert imports["per_cell"][STAGE_CELL]["drift_reference"] == 0.2
    assert imports["per_cell"][STAGE_CELL]["drift_reference_basis"] == "deployment_aa"
    # failure review P2-2: the sample the run will draw, and the note on dropped imports
    assert imports["per_cell"][STAGE_CELL]["drift_sample"] == 8
    assert imports["per_cell"][STAGE_CELL]["drift_sample_floor"] == 8
    assert imports["per_cell"][STAGE_CELL]["drift_status_before_run"] == "pending"
    assert "run fresh" in imports["note"]
    assert w.executor.calls == []
    fault("CALIBRATION_PLAN", ops_calibrate, w.ops, import_from=source)  # no expectation
    caught = hold("CALIBRATION_IMPORT_EXPECTED", ops_calibrate, w.ops, import_from=source,
                  expect_imported=31)  # fmt: skip
    assert caught.details["found"] == 32 and len(w.objects("calibration-plan")) == plans
    announced = []
    second = ops_calibrate(w.ops, import_from=source, expect_imported=32,
                           announce=announced.append)  # fmt: skip
    (shown,) = announced
    assert shown["plan_id"] == second["plan_ref"]["id"]
    assert second["state"] == "done" and second["imports"] == shown["imports"]
    # only the drift sample ran (round 1 came from the first plan)
    assert len(w.executor.calls) == 8 and {rep for _, _, rep in w.executor.calls} == {1}
    plan = w.store.get(w.scope, "calibration-plan", second["plan_ref"])
    assert plan["import_policy"]["expected_imported"] == 32
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
    # failure review P2-3: the first process dies before it writes trial-metrics
    record_metrics = w.ops._record_metrics
    w.ops._record_metrics = lambda refs: None
    first = ops_calibrate(w.ops)
    w.ops._record_metrics = record_metrics
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
    # failure review P3: a newer qualification is not taken silently; the run's own is pinned
    original = store.head(scope, "calibration-run", first["plan_ref"]["id"])["data"]
    w.ops.qualify_executor("another basis", ["another evidence"], 123)
    resumed = w.ops.calibration_resume(first["plan_ref"]["id"])
    assert resumed["state"] == "done" and resumed["trials"] == 32
    assert len(w.executor.calls) == 32 - 3
    after = store.head(scope, "calibration-run", first["plan_ref"]["id"])["data"]
    assert after["qualification_ref"] == original["qualification_ref"]
    assert "requalified" not in after["resumes"][-1]
    recorded = {r["id"] for r, _ in store.list_objects(scope, "trial-metrics")}
    assert all("tm-" + r["id"] in recorded for r in after["trial_refs"])
    assert w.ops.stage_runner(pid, cell_id=STAGE_CELL).summary_ref == resumed["summary_ref"]
    assert resumed["summary_ref"]["revision"] == 2


def test_an_evaluator_change_may_declare_trial_compatible_versions(w):
    # review P2-a: stored lower versions only, approved with the change (the target)
    from amplai_foundry.evaluation.quality import QualityService

    make(w)  # writes eval-2
    q = QualityService(w.m.d.store, w.scope)
    current = q._latest_version()
    to = {"version": "eval-3", "changes": [{"kind": "analysis_code", "detail": "accounting"}]}
    target = q._target({**to, "trial_compatible_with": ["eval-2"]}, current)
    assert target["trial_compatible_with"] == ["eval-2"]
    assert "trial_compatible_with" not in q._target(to, current)
    for bad in (["eval-9"], ["eval-3"], "eval-2", ["eval-2", "eval-2"]):
        fault("EVALUATOR_CHANGE_TARGET", q._target, {**to, "trial_compatible_with": bad}, current)


# --- regression review (PR #92 at c056093) -------------------------------------------------------


class FakeLedger:
    """A meta-budget head in memory (EvolutionBudget needs head, cas, clock and epoch)."""

    def __init__(self, data):
        self.heads = {"root": {"row_version": 1, "data": data}}
        self.epoch = 1

    def head(self, scope, kind, oid, db=None):
        return self.heads[oid]

    def cas(self, db, scope, kind, oid, version, state, data):
        self.heads[oid] = {"row_version": version + 1, "state": state, "data": data}

    def clock(self):
        return 0.0


def test_rm_a_reservation_while_another_trial_is_still_reserved_holds_the_observed_max():
    # RM: parallel >= 2; one trial settled at 4.7M raw, one still reserved at 1M
    store = FakeLedger({
        "limits": {**BUDGET, "max_tokens": 100_000_000, "max_parallel_works": 4},
        "experiment_refs": [], "started_epoch": 0.0,
        "allocations": {
            "done": {"status": "settled", "tokens": 4_700_000, "cost": 0, "overrun": True},
            "running": {"status": "reserved", "tokens": 1_000_000, "cost": 0},
            "import": {"status": "settled", "tokens": 0, "cost": 0, "imported": {"x": 1}},
        },
    })  # fmt: skip
    ledger = EvolutionBudget(store)  # type: ignore[arg-type]
    ledger.reserve(None, None, "root", "next", tokens=1_000_000, cost=0,
                   prior_overrun_blocks=False, reserve_observed_max=True)  # fmt: skip
    allocation = store.heads["root"]["data"]["allocations"]["next"]
    assert allocation["tokens"] == 4_700_000 and allocation["token_ceiling"] == 1_000_000
    # without the flag (experiments, canary) the reservation stays the ceiling
    ledger.reserve(None, None, "root", "plain", tokens=1_000_000, cost=0,
                   prior_overrun_blocks=False)  # fmt: skip
    assert store.heads["root"]["data"]["allocations"]["plain"]["tokens"] == 1_000_000


def test_dr8_the_verdict_uses_the_wilson_lower_bound_not_the_point_estimate():
    # 1 flip in 8: point estimate 0.125 > 0.05, Wilson 95 % lower bound about 0.022 < 0.05
    verdict, lower = calibration.drift_verdict(8, [True] + [False] * 7, 0.05)
    assert verdict == "pass" and lower is not None and lower < 0.05 < 1 / 8


def test_dr11_a_source_with_repeat_0_and_1_pairs_gives_a_source_aa_reference(w):
    # ``outcome``: val-00 agrees with itself in both cells; val-01 alternates (a: T,F; b: F,T)
    c, facts = make(w), Facts()
    svc, source = flipping_source(w, c, facts)
    plan = plan_for(w, c)
    reference, basis = svc.drift_reference(w.scope, plan, [source])
    assert basis == {cell: "source_aa" for cell in CELLS}
    assert reference == {cell: 0.5 for cell in CELLS}  # 1 flip in 2 tasks with both repeats


def test_dr13_a_drift_check_inconclusive_after_its_run_drops_the_imports(w):
    # most re-runs of cell-a have no graded outcome: fewer than 4 pairs compared
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    plan, _ = importing_plan(w, c, svc, [source])
    plan_ref = svc.freeze(w.m.reviewer, plan)

    def ungraded(role, case_id, repeat):
        if role == "cell-a" and repeat == 1:
            return None
        return steady(role, case_id, repeat)

    svc.run(w.m.reviewer, plan_ref, WithFacts(w, Scripted(w, c.roles, ungraded), facts))
    drift = head(w, plan_ref)["data"]["drift_check"]
    assert drift["cell-a"]["status"] == "inconclusive" and drift["cell-a"]["compared"] == 0
    assert len(drift["cell-a"]["dropped"]) == 6 and drift["cell-b"]["status"] == "pass"
    pooled = trials(w, plan_ref)
    assert not any("imported_from" in t and t["cell_id"] == "cell-a" for t in pooled)


def test_rk5_a_source_under_an_incompatible_evaluator_version_is_a_key_mismatch(w):
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)  # under eval-2
    evaluator_version(w, "eval-3")  # lists nothing compatible
    plan = plan_for(w, c)
    plan["import_policy"] = import_policy()
    found = svc.eligible_imports(w.scope, plan, [source])
    assert found["imported"] == 0
    mismatched = [e for e in found["excluded"] if e["reason"] == "key_mismatch"]
    assert mismatched and all(e["fields"] == ["evaluator_version_ref"] for e in mismatched)
    # with eval-4 listing eval-2, the same trials are eligible again
    evaluator_version(w, "eval-4", ["eval-2"])
    assert svc.eligible_imports(w.scope, plan, [source])["imported"] == 12


def test_rk4_a_source_plan_on_another_corpus_is_excluded(w):
    c, facts = make(w), Facts()
    svc, source = source_run(w, c, facts)
    other = make(w)  # another frozen corpus with the same case ids
    plan = plan_for(w, other, composition_refs=c.refs)
    plan["import_policy"] = import_policy()
    found = svc.eligible_imports(w.scope, plan, [source])
    assert found["imported"] == 0
    mismatched = [e for e in found["excluded"] if e["reason"] == "key_mismatch"]
    assert mismatched and all("corpus_ref" in e["fields"] for e in mismatched)


def test_settle_ended_skips_a_run_without_a_dispatch_with_a_code(w):
    store, scope = w.m.d.store, w.scope
    seed_run(store, scope, "run-nodispatch", "failed", dispatches=0)
    out = BudgetService(store).settle_ended(scope, lambda dispatch_id: None)
    assert {"run_id": "run-nodispatch", "reason": "dispatches:0"} in out["skipped"]
