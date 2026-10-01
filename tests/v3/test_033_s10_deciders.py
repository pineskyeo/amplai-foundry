"""Work 033 S10: per-layer deciders (interfaces.md §6, §3.7, §2.5, IC-21, IC-22).

Contract read: §6.1 decision points, §6.2 decision method parts, §6.3 partial pooling, §6.4
selection
rule and priors, §6.5 rule-table fitting, §6.8 decision records; §2.2 `decider` / `decision_method`
components; §3.7 API. The tests state the contract with independent arithmetic (the Wilson score
interval and the pooled posterior are recomputed here, not read back from the module).

Real: store, `RuleTable`, `Decider`, `ComponentService`, `ManifestService.materialize`, the rc06
product rig with the S9 stand-ins (host-process agent, scripted read-only turns), the real
`LocalExecutionService.plan` / `approve`, the real `ExecutionLoop` and the protected verification.
Stand-ins (named, as in every rc06 test): the "container" runs a script on the host that writes the
files a call asks for. No driver, docker, provider or network is used.

What is proved here, in order: the constants of §6.1/§6.4; the pooled estimator and the shrink-back
rule; the selection rule; rows of a rule table (development split only, the cell it was fitted on);
the decision method parts as versioned components; and every decision point of a goal writes its
`harness-decision` (L1 at intake, L2/L3/L8 after the plan, L4/L5 at dispatch, L6 on failure, L7
before
the final verification) with the v1 priors reproducing today's behaviour exactly.

`hold`, `fault`, `build_world` and the record builders are shared with the other S10 test modules.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from amplai_foundry.meta_harness import deciders
from amplai_foundry.meta_harness.components import ComponentService
from amplai_foundry.meta_harness.deciders import (
    FEATURES,
    LAYERS,
    OPTIONS,
    POINTS,
    PRIORS,
    Decider,
    DecisionContext,
    DecisionRow,
    OptionEstimate,
    RuleTable,
    rows_from_trial_metrics,
    select_noninferior_cheapest,
    write_table,
)
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import new_id
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.execution import policies, releases
from amplai_foundry.runtime.execution.integration_queue import IntegrationQueue
from amplai_foundry.runtime.execution.product import AppConfig, VerifierCommand
from amplai_foundry.runtime.execution.strategy_runner import StrategyRunner
from e2e.test_033_s9_strategies import (
    SUITE,
    SUITE_DRAFT,
    VERIFIER,
    WRONG,
    Agents,
    Components,
    FakeTurns,
    World,
    always,
    outcomes,
    right,
    trial_context,
    wrong_first,
)
from rc06_rig import rig_with_codex, rig_with_two_drivers, submit

Ref = dict[str, Any]
CELL = "codex-cli"  # IC-07: the legacy cell id is the driver id
OTHER = "claude-cli"
Z = NormalDist().inv_cdf(0.975)
# a quick check: fails while the change still holds the marker `return 3`; the protected suite
# (SUITE) fails on the marker BAD, so the two judge different things
QUICK = (
    "python3",
    "-c",
    "import pathlib, sys; sys.exit(1 if 'return 3' in pathlib.Path('app.py').read_text() else 0)",
)
QUESTION_DRAFT = {**SUITE_DRAFT, "questions": ["Which file should change?"]}


def hold(code: str, fn: Any, *args: Any, **kwargs: Any) -> Hold:
    """`fn` is refused with a Hold of this public code."""
    with pytest.raises(Hold) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def fault(code: str, fn: Any, *args: Any, **kwargs: Any) -> RuntimeFault:
    """`fn` is refused with a RuntimeFault (not a Hold) of this public code."""
    with pytest.raises(RuntimeFault) as caught:
        fn(*args, **kwargs)
    assert not isinstance(caught.value, Hold)
    assert caught.value.code == code
    return caught.value


def wilson_ref(successes: float, n: float) -> tuple[float, float]:
    """The Wilson score interval, recomputed here (95 %): the contract of §6.3 step 2."""
    p = successes / n
    centre = p + Z * Z / (2 * n)
    spread = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    denominator = 1 + Z * Z / n
    return (centre - spread) / denominator, (centre + spread) / denominator


# ======================================================================================
# builders: records of a store (unit) and of a goal's world (integration)
# ======================================================================================
def rows(
    option: str,
    n: int,
    successes: int,
    features: dict[str, Any],
    *,
    tokens: int | None = 100,
    seconds: float | None = 1.0,
) -> list[DecisionRow]:
    """``n`` rows of one option, the first ``successes`` of them solved."""
    return [
        DecisionRow(f"{option}-{i}", dict(features), option, i < successes, tokens, seconds)
        for i in range(n)
    ]


class Env:
    """Components, tables, decisions and trial rows written into one deployment's store."""

    def __init__(self, deployment: Any) -> None:
        d = deployment
        self.store, self.scope = d.store, d.scope
        self.proposer = Actor("meta-proposer", d.scope, frozenset({"harness.propose"}), "service")
        self.components = ComponentService(d.store, d.scope)
        self.counter = 0

    def put(self, kind: str, object_id: str, value: dict[str, Any], revision: int = 1) -> Ref:
        with self.store.tx() as db:
            ref: Ref = self.store.put(db, self.scope, kind, object_id, revision, value)
        return ref

    def register(self, kind: str, name: str, content: dict[str, Any]) -> Ref:
        ref: Ref = self.components.register(
            self.proposer,
            component_id=f"{kind}.{name}",
            kind=kind,
            content=content,
            source="proposer",
            rationale="S10 test",
        )
        return ref

    def method(self, features: list[str], name: str | None = None, **over: Any) -> Ref:
        self.counter += 1
        content = {
            "features": features,
            "estimator": "pooled_beta_binomial_v1",
            "selection": "noninferior_then_cheapest_v1",
            "fallback": "prior_v1",
            "utility_lambda": None,
            **over,
        }
        return self.register("decision_method", name or f"m{self.counter}", content)

    def table(
        self,
        layer: str,
        data: list[DecisionRow],
        *,
        features: tuple[str, ...],
        options: tuple[str, ...],
        cells: tuple[str, ...] = (CELL,),
        method: Ref | None = None,
        pooling_strength: float = 4,
    ) -> Ref:
        table = RuleTable.fit(
            data,
            layer=layer,
            features=features,
            hierarchy=deciders.default_hierarchy(features),
            options=options,
            pooling_strength=pooling_strength,
        )
        source = {
            "corpus_ref": None,
            "trial_metrics_query": {"cells": list(cells), "since": None, "splits": ["development"]},
        }
        return write_table(self.store, self.scope, table, method_ref=method, source=source)

    def decider(
        self,
        layer: str,
        method: Ref,
        table: Ref | None = None,
        *,
        options: list[Ref] | None = None,
        judge: Ref | None = None,
        name: str | None = None,
        min_samples: int = 5,
        margin: float = 0.1,
        pooling_strength: float = 4,
    ) -> Ref:
        self.counter += 1
        content = {
            "layer": layer,
            "method": method,
            "table": table,
            "judge": judge,
            "options": options,
            "policy": {
                "min_samples": min_samples,
                "margin": margin,
                "pooling_strength": pooling_strength,
            },
        }
        return self.register("decider", name or f"{layer.lower()}{self.counter}", content)

    def content(self, ref: Ref) -> dict[str, Any]:
        content: dict[str, Any] = self.components.get(ref)["content"]
        return content

    def decide(
        self,
        ref: Ref | None,
        layer: str,
        options: tuple[str, ...],
        prior: str,
        features: dict[str, Any],
        *,
        cell: str = CELL,
        production: bool = True,
        ineligible: dict[str, str] | None = None,
    ) -> deciders.Decision:
        decider = Decider.of(self.store, self.scope, self.content(ref) if ref else None, ref=ref)
        return decider.decide(
            DecisionContext(
                layer=layer,
                cell_id=cell,
                features=features,
                options=options,
                prior=prior,
                subject={"goal_id": "g-unit"},
                production=production,
                ineligible=dict(ineligible or {}),
            )
        )

    def record(self, decision: deciders.Decision) -> dict[str, Any]:
        value: dict[str, Any] = self.store.get(self.scope, "harness-decision", decision.record_ref)
        return value

    def metrics(
        self,
        *,
        task: str,
        split: str = "development",
        cell: str = CELL,
        domain: str = "bug",
        success: bool = True,
        tokens: tuple[int | None, int | None] = (60, 40),
        strategy: str | None = None,
        source: str = "experiment",
        goal: str | None = None,
    ) -> Ref:
        """A trial and its ``trial-metrics`` row as their writers shape them (§2.10).
        ``goal`` points the trial's receipt at that goal's plan record (how a row finds the
        decisions its trial ran with)."""
        trial: dict[str, Any] = {"task_id": task, "success": success}
        if goal is not None:
            receipt = ArtifactStore(self.store).admit(
                self.scope,
                json.dumps({"goal_id": goal}).encode(),
                "application/json",
                trust="verifier",
            )
            trial["artifact_refs"] = [receipt]
        kind = "calibration-trial" if source == "calibration" else "eval-trial"
        trial_ref = self.put(kind, new_id("trial"), trial)
        value = {
            "schema": "amplai.trial-metrics.v1",
            "scope": self.scope.wire(),
            "trial_ref": trial_ref,
            "split": split,
            "cell_id": cell,
            "task_id": task,
            "domain": domain,
            "success": success,
            "strategy": strategy,
            "tokens": {"input": tokens[0], "output": tokens[1]},
            "wall_seconds": 2.0,
            ("calibration_plan_ref" if source == "calibration" else "experiment_ref"): {
                "id": "x",
                "revision": 1,
                "digest": "sha256:" + "0" * 64,
            },
        }
        return self.put("trial-metrics", new_id("tm"), value)

    def plan_head(
        self,
        goal_id: str,
        decisions: list[Ref],
        draft: dict[str, Any] | None = None,
        trial: dict[str, Any] | None = None,
    ) -> None:
        """A goal's ``execution-plan`` head holding the decisions its trial ran with (and the
        trial wire when given)."""
        record: dict[str, Any] = {"goal_id": goal_id, "decisions": decisions, "draft": draft}
        if trial is not None:
            record["trial"] = trial
        with self.store.tx() as db:
            self.store.cas(db, self.scope, "execution-plan", goal_id, 0, "approved", record)


