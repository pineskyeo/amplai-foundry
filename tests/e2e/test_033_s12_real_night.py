"""Work 033 S12: whole nights on a real local meta deployment (clarification after S12, IC-10
mechanics, IC-29, the drift rule, the night length; interfaces.md §8.1, §8.8).

Real: the rc06 local product with `LocalMeta` (real `MetaHarness`, `EvaluationService`,
`LocalMetaApprovals` with `issue`, `issue_nightly`, `issue_standing` and `check` at every trial
guard), a frozen corpus v2 main set and regression set with their task and leak indexes, an
evaluator version, `LocalMetaOps` (IC-29 qualification record, `calibrate --set regression`,
`approve_stage --queue`), `StageRunner` with the nightly approval issuer, `NightlyRunner`,
`LocalNightlyBackend` and the dashboard pages. Stand-in: the trial executor (the S11 scripted
executor: scripted outcomes with receipts of the receipt v2 shape; no driver, docker or network)
and the main-set calibration summary of the S11 world.

Covered: a standing approval → night 1: preflight (IC-29 record, no extra argument), drift on the
regression set against the operator's regression calibration (a small sample inside the 99 %
prediction range does not stop), search through `StageRunner.advance` under derived approvals (a
screening stage records its report and prediction score; a failed screening is recorded and left
for the operator), the focused experiment of the surviving candidate built and queued → the
operator's `approve-stage --queue` (exact digest, nothing runs) → night 2 runs the queued stage and
the ablation under the standing approval and stops at the holdout gate → the dashboard pages pass
`inspect_page`. Negative: no IC-29 record → preflight stop; a standing approval revoked mid-search
→ `NIGHT_STOPPED`; a standing approval revoked during the ablation after a confirmation stops the
night and its trials still count; a Hold out of the ablation keeps the focused and ablation trials
(the kill switch stops the night, another Hold is a finding); the nightly identity cannot run,
queue or select holdout and derives no confirmatory approval.
"""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "v3"))

from test_033_s11_stages import CELL, World, build_world, hold, task_ids

from amplai_foundry.meta_harness import corpus_v2
from amplai_foundry.meta_harness.nightly import (
    LocalNightlyBackend,
    NightlyConfig,
    NightlyRunner,
    nightly_policy,
)
from amplai_foundry.meta_harness.stages import (
    StageRunner,
    queue_head,
    stage_findings,
    standing_issuer,
)
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.meta_local import APPROVAL_KIND, NIGHTLY_ID
from amplai_foundry.runtime.execution.meta_ops import LocalMetaOps
from amplai_foundry.verification.runtime.render_acceptance import inspect_page

DATE, NEXT = "2026-10-01", "2026-10-02"
REGRESSION = 8
SHARES = {"drift": 0.1, "screening_design": 0.0, "search": 0.6, "confirmation": 0.3}
MAX_BUDGET = {
    "max_wall_seconds": 604800, "max_attempts": 10, "max_tokens": 1_000_000,
    "max_cost_microunits": 0, "currency": "USD", "max_parallel_works": 1,
    "max_delegation_depth": 0,
}  # fmt: skip
PLAIN = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "250"}


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def prediction() -> dict[str, Any]:
    return {"improve_task_ids": [], "regress_task_ids": [], "improve_buckets": [],
            "regress_buckets": [], "expected_delta": 0.1, "risk": "low"}  # fmt: skip


