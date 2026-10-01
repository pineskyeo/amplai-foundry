"""Per-layer deciders (Work 033 S10, interfaces.md §2.5, §3.7, §6; ``plan.md`` §8, §8.1, §10).

Each layer has a decider, called at the point of a goal where that layer's inputs exist (§6.1):
L1 at intake, L2/L3/L8 after the plan, L4/L5 at dispatch, L6 on failure, L7 before the final
verification. A decider is a ``decider`` component whose ``decision_method`` names four
swappable parts (features, estimator, selection rule, fallback, §6.2). Every decision writes a
``harness-decision`` record, including the ones the prior made.

v1 is no decider: every point takes its prior, which is the manifest as written (§6.1 L4 "the
manifest as written"; S9 clarification "the first enabled strategy runs when eligible"), so a v1
manifest reproduces today. ``PRIORS`` names the rule of each layer; the caller computes the option
it gives (``DecisionContext.prior``).

The rule-table estimator pools partially (§6.3): per bucket of a feature hierarchy
``[] ⊂ h_1 ⊂ … ⊂ h_k`` and option, ``p_j = (s_j + m·p_{j-1}) / (n_j + m)`` from ``p_{-1} = 0.5``,
with a Wilson interval on the effective counts (an approximation, recorded in the table). A
decision uses the finest level where every compared option has ``min_samples`` and some option's
interval excludes its parent posterior; otherwise it shrinks back to a coarser level.

Fixed for every method (§6.2, ``plan.md`` §10.3): table rows come from development-split
``trial-metrics`` only (``DECIDER_SPLIT``); a table is used only for the cells it was fitted on;
a real goal never explores (an option below ``min_samples`` is never chosen for it); a decider
never evaluates itself.

Choices S10 made where the contract is silent (reported with the slice): the bucket boundaries
below; the record's ``bucket`` lists ``feature=value`` strings of the level used; an L5/L8 option
is ``<component_id>@<version>``; an L4 option is the "+"-joined set of context parts kept on
(``none`` for all off); a judge answers only the L1 question of ``plan.md`` §10.2.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from statistics import NormalDist
from typing import Any

from jsonschema import Draft202012Validator

from ..evaluation.sequential import wilson
from ..runtime.contracts.identity import digest, new_id, now
from ..runtime.contracts.semantics import resolve_ref
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.evidence.cas import ArtifactStore
from ..runtime.execution import policies
from ..runtime.storage.store import Scope, Store
from .judges import JudgeQuestion, JudgeService, JudgeState, QuestionType, usage_tokens

Ref = dict[str, Any]
LAYERS = ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8")
POINTS = {
    "L1": "intake", "L2": "after_plan", "L3": "after_plan", "L8": "after_plan",
    "L4": "dispatch", "L5": "dispatch", "L6": "on_failure", "L7": "before_final_verification",
}  # fmt: skip
FEATURES: dict[str, tuple[str, ...]] = policies.DECIDER_FEATURES  # §6.1
PRIORS: dict[str, str] = {
    "L1": "proceed_unless_questions",
    "L2": "repair_loop",
    "L3": "router_order",
    "L4": "manifest",
    "L5": "driver_defaults",
    "L6": "v1_attempt_policy",
    "L7": "none",
    "L8": "contract_defaults",
}
# the layers whose options are fixed (§6.1); the others come from the manifest or the decider
OPTIONS: dict[str, tuple[str, ...]] = {
    "L1": ("proceed", "ask_back", "replan_ask_first"),
    "L6": ("retry_feedback", "retry_fresh", "escalate", "stop"),
    "L7": ("none", "quick_checks"),
}
DECISION_KIND = "harness-decision"
TABLE_KIND = "decider-table"
TRIAL_METRICS_KIND = "trial-metrics"
INTERVAL_METHOD = "wilson_on_effective_counts_approximation"
DEVELOPMENT = "development"
# The ``domain`` feature (L1, L2, §6.1) of a goal's decisions: a goal never knows it (§6.1). A
# trial's decisions read its corpus domain from ``TrialContext.domain`` (IC-23, provisional). A
# row keeps the features its decision saw (never the trial-metrics ``domain``); a row without a
# decision takes the domain of its plan's trial wire, so fitted buckets and the buckets a decision
# looks up agree.
UNKNOWN_DOMAIN = "unknown"
# plan.md §10.2: the judge question of the L1 decider (the only layer with a defined question)
AMBIGUITY_QUESTION = "Is this goal ambiguous in a way the repository cannot answer?"


# -- buckets (S10 choices; acceptance_items is §6.1's 1 / 2-3 / 4+) --------------------------------
def count_bucket(n: int) -> str:
    """0, 1, 2-3, 4+ (acceptance items, planned files, changed files, failing acceptances)."""
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    return "2-3" if n <= 3 else "4+"


def repo_files_bucket(entries: int, truncated: bool = False) -> str:
    if truncated or entries >= 200:
        return "200+"
    return "<50" if entries < 50 else "50-199"


def tokens_bucket(tokens: int | None) -> str:
    if tokens is None:
        return "unknown"
    if tokens < 100_000:
        return "<100k"
    return "100k-1M" if tokens < 1_000_000 else "1M+"


def fraction_bucket(fraction: float) -> str:
    if fraction < 0.25:
        return "<0.25"
    if fraction < 0.5:
        return "0.25-0.5"
    return "0.5-0.75" if fraction < 0.75 else "0.75+"


def intake_features(draft: dict[str, Any], *, domain: str) -> dict[str, Any]:
    """L1 (§6.1): questions, in_scope_empty, acceptance_items, domain."""
    items = draft.get("work_items")
    acceptance = (
        sum(len(i.get("acceptance") or []) for i in items)
        if isinstance(items, list) and items
        else len(draft.get("acceptance") or [])
    )
    return {
        "questions": len(draft.get("questions") or []),
        "in_scope_empty": not (draft.get("in_scope") or []),
        "acceptance_items": count_bucket(acceptance),
        "domain": domain,
    }


def after_plan_features(
    layer: str, draft: dict[str, Any], *, domain: str, apps: int, strategy: str | None
) -> dict[str, Any]:
    """L2, L3, L8 (§6.1) from the cleaned draft, the goal's apps and the strategy (L3, L8)."""
    acceptance = len(draft.get("acceptance") or []) or sum(
        len(i.get("acceptance") or []) for i in draft.get("work_items") or []
    )
    every = {
        "domain": domain,
        "task_class": str(draft.get("task_class") or "unknown"),
        "planned_files": count_bucket(len(draft.get("in_scope") or [])),
        "acceptance_items": count_bucket(acceptance),
        "apps": int(apps),
        "risk": str(draft.get("risk") or "unknown"),
        "strategy": str(strategy or "unknown"),
    }
    return {k: every[k] for k in FEATURES[layer]}


