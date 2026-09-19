"""Evidence-backed evolution: independent gates, bounded canary and atomic releases.

Only new admissions change on promotion. Existing runs retain their composition.
Release rollback never restores grants, secrets, data or already committed effects.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from amplai_foundry.evaluation.analysis import analyze_pairs
from amplai_foundry.evaluation.corpus import CorpusService
from amplai_foundry.evaluation.receipts import check_observation, read_receipt
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.gates import Observation, StateMachines
from amplai_foundry.runtime.contracts.identity import digest, new_id, now, verify_signature
from amplai_foundry.runtime.contracts.semantics import check_refs, resolve_ref
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.storage.store import Scope, Store

from .budget import EvolutionBudget
from .composition import CompositionService

PROTECTED_PATHS = (
    "src/amplai_foundry/governance/",
    "src/amplai_foundry/runtime/contracts/",
    "src/amplai_foundry/runtime/budgets/",
    "src/amplai_foundry/runtime/effects/",
    "src/amplai_foundry/runtime/storage/",
    "src/amplai_foundry/verification/",
    "src/amplai_foundry/evaluation/",
    "src/amplai_foundry/meta_harness/",
    "eval/holdout/",
    "contracts/invariant-registry",
    "contracts/gate-matrix",
    "contracts/state-machines",
)
CANARY_FIELDS = {
    "eligible_task_ids",
    "max_runs",
    "max_wall_seconds",
    "max_cost_microunits",
    "max_trial_cost_microunits",
    "max_trial_tokens",
    "max_concurrent",
    "project_opt_in",
    "eligible_risk_classes",
    "target_binding_refs",
    "abort_on_safety_failure",
    "abort_on_unknown_effect",
    "fallback_release_ref",
}


class MetaHarness:
    def __init__(
        self,
        store: Store,
        contracts: Any,
        artifacts: ArtifactStore,
        *,
        approval_check: Callable[..., Any],
        trusted_release_keys: dict[str, Ed25519PublicKey],
    ) -> None:
        self.store, self.contracts, self.artifacts = store, contracts, artifacts
        self.approval_check, self.keys = approval_check, trusted_release_keys
        self.compositions = CompositionService(store, contracts)
        self.machines, self.budgets = StateMachines(contracts), EvolutionBudget(store)

    def _move(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        proposal_id: str,
        command: str,
        observations: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> str:
        head = self.store.head(scope, "evolution", proposal_id, db=db)
        state, gates = self.machines.transition("evolution", head["state"], command, observations)
        data = {**head["data"], **(extra or {}), "gate_results": gates}
        self.store.cas(db, scope, "evolution", proposal_id, head["row_version"], state, data)
        self.store.event(
            db,
            scope,
            "release",
            proposal_id,
            "evolution." + state,
            {"command": command, "gate_results": gates},
        )
        return state

    def _independent(self, actor: Actor, head: dict[str, Any]) -> None:
        if (
            actor.subject_id == head["data"]["proposer_id"]
            or "harness.propose" in actor.permissions
        ):
            raise Hold(
                "SELF_APPROVAL",
                "A proposer identity cannot review, approve, execute or promote a candidate",
            )

    def submit(self, actor: Actor, proposal: dict[str, Any]) -> dict[str, Any]:
        actor.require("harness.propose")
        self.contracts.validate("harness-change-proposal", proposal)
        if (
            proposal["scope"] != actor.scope.wire()
            or proposal["proposer"] != actor.wire()
            or proposal["status"] != "draft"
        ):
            raise RuntimeFault(
                "PROPOSAL_ATTRIBUTION", "Draft must be attributed to the authenticated actor"
            )
        check_refs(self.store, actor.scope, proposal)
        self.artifacts.read(actor.scope, proposal["change_artifact"])
        classification = self.compositions.classify(
            actor.scope, proposal["baseline_ref"], proposal["candidate_ref"]
        )
        if proposal["surface_class"] != classification["surface_class"]:
            raise Hold(
                "SURFACE_CLASSIFICATION",
                "Declared surface differs from actual immutable component diff",
            )
        with self.store.tx() as db:
            ref = self.store.put(
                db, actor.scope, "harness-change-proposal", proposal["proposal_id"], 1, proposal
            )
            self.store.cas(
                db,
                actor.scope,
                "evolution",
                proposal["proposal_id"],
                0,
                "draft",
                {
                    "proposal_ref": ref,
                    "proposer_id": actor.subject_id,
                    "classification": classification,
                },
            )
        return ref

    def record_review(
        self, actor: Actor, proposal_id: str, review_artifact: dict[str, Any]
    ) -> dict[str, Any]:
        actor.require("harness.review")
        head = self.store.head(actor.scope, "evolution", proposal_id)
        self._independent(actor, head)
        if actor.kind != "human":
            raise Hold(
                "HUMAN_CODE_REVIEW",
                "Behavior/runtime changes require an independent human code review",
            )
        proposal = self.store.get(
            actor.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        value = read_receipt(self.artifacts.read(actor.scope, review_artifact, trusted=True))
        check_observation(
            value,
            {
                "proposal_ref": head["data"]["proposal_ref"],
                "candidate_ref": proposal["candidate_ref"],
                "reviewer_subject_id": actor.subject_id,
                "outcome": "pass",
                "protected_controls_changed": False,
            },
        )
        with self.store.tx() as db:
            ref = self.store.put(
                db,
                actor.scope,
                "harness-review",
                new_id("review"),
                1,
                {
                    "proposal_ref": head["data"]["proposal_ref"],
                    "artifact_ref": review_artifact,
                    "reviewer": actor.wire(),
                    "issued_at": now(),
                },
            )
            self.store.cas(
                db,
                actor.scope,
                "evolution",
                proposal_id,
                head["row_version"],
                head["state"],
                {**head["data"], "review_ref": ref},
            )
        return ref

    def screen(self, actor: Actor, proposal_id: str) -> dict[str, Any]:
        actor.require("harness.review")
        scope, head = actor.scope, self.store.head(actor.scope, "evolution", proposal_id)
        self._independent(actor, head)
        proposal = self.store.get(scope, "harness-change-proposal", head["data"]["proposal_ref"])
        classification = self.compositions.classify(
            scope, proposal["baseline_ref"], proposal["candidate_ref"]
        )
        change = read_receipt(self.artifacts.read(scope, proposal["change_artifact"]))
        check_observation(
            change,
            {"baseline_ref": proposal["baseline_ref"], "candidate_ref": proposal["candidate_ref"]},
        )
        paths = change.get("changed_paths")
        if (
            not isinstance(paths, list)
            or not paths
            or any(not isinstance(p, str) for p in paths)
            or len(set(paths)) != len(paths)
        ):
            raise RuntimeFault("CHANGE_PATH", "Change manifest must list unique paths")
        for p in paths:
            if (
                not isinstance(p, str)
                or not p
                or "\\" in p
                or "\x00" in p
                or ":" in p
                or PurePosixPath(p).is_absolute()
                or ".." in p.split("/")
                or "." in p.split("/")
                or "" in p.split("/")
            ):
                raise RuntimeFault("CHANGE_PATH", "Invalid or noncanonical candidate change path")
        protected = [p for p in paths if any(p.startswith(prefix) for prefix in PROTECTED_PATHS)]
        if (
            classification["surface_class"] in {"C", "D"}
            or protected
            or proposal["protected_surface_findings"]
        ):
            raise Hold(
                "PROTECTED_META_SURFACE",
                "Protected controls require a separate governed engineering Work",
            )
        if classification["surface_class"] == "B" and not head["data"].get("review_ref"):
            raise Hold(
                "CODE_REVIEW_REQUIRED",
                "Behavior/runtime changes require independent human review before experiments",
            )
        if not proposal["observation_refs"] or not classification["changed_fields"]:
            raise Hold(
                "NO_EVIDENCE_CHANGE",
                "A hypothesis needs observations and an actual component change",
            )
        obs = {
            "G-16": Observation.check(True, "Exact immutable change and evidence references"),
            "G-22": Observation.check(
                True, "Protected surfaces unchanged; component refs are the actual applied diff"
            ),
        }
        with self.store.tx() as db:
            state = self._move(db, scope, proposal_id, "screen", obs)
        return {"state": state, "classification": classification}

    def approve_experiment(
        self,
        actor: Actor,
        proposal_id: str,
        approval_ref: dict[str, Any],
        experiment_ref: dict[str, Any],
    ) -> dict[str, Any]:
        actor.require("experiment.approve")
        scope, head = actor.scope, self.store.head(actor.scope, "evolution", proposal_id)
        self._independent(actor, head)
        exp = self.store.get(scope, "eval-experiment", experiment_ref)
        if exp["approval_ref"] != approval_ref:
            raise Hold("EXPERIMENT_APPROVAL", "Experiment names another approval receipt")
        self.approval_check(
            scope,
            approval_ref,
            "experiment.execute",
            digest({k: v for k, v in exp.items() if k != "approval_ref"}),
        )
        proposal = self.store.get(scope, "harness-change-proposal", head["data"]["proposal_ref"])
        if (
            exp["proposal_ref"] != head["data"]["proposal_ref"]
            or exp["baseline_ref"] != proposal["baseline_ref"]
            or exp["candidate_ref"] != proposal["candidate_ref"]
        ):
            raise Hold("EXPERIMENT_BINDING", "Experiment differs from the screened proposal")
        if self.store.head(scope, "experiment", exp["experiment_id"])["state"] != "frozen":
            raise Hold("EXPERIMENT_STATE", "Only the actual frozen experiment may be authorized")
        obs = {
            "G-14": Observation.check(True, "Independent current governed approval"),
            "G-16": Observation.check(True, "Frozen experiment bound to the screened proposal"),
        }
        with self.store.tx() as db:
            state = self._move(
                db,
                scope,
                proposal_id,
                "approve_experiment",
                obs,
                {"experiment_ref": experiment_ref, "experiment_approval_ref": approval_ref},
            )
        return {"state": state}

    def start_offline(self, actor: Actor, proposal_id: str) -> str:
        actor.require("experiment.run")
        head = self.store.head(actor.scope, "evolution", proposal_id)
        self._independent(actor, head)
        self._check_kill(actor.scope)
        exp = self.store.get(actor.scope, "eval-experiment", head["data"]["experiment_ref"])
        self.approval_check(
            actor.scope,
            exp["approval_ref"],
            "experiment.execute",
            digest({k: v for k, v in exp.items() if k != "approval_ref"}),
        )
        obs = {
            "G-08": Observation.check(True, "Persisted bounded root budget"),
            "G-16": Observation.check(
                True, "Current independent approval and immutable experiment"
            ),
        }
        with self.store.tx() as db:
            return self._move(db, actor.scope, proposal_id, "start_offline", obs)

    def _report(
        self, scope: Scope, report_ref: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        report = self.store.get(scope, "eval-report", report_ref)
        self.contracts.validate("eval-report", report)
        exp = self.store.get(scope, "eval-experiment", report["experiment_ref"])
        head = self.store.head(scope, "experiment", exp["experiment_id"])
        if (
            head["state"] != "evaluated"
            or head["data"].get("report_ref") != report_ref
            or head["data"]["trial_refs"] != report["run_refs"]
        ):
            raise Hold(
                "REPORT_PROVENANCE",
                "Report must be the exact independently executed experiment output",
            )
        analysis = read_receipt(
            self.artifacts.read(scope, report["analysis_artifact"], trusted=True)
        )
        trials = [self.store.get(scope, "eval-trial", ref) for ref in report["run_refs"]]
        _, ap = resolve_ref(self.store, scope, exp["analysis_plan_ref"])
        _, sample = resolve_ref(self.store, scope, exp["sampling_plan_ref"])
        for trial in trials:
            if trial["experiment_ref"] != report["experiment_ref"] or trial["mode"] != exp["mode"]:
                raise Hold("TRIAL_PROVENANCE", "Foreign trials cannot be substituted into a report")
            for artifact in trial["artifact_refs"]:
                self.artifacts.read(scope, artifact, trusted=True)
        recomputed = analyze_pairs(
            trials,
            ap["policy"],
            expected_tasks=sample["case_ids"],
            environment_drifted=report["environment_drifted"],
            contamination=bool(report["contamination_findings"]),
        )
        if report["verdict"] == "pass" and (
            analysis.get("verdict") != "pass" or recomputed["verdict"] != "pass"
        ):
            raise Hold(
                "REPORT_RECOMPUTE",
                "Recorded passing report is not supported by its immutable observations",
            )
        return report, analysis, exp

    def evaluate(self, actor: Actor, proposal_id: str, report_ref: dict[str, Any]) -> str:
        actor.require("harness.review")
        head = self.store.head(actor.scope, "evolution", proposal_id)
        self._independent(actor, head)
        report, _, _ = self._report(actor.scope, report_ref)
        if report["experiment_ref"] != head["data"]["experiment_ref"]:
            raise Hold("REPORT_BINDING", "Report evaluates another experiment")
        obs = {
            "G-11": Observation.check(
                True, "Report bytes, originating execution and trial refs verified"
            ),
            "G-16": Observation.check(True, "Report bound to exact frozen experiment"),
        }
        with self.store.tx() as db:
            return self._move(
                db,
                actor.scope,
                proposal_id,
                "evaluate",
                obs,
                {
                    "report_ref": report_ref,
                    "verdict": report["verdict"],
                    "reviewer_id": actor.subject_id,
                },
            )

    def _passing_report(self, scope: Scope, report_ref: dict[str, Any]) -> dict[str, Any]:
        report, analysis, exp = self._report(scope, report_ref)
        if (
            report["verdict"] != "pass"
            or analysis.get("verdict") != "pass"
            or report["environment_drifted"]
            or report["contamination_findings"]
        ):
            raise Hold(
                "EVAL_NOT_PASSING",
                "Fail/inconclusive/aborted/drifted/contaminated results cannot promote",
            )
        if not report["safety_gate_results"] or any(
            g["outcome"] != "pass" for g in report["safety_gate_results"]
        ):
            raise Hold("SAFETY_GATE", "Safety gates did not all pass")
        if exp["mode"] in {"static", "replay", "canary"}:
            raise Hold("REPLAY_NOT_CAUSAL", "Replay/static alone cannot support live promotion")
        _, policy = resolve_ref(self.store, scope, exp["analysis_plan_ref"])
        purpose = policy["policy"].get("purpose", "exploratory")
        if purpose != "confirmatory" and not (
            purpose == "local_qualification" and scope.tenant_id == "demo-local"
        ):
            raise Hold(
                "CONFIRMATORY_REQUIRED",
                "Local drills and exploratory results cannot authorize production promotion",
            )
        if CorpusService(self.store, self.artifacts).contamination(scope, exp["corpus_ref"]):
            raise Hold(
                "HOLDOUT_CONTAMINATED", "Corpus was contaminated after the report was issued"
            )
        budget = self.budgets.totals(scope, exp["proposal_ref"]["id"])
        if budget["unknown"] or budget["pending"] or budget["overruns"]:
            raise Hold(
                "META_BUDGET_UNSETTLED",
                "All evaluation/canary spending must be settled without overruns",
            )
        return report

    def _canary_policy(self, scope: Scope, policy_ref: dict[str, Any]) -> dict[str, Any]:
        _, p = resolve_ref(self.store, scope, policy_ref)
        if not (CANARY_FIELDS | {"task_binding_refs"}) <= set(p):
            raise Hold(
                "CANARY_POLICY",
                "Declare opt-in, eligible risk/targets, all budgets, concurrency, "
                "abort and fallback",
            )
        for k in ("max_runs", "max_wall_seconds", "max_concurrent"):
            if type(p[k]) is not int or p[k] < 1:
                raise Hold("CANARY_POLICY", "Canary limits must be positive integers")
        for k in ("max_cost_microunits", "max_trial_cost_microunits", "max_trial_tokens"):
            if type(p[k]) is not int or p[k] < 0:
                raise Hold("CANARY_POLICY", "Canary budgets must be finite nonnegative integers")
        if (
            p["project_opt_in"] is not True
            or p["abort_on_safety_failure"] is not True
            or p["abort_on_unknown_effect"] is not True
        ):
            raise Hold(
                "CANARY_POLICY", "Explicit project opt-in and hard safety aborts are mandatory"
            )
        if p["eligible_risk_classes"] != ["low"]:
            raise Hold("CANARY_RISK", "The qualified canary path admits low-risk workloads only")
        tasks = p["eligible_task_ids"]
        if (
            not isinstance(tasks, list)
            or not tasks
            or any(not isinstance(t, str) or not t for t in tasks)
            or len(set(tasks)) != len(tasks)
            or p["max_runs"] > len(tasks)
            or p["max_concurrent"] > p["max_runs"]
        ):
            raise Hold(
                "CANARY_POLICY",
                "Canary task IDs must be unique and cover its bounded run population",
            )
        if not p["target_binding_refs"] or len(
            {digest(x) for x in p["target_binding_refs"]}
        ) != len(p["target_binding_refs"]):
            raise Hold("CANARY_TARGETS", "An explicit unique set of target bindings is required")
        for ref in p["target_binding_refs"]:
            self.store.get(scope, "app-binding", ref)
        mapping = p["task_binding_refs"]
        if (
            not isinstance(mapping, dict)
            or set(mapping) != set(tasks)
            or any(ref not in p["target_binding_refs"] for ref in mapping.values())
        ):
            raise Hold(
                "CANARY_TASK_TARGET", "Every eligible task must bind an exact approved target"
            )
        self._release(scope, p["fallback_release_ref"])
        return p

    def _canary_authority(self, actor: Actor, head: dict[str, Any]) -> None:
        self._independent(actor, head)
        self._check_kill(actor.scope)
        self.approval_check(
            actor.scope,
            head["data"]["canary_approval_ref"],
            "canary.execute",
            digest(
                {
                    "proposal_ref": head["data"]["proposal_ref"],
                    "report_ref": head["data"]["report_ref"],
                    "policy_ref": head["data"]["canary_policy_ref"],
                }
            ),
        )

    def approve_canary(
        self,
        actor: Actor,
        proposal_id: str,
        policy_ref: dict[str, Any],
        approval_ref: dict[str, Any],
    ) -> str:
        actor.require("canary.approve")
        scope, head = actor.scope, self.store.head(actor.scope, "evolution", proposal_id)
        self._independent(actor, head)
        self._passing_report(scope, head["data"]["report_ref"])
        policy = self._canary_policy(scope, policy_ref)
        if (
            self.store.head(scope, "release-pointer", "active")["data"]["release_ref"]
            != policy["fallback_release_ref"]
        ):
            raise Hold("CANARY_BASELINE", "Fallback must be the actual current release")
        self.approval_check(
            scope,
            approval_ref,
            "canary.execute",
            digest(
                {
                    "proposal_ref": head["data"]["proposal_ref"],
                    "report_ref": head["data"]["report_ref"],
                    "policy_ref": policy_ref,
                }
            ),
        )
        obs = {
            "G-14": Observation.check(True, "Independent canary approval"),
            "G-17": Observation.check(
                True, "Recomputed evaluation, no safety or budget violations"
            ),
            "G-18": Observation.check(True, "Opt-in, exact population, bounded spend and fallback"),
        }
        with self.store.tx() as db:
            return self._move(
                db,
                scope,
                proposal_id,
                "approve_canary",
                obs,
                {
                    "canary_policy_ref": policy_ref,
                    "canary_approval_ref": approval_ref,
                    "canary_trials": [],
                    "canary_pending": {},
                    "canary_incidents": [],
                },
            )

    def start_canary(self, actor: Actor, proposal_id: str) -> str:
        actor.require("canary.run")
        head = self.store.head(actor.scope, "evolution", proposal_id)
        self._canary_authority(actor, head)
        self._passing_report(actor.scope, head["data"]["report_ref"])
        obs = {
            "G-08": Observation.check(True, "Persisted bounded root budget"),
            "G-18": Observation.check(True, "Current approval and kill switch checked"),
        }
        with self.store.tx() as db:
            return self._move(
                db,
                actor.scope,
                proposal_id,
                "start_canary",
                obs,
                {
                    "canary_started_epoch": self.store.clock(),
                    "canary_owner_epoch": self.store.epoch,
                },
            )

    def canary_trial(
        self,
        actor: Actor,
        proposal_id: str,
        task_id: str,
        execute: Callable[[str], dict[str, Any]],
    ) -> dict[str, Any]:
        actor.require("canary.run")
        scope = actor.scope
        head = self.store.head(scope, "evolution", proposal_id)
        self._canary_authority(actor, head)
        if head["state"] != "canary_running":
            raise Hold("CANARY_STATE", "Canary is not accepting runs")
        if head["data"]["canary_owner_epoch"] != self.store.epoch:
            raise Hold(
                "CANARY_RECOVERY_REQUIRED",
                "A prior-owner canary must reconcile before new execution",
            )
        policy = self._canary_policy(scope, head["data"]["canary_policy_ref"])
        if self.store.clock() - head["data"]["canary_started_epoch"] >= policy["max_wall_seconds"]:
            return self.abort(actor, proposal_id, "wall_budget")
        proposal = self.store.get(scope, "harness-change-proposal", head["data"]["proposal_ref"])
        with self.store.tx() as db:
            current = self.store.head(scope, "evolution", proposal_id, db=db)
            if current["state"] != "canary_running":
                raise Hold("CANARY_STATE", "Canary admission changed")
            data = current["data"]
            pending, prior = data["canary_pending"], data["canary_trials"]
            if (
                task_id not in policy["eligible_task_ids"]
                or len(prior) + len(pending) >= policy["max_runs"]
            ):
                raise Hold("CANARY_POPULATION", "Task not eligible or bounded workload exhausted")
            if task_id in pending or any(t["task_id"] == task_id for t in prior):
                raise Hold("CANARY_DUPLICATE", "A canary task cannot be executed twice")
            if len(pending) >= policy["max_concurrent"]:
                raise Hold(
                    "CANARY_CONCURRENCY", "Another admitted canary occupies the available slot"
                )
            reserved_cost = len(pending) * policy["max_trial_cost_microunits"]
            spent = sum(t["cost_microunits"] for t in prior)
            if (
                spent + reserved_cost + policy["max_trial_cost_microunits"]
                > policy["max_cost_microunits"]
            ):
                raise Hold("CANARY_COST", "Reserve the worst-case trial cost before execution")
            allocation_id = new_id("canary")
            self.budgets.reserve(
                db,
                scope,
                proposal_id,
                allocation_id,
                tokens=policy["max_trial_tokens"],
                cost=policy["max_trial_cost_microunits"],
            )
            self.store.cas(
                db,
                scope,
                "evolution",
                proposal_id,
                current["row_version"],
                current["state"],
                {
                    **data,
                    "canary_pending": {
                        **pending,
                        task_id: {
                            "allocation_id": allocation_id,
                            "owner_epoch": self.store.epoch,
                            "issued_at": now(),
                        },
                    },
                },
            )
            self.store.event(
                db,
                scope,
                "release",
                proposal_id,
                "canary.admitted",
                {
                    "allocation_id": allocation_id,
                    "task_id": task_id,
                    "candidate_ref": proposal["candidate_ref"],
                },
            )
        result, issue = None, None
        self.store.assert_outside_tx()
        try:
            result = execute(task_id)
            if not isinstance(result, dict):
                raise RuntimeFault("CANARY_RESULT", "Executor did not return a result object")
            for key in ("success",):
                if type(result.get(key)) is not bool:
                    raise RuntimeFault("CANARY_RESULT", "Canary success must be boolean")
            for key in (
                "safety_failures",
                "unknown_effects",
                "cost_microunits",
                "input_tokens",
                "output_tokens",
            ):
                if type(result.get(key)) is not int or result[key] < 0:
                    raise RuntimeFault(
                        "CANARY_RESULT", "Canary counters must be nonnegative known integers"
                    )
            observed = read_receipt(
                self.artifacts.read(scope, result["artifact_ref"], trusted=True)
            )
            check_observation(
                observed,
                {
                    **{
                        k: result[k]
                        for k in (
                            "success",
                            "safety_failures",
                            "unknown_effects",
                            "cost_microunits",
                            "input_tokens",
                            "output_tokens",
                        )
                    },
                    "composition_ref": proposal["candidate_ref"],
                    "task_id": task_id,
                    "mode": "canary",
                    "usage_status": "measured",
                    "scope": scope.wire(),
                    "risk_class": "low",
                    "target_binding_ref": policy["task_binding_refs"][task_id],
                },
            )
            self._canary_authority(actor, self.store.head(scope, "evolution", proposal_id))
            if result["safety_failures"] or result["unknown_effects"]:
                issue = "safety_or_unknown_effect"
            if not result["success"]:
                issue = issue or "canary_verifier_failure"
            if (
                self.store.clock() - head["data"]["canary_started_epoch"]
                >= policy["max_wall_seconds"]
            ):
                issue = issue or "wall_budget"
        except Exception as exc:
            # An exception or malformed receipt is not proof that a spawned action stopped.
            issue = "uncertain_canary_" + type(exc).__name__
            result = None
        with self.store.tx() as db:
            current = self.store.head(scope, "evolution", proposal_id, db=db)
            data = current["data"]
            settle = self.budgets.settle(
                db,
                scope,
                proposal_id,
                allocation_id,
                tokens=result["input_tokens"] + result["output_tokens"] if result else None,
                cost=result["cost_microunits"] if result else None,
                uncertain=result is None or bool(result["unknown_effects"]),
            )
            if settle["overrun"]:
                issue = issue or "canary_budget_overrun"
            pending = {k: v for k, v in data["canary_pending"].items() if k != task_id}
            incident = {"task_id": task_id, "allocation_id": allocation_id, "reason": issue}
            updates = {
                **data,
                "canary_pending": pending,
                "canary_trials": [
                    *data["canary_trials"],
                    {"task_id": task_id, "allocation_id": allocation_id, **result},
                ]
                if result
                else data["canary_trials"],
                "canary_incidents": [*data["canary_incidents"], incident]
                if issue
                else data["canary_incidents"],
            }
            self.store.cas(
                db,
                scope,
                "evolution",
                proposal_id,
                current["row_version"],
                current["state"],
                updates,
            )
            self.store.event(db, scope, "release", proposal_id, "canary.observed", incident)
        if issue:
            return self.abort(actor, proposal_id, issue)
        state = self.store.head(scope, "evolution", proposal_id)
        return {
            "state": state["state"],
            "observed_runs": len(state["data"]["canary_trials"]),
            "allocation_id": allocation_id,
        }

    def request_promotion(self, actor: Actor, proposal_id: str) -> str:
        actor.require("harness.review")
        head = self.store.head(actor.scope, "evolution", proposal_id)
        self._canary_authority(actor, head)
        policy = self._canary_policy(actor.scope, head["data"]["canary_policy_ref"])
        trials = head["data"]["canary_trials"]
        if (
            head["data"]["canary_pending"]
            or head["data"]["canary_incidents"]
            or len(trials) != policy["max_runs"]
            or not all(t["success"] is True for t in trials)
        ):
            raise Hold(
                "CANARY_INCOMPLETE",
                "Every planned unique trial must pass with no pending actions or incidents",
            )
        self._passing_report(actor.scope, head["data"]["report_ref"])
        obs = {
            "G-17": Observation.check(True, "Independent experiment and all spending revalidated"),
            "G-18": Observation.check(True, "Complete canary with no pending/unknown actions"),
        }
        with self.store.tx() as db:
            return self._move(db, actor.scope, proposal_id, "request_promotion", obs)

    def _release(self, scope: Scope, ref: dict[str, Any]) -> dict[str, Any]:
        release = self.store.get(scope, "release-set", ref)
        self.contracts.validate("release-set", release)
        verify_signature(release, self.keys)
        if release["status"] != "approved":
            raise Hold("RELEASE_NOT_APPROVED", "A signature alone is not release approval")
        check_refs(self.store, scope, release)
        _, matrix = resolve_ref(self.store, scope, release["qualification_matrix_ref"])
        if matrix.get("status") != "pass":
            raise Hold("RELEASE_QUALIFICATION", "Release qualification has not passed")
        return release

    def promote(self, actor: Actor, proposal_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        actor.require("release.promote")
        scope = actor.scope
        self.contracts.validate("promotion-plan", plan)
        if plan["scope"] != scope.wire():
            raise RuntimeFault("SCOPE_MISMATCH", "Promotion scope differs")
        head = self.store.head(scope, "evolution", proposal_id)
        self._canary_authority(actor, head)
        if (
            head["state"] != "promotion_pending"
            or head["data"]["report_ref"] != plan["eval_report_ref"]
        ):
            raise Hold("PROMOTION_BINDING", "Promotion differs from the reviewed report and canary")
        self._passing_report(scope, plan["eval_report_ref"])
        release = self._release(scope, plan["candidate_release_ref"])
        proposal = self.store.get(scope, "harness-change-proposal", head["data"]["proposal_ref"])
        if proposal["candidate_ref"] not in release["component_refs"]:
            raise Hold("RELEASE_COMPOSITION", "Release does not contain the tested candidate")
        if plan["canary_policy_ref"] != head["data"]["canary_policy_ref"]:
            raise Hold("CANARY_BINDING", "Promotion changed the canary policy")
        policy = self._canary_policy(scope, plan["canary_policy_ref"])
        if {digest(r) for r in plan["target_binding_refs"]} != {
            digest(r) for r in policy["target_binding_refs"]
        } or len(plan["target_binding_refs"]) != len(policy["target_binding_refs"]):
            raise Hold("PROMOTION_TARGETS", "Promotion must name exactly the authorized target set")
        if (
            plan["rollback_release_ref"] != policy["fallback_release_ref"]
            or plan["expected_active_release_ref"] != policy["fallback_release_ref"]
        ):
            raise Hold(
                "ROLLBACK_BINDING",
                "Rollback and expected baseline must match the qualified fallback",
            )
        if (
            datetime.fromisoformat(plan["expires_at"].replace("Z", "+00:00")).timestamp()
            <= self.store.clock()
        ):
            raise Hold("PROMOTION_EXPIRED", "Promotion plan expired")
        self.approval_check(
            scope,
            plan["grant_ref"],
            "release.promote",
            digest({k: v for k, v in plan.items() if k != "grant_ref"}),
        )
        with self.store.tx() as db:
            current = self.store.head(scope, "evolution", proposal_id, db=db)
            if current["row_version"] != head["row_version"]:
                raise Conflict("PROMOTION_RACE", "Candidate state changed during release checks")
            active = self.store.head(scope, "release-pointer", "active", db=db)
            if active["data"]["release_ref"] != plan["expected_active_release_ref"]:
                raise Conflict("RELEASE_CAS", "Active release changed; re-evaluate promotion")
            runs = db.execute(
                "SELECT state,data FROM heads WHERE tenant=? AND project=? AND kind='run' "
                "AND state NOT IN ('succeeded','failed','cancelled','lost')",
                scope.keys(),
            ).fetchall()
            if plan["active_run_policy"] == "drain" and runs:
                raise Hold("DRAIN_REQUIRED", "Active executions must drain first")
            if plan["active_run_policy"] == "checkpoint_explicit":
                for row in runs:
                    data = json.loads(row["data"])
                    if row["state"] != "paused" or not data.get("checkpoint_ref"):
                        raise Hold(
                            "CHECKPOINT_REQUIRED",
                            "Each active run needs an observed stopped-process checkpoint",
                        )
                    cp = self.store.get(scope, "checkpoint", data["checkpoint_ref"], db=db)
                    if (
                        data.get("process_stopped") is not True
                        or cp["pending_calls"]
                        or cp["driver_session_handle"] != data.get("session_handle")
                    ):
                        raise Hold(
                            "CHECKPOINT_PROCESS",
                            "Checkpoint does not prove the exact old process is stopped",
                        )
            self.store.cas(
                db,
                scope,
                "release-pointer",
                "active",
                active["row_version"],
                "active",
                {
                    "release_ref": plan["candidate_release_ref"],
                    "previous_ref": plan["expected_active_release_ref"],
                    "target_binding_refs": plan["target_binding_refs"],
                },
            )
            state = self._move(
                db,
                scope,
                proposal_id,
                "promote",
                {
                    "G-19": Observation.check(
                        True, "Signed release, exact independent grant, target set and pointer CAS"
                    )
                },
                {"promotion_plan": plan},
            )
            self.store.event(
                db,
                scope,
                "release",
                release["release_id"],
                "release.promoted",
                {
                    "release_ref": plan["candidate_release_ref"],
                    "target_binding_refs": plan["target_binding_refs"],
                },
            )
        return {"state": state, "active_release_ref": plan["candidate_release_ref"]}

    def rollback(
        self,
        actor: Actor,
        proposal_id: str,
        target_ref: dict[str, Any],
        expected_active_ref: dict[str, Any],
        approval_ref: dict[str, Any],
    ) -> dict[str, Any]:
        actor.require("release.rollback")
        scope, head = actor.scope, self.store.head(actor.scope, "evolution", proposal_id)
        self._independent(actor, head)
        self._release(scope, target_ref)
        plan = head["data"].get("promotion_plan", {})
        if target_ref != plan.get("rollback_release_ref") or expected_active_ref != plan.get(
            "candidate_release_ref"
        ):
            raise Hold(
                "ROLLBACK_BINDING",
                "Rollback only to the prequalified previous release for this exact promotion",
            )
        self.approval_check(
            scope,
            approval_ref,
            "release.rollback",
            digest({"target_ref": target_ref, "expected_active_ref": expected_active_ref}),
        )
        with self.store.tx() as db:
            active = self.store.head(scope, "release-pointer", "active", db=db)
            if active["data"]["release_ref"] != expected_active_ref:
                raise Conflict("RELEASE_CAS", "Active release changed before rollback")
            unknown = db.execute(
                "SELECT COUNT(*) FROM heads WHERE tenant=? AND project=? AND kind='effect' "
                "AND state IN ('prepared','dispatched','unknown')",
                scope.keys(),
            ).fetchone()[0]
            if unknown:
                raise Hold(
                    "ROLLBACK_UNKNOWN_EFFECT",
                    "Reconcile external effects before declaring rollback complete",
                )
            self.store.cas(
                db,
                scope,
                "release-pointer",
                "active",
                active["row_version"],
                "active",
                {
                    "release_ref": target_ref,
                    "previous_ref": expected_active_ref,
                    "target_binding_refs": plan["target_binding_refs"],
                },
            )
            state = self._move(
                db,
                scope,
                proposal_id,
                "rollback",
                {
                    "G-19": Observation.check(
                        True, "Exact rollback grant, signed target and active-pointer CAS"
                    ),
                    "G-21": Observation.check(
                        True, "No unknown effects hidden by release rollback"
                    ),
                },
            )
            self.store.event(
                db,
                scope,
                "release",
                proposal_id,
                "release.rolled_back",
                {"target_ref": target_ref, "authority_restored": False, "data_restored": False},
            )
        return {
            "state": state,
            "active_release_ref": target_ref,
            "authority_restored": False,
            "data_restored": False,
        }

    def abort(self, actor: Actor, proposal_id: str, reason: str) -> dict[str, Any]:
        actor.require("canary.run")
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeFault("ABORT_REASON", "Audit reason is required")
        head = self.store.head(actor.scope, "evolution", proposal_id)
        self._independent(actor, head)
        if head["state"] == "aborted":
            return {
                "state": "aborted",
                "reason": head["data"]["abort_reason"],
                "fallback": "admission_stopped",
            }
        obs = {
            "G-18": Observation.check(True, "Canary stopped with explicit reason"),
            "G-21": Observation.check(
                True, "Admission stops; in-flight uncertainty remains reserved for reconciliation"
            ),
        }
        with self.store.tx() as db:
            state = self._move(db, actor.scope, proposal_id, "abort", obs, {"abort_reason": reason})
        return {
            "state": state,
            "reason": reason,
            "fallback": "candidate admission stopped; active pointer unchanged",
        }

    def recover_canary(self, actor: Actor, proposal_id: str) -> dict[str, Any]:
        actor.require("experiment.reconcile")
        head = self.store.head(actor.scope, "evolution", proposal_id)
        self._independent(actor, head)
        if (
            head["state"] != "canary_running"
            or head["data"].get("canary_owner_epoch") == self.store.epoch
        ):
            raise Hold(
                "CANARY_RECOVERY_STATE", "Recovery requires an interrupted previous-owner canary"
            )
        # Pending slots/reservations deliberately remain. No process-stop claim is synthesized.
        return self.abort(
            actor, proposal_id, "previous_owner_interrupted_reconcile_pending_actions"
        )

    def kill_switch(self, actor: Actor, enabled: bool, reason: str) -> dict[str, Any]:
        actor.require("runtime.admin")
        if type(enabled) is not bool or not isinstance(reason, str) or not reason.strip():
            raise RuntimeFault("KILL_REASON", "Boolean state and audit reason required")
        if not enabled and "harness.propose" in actor.permissions:
            raise Hold("PROPOSER_KILL_SWITCH", "A proposer cannot re-enable admissions")
        with self.store.tx() as db:
            try:
                head = self.store.head(actor.scope, "runtime-control", "kill", db=db)
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
                head = {"row_version": 0, "data": {}}
            self.store.cas(
                db,
                actor.scope,
                "runtime-control",
                "kill",
                head["row_version"],
                "enabled" if enabled else "disabled",
                {"enabled": enabled, "reason": reason, "actor": actor.subject_id},
            )
            self.store.event(
                db,
                actor.scope,
                "release",
                "kill-switch",
                "runtime.kill_changed",
                {"enabled": enabled, "actor": actor.subject_id},
            )
        return {"enabled": enabled, "reason": reason, "actor": actor.subject_id}

    def _check_kill(self, scope: Scope) -> None:
        try:
            head = self.store.head(scope, "runtime-control", "kill")
        except RuntimeFault as exc:
            if exc.code == "NOT_FOUND":
                return
            raise
        if head["data"]["enabled"]:
            raise Hold("KILL_SWITCH", "Operator disabled new runtime/canary admission")
