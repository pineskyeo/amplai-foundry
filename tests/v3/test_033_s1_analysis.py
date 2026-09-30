"""Work 033 S1: evaluator analysis (D-099, interfaces.md 7).

Covers Q-03 (negative controls), Q-04 (positive control), Q-05 (every decision class, exact
boundaries), Q-07 (`tokens_mean` benefit, IC-08), Q-08 confirmatory part (IC-09), Q-14 (safety
attribution, IC-18 a) and the plan validation / arm / pass^k / e-process / reference-arm rules of
7.1-7.6. Q-01 (legacy plans identical) stays in `test_033_golden_analysis.py`; here a few legacy
spot checks pin the differences the new fields must not leak into legacy plans.
"""

from __future__ import annotations

import copy
import math
import random
from statistics import NormalDist
from typing import Any

import pytest

from amplai_foundry.evaluation import analysis
from amplai_foundry.evaluation.analysis import (
    analyze_pairs,
    decision_class,
    per_task_rates,
    report_verdict,
    validate_analysis_plan,
)
from amplai_foundry.evaluation.sequential import (
    e_process_update,
    e_value,
    min_tasks_for_margin,
    wilson,
)
from amplai_foundry.runtime.errors import Hold, RuntimeFault

EV_REF = {"id": "evaluator-1", "revision": 1, "digest": "sha256:" + "a" * 64}
REF_COMP = {"id": "comp-ref", "revision": 2, "digest": "sha256:" + "b" * 64}
PRIOR_REF = {"id": "report-1", "revision": 1, "digest": "sha256:" + "c" * 64}
Z95 = NormalDist().inv_cdf(1 - 0.05 / 4)