def est(
    option: str,
    n: int,
    p: float,
    interval: tuple[float, float],
    per_solved: float | None,
    cost: float | None = None,
) -> OptionEstimate:
    """A hand-made estimate for the selection-rule tests."""
    return OptionEstimate(option, n, round(p * n), p, interval, cost, per_solved)


@pytest.fixture
def env(deployment: Any) -> Env:
    return Env(deployment)


# ======================================================================================
# 1. the constants of §6.1, §6.4, §3.7
# ======================================================================================
def test_layers_points_features_and_priors_are_the_spec_tables() -> None:
    assert LAYERS == ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8")
    assert POINTS == {
        "L1": "intake",
        "L2": "after_plan",
        "L3": "after_plan",
        "L8": "after_plan",
        "L4": "dispatch",
        "L5": "dispatch",
        "L6": "on_failure",
        "L7": "before_final_verification",
    }
    assert FEATURES == {
        "L1": ("questions", "in_scope_empty", "acceptance_items", "domain"),
        "L2": ("domain", "task_class", "planned_files", "acceptance_items", "apps", "risk"),
        "L3": ("task_class", "risk", "strategy"),
        "L4": ("strategy", "repo_files", "prior_failures"),
        "L5": ("cell", "strategy"),
        "L6": (
            "attempt",
            "failing_acceptance",
            "same_signature",
            "tokens_so_far",
            "remaining_fraction",
        ),
        "L7": ("changed_files", "quick_available"),
        "L8": ("task_class", "strategy"),
    }
    assert PRIORS == {
        "L1": "proceed_unless_questions",
        "L2": "repair_loop",
        "L3": "router_order",
        "L4": "manifest",
        "L5": "driver_defaults",
        "L6": "v1_attempt_policy",
        "L7": "none",
        "L8": "contract_defaults",
    }


def test_the_fixed_option_sets_of_l1_l6_and_l7() -> None:
    assert OPTIONS["L1"] == ("proceed", "ask_back", "replan_ask_first")
    assert OPTIONS["L6"] == ("retry_feedback", "retry_fresh", "escalate", "stop")
    assert OPTIONS["L7"] == ("none", "quick_checks")
    # L2/L3 options come from the manifest, L4 from the context parts, L5/L8 from the decider
    assert not {"L2", "L3", "L4", "L5", "L8"} & set(OPTIONS)


# ======================================================================================
# 2. partial pooling (§6.3)
# ======================================================================================
FEATURES_DT = ("domain", "task_class")


def pooled_table(m: float = 4) -> RuleTable:
    data = [
        *rows("A", 30, 29, {"domain": "bug", "task_class": "x"}),
        *rows("A", 10, 1, {"domain": "feature", "task_class": "x"}),
        *rows("B", 30, 15, {"domain": "bug", "task_class": "x"}),
        *rows("B", 10, 5, {"domain": "feature", "task_class": "x"}),
    ]
    return RuleTable.fit(
        data,
        layer="L2",
        features=FEATURES_DT,
        hierarchy=deciders.default_hierarchy(FEATURES_DT),
        options=("A", "B"),
        pooling_strength=m,
    )


def test_posterior_is_the_pooled_beta_binomial_of_the_spec() -> None:
    table = pooled_table(4)
    bucket, estimates = table.estimates_at({"domain": "bug", "task_class": "x"}, 1)
    assert bucket == ("domain=bug",) or (len(bucket) == 1 and "bug" in bucket[0])
    a = next(e for e in estimates if e.option == "A")
    # level 0: p0 = (s + m * 0.5) / (n + m) over A's 40 rows (30 solved); level 1 pools toward p0
    p0 = (30 + 4 * 0.5) / (40 + 4)
    effective = 29 + 4 * p0
    p1 = effective / (30 + 4)
    assert a.n == 30 and a.successes == 29
    assert a.posterior_success == pytest.approx(p1, abs=1e-6)
    low, high = wilson_ref(effective, 30 + 4)  # Wilson on the effective counts
    assert a.interval == pytest.approx((low, high), abs=1e-5)
    assert a.cost_per_attempt == pytest.approx(100.0)
    assert a.cost_per_solved == pytest.approx(100.0 / p1, abs=1e-2)  # cost per attempt / p
    # B: p0 = (20 + 2) / 44 = 0.5; the bug bucket is (15 + 4 * 0.5) / 34
    b = next(e for e in estimates if e.option == "B")
    assert b.posterior_success == pytest.approx((15 + 4 * 0.5) / 34, abs=1e-6)


def test_the_global_level_starts_from_one_half() -> None:
    table = pooled_table(4)
    bucket, estimates = table.estimates_at({"domain": "bug", "task_class": "x"}, 0)
    assert bucket == ()
    a = next(e for e in estimates if e.option == "A")
    assert a.posterior_success == pytest.approx((30 + 4 * 0.5) / (40 + 4), abs=1e-6)
    assert a.n == 40 and a.successes == 30


def test_pooling_strength_zero_is_the_raw_rate_and_a_bucket_without_rows_takes_its_parent() -> None:
    raw = pooled_table(0)
    _bucket, estimates = raw.estimates_at({"domain": "bug", "task_class": "x"}, 1)
    assert next(e for e in estimates if e.option == "A").posterior_success == pytest.approx(29 / 30)
    # a bucket with no rows of its own: (0 + m * p_parent) / (0 + m) = the parent posterior
    unseen = pooled_table(4)
    bucket, estimates = unseen.estimates_at({"domain": "refactor", "task_class": "x"}, 1)
    assert "refactor" in bucket[0]
    a = next(e for e in estimates if e.option == "A")
    assert a.n == 0
    assert a.posterior_success == pytest.approx((30 + 4 * 0.5) / (40 + 4), abs=1e-6)


def test_the_decision_uses_the_finest_credible_level_and_shrinks_back_otherwise() -> None:
    table = pooled_table(4)
    features = {"domain": "bug", "task_class": "x"}
    # level 2 (domain, task_class) has the same rows as level 1 and no interval excludes its
    # parent's posterior, so the finest credible level is level 1: the domain bucket
    bucket, estimates = table.estimates(features, min_samples=5)
    assert len(bucket) == 1 and "bug" in bucket[0]
    assert {e.option for e in estimates} == {"A", "B"}
    # no option has 40 rows in the domain bucket: it shrinks back to the global level
    bucket, estimates = table.estimates(features, min_samples=40)
    assert bucket == ()
    assert all(e.n == 40 for e in estimates)
    # a bucket where every interval contains the parent's posterior is not credible either
    flat = RuleTable.fit(
        [
            *rows("A", 20, 10, {"domain": "bug", "task_class": "x"}),
            *rows("B", 20, 10, {"domain": "bug", "task_class": "x"}),
        ],
        layer="L2",
        features=FEATURES_DT,
        hierarchy=deciders.default_hierarchy(FEATURES_DT),
        options=("A", "B"),
        pooling_strength=4,
    )
    bucket, _ = flat.estimates({"domain": "bug", "task_class": "x"}, min_samples=5)
    assert bucket == ()  # pooled: the specialised bucket shows no credible difference


def test_a_feature_missing_from_the_context_stops_at_the_levels_it_reaches() -> None:
    table = pooled_table(4)
    bucket, _ = table.estimates({"domain": "bug"}, min_samples=5)  # no task_class
    assert len(bucket) <= 1
    assert table.levels({"domain": "bug"}) == 2  # global and domain
    assert table.levels({"domain": "bug", "task_class": "x"}) == 3


def test_a_table_record_round_trips_and_marks_the_interval_as_an_approximation(env: Env) -> None:
    table = pooled_table(4)
    value = table.record(
        method_ref=None,
        source={
            "corpus_ref": None,
            "trial_metrics_query": {"cells": [CELL], "since": None, "splits": ["development"]},
        },
    )
    assert value["schema"] == "amplai.decider-table.v1" and value["layer"] == "L2"
    assert value["pooling_strength"] == 4 and value["source"]["rows"] == 80
    assert "approximation" in value["interval_method"]  # recorded as such (§6.3 step 2)
    assert value["hierarchy"] == [[], ["domain"], ["domain", "task_class"]]
    entry = next(
        c for c in value["cells"] if c["bucket"] == {"domain": "bug"} and c["option"] == "A"
    )
    assert entry["n"] == 30 and entry["successes"] == 29 and entry["pooled_from"] == [{}]
    assert entry["cost_tokens_mean"] == 100.0
    again = RuleTable.from_record({**value, "scope": env.scope.wire()})
    features = {"domain": "bug", "task_class": "x"}
    assert again.estimates(features, min_samples=5) == table.estimates(features, min_samples=5)


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"layer": "L9"}, "layer"),
        ({"features": ("domain", "domain")}, "distinct"),
        ({"features": ("domain", "not_a_feature")}, "features"),
        ({"hierarchy": ((), ("domain",), ("task_class",))}, "level"),  # not nested
        ({"hierarchy": (("domain",),)}, "global"),  # must start with []
        ({"options": ()}, "options"),
        ({"options": ("A", "A")}, "options"),
        ({"pooling_strength": 101}, "pooling"),
        ({"pooling_strength": -1}, "pooling"),
        ({"confidence": 1.0}, "confidence"),
        ({"confidence": 0.0}, "confidence"),
    ],
)
def test_a_malformed_table_shape_is_refused(override: dict[str, Any], why: str) -> None:
    kwargs: dict[str, Any] = {
        "layer": "L2",
        "features": FEATURES_DT,
        "hierarchy": deciders.default_hierarchy(FEATURES_DT),
        "options": ("A", "B"),
        "pooling_strength": 4,
    }
    kwargs.update(override)
    err = fault("DECIDER_TABLE", RuleTable.fit, [], **kwargs)
    assert why in " ".join([err.message, str(err.details)])


