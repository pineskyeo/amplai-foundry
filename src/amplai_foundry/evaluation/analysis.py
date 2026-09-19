"""Predeclared paired analysis with tasks, not repeated trajectories, as units.

Binary paired difference uses conservative Wilson bounds for the two discordance
probabilities with Bonferroni-adjusted nominal coverage. This avoids a zero-width bootstrap interval
when both candidates pass every observed task. It is deliberately conservative.
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist, mean
from typing import Any

from amplai_foundry.runtime.errors import RuntimeFault

REQUIRED = {
    "method",
    "confidence",
    "minimum_tasks",
    "repeats_per_task",
    "noninferiority_margin",
    "safety_failure_limit",
    "missing_policy",
}


def validate_analysis_plan(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict) or not set(plan) >= REQUIRED:
        raise RuntimeFault(
            "ANALYSIS_PLAN", "Predeclare analysis, sampling and missing-data policies"
        )
    if (
        plan["method"] != "paired_binary_conservative"
        or type(plan["confidence"]) not in (float, int)
        or not math.isfinite(plan["confidence"])
        or not 0.5 < plan["confidence"] < 1
    ):
        raise RuntimeFault("ANALYSIS_METHOD", "Unsupported or invalid predeclared analysis method")
    if (
        type(plan["minimum_tasks"]) is not int
        or not 2 <= plan["minimum_tasks"] <= 100000
        or type(plan["repeats_per_task"]) is not int
        or not 1 <= plan["repeats_per_task"] <= 100
    ):
        raise RuntimeFault(
            "SAMPLE_PLAN", "Use positive integral counts of DISTINCT tasks and repeats"
        )
    margin = plan["noninferiority_margin"]
    if (
        type(margin) not in (int, float)
        or not math.isfinite(margin)
        or not 0 <= margin <= 1
        or type(plan["safety_failure_limit"]) is not int
        or plan["safety_failure_limit"] != 0
        or plan["missing_policy"] != "inconclusive"
    ):
        raise RuntimeFault(
            "ANALYSIS_SAFETY", "Safety cannot be traded for score; missing data stays inconclusive"
        )
    if plan.get("purpose", "exploratory") not in {
        "exploratory",
        "confirmatory",
        "local_qualification",
    }:
        raise RuntimeFault(
            "ANALYSIS_PURPOSE", "Declare exploratory, confirmatory, or local_qualification"
        )
    if plan.get("purpose") == "confirmatory":
        if not all(
            isinstance(plan.get(k), str) and plan[k].strip()
            for k in ("sample_rationale", "variance_basis", "sequential_rule")
        ):
            raise RuntimeFault(
                "SAMPLE_RATIONALE",
                "Confirmatory plans need sample rationale, variance basis and stopping rule",
            )
        if plan["sequential_rule"] != "fixed_sample_safety_abort_only":
            raise RuntimeFault(
                "SEQUENTIAL_RULE", "This implementation qualifies fixed-sample analysis only"
            )
    benefit = plan.get("benefit")
    if benefit is not None and (
        not isinstance(benefit, dict)
        or set(benefit) != {"endpoint", "minimum_reduction", "bootstrap_samples", "seed"}
        or benefit["endpoint"] != "cost_mean_microunits"
        or type(benefit["minimum_reduction"]) not in (float, int)
        or not math.isfinite(benefit["minimum_reduction"])
        or benefit["minimum_reduction"] <= 0
        or type(benefit["bootstrap_samples"]) is not int
        or not 1000 <= benefit["bootstrap_samples"] <= 20000
        or type(benefit["seed"]) is not int
    ):
        raise RuntimeFault("BENEFIT_PLAN", "Predeclare the supported paired-task benefit analysis")


def wilson(successes: int, n: int, z: float) -> tuple[float, float]:
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0, center - radius), min(1, center + radius)


def analyze_pairs(
    trials: list[dict[str, Any]],
    plan: dict[str, Any],
    *,
    expected_tasks: list[str],
    environment_drifted: bool = False,
    contamination: bool = False,
) -> dict[str, Any]:
    validate_analysis_plan(plan)
    groups: dict[tuple[str, str, int], dict[str, Any]] = {}
    reasons = []
    if (
        not isinstance(expected_tasks, list)
        or any(not isinstance(x, str) or not x for x in expected_tasks)
        or len(set(expected_tasks)) != len(expected_tasks)
    ):
        raise RuntimeFault("SAMPLE_IDS", "Expected tasks must be unique nonempty identifiers")
    for t in trials:
        if (
            t.get("task_id") not in expected_tasks
            or type(t.get("repeat")) is not int
            or not 0 <= t["repeat"] < plan["repeats_per_task"]
        ):
            raise RuntimeFault(
                "UNPLANNED_TRIAL", "Trial is outside the frozen sampling/repeat plan"
            )
        if t.get("success") is not None and type(t["success"]) is not bool:
            raise RuntimeFault("TRIAL_TYPE", "A trial result is boolean or explicitly unknown")
        if any(
            type(t.get(k, 0)) is not int or t.get(k, 0) < 0
            for k in ("safety_failures", "unknown_effects")
        ):
            raise RuntimeFault("TRIAL_COUNTER", "Safety/unknown counters are nonnegative integers")
        for k in ("cost_microunits", "elapsed_ms"):
            v = t.get(k)
            if v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0):
                raise RuntimeFault("TRIAL_USAGE", "Usage/duration must be finite and nonnegative")
    for t in trials:
        key = (t["task_id"], t["arm"], t["repeat"])
        if key in groups:
            raise RuntimeFault("DUPLICATE_TRIAL", "A task/arm/repeat was counted twice")
        if t["arm"] not in {"baseline", "candidate"}:
            raise RuntimeFault("TRIAL_ARM", "Unknown paired arm")
        groups[key] = t
    missing = []
    paired = []
    safety = sum(t.get("safety_failures", 0) for t in trials)
    for task in expected_tasks:
        arm_results = {}
        for arm in ("baseline", "candidate"):
            values = [groups.get((task, arm, i)) for i in range(plan["repeats_per_task"])]
            if any(
                v is None or v.get("success") is None or v.get("unknown_effects", 0) > 0
                for v in values
            ):
                missing.append({"task_id": task, "arm": arm})
            else:
                arm_results[arm] = all(v is not None and v["success"] is True for v in values)
        if len(arm_results) == 2:
            paired.append((arm_results["baseline"], arm_results["candidate"]))
    n = len(paired)
    if n < plan["minimum_tasks"]:
        reasons.append("insufficient_distinct_tasks")
    if missing:
        reasons.append("missing_or_unknown_trials")
    if any(t.get("cost_microunits") is None for t in trials):
        reasons.append("unknown_cost")
    if any(t.get("usage_status", "measured") != "measured" for t in trials):
        reasons.append("nonmeasured_usage")
    if environment_drifted:
        reasons.append("environment_drift")
    if contamination:
        reasons.append("corpus_contamination")
    if not paired:
        return {
            "verdict": "fail" if safety > 0 else "inconclusive",
            "reasons": reasons or ["no_paired_tasks"],
            "task_count": 0,
            "safety_failures": safety,
            "missing": missing,
        }
    positive = sum(not b and c for b, c in paired)
    negative = sum(b and not c for b, c in paired)
    # Wilson is an approximate interval, not an exact finite-sample guarantee.
    # Bonferroni adjustment allocates alpha/2 to each two-sided interval.
    z = NormalDist().inv_cdf(1 - (1 - plan["confidence"]) / 4)
    pl, pu = wilson(positive, n, z)
    nl, nu = wilson(negative, n, z)
    low, high = pl - nu, pu - nl
    delta = (positive - negative) / n
    if safety > 0:
        verdict = "fail"
        reasons.append("safety_failure")
    elif reasons:
        verdict = "inconclusive"
    elif low >= -plan["noninferiority_margin"]:
        verdict = "pass"
    elif high < -plan["noninferiority_margin"]:
        verdict = "fail"
        reasons.append("inferiority")
    else:
        verdict = "inconclusive"
        reasons.append("interval_crosses_noninferiority_boundary")
    cost_pairs = []
    latencies = []
    for task in expected_tasks:
        bs = [groups.get((task, "baseline", i)) for i in range(plan["repeats_per_task"])]
        cs = [groups.get((task, "candidate", i)) for i in range(plan["repeats_per_task"])]
        present = [v for v in bs + cs if v is not None]
        if len(present) == len(bs + cs) and all(
            v.get("cost_microunits") is not None for v in present
        ):
            cs_present = [v for v in cs if v is not None]
            bs_present = [v for v in bs if v is not None]
            cost_pairs.append(
                mean(v["cost_microunits"] for v in cs_present)
                - mean(v["cost_microunits"] for v in bs_present)
            )
        if len(present) == len(bs + cs) and all(v.get("elapsed_ms") is not None for v in present):
            cs_present = [v for v in cs if v is not None]
            bs_present = [v for v in bs if v is not None]
            latencies.append(
                mean(v["elapsed_ms"] for v in cs_present)
                - mean(v["elapsed_ms"] for v in bs_present)
            )
    benefit_result = None
    if plan.get("benefit"):
        benefit_result = analyze_benefit(cost_pairs, plan, len(expected_tasks))
        if verdict == "pass" and benefit_result["verdict"] != "pass":
            verdict = benefit_result["verdict"]
            reasons.append("benefit_not_established")
    return {
        "verdict": verdict,
        "purpose": plan.get("purpose", "exploratory"),
        "benefit": benefit_result,
        "method": plan["method"],
        "unit": "distinct_task_all_repeats_pass",
        "task_count": n,
        "candidate_minus_baseline": delta,
        "confidence": plan["confidence"],
        "confidence_interval": [low, high],
        "noninferiority_margin": plan["noninferiority_margin"],
        "safety_failures": safety,
        "missing": missing,
        "reasons": reasons,
        "cost_delta_mean_microunits": mean(cost_pairs)
        if len(cost_pairs) == len(expected_tasks)
        else None,
        "elapsed_delta_mean_ms": mean(latencies) if len(latencies) == len(expected_tasks) else None,
        "interval_limitations": (
            "Wilson approximation with conservative simultaneous adjustment; "
            "not an exact finite-sample guarantee"
        ),
        "secondary_metrics_use": "descriptive_only_not_a_safety_tradeoff",
    }


def analyze_benefit(
    deltas: list[float], plan: dict[str, Any], expected_count: int
) -> dict[str, Any]:
    """Paired TASK bootstrap, never resampling repeated trajectories as independent.

    Percentile bootstrap is approximate. Report its bounds and limitations; it
    does not certify small-sample coverage or justify optional stopping.
    """
    b = plan["benefit"]
    if len(deltas) != expected_count or len(deltas) < plan["minimum_tasks"]:
        return {
            "verdict": "inconclusive",
            "reason": "missing_cost_pairs",
            "task_count": len(deltas),
        }
    if len(deltas) * b["bootstrap_samples"] > 2_000_000:
        return {
            "verdict": "inconclusive",
            "reason": "analysis_resampling_budget",
            "task_count": len(deltas),
        }
    rng = random.Random(b["seed"])
    samples = sorted(
        mean(rng.choices(deltas, k=len(deltas))) for _ in range(b["bootstrap_samples"])
    )
    alpha = (1 - plan["confidence"]) / 2
    low, high = (
        samples[int(alpha * len(samples))],
        samples[min(len(samples) - 1, int((1 - alpha) * len(samples)))],
    )
    boundary = -b["minimum_reduction"]
    verdict = "pass" if high <= boundary else "fail" if low > boundary else "inconclusive"
    return {
        "verdict": verdict,
        "endpoint": b["endpoint"],
        "candidate_minus_baseline": mean(deltas),
        "confidence_interval": [low, high],
        "task_count": len(deltas),
        "method": "paired_task_percentile_bootstrap",
        "seed": b["seed"],
        "resamples": b["bootstrap_samples"],
        "limitation": (
            "Approximate bootstrap interval; fixed sample, no optional stopping "
            "or task pseudoreplication"
        ),
    }