class Meta:
    """The S11 world as a meta deployment: a corpus with a regression set loaded and frozen, the
    operator's regression calibration, a standing approval, and a night runner on it."""

    def __init__(self, w: World, tmp_path: Path, *, budget_trials: int = 400) -> None:
        self.w = w
        regression = tuple(
            replace(t, task_id=f"reg-val-{i:02d}", set="regression", split="validation",
                    domain="regression")
            for i, t in enumerate(w.corpus.tasks[:REGRESSION])
        )  # fmt: skip
        self.reg_ids = [t.task_id for t in regression]
        combined = replace(w.corpus, tasks=(*w.corpus.tasks, *regression))
        regression_set = corpus_v2.for_set(combined, "regression")
        corpus_v2.freeze(w.operator, w.store, w.artifacts, regression_set, holdout_use_limit=1)
        w.ops = LocalMetaOps(w.dep, combined, w.executor, driver=CELL)  # type: ignore[arg-type]
        self.ops = w.ops
        # the dashboard writes into <runtime_root>/dashboard of the meta deployment
        self.root = tmp_path / "meta-runtime"
        w.dep.config = SimpleNamespace(runtime_root=str(self.root))  # type: ignore[attr-defined]
        w.dep.local = lambda p: Path(p)  # type: ignore[attr-defined]
        self.bad: set[str] = set()  # candidate compositions whose trials fail
        self.drift_fail: set[str] = set()  # regression tasks the champion fails
        self.hook: Any = None
        w.executor.role_of = self.role_of  # type: ignore[method-assign]
        w.executor.outcome = self.outcome
        start = time.time()
        self.cfg = NightlyConfig(
            budget_trials=budget_trials, shares=dict(SHARES), cells=(CELL,),
            drift_tasks=tuple(self.reg_ids), pilot=False, pilot_nights=0,
            keep_operator_share=0.5, max_parallel=1, stop_at=hhmm(start + 6 * 3600),
        )  # fmt: skip
        policy = nightly_policy(
            self.cfg, budget_trials=budget_trials, valid_from=iso(start - 60),
            valid_until=iso(start + 3 * 86400), max_budget=dict(MAX_BUDGET),
        )  # fmt: skip
        self.standing_ref = w.local.approvals.issue_nightly(w.operator, policy)
        self.runner = NightlyRunner(w.dep, self.cfg)  # type: ignore[arg-type]
        self.backend = LocalNightlyBackend(self.ops, self.runner)
        self.runner._backend = self.backend

    # -- the scripted executor ------------------------------------------------------------------
    def role_of(self, composition: dict[str, Any]) -> str:
        if composition in self.bad_refs():
            return "bad"
        if composition == self.ops.app.compositions[CELL]:
            return "baseline"
        return "candidate"

    def bad_refs(self) -> list[dict[str, Any]]:
        return [self.w.proposal(pid)["candidate_ref"] for pid in self.bad]

    def outcome(self, role: str, case_id: str, repeat: int) -> bool:
        if self.hook is not None:
            self.hook(role, case_id, repeat)
        if role == "bad":
            return False
        return not (role == "baseline" and case_id in self.drift_fail)

    # -- the operator's acts --------------------------------------------------------------------
    def calibrate_regression(self) -> dict[str, Any]:
        return dict(self.ops.calibrate([CELL], max_repeats=1, max_trials=100, parallel=1,
                                       max_tokens=100_000, max_wall_seconds=3600,
                                       corpus_set="regression"))  # fmt: skip

    def candidate(self, kind: str = "a2", *, bad: bool = False) -> str:
        pid = self.w.propose(kind, prediction=prediction())
        self.ops.screen(pid)  # the operator's screen gate (`amplai meta screen`)
        if bad:
            self.bad.add(pid)
        return pid

    # -- reading --------------------------------------------------------------------------------
    def run(self, date: str) -> tuple[dict[str, Any] | None, Hold | None]:
        try:
            return self.runner.run(date), None
        except Hold as held:
            assert held.code == "NIGHT_STOPPED", held.as_dict()
            return None, held

    def night(self, date: str) -> dict[str, Any]:
        return dict(self.w.store.head(self.w.scope, "nightly-run", f"night-{date}")["data"])

    def phases(self, date: str) -> dict[str, str]:
        return {p["phase"]: p["state"] for p in self.night(date)["phases"]}

    def stages(self, pid: str) -> dict[str, str]:
        return {s["stage"]: s["state"] for s in self.ops.stages(pid)["steps"]}

    def approval(self, ref: dict[str, Any]) -> dict[str, Any]:
        return dict(self.w.store.get(self.w.scope, APPROVAL_KIND, ref))

    def experiment_approval(self, experiment_ref: dict[str, Any]) -> dict[str, Any]:
        experiment = self.w.experiment(experiment_ref)
        return self.approval(experiment["approval_ref"])


@pytest.fixture
def meta(tmp_path: Path):  # type: ignore[no-untyped-def]
    with build_world(tmp_path) as world:
        yield Meta(world, tmp_path)