# ======================================================================================
# 3. the selection rule (§6.4)
# ======================================================================================
RANK = {"A": 1, "B": 2, "C": 3}


def pick(estimates: list[OptionEstimate], **over: Any) -> str | None:
    kwargs = {"margin": 0.1, "min_samples": 5, "rank": RANK, **over}
    return select_noninferior_cheapest(estimates, **kwargs)


def test_the_cheapest_option_per_solved_task_wins_among_the_not_credibly_worse() -> None:
    best = est("A", 20, 0.90, (0.80, 0.95), 120.0)
    cheap = est("B", 20, 0.88, (0.70, 0.96), 100.0)  # upper 0.96 >= 0.90 - 0.1: not credibly worse
    assert pick([best, cheap]) == "B"


def test_a_credibly_worse_option_is_dropped_however_cheap() -> None:
    best = est("A", 20, 0.90, (0.80, 0.95), 120.0)
    cheap_but_worse = est("B", 20, 0.50, (0.30, 0.65), 10.0)  # upper 0.65 < 0.90 - 0.1
    assert pick([best, cheap_but_worse]) == "A"
    # the margin moves the line: with a wide margin the cheap option is no longer credibly worse
    assert pick([best, cheap_but_worse], margin=0.5) == "B"


def test_costs_within_one_percent_tie_to_the_simpler_option() -> None:
    a = est("A", 20, 0.9, (0.8, 0.95), 100.9)  # rank 1
    b = est("B", 20, 0.9, (0.8, 0.95), 100.0)  # rank 2, cheapest
    assert pick([b, a]) == "A"  # 100.9 <= 100.0 * 1.01: a tie, the lower rank wins
    c = est("A", 20, 0.9, (0.8, 0.95), 101.5)
    assert pick([b, c]) == "B"  # 1.5 % apart: not a tie


def test_an_option_below_min_samples_is_never_chosen_and_none_eligible_gives_none() -> None:
    thin = est("C", 2, 0.99, (0.80, 1.0), 1.0)  # the cheapest and highest posterior, but n = 2
    thick = est("A", 30, 0.8, (0.65, 0.9), 150.0)
    assert pick([thin, thick]) == "A"
    assert pick([thin]) is None  # the caller falls back to the prior
    assert pick([]) is None
    assert pick([thick], min_samples=31) is None


def test_the_best_is_the_highest_posterior_not_the_highest_raw_rate() -> None:
    # the interval of the thinner option sits above 0.7; B is cheaper and not credibly worse
    a = est("A", 40, 0.80, (0.66, 0.89), 200.0)
    b = est("B", 40, 0.75, (0.60, 0.86), 100.0)
    assert pick([a, b]) == "B"


# ======================================================================================
# 4. rows of a rule table (§6.5): development only, the cell it was fitted on
# ======================================================================================
def test_rows_come_from_development_trials_of_the_named_cells_only(env: Env) -> None:
    env.metrics(task="d-1", strategy="single")  # development, experiment source
    env.metrics(task="d-2", strategy="repair_loop", source="calibration")  # development calibration
    env.metrics(task="v-1", split="validation", strategy="single", source="calibration")
    env.metrics(task="v-2", split="validation", strategy="single")
    env.metrics(task="h-1", split="holdout", strategy="single")
    env.metrics(task="o-1", cell=OTHER, strategy="single")  # another cell
    got = rows_from_trial_metrics(env.store, env.scope, layer="L2", cells=[CELL])
    assert sorted(r.task_id for r in got) == ["d-1", "d-2"]
    assert {r.option for r in got} == {"single", "repair_loop"}
    assert all(r.tokens == 100 and r.seconds == 2.0 for r in got)  # input + output tokens
    # two cells: the other cell's development trial is now a row too
    both = rows_from_trial_metrics(env.store, env.scope, layer="L2", cells=[CELL, OTHER])
    assert sorted(r.task_id for r in both) == ["d-1", "d-2", "o-1"]


def test_validation_calibration_rows_never_reach_a_table(env: Env) -> None:
    """Calibration also runs validation tasks (§2.8); the row's own split decides (§6.5 step 1)."""
    for i in range(6):
        env.metrics(task=f"cal-{i}", split="validation", source="calibration", strategy="single")
    assert rows_from_trial_metrics(env.store, env.scope, layer="L2", cells=[CELL]) == []


@pytest.mark.parametrize(
    "splits", [("validation",), ("holdout",), ("development", "validation"), ()]
)
def test_asking_for_validation_or_holdout_rows_holds_decider_split(
    env: Env, splits: tuple[str, ...]
) -> None:
    env.metrics(task="d-1", strategy="single")
    held = hold(
        "DECIDER_SPLIT",
        rows_from_trial_metrics,
        env.store,
        env.scope,
        layer="L2",
        cells=[CELL],
        splits=splits,
    )
    assert held.code == "DECIDER_SPLIT"


def test_an_unknown_layer_is_a_fault(env: Env) -> None:
    fault("DECIDER_LAYER", rows_from_trial_metrics, env.store, env.scope, layer="L9", cells=[CELL])


def test_l2_rows_without_a_known_strategy_and_other_layers_without_a_decision_are_skipped(
    env: Env,
) -> None:
    env.metrics(task="d-1", strategy=None)
    assert rows_from_trial_metrics(env.store, env.scope, layer="L2", cells=[CELL]) == []
    env.metrics(task="d-2", strategy="single")
    # L6 has no option a trial could have run with unless a decision says so
    assert rows_from_trial_metrics(env.store, env.scope, layer="L6", cells=[CELL]) == []
    # L3: the option is the cell the trial ran on
    cells = rows_from_trial_metrics(env.store, env.scope, layer="L3", cells=[CELL])
    assert [r.option for r in cells] == [CELL, CELL]


def test_a_row_takes_the_option_and_features_its_trial_ran_with(env: Env) -> None:
    """The option of a row is the value its goal's decision chose (§6.5 step 1)."""
    decision = env.decide(
        None,
        "L4",
        ("none", "env_bootstrap"),
        "none",
        {"strategy": "repair_loop"},
    )
    env.plan_head("goal-row-1", [decision.record_ref])
    env.metrics(task="d-1", goal="goal-row-1", success=True)
    got = rows_from_trial_metrics(env.store, env.scope, layer="L4", cells=[CELL])
    assert len(got) == 1
    assert got[0].option == "none" and got[0].features == {"strategy": "repair_loop"}
    assert got[0].success is True


ROW_DRAFT = {
    "task_class": "logic_change",
    "risk": "low",
    "in_scope": ["app.py"],
    "acceptance": [{"statement": "value() returns 2", "verifier": "suite"}],
}


def test_a_row_has_the_domain_its_decision_point_saw_not_the_trial_metrics_domain(
    env: Env,
) -> None:
    """§6.1, IC-23: L1/L2 read ``domain``; a trial's decisions see ``TrialContext.domain`` and a
    goal's see ``UNKNOWN_DOMAIN``. A row keeps the features its decision recorded; a row without a
    decision gets the features the point would have computed, with its plan's trial domain (or
    ``unknown`` for a wire without one). The trial-metrics ``domain`` never replaces them, else
    the fitted domain buckets are ones no decision looks up."""
    from amplai_foundry.runtime.execution import product

    assert deciders.UNKNOWN_DOMAIN == product.UNKNOWN_DOMAIN == "unknown"
    seen = deciders.after_plan_features("L2", ROW_DRAFT, domain="bug", apps=1, strategy=None)
    decision = env.decide(None, "L2", ("repair_loop", "single"), "repair_loop", seen)
    env.plan_head("goal-dom-1", [decision.record_ref], draft=ROW_DRAFT)
    # the trial-metrics row says "feature": the recorded decision features win
    env.metrics(task="dom-1", goal="goal-dom-1", domain="feature", strategy="repair_loop")
    # a trial from before S10 (no decision) whose plan carries an IC-23 trial wire
    env.plan_head("goal-dom-2", [], draft=ROW_DRAFT, trial=trial_context(domain="bug").wire())
    env.metrics(task="dom-2", goal="goal-dom-2", domain="feature", strategy="single")
    # and one whose trial wire predates IC-23 (no domain): unknown
    old_wire = {k: v for k, v in trial_context().wire().items() if k != "domain"}
    env.plan_head("goal-dom-3", [], draft=ROW_DRAFT, trial=old_wire)
    env.metrics(task="dom-3", goal="goal-dom-3", domain="feature", strategy="single")
    got = {
        r.task_id: r
        for r in rows_from_trial_metrics(env.store, env.scope, layer="L2", cells=[CELL])
    }
    assert got["dom-1"].option == "repair_loop" and got["dom-1"].features == seen
    assert got["dom-2"].option == "single" and got["dom-2"].features == seen
    unknown = deciders.after_plan_features(
        "L2", ROW_DRAFT, domain=deciders.UNKNOWN_DOMAIN, apps=1, strategy=None
    )
    assert got["dom-3"].features == unknown and unknown["domain"] == "unknown"


