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

eval-11 (operator decision 3, 2026-10-11, interfaces.md "Clarification: Drift Subset"): a plan
with `"purpose": "drift"` (the nightly drift calibration, `LocalNightlyBackend.drift_run`) may pin
a non-empty subset of those cases (the night's `meta.nightly.drift_tasks`), still distinct and in
corpus order; a case outside them is `SAMPLING_CHANGED` with the unknown ids. Any other plan keeps
the every-case rule. A drift summary is never a cell's calibration baseline or a stage plan's
calibration summary (`is_drift_plan`).

D-128 (operator decision 2026-10-14, interfaces.md "Clarification: Calibration Resume (D-128)"):

- A trial over the per-trial token ceiling is recorded (``ceiling_overrun``, the run head's
  ``overruns``, the summary's ``warnings``) and the run goes on; the root token budget, the wall
  budget and the kill switch still stop it. The ceiling is judged by a cost-weighted
  uncached-equivalent count (``ceiling_tokens``, injected; raw tokens without one), never by the
  ledger tokens, and a recorded overrun does not refuse the next reservation of this root.
- A trial whose executor proves that it started nothing (``not_run``) settles at 0 tokens.
- ``resume`` re-runs a ``stopped`` plan under its own approval: completed graded trials stay,
  unfinished slots run again, and anything behaviour-affecting that differs from the original
  run (evaluator version, harness sha, driver version, image, model, corpus version) holds
  ``CALIBRATION_RESUME_CHANGED`` with the differences.
- A plan may import completed graded round-1 trials of other plans (``imports``, approved with
  the plan) whose reuse key matches exactly (``reuse_key``), only for cases complete in every
  cell and composition, within ``import_policy.window_hours``; each gets provenance and a
  0-charge allocation. A drift check re-runs a sample per cell first and drops a cell's imports
  when the paired outcomes disagree beyond the cell's A/A discordance.
- A summary records ``partial`` (the run did not end ``done``) and ``run_segment``; readers use
  ``complete_summaries`` / ``summary_complete`` and never select a partial one.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from datetime import datetime
from itertools import count
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
# S7b: the app whose cases a plan calibrates (absent: every case of the splits); eval-11: the
# plan's purpose (absent: an ordinary calibration; "drift": the nightly drift calibration);
# D-128: the trials imported from other plans and the rule they are pooled by
OPTIONAL_PLAN_FIELDS = frozenset({"app", "purpose", "imports", "import_policy"})
IMPORT_POLICY_FIELDS = frozenset(
    {"window_hours", "drift_share", "drift_min_per_cell", "drift_reference"}
)
# D-128 defaults (local.json meta.calibration_import overrides them; the plan records the values)
IMPORT_WINDOW_HOURS = 72
DRIFT_SHARE = 0.1
DRIFT_MIN_PER_CELL = 4
# §8.5: a reuse key built without the harness sha is never reused (local_executor.UNKNOWN_SHA)
UNKNOWN_SHA = "unknown"
# the receipt facts a reuse key binds besides the plan's own refs
FACT_FIELDS = ("harness_sha", "model_snapshot", "corpus_version")
SUMMARY_FIELDS = frozenset(
    {
        "schema",
        "scope",
        "plan_ref",
        "cells",
        "saturated_everywhere",
        "informative_any",
        "evaluator_version_ref",
        "summarized_at",
    }
)
# D-128: a summary may also say that its run did not end done, which run segment it covers, the
# per-trial ceiling overruns, the imports and the drift check
OPTIONAL_SUMMARY_FIELDS = frozenset(
    {"partial", "run_segment", "warnings", "imports", "drift_check"}
)
DRIFT_PURPOSE = "drift"
PURPOSES = (DRIFT_PURPOSE,)
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
    if "purpose" in plan and plan["purpose"] not in PURPOSES:
        raise RuntimeFault("CALIBRATION_PLAN", "purpose is absent or one of " + ", ".join(PURPOSES))
    _validate_imports(plan)
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


def _validate_imports(plan: dict[str, Any]) -> None:
    """D-128 shape: ``imports`` is a non-empty list of ``{"plan_ref": ref, "trial_refs": [ref]}``
    (distinct plans, distinct non-empty trial refs) and comes with ``import_policy``
    ``{"window_hours": 1..8760, "drift_share": (0, 1], "drift_min_per_cell": >= 1,
    "drift_reference": {cell: [0, 1] | null}}`` naming every cell; neither field without the
    other. A drift plan imports nothing."""
    if "imports" not in plan and "import_policy" not in plan:
        return
    imports, policy = plan.get("imports"), plan.get("import_policy")
    entries_ok = (
        isinstance(imports, list)
        and bool(imports)
        and all(
            isinstance(e, dict)
            and set(e) == {"plan_ref", "trial_refs"}
            and is_ref(e["plan_ref"])
            and isinstance(e["trial_refs"], list)
            and bool(e["trial_refs"])
            and all(is_ref(r) for r in e["trial_refs"])
            and len({digest(r) for r in e["trial_refs"]}) == len(e["trial_refs"])
            for e in imports
        )
        and len({digest(e["plan_ref"]) for e in imports}) == len(imports)
    )
    cells: list[Any] = plan["cells"] if isinstance(plan.get("cells"), list) else []
    reference = policy.get("drift_reference") if isinstance(policy, dict) else None
    share: Any = policy.get("drift_share") if isinstance(policy, dict) else None
    policy_ok = (
        isinstance(policy, dict)
        and set(policy) == IMPORT_POLICY_FIELDS
        and _count(policy["window_hours"], 1, 8760)
        and type(share) in (int, float)
        and math.isfinite(share)
        and 0 < share <= 1
        and _count(policy["drift_min_per_cell"], 1, 1000)
        and isinstance(reference, dict)
        and set(reference) == set(cells)
        and all(
            v is None or (type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1)
            for v in reference.values()
        )
    )
    if not (entries_ok and policy_ok) or is_drift_plan(plan):
        raise RuntimeFault(
            "CALIBRATION_PLAN",
            "imports name source plans and their trials, with import_policy (D-128)",
        )


def reuse_key(
    *,
    composition_ref: dict[str, Any],
    cell_id: str,
    task_id: str,
    corpus_ref: dict[str, Any],
    facts: dict[str, Any],
    evaluator_version_ref: dict[str, Any] | None,
) -> dict[str, Any]:
    """D-128: what an imported trial must share with the slot it fills. The composition ref pins
    the cell's driver, model, effort, image and its ``limits`` component; ``harness_sha`` pins
    the code, including the deployment's goal-budget ceiling (``product.Budget``); the receipt's
    ``model_snapshot`` the driver version, model id and image that ran; ``corpus_ref``,
    ``corpus_version`` and ``task_id`` the case; the evaluator version the trial-record rules.
    The per-trial token ceiling and the root budgets are accounting only (the executor never
    reads them) and are not part of it."""
    return {
        "composition_ref": composition_ref,
        "cell_id": cell_id,
        "task_id": task_id,
        "corpus_ref": corpus_ref,
        **{k: facts.get(k) for k in FACT_FIELDS},
        "evaluator_version_ref": evaluator_version_ref,
        "mode": MODE,
    }


def key_differences(was: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """The fields of two reuse keys that differ, plus the ones that can never match: an unknown
    harness sha (§8.5) or an unknown evaluator version."""
    fields = [k for k in current if was.get(k) != current.get(k)]
    for key in (was, current):
        if key.get("harness_sha") in (None, UNKNOWN_SHA) and "harness_sha" not in fields:
            fields.append("harness_sha")
        if key.get("evaluator_version_ref") is None and "evaluator_version_ref" not in fields:
            fields.append("evaluator_version_ref")
    return fields


def _instant(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def summary_complete(summary: Any, run_state: str | None, run_plan_ref: Any) -> bool:
    """D-128: a reader may select a summary only when its run ended ``done`` (not ``stopped``,
    ``owner_lost`` or still running) and it is not marked ``partial``."""
    return (
        isinstance(summary, dict)
        and summary.get("partial") is not True
        and run_state == "done"
        and run_plan_ref == summary.get("plan_ref")
    )


def complete_summaries(store: Store, scope: Scope) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(ref, value) of the newest revision of every calibration summary whose run ended ``done``
    (``summary_complete``). A summary whose run head cannot be read is left out (fail closed)."""
    newest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for ref, value in store.list_objects(scope, SUMMARY_KIND):
        if ref["id"] not in newest or ref["revision"] > newest[ref["id"]][0]["revision"]:
            newest[ref["id"]] = (ref, value)
    out = []
    for ref, value in newest.values():
        try:
            head = store.head(scope, RUN_KIND, value["plan_ref"]["id"])
        except (RuntimeFault, KeyError, TypeError):
            continue
        if summary_complete(value, head["state"], head["data"].get("plan_ref")):
            out.append((ref, value))
    return out


def is_drift_plan(plan: dict[str, Any]) -> bool:
    """eval-11: the plan is a drift calibration (``"purpose": "drift"``). It may pin a subset of
    the cases; its summary is never a cell's calibration baseline."""
    return isinstance(plan, dict) and plan.get("purpose") == DRIFT_PURPOSE


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
        and SUMMARY_FIELDS <= set(value) <= SUMMARY_FIELDS | OPTIONAL_SUMMARY_FIELDS
        and value["schema"] == SUMMARY_SCHEMA
        and ("partial" not in value or type(value["partial"]) is bool)
        and ("run_segment" not in value or _count(value["run_segment"], 0, 1_000_000))
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


def drift_verdict(
    sampled: int, flips: list[bool], reference: float | None
) -> tuple[str, float | None]:
    """D-128: the drift check of one cell's imports, from the paired outcomes of its sampled
    tasks (``flips``: the imported round-1 outcome differs from the new run's).

    ``inconclusive`` when fewer than half of the ``sampled`` pairs (at least one) were compared
    (a re-run without a graded outcome is no comparison). Otherwise ``fail`` when the 95 % Wilson
    lower bound of flips / compared exceeds ``reference``, the cell's A/A discordance (repeat 0
    against repeat 1 of the same task, as ``aa_discordance``) in the source runs; without a
    measured discordance the reference is 0, so any flip fails. Returns (verdict, lower bound)."""
    compared = len(flips)
    if compared == 0 or compared < math.ceil(sampled / 2):
        return "inconclusive", None
    lower = wilson(sum(flips), compared, _Z95)[0]
    return ("fail" if lower > (reference or 0.0) else "pass"), lower


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
        facts: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
        ceiling_tokens: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        """D-128: ``facts(composition_ref, case)`` gives what a trial of that slot would run on
        now (``harness_sha``, ``model_snapshot``, ``corpus_version``; ``LocalTrialExecutor.facts``),
        which resume and imports compare with stored receipts (none: both hold
        ``CALIBRATION_FACTS_UNAVAILABLE``). ``ceiling_tokens(trial)`` gives the cost-weighted
        uncached-equivalent count the per-trial ceiling is judged by (``{"tokens", "basis":
        "price_weighted", "price_table_id"}``, or ``{"basis": "raw", "reason"}``); none judges
        the raw tokens."""
        self.store, self.contracts, self.artifacts = store, contracts, artifacts
        self.approval_check, self.executor_id = approval_check, executor_id
        self.executor_policy = executor_policy
        self.facts, self.ceiling_tokens = facts, ceiling_tokens
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
        if is_drift_plan(plan):
            return self._drift_cases(plan, cases)
        if not cases or plan["case_ids"] != [c["case_id"] for c in cases]:
            raise Hold(
                "SAMPLING_CHANGED",
                "Pin every case of the calibrated splits (of the plan's app) in corpus order",
            )
        return cases

    @staticmethod
    def _drift_cases(plan: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """eval-11: a drift plan pins a non-empty subset of ``cases``, distinct and in corpus
        order; a case outside them is SAMPLING_CHANGED with the unknown ids."""
        pinned = plan["case_ids"]
        known = {c["case_id"] for c in cases}
        unknown = [cid for cid in pinned if not isinstance(cid, str) or cid not in known]
        if unknown:
            raise Hold(
                "SAMPLING_CHANGED",
                "A drift plan pins cases of the calibrated splits (of the plan's app) only",
                details={"unknown": unknown},
            )
        chosen = set(pinned)
        subset = [c for c in cases if c["case_id"] in chosen]
        if not subset or pinned != [c["case_id"] for c in subset]:
            raise Hold(
                "SAMPLING_CHANGED",
                "A drift plan pins a non-empty subset of distinct cases in corpus order",
            )
        return subset

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
        if "imports" in plan:
            if plan["adaptive"]["max_repeats"] < 2:
                raise RuntimeFault(
                    "CALIBRATION_PLAN",
                    "A plan with imports needs max_repeats >= 2: its drift check re-runs a "
                    "sample of the imported tasks (D-128)",
                )
            self._import_rows(scope, plan)
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
        pid = plan_ref["id"]
        self._guard(actor, plan)
        policy = self._validate_executor(scope)
        workers = effective_parallel(
            parallel, plan["max_parallel"], plan["budget"]["max_parallel_works"]
        )
        cases = self._cases(actor, plan)
        version_ref = versions.current_version_ref(self.store, scope)
        if version_ref is None:
            # The summary pins the evaluator version; do not spend budget without one.
            raise Hold("EVALUATOR_UNQUALIFIED", "No evaluator version matches the running code")
        head = self.store.head(scope, RUN_KIND, pid)
        if head["state"] != "frozen" or head["data"]["plan_ref"] != plan_ref:
            raise Hold("CALIBRATION_STATE", "A calibration plan runs once")
        # D-128: the imports are checked again against what would run now, before anything runs
        rows = self._import_rows(scope, plan) if "imports" in plan else []
        with self.store.tx() as db:
            self.store.cas(
                db,
                scope,
                RUN_KIND,
                pid,
                head["row_version"],
                "running",
                {
                    **head["data"],
                    "state": "running",
                    "owner_epoch": self.store.epoch,
                    # D-128: the evaluator version the trials run under (resume and imports
                    # compare it); the run segment a summary covers
                    "evaluator_version_ref": version_ref,
                    "segment": 0,
                },
            )
        if rows:
            self._materialize(scope, plan_ref, plan, rows)
        return self._drive(actor, plan_ref, plan, policy, workers, cases, executor)

    # -- D-128: resume --------------------------------------------------------------------------
    def resume(
        self,
        actor: Actor,
        plan_ref: dict[str, Any],
        executor: Callable[..., TrialObservation],
        *,
        parallel: int = 1,
    ) -> dict[str, Any]:
        """Run a ``stopped`` calibration plan again under its own approval (D-128, Harbor
        ``jobs resume``): completed graded trials stay, every unfinished slot runs again, the
        outcomes are rebuilt from the stored trials and the summary is written as a new revision.

        Hold ``CALIBRATION_RESUME_STATE`` (not stopped), ``CALIBRATION_RESUME_UNRESOLVED`` (the
        root has reserved or unknown allocations: reconcile them first), ``META_WALL_BUDGET``
        (the approved wall time since the freeze is spent; resume never resets it) and
        ``CALIBRATION_RESUME_CHANGED`` (``details.differences``: the evaluator version, or a kept
        trial's harness sha, model snapshot or corpus version differs from what would run now)."""
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
        version_ref = versions.current_version_ref(self.store, scope)
        if version_ref is None:
            raise Hold("EVALUATOR_UNQUALIFIED", "No evaluator version matches the running code")
        head = self.store.head(scope, RUN_KIND, pid)
        if head["state"] != "stopped" or head["data"]["plan_ref"] != plan_ref:
            raise Hold(
                "CALIBRATION_RESUME_STATE",
                "Only a stopped calibration run is resumed",
                details={"state": head["state"]},
            )
        budget = self.store.head(scope, "meta-budget", root)["data"]
        unresolved = [
            {
                "allocation_id": key,
                "status": value["status"],
                "owner_epoch": value.get("owner_epoch"),
            }
            for key, value in sorted(budget["allocations"].items())
            if value["status"] in ("reserved", "unknown")
        ]
        if unresolved:
            raise Hold(
                "CALIBRATION_RESUME_UNRESOLVED",
                "Reconcile the root's reserved or unknown allocations first "
                "(amplai meta reconcile)",
                details={"root": root, "allocations": unresolved},
            )
        elapsed = self.store.clock() - budget["started_epoch"]
        if elapsed >= budget["limits"]["max_wall_seconds"]:
            raise Hold(
                "META_WALL_BUDGET",
                "The plan's approved wall time since its freeze is spent; import its trials "
                "into a new plan instead",
                details={"elapsed_seconds": round(elapsed),
                         "max_wall_seconds": budget["limits"]["max_wall_seconds"]},
            )  # fmt: skip
        kept: list[tuple[dict[str, Any], dict[str, Any]]] = []
        superseded: list[dict[str, Any]] = []
        for ref in head["data"]["trial_refs"]:
            trial = self.store.get(scope, TRIAL_KIND, ref)
            state = self.store.head(scope, TRIAL_KIND, ref["id"])["state"]
            if state == "observed" and _known(trial):
                kept.append((ref, trial))
            else:
                superseded.append(ref)
        differences = self._resume_differences(scope, plan, pid, [t for _, t in kept], version_ref)
        if differences:
            raise Hold(
                "CALIBRATION_RESUME_CHANGED",
                "What would run now differs from the original run; run a new plan",
                details={"differences": differences},
            )
        with self.store.tx() as db:
            current = self.store.head(scope, RUN_KIND, pid, db=db)
            if current["row_version"] != head["row_version"]:
                raise Hold("CALIBRATION_STATE", "The calibration run changed underneath")
            data = current["data"]
            self.store.cas(
                db,
                scope,
                RUN_KIND,
                pid,
                current["row_version"],
                "running",
                {
                    **data,
                    "state": "running",
                    "owner_epoch": self.store.epoch,
                    "trial_refs": [ref for ref, _ in kept],
                    "superseded_trial_refs": [*data.get("superseded_trial_refs", []), *superseded],
                    "stop_reason": None,
                    "segment": int(data.get("segment", 0)) + 1,
                    "evaluator_version_ref": version_ref,
                    "resumes": [
                        *data.get("resumes", []),
                        {"at": now(), "from_stop_reason": data.get("stop_reason"),
                         "kept": len(kept), "superseded": len(superseded),
                         "owner_epoch": self.store.epoch},
                    ],
                },
            )  # fmt: skip
        return self._drive(actor, plan_ref, plan, policy, workers, cases, executor)

    def _run_evaluator(self, scope: Scope, pid: str) -> dict[str, Any] | None:
        """The evaluator version a run's trials ran under: the run head's (written at its start
        since D-128), else that of the plan's newest summary (``run`` writes it in the same
        process); None when neither exists (a run ``recover`` closed before D-128)."""
        try:
            head = self.store.head(scope, RUN_KIND, pid)
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None
        found = head["data"].get("evaluator_version_ref")
        if found is not None:
            return dict(found)
        rows = [
            (ref, value)
            for ref, value in self.store.list_objects(scope, SUMMARY_KIND)
            if ref["id"] == "calsum-" + pid
        ]
        if not rows:
            return None
        newest = max(rows, key=lambda row: row[0]["revision"])[1].get("evaluator_version_ref")
        return dict(newest) if isinstance(newest, dict) else None

    def _receipt_facts(self, scope: Scope, trial: dict[str, Any]) -> dict[str, Any]:
        """The reuse-key facts a stored trial's receipt records (§2.10); empty when it has none."""
        refs = trial.get("artifact_refs") or []
        receipt: Any = {}
        if refs:
            try:
                receipt = json.loads(self.artifacts.read(scope, refs[0], trusted=True))
            except (RuntimeFault, ValueError, TypeError, KeyError):
                receipt = {}
        receipt = receipt if isinstance(receipt, dict) else {}
        return {k: receipt.get(k) for k in FACT_FIELDS}

    def _current_facts(
        self, composition_ref: dict[str, Any], case: dict[str, Any] | None
    ) -> dict[str, Any]:
        if self.facts is None:
            raise Hold(
                "CALIBRATION_FACTS_UNAVAILABLE",
                "No executor facts to compare stored trials with (D-128)",
            )
        if case is None:
            raise Hold("SAMPLING_CHANGED", "The case is not in the plan's frozen corpus")
        return dict(self.facts(composition_ref, case))

    def _frozen_cases(self, scope: Scope, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        corpus = self.store.get(scope, "eval-corpus", plan["corpus_ref"])
        return {c["case_id"]: c for c in corpus["cases"]}

    def _resume_differences(
        self,
        scope: Scope,
        plan: dict[str, Any],
        pid: str,
        kept: list[dict[str, Any]],
        version_ref: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """What differs between the original run and what would run now: the evaluator version,
        and per kept trial its receipt's harness sha, model snapshot and corpus version."""
        differences: list[dict[str, Any]] = []
        was = self._run_evaluator(scope, pid)
        if was is None or was != version_ref:
            differences.append({"field": "evaluator_version_ref", "was": was, "now": version_ref})
        cases = self._frozen_cases(scope, plan)
        cache: dict[tuple[str, str], dict[str, Any]] = {}
        seen: set[str] = set()
        for trial in kept:
            key = (digest(trial["composition_ref"]), trial["task_id"])
            if key not in cache:
                cache[key] = self._current_facts(trial["composition_ref"], cases.get(key[1]))
            stored, current = self._receipt_facts(scope, trial), cache[key]
            for field in FACT_FIELDS:
                a, b = stored.get(field), current.get(field)
                if a == b and not (field == "harness_sha" and a in (None, UNKNOWN_SHA)):
                    continue
                row = {"field": field, "cell_id": trial["cell_id"], "was": a, "now": b}
                mark = digest(row)
                if mark not in seen:
                    seen.add(mark)
                    differences.append(row)
        return differences

    # -- D-128: imports -------------------------------------------------------------------------
    def _import_candidates(
        self,
        scope: Scope,
        plan: dict[str, Any],
        sources: list[tuple[dict[str, Any], list[dict[str, Any]] | None]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(eligible rows, excluded rows) of the trials ``sources`` name (each a source plan ref
        and its listed trial refs; None: every trial of its run) for ``plan``.

        A trial is eligible when it is in its source run, ``observed`` and graded (``_known``),
        not itself imported, round 1 (repeat 0), of a composition the plan names for its cell,
        of a case the plan pins, finished at most ``import_policy.window_hours`` before the
        plan's ``frozen_at``, and its ``reuse_key`` equals the slot's now. Then only cases with
        one eligible trial for every (cell, composition) of the plan stay (the paired rule)."""
        policy = plan["import_policy"]
        window = policy["window_hours"] * 3600
        frozen = _instant(plan["frozen_at"])
        current_ev = versions.current_version_ref(self.store, scope)
        cells = list(plan["cells"])
        comps = {cell: list(plan["composition_refs"][cell]) for cell in cells}
        pinned = set(plan["case_ids"])
        cases = self._frozen_cases(scope, plan)
        facts: dict[tuple[str, str], dict[str, Any]] = {}
        eligible: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []

        def out(trial_id: Any, reason: str, **details: Any) -> None:
            excluded.append({"trial_id": trial_id, "reason": reason, **details})

        for source_ref, listed in sources:
            try:
                kind, source = resolve_ref(self.store, scope, source_ref)
            except (Hold, RuntimeFault):
                kind, source = "", {}
            try:
                head = self.store.head(scope, RUN_KIND, source_ref["id"])
            except RuntimeFault:
                head = None
            if kind != PLAN_KIND or head is None or head["data"].get("plan_ref") != source_ref:
                out(None, "source_not_a_calibration_run", plan_ref=source_ref)
                continue
            run_refs = list(head["data"].get("trial_refs") or [])
            source_ev = self._run_evaluator(scope, source_ref["id"])
            for ref in run_refs if listed is None else listed:
                trial_id = ref.get("id")
                if ref not in run_refs:
                    out(trial_id, "not_in_source_run")
                    continue
                trial = self.store.get(scope, TRIAL_KIND, ref)
                state = self.store.head(scope, TRIAL_KIND, ref["id"])["state"]
                cell, task_id = trial["cell_id"], trial["task_id"]
                finished = _instant(trial.get("finished_at"))
                if state != "observed" or not _known(trial):
                    out(trial_id, "not_graded")
                elif "imported_from" in trial:
                    out(trial_id, "already_imported")
                elif trial["repeat"] != 0:
                    out(trial_id, "not_round_one")
                elif cell not in comps or trial["composition_ref"] not in comps[cell]:
                    out(trial_id, "composition_mismatch")
                elif task_id not in pinned:
                    out(trial_id, "task_not_in_plan")
                elif (
                    frozen is None
                    or finished is None
                    or not 0 <= (frozen - finished).total_seconds() <= window
                ):
                    out(trial_id, "outside_window", finished_at=trial.get("finished_at"))
                else:
                    slot = (digest(trial["composition_ref"]), task_id)
                    if slot not in facts:
                        try:
                            facts[slot] = self._current_facts(
                                trial["composition_ref"], cases.get(task_id)
                            )
                        except (Hold, RuntimeFault) as exc:
                            if exc.code == "CALIBRATION_FACTS_UNAVAILABLE":
                                raise
                            facts[slot] = {"unavailable": exc.code}
                    stored = reuse_key(
                        composition_ref=trial["composition_ref"], cell_id=cell, task_id=task_id,
                        corpus_ref=source["corpus_ref"],
                        facts=self._receipt_facts(scope, trial), evaluator_version_ref=source_ev,
                    )  # fmt: skip
                    current = reuse_key(
                        composition_ref=trial["composition_ref"], cell_id=cell, task_id=task_id,
                        corpus_ref=plan["corpus_ref"], facts=facts[slot],
                        evaluator_version_ref=current_ev,
                    )  # fmt: skip
                    fields = key_differences(stored, current)
                    if fields:
                        out(trial_id, "key_mismatch", fields=sorted(fields))
                        continue
                    eligible.append(
                        {"trial_ref": ref, "trial": trial, "source_plan_ref": source_ref,
                         "cell_id": cell, "task_id": task_id,
                         "slot": (cell, digest(trial["composition_ref"])),
                         "key_digest": digest(current)}
                    )  # fmt: skip
        required = {(cell, digest(c)) for cell in cells for c in comps[cell]}
        kept: list[dict[str, Any]] = []
        for task_id in plan["case_ids"]:  # corpus order
            chosen: dict[tuple[str, str], dict[str, Any]] = {}
            for row in (r for r in eligible if r["task_id"] == task_id):
                if row["slot"] in chosen:
                    out(row["trial_ref"]["id"], "duplicate_slot")
                else:
                    chosen[row["slot"]] = row
            if set(chosen) != required:
                for row in chosen.values():
                    out(row["trial_ref"]["id"], "partial_case")
                continue
            kept.extend(chosen.values())
        return kept, excluded

    def eligible_imports(
        self, scope: Scope, plan: dict[str, Any], source_refs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """For an operator composing a plan before its approval: the ``imports`` value a plan
        like ``plan`` (with ``import_policy``) gets from the runs of ``source_refs``, the number
        imported, the imported cases, and every excluded trial with its reason."""
        kept, excluded = self._import_candidates(scope, plan, [(r, None) for r in source_refs])
        imports = []
        for source in source_refs:
            refs = [row["trial_ref"] for row in kept if row["source_plan_ref"] == source]
            if refs:
                imports.append({"plan_ref": source, "trial_refs": refs})
        cases = {row["task_id"] for row in kept}
        return {
            "imports": imports,
            "imported": len(kept),
            "cases": [cid for cid in plan["case_ids"] if cid in cases],
            "excluded": excluded,
        }

    def drift_reference(
        self, scope: Scope, plan: dict[str, Any], source_refs: list[dict[str, Any]]
    ) -> dict[str, float | None]:
        """Per cell, the A/A discordance of its v1 composition in the source runs (graded
        repeat 0 against repeat 1 of the same task, pooled over the sources); None when no task
        has both. ``drift_verdict`` compares the drift check with it."""
        reference: dict[str, float | None] = {}
        trials = []
        for source in source_refs:
            try:
                head = self.store.head(scope, RUN_KIND, source["id"])
            except RuntimeFault:
                continue
            trials += [self.store.get(scope, TRIAL_KIND, r) for r in head["data"]["trial_refs"]]
        for cell in plan["cells"]:
            v1 = plan["composition_refs"][cell][0]
            firsts: dict[tuple[str, str], dict[int, bool]] = {}
            for t in trials:
                if (
                    t["cell_id"] == cell
                    and t["composition_ref"] == v1
                    and t["repeat"] in (0, 1)
                    and _known(t)
                    and "imported_from" not in t
                ):
                    key = (digest(t["calibration_plan_ref"]), t["task_id"])
                    firsts.setdefault(key, {})[t["repeat"]] = t["success"]
            pairs = [f[0] != f[1] for f in firsts.values() if len(f) == 2]
            reference[cell] = sum(pairs) / len(pairs) if pairs else None
        return reference

    def _import_rows(self, scope: Scope, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """The plan's imports, every one eligible (Hold CALIBRATION_IMPORT with
        ``details.excluded`` otherwise)."""
        sources: list[tuple[dict[str, Any], list[dict[str, Any]] | None]] = [
            (e["plan_ref"], list(e["trial_refs"])) for e in plan["imports"]
        ]
        kept, excluded = self._import_candidates(scope, plan, sources)
        if excluded:
            raise Hold(
                "CALIBRATION_IMPORT",
                "An imported trial is not eligible for this plan (D-128)",
                details={"excluded": excluded},
            )
        return kept

    def _materialize(
        self,
        scope: Scope,
        plan_ref: dict[str, Any],
        plan: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> None:
        """Store each imported trial as a trial of this plan with its provenance
        (``imported_from``: source plan, trial ref and id, finish time, reuse key digest) and a
        0-charge allocation naming its source; choose each cell's drift-check sample."""
        pid, root = plan_ref["id"], "calibration:" + plan_ref["id"]
        imported = [cid for cid in plan["case_ids"] if any(r["task_id"] == cid for r in rows)]
        policy = plan["import_policy"]
        n = len(imported)
        k = min(n, max(policy["drift_min_per_cell"], math.ceil(policy["drift_share"] * n)))
        drift: dict[str, Any] = {}
        for cell in plan["cells"]:
            order = sorted(
                imported, key=lambda cid: digest({"plan_id": pid, "cell_id": cell, "task_id": cid})
            )
            chosen = set(order[:k])
            drift[cell] = {
                "status": "pending",
                "sample": [cid for cid in imported if cid in chosen],
                "reference": policy["drift_reference"].get(cell),
            }
        refs = []
        with self.store.tx() as db:
            for row in rows:
                source, trial_id = row["trial"], new_id("caltrial")
                provenance = {
                    "plan_ref": row["source_plan_ref"],
                    "trial_ref": row["trial_ref"],
                    "trial_id": source["trial_id"],
                    "finished_at": source.get("finished_at"),
                    "key_digest": row["key_digest"],
                }
                trial = {
                    **source,
                    "trial_id": trial_id,
                    "calibration_plan_ref": plan_ref,
                    "imported_from": provenance,
                }
                ref = self.store.put(db, scope, TRIAL_KIND, trial_id, 1, trial)
                self.store.cas(
                    db, scope, TRIAL_KIND, trial_id, 0, "observed",
                    {"calibration_plan_ref": plan_ref, "task_id": source["task_id"],
                     "cell_id": source["cell_id"], "repeat": source["repeat"],
                     "owner_epoch": self.store.epoch, "trial_ref": ref,
                     "imported_from": provenance},
                )  # fmt: skip
                self.budgets.record_import(db, scope, root, trial_id, source=provenance)
                refs.append(ref)
            head = self.store.head(scope, RUN_KIND, pid, db=db)
            self.store.cas(
                db,
                scope,
                RUN_KIND,
                pid,
                head["row_version"],
                "running",
                {
                    **head["data"],
                    "trial_refs": [*head["data"]["trial_refs"], *refs],
                    "imports": {
                        "sources": [e["plan_ref"] for e in plan["imports"]],
                        "trial_refs": refs,
                        "dropped": [],
                    },
                    "drift_check": drift,
                },
            )

    # -- the run loop ---------------------------------------------------------------------------
    def _judge(self, trial: dict[str, Any], tokens: int | None) -> dict[str, Any]:
        """D-128: the count the per-trial ceiling is judged by. A trial with known usage is
        weighed by ``ceiling_tokens`` (cache reads and writes at their price relative to the
        uncached input price of the deployment's price table); an unknown usage (its charge), a
        trial without a weigher or one the weigher cannot price is judged by its raw tokens, and
        the record says why."""
        raw: dict[str, Any] = {"tokens": tokens, "basis": "raw", "price_table_id": None}
        if tokens is None or trial.get("charged_tokens") is not None:
            return {**raw, "reason": "usage_unknown"}
        if self.ceiling_tokens is None:
            return {**raw, "reason": "no_price_weigher"}
        try:
            found = self.ceiling_tokens(trial)
        except (Hold, RuntimeFault, ValueError, KeyError, TypeError) as exc:
            return {
                **raw,
                "reason": "weigher_error:" + str(getattr(exc, "code", type(exc).__name__)),
            }
        weighed = found.get("tokens") if isinstance(found, dict) else None
        if found.get("basis") == "price_weighted" and type(weighed) is int and weighed >= 0:
            return {
                "tokens": weighed,
                "basis": "price_weighted",
                "price_table_id": found.get("price_table_id"),
                "reason": None,
            }
        return {**raw, "reason": str((found or {}).get("reason") or "unpriced")}

    def _drive(
        self,
        actor: Actor,
        plan_ref: dict[str, Any],
        plan: dict[str, Any],
        policy: ExecutorPolicy,
        workers: int,
        cases: list[dict[str, Any]],
        executor: Callable[..., TrialObservation],
    ) -> dict[str, Any]:
        """Run a ``running`` calibration from the trials its head already holds (none for a new
        run; imported ones; the kept ones of a resumed run): the drift check of pending imports,
        round 1 for every slot without a v1 outcome, then the adaptive rounds (§8.2)."""
        scope = actor.scope
        pid, root = plan_ref["id"], "calibration:" + plan_ref["id"]
        cells = list(plan["cells"])
        compositions = {cell: list(plan["composition_refs"][cell]) for cell in cells}
        v1 = {cell: compositions[cell][0] for cell in cells}
        splits = {c["case_id"]: c["split"] for c in cases}
        by_id = {c["case_id"]: c for c in cases}
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
        refs: list[dict[str, Any]] = list(
            self.store.head(scope, RUN_KIND, pid)["data"]["trial_refs"]
        )
        loaded: dict[str, dict[str, Any]] = {}
        outcomes: dict[tuple[str, str], list[bool | None]] = {}
        used: dict[tuple[str, str], set[int]] = {}
        have: dict[tuple[str, str, str], set[int]] = {}

        def load(ref: dict[str, Any]) -> dict[str, Any]:
            if ref["id"] not in loaded:
                loaded[ref["id"]] = self.store.get(scope, TRIAL_KIND, ref)
            return loaded[ref["id"]]

        def note(trial: dict[str, Any]) -> None:
            cell, case_id = trial["cell_id"], trial["task_id"]
            if (cell, case_id) not in outcomes:
                return
            used[(cell, case_id)].add(trial["repeat"])
            have.setdefault((cell, case_id, digest(trial["composition_ref"])), set()).add(
                trial["repeat"]
            )
            if trial["composition_ref"] == v1[cell]:
                # an unknown-usage trial is a missing outcome (``success`` null in its record),
                # unless it attempted an answer lookup: then it is failed (decision (C))
                outcomes[(cell, case_id)].append(trial["success"] if _known(trial) else None)

        def rebuild() -> None:
            outcomes.clear()
            used.clear()
            have.clear()
            for cell in cells:
                for c in cases:
                    outcomes[(cell, c["case_id"])] = []
                    used[(cell, c["case_id"])] = set()
            for trial in sorted((load(r) for r in refs), key=lambda t: t["repeat"]):
                note(trial)

        rebuild()

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
                        # D-128: a recorded per-trial overrun never refuses the next trial
                        prior_overrun_blocks=False,
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
                # decision (B): charge the reservation, keep running, the outcome is missing;
                # D-128: a trial that provably started nothing is charged 0
                fields, tokens, cost = unknown_usage_charge(
                    observation,
                    tokens=policy.max_trial_tokens,
                    cost=policy.max_trial_cost_microunits,
                    ran="not_run" not in failure,
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
            # D-128: the per-trial ceiling is judged by the cost-weighted count and recorded;
            # an overrun no longer stops the run
            judged = self._judge(trial, tokens)
            over = judged["tokens"] is not None and judged["tokens"] > policy.max_trial_tokens
            trial["ceiling_tokens"] = judged
            trial["ceiling_overrun"] = over
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
                    judged_tokens=judged["tokens"],
                )
                current = self.store.head(scope, RUN_KIND, pid, db=db)
                data = {**current["data"], "trial_refs": [*current["data"]["trial_refs"], ref]}
                if over:
                    data["overruns"] = [
                        *current["data"].get("overruns", []),
                        {"trial_id": trial_id, "cell_id": cell, "task_id": case["case_id"],
                         "repeat": ctx["repeat"], "tokens": tokens,
                         "ceiling_tokens": judged["tokens"], "basis": judged["basis"],
                         "ceiling": policy.max_trial_tokens},
                    ]  # fmt: skip
                self.store.cas(db, scope, RUN_KIND, pid, current["row_version"], "running", data)
            refs.append(ref)
            loaded[trial_id] = trial
            note(trial)
            unknown_usage = trial.get("outcome_missing") == "usage_unknown"
            state["unknown_usage_streak"] = (
                state["unknown_usage_streak"] + 1 if unknown_usage else 0
            )
            code = trial.get("error_code") if trial.get("outcome_missing") == "not_run" else None
            last, count_ = state["not_run"]
            state["not_run"] = (
                code,
                count_ + 1 if code is not None and code == last else int(code is not None),
            )
            if post_guard:
                stop(post_guard)
            elif observation.unknown_effects:
                stop("safety_or_unknown_effect")
            elif settlement["uncertain"]:
                stop("unknown_usage")
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

        def next_repeat(cell: str, case_id: str) -> int:
            """The first repeat index the (cell, task) has no trial at (``len(outcomes)`` in a
            run without imports or superseded trials)."""
            return next(n for n in count() if n not in used[(cell, case_id)])

        def slot(case: dict[str, Any], cell: str, repeat: int) -> list[Any]:
            """The compositions of the cell that have no trial in this slot (all of them for a
            new slot; the variants paired with an imported or kept v1 trial)."""
            return [
                (case, cell, composition, repeat)
                for composition in compositions[cell]
                if repeat not in have.get((cell, case["case_id"], digest(composition)), set())
            ]

        def run_items(items: list[Any]) -> None:
            run_bounded(
                items,
                parallel=workers,
                dispatch=dispatch,
                execute=execute,
                record=record,
                stopped=lambda: state["stop"] is not None,
            )

        def pool_v1(cell: str, case_id: str) -> list[dict[str, Any]]:
            return sorted(
                (
                    t
                    for t in (load(r) for r in refs)
                    if t["cell_id"] == cell
                    and t["task_id"] == case_id
                    and t["composition_ref"] == v1[cell]
                ),
                key=lambda t: t["repeat"],
            )

        # D-128: the drift check of the imports, before they are pooled
        drift = dict(self.store.head(scope, RUN_KIND, pid)["data"].get("drift_check") or {})
        pending = [cell for cell in cells if (drift.get(cell) or {}).get("status") == "pending"]
        if pending:
            items: list[Any] = []
            for cell in pending:
                for case_id in drift[cell]["sample"]:
                    if any("imported_from" not in t for t in pool_v1(cell, case_id)):
                        continue  # a re-run of this pair already exists (a resumed run)
                    items += slot(by_id[case_id], cell, next_repeat(cell, case_id))
            run_items(items)
            if state["stop"] is None:
                dropped: list[dict[str, Any]] = []
                for cell in pending:
                    entry = dict(drift[cell])
                    flips = []
                    for case_id in entry["sample"]:
                        mine = pool_v1(cell, case_id)
                        imported = [t for t in mine if "imported_from" in t]
                        fresh = [t for t in mine if "imported_from" not in t]
                        if imported and fresh and _known(imported[0]) and _known(fresh[0]):
                            flips.append(imported[0]["success"] != fresh[0]["success"])
                    verdict, lower = drift_verdict(len(entry["sample"]), flips, entry["reference"])
                    entry.update(
                        status=verdict, compared=len(flips), flips=sum(flips), wilson_lower=lower
                    )
                    if verdict != "pass":
                        # the cell's imports are dropped and its imported tasks run fresh
                        drop = [
                            r for r in refs
                            if "imported_from" in load(r) and load(r)["cell_id"] == cell
                        ]  # fmt: skip
                        entry["dropped"] = [r["id"] for r in drop]
                        dropped += drop
                    drift[cell] = entry
                refs[:] = [r for r in refs if r not in dropped]
                with self.store.tx() as db:
                    current = self.store.head(scope, RUN_KIND, pid, db=db)
                    imports = dict(current["data"].get("imports") or {})
                    imports["dropped"] = [*imports.get("dropped", []), *(r["id"] for r in dropped)]
                    self.store.cas(
                        db, scope, RUN_KIND, pid, current["row_version"], "running",
                        {**current["data"], "trial_refs": list(refs), "drift_check": drift,
                         "imports": imports},
                    )  # fmt: skip
                rebuild()
        if state["stop"] is None:
            # round 1: every (case, cell) without a v1 trial once; variants paired with a kept
            # or imported v1 trial in its slot
            items = []
            for case in cases:
                for cell in cells:
                    case_id = case["case_id"]
                    v1_repeats = have.get((cell, case_id, digest(v1[cell])), set())
                    if not v1_repeats:
                        items += slot(case, cell, next_repeat(cell, case_id))
                    else:
                        items += [i for r in sorted(v1_repeats) for i in slot(case, cell, r)]
            run_items(items)
        while state["stop"] is None:
            state["adaptive"] = True
            items = [
                item
                for case in cases
                for cell in cells
                if needs_repeat(cell, case["case_id"])
                for item in slot(case, cell, next_repeat(cell, case["case_id"]))
            ]
            if not items:
                break
            run_items(items)
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
        """Write and return the `calibration-summary` of a finished run (§2.8): once per run
        segment (D-128: a resumed run writes a new revision). ``partial`` is true when the run
        did not end ``done``; readers never select such a summary (``complete_summaries``)."""
        plan = self.store.get(scope, PLAN_KIND, plan_ref)
        pid = plan_ref["id"]
        head = self.store.head(scope, RUN_KIND, pid)
        if head["state"] not in {"done", "stopped"} or head["data"]["plan_ref"] != plan_ref:
            raise Hold("CALIBRATION_STATE", "Only a finished calibration run is summarized")
        data = head["data"]
        segment = int(data.get("segment", 0))
        summary_id = "calsum-" + pid
        existing = [
            ref
            for ref, _ in self.store.list_objects(scope, SUMMARY_KIND)
            if ref["id"] == summary_id
        ]
        revision = 1
        if existing:
            ref = max(existing, key=lambda r: r["revision"])
            stored = self.store.get(scope, SUMMARY_KIND, ref)  # digest-checked read
            if int(stored.get("run_segment", 0)) == segment:
                return ref
            revision = ref["revision"] + 1
        version_ref = versions.current_version_ref(self.store, scope)
        if version_ref is None:
            raise Hold("EVALUATOR_UNQUALIFIED", "No evaluator version matches the running code")
        trials = [self.store.get(scope, TRIAL_KIND, ref) for ref in data["trial_refs"]]
        if any(t["calibration_plan_ref"] != plan_ref for t in trials):
            raise Hold("TRIAL_PROVENANCE", "Foreign trials cannot enter a calibration summary")
        value: dict[str, Any] = {
            "schema": SUMMARY_SCHEMA,
            "scope": scope.wire(),
            "plan_ref": plan_ref,
            **summarize_trials(plan, trials),
            "evaluator_version_ref": version_ref,
            "summarized_at": now(),
            "partial": head["state"] != "done",
            "run_segment": segment,
        }
        if data.get("overruns"):
            value["warnings"] = [{"code": "TRIAL_OVER_CEILING", **o} for o in data["overruns"]]
        if isinstance(data.get("imports"), dict):
            imports = data["imports"]
            value["imports"] = {
                "sources": imports.get("sources", []),
                "imported": len(imports.get("trial_refs", [])),
                "pooled": sum(r in data["trial_refs"] for r in imports.get("trial_refs", [])),
                "dropped": list(imports.get("dropped", [])),
            }
        if data.get("drift_check"):
            value["drift_check"] = data["drift_check"]
        validate_summary(value)
        with self.store.tx() as db:
            return self.store.put(db, scope, SUMMARY_KIND, summary_id, revision, value)