def plan(**over: Any) -> dict[str, Any]:
    """A versioned exploratory plan; `over` overrides or (None) drops fields."""
    base: dict[str, Any] = {
        "method": "paired_binary_conservative",
        "confidence": 0.95,
        "minimum_tasks": 2,
        "repeats_per_task": 1,
        "noninferiority_margin": 0.25,
        "safety_failure_limit": 0,
        "missing_policy": "inconclusive",
        "cost_basis": "not_compared",
        "evaluator_version_ref": EV_REF,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def legacy(**over: Any) -> dict[str, Any]:
    return {k: v for k, v in plan(evaluator_version_ref=None, **over).items()}


def confirmatory(**over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "purpose": "confirmatory",
        "sample_rationale": "fixed corpus\nMDE: 0.3 at n=40",
        "variance_basis": "A/A calibration",
        "sequential_rule": "fixed_sample_safety_abort_only",
        "mde": 0.3,
    }
    fields.update(over)
    return plan(**fields)


def tasks(n: int) -> list[str]:
    return [f"t{i:03d}" for i in range(n)]


def trial(task: str, arm: str, success: bool | None, repeat: int = 0, **extra: Any) -> dict:
    return {"task_id": task, "arm": arm, "repeat": repeat, "success": success, **extra}


def outcome_trials(
    names: list[str],
    wins: int = 0,
    losses: int = 0,
    both_pass: int = 0,
    both_fail: int = 0,
) -> list[dict[str, Any]]:
    """Paired trials: the first `wins` tasks are candidate-only passes, then `losses`
    baseline-only passes, then both-pass, then both-fail."""
    assert wins + losses + both_pass + both_fail == len(names)
    pairs = (
        [(False, True)] * wins
        + [(True, False)] * losses
        + [(True, True)] * both_pass
        + [(False, False)] * both_fail
    )
    out = []
    for name, (b, c) in zip(names, pairs, strict=True):
        out.append(trial(name, "baseline", b))
        out.append(trial(name, "candidate", c))
    return out


def run(trials: list[dict], p: dict[str, Any], n: int, **flags: bool) -> dict[str, Any]:
    return analyze_pairs(trials, p, expected_tasks=tasks(n), **flags)


def klass(
    *,
    low: float = 0.0,
    high: float = 0.5,
    delta: float = 0.0,
    benefit: dict | None = None,
    endpoint: str = "noninferiority",
    margin: float = 0.25,
    minimum_effect: float | None = 0.05,
    noise_band: float | None = None,
    cand: int = 0,
    base: int = 0,
    missing: bool = False,
) -> str:
    return decision_class(
        low=low,
        high=high,
        delta=delta,
        benefit=benefit,
        endpoint=endpoint,
        margin=margin,
        minimum_effect=minimum_effect,
        noise_band=noise_band,
        candidate_safety_failures=cand,
        baseline_safety_failures=base,
        missing=missing,
    )


CHEAPER = {"verdict": "pass"}
COSTLIER = {"verdict": "fail"}
UNKNOWN = {"verdict": "inconclusive"}


def fault(code: str, fn: Any, *args: Any, **kwargs: Any) -> RuntimeFault:
    with pytest.raises(RuntimeFault) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


# --- Q-05: every decision class, exact boundaries ------------------------------------------


def test_class_improvement() -> None:
    assert klass(low=0.2, high=0.9, delta=0.5) == "improvement"


def test_class_improvement_with_unknown_cost_and_cheaper() -> None:
    assert klass(low=0.2, delta=0.5, benefit=UNKNOWN) == "improvement"
    assert klass(low=0.2, delta=0.5, benefit=CHEAPER) == "improvement"


def test_class_tradeoff_better_but_costlier() -> None:
    assert klass(low=0.2, delta=0.5, benefit=COSTLIER) == "tradeoff"


def test_class_tradeoff_worse_but_cheaper() -> None:
    assert klass(low=-0.9, high=-0.4, delta=-0.6, benefit=CHEAPER) == "tradeoff"


def test_class_efficiency() -> None:
    assert klass(low=-0.1, high=0.1, benefit=CHEAPER, minimum_effect=None) == "efficiency"


def test_class_non_inferior() -> None:
    assert klass(low=-0.1, high=0.1) == "non_inferior"
    assert klass(low=-0.1, high=0.1, benefit=COSTLIER) == "non_inferior"
    assert klass(low=-0.1, high=0.1, benefit=UNKNOWN) == "non_inferior"


def test_class_regression() -> None:
    assert klass(low=-0.9, high=-0.4, delta=-0.6) == "regression"
    assert klass(low=-0.9, high=-0.4, delta=-0.6, benefit=COSTLIER) == "regression"


def test_class_inconclusive_when_the_interval_is_unclear() -> None:
    assert klass(low=-0.5, high=0.3) == "inconclusive"


def test_class_regression_on_candidate_safety_failure_even_when_better() -> None:
    assert klass(low=0.2, delta=0.5, cand=1) == "regression"


def test_class_inconclusive_on_baseline_safety_failure() -> None:
    assert klass(low=0.2, delta=0.5, base=1) == "inconclusive"


def test_class_candidate_safety_wins_over_baseline_safety() -> None:
    assert klass(cand=1, base=1) == "regression"


def test_class_inconclusive_when_data_missing() -> None:
    assert klass(low=0.2, delta=0.5, missing=True) == "inconclusive"
    assert klass(low=-0.9, high=-0.4, missing=True) == "inconclusive"


def test_class_candidate_safety_beats_missing() -> None:
    assert klass(cand=2, missing=True) == "regression"


def test_boundary_low_equal_to_minus_margin_is_non_inferior() -> None:
    assert klass(low=-0.25, high=0.2, margin=0.25) == "non_inferior"
    assert klass(low=math.nextafter(-0.25, -1), high=0.2, margin=0.25) == "inconclusive"


def test_boundary_high_equal_to_minus_margin_is_not_worse() -> None:
    """`worse` needs high < -margin strictly."""
    assert klass(low=-0.9, high=-0.25, margin=0.25) == "inconclusive"
    assert klass(low=-0.9, high=math.nextafter(-0.25, -1), margin=0.25) == "regression"


def test_boundary_low_equal_to_minimum_effect_is_not_better() -> None:
    assert klass(low=0.05, high=0.6, minimum_effect=0.05) == "non_inferior"
    assert klass(low=math.nextafter(0.05, 1), high=0.6, minimum_effect=0.05) == "improvement"


def test_no_minimum_effect_means_never_better() -> None:
    assert klass(low=0.9, high=1.0, delta=0.95, minimum_effect=None) == "non_inferior"


def test_noise_band_blocks_better_and_worse() -> None:
    assert klass(low=0.2, delta=0.1, noise_band=0.3) != "improvement"
    assert klass(low=-0.9, high=-0.4, delta=-0.1, noise_band=0.3) != "regression"


def test_noise_band_boundary_is_strict() -> None:
    """|delta| < noise_band is unresolved; |delta| == noise_band is not."""
    assert klass(low=0.2, delta=0.3, noise_band=0.3) == "improvement"
    assert klass(low=0.2, delta=0.29, noise_band=0.3) != "improvement"


def test_noise_band_does_not_touch_non_inferior() -> None:
    assert klass(low=-0.1, high=0.1, delta=0.0, noise_band=0.5) == "non_inferior"


def test_noise_band_zero_changes_nothing() -> None:
    assert klass(low=0.2, delta=0.0, noise_band=0.0) == "improvement"


def test_unknown_endpoint_is_refused() -> None:
    fault("ANALYSIS_METHOD", klass, endpoint="dominance")


@pytest.mark.parametrize(
    "decision,endpoint,verdict",
    [
        ("improvement", "noninferiority", "pass"),
        ("efficiency", "noninferiority", "pass"),
        ("non_inferior", "noninferiority", "pass"),
        ("tradeoff", "noninferiority", "inconclusive"),
        ("inconclusive", "noninferiority", "inconclusive"),
        ("regression", "noninferiority", "fail"),
        ("improvement", "superiority", "pass"),
        ("efficiency", "superiority", "inconclusive"),
        ("non_inferior", "superiority", "inconclusive"),
        ("tradeoff", "superiority", "inconclusive"),
        ("regression", "superiority", "fail"),
    ],
)
def test_report_verdict_table(decision: str, endpoint: str, verdict: str) -> None:
    assert report_verdict(decision, endpoint) == verdict


def test_every_class_reachable_through_analyze_pairs() -> None:
    n = 40
    minimum = 0.05
    cases = {
        "improvement": (
            outcome_trials(tasks(n), wins=20, both_pass=20),
            {"minimum_effect": minimum},
        ),
        "non_inferior": (outcome_trials(tasks(n), both_pass=n), {}),
        "regression": (
            outcome_trials(tasks(n), losses=20, both_pass=20),
            {"noninferiority_margin": 0.15},
        ),
        "inconclusive": (outcome_trials(tasks(n), wins=4, losses=10, both_pass=26), {}),
    }
    for expected, (trials, extra) in cases.items():
        result = run(trials, plan(**extra), n)
        assert result["decision_class"] == expected, (expected, result["reasons"])


def test_efficiency_and_tradeoff_through_analyze_pairs_with_token_benefit() -> None:
    n = 40
    names = tasks(n)
    benefit = {
        "endpoint": "tokens_mean",
        "minimum_reduction": 10,
        "bootstrap_samples": 1000,
        "seed": 7,
    }

    def with_tokens(base: int, cand: int, **kw: int) -> list[dict]:
        trials = outcome_trials(names, **kw)
        for t in trials:
            t["input_tokens"] = base if t["arm"] == "baseline" else cand
            t["output_tokens"] = 1
        return trials

    result = run(with_tokens(1000, 100, both_pass=n), plan(benefit=benefit), n)
    assert result["decision_class"] == "efficiency"
    assert result["cost_axis"] == "cheaper"
    assert result["verdict"] == "pass"
    result = run(
        with_tokens(100, 1000, wins=20, both_pass=20),
        plan(benefit=benefit, minimum_effect=0.05),
        n,
    )
    assert result["decision_class"] == "tradeoff"
    assert result["cost_axis"] == "costlier"
    assert result["verdict"] == "inconclusive"
    result = run(
        with_tokens(100, 1000, losses=20, both_pass=20),
        plan(benefit=benefit, noninferiority_margin=0.15),
        n,
    )
    assert result["decision_class"] == "regression" and result["verdict"] == "fail"
    # worse but cheaper is a tradeoff, not a regression (7.2 step 5)
    result = run(
        with_tokens(1000, 100, losses=20, both_pass=20),
        plan(benefit=benefit, noninferiority_margin=0.15),
        n,
    )
    assert result["decision_class"] == "tradeoff" and result["verdict"] == "inconclusive"
    result = run(with_tokens(1, 1, losses=20, both_pass=20), plan(noninferiority_margin=0.15), n)
    assert result["decision_class"] == "regression" and result["verdict"] == "fail"


# --- Q-03: negative controls ----------------------------------------------------------------


def test_candidate_failing_half_the_tasks_is_a_regression_and_fails() -> None:
    n = 40
    result = run(
        outcome_trials(tasks(n), losses=20, both_pass=20), plan(noninferiority_margin=0.15), n
    )
    assert result["decision_class"] == "regression"
    assert result["verdict"] == "fail"
    assert "inferiority" in result["reasons"]


def test_half_failing_is_only_a_regression_when_the_margin_is_tight_enough() -> None:
    """Q-03 does not fix the margin: at m = 0.25 and n = 40, 20 lost tasks have upper bound
    -0.221 (not < -0.25, inconclusive); 22 lost tasks are needed. Recorded in the S1 report."""
    n = 40
    loose = run(outcome_trials(tasks(n), losses=20, both_pass=20), plan(), n)
    assert loose["decision_class"] == "inconclusive"
    assert loose["confidence_interval"][1] > -0.25
    tight = run(outcome_trials(tasks(n), losses=22, both_pass=18), plan(), n)
    assert tight["decision_class"] == "regression"


def test_candidate_failing_every_task_is_a_regression() -> None:
    n = 12
    result = run(outcome_trials(tasks(n), losses=n), plan(), n)
    assert result["decision_class"] == "regression" and result["verdict"] == "fail"


def test_aa_simulations_rarely_report_improvement() -> None:
    """1,000 seeded A/A simulations (same per-task pass probability in both arms) under a
    superiority plan: the improvement rate has a Wilson 99 % upper bound <= 0.05."""
    n, sims = 40, 1000
    rng = random.Random(0x03303)
    names = tasks(n)
    p = plan(endpoint="superiority", minimum_effect=0.05)
    improvements = 0
    for _ in range(sims):
        rates = [rng.uniform(0.2, 0.95) for _ in names]
        trials = []
        for name, rate in zip(names, rates, strict=True):
            for arm in ("baseline", "candidate"):
                trials.append(trial(name, arm, rng.random() < rate))
        result = run(trials, p, n)
        improvements += result["decision_class"] == "improvement"
    z99 = NormalDist().inv_cdf(0.995)
    _, upper = wilson(improvements, sims, z99)
    assert upper <= 0.05, improvements


def test_aa_with_non_inferiority_plan_never_regresses_when_identical() -> None:
    n = 40
    result = run(outcome_trials(tasks(n), both_pass=20, both_fail=20), plan(), n)
    assert result["decision_class"] == "non_inferior"
    assert result["candidate_minus_baseline"] == 0


# --- Q-04: positive control -----------------------------------------------------------------


def test_sixteen_wins_of_forty_with_minimum_effect_is_an_improvement() -> None:
    n = 40
    result = run(outcome_trials(tasks(n), wins=16, both_pass=24), plan(minimum_effect=0.05), n)
    assert result["decision_class"] == "improvement"
    assert result["verdict"] == "pass"
    assert result["success_axis"] == "better"
    assert result["confidence_interval"][0] > 0.05


def test_twelve_wins_suffice_at_forty_and_eleven_do_not() -> None:
    n = 40
    for wins, expected in ((12, "improvement"), (11, "non_inferior")):
        result = run(
            outcome_trials(tasks(n), wins=wins, both_pass=n - wins),
            plan(minimum_effect=0.05),
            n,
        )
        assert result["decision_class"] == expected, wins


def test_superiority_endpoint_passes_only_on_improvement() -> None:
    n = 40
    p = plan(endpoint="superiority", minimum_effect=0.05)
    assert run(outcome_trials(tasks(n), wins=16, both_pass=24), p, n)["verdict"] == "pass"
    flat = run(outcome_trials(tasks(n), both_pass=n), p, n)
    assert flat["decision_class"] == "non_inferior" and flat["verdict"] == "inconclusive"


def test_interval_fields_are_the_legacy_ones() -> None:
    n = 40
    result = run(outcome_trials(tasks(n), wins=16, both_pass=24), plan(minimum_effect=0.05), n)
    pl, pu = wilson(16, n, Z95)
    nl, nu = wilson(0, n, Z95)
    assert result["confidence_interval"] == pytest.approx([pl - nu, pu - nl])
    assert result["candidate_minus_baseline"] == pytest.approx(16 / n)


# --- Q-14: safety attribution (IC-18 a) -----------------------------------------------------


def _with_safety(trials: list[dict], arm: str, count: int = 1) -> list[dict]:
    trials = copy.deepcopy(trials)
    for t in trials:
        if t["arm"] == arm:
            t["safety_failures"] = count
            break
    return trials


def test_candidate_safety_failure_is_regression_and_fail() -> None:
    n = 40
    trials = _with_safety(outcome_trials(tasks(n), wins=20, both_pass=20), "candidate")
    result = run(trials, plan(minimum_effect=0.05), n)
    assert result["decision_class"] == "regression"
    assert result["verdict"] == "fail"
    assert "safety_failure" in result["reasons"]
    assert result["safety_failures_by_arm"]["candidate"] == 1


def test_baseline_only_safety_failure_is_inconclusive_with_reason() -> None:
    n = 40
    trials = _with_safety(outcome_trials(tasks(n), wins=20, both_pass=20), "baseline")
    result = run(trials, plan(minimum_effect=0.05), n)
    assert result["decision_class"] == "inconclusive"
    assert result["verdict"] == "inconclusive"
    assert "baseline_safety_failure" in result["reasons"]
    assert "safety_failure" not in result["reasons"]
    assert result["safety_failures_by_arm"] == {"baseline": 1, "candidate": 0}


def test_both_arms_safety_failure_is_regression() -> None:
    n = 40
    trials = _with_safety(outcome_trials(tasks(n), both_pass=n), "baseline")
    trials = _with_safety(trials, "candidate")
    result = run(trials, plan(), n)
    assert result["decision_class"] == "regression" and result["verdict"] == "fail"


def test_legacy_plan_sums_safety_over_both_arms() -> None:
    """G3: a legacy plan fails on a baseline-only safety failure, as at c9f896a."""
    n = 40
    trials = _with_safety(outcome_trials(tasks(n), both_pass=n), "baseline")
    result = run(trials, legacy(), n)
    assert result["verdict"] == "fail"
    assert "safety_failure" in result["reasons"]
    assert "decision_class" not in result
    assert "baseline_safety_failure" not in result["reasons"]


def test_safety_with_no_paired_tasks() -> None:
    result = run([trial("t000", "candidate", True, safety_failures=1)], plan(), 3)
    assert result["decision_class"] == "regression" and result["verdict"] == "fail"
    result = run([trial("t000", "baseline", True, safety_failures=1)], plan(), 3)
    assert result["decision_class"] == "inconclusive"
    assert "baseline_safety_failure" in result["reasons"]


# --- data reasons ---------------------------------------------------------------------------


def test_missing_trials_are_inconclusive() -> None:
    n = 40
    trials = outcome_trials(tasks(n), both_pass=n)[:-2]
    result = run(trials, plan(), n)
    assert result["decision_class"] == "inconclusive"
    assert "missing_or_unknown_trials" in result["reasons"]
    assert result["missing"] == [
        {"task_id": "t039", "arm": "baseline"},
        {"task_id": "t039", "arm": "candidate"},
    ]


def test_unknown_result_and_unknown_effect_are_missing() -> None:
    n = 40
    trials = outcome_trials(tasks(n), both_pass=n)
    trials[0]["success"] = None
    trials[3]["unknown_effects"] = 1
    result = run(trials, plan(), n)
    assert result["decision_class"] == "inconclusive"
    assert len(result["missing"]) == 2


def test_insufficient_tasks_drift_and_contamination_are_inconclusive() -> None:
    n = 4
    trials = outcome_trials(tasks(n), both_pass=n)
    result = run(trials, plan(minimum_tasks=5), n)
    assert result["decision_class"] == "inconclusive"
    assert "insufficient_distinct_tasks" in result["reasons"]
    n = 40
    trials = outcome_trials(tasks(n), both_pass=n)
    drift = run(trials, plan(), n, environment_drifted=True)
    assert drift["decision_class"] == "inconclusive" and "environment_drift" in drift["reasons"]
    dirty = run(trials, plan(), n, contamination=True)
    assert dirty["decision_class"] == "inconclusive"
    assert "corpus_contamination" in dirty["reasons"]


def test_compared_cost_with_unknown_cost_is_inconclusive() -> None:
    n = 40
    result = run(outcome_trials(tasks(n), both_pass=n), plan(cost_basis="compared"), n)
    assert result["decision_class"] == "inconclusive"
    assert "unknown_cost" in result["reasons"]


def test_no_paired_tasks_reason() -> None:
    result = run([], plan(), 3)
    assert result["decision_class"] == "inconclusive"
    assert result["task_count"] == 0
    assert result["reasons"]


def test_noise_band_shows_in_analysis_and_blocks_improvement() -> None:
    n = 40
    trials = outcome_trials(tasks(n), wins=16, both_pass=24)
    result = run(trials, plan(minimum_effect=0.05, noise_band=0.9), n)
    assert result["decision_class"] != "improvement"
    assert result["success_axis"] == "unresolved"
    assert result["noise_band"] == 0.9


# --- Q-07: tokens_mean benefit (IC-08) ------------------------------------------------------

BENEFIT_TOKENS = {
    "endpoint": "tokens_mean",
    "minimum_reduction": 50,
    "bootstrap_samples": 1000,
    "seed": 3,
}
BENEFIT_COST = {**BENEFIT_TOKENS, "endpoint": "cost_mean_microunits"}


def test_tokens_mean_benefit_is_allowed_with_not_compared() -> None:
    validate_analysis_plan(plan(cost_basis="not_compared", benefit=BENEFIT_TOKENS))
    validate_analysis_plan(legacy(cost_basis="not_compared", benefit=BENEFIT_TOKENS))


def test_cost_benefit_is_still_refused_with_not_compared() -> None:
    fault(
        "COST_BASIS", validate_analysis_plan, plan(cost_basis="not_compared", benefit=BENEFIT_COST)
    )
    fault(
        "COST_BASIS",
        validate_analysis_plan,
        legacy(cost_basis="not_compared", benefit=BENEFIT_COST),
    )


def test_cost_benefit_is_allowed_with_compared() -> None:
    validate_analysis_plan(plan(cost_basis="compared", benefit=BENEFIT_COST))


def test_tokens_benefit_with_compared_basis_is_allowed() -> None:
    validate_analysis_plan(plan(cost_basis="compared", benefit=BENEFIT_TOKENS))


def test_unknown_benefit_endpoint_and_bad_shape_are_refused() -> None:
    fault(
        "BENEFIT_PLAN",
        validate_analysis_plan,
        plan(cost_basis="compared", benefit={**BENEFIT_TOKENS, "endpoint": "latency_mean"}),
    )
    # With not_compared any non-token endpoint is a COST_BASIS refusal first.
    fault(
        "COST_BASIS",
        validate_analysis_plan,
        plan(benefit={**BENEFIT_TOKENS, "endpoint": "latency_mean"}),
    )
    fault(
        "BENEFIT_PLAN",
        validate_analysis_plan,
        plan(benefit={"endpoint": "tokens_mean", "minimum_reduction": 5}),
    )
    fault(
        "BENEFIT_PLAN",
        validate_analysis_plan,
        plan(benefit={**BENEFIT_TOKENS, "minimum_reduction": 0}),
    )
    fault(
        "BENEFIT_PLAN",
        validate_analysis_plan,
        plan(benefit={**BENEFIT_TOKENS, "bootstrap_samples": 10}),
    )


def _token_trials(n: int, base: int, cand: int) -> list[dict]:
    trials = outcome_trials(tasks(n), both_pass=n)
    for t in trials:
        t["input_tokens"] = (base if t["arm"] == "baseline" else cand) - 1
        t["output_tokens"] = 1
    return trials


def test_token_benefit_pass_and_delta() -> None:
    n = 40
    result = run(_token_trials(n, 1000, 100), plan(benefit=BENEFIT_TOKENS), n)
    assert result["benefit"]["verdict"] == "pass"
    assert result["benefit"]["endpoint"] == "tokens_mean"
    assert result["benefit"]["candidate_minus_baseline"] == pytest.approx(-900)
    assert result["tokens_delta_mean"] == pytest.approx(-900)
    assert result["cost_axis"] == "cheaper"


def test_token_benefit_fail_when_candidate_uses_more() -> None:
    n = 40
    result = run(_token_trials(n, 100, 1000), plan(benefit=BENEFIT_TOKENS), n)
    assert result["benefit"]["verdict"] == "fail"
    assert result["cost_axis"] == "costlier"
    assert result["decision_class"] == "non_inferior"


def test_token_benefit_missing_tokens_is_inconclusive_with_its_own_reason() -> None:
    n = 40
    trials = _token_trials(n, 1000, 100)
    del trials[0]["input_tokens"]
    result = run(trials, plan(benefit=BENEFIT_TOKENS), n)
    assert result["benefit"] == {
        "verdict": "inconclusive",
        "reason": "missing_token_pairs",
        "task_count": n - 1,
    }
    assert result["cost_axis"] == "unknown"


def test_missing_cost_pairs_reason_is_unchanged() -> None:
    n = 40
    trials = outcome_trials(tasks(n), both_pass=n)
    result = run(trials, plan(cost_basis="compared", benefit=BENEFIT_COST), n)
    assert result["benefit"]["reason"] == "missing_cost_pairs"


@pytest.mark.parametrize("bad", [-1, 1.5, True, "9"])
def test_token_counts_are_validated_when_a_token_benefit_is_declared(bad: Any) -> None:
    n = 4
    trials = outcome_trials(tasks(n), both_pass=n)
    trials[0]["input_tokens"] = bad
    fault("TRIAL_USAGE", run, trials, plan(benefit=BENEFIT_TOKENS), n)


def test_token_counts_are_not_validated_without_a_token_benefit() -> None:
    n = 40
    trials = outcome_trials(tasks(n), both_pass=n)
    trials[0]["input_tokens"] = -1
    assert run(trials, plan(), n)["decision_class"] == "non_inferior"


# --- Q-08 (confirmatory part), IC-09 --------------------------------------------------------


def _concordant_class(n: int, margin: float = 0.25) -> str:
    result = run(outcome_trials(tasks(n), both_pass=n), plan(noninferiority_margin=margin), n)
    return result["decision_class"]


def test_confirmatory_minimum_is_exactly_what_the_analysis_can_pass() -> None:
    """n = 15 cannot pass at m = 0.25; n = 16 can: the boundary of min_tasks_for_margin."""
    assert min_tasks_for_margin(0.25, 0.95) == 16
    assert _concordant_class(15) == "inconclusive"
    assert _concordant_class(16) == "non_inferior"


@pytest.mark.parametrize("margin", [0.2, 0.15, 0.1])
def test_min_tasks_boundary_holds_for_other_margins(margin: float) -> None:
    n = min_tasks_for_margin(margin, 0.95)
    assert _concordant_class(n, margin) == "non_inferior"
    assert _concordant_class(n - 1, margin) == "inconclusive"


# --- 7.1 plan validation --------------------------------------------------------------------


def test_versioned_plan_validates() -> None:
    validate_analysis_plan(plan())
    validate_analysis_plan(plan(endpoint="superiority", minimum_effect=0.05))
    validate_analysis_plan(plan(endpoint="noninferiority", minimum_effect=0.05, noise_band=0.1))
    validate_analysis_plan(plan(mde=0.3))
    validate_analysis_plan(plan(max_trial_tokens=5000))


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", "superiority"),
        ("minimum_effect", 0.05),
        ("noise_band", 0.1),
        ("mde", 0.3),
        (
            "reference_arm",
            {"composition_ref": REF_COMP, "kind": "best_of_n", "n": 2, "cost_match": "tokens"},
        ),
    ],
)
def test_evaluator_fields_need_the_version_ref(field: str, value: Any) -> None:
    extra = {"minimum_effect": 0.05} if field == "endpoint" else {}
    fault("ANALYSIS_PLAN", validate_analysis_plan, legacy(**{field: value}, **extra))


