"""Work 033 S12: the nightly runner on fakes (interfaces.md §2.14, §3.13, §8.8-§8.9, IC-13, IC-17,
IC-18; plan.md §9 L10 and §10.5).

IC-17 and IC-18 are provisional: no night runs until the human operator issues a standing approval.
The runner (`meta_harness/nightly.py`) owns the phase order, the budget shares, the stop
conditions, the drift rule, the surrogate ranking, successive halving and the records; the phase
actions are a `NightlyBackend`, which these tests replace with a scripted fake (no driver, docker,
model turn or network). The store, the approvals and the standing approval are the real ones of
the product deployment (`test_rc06_local_product.product`).
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pytest
from test_rc06_local_product import product  # noqa: F401  (fixture)
from typer.testing import CliRunner

from amplai_foundry.evaluation.sequential import wilson
from amplai_foundry.meta_harness import nightly as nightly_module
from amplai_foundry.meta_harness.nightly import (
    PHASES,
    NightlyConfig,
    NightlyRunner,
    nightly_policy,
    phase_budget,
    stop_epoch,
)
from amplai_foundry.meta_harness.surrogate import fold_over, plackett_burman_12
from amplai_foundry.runtime.cli import app
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.meta_local import APPROVAL_KIND, NIGHTLY_ID
from amplai_foundry.runtime.meta_commands import nightly as commands

CELL = "codex-cli"
OTHER_CELL = "claude-cli"
DATE = "2026-10-01"
SHARES = {"drift": 0.1, "screening_design": 0.1, "search": 0.6, "confirmation": 0.3}
SHARES_NO_SCREEN = {**SHARES, "screening_design": 0.0}
MAX_BUDGET = {
    "max_wall_seconds": 3600, "max_attempts": 3, "max_tokens": 100000, "max_cost_microunits": 0,
    "currency": "USD", "max_parallel_works": 2, "max_delegation_depth": 0,
}  # fmt: skip
SNAP = {"provider_model_id": "model-1", "driver_version": "1.2.3", "image": "sha256:image-1"}
Z95 = NormalDist().inv_cdf(0.975)
DAY = 86400

# the surrogate ranks c4 > c2 > c3 > c1 from these development rows (strategy option per candidate)
OPTIONS = {"c1": "bad", "c2": "second", "c3": "third", "c4": "best"}
SUCCESS = {"bad": 0, "third": 2, "second": 4, "best": 6}  # of 6 runs per task
RATES = {"c1": 0.25, "c2": 0.75, "c3": 0.5, "c4": 0.9}
SURROGATE_ORDER = ["c4", "c2", "c3", "c1"]


def iso(epoch: float) -> str:
    from datetime import UTC

    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def config(clock: Clock | None = None, **over: Any) -> NightlyConfig:
    base: dict[str, Any] = {
        "budget_trials": 200, "shares": dict(SHARES), "cells": (CELL,),
        "drift_tasks": ("t1", "t2"), "pilot": True, "pilot_nights": 3,
        "keep_operator_share": 0.5, "max_parallel": 2,
        "stop_at": hhmm((clock.t if clock else time.time()) + 6 * 3600),
    }  # fmt: skip
    return NightlyConfig(**{**base, **over})


class Fake:
    """A scripted `NightlyBackend`; every call is logged in `calls`."""

    def __init__(self, clock: Clock) -> None:
        self.clock, self.calls = clock, []
        self.runner: NightlyRunner | None = None
        self.kill = False
        self.quota = False
        self.quota_since: float | None = None
        self.problems: list[dict[str, Any]] = []
        self.snaps: dict[str, dict[str, Any]] = {}
        self.drift_batch: dict[str, Any] = {"trials": 5, "passes": 4, "runs": 5}
        self.confirm_items: list[dict[str, Any]] = []
        self.proposed = ["prop-1"]
        self.propose_error: Hold | None = None
        self.search_hold: dict[str, Hold] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.derived: list[dict[str, Any]] = []
        self.unknown_cid: str | None = None
        self.bad_batch = False
        self.step = 1.0
        self.before_search: Callable[[Fake, str, int], None] | None = None
        self.after_search: Callable[[Fake, str, int], None] | None = None
        self.search_n = 0
        self.guards: list[Callable[[int], None]] = []
        self.screening: list[list[dict[str, str]]] = []
        self.dashboard_error: Exception | None = None

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def args(self, name: str) -> list[tuple[Any, ...]]:
        return [c[1:] for c in self.calls if c[0] == name]

    def kill_switch(self) -> bool:
        self.calls.append(("kill_switch",))
        return self.kill

    def quota_signal(self, since: float) -> bool:
        self.quota_since = since
        return self.quota

    def preflight(self, cells: tuple[str, ...]) -> list[dict[str, Any]]:
        self.calls.append(("preflight", cells))
        return list(self.problems)

    def snapshot(self, cell_id: str, drift_tasks: tuple[str, ...]) -> dict[str, Any]:
        self.calls.append(("snapshot", cell_id))
        return self.snaps.get(
            cell_id, {"current": SNAP, "calibrated": SNAP, "band": {"passes": 8, "runs": 10}}
        )

    def drift_run(self, cell_id: str, tasks: list[str], guard: Any, limit: int) -> dict[str, Any]:
        self.calls.append(("drift_run", cell_id, tuple(tasks), limit))
        self.guards.append(guard)
        guard(self.drift_batch["trials"])
        return dict(self.drift_batch)

    def schedule_recalibration(self, cell_id: str, reason: str) -> dict[str, Any]:
        self.calls.append(("schedule_recalibration", cell_id, reason))
        return {"scheduled": True, "reason": reason}

    def removal_sweep(self, cell_id: str, reason: str) -> list[str]:
        self.calls.append(("removal_sweep", cell_id, reason))
        return [f"sweep-{cell_id}"]

    def screening_factors(self, cell_id: str) -> list[str]:
        self.calls.append(("screening_factors", cell_id))
        return [f"layer{i}" for i in range(11)]

    def screening_run(self, cell_id: str, configs: list[dict[str, str]], guard: Any, limit: int
                      ) -> dict[str, Any]:  # fmt: skip
        self.calls.append(("screening_run", cell_id, len(configs)))
        self.screening.append(configs)
        guard(len(configs))
        return {"trials": len(configs), "passes": len(configs) // 2, "runs": len(configs)}

    def propose(self, cell_id: str) -> list[str]:
        self.calls.append(("propose", cell_id))
        if self.propose_error is not None:
            raise self.propose_error
        return list(self.proposed)

    def search_candidates(self, cell_id: str) -> list[dict[str, Any]]:
        self.calls.append(("search_candidates", cell_id))
        return [{"candidate_id": cid, "options": {"strategy": opt}} for cid, opt in OPTIONS.items()]

    def search_rows(self, cell_id: str) -> list[dict[str, Any]]:
        self.calls.append(("search_rows", cell_id))
        return [
            {"task_id": task, "options": {"strategy": opt}, "success": i < wins}
            for opt, wins in SUCCESS.items()
            for task in ("t1", "t2")
            for i in range(6)
        ]

    def development_tasks(self, cell_id: str) -> list[str]:
        self.calls.append(("development_tasks", cell_id))
        return ["t1", "t2"]

    def search_run(self, cell_id: str, cid: str, tasks: int, guard: Any, limit: int
                   ) -> dict[str, Any]:  # fmt: skip
        self.search_n += 1
        self.calls.append(("search_run", cid, tasks))
        self.guards.append(guard)
        if self.before_search is not None:
            self.before_search(self, cid, tasks)
        if cid in self.plans:
            assert self.runner is not None
            self.derived.append(self.runner.derive(self.plans[cid]))
        if cid in self.search_hold:
            raise self.search_hold[cid]
        guard(tasks)
        self.clock.advance(self.step)
        if self.after_search is not None:
            self.after_search(self, cid, tasks)
        if self.bad_batch:
            return {"trials": -1}
        passes = round(RATES[cid] * tasks)
        batch: dict[str, Any] = {
            "trials": tasks,
            "passes": passes,
            "runs": tasks,
            "promising": tasks == 16,
            "proposal_id": f"proposal-{cid}",
        }
        if cid == self.unknown_cid:
            batch.update(unknown_effects=1, root="proposal-xyz")
        return batch

    def queue_confirmation(self, cell_id: str, candidate_id: str) -> dict[str, Any] | None:
        self.calls.append(("queue_confirmation", cell_id, candidate_id))
        return {"id": f"exp-{candidate_id}", "revision": 1, "digest": "sha256:" + "a" * 64}

    def confirmations(self) -> list[dict[str, Any]]:
        self.calls.append(("confirmations",))
        return list(self.confirm_items)

    def confirmation_run(self, item: dict[str, Any], guard: Any, limit: int) -> dict[str, Any]:
        self.calls.append(("confirmation_run", item["id"]))
        guard(3)
        return {"trials": 3, "passes": 2, "runs": 3}

    def dream(self, cell_id: str, night: str) -> dict[str, Any] | None:
        self.calls.append(("dream", cell_id, night))
        return {"id": f"dream-{cell_id}", "revision": 1, "digest": "sha256:" + "d" * 64}

    def dashboard(self) -> list[str] | None:
        self.calls.append(("dashboard",))
        if self.dashboard_error is not None:
            raise self.dashboard_error
        return ["index.html", "approvals.html"]


class Env:
    def __init__(self, dep: Any) -> None:
        self.dep = dep
        self.store, self.scope = dep.store, dep.scope
        self.approvals = dep.meta_local.approvals
        self.operator = dep.meta_operator()
        self.clock = Clock()
        self.fake = Fake(self.clock)

    def put(self, kind: str, object_id: str, value: dict[str, Any]) -> dict[str, Any]:
        with self.store.tx() as db:
            return self.store.put(
                db, self.scope, kind, object_id, 1, {"scope": self.scope.wire(), **value}
            )

    def standing(self, cfg: NightlyConfig | None = None, **over: Any) -> dict[str, Any]:
        now = time.time()
        args: dict[str, Any] = {
            "budget_trials": 200, "valid_from": iso(now - 60), "valid_until": iso(now + 3 * DAY),
            "max_budget": dict(MAX_BUDGET),
        }  # fmt: skip
        policy = nightly_policy(cfg or config(self.clock), **{**args, **over})
        return self.approvals.issue_nightly(self.operator, policy)

    def runner(self, cfg: NightlyConfig | None = None) -> NightlyRunner:
        runner = NightlyRunner(
            self.dep, cfg or config(self.clock), clock=self.clock, backend=self.fake
        )
        self.fake.runner = runner
        return runner

    def ready(self, cfg: NightlyConfig | None = None, **standing: Any) -> NightlyRunner:
        """A runner with a current standing approval of the same config."""
        cfg = cfg or config(self.clock)
        self.standing(cfg, **standing)
        return self.runner(cfg)

    def head(self, date: str = DATE, *, dry: bool = False) -> dict[str, Any]:
        head: dict[str, Any] = self.store.head(
            self.scope, "nightly-run", f"night-{date}" + ("-dry" if dry else "")
        )
        return head

    def records(self, kind: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return list(self.store.list_objects(self.scope, kind))

    def exploratory_plan(self, tag: str, **over: Any) -> dict[str, Any]:
        purpose, split = over.get("purpose", "exploratory"), over.get("split", "development")
        analysis = self.put("analysis-plan", f"an-{tag}", {"policy": {"purpose": purpose}})
        sampling = self.put("sampling-plan", f"sa-{tag}", {"split": split, "cell_id": CELL})
        proposal = self.put("proposal-stub", f"pr-{tag}", {})
        budget = over.get("budget", dict(MAX_BUDGET))
        return {
            "scope": self.scope.wire(), "proposal_ref": proposal, "analysis_plan_ref": analysis,
            "sampling_plan_ref": sampling, "budget": budget,
        }  # fmt: skip


@pytest.fixture
def env(product: Any) -> Env:  # noqa: F811
    dep, _client, _token = product
    return Env(dep)


def stopped(env: Env, runner: NightlyRunner, date: str = DATE, **kw: Any) -> Hold:
    with pytest.raises(Hold) as caught:
        runner.run(date, **kw)
    assert caught.value.code == "NIGHT_STOPPED" and caught.value.outcome == "hold"
    return caught.value


def phases(data: dict[str, Any]) -> dict[str, str]:
    """phase -> the last state recorded for it."""
    return {p["phase"]: p["state"] for p in data["phases"]}


def first(env: Env, name: str) -> int:
    return env.fake.names().index(name)


# --- the happy night ----------------------------------------------------------------------------


def test_a_night_runs_the_phases_in_order_and_records_the_run(env: Env) -> None:
    runner = env.ready()
    ref = runner.run(DATE)
    fake = env.fake
    # call order follows the phases: preflight, drift, screening design, search, dreaming, dashboard
    order = [first(env, n) for n in ("preflight", "snapshot", "drift_run", "screening_run",
                                     "propose", "search_run", "dream", "dashboard")]  # fmt: skip
    assert order == sorted(order) and len(set(order)) == len(order)
    assert [p["phase"] for p in env.head()["data"]["phases"]] == list(PHASES)
    assert PHASES == ("preflight", "drift", "screening_design", "search", "confirmation",
                      "dreaming", "dashboard")  # fmt: skip
    # the returned ref is the nightly-run record of the night
    record = env.store.get(env.scope, "nightly-run", ref)
    assert ref["id"] == f"night-{DATE}" and record["state"] == "finished"
    head = env.head()
    assert head["state"] == "finished" and head["data"]["stopped"] is None
    assert head["data"]["phase"] == "dashboard" and head["data"]["finished_at"]
    assert head["data"]["trials"] == 5 + 12 + 48  # drift + PB12 screening + 4 + 4*2... see rungs
    assert phases(head["data"]) == {
        "preflight": "done", "drift": "done", "screening_design": "done", "search": "done",
        "confirmation": "skipped", "dreaming": "done", "dashboard": "done",
    }  # fmt: skip
    assert fake.args("dashboard") == [()]


def test_the_night_head_has_the_documented_shape(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    data = env.head()["data"]
    for key in ("plan_ref", "phase", "trials", "stopped", "drift", "queued_confirmations",
                "proposals", "dreaming_ref", "finished_at"):  # fmt: skip
        assert key in data, key
    plan = env.store.get(env.scope, "nightly-plan", data["plan_ref"])
    assert data["plan_ref"]["id"] == f"nightplan-{DATE}"
    assert data["drift"][CELL]["status"] == "ok"
    assert data["dreaming_ref"]["id"] == f"dream-{CELL}"
    assert "prop-1" in data["proposals"] and "proposal-c4" in data["proposals"]
    assert plan["date"] == DATE


def test_the_nightly_plan_record_has_the_canonical_shape(env: Env) -> None:
    standing = env.standing()
    runner = env.runner()
    ref = runner.plan_night(DATE)
    plan = env.store.get(env.scope, "nightly-plan", ref)
    assert ref["id"] == f"nightplan-{DATE}"
    assert plan["date"] == DATE and plan["budget_trials"] == 200
    assert plan["shares"] == SHARES and plan["cells"] == [CELL]
    assert plan["quota_ref"] is None and plan["pilot"] is True
    assert plan["standing_ref"] == {k: standing[k] for k in ("id", "revision", "digest")}
    assert plan["created_at"]
    shares = plan["shares"]
    assert shares["drift"] + shares["search"] + shares["confirmation"] == pytest.approx(1)
    assert 0 <= shares["screening_design"] <= shares["search"]


def test_a_night_derives_the_phase_budgets_from_the_shares(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    caps = phase_budget(200, SHARES, screening=True)
    assert caps == {"drift": 20, "screening_design": 20, "search": 100, "confirmation": 60}
    # the drift unit was given at most the drift share
    assert env.fake.args("drift_run")[0][-1] <= caps["drift"]


def test_screening_design_runs_pb12_with_one_unit_per_cell_on_the_first_night(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    (configs,) = env.fake.screening
    design = plackett_burman_12()
    assert len(configs) == 12 and all(len(c) == 11 for c in configs)
    assert configs == [
        {f"layer{i}": ("on" if row[i] > 0 else "off") for i in range(11)} for row in design
    ]
    unit = next(p for p in env.head()["data"]["phases"] if p["phase"] == "screening_design")
    assert unit["design"] == "PB12" and unit["units"][0]["fold_over"] is False


def test_the_next_screening_night_uses_the_fold_over_mirror(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    runner.run("2026-10-02")
    first_night, second_night = env.fake.screening
    mirror = fold_over(plackett_burman_12())[12:]
    assert second_night == [
        {f"layer{i}": ("on" if row[i] > 0 else "off") for i in range(11)} for row in mirror
    ]
    assert all(
        a[f] != b[f] for a, b in zip(first_night, second_night, strict=True) for f in a
    )  # every factor flips
    # the second night's plan points at the first night's quota history (none recorded: no run)
    assert env.head("2026-10-02")["data"]["phases"][2]["units"][0]["fold_over"] is True


def test_screening_is_skipped_when_the_operator_set_its_share_to_zero(env: Env) -> None:
    cfg = config(env.clock, shares=dict(SHARES_NO_SCREEN))
    runner = env.ready(cfg)
    runner.run(DATE)
    assert "screening_run" not in env.fake.names() and "screening_factors" not in env.fake.names()
    state = next(p for p in env.head()["data"]["phases"] if p["phase"] == "screening_design")
    assert (state["state"], state["reason"]) == ("skipped", "share_zero")
    # the share was never taken out of search
    assert phase_budget(200, SHARES_NO_SCREEN, screening=False)["screening_design"] == 0


class DesignRows:
    """The candidates `LocalNightlyBackend.screening_run` reads (`_candidates`, `_factors_of`,
    `search_run`), scripted: the first `buildable` PB12 rows are waiting screened candidates whose
    screening stage costs `trials` trials (`_need`: Hold NIGHT_STOPPED phase_share when it needs
    more than the rest of the share); `before(n)` runs before each row, `n` = rows run so far."""

    def __init__(self, buildable: int, trials: int = 12) -> None:
        design = plackett_burman_12()
        self.on = [frozenset(f"layer{i}" for i in range(11) if row[i] > 0) for row in design]
        self.buildable, self.trials = buildable, trials
        self.ran: list[str] = []
        self.before: Callable[[int], None] | None = None

    def _candidates(self, cell_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [(f"row-{i}", {"on": self.on[i]}) for i in range(self.buildable)]

    def _factors_of(self, proposal: dict[str, Any]) -> list[str]:
        return sorted(proposal["on"])

    def search_run(self, cell_id: str, cid: str, tasks: int, guard: Any, limit: int
                   ) -> dict[str, Any]:  # fmt: skip
        if self.before is not None:
            self.before(len(self.ran))
        if self.trials > limit:
            raise Hold("NIGHT_STOPPED", "share", details={"reason": "phase_share",
                                                          "needs": self.trials})  # fmt: skip
        guard(self.trials)
        self.ran.append(cid)
        return {"trials": self.trials, "passes": 6, "runs": 6, "unknown_effects": 0,
                "stopped": None, "root": cid}  # fmt: skip


def with_design_rows(env: Env, rows: DesignRows) -> None:
    def screening_run(cell_id: str, configs: list[dict[str, str]], guard: Any, limit: int
                      ) -> dict[str, Any]:  # fmt: skip
        env.fake.calls.append(("screening_run", cell_id, len(configs)))
        return nightly_module.LocalNightlyBackend.screening_run(
            rows,  # type: ignore[arg-type]
            cell_id, configs, guard, limit,
        )  # fmt: skip

    env.fake.screening_run = screening_run  # type: ignore[method-assign]


def test_a_design_row_over_the_share_keeps_the_trials_of_the_rows_before_it(env: Env) -> None:
    """Screening cap 20 of 200: row 0 runs 12 trials, row 1 needs 12 of the 8 left (phase share).
    The unit ends there and the night counts row 0's trials (they were lost before)."""
    rows = DesignRows(buildable=3)
    with_design_rows(env, rows)
    runner = env.ready()
    runner.run(DATE)
    data = env.head()["data"]
    assert rows.ran == ["row-0"]
    unit = next(p for p in data["phases"] if p["phase"] == "screening_design")["units"][0]
    assert unit["trials"] == 12 and [r["row"] for r in unit["rows"]] == [0]
    search = sum(t for _cid, t in env.fake.args("search_run"))
    assert data["trials"] == env.fake.drift_batch["trials"] + 12 + search
    assert data["stopped"] is None and data["findings"] == []


