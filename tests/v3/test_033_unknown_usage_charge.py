"""Operator decision 2026-10-08 (B) and IC-34 (A): unknown usage and the total-input meaning.

(B): a trial whose token counts are unknown is charged its per-trial reservation against the root
budget, the run continues, and the trial is a missing outcome (``success`` null, the executor's
value kept in ``reported_success``). A real overrun or a safety failure still stops the run.
Usage is never recovered from a file the agent can write.

(A): ``usage.input_tokens`` is every input token, cache included, for every provider. A read-only
turn's usage is priced from its cache breakdown (``protocol.turn_detail``) the way a run's usage
detail is, so pricing counts no cache token twice and no cached token at the uncached rate.

Real: store, budget ledger, EvaluationService, CalibrationService, pricing. Stand-in: the scripted
executor of the S2 tests (``test_033_s2_service.Scripted``), whose receipts the services check.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_033_s2_calibration import make, outcome, plan_for, service, tokens_policy
from test_033_s2_service import Scripted, make_world, policy, ref_key

from amplai_foundry.agent_drivers.protocol import EventNormalizer, turn_detail, usage_detail
from amplai_foundry.evaluation import pricing
from amplai_foundry.meta_harness.trial_metrics import TrialMetrics
from amplai_foundry.runtime.contracts.identity import canonical
from amplai_foundry.runtime.execution.readonly_turn import _claude_usage

# The Claude result recorded for run-730c167378a14c84b059c83c2cdd11e3
# (usage-detail-0f803a17b208ebbdd7f99eed843439e2).
CLAUDE_USAGE = {
    "input_tokens": 20,
    "cache_read_input_tokens": 324979,
    "cache_creation_input_tokens": 43552,
    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 43552},
    "output_tokens": 9363,
}
CLAUDE_TOTAL = 20 + 324979 + 43552


@pytest.fixture
def w(tmp_path):
    with make_world(tmp_path) as world:
        yield world


class UnknownUsage(Scripted):
    """``Scripted``, except the trials ``unknown(role, case_id, repeat)`` names report no token
    counts and no cost (``usage_status`` unknown), the way ``LocalTrialExecutor`` reports a trial
    whose run usage is unknown (``local_executor.py`` ``_usage``)."""

    def __init__(
        self, world, roles, outcome=None, *, unknown, safety=None, lookup=None, known=None, **kw
    ):
        super().__init__(world, roles, outcome, **kw)
        self.unknown, self.safety = unknown, safety or (lambda role, cid, rep: 0)
        # ``lookup``: answer-lookup evidence entries of the trial (decision (C)), which then
        # reports success False the way ``LocalTrialExecutor._observe`` does; ``known``: the
        # tokens the reporting parts of an unknown usage add up to (``known_tokens``)
        self.lookup = lookup or (lambda role, cid, rep: 0)
        self.known = known or (lambda role, cid, rep: None)

    def _observe(self, composition, case, repeat, mode):
        role = self.roles[ref_key(composition)]
        cid = case["case_id"]
        unknown, safety = self.unknown(role, cid, repeat), self.safety(role, cid, repeat)
        if not unknown and not safety:
            return super()._observe(composition, case, repeat, mode)
        key = (ref_key(composition), cid, repeat)
        if key not in self.cache:
            d = self.world.m.d
            success = self.outcome(role, cid, repeat)
            success = success[0] if isinstance(success, tuple) else success
            lookups = self.lookup(role, cid, repeat)
            success = False if lookups else success
            known = self.known(role, cid, repeat) if unknown else None
            tin, tout = (None, None) if unknown else self.tokens
            cost = None if unknown else 0
            usage_status = "unknown" if unknown else "measured"
            receipt = {
                "success": success, "safety_failures": safety, "unknown_effects": 0,
                "cost_microunits": cost, "input_tokens": tin, "output_tokens": tout,
                "usage_status": usage_status, "mode": mode, "composition_ref": composition,
                "task_id": cid, "repeat": repeat, "scope": d.scope.wire(),
                "answer_lookup": [{"kind": "web_search", "rule": "codex_web_search"}] * lookups,
            }  # fmt: skip
            if known is not None:
                receipt |= {"known_tokens": known, "known_cost_microunits": 0}
            artifact = d.artifacts.admit(
                d.scope, canonical(receipt), "application/json", trust="verifier"
            )
            from amplai_foundry.evaluation.service import TrialObservation

            self.cache[key] = TrialObservation(
                success, (artifact,), safety_failures=safety, cost_microunits=cost,
                input_tokens=tin, output_tokens=tout, usage_status=usage_status,
                answer_lookup=lookups, known_tokens=known,
                known_cost_microunits=None if known is None else 0,
            )  # fmt: skip
        return self.cache[key]


# --- (B) EvaluationService -------------------------------------------------------------------


def with_executor_tokens(w, tokens):
    meta = w.m
    meta.eval.executor_policy = dataclasses.replace(
        meta.eval.executor_policy, max_trial_tokens=tokens
    )


def experiment_run(w, executor_for, *, val=4, ceiling=100):
    s = w.setup(val=val)
    with_executor_tokens(w, ceiling)
    ref = w.freeze(w.experiment(s, policy(s.ev)))
    executor = executor_for(s)
    report_ref = w.run(ref, executor)
    allocations = w.budget(s.prop["proposal_id"])["data"]["allocations"]
    return SimpleNamespace(s=s, report_ref=report_ref, executor=executor, allocations=allocations)


def test_an_experiment_with_one_unknown_usage_trial_runs_every_other_trial(w):
    def executor_for(s):
        return UnknownUsage(
            w, w.roles(s), tokens=(5, 5),
            unknown=lambda role, cid, rep: (role, cid) == ("candidate", "val-00"),
        )  # fmt: skip

    r = experiment_run(w, executor_for)
    trials = w.trials(r.report_ref)
    assert len(trials) == 8  # 4 tasks x 2 arms: the run did not stop
    analysis = w.analysis(r.report_ref)
    assert "budget_overrun_or_unknown_usage" not in analysis["reasons"]
    (missing,) = [t for t in trials if t.get("outcome_missing")]
    assert (missing["task_id"], missing["arm"]) == ("val-00", "candidate")
    assert missing["success"] is None and missing["reported_success"] is True
    assert missing["outcome_missing"] == "usage_unknown" and missing["charged_tokens"] == 100
    assert missing["input_tokens"] is None and missing["usage_status"] == "unknown"
    # the missing outcome is reported as such by the analysis (§7.2 missing rule)
    assert "missing_or_unknown_trials" in analysis["reasons"]


def test_the_root_budget_counts_the_reservation_of_an_unknown_usage_trial(w):
    def executor_for(s):
        return UnknownUsage(
            w, w.roles(s), tokens=(5, 5),
            unknown=lambda role, cid, rep: (role, cid) == ("baseline", "val-01"),
        )  # fmt: skip

    r = experiment_run(w, executor_for, ceiling=100)
    by_status = sorted((a["status"], a["tokens"], a["overrun"]) for a in r.allocations.values())
    # seven measured trials of 10 tokens and the unknown one at its 100-token reservation; every
    # allocation is settled, so later reservations of the root are admitted (no META_USAGE_UNKNOWN)
    assert by_status == [("settled", 10, False)] * 7 + [("settled", 100, False)]
    cost_ceiling = w.m.eval.executor_policy.max_trial_cost_microunits
    unknown = [a for a in r.allocations.values() if a["tokens"] == 100]
    assert unknown[0]["cost"] == cost_ceiling
    assert w.m.eval.budgets.totals(w.scope, r.s.prop["proposal_id"])["unknown"] == 0


def test_a_safety_failure_still_stops_an_experiment_after_an_unknown_usage_trial(w):
    def executor_for(s):
        return UnknownUsage(
            w, w.roles(s), tokens=(5, 5),
            unknown=lambda role, cid, rep: cid == "val-00",
            safety=lambda role, cid, rep: 1 if (role, cid) == ("baseline", "val-01") else 0,
        )  # fmt: skip

    r = experiment_run(w, executor_for)
    trials = w.trials(r.report_ref)
    assert [(t["task_id"], t["arm"]) for t in trials] == [
        ("val-00", "baseline"), ("val-00", "candidate"), ("val-01", "baseline"),
    ]  # fmt: skip
    assert "safety_or_unknown_effect" in w.analysis(r.report_ref)["reasons"]
    assert w.report(r.report_ref)["verdict"] in {"fail", "aborted"}


def test_a_real_overrun_still_stops_an_experiment(w):
    def executor_for(s):
        # 60 + 60 tokens against a 100-token reservation
        return UnknownUsage(w, w.roles(s), tokens=(60, 60), unknown=lambda *a: False)

    r = experiment_run(w, executor_for)
    assert len(w.trials(r.report_ref)) == 1
    assert "budget_overrun_or_unknown_usage" in w.analysis(r.report_ref)["reasons"]


def test_an_answer_lookup_with_unknown_usage_stays_a_failed_experiment_trial(w):
    """The recorded case (caltrial-441e1077…): the turn searched the web, never completed, and its
    usage is unknown. Decision (C) wins for the outcome: the trial is FAILED, not missing; (B)
    still charges the reservation and the run continues."""

    def executor_for(s):
        return UnknownUsage(
            w, w.roles(s), tokens=(5, 5),
            unknown=lambda role, cid, rep: (role, cid) == ("candidate", "val-00"),
            lookup=lambda role, cid, rep: 1 if (role, cid) == ("candidate", "val-00") else 0,
        )  # fmt: skip

    r = experiment_run(w, executor_for, ceiling=100)
    trials = w.trials(r.report_ref)
    assert len(trials) == 8  # the run did not stop
    (looked,) = [t for t in trials if (t["task_id"], t["arm"]) == ("val-00", "candidate")]
    assert looked["success"] is False and looked["reported_success"] is False
    assert "outcome_missing" not in looked
    assert looked["charged_tokens"] == 100 and looked["input_tokens"] is None
    analysis = w.analysis(r.report_ref)
    assert "budget_overrun_or_unknown_usage" not in analysis["reasons"]
    # a known failure, not a missing outcome
    assert "missing_or_unknown_trials" not in analysis["reasons"]
    assert sorted(a["tokens"] for a in r.allocations.values()) == [10] * 7 + [100]


def test_reported_parts_above_the_reservation_still_stop_an_experiment(w):
    """The planner and auxiliary turns already reported more than the reservation and the run's
    usage is unknown: the charge is the reported lower bound, an overrun, so the run stops."""

    def executor_for(s):
        return UnknownUsage(
            w, w.roles(s), tokens=(5, 5),
            unknown=lambda role, cid, rep: (role, cid) == ("baseline", "val-00"),
            known=lambda role, cid, rep: 120,
        )  # fmt: skip

    r = experiment_run(w, executor_for, ceiling=100)
    trials = w.trials(r.report_ref)
    assert len(trials) == 1 and trials[0]["charged_tokens"] == 120
    assert trials[0]["known_tokens"] == 120
    assert "budget_overrun_or_unknown_usage" in w.analysis(r.report_ref)["reasons"]
    (allocation,) = r.allocations.values()
    assert allocation["tokens"] == 120 and allocation["overrun"] is True


def test_reported_parts_below_the_reservation_are_charged_the_reservation(w):
    def executor_for(s):
        return UnknownUsage(
            w, w.roles(s), tokens=(5, 5),
            unknown=lambda role, cid, rep: (role, cid) == ("baseline", "val-00"),
            known=lambda role, cid, rep: 30,
        )  # fmt: skip

    r = experiment_run(w, executor_for, ceiling=100)
    assert len(w.trials(r.report_ref)) == 8
    assert sorted(a["tokens"] for a in r.allocations.values()) == [10] * 7 + [100]


# --- (B) CalibrationService ------------------------------------------------------------------


def calibration(w, executor_for, *, svc=None):
    c = make(w)
    svc = svc or service(w)
    plan = plan_for(w, c)
    plan_ref = svc.freeze(w.m.reviewer, plan)
    executor = executor_for(c)
    summary_ref = svc.run(w.m.reviewer, plan_ref, executor, parallel=1)
    store = w.m.d.store
    head = store.head(w.scope, "calibration-run", plan_ref["id"])
    return SimpleNamespace(
        plan=plan,
        head=head,
        summary=store.get(w.scope, "calibration-summary", summary_ref),
        trials=[store.get(w.scope, "calibration-trial", r) for r in head["data"]["trial_refs"]],
        allocations=store.head(w.scope, "meta-budget", "calibration:" + plan_ref["id"])["data"][
            "allocations"
        ],
    )


def test_a_calibration_with_one_unknown_usage_trial_completes_the_rest(w):
    def executor_for(c):
        return UnknownUsage(
            w, c.roles, outcome, tokens=(5, 5),
            unknown=lambda role, cid, rep: (role, cid, rep) == ("cell-a", "dev-00", 0),
        )  # fmt: skip

    r = calibration(w, executor_for)
    # the same 28 trials as a run without the unknown trial
    # (test_q11_one_run_per_task_and_cell_then_repeats_only_where_cells_disagree)
    assert len(r.trials) == 28
    assert r.head["state"] == "done" and r.head["data"]["stop_reason"] is None
    (missing,) = [t for t in r.trials if t.get("outcome_missing")]
    assert (missing["cell_id"], missing["task_id"]) == ("cell-a", "dev-00")
    assert missing["success"] is None and missing["reported_success"] is True
    cell_a = r.summary["cells"]["cell-a"]
    # a missing outcome makes its task unknown (§8.2 "any unknown trial")
    assert cell_a["tasks"]["dev-00"]["class"] == "unknown"
    assert cell_a["tasks"]["dev-00"]["runs"] == 0
    assert cell_a["usage_unknown_trials"] == 1
    assert r.summary["cells"]["cell-b"]["usage_unknown_trials"] == 0
    # tokens per solved counts the missing trial at its charge (the reservation) and never as
    # solved: the measured cell-a trials of 10 tokens plus the charged one, over the solved ones
    solved = sum(t["success"] is True for t in r.trials if t["cell_id"] == "cell-a")
    measured = sum(1 for t in r.trials if t["cell_id"] == "cell-a" and not t.get("outcome_missing"))
    assert missing["charged_tokens"] > 0
    assert cell_a["tokens_per_solved"] == pytest.approx(
        (10 * measured + missing["charged_tokens"]) / solved
    )


def test_the_calibration_budget_counts_the_reservation_of_an_unknown_usage_trial(w):
    svc = service(w, tokens_policy(w, 40))

    def executor_for(c):
        return UnknownUsage(
            w, c.roles, outcome, tokens=(5, 5),
            unknown=lambda role, cid, rep: (role, cid, rep) == ("cell-b", "val-02", 0),
        )  # fmt: skip

    r = calibration(w, executor_for, svc=svc)
    tokens = sorted(a["tokens"] for a in r.allocations.values())
    assert tokens == [10] * 27 + [40]
    assert {a["status"] for a in r.allocations.values()} == {"settled"}
    assert not any(a["overrun"] for a in r.allocations.values())


def test_a_safety_failure_fails_a_calibration_trial_even_with_unknown_usage(w):
    # operator decision 2026-10-09 (A): calibration records a safety failure as a FAILED trial
    # and goes on; with unknown usage too, the safety failure is the outcome (not missing)
    def executor_for(c):
        return UnknownUsage(
            w, c.roles, outcome, tokens=(5, 5),
            unknown=lambda role, cid, rep: cid in ("dev-00", "val-00"),
            safety=lambda role, cid, rep: 1 if (role, cid) == ("cell-a", "val-00") else 0,
        )  # fmt: skip

    r = calibration(w, executor_for)
    assert r.head["state"] == "done" and r.head["data"]["stop_reason"] is None
    unsafe = [t for t in r.trials if t["cell_id"] == "cell-a" and t["task_id"] == "val-00"]
    assert unsafe and all(t["success"] is False and t["safety_failures"] == 1 for t in unsafe)
    assert all("outcome_missing" not in t for t in unsafe)


def test_a_real_overrun_still_stops_a_calibration(w):
    def executor_for(c):
        return UnknownUsage(w, c.roles, outcome, tokens=(6, 5), unknown=lambda *a: False)

    r = calibration(w, executor_for)  # 11 tokens against the 10-token reservation
    assert r.head["state"] == "stopped" and len(r.trials) == 1
    assert r.head["data"]["stop_reason"] == "budget_overrun_or_unknown_usage"


def test_an_answer_lookup_with_unknown_usage_is_a_failed_calibration_outcome(w):
    svc = service(w, tokens_policy(w, 40))

    def executor_for(c):
        # cell-a solves dev-00 in ``outcome``; the lookup fails it
        return UnknownUsage(
            w, c.roles, outcome, tokens=(5, 5),
            unknown=lambda role, cid, rep: (role, cid, rep) == ("cell-a", "dev-00", 0),
            lookup=lambda role, cid, rep: 2 if (role, cid, rep) == ("cell-a", "dev-00", 0) else 0,
        )  # fmt: skip

    r = calibration(w, executor_for, svc=svc)
    assert r.head["data"]["stop_reason"] is None  # the run continued
    (looked,) = [t for t in r.trials if t.get("charged_tokens") is not None]
    assert looked["success"] is False and "outcome_missing" not in looked
    assert looked["charged_tokens"] == 40
    cell_a = r.summary["cells"]["cell-a"]
    # a failed outcome, counted: the task has a run and is not "unknown"
    assert cell_a["tasks"]["dev-00"]["runs"] >= 1
    assert cell_a["tasks"]["dev-00"]["class"] != "unknown"
    assert cell_a["usage_unknown_trials"] == 1
    assert sorted(a["tokens"] for a in r.allocations.values())[-1] == 40


def test_reported_parts_above_the_reservation_still_stop_a_calibration(w):
    def executor_for(c):
        return UnknownUsage(
            w, c.roles, outcome, tokens=(5, 5),
            unknown=lambda role, cid, rep: True,
            known=lambda role, cid, rep: 11,
        )  # fmt: skip

    r = calibration(w, executor_for)  # 11 reported tokens against the 10-token reservation
    assert r.head["state"] == "stopped" and len(r.trials) == 1
    assert r.head["data"]["stop_reason"] == "budget_overrun_or_unknown_usage"
    assert r.trials[0]["charged_tokens"] == 11


# --- (B) trial metrics and the executor's lower bound -----------------------------------------


def _row(success, tin, tout, charged=None):
    return {"success": success, "input_tokens": tin, "output_tokens": tout,
            "charged_tokens": charged, "seconds": None}  # fmt: skip


def test_trial_metrics_count_the_charge_of_an_unknown_usage_trial() -> None:
    rows = [_row(True, 5, 5), _row(False, 5, 5), _row(None, None, None, charged=100)]
    # (10 + 10 + 100 charged) over one solved trial
    assert TrialMetrics._brief(rows)["tokens_per_solved"] == 120.0


def test_trial_metrics_report_no_tokens_per_solved_with_a_trial_of_no_recorded_tokens() -> None:
    # an executor fault (or a trial stored before (B)) has neither counts nor a charge: unknown
    # is never fewer, so the arm has no tokens per solved rather than a smaller one
    rows = [_row(True, 5, 5), _row(None, None, None)]
    assert TrialMetrics._brief(rows)["tokens_per_solved"] is None


def _usage_of(planner, aux):
    from amplai_foundry.meta_harness.local_executor import LocalTrialExecutor

    executor = SimpleNamespace(store=None, scope=None)
    return LocalTrialExecutor._usage(executor, [], planner, real=True, aux=aux)  # type: ignore[arg-type]


def test_the_executor_keeps_the_reported_parts_of_an_unknown_usage() -> None:
    planner = {"input_tokens": 700_000, "output_tokens": 20_000}
    aux = [
        {"tokens": 1, "usage": {"input_tokens": 480_000, "output_tokens": 5_000}},
        {"tokens": None, "usage": None},  # a turn that may have spent tokens unreported
    ]
    usage = _usage_of(planner, aux)
    assert usage["input_tokens"] is None and usage["usage_status"] == "unknown"
    assert usage["known_tokens"] == 1_205_000 and usage["known_cost_microunits"] == 0


def test_a_known_usage_carries_no_lower_bound() -> None:
    usage = _usage_of({"input_tokens": 7, "output_tokens": 3}, [])
    assert usage["input_tokens"] == 7 and "known_tokens" not in usage


# --- (A) one input meaning, priced once ------------------------------------------------------


def price_tables(tmp_path: Path) -> list[pricing.PriceTable]:
    root = tmp_path / "prices"
    root.mkdir()
    (root / "t1.json").write_text(json.dumps({
        "schema_version": "price-table-1", "price_table_id": "t1",
        "effective_from": "2026-09-30", "currency": "USD",
        "models": {
            "claude-opus-5-5": {
                "provider": "anthropic", "input": 2_000_000, "cache_read": 200_000,
                "cache_write_5m": 2_500_000, "cache_write_1h": 4_000_000,
                "output": 10_000_000, "sources": ["https://y"]},
            "gpt-5.6": {
                "provider": "openai", "input": 1_000_000, "cache_read": 100_000,
                "cache_write": 0, "output": 8_000_000, "sources": ["https://x"]},
        },
    }))  # fmt: skip
    return pricing.load_tables(root)


def test_a_claude_read_only_turn_rebuilds_the_run_detail_of_the_same_counts() -> None:
    turn = _claude_usage(CLAUDE_USAGE)
    assert turn["input_tokens"] == CLAUDE_TOTAL
    run = EventNormalizer("claude")
    run.accept({"type": "result", "subtype": "success", "usage": CLAUDE_USAGE})
    assert turn_detail("claude", turn) == run.usage_detail == usage_detail("claude", CLAUDE_USAGE)
    assert run.usage["input_tokens"] == turn["input_tokens"]


def test_a_codex_read_only_turn_detail_is_its_own_breakdown() -> None:
    usage = {"input_tokens": 78089, "cached_input_tokens": 52608, "output_tokens": 2691}
    assert turn_detail("codex", usage) == usage_detail("codex", usage)


@pytest.mark.parametrize(
    "provider,usage",
    [
        # a Claude planner turn stored before IC-34 kept input and output only
        # (caltrial-8983599a receipt planner.usage)
        ("claude", {"input_tokens": 8, "output_tokens": 3321}),
        (
            "claude",
            {
                "input_tokens": 5,
                "cache_read_input_tokens": 9,
                "cache_creation_input_tokens": 0,
                "output_tokens": 1,
            },
        ),
        ("codex", {"input_tokens": 10, "output_tokens": 1}),
        ("claude", {"input_tokens": None, "output_tokens": None}),
        ("opencode", {"input_tokens": 1, "output_tokens": 1}),
    ],
)
def test_a_turn_without_a_usable_cache_breakdown_has_no_detail(provider, usage) -> None:
    assert turn_detail(provider, usage) is None


def test_a_claude_planner_turn_is_priced_exactly_and_counts_cache_once(tmp_path: Path) -> None:
    tables = price_tables(tmp_path)
    metrics = SimpleNamespace(tables=tables)
    turn = _claude_usage(CLAUDE_USAGE)
    detail = TrialMetrics._turn_detail(metrics, "claude-opus-5-5", "2026-10-08", turn)  # type: ignore[arg-type]
    got = pricing.estimate(
        "claude-opus-5-5", "2026-10-08", detail=detail, usage=turn, tables=tables
    )
    assert got["upper_bound"] is False
    assert got["tokens"] == {"input": 20, "cache_read": 324979, "cache_write_5m": 0,
                             "cache_write_1h": 43552, "output": 9363}  # fmt: skip
    assert sum(got["tokens"].values()) == CLAUDE_TOTAL + 9363
    # the same price as the run that reported these counts
    run = EventNormalizer("claude")
    run.accept({"type": "result", "subtype": "success", "usage": CLAUDE_USAGE})
    as_run = pricing.estimate(
        "claude-opus-5-5", "2026-10-08", detail=run.usage_detail, usage=run.usage, tables=tables
    )
    assert got["cost_microunits"] == as_run["cost_microunits"]


def test_an_old_claude_planner_turn_stays_an_upper_bound(tmp_path: Path) -> None:
    tables = price_tables(tmp_path)
    old = {"input_tokens": 8, "output_tokens": 3321}
    metrics = SimpleNamespace(tables=tables)
    assert TrialMetrics._turn_detail(metrics, "claude-opus-5-5", "2026-10-08", old) is None  # type: ignore[arg-type]
    got = pricing.estimate("claude-opus-5-5", "2026-10-08", detail=None, usage=old, tables=tables)
    assert got["upper_bound"] is True


def test_a_codex_aux_turn_is_priced_from_its_breakdown(tmp_path: Path) -> None:
    tables = price_tables(tmp_path)
    usage = {"input_tokens": 78089, "cached_input_tokens": 52608, "output_tokens": 2691}
    metrics = SimpleNamespace(tables=tables)
    detail = TrialMetrics._turn_detail(metrics, "gpt-5.6", "2026-10-08", usage)  # type: ignore[arg-type]
    got = pricing.estimate("gpt-5.6", "2026-10-08", detail=detail, usage=usage, tables=tables)
    assert got["tokens"] == {"input": 78089 - 52608, "cache_read": 52608, "cache_write": 0,
                             "output": 2691}  # fmt: skip
    assert got["upper_bound"] is False


def test_an_unpriced_model_gives_no_turn_detail(tmp_path: Path) -> None:
    metrics = SimpleNamespace(tables=price_tables(tmp_path))
    turn = _claude_usage(CLAUDE_USAGE)
    assert TrialMetrics._turn_detail(metrics, "no-such-model", "2026-10-08", turn) is None  # type: ignore[arg-type]