def test_e_process_field_needs_the_version_ref() -> None:
    spec = {"alpha": 0.05, "key": "k", "prior_state": None, "prior_report_refs": []}
    fault("ANALYSIS_PLAN", validate_analysis_plan, legacy(e_process=spec))


def test_max_trial_tokens_is_checked_for_legacy_and_versioned_plans() -> None:
    validate_analysis_plan(legacy(max_trial_tokens=10))
    for bad in (0, -1, 1.5, True, "5"):
        fault("ANALYSIS_PLAN", validate_analysis_plan, plan(max_trial_tokens=bad))
        fault("ANALYSIS_PLAN", validate_analysis_plan, legacy(max_trial_tokens=bad))


@pytest.mark.parametrize(
    "ref",
    [
        "evaluator-1",
        {"id": "evaluator-1"},
        {**EV_REF, "revision": 0},
        {**EV_REF, "revision": True},
        {**EV_REF, "digest": "sha256:xyz"},
        {**EV_REF, "extra": 1},
        {**EV_REF, "id": "bad id"},
        None,
    ],
)
def test_evaluator_version_ref_must_be_an_exact_ref(ref: Any) -> None:
    p = plan()
    p["evaluator_version_ref"] = ref
    fault("ANALYSIS_PLAN", validate_analysis_plan, p)


