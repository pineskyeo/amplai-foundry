"""Work 033 S12: the additive surrogate, Plackett-Burman 12 and successive halving.

interfaces.md §3.13 (`meta_harness/surrogate.py`), §8.7. Pure standard library: no store, no
driver, no network. The surrogate ranks candidates for the search phase; it decides nothing.
"""

from __future__ import annotations

import math

import pytest

from amplai_foundry.meta_harness.surrogate import (
    AdditiveSurrogate,
    fold_over,
    logit,
    plackett_burman_12,
    sigmoid,
    successive_halving,
)
from amplai_foundry.runtime.errors import RuntimeFault


def row(task: str, success: object, **options: str) -> dict:
    return {"task_id": task, "options": dict(options), "success": success}


def corpus_rows() -> list[dict]:
    """Option `strong` of slot `strategy` succeeds on every task, `weak` fails on most."""
    rows = []
    for task in ("t1", "t2", "t3", "t4"):
        for _ in range(6):
            rows.append(row(task, True, strategy="strong", context="plain"))
        for i in range(6):
            rows.append(row(task, i == 0, strategy="weak", context="plain"))
    return rows


# --- the additive model ------------------------------------------------------------------------


def test_the_task_intercept_is_the_calibration_logit_of_the_observed_runs() -> None:
    model = AdditiveSurrogate.fit([row("t1", True, a="x")] * 3 + [row("t1", False, a="x")])
    assert model.intercepts["t1"] == pytest.approx(logit((3 + 0.5) / (4 + 1)))


def test_the_model_ranks_the_better_option_first_and_predicts_a_probability() -> None:
    model = AdditiveSurrogate.fit(corpus_rows())
    strong = model.predict("t1", {"strategy": "strong", "context": "plain"})
    weak = model.predict("t1", {"strategy": "weak", "context": "plain"})
    assert 0 < weak < strong < 1
    ranked = model.rank(
        [{"strategy": "weak"}, {"strategy": "strong"}, {"strategy": "weak"}], ["t1", "t2"]
    )
    assert next(i for i, _s in ranked) == 1
    assert [s for _i, s in ranked] == sorted((s for _i, s in ranked), reverse=True)


def test_ties_keep_the_given_candidate_order() -> None:
    model = AdditiveSurrogate.fit(corpus_rows())
    ranked = model.rank([{"strategy": "weak"}, {"strategy": "weak"}, {"strategy": "weak"}], ["t1"])
    assert [i for i, _s in ranked] == [0, 1, 2]


def test_an_unseen_option_adds_nothing_and_an_unseen_task_takes_the_pooled_intercept() -> None:
    model = AdditiveSurrogate.fit(corpus_rows())
    assert model.predict("t1", {"strategy": "unheard-of"}) == pytest.approx(model.predict("t1", {}))
    pooled = model.predict("never-seen", {})
    assert pooled == pytest.approx(sigmoid(model.default_intercept))
    assert 0 < pooled < 1


def test_rank_without_tasks_uses_the_pooled_intercept_and_still_orders_by_option() -> None:
    model = AdditiveSurrogate.fit(corpus_rows())
    ranked = model.rank([{"strategy": "weak"}, {"strategy": "strong"}], [])
    assert [i for i, _s in ranked] == [1, 0]


def test_there_are_main_effects_only_no_pairwise_terms() -> None:
    model = AdditiveSurrogate.fit(corpus_rows())
    assert all(isinstance(key, tuple) and len(key) == 2 for key in model.effects)
    assert set(model.effects) == {
        ("strategy", "strong"), ("strategy", "weak"), ("context", "plain"),
    }  # fmt: skip


def test_fitting_is_deterministic() -> None:
    first, second = AdditiveSurrogate.fit(corpus_rows()), AdditiveSurrogate.fit(corpus_rows())
    assert first.effects == second.effects and first.intercepts == second.intercepts


def test_l2_shrinks_the_effects_toward_zero() -> None:
    loose = AdditiveSurrogate.fit(corpus_rows(), l2=0.0)
    tight = AdditiveSurrogate.fit(corpus_rows(), l2=50.0)
    key = ("strategy", "strong")
    assert abs(tight.effects[key]) < abs(loose.effects[key])
    assert loose.effects[key] > 0 > loose.effects[("strategy", "weak")]


def test_zero_iterations_leave_every_effect_at_zero() -> None:
    model = AdditiveSurrogate.fit(corpus_rows(), iterations=0)
    assert set(model.effects.values()) == {0.0}


def test_unknown_outcomes_are_ignored_not_counted_as_failures() -> None:
    rows = corpus_rows()
    noisy = [*rows, *[row("t1", None, strategy="strong") for _ in range(30)],
             row("t1", "yes", strategy="strong")]  # fmt: skip
    assert AdditiveSurrogate.fit(noisy).rows == AdditiveSurrogate.fit(rows).rows
    assert AdditiveSurrogate.fit(noisy).intercepts == AdditiveSurrogate.fit(rows).intercepts


