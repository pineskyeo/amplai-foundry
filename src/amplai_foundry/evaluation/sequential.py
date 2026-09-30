"""Pure statistics for the Work 033 evaluator (D-099, interfaces.md §3.8, §7.5, §7.6).

Standard library only (no numpy/scipy dependency, interfaces.md Conventions). Nothing here
reads the store; `analysis.py` and the stage runner call these functions with frozen inputs.

- `wilson`: the Wilson score interval of the existing conservative method, shared with
  `analysis.py` so the power rules below use the exact interval the analysis uses.
- e-process (§7.6): a fixed-weight mixture of likelihood ratios over the discordant paired tasks
  of the primary comparison. Each factor has expectation `2q(2q1-1) + 2 - 2q1 <= 1` when the
  conditional win probability q <= 1/2 and q1 > 1/2, so the mixture is a nonnegative
  supermartingale under H0 and Ville's inequality bounds P(sup e >= 1/alpha) by alpha.
- noise band, MDE and minimum tasks (§7.5, IC-09).
"""

from __future__ import annotations

import math
from statistics import NormalDist

# §3.8: the alternatives q1 of the mixture; the state holds one running log-product per entry.
ALTERNATIVES: tuple[float, ...] = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)

_LOG_WIN = tuple(math.log(q1 / 0.5) for q1 in ALTERNATIVES)
_LOG_LOSS = tuple(math.log((1 - q1) / 0.5) for q1 in ALTERNATIVES)


def wilson(successes: int, n: int, z: float) -> tuple[float, float]:
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0, center - radius), min(1, center + radius)


def paired_z(confidence: float) -> float:
    """Bonferroni-adjusted z of the paired method: alpha/2 per two-sided interval (IC-09)."""
    return NormalDist().inv_cdf(1 - (1 - confidence) / 4)


def _count(value: int, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _unit(value: float, name: str, *, low_open: bool = True, high_open: bool = True) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    below = value <= 0 if low_open else value < 0
    above = value >= 1 if high_open else value > 1
    if below or above:
        raise ValueError(f"{name} is outside its range")
    return float(value)


def e_process_update(state: tuple[float, ...] | None, wins: int, losses: int) -> tuple[float, ...]:
    """Add `wins` and `losses` (discordant paired tasks) to the running log-products (§7.6).

    `None` is the empty process (every log-product 0, e-value 1). The update is order-free:
    only the counts enter, so a night's batch gives the same state as its pairs one by one.
    """
    _count(wins, "wins")
    _count(losses, "losses")
    if state is None:
        state = tuple(0.0 for _ in ALTERNATIVES)
    state = tuple(state)
    if len(state) != len(ALTERNATIVES) or any(
        type(s) not in (int, float) or not math.isfinite(s) for s in state
    ):
        raise ValueError("e-process state holds one finite log-product per alternative")
    return tuple(
        float(s) + wins * lw + losses * ll
        for s, lw, ll in zip(state, _LOG_WIN, _LOG_LOSS, strict=True)
    )


def e_value(state: tuple[float, ...]) -> float:
    """`mean_j exp(state_j)` (§7.6); `inf` when it exceeds the float range."""
    state = tuple(state)
    if len(state) != len(ALTERNATIVES) or any(
        type(s) not in (int, float) or not math.isfinite(s) for s in state
    ):
        raise ValueError("e-process state holds one finite log-product per alternative")
    top = max(state)
    scaled = sum(math.exp(s - top) for s in state) / len(state)
    try:
        return math.exp(top) * scaled
    except OverflowError:
        return math.inf


def noise_band(discordance: float, n: int, *, confidence: float = 0.95) -> float:
    """`z_{(1+c)/2} * sqrt(d / n)`: normal approximation of the paired difference under no
    effect, with `d` the A/A discordance of the cell (§7.5)."""
    d = _unit(discordance, "discordance", low_open=False, high_open=False)
    c = _unit(confidence, "confidence")
    if type(n) is not int or n < 1:
        raise ValueError("n must be a positive integer")
    return NormalDist().inv_cdf((1 + c) / 2) * math.sqrt(d / n)


def mde(n: int, discordance: float, *, alpha: float = 0.05, power: float = 0.8) -> float:
    """`(z_{1-alpha/2} + z_power) * sqrt(d / n)` (§7.5). Without calibration data the caller
    passes d = 0.5 and says so in the rationale."""
    d = _unit(discordance, "discordance", low_open=False, high_open=False)
    a = _unit(alpha, "alpha")
    p = _unit(power, "power")
    if type(n) is not int or n < 1:
        raise ValueError("n must be a positive integer")
    dist = NormalDist()
    return (dist.inv_cdf(1 - a / 2) + dist.inv_cdf(p)) * math.sqrt(d / n)


def min_tasks_for_margin(margin: float, confidence: float) -> int:
    """Smallest n whose fully concordant sample passes non-inferiority (IC-09).

    A concordant sample has lower bound `-z^2/(n+z^2)`, which is `>= -m` iff
    `n >= z^2 (1-m) / m`. m = 0.25, c = 0.95 gives 16.
    """
    m = _unit(margin, "margin", high_open=False)
    c = _unit(confidence, "confidence")
    z = paired_z(c)
    return max(1, math.ceil(z * z * (1 - m) / m))


def min_wins_for_superiority(n: int, minimum_effect: float, confidence: float) -> int | None:
    """Smallest number of wins with 0 losses of n paired tasks whose interval lower bound
    exceeds `minimum_effect` (the `better` rule of §7.2); `None` when no count does (IC-09).
    """
    if type(n) is not int or n < 1:
        raise ValueError("n must be a positive integer")
    effect = _unit(minimum_effect, "minimum_effect", high_open=False)
    z = paired_z(_unit(confidence, "confidence"))
    _, loss_high = wilson(0, n, z)
    for wins in range(n + 1):
        win_low, _ = wilson(wins, n, z)
        if win_low - loss_high > effect:
            return wins
    return None