@pytest.mark.parametrize(
    "over",
    [
        {"endpoint": "dominance"},
        {"endpoint": "superiority"},
        {"minimum_effect": 0},
        {"minimum_effect": 1.5},
        {"minimum_effect": -0.1},
        {"minimum_effect": "0.1"},
        {"minimum_effect": math.nan},
        {"noise_band": -0.1},
        {"noise_band": 1.5},
        {"noise_band": math.inf},
        {"noise_band": "0.1"},
    ],
)
def test_bad_endpoint_effect_or_noise_band_is_refused(over: dict) -> None:
    fault("ANALYSIS_METHOD", validate_analysis_plan, plan(**over))


def test_minimum_effect_bounds_are_half_open() -> None:
    validate_analysis_plan(plan(minimum_effect=1))
    validate_analysis_plan(plan(noise_band=0))
    validate_analysis_plan(plan(noise_band=1))


@pytest.mark.parametrize("mde", [0, -0.5, math.nan, math.inf, "0.3"])
def test_bad_mde_is_refused(mde: Any) -> None:
    fault("SAMPLE_RATIONALE", validate_analysis_plan, plan(mde=mde))


def test_confirmatory_plan_needs_the_mde_line_and_value() -> None:
    validate_analysis_plan(confirmatory())
    no_value = confirmatory()
    del no_value["mde"]
    fault("SAMPLE_RATIONALE", validate_analysis_plan, no_value)
    fault(
        "SAMPLE_RATIONALE",
        validate_analysis_plan,
        confirmatory(sample_rationale="fixed corpus, no line"),
    )
    fault(
        "SAMPLE_RATIONALE",
        validate_analysis_plan,
        confirmatory(sample_rationale="MDE: 0.5 at n=40"),
    )
    fault(
        "SAMPLE_RATIONALE",
        validate_analysis_plan,
        confirmatory(sample_rationale="MDE: abc at n=40"),
    )


