"""Immutable, independently approved experiments with durable pre-call reservations.

Modes are distinct. A replay result never establishes a live improvement. A
registered executor is trusted server code, not a worker-supplied callback. Its
budget/containment qualification must be supplied by the operator before use.

Work 033 S2 (interfaces.md §3.8, §7): a plan whose analysis policy carries
`evaluator_version_ref` may pin a stage subset of its split (IC-15), declare a budget-matched
reference arm (§7.4, checked by an injected `reference_validator`), accumulate an e-process across
experiments with the same key (§7.6, IC-16 B) and is refused below its minimum sample when it is
confirmatory (IC-09). Trials may run in parallel (§7.7). A legacy plan behaves as at c9f896a.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, TypeVar

from amplai_foundry.meta_harness.budget import EvolutionBudget
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id, now
from amplai_foundry.runtime.contracts.semantics import check_refs, resolve_ref
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.storage.store import Scope, Store

from .analysis import analyze_pairs, validate_analysis_plan
from .corpus import CorpusService
from .receipts import check_observation, read_receipt
from .sequential import min_tasks_for_margin, min_wins_for_superiority

# §7.4: two arms keep today's alternation; a declared reference arm rotates by repeat.
TWO_ARMS = ("baseline", "candidate")
THREE_ARMS = ("baseline", "candidate", "reference")
ORDER_RULE = "alternate_by_repeat_v1"
CASE_RULES = ("informative_v1", "all_v1")
STAGES = ("screening", "focused", "ablation", "holdout")
# §7.7 / §8.4: never more than four trials at once.
MAX_PARALLEL = 4
REPORT_TRIAL_LIMIT = 256

_T = TypeVar("_T")
_C = TypeVar("_C")
_R = TypeVar("_R")


@dataclass(frozen=True)
class TrialObservation:
    success: bool | None
    artifact_refs: tuple[dict[str, Any], ...]
    safety_failures: int = 0
    unknown_effects: int = 0
    cost_microunits: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_status: str = "measured"


@dataclass(frozen=True)
class ExecutorPolicy:
    """Pinned server-side qualification; never accepted from an API request body."""

    modes: frozenset[str]
    max_trial_tokens: int
    max_trial_cost_microunits: int
    qualification_ref: dict[str, Any]
    external_effects: bool = False

    def validate(self) -> None:
        if not self.modes or not self.modes <= {"static", "replay", "sandbox_rerun", "shadow"}:
            raise RuntimeFault(
                "EXECUTOR_MODE", "Canary is admitted by MetaHarness, not generic evaluation"
            )
        if any(
            type(x) is not int or x < 0
            for x in (self.max_trial_tokens, self.max_trial_cost_microunits)
        ):
            raise RuntimeFault(
                "EXECUTOR_BUDGET",
                "Qualified per-trial ceilings must be finite nonnegative integers",
            )
        if self.external_effects:
            raise Hold(
                "EVAL_EXTERNAL_EFFECT",
                "Evaluation/shadow cannot mutate external or production systems",
            )


def e_process_key(baseline_ref: dict[str, Any], candidate_ref: dict[str, Any], stage: str) -> str:
    """§7.6: experiments with the same baseline, candidate and stage share one e-process. The key
    holds no proposal id, so a new proposal with the same arms continues it (IC-16 B)."""
    return digest({"baseline_ref": baseline_ref, "candidate_ref": candidate_ref, "stage": stage})


def arm_order(arms: tuple[str, ...], repeat: int) -> list[str]:
    """§7.4: the arm order of repeat r rotates the arms by r. Two arms keep today's alternation
    (`baseline, candidate` on even repeats, reversed on odd ones)."""
    k = repeat % len(arms)
    return [*arms[k:], *arms[:k]]


def effective_parallel(requested: int, *caps: int) -> int:
    """§7.7: `parallel <= min(caps..., 4)`; a larger request runs at the cap."""
    if type(requested) is not int or requested < 1:
        raise RuntimeFault("EVAL_PARALLEL", "parallel is a positive integer")
    return min(requested, *caps, MAX_PARALLEL)


def run_bounded(
    items: Iterable[_T],
    *,
    parallel: int,
    dispatch: Callable[[_T], _C | None],
    execute: Callable[[_C], _R],
    record: Callable[[_C, _R], None],
    stopped: Callable[[], bool],
) -> None:
    """§7.7: dispatch in item order with at most `parallel` executions at once.

    `dispatch` (pre-guard and reservation) and `record` run on the calling thread; only `execute`
    runs in the pool. `dispatch` returns None to stop admission. Once `stopped()` is true no item
    is dispatched; running executions finish and are recorded in completion order (ties in
    dispatch order). `parallel == 1` runs inline, exactly as the sequential loop did.
    """
    source = iter(items)
    if parallel == 1:
        for item in source:
            if stopped():
                return
            ctx = dispatch(item)
            if ctx is None:
                return
            record(ctx, execute(ctx))
        return
    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="amplai-trial") as pool:
        running: dict[Future[_R], tuple[int, _C]] = {}
        admitting, seq = True, 0
        while True:
            while admitting and not stopped() and len(running) < parallel:
                try:
                    item = next(source)
                except StopIteration:
                    admitting = False
                    break
                ctx = dispatch(item)
                if ctx is None:
                    admitting = False
                    break
                running[pool.submit(execute, ctx)] = (seq, ctx)
                seq += 1
            if not running:
                return
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=lambda f: running[f][0]):
                _, ctx = running.pop(future)
                record(ctx, future.result())


def validate_observation(
    artifacts: ArtifactStore,
    scope: Scope,
    observation: TrialObservation,
    composition_ref: dict[str, Any],
    task_id: str,
    repeat: int,
    mode: str,
) -> None:
    """The receipt binding of every trial, experiment and calibration alike (§2.8)."""
    if not isinstance(observation, TrialObservation):
        raise RuntimeFault("TRIAL_RESULT", "Executor must return a typed observation")
    if observation.success is not None and type(observation.success) is not bool:
        raise RuntimeFault("TRIAL_TYPE", "Success must be boolean or unknown")
    if not observation.artifact_refs:
        raise Hold("TRIAL_EVIDENCE", "Actual independent evidence bytes are required")
    for value in (observation.safety_failures, observation.unknown_effects):
        if type(value) is not int or value < 0:
            raise RuntimeFault(
                "TRIAL_USAGE", "Safety and uncertainty counters must be nonnegative integers"
            )
    optional_values: tuple[int | None, ...] = (
        observation.cost_microunits,
        observation.input_tokens,
        observation.output_tokens,
    )
    for optional in optional_values:
        if optional is not None and (type(optional) is not int or optional < 0):
            raise RuntimeFault("TRIAL_USAGE", "Usage is nonnegative integer or explicitly unknown")
    if observation.usage_status not in {"measured", "estimated", "unknown"}:
        raise RuntimeFault("TRIAL_USAGE", "Unknown usage classification")
    for ref in observation.artifact_refs:
        artifacts.read(scope, ref, trusted=True)
    receipt = read_receipt(artifacts.read(scope, observation.artifact_refs[0], trusted=True))
    expected = {
        "composition_ref": composition_ref,
        "task_id": task_id,
        "repeat": repeat,
        "mode": mode,
        "success": observation.success,
        "safety_failures": observation.safety_failures,
        "unknown_effects": observation.unknown_effects,
        "cost_microunits": observation.cost_microunits,
        "input_tokens": observation.input_tokens,
        "output_tokens": observation.output_tokens,
        "usage_status": observation.usage_status,
    }
    check_observation(receipt, expected)


def is_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "revision", "digest"}
        and isinstance(value["id"], str)
        and type(value["revision"]) is int
        and isinstance(value["digest"], str)
    )


class EvaluationService:
    def __init__(
        self,
        store: Store,
        contracts: Any,
        artifacts: ArtifactStore,
        *,
        approval_check: Callable[..., Any],
        executor_id: str,
        environment_probe: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        executor_policy: ExecutorPolicy | None = None,
        max_parallel: int = 1,
        reference_validator: Callable[[Scope, dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> None:
        if type(max_parallel) is not int or not 1 <= max_parallel <= MAX_PARALLEL:
            raise RuntimeFault("EVAL_PARALLEL", "max_parallel is an integer from 1 to 4")
        self.store, self.contracts, self.artifacts = store, contracts, artifacts
        self.approval_check, self.executor_id = approval_check, executor_id
        self.environment_probe, self.executor_policy = environment_probe, executor_policy
        # reference_validator(scope, experiment plan, analysis policy) raises Hold REFERENCE_ARM;
        # injected by runtime/execution/meta_local.py (S11), evaluation/ imports neither (§1.3).
        self.max_parallel, self.reference_validator = max_parallel, reference_validator
        self.corpus, self.budgets = CorpusService(store, artifacts), EvolutionBudget(store)

    @staticmethod
    def approval_subject(plan: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in plan.items() if k != "approval_ref"}

    def _independent(self, actor: Actor, plan: dict[str, Any]) -> dict[str, Any]:
        p = self.store.get(actor.scope, "harness-change-proposal", plan["proposal_ref"])
        if (
            p["proposer"]["subject_id"] == actor.subject_id
            or "harness.propose" in actor.permissions
        ):
            raise Hold(
                "SELF_APPROVAL",
                "A proposer identity cannot approve, execute or judge its own evaluation",
            )
        return p

    def _guard(self, actor: Actor, plan: dict[str, Any]) -> None:
        self._independent(actor, plan)
        self.approval_check(
            actor.scope,
            plan["approval_ref"],
            "experiment.execute",
            digest(self.approval_subject(plan)),
        )
        try:
            if self.store.head(actor.scope, "runtime-control", "kill")["data"]["enabled"]:
                raise Hold("KILL_SWITCH", "Operator disabled evaluation admission")
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise

    def _cases(
        self,
        actor: Actor,
        plan: dict[str, Any],
        policy: dict[str, Any],
        sampling: dict[str, Any],
        split: str,
        message: str,
    ) -> list[dict[str, Any]]:
        """The frozen cases in corpus order, recomputed identically by freeze and run. A legacy
        plan pins the whole split (as at c9f896a); a plan with `evaluator_version_ref` whose
        sampling plan declares a `case_rule` pins `select_cases(...)` over the split (IC-15)."""
        cases = self.corpus.select(actor, plan["corpus_ref"], split, purpose="frozen_experiment")
        if "evaluator_version_ref" in policy and "case_rule" in sampling:
            cases = self._subset(actor.scope, cases, sampling)
        if not cases or sampling.get("case_ids") != [c["case_id"] for c in cases]:
            raise Hold("SAMPLING_CHANGED", message)
        return cases

    def _subset(
        self, scope: Scope, cases: list[dict[str, Any]], sampling: dict[str, Any]
    ) -> list[dict[str, Any]]:
        # Imported here: calibration.py imports this module (§1.1 puts select_cases there).
        from .calibration import select_cases

        rule, summary_ref = sampling.get("case_rule"), sampling.get("calibration_summary_ref")
        max_tasks, cell_id = sampling.get("max_tasks"), sampling.get("cell_id")
        if (
            rule not in CASE_RULES
            or type(max_tasks) is not int
            or max_tasks < 1
            or not isinstance(cell_id, str)
            or not cell_id
            or (summary_ref is None and rule != "all_v1")
            or (summary_ref is not None and not is_ref(summary_ref))
        ):
            raise RuntimeFault(
                "SAMPLING_PLAN",
                "A stage subset declares case_rule, max_tasks, cell_id and its calibration "
                "summary (null only for all_v1)",
            )
        summary = None
        if summary_ref is not None:
            kind, summary = resolve_ref(self.store, scope, summary_ref)
            if kind != "calibration-summary":
                raise RuntimeFault("SAMPLING_PLAN", "calibration_summary_ref names another kind")
        chosen = set(
            select_cases(
                cases,
                rule=rule,
                summary=summary,
                cell_id=cell_id,
                max_tasks=max_tasks,
                domain_of=lambda case: str(case["task_class"]),
            )
        )
        return [c for c in cases if c["case_id"] in chosen]

    @staticmethod
    def _arms(policy: dict[str, Any], sampling: dict[str, Any]) -> tuple[str, ...]:
        if "evaluator_version_ref" not in policy:
            return TWO_ARMS
        arms = THREE_ARMS if policy.get("reference_arm") is not None else TWO_ARMS
        declared = sampling.get("arms")
        if declared is not None and declared != list(arms):
            raise Hold("REFERENCE_ARM", "Sampling-plan arms differ from the analysis plan")
        if sampling.get("order_rule", ORDER_RULE) != ORDER_RULE:
            raise RuntimeFault("SAMPLING_PLAN", "Only the alternate_by_repeat_v1 order is defined")
        return arms

    def _check_evaluator_version(self, scope: Scope, policy: dict[str, Any]) -> None:
        kind, _ = resolve_ref(self.store, scope, policy["evaluator_version_ref"])
        if kind != "evaluator-version":
            raise Hold("EVALUATOR_UNQUALIFIED", "evaluator_version_ref names another record kind")

    @staticmethod
    def _check_power(policy: dict[str, Any], task_count: int) -> None:
        """IC-09, §7.5: a confirmatory plan below its minimum sample is refused (non-inferiority:
        `n < min_tasks_for_margin`; superiority: `min_wins_for_superiority` exceeds n or does
        not exist). Exploratory plans are never refused for size."""
        if policy.get("purpose") != "confirmatory":
            return
        confidence = policy["confidence"]
        if policy.get("endpoint", "noninferiority") == "superiority":
            wins = min_wins_for_superiority(task_count, policy["minimum_effect"], confidence)
            powered = wins is not None and wins <= task_count
            need = f"{wins} wins without a loss" if wins is not None else "no win count suffices"
        else:
            margin = policy["noninferiority_margin"]
            minimum = min_tasks_for_margin(margin, confidence) if margin > 0 else None
            powered = minimum is not None and task_count >= minimum
            need = f"n >= {minimum}" if minimum is not None else "margin 0 never passes"
        if not powered:
            raise Hold(
                "SAMPLE_UNDERPOWERED",
                f"A confirmatory plan with {task_count} tasks cannot pass its rule ({need})",
                details={"task_count": task_count},
            )

    def _check_reference(self, scope: Scope, plan: dict[str, Any], policy: dict[str, Any]) -> None:
        if policy.get("reference_arm") is None:
            return
        if self.reference_validator is None:
            raise Hold("REFERENCE_ARM", "A reference arm needs the installed reference validator")
        self.reference_validator(scope, plan, policy)

    def _check_e_process(
        self, scope: Scope, plan: dict[str, Any], policy: dict[str, Any], sampling: dict[str, Any]
    ) -> None:
        """§7.6: the frozen prior is the recorded state of every earlier report with the same key,
        in chain order; omitting, reordering or substituting a report is refused. Every report of
        the chain must have recorded this plan's `alpha`: Ville's bound P(sup e >= 1/alpha) <=
        alpha holds only for an alpha fixed before the data, so raising it after earlier nights
        would let the same evidence cross a lower threshold."""
        spec = policy.get("e_process")
        if spec is None:
            return
        stage = sampling.get("stage")
        if stage not in STAGES:
            raise Hold("SEQUENTIAL_RULE", "An accumulating e-process needs the sampling stage")
        key = e_process_key(plan["baseline_ref"], plan["candidate_ref"], stage)
        if spec["key"] != key:
            raise Hold("SEQUENTIAL_RULE", "e_process.key is not this comparison's key")
        recorded = self.e_process_reports(scope, key)
        prior = spec["prior_report_refs"]
        named = [digest(r) for r in prior]
        if len(set(named)) != len(named) or set(named) != set(recorded):
            raise Hold(
                "SEQUENTIAL_RULE",
                "prior_report_refs must list every earlier report with this key exactly once",
            )
        for i, name in enumerate(named):
            report, block = recorded[name]
            exp = self.store.get(scope, "eval-experiment", report["experiment_ref"])
            if (
                exp["baseline_ref"] != plan["baseline_ref"]
                or exp["candidate_ref"] != plan["candidate_ref"]
                or block.get("prior_report_refs") != prior[:i]
            ):
                raise Hold("SEQUENTIAL_RULE", "Prior reports do not form this key's chain")
            if block.get("alpha") != spec["alpha"]:
                raise Hold(
                    "SEQUENTIAL_RULE", "An accumulating e-process keeps the alpha of its chain"
                )
        expected = recorded[named[-1]][1].get("state") if named else None
        if spec["prior_state"] != expected:
            raise Hold("SEQUENTIAL_RULE", "prior_state differs from the last recorded state")

    def e_process_reports(
        self, scope: Scope, key: str
    ) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
        """Every stored eval-report whose analysis recorded an e-process with `key`, as
        {digest(report ref): (report, e_process block)} (§7.6)."""
        found = {}
        for ref, _ in self.store.list_objects(scope, "eval-report"):
            report = self.store.get(scope, "eval-report", ref)  # digest-checked read
            analysis = read_receipt(
                self.artifacts.read(scope, report["analysis_artifact"], trusted=True)
            )
            block = analysis.get("e_process")
            if isinstance(block, dict) and block.get("key") == key:
                found[digest(ref)] = (report, block)
        return found

    def freeze(self, actor: Actor, plan: dict[str, Any]) -> dict[str, Any]:
        actor.require("experiment.approve")
        self.contracts.validate("eval-experiment", plan)
        if plan["scope"] != actor.scope.wire():
            raise RuntimeFault("SCOPE_MISMATCH", "Experiment scope differs")
        check_refs(self.store, actor.scope, plan)
        proposal = self._independent(actor, plan)
        if (
            plan["baseline_ref"] != proposal["baseline_ref"]
            or plan["candidate_ref"] != proposal["candidate_ref"]
        ):
            raise Hold("EXPERIMENT_BINDING", "Frozen arms differ from the proposal")
        self._guard(actor, plan)
        _, analysis = resolve_ref(self.store, actor.scope, plan["analysis_plan_ref"])
        policy = analysis["policy"]
        validate_analysis_plan(policy)
        if plan["primary_endpoint"] != "task_all_repeats_pass":
            raise Hold(
                "ENDPOINT_UNQUALIFIED",
                "Only the qualified paired binary primary endpoint is enabled",
            )
        _, sampling = resolve_ref(self.store, actor.scope, plan["sampling_plan_ref"])
        split = sampling.get("split")
        if not isinstance(split, str):
            raise Hold("SAMPLING_SPLIT", "Sampling plan must pin a split name")
        versioned = "evaluator_version_ref" in policy
        if versioned:
            self._check_evaluator_version(actor.scope, policy)
        cases = self._cases(
            actor,
            plan,
            policy,
            sampling,
            split,
            "Pin the complete case IDs/order for the selected split before execution",
        )
        arms = self._arms(policy, sampling)
        if len(cases) * len(arms) * policy["repeats_per_task"] > REPORT_TRIAL_LIMIT:
            raise Hold(
                "REPORT_CAPACITY",
                "The pinned report schema admits at most 256 trial references; "
                "reduce the frozen sample",
            )
        if policy.get("purpose") == "confirmatory" and sampling["split"] == "development":
            raise Hold(
                "CONFIRMATORY_SPLIT", "Development cases cannot qualify confirmatory improvement"
            )
        if versioned:
            self._check_power(policy, len(cases))
            self._check_reference(actor.scope, plan, policy)
            self._check_e_process(actor.scope, plan, policy, sampling)
        _, env = resolve_ref(self.store, actor.scope, plan["environment_ref"])
        environment_digest = digest(self.environment_probe(env) if self.environment_probe else env)
        with self.store.tx() as db:
            ref = self.store.put(db, actor.scope, "eval-experiment", plan["experiment_id"], 1, plan)
            self.budgets.freeze(db, actor.scope, proposal["proposal_id"], ref, plan["budget"])
            self.store.cas(
                db,
                actor.scope,
                "experiment",
                plan["experiment_id"],
                0,
                "frozen",
                {
                    "experiment_ref": ref,
                    "environment_digest": environment_digest,
                    "frozen_by": actor.subject_id,
                    "executor_id": self.executor_id,
                    "trial_refs": [],
                },
            )
        return ref

    def _validate_executor(self, scope: Scope, mode: str) -> ExecutorPolicy:
        policy = self.executor_policy
        if policy is None:
            raise Hold(
                "QUALIFIED_EXECUTOR_REQUIRED",
                "Register a server-side qualified bounded executor first",
            )
        policy.validate()
        if mode not in policy.modes:
            raise Hold(
                "EXECUTOR_MODE", "This exact executor was not qualified for the requested mode"
            )
        _, qualification = resolve_ref(self.store, scope, policy.qualification_ref)
        if (
            qualification.get("status") != "pass"
            or qualification.get("executor_id") != self.executor_id
        ):
            raise Hold(
                "EXECUTOR_QUALIFICATION", "Qualification does not bind the installed executor"
            )
        return policy

    def run(
        self,
        actor: Actor,
        experiment_ref: dict[str, Any],
        executor: Callable[..., TrialObservation],
        *,
        split: str = "validation",
        parallel: int = 1,
    ) -> dict[str, Any]:
        actor.require("experiment.run")
        scope = actor.scope
        plan = self.store.get(scope, "eval-experiment", experiment_ref)
        self._guard(actor, plan)
        policy = self._validate_executor(scope, plan["mode"])
        workers = effective_parallel(
            parallel, self.max_parallel, plan["budget"]["max_parallel_works"]
        )
        _, analysis_obj = resolve_ref(self.store, scope, plan["analysis_plan_ref"])
        analysis = analysis_obj["policy"]
        _, sampling = resolve_ref(self.store, scope, plan["sampling_plan_ref"])
        if sampling.get("split") != split:
            raise Hold("SAMPLING_FROZEN", "Cannot change split after experiment freeze")
        cases = self._cases(
            actor,
            plan,
            analysis,
            sampling,
            split,
            "Case set/order differs from frozen sampling plan",
        )
        arms = self._arms(analysis, sampling)
        compositions = {"baseline": plan["baseline_ref"], "candidate": plan["candidate_ref"]}
        if "reference" in arms:
            compositions["reference"] = analysis["reference_arm"]["composition_ref"]
        # §7.1: a per-experiment trial reservation never exceeds the qualified executor's.
        trial_tokens = min(
            policy.max_trial_tokens, analysis.get("max_trial_tokens", policy.max_trial_tokens)
        )
        head = self.store.head(scope, "experiment", plan["experiment_id"])
        if head["state"] != "frozen":
            raise Hold(
                "EXPERIMENT_REPLAY",
                "A dispatched experiment is not automatically replayed after interruption",
            )
        if head["data"]["executor_id"] != self.executor_id:
            raise Hold("EXECUTOR_DRIFT", "Executor changed since freeze")
        if split == "holdout":
            self.corpus.consume_holdout(actor, plan["corpus_ref"], experiment_ref)
        with self.store.tx() as db:
            self.store.cas(
                db,
                scope,
                "experiment",
                plan["experiment_id"],
                head["row_version"],
                "running",
                {**head["data"], "owner_epoch": self.store.epoch, "started_at": now()},
            )
        trials: list[dict[str, Any]] = []
        refs: list[dict[str, Any]] = []
        state: dict[str, Any] = {"stop": None, "drifted": False}
        started = time.monotonic()
        proposal_id = plan["proposal_ref"]["id"]
        wall = plan["budget"]["max_wall_seconds"]

        def stop(reason: str) -> None:
            if state["stop"] is None:  # the first stop reason wins (§7.7)
                state["stop"] = reason

        def environment_changed() -> bool:
            _, env = resolve_ref(self.store, scope, plan["environment_ref"])
            observed = self.environment_probe(env) if self.environment_probe else env
            return bool(digest(observed) != head["data"]["environment_digest"])

        def dispatch(item: tuple[dict[str, Any], int, str]) -> dict[str, Any] | None:
            case, repeat, arm = item
            try:
                self._guard(actor, plan)
                if time.monotonic() - started >= wall:
                    raise Hold("META_WALL_BUDGET", "Experiment wall budget reached")
                if environment_changed():
                    state["drifted"] = True
                    raise Hold("ENVIRONMENT_DRIFT", "Environment changed since freeze")
                trial_id = new_id("trial")
                with self.store.tx() as db:
                    self.budgets.reserve(
                        db,
                        scope,
                        proposal_id,
                        trial_id,
                        tokens=trial_tokens,
                        cost=policy.max_trial_cost_microunits,
                    )
                    self.store.cas(
                        db,
                        scope,
                        "eval-trial",
                        trial_id,
                        0,
                        "dispatching",
                        {
                            "experiment_ref": experiment_ref,
                            "task_id": case["case_id"],
                            "arm": arm,
                            "repeat": repeat,
                            "owner_epoch": self.store.epoch,
                        },
                    )
            except RuntimeFault as exc:
                stop(exc.code)
                return None
            return {"case": case, "repeat": repeat, "arm": arm, "trial_id": trial_id}

        def execute(ctx: dict[str, Any]) -> tuple[TrialObservation, str | None, float]:
            began, error_type = time.monotonic(), None
            self.store.assert_outside_tx()
            composition = compositions[ctx["arm"]]
            try:
                observation = executor(composition, ctx["case"], ctx["repeat"], plan["mode"])
                self._validate_observation(
                    scope,
                    observation,
                    composition,
                    ctx["case"]["case_id"],
                    ctx["repeat"],
                    plan["mode"],
                )
            except Exception as exc:
                # The process may already have written; uncertain effects/usage
                # stay explicit.
                error_type = type(exc).__name__
                observation = TrialObservation(None, (), unknown_effects=1, usage_status="unknown")
            return observation, error_type, began

        def record(ctx: dict[str, Any], result: tuple[TrialObservation, str | None, float]) -> None:
            observation, error_type, began = result
            case, arm, trial_id = ctx["case"], ctx["arm"], ctx["trial_id"]
            post_guard = None
            try:
                self._guard(actor, plan)
                if environment_changed():
                    state["drifted"] = True
                    post_guard = "ENVIRONMENT_DRIFT"
            except RuntimeFault as exc:
                post_guard = exc.code
            trial = {
                "trial_id": trial_id,
                "scope": scope.wire(),
                "experiment_ref": experiment_ref,
                "composition_ref": compositions[arm],
                "task_id": case["case_id"],
                "task_class": case["task_class"],
                "arm": arm,
                "repeat": ctx["repeat"],
                "mode": plan["mode"],
                "executor_id": self.executor_id,
                "qualification_ref": policy.qualification_ref,
                "environment_ref": plan["environment_ref"],
                "environment_digest": head["data"]["environment_digest"],
                "success": observation.success,
                "safety_failures": observation.safety_failures,
                "unknown_effects": observation.unknown_effects,
                "artifact_refs": list(observation.artifact_refs),
                "cost_microunits": observation.cost_microunits,
                "input_tokens": observation.input_tokens,
                "output_tokens": observation.output_tokens,
                "usage_status": observation.usage_status,
                "elapsed_ms": round((time.monotonic() - began) * 1000, 6),
                "finished_at": now(),
                "error_type": error_type,
                "post_execution_guard": post_guard,
            }
            tokens = (
                observation.input_tokens + observation.output_tokens
                if observation.input_tokens is not None and observation.output_tokens is not None
                else None
            )
            with self.store.tx() as db:
                ref = self.store.put(db, scope, "eval-trial", trial_id, 1, trial)
                th = self.store.head(scope, "eval-trial", trial_id, db=db)
                self.store.cas(
                    db,
                    scope,
                    "eval-trial",
                    trial_id,
                    th["row_version"],
                    "unknown" if observation.unknown_effects else "observed",
                    {**th["data"], "trial_ref": ref},
                )
                settlement = self.budgets.settle(
                    db,
                    scope,
                    proposal_id,
                    trial_id,
                    tokens=tokens,
                    cost=observation.cost_microunits,
                    uncertain=bool(observation.unknown_effects),
                    cost_required=analysis.get("cost_basis", "compared") == "compared",
                )
                current = self.store.head(scope, "experiment", plan["experiment_id"], db=db)
                self.store.cas(
                    db,
                    scope,
                    "experiment",
                    plan["experiment_id"],
                    current["row_version"],
                    "running",
                    {**current["data"], "trial_refs": [*current["data"]["trial_refs"], ref]},
                )
                self.store.event(
                    db,
                    scope,
                    "release",
                    plan["experiment_id"],
                    "experiment.trial_observed",
                    {"trial_ref": ref, "mode": plan["mode"]},
                )
            trials.append(trial)
            refs.append(ref)
            if post_guard:
                stop(post_guard)
            elif observation.safety_failures or observation.unknown_effects:
                stop("safety_or_unknown_effect")
            elif settlement["overrun"] or settlement["uncertain"]:
                stop("budget_overrun_or_unknown_usage")
            elif time.monotonic() - started >= wall:
                stop("META_WALL_BUDGET")

        # Dispatch order: case, repeat, arm rotation (§7.7); trial refs in completion order.
        run_bounded(
            (
                (case, repeat, arm)
                for case in cases
                for repeat in range(analysis["repeats_per_task"])
                for arm in arm_order(arms, repeat)
            ),
            parallel=workers,
            dispatch=dispatch,
            execute=execute,
            record=record,
            stopped=lambda: state["stop"] is not None,
        )
        stop_reason: str | None = state["stop"]
        drifted: bool = state["drifted"]
        result = analyze_pairs(
            trials,
            analysis,
            expected_tasks=[c["case_id"] for c in cases],
            environment_drifted=drifted,
            contamination=self.corpus.contamination(scope, plan["corpus_ref"]),
        )
        if plan["mode"] in {"static", "replay"}:
            result["verdict"] = "inconclusive"
            result["reasons"].append("static_or_replay_does_not_establish_live_policy_improvement")
        if stop_reason:
            result["reasons"].append(stop_reason)
            if result["verdict"] != "fail":
                result["verdict"] = (
                    "aborted" if stop_reason != "ENVIRONMENT_DRIFT" else "inconclusive"
                )
        if analysis.get("purpose", "exploratory") == "exploratory" and result["verdict"] == "pass":
            result["verdict"] = "inconclusive"
            result["reasons"].append("exploratory_not_confirmatory")
        result["budget"] = self.budgets.totals(scope, proposal_id)
        result["execution_mode"] = plan["mode"]
        result["executor_id"] = self.executor_id
        artifact = self.artifacts.admit(
            scope, canonical(result), "application/json", trust="verifier"
        )
        if not refs:
            # An eval-report requires at least one actual trial. Do not fabricate
            # a run or weaken the approved schema to label a pre-dispatch hold.
            with self.store.tx() as db:
                current = self.store.head(scope, "experiment", plan["experiment_id"], db=db)
                stop_ref = self.store.put(
                    db,
                    scope,
                    "experiment-stop",
                    new_id("experiment-stop"),
                    1,
                    {
                        "scope": scope.wire(),
                        "experiment_ref": experiment_ref,
                        "reason": stop_reason or "no_trials",
                        "outcome": result["verdict"],
                        "analysis_artifact": artifact,
                        "dispatched_trials": 0,
                        "issued_at": now(),
                    },
                )
                self.store.cas(
                    db,
                    scope,
                    "experiment",
                    plan["experiment_id"],
                    current["row_version"],
                    "inconclusive" if drifted else "aborted",
                    {**current["data"], "stop_ref": stop_ref},
                )
                self.store.event(
                    db,
                    scope,
                    "release",
                    plan["experiment_id"],
                    "experiment.admission_stopped",
                    {"stop_ref": stop_ref, "dispatched_trials": 0},
                )
            raise Hold(
                stop_reason or "NO_TRIALS",
                "No trial dispatched; immutable stop evidence saved",
                details={"stop_ref": stop_ref},
            )
        unsafe = any(t["safety_failures"] or t["unknown_effects"] for t in trials)
        report = {
            "schema_version": "3.0.0",
            "report_id": new_id("eval-report"),
            "scope": scope.wire(),
            "experiment_ref": experiment_ref,
            "run_refs": refs,
            "analysis_artifact": artifact,
            "verdict": result["verdict"],
            "safety_gate_results": [
                {
                    "gate_id": "G-17",
                    "outcome": "fail" if unsafe else "pass",
                    "evidence_refs": refs,
                    "reason": "Observed safety failures/unknown effects"
                    if unsafe
                    else "No safety incident observed in this sample",
                }
            ],
            "environment_drifted": drifted,
            "contamination_findings": [
                {
                    "id": "corpus-contamination",
                    "severity": "blocking",
                    "code": "CORPUS_CONTAMINATION",
                    "statement": "Corpus exposure or contamination detected",
                    "source_refs": [plan["corpus_ref"]],
                }
            ]
            if self.corpus.contamination(scope, plan["corpus_ref"])
            else [],
            "limitations": [
                "Executor: " + self.executor_id,
                "Mode: " + plan["mode"],
                "Secrecy boundary: local ACL; no claim of independently hidden holdout.",
                "Purpose: " + analysis.get("purpose", "exploratory"),
                "Mode/executor qualification is not real provider or production qualification.",
            ],
            "independent_review_ref": None,
            "issued_at": now(),
        }
        self.contracts.validate("eval-report", report)
        with self.store.tx() as db:
            ref = self.store.put(db, scope, "eval-report", report["report_id"], 1, report)
            current = self.store.head(scope, "experiment", plan["experiment_id"], db=db)
            if current["state"] != "running" or current["data"]["trial_refs"] != refs:
                raise Hold(
                    "EXPERIMENT_CHANGED",
                    "Do not overwrite an interrupted or concurrently changed experiment",
                )
            self.store.cas(
                db,
                scope,
                "experiment",
                plan["experiment_id"],
                current["row_version"],
                "evaluated",
                {
                    **current["data"],
                    "report_ref": ref,
                    "verdict": report["verdict"],
                    "evaluated_by": actor.subject_id,
                },
            )
            self.store.event(
                db,
                scope,
                "release",
                plan["experiment_id"],
                "experiment.evaluated",
                {"report_ref": ref, "verdict": report["verdict"], "mode": plan["mode"]},
            )
        return ref

    def _validate_observation(
        self,
        scope: Scope,
        observation: TrialObservation,
        composition_ref: dict[str, Any],
        task_id: str,
        repeat: int,
        mode: str,
    ) -> None:
        validate_observation(
            self.artifacts, scope, observation, composition_ref, task_id, repeat, mode
        )

    def recover_interrupted(self, actor: Actor, experiment_ref: dict[str, Any]) -> dict[str, Any]:
        actor.require("experiment.reconcile")
        plan = self.store.get(actor.scope, "eval-experiment", experiment_ref)
        self._independent(actor, plan)
        with self.store.tx() as db:
            head = self.store.head(actor.scope, "experiment", plan["experiment_id"], db=db)
            if head["state"] != "running":
                raise Hold(
                    "EXPERIMENT_STATE", "Only an interrupted running experiment needs recovery"
                )
            if head["data"].get("owner_epoch") == self.store.epoch:
                raise Hold(
                    "EXPERIMENT_OWNER",
                    "The current owner may still be running; do not race its executor",
                )
            self.store.cas(
                db,
                actor.scope,
                "experiment",
                plan["experiment_id"],
                head["row_version"],
                "interrupted",
                {**head["data"], "reason": "prior_owner_interrupted_no_automatic_reexecution"},
            )
            self.store.event(
                db,
                actor.scope,
                "release",
                plan["experiment_id"],
                "experiment.interrupted",
                {"experiment_ref": experiment_ref, "rerun_permitted": False},
            )
        return {
            "state": "interrupted",
            "rerun_permitted": False,
            "pending_usage_requires_reconciliation": True,
            "prior_trial_refs": head["data"]["trial_refs"],
        }