def assert_pages_pass(paths: list[str]) -> None:
    assert paths
    for path in paths:
        report = inspect_page(Path(path))
        assert report["outcome"] == "pass", (path, report["findings"])


# ==================================================================================================
# a whole night, the operator's queue approval, the next night
# ==================================================================================================
def test_a_real_night_searches_through_stage_runner_queues_and_the_next_night_confirms(
    meta: Meta,
) -> None:
    w = meta.w
    baseline = meta.calibrate_regression()  # the operator's drift baseline: 8 of 8
    assert baseline["trials"] == REGRESSION and baseline["state"] == "done"
    good = meta.candidate("a2")
    bad = meta.candidate("a1", bad=True)

    # night 1 (IC-29: no qualification argument; the operator's record qualifies the cell)
    w.local.evaluation.executor_policy = None
    meta.drift_fail = set(meta.reg_ids[:4])  # 4 of 8: outside the Wilson band, inside 99 %
    ref, held = meta.run(DATE)
    assert held is None and ref is not None, meta.night(DATE)
    night = meta.night(DATE)
    assert meta.phases(DATE) == {
        "preflight": "done", "drift": "done", "screening_design": "skipped", "search": "done",
        "confirmation": "skipped", "dreaming": "done", "dashboard": "done",
    }  # fmt: skip
    drift = night["drift"][CELL]
    assert drift["observed"] == {"passes": 4, "runs": REGRESSION}
    assert drift["status"] == "ok" and drift["outside_band"] is False
    assert drift["band"]["wilson_95"][0] > 0.5  # 4/8 is below the calibrated Wilson band
    assert drift["prediction_99"][0] <= 4

    # search: both candidates' screening ran through StageRunner under derived approvals
    assert meta.stages(good)["screening"] == "passed"
    assert meta.stages(good)["focused"] == "waiting_approval"
    screening = w.stage_run(good)["data"]["stages"]["screening"]
    report = w.store.get(w.scope, "eval-report", screening["report_ref"])
    assert len(report["run_refs"]) == 24  # 12 informative development tasks x 2 arms
    approval = meta.experiment_approval(screening["experiment_ref"])
    assert approval["approved_by"]["subject_id"] == NIGHTLY_ID
    assert (
        approval["plan_kind"] == "experiment"
        and approval["standing_ref"]["id"] == (meta.standing_ref["id"])
    )
    score_id = f"predscore-{good}-screening"
    scores = [r for r, _v in w.objects("prediction-score") if r["id"] == score_id]
    assert scores  # the screening stage's prediction score is recorded
    # the failed screening is recorded and left for the operator (never rejected at night)
    assert meta.stages(bad)["screening"] == "failed" and w.state(bad) == "screened"
    findings = stage_findings(w.store, w.scope, bad)["screening"]
    assert any(f.startswith("SCREENING_FAILED: ") for f in findings)
    assert night["trials"] == REGRESSION + 48

    # the surviving candidate's focused experiment is built and queued, not approved or run
    (item,) = night["queued_confirmations"]
    assert item["proposal_id"] == good and item["stage"] == "focused"
    queue = queue_head(w.store, w.scope, good)
    assert queue is not None and queue["state"] == "queued"
    assert queue["data"]["subject_digest"] == digest(queue["data"]["experiment"])
    assert queue["data"]["approval_ref"] is None
    assert not [r for r, v in w.objects("eval-experiment")
                if v["experiment_id"] == queue["data"]["experiment"]["experiment_id"]]  # fmt: skip
    # every approval the night used is a derived exploratory one; none is confirmatory
    derived = [v for _r, v in w.objects(APPROVAL_KIND) if v.get("standing_ref")]
    assert derived and {v["plan_kind"] for v in derived} <= {"experiment", "drift"}

    # the night's dashboard lists the queued confirmation and the failed screening
    pages = [p for p in (meta.root / "dashboard").iterdir() if p.suffix == ".html"]
    assert_pages_pass([str(p) for p in pages])
    approvals = (meta.root / "dashboard" / "approvals.html").read_text()
    assert f"approve-stage {good} --stage focused --queue" in approvals
    assert "screening failed" in approvals and "amplai meta reject" in approvals

    # the operator approves the exact queued digest; nothing runs
    calls = len(w.executor.calls)
    wrong = hold("QUEUE_DIGEST", meta.ops.approve_stage, good, "focused", queue=True,
                 subject_digest="sha256:" + "0" * 64)  # fmt: skip
    assert wrong.details == {"queued": queue["data"]["subject_digest"]}
    step = meta.ops.approve_stage(good, "focused", queue=True,
                                  subject_digest=queue["data"]["subject_digest"])  # fmt: skip
    assert step["state"] == "frozen" and step["queued"] is True
    assert len(w.executor.calls) == calls
    approved = queue_head(w.store, w.scope, good)
    assert approved is not None and approved["state"] == "approved"
    human = meta.approval(approved["data"]["approval_ref"])
    assert human["approved_by"]["kind"] == "human" and human.get("standing_ref") is None
    assert human["subject_digest"] == queue["data"]["subject_digest"]
    hold("META_STATE", meta.ops.approve_stage, good, "focused", queue=True)  # approved once

    # night 2: the confirmation phase runs the approved stage, then the ablation, and stops at
    # the holdout gate (the operator's)
    meta.drift_fail = set()
    w.local.evaluation.executor_policy = None
    ref, held = meta.run(NEXT)
    assert held is None and ref is not None, meta.night(NEXT)
    assert meta.phases(NEXT)["confirmation"] == "done"
    stages = meta.stages(good)
    assert stages["focused"] == "passed"
    assert stages["ablation"] in ("passed", "skipped")
    assert stages["holdout"] == "waiting_approval"
    ran = queue_head(w.store, w.scope, good)
    assert ran is not None and ran["state"] == "ran" and ran["data"]["report_ref"]
    focused = w.stage_run(good)["data"]["stages"]["focused"]
    assert focused["experiment_ref"] == approved["data"]["experiment_ref"]
    assert [r for r, _v in w.objects("prediction-score") if r["id"] == f"predscore-{good}-focused"]
    run_head = w.store.head(w.scope, "experiment", w.experiment(focused["experiment_ref"])[
        "experiment_id"])  # fmt: skip
    assert run_head["state"] == "evaluated"
    # the ablation's derived proposals ran under derived approvals of the nightly identity
    for derived_id in w.stage_run(good)["data"]["stages"]["ablation"]["ablation_proposals"]:
        entry = w.stage_run(derived_id)["data"]["stages"]["ablation"]
        if entry["experiment_ref"] is not None:
            assert meta.experiment_approval(entry["experiment_ref"])["approved_by"][
                "subject_id"] == NIGHTLY_ID  # fmt: skip
    night2 = meta.night(NEXT)
    (done,) = [p for p in night2["phases"] if p["phase"] == "confirmation"]
    assert done["items"][0]["trials"] >= 64
    assert_pages_pass([str(p) for p in (meta.root / "dashboard").iterdir()
                       if p.suffix == ".html"])  # fmt: skip
    # nothing of the night touched holdout
    assert not [c for c in w.executor.calls if "-hol-" in c[1]]