def test_exploratory_plan_never_needs_the_mde_line() -> None:
    validate_analysis_plan(plan(purpose="exploratory"))
    validate_analysis_plan(plan(purpose="exploratory", mde=0.4))


def test_legacy_confirmatory_plan_needs_no_mde() -> None:
    validate_analysis_plan(
        legacy(
            purpose="confirmatory",
            sample_rationale="fixed corpus",
            variance_basis="pilot",
            sequential_rule="fixed_sample_safety_abort_only",
        )
    )


def test_confirmatory_still_needs_rationale_variance_and_rule() -> None:
    for key in ("sample_rationale", "variance_basis", "sequential_rule"):
        p = confirmatory()
        p[key] = " "
        fault("SAMPLE_RATIONALE", validate_analysis_plan, p)


def test_e_process_accumulating_is_confirmatory_and_versioned_only() -> None:
    spec = {"alpha": 0.05, "key": "k", "prior_state": None, "prior_report_refs": []}
    validate_analysis_plan(confirmatory(sequential_rule="e_process_accumulating", e_process=spec))
    legacy_conf = legacy(
        purpose="confirmatory",
        sample_rationale="r",
        variance_basis="v",
        sequential_rule="e_process_accumulating",
    )
    fault("SEQUENTIAL_RULE", validate_analysis_plan, legacy_conf)
    fault("SEQUENTIAL_RULE", validate_analysis_plan, confirmatory(sequential_rule="anytime"))


