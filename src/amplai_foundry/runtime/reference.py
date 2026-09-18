"""Isolated local end-to-end reference deployment; never grants corporate authority.

This uses a deterministic data-only worker to exercise the real orchestration,
artifact, verification and release paths without provider credentials. It does not
claim to measure model quality or qualify an external LLM account.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from amplai_foundry.agent_drivers.local import RecipeDriver
from amplai_foundry.knowledge_runtime.service import KnowledgeService
from amplai_foundry.sandbox.local import DataSandbox
from amplai_foundry.verification.runtime.service import (
    JsonVerifier,
    VerificationObservation,
    VerificationService,
)

from .contracts.authority import Actor, Authority
from .contracts.identity import canonical, digest, new_id, now
from .contracts.registry import Contracts
from .errors import Hold, RuntimeFault
from .evidence.cas import ArtifactStore
from .execution.service import Runtime
from .goals.service import GoalService
from .storage.store import Scope, Store

PERMISSIONS = frozenset(
    {
        "goal.submit",
        "goal.resolve",
        "contract.propose",
        "graph.propose",
        "goal.activate",
        "app.register",
        "grant.issue",
        "worker.execute",
        "verifier.run",
        "question.answer",
        "effect.reconcile",
        "goal.steer",
        "knowledge.propose",
        "harness.propose",
        "experiment.approve",
        "experiment.run",
        "release.promote",
        "release.rollback",
        "pack.install",
        "migration.apply",
        "runtime.admin",
    }
)


def qualify_data_sandbox(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    sandbox = DataSandbox(directory / "sandbox")
    checks = {}
    sandbox.write("result.json", b'{"ok":true}')
    checks["write_read"] = sandbox.read("result.json") == b'{"ok":true}'
    for name, path in [
        ("traversal", "../escape"),
        ("absolute", "/tmp/escape"),
        ("protected", "tests/replace.py"),
    ]:
        try:
            sandbox.write(path, b"bad")
        except RuntimeFault:
            checks[name] = True
        else:
            checks[name] = False
    outside = directory / "outside"
    outside.mkdir(exist_ok=True)
    (sandbox.root / "escape").symlink_to(outside, target_is_directory=True)
    try:
        sandbox.write("escape/injected", b"bad")
    except RuntimeFault:
        checks["symlink"] = not (outside / "injected").exists()
    else:
        checks["symlink"] = False
    try:
        sandbox.execute([{"op": "shell", "path": "result", "content": "echo BAD"}])
    except RuntimeFault:
        checks["no_execution"] = True
    else:
        checks["no_execution"] = False
    return {
        "qualification_id": new_id("qualification"),
        "status": "pass" if all(checks.values()) else "fail",
        "driver_version": "3.0.0",
        "boundary": "data-only broker, no arbitrary executable code",
        "checks": checks,
        "checked_at": now(),
    }


class ReferenceDeployment:
    def __init__(self, root: Path, *, clock=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.scope = Scope("demo-local", "demo-amplai")
        self.store = Store(self.root / "state", **({"clock": clock} if clock else {}))
        self.contracts = Contracts()
        self.actor = Actor(
            "demo-owner", self.scope, PERMISSIONS, authn_context_ref="isolated-local-demo"
        )
        self.worker = Actor(
            "demo-worker",
            self.scope,
            frozenset({"worker.execute"}),
            "service",
            "isolated-local-demo-worker",
        )
        self.verifier = Actor(
            "demo-verifier",
            self.scope,
            frozenset({"verifier.run"}),
            "service",
            "isolated-independent-demo-verifier",
        )
        self.signer = Ed25519PrivateKey.generate()
        self.verifier_signer = Ed25519PrivateKey.generate()
        self.decisions = {}

        def resolve(scope, ref):
            if scope != self.scope:
                raise RuntimeFault("DEMO_SCOPE", "Demo authority never covers another scope")
            value = self.decisions.get(digest(ref))
            if not value:
                raise Hold("DEMO_DECISION", "No explicit local demo decision")
            return value

        self.authority = Authority(
            self.store,
            self.contracts,
            {"demo-authority": self.signer.public_key()},
            resolve,
            signer=self.signer,
            key_id="demo-authority",
        )
        self.artifacts = ArtifactStore(self.store)
        self.goals = GoalService(self.store, self.contracts)
        self.runtime = Runtime(self.store, self.contracts, self.authority, self.artifacts)
        self.verification = VerificationService(
            self.runtime,
            signer=self.verifier_signer,
            key_id="demo-verifier-key",
            trusted_keys={"demo-verifier-key": self.verifier_signer.public_key()},
        )
        self.driver = RecipeDriver(self.root / "workspaces")
        self.knowledge = KnowledgeService(self.store, self.contracts)

    def put(self, kind: str, object_id: str, value: dict, revision: int = 1) -> dict:
        if kind in self.contracts.definitions:
            self.contracts.validate(kind, value)
        with self.store.tx() as db:
            return self.store.put(db, self.scope, kind, object_id, revision, value)

    def prepare(self, *, two_apps: bool = False, initial_value: str = "AMPLAI V3") -> dict:
        scope = self.scope
        caps = [
            {
                "action": "workspace.write",
                "resource": "sandbox:demo-output",
                "effect_class": "sandbox_write",
            }
        ]
        qualification = qualify_data_sandbox(self.root / "qualification")
        qual_ref = self.put("qualification", qualification["qualification_id"], qualification)
        environment = {
            "environment_id": "demo-environment",
            "scope": scope.wire(),
            "status": "qualified" if qualification["status"] == "pass" else "disabled",
            "containment_enforced": qualification["status"] == "pass",
            "boundary": "data-only",
            "capabilities": caps,
            "qualification_ref": qual_ref,
        }
        env_ref = self.put("environment", "demo-environment", environment)
        driver = {
            "schema_version": "3.0.0",
            "driver_id": "local-recipe",
            "driver_version": "3.0.0",
            "transport": "cli",
            "environment_ref": env_ref,
            "declared": ["workspace.write"],
            "observed": ["workspace.write"],
            "qualified": ["workspace.write"],
            "qualification_report_ref": qual_ref,
            "maturity": "qualified",
            "probed_at": now(),
        }
        driver_ref = self.put("driver-capabilities", "local-recipe", driver)
        model = {
            "schema_version": "3.0.0",
            "profile_id": "demo-deterministic-model",
            "provider": "local-deterministic",
            "provider_model_id": "local-recipe-v1",
            "model_version_policy": "pinned",
            "driver_profile_ref": driver_ref,
            "reasoning_profile": "none",
            "data_classes_allowed": ["internal"],
            "required_capabilities": [],
            "context_limit_tokens": 8192,
            "price_snapshot_ref": None,
            "qualification_ref": qual_ref,
            "enabled": True,
        }
        model_ref = self.put("model-profile", "demo-deterministic-model", model)
        policy = {
            "policy_id": "demo-policy",
            "scope": scope.wire(),
            "production": False,
            "requested_ceiling": caps,
            "classification": "internal",
        }
        policy_ref = self.put("policy", "demo-policy", policy)
        invariant_ref = self.put(
            "invariant-registry",
            "demo-invariants",
            {
                "registry_id": "demo-invariants",
                "scope": scope.wire(),
                "rules": [
                    "No production",
                    "No unverified success",
                    "No shell in data-only sandbox",
                ],
            },
        )
        profile = {
            "schema_version": "3.0.0",
            "profile_id": "demo-json-verifier",
            "version": "3.0.0",
            "kind": "deterministic",
            "tool_refs": [],
            "allowed_command_ids": [],
            "required_capabilities": [],
            "environment_ref": env_ref,
            "rubric_ref": None,
            "golden_refs": [],
            "protected": True,
            "owner_subject_id": self.verifier.subject_id,
        }
        verifier_ref = self.put("verifier-profile", "demo-json-verifier", profile)
        global_ref = self.put(
            "global-verifier",
            "demo-global",
            {
                "global_id": "demo-global",
                "scope": scope.wire(),
                "rule": "all named JSON outputs have distinct per-app identifiers and expected message",
                "owner": self.verifier.subject_id,
            },
        )
        composition = {
            "schema_version": "3.0.0",
            "composition_id": "demo-composition",
            "revision": 1,
            "model_profile_ref": model_ref,
            "driver_profile_ref": driver_ref,
            "sandbox_profile_ref": env_ref,
            "pack_refs": [],
            "prompt_bundle_ref": policy_ref,
            "router_policy_ref": policy_ref,
            "context_policy_ref": policy_ref,
            "verification_policy_ref": policy_ref,
            "budget_policy_ref": policy_ref,
            "protocol_major": 3,
            "qualification_ref": qual_ref,
            "created_at": now(),
        }
        composition_ref = self.put("harness-composition", "demo-composition", composition)
        target_refs = []
        for app in ["alpha", "beta"] if two_apps else ["alpha"]:
            binding = {
                "schema_version": "3.0.0",
                "app_id": app,
                "scope": scope.wire(),
                "repo_identity": "demo:" + app,
                "aliases": [app],
                "owner_subject_id": self.actor.subject_id,
                "allowed_roots": [str(self.root / "workspaces")],
                "environment_refs": [env_ref],
                "invariant_refs": [invariant_ref],
                "verifier_profile_refs": [verifier_ref],
                "data_classification": "internal",
                "requested_capabilities_ceiling": caps,
                "registry_revision": 1,
            }
            target_refs.append(self.goals.apps.register(self.actor, binding))
        submitted = self.goals.submit(
            self.actor,
            text="alpha 및 beta 결과 JSON 생성" if two_apps else "alpha 결과 JSON 생성",
            key=new_id("submit"),
        )
        goal_id = submitted["goal_id"]
        intent_ref = submitted["intent_ref"]
        facts = self.knowledge.record_observation(
            scope,
            "local-reference-task",
            "Reference task: output exact message and app identity; no performance threshold inferred",
        )
        readiness = self.knowledge.readiness(
            scope,
            {
                area: [facts]
                for area in [
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
        resolution_ref, resolution = self.goals.resolve(self.actor, goal_id, readiness)
        bundle_ref, bundle = self.knowledge.bundle(
            scope,
            bundle_id=new_id("context"),
            core_refs=[policy_ref, invariant_ref],
            entries=[
                {
                    "ref": facts,
                    "kind": "repo_fact",
                    "trust": "observed",
                    "mandatory": True,
                    "freshness": "current",
                    "superseded_by": None,
                    "excerpt": "Produce reference JSON in the data-only sandbox.",
                    "source_locator": "local-reference-task",
                }
            ],
            invariant_registry_ref=invariant_ref,
            token_budget=8192,
            assembled_at=now(),
        )
        bindings = []
        acceptance = []
        nodes = []
        recipes = {}
        t = now()
        root_budget = {
            "max_wall_seconds": 3600,
            "max_attempts": 3,
            "max_tokens": 12000,
            "max_cost_microunits": 2000000,
            "currency": "USD",
            "max_parallel_works": 4,
            "max_delegation_depth": 2,
        }
        for target in target_refs:
            app = target["id"]
            ac = "AC-" + app
            port = "result-" + app
            bindings.append(
                {
                    "acceptance_id": ac,
                    "verifier_ref": verifier_ref,
                    "subject_selector": port,
                    "environment_ref": env_ref,
                    "required_evidence_types": ["json-verification"],
                    "decision_rule": "Pinned schema and exact message checks",
                    "independent_review": True,
                    "timeout_seconds": 30,
                }
            )
            acceptance.append(
                {
                    "id": ac,
                    "statement": "App result contains the expected message and app identity",
                    "facet": "functional",
                    "mandatory": True,
                    "verifier_ref": verifier_ref,
                    "required_evidence_types": ["json-verification"],
                    "success_rule": "message equals AMPLAI V3 and app is named",
                    "human_acceptance_required": False,
                }
            )
            node = {
                "node_id": "node-" + app,
                "work_id": new_id("work-" + app),
                "target_ref": target,
                "objective": "Create " + app + " result JSON",
                "strategy": "bounded_loop",
                "depends_on": [],
                "join": "all_required",
                "consumes": [],
                "produces": [{"name": port, "media_type": "application/json", "required": True}],
                "acceptance_ids": [ac],
                "verification_profile_ref": verifier_ref,
                "capabilities": caps,
                "resource_claims": [{"resource": "sandbox:" + app, "mode": "exclusive_write"}],
                "budget": {**root_budget, "max_tokens": 2000, "max_cost_microunits": 200000},
            }
            nodes.append(node)
            recipes[node["work_id"]] = {
                "operations": [
                    {
                        "op": "write_json",
                        "path": "result.json",
                        "content": {"message": initial_value, "app": app},
                    }
                ],
                "outputs": {port: {"path": "result.json", "media_type": "application/json"}},
            }
        plan = {
            "schema_version": "3.0.0",
            "plan_id": new_id("plan"),
            "scope": scope.wire(),
            "contract_ref": None,
            "bindings": bindings,
            "protected_regression_refs": [],
            "policy_ref": policy_ref,
        }
        plan_ref = self.put("verification-plan", plan["plan_id"], plan)
        contract = {
            "schema_version": "3.0.0",
            "goal_id": goal_id,
            "scope": scope.wire(),
            "revision": 1,
            "intent_ref": intent_ref,
            "resolution_ref": resolution_ref,
            "mode": "work",
            "objective": "Produce verified per-app JSON artifacts through the actual V3 runtime",
            "non_goals": ["LLM quality measurement", "Corporate deployment"],
            "targets": resolution["target_refs"],
            "constraints": [
                {
                    "id": "C-LOCAL",
                    "statement": "Data-only sandbox; no production effects",
                    "source_refs": [policy_ref],
                    "protected": True,
                }
            ],
            "acceptance": acceptance,
            "assumptions": [],
            "open_question_refs": [],
            "risk": "low",
            "budget": root_budget,
            "requested_capabilities": caps,
            "verification_plan_ref": plan_ref,
            "context_bundle_ref": bundle_ref,
            "policy_ref": policy_ref,
            "created_at": t,
        }
        contract_ref = self.goals.freeze_contract(
            self.actor,
            contract,
            expected_version=self.store.head(scope, "goal", goal_id)["row_version"],
        )
        graph = {
            "schema_version": "3.0.0",
            "graph_id": new_id("graph"),
            "scope": scope.wire(),
            "revision": 1,
            "contract_ref": contract_ref,
            "previous_graph_ref": None,
            "replan_reason": None,
            "nodes": nodes,
            "global_verification_ref": global_ref,
            "compiler_version": "3.0.0",
            "created_at": t,
        }
        graph_ref = self.runtime.save_graph(self.actor, graph, contract_ref)
        decision = {
            "decision_id": new_id("demo-decision"),
            "scope": scope.wire(),
            "status": "approved",
            "revoked": False,
            "generation": 1,
            "issuer_subject_id": self.actor.subject_id,
            "subject_id": self.actor.subject_id,
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "capabilities": caps,
        }
        decision_ref = self.put("demo-decision", decision["decision_id"], decision)
        self.decisions[digest(decision_ref)] = decision
        current = datetime.fromtimestamp(self.store.clock(), UTC)
        grant = {
            "schema_version": "3.0.0",
            "grant_id": new_id("grant"),
            "scope": scope.wire(),
            "issuer": self.actor.wire(),
            "subject_id": self.actor.subject_id,
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "policy_ref": policy_ref,
            "generation": 1,
            "capabilities": caps,
            "artifact_bounds": [],
            "max_uses": 32,
            "effect_key": None,
            "not_before": (current - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (current + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "decision_ref": decision_ref,
        }
        grant_ref = self.authority.issue(self.actor, grant)
        execution_profile = {
            "composition_ref": composition_ref,
            "driver_profile_ref": driver_ref,
            "model_profile_ref": model_ref,
            "environment_ref": env_ref,
        }
        self.runtime.activate(
            self.actor,
            contract_ref,
            graph_ref,
            grant_ref,
            execution_profile,
            expected_version=self.store.head(scope, "goal", goal_id)["row_version"],
        )
        self.verification.register(
            verifier_ref,
            JsonVerifier(
                {
                    "type": "object",
                    "properties": {"message": {"const": "AMPLAI V3"}, "app": {"type": "string"}},
                    "required": ["message", "app"],
                    "additionalProperties": False,
                }
            ),
        )
        return {
            "goal_id": goal_id,
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "grant_ref": grant_ref,
            "verifier_ref": verifier_ref,
            "decision_ref": decision_ref,
            "recipes": recipes,
            "execution_profile": execution_profile,
        }

    def execute(self, prepared: dict) -> dict:
        runs = []
        while True:
            dispatch = self.runtime.claim(self.worker, goal_id=prepared["goal_id"])
            if dispatch is None:
                break
            self.driver.run(
                self.runtime,
                self.worker,
                dispatch,
                prepared["recipes"][dispatch["lease"]["work_id"]],
            )
            work = self.store.head(self.scope, "work", dispatch["lease"]["work_id"])
            for ac in dispatch["node"]["acceptance_ids"]:
                port = "result-" + dispatch["node"]["target_ref"]["id"]
                self.verification.verify(
                    self.verifier, dispatch["run_id"], ac, work["data"]["outputs"][port]
                )
            result = self.verification.finish_work(self.verifier, dispatch["run_id"])
            runs.append({"run_id": dispatch["run_id"], **result})

        def integration(contract, graph, outputs):
            values = [
                json.loads(self.artifacts.read(self.scope, a))
                for ports in outputs.values()
                for a in ports.values()
            ]
            good = all(v["message"] == "AMPLAI V3" for v in values) and len(
                {v["app"] for v in values}
            ) == len(graph["nodes"])
            return VerificationObservation(
                "pass" if good else "fail",
                "Independent cross-app JSON integration",
                {"app_count": len(values), "distinct_apps": len({v["app"] for v in values})},
            )

        graph = self.store.get(self.scope, "workgraph", prepared["graph_ref"])
        self.verification.register_global(graph["global_verification_ref"], integration)
        final = self.verification.finish_goal(self.verifier, prepared["goal_id"], integration)
        report = {
            "status": self.store.head(self.scope, "goal", prepared["goal_id"])["state"],
            "goal_id": prepared["goal_id"],
            "verification_ref": final,
            "runs": runs,
            "budget": self.runtime.budgets.totals(self.scope, prepared["goal_id"]),
            "qualification_kind": "local data-only deterministic reference, not external LLM or production",
        }
        (self.root / "reference-report.json").write_bytes(canonical(report))
        return report

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def run_reference(root: Path, *, two_apps: bool = True) -> dict:
    with ReferenceDeployment(root) as deployment:
        return deployment.execute(deployment.prepare(two_apps=two_apps))