# ==================================================================================================
# negative cases
# ==================================================================================================
def pin_in_memory_only(self: Any, basis: str, evidence: list[str], per_trial_tokens: int) -> None:
    """A pre-IC-29 qualification: pinned in memory, no record of the cell's qualification."""
    from amplai_foundry.evaluation.service import ExecutorPolicy
    from amplai_foundry.runtime.contracts.identity import new_id
    from amplai_foundry.runtime.execution.meta_local import EXECUTOR_ID

    ref = self._put("executor-qualification", new_id("executor-qualification"),
                    {"status": "pass", "executor_id": EXECUTOR_ID, "scope_note": basis,
                     "evidence": evidence})  # fmt: skip
    self.local.evaluation.executor_policy = ExecutorPolicy(
        frozenset({"sandbox_rerun"}), per_trial_tokens, 0, ref
    )


def test_without_an_ic29_record_the_night_stops_at_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LocalMetaOps, "qualify_executor", pin_in_memory_only)
    with build_world(tmp_path) as world:
        meta = Meta(world, tmp_path)
        meta.candidate("a2")
        calls = len(world.executor.calls)
        _ref, held = meta.run(DATE)
        assert held is not None and held.details["reason"] == "preflight"
        night = meta.night(DATE)
        (preflight,) = [p for p in night["phases"] if p["phase"] == "preflight"]
        assert {"code": "QUALIFIED_EXECUTOR_REQUIRED", "cell_id": CELL} in preflight["problems"]
        assert len(world.executor.calls) == calls and night["trials"] == 0