# --- reference arm (7.4 shape) --------------------------------------------------------------


def arm(**over: Any) -> dict[str, Any]:
    base = {"composition_ref": REF_COMP, "kind": "best_of_n", "n": 2, "cost_match": "tokens"}
    base.update(over)
    return base


def test_reference_arm_shape_is_validated_with_the_hold_code() -> None:
    validate_analysis_plan(plan(reference_arm=arm()))
    for n in (1, 3):
        validate_analysis_plan(plan(reference_arm=arm(n=n)))
    bad = [
        arm(n=0),
        arm(n=4),
        arm(n=True),
        arm(n=1.5),
        arm(kind="majority"),
        arm(cost_match="cost"),
        arm(composition_ref="comp"),
        arm(composition_ref={"id": "x"}),
        {"composition_ref": REF_COMP, "kind": "best_of_n", "n": 2},
        {**arm(), "extra": 1},
        "best_of_n",
    ]
    for value in bad:
        exc = fault("REFERENCE_ARM", validate_analysis_plan, plan(reference_arm=value))
        assert isinstance(exc, Hold) and exc.outcome == "hold"


def test_null_reference_arm_is_the_two_arm_plan() -> None:
    p = plan(reference_arm=None)
    p["reference_arm"] = None
    validate_analysis_plan(p)
    fault(
        "TRIAL_ARM",
        run,
        [trial("t000", "reference", True)],
        p,
        3,
    )


def test_reference_trials_are_refused_without_a_declared_arm() -> None:
    n = 4
    trials = [*outcome_trials(tasks(n), both_pass=n), trial("t000", "reference", True)]
    fault("TRIAL_ARM", run, trials, plan(), n)
    fault("TRIAL_ARM", run, trials, legacy(), n)


def _three_arm(n: int, ref_pass: list[bool], wins: int, both_pass: int) -> list[dict]:
    trials = outcome_trials(tasks(n), wins=wins, both_pass=both_pass)
    for name, ok in zip(tasks(n), ref_pass, strict=True):
        trials.append(trial(name, "reference", ok))
    return trials


def test_dominated_candidate_is_inconclusive_with_the_reason() -> None:
    n = 40
    p = plan(reference_arm=arm(), minimum_effect=0.05)
    trials = _three_arm(n, [True] * n, wins=20, both_pass=20)
    # Candidate beats baseline but loses to the reference: reference passes everything.
    result = run(trials, p, n)
    assert result["decision_class"] == "improvement"
    assert result["vs_reference"]["decision_class"] != "regression"
    # Now make the reference clearly better than a merely non-inferior candidate.
    ref_all = [True] * n
    trials = outcome_trials(tasks(n), both_pass=20, both_fail=20)
    for name, ok in zip(tasks(n), ref_all, strict=True):
        trials.append(trial(name, "reference", ok))
    result = run(trials, plan(reference_arm=arm(), noninferiority_margin=0.15), n)
    assert result["decision_class"] == "non_inferior"
    assert result["vs_reference"]["decision_class"] == "regression"
    assert result["verdict"] == "inconclusive"
    assert "dominated_by_budget_matched_reference" in result["reasons"]


def test_failing_primary_comparison_stays_a_failure_when_also_dominated() -> None:
    n = 40
    trials = outcome_trials(tasks(n), losses=20, both_pass=20)
    for name in tasks(n):
        trials.append(trial(name, "reference", True))
    result = run(trials, plan(reference_arm=arm(), noninferiority_margin=0.15), n)
    assert result["verdict"] == "fail" and result["decision_class"] == "regression"
    assert "dominated_by_budget_matched_reference" in result["reasons"]


def test_reference_arm_not_dominating_leaves_the_verdict() -> None:
    n = 40
    trials = outcome_trials(tasks(n), both_pass=n)
    for name in tasks(n):
        trials.append(trial(name, "reference", True))
    result = run(trials, plan(reference_arm=arm()), n)
    assert result["verdict"] == "pass"
    assert result["vs_reference"]["decision_class"] == "non_inferior"
    assert "dominated_by_budget_matched_reference" not in result["reasons"]


def test_missing_reference_trials_make_vs_reference_inconclusive_only() -> None:
    n = 40
    trials = outcome_trials(tasks(n), both_pass=n)
    result = run(trials, plan(reference_arm=arm()), n)
    assert result["verdict"] == "pass"
    versus = result["vs_reference"]
    assert versus["decision_class"] == "inconclusive"
    assert versus["task_count"] == 0
    assert versus["reasons"]


def test_reference_safety_failure_is_not_a_candidate_regression() -> None:
    n = 40
    trials = outcome_trials(tasks(n), both_pass=n)
    for name in tasks(n):
        trials.append(trial(name, "reference", True))
    trials[-1]["safety_failures"] = 1
    result = run(trials, plan(reference_arm=arm()), n)
    assert result["safety_failures_by_arm"] == {"baseline": 0, "candidate": 0, "reference": 1}
    assert "reference_safety_failure" in result["reasons"]
    assert result["decision_class"] == "inconclusive"
    assert result["verdict"] == "inconclusive"


def test_two_arm_result_has_no_vs_reference_block() -> None:
    n = 4
    result = run(outcome_trials(tasks(n), both_pass=n), plan(), n)
    assert "vs_reference" not in result
    assert set(result["safety_failures_by_arm"]) == {"baseline", "candidate"}