# -- dataclasses (§3.7) ----------------------------------------------------------------------------
@dataclass(frozen=True)
class DecisionContext:
    layer: str
    cell_id: str  # the harness cell; a table applies only if fitted on it
    features: dict[str, str | int | float | bool]
    options: tuple[str, ...]  # L5/L8: ids of the decider component's `options` refs
    prior: str  # the option the layer's prior rule gives here (the manifest as written)
    subject: dict[str, str]  # goal or trial id
    production: bool  # True for real goals: never explore
    # S10 additions (defaults keep the §3.7 shape): why an option cannot run here, and the
    # state a judge reads (L1)
    ineligible: Mapping[str, str] = field(default_factory=dict)
    judge_state: JudgeState | None = None


@dataclass(frozen=True)
class OptionEstimate:
    option: str
    n: int
    successes: int
    posterior_success: float
    interval: tuple[float, float]
    cost_per_attempt: float | None  # tokens
    cost_per_solved: float | None


@dataclass(frozen=True)
class Decision:
    option: str
    used_prior: bool
    bucket: tuple[str, ...]
    estimates: tuple[OptionEstimate, ...]
    record_ref: Ref


@dataclass(frozen=True)
class DecisionRow:
    task_id: str
    features: dict[str, Any]
    option: str
    success: bool | None
    tokens: int | None
    seconds: float | None


# -- rows from stored trials (§6.5) ----------------------------------------------------------------
def latest(store: Store, scope: Scope, kind: str) -> dict[str, tuple[Ref, dict[str, Any]]]:
    out: dict[str, tuple[Ref, dict[str, Any]]] = {}
    for ref, value in store.list_objects(scope, kind):
        if ref["id"] not in out or ref["revision"] > out[ref["id"]][0]["revision"]:
            out[ref["id"]] = (ref, value)
    return out


