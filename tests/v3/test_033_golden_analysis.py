"""Work 033 S0, golden G3 (interfaces.md 4.2): the legacy paired analysis is frozen.

`evaluation.analysis` must give the same result as the frozen oracle
(`tests/golden033/analysis_oracle.py`, a copy of the whole file at c9f896a) for every legacy
analysis plan, over 200 seeded random trial sets: 2-40 tasks, 1-3 repeats, missing and unknown
trials, safety failures, cost present or absent, `benefit` plans, drift and contamination flags.
A call that raises must raise the same `RuntimeFault` code in both.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any

import pytest

from amplai_foundry.evaluation import analysis
from amplai_foundry.runtime.errors import RuntimeFault
from golden033 import analysis_oracle

CASES = 200
SEED = 0x033


def _plan(rng: random.Random, tasks: int, repeats: int) -> dict[str, Any]:
    purpose = rng.choice(["exploratory", "confirmatory", "local_qualification", None])
    plan: dict[str, Any] = {
        "method": "paired_binary_conservative",
        "confidence": rng.choice([0.8, 0.9, 0.95, 0.99]),
        "minimum_tasks": rng.randint(2, max(2, tasks)),
        "repeats_per_task": repeats,
        "noninferiority_margin": rng.choice([0, 0.05, 0.1, 0.2]),
        "safety_failure_limit": 0,
        "missing_policy": "inconclusive",
    }
    if purpose is not None:
        plan["purpose"] = purpose
    if purpose == "confirmatory":
        plan.update(
            sample_rationale="fixed corpus",
            variance_basis="pilot",
            sequential_rule="fixed_sample_safety_abort_only",
        )
    if rng.random() < 0.3:
        plan["cost_basis"] = "not_compared"
    elif rng.random() < 0.5:
        plan["cost_basis"] = "compared"
        if rng.random() < 0.6:
            plan["benefit"] = {
                "endpoint": "cost_mean_microunits",
                "minimum_reduction": rng.choice([1, 50, 500]),
                "bootstrap_samples": rng.choice([1000, 2000]),
                "seed": rng.randint(0, 10_000),
            }
    return plan


def _trials(
    rng: random.Random, tasks: list[str], plan: dict[str, Any], *, clean: bool
) -> list[dict[str, Any]]:
    trials: list[dict[str, Any]] = []
    quality = {"baseline": rng.uniform(0.2, 0.98)}
    quality["candidate"] = min(1.0, max(0.0, quality["baseline"] + rng.uniform(-0.3, 0.3)))
    if clean:  # nothing missing or unsafe and a candidate at least as good: reachable "pass"
        quality = {"baseline": 0.95, "candidate": 0.95}
    with_cost = rng.random() < 0.7
    drop, unknown, safety = (
        rng.choice([0, 0, 0.05]),
        rng.choice([0, 0, 0.05]),
        rng.choice([0, 0, 0.03]),
    )
    if clean:
        drop = unknown = safety = 0
        with_cost = True
    for task in tasks:
        for arm in ("baseline", "candidate"):
            for repeat in range(plan["repeats_per_task"]):
                if rng.random() < drop:
                    continue
                trial: dict[str, Any] = {
                    "task_id": task,
                    "arm": arm,
                    "repeat": repeat,
                    "success": None if rng.random() < unknown else rng.random() < quality[arm],
                    "elapsed_ms": rng.randint(1_000, 900_000),
                }
                if with_cost or rng.random() < 0.1:
                    trial["cost_microunits"] = rng.randint(100, 5_000)
                    if clean or rng.random() < 0.9:
                        trial["usage_status"] = "measured"
                    else:
                        trial["usage_status"] = rng.choice(["estimated", "unknown"])
                if rng.random() < safety:
                    trial["safety_failures"] = 1
                if not clean and rng.random() < 0.02:
                    trial["unknown_effects"] = 1
                trials.append(trial)
    return trials


def _case(index: int) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], dict[str, bool]]:
    rng = random.Random(SEED * 1000 + index)
    task_count = rng.randint(2, 40)
    repeats = rng.randint(1, 3)
    tasks = [f"task-{i:02d}" for i in range(task_count)]
    clean = index % 3 == 0
    if clean:
        task_count = rng.randint(30, 40)
        tasks = [f"task-{i:02d}" for i in range(task_count)]
    plan = _plan(rng, task_count, repeats)
    if clean:
        plan["noninferiority_margin"] = 0.2
        plan["minimum_tasks"] = min(plan["minimum_tasks"], task_count)
    trials = _trials(rng, tasks, plan, clean=clean)
    if index % 25 == 7 and not clean:  # a trial outside the frozen sample: both must refuse it
        trials.append({"task_id": "task-unknown", "arm": "baseline", "repeat": 0, "success": True})
    if index % 40 == 11 and trials and not clean:  # a duplicate trial
        trials.append(dict(trials[0]))
    flags = {
        "environment_drifted": rng.random() < 0.1,
        "contamination": rng.random() < 0.1,
    }
    return trials, plan, tasks, flags


def _run(module: Any, index: int) -> tuple[str, Any]:
    trials, plan, tasks, flags = _case(index)
    try:
        return "ok", module.analyze_pairs(trials, plan, expected_tasks=tasks, **flags)
    except RuntimeFault as fault:
        return "fault", fault.code


@pytest.mark.parametrize("index", range(CASES))
def test_analyze_pairs_equals_the_oracle(index: int) -> None:
    assert _run(analysis, index) == _run(analysis_oracle, index)


def test_the_seeded_cases_cover_every_verdict_and_the_benefit_and_fault_paths() -> None:
    """The generator is not degenerate: it reaches the branches the oracle protects."""
    verdicts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    faults: Counter[str] = Counter()
    benefit_verdicts: Counter[str] = Counter()
    for index in range(CASES):
        kind, value = _run(analysis_oracle, index)
        if kind == "fault":
            faults[value] += 1
            continue
        verdicts[value["verdict"]] += 1
        reasons.update(value["reasons"])
        if value.get("benefit"):
            benefit_verdicts[value["benefit"]["verdict"]] += 1
    assert {"pass", "fail", "inconclusive"} <= set(verdicts), verdicts
    assert {"missing_or_unknown_trials", "unknown_cost", "safety_failure"} <= set(reasons), reasons
    assert {"UNPLANNED_TRIAL"} <= set(faults), faults
    assert benefit_verdicts, "no case reached the benefit analysis"


@pytest.mark.parametrize("index", range(40))
def test_analyze_benefit_equals_the_oracle(index: int) -> None:
    rng = random.Random(SEED * 7 + index)
    count = rng.randint(2, 40)
    deltas = [rng.uniform(-800, 400) for _ in range(count)]
    plan = {
        "minimum_tasks": rng.randint(2, count),
        "confidence": rng.choice([0.9, 0.95]),
        "benefit": {
            "endpoint": "cost_mean_microunits",
            "minimum_reduction": rng.choice([1, 100]),
            "bootstrap_samples": rng.choice([1000, 3000]),
            "seed": rng.randint(0, 999),
        },
    }
    expected = count if index % 5 else count + 1  # a missing pair is a distinct result
    got = analysis.analyze_benefit(deltas, plan, expected)
    assert got == analysis_oracle.analyze_benefit(deltas, plan, expected)
    if index % 5 == 0:
        assert got["reason"] == "missing_cost_pairs"


@pytest.mark.parametrize("field", ["method", "confidence", "minimum_tasks", "safety_failure_limit"])
def test_validate_analysis_plan_refuses_as_the_oracle_refuses(field: str) -> None:
    bad = {
        "method": "other",
        "confidence": 1.5,
        "minimum_tasks": 1,
        "safety_failure_limit": 1,
    }[field]
    plan = {
        "method": "paired_binary_conservative",
        "confidence": 0.95,
        "minimum_tasks": 5,
        "repeats_per_task": 1,
        "noninferiority_margin": 0.1,
        "safety_failure_limit": 0,
        "missing_policy": "inconclusive",
        field: bad,
    }
    with pytest.raises(RuntimeFault) as new:
        analysis.validate_analysis_plan(plan)
    with pytest.raises(RuntimeFault) as old:
        analysis_oracle.validate_analysis_plan(plan)
    assert new.value.code == old.value.code