@pytest.mark.parametrize("bad", ["", "Bug", "bug-x", 3, None, "x" * 65])
def test_a_trial_context_domain_is_a_short_lowercase_id(bad: Any) -> None:
    fault("TRIAL_CONTEXT", trial_context, domain=bad)
    assert trial_context().domain == "unknown"  # IC-23: a case that names none
    assert trial_context(domain="cli_ops").wire()["domain"] == "cli_ops"


def test_write_table_stores_a_content_addressed_development_table(env: Env) -> None:
    method = env.method(["domain"])
    data = [*rows("A", 12, 10, {"domain": "bug"}), *rows("B", 12, 6, {"domain": "bug"})]
    ref = env.table("L2", data, features=("domain",), options=("A", "B"), method=method)
    value = env.store.get(env.scope, "decider-table", ref)
    assert ref["id"].startswith("table-L2-") and len(ref["id"]) == len("table-L2-") + 24
    assert value["source"]["trial_metrics_query"]["splits"] == ["development"]
    assert value["source"]["rows"] == 24 and value["method_ref"] == method
    again = env.table("L2", data, features=("domain",), options=("A", "B"), method=method)
    assert again == ref  # the same fit gives the same record
    changed = env.table("L2", data[:-1], features=("domain",), options=("A", "B"), method=method)
    assert changed["id"] != ref["id"]


def test_a_table_that_names_validation_or_holdout_is_refused(env: Env) -> None:
    table = RuleTable.fit(
        rows("A", 3, 3, {"domain": "bug"}),
        layer="L2",
        features=("domain",),
        hierarchy=deciders.default_hierarchy(("domain",)),
        options=("A",),
        pooling_strength=4,
    )
    for splits in (["validation"], ["development", "holdout"]):
        source = {
            "corpus_ref": None,
            "trial_metrics_query": {"cells": [CELL], "since": None, "splits": splits},
        }
        with pytest.raises(RuntimeFault) as caught:
            write_table(env.store, env.scope, table, method_ref=None, source=source)
        assert caught.value.code in {"DECIDER_SPLIT", "DECIDER_TABLE"}
    assert list(env.store.list_objects(env.scope, "decider-table")) == []


# ======================================================================================
# 5. the Decider (§3.7, §6.2, §6.4, §6.8)
# ======================================================================================
FEATS = {"domain": "bug"}


def l2_decider(
    env: Env,
    *,
    cells: tuple[str, ...] = (CELL,),
    method_over: dict[str, Any] | None = None,
    a: tuple[int, int, int] = (20, 20, 200),
    b: tuple[int, int, int] = (20, 20, 100),
    elsewhere: bool = True,
    **policy: Any,
) -> Ref:
    """An L2 decider over `repair_loop` (a) and `single` (b); (n, successes, tokens) each."""
    method = env.method(["domain"], **(method_over or {}))
    poor = {"domain": "feature"}  # a poor domain for both, so the bug bucket stands out
    data = [
        *rows("repair_loop", a[0], a[1], FEATS, tokens=a[2]),
        *rows("single", b[0], b[1], FEATS, tokens=b[2]),
        *(rows("repair_loop", 30, 6, poor, tokens=a[2]) if elsewhere else []),
        *(rows("single", 30, 6, poor, tokens=b[2]) if elsewhere else []),
    ]
    table = env.table(
        "L2",
        data,
        features=("domain",),
        options=("repair_loop", "single"),
        cells=cells,
        method=method,
    )
    return env.decider("L2", method, table, **policy)


def test_the_cheapest_equal_option_is_chosen_and_the_decision_record_is_complete(env: Env) -> None:
    ref = l2_decider(env)
    decision = env.decide(ref, "L2", ("repair_loop", "single"), "repair_loop", FEATS)
    assert decision.option == "single" and decision.used_prior is False
    assert [e.option for e in decision.estimates] == ["repair_loop", "single"]
    assert len(decision.bucket) == 1 and "bug" in decision.bucket[0]
    value = env.record(decision)
    content = env.content(ref)
    assert value["schema"] == "amplai.harness-decision.v1"
    assert (value["layer"], value["point"], value["cell_id"]) == ("L2", "after_plan", CELL)
    assert value["subject"] == {"goal_id": "g-unit"} and value["features"] == FEATS
    assert value["decider_ref"] == ref and value["method_ref"] == content["method"]
    assert value["table_ref"] == content["table"] and value["judge_call_ref"] is None
    assert value["chosen"] == "single" and value["used_prior"] is False
    assert value["prior"] == PRIORS["L2"] and value["bucket"] == list(decision.bucket)
    by_option = {o["option"]: o for o in value["options"]}
    assert set(by_option) == {"repair_loop", "single"}
    assert by_option["single"]["n"] == 20 and by_option["single"]["eligible"] is True
    assert by_option["single"]["cost_per_solved"] < by_option["repair_loop"]["cost_per_solved"]
    assert by_option["single"]["interval"][0] < by_option["single"]["posterior_success"]
    assert by_option["single"]["why"]  # the chosen option says why it was chosen
    assert all(isinstance(o["why"], str) for o in value["options"])


def test_the_v1_prior_is_taken_with_a_record_at_every_layer(env: Env) -> None:
    """No decider component: the prior (the manifest as written) is the decision (§6.4)."""
    cases = {
        "L1": (OPTIONS["L1"], "proceed"),
        "L2": (("repair_loop", "single"), "repair_loop"),
        "L3": ((CELL, OTHER), CELL),
        "L4": (("none", "env_bootstrap"), "env_bootstrap"),
        "L5": (("driver_options.a@1",), "driver_defaults"),
        "L6": (OPTIONS["L6"], "retry_feedback"),
        "L7": (OPTIONS["L7"], "none"),
        "L8": (("limits.a@1",), "limits.a@1"),
    }
    for layer, (options, prior) in cases.items():
        decision = env.decide(None, layer, options, prior, {})
        value = env.record(decision)
        assert decision.option == prior and decision.used_prior is True, layer
        assert value["chosen"] == prior and value["prior"] == PRIORS[layer], layer
        assert value["decider_ref"] is None and value["method_ref"] is None, layer
        assert value["table_ref"] is None and value["bucket"] == [], layer
        assert value["point"] == POINTS[layer] and value["layer"] == layer, layer


def test_a_table_fitted_on_another_cell_is_not_used(env: Env) -> None:
    ref = l2_decider(env, cells=(OTHER,))
    here = env.decide(ref, "L2", ("repair_loop", "single"), "repair_loop", FEATS, cell=CELL)
    assert here.option == "repair_loop" and here.used_prior is True  # the prior
    value = env.record(here)
    assert (
        value["table_ref"] is not None
    )  # the decider names its table, the decision did not use it
    chosen = next(o for o in value["options"] if o["option"] == "repair_loop")
    assert "cell" in chosen["why"]  # the record says why
    # the cell it was fitted on uses it
    there = env.decide(ref, "L2", ("repair_loop", "single"), "repair_loop", FEATS, cell=OTHER)
    assert there.option == "single" and there.used_prior is False


def test_a_table_of_another_layer_is_not_used_and_check_names_it(env: Env) -> None:
    method = env.method(["task_class"])
    table = env.table(
        "L3",
        rows("a", 20, 20, {"task_class": "x"}),
        features=("task_class",),
        options=("a", "b"),
        method=method,
    )
    ref = env.decider("L2", method, table)
    decision = env.decide(ref, "L2", ("repair_loop", "single"), "repair_loop", FEATS)
    assert decision.used_prior is True
    problems = deciders.check(env.store, env.scope, "L2", env.content(ref))
    assert any("fitted for L3" in p for p in problems)
    assert deciders.check(env.store, env.scope, "L2", None) == []
    assert deciders.check(env.store, env.scope, "L3", env.content(ref))[0].endswith("is for L2")


def test_prior_below_min_samples_and_a_production_goal_never_explores(env: Env) -> None:
    # `single` looks perfect but has 3 trials; `repair_loop` has 30
    ref = l2_decider(env, a=(30, 24, 300), b=(3, 3, 10), min_samples=5, elsewhere=False)
    decision = env.decide(ref, "L2", ("repair_loop", "single"), "repair_loop", FEATS)
    assert decision.option == "repair_loop"  # the only option with min_samples; never `single`
    thin = {e.option: e.n for e in decision.estimates}
    assert thin["single"] == 3
    why = {o["option"]: o["why"] for o in env.record(decision)["options"]}
    assert "min_samples" in why["single"]
    # nothing reaches min_samples: the prior, and it is recorded as the prior
    sparse = l2_decider(env, a=(3, 3, 300), b=(3, 3, 10), min_samples=5, elsewhere=False)
    prior = env.decide(sparse, "L2", ("repair_loop", "single"), "repair_loop", FEATS)
    assert prior.option == "repair_loop" and prior.used_prior is True
    assert env.record(prior)["chosen"] == "repair_loop"


def test_an_option_the_goal_cannot_run_is_never_chosen(env: Env) -> None:
    ref = l2_decider(env)
    decision = env.decide(
        ref,
        "L2",
        ("repair_loop", "single"),
        "repair_loop",
        FEATS,
        ineligible={"single": "needs an auxiliary turn"},
    )
    assert decision.option == "repair_loop"
    value = env.record(decision)
    single = next(o for o in value["options"] if o["option"] == "single")
    assert single["eligible"] is False and single["why"] == "needs an auxiliary turn"