def trial_plan(
    store: Store, scope: Scope, artifacts: ArtifactStore, trial_ref: Ref
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(the trial record, its goal's plan record or None) of a trial-metrics row."""
    _kind, trial = resolve_ref(store, scope, trial_ref)
    refs = trial.get("artifact_refs") or []
    if not refs:
        return trial, None
    receipt = json.loads(artifacts.read(scope, refs[0], trusted=True))
    goal_id = receipt.get("goal_id")
    if not goal_id:
        return trial, None
    plan: dict[str, Any] = store.head(scope, "execution-plan", goal_id)["data"]
    return trial, plan


def plan_decisions(store: Store, scope: Scope, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The harness-decision records a plan names, in order."""
    out = []
    for ref in plan.get("decisions") or []:
        try:
            out.append(store.get(scope, DECISION_KIND, ref))
        except (Hold, RuntimeFault):
            continue
    return out


def rows_from_trial_metrics(
    store: Store,
    scope: Scope,
    *,
    layer: str,
    cells: list[str],
    splits: tuple[str, ...] = ("development",),
) -> list[DecisionRow]:
    """Rows of ``layer`` from stored trial-metrics of ``cells`` (§6.5).

    Hold DECIDER_SPLIT unless ``splits`` is the development split only (``plan.md`` §10.3: a
    decider never reads validation or holdout results). Every record is filtered on its own
    ``split`` too, so calibration rows of validation tasks are skipped. A row's option and
    features are the trial's first decision of the layer; without one (a trial from before S10)
    an L2 row takes the strategy it ran and an L3 row its cell, with the features recomputed from
    its plan; other layers' rows without a decision are skipped (the option is not known).
    A row's features are the ones its decision point saw (or would have seen): ``domain`` is the
    plan's trial domain as at the decision (IC-23; ``UNKNOWN_DOMAIN`` for a wire without one),
    never the trial-metrics ``domain``, so a table is bucketed on the domains decisions name."""
    if layer not in LAYERS:
        raise RuntimeFault("DECIDER_LAYER", "Unknown layer", details=layer)
    if not splits or any(s != DEVELOPMENT for s in splits):
        raise Hold(
            "DECIDER_SPLIT", "A decider table reads development-split trials only",
            details=list(splits),
        )  # fmt: skip
    wanted = set(cells)
    artifacts = ArtifactStore(store)
    rows: list[DecisionRow] = []
    for _id, (_ref, tm) in sorted(latest(store, scope, TRIAL_METRICS_KIND).items()):
        if tm.get("split") != DEVELOPMENT or tm.get("cell_id") not in wanted:
            continue
        try:
            _trial, plan = trial_plan(store, scope, artifacts, tm["trial_ref"])
        except (Hold, RuntimeFault, KeyError, ValueError):
            continue
        found = _row_option(store, scope, layer, tm, plan)
        if found is None:
            continue
        option, features = found
        tokens = tm.get("tokens") or {}
        counts = [tokens.get("input"), tokens.get("output")]
        rows.append(
            DecisionRow(
                task_id=str(tm["task_id"]),
                features=features,
                option=option,
                success=tm.get("success"),
                tokens=sum(counts) if all(type(c) is int for c in counts) else None,  # type: ignore[arg-type]
                seconds=float(tm["wall_seconds"]) if tm.get("wall_seconds") is not None else None,
            )
        )
    return rows


def _row_option(
    store: Store, scope: Scope, layer: str, tm: dict[str, Any], plan: dict[str, Any] | None
) -> tuple[str, dict[str, Any]] | None:
    if plan is not None:
        for decision in plan_decisions(store, scope, plan):
            if decision.get("layer") == layer:
                # the features the decision saw, as recorded: what a later decision looks up
                return str(decision["chosen"]), dict(decision.get("features") or {})
    option = {"L2": tm.get("strategy"), "L3": tm.get("cell_id")}.get(layer)
    if not option:
        return None
    if plan is None or not isinstance(plan.get("draft"), dict):
        return str(option), {}  # the option without features: the global level only
    strategy = (plan.get("strategy") or {}).get("strategy") or tm.get("strategy")
    features = after_plan_features(
        layer, plan["draft"], domain=plan_domain(plan),
        apps=len(plan.get("apps") or [plan.get("app")]), strategy=strategy,
    )  # fmt: skip
    return str(option), features


def plan_domain(plan: dict[str, Any]) -> str:
    """The domain a plan's decisions saw (IC-23): its trial wire's ``domain``; ``UNKNOWN_DOMAIN``
    for a goal or a trial planned before IC-23."""
    trial = plan.get("trial")
    domain = trial.get("domain") if isinstance(trial, dict) else None
    return domain if isinstance(domain, str) and domain else UNKNOWN_DOMAIN


# -- the rule table (§6.3, §6.5) -------------------------------------------------------------------
@dataclass
class _Agg:
    n: int = 0
    s: int = 0
    tokens: list[int] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    tokens_mean: float | None = None
    seconds_mean: float | None = None

    def means(self) -> tuple[float | None, float | None]:
        tokens = self.tokens_mean
        if tokens is None and self.tokens:
            tokens = sum(self.tokens) / len(self.tokens)
        seconds = self.seconds_mean
        if seconds is None and self.seconds:
            seconds = sum(self.seconds) / len(self.seconds)
        return tokens, seconds


@dataclass(frozen=True)
class _Level:
    level: int
    key: tuple[tuple[str, Any], ...]
    n: int
    s: int
    posterior: float
    interval: tuple[float, float]
    tokens_mean: float | None
    seconds_mean: float | None
    parent: float


def _key(level: tuple[str, ...], features: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple((k, features[k]) for k in level)


def _bucket_strings(key: tuple[tuple[str, Any], ...]) -> tuple[str, ...]:
    return tuple(f"{k}={json.dumps(v) if not isinstance(v, str) else v}" for k, v in key)


class RuleTable:
    def __init__(
        self,
        *,
        layer: str,
        features: tuple[str, ...],
        hierarchy: tuple[tuple[str, ...], ...],
        options: tuple[str, ...],
        pooling_strength: float,
        confidence: float,
        aggregates: dict[tuple[int, tuple[tuple[str, Any], ...], str], _Agg],
        rows: int,
    ) -> None:
        _check_shape(layer, features, hierarchy, options, pooling_strength, confidence)
        self.layer, self.features, self.hierarchy = layer, features, hierarchy
        self.options, self.pooling_strength, self.confidence = options, pooling_strength, confidence
        self._agg, self.rows = aggregates, rows
        self._z = NormalDist().inv_cdf((1 + confidence) / 2)

    @classmethod
    def fit(
        cls,
        rows: Iterable[DecisionRow],
        *,
        layer: str,
        features: tuple[str, ...],
        hierarchy: tuple[tuple[str, ...], ...],
        options: tuple[str, ...],
        pooling_strength: float,
        confidence: float = 0.95,
    ) -> RuleTable:
        """Aggregate rows per (level, bucket, option). A row with an unknown outcome or an option
        outside ``options`` is left out; a row lacking a level's features counts only at the
        coarser levels it has."""
        features, options = tuple(features), tuple(options)
        hierarchy = tuple(tuple(level) for level in hierarchy)
        _check_shape(layer, features, hierarchy, options, pooling_strength, confidence)
        aggregates: dict[tuple[int, tuple[tuple[str, Any], ...], str], _Agg] = {}
        used = 0
        for row in rows:
            if row.option not in options or row.success is None:
                continue
            used += 1
            for j, level in enumerate(hierarchy):
                if any(k not in row.features for k in level):
                    break
                agg = aggregates.setdefault((j, _key(level, row.features), row.option), _Agg())
                agg.n += 1
                agg.s += 1 if row.success else 0
                if row.tokens is not None:
                    agg.tokens.append(int(row.tokens))
                if row.seconds is not None:
                    agg.seconds.append(float(row.seconds))
        return cls(
            layer=layer, features=features, hierarchy=hierarchy, options=options,
            pooling_strength=float(pooling_strength), confidence=float(confidence),
            aggregates=aggregates, rows=used,
        )  # fmt: skip

    @classmethod
    def from_record(cls, value: dict[str, Any]) -> RuleTable:
        """The table a ``decider-table`` record holds."""
        validate_table(value)
        hierarchy = tuple(tuple(level) for level in value["hierarchy"])
        aggregates: dict[tuple[int, tuple[tuple[str, Any], ...], str], _Agg] = {}
        for cell in value["cells"]:
            # stored JSON is canonical (sorted keys): the level is found by its feature set
            names = set(cell["bucket"])
            matches = [j for j, level in enumerate(hierarchy) if set(level) == names]
            if not matches:
                raise RuntimeFault("DECIDER_TABLE", "A table entry's bucket is not a level")
            j = matches[0]
            level = hierarchy[j]
            aggregates[(j, _key(level, cell["bucket"]), cell["option"])] = _Agg(
                n=int(cell["n"]), s=int(cell["successes"]),
                tokens_mean=cell["cost_tokens_mean"], seconds_mean=cell["seconds_mean"],
            )  # fmt: skip
        return cls(
            layer=value["layer"], features=tuple(value["features"]), hierarchy=hierarchy,
            options=tuple(value["options"]), pooling_strength=float(value["pooling_strength"]),
            confidence=float(value["confidence"]), aggregates=aggregates,
            rows=int(value["source"]["rows"]),
        )  # fmt: skip

    def _chain(self, features: Mapping[str, Any], option: str) -> list[_Level]:
        m = self.pooling_strength
        out: list[_Level] = []
        parent = 0.5  # p_{-1}
        for j, level in enumerate(self.hierarchy):
            if any(k not in features for k in level):
                break
            key = _key(level, features)
            agg = self._agg.get((j, key, option)) or _Agg()
            denominator = agg.n + m
            if denominator > 0:
                effective = agg.s + m * parent
                posterior = effective / denominator
                low, high = wilson(effective, denominator, self._z)  # type: ignore[arg-type]
                interval = (float(low), float(high))
            else:  # m = 0 and no rows: nothing moves the parent
                posterior, interval = parent, (0.0, 1.0)
            tokens, seconds = agg.means() if agg.n else (None, None)
            out.append(_Level(j, key, agg.n, agg.s, posterior, interval, tokens, seconds, parent))
            parent = posterior
        return out

    @staticmethod
    def _estimate(option: str, level: _Level) -> OptionEstimate:
        cost = level.tokens_mean
        per_solved = cost / level.posterior if cost is not None and level.posterior > 0 else None
        return OptionEstimate(
            option, level.n, level.s, round(level.posterior, 6),
            (round(level.interval[0], 6), round(level.interval[1], 6)),
            round(cost, 3) if cost is not None else None,
            round(per_solved, 3) if per_solved is not None else None,
        )  # fmt: skip

    def level_index(self, bucket: tuple[str, ...]) -> int:
        """The hierarchy level of a bucket returned by ``estimates`` (0 = global)."""
        names = {b.split("=", 1)[0] for b in bucket}
        for j, level in enumerate(self.hierarchy):
            if set(level) == names:
                return j
        raise RuntimeFault("DECIDER_TABLE", "Not a bucket of this table", details=list(bucket))

    def levels(self, features: Mapping[str, Any]) -> int:
        """How many hierarchy levels ``features`` reach (the global level always)."""
        reached = 0
        for level in self.hierarchy:
            if any(k not in features for k in level):
                break
            reached += 1
        return reached

    def estimates_at(
        self, features: Mapping[str, Any], level: int, *, options: Iterable[str] | None = None
    ) -> tuple[tuple[str, ...], list[OptionEstimate]]:
        compared = list(self.options if options is None else options)
        chains = {o: self._chain(features, o) for o in compared}
        if not compared or level >= min(len(c) for c in chains.values()):
            return (), []
        key = chains[compared[0]][level].key
        return _bucket_strings(key), [self._estimate(o, chains[o][level]) for o in compared]

    def estimates(
        self,
        features: dict[str, Any],
        *,
        min_samples: int,
        options: Iterable[str] | None = None,
    ) -> tuple[tuple[str, ...], list[OptionEstimate]]:
        """(the bucket used, one estimate per compared option) at the finest level where every
        compared option has ``min_samples`` and some option's interval excludes its parent
        posterior (a credible difference, §6.3 step 3); else the global level."""
        compared = list(self.options if options is None else options)
        if not compared:
            return (), []
        chains = {o: self._chain(features, o) for o in compared}
        depth = min(len(c) for c in chains.values())
        chosen = 0
        for j in range(depth - 1, 0, -1):
            here = [chains[o][j] for o in compared]
            enough = all(level.n >= min_samples for level in here)
            credible = any(
                level.interval[0] > level.parent or level.interval[1] < level.parent
                for level in here
            )
            if enough and credible:
                chosen = j
                break
        return self.estimates_at(features, chosen, options=compared)

    def record(self, *, method_ref: Ref | None, source: dict[str, Any]) -> dict[str, Any]:
        """The ``decider-table`` value (§2.5) without ``scope`` (``write_table`` adds it)."""
        cells = []
        for (j, key, option), agg in sorted(
            self._agg.items(), key=lambda item: (item[0][0], json.dumps(item[0][1]), item[0][2])
        ):
            bucket = dict(key)
            chain = self._chain(bucket, option)
            if len(chain) <= j:
                continue
            here = chain[j]
            estimate = self._estimate(option, here)
            tokens, seconds = agg.means()
            cells.append({
                "bucket": bucket, "option": option, "n": agg.n, "successes": agg.s,
                "cost_tokens_mean": round(tokens, 3) if tokens is not None else None,
                "seconds_mean": round(seconds, 3) if seconds is not None else None,
                "posterior_success": estimate.posterior_success,
                "interval": list(estimate.interval),
                "cost_per_solved": estimate.cost_per_solved,
                "pooled_from": [dict(chain[i].key) for i in range(j)],
            })  # fmt: skip
        return {
            "schema": "amplai.decider-table.v1",
            "layer": self.layer,
            "features": list(self.features),
            "hierarchy": [list(level) for level in self.hierarchy],
            "options": list(self.options),
            "source": {**source, "rows": self.rows},
            "cells": cells,
            "method_ref": method_ref,
            "pooling_strength": self.pooling_strength,
            "confidence": self.confidence,
            "interval_method": INTERVAL_METHOD,
            "fitted_at": now(),
        }


def _check_shape(
    layer: str,
    features: tuple[str, ...],
    hierarchy: tuple[tuple[str, ...], ...],
    options: tuple[str, ...],
    pooling_strength: float,
    confidence: float,
) -> None:
    problems = []
    if layer not in LAYERS:
        problems.append("layer")
    if any(f not in FEATURES.get(layer, ()) for f in features) or len(set(features)) != len(
        features
    ):
        problems.append("features are distinct features of the layer")
    if not hierarchy or hierarchy[0] != ():
        problems.append("the hierarchy starts with the global level []")
    for coarse, fine in pairwise(hierarchy):
        if not set(coarse) < set(fine):
            problems.append("each hierarchy level adds features to the previous one")
            break
    if any(k not in features for level in hierarchy for k in level):
        problems.append("hierarchy levels use the table's features")
    if (
        not options
        or len(set(options)) != len(options)
        or any(not isinstance(o, str) or not o for o in options)
    ):
        problems.append("options are distinct option ids")
    if type(pooling_strength) not in (int, float) or not 0 <= pooling_strength <= 100:
        problems.append("pooling_strength is 0..100")
    if type(confidence) is not float or not 0 < confidence < 1:
        problems.append("confidence is in (0, 1)")
    if problems:
        raise RuntimeFault("DECIDER_TABLE", "Invalid rule table", details=problems)


def default_hierarchy(features: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    """[] → f1 → (f1, f2) → … (§6.3), from the features in order."""
    names = tuple(features)
    return tuple(names[:i] for i in range(len(names) + 1))


# -- selection rules (§6.4, §6.2) ------------------------------------------------------------------
def _rank_key(rank: Mapping[str, int], option: str) -> tuple[float, str]:
    return (float(rank.get(option, math.inf)), option)


def select_noninferior_cheapest(
    estimates: list[OptionEstimate], *, margin: float, min_samples: int, rank: dict[str, int]
) -> str | None:
    """Eligible = n ≥ min_samples; best = highest posterior; drop an option whose interval upper
    bound is below ``p_best - margin`` (credibly worse); among the rest the lowest cost per solved
    task, ties within 1 % to the lower rank. Options without a cost are compared only when no
    option has one (then the lowest rank). None when nothing is eligible (→ fallback)."""
    eligible = [e for e in estimates if e.n >= min_samples]
    if not eligible:
        return None
    best = max(e.posterior_success for e in eligible)
    kept = [e for e in eligible if e.interval[1] >= best - margin]
    costed = [e for e in kept if e.cost_per_solved is not None]
    if not costed:
        return min(kept, key=lambda e: _rank_key(rank, e.option)).option
    cheapest = min(float(e.cost_per_solved) for e in costed)  # type: ignore[arg-type]
    tied = [e for e in costed if float(e.cost_per_solved) <= cheapest * 1.01]  # type: ignore[arg-type]
    return min(tied, key=lambda e: _rank_key(rank, e.option)).option


def select_max_success(
    estimates: list[OptionEstimate], *, min_samples: int, rank: dict[str, int]
) -> str | None:
    eligible = [e for e in estimates if e.n >= min_samples]
    if not eligible:
        return None
    best = max(e.posterior_success for e in eligible)
    tied = [e for e in eligible if e.posterior_success == best]
    return min(tied, key=lambda e: _rank_key(rank, e.option)).option


def select_utility(
    estimates: list[OptionEstimate], *, utility_lambda: float, min_samples: int,
    rank: dict[str, int],
) -> str | None:  # fmt: skip
    """utility = success - λ·tokens/10⁶ (§6.2), tokens per attempt; an option without a token
    cost has no utility unless λ is 0."""
    scored = []
    for e in estimates:
        if e.n < min_samples:
            continue
        if e.cost_per_attempt is None and utility_lambda != 0:
            continue
        cost = e.cost_per_attempt or 0.0
        scored.append((e.posterior_success - utility_lambda * cost / 1e6, e))
    if not scored:
        return None
    best = max(u for u, _ in scored)
    tied = [e for u, e in scored if u == best]
    return min(tied, key=lambda e: _rank_key(rank, e.option)).option


# -- configuration checks (refusal before any claim) -----------------------------------------------
def _component(store: Store, scope: Scope, ref: Ref, kind: str) -> dict[str, Any]:
    return policies.component_content(store, scope, ref, kind, kind)


def check(store: Store, scope: Scope, layer: str, content: dict[str, Any] | None) -> list[str]:
    """What of a ``decider`` component cannot be honoured at ``layer`` (empty = honoured). A part
    that cannot run is refused, never ignored: a judge estimator or fallback outside L1 (no
    question is defined there) or naming Jev (not configured, §14 Q9), method features of another
    layer, a table of another layer, unreadable options."""
    if content is None:
        return []
    problems: list[str] = []
    name = f"decider {layer}"
    if content.get("layer") != layer:
        return [f"{name}: the component is for {content.get('layer')}"]
    try:
        method = _component(store, scope, content["method"], "decision_method")
    except (Hold, RuntimeFault) as exc:
        return [f"{name}: method {exc.code}"]
    foreign = sorted(f for f in method["features"] if f not in FEATURES[layer])
    if foreign:
        problems.append(f"{name}: features {', '.join(foreign)} are not {layer} features")
    if content["table"] is not None:
        try:
            table = store.get(scope, TABLE_KIND, content["table"])
            validate_table(table)
            if table["layer"] != layer:
                problems.append(f"{name}: its table was fitted for {table['layer']}")
        except (Hold, RuntimeFault) as exc:
            problems.append(f"{name}: table {exc.code}")
    uses_judge = method["estimator"] == "judge_v1" or method["fallback"] == "judge_v1"
    if uses_judge:
        judge: dict[str, Any] = {"judge": "none"}
        if content["judge"] is not None:
            try:
                judge = _component(store, scope, content["judge"], "judge_model")
            except (Hold, RuntimeFault) as exc:
                problems.append(f"{name}: judge {exc.code}")
        if layer != "L1":
            problems.append(f"{name}: judge_v1 has a defined question at L1 only")
        elif judge["judge"] == "none" or "yes_no" not in (judge.get("question_types") or []):
            problems.append(f"{name}: judge_v1 needs a yes_no judge")
        elif judge["judge"] == "jev":
            problems.append(f"{name}: the Jev judge is not configured (§14 Q9)")
    want = {"L5": "driver_options", "L8": "limits"}.get(layer)
    if want is not None:
        for ref in content["options"] or []:
            try:
                _component(store, scope, ref, want)
            except (Hold, RuntimeFault) as exc:
                problems.append(f"{name}: option {ref.get('id')}: {exc.code}")
    return problems


def option_id(ref: Ref) -> str:
    """An L5/L8 option: ``<component_id>@<version>`` (the §2.12 change-path form)."""
    return f"{ref['id']}@{ref['revision']}"


def load_table(
    store: Store, scope: Scope, content: dict[str, Any] | None
) -> tuple[RuleTable | None, tuple[str, ...]]:
    """The decider's table and the cells it was fitted on (``source.trial_metrics_query.cells``)."""
    if content is None or content.get("table") is None:
        return None, ()
    value = store.get(scope, TABLE_KIND, content["table"])
    cells = ((value.get("source") or {}).get("trial_metrics_query") or {}).get("cells") or []
    return RuleTable.from_record(value), tuple(str(c) for c in cells)


# -- the decider (§3.7) ----------------------------------------------------------------------------
class Decider:
    def __init__(
        self,
        store: Store,
        scope: Scope,
        component: dict[str, Any] | None,
        *,
        table: RuleTable | None,
        table_cells: tuple[str, ...],
        judges: JudgeService | None,
        choose_judge: Callable[[QuestionType], Any] | None,
        ref: Ref | None = None,
        aux: Any = None,
    ) -> None:
        """``component`` is the decider's content (or its harness-component record); ``ref`` its
        component ref, recorded as ``decider_ref``. ``aux`` is the goal's auxiliary-turn ledger
        (``strategy_runner.AuxLedger``): a judge turn is auxiliary (IC-21) and is never asked
        without it."""
        if component is not None and "content" in component and "kind" in component:
            component = component["content"]
        self.store, self.scope, self.content = store, scope, component
        self.table, self.table_cells = table, tuple(table_cells)
        self.judges, self.choose_judge = judges, choose_judge
        self.ref, self.aux = ref, aux

    @classmethod
    def of(
        cls,
        store: Store,
        scope: Scope,
        component: dict[str, Any] | None,
        *,
        ref: Ref | None = None,
        judges: JudgeService | None = None,
        choose_judge: Callable[[QuestionType], Any] | None = None,
        aux: Any = None,
    ) -> Decider:
        """A decider with the table its component names."""
        table, cells = load_table(store, scope, component)
        return cls(
            store, scope, component, table=table, table_cells=cells, judges=judges,
            choose_judge=choose_judge, ref=ref, aux=aux,
        )  # fmt: skip

    def _rank(self, ctx: DecisionContext) -> dict[str, int]:
        if ctx.layer == "L2":
            return dict(policies.STRATEGY_RANK)
        return {o: i for i, o in enumerate(ctx.options)}

    def _select(
        self,
        method: dict[str, Any],
        estimates: list[OptionEstimate],
        policy: dict[str, Any],
        rank: dict[str, int],
    ) -> str | None:
        min_samples = int(policy["min_samples"])
        if method["selection"] == "max_success_v1":
            return select_max_success(estimates, min_samples=min_samples, rank=rank)
        if method["selection"] == "utility_v1":
            return select_utility(
                estimates, utility_lambda=float(method["utility_lambda"]),
                min_samples=min_samples, rank=rank,
            )  # fmt: skip
        return select_noninferior_cheapest(
            estimates, margin=float(policy["margin"]), min_samples=min_samples, rank=rank
        )

    def _table_for(self, ctx: DecisionContext) -> tuple[RuleTable | None, str]:
        if self.table is None:
            return None, "no table"
        if self.table.layer != ctx.layer:
            return None, "the table is for another layer"
        if ctx.cell_id not in self.table_cells:
            return None, "the table was fitted on other cells"
        return self.table, ""

    def _judge(
        self, ctx: DecisionContext, eligible: list[str]
    ) -> tuple[str | None, Ref | None, str]:
        """L1 only (``plan.md`` §10.2): a qualified yes/no judge on whether the goal is ambiguous
        in a way the repository cannot answer; yes → ask_back, no → proceed."""
        if ctx.layer != "L1":
            return None, None, "judge: no question is defined for this layer"
        if self.judges is None or self.choose_judge is None or ctx.judge_state is None:
            return None, None, "judge: none configured"
        if self.aux is None:
            return None, None, "judge: no auxiliary-turn ledger (IC-21)"
        connector = self.choose_judge("yes_no")
        if connector is None:
            return None, None, "judge: no qualified judge"
        try:
            self.aux.check("judge")
        except Hold as exc:
            return None, None, f"judge: {exc.code}"
        question = JudgeQuestion("ambiguous", "yes_no", AMBIGUITY_QUESTION)
        entry: dict[str, Any] = {
            "role": "judge", "purpose": "L1", "cell_id": getattr(connector, "cell_id", None),
            "node_id": None, "prompt_digest": digest({"text": AMBIGUITY_QUESTION}), "at": now(),
        }  # fmt: skip
        try:
            answers, ref = self.judges.ask(
                connector, ctx.judge_state, [question], subject=ctx.subject
            )
        except (Hold, RuntimeFault) as exc:
            # refused before the turn ran: nothing spent; else the usage is unknown (IC-21)
            before = exc.code in {
                "JUDGE_UNQUALIFIED",
                "JUDGE_DATA_CLASS",
                "JUDGE_WORKSPACE",
                "JUDGE_NONE",
                "JUDGE_NOT_CONFIGURED",
                "JUDGE_QUESTION",
            }
            self.aux.add({**entry, "usage": None, "tokens": 0 if before else None,
                          "seconds": 0.0 if before else None, "error": exc.code})  # fmt: skip
            return None, None, f"judge: {exc.code}"
        usage = getattr(connector, "last_usage", None)
        self.aux.add({**entry, "usage": usage, "tokens": usage_tokens(usage), "seconds": None,
                      "error": None})  # fmt: skip
        picked = "ask_back" if answers[0].value is True else "proceed"
        if picked not in eligible:
            return None, ref, f"judge: {picked} cannot run here"
        return picked, ref, "judge"

    def decide(self, ctx: DecisionContext) -> Decision:
        """Choose an option for ``ctx`` and write its ``harness-decision`` (§2.5, §6.8)."""
        if ctx.layer not in LAYERS:
            raise RuntimeFault("DECIDER_CONTEXT", "Unknown layer", details=ctx.layer)
        options = list(dict.fromkeys(ctx.options))
        if not options or not isinstance(ctx.prior, str) or not ctx.prior:
            raise RuntimeFault("DECIDER_CONTEXT", "A decision needs options and a prior")
        eligible = [o for o in options if o not in ctx.ineligible]
        rank = self._rank(ctx)
        estimates: list[OptionEstimate] = []
        bucket: tuple[str, ...] = ()
        chosen, used_prior, why = ctx.prior, True, "prior: no decider"
        method_ref = table_ref = None
        judge_ref: Ref | None = None
        min_samples = 0
        if self.content is not None:
            content = self.content
            method_ref, table_ref = content["method"], content["table"]
            method = _component(self.store, self.scope, content["method"], "decision_method")
            policy = content["policy"]
            min_samples = int(policy["min_samples"])
            features = {f: ctx.features[f] for f in method["features"] if f in ctx.features}
            table, why = self._table_for(ctx)
            picked: str | None = None
            if method["estimator"] == "pooled_beta_binomial_v1":
                if table is not None and eligible:
                    bucket, estimates = table.estimates(
                        features, min_samples=min_samples, options=eligible
                    )
                    picked = self._select(method, estimates, policy, rank)
                    why = "" if picked else "no option reaches min_samples in the bucket"
            else:
                picked, judge_ref, why = self._judge(ctx, eligible)
            if picked is None and method["fallback"] == "coarser_bucket_v1" and table is not None:
                start = table.level_index(bucket) - 1 if estimates else -1
                for level in range(start, -1, -1):
                    coarse, at = table.estimates_at(features, level, options=eligible)
                    better = self._select(method, at, policy, rank)
                    if better is not None:
                        picked, bucket, estimates = better, coarse, at
                        break
            elif picked is None and method["fallback"] == "judge_v1" and judge_ref is None:
                picked, judge_ref, why = self._judge(ctx, eligible)
            if picked is not None:
                chosen, used_prior = picked, False
            else:
                why = "prior: " + (why or "fallback")
        by_option = {e.option: e for e in estimates}
        if ctx.production and not used_prior and judge_ref is None:
            # a real goal never explores (§6.2): the chosen option has its samples
            found = by_option.get(chosen)
            if found is None or found.n < min_samples:
                raise RuntimeFault("DECIDER_EXPLORE", "A real goal never explores", details=chosen)
        rows = []
        for option in options:
            e = by_option.get(option)
            reason = ctx.ineligible.get(option, "")
            if not reason and option == chosen:
                reason = why if used_prior else ("judge" if judge_ref else "chosen")
            elif not reason and e is not None and e.n < min_samples:
                reason = "below min_samples"
            rows.append({
                "option": option, "n": e.n if e else 0,
                "posterior_success": e.posterior_success if e else None,
                "interval": list(e.interval) if e else None,
                "cost_per_solved": e.cost_per_solved if e else None,
                "eligible": option not in ctx.ineligible, "why": reason[:600],
            })  # fmt: skip
        value = {
            "schema": "amplai.harness-decision.v1",
            "scope": self.scope.wire(),
            "subject": dict(ctx.subject),
            "cell_id": ctx.cell_id,
            "layer": ctx.layer,
            "point": POINTS[ctx.layer],
            "decider_ref": self.ref if self.content is not None else None,
            "method_ref": method_ref,
            "table_ref": table_ref,
            "features": dict(ctx.features),
            "bucket": list(bucket),
            "options": rows,
            "chosen": chosen,
            "used_prior": used_prior,
            "prior": PRIORS[ctx.layer],
            "judge_call_ref": judge_ref,
            "decided_at": now(),
        }
        validate_decision(value)
        with self.store.tx() as db:
            ref: Ref = self.store.put(db, self.scope, DECISION_KIND, new_id("decision"), 1, value)
        return Decision(chosen, used_prior, tuple(bucket), tuple(estimates), ref)


# -- records ---------------------------------------------------------------------------------------
_REF: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "revision", "digest"],
    "properties": {
        "id": {"type": "string"},
        "revision": {"type": "integer", "minimum": 1},
        "digest": {"type": "string"},
    },
}
_OPT_REF = {"oneOf": [_REF, {"type": "null"}]}
_INTERVAL = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
_SCALAR = {"type": ["string", "integer", "number", "boolean"]}
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema", "scope", "subject", "cell_id", "layer", "point", "decider_ref", "method_ref",
        "table_ref", "features", "bucket", "options", "chosen", "used_prior", "prior",
        "judge_call_ref", "decided_at",
    ],
    "properties": {
        "schema": {"const": "amplai.harness-decision.v1"},
        "scope": {"type": "object"},
        "subject": {
            "type": "object", "additionalProperties": {"type": "string"}, "minProperties": 1
        },
        "cell_id": {"type": "string"},
        "layer": {"enum": list(LAYERS)},
        "point": {"enum": sorted(set(POINTS.values()))},
        "decider_ref": _OPT_REF,
        "method_ref": _OPT_REF,
        "table_ref": _OPT_REF,
        "features": {"type": "object", "additionalProperties": _SCALAR},
        "bucket": {"type": "array", "items": {"type": "string"}},
        "options": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["option", "n", "posterior_success", "interval", "cost_per_solved",
                             "eligible", "why"],
                "properties": {
                    "option": {"type": "string", "minLength": 1},
                    "n": {"type": "integer", "minimum": 0},
                    "posterior_success": {"type": ["number", "null"]},
                    "interval": {"oneOf": [_INTERVAL, {"type": "null"}]},
                    "cost_per_solved": {"type": ["number", "null"]},
                    "eligible": {"type": "boolean"},
                    "why": {"type": "string"},
                },
            },
        },
        "chosen": {"type": "string", "minLength": 1},
        "used_prior": {"type": "boolean"},
        "prior": {"type": "string", "minLength": 1},
        "judge_call_ref": _OPT_REF,
        "decided_at": {"type": "string", "minLength": 1},
    },
}  # fmt: skip
TABLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema", "layer", "features", "hierarchy", "options", "source", "cells", "method_ref",
        "pooling_strength", "confidence", "interval_method", "fitted_at",
    ],
    "properties": {
        "schema": {"const": "amplai.decider-table.v1"},
        "scope": {"type": "object"},
        "layer": {"enum": list(LAYERS)},
        "features": {"type": "array", "items": {"type": "string"}},
        "hierarchy": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        "options": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "source": {
            "type": "object",
            "required": ["corpus_ref", "trial_metrics_query", "rows"],
            "properties": {
                "corpus_ref": _OPT_REF,
                "trial_metrics_query": {
                    "type": "object",
                    "required": ["cells", "since", "splits"],
                    "properties": {
                        "cells": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "since": {"type": ["string", "null"]},
                        "splits": {"type": "array", "items": {"const": DEVELOPMENT},
                                   "minItems": 1},
                    },
                },
                "rows": {"type": "integer", "minimum": 0},
            },
        },
        "cells": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["bucket", "option", "n", "successes", "cost_tokens_mean",
                             "seconds_mean", "posterior_success", "interval", "cost_per_solved",
                             "pooled_from"],
                "properties": {
                    "bucket": {"type": "object", "additionalProperties": _SCALAR},
                    "option": {"type": "string"},
                    "n": {"type": "integer", "minimum": 0},
                    "successes": {"type": "integer", "minimum": 0},
                    "cost_tokens_mean": {"type": ["number", "null"]},
                    "seconds_mean": {"type": ["number", "null"]},
                    "posterior_success": {"type": "number"},
                    "interval": _INTERVAL,
                    "cost_per_solved": {"type": ["number", "null"]},
                    "pooled_from": {"type": "array", "items": {"type": "object"}},
                },
            },
        },
        "method_ref": _OPT_REF,
        "pooling_strength": {"type": "number", "minimum": 0, "maximum": 100},
        "confidence": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
        "interval_method": {"const": INTERVAL_METHOD},
        "fitted_at": {"type": "string", "minLength": 1},
    },
}  # fmt: skip