def test_a_standing_approval_revoked_mid_search_stops_the_night(meta: Meta) -> None:
    w = meta.w
    meta.calibrate_regression()
    pid = meta.candidate("a2")
    seen: list[str] = []

    def revoke_on_third_screening_trial(role: str, case_id: str, repeat: int) -> None:
        if "-dev-" in case_id:
            seen.append(case_id)
            if len(seen) == 3:
                w.local.approvals.revoke(w.operator, meta.standing_ref)

    meta.hook = revoke_on_third_screening_trial
    _ref, held = meta.run(DATE)
    assert held is not None and held.details["reason"] == "standing_approval"
    assert len(seen) == 3  # the trial guard re-checked the derived approval before the 4th
    night = meta.night(DATE)
    assert night["stopped"] == "standing_approval"
    assert meta.phases(DATE)["search"] == "stopped" and meta.phases(DATE)["dreaming"] == "skipped"
    assert meta.stages(pid)["screening"] == "aborted" and w.state(pid) == "screened"
    assert night["queued_confirmations"] == []


def test_the_nightly_identity_cannot_run_queue_or_select_holdout_or_derive_a_confirmatory_approval(
    meta: Meta,
) -> None:
    w = meta.w
    pid = meta.candidate("a2")
    runner = w.runner()  # the operator's runner: screening, then the focused gate
    runner.plan(pid, cell_id=CELL, root_budget=dict(MAX_BUDGET))
    runner.advance(pid)
    w.ops.approve_stage(pid, "focused")
    runner.advance(pid)  # ablation; stops at the holdout gate
    assert meta.stages(pid)["holdout"] == "waiting_approval"
    meta.runner._standing_ref = dict(meta.standing_ref)  # inside a night
    try:
        nightly = StageRunner(
            w.ops, w.refs, calibration_summary_ref=w.summary_ref,
            evaluator_version_ref=w.version_ref, metrics=runner.metrics,
            leak_gate=runner.leak_gate, parallel=1,
            issuer=standing_issuer(meta.runner.identity, meta.runner.derive),
        )  # fmt: skip
        hold("APPROVAL_HUMAN", nightly.approve_stage, pid, "holdout")
        hold("APPROVAL_HUMAN", nightly.approve_queued, pid, "focused")
        hold("META_STATE", nightly.queue_stage, pid, "holdout")
        with pytest.raises(RuntimeFault) as forbidden:
            nightly._select(w.refs["corpus_ref"], "holdout")
        assert forbidden.value.code == "FORBIDDEN"
        # a confirmatory (validation) experiment is never derived under the standing approval
        plan = runner._plan(pid)[1]
        entry = next(s for s in plan["stages"] if s["stage"] == "focused")
        proposal = w.proposal(pid)
        experiment, _cases, _ids = runner._build(pid, proposal, plan, entry)
        hold("STANDING_APPROVAL", meta.runner.derive, experiment)
    finally:
        meta.runner._standing_ref = None
    assert w.state(pid) == "screened"
    assert not [c for c in w.executor.calls if "-hol-" in c[1]]


# ==================================================================================================
# calibrate --set regression, approve-stage --queue (command line), night length
# ==================================================================================================
def test_calibrate_the_regression_set_is_the_drift_baseline(meta: Meta) -> None:
    result = meta.calibrate_regression()
    plan = meta.w.store.get(meta.w.scope, "calibration-plan", result["plan_ref"])
    assert sorted(plan["case_ids"]) == sorted(meta.reg_ids) and plan["splits"] == ["validation"]
    snap = meta.backend.snapshot(CELL, tuple(meta.reg_ids))
    assert snap["band"] == {"passes": REGRESSION, "runs": REGRESSION}
    assert snap["calibrated_at"] is not None
    with pytest.raises(RuntimeFault) as bad:
        meta.ops.calibrate([CELL], max_repeats=1, max_trials=100, parallel=1, max_tokens=10,
                           max_wall_seconds=10, corpus_set="holdout")  # fmt: skip
    assert bad.value.code == "CALIBRATION_PLAN"


