"""Work 033 S1: pure statistics of `evaluation/sequential.py` (interfaces.md 3.8, 7.5, 7.6).

Covers Q-12 (e-process), Q-08 confirmatory part (minimum tasks for a margin, IC-09) and the
noise band / MDE formulas of 7.5. Everything here is deterministic (seeded) and pure.
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist

import pytest

from amplai_foundry.evaluation import sequential as seq
from amplai_foundry.evaluation.sequential import (
    ALTERNATIVES,
    e_process_update,
    e_value,
    mde,
    min_tasks_for_margin,
    min_wins_for_superiority,
    noise_band,
    wilson,
)

Z99 = NormalDist().inv_cdf(0.995)


def test_alternatives_match_the_spec() -> None:
    assert ALTERNATIVES == (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)


# --- Q-12: e-process -----------------------------------------------------------------------


def test_empty_process_has_e_value_one() -> None:
    state = e_process_update(None, 0, 0)
    assert state == (0.0,) * 9
    assert e_value(state) == pytest.approx(1.0)


@pytest.mark.parametrize("q1", ALTERNATIVES)
def test_single_factor_has_expectation_one_at_half(q1: float) -> None:
    """E[factor] = 1/2 * q1/0.5 + 1/2 * (1-q1)/0.5 = 1 for every alternative (Q-12)."""
    index = ALTERNATIVES.index(q1)
    win = math.exp(e_process_update(None, 1, 0)[index])
    loss = math.exp(e_process_update(None, 0, 1)[index])
    assert win == pytest.approx(q1 / 0.5)
    assert loss == pytest.approx((1 - q1) / 0.5)
    assert 0.5 * win + 0.5 * loss == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize("m", [1, 2, 7, 25])
def test_batch_expectation_is_one_for_every_alternative_and_the_mixture(m: int) -> None:
    """Exact binomial expectation of each product and of the mixture at q = 1/2."""
    per_alt = [0.0] * len(ALTERNATIVES)
    mixture = 0.0
    for wins in range(m + 1):
        weight = math.comb(m, wins) / 2**m
        state = e_process_update(None, wins, m - wins)
        mixture += weight * e_value(state)
        for j, s in enumerate(state):
            per_alt[j] += weight * math.exp(s)
    for total in per_alt:
        assert total == pytest.approx(1.0, abs=1e-9)
    assert mixture == pytest.approx(1.0, abs=1e-9)


def test_supermartingale_below_half_stays_at_most_one() -> None:
    """Conditional q < 1/2: the factor expectation 2q(2q1-1)+2-2q1 is <= 1 for every q1."""
    for q in (0.0, 0.1, 0.3, 0.49):
        for q1 in ALTERNATIVES:
            assert 2 * q * (2 * q1 - 1) + 2 - 2 * q1 <= 1 + 1e-12


def test_update_is_order_free_and_chains() -> None:
    once = e_process_update(None, 7, 3)
    chained = e_process_update(e_process_update(e_process_update(None, 3, 1), 4, 0), 0, 2)
    assert chained == pytest.approx(once)
    assert e_process_update(once, 0, 0) == once


def test_wins_raise_and_losses_lower_the_evidence() -> None:
    assert e_value(e_process_update(None, 10, 0)) > 1
    assert e_value(e_process_update(None, 0, 10)) < 1


def test_strong_signal_crosses_one_over_alpha() -> None:
    assert e_value(e_process_update(None, 12, 0)) >= 1 / 0.05


def test_e_value_overflow_is_inf_not_an_exception() -> None:
    assert e_value(e_process_update(None, 5000, 0)) == math.inf


def test_a_hundred_thousand_losses_do_not_raise() -> None:
    assert 0.0 <= e_value(e_process_update(None, 0, 100_000)) < 1e-100


@pytest.mark.parametrize(
    "wins,losses",
    [(-1, 0), (0, -1), (1.0, 0), (0, 2.5), (True, 0), ("1", 0), (None, 0)],
)
def test_update_refuses_bad_counts(wins: object, losses: object) -> None:
    with pytest.raises(ValueError):
        e_process_update(None, wins, losses)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "state",
    [(0.0,) * 8, (0.0,) * 10, (), (0.0,) * 8 + (math.nan,), (0.0,) * 8 + (math.inf,), ("0",) * 9],
)
def test_update_and_value_refuse_a_malformed_state(state: tuple) -> None:
    with pytest.raises(ValueError):
        e_process_update(state, 0, 0)
    with pytest.raises(ValueError):
        e_value(state)


def test_simulated_aa_sequences_cross_at_most_alpha() -> None:
    """Q-12: 500 A/A sequences of 50 batches; peeking after every batch. The crossing count
    stays inside the binomial 99 % upper bound around alpha * 500."""
    alpha, runs, batches = 0.05, 500, 50
    rng = random.Random(0x03312)
    crossed = 0
    for _ in range(runs):
        state: tuple[float, ...] | None = None
        hit = False
        for _ in range(batches):
            discordant = rng.randint(1, 8)
            wins = sum(rng.random() < 0.5 for _ in range(discordant))
            state = e_process_update(state, wins, discordant - wins)
            if e_value(state) >= 1 / alpha:
                hit = True
                break
        crossed += hit
    bound = runs * alpha + Z99 * math.sqrt(runs * alpha * (1 - alpha))
    assert crossed <= bound


def test_simulated_true_effect_sequences_cross_often() -> None:
    """Sanity of power: a real 80 % win rate crosses in nearly every sequence."""
    rng = random.Random(0x03313)
    crossed = 0
    for _ in range(100):
        state: tuple[float, ...] | None = None
        for _ in range(20):
            wins = sum(rng.random() < 0.8 for _ in range(5))
            state = e_process_update(state, wins, 5 - wins)
        assert state is not None
        crossed += e_value(state) >= 20
    assert crossed >= 95


# --- 7.5: noise band and MDE ---------------------------------------------------------------


def test_noise_band_formula_and_defaults() -> None:
    expected = NormalDist().inv_cdf(0.975) * math.sqrt(0.2 / 40)
    assert noise_band(0.2, 40) == pytest.approx(expected)
    assert noise_band(0.2, 40, confidence=0.9) == pytest.approx(
        NormalDist().inv_cdf(0.95) * math.sqrt(0.2 / 40)
    )
    assert noise_band(0.0, 10) == 0.0
    assert noise_band(0.5, 100) < noise_band(0.5, 25)


def test_mde_formula_and_defaults() -> None:
    dist = NormalDist()
    expected = (dist.inv_cdf(0.975) + dist.inv_cdf(0.8)) * math.sqrt(0.5 / 20)
    assert mde(20, 0.5) == pytest.approx(expected)
    assert mde(20, 0.5, alpha=0.1, power=0.9) == pytest.approx(
        (dist.inv_cdf(0.95) + dist.inv_cdf(0.9)) * math.sqrt(0.5 / 20)
    )
    assert mde(80, 0.5) == pytest.approx(mde(20, 0.5) / 2)


@pytest.mark.parametrize("d", [-0.1, 1.1, math.nan, math.inf, "0.2", None])
def test_noise_band_and_mde_refuse_bad_discordance(d: object) -> None:
    with pytest.raises(ValueError):
        noise_band(d, 10)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        mde(10, d)  # type: ignore[arg-type]


@pytest.mark.parametrize("n", [0, -3, 2.5, True, "5"])
def test_noise_band_and_mde_refuse_bad_n(n: object) -> None:
    with pytest.raises(ValueError):
        noise_band(0.2, n)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        mde(n, 0.2)  # type: ignore[arg-type]


@pytest.mark.parametrize("c", [0.0, 1.0, -0.5, 1.5, math.nan])
def test_noise_band_refuses_bad_confidence(c: float) -> None:
    with pytest.raises(ValueError):
        noise_band(0.2, 10, confidence=c)


@pytest.mark.parametrize("kwargs", [{"alpha": 0.0}, {"alpha": 1.0}, {"power": 0.0}, {"power": 1.0}])
def test_mde_refuses_bad_alpha_or_power(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        mde(10, 0.2, **kwargs)


# --- Q-08 (confirmatory part) and IC-09: minimum tasks -------------------------------------


@pytest.mark.parametrize(
    "margin,confidence,expected",
    [(0.25, 0.95, 16), (0.2, 0.95, 21), (0.15, 0.95, 29), (0.1, 0.95, 46)],
)
def test_min_tasks_for_margin_matches_ic09(margin: float, confidence: float, expected: int) -> None:
    assert min_tasks_for_margin(margin, confidence) == expected


def test_min_tasks_is_exactly_the_concordant_sample_boundary() -> None:
    """15 tasks cannot pass at m = 0.25, c = 0.95; 16 can (Q-08): a fully concordant sample has
    lower bound -z^2/(n+z^2) computed with the interval the analysis uses."""
    z = seq.paired_z(0.95)
    for n, passes in ((15, False), (16, True)):
        pl, pu = wilson(0, n, z)
        low = pl - pu  # wins = 0, losses = 0
        assert (low >= -0.25) is passes
        assert low == pytest.approx(-z * z / (n + z * z))


def test_min_tasks_never_below_one() -> None:
    assert min_tasks_for_margin(1.0, 0.95) == 1


@pytest.mark.parametrize("margin", [0.0, -0.1, 1.5, math.nan, "0.25"])
def test_min_tasks_refuses_bad_margin(margin: object) -> None:
    with pytest.raises(ValueError):
        min_tasks_for_margin(margin, 0.95)  # type: ignore[arg-type]


@pytest.mark.parametrize("confidence", [0.0, 1.0, 1.2, math.nan])
def test_min_tasks_refuses_bad_confidence(confidence: float) -> None:
    with pytest.raises(ValueError):
        min_tasks_for_margin(0.25, confidence)


def test_min_wins_for_superiority_n20_needs_ten_wins_and_no_losses() -> None:
    assert min_wins_for_superiority(20, 0.05, 0.95) == 10


def test_min_wins_for_superiority_n40_needs_at_most_twelve() -> None:
    wins = min_wins_for_superiority(40, 0.05, 0.95)
    assert wins is not None and wins <= 12


def test_min_wins_is_the_smallest_count_that_clears_the_interval() -> None:
    z = seq.paired_z(0.95)
    for n in (20, 40):
        wins = min_wins_for_superiority(n, 0.05, 0.95)
        assert wins is not None
        _, loss_high = wilson(0, n, z)
        assert wilson(wins, n, z)[0] - loss_high > 0.05
        assert wilson(wins - 1, n, z)[0] - loss_high <= 0.05


def test_min_wins_is_none_when_the_sample_is_too_small() -> None:
    assert min_wins_for_superiority(3, 0.5, 0.95) is None
    assert min_wins_for_superiority(5, 0.9, 0.95) is None


@pytest.mark.parametrize("n", [0, -1, 1.5, True])
def test_min_wins_refuses_bad_n(n: object) -> None:
    with pytest.raises(ValueError):
        min_wins_for_superiority(n, 0.05, 0.95)  # type: ignore[arg-type]


@pytest.mark.parametrize("effect", [0.0, -0.1, 2.0, math.nan])
def test_min_wins_refuses_bad_effect(effect: float) -> None:
    with pytest.raises(ValueError):
        min_wins_for_superiority(20, effect, 0.95)


def test_wilson_matches_the_known_interval() -> None:
    low, high = wilson(3, 10, 1.96)
    assert low == pytest.approx(0.1078, abs=1e-3)
    assert high == pytest.approx(0.6032, abs=1e-3)
    assert wilson(0, 5, 1.96)[0] == 0
    assert wilson(5, 5, 1.96)[1] == 1