def _validate(schema: dict[str, Any], value: dict[str, Any], code: str, what: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if errors:
        raise RuntimeFault(
            code, f"{what} does not match §2.5",
            details=[f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]],
        )  # fmt: skip


def validate_decision(value: dict[str, Any]) -> None:
    _validate(DECISION_SCHEMA, value, "DECISION_RECORD", "harness-decision record")


def validate_table(value: dict[str, Any]) -> None:
    _validate(TABLE_SCHEMA, value, "DECIDER_TABLE", "decider-table record")


def write_table(
    store: Store, scope: Scope, table: RuleTable, *, method_ref: Ref | None, source: dict[str, Any]
) -> Ref:
    """Store a fitted table as ``decider-table`` (id ``table-<layer>-<digest24>`` of its content
    without the fit time; the same fit gives the same ref)."""
    value = {**table.record(method_ref=method_ref, source=source), "scope": scope.wire()}
    validate_table(value)
    splits = value["source"]["trial_metrics_query"]["splits"]
    if any(s != DEVELOPMENT for s in splits):
        raise Hold("DECIDER_SPLIT", "A decider table reads development-split trials only")
    content = {k: v for k, v in value.items() if k != "fitted_at"}
    table_id = f"table-{table.layer}-{digest(content)[7:31]}"
    existing = latest(store, scope, TABLE_KIND).get(table_id)
    if existing is not None:
        return existing[0]
    with store.tx() as db:
        ref: Ref = store.put(db, scope, TABLE_KIND, table_id, 1, value)
    return ref