def test_a_night_stop_on_a_later_design_row_still_counts_the_rows_that_ran(env: Env) -> None:
    rows = DesignRows(buildable=3, trials=6)

    def kill_before_the_third_row(ran: int) -> None:
        if ran == 2:
            env.fake.kill = True

    rows.before = kill_before_the_third_row
    with_design_rows(env, rows)
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "kill_switch"
    data = env.head()["data"]
    assert rows.ran == ["row-0", "row-1"]
    assert data["trials"] == env.fake.drift_batch["trials"] + 12
    assert data["stopped"] == "kill_switch"
    assert phases(data)["screening_design"] == "stopped"


def test_a_stage_hold_on_a_later_design_row_is_a_finding_and_keeps_the_earlier_trials(
    env: Env,
) -> None:
    rows = DesignRows(buildable=3, trials=6)

    def refuse_the_second_row(ran: int) -> None:
        if ran == 1:
            raise Hold("META_TOKEN_BUDGET", "The root budget cannot cover the screening stage")

    rows.before = refuse_the_second_row
    with_design_rows(env, rows)
    runner = env.ready()
    runner.run(DATE)
    data = env.head()["data"]
    assert rows.ran == ["row-0"]
    assert f"SCREENING_DESIGN {CELL}: META_TOKEN_BUDGET" in data["findings"]
    unit = next(p for p in data["phases"] if p["phase"] == "screening_design")["units"][0]
    assert unit["trials"] == 6
    search = sum(t for _cid, t in env.fake.args("search_run"))
    assert data["trials"] == env.fake.drift_batch["trials"] + 6 + search


