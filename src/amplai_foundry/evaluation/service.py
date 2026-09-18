"""Immutable, independently approved experiments with durable pre-call reservations.

Modes are distinct. A replay result never establishes a live improvement. A
registered executor is trusted server code, not a worker-supplied callback. Its
budget/containment qualification must be supplied by the operator before use.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from amplai_foundry.meta_harness.budget import EvolutionBudget
from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id, now
from amplai_foundry.runtime.contracts.semantics import check_refs, resolve_ref
from amplai_foundry.runtime.errors import Hold, RuntimeFault

from .analysis import analyze_pairs, validate_analysis_plan
from .corpus import CorpusService
from .receipts import check_observation, read_receipt


@dataclass(frozen=True)
class TrialObservation:
    success: bool | None
    artifact_refs: tuple
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
    qualification_ref: dict
    external_effects: bool = False

    def validate(self):
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


class EvaluationService:
    def __init__(
        self,
        store,
        contracts,
        artifacts,
        *,
        approval_check,
        executor_id: str,
        environment_probe: Callable | None = None,
        executor_policy: ExecutorPolicy | None = None,
    ):
        self.store, self.contracts, self.artifacts = store, contracts, artifacts
        self.approval_check, self.executor_id = approval_check, executor_id
        self.environment_probe, self.executor_policy = environment_probe, executor_policy
        self.corpus, self.budgets = CorpusService(store, artifacts), EvolutionBudget(store)

    @staticmethod
    def approval_subject(plan: dict):
        return {k: v for k, v in plan.items() if k != "approval_ref"}

    def _independent(self, actor, plan):
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

    def _guard(self, actor, plan):
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

    def freeze(self, actor, plan: dict) -> dict:
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
        validate_analysis_plan(analysis["policy"])
        if plan["primary_endpoint"] != "task_all_repeats_pass":
            raise Hold(
                "ENDPOINT_UNQUALIFIED",
                "Only the qualified paired binary primary endpoint is enabled",
            )
        _, sampling = resolve_ref(self.store, actor.scope, plan["sampling_plan_ref"])
        cases = self.corpus.select(
            actor, plan["corpus_ref"], sampling.get("split"), purpose="frozen_experiment"
        )
        if not cases or sampling.get("case_ids") != [c["case_id"] for c in cases]:
            raise Hold(
                "SAMPLING_CHANGED",
                "Pin the complete case IDs/order for the selected split before execution",
            )
        if len(cases) * 2 * analysis["policy"]["repeats_per_task"] > 256:
            raise Hold(
                "REPORT_CAPACITY",
                "The pinned report schema admits at most 256 trial references; reduce the frozen sample",
            )
        if (
            analysis["policy"].get("purpose") == "confirmatory"
            and sampling["split"] == "development"
        ):
            raise Hold(
                "CONFIRMATORY_SPLIT", "Development cases cannot qualify confirmatory improvement"
            )
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

    def _validate_executor(self, scope, mode):
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

    def run(self, actor, experiment_ref: dict, executor, *, split: str = "validation") -> dict:
        actor.require("experiment.run")
        scope = actor.scope
        plan = self.store.get(scope, "eval-experiment", experiment_ref)
        self._guard(actor, plan)
        policy = self._validate_executor(scope, plan["mode"])
        _, analysis_obj = resolve_ref(self.store, scope, plan["analysis_plan_ref"])
        analysis = analysis_obj["policy"]
        _, sampling = resolve_ref(self.store, scope, plan["sampling_plan_ref"])
        if sampling.get("split") != split:
            raise Hold("SAMPLING_FROZEN", "Cannot change split after experiment freeze")
        cases = self.corpus.select(actor, plan["corpus_ref"], split, purpose="frozen_experiment")
        if sampling["case_ids"] != [c["case_id"] for c in cases]:
            raise Hold("SAMPLING_CHANGED", "Case set/order differs from frozen sampling plan")
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
        trials, refs = [], []
        drifted, stop_reason = False, None
        started = time.monotonic()
        proposal_id = plan["proposal_ref"]["id"]
        for case in cases:
            for repeat in range(analysis["repeats_per_task"]):
                arms = ["baseline", "candidate"] if repeat % 2 == 0 else ["candidate", "baseline"]
                for arm in arms:
                    try:
                        self._guard(actor, plan)
                        if time.monotonic() - started >= plan["budget"]["max_wall_seconds"]:
                            raise Hold("META_WALL_BUDGET", "Experiment wall budget reached")
                        _, env = resolve_ref(self.store, scope, plan["environment_ref"])
                        observed_env = (
                            self.environment_probe(env) if self.environment_probe else env
                        )
                        if digest(observed_env) != head["data"]["environment_digest"]:
                            drifted = True
                            raise Hold("ENVIRONMENT_DRIFT", "Environment changed since freeze")
                        trial_id = new_id("trial")
                        with self.store.tx() as db:
                            self.budgets.reserve(
                                db,
                                scope,
                                proposal_id,
                                trial_id,
                                tokens=policy.max_trial_tokens,
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
                        stop_reason = exc.code
                        break
                    began, error_type = time.monotonic(), None
                    self.store.assert_outside_tx()
                    try:
                        observation = executor(plan[arm + "_ref"], case, repeat, plan["mode"])
                        self._validate_observation(
                            scope,
                            observation,
                            plan[arm + "_ref"],
                            case["case_id"],
                            repeat,
                            plan["mode"],
                        )
                    except Exception as exc:
                        # The process may already have written; uncertain effects/usage stay explicit.
                        error_type = type(exc).__name__
                        observation = TrialObservation(
                            None, (), unknown_effects=1, usage_status="unknown"
                        )
                    post_guard = None
                    try:
                        self._guard(actor, plan)
                        _, env = resolve_ref(self.store, scope, plan["environment_ref"])
                        if (
                            digest(self.environment_probe(env) if self.environment_probe else env)
                            != head["data"]["environment_digest"]
                        ):
                            drifted = True
                            post_guard = "ENVIRONMENT_DRIFT"
                    except RuntimeFault as exc:
                        post_guard = exc.code
                    trial = {
                        "trial_id": trial_id,
                        "scope": scope.wire(),
                        "experiment_ref": experiment_ref,
                        "composition_ref": plan[arm + "_ref"],
                        "task_id": case["case_id"],
                        "task_class": case["task_class"],
                        "arm": arm,
                        "repeat": repeat,
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
                        if observation.input_tokens is not None
                        and observation.output_tokens is not None
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
                        )
                        current = self.store.head(scope, "experiment", plan["experiment_id"], db=db)
                        self.store.cas(
                            db,
                            scope,
                            "experiment",
                            plan["experiment_id"],
                            current["row_version"],
                            "running",
                            {
                                **current["data"],
                                "trial_refs": [*current["data"]["trial_refs"], ref],
                            },
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
                        stop_reason = post_guard
                    elif observation.safety_failures or observation.unknown_effects:
                        stop_reason = "safety_or_unknown_effect"
                    elif settlement["overrun"] or settlement["uncertain"]:
                        stop_reason = "budget_overrun_or_unknown_usage"
                    elif time.monotonic() - started >= plan["budget"]["max_wall_seconds"]:
                        stop_reason = "META_WALL_BUDGET"
                    if stop_reason:
                        break
                if stop_reason:
                    break
            if stop_reason:
                break
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

    def _validate_observation(self, scope, observation, composition_ref, task_id, repeat, mode):
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
        for value in (
            observation.cost_microunits,
            observation.input_tokens,
            observation.output_tokens,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise RuntimeFault(
                    "TRIAL_USAGE", "Usage is nonnegative integer or explicitly unknown"
                )
        if observation.usage_status not in {"measured", "estimated", "unknown"}:
            raise RuntimeFault("TRIAL_USAGE", "Unknown usage classification")
        for ref in observation.artifact_refs:
            self.artifacts.read(scope, ref, trusted=True)
        receipt = read_receipt(
            self.artifacts.read(scope, observation.artifact_refs[0], trusted=True)
        )
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

    def recover_interrupted(self, actor, experiment_ref: dict) -> dict:
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
