"""The nightly search surrogate and screening designs (Work 033 S12, interfaces.md §3.13, §8.7).

Deliberately simple and pure standard library (no numpy/scipy, interfaces.md Conventions):

- ``AdditiveSurrogate`` v1:
  ``logit P(success | task t, config c) = a_t + sum_slot b_{slot, option(c)}``.
  The task intercept is fixed from the task's observed runs, ``a_t = logit((s + 0.5)/(n + 1))``
  (calibration rows included); only main effects (no pairwise terms); the b are an L2-penalized
  logistic regression fitted by plain gradient descent. It **ranks** candidate configurations
  for the search phase; it never decides a verdict, a stage or a promotion.
- ``plackett_burman_12``: the 12-run Plackett-Burman design for 11 two-level factors (+1/-1), and
  ``fold_over``, its mirror (24 runs) that separates main effects from two-factor interactions.
- ``successive_halving``: the rung plan (4 → 8 → 16 development tasks, keep half) the search phase
  spends its share with; the candidates kept at each rung are chosen from measured results by the
  caller (``nightly.NightlyRunner``), ties by the given (surrogate) order.

A row is ``{"task_id": str, "options": {slot: option}, "success": bool}``; a row whose success is
not a boolean (an unknown outcome) is ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..runtime.errors import RuntimeFault

# The first row of the 12-run Plackett-Burman design (Plackett & Burman 1946); the next ten rows
# are its cyclic shifts and the last row is all minus.
PB12_GENERATOR = (1, 1, -1, 1, 1, 1, -1, -1, -1, 1, -1)
LEARNING_RATE = 0.5
RUNGS = (4, 8, 16)  # §8.7: development tasks per rung


def logit(p: float) -> float:
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def _rows(rows: Any) -> list[tuple[str, dict[str, str], int]]:
    if not isinstance(rows, list):
        raise RuntimeFault("SURROGATE_ROWS", "Rows are a list")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeFault("SURROGATE_ROWS", "A row is an object")
        task, options, success = row.get("task_id"), row.get("options"), row.get("success")
        if not isinstance(task, str) or not task or not isinstance(options, dict):
            raise RuntimeFault("SURROGATE_ROWS", "A row has task_id and options")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in options.items()):
            raise RuntimeFault("SURROGATE_ROWS", "Options map slot names to option names")
        if not isinstance(success, bool):
            continue  # unknown outcomes say nothing about the configuration
        out.append((task, dict(options), int(success)))
    return out


@dataclass(frozen=True)
class AdditiveSurrogate:
    intercepts: dict[str, float]
    default_intercept: float
    effects: dict[tuple[str, str], float] = field(default_factory=dict)
    rows: int = 0

    @classmethod
    def fit(
        cls, rows: list[dict[str, Any]], *, l2: float = 1.0, iterations: int = 500
    ) -> AdditiveSurrogate:
        """Fixed task intercepts, then β by gradient descent on the mean log-loss plus
        ``l2 / (2 N) Σ β²``. Deterministic: the same rows give the same model."""
        if type(iterations) is not int or iterations < 0:
            raise RuntimeFault("SURROGATE_FIT", "iterations is a nonnegative integer")
        if type(l2) not in (int, float) or not math.isfinite(l2) or l2 < 0:
            raise RuntimeFault("SURROGATE_FIT", "l2 is a nonnegative number")
        data = _rows(rows)
        counts: dict[str, list[int]] = {}
        for task, _options, y in data:
            counts.setdefault(task, [0, 0])
            counts[task][0] += y
            counts[task][1] += 1
        intercepts = {t: logit((s + 0.5) / (n + 1)) for t, (s, n) in sorted(counts.items())}
        total_s = sum(s for s, _n in counts.values())
        total_n = sum(n for _s, n in counts.values())
        default = logit((total_s + 0.5) / (total_n + 1))
        keys = sorted(
            {(slot, option) for _t, options, _y in data for slot, option in options.items()}
        )
        beta = dict.fromkeys(keys, 0.0)
        n_rows = len(data)
        for _ in range(iterations if n_rows else 0):
            grad = dict.fromkeys(keys, 0.0)
            for task, options, y in data:
                z = intercepts[task] + sum(beta[(s, o)] for s, o in options.items())
                residual = sigmoid(z) - y
                for s, o in options.items():
                    grad[(s, o)] += residual
            for key in keys:
                beta[key] -= LEARNING_RATE * (grad[key] + l2 * beta[key]) / n_rows
        return cls(intercepts, default, beta, n_rows)

    def predict(self, task_id: str, options: dict[str, str]) -> float:
        """P(success); an unseen task takes the pooled intercept, an unseen option adds 0."""
        z = self.intercepts.get(task_id, self.default_intercept)
        z += sum(self.effects.get((slot, option), 0.0) for slot, option in options.items())
        return sigmoid(z)

    def rank(self, candidates: list[dict[str, str]], tasks: list[str]) -> list[tuple[int, float]]:
        """(candidate index, mean predicted success over ``tasks``), best first; ties keep the
        given order. With no task the pooled intercept is used."""
        names = list(tasks) or ["\x00pooled"]
        scored = [
            (i, sum(self.predict(t, c) for t in names) / len(names))
            for i, c in enumerate(candidates)
        ]
        return sorted(scored, key=lambda row: (-row[1], row[0]))


def plackett_burman_12() -> list[list[int]]:
    """12 runs x 11 factors, +1/-1; every column is balanced and every pair orthogonal."""
    g = list(PB12_GENERATOR)
    rows = [g[-k:] + g[:-k] if k else list(g) for k in range(11)]
    rows.append([-1] * 11)
    return rows


def fold_over(design: list[list[int]]) -> list[list[int]]:
    """The design followed by its sign-reversed mirror."""
    if not isinstance(design, list) or not all(
        isinstance(r, list) and all(x in (-1, 1) for x in r) for r in design
    ):
        raise RuntimeFault("SCREENING_DESIGN", "A design is a list of +1/-1 rows")
    return [list(r) for r in design] + [[-x for x in r] for r in design]


def successive_halving(
    candidates: list[str], rungs: tuple[int, ...] = RUNGS, keep: float = 0.5
) -> list[dict[str, Any]]:
    """The rung plan: rung i evaluates ``count_i`` candidates on ``rungs[i]`` tasks, with
    ``count_0`` = all and ``count_{i+1}`` = max(1, floor(count_i x keep)). ``candidates`` is the
    surrogate order; each entry lists the first ``count`` of it as the provisional set (the caller
    re-selects from measured results). A rung with fewer than one candidate is not planned."""
    if not isinstance(candidates, list) or len(set(candidates)) != len(candidates):
        raise RuntimeFault("SEARCH_PLAN", "Candidates are distinct")
    if not rungs or any(type(r) is not int or r < 1 for r in rungs) or list(rungs) != sorted(rungs):
        raise RuntimeFault("SEARCH_PLAN", "Rungs are increasing positive task counts")
    if type(keep) not in (int, float) or not 0 < keep < 1:
        raise RuntimeFault("SEARCH_PLAN", "keep is a fraction in (0, 1)")
    plan: list[dict[str, Any]] = []
    count = len(candidates)
    for index, tasks in enumerate(rungs):
        if count < 1:
            break
        plan.append({"rung": index, "tasks": tasks, "count": count,
                     "candidates": list(candidates[:count])})  # fmt: skip
        if count == 1:
            break
        count = max(1, math.floor(count * keep))
    return plan