def test_search_ranks_by_the_surrogate_then_halves_4_8_16(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    unit = next(p for p in env.head()["data"]["phases"] if p["phase"] == "search")["units"][0]
    assert [r["candidate_id"] for r in unit["ranking"]] == SURROGATE_ORDER
    scores = [r["score"] for r in unit["ranking"]]
    assert scores == sorted(scores, reverse=True)
    assert unit["rungs"] == [
        {"rung": 0, "tasks": 4, "count": 4},
        {"rung": 1, "tasks": 8, "count": 2},
        {"rung": 2, "tasks": 16, "count": 1},
    ]
    runs = [c[1:] for c in env.fake.calls if c[0] == "search_run"]
    # rung 0 in surrogate order; rung 1 keeps the best measured pass rates; rung 2 the best of those
    assert runs == [("c4", 4), ("c2", 4), ("c3", 4), ("c1", 4), ("c4", 8), ("c2", 8), ("c4", 16)]
    assert [(r["rung"], r["candidate_id"]) for r in unit["results"]] == [
        (0, "c4"), (0, "c2"), (0, "c3"), (0, "c1"), (1, "c4"), (1, "c2"), (2, "c4"),
    ]  # fmt: skip


def test_the_proposer_drafts_before_the_search_and_its_proposals_are_recorded(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    assert first(env, "propose") < first(env, "search_candidates") < first(env, "search_run")
    unit = next(p for p in env.head()["data"]["phases"] if p["phase"] == "search")["units"][0]
    assert unit["drafted"] == ["prop-1"]


def test_a_refused_proposer_is_a_finding_and_the_night_goes_on(env: Env) -> None:
    env.fake.propose_error = Hold("PROPOSER_BUDGET", "no budget")
    runner = env.ready()
    runner.run(DATE)
    data = env.head()["data"]
    assert f"PROPOSER {CELL}: PROPOSER_BUDGET" in data["findings"]
    assert data["stopped"] is None and "search_run" in env.fake.names()


def test_a_promising_candidate_is_queued_for_the_operator_and_never_run(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    assert env.fake.args("queue_confirmation") == [(CELL, "c4")]
    data = env.head()["data"]
    assert [q["id"] for q in data["queued_confirmations"]] == ["exp-c4"]
    assert "confirmation_run" not in env.fake.names()
    state = next(p for p in data["phases"] if p["phase"] == "confirmation")
    assert (state["state"], state["reason"]) == ("skipped", "none_approved")


def test_confirmation_runs_only_experiments_the_operator_approved(env: Env) -> None:
    approved = {"id": "exp-approved", "revision": 1, "digest": "sha256:" + "b" * 64}
    env.fake.confirm_items = [approved]
    runner = env.ready()
    runner.run(DATE)
    assert env.fake.args("confirmation_run") == [("exp-approved",)]
    # the experiment the night itself queued is not run the same night
    assert env.fake.args("queue_confirmation") == [(CELL, "c4")]
    assert env.fake.names().index("confirmation_run") > env.fake.names().index("search_run")
    state = next(p for p in env.head()["data"]["phases"] if p["phase"] == "confirmation")
    assert state["state"] == "done" and state["items"][0]["trials"] == 3


def test_without_an_approved_experiment_the_confirmation_share_returns_to_search(
    env: Env,
) -> None:
    # B=20: search share 12, confirmation share 6; the returned share lets a 4th 4-task unit run
    cfg = config(env.clock, budget_trials=20, shares=dict(SHARES_NO_SCREEN), drift_tasks=())
    runner = env.ready(cfg, budget_trials=20)
    runner.run(DATE)
    assert env.head()["data"]["trials"] == 16  # more than the search share alone (12), at most 18


def test_with_an_approved_experiment_search_keeps_its_own_share(env: Env) -> None:
    env.fake.confirm_items = [{"id": "exp-1", "revision": 1, "digest": "sha256:" + "b" * 64}]
    cfg = config(env.clock, budget_trials=20, shares=dict(SHARES_NO_SCREEN), drift_tasks=())
    runner = env.ready(cfg, budget_trials=20)
    runner.run(DATE)
    assert env.head()["data"]["trials"] == 12  # exactly the search share: nothing returned


def test_the_quota_observation_of_the_night_is_recorded(env: Env) -> None:
    driver = env.put("driver-capabilities", "cap-codex", {"driver_id": CELL})
    env.put("run-record", "run-1", {
        "driver_profile_ref": driver, "finished_at": iso(time.time() - 60),
        "usage": {"input_tokens": 500, "output_tokens": 50},
    })  # fmt: skip
    runner = env.ready()
    runner.run(DATE)
    ((ref, value),) = env.records("quota-observation")
    assert ref["id"] == f"quota-{CELL}-{DATE}" and value["night"] == DATE
    assert value["windows"][0]["input_tokens"] == 500
    assert env.head()["data"]["quota_refs"] == [ref]
    # the next night's plan points at it
    plan = env.store.get(env.scope, "nightly-plan", runner.plan_night("2026-10-05"))
    assert plan["quota_ref"]["id"] == f"quota-{CELL}-{DATE}"


def test_a_night_cannot_run_twice(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    with pytest.raises(Hold) as again:
        runner.run(DATE)
    assert again.value.code == "NIGHT_STATE"
    assert env.fake.names().count("dream") == 1


def test_the_pilot_flag_ends_after_the_pilot_nights(env: Env) -> None:
    runner = env.ready()
    assert env.store.get(env.scope, "nightly-plan", runner.plan_night("2026-10-01"))["pilot"]
    for n in range(3):
        env.put("nightly-run", f"night-2026-09-2{n}", {
            "date": f"2026-09-2{n}", "dry_run": False, "finished_at": f"2026-09-2{n}T06:00:00Z",
        })  # fmt: skip
    assert (
        env.store.get(env.scope, "nightly-plan", runner.plan_night("2026-10-02"))["pilot"] is False
    )


# --- dry run ------------------------------------------------------------------------------------


def test_a_dry_run_plans_every_phase_and_dispatches_nothing(env: Env) -> None:
    env.fake.confirm_items = [{"id": "exp-1", "revision": 1, "digest": "sha256:" + "b" * 64}]
    runner = env.ready()
    ref = runner.run(DATE, dry_run=True)
    assert ref["id"] == f"night-{DATE}-dry"
    names = set(env.fake.names())
    assert not names & {
        "drift_run",
        "screening_run",
        "propose",
        "search_run",
        "queue_confirmation",
        "confirmation_run",
        "dream",
        "schedule_recalibration",
        "removal_sweep",
    }
    assert {"preflight", "snapshot", "screening_factors", "search_candidates", "search_rows",
            "confirmations", "dashboard"} <= names  # fmt: skip
    data = env.head(dry=True)["data"]
    assert data["dry_run"] is True and data["trials"] == 0 and data["stopped"] is None
    assert phases(data) == {
        "preflight": "done", "drift": "done", "screening_design": "planned",
        "search": "planned", "confirmation": "planned", "dreaming": "skipped",
        "dashboard": "done",
    }  # fmt: skip
    assert [p["phase"] for p in data["phases"]] == list(PHASES)
    unit = next(p for p in data["phases"] if p["phase"] == "search")["units"][0]
    assert [r["candidate_id"] for r in unit["ranking"]] == SURROGATE_ORDER
    assert [r["tasks"] for r in unit["rungs"]] == [4, 8, 16]
    screening = next(p for p in data["phases"] if p["phase"] == "screening_design")
    assert screening["units"][0]["runs"] == 12 and screening["design"] == "PB12"


def test_a_dry_run_writes_dry_records_derives_no_approval_and_no_quota_observation(
    env: Env,
) -> None:
    driver = env.put("driver-capabilities", "cap-codex", {"driver_id": CELL})
    env.put("run-record", "run-1", {
        "driver_profile_ref": driver, "finished_at": iso(time.time() - 60),
        "usage": {"input_tokens": 5, "output_tokens": 5},
    })  # fmt: skip
    runner = env.ready()
    runner.run(DATE, dry_run=True)
    assert [r["id"] for r, _v in env.records("nightly-plan")] == [f"nightplan-{DATE}-dry"]
    assert [r["id"] for r, _v in env.records("nightly-run")] == [f"night-{DATE}-dry"]
    assert env.records("quota-observation") == []
    actions = {v["action"] for _r, v in env.records(APPROVAL_KIND)}
    assert actions == {"nightly.explore"}
    # the real night of the same date is still free
    runner.run(DATE)
    assert env.head()["state"] == "finished"


def test_a_dry_run_can_be_repeated(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE, dry_run=True)
    runner.run(DATE, dry_run=True)
    assert sorted(r["revision"] for r, _v in env.records("nightly-plan")) == [1, 2]


def test_a_dry_run_without_a_standing_approval_holds(env: Env) -> None:
    runner = env.runner()
    held = stopped(env, runner, dry_run=True)
    assert held.details["reason"] == "no_standing_approval"
    assert env.fake.calls == [] and env.records("nightly-plan") == []


# --- drift --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["provider_model_id", "driver_version", "image"])
def test_a_changed_model_driver_or_image_stops_search_and_schedules_recalibration(
    env: Env, field: str
) -> None:
    env.fake.snaps[CELL] = {
        "current": {**SNAP, field: "changed"}, "calibrated": SNAP,
        "band": {"passes": 8, "runs": 10},
    }  # fmt: skip
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "drift" and held.details["run_ref"]["id"] == f"night-{DATE}"
    names = set(env.fake.names())
    assert not names & {"drift_run", "screening_run", "propose", "search_run", "search_candidates",
                        "confirmations", "confirmation_run", "queue_confirmation"}  # fmt: skip
    assert env.fake.args("schedule_recalibration")[0][0] == CELL
    assert "drift" in env.fake.args("schedule_recalibration")[0][1]
    assert env.fake.args("removal_sweep") == [(CELL, env.fake.args("schedule_recalibration")[0][1])]
    data = env.head()["data"]
    assert data["stopped"] == "drift" and env.head()["state"] == "stopped"
    assert data["drift"][CELL]["status"] == "changed" and data["drift"][CELL]["changed"] == [field]
    reason = env.fake.args("schedule_recalibration")[0][1]
    assert data["recalibration"] == [{"cell_id": CELL, "scheduled": True, "reason": reason}]
    assert data["proposals"] == [f"sweep-{CELL}"]
    assert phases(data) == {
        "preflight": "done", "drift": "drift", "screening_design": "skipped",
        "search": "skipped", "confirmation": "skipped", "dreaming": "done", "dashboard": "done",
    }  # fmt: skip
    assert data["trials"] == 0


def binomial_cdf(k: int, n: int, p: float) -> float:
    from math import comb

    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def test_the_drift_range_is_the_99_percent_binomial_prediction_at_the_wilson_ends() -> None:
    """Clarification after S12: drift iff k < Bin^-1(n, p_lo)(0.005) or k > Bin^-1(n, p_hi)
    (0.995), (p_lo, p_hi) the calibrated 95 % Wilson interval."""
    low, high = wilson(8, 10, Z95)
    for n in (1, 5, 10, 20):
        lower, upper = nightly_module.drift_range(n, (low, high))
        # the smallest k whose CDF reaches the quantile, at each end
        assert binomial_cdf(lower, n, low) >= 0.005
        assert lower == 0 or binomial_cdf(lower - 1, n, low) < 0.005
        assert binomial_cdf(upper, n, high) >= 0.995
        assert upper == 0 or binomial_cdf(upper - 1, n, high) < 0.995
    # small n: any count of 5 trials is inside a band of 8/10 (no stop by chance)
    assert nightly_module.drift_range(5, (low, high)) == (0, 5)
    assert nightly_module.drift_range(10, (low, high))[0] == 1
    assert nightly_module.binomial_quantile(0, 0.5, 0.5) == 0
    assert nightly_module.binomial_quantile(4, 1.0, 0.005) == 4
    assert nightly_module.binomial_quantile(4, 0.0, 0.995) == 0
    with pytest.raises(RuntimeFault):
        nightly_module.binomial_quantile(-1, 0.5, 0.5)


def test_a_pass_count_outside_the_calibrated_wilson_band_stops_search(env: Env) -> None:
    env.fake.drift_batch = {"trials": 10, "passes": 0, "runs": 10}
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "drift"
    check = env.head()["data"]["drift"][CELL]
    assert check["status"] == "outside_band" and check["outside_band"] is True
    assert check["observed"] == {"passes": 0, "runs": 10}
    assert check["band"]["wilson_95"] == pytest.approx(list(wilson(8, 10, Z95)))
    assert check["prediction_99"] == [1, 10]
    assert "search_run" not in env.fake.names() and "propose" not in env.fake.names()
    assert env.fake.args("schedule_recalibration")[0][1].startswith("drift outside_band")
    assert env.fake.args("removal_sweep")
    assert env.head()["data"]["trials"] == 10  # the drift trials still count


def test_a_small_drift_sample_inside_the_prediction_range_does_not_stop_the_night(
    env: Env,
) -> None:
    """0 of 5 is far from a band of 8/10, but Bin(5, p_lo) puts 0 inside the 99 % range."""
    env.fake.drift_batch = {"trials": 5, "passes": 0, "runs": 5}
    runner = env.ready()
    runner.run(DATE)
    check = env.head()["data"]["drift"][CELL]
    assert check["status"] == "ok" and check["outside_band"] is False
    assert check["prediction_99"] == [0, 5]
    assert "search_run" in env.fake.names()


def test_a_pass_count_above_the_band_is_outside_it_too(env: Env) -> None:
    env.fake.snaps[CELL] = {"current": SNAP, "calibrated": SNAP, "band": {"passes": 2, "runs": 10}}
    env.fake.drift_batch = {"trials": 10, "passes": 10, "runs": 10}  # vs a band below 0.52
    runner = env.ready()
    assert stopped(env, runner).details["reason"] == "drift"
    assert env.head()["data"]["drift"][CELL]["prediction_99"][1] == 9


def test_a_perfect_drift_run_against_a_saturated_band_is_inside_it(env: Env) -> None:
    """wilson(4, 4) has an upper bound of 1 - 1e-16 in floats; 5/5 is still inside it."""
    assert wilson(4, 4, Z95)[1] < 1.0
    env.fake.snaps[CELL] = {"current": SNAP, "calibrated": SNAP, "band": {"passes": 4, "runs": 4}}
    env.fake.drift_batch = {"trials": 5, "passes": 5, "runs": 5}
    runner = env.ready()
    runner.run(DATE)
    check = env.head()["data"]["drift"][CELL]
    assert check["status"] == "ok" and check["outside_band"] is False


def test_a_pass_count_inside_the_band_lets_the_night_search(env: Env) -> None:
    low, high = wilson(8, 10, Z95)
    env.fake.drift_batch = {"trials": 5, "passes": 4, "runs": 5}
    assert low <= 0.8 <= high
    runner = env.ready()
    runner.run(DATE)
    check = env.head()["data"]["drift"][CELL]
    assert check["status"] == "ok" and check["outside_band"] is False
    assert check["observed"] == {"passes": 4, "runs": 5}
    assert "search_run" in env.fake.names() and env.fake.args("schedule_recalibration") == []


def test_drift_check_reports_status_without_running_a_trial(env: Env) -> None:
    runner = env.ready()
    ok = runner.drift_check(CELL)
    assert ok["status"] == "ok" and ok["changed"] == [] and ok["snapshot"] == SNAP
    assert ok["band"]["wilson_95"] == pytest.approx(list(wilson(8, 10, Z95)))
    env.fake.snaps[CELL] = {"current": SNAP, "calibrated": None, "band": None}
    unknown = runner.drift_check(CELL)
    assert unknown["status"] == "uncalibrated" and unknown["band"] is None
    env.fake.snaps[CELL] = {"current": {**SNAP, "image": "x"}, "calibrated": SNAP, "band": None}
    assert runner.drift_check(CELL)["changed"] == ["image"]
    assert "drift_run" not in env.fake.names()


def test_an_uncalibrated_cell_is_not_drift(env: Env) -> None:
    env.fake.snaps[CELL] = {"current": SNAP, "calibrated": None, "band": None}
    runner = env.ready()
    runner.run(DATE)
    assert env.head()["data"]["drift"][CELL]["status"] == "uncalibrated"
    assert env.head()["data"]["stopped"] is None and "search_run" in env.fake.names()


def test_one_drifted_cell_stops_the_search_of_all_cells(env: Env) -> None:
    env.fake.snaps[OTHER_CELL] = {"current": {**SNAP, "driver_version": "9"}, "calibrated": SNAP,
                                  "band": None}  # fmt: skip
    cfg = config(env.clock, cells=(CELL, OTHER_CELL))
    runner = env.ready(cfg)
    assert stopped(env, runner).details["reason"] == "drift"
    assert [a[0] for a in env.fake.args("schedule_recalibration")] == [OTHER_CELL]
    assert env.fake.args("drift_run") and env.fake.args("drift_run")[0][0] == CELL
    assert "search_run" not in env.fake.names()
    data = env.head()["data"]
    assert (
        data["drift"][CELL]["status"] == "ok" and data["drift"][OTHER_CELL]["status"] == "changed"
    )


def test_a_drift_stop_in_a_dry_run_schedules_nothing(env: Env) -> None:
    env.fake.snaps[CELL] = {"current": {**SNAP, "image": "x"}, "calibrated": SNAP, "band": None}
    runner = env.ready()
    held = stopped(env, runner, dry_run=True)
    assert held.details["reason"] == "drift"
    assert env.fake.args("schedule_recalibration") == [] and env.fake.args("removal_sweep") == []
    assert env.head(dry=True)["data"]["recalibration"] == [{"cell_id": CELL, "dry_run": True}]


NEXT, LATER = "2026-10-02", "2026-10-03"


def test_a_drifted_cell_stays_stopped_on_later_nights_until_the_operator_recalibrates(
    env: Env,
) -> None:
    """§8.8: drift stops search and schedules re-calibration; only the operator's re-calibration
    re-baselines (plan.md §9), so a later night does not search against a stale or self-made
    baseline."""
    env.fake.drift_batch = {"trials": 10, "passes": 0, "runs": 10}
    runner = env.ready()
    assert stopped(env, runner).details["reason"] == "drift"
    # night 2: the baseline is still the operator's calibration of before night 1
    env.fake.snaps[CELL] = {"current": SNAP, "calibrated": SNAP, "band": {"passes": 8, "runs": 10},
                            "calibrated_at": iso(time.time() - DAY)}  # fmt: skip
    env.fake.calls.clear()
    held = stopped(env, runner, NEXT)
    assert held.details["reason"] == "drift"
    data = env.head(NEXT)["data"]
    check = data["drift"][CELL]
    assert check["status"] == "recalibration_pending"
    assert check["recalibration_pending_since"] == DATE
    assert not set(env.fake.names()) & {"drift_run", "schedule_recalibration", "removal_sweep",
                                        "propose", "search_run", "screening_run"}  # fmt: skip
    assert data["recalibration"] == [{"cell_id": CELL, "pending_since": DATE}]
    assert data["trials"] == 0 and phases(data)["search"] == "skipped"
    # night 3: an operator calibration summarized after night 1 started answers it
    env.fake.snaps[CELL] = {**env.fake.snaps[CELL], "calibrated_at": nightly_module.now()}
    env.fake.drift_batch = {"trials": 5, "passes": 4, "runs": 5}
    env.fake.calls.clear()
    runner.run(LATER)
    data = env.head(LATER)["data"]
    assert data["drift"][CELL]["status"] == "ok" and data["stopped"] is None
    assert "drift_run" in env.fake.names() and "search_run" in env.fake.names()


def test_a_baseline_without_a_known_time_keeps_the_recalibration_pending(env: Env) -> None:
    env.fake.snaps[CELL] = {"current": {**SNAP, "image": "x"}, "calibrated": SNAP, "band": None}
    runner = env.ready()
    assert stopped(env, runner).details["reason"] == "drift"
    env.fake.snaps[CELL] = {"current": SNAP, "calibrated": SNAP, "band": {"passes": 8, "runs": 10}}
    assert stopped(env, runner, NEXT).details["reason"] == "drift"
    assert env.head(NEXT)["data"]["drift"][CELL]["status"] == "recalibration_pending"


def test_a_dry_run_drift_leaves_no_recalibration_pending(env: Env) -> None:
    env.fake.snaps[CELL] = {"current": {**SNAP, "image": "x"}, "calibrated": SNAP, "band": None}
    runner = env.ready()
    stopped(env, runner, dry_run=True)
    env.fake.snaps.clear()
    runner.run(NEXT)
    assert env.head(NEXT)["data"]["drift"][CELL]["status"] == "ok"


def test_a_refused_removal_sweep_is_a_finding(env: Env) -> None:
    env.fake.snaps[CELL] = {"current": {**SNAP, "image": "x"}, "calibrated": SNAP, "band": None}

    def refuse(cell_id: str, reason: str) -> list[str]:
        raise Hold("SWEEP_BUSY", "busy")

    env.fake.removal_sweep = refuse  # type: ignore[method-assign]
    runner = env.ready()
    assert stopped(env, runner).details["reason"] == "drift"
    assert f"REMOVAL_SWEEP {CELL}: SWEEP_BUSY" in env.head()["data"]["findings"]


# --- stops: budget, stop_at, quota, kill switch, preflight, errors ------------------------------


def test_the_budget_ends_the_night_normally_and_no_trial_goes_past_it(env: Env) -> None:
    cfg = config(env.clock, budget_trials=10, shares={"drift": 0.0, "screening_design": 0.0,
                                                      "search": 1.0, "confirmation": 0.0},
                 drift_tasks=())  # fmt: skip
    runner = env.ready(cfg, budget_trials=10)
    ref = runner.run(DATE)  # a budget stop is not a Hold
    data = env.head()["data"]
    assert data["stopped"] == "budget" and data["trials"] == 8 <= 10
    assert env.head()["state"] == "finished"
    assert env.store.get(env.scope, "nightly-run", ref)["state"] == "finished"
    assert [c[1:] for c in env.fake.calls if c[0] == "search_run"] == [("c4", 4), ("c2", 4),
                                                                       ("c3", 4)]  # fmt: skip
    # the third unit was refused by the guard before it dispatched: the fake never took its trials
    assert phases(data)["search"] == "stopped"
    assert phases(data)["dreaming"] == "done" and phases(data)["dashboard"] == "done"


def test_a_zero_budget_runs_no_trial_and_ends_on_budget(env: Env) -> None:
    cfg = config(env.clock, budget_trials=0)
    runner = env.ready(cfg, budget_trials=0)
    runner.run(DATE)
    data = env.head()["data"]
    assert data["trials"] == 0 and data["stopped"] == "budget"  # the first guard refuses


def test_the_drift_share_is_a_cap_on_the_drift_unit(env: Env) -> None:
    cfg = config(env.clock, budget_trials=30, shares=dict(SHARES_NO_SCREEN))  # drift share 3
    runner = env.ready(cfg, budget_trials=30)
    runner.run(DATE)  # the fake drift unit asks 5 trials > 3: refused as a share, not a stop
    data = env.head()["data"]
    assert data["trials"] <= 30
    assert f"DRIFT {CELL}: PHASE_SHARE" in data["findings"]
    assert data["drift"][CELL]["observed"] is None


def test_stop_at_ends_the_night_at_the_next_unit(env: Env) -> None:
    cfg = config(env.clock, stop_at=hhmm(env.clock.t + 3600))
    env.fake.step = 7200  # the first search unit runs past stop_at
    runner = env.ready(cfg)
    held = stopped(env, runner)
    assert held.details["reason"] == "stop_at"
    assert len(env.fake.args("search_run")) == 1
    data = env.head()["data"]
    assert data["stopped"] == "stop_at" and env.head()["state"] == "stopped"
    assert data["trials"] == 5 + 12 + 4  # what ran before the stop is accounted
    assert phases(data)["dreaming"] == "skipped" and phases(data)["dashboard"] == "done"
    assert "dream" not in env.fake.names() and "dashboard" in env.fake.names()
    assert env.fake.args("queue_confirmation") == []


def test_stop_epoch_is_the_first_local_stop_time_after_the_start() -> None:
    start = datetime(2026, 10, 1, 1, 0, 0).astimezone().timestamp()
    at = stop_epoch(start, "07:00")
    assert datetime.fromtimestamp(at).strftime("%H:%M:%S") == "07:00:00"
    assert 0 < at - start <= DAY and at - start == pytest.approx(6 * 3600, abs=3700)
    # at exactly the stop time the next one is a day away
    assert stop_epoch(at, "07:00") == pytest.approx(at + DAY, abs=3700)
    with pytest.raises(RuntimeFault) as fault:
        stop_epoch(start, "7am")
    assert fault.value.code == "NIGHTLY_CONFIG"


def test_a_quota_signal_stops_the_night_at_the_next_unit(env: Env) -> None:
    def signal(fake: Fake, cid: str, tasks: int) -> None:
        fake.quota = True

    env.fake.after_search = signal
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "quota_signal"
    assert len(env.fake.args("search_run")) == 1
    data = env.head()["data"]
    assert data["stopped"] == "quota_signal" and phases(data)["dreaming"] == "skipped"
    assert env.fake.quota_since == pytest.approx(env.clock.t, abs=2 * 3600)


def test_a_quota_signal_before_the_first_trial_dispatches_nothing(env: Env) -> None:
    env.fake.quota = True
    start = env.clock.t
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "quota_signal"
    assert not {"drift_run", "screening_run", "search_run"} & set(env.fake.names())
    assert env.fake.quota_since == start
    assert env.head()["data"]["trials"] == 0


def test_the_kill_switch_ends_the_night_before_any_phase(env: Env) -> None:
    env.fake.kill = True
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "kill_switch"
    assert "preflight" not in env.fake.names() and "snapshot" not in env.fake.names()
    assert phases(env.head()["data"])["preflight"] == "stopped"


def test_the_kill_switch_pulled_mid_night_stops_at_the_next_unit(env: Env) -> None:
    def pull(fake: Fake, cid: str, tasks: int) -> None:
        fake.kill = True

    env.fake.after_search = pull
    runner = env.ready()
    assert stopped(env, runner).details["reason"] == "kill_switch"
    assert len(env.fake.args("search_run")) == 1


def test_a_failed_preflight_stops_the_night_before_drift(env: Env) -> None:
    env.fake.problems = [{"code": "EVALUATOR_UNQUALIFIED"}]
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "preflight"
    assert "snapshot" not in env.fake.names() and "drift_run" not in env.fake.names()
    data = env.head()["data"]
    assert data["stop_details"] == [{"code": "EVALUATOR_UNQUALIFIED"}]
    assert phases(data)["preflight"] == "stopped"
    assert "dream" not in env.fake.names()  # no model turn after a failed preflight


def test_a_stopped_night_still_records_quota_and_closes_its_run(env: Env) -> None:
    driver = env.put("driver-capabilities", "cap-codex", {"driver_id": CELL})
    env.put("run-record", "run-1", {
        "driver_profile_ref": driver, "finished_at": iso(time.time() - 60),
        "usage": {"input_tokens": 5, "output_tokens": 5},
    })  # fmt: skip
    env.fake.quota = True
    runner = env.ready()
    held = stopped(env, runner)
    assert [r["id"] for r, _v in env.records("quota-observation")] == [f"quota-{CELL}-{DATE}"]
    record = env.store.get(env.scope, "nightly-run", held.details["run_ref"])
    assert record["state"] == "stopped" and record["finished_at"]


def test_a_backend_that_returns_a_bad_batch_is_a_rejected_fault_and_the_night_is_closed(
    env: Env,
) -> None:
    env.fake.bad_batch = True
    runner = env.ready()
    with pytest.raises(RuntimeFault) as fault:
        runner.run(DATE)
    assert fault.value.code == "NIGHT_BATCH" and fault.value.outcome == "rejected"
    data = env.head()["data"]
    assert data["stopped"] == "error" and env.head()["state"] == "stopped"
    assert data["finished_at"] and phases(data)["dashboard"] == "done"
    assert "dream" not in env.fake.names()


def test_a_failing_dashboard_is_recorded_not_fatal(env: Env) -> None:
    env.fake.dashboard_error = RuntimeError("disk full")
    runner = env.ready()
    runner.run(DATE)
    assert phases(env.head()["data"])["dashboard"] == "failed"
    assert env.head()["state"] == "finished"


def test_the_guard_a_unit_receives_refuses_a_bad_trial_count(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    guard = env.fake.guards[0]
    for bad in (-1, 1.5, "3", None):
        with pytest.raises(RuntimeFault) as fault:
            guard(bad)  # type: ignore[arg-type]
        assert fault.value.code == "NIGHT_GUARD"


def test_a_backend_batch_with_a_negative_count_is_rejected_before_it_is_accounted(
    env: Env,
) -> None:
    env.fake.drift_batch = {"trials": 1, "passes": 1, "runs": "x"}
    runner = env.ready()
    with pytest.raises(RuntimeFault) as fault:
        runner.run(DATE)
    assert fault.value.code == "NIGHT_BATCH"


# --- standing approval at the night level -------------------------------------------------------


def test_no_night_runs_without_a_standing_approval(env: Env) -> None:
    runner = env.runner()
    held = stopped(env, runner)
    assert held.details["reason"] == "no_standing_approval"
    assert env.fake.calls == []  # not even preflight: IC-17 is inert until the operator acts
    assert env.records("nightly-plan") == [] and env.records("nightly-run") == []
    with pytest.raises(RuntimeFault) as no_head:
        env.head()
    assert no_head.value.code == "NOT_FOUND"
    assert nightly_module.__doc__ and "provisional" in nightly_module.__doc__


def test_a_revoked_or_expired_standing_approval_runs_no_night(env: Env) -> None:
    standing = env.standing()
    runner = env.runner()
    env.approvals.revoke(env.operator, standing)
    assert stopped(env, runner).details["reason"] == "no_standing_approval"
    now = time.time()
    env.standing(valid_from=iso(now - 2 * DAY), valid_until=iso(now - DAY))
    assert stopped(env, runner).details["reason"] == "no_standing_approval"
    assert env.fake.calls == []


def test_a_standing_approval_revoked_mid_night_stops_it_at_the_next_trial_guard(env: Env) -> None:
    standing_ref: list[dict[str, Any]] = []

    def revoke(fake: Fake, cid: str, tasks: int) -> None:
        if fake.search_n == 2:  # after the first unit's check, before this unit's own guard
            env.approvals.revoke(env.operator, standing_ref[0])

    env.fake.before_search = revoke
    standing_ref.append(env.standing())
    runner = env.runner()
    held = stopped(env, runner)
    assert held.details["reason"] == "standing_approval"
    assert len(env.fake.args("search_run")) == 2  # the second unit's guard refused it
    data = env.head()["data"]
    assert data["stopped"] == "standing_approval" and data["trials"] == 5 + 12 + 4
    assert phases(data)["dreaming"] == "skipped"  # no further model turn after the revocation
    assert "dream" not in env.fake.names()


def test_a_standing_approval_revoked_between_units_stops_before_the_next_unit(env: Env) -> None:
    standing = env.standing()

    def revoke(fake: Fake, cid: str, tasks: int) -> None:
        env.approvals.revoke(env.operator, standing)

    env.fake.after_search = revoke
    runner = env.runner()
    assert stopped(env, runner).details["reason"] == "standing_approval"
    assert len(env.fake.args("search_run")) == 1


def test_a_standing_approval_that_expires_mid_night_stops_it(env: Env) -> None:
    def expire(fake: Fake, cid: str, tasks: int) -> None:
        env.store.clock = lambda: time.time() + 10 * DAY

    env.fake.after_search = expire
    runner = env.ready()
    assert stopped(env, runner).details["reason"] == "standing_approval"
    assert len(env.fake.args("search_run")) == 1


def test_a_backend_that_reports_a_standing_hold_after_revocation_stops_the_night(env: Env) -> None:
    standing = env.standing()

    def revoke_and_hold(fake: Fake, cid: str, tasks: int) -> None:
        env.approvals.revoke(env.operator, standing)
        raise Hold("STANDING_APPROVAL", "revoked")

    env.fake.before_search = revoke_and_hold
    runner = env.runner()
    assert stopped(env, runner).details["reason"] == "standing_approval"


def test_a_confirmatory_validation_or_over_budget_plan_is_refused_and_the_night_goes_on(
    env: Env,
) -> None:
    env.fake.plans = {
        "c4": env.exploratory_plan("conf", purpose="confirmatory"),
        "c2": env.exploratory_plan("val", split="validation"),
        "c3": env.exploratory_plan("big", budget={**MAX_BUDGET, "max_tokens": 10**9}),
        "c1": env.exploratory_plan("ok"),
    }
    for cid in ("c4", "c2", "c3"):
        env.fake.search_hold[cid] = Hold("PLACEHOLDER", "unused")  # never reached: derive refuses
    runner = env.ready()
    runner.run(DATE)
    data = env.head()["data"]
    assert data["stopped"] is None  # a refused plan is not a revoked standing approval
    for cid in ("c4", "c2", "c3"):
        assert f"SEARCH {CELL} {cid}: STANDING_APPROVAL" in data["findings"]
    # the exploratory development plan was derived under the standing approval
    c1_runs = [a for a in env.fake.args("search_run") if a[0] == "c1"]
    assert env.fake.derived and len(env.fake.derived) == len(c1_runs)
    value = env.store.get(env.scope, APPROVAL_KIND, env.fake.derived[0])
    assert value["approved_by"]["subject_id"] == NIGHTLY_ID == "amplai-meta-nightly"
    assert value["standing_ref"]["id"] and value["action"] == "experiment.execute"


def test_the_runner_derives_only_experiment_approvals_never_confirmatory_canary_or_promotion(
    env: Env,
) -> None:
    env.fake.plans = {"c1": env.exploratory_plan("ok"), "c3": env.exploratory_plan("ok2")}
    env.fake.confirm_items = [{"id": "exp-1", "revision": 1, "digest": "sha256:" + "b" * 64}]
    runner = env.ready()
    runner.run(DATE)
    kinds = {(v["action"], v["approved_by"]["kind"]) for _r, v in env.records(APPROVAL_KIND)}
    assert kinds == {("nightly.explore", "human"), ("experiment.execute", "service")}
    # the operator-approved confirmation ran under the operator's own approval, not the night's
    assert env.fake.args("confirmation_run") == [("exp-1",)]
    import inspect

    assert list(inspect.signature(runner.derive).parameters) == ["plan"]


def test_derive_outside_a_night_is_a_standing_approval_hold(env: Env) -> None:
    runner = env.ready()
    with pytest.raises(Hold) as caught:
        runner.derive(env.exploratory_plan("x"))
    assert caught.value.code == "STANDING_APPROVAL"


def test_a_plan_over_the_standing_policy_is_refused_before_anything_is_written(env: Env) -> None:
    env.standing(budget_trials=100)
    for cfg, key in (
        (config(env.clock, budget_trials=200), "budget_trials"),
        (config(env.clock, cells=(CELL, OTHER_CELL)), "cells"),
        (config(env.clock, shares=dict(SHARES_NO_SCREEN)), "shares"),
    ):
        runner = env.runner(cfg)
        with pytest.raises(Hold) as caught:
            runner.run(DATE)
        assert caught.value.code == "STANDING_APPROVAL" and key in caught.value.details
    assert env.records("nightly-plan") == [] and env.records("nightly-run") == []
    assert env.fake.calls == []


# --- unknown effect (IC-18) ---------------------------------------------------------------------


def test_an_unknown_effect_ends_the_night_and_lists_the_root_as_reconcile_pending(env: Env) -> None:
    env.fake.unknown_cid = "c2"
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "unknown_effect"
    assert held.details["reconcile_pending"] == ["proposal-xyz"]
    data = env.head()["data"]
    assert data["stopped"] == "unknown_effect" and data["reconcile_pending"] == ["proposal-xyz"]
    assert data["stop_details"] == {"root": "proposal-xyz"}
    assert len(env.fake.args("search_run")) == 2  # c4, then c2 (unknown): nothing after it
    assert data["trials"] == 5 + 12 + 4 + 4  # the trials of the unknown unit still count
    assert phases(data)["dreaming"] == "skipped" and "dream" not in env.fake.names()
    assert env.fake.args("queue_confirmation") == []


def test_unknown_effects_of_ablation_variants_list_every_variant_root(env: Env) -> None:
    """Each leave-one-out variant runs on its own budget root (`StageRunner._covers(derived_id)`):
    a confirmation batch naming `roots` lists each of them as reconcile pending (IC-18)."""
    env.fake.confirm_items = [{"id": "exp-1", "revision": 1}]

    def confirmation_run(item: dict[str, Any], guard: Any, limit: int) -> dict[str, Any]:
        env.fake.calls.append(("confirmation_run", item["id"]))
        guard(3)
        return {"trials": 3, "passes": 2, "runs": 3, "unknown_effects": 2, "root": "parent",
                "roots": ["derived-a", "derived-b"]}  # fmt: skip

    env.fake.confirmation_run = confirmation_run  # type: ignore[method-assign]
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "unknown_effect"
    data = env.head()["data"]
    assert data["reconcile_pending"] == ["derived-a", "derived-b"]
    assert data["stop_details"] == {"root": "derived-a"}


def test_an_unknown_effect_in_the_drift_unit_stops_the_night_too(env: Env) -> None:
    env.fake.drift_batch = {"trials": 5, "passes": 4, "runs": 5, "unknown_effects": 1,
                            "root": "calibration:cal-1"}  # fmt: skip
    runner = env.ready()
    held = stopped(env, runner)
    assert held.details["reason"] == "unknown_effect"
    assert env.head()["data"]["reconcile_pending"] == ["calibration:cal-1"]
    assert "search_run" not in env.fake.names()


def test_the_dashboard_shows_the_root_as_reconcile_pending_after_an_unknown_effect(
    env: Env,
) -> None:
    from amplai_foundry.meta_harness import dashboard

    with env.store.tx() as db:  # what the evaluation service settles for an unknown effect (§8.8)
        env.store.cas(db, env.scope, "meta-budget", "proposal-xyz", 0, "open",
                      {"allocations": {"a1": {"status": "unknown"}}})  # fmt: skip
    env.fake.unknown_cid = "c2"
    runner = env.ready()
    held = stopped(env, runner)
    (root,) = held.details["reconcile_pending"]
    feed = dashboard.build_feed(env.store, env.scope)
    rows = [a for a in feed.approvals if a.kind == "reconcile pending"]
    assert [r.subject for r in rows] == [root] == ["proposal-xyz"]
    assert "reconcile pending" in dashboard.render_pages(feed)["approvals.html"]


def test_without_an_unknown_effect_nothing_is_reconcile_pending(env: Env) -> None:
    runner = env.ready()
    runner.run(DATE)
    assert env.head()["data"]["reconcile_pending"] == []


# --- config and plan validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"budget_trials": -1},
        {"budget_trials": 1.5},
        {"shares": {**SHARES, "search": 0.7}},
        {"shares": {"drift": 0.1, "search": 0.6, "confirmation": 0.3}},
        {"shares": {**SHARES, "screening_design": 0.7}},
        {"shares": {**SHARES, "drift": -0.1, "search": 0.8}},
        {"cells": ("a", "a")},
        {"cells": ("",)},
        {"drift_tasks": ("",)},
        {"pilot_nights": -1},
        {"keep_operator_share": 1.5},
        {"max_parallel": 0},
        {"max_parallel": 5},
        {"stop_at": "7:00"},
        {"stop_at": "25:00"},
        {"stop_at": "07:60"},
        {"stop_at": "0700"},
    ],
)
def test_an_invalid_nightly_config_is_a_rejected_fault(over: dict[str, Any]) -> None:
    with pytest.raises(RuntimeFault) as fault:
        config(**over)
    assert fault.value.code == "NIGHTLY_CONFIG" and fault.value.outcome == "rejected"
    assert fault.value.details


def test_a_config_is_built_from_a_local_json_entry_or_a_mapping() -> None:
    entry = {
        "budget_trials": 150, "shares": dict(SHARES_NO_SCREEN), "cells": [CELL], "max_parallel": 2,
        "pilot": True, "stop_at": "07:00", "drift_tasks": ["t1"], "pilot_nights": 3,
        "keep_operator_share": 0.5,
    }  # fmt: skip
    cfg = NightlyConfig.from_entry(entry)
    assert cfg.cells == (CELL,) and cfg.drift_tasks == ("t1",) and cfg.budget_trials == 150
    from types import SimpleNamespace

    assert NightlyConfig.from_entry(SimpleNamespace(**entry)) == cfg


def test_local_json_meta_nightly_max_hours_reaches_the_config() -> None:
    # clarification after S12: a night ends at min(stop_at, start + meta.nightly.max_hours)
    from pydantic import ValidationError

    from amplai_foundry.runtime.local_deployment import NightlyEntry

    base = {"shares": dict(SHARES_NO_SCREEN), "cells": [CELL]}
    without = NightlyEntry.model_validate(base)
    assert without.max_hours == 8 and NightlyConfig.from_entry(without).max_hours == 8
    with_it = NightlyEntry.model_validate({**base, "max_hours": 5.5})
    assert NightlyConfig.from_entry(with_it).max_hours == 5.5
    assert NightlyEntry.model_validate({**base, "max_hours": 24}).max_hours == 24
    for bad in (0, -1, 24.5):
        with pytest.raises(ValidationError):
            NightlyEntry.model_validate({**base, "max_hours": bad})


def test_the_runner_needs_a_nightly_config(env: Env) -> None:
    with pytest.raises(RuntimeFault) as fault:
        NightlyRunner(env.dep, {"budget_trials": 1})  # type: ignore[arg-type]
    assert fault.value.code == "NIGHTLY_CONFIG"


@pytest.mark.parametrize(
    "date", ["2026-13-01", "2026-1-1", "tonight", "", "2026-10-01T00:00", None]
)
def test_a_night_is_a_valid_date(env: Env, date: Any) -> None:
    runner = env.ready()
    for call in (runner.run, runner.plan_night):
        with pytest.raises(RuntimeFault) as fault:
            call(date)
        assert fault.value.code == "NIGHT_DATE" and fault.value.outcome == "rejected"
    assert env.fake.calls == []


def test_phase_budgets_add_up_to_the_budget_and_screening_comes_out_of_search() -> None:
    assert phase_budget(100, SHARES_NO_SCREEN, screening=False) == {
        "drift": 10, "screening_design": 0, "search": 60, "confirmation": 30,
    }  # fmt: skip
    with_screening = phase_budget(100, SHARES, screening=True)
    assert with_screening == {"drift": 10, "screening_design": 10, "search": 50, "confirmation": 30}
    for budget in (0, 1, 7, 33, 150, 999):
        caps = phase_budget(budget, SHARES, screening=True)
        assert sum(caps.values()) == budget and all(v >= 0 for v in caps.values())
        assert caps["screening_design"] <= phase_budget(budget, SHARES, screening=False)["search"]


def test_the_nightly_policy_binds_the_config_cells_and_allows_only_exploratory_work() -> None:
    now = time.time()
    policy = nightly_policy(
        config(), budget_trials=150, valid_from=iso(now), valid_until=iso(now + 2 * DAY),
        max_budget=dict(MAX_BUDGET),
    )  # fmt: skip
    assert policy["cells"] == [CELL] and policy["budget_trials"] == 150
    assert policy["allowed"] == [
        {"kind": "experiment", "split": "development", "purpose": "exploratory"},
        {"kind": "calibration"},
        {"kind": "drift", "corpus": "amplai-regression-v1"},
    ]
    with pytest.raises(RuntimeFault) as fault:
        nightly_policy(config(), budget_trials=1, valid_from=iso(now),
                       valid_until=iso(now + 9 * DAY), max_budget=dict(MAX_BUDGET))  # fmt: skip
    assert fault.value.code == "NIGHTLY_POLICY"


# --- the separate meta deployment ---------------------------------------------------------------


def test_the_runner_works_on_the_deployment_it_is_given_and_opens_no_other_store(env: Env) -> None:
    runner = env.ready()
    assert runner.store is env.dep.store and runner.scope == env.dep.scope
    assert runner.identity.subject_id == NIGHTLY_ID and runner.identity.kind == "service"
    runner.run(DATE)
    assert runner.backend is env.fake  # the injected backend: no deployment was opened for it


def test_the_server_config_is_refused_as_a_meta_deployment(tmp_path: Path) -> None:
    server = commands.SERVER_CONFIG.expanduser()
    with pytest.raises(Hold) as caught:
        commands.meta_config(server)
    assert caught.value.code == "NIGHT_DEPLOYMENT" and caught.value.outcome == "hold"
    assert (
        commands.meta_config(tmp_path / "meta" / "local.json")
        == (tmp_path / "meta" / "local.json").absolute()
    )
    with pytest.raises(Hold), commands.opened_meta(server):
        pass


def test_a_meta_deployment_sharing_the_servers_store_is_refused(env: Env, tmp_path: Path) -> None:
    mine = env.dep.local(env.dep.config.runtime_root).resolve()
    server = tmp_path / "server" / "local.json"
    server.parent.mkdir()
    server.write_text(
        env.dep.config.model_copy(update={"runtime_root": str(mine)}).model_dump_json()
    )
    assert commands.server_runtime_root(server) == mine
    with pytest.raises(Hold) as caught:
        commands.check_separate(env.dep, server)
    assert caught.value.code == "NIGHT_DEPLOYMENT"
    other = tmp_path / "server2" / "local.json"
    other.parent.mkdir()
    other.write_text(
        env.dep.config.model_copy(
            update={"runtime_root": str(tmp_path / "other-store")}
        ).model_dump_json()
    )
    commands.check_separate(env.dep, other)  # a different store is fine
    commands.check_separate(env.dep, tmp_path / "no-such-server.json")  # no readable server config


def test_a_deployment_without_meta_nightly_is_not_a_meta_deployment(env: Env) -> None:
    with pytest.raises(Hold) as caught:
        commands.nightly_config(env.dep)
    assert caught.value.code == "NIGHT_CONFIG"


def test_the_run_command_refuses_the_servers_config_before_opening_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    server = tmp_path / ".amplai" / "local" / "local.json"
    result = CliRunner().invoke(app, ["meta", "nightly", "run", "--config", str(server)])
    assert result.exit_code == 3, result.output  # a Hold
    assert "NIGHT_DEPLOYMENT" in result.output
    assert not (tmp_path / ".amplai").exists()  # nothing was created under the server's home


# --- the commands: approve, revoke, status ------------------------------------------------------


@pytest.fixture
def meta_dep(env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(commands, "nightly_config", lambda dep: config(env.clock))
    return env


def test_approve_issues_the_standing_approval_as_the_human_operator(meta_dep: Env) -> None:
    env = meta_dep
    result = commands.approve(env.dep, nights=3, budget_trials=150, max_tokens=9000,
                              max_wall_seconds=3600)  # fmt: skip
    policy = result["policy"]
    assert result["provisional"] == ["IC-17", "IC-18"]
    assert policy["budget_trials"] == 150 and policy["cells"] == [CELL]
    assert policy["max_budget"]["max_tokens"] == 9000
    assert policy["max_budget"]["max_parallel_works"] == 2
    from amplai_foundry.runtime.execution.meta_local import epoch_of

    span = epoch_of(policy["valid_until"]) - epoch_of(policy["valid_from"])  # type: ignore[operator]
    assert span == 3 * DAY
    record = env.store.get(env.scope, APPROVAL_KIND, result["standing_ref"])
    assert record["approved_by"]["kind"] == "human" and record["action"] == "nightly.explore"
    assert env.approvals.current_standing() == result["standing_ref"]


@pytest.mark.parametrize("nights", [0, 8, -1, 1.5, "3"])
def test_approve_is_for_one_to_seven_nights(meta_dep: Env, nights: Any) -> None:
    with pytest.raises(RuntimeFault) as fault:
        commands.approve(meta_dep.dep, nights=nights, budget_trials=10, max_tokens=10,
                         max_wall_seconds=60)  # fmt: skip
    assert fault.value.code == "NIGHTLY_POLICY"
    assert meta_dep.approvals.current_standing() is None


def test_revoke_takes_the_current_or_a_named_standing_approval(meta_dep: Env) -> None:
    env = meta_dep
    with pytest.raises(Hold) as none_yet:
        commands.revoke(env.dep, None)
    assert none_yet.value.code == "STANDING_APPROVAL"
    issued = commands.approve(env.dep, nights=2, budget_trials=10, max_tokens=10,
                              max_wall_seconds=60)  # fmt: skip
    with pytest.raises(Hold) as unknown:
        commands.revoke(env.dep, "meta-approval-nope")
    assert unknown.value.code == "STANDING_APPROVAL"
    assert commands.revoke(env.dep, None) == {"revoked": issued["standing_ref"]}
    assert env.approvals.current_standing() is None
    again = commands.approve(env.dep, nights=2, budget_trials=10, max_tokens=10,
                             max_wall_seconds=60)  # fmt: skip
    named = commands.revoke(env.dep, again["standing_ref"]["id"])
    assert named == {"revoked": again["standing_ref"]}


def test_status_shows_the_standing_approval_and_the_night(meta_dep: Env) -> None:
    env = meta_dep
    assert commands.status(env.dep, DATE) == {"date": DATE, "standing_ref": None, "night": None}
    env.standing()
    runner = env.runner()
    runner.run(DATE)
    runner.run("2026-10-02", dry_run=True)
    shown = commands.status(env.dep, DATE)
    assert shown["standing_ref"] and shown["standing"]["budget_trials"] == 200
    assert shown["night"]["state"] == "finished" and shown["night"]["trials"] == 65
    assert shown["night"]["reconcile_pending"] == []
    dry = commands.status(env.dep, "2026-10-02")
    assert dry["night"] is None and dry["dry_run"]["state"] == "finished"


# --- print-agent: the launchd template ----------------------------------------------------------


@pytest.fixture
def no_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME in tmp_path; any process launch fails the test."""
    monkeypatch.setenv("HOME", str(tmp_path))

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("print-agent must not run a process")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    import os

    monkeypatch.setattr(os, "system", forbidden)
    return tmp_path


def test_print_agent_fills_the_template_and_installs_nothing(no_install: Path) -> None:
    config_path = no_install / "meta" / "local.json"
    text = commands.print_agent(amplai="/opt/bin/amplai", config=config_path,
                                path_env="/usr/bin:/opt/homebrew/bin")  # fmt: skip
    assert "{{" not in text and "}}" not in text
    agent = plistlib.loads(text.encode())
    assert agent["Label"] == "ai.amplai.meta-nightly"
    assert agent["ProgramArguments"] == [
        "/opt/bin/amplai", "meta", "nightly", "run", "--config", str(config_path.absolute()),
    ]  # fmt: skip
    assert agent["StartCalendarInterval"] == {"Hour": 1, "Minute": 0}
    logs = (no_install / ".amplai" / "meta" / "logs").absolute()
    assert agent["StandardOutPath"].startswith(str(logs))
    assert agent["StandardErrorPath"].startswith(str(logs))
    assert agent["EnvironmentVariables"] == {"PATH": "/usr/bin:/opt/homebrew/bin"}
    # nothing was written anywhere: no LaunchAgents file, no log directory, no meta home
    assert not (no_install / "Library").exists() and not (no_install / ".amplai").exists()
    assert list(no_install.iterdir()) == []


def test_print_agent_escapes_xml_in_every_value(no_install: Path) -> None:
    text = commands.print_agent(amplai="/opt/a&b/amplai", config=no_install / "<m>" / "local.json",
                                path_env="/x:/y&z")  # fmt: skip
    agent = plistlib.loads(text.encode())
    assert agent["ProgramArguments"][0] == "/opt/a&b/amplai"
    assert agent["ProgramArguments"][-1].endswith("/<m>/local.json")
    assert agent["EnvironmentVariables"]["PATH"] == "/x:/y&z"
    assert "&amp;" in text and "&lt;m&gt;" in text


def test_the_template_has_the_documented_placeholders_and_label() -> None:
    template = commands.TEMPLATE.read_text()
    assert commands.TEMPLATE.name == "ai.amplai.meta-nightly.plist.template"
    for name in commands.PLACEHOLDERS:
        assert "{{" + name + "}}" in template
    assert commands.PLACEHOLDERS == ("LABEL", "AMPLAI", "CONFIG", "LOG_DIR", "PATH")
    assert commands.LABEL == "ai.amplai.meta-nightly"


def test_print_agent_without_an_amplai_on_path_is_a_hold(
    no_install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    with pytest.raises(Hold) as caught:
        commands.print_agent(amplai=None, config=no_install / "local.json")
    assert caught.value.code == "AMPLAI_PATH"


def test_print_agent_refuses_the_servers_config(no_install: Path) -> None:
    with pytest.raises(Hold) as caught:
        commands.print_agent(amplai="/opt/bin/amplai",
                             config=no_install / ".amplai" / "local" / "local.json")  # fmt: skip
    assert caught.value.code == "NIGHT_DEPLOYMENT"


def test_the_print_agent_command_prints_a_plist_and_installs_nothing(no_install: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["meta", "nightly", "print-agent", "--amplai", "/opt/bin/amplai",
         "--config", str(no_install / "meta" / "local.json")],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert plistlib.loads(result.output.encode())["Label"] == "ai.amplai.meta-nightly"
    assert list(no_install.iterdir()) == []


def test_the_print_agent_command_holds_for_the_servers_config(no_install: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["meta", "nightly", "print-agent", "--amplai", "/opt/bin/amplai",
         "--config", str(no_install / ".amplai" / "local" / "local.json")],
    )  # fmt: skip
    assert result.exit_code == 3, result.output
    assert list(no_install.iterdir()) == []


# --- LocalNightlyBackend on a real store ----------------------------------------------------------
# The S11 world (`test_033_s11_stages.build_world`): the rc06 product with `LocalMeta`, a frozen
# corpus v2, an evaluator version and `LocalMetaOps` with a pinned qualified executor. Stand-in:
# its scripted trial executor (receipt v2 shape; no driver). Real: the regression corpus freeze,
# `CalibrationService`, `LocalMetaApprovals` (`issue`, `issue_nightly`, `issue_standing`, `check`
# at every trial guard, `revoke`), `NightlyRunner` and `LocalNightlyBackend` (preflight, snapshot,
# drift_run, quota_signal).
REGRESSION_TASKS = 8  # 0 of 8 is outside the 99 % prediction range of a calibrated 8/8


class RealNight:
    def __init__(self, world: Any) -> None:
        from dataclasses import replace

        from amplai_foundry.meta_harness import corpus_v2

        self.w = world
        tasks = tuple(
            replace(t, set="regression", split="validation", domain="regression")
            for t in world.corpus.tasks[:REGRESSION_TASKS]
        )
        regression = replace(world.corpus, corpus_id=corpus_v2.REGRESSION_CORPUS_ID, tasks=tasks)
        self.refs = corpus_v2.freeze(
            world.operator, world.store, world.artifacts, regression, holdout_use_limit=1
        )
        self.case_ids = [t.task_id for t in tasks]
        start = time.time()
        self.cfg = NightlyConfig(
            budget_trials=100, shares=dict(SHARES_NO_SCREEN), cells=(CELL,),
            drift_tasks=tuple(self.case_ids), pilot=False, pilot_nights=0,
            keep_operator_share=0.5, max_parallel=1, stop_at=hhmm(start - 120),
        )  # fmt: skip
        self.budget = {**MAX_BUDGET, "max_parallel_works": 1}
        policy = nightly_policy(
            self.cfg, budget_trials=100, valid_from=iso(start - 60),
            valid_until=iso(start + 3 * DAY), max_budget=self.budget,
        )  # fmt: skip
        self.standing_ref = world.local.approvals.issue_nightly(world.operator, policy)
        self.runner = NightlyRunner(world.dep, self.cfg)
        self.backend = nightly_module.LocalNightlyBackend(world.ops, self.runner)
        self.runner._backend = self.backend

    def outcome(self, success: bool) -> None:
        self.w.executor.outcome = lambda role, case_id, repeat: success

    def calibrate(self) -> dict[str, Any]:
        """The human operator's calibration of the cell on the regression corpus (as
        `LocalMetaOps.calibrate`, on this corpus)."""
        from amplai_foundry.evaluation import calibration
        from amplai_foundry.runtime.contracts.identity import digest, now
        from amplai_foundry.runtime.execution.meta_local import EXECUTOR_ID

        w = self.w
        plan: dict[str, Any] = {
            "schema": calibration.PLAN_SCHEMA, "scope": w.scope.wire(), "cells": [CELL],
            "composition_refs": {CELL: [w.ops.app.compositions[CELL]]},
            "corpus_ref": self.refs["corpus_ref"], "splits": ["validation"],
            "case_ids": list(self.case_ids), "initial_repeats": 1,
            "adaptive": {"max_repeats": 1, "rule": calibration.RULE,
                         "borderline": list(calibration.INFORMATIVE)},
            "budget": dict(self.budget), "max_parallel": 1, "frozen_at": now(),
        }  # fmt: skip
        plan["approval_ref"] = w.local.approvals.issue(
            w.operator, "experiment.execute", digest(plan)
        )
        service = calibration.CalibrationService(
            w.store, w.d.contracts, w.artifacts, approval_check=w.local.approvals.check,
            executor_id=EXECUTOR_ID, executor_policy=w.local.evaluation.executor_policy,
        )  # fmt: skip
        plan_ref = service.freeze(w.operator, plan)
        return dict(service.run(w.operator, plan_ref, w.executor, parallel=1))

    def run(self, date: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            return self.runner.run(date), None
        except Hold as held:
            assert held.code == "NIGHT_STOPPED", held.as_dict()
            return None, held.details["reason"]

    def head(self, date: str) -> dict[str, Any]:
        return dict(self.w.store.head(self.w.scope, "nightly-run", f"night-{date}")["data"])

    def drift_approvals(self) -> list[dict[str, Any]]:
        """The approvals of the calibration plans the nightly identity froze (drift runs)."""
        out = []
        for _ref, plan in self.w.store.list_objects(self.w.scope, "calibration-plan"):
            if not isinstance(plan.get("approval_ref"), dict):
                continue  # the world's own summary fixture has no approval
            approval = self.w.store.get(self.w.scope, APPROVAL_KIND, plan["approval_ref"])
            if approval["approved_by"]["kind"] != "human":
                out.append(approval)
        return out


@pytest.fixture
def real(tmp_path: Path) -> Any:
    from test_033_s11_stages import build_world

    with build_world(tmp_path) as world:
        yield RealNight(world)


def test_local_backend_drift_derives_a_nightly_approval_and_never_rebaselines_itself(
    real: RealNight,
) -> None:
    real.outcome(True)
    real.calibrate()  # the operator's baseline: 4 of 4
    assert real.backend.preflight(real.cfg.cells) == []
    calls = len(real.w.executor.calls)

    # night 1: the champion fails every drift task -> outside the calibrated band
    real.outcome(False)
    ref, reason = real.run(DATE)
    assert reason == "drift" and ref is None
    night = real.head(DATE)
    check = night["drift"][CELL]
    assert check["status"] == "outside_band"
    assert check["band"]["passes"] == REGRESSION_TASKS
    assert check["band"]["runs"] == REGRESSION_TASKS
    assert check["observed"] == {"passes": 0, "runs": REGRESSION_TASKS}
    assert night["trials"] == REGRESSION_TASKS
    assert len(real.w.executor.calls) - calls == REGRESSION_TASKS
    # the drift run's approval is derived by the nightly identity under the standing approval
    (derived,) = real.drift_approvals()
    assert derived["plan_kind"] == "drift" and derived["action"] == "experiment.execute"
    assert derived["approved_by"]["subject_id"] == NIGHTLY_ID
    assert derived["approved_by"]["kind"] == "service"
    assert derived["standing_ref"]["id"] == real.standing_ref["id"]
    assert [e["cell_id"] for e in night["recalibration"]] == [CELL]

    # night 2: night 1's drift summary (0 of 8) is not the baseline; search stays stopped
    snap = real.backend.snapshot(CELL, real.cfg.drift_tasks)
    assert snap["band"] == {"passes": REGRESSION_TASKS, "runs": REGRESSION_TASKS}
    calls = len(real.w.executor.calls)
    ref, reason = real.run(NEXT)
    assert reason == "drift"
    check = real.head(NEXT)["drift"][CELL]
    assert check["status"] == "recalibration_pending"
    assert check["recalibration_pending_since"] == DATE
    assert len(real.w.executor.calls) == calls  # no drift trial against the stale baseline

    # the operator re-calibrates; night 3 compares against that calibration and searches
    real.outcome(True)
    real.calibrate()
    ref, reason = real.run(LATER)
    assert reason is None and ref is not None, json.dumps(real.head(LATER)["drift"], indent=1)
    night = real.head(LATER)
    assert night["drift"][CELL]["status"] == "ok" and night["stopped"] is None
    assert phases(night)["search"] == "done"
    assert len(real.drift_approvals()) == 2


def test_local_backend_a_standing_approval_revoked_mid_calibration_stops_the_night(
    real: RealNight,
) -> None:
    real.outcome(True)
    real.calibrate()
    seen: list[str] = []

    def revoke_on_second(role: str, case_id: str, repeat: int) -> bool:
        seen.append(case_id)
        if len(seen) == 2:
            real.w.local.approvals.revoke(real.w.operator, real.standing_ref)
        return True

    real.w.executor.outcome = revoke_on_second
    ref, reason = real.run(DATE)
    assert reason == "standing_approval" and ref is None
    night = real.head(DATE)
    assert night["stopped"] == "standing_approval"
    assert len(seen) == 2  # the trial guard re-checks the derived approval before each trial
    assert phases(night)["dreaming"] == "skipped"


def pin_in_memory_only(self: Any, basis: str, evidence: list[str], per_trial_tokens: int) -> None:
    """The pre-IC-29 qualification: pinned in this process, no record a later process reads."""
    from amplai_foundry.evaluation.service import ExecutorPolicy
    from amplai_foundry.runtime.contracts.identity import new_id
    from amplai_foundry.runtime.execution.meta_local import EXECUTOR_ID

    ref = self._put("executor-qualification", new_id("executor-qualification"),
                    {"status": "pass", "executor_id": EXECUTOR_ID, "scope_note": basis,
                     "evidence": evidence})  # fmt: skip
    self.local.evaluation.executor_policy = ExecutorPolicy(
        frozenset({"sandbox_rerun"}), per_trial_tokens, 0, ref
    )


@pytest.fixture
def unqualified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from test_033_s11_stages import build_world

    from amplai_foundry.runtime.execution.meta_ops import LocalMetaOps

    monkeypatch.setattr(LocalMetaOps, "qualify_executor", pin_in_memory_only)
    with build_world(tmp_path) as world:
        yield RealNight(world)


def test_local_backend_preflight_needs_an_ic29_qualification_record(unqualified: RealNight) -> None:
    """IC-29: a qualification pinned in memory by another process is not the cell's record."""
    real = unqualified
    assert real.w.local.evaluation.executor_policy is not None  # pinned in memory only
    assert real.backend.preflight(real.cfg.cells) == [
        {"code": "QUALIFIED_EXECUTOR_REQUIRED", "cell_id": CELL}
    ]
    _ref, reason = real.run(DATE)
    assert reason == "preflight" and real.head(DATE)["drift"] == {}


def test_ic29_the_record_keeps_tokens_basis_evidence_cell_and_the_human_actor(
    real: RealNight,
) -> None:
    from amplai_foundry.runtime.execution.meta_ops import qualification_record

    ref, value = qualification_record(real.w.store, real.w.scope, CELL) or (None, {})
    assert ref is not None
    assert value["per_trial_tokens"] == 10 and value["basis"] == "scripted S11 trials"
    assert value["evidence"] == ["tests/v3/test_033_s11_stages.py"]
    assert value["cell_id"] == CELL and value["qualified_by"]["kind"] == "human"
    assert value["qualified_by"]["subject_id"] == real.w.operator.subject_id
    assert qualification_record(real.w.store, real.w.scope, "another-cell") is None


def test_ic29_a_fresh_ops_and_a_night_rebuild_the_qualification_from_the_record(
    real: RealNight,
) -> None:
    from amplai_foundry.runtime.execution.meta_ops import LocalMetaOps

    w = real.w
    w.local.evaluation.executor_policy = None  # a new process: nothing pinned in memory
    fresh = LocalMetaOps(w.dep, w.corpus, w.executor, driver=CELL)  # type: ignore[arg-type]
    policy = fresh.local.evaluation.executor_policy
    assert policy is not None and policy.max_trial_tokens == 10
    w.local.evaluation.executor_policy = None
    assert real.backend.preflight(real.cfg.cells) == []
    real.outcome(True)
    ref, reason = real.run(DATE)  # uncalibrated drift: the night searches (nothing to search)
    assert reason is None and ref is not None
    assert w.local.evaluation.executor_policy is not None


def test_ic29_a_record_of_the_nightly_or_proposer_identity_never_qualifies(real: RealNight) -> None:
    from amplai_foundry.runtime.contracts.identity import new_id, now
    from amplai_foundry.runtime.execution.meta_local import EXECUTOR_ID, PROPOSER_ID
    from amplai_foundry.runtime.execution.meta_ops import qualification_record

    w = real.w
    before = qualification_record(w.store, w.scope, CELL)
    for subject, kind in ((NIGHTLY_ID, "service"), (PROPOSER_ID, "service"), (NIGHTLY_ID, "human")):
        w.put("executor-qualification", new_id("executor-qualification"), {
            "status": "pass", "executor_id": EXECUTOR_ID, "scope_note": "x", "evidence": ["e"],
            "basis": "x", "per_trial_tokens": 999, "cell_id": CELL,
            "qualified_by": {"subject_id": subject, "kind": kind, "authn_context_ref": "x"},
            "qualified_at": now(),
        })  # fmt: skip
    assert qualification_record(w.store, w.scope, CELL) == before
