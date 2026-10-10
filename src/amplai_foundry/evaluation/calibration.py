"""Frozen, approved, budgeted calibration of cells with adaptive repeats (Work 033 S2).

interfaces.md §2.8 (records), §3.8 (API), §8.2 (rule). A calibration plan names the cells, their
compositions (the v1 composition first, then optional manifest variants), and every development
and validation case of a frozen corpus; holdout is never calibrated. The operator approves the
exact plan (`experiment.execute` over the plan without `approval_ref`), a proposer identity can
neither freeze nor run it, and every trial is reserved against its own root budget
`calibration:<plan id>` before the executor is called, exactly as experiment trials are.

Adaptive rule `disagree_or_borderline_v1` (§8.2): round 1 runs every (case, cell) once. Then,
round by round while the budget admits it, a (cell, task) gets one more run when the cells
disagree on the task (their pass rates on it differ) or when its own pass rate after at least two
runs lies in the plan's `borderline` interval, and it has fewer than `max_repeats` runs. The rule
reads the cell's v1 composition; the cell's variants are run in the same slots (same repeat
index) so §6.5 table fitting sees paired data, and are not part of the summary.

`select_cases` is the pure stage-subset rule `EvaluationService` recomputes (IC-15).

Work 033 S7b (clarification after the S10/S13/S14 fix wave, "Open (S7b)"): a plan may name one
app, `"app": {"app_id": str, "base_ids": [str]}` (the corpus bases whose app it is). Its
`case_ids` are then every case of its splits whose frozen case payload (§2.7, operator trust)
names one of those bases, in corpus order (`SAMPLING_CHANGED` otherwise), so the bench app and
`amplai-tb2` are calibrated separately on their own compositions. A plan without `app` keeps
every case of its splits (unchanged).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from statistics import NormalDist, mean, median, stdev
from typing import Any, Literal

from amplai_foundry.meta_harness.budget import EvolutionBudget
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import digest, new_id, now
from amplai_foundry.runtime.contracts.semantics import check_refs, resolve_ref
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.storage.store import Scope, Store

from . import versions
from .corpus import CorpusService
from .sequential import noise_band, wilson
from .service import (
    ExecutorPolicy,
    TrialObservation,
    effective_parallel,
    failed_trial,
    is_ref,
    run_bounded,
    spent_tokens,
    unknown_usage_charge,
    usage_unknown,
    validate_observation,
)

PLAN_KIND = "calibration-plan"
RUN_KIND = "calibration-run"
TRIAL_KIND = "calibration-trial"
SUMMARY_KIND = "calibration-summary"
PLAN_SCHEMA = "amplai.calibration-plan.v1"
TRIAL_SCHEMA = "amplai.calibration-trial.v1"
SUMMARY_SCHEMA = "amplai.calibration-summary.v1"
RULE = "disagree_or_borderline_v1"
MAX_REPEATS = 5
MAX_PARALLEL = 4
# Operator decision 2026-10-10: this many unknown-usage trials in a row stop a calibration
# (``provider_failures``). Plan calplan-0034e3f6 kept dispatching for nine hours after both
# providers began failing: 65 trials in a row, each up to its 30-minute deadline, each charged
# the full token reservation (decision (B)).
PROVIDER_FAILURE_STREAK = 4
# Pilot 2026-10-11: this many not-run trials in a row with the same error code stop a calibration
# (``pre_run_failures``). Plan calplan-75196f2d held all 50 regression trials CORPUS_CHANGED in
# seconds; each not-run trial is charged the token reservation (decision (B)), so the token
# budget, not the cause, ended the run.
PRE_RUN_FAILURE_STREAK = 4
SPLITS = ("development", "validation")
# §8.2: summary class `informative` = pass rate in [0.2, 0.9].
INFORMATIVE = (0.2, 0.9)
NOISE_BAND_TASKS = (16, 20, 30)
CLASSES = ("informative", "saturated", "unsolved", "flaky_grading", "unknown")
# Calibration trials run as the qualified sandbox rerun mode of the trial executor (§8.2: "trials go
# through LocalTrialExecutor like experiment trials"); the receipt binds this mode.
MODE = "sandbox_rerun"
# Budget holds that end the adaptive phase normally ("while budget remains", §8.2).
BUDGET_STOPS = frozenset({"META_TOKEN_BUDGET", "META_COST_BUDGET", "META_WALL_BUDGET"})
PLAN_FIELDS = frozenset(
    {
        "schema",
        "scope",
        "cells",
        "composition_refs",
        "corpus_ref",
        "splits",
        "case_ids",
        "initial_repeats",
        "adaptive",
        "budget",
        "max_parallel",
        "approval_ref",
        "frozen_at",
    }
)
# S7b: the app whose cases a plan calibrates (absent: every case of the splits)
OPTIONAL_PLAN_FIELDS = frozenset({"app"})
BUDGET_FIELDS = frozenset(
    {
        "max_wall_seconds",
        "max_attempts",
        "max_tokens",
        "max_cost_microunits",
        "currency",
        "max_parallel_works",
        "max_delegation_depth",
    }
)
_Z95 = NormalDist().inv_cdf(0.975)


def select_cases(
    cases: list[dict[str, Any]],
    *,
    rule: Literal["informative_v1", "all_v1"],
    summary: dict[str, Any] | None,
    cell_id: str,
    max_tasks: int,
    domain_of: Callable[[dict[str, Any]], str],
) -> list[str]:
    """IC-15, pure. `informative_v1`: the cases whose summary class for `cell_id` is
    `informative`; `all_v1`: every case. Then round-robin over domains in order of first
    appearance in `cases` (corpus order), each domain in corpus order, until `max_tasks`; the
    result is in corpus order. Same inputs give the same ids."""
    if rule not in ("informative_v1", "all_v1") or type(max_tasks) is not int or max_tasks < 1:
        raise RuntimeFault("SAMPLING_PLAN", "Unknown case rule or invalid max_tasks")
    ids = [c.get("case_id") for c in cases]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise RuntimeFault("SAMPLING_PLAN", "Case ids are unique nonempty strings")
    pool = list(cases)
    if rule == "informative_v1":
        cells = summary.get("cells") if isinstance(summary, dict) else None
        if not isinstance(cells, dict) or not isinstance(cells.get(cell_id), dict):
            raise RuntimeFault("SAMPLING_PLAN", "informative_v1 needs a summary row for the cell")
        tasks = cells[cell_id].get("tasks")
        if not isinstance(tasks, dict):
            raise RuntimeFault("SAMPLING_PLAN", "The summary row has no task classes")
        pool = [
            c
            for c in cases
            if isinstance(tasks.get(c["case_id"]), dict)
            and tasks[c["case_id"]].get("class") == "informative"
        ]
    order: list[str] = []
    for case in cases:
        domain = domain_of(case)
        if domain not in order:
            order.append(domain)
    queues = {d: [c["case_id"] for c in pool if domain_of(c) == d] for d in order}
    chosen: set[str] = set()
    while len(chosen) < max_tasks and any(queues.values()):
        for domain in order:
            if queues[domain] and len(chosen) < max_tasks:
                chosen.add(queues[domain].pop(0))
    return [c["case_id"] for c in pool if c["case_id"] in chosen]


def _count(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def validate_budget(budget: Any) -> None:
    """`$defs.budget` of `contracts/schemas/common.schema.json`."""
    ok = (
        isinstance(budget, dict)
        and set(budget) == BUDGET_FIELDS
        and _count(budget["max_wall_seconds"], 1, 604800)
        and _count(budget["max_attempts"], 1, 10)
        and _count(budget["max_tokens"], 1, 9007199254740991)
        and (
            budget["max_cost_microunits"] is None
            or _count(budget["max_cost_microunits"], 0, 9007199254740991)
        )
        and isinstance(budget["currency"], str)
        and len(budget["currency"]) == 3
        and budget["currency"].isascii()
        and budget["currency"].isupper()
        and budget["currency"].isalpha()
        and _count(budget["max_parallel_works"], 1, 64)
        and _count(budget["max_delegation_depth"], 0, 8)
    )
    if not ok:
        raise RuntimeFault("CALIBRATION_PLAN", "budget does not match $defs.budget")


def validate_calibration_plan(plan: dict[str, Any]) -> None:
    """§2.0: validated before `put` (shape of §2.8)."""
    if (
        not isinstance(plan, dict)
        or not PLAN_FIELDS <= set(plan) <= PLAN_FIELDS | OPTIONAL_PLAN_FIELDS
        or plan["schema"] != PLAN_SCHEMA
    ):
        raise RuntimeFault("CALIBRATION_PLAN", "A calibration plan has exactly the §2.8 fields")
    app = plan.get("app")
    if "app" in plan and not (
        isinstance(app, dict)
        and set(app) == {"app_id", "base_ids"}
        and isinstance(app["app_id"], str)
        and bool(app["app_id"])
        and isinstance(app["base_ids"], list)
        and bool(app["base_ids"])
        and all(isinstance(b, str) and b for b in app["base_ids"])
        and len(set(app["base_ids"])) == len(app["base_ids"])
    ):
        raise RuntimeFault("CALIBRATION_PLAN", "app names one app id and its corpus bases")
    cells, refs = plan["cells"], plan["composition_refs"]
    adaptive, splits = plan["adaptive"], plan["splits"]
    ok = (
        isinstance(cells, list)
        and bool(cells)
        and all(isinstance(c, str) and c for c in cells)
        and len(set(cells)) == len(cells)
        and isinstance(refs, dict)
        and set(refs) == set(cells)
        and all(
            isinstance(v, list)
            and v
            and all(is_ref(r) for r in v)
            # a composition is calibrated once per cell
            and len({digest(r) for r in v}) == len(v)
            for v in refs.values()
        )
        and is_ref(plan["corpus_ref"])
        and is_ref(plan["approval_ref"])
        and isinstance(splits, list)
        and bool(splits)
        and len(set(splits)) == len(splits)
        and set(splits) <= set(SPLITS)  # holdout is never calibrated (§8.2)
        and isinstance(plan["case_ids"], list)
        and bool(plan["case_ids"])
        and plan["initial_repeats"] == 1
        and type(plan["initial_repeats"]) is int
        and isinstance(adaptive, dict)
        and set(adaptive) == {"max_repeats", "rule", "borderline"}
        and _count(adaptive["max_repeats"], 1, MAX_REPEATS)
        and adaptive["rule"] == RULE
        and isinstance(adaptive["borderline"], list)
        and len(adaptive["borderline"]) == 2
        and all(
            type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1
            for x in adaptive["borderline"]
        )
        and adaptive["borderline"][0] <= adaptive["borderline"][1]
        and _count(plan["max_parallel"], 1, MAX_PARALLEL)
        and isinstance(plan["frozen_at"], str)
    )
    if not ok:
        raise RuntimeFault("CALIBRATION_PLAN", "Invalid calibration plan (§2.8)")
    validate_budget(plan["budget"])


def plan_id(plan: dict[str, Any]) -> str:
    """Content-addressed (§2.0): the id binds the approved content, so one approval cannot
    freeze two calibration roots."""
    return "calplan-" + digest(CalibrationService.approval_subject(plan))[7:31]


def _known(trial: dict[str, Any]) -> bool:
    return trial.get("success") is not None and trial.get("unknown_effects", 0) == 0


def _task_row(trials: list[dict[str, Any]]) -> dict[str, Any]:
    known = [t for t in trials if _known(t)]
    runs, passes = len(known), sum(t["success"] is True for t in known)
    rate = passes / runs if runs else None
    if len(known) != len(trials) or rate is None:
        klass = "unknown"
    elif passes == runs:
        klass = "saturated"
    elif passes == 0:
        klass = "unsolved"
    elif INFORMATIVE[0] <= rate <= INFORMATIVE[1]:
        klass = "informative"
    else:  # unreachable with at most 5 runs; kept total
        klass = "unknown"
    return {
        "runs": runs,
        "passes": passes,
        "pass_rate": rate,
        "wilson_95": list(wilson(passes, runs, _Z95)) if runs else None,
        "class": klass,
    }


def _cell_summary(case_ids: list[str], trials: list[dict[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, list[dict[str, Any]]] = {cid: [] for cid in case_ids}
    for trial in trials:
        by_task.setdefault(trial["task_id"], []).append(trial)
    tasks = {cid: _task_row(by_task[cid]) for cid in case_ids}
    rates = [row["pass_rate"] for row in tasks.values() if row["pass_rate"] is not None]
    rate = mean(rates) if rates else None
    if rate is None:
        interval = None
    elif len(rates) < 2:
        interval = [0.0, 1.0]
    else:
        # Unit = task: normal interval of the mean of per-task rates.
        half = _Z95 * stdev(rates) / math.sqrt(len(rates))
        interval = [max(0.0, rate - half), min(1.0, rate + half)]
    known_by_task = {
        cid: sorted((t for t in by_task[cid] if _known(t)), key=lambda t: t["repeat"])
        for cid in case_ids
    }
    counts = [len(v) for v in known_by_task.values() if v]
    k = min(counts) if counts else 0
    pass_k = {
        "k": k,
        "rate": (
            sum(all(t["success"] is True for t in v[:k]) for v in known_by_task.values() if v)
            / len(counts)
            if counts
            else None
        ),
    }
    pairs = []
    for cid in case_ids:
        first = {t["repeat"]: t["success"] for t in known_by_task[cid] if t["repeat"] in (0, 1)}
        if len(first) == 2:
            pairs.append(first[0] != first[1])
    discordance = sum(pairs) / len(pairs) if pairs else None
    # decision (B): an unknown-usage trial counts its charged tokens (``charged_tokens``, the
    # reservation or more) toward tokens per solved and is never a solved trial (``success`` null,
    # or False after an answer lookup); a trial with no tokens recorded at all leaves the value
    # null (``spent_tokens``: unknown is never fewer)
    missing_usage = [t for t in trials if t.get("charged_tokens") is not None]
    tokens = [spent_tokens(t) for t in trials]
    solved = sum(t.get("success") is True for t in trials)
    elapsed = [t["elapsed_ms"] for t in trials if t.get("elapsed_ms") is not None]
    return {
        "tasks": tasks,
        "pass_rate": rate,
        "pass_rate_interval": interval,
        "pass_k": pass_k,
        "aa_discordance": discordance,
        "noise_band_at": (
            {str(n): noise_band(discordance, n) for n in NOISE_BAND_TASKS}
            if discordance is not None
            else None
        ),
        "tokens_per_solved": (
            sum(x for x in tokens if x is not None) / solved
            if solved and all(x is not None for x in tokens)
            else None
        ),
        "median_seconds": median(elapsed) / 1000 if elapsed else None,
        "usage_unknown_trials": len(missing_usage),
    }


def summarize_trials(plan: dict[str, Any], trials: list[dict[str, Any]]) -> dict[str, Any]:
    """The `cells`, `saturated_everywhere` and `informative_any` parts of a summary (§2.8, §8.2),
    from each cell's v1 composition. `flaky_grading` comes from `corpus_check --repeats`, which no
    stored record carries, so this function never assigns it."""
    cells = {}
    for cell in plan["cells"]:
        v1 = plan["composition_refs"][cell][0]
        own = [t for t in trials if t["cell_id"] == cell and t["composition_ref"] == v1]
        cells[cell] = _cell_summary(list(plan["case_ids"]), own)
    return {
        "cells": cells,
        "saturated_everywhere": [
            cid
            for cid in plan["case_ids"]
            if all(cells[c]["tasks"][cid]["class"] == "saturated" for c in plan["cells"])
        ],
        "informative_any": [
            cid
            for cid in plan["case_ids"]
            if any(cells[c]["tasks"][cid]["class"] == "informative" for c in plan["cells"])
        ],
    }


def validate_summary(value: dict[str, Any]) -> None:
    ok = (
        isinstance(value, dict)
        and set(value)
        == {
            "schema",
            "scope",
            "plan_ref",
            "cells",
            "saturated_everywhere",
            "informative_any",
            "evaluator_version_ref",
            "summarized_at",
        }
        and value["schema"] == SUMMARY_SCHEMA
        and is_ref(value["plan_ref"])
        and is_ref(value["evaluator_version_ref"])
        and isinstance(value["cells"], dict)
        and all(
            isinstance(row, dict)
            and isinstance(row.get("tasks"), dict)
            and all(t.get("class") in CLASSES for t in row["tasks"].values())
            for row in value["cells"].values()
        )
    )
    if not ok:
        raise RuntimeFault("CALIBRATION_SUMMARY", "Invalid calibration summary (§2.8)")


class CalibrationService:
    def __init__(
        self,
        store: Store,
        contracts: Any,
        artifacts: ArtifactStore,
        *,
        approval_check: Callable[..., Any],
        executor_id: str,
        executor_policy: ExecutorPolicy | None,
    ) -> None:
        self.store, self.contracts, self.artifacts = store, contracts, artifacts
        self.approval_check, self.executor_id = approval_check, executor_id
        self.executor_policy = executor_policy
        self.corpus, self.budgets = CorpusService(store, artifacts), EvolutionBudget(store)

    @staticmethod
    def approval_subject(plan: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in plan.items() if k != "approval_ref"}

    @staticmethod
    def _independent(actor: Actor, permission: str) -> None:
        actor.require(permission)
        if "harness.propose" in actor.permissions:
            raise Hold(
                "CALIBRATION_PROPOSER", "A proposer identity cannot freeze or run a calibration"
            )

    def _guard(self, actor: Actor, plan: dict[str, Any]) -> None:
        self.approval_check(
            actor.scope,
            plan["approval_ref"],
            "experiment.execute",
            digest(self.approval_subject(plan)),
        )
        try:
            if self.store.head(actor.scope, "runtime-control", "kill")["data"]["enabled"]:
                raise Hold("KILL_SWITCH", "Operator disabled evaluation admission")
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise

    def _cases(self, actor: Actor, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """Every case of the plan's splits in corpus order (§8.2), of the plan's app when it
        names one (S7b); the split ACL applies."""
        selected: dict[str, dict[str, Any]] = {}
        for split in plan["splits"]:
            for case in self.corpus.select(actor, plan["corpus_ref"], split, purpose="calibration"):
                selected[case["case_id"]] = case
        corpus = self.store.get(actor.scope, "eval-corpus", plan["corpus_ref"])
        cases = [selected[c["case_id"]] for c in corpus["cases"] if c["case_id"] in selected]
        app = plan.get("app")
        if app is not None:
            cases = [c for c in cases if self._base_of(actor, c) in set(app["base_ids"])]
        if not cases or plan["case_ids"] != [c["case_id"] for c in cases]:
            raise Hold(
                "SAMPLING_CHANGED",
                "Pin every case of the calibrated splits (of the plan's app) in corpus order",
            )
        return cases

    def _base_of(self, actor: Actor, case: dict[str, Any]) -> str | None:
        """The ``base_id`` of a frozen case payload (§2.7); None when it names none."""
        try:
            payload = json.loads(
                self.artifacts.read(actor.scope, case["artifact_ref"], trusted=True)
            )
        except (ValueError, TypeError, KeyError):
            return None
        base = payload.get("base_id") if isinstance(payload, dict) else None
        return base if isinstance(base, str) else None

    def _validate_executor(self, scope: Scope) -> ExecutorPolicy:
        policy = self.executor_policy
        if policy is None:
            raise Hold(
                "QUALIFIED_EXECUTOR_REQUIRED",
                "Register a server-side qualified bounded executor first",
            )
        policy.validate()
        if MODE not in policy.modes:
            raise Hold("EXECUTOR_MODE", "The executor is not qualified for sandbox reruns")
        _, qualification = resolve_ref(self.store, scope, policy.qualification_ref)
        if (
            qualification.get("status") != "pass"
            or qualification.get("executor_id") != self.executor_id
        ):
            raise Hold(
                "EXECUTOR_QUALIFICATION", "Qualification does not bind the installed executor"
            )
        return policy

    def freeze(self, actor: Actor, plan: dict[str, Any]) -> dict[str, Any]:
        self._independent(actor, "experiment.approve")
        validate_calibration_plan(plan)
        scope = actor.scope
        if plan["scope"] != scope.wire():
            raise RuntimeFault("SCOPE_MISMATCH", "Calibration scope differs")
        check_refs(self.store, scope, plan)
        for refs in plan["composition_refs"].values():
            for ref in refs:
                if resolve_ref(self.store, scope, ref)[0] != "harness-composition":
                    raise RuntimeFault("CALIBRATION_PLAN", "A composition ref names another kind")
        if resolve_ref(self.store, scope, plan["corpus_ref"])[0] != "eval-corpus":
            raise RuntimeFault("CALIBRATION_PLAN", "corpus_ref names another kind")
        self._guard(actor, plan)
        self._cases(actor, plan)
        pid = plan_id(plan)
        with self.store.tx() as db:
            ref = self.store.put(db, scope, PLAN_KIND, pid, 1, plan)
            try:
                head = self.store.head(scope, RUN_KIND, pid, db=db)
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
            else:
                if head["data"]["plan_ref"] != ref:
                    raise Hold("CALIBRATION_STATE", "Another plan owns this calibration run")
                return ref
            self.budgets.freeze(db, scope, "calibration:" + pid, ref, plan["budget"])
            self.store.cas(
                db,
                scope,
                RUN_KIND,
                pid,
                0,
                "frozen",
                {
                    "plan_ref": ref,
                    "state": "frozen",
                    "trial_refs": [],
                    "repeats_added": {},
                    "stop_reason": None,
                    "owner_epoch": None,
                },
            )
        return ref

    def run(
        self,
        actor: Actor,
        plan_ref: dict[str, Any],
        executor: Callable[..., TrialObservation],
        *,
        parallel: int = 1,
    ) -> dict[str, Any]:
        self._independent(actor, "experiment.run")
        scope = actor.scope
        plan = self.store.get(scope, PLAN_KIND, plan_ref)
        pid, root = plan_ref["id"], "calibration:" + plan_ref["id"]
        self._guard(actor, plan)
        policy = self._validate_executor(scope)
        workers = effective_parallel(
            parallel, plan["max_parallel"], plan["budget"]["max_parallel_works"]
        )
        cases = self._cases(actor, plan)
        if versions.current_version_ref(self.store, scope) is None:
            # The summary pins the evaluator version; do not spend budget without one.
            raise Hold("EVALUATOR_UNQUALIFIED", "No evaluator version matches the running code")
        head = self.store.head(scope, RUN_KIND, pid)
        if head["state"] != "frozen" or head["data"]["plan_ref"] != plan_ref:
            raise Hold("CALIBRATION_STATE", "A calibration plan runs once")
        with self.store.tx() as db:
            self.store.cas(
                db,
                scope,
                RUN_KIND,
                pid,
                head["row_version"],
                "running",
                {**head["data"], "state": "running", "owner_epoch": self.store.epoch},
            )
        cells = list(plan["cells"])
        compositions = {cell: list(plan["composition_refs"][cell]) for cell in cells}
        v1 = {cell: compositions[cell][0] for cell in cells}
        splits = {c["case_id"]: c["split"] for c in cases}
        low, high = plan["adaptive"]["borderline"]
        max_repeats = plan["adaptive"]["max_repeats"]
        wall = plan["budget"]["max_wall_seconds"]
        started = time.monotonic()
        state: dict[str, Any] = {
            "stop": None,
            "adaptive": False,
            "unknown_usage_streak": 0,
            "not_run": (None, 0),  # (error code, trials in a row not run with it)
        }
        outcomes: dict[tuple[str, str], list[bool | None]] = {
            (cell, c["case_id"]): [] for cell in cells for c in cases
        }
        refs: list[dict[str, Any]] = []

        def stop(reason: str) -> None:
            if state["stop"] is None:
                state["stop"] = reason

        def dispatch(
            item: tuple[dict[str, Any], str, dict[str, Any], int],
        ) -> dict[str, Any] | None:
            case, cell, composition, repeat = item
            try:
                self._guard(actor, plan)
                if time.monotonic() - started >= wall:
                    raise Hold("META_WALL_BUDGET", "Calibration wall budget reached")
                trial_id = new_id("caltrial")
                with self.store.tx() as db:
                    self.budgets.reserve(
                        db,
                        scope,
                        root,
                        trial_id,
                        tokens=policy.max_trial_tokens,
                        cost=policy.max_trial_cost_microunits,
                        cost_compared=False,  # calibration compares no cost (D-088)
                    )
                    self.store.cas(
                        db,
                        scope,
                        TRIAL_KIND,
                        trial_id,
                        0,
                        "dispatching",
                        {
                            "calibration_plan_ref": plan_ref,
                            "task_id": case["case_id"],
                            "cell_id": cell,
                            "repeat": repeat,
                            "owner_epoch": self.store.epoch,
                        },
                    )
            except RuntimeFault as exc:
                stop(exc.code)
                return None
            return {
                "case": case,
                "cell": cell,
                "composition": composition,
                "repeat": repeat,
                "trial_id": trial_id,
            }

        def execute(ctx: dict[str, Any]) -> tuple[TrialObservation, dict[str, Any], float]:
            began = time.monotonic()
            failure: dict[str, Any] = {}
            self.store.assert_outside_tx()
            try:
                observation = executor(ctx["composition"], ctx["case"], ctx["repeat"], MODE)
                validate_observation(
                    self.artifacts,
                    scope,
                    observation,
                    ctx["composition"],
                    ctx["case"]["case_id"],
                    ctx["repeat"],
                    MODE,
                )
            except Exception as exc:
                # an unknown effect unless the executor's evidence shows nothing ran
                # (operator decision 2026-10-09, ``failed_trial``)
                observation, failure = failed_trial(exc)
            return observation, failure, began

        def record(
            ctx: dict[str, Any], result: tuple[TrialObservation, dict[str, Any], float]
        ) -> None:
            observation, failure, began = result
            case, cell, trial_id = ctx["case"], ctx["cell"], ctx["trial_id"]
            post_guard = None
            try:
                self._guard(actor, plan)
            except RuntimeFault as exc:
                post_guard = exc.code
            trial = {
                "schema": TRIAL_SCHEMA,
                "trial_id": trial_id,
                "scope": scope.wire(),
                "calibration_plan_ref": plan_ref,
                "composition_ref": ctx["composition"],
                "cell_id": cell,
                "split": splits[case["case_id"]],
                "task_id": case["case_id"],
                "task_class": case["task_class"],
                "repeat": ctx["repeat"],
                "mode": MODE,
                "executor_id": self.executor_id,
                "qualification_ref": policy.qualification_ref,
                "success": observation.success,
                "safety_failures": observation.safety_failures,
                "unknown_effects": observation.unknown_effects,
                "artifact_refs": list(observation.artifact_refs),
                "cost_microunits": observation.cost_microunits,
                "input_tokens": observation.input_tokens,
                "output_tokens": observation.output_tokens,
                "usage_status": observation.usage_status,
                "elapsed_ms": round((time.monotonic() - began) * 1000, 6),
                "finished_at": now(),
                "error_type": None,
                "post_execution_guard": post_guard,
                **failure,  # error_type, error_code and not_run of an executor exception
            }
            tokens = (
                observation.input_tokens + observation.output_tokens
                if observation.input_tokens is not None and observation.output_tokens is not None
                else None
            )
            cost = observation.cost_microunits
            if usage_unknown(observation):
                # decision (B): charge the reservation, keep running, the outcome is missing
                fields, tokens, cost = unknown_usage_charge(
                    observation,
                    tokens=policy.max_trial_tokens,
                    cost=policy.max_trial_cost_microunits,
                )
                trial.update(fields)
                if "not_run" in failure:
                    trial["outcome_missing"] = "not_run"  # decision 2026-10-09: nothing ran
            if observation.safety_failures and not observation.unknown_effects:
                # operator decision 2026-10-09 (A): calibration measures a cell, and editing an
                # existing test, a protected path or SECRET_DETECTED is part of what it measures,
                # so the trial is FAILED and the run goes on; an experiment still stops (IC-18)
                trial["success"] = False
                trial.pop("outcome_missing", None)
            with self.store.tx() as db:
                ref = self.store.put(db, scope, TRIAL_KIND, trial_id, 1, trial)
                th = self.store.head(scope, TRIAL_KIND, trial_id, db=db)
                self.store.cas(
                    db,
                    scope,
                    TRIAL_KIND,
                    trial_id,
                    th["row_version"],
                    "unknown" if observation.unknown_effects else "observed",
                    {**th["data"], "trial_ref": ref},
                )
                # Calibration compares no cost: a missing cost keeps the reservation (D-088).
                settlement = self.budgets.settle(
                    db,
                    scope,
                    root,
                    trial_id,
                    tokens=tokens,
                    cost=cost,
                    uncertain=bool(observation.unknown_effects),
                    cost_required=False,
                )
                current = self.store.head(scope, RUN_KIND, pid, db=db)
                self.store.cas(
                    db,
                    scope,
                    RUN_KIND,
                    pid,
                    current["row_version"],
                    "running",
                    {**current["data"], "trial_refs": [*current["data"]["trial_refs"], ref]},
                )
            refs.append(ref)
            if ctx["composition"] == v1[cell]:
                # an unknown-usage trial is a missing outcome (``success`` null in its record),
                # unless it attempted an answer lookup: then it is failed (decision (C))
                known = trial["success"] is not None and not observation.unknown_effects
                outcomes[(cell, case["case_id"])].append(trial["success"] if known else None)
            unknown_usage = trial.get("outcome_missing") == "usage_unknown"
            state["unknown_usage_streak"] = (
                state["unknown_usage_streak"] + 1 if unknown_usage else 0
            )
            code = trial.get("error_code") if trial.get("outcome_missing") == "not_run" else None
            last, count = state["not_run"]
            state["not_run"] = (
                code,
                count + 1 if code is not None and code == last else int(code is not None),
            )
            if post_guard:
                stop(post_guard)
            elif observation.unknown_effects:
                stop("safety_or_unknown_effect")
            elif settlement["overrun"] or settlement["uncertain"]:
                stop("budget_overrun_or_unknown_usage")
            elif state["unknown_usage_streak"] >= PROVIDER_FAILURE_STREAK:
                stop("provider_failures")
            elif state["not_run"][1] >= PRE_RUN_FAILURE_STREAK:
                stop("pre_run_failures")

        def rate(cell: str, case_id: str) -> float | None:
            known = [x for x in outcomes[(cell, case_id)] if x is not None]
            return sum(known) / len(known) if known else None

        def needs_repeat(cell: str, case_id: str) -> bool:
            runs = len(outcomes[(cell, case_id)])
            if runs >= max_repeats:
                return False
            rates = {r for c in cells if (r := rate(c, case_id)) is not None}
            own = rate(cell, case_id)
            borderline = runs >= 2 and own is not None and low <= own <= high
            return len(rates) > 1 or borderline

        def items_for(slots: list[tuple[dict[str, Any], str, int]]) -> list[Any]:
            return [
                (case, cell, composition, repeat)
                for case, cell, repeat in slots
                for composition in compositions[cell]
            ]

        slots = [(case, cell, 0) for case in cases for cell in cells]
        while slots and state["stop"] is None:
            run_bounded(
                items_for(slots),
                parallel=workers,
                dispatch=dispatch,
                execute=execute,
                record=record,
                stopped=lambda: state["stop"] is not None,
            )
            if state["stop"] is not None:
                break
            state["adaptive"] = True
            slots = [
                (case, cell, len(outcomes[(cell, case["case_id"])]))
                for case in cases
                for cell in cells
                if needs_repeat(cell, case["case_id"])
            ]
        # Adaptive repeats per task: v1 runs beyond the first, summed over cells.
        added = {
            c["case_id"]: n
            for c in cases
            if (n := sum(max(0, len(outcomes[(cell, c["case_id"])]) - 1) for cell in cells))
        }
        reason: str | None = state["stop"]
        final = (
            "done"
            if reason is None or (state["adaptive"] and reason in BUDGET_STOPS)
            else ("stopped")
        )
        with self.store.tx() as db:
            current = self.store.head(scope, RUN_KIND, pid, db=db)
            if current["state"] != "running" or current["data"]["trial_refs"] != refs:
                raise Hold("CALIBRATION_STATE", "The calibration run changed underneath")
            self.store.cas(
                db,
                scope,
                RUN_KIND,
                pid,
                current["row_version"],
                final,
                {
                    **current["data"],
                    "state": final,
                    "repeats_added": added,
                    "stop_reason": reason,
                },
            )
        if not refs:
            raise Hold(reason or "NO_TRIALS", "No calibration trial dispatched")
        return self.summarize(scope, plan_ref)

    def recover(self, actor: Actor, plan_ref: dict[str, Any]) -> dict[str, Any]:
        """Close a calibration run whose owner process is gone (design 08 recovery, 20 §5 "CP
        crash"). Operator decision 2026-10-10: calplan-0034e3f6 lost its process and stayed
        ``running``, which held every evaluator change with ``EVALUATOR_CHANGE_ACTIVE``.

        Only a run a previous store owner started qualifies (its ``owner_epoch`` differs from this
        store's), so a run this process owns is never closed. Trials still ``dispatching`` become
        ``unknown``: no process-stop claim is made for them (IC-18) and their allocations stay
        reserved. The run ends ``stopped`` with ``owner_lost``. No summary is written: a summary
        pins the evaluator version current when it is written, not the one the trials ran under.
        """
        self._independent(actor, "experiment.run")
        scope, pid = actor.scope, plan_ref["id"]
        with self.store.tx() as db:
            head = self.store.head(scope, RUN_KIND, pid, db=db)
            if (
                head["state"] != "running"
                or head["data"]["plan_ref"] != plan_ref
                or head["data"].get("owner_epoch") == self.store.epoch
            ):
                raise Hold(
                    "CALIBRATION_RECOVERY_STATE",
                    "Recovery needs a running calibration a previous store owner started",
                )
            rows = db.execute(
                "SELECT id,row_version,data FROM heads WHERE tenant=? AND project=? AND kind=? "
                "AND state='dispatching' ORDER BY id",
                (*scope.keys(), TRIAL_KIND),
            ).fetchall()
            unknown = []
            for row in rows:
                data = json.loads(row["data"])
                if data.get("calibration_plan_ref") != plan_ref:
                    continue
                self.store.cas(
                    db, scope, TRIAL_KIND, row["id"], row["row_version"], "unknown",
                    {**data, "recovered": "owner_lost"},
                )  # fmt: skip
                unknown.append(row["id"])
            self.store.cas(
                db,
                scope,
                RUN_KIND,
                pid,
                head["row_version"],
                "stopped",
                {
                    **head["data"],
                    "state": "stopped",
                    "stop_reason": "owner_lost",
                    "recovered_trials": unknown,
                },
            )
        return {
            "plan_id": pid,
            "state": "stopped",
            "stop_reason": "owner_lost",
            "trials": len(head["data"]["trial_refs"]),
            "unknown_trials": unknown,
        }

    def summarize(self, scope: Scope, plan_ref: dict[str, Any]) -> dict[str, Any]:
        """Write (once) and return the `calibration-summary` of a finished run (§2.8)."""
        plan = self.store.get(scope, PLAN_KIND, plan_ref)
        pid = plan_ref["id"]
        head = self.store.head(scope, RUN_KIND, pid)
        if head["state"] not in {"done", "stopped"} or head["data"]["plan_ref"] != plan_ref:
            raise Hold("CALIBRATION_STATE", "Only a finished calibration run is summarized")
        summary_id = "calsum-" + pid
        existing = [
            ref
            for ref, _ in self.store.list_objects(scope, SUMMARY_KIND)
            if ref["id"] == summary_id
        ]
        if existing:
            ref = max(existing, key=lambda r: r["revision"])
            self.store.get(scope, SUMMARY_KIND, ref)  # digest-checked read
            return ref
        version_ref = versions.current_version_ref(self.store, scope)
        if version_ref is None:
            raise Hold("EVALUATOR_UNQUALIFIED", "No evaluator version matches the running code")
        trials = [self.store.get(scope, TRIAL_KIND, ref) for ref in head["data"]["trial_refs"]]
        if any(t["calibration_plan_ref"] != plan_ref for t in trials):
            raise Hold("TRIAL_PROVENANCE", "Foreign trials cannot enter a calibration summary")
        value = {
            "schema": SUMMARY_SCHEMA,
            "scope": scope.wire(),
            "plan_ref": plan_ref,
            **summarize_trials(plan, trials),
            "evaluator_version_ref": version_ref,
            "summarized_at": now(),
        }
        validate_summary(value)
        with self.store.tx() as db:
            return self.store.put(db, scope, SUMMARY_KIND, summary_id, 1, value)