# --- 7.3 pass^k and per-task rates ----------------------------------------------------------


def _repeat_trials(names: list[str], repeats: int, results: dict) -> list[dict]:
    trials = []
    for name in names:
        for arm_name in ("baseline", "candidate"):
            for r in range(repeats):
                trials.append(trial(name, arm_name, results[(name, arm_name, r)], repeat=r))
    return trials


def test_per_task_rates_counts_known_runs_only() -> None:
    trials = [
        trial("a", "baseline", True, 0),
        trial("a", "baseline", False, 1),
        trial("a", "baseline", None, 2),
        trial("b", "baseline", True, 0, unknown_effects=1),
        trial("a", "candidate", True, 0),
        trial("a", "baseline", True, 7),
    ]
    rows = per_task_rates(trials, ["a", "b", "c"], 3, "baseline")
    assert rows["a"] == {"passes": 1, "runs": 2, "rate": 0.5}
    assert rows["b"] == {"passes": 0, "runs": 0, "rate": None}
    assert rows["c"] == {"passes": 0, "runs": 0, "rate": None}
    assert per_task_rates(trials, ["a"], 3, "candidate")["a"]["rate"] == 1.0


def test_pass_k_and_per_task_appear_only_with_repeats() -> None:
    n = 4
    single = run(outcome_trials(tasks(n), both_pass=n), plan(), n)
    assert "pass_k" not in single and "per_task" not in single
    names = tasks(n)
    results = {}
    for i, name in enumerate(names):
        for r in range(3):
            results[(name, "baseline", r)] = True
            results[(name, "candidate", r)] = not (i == 0 and r == 2)
    result = run(_repeat_trials(names, 3, results), plan(repeats_per_task=3), n)
    assert result["pass_k"]["baseline"] == {"k": 3, "rate": 1.0}
    assert result["pass_k"]["candidate"] == {"k": 3, "rate": 0.75}
    assert result["per_task"]["candidate"]["t000"] == {
        "passes": 2,
        "runs": 3,
        "rate": pytest.approx(2 / 3),
    }
    assert result["per_task"]["baseline"]["t001"]["rate"] == 1.0
    # The unit stays "every repeat passes": task t000 is a loss.
    assert result["candidate_minus_baseline"] == pytest.approx(-0.25)


def test_pass_k_rate_ignores_tasks_with_an_incomplete_arm() -> None:
    names = tasks(2)
    results = {(n, a, r): True for n in names for a in ("baseline", "candidate") for r in range(2)}
    trials = _repeat_trials(names, 2, results)
    trials = [
        t
        for t in trials
        if not (t["task_id"] == "t000" and t["arm"] == "candidate" and t["repeat"] == 1)
    ]
    result = run(trials, plan(repeats_per_task=2), 2)
    assert result["pass_k"]["candidate"] == {"k": 2, "rate": 1.0}
    assert result["missing"] == [{"task_id": "t000", "arm": "candidate"}]


# --- e-process in the analysis (7.6) --------------------------------------------------------


def e_spec(prior_state: Any = None, refs: Any = None, alpha: float = 0.05) -> dict[str, Any]:
    return {
        "alpha": alpha,
        "key": "digest-key",
        "prior_state": prior_state,
        "prior_report_refs": [] if refs is None else refs,
    }