def test_no_rows_give_a_model_that_still_predicts_one_half() -> None:
    model = AdditiveSurrogate.fit([])
    assert model.rows == 0
    assert model.predict("t", {"a": "b"}) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "rows",
    [
        "not-a-list",
        ["not-a-row"],
        [{"task_id": "", "options": {}, "success": True}],
        [{"task_id": "t", "options": "x", "success": True}],
        [{"task_id": "t", "options": {"a": 1}, "success": True}],
        [{"options": {}, "success": True}],
    ],
)
def test_malformed_rows_are_a_rejected_fault(rows: object) -> None:
    with pytest.raises(RuntimeFault) as fault:
        AdditiveSurrogate.fit(rows)  # type: ignore[arg-type]
    assert fault.value.code == "SURROGATE_ROWS" and fault.value.outcome == "rejected"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"iterations": -1},
        {"iterations": 1.5},
        {"l2": -1.0},
        {"l2": float("nan")},
        {"l2": float("inf")},
    ],
)
def test_bad_fit_parameters_are_a_rejected_fault(kwargs: dict) -> None:
    with pytest.raises(RuntimeFault) as fault:
        AdditiveSurrogate.fit(corpus_rows(), **kwargs)
    assert fault.value.code == "SURROGATE_FIT"


def test_the_surrogate_exposes_no_verdict_or_promotion_surface() -> None:
    names = {n for n in dir(AdditiveSurrogate) if not n.startswith("_")}
    assert not names & {"verdict", "decide", "promote", "approve", "select"}


# --- Plackett-Burman 12 and the fold-over ------------------------------------------------------


def test_pb12_is_12_runs_by_11_factors_of_plus_minus_one() -> None:
    design = plackett_burman_12()
    assert len(design) == 12 and all(len(r) == 11 for r in design)
    assert {x for r in design for x in r} == {-1, 1}


def test_pb12_columns_are_balanced_and_every_pair_is_orthogonal() -> None:
    design = plackett_burman_12()
    columns = list(zip(*design, strict=True))
    assert all(sum(c) == 0 for c in columns)
    for i in range(11):
        for j in range(i + 1, 11):
            assert sum(a * b for a, b in zip(columns[i], columns[j], strict=True)) == 0


def test_pb12_rows_are_distinct_and_the_call_returns_fresh_lists() -> None:
    design = plackett_burman_12()
    assert len({tuple(r) for r in design}) == 12
    design[0][0] = 99
    assert plackett_burman_12()[0][0] in (-1, 1)


def test_the_fold_over_appends_the_sign_reversed_mirror() -> None:
    design = plackett_burman_12()
    folded = fold_over(design)
    assert len(folded) == 24
    assert folded[:12] == design
    assert folded[12:] == [[-x for x in r] for r in design]
    # a folded design keeps every column balanced and every pair orthogonal
    columns = list(zip(*folded, strict=True))
    assert all(sum(c) == 0 for c in columns)
    assert sum(a * b for a, b in zip(columns[0], columns[1], strict=True)) == 0


def test_the_fold_over_does_not_mutate_its_input() -> None:
    design = plackett_burman_12()
    before = [list(r) for r in design]
    fold_over(design)
    assert design == before


@pytest.mark.parametrize("bad", ["x", [[1, 0]], [[1, 2, -1]], [1, -1], [["a"]]])
def test_fold_over_refuses_a_non_design(bad: object) -> None:
    with pytest.raises(RuntimeFault) as fault:
        fold_over(bad)  # type: ignore[arg-type]
    assert fault.value.code == "SCREENING_DESIGN"


# --- successive halving ------------------------------------------------------------------------


def test_halving_plans_4_8_16_tasks_keeping_half_in_the_given_order() -> None:
    names = [f"c{i}" for i in range(8)]
    plan = successive_halving(names)
    assert [(p["rung"], p["tasks"], p["count"]) for p in plan] == [(0, 4, 8), (1, 8, 4), (2, 16, 2)]
    assert plan[0]["candidates"] == names
    assert plan[1]["candidates"] == names[:4]
    assert plan[2]["candidates"] == names[:2]


def test_halving_stops_at_a_single_candidate() -> None:
    plan = successive_halving(["a", "b"])
    assert [(p["tasks"], p["count"]) for p in plan] == [(4, 2), (8, 1)]
    assert [(p["tasks"], p["count"]) for p in successive_halving(["a"])] == [(4, 1)]


def test_halving_of_three_floors_to_one_but_never_below_one() -> None:
    plan = successive_halving(["a", "b", "c"])
    assert [p["count"] for p in plan] == [3, 1]


def test_halving_of_nothing_plans_nothing() -> None:
    assert successive_halving([]) == []


def test_halving_accepts_other_rungs_and_keep() -> None:
    plan = successive_halving(list("abcdefgh"), rungs=(2, 6), keep=0.25)
    assert [(p["tasks"], p["count"]) for p in plan] == [(2, 8), (6, 2)]
    assert math.isclose(sum(p["count"] for p in plan), 10)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"candidates": ["a", "a"]},
        {"candidates": "abc"},
        {"candidates": ["a"], "rungs": ()},
        {"candidates": ["a"], "rungs": (8, 4)},
        {"candidates": ["a"], "rungs": (0, 4)},
        {"candidates": ["a"], "rungs": (4.0,)},
        {"candidates": ["a"], "keep": 1.0},
        {"candidates": ["a"], "keep": 0},
    ],
)
def test_a_bad_halving_plan_is_a_rejected_fault(kwargs: dict) -> None:
    with pytest.raises(RuntimeFault) as fault:
        successive_halving(**kwargs)
    assert fault.value.code == "SEARCH_PLAN" and fault.value.outcome == "rejected"