def test_selection_parts_max_success_and_utility(env: Env) -> None:
    # A: 100 % at 200 tokens; B: 80 % at 20 tokens (both measured, n = 20)
    method_a = {"fallback": "prior_v1"}
    data = [
        *rows("repair_loop", 20, 20, FEATS, tokens=200_000),
        *rows("single", 20, 16, FEATS, tokens=20_000),
    ]
    opts = ("repair_loop", "single")
    chosen = {}
    for name, over in {
        "max": {"selection": "max_success_v1", **method_a},
        "util_cheap": {"selection": "utility_v1", "utility_lambda": 1.0},
        "util_free": {"selection": "utility_v1", "utility_lambda": 0.0},
    }.items():
        method = env.method(["domain"], **over)
        table = env.table("L2", data, features=("domain",), options=opts, method=method)
        decider = env.decider("L2", method, table, margin=0.5)
        chosen[name] = env.decide(decider, "L2", opts, "single", FEATS).option
    assert chosen["max"] == "repair_loop"  # the highest posterior, whatever it costs
    # utility = success - lambda * tokens / 10^6: A pays 0.2 for its tokens, B 0.02, so a weight of
    # 1
    # prefers B's lower success; a zero weight is max_success
    assert chosen["util_cheap"] == "single"
    assert chosen["util_free"] == "repair_loop"


def test_a_thin_bucket_shrinks_back_to_the_global_level_under_either_fallback(env: Env) -> None:
    """The finest bucket (bug, x) holds 2 rows per option; the global level holds 32. §6.3 step 3
    shrinks back to the level where every compared option has min_samples; the fallback part
    (`prior_v1` or `coarser_bucket_v1`) only matters when no level answers."""
    features = ("domain", "task_class")
    data = [
        *rows("repair_loop", 30, 24, {"domain": "feature", "task_class": "y"}, tokens=200),
        *rows("single", 30, 24, {"domain": "feature", "task_class": "y"}, tokens=100),
        *rows("repair_loop", 2, 2, {"domain": "bug", "task_class": "x"}, tokens=200),
        *rows("single", 2, 2, {"domain": "bug", "task_class": "x"}, tokens=100),
    ]
    opts = ("repair_loop", "single")
    for fallback in ("prior_v1", "coarser_bucket_v1"):
        method = env.method(list(features), fallback=fallback)
        table = env.table("L2", data, features=features, options=opts, method=method)
        decider = env.decider("L2", method, table, min_samples=10)
        decision = env.decide(
            decider, "L2", opts, "repair_loop", {"domain": "bug", "task_class": "x"}
        )
        assert decision.option == "single" and decision.used_prior is False, fallback
        assert decision.bucket == (), fallback  # pooled: the global level answered
        assert all(e.n == 32 for e in decision.estimates), fallback
    # when no level reaches min_samples every fallback ends at the prior
    for fallback in ("prior_v1", "coarser_bucket_v1"):
        method = env.method(list(features), fallback=fallback, name=f"thin{fallback}")
        table = env.table("L2", data, features=features, options=opts, method=method)
        decider = env.decider("L2", method, table, min_samples=100, name=f"thin{fallback[:5]}")
        decision = env.decide(
            decider, "L2", opts, "repair_loop", {"domain": "bug", "task_class": "x"}
        )
        assert decision.option == "repair_loop" and decision.used_prior is True, fallback


def test_a_decision_context_is_checked(env: Env) -> None:
    fault("DECIDER_CONTEXT", env.decide, None, "L9", ("a",), "a", {})
    fault("DECIDER_CONTEXT", env.decide, None, "L2", (), "repair_loop", {})
    fault("DECIDER_CONTEXT", env.decide, None, "L2", ("a",), "", {})


# ======================================================================================
# 6. decision method parts are versioned components (§6.2, §2.2)
# ======================================================================================
def test_method_parts_are_versioned_components_and_a_new_feature_list_is_a_new_version(
    env: Env,
) -> None:
    v1 = env.method(["domain"], name="shared")
    v2 = env.method(["domain", "task_class"], name="shared")
    v3 = env.method(
        ["domain", "task_class"],
        name="shared",
        estimator="pooled_beta_binomial_v1",
        selection="max_success_v1",
        fallback="coarser_bucket_v1",
    )
    assert (v1["revision"], v2["revision"], v3["revision"]) == (1, 2, 3)
    assert (
        env.method(
            ["domain", "task_class"],
            name="shared",
            selection="max_success_v1",
            fallback="coarser_bucket_v1",
        )
        == v3
    )  # same content: the same version
    first, second = env.components.get(v1), env.components.get(v2)
    assert first["kind"] == second["kind"] == "decision_method"
    assert first["content"]["features"] == ["domain"] and first["surface_class"] == "B"
    assert second["content"]["features"] == ["domain", "task_class"]
    parts = env.components.get(v3)["content"]
    assert (parts["estimator"], parts["selection"], parts["fallback"]) == (
        "pooled_beta_binomial_v1",
        "max_success_v1",
        "coarser_bucket_v1",
    )
    # each part is its own switch: a decider pinned to v1 keeps v1's features
    pinned = env.decider("L2", v1)
    decision = env.decide(pinned, "L2", ("repair_loop", "single"), "repair_loop", {"domain": "bug"})
    assert env.record(decision)["method_ref"] == v1


@pytest.mark.parametrize(
    "bad",
    [
        {"estimator": "bayes"},
        {"selection": "cheapest"},
        {"fallback": "guess"},
        {"features": []},
        {"features": ["domain", "domain"]},
        {"features": ["not_a_feature"]},
        {"utility_lambda": 0.5},  # only utility_v1 has a weight
        {"selection": "utility_v1"},  # and utility_v1 needs it
        {"selection": "utility_v1", "utility_lambda": -1.0},
        {"surprise": 1},
    ],
)
def test_an_invalid_method_is_refused_with_component_content(env: Env, bad: dict[str, Any]) -> None:
    content = {
        "features": ["domain"],
        "estimator": "pooled_beta_binomial_v1",
        "selection": "noninferior_then_cheapest_v1",
        "fallback": "prior_v1",
        "utility_lambda": None,
        **bad,
    }
    fault("COMPONENT_CONTENT", env.register, "decision_method", "bad", content)


def test_a_component_id_must_carry_its_kind(env: Env) -> None:
    content = copy.deepcopy(policies.V1["limits"])
    fault(
        "COMPONENT_ID",
        env.components.register,
        env.proposer,
        component_id="decider.x",
        kind="limits",
        content=content,
        source="proposer",
        rationale="r",
    )
    fault(
        "COMPONENT_KIND",
        env.components.register,
        env.proposer,
        component_id="nokind.x",
        kind="nokind",
        content={},
        source="proposer",
        rationale="r",
    )


def test_decider_content_is_validated(env: Env) -> None:
    method = env.method(["domain"])
    base = {
        "layer": "L2",
        "method": method,
        "table": None,
        "judge": None,
        "options": None,
        "policy": {"min_samples": 5, "margin": 0.1, "pooling_strength": 4},
    }

    def attempt(**over: Any) -> None:
        env.register("decider", "probe", {**copy.deepcopy(base), **over})

    attempt()  # valid
    fault("COMPONENT_CONTENT", attempt, layer="L9")
    fault("COMPONENT_CONTENT", attempt, options=[method])  # options only at L5 and L8
    fault("COMPONENT_CONTENT", attempt, layer="L5", options=None)  # required there
    fault("COMPONENT_CONTENT", attempt, layer="L8", options=[])
    fault(
        "COMPONENT_CONTENT",
        attempt,
        policy={"min_samples": 0, "margin": 0.1, "pooling_strength": 4},
    )
    fault(
        "COMPONENT_CONTENT",
        attempt,
        policy={"min_samples": 5, "margin": 0.6, "pooling_strength": 4},
    )
    fault(
        "COMPONENT_CONTENT",
        attempt,
        policy={"min_samples": 5, "margin": 0.1, "pooling_strength": 101},
    )
    fault("COMPONENT_CONTENT", attempt, method={"id": "nope", "revision": 1, "digest": "sha256:0"})
    fault("COMPONENT_CONTENT", attempt, method=env.register("limits", "x", policies.V1["limits"]))
    fault("COMPONENT_CONTENT", attempt, table=method)  # a table names a decider-table record


def test_check_refuses_parts_that_cannot_run(env: Env) -> None:
    foreign = env.decider("L2", env.method(["attempt"]), name="foreign")  # an L6 feature at L2
    assert any(
        "attempt" in p for p in deciders.check(env.store, env.scope, "L2", env.content(foreign))
    )
    judged = env.method(["questions"], estimator="judge_v1")
    outside = env.decider("L2", judged, name="outside")  # a judge estimator outside L1
    assert any(
        "L1 only" in p for p in deciders.check(env.store, env.scope, "L2", env.content(outside))
    )
    # at L1 it needs a yes/no judge: none configured, or Jev (not configured, §14 Q9)
    none_judge = env.decider("L1", judged, name="nojudge")
    assert any(
        "yes_no" in p for p in deciders.check(env.store, env.scope, "L1", env.content(none_judge))
    )
    jev = env.register("judge_model", "jevj", {"judge": "jev", "question_types": ["yes_no"]})
    with_jev = env.decider("L1", judged, judge=jev, name="withjev")
    assert any(
        "Jev" in p for p in deciders.check(env.store, env.scope, "L1", env.content(with_jev))
    )


def test_a_decider_of_one_layer_does_not_fit_another_slot(env: Env) -> None:
    from amplai_foundry.meta_harness.manifest import Manifest, ManifestService

    manifests = ManifestService(env.store, env.scope, None, env.components)
    l2 = env.decider("L2", env.method(["domain"]), name="slotcheck")
    empty = Manifest(l2, {}, {}, {})  # the carriers do not matter: the slot is refused first
    manifests.change(empty, L2=l2)  # its own layer
    for slot in ("L3", "L6"):
        with pytest.raises(RuntimeFault) as caught:
            manifests.change(empty, **{slot: l2})
        assert caught.value.code == "MANIFEST_SLOT", slot


