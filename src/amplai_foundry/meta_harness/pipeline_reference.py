"""Local evolution drill through real V3 goals, leases, workers and verifiers.

The code under comparison is a deterministic recipe algorithm, not an LLM. Every
trial creates a fresh goal with an exact frozen composition and an independent
expected-output verifier. Its immutable RunRecords and global verification are
retained in the same scoped Runtime Store and linked by the evaluation receipt.
No provider credentials, production grants or real external effects are used.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from amplai_foundry.evaluation.service import EvaluationService, ExecutorPolicy, TrialObservation
from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id, now
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.verification.runtime.service import JsonVerifier, VerificationObservation

from .reference import MetaReference


class PipelineMetaReference(MetaReference):
    """A deployable local acceptance drill; independent of production authorization."""

    def __init__(self, root: str | Path) -> None:
        super().__init__(root)
        self.case_runs: list[dict[str, Any]] = []
        self.case_bindings: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
        qualification = self.put(
            "executor-qualification",
            {
                "qualification_id": new_id("executor-qualification"),
                "status": "pass",
                "executor_id": "local-v3-pipeline",
                "source_qualification_ref": self.prepared["execution_profile"]["environment_ref"],
                "scope_note": (
                    "Real V3 local recipe worker; "
                    "not an external LLM or physical container qualification"
                ),
            },
        )
        self.eval = EvaluationService(
            self.d.store,
            self.d.contracts,
            self.d.artifacts,
            approval_check=self.check,
            executor_id="local-v3-pipeline",
            executor_policy=ExecutorPolicy(
                frozenset({"sandbox_rerun", "shadow"}), 0, 0, qualification
            ),
        )

    def execute_case(
        self, composition_ref: dict[str, Any], case: dict[str, Any], repeat: int, mode: str
    ) -> TrialObservation:
        if mode not in {"sandbox_rerun", "shadow", "canary"}:
            raise Hold("PIPELINE_MODE", "This local executor does not label reruns as trace replay")
        d, scope = self.d, self.d.scope
        value = json.loads(d.artifacts.read(scope, case["artifact_ref"], trusted=True))
        composition = d.store.get(scope, "harness-composition", composition_ref)
        prompt = d.store.get(scope, "prompt-bundle", composition["prompt_bundle_ref"])
        values = value["numbers"]
        if prompt["algorithm"] == "loop_sum":
            result = 0
            for number in values:
                result += number
        elif prompt["algorithm"] == "builtin_sum":
            result = sum(values)
        elif prompt["algorithm"] == "wrong_sum":
            result = sum(values) + 1
        else:
            raise Hold("PIPELINE_ALGORITHM", "Unsupported deterministic qualification algorithm")
        seed = d.store.get(scope, "goal-contract", self.prepared["contract_ref"])
        env_ref = self.prepared["execution_profile"]["environment_ref"]
        caps = seed["requested_capabilities"]
        app_id, work_id, port, ac = new_id("case-app"), new_id("case-work"), "result", "AC-SUM"
        verifier_value = d.store.get(scope, "verifier-profile", self.prepared["verifier_ref"])
        # Every expected-output verifier has an immutable task-specific rubric.
        if case["case_id"] in self.case_bindings:
            target, verifier_ref, rubric = self.case_bindings[case["case_id"]]
            app_id = target["id"]
        else:
            rubric = self.put(
                "arithmetic-rubric",
                {
                    "rubric_id": new_id("rubric"),
                    "task_id": case["case_id"],
                    "expected": value["expected"],
                    "scope": scope.wire(),
                },
            )
            verifier_id = new_id("case-verifier")
            verifier_ref = d.put(
                "verifier-profile",
                verifier_id,
                {**verifier_value, "profile_id": verifier_id, "rubric_ref": rubric},
            )
            invariants = d.store.get(scope, "app-binding", seed["targets"][0])["invariant_refs"]
            target = d.goals.apps.register(
                d.actor,
                {
                    "schema_version": "3.0.0",
                    "app_id": app_id,
                    "scope": scope.wire(),
                    "repo_identity": "local-evaluation:" + case["case_id"],
                    "aliases": [app_id],
                    "owner_subject_id": d.actor.subject_id,
                    "allowed_roots": [str(d.root / "workspaces")],
                    "environment_refs": [env_ref],
                    "invariant_refs": invariants,
                    "verifier_profile_refs": [verifier_ref],
                    "data_classification": "internal",
                    "requested_capabilities_ceiling": caps,
                    "registry_revision": 1,
                },
            )
            self.case_bindings[case["case_id"]] = (target, verifier_ref, rubric)
        submitted = d.goals.submit(
            d.actor,
            text=app_id + " arithmetic output",
            target_hints=[app_id],
            key=new_id("case-submit"),
        )
        goal_id = submitted["goal_id"]
        fact = d.knowledge.record_observation(
            scope, "case:" + case["case_id"], "Verify a frozen arithmetic case; no external effects"
        )
        readiness = d.knowledge.readiness(
            scope,
            {
                k: [fact]
                for k in [
                    "terminology",
                    "current_behavior",
                    "boundary",
                    "invariants",
                    "ssot",
                    "contradictions",
                    "acceptance",
                    "verifier",
                ]
            },
        )
        resolution_ref, _ = d.goals.resolve(d.actor, goal_id, readiness)
        plan = {
            "schema_version": "3.0.0",
            "plan_id": new_id("case-plan"),
            "scope": scope.wire(),
            "contract_ref": None,
            "bindings": [
                {
                    "acceptance_id": ac,
                    "verifier_ref": verifier_ref,
                    "subject_selector": port,
                    "environment_ref": env_ref,
                    "required_evidence_types": ["json-verification"],
                    "decision_rule": "Exact frozen expected sum",
                    "independent_review": True,
                    "timeout_seconds": 30,
                }
            ],
            "protected_regression_refs": [],
            "policy_ref": seed["policy_ref"],
        }
        plan_ref = self.put("verification-plan", plan)
        contract = {
            **seed,
            "goal_id": goal_id,
            "intent_ref": submitted["intent_ref"],
            "resolution_ref": resolution_ref,
            "objective": "Verify frozen task " + case["case_id"],
            "targets": [target],
            "acceptance": [
                {
                    "id": ac,
                    "statement": "Frozen arithmetic output matches expectation",
                    "facet": "functional",
                    "mandatory": True,
                    "verifier_ref": verifier_ref,
                    "required_evidence_types": ["json-verification"],
                    "success_rule": "value equals frozen expected sum",
                    "human_acceptance_required": False,
                }
            ],
            "verification_plan_ref": plan_ref,
            "created_at": now(),
        }
        contract_ref = d.goals.freeze_contract(
            d.actor, contract, expected_version=d.store.head(scope, "goal", goal_id)["row_version"]
        )
        global_ref = self.put(
            "global-verifier",
            {
                "global_id": new_id("case-global"),
                "scope": scope.wire(),
                "owner": d.verifier.subject_id,
                "rubric_ref": rubric,
                "rule": "One frozen case output, exact expected value",
            },
        )
        node = {
            "node_id": new_id("case-node"),
            "work_id": work_id,
            "target_ref": target,
            "objective": "Write frozen arithmetic result",
            "strategy": "direct",
            "depends_on": [],
            "join": "all_required",
            "consumes": [],
            "produces": [{"name": port, "media_type": "application/json", "required": True}],
            "acceptance_ids": [ac],
            "verification_profile_ref": verifier_ref,
            "capabilities": caps,
            "resource_claims": [{"resource": "sandbox:" + app_id, "mode": "exclusive_write"}],
            "budget": {**seed["budget"], "max_tokens": 1, "max_cost_microunits": 0},
        }
        graph = {
            "schema_version": "3.0.0",
            "graph_id": new_id("case-graph"),
            "scope": scope.wire(),
            "revision": 1,
            "contract_ref": contract_ref,
            "previous_graph_ref": None,
            "replan_reason": None,
            "nodes": [node],
            "global_verification_ref": global_ref,
            "compiler_version": "3.0.0",
            "created_at": now(),
        }
        graph_ref = d.runtime.save_graph(d.actor, graph, contract_ref)
        decision = {
            "decision_id": new_id("demo-decision"),
            "scope": scope.wire(),
            "status": "approved",
            "revoked": False,
            "generation": 1,
            "issuer_subject_id": d.actor.subject_id,
            "subject_id": d.actor.subject_id,
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "capabilities": caps,
        }
        decision_ref = self.put("demo-decision", decision)
        d.decisions[digest(decision_ref)] = decision
        current = datetime.fromtimestamp(d.store.clock(), UTC)
        grant = {
            "schema_version": "3.0.0",
            "grant_id": new_id("grant"),
            "scope": scope.wire(),
            "issuer": d.actor.wire(),
            "subject_id": d.actor.subject_id,
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "policy_ref": seed["policy_ref"],
            "generation": 1,
            "capabilities": caps,
            "artifact_bounds": [],
            "max_uses": 4,
            "effect_key": None,
            "not_before": (current - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (current + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "decision_ref": decision_ref,
        }
        grant_ref = d.authority.issue(d.actor, grant)
        execution = {**self.prepared["execution_profile"], "composition_ref": composition_ref}
        d.runtime.activate(
            d.actor,
            contract_ref,
            graph_ref,
            grant_ref,
            execution,
            expected_version=d.store.head(scope, "goal", goal_id)["row_version"],
        )
        dispatch = d.runtime.claim(d.worker, goal_id=goal_id)
        if dispatch is None:
            raise Hold("PIPELINE_NO_DISPATCH", "Reference goal admitted no work to execute")
        d.driver.run(
            d.runtime,
            d.worker,
            dispatch,
            {
                "operations": [
                    {
                        "op": "write_json",
                        "path": "result.json",
                        "content": {"task_id": case["case_id"], "value": result},
                    }
                ],
                "outputs": {port: {"path": "result.json", "media_type": "application/json"}},
            },
        )
        subject = d.store.head(scope, "work", work_id)["data"]["outputs"][port]
        if digest(verifier_ref) not in d.verification.runners:
            d.verification.register(
                verifier_ref,
                JsonVerifier(
                    {
                        "type": "object",
                        "properties": {
                            "task_id": {"const": case["case_id"]},
                            "value": {"const": value["expected"]},
                        },
                        "required": ["task_id", "value"],
                        "additionalProperties": False,
                    }
                ),
            )
        verdict_ref = d.verification.verify(d.verifier, dispatch["run_id"], ac, subject)
        verdict = d.store.get(scope, "verdict", verdict_ref)
        global_result = None
        if verdict["outcome"] == "pass":
            d.verification.finish_work(d.verifier, dispatch["run_id"])

            def global_check(
                contract: dict[str, Any], graph: dict[str, Any], outputs: dict[str, Any]
            ) -> VerificationObservation:
                actual = [
                    json.loads(d.artifacts.read(scope, a))
                    for ports in outputs.values()
                    for a in ports.values()
                ]
                correct = actual == [{"task_id": case["case_id"], "value": value["expected"]}]
                return VerificationObservation(
                    "pass" if correct else "fail",
                    "Frozen independent sum check",
                    {"task_id": case["case_id"], "actual_output_count": len(actual)},
                )

            d.verification.register_global(global_ref, global_check)
            global_result = d.verification.finish_goal(d.verifier, goal_id, global_check)
        if global_result is None:
            # Failed candidates do not leak active goals or resource leases into
            # later cases. The stopped recipe worker is known locally; record an
            # explicit cancellation, while retaining the independent FAIL verdict.
            from amplai_foundry.runtime.execution.steering import SteeringService

            steering = SteeringService(d.runtime)
            stop = steering.receive(
                d.actor,
                goal_id,
                "cancel",
                "Frozen candidate failed independent verification",
                expected_contract_ref=contract_ref,
                key=new_id("trial-cancel"),
                evidence_refs=[verdict_ref],
            )

            def stopped(run_id: str, kind: str) -> dict[str, Any]:
                run = d.store.head(scope, "run", run_id)["data"]
                return {
                    "process_stopped": run.get("process_stopped") is True,
                    "session_handle": run.get("session_handle"),
                }

            steering.quiesce(d.actor, stop["steering_id"], stopped)
        record = d.store.head(scope, "run", dispatch["run_id"])["data"]["record"]
        proof = {
            "task_id": case["case_id"],
            "composition_ref": composition_ref,
            "goal_id": goal_id,
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "run_record": record,
            "verdict_ref": verdict_ref,
            "global_verification_ref": global_result,
            "actual_subject_ref": subject,
        }
        proof_artifact = d.artifacts.admit(
            scope, canonical(proof), "application/json", trust="verifier"
        )
        success = global_result is not None
        receipt = {
            "task_id": case["case_id"],
            "repeat": repeat,
            "composition_ref": composition_ref,
            "mode": mode,
            "success": success,
            "safety_failures": 0,
            "unknown_effects": 0,
            "cost_microunits": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "usage_status": "measured",
            "pipeline_proof_ref": proof_artifact,
            "target_binding_ref": target,
            "scope": scope.wire(),
            "risk_class": contract["risk"],
        }
        artifact = d.artifacts.admit(
            scope, canonical(receipt), "application/json", trust="verifier"
        )
        self.case_runs.append(
            {
                "goal_id": goal_id,
                "run_id": dispatch["run_id"],
                "composition_ref": composition_ref,
                "task_id": case["case_id"],
                "mode": mode,
                "success": success,
            }
        )
        return TrialObservation(
            success, (artifact, proof_artifact), cost_microunits=0, input_tokens=0, output_tokens=0
        )

    def canary_target(self, task_id: str) -> dict[str, Any]:
        if task_id not in self.case_bindings:
            raise Hold(
                "CANARY_CASE_UNQUALIFIED",
                "Canary target must have a prior actual qualification task",
            )
        return self.case_bindings[task_id][0]

    def execute(self, prepared: dict[str, Any], *, rollback: bool = True) -> dict[str, Any]:
        result = super().execute(prepared, rollback=rollback)
        result = {
            **result,
            "actual_pipeline_trials": len(self.case_runs),
            "case_runs": self.case_runs,
            "qualification": (
                "local actual V3 pipeline; not model-quality, live-provider "
                "or physical-container qualification"
            ),
        }
        (self.root / "pipeline-evolution-report.json").write_bytes(canonical(result))
        return result


def run_pipeline_evolution(root: str | Path) -> dict[str, Any]:
    deployment = PipelineMetaReference(root)
    try:
        return deployment.execute(deployment.prepare())
    finally:
        deployment.close()
