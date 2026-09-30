"""Predeclared paired analysis with tasks, not repeated trajectories, as units.

Binary paired difference uses conservative Wilson bounds for the two discordance
probabilities with Bonferroni-adjusted nominal coverage. This avoids a zero-width bootstrap interval
when both candidates pass every observed task. It is deliberately conservative.

Work 033 (D-099, interfaces.md §7): a plan that carries `evaluator_version_ref` also gets a
decision class, candidate-only safety attribution (IC-18 a), an optional superiority endpoint,
noise band, pass^k, a budget-matched reference arm and an accumulating e-process. A plan
without it (every stored plan) is analysed exactly as at c9f896a (golden G3).
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Callable
from statistics import NormalDist, mean
from typing import Any

from amplai_foundry.runtime.errors import Hold, RuntimeFault

from .sequential import e_process_update, e_value, paired_z, wilson

REQUIRED = {
    "method",
    "confidence",
    "minimum_tasks",
    "repeats_per_task",
    "noninferiority_margin",
    "safety_failure_limit",
    "missing_policy",
}

# §7.1: optional fields that change the analysis. Only a plan that carries
# `evaluator_version_ref` may declare them; its presence switches the new checks on.
VERSIONED_ONLY = ("endpoint", "minimum_effect", "noise_band", "mde", "reference_arm", "e_process")
ENDPOINTS = ("noninferiority", "superiority")
BENEFIT_ENDPOINTS = ("cost_mean_microunits", "tokens_mean")
# §7.2: report verdict per endpoint; "regression" fails, every other class is inconclusive.
PASSING = {
    "noninferiority": frozenset({"improvement", "efficiency", "non_inferior"}),
    "superiority": frozenset({"improvement"}),
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MDE_LINE = re.compile(r"^MDE: (\S+) at n=(\d+)$", re.MULTILINE)


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
        # §7.1: e-process accumulation is allowed for confirmatory plans of the new evaluator.
        if plan["sequential_rule"] != "fixed_sample_safety_abort_only" and not (
            "evaluator_version_ref" in plan and plan["sequential_rule"] == "e_process_accumulating"
        ):
            raise RuntimeFault(
                "SEQUENTIAL_RULE", "This implementation qualifies fixed-sample analysis only"
            )
    # D-088: subscription drivers report no settled cost; such a plan declares that cost is
    # not compared, so unknown cost and non-measured usage are not reasons for inconclusive.
    # It cannot then claim a cost benefit.
    if plan.get("cost_basis", "compared") not in {"compared", "not_compared"}:
        raise RuntimeFault("COST_BASIS", "Declare cost as compared or not_compared")
    benefit = plan.get("benefit")
    # IC-08: a paired token benefit needs no cost comparison; a cost benefit still does.
    if (
        plan.get("cost_basis") == "not_compared"
        and benefit is not None
        and _benefit_endpoint(plan) != "tokens_mean"
    ):
        raise RuntimeFault("COST_BASIS", "A cost benefit needs compared cost")
    if benefit is not None and (
        not isinstance(benefit, dict)
        or set(benefit) != {"endpoint", "minimum_reduction", "bootstrap_samples", "seed"}
        or benefit["endpoint"] not in BENEFIT_ENDPOINTS
        or type(benefit["minimum_reduction"]) not in (float, int)
        or not math.isfinite(benefit["minimum_reduction"])
        or benefit["minimum_reduction"] <= 0
        or type(benefit["bootstrap_samples"]) is not int
        or not 1000 <= benefit["bootstrap_samples"] <= 20000
        or type(benefit["seed"]) is not int
    ):
        raise RuntimeFault("BENEFIT_PLAN", "Predeclare the supported paired-task benefit analysis")
    _validate_evaluator_fields(plan)


def _benefit_endpoint(plan: dict[str, Any]) -> str | None:
    benefit = plan.get("benefit")
    return benefit.get("endpoint") if isinstance(benefit, dict) else None


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _is_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "revision", "digest"}
        and isinstance(value["id"], str)
        and _ID.fullmatch(value["id"]) is not None
        and type(value["revision"]) is int
        and 1 <= value["revision"] <= 9007199254740991
        and isinstance(value["digest"], str)
        and _DIGEST.fullmatch(value["digest"]) is not None
    )


def _validate_evaluator_fields(plan: dict[str, Any]) -> None:
    """§7.1 fields. A legacy plan has none of them and is checked exactly as at c9f896a (G3)."""
    tokens = plan.get("max_trial_tokens")
    if "max_trial_tokens" in plan and (type(tokens) is not int or tokens < 1):
        raise RuntimeFault("ANALYSIS_PLAN", "max_trial_tokens is a positive integer")
    if "evaluator_version_ref" not in plan:
        if any(plan.get(k) is not None for k in VERSIONED_ONLY):
            raise RuntimeFault(
                "ANALYSIS_PLAN", "Evaluator fields need a pinned evaluator_version_ref"
            )
        return
    if not _is_ref(plan["evaluator_version_ref"]):
        raise RuntimeFault("ANALYSIS_PLAN", "evaluator_version_ref must be an exact ref")
    endpoint = plan.get("endpoint", "noninferiority")
    effect = plan.get("minimum_effect")
    band = plan.get("noise_band")
    if (
        endpoint not in ENDPOINTS
        or (effect is not None and (not _number(effect) or not 0 < effect <= 1))
        or (endpoint == "superiority" and effect is None)
        or (band is not None and (not _number(band) or not 0 <= band <= 1))
    ):
        raise RuntimeFault(
            "ANALYSIS_METHOD", "Declare the endpoint, its minimum effect and the noise band"
        )
    _validate_mde(plan)
    _validate_reference_arm(plan.get("reference_arm"))
    _validate_e_process(plan)


def _validate_mde(plan: dict[str, Any]) -> None:
    value = plan.get("mde")
    if value is not None and (not _number(value) or value <= 0):
        raise RuntimeFault("SAMPLE_RATIONALE", "mde is a positive finite number")
    if plan.get("purpose") != "confirmatory":
        return  # exploratory stages carry mde as a descriptive value only (§7.1)
    lines = _MDE_LINE.findall(plan["sample_rationale"])
    if value is None or not any(_float(v) == float(value) for v, _ in lines):
        raise RuntimeFault(
            "SAMPLE_RATIONALE", "Confirmatory plans state 'MDE: <value> at n=<tasks>'"
        )


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _validate_reference_arm(arm: Any) -> None:
    """§7.4 shape; pinnability, cell and component checks belong to the injected validator."""
    if arm is None:
        return
    if (
        not isinstance(arm, dict)
        or set(arm) != {"composition_ref", "kind", "n", "cost_match"}
        or not _is_ref(arm["composition_ref"])
        or arm["kind"] != "best_of_n"
        or type(arm["n"]) is not int
        or not 1 <= arm["n"] <= 3
        or arm["cost_match"] != "tokens"
    ):
        raise Hold("REFERENCE_ARM", "Reference arm is a budget-matched best-of-n with n <= 3")


def _validate_e_process(plan: dict[str, Any]) -> None:
    accumulating = (
        plan.get("purpose") == "confirmatory"
        and plan.get("sequential_rule") == "e_process_accumulating"
    )
    spec = plan.get("e_process")
    if not accumulating:
        if spec is not None:
            raise RuntimeFault("SEQUENTIAL_RULE", "e_process needs e_process_accumulating")
        return
    if (
        not isinstance(spec, dict)
        or set(spec) != {"alpha", "key", "prior_state", "prior_report_refs"}
        or not _number(spec["alpha"])
        or not 0 < spec["alpha"] < 1
        or not isinstance(spec["key"], str)
        or not spec["key"].strip()
        or not isinstance(spec["prior_report_refs"], list)
        or not all(_is_ref(r) for r in spec["prior_report_refs"])
    ):
        raise RuntimeFault("SEQUENTIAL_RULE", "Declare the e-process alpha, key and prior")
    prior = spec["prior_state"]
    if prior is None:
        consistent = not spec["prior_report_refs"]
    else:
        consistent = bool(spec["prior_report_refs"]) and isinstance(prior, list)
        try:
            e_process_update(tuple(prior) if isinstance(prior, list) else None, 0, 0)
        except (TypeError, ValueError):
            consistent = False
    if not consistent:
        raise RuntimeFault(
            "SEQUENTIAL_RULE", "A prior e-process state comes with the reports it came from"
        )


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
    token_benefit = _benefit_endpoint(plan) == "tokens_mean"
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
        if token_benefit and any(
            t.get(k) is not None and (type(t[k]) is not int or t[k] < 0)
            for k in ("input_tokens", "output_tokens")
        ):
            raise RuntimeFault("TRIAL_USAGE", "Token counts are nonnegative integers or unknown")
    arms = _arms(plan)
    for t in trials:
        key = (t["task_id"], t["arm"], t["repeat"])
        if key in groups:
            raise RuntimeFault("DUPLICATE_TRIAL", "A task/arm/repeat was counted twice")
        if t["arm"] not in arms:
            raise RuntimeFault("TRIAL_ARM", "Unknown paired arm")
        groups[key] = t
    if "evaluator_version_ref" in plan:
        return _analyze_versioned(
            trials,
            groups,
            plan,
            expected_tasks,
            environment_drifted=environment_drifted,
            contamination=contamination,
        )
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
    compared = plan.get("cost_basis", "compared") == "compared"
    if compared and any(t.get("cost_microunits") is None for t in trials):
        reasons.append("unknown_cost")
    if compared and any(t.get("usage_status", "measured") != "measured" for t in trials):
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
        deltas = (
            _paired_deltas(groups, plan, expected_tasks, _tokens, "baseline", "candidate")
            if token_benefit
            else cost_pairs
        )
        benefit_result = analyze_benefit(deltas, plan, len(expected_tasks))
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


def _arms(plan: dict[str, Any]) -> tuple[str, ...]:
    """§7.4: `arm: "reference"` is accepted only when the plan declares a reference arm."""
    if "evaluator_version_ref" in plan and plan.get("reference_arm") is not None:
        return ("baseline", "candidate", "reference")
    return ("baseline", "candidate")


def _tokens(trial: dict[str, Any]) -> int | None:
    """Trial tokens for the IC-08 token benefit; unknown when either count is unknown."""
    inp, out = trial.get("input_tokens"), trial.get("output_tokens")
    return None if inp is None or out is None else inp + out


def _cost(trial: dict[str, Any]) -> Any:
    return trial.get("cost_microunits")


def _elapsed(trial: dict[str, Any]) -> Any:
    return trial.get("elapsed_ms")


def _paired_deltas(
    groups: dict[tuple[str, str, int], dict[str, Any]],
    plan: dict[str, Any],
    expected_tasks: list[str],
    value: Callable[[dict[str, Any]], Any],
    base: str,
    other: str,
) -> list[float]:
    """Per task `mean(other) - mean(base)` when every repeat of both arms has a value: the
    pairing rule of the legacy cost and latency deltas."""
    deltas: list[float] = []
    for task in expected_tasks:
        bs = [groups.get((task, base, i)) for i in range(plan["repeats_per_task"])]
        xs = [groups.get((task, other, i)) for i in range(plan["repeats_per_task"])]
        present = [v for v in bs + xs if v is not None]
        if len(present) == len(bs + xs) and all(value(v) is not None for v in present):
            deltas.append(
                mean(value(v) for v in xs if v is not None)
                - mean(value(v) for v in bs if v is not None)
            )
    return deltas


def _mean_delta(
    groups: dict[tuple[str, str, int], dict[str, Any]],
    plan: dict[str, Any],
    expected_tasks: list[str],
    value: Callable[[dict[str, Any]], Any],
) -> float | None:
    """Descriptive mean paired delta, only when every task has one (legacy rule)."""
    deltas = _paired_deltas(groups, plan, expected_tasks, value, "baseline", "candidate")
    return mean(deltas) if expected_tasks and len(deltas) == len(expected_tasks) else None


def _pairs(
    groups: dict[tuple[str, str, int], dict[str, Any]],
    plan: dict[str, Any],
    expected_tasks: list[str],
    base: str,
    other: str,
) -> tuple[list[tuple[bool, bool]], list[dict[str, str]]]:
    """Paired task outcomes (unit: every repeat passes) and missing arms, as the legacy loop."""
    missing: list[dict[str, str]] = []
    paired: list[tuple[bool, bool]] = []
    for task in expected_tasks:
        arm_results = {}
        for arm in (base, other):
            values = [groups.get((task, arm, i)) for i in range(plan["repeats_per_task"])]
            if any(
                v is None or v.get("success") is None or v.get("unknown_effects", 0) > 0
                for v in values
            ):
                missing.append({"task_id": task, "arm": arm})
            else:
                arm_results[arm] = all(v is not None and v["success"] is True for v in values)
        if len(arm_results) == 2:
            paired.append((arm_results[base], arm_results[other]))
    return paired, missing


def _interval(
    paired: list[tuple[bool, bool]], confidence: float
) -> tuple[float, float, float, int, int]:
    """The existing conservative paired interval (`low`, `high`, `delta`) plus wins and
    losses; §7.2 computes them exactly as today."""
    n = len(paired)
    wins = sum(not b and c for b, c in paired)
    losses = sum(b and not c for b, c in paired)
    z = paired_z(confidence)
    pl, pu = wilson(wins, n, z)
    nl, nu = wilson(losses, n, z)
    return pl - nu, pu - nl, (wins - losses) / n, wins, losses


def _data_reasons(
    trials: list[dict[str, Any]],
    plan: dict[str, Any],
    task_count: int,
    missing: list[dict[str, str]],
    *,
    environment_drifted: bool,
    contamination: bool,
) -> list[str]:
    """§7.2 step 2 reasons, with the legacy names and order."""
    reasons = []
    if task_count < plan["minimum_tasks"]:
        reasons.append("insufficient_distinct_tasks")
    if missing:
        reasons.append("missing_or_unknown_trials")
    compared = plan.get("cost_basis", "compared") == "compared"
    if compared and any(t.get("cost_microunits") is None for t in trials):
        reasons.append("unknown_cost")
    if compared and any(t.get("usage_status", "measured") != "measured" for t in trials):
        reasons.append("nonmeasured_usage")
    if environment_drifted:
        reasons.append("environment_drift")
    if contamination:
        reasons.append("corpus_contamination")
    return reasons


def decision_class(
    *,
    low: float,
    high: float,
    delta: float,
    benefit: dict[str, Any] | None,
    endpoint: str,
    margin: float,
    minimum_effect: float | None,
    noise_band: float | None,
    candidate_safety_failures: int,
    baseline_safety_failures: int,
    missing: bool,
) -> str:
    """§7.2 decision class (IC-18 a). `missing` is true when any step-2 reason holds (missing
    or unknown trials, unknown cost when compared, drift, contamination, insufficient tasks)."""
    return _classify(
        low=low,
        high=high,
        delta=delta,
        benefit=benefit,
        endpoint=endpoint,
        margin=margin,
        minimum_effect=minimum_effect,
        noise_band=noise_band,
        candidate_safety_failures=candidate_safety_failures,
        baseline_safety_failures=baseline_safety_failures,
        missing=missing,
        e_crossed=False,
    )[0]


def _classify(
    *,
    low: float,
    high: float,
    delta: float,
    benefit: dict[str, Any] | None,
    endpoint: str,
    margin: float,
    minimum_effect: float | None,
    noise_band: float | None,
    candidate_safety_failures: int,
    baseline_safety_failures: int,
    missing: bool,
    e_crossed: bool,
) -> tuple[str, str, str, str | None]:
    """(class, success axis S, cost axis C, basis of `better`) of §7.2 steps 1-5."""
    if endpoint not in ENDPOINTS:
        raise RuntimeFault("ANALYSIS_METHOD", "Unknown endpoint")
    # Step 3. §7.6: a crossed e-process may stand in for `low > minimum_effect`, and only while
    # the fixed-sample interval is non-inferior.
    basis: str | None = None
    if minimum_effect is not None and low > minimum_effect:
        success, basis = "better", "interval"
    elif e_crossed and low >= -margin:
        success, basis = "better", "e_process"
    elif high < -margin:
        success = "worse"
    elif low >= -margin:
        success = "noninf"
    else:
        success = "unclear"
    if noise_band is not None and abs(delta) < noise_band and success in {"better", "worse"}:
        # Inside the A/A noise band S is neither better nor worse ("unresolved"). The step-5
        # table has no row for it; it maps like "unclear" (conservative, see the S1 report).
        success, basis = "unresolved", None
    # Step 4.
    outcome = benefit.get("verdict") if benefit else None
    cost = "cheaper" if outcome == "pass" else "costlier" if outcome == "fail" else "unknown"
    # Steps 1 and 2: only candidate-arm safety failures are a regression (IC-18 a).
    if candidate_safety_failures > 0:
        return "regression", success, cost, basis
    if baseline_safety_failures > 0 or missing:
        return "inconclusive", success, cost, basis
    # Step 5.
    if success == "better":
        return ("tradeoff" if cost == "costlier" else "improvement"), success, cost, basis
    if success == "noninf":
        return ("efficiency" if cost == "cheaper" else "non_inferior"), success, cost, basis
    if success == "worse":
        return ("tradeoff" if cost == "cheaper" else "regression"), success, cost, basis
    return "inconclusive", success, cost, basis


def report_verdict(decision: str, endpoint: str) -> str:
    """§7.2: noninferiority passes on improvement/efficiency/non_inferior, superiority on
    improvement only; regression fails; every other class is inconclusive."""
    if decision in PASSING[endpoint]:
        return "pass"
    return "fail" if decision == "regression" else "inconclusive"


def per_task_rates(
    trials: list[dict[str, Any]], expected_tasks: list[str], repeats: int, arm: str
) -> dict[str, dict[str, Any]]:
    """§7.3, descriptive. `runs` counts the arm's trials of the task with a known result and no
    unknown effect (the analysis treats the others as missing); `rate` is None without runs."""
    rows: dict[str, dict[str, Any]] = {}
    for task in expected_tasks:
        known = [
            t
            for t in trials
            if t["task_id"] == task
            and t["arm"] == arm
            and 0 <= t["repeat"] < repeats
            and t.get("success") is not None
            and t.get("unknown_effects", 0) == 0
        ]
        passes = sum(t["success"] is True for t in known)
        rows[task] = {
            "passes": passes,
            "runs": len(known),
            "rate": passes / len(known) if known else None,
        }
    return rows


def _pass_k(
    trials: list[dict[str, Any]], expected_tasks: list[str], repeats: int, arm: str
) -> dict[str, Any]:
    """§7.3: share of tasks whose every repeat passes, over the tasks with all `repeats` runs
    known (a task with a missing run counts neither way)."""
    rows = per_task_rates(trials, expected_tasks, repeats, arm)
    complete = [r for r in rows.values() if r["runs"] == repeats]
    rate = sum(r["passes"] == repeats for r in complete) / len(complete) if complete else None
    return {"k": repeats, "rate": rate}


def _e_process_block(plan: dict[str, Any], wins: int, losses: int) -> dict[str, Any] | None:
    """§7.6: the running state after this experiment's discordant pairs, recorded so the next
    experiment with the same key can pin it as `prior_state`."""
    if not (
        plan.get("purpose") == "confirmatory"
        and plan.get("sequential_rule") == "e_process_accumulating"
    ):
        return None
    spec = plan["e_process"]
    prior = spec["prior_state"]
    state = e_process_update(tuple(prior) if prior is not None else None, wins, losses)
    value = e_value(state)
    return {
        "key": spec["key"],
        "alpha": spec["alpha"],
        "prior_state": prior,
        "prior_report_refs": spec["prior_report_refs"],
        "wins": wins,
        "losses": losses,
        "state": list(state),
        "e_value": value if math.isfinite(value) else None,
        "crossed": value >= 1 / spec["alpha"],
        "limitation": (
            "H0 per task: P(win | discordant, earlier pairs) <= 1/2 on every task, runs "
            "independent given the task; a crossing shows the candidate is better on some "
            "task. The e-process tests direction, not the size of the effect."
        ),
    }


def _comparison(
    plan: dict[str, Any],
    *,
    paired: list[tuple[bool, bool]],
    benefit: dict[str, Any] | None,
    candidate_safety: int,
    other_safety: int,
    missing: bool,
    e_crossed: bool,
) -> tuple[str, dict[str, Any]]:
    """Class and interval fields of one comparison (primary or `vs_reference`)."""
    if not paired:
        return ("regression" if candidate_safety > 0 else "inconclusive"), {"task_count": 0}
    low, high, delta, _, _ = _interval(paired, plan["confidence"])
    decision, success, cost, basis = _classify(
        low=low,
        high=high,
        delta=delta,
        benefit=benefit,
        endpoint=plan.get("endpoint", "noninferiority"),
        margin=plan["noninferiority_margin"],
        minimum_effect=plan.get("minimum_effect"),
        noise_band=plan.get("noise_band"),
        candidate_safety_failures=candidate_safety,
        baseline_safety_failures=other_safety,
        missing=missing,
        e_crossed=e_crossed,
    )
    fields: dict[str, Any] = {
        "task_count": len(paired),
        "difference": delta,
        "confidence_interval": [low, high],
        "success_axis": success,
        "cost_axis": cost,
    }
    if basis == "e_process":
        fields["basis"] = "e_process"
    return decision, fields


def _benefit_versus(
    groups: dict[tuple[str, str, int], dict[str, Any]],
    plan: dict[str, Any],
    expected_tasks: list[str],
    base: str,
) -> dict[str, Any] | None:
    if not plan.get("benefit"):
        return None
    value = _tokens if _benefit_endpoint(plan) == "tokens_mean" else _cost
    deltas = _paired_deltas(groups, plan, expected_tasks, value, base, "candidate")
    return analyze_benefit(deltas, plan, len(expected_tasks))


def _versus_reference(
    trials: list[dict[str, Any]],
    groups: dict[tuple[str, str, int], dict[str, Any]],
    plan: dict[str, Any],
    expected_tasks: list[str],
    safety: dict[str, int],
    flags: dict[str, bool],
) -> dict[str, Any]:
    """§7.4 secondary block: candidate vs the budget-matched reference, same method."""
    paired, missing = _pairs(groups, plan, expected_tasks, "reference", "candidate")
    reasons = _data_reasons(trials, plan, len(paired), missing, **flags)
    if safety["reference"] > 0:
        reasons.append("reference_safety_failure")
    benefit = _benefit_versus(groups, plan, expected_tasks, "reference") if paired else None
    decision, fields = _comparison(
        plan,
        paired=paired,
        benefit=benefit,
        candidate_safety=safety["candidate"],
        other_safety=safety["reference"],
        missing=bool(reasons),
        e_crossed=False,
    )
    return {
        "decision_class": decision,
        **fields,
        "benefit": benefit,
        "missing": missing,
        "reasons": reasons,
    }


def _analyze_versioned(
    trials: list[dict[str, Any]],
    groups: dict[tuple[str, str, int], dict[str, Any]],
    plan: dict[str, Any],
    expected_tasks: list[str],
    *,
    environment_drifted: bool,
    contamination: bool,
) -> dict[str, Any]:
    """D-099 analysis of a plan with `evaluator_version_ref` (§7.2-§7.6). Interval, unit and
    step-2 reasons are the legacy ones; the verdict follows the decision class."""
    arms = _arms(plan)
    repeats = plan["repeats_per_task"]
    endpoint = plan.get("endpoint", "noninferiority")
    safety = {
        arm: sum(t.get("safety_failures", 0) for t in trials if t["arm"] == arm) for arm in arms
    }
    flags = {"environment_drifted": environment_drifted, "contamination": contamination}
    paired, missing = _pairs(groups, plan, expected_tasks, "baseline", "candidate")
    reasons = _data_reasons(trials, plan, len(paired), missing, **flags)
    if safety.get("reference", 0) > 0:
        # Not the candidate's failure (IC-18 a), but the run is not clean either.
        reasons.append("reference_safety_failure")
    step_two = bool(reasons)
    if safety["candidate"] > 0:
        reasons.append("safety_failure")
    elif safety["baseline"] > 0:
        reasons.append("baseline_safety_failure")
    e_block = _e_process_block(
        plan, sum(not b and c for b, c in paired), sum(b and not c for b, c in paired)
    )
    benefit = _benefit_versus(groups, plan, expected_tasks, "baseline") if paired else None
    decision, fields = _comparison(
        plan,
        paired=paired,
        benefit=benefit,
        candidate_safety=safety["candidate"],
        other_safety=safety["baseline"],
        missing=step_two,
        e_crossed=bool(e_block and e_block["crossed"]),
    )
    if fields.get("success_axis") == "worse" and decision == "regression":
        reasons.append("inferiority")
    elif fields.get("success_axis") == "unclear" and decision == "inconclusive":
        reasons.append("interval_crosses_noninferiority_boundary")
    if not paired and not reasons:
        reasons.append("no_paired_tasks")
    result: dict[str, Any] = {
        "verdict": report_verdict(decision, endpoint),
        "decision_class": decision,
        "evaluator_version_ref": plan["evaluator_version_ref"],
        "endpoint": endpoint,
        "minimum_effect": plan.get("minimum_effect"),
        "noise_band": plan.get("noise_band"),
        "mde": plan.get("mde"),
        "purpose": plan.get("purpose", "exploratory"),
        "benefit": benefit,
        "method": plan["method"],
        "unit": "distinct_task_all_repeats_pass",
        "task_count": fields["task_count"],
        "candidate_minus_baseline": fields.get("difference"),
        "confidence": plan["confidence"],
        "confidence_interval": fields.get("confidence_interval"),
        "noninferiority_margin": plan["noninferiority_margin"],
        "success_axis": fields.get("success_axis"),
        "cost_axis": fields.get("cost_axis"),
        "safety_failures": sum(safety.values()),
        "safety_failures_by_arm": safety,
        "missing": missing,
        "reasons": reasons,
        "cost_delta_mean_microunits": _mean_delta(groups, plan, expected_tasks, _cost),
        "elapsed_delta_mean_ms": _mean_delta(groups, plan, expected_tasks, _elapsed),
        "tokens_delta_mean": _mean_delta(groups, plan, expected_tasks, _tokens),
        "interval_limitations": (
            "Wilson approximation with conservative simultaneous adjustment; "
            "not an exact finite-sample guarantee"
        ),
        "secondary_metrics_use": "descriptive_only_not_a_safety_tradeoff",
    }
    if "basis" in fields:
        result["basis"] = fields["basis"]
    if e_block is not None:
        result["e_process"] = e_block
    if repeats > 1:
        result["pass_k"] = {arm: _pass_k(trials, expected_tasks, repeats, arm) for arm in arms}
        result["per_task"] = {
            arm: per_task_rates(trials, expected_tasks, repeats, arm) for arm in arms
        }
    if "reference" in arms:
        versus = _versus_reference(trials, groups, plan, expected_tasks, safety, flags)
        result["vs_reference"] = versus
        if versus["decision_class"] == "regression":
            # §7.4: a candidate dominated by the budget-matched reference does not pass; a
            # failing primary comparison stays a failure.
            reasons.append("dominated_by_budget_matched_reference")
            if result["verdict"] == "pass":
                result["verdict"] = "inconclusive"
    return result


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
            # IC-08: a token benefit names its own missing pairs; the cost reason is unchanged.
            "reason": "missing_token_pairs"
            if b["endpoint"] == "tokens_mean"
            else "missing_cost_pairs",
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