# ======================================================================================
# 7. decision points of a goal (§6.1): one harness-decision per point, v1 priors exact
# ======================================================================================
def build_world(
    deployment: Any,
    tmp_path: Path,
    script: Any = right,
    *,
    quick: bool = False,
    quick_argv: tuple[str, ...] = QUICK,
    two: bool = False,
    draft: dict[str, Any] | None = None,
) -> World:
    """The S9 world (rc06 rig, host-process agent, scripted read-only turns) with the protected
    suite of that file, an optional quick verifier (L7) and optionally the Claude cell too."""
    if two:
        rig, loop, container, claude_box, _planner = rig_with_two_drivers(deployment, tmp_path)
    else:
        rig, loop, container = rig_with_codex(deployment, tmp_path, "right")
        claude_box = None
    verifiers = [VerifierCommand(VERIFIER, SUITE, "no BAD marker in a .py file", 60)]
    if quick:
        verifiers.append(VerifierCommand("quick", quick_argv, "no `return 3` in app.py", 30))
    rig.service.install(
        AppConfig("app", rig.repo, tuple(verifiers), quick_verifiers=("quick",) if quick else ())
    )
    rig.planner.draft_value = copy.deepcopy(draft or SUITE_DRAFT)
    turns = FakeTurns()
    runner = StrategyRunner(rig.service, turns, IntegrationQueue(rig.workspaces, rig.d.scope))
    rig.service.strategies = runner
    agents = Agents(container, script)
    agents.order = turns.order
    world = World(rig, loop, container, agents, turns, Components(rig), runner)
    world.extra["claude_box"] = claude_box
    return world


def candidate(w: World, **slots: Ref | None) -> Ref:
    """A composition derived from the base with these manifest slots (component refs)."""
    comps = w.comps
    manifest = comps.manifests.change(comps.manifests.of_composition(comps.base_ref), **slots)
    comps.count += 1
    ref: Ref = comps.manifests.materialize(
        comps.proposer,
        base_composition_ref=comps.base_ref,
        manifest=manifest,
        suffix=f"s10c{comps.count}",
    )
    return ref


KEY = Ed25519PrivateKey.generate()


def promote_router(w: World, **slots: Ref | None) -> dict[str, Ref]:
    """One candidate per installed cell with the same manifest change (so every cell names the
    same router), made the active release, which is what promotion writes. The app's router is
    then the candidates' (``releases.router_ref``), so an unpinned goal reads it."""
    comps, d = w.comps, w.rig.d
    comps.count += 1
    made: dict[str, Ref] = {}
    for cell, base_ref in w.service.apps["app"].compositions.items():
        manifest = comps.manifests.change(comps.manifests.of_composition(base_ref), **slots)
        made[cell] = comps.manifests.materialize(
            comps.proposer,
            base_composition_ref=base_ref,
            manifest=manifest,
            suffix=f"s10p{comps.count}",
        )
    release = releases.build(
        d.store,
        d.scope,
        d.contracts,
        f"local-test-s10-{comps.count}",
        list(made.values()),
        signer=KEY,
        key_id="local-authority",
        basis="S10 decider test",
    )
    with d.store.tx() as db:
        try:  # the rig installs no release pointer; a deployment with one moves it
            version = d.store.head(d.scope, releases.POINTER_KIND, releases.POINTER_ID, db=db)[
                "row_version"
            ]
        except RuntimeFault:
            version = 0
        d.store.cas(
            db,
            d.scope,
            releases.POINTER_KIND,
            releases.POINTER_ID,
            version,
            "active",
            {"release_ref": release},
        )
    return made


def plan_free(w: World, text: str = "make value return 2") -> str:
    """Plan a goal with no pinned composition: the router selects the cell."""
    w.extra["goals"] = n = w.extra.get("goals", 0) + 1
    goal = submit(w.rig, f"({n}) {text}")
    w.service.plan(goal)
    return goal


def decisions_of(w: World, goal: str) -> list[dict[str, Any]]:
    return deciders.plan_decisions(w.rig.d.store, w.rig.d.scope, w.service.plan_record(goal))


def layers_of(w: World, goal: str) -> list[str]:
    return [d["layer"] for d in decisions_of(w, goal)]


def only(w: World, goal: str, layer: str) -> dict[str, Any]:
    found = [d for d in decisions_of(w, goal) if d["layer"] == layer]
    assert len(found) == 1, (layer, [d["layer"] for d in decisions_of(w, goal)])
    return found[0]


def env_of(w: World) -> Env:
    return Env(w.rig.d)


def test_v1_priors_reproduce_today_exactly(deployment: Any, tmp_path: Path) -> None:
    """Golden G1 / repair_loop: with no decider component every point takes its prior, the run is
    the v1 run (fail, then repair on the previous patch) and the prompts are v1's text."""
    w = build_world(deployment, tmp_path, wrong_first)
    goal = w.approved(w.comps.base_ref)
    record = w.loop.run_goal(goal)
    assert outcomes(record) == ["fail", "pass"] and record["status"] == "published"
    assert record["strategy"]["strategy"] == "repair_loop"
    # one decision per point, in the order the points happen (§6.1)
    assert layers_of(w, goal) == [
        "L1",
        "L2",
        "L3",
        "L8",  # intake, after the plan
        "L5",  # dispatch options, once before the first dispatch
        "L4",
        "L7",
        "L6",  # attempt 1: context, before verification, on failure
        "L4",
        "L7",  # attempt 2
    ]
    expected = {
        "L1": "proceed",
        "L2": "repair_loop",
        "L3": CELL,
        "L5": "driver_defaults",
        "L4": "none",
        "L7": "none",
        "L6": "retry_feedback",
    }
    for value in decisions_of(w, goal):
        layer = value["layer"]
        assert value["used_prior"] is True and value["decider_ref"] is None, layer
        assert value["method_ref"] is None and value["table_ref"] is None, layer
        assert value["prior"] == PRIORS[layer] and value["point"] == POINTS[layer], layer
        assert value["subject"] == {"goal_id": goal} and value["cell_id"] == CELL, layer
        if layer in expected:
            assert value["chosen"] == expected[layer], layer
        assert value["chosen"] in {o["option"] for o in value["options"]}, layer
    # L8's prior is the manifest's limits version; the contract keeps the deployment's budget
    limits = only(w, goal, "L8")
    assert limits["chosen"].startswith("limits.") and limits["chosen"].endswith("@1")
    assert "decided_limits" not in record
    # the prompts are exactly what `loop.prompt` renders: no section, the v1 feedback form
    contract = w.rig.d.store.get(w.rig.d.scope, "goal-contract", record["contract_ref"])
    first, second = w.agents.prompts
    assert first == w.loop.prompt(contract, record, None, node=w.nodes(goal)[0])
    assert "did not pass" not in first and "did not pass" in second
    assert "Environment facts" not in first + second
    assert w.turns.calls == []  # a decider-free goal starts no auxiliary turn (IC-21)
    assert record.get("aux_usage", []) == []


def test_l1_asks_back_exactly_when_the_planner_asked_and_records_the_prior(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, draft=QUESTION_DRAFT)
    goal = w.plan(w.comps.base_ref)
    record = w.record(goal)
    assert record["status"] == "needs_answers" and record["contract_ref"] is None
    assert layers_of(w, goal) == ["L1"]  # nothing after the plan ran
    l1 = only(w, goal, "L1")
    assert (l1["chosen"], l1["used_prior"], l1["point"]) == ("ask_back", True, "intake")
    assert l1["features"] == {
        "questions": 1,
        "in_scope_empty": False,
        "acceptance_items": "1",
        "domain": "unknown",
    }
    options = {o["option"]: o for o in l1["options"]}
    assert set(options) == {"proceed", "ask_back", "replan_ask_first"}
    # the rig's fixed planner has no ask_first text (an auxiliary turn would also need a cap)
    assert options["replan_ask_first"]["eligible"] is False
    assert options["replan_ask_first"]["why"] == "the planner has no ask_first text"
    assert options["proceed"]["eligible"] is True and options["ask_back"]["eligible"] is True
    # without questions the prior is to proceed
    w2 = build_world(deployment, tmp_path / "b")
    goal2 = w2.plan(w2.comps.base_ref)
    assert only(w2, goal2, "L1")["chosen"] == "proceed"
    assert only(w2, goal2, "L1")["options"][1]["eligible"] is False  # ask_back: nothing asked


def test_an_l1_decider_may_proceed_despite_the_planners_questions(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path / "w", draft=QUESTION_DRAFT)
    e = env_of(w)
    method = e.method(["questions"], name="l1m")
    data = [
        *rows("proceed", 20, 19, {"questions": 1}, tokens=100),
        *rows("ask_back", 20, 6, {"questions": 1}, tokens=100),
    ]
    table = e.table(
        "L1", data, features=("questions",), options=("proceed", "ask_back"), method=method
    )
    ref = e.decider("L1", method, table)
    goal = w.plan(candidate(w, L1=ref))
    record = w.record(goal)
    assert record["status"] == "awaiting_approval"  # the decider chose to proceed
    l1 = only(w, goal, "L1")
    assert l1["chosen"] == "proceed" and l1["used_prior"] is False and l1["decider_ref"] == ref
    assert l1["table_ref"] == table and l1["prior"] == PRIORS["L1"]
    assert layers_of(w, goal) == ["L1", "L2", "L3", "L8"]