def e_plan(spec: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    return confirmatory(
        sequential_rule="e_process_accumulating",
        e_process=spec or e_spec(),
        minimum_effect=0.05,
        **over,
    )


@pytest.mark.parametrize(
    "spec,code",
    [
        (None, "SEQUENTIAL_RULE"),
        ("x", "SEQUENTIAL_RULE"),
        ({"alpha": 0.05, "key": "k", "prior_state": None}, "SEQUENTIAL_RULE"),
        (e_spec(alpha=0), "SEQUENTIAL_RULE"),
        (e_spec(alpha=1), "SEQUENTIAL_RULE"),
        (e_spec(alpha="0.05"), "SEQUENTIAL_RULE"),
        ({**e_spec(), "key": " "}, "SEQUENTIAL_RULE"),
        ({**e_spec(), "key": 7}, "SEQUENTIAL_RULE"),
        (e_spec(refs="x"), "SEQUENTIAL_RULE"),
        (e_spec(refs=[{"id": "x"}]), "SEQUENTIAL_RULE"),
        # a prior state comes with the reports it came from, and only then
        (e_spec(prior_state=[0.0] * 9), "SEQUENTIAL_RULE"),
        (e_spec(prior_state=None, refs=[PRIOR_REF]), "SEQUENTIAL_RULE"),
        (e_spec(prior_state=[0.0] * 8, refs=[PRIOR_REF]), "SEQUENTIAL_RULE"),
        (e_spec(prior_state=[0.0] * 8 + [math.nan], refs=[PRIOR_REF]), "SEQUENTIAL_RULE"),
        (e_spec(prior_state="x", refs=[PRIOR_REF]), "SEQUENTIAL_RULE"),
    ],
)
def test_e_process_spec_is_validated(spec: Any, code: str) -> None:
    p = e_plan()
    p["e_process"] = spec
    fault(code, validate_analysis_plan, p)


def test_e_process_spec_forbidden_without_the_rule() -> None:
    fault(
        "SEQUENTIAL_RULE",
        validate_analysis_plan,
        confirmatory(e_process=e_spec()),
    )
    fault("SEQUENTIAL_RULE", validate_analysis_plan, plan(e_process=e_spec()))


def test_valid_e_process_with_prior_state_validates() -> None:
    prior = list(e_process_update(None, 3, 1))
    validate_analysis_plan(e_plan(e_spec(prior_state=prior, refs=[PRIOR_REF])))


def test_exploratory_plan_has_no_e_process_block() -> None:
    n = 40
    result = run(outcome_trials(tasks(n), wins=20, both_pass=20), plan(minimum_effect=0.05), n)
    assert "e_process" not in result and "basis" not in result


def test_e_process_block_records_the_running_state() -> None:
    n = 40
    prior = list(e_process_update(None, 2, 1))
    p = e_plan(e_spec(prior_state=prior, refs=[PRIOR_REF]))
    result = run(outcome_trials(tasks(n), wins=5, losses=1, both_pass=34), p, n)
    block = result["e_process"]
    expected = e_process_update(tuple(prior), 5, 1)
    assert block["wins"] == 5 and block["losses"] == 1
    assert block["state"] == pytest.approx(list(expected))
    assert block["e_value"] == pytest.approx(e_value(expected))
    assert block["key"] == "digest-key" and block["alpha"] == 0.05
    assert block["prior_state"] == prior and block["prior_report_refs"] == [PRIOR_REF]
    assert block["crossed"] is (e_value(expected) >= 20)
    assert "limitation" in block and "direction" in block["limitation"]


def test_state_chains_across_nights() -> None:
    n = 40
    night1 = run(outcome_trials(tasks(n), wins=3, losses=0, both_pass=37), e_plan(), n)
    p2 = e_plan(e_spec(prior_state=night1["e_process"]["state"], refs=[PRIOR_REF]))
    night2 = run(outcome_trials(tasks(n), wins=2, losses=1, both_pass=37), p2, n)
    joint = e_process_update(None, 5, 1)
    assert night2["e_process"]["state"] == pytest.approx(list(joint))


def test_crossed_e_process_can_stand_in_for_the_interval_when_non_inferior() -> None:
    """9 wins of 40 is not better by the interval (low <= 0.05) but crosses the e-process."""
    n = 40
    trials = outcome_trials(tasks(n), wins=9, both_pass=31)
    fixed = run(trials, plan(minimum_effect=0.05), n)
    assert fixed["decision_class"] == "non_inferior"
    result = run(trials, e_plan(), n)
    assert result["e_process"]["crossed"] is True
    assert result["decision_class"] == "improvement"
    assert result["basis"] == "e_process"
    assert result["verdict"] == "pass"


def test_uncrossed_e_process_changes_nothing() -> None:
    n = 40
    trials = outcome_trials(tasks(n), wins=1, both_pass=39)
    result = run(trials, e_plan(), n)
    assert result["e_process"]["crossed"] is False
    assert result["decision_class"] == "non_inferior"
    assert "basis" not in result


def test_crossed_e_process_needs_a_non_inferior_interval() -> None:
    """Many wins but many more losses: the e-process is not crossed and the interval says worse."""
    n = 40
    result = run(
        outcome_trials(tasks(n), wins=5, losses=25, both_pass=10),
        e_plan(noninferiority_margin=0.15),
        n,
    )
    assert result["e_process"]["crossed"] is False
    assert result["decision_class"] == "regression"


def test_e_process_crossed_but_interval_not_non_inferior_is_not_better() -> None:
    n = 20
    prior = list(e_process_update(None, 40, 0))  # huge prior evidence
    p = e_plan(e_spec(prior_state=prior, refs=[PRIOR_REF]), minimum_tasks=2)
    result = run(outcome_trials(tasks(n), wins=0, losses=10, both_pass=10), p, n)
    assert result["e_process"]["crossed"] is True
    assert result["decision_class"] != "improvement"
    assert "basis" not in result


def test_e_process_value_none_when_infinite() -> None:
    n = 40
    prior = list(e_process_update(None, 5000, 0))
    result = run(
        outcome_trials(tasks(n), wins=10, both_pass=30),
        e_plan(e_spec(prior_state=prior, refs=[PRIOR_REF])),
        n,
    )
    assert result["e_process"]["e_value"] is None
    assert result["e_process"]["crossed"] is True


# --- shared input checks (unchanged behaviour on versioned plans) ---------------------------


def test_input_faults_apply_to_versioned_plans() -> None:
    n = 4
    good = outcome_trials(tasks(n), both_pass=n)
    fault("UNPLANNED_TRIAL", run, [*good, trial("zzz", "baseline", True)], plan(), n)
    fault("UNPLANNED_TRIAL", run, [*good, trial("t000", "baseline", True, repeat=1)], plan(), n)
    fault("DUPLICATE_TRIAL", run, [*good, trial("t000", "baseline", True)], plan(), n)
    fault("TRIAL_ARM", run, [trial("t000", "control", True)], plan(), n)
    fault("TRIAL_TYPE", run, [trial("t000", "baseline", 1)], plan(), n)
    fault("TRIAL_COUNTER", run, [trial("t000", "baseline", True, safety_failures=-1)], plan(), n)
    fault("TRIAL_USAGE", run, [trial("t000", "baseline", True, elapsed_ms=math.nan)], plan(), n)
    fault("SAMPLE_IDS", analyze_pairs, good, plan(), expected_tasks=["a", "a"])


def test_analyze_pairs_validates_the_plan_first() -> None:
    fault("ANALYSIS_PLAN", run, [], legacy(minimum_effect=0.1), 3)
    fault("ANALYSIS_METHOD", run, [], plan(endpoint="superiority"), 3)


def test_result_carries_the_version_ref_and_plan_fields() -> None:
    n = 40
    result = run(
        outcome_trials(tasks(n), both_pass=n),
        plan(minimum_effect=0.1, noise_band=0.05, mde=0.4, purpose="exploratory"),
        n,
    )
    assert result["evaluator_version_ref"] == EV_REF
    assert result["endpoint"] == "noninferiority"
    assert (result["minimum_effect"], result["noise_band"], result["mde"]) == (0.1, 0.05, 0.4)
    assert result["purpose"] == "exploratory"
    assert result["safety_failures"] == 0


# --- legacy plans stay legacy (spot checks; the full oracle is in the golden test) ----------


def test_legacy_result_has_no_new_fields() -> None:
    n = 40
    result = run(outcome_trials(tasks(n), wins=16, both_pass=24), legacy(), n)
    for key in ("decision_class", "vs_reference", "e_process", "pass_k", "safety_failures_by_arm"):
        assert key not in result
    assert result["verdict"] == "pass"


def test_legacy_plan_ignores_none_valued_evaluator_fields() -> None:
    p = legacy()
    p.update(endpoint=None, minimum_effect=None, noise_band=None, mde=None, reference_arm=None)
    n = 40
    assert run(outcome_trials(tasks(n), both_pass=n), p, n)["verdict"] == "pass"


def test_legacy_verdict_at_the_boundary_is_pass() -> None:
    """low == -margin passes in the legacy rule too (`low >= -margin`)."""
    n = 16
    result = run(outcome_trials(tasks(n), both_pass=n), legacy(), n)
    assert result["verdict"] == "pass"
    assert result["confidence_interval"][0] >= -0.25
    n = 15
    assert run(outcome_trials(tasks(n), both_pass=n), legacy(), n)["verdict"] == "inconclusive"


def test_module_exports_are_the_spec_names() -> None:
    for name in (
        "validate_analysis_plan",
        "analyze_pairs",
        "analyze_benefit",
        "decision_class",
        "per_task_rates",
    ):
        assert callable(getattr(analysis, name))