# -- regret (§6.8, plan.md §8.1) -------------------------------------------------------------------
def _rate(e: OptionEstimate) -> float:
    return float(e.posterior_success)


def regret(
    decisions: list[dict[str, Any]], measured: dict[str, dict[str, OptionEstimate]]
) -> dict[str, Any]:
    """Regret of decisions against the best option in hindsight (§6.8).

    ``measured`` maps a task id to the measured estimate of each option on that task for the
    decisions' cell; a decision names its task by ``task_id`` (the caller resolves it from the
    decision's trial). Only decisions whose task has every eligible option measured count:
    best = highest success rate, ties to the lowest tokens per solved task; ``success_regret`` =
    s_best - s_chosen and, when equal, ``cost_regret`` = cost_chosen - cost_best. Coverage is the
    share of decisions not made by the prior; for L6 also the escalation rate and the measured
    tokens of the escalations chosen."""
    per: list[dict[str, Any]] = []
    for decision in decisions:
        task = decision.get("task_id")
        table = measured.get(task) if isinstance(task, str) else None
        options = [o["option"] for o in decision.get("options") or [] if o.get("eligible")]
        chosen = decision.get("chosen")
        if not options:
            options = [chosen]
        if table is None or chosen not in table or any(o not in table for o in options):
            continue
        ranked = sorted(
            (table[o] for o in options),
            key=lambda e: (
                -_rate(e),
                e.cost_per_solved if e.cost_per_solved is not None else math.inf,
                e.option,
            ),
        )
        best, mine = ranked[0], table[chosen]
        success_regret = round(_rate(best) - _rate(mine), 6)
        cost_regret = None
        if (
            success_regret == 0
            and mine.cost_per_solved is not None
            and (best.cost_per_solved is not None)
        ):
            cost_regret = round(mine.cost_per_solved - best.cost_per_solved, 3)
        per.append({
            "task_id": task, "layer": decision.get("layer"), "chosen": chosen,
            "best": best.option, "success_regret": success_regret, "cost_regret": cost_regret,
            "used_prior": bool(decision.get("used_prior")),
        })  # fmt: skip
    n = len(decisions)
    costs = [p["cost_regret"] for p in per if p["cost_regret"] is not None]
    out: dict[str, Any] = {
        "decisions": n,
        "measured": len(per),
        "coverage": round(sum(1 for d in decisions if not d.get("used_prior")) / n, 6)
        if n
        else None,
        "success_regret_mean": round(sum(p["success_regret"] for p in per) / len(per), 6)
        if per
        else None,
        "cost_regret_mean": round(sum(costs) / len(costs), 3) if costs else None,
        "per_decision": per,
    }
    l6 = [d for d in decisions if d.get("layer") == "L6"]
    if l6:
        escalated = [d for d in l6 if d.get("chosen") == "escalate"]
        tokens = [
            measured[d["task_id"]]["escalate"].cost_per_attempt
            for d in escalated
            if isinstance(d.get("task_id"), str)
            and "escalate" in measured.get(d["task_id"], {})
            and measured[d["task_id"]]["escalate"].cost_per_attempt is not None
        ]
        out["escalation_rate"] = round(len(escalated) / len(l6), 6)
        spent = [t for t in tokens if t is not None]
        out["escalation_tokens"] = round(sum(spent), 3) if spent else None
    return out


def measured_from_rows(rows: Iterable[DecisionRow]) -> dict[str, dict[str, OptionEstimate]]:
    """Per task and option, the raw success rate (Wilson 95 %) and tokens of the rows: the
    ``measured`` input of ``regret`` (no pooling: regret is in hindsight)."""
    grouped: dict[str, dict[str, list[DecisionRow]]] = {}
    for row in rows:
        if row.success is None:
            continue
        grouped.setdefault(row.task_id, {}).setdefault(row.option, []).append(row)
    z = NormalDist().inv_cdf(0.975)
    out: dict[str, dict[str, OptionEstimate]] = {}
    for task, by_option in grouped.items():
        for option, items in by_option.items():
            n = len(items)
            s = sum(1 for r in items if r.success)
            tokens = [r.tokens for r in items if r.tokens is not None]
            cost = sum(tokens) / len(tokens) if tokens else None
            rate = s / n
            low, high = wilson(s, n, z)
            out.setdefault(task, {})[option] = OptionEstimate(
                option, n, s, round(rate, 6), (round(low, 6), round(high, 6)),
                round(cost, 3) if cost is not None else None,
                round(cost / rate, 3) if cost is not None and rate > 0 else None,
            )  # fmt: skip
    return out