def test_l2_decides_the_strategy_after_the_plan_and_the_goal_runs_it(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path)
    e = env_of(w)
    method = e.method(["task_class"], name="l2m")
    feats = {"task_class": "logic_change"}
    data = [
        *rows("repair_loop", 20, 20, feats, tokens=200),
        *rows("single", 20, 20, feats, tokens=100),
    ]
    table = e.table(
        "L2", data, features=("task_class",), options=("repair_loop", "single"), method=method
    )
    decider = e.decider("L2", method, table)
    execution = e.register(
        "execution_strategy", "two", {"enabled": ["repair_loop", "single"], "params": {}}
    )
    goal = w.approved(candidate(w, L2=decider, execution_strategy=execution))
    record = w.record(goal)
    assert layers_of(w, goal) == ["L1", "L2", "L3", "L8"]
    l2 = only(w, goal, "L2")
    assert l2["chosen"] == "single" and l2["used_prior"] is False and l2["prior"] == "repair_loop"
    assert l2["decider_ref"] == decider and l2["table_ref"] == table and l2["cell_id"] == CELL
    assert l2["features"]["task_class"] == "logic_change" and l2["features"]["apps"] == 1
    assert record["strategy"]["strategy"] == "single"
    assert record["strategy"]["decision_ref"] == record["decisions"][1]
    assert record["strategy"]["enabled"] == ["repair_loop", "single"]
    node = w.nodes(goal)[0]
    assert node["budget"]["max_attempts"] == 1  # the decided strategy's node budget
    done = w.loop.run_goal(goal)
    assert done["status"] == "published" and outcomes(done) == ["pass"]
    # the decision is also the strategy's own record in the goal's metrics
    assert done["strategy_metrics"]["strategy"] == "single"


def test_a_trial_decision_reaches_the_domain_bucket_fitted_from_rows_of_its_domain(
    deployment: Any, tmp_path: Path
) -> None:
    """IC-23 (provisional): ``TrialContext.domain`` reaches the trial's L1/L2 features, so an L2
    table fitted from rows of that domain is used at the domain level (§6.3: the finest credible
    level); a real goal of the same composition sees ``unknown`` and shrinks back to the global
    level."""
    w = build_world(deployment, tmp_path)
    e = env_of(w)
    method = e.method(["domain"], name="l2dom")
    data = [
        *rows("repair_loop", 20, 20, {"domain": "bug"}, tokens=200),
        *rows("single", 20, 20, {"domain": "bug"}, tokens=100),
        *rows("repair_loop", 30, 6, {"domain": "feature"}, tokens=200),  # a poor domain for both
        *rows("single", 30, 6, {"domain": "feature"}, tokens=100),
    ]
    table = e.table(
        "L2", data, features=("domain",), options=("repair_loop", "single"), method=method
    )
    decider = e.decider("L2", method, table)
    execution = e.register(
        "execution_strategy", "twodom", {"enabled": ["repair_loop", "single"], "params": {}}
    )
    ref = candidate(w, L2=decider, execution_strategy=execution)
    goal = w.plan(ref, trial=trial_context(domain="bug"))
    assert w.record(goal)["trial"]["domain"] == "bug"
    assert only(w, goal, "L1")["features"]["domain"] == "bug"
    l2 = only(w, goal, "L2")
    assert l2["features"] == {**l2["features"], "domain": "bug"} and l2["used_prior"] is False
    assert len(l2["bucket"]) == 1 and "bug" in l2["bucket"][0]  # the domain level
    assert l2["chosen"] == "single" and l2["subject"] == trial_context().subject
    by_option = {o["option"]: o for o in l2["options"]}
    assert by_option["single"]["n"] == by_option["repair_loop"]["n"] == 20  # bug rows only
    # a real goal: domain "unknown" has no rows, so the decision uses the global level
    real = w.plan(ref)
    l2_real = only(w, real, "L2")
    assert l2_real["features"]["domain"] == "unknown" and l2_real["bucket"] == []
    assert {o["n"] for o in l2_real["options"]} == {50}


def test_l2_keeps_the_prior_when_its_table_was_fitted_on_another_cell(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path)
    e = env_of(w)
    method = e.method(["task_class"], name="l2o")
    feats = {"task_class": "logic_change"}
    data = [
        *rows("repair_loop", 20, 20, feats, tokens=200),
        *rows("single", 20, 20, feats, tokens=100),
    ]
    table = e.table(
        "L2",
        data,
        features=("task_class",),
        options=("repair_loop", "single"),
        method=method,
        cells=(OTHER,),
    )
    decider = e.decider("L2", method, table)
    execution = e.register(
        "execution_strategy", "two", {"enabled": ["repair_loop", "single"], "params": {}}
    )
    goal = w.plan(candidate(w, L2=decider, execution_strategy=execution))
    l2 = only(w, goal, "L2")
    assert l2["chosen"] == "repair_loop" and l2["used_prior"] is True
    assert w.record(goal)["strategy"]["strategy"] == "repair_loop"


def test_a_trial_decision_reaches_the_domain_buckets_its_trials_rows_were_fitted_into(
    deployment: Any, tmp_path: Path
) -> None:
    """§6.1, §6.3, §6.5 through the real ``plan``: the rows fitted from trials' L1/L2 decisions
    fill the buckets a later trial's decision looks up, at the domain level and every level
    below it, whatever domain the trial-metrics name ("bug" here). Were the rows bucketed on the
    trial-metrics domain, every level from ``domain`` down would have n = 0 for the decision
    (L2's hierarchy starts with ``domain``) and the table could never specialise."""
    w = build_world(deployment, tmp_path)
    e = env_of(w)

    def trial_goal(i: int) -> str:
        subject = {"experiment_id": "exp-dom", "trial_id": f"trial-dom-{i}"}
        return w.plan(w.comps.base_ref, trial=trial_context(subject=subject))

    for i in range(3):
        e.metrics(task=f"dom-{i}", goal=trial_goal(i), domain="bug", strategy="repair_loop")
    later = trial_goal(3)
    for layer in ("L1", "L2"):
        decided = only(w, later, layer)
        assert decided["subject"] == {"experiment_id": "exp-dom", "trial_id": "trial-dom-3"}
        features = decided["features"]
        assert features["domain"] == deciders.UNKNOWN_DOMAIN
        fitted = rows_from_trial_metrics(e.store, e.scope, layer=layer, cells=[CELL])
        assert len(fitted) == 3, layer
        table = RuleTable.fit(
            fitted,
            layer=layer,
            features=FEATURES[layer],
            hierarchy=deciders.default_hierarchy(FEATURES[layer]),
            options=(decided["chosen"],),
            pooling_strength=4,
        )
        depth = table.levels(features)
        assert depth == len(FEATURES[layer]) + 1, layer  # every level, the global one included
        domain_level = FEATURES[layer].index("domain") + 1
        for level in range(depth):
            bucket, (estimate,) = table.estimates_at(features, level)
            assert estimate.n == 3 and estimate.successes == 3, (layer, level)
            assert ("domain=unknown" in bucket) is (level >= domain_level), (layer, bucket)
        assert all(r.features == features for r in fitted), layer  # the features it saw


def l3_world(deployment: Any, tmp_path: Path) -> tuple[World, Ref, dict[str, Ref], Ref]:
    """Both cells; an L3 decider (Claude's table says it solves what Codex does not) carried by
    the router of the active release. (world, decider, the promoted compositions, table)"""
    w = build_world(deployment, tmp_path, two=True)
    e = env_of(w)
    method = e.method(["task_class"], name="l3m")
    feats = {"task_class": "logic_change"}
    data = [*rows(OTHER, 20, 20, feats, tokens=100), *rows(CELL, 20, 4, feats, tokens=100)]
    table = e.table("L3", data, features=("task_class",), options=(CELL, OTHER), method=method)
    decider = e.decider("L3", method, table)
    return w, decider, promote_router(w, L3=decider), table


def test_l3_decides_the_executor_cell_and_records_it_on_the_composition(
    deployment: Any, tmp_path: Path
) -> None:
    w, decider, _made, table = l3_world(deployment, tmp_path)
    goal = plan_free(w)
    record = w.record(goal)
    l3 = only(w, goal, "L3")
    assert l3["chosen"] == OTHER and l3["used_prior"] is False and l3["prior"] == PRIORS["L3"]
    assert l3["cell_id"] == CELL  # the cell the decision is for: the router's first eligible one
    assert l3["decider_ref"] == decider and l3["table_ref"] == table
    assert {o["option"] for o in l3["options"]} == {CELL, OTHER}
    assert all(o["eligible"] for o in l3["options"])
    chosen = record["composition"]
    assert chosen["cell_id"] == OTHER and chosen["driver_id"] == "claude-cli"
    assert chosen["decision_ref"] == record["decisions"][2]  # IC-22: the harness-decision
    assert chosen["pinned"] is False and record["status"] == "awaiting_approval"
    assert layers_of(w, goal) == ["L1", "L2", "L3", "L8"]


def test_l3_keeps_the_cell_of_a_pinned_composition(deployment: Any, tmp_path: Path) -> None:
    w, _decider, made, _table = l3_world(deployment, tmp_path)
    goal = w.plan(made[CELL])  # an experiment arm or canary: the composition is fixed
    l3 = only(w, goal, "L3")
    assert [o["option"] for o in l3["options"]] == [CELL] and l3["chosen"] == CELL
    record = w.record(goal)
    assert record["composition"]["pinned"] is True and record["composition"]["cell_id"] == CELL


def test_l3_without_a_decider_takes_the_routers_first_eligible_cell(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, two=True)
    goal = plan_free(w)
    l3 = only(w, goal, "L3")
    assert (l3["chosen"], l3["used_prior"], l3["decider_ref"]) == (CELL, True, None)
    assert {o["option"] for o in l3["options"]} == {CELL, OTHER}
    assert w.record(goal)["composition"].get("decision_ref") is None  # nothing to re-check