def test_calibrate_the_regression_set_needs_regression_tasks(tmp_path: Path) -> None:
    with build_world(tmp_path) as w:
        hold("META_STATE", w.ops.calibrate, [CELL], max_repeats=1, max_trials=100, parallel=1,
             max_tokens=10, max_wall_seconds=10, corpus_set="regression")  # fmt: skip
        assert w.executor.calls == []


def test_the_commands_declare_queue_digest_and_set() -> None:
    result = CliRunner().invoke(cli.app, ["meta", "approve-stage", "--help"], env=PLAIN)
    assert result.exit_code == 0 and "--queue" in result.output and "--digest" in result.output
    result = CliRunner().invoke(cli.app, ["meta", "calibrate", "--help"], env=PLAIN)
    assert result.exit_code == 0 and "--set" in result.output


def test_queue_takes_no_qualification_and_partial_qualification_is_refused() -> None:
    for extra in (["--queue", "--per-trial-tokens", "5"], ["--per-trial-tokens", "5"]):
        result = CliRunner().invoke(
            cli.app,
            ["meta", "approve-stage", "p1", "--stage", "focused", *extra,
             "--config", "/nonexistent/local.json"],
        )  # fmt: skip
        assert result.exit_code == 2, result.output
        assert '"LOCAL_INPUT"' in result.output


def test_a_night_ends_at_the_earlier_of_stop_at_and_max_hours() -> None:
    from amplai_foundry.meta_harness.nightly import night_end, stop_epoch

    start = time.time()
    far = hhmm(start - 120)  # the next stop_at is almost a day away
    assert night_end(start, far) == pytest.approx(start + 8 * 3600)
    assert night_end(start, far, 2) == pytest.approx(start + 2 * 3600)
    near = hhmm(start + 3600 + 60)
    assert night_end(start, near) == stop_epoch(start, near)
    with pytest.raises(RuntimeFault):
        NightlyConfig(budget_trials=1, shares=dict(SHARES), cells=(CELL,), drift_tasks=(),
                      pilot=False, pilot_nights=0, keep_operator_share=0.5, max_parallel=1,
                      stop_at="07:00", max_hours=0)  # fmt: skip
    assert NightlyConfig.from_entry({
        "budget_trials": 1, "shares": dict(SHARES), "cells": [CELL], "drift_tasks": [],
        "pilot": False, "pilot_nights": 0, "keep_operator_share": 0.5, "max_parallel": 1,
        "stop_at": "07:00",
    }).max_hours == 8  # fmt: skip
    assert task_ids("development")  # the S11 corpus layout this module builds on


def test_a_standing_approval_revoked_during_a_confirmation_stops_the_night(meta: Meta) -> None:
    """The queued stage runs under the operator's approval; every trial guard of the night still
    re-checks the night's standing approval (§8.8)."""
    w = meta.w
    meta.calibrate_regression()
    good = meta.candidate("a2")
    ref, held = meta.run(DATE)
    assert held is None and ref is not None
    meta.ops.approve_stage(good, "focused", queue=True)
    seen: list[str] = []

    def revoke_on_third_validation_trial(role: str, case_id: str, repeat: int) -> None:
        if "-val-" in case_id and not case_id.startswith("reg-"):  # focused, not drift
            seen.append(case_id)
            if len(seen) == 3:
                w.local.approvals.revoke(w.operator, meta.standing_ref)

    meta.hook = revoke_on_third_validation_trial
    _ref, held = meta.run(NEXT)
    assert held is not None and held.details["reason"] == "standing_approval"
    assert len(seen) == 3
    assert meta.stages(good)["focused"] == "aborted"
    assert meta.stages(good)["ablation"] == "pending"  # nothing ran after the stop
    # the operator's approval itself is untouched: only the night ended
    approved = queue_head(w.store, w.scope, good)
    assert approved is not None and approved["data"]["approval_ref"] is not None


