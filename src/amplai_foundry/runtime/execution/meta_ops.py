"""The operator's meta-harness gates on the local product (Work 030 S6, D-089).

One method per gate, each an explicit operator command. The meta-proposer identity only proposes;
everything else runs as the human operator, and every approval is issued to the exact subject it
covers (``LocalMetaApprovals``). Nothing here decides for the operator: a gate that is not commanded
does not happen, and a candidate that does not pass stays where it stopped.

The gates in order: ``propose`` (proposer) → ``screen`` → ``approve_experiment`` →
``run_experiment`` → ``approve_canary`` → ``run_canary`` → ``promote`` → ``rollback``.
``reject`` and ``abort`` end a candidate; ``status`` reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ...evaluation.corpus import CorpusService
from ...evaluation.service import ExecutorPolicy
from ...meta_harness import local_corpus
from ...meta_harness.local_canary import canary_execute, canary_policy
from ...meta_harness.local_executor import LocalTrialExecutor, corpus_cases
from ..contracts.identity import canonical, digest, new_id, now
from ..errors import Hold
from ..local_deployment import LocalProductDeployment
from . import prompts, releases
from .meta_local import EXECUTOR_ID, RELEASE_KEY_ID

EXPERIMENT_MODE = "sandbox_rerun"
EXPERIMENT_SPLIT = "validation"


class LocalMetaOps:
    def __init__(
        self,
        dep: LocalProductDeployment,
        corpus: local_corpus.Corpus,
        executor: LocalTrialExecutor,
        *,
        driver: str,
    ) -> None:
        self.dep, self.corpus, self.executor, self.driver = dep, corpus, executor, driver
        self.store, self.scope = dep.store, dep.scope
        self.local = dep.meta_local
        self.operator = dep.meta_operator()
        self.app = dep.service.apps[corpus.app_id]

    # -- helpers -------------------------------------------------------------------------------
    def _put(self, kind: str, object_id: str, value: dict[str, Any]) -> dict[str, Any]:
        return self.dep.service._put(kind, object_id, value)

    def _head(self, proposal_id: str) -> dict[str, Any]:
        return dict(self.store.head(self.scope, "evolution", proposal_id))

    def _active_release(self) -> dict[str, Any]:
        head = self.store.head(self.scope, releases.POINTER_KIND, releases.POINTER_ID)
        ref: dict[str, Any] = head["data"]["release_ref"]
        return ref

    # -- 1. propose (the meta-proposer identity) -----------------------------------------------
    def propose(
        self,
        *,
        suffix: str,
        implementer_lines: list[str],
        hypothesis: str,
        expected_benefit: str,
        observation: str,
        risks: list[str],
    ) -> str:
        """A class-A change of the IMPLEMENTER prompt, submitted as the meta-proposer."""
        proposer = self.local.proposer
        base_ref = self.app.compositions[self.driver]
        base = self.store.get(self.scope, "harness-composition", base_ref)
        bundle = self._put(
            prompts.KIND,
            f"implementer-{suffix}",
            prompts.bundle(f"implementer-{suffix}", implementer_lines, "meta-harness proposal"),
        )
        candidate = self.local.meta.compositions.register(
            proposer,
            {
                **base,
                "composition_id": base["composition_id"] + releases.CANDIDATE_SEP + suffix,
                "prompt_bundle_ref": bundle,
            },
        )
        artifacts = self.dep.artifacts
        observed = artifacts.admit(
            self.scope, canonical({"issue": observation}), "application/json", trust="verifier"
        )
        observation_ref = self._put(
            "harness-observation",
            new_id("observation"),
            {"observation_id": new_id("observation"), "artifact": observed},
        )
        change = artifacts.admit(
            self.scope,
            canonical(
                {
                    "changed_paths": ["prompts/implementer.txt"],
                    "baseline_ref": base_ref,
                    "candidate_ref": candidate,
                }
            ),
            "application/json",
            trust="operator",
        )
        proposal = {
            "schema_version": "3.0.0",
            "proposal_id": new_id("harness-proposal"),
            "scope": self.scope.wire(),
            "baseline_ref": base_ref,
            "candidate_ref": candidate,
            "surface_class": "A",
            "hypothesis": hypothesis,
            "observation_refs": [observation_ref],
            "change_artifact": change,
            "expected_benefit": expected_benefit,
            "risks": risks,
            "protected_surface_findings": [],
            "experiment_plan_ref": self._put(
                "experiment-draft",
                new_id("exp-draft"),
                {
                    "draft_id": new_id("exp-draft"),
                    "comparison": "demo-app corpus, paired baseline and candidate",
                },
            ),
            "rollback_plan_ref": self._put(
                "rollback-plan",
                new_id("rollback"),
                {"rollback_id": new_id("rollback"), "target_release_ref": self._active_release()},
            ),
            "proposer": proposer.wire(),
            "status": "draft",
        }
        self.local.meta.submit(proposer, proposal)
        return str(proposal["proposal_id"])

    # -- 2. screen / reject / abort ------------------------------------------------------------
    def screen(self, proposal_id: str) -> dict[str, Any]:
        return self.local.meta.screen(self.operator, proposal_id)

    def reject(self, proposal_id: str, reason: str) -> str:
        return self.local.meta.reject(self.operator, proposal_id, reason)

    def abort(self, proposal_id: str, reason: str) -> dict[str, Any]:
        return self.local.meta.abort(self.operator, proposal_id, reason)

    # -- 3. the experiment ---------------------------------------------------------------------
    def approve_experiment(
        self,
        proposal_id: str,
        *,
        max_tokens: int,
        max_wall_seconds: int,
        margin: float = 0.25,
        confidence: float = 0.95,
    ) -> dict[str, Any]:
        """Freeze the corpus, the analysis plan (D-088, D-093) and the budget, then approve."""
        head = self._require(proposal_id, "screened")
        proposal = self.store.get(
            self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        artifacts = self.dep.artifacts
        cases = []
        for payload in corpus_cases(self.corpus):
            cases.append(
                {
                    "case_id": payload["case_id"],
                    "split": EXPERIMENT_SPLIT,
                    "task_class": payload["difficulty"],
                    "artifact_ref": artifacts.admit(
                        self.scope, canonical(payload), "application/json", trust="operator"
                    ),
                }
            )
        corpus_ref = CorpusService(self.store, artifacts).freeze(
            self.operator, new_id("corpus"), cases, holdout_use_limit=1
        )
        tasks = len(cases)
        analysis = self._put(
            "analysis-plan",
            new_id("analysis"),
            {
                "analysis_id": new_id("analysis"),
                "policy": {
                    "method": "paired_binary_conservative",
                    "confidence": confidence,
                    "minimum_tasks": tasks,
                    "repeats_per_task": 1,
                    "purpose": "confirmatory",
                    "noninferiority_margin": margin,
                    "safety_failure_limit": 0,
                    "missing_policy": "inconclusive",
                    "cost_basis": "not_compared",
                    "sample_rationale": (
                        f"{tasks} fixed demo-app tasks, one run per arm: the corpus size the "
                        "operator set (D-089 OD-5). The baseline passes every task (D-093), so "
                        "the test can show non-inferiority, not a higher success rate."
                    ),
                    "variance_basis": (
                        "Paired binary outcomes per task; the conservative paired method is "
                        "declared before any candidate ran."
                    ),
                    "sequential_rule": "fixed_sample_safety_abort_only",
                },
                "scope_note": "Non-inferiority on success rate only; cost is not compared (D-088).",
            },
        )
        sampling = self._put(
            "sampling-plan",
            new_id("sampling"),
            {
                "sampling_id": new_id("sampling"),
                "split": EXPERIMENT_SPLIT,
                "case_ids": [c["case_id"] for c in cases],
            },
        )
        holdout = self._put(
            "holdout-policy", new_id("holdout"), {"policy_id": new_id("holdout"), "sealed": True,
                                                  "max_uses": 1}
        )  # fmt: skip
        base_composition = self.store.get(
            self.scope, "harness-composition", proposal["baseline_ref"]
        )
        plan = {
            "schema_version": "3.0.0",
            "experiment_id": new_id("experiment"),
            "scope": self.scope.wire(),
            "proposal_ref": head["data"]["proposal_ref"],
            "baseline_ref": proposal["baseline_ref"],
            "candidate_ref": proposal["candidate_ref"],
            "corpus_ref": corpus_ref,
            "verifier_ref": self.app.verifier_refs[next(iter(self.app.verifier_refs))],
            "environment_ref": base_composition["sandbox_profile_ref"],
            "mode": EXPERIMENT_MODE,
            "primary_endpoint": "task_all_repeats_pass",
            "analysis_plan_ref": analysis,
            "sampling_plan_ref": sampling,
            "safety_stop_rules": ["Any safety failure or unknown effect stops the run"],
            "holdout_policy_ref": holdout,
            "budget": {
                "max_wall_seconds": max_wall_seconds,
                "max_attempts": 1,
                "max_tokens": max_tokens,
                "max_cost_microunits": 0,
                "currency": "USD",
                "max_parallel_works": 1,
                "max_delegation_depth": 0,
            },
            "frozen_at": now(),
        }
        plan["approval_ref"] = self.local.approvals.issue(
            self.operator, "experiment.execute", digest(plan)
        )
        experiment_ref = self.local.evaluation.freeze(self.operator, plan)
        self.local.meta.approve_experiment(
            self.operator, proposal_id, plan["approval_ref"], experiment_ref
        )
        return {"experiment_ref": experiment_ref, "tasks": tasks}

    def qualify_executor(self, basis: str, evidence: list[str], per_trial_tokens: int) -> None:
        """Pin the offline executor's qualification (the operator states what it rests on)."""
        qualification = self._put(
            "executor-qualification",
            new_id("executor-qualification"),
            {
                "qualification_id": new_id("executor-qualification"),
                "status": "pass",
                "executor_id": EXECUTOR_ID,
                "scope_note": basis,
                "evidence": evidence,
            },
        )
        self.local.evaluation.executor_policy = ExecutorPolicy(
            frozenset({EXPERIMENT_MODE}), per_trial_tokens, 0, qualification
        )

    def run_experiment(self, proposal_id: str) -> dict[str, Any]:
        head = self._require(proposal_id, "experiment_approved")
        experiment_ref = self._experiment_ref(head)
        self.local.meta.start_offline(self.operator, proposal_id)
        report_ref = self.local.evaluation.run(self.operator, experiment_ref, self.executor)
        report = self.store.get(self.scope, "eval-report", report_ref)
        self.local.meta.evaluate(self.operator, proposal_id, report_ref)
        return {"report_ref": report_ref, "verdict": report["verdict"]}

    def _experiment_ref(self, head: dict[str, Any]) -> dict[str, Any]:
        ref: dict[str, Any] = head["data"]["experiment_ref"]
        return ref

    # -- 4. the canary -------------------------------------------------------------------------
    def approve_canary(
        self, proposal_id: str, task_ids: list[str], *, max_trial_tokens: int
    ) -> dict[str, Any]:
        head = self._require(proposal_id, "offline_evaluated")
        policy = canary_policy(
            task_ids=task_ids,
            binding_ref=self.app.binding_ref,
            fallback_release_ref=self._active_release(),
            policy_id=new_id("canary-policy"),
            max_trial_tokens=max_trial_tokens,
        )
        policy_ref = self._put("canary-policy", policy["policy_id"], policy)
        approval = self.local.approvals.issue(
            self.operator,
            "canary.execute",
            digest(
                {
                    "proposal_ref": head["data"]["proposal_ref"],
                    "report_ref": head["data"]["report_ref"],
                    "policy_ref": policy_ref,
                }
            ),
        )
        self.local.meta.approve_canary(self.operator, proposal_id, policy_ref, approval)
        self.local.meta.start_canary(self.operator, proposal_id)
        return {"policy_ref": policy_ref, "tasks": task_ids}

    def run_canary(self, proposal_id: str) -> dict[str, Any]:
        head = self._require(proposal_id, "canary_running")
        proposal = self.store.get(
            self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        policy = self.store.get(self.scope, "canary-policy", head["data"]["canary_policy_ref"])
        execute = canary_execute(
            self.executor,
            self.dep.artifacts,
            self.scope,
            candidate_ref=proposal["candidate_ref"],
            task_binding_refs=policy["task_binding_refs"],
        )
        outcomes = []
        for task in policy["eligible_task_ids"]:
            outcome = self.local.meta.canary_trial(self.operator, proposal_id, task, execute)
            outcomes.append(outcome)
            if outcome["state"] != "canary_running":
                return {"state": outcome["state"], "outcomes": outcomes}
        return {
            "state": self.local.meta.request_promotion(self.operator, proposal_id),
            "outcomes": outcomes,
        }

    # -- 5. promote and rollback ---------------------------------------------------------------
    def promote(self, proposal_id: str) -> dict[str, Any]:
        head = self._require(proposal_id, "promotion_pending")
        proposal = self.store.get(
            self.scope, "harness-change-proposal", head["data"]["proposal_ref"]
        )
        policy = self.store.get(self.scope, "canary-policy", head["data"]["canary_policy_ref"])
        baseline_release = self._active_release()
        components = [
            proposal["candidate_ref"] if ref == proposal["baseline_ref"] else ref
            for app in self.dep.service.apps.values()
            for ref in app.compositions.values()
        ]
        candidate_release = releases.build(
            self.store,
            self.scope,
            self.dep.contracts,
            "local-candidate-" + digest(proposal["candidate_ref"])[7:19],
            sorted(components, key=lambda r: (r["id"], r["revision"])),
            signer=self.dep._release_signer,
            key_id=RELEASE_KEY_ID,
            basis="operator-promoted class-A candidate; each component keeps its driver report",
        )
        plan = {
            "schema_version": "3.0.0",
            "promotion_id": new_id("promotion"),
            "scope": self.scope.wire(),
            "expected_active_release_ref": baseline_release,
            "candidate_release_ref": candidate_release,
            "eval_report_ref": head["data"]["report_ref"],
            "target_binding_refs": policy["target_binding_refs"],
            "canary_policy_ref": head["data"]["canary_policy_ref"],
            "abort_rules": ["Abort on an unknown effect", "Abort on a safety failure"],
            "rollback_release_ref": baseline_release,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30))
            .isoformat()
            .replace("+00:00", "Z"),
            "active_run_policy": "drain",
        }
        plan["grant_ref"] = self.local.approvals.issue(
            self.operator, "release.promote", digest(plan)
        )
        promoted = self.local.meta.promote(self.operator, proposal_id, plan)
        return {
            "promotion": promoted,
            "candidate_release_ref": candidate_release,
            "baseline_release_ref": baseline_release,
        }

    def rollback(self, proposal_id: str) -> dict[str, Any]:
        """Return the pointer to the release the promotion named (read from its recorded plan)."""
        head = self._require(proposal_id, "promoted")
        plan = head["data"]["promotion_plan"]
        baseline, candidate = plan["rollback_release_ref"], plan["candidate_release_ref"]
        approval = self.local.approvals.issue(
            self.operator,
            "release.rollback",
            digest({"target_ref": baseline, "expected_active_ref": candidate}),
        )
        return self.local.meta.rollback(self.operator, proposal_id, baseline, candidate, approval)

    # -- read ----------------------------------------------------------------------------------
    def status(self, proposal_id: str) -> dict[str, Any]:
        head = self._head(proposal_id)
        data = head["data"]
        return {
            "proposal_id": proposal_id,
            "state": head["state"],
            "classification": data.get("classification"),
            "rejection": data.get("rejection"),
            "canary_trials": len(data.get("canary_trials") or []),
            "canary_incidents": data.get("canary_incidents") or [],
            "active_release": self._active_release()["id"],
        }

    def _require(self, proposal_id: str, *states: str) -> dict[str, Any]:
        """The gate needs the candidate in one of ``states`` (MetaHarness enforces it as well)."""
        head = self._head(proposal_id)
        if head["state"] not in states:
            raise Hold(
                "META_STATE",
                f"The candidate is {head['state']}; this gate needs {' or '.join(states)}",
            )
        return head