def test_l8_decides_the_limits_among_the_deciders_versions(deployment: Any, tmp_path: Path) -> None:
    w = build_world(deployment, tmp_path)
    e = env_of(w)
    base_limits = w.service._budget_policy(w.comps.base_ref).refs["limits"]
    smaller = e.register(
        "limits",
        "small",
        {**policies.V1["limits"], "max_wall_seconds": 900, "max_tokens": 30_000_000},
    )
    method = e.method(["task_class"], name="l8m")
    feats = {"task_class": "logic_change"}
    big, small = deciders.option_id(base_limits), deciders.option_id(smaller)
    data = [*rows(big, 20, 20, feats, tokens=900), *rows(small, 20, 20, feats, tokens=300)]
    table = e.table("L8", data, features=("task_class",), options=(big, small), method=method)
    decider = e.decider("L8", method, table, options=[base_limits, smaller])
    goal = w.plan(candidate(w, L8=decider))
    record = w.record(goal)
    l8 = only(w, goal, "L8")
    assert l8["chosen"] == small and l8["used_prior"] is False and l8["prior"] == PRIORS["L8"]
    assert {o["option"] for o in l8["options"]} == {big, small}
    assert record["decided_limits"]["option"] == small
    contract = w.rig.d.store.get(w.rig.d.scope, "goal-contract", record["contract_ref"])
    assert contract["budget"]["max_wall_seconds"] == 900  # the decided limits are the root budget


def test_l8_never_chooses_limits_above_the_deployment_budget(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path)
    e = env_of(w)
    base_limits = w.service._budget_policy(w.comps.base_ref).refs["limits"]
    above = e.register("limits", "above", {**policies.V1["limits"], "max_wall_seconds": 3600})
    method = e.method(["task_class"], name="l8a")
    feats = {"task_class": "logic_change"}
    big, huge = deciders.option_id(base_limits), deciders.option_id(above)
    data = [*rows(big, 20, 10, feats, tokens=900), *rows(huge, 20, 20, feats, tokens=100)]
    table = e.table("L8", data, features=("task_class",), options=(big, huge), method=method)
    decider = e.decider("L8", method, table, options=[base_limits, above])
    goal = w.plan(candidate(w, L8=decider))
    l8 = only(w, goal, "L8")
    assert l8["chosen"] == big  # the better option cannot run: the deployment budget is 1800 s
    huge_row = next(o for o in l8["options"] if o["option"] == huge)
    assert huge_row["eligible"] is False and "deployment" in huge_row["why"]
    assert "decided_limits" not in w.record(goal)


def test_l4_decides_which_context_parts_this_attempt_shows(deployment: Any, tmp_path: Path) -> None:
    w = build_world(deployment, tmp_path)
    e = env_of(w)
    boot = w.comps.version("env_bootstrap", enabled=True)
    method = e.method(["strategy"], name="l4m")
    feats = {"strategy": "repair_loop"}
    data = [
        *rows("none", 20, 20, feats, tokens=100),
        *rows("env_bootstrap", 20, 20, feats, tokens=300),
    ]
    table = e.table(
        "L4", data, features=("strategy",), options=("none", "env_bootstrap"), method=method
    )
    decider = e.decider("L4", method, table)
    # control: the manifest's env bootstrap alone shows its section (the prior is the manifest)
    control = w.run(candidate(w, env_bootstrap=boot))
    assert control["status"] == "published"
    assert "Environment facts" in w.agents.prompts[0]
    prior = [d for d in deciders.plan_decisions(e.store, e.scope, control) if d["layer"] == "L4"]
    assert [(d["chosen"], d["used_prior"]) for d in prior] == [("env_bootstrap", True)]
    w.agents.calls.clear()
    decided = w.run(candidate(w, env_bootstrap=boot, L4=decider))
    assert decided["status"] == "published"
    assert "Environment facts" not in w.agents.prompts[0]  # the decider turned the part off
    l4 = [d for d in deciders.plan_decisions(e.store, e.scope, decided) if d["layer"] == "L4"]
    assert len(l4) == 1 and l4[0]["chosen"] == "none" and l4[0]["used_prior"] is False
    assert l4[0]["point"] == "dispatch" and l4[0]["prior"] == PRIORS["L4"]
    assert l4[0]["features"] == {
        "strategy": "repair_loop",
        "repo_files": "<50",
        "prior_failures": 0,
    }
    assert l4[0]["decider_ref"] == decider and l4[0]["table_ref"] == table


def test_l5_records_the_dispatch_options_decision_once_before_the_first_dispatch(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, wrong_first)
    e = env_of(w)
    null = {
        "claude": {"max_turns": None, "append_system_prompt": None, "allowed_tools": None},
        "codex": {"config": []},
    }
    a = e.register("driver_options", "opta", null)
    b = e.register("driver_options", "optb", null)
    method = e.method(["strategy"], name="l5m")
    feats = {"strategy": "repair_loop"}
    one, two = deciders.option_id(a), deciders.option_id(b)
    data = [*rows(one, 20, 20, feats, tokens=100), *rows(two, 20, 20, feats, tokens=500)]
    table = e.table("L5", data, features=("strategy",), options=(one, two), method=method)
    decider = e.decider("L5", method, table, options=[a, b])
    record = w.run(candidate(w, L5=decider))
    assert outcomes(record) == ["fail", "pass"]
    l5 = [d for d in deciders.plan_decisions(e.store, e.scope, record) if d["layer"] == "L5"]
    assert len(l5) == 1  # once per goal, not once per attempt
    assert l5[0]["chosen"] == one and l5[0]["used_prior"] is False
    assert l5[0]["prior"] == PRIORS["L5"] and l5[0]["features"] == {
        "cell": CELL,
        "strategy": "repair_loop",
    }
    assert {o["option"] for o in l5[0]["options"]} == {one, two}


def test_l6_stops_the_goal_when_its_decider_says_so(deployment: Any, tmp_path: Path) -> None:
    w = build_world(deployment, tmp_path, always(WRONG))
    e = env_of(w)
    method = e.method(["attempt"], name="l6m")
    feats = {"attempt": 1}
    data = [
        *rows("retry_feedback", 20, 3, feats, tokens=100),
        *rows("stop", 20, 18, feats, tokens=10),
    ]
    table = e.table(
        "L6", data, features=("attempt",), options=("retry_feedback", "stop"), method=method
    )
    decider = e.decider("L6", method, table)
    record = w.run(candidate(w, L6=decider))
    assert outcomes(record) == ["fail"] and record["status"] == "failed"
    assert "L6" in record["reason"]  # stopped by the L6 decider, not by the attempt budget
    assert len(w.agents.prompts) == 1  # no second attempt
    l6 = [d for d in deciders.plan_decisions(e.store, e.scope, record) if d["layer"] == "L6"]
    assert len(l6) == 1 and l6[0]["chosen"] == "stop" and l6[0]["used_prior"] is False
    assert l6[0]["point"] == "on_failure" and l6[0]["prior"] == PRIORS["L6"]
    assert l6[0]["features"]["attempt"] == 1 and "failing_acceptance" in l6[0]["features"]
    options = {o["option"]: o for o in l6[0]["options"]}
    assert set(options) == set(OPTIONS["L6"])
    assert options["retry_fresh"]["eligible"] is False  # the attempt policy's next form is feedback
    assert options["escalate"]["eligible"] is False  # no cascade: no next cell


def test_l6_without_a_decider_retries_as_the_attempt_policy_says(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path, always(WRONG))
    record = w.run(w.comps.base_ref)
    assert outcomes(record) == ["fail", "fail", "fail"] and record["status"] == "failed"
    l6 = [
        d
        for d in deciders.plan_decisions(*(w.rig.d.store, w.rig.d.scope), record)
        if d["layer"] == "L6"
    ]
    # the attempt cap leaves no further attempt on the last failure: the prior is to stop there
    assert [d["chosen"] for d in l6] == ["retry_feedback", "retry_feedback", "stop"]
    assert all(d["used_prior"] for d in l6)
    assert [d["features"]["attempt"] for d in l6] == [1, 2, 3]
    assert l6[2]["features"]["same_signature"] is True


def test_l7_records_none_before_the_final_verification_when_fast_checks_are_off(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path)
    record = w.run(w.comps.base_ref)
    l7 = [
        d
        for d in deciders.plan_decisions(w.rig.d.store, w.rig.d.scope, record)
        if d["layer"] == "L7"
    ]
    assert len(l7) == 1 and l7[0]["chosen"] == "none" and l7[0]["used_prior"] is True
    assert l7[0]["point"] == "before_final_verification" and l7[0]["prior"] == PRIORS["L7"]
    assert l7[0]["features"] == {"changed_files": "1", "quick_available": False}
    quick = next(o for o in l7[0]["options"] if o["option"] == "quick_checks")
    assert quick["eligible"] is False and "off" in quick["why"]


def test_a_production_goal_never_explores_even_when_the_table_prefers_a_thin_option(
    deployment: Any, tmp_path: Path
) -> None:
    w = build_world(deployment, tmp_path)
    e = env_of(w)
    method = e.method(["task_class"], name="l2t")
    feats = {"task_class": "logic_change"}
    data = [
        *rows("repair_loop", 30, 20, feats, tokens=200),
        *rows("single", 3, 3, feats, tokens=10),
    ]
    table = e.table(
        "L2", data, features=("task_class",), options=("repair_loop", "single"), method=method
    )
    decider = e.decider("L2", method, table, min_samples=5)
    execution = e.register(
        "execution_strategy", "two", {"enabled": ["repair_loop", "single"], "params": {}}
    )
    goal = w.plan(candidate(w, L2=decider, execution_strategy=execution))
    assert only(w, goal, "L2")["chosen"] == "repair_loop"
    assert w.record(goal)["strategy"]["strategy"] == "repair_loop"