def test_a_standing_approval_revoked_during_the_ablation_stops_the_night_and_counts_its_trials(
    meta: Meta,
) -> None:
    """The ablation after a confirmation runs under derived approvals of the standing approval: a
    revocation on its first trial ends the night, and the night's trial count keeps the focused
    stage's trials and the ablation trials that ran (§8.8)."""
    w = meta.w
    meta.calibrate_regression()
    good = meta.candidate("a2")
    ref, held = meta.run(DATE)
    assert held is None and ref is not None
    meta.ops.approve_stage(good, "focused", queue=True)
    seen: list[str] = []

    def revoke_on_first_ablation_trial(role: str, case_id: str, repeat: int) -> None:
        if "-dev-" in case_id:  # night 2 runs no screening: development trials are the ablation's
            seen.append(case_id)
            if len(seen) == 1:
                w.local.approvals.revoke(w.operator, meta.standing_ref)

    meta.hook = revoke_on_first_ablation_trial
    _ref, held = meta.run(NEXT)
    assert held is not None and held.details["reason"] == "standing_approval", meta.night(NEXT)
    assert len(seen) == 1
    stages = meta.stages(good)
    assert stages["focused"] == "passed"
    night = meta.night(NEXT)
    assert night["stopped"] == "standing_approval"
    assert meta.phases(NEXT)["confirmation"] == "stopped"
    assert meta.phases(NEXT)["dreaming"] == "skipped"
    focused = w.stage_run(good)["data"]["stages"]["focused"]
    focused_trials = len(w.store.get(w.scope, "eval-report", focused["report_ref"])["run_refs"])
    ablation_trials = 0
    for derived_id in w.stage_run(good)["data"]["stages"]["ablation"]["ablation_proposals"]:
        entry = w.stage_run(derived_id)["data"]["stages"]["ablation"]
        if entry["report_ref"] is not None:
            report = w.store.get(w.scope, "eval-report", entry["report_ref"])
            ablation_trials += len(report["run_refs"])
    assert ablation_trials >= 1  # the trial that ran before the revocation was recorded
    assert night["trials"] == REGRESSION + focused_trials + ablation_trials
    # the operator's approvals are untouched and the holdout never ran
    assert not [c for c in w.executor.calls if "-hol-" in c[1]]


@pytest.mark.parametrize(("code", "reason"), [("KILL_SWITCH", "kill_switch"),
                                              ("CORPUS_CHANGED", None)])  # fmt: skip
def test_a_hold_out_of_the_ablation_keeps_the_focused_and_ablation_trials(
    meta: Meta, monkeypatch: pytest.MonkeyPatch, code: str, reason: str | None
) -> None:
    """``advance`` after a confirmation raises out of the ablation once its variants ran: the
    night still counts the focused stage's and the variants' trials; the kill switch stops the
    night, another Hold is a finding."""
    w = meta.w
    meta.calibrate_regression()
    good = meta.candidate("a2")
    ref, held = meta.run(DATE)
    assert held is None and ref is not None
    meta.ops.approve_stage(good, "focused", queue=True)
    original = StageRunner._ablation

    def ablation_then_hold(self: StageRunner, *args: Any, **kw: Any) -> bool:
        original(self, *args, **kw)
        raise Hold(code, "held after the variants ran")

    monkeypatch.setattr(StageRunner, "_ablation", ablation_then_hold)
    _ref, held = meta.run(NEXT)
    night = meta.night(NEXT)
    if reason is None:
        assert held is None, night
        assert f"CONFIRMATION: {code}" in night["findings"]
    else:
        assert held is not None and held.details["reason"] == reason
        assert night["stopped"] == reason
    focused = w.stage_run(good)["data"]["stages"]["focused"]
    focused_trials = len(w.store.get(w.scope, "eval-report", focused["report_ref"])["run_refs"])
    ablation_trials = 0
    for derived_id in w.stage_run(good)["data"]["stages"]["ablation"]["ablation_proposals"]:
        entry = w.stage_run(derived_id)["data"]["stages"]["ablation"]
        if entry["report_ref"] is not None:
            report = w.store.get(w.scope, "eval-report", entry["report_ref"])
            ablation_trials += len(report["run_refs"])
    assert ablation_trials > 0
    assert night["trials"] == REGRESSION + focused_trials + ablation_trials
    if reason is None:
        (done,) = [p for p in night["phases"] if p["phase"] == "confirmation"]
        assert done["items"][0]["trials"] == focused_trials + ablation_trials
