"""Local single-operator execution product (Work 018): plan → approve → run → verify → publish.

``LocalExecutionService`` composes existing V3 services with the Work 018 pieces:

- install: per registered app, the verifier profiles (one per pinned command), policy,
  invariants, global check and composition, and the runners bound to them.
- plan: a read-only Codex draft on a fresh base copy, compiled deterministically into a goal
  contract, verification plan and one ``bounded_loop`` node that produces a change artifact.
  Everything passes ``GoalService.freeze_contract`` (schema + critic) and ``Runtime.save_graph``.
- approve: only an authenticated ``kind=human`` operator with ``execution.approve`` records an
  approval bound to the exact contract, graph and capabilities (D-070). That record is the
  decision the Authority resolves; the grant and activation follow from it.
- revoke: flips the approval; every later preflight holds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ...sandbox.git_workspace import CHANGE_MEDIA, GitWorkspaceManager
from ..contracts.authority import Actor
from ..contracts.identity import digest, new_id, now
from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store
from .codex import put_record
from .planner_codex import TASK_CLASSES

READINESS_AREAS = (
    "terminology",
    "current_behavior",
    "boundary",
    "invariants",
    "ssot",
    "contradictions",
    "acceptance",
    "verifier",
)
APPROVAL_KIND = "operator-approval"
PLAN_KIND = "execution-plan"
TASK_CLASS_KIND = "task-class"
PORT = "change"


@dataclass(frozen=True)
class VerifierCommand:
    id: str
    argv: tuple[str, ...]
    description: str
    timeout_seconds: int = 900


@dataclass(frozen=True)
class AppConfig:
    app_id: str
    repo: Path
    verifiers: tuple[VerifierCommand, ...]
    aliases: tuple[str, ...] = ()
    # Plans start from this branch and draft PRs target it; the operator's current checkout
    # (possibly another branch) is never the implicit base.
    base_branch: str = "main"
    remote: str = "origin"


@dataclass(frozen=True)
class Budget:
    max_wall_seconds: int = 1800  # operator decision 2026-09-28
    max_attempts: int = 3  # OD-1: design default, first attempt included
    max_tokens: int = 60_000_000  # root; each attempt reserves max_tokens // max_attempts
    max_cost_microunits: int | None = None

    def wire(self) -> dict[str, Any]:
        return {
            "max_wall_seconds": self.max_wall_seconds,
            "max_attempts": self.max_attempts,
            "max_tokens": self.max_tokens,
            "max_cost_microunits": self.max_cost_microunits,
            "currency": "USD",
            "max_parallel_works": 1,
            "max_delegation_depth": 0,
        }


class OperatorDecisions:
    """Decision resolver over operator approvals (Authority's ``decision_resolver``)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def __call__(self, scope: Scope, ref: dict[str, Any]) -> dict[str, Any]:
        value = dict(self.store.get(scope, APPROVAL_KIND, ref))
        try:
            state = self.store.head(scope, APPROVAL_KIND + "-state", value["decision_id"])
        except RuntimeFault:
            return value
        return {**value, "revoked": bool(state["data"].get("revoked"))}


@dataclass
class Actors:
    service: Actor  # plans, issues grants, activates (AMPLAI controller)
    worker: Actor
    verifier: Actor


@dataclass
class InstalledApp:
    config: AppConfig
    binding_ref: dict[str, Any]
    verifier_refs: dict[str, dict[str, Any]]
    global_ref: dict[str, Any]
    policy_ref: dict[str, Any]
    invariant_ref: dict[str, Any]
    composition_ref: dict[str, Any]
    capabilities: list[dict[str, Any]] = field(default_factory=list)


def _clean_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """Model text within contract schema bounds: trimmed, non-empty, at most 4000 characters."""

    def text(value: str) -> str:
        return value.strip()[:4000]

    cleaned = dict(draft)
    for key in ("in_scope", "non_goals", "constraints", "assumptions", "questions"):
        cleaned[key] = [text(v) for v in draft.get(key) or [] if text(v)]
    cleaned["acceptance"] = [
        {**a, "statement": text(a["statement"])}
        for a in draft.get("acceptance") or []
        if text(a["statement"])
    ]
    cleaned["objective"] = text(draft.get("objective", ""))
    cleaned["summary"] = text(draft.get("summary", "")) or cleaned["objective"][:200]
    if not cleaned["objective"]:
        raise Hold("PLANNING_OBJECTIVE", "The draft has no objective")
    return cleaned


def app_capabilities(app_id: str) -> list[dict[str, Any]]:
    return [
        {
            "action": "workspace.write",
            "resource": "sandbox:" + app_id,
            "effect_class": "sandbox_write",
        }
    ]


class LocalExecutionService:
    def __init__(
        self,
        *,
        store: Store,
        runtime: Any,
        goals: Any,
        knowledge: Any,
        authority: Any,
        verification: Any,
        workspaces: GitWorkspaceManager,
        planner: Any,
        actors: Actors,
        codex_refs: dict[str, dict[str, Any]],
        verifier_factory: Any,
        global_factory: Any,
        budget: Budget | None = None,
        publish_mode: str = "draft_pr",
    ) -> None:
        self.store, self.runtime, self.goals, self.knowledge = store, runtime, goals, knowledge
        self.authority, self.verification, self.workspaces = authority, verification, workspaces
        self.planner, self.actors, self.codex = planner, actors, codex_refs
        self.verifier_factory, self.global_factory = verifier_factory, global_factory
        self.budget = budget or Budget()
        if publish_mode not in {"draft_pr", "branch", "none"}:
            raise RuntimeFault("PUBLISH_MODE", "publish_mode is draft_pr, branch or none")
        self.publish_mode = publish_mode
        self.apps: dict[str, InstalledApp] = {}
        self._base_checks: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @property
    def scope(self) -> Scope:
        return self.actors.service.scope

    def _put(self, kind: str, object_id: str, value: dict[str, Any]) -> dict[str, Any]:
        return put_record(self.store, self.scope, self.runtime.contracts, kind, object_id, value)

    # -- install ---------------------------------------------------------------------------
    def install(self, app: AppConfig) -> InstalledApp:
        scope, a = self.scope, app.app_id
        caps = app_capabilities(a)
        env_ref = self.codex["environment"]
        invariant_ref = self._put(
            "invariant-registry",
            f"{a}-invariants",
            {
                "registry_id": f"{a}-invariants",
                "scope": scope.wire(),
                "rules": [
                    "Only the host-computed patch leaves the sandbox",
                    "No push, merge or deploy before goal.verified and operator approval",
                    "The operator's checkout and main are never written",
                ],
            },
        )
        policy_ref = self._put(
            "policy",
            f"{a}-policy",
            {
                "policy_id": f"{a}-policy",
                "scope": scope.wire(),
                "production": False,
                "requested_ceiling": caps,
                "classification": "internal",
            },
        )
        # One suite profile per app: a node carries exactly one verifier profile
        # (Runtime.save_graph NODE_VERIFIER), so every acceptance binds to the suite, which runs
        # all installed commands on base + patch.
        profile = {
            "schema_version": "3.0.0",
            "profile_id": f"{a}-suite",
            "version": "1",
            "kind": "deterministic",
            "tool_refs": [],
            "allowed_command_ids": [v.id for v in app.verifiers],
            "required_capabilities": [],
            "environment_ref": env_ref,
            "rubric_ref": None,
            "golden_refs": [],
            "protected": True,
            "owner_subject_id": self.actors.verifier.subject_id,
        }
        suite_ref = self._put("verifier-profile", f"{a}-suite", profile)
        if digest(suite_ref) not in self.verification.runners:
            self.verification.register(suite_ref, self.verifier_factory(app))
        verifier_refs = {v.id: suite_ref for v in app.verifiers}
        global_ref = self._put(
            "global-verifier",
            f"{a}-global",
            {
                "global_id": f"{a}-global",
                "scope": scope.wire(),
                "rule": "every node produced a non-empty change that verified on its base",
                "owner": self.actors.verifier.subject_id,
            },
        )
        if digest(global_ref) not in self.verification.global_runners:
            self.verification.register_global(global_ref, self.global_factory(app))
        composition_ref = self._put(
            "harness-composition",
            f"{a}-codex",
            {
                "schema_version": "3.0.0",
                "composition_id": f"{a}-codex",
                "revision": 1,
                "model_profile_ref": self.codex["model"],
                "driver_profile_ref": self.codex["driver"],
                "sandbox_profile_ref": env_ref,
                "pack_refs": [],
                "prompt_bundle_ref": policy_ref,
                "router_policy_ref": policy_ref,
                "context_policy_ref": policy_ref,
                "verification_policy_ref": policy_ref,
                "budget_policy_ref": policy_ref,
                "protocol_major": 3,
                "qualification_ref": self.codex["qualification"],
                "created_at": "2026-09-28T00:00:00Z",
            },
        )
        binding = {
            "schema_version": "3.0.0",
            "app_id": a,
            "scope": scope.wire(),
            "repo_identity": "git:" + a,
            "aliases": sorted({a, *app.aliases}),
            "owner_subject_id": self.actors.service.subject_id,
            "allowed_roots": [str(self.workspaces.root)],
            "environment_refs": [env_ref],
            "invariant_refs": [invariant_ref],
            "verifier_profile_refs": [suite_ref],
            "data_classification": "internal",
            "requested_capabilities_ceiling": caps,
            "registry_revision": 1,
        }
        binding_ref = self._register_app(binding)
        installed = InstalledApp(
            app, binding_ref, verifier_refs, global_ref, policy_ref, invariant_ref,
            composition_ref, caps,
        )  # fmt: skip
        self.apps[a] = installed
        return installed

    def _register_app(self, binding: dict[str, Any]) -> dict[str, Any]:
        existing = [
            (r, v) for r, v in self.store.list_objects(self.scope, "app-binding")
            if r["id"] == binding["app_id"]
        ]  # fmt: skip
        if existing:
            ref, value = max(existing, key=lambda x: x[0]["revision"])
            if {k: v for k, v in value.items() if k != "registry_revision"} == {
                k: v for k, v in binding.items() if k != "registry_revision"
            }:
                return ref
            binding = {**binding, "registry_revision": value["registry_revision"] + 1}
        result: dict[str, Any] = self.goals.apps.register(self.actors.service, binding)
        return result

    # -- plan --------------------------------------------------------------------------------
    def plan(self, goal_id: str) -> dict[str, Any]:
        """Draft (Codex, read-only) → compile → freeze contract → save graph."""
        service, scope = self.actors.service, self.scope
        goal = self.store.head(scope, "goal", goal_id)
        intent = self.store.get(scope, "intent-envelope", goal["data"]["intent_ref"])
        installed = self._target(intent)
        app = installed.config
        base = self.workspaces.base_snapshot(scope, app.app_id, app.base_branch)
        base_value = self._base(base)
        base_check = self.base_check(installed, base_value)
        workspace = self.workspaces.materialize(scope, new_id("plan-ws"), base)
        try:
            drafted = self.planner.draft(
                intent["text"],
                app.app_id,
                {v.id: v.description for v in app.verifiers},
                workspace,
            )
        finally:
            self.workspaces.discard(workspace)
        draft = _clean_draft(drafted["draft"])
        record: dict[str, Any] = {
            "goal_id": goal_id,
            "scope": scope.wire(),
            "app": app.app_id,
            "base": base,
            "base_commit": base_value["commit"],
            "draft": draft,
            "planner_usage": drafted.get("usage"),
            "base_check": base_check,
            "created_at": now(),
        }
        if draft["questions"]:
            record.update(status="needs_answers", contract_ref=None, graph_ref=None)
            self._save_plan(goal_id, record, ("question.asked", {"count": len(draft["questions"])}))
            return record
        facts = self.knowledge.record_observation(
            scope,
            f"git:{app.app_id}@{base_value['commit']}",
            "Planner draft on base commit " + base_value["commit"] + ": " + draft["summary"],
        )
        readiness = self.knowledge.readiness(scope, {area: [facts] for area in READINESS_AREAS})
        resolution_ref, resolution = self.goals.resolve(service, goal_id, readiness)
        if resolution["status"] != "resolved":
            raise Hold("RESOLUTION_HOLD", "Goal could not be resolved", details=resolution)
        bundle_ref, _ = self.knowledge.bundle(
            scope,
            bundle_id=new_id("context"),
            core_refs=[installed.policy_ref, installed.invariant_ref],
            entries=[
                {
                    "ref": facts,
                    "kind": "repo_fact",
                    "trust": "observed",
                    "mandatory": True,
                    "freshness": "current",
                    "superseded_by": None,
                    "excerpt": draft["summary"][:500],
                    "source_locator": f"git:{app.app_id}@{base_value['commit']}",
                }
            ],
            invariant_registry_ref=installed.invariant_ref,
            token_budget=8192,
            assembled_at=now(),
        )
        contract_ref, graph_ref = self._compile(
            goal_id, intent, resolution_ref, resolution, bundle_ref, installed, draft
        )
        record.update(status="awaiting_approval", contract_ref=contract_ref, graph_ref=graph_ref)
        self._save_plan(goal_id, record, ("approval.requested", {"contract_ref": contract_ref}))
        return record

    def base_check(self, installed: InstalledApp, base_value: dict[str, Any]) -> dict[str, Any]:
        """Run the app suite on the untouched base (cached per commit).

        A red base is shown to the operator before approval, not a block: "fix this failing
        test" legitimately starts red, but otherwise nothing could ever verify (found by the
        first real run, where the base failed in a copy without .git).
        """
        commit = base_value["commit"]
        if commit in self._base_checks:
            return self._base_checks[commit]
        empty = self.workspaces.artifacts.admit(self.scope, b"", "text/x-diff")
        change = {
            "format": "amplai.change.v1",
            "base": {k: base_value[k] for k in ("repo", "commit", "tree")},
            "patch": empty,
            "patch_bytes": 0,
        }
        from ..contracts.identity import canonical

        suite = installed.verifier_refs[installed.config.verifiers[0].id]
        observation = self.verification.runners[digest(suite)](canonical(change))
        commands = observation.details.get("commands") or []
        result = {
            "commit": commit,
            "outcome": observation.outcome,
            "reason": observation.reason,
            "commands": [
                {k: c.get(k) for k in ("command_id", "exit_code", "seconds")} for c in commands
            ],
            "tail": (
                (observation.details.get("stdout_tail") or "")
                + (observation.details.get("stderr_tail") or "")
            )[-1500:],
        }
        self._base_checks[commit] = result
        return result

    def _base(self, base: dict[str, Any]) -> dict[str, Any]:
        import json

        value: dict[str, Any] = json.loads(self.workspaces.artifacts.read(self.scope, base))
        return value

    def _target(self, intent: dict[str, Any]) -> InstalledApp:
        hints = intent.get("target_hints") or []
        candidates = [a for a in self.apps.values() if a.config.app_id in hints] or (
            list(self.apps.values()) if len(self.apps) == 1 else []
        )
        if len(candidates) != 1:
            raise Hold("TARGET_REQUIRED", "Name exactly one registered app (--app)")
        return candidates[0]

    def _compile(
        self,
        goal_id: str,
        intent: dict[str, Any],
        resolution_ref: dict[str, Any],
        resolution: dict[str, Any],
        bundle_ref: dict[str, Any],
        installed: InstalledApp,
        draft: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        scope, service = self.scope, self.actors.service
        env_ref, root = self.codex["environment"], self.budget.wire()
        draft = _clean_draft(draft)
        bindings, acceptance = [], []
        if not draft["acceptance"] or any(
            a["verifier"] not in installed.verifier_refs for a in draft["acceptance"]
        ):
            raise Hold("PLANNING_VERIFIER", "Every acceptance needs one installed verifier")
        for i, item in enumerate(draft["acceptance"], start=1):
            ac, command = f"AC-{i}", installed.config.verifiers
            vref = installed.verifier_refs[item["verifier"]]
            timeout = next(v.timeout_seconds for v in command if v.id == item["verifier"])
            bindings.append(
                {
                    "acceptance_id": ac,
                    "verifier_ref": vref,
                    "subject_selector": PORT,
                    "environment_ref": env_ref,
                    "required_evidence_types": ["command-verification"],
                    "decision_rule": "every installed suite command exits 0 on base + patch; "
                    + f"{item['verifier']} demonstrates this statement",
                    "independent_review": True,
                    "timeout_seconds": timeout,
                }
            )
            acceptance.append(
                {
                    "id": ac,
                    "statement": item["statement"],
                    "facet": "functional",
                    "mandatory": True,
                    "verifier_ref": vref,
                    "required_evidence_types": ["command-verification"],
                    "success_rule": f"suite passes ({item['verifier']} shows it) on base + patch",
                    "human_acceptance_required": False,
                }
            )
        plan_ref = self._put(
            "verification-plan",
            new_id("plan"),
            {
                "schema_version": "3.0.0",
                "plan_id": new_id("plan"),
                "scope": scope.wire(),
                "contract_ref": None,
                "bindings": bindings,
                "protected_regression_refs": [],
                "policy_ref": installed.policy_ref,
            },
        )
        t = now()
        constraints = [
            {
                "id": f"C-{i}",
                "statement": text,
                "source_refs": [installed.policy_ref],
                "protected": False,
            }
            for i, text in enumerate(draft["constraints"], start=1)
        ] + [
            {
                "id": "C-SANDBOX",
                "statement": "Changes happen only in the sandbox copy; the patch is the result",
                "source_refs": [installed.policy_ref],
                "protected": True,
            }
        ]
        contract = {
            "schema_version": "3.0.0",
            "goal_id": goal_id,
            "scope": scope.wire(),
            "revision": 1,
            "intent_ref": self.store.head(scope, "goal", goal_id)["data"]["intent_ref"],
            "resolution_ref": resolution_ref,
            "mode": "work",
            "objective": draft["objective"],
            "non_goals": draft["non_goals"],
            "targets": resolution["target_refs"],
            "constraints": constraints,
            "acceptance": acceptance,
            "assumptions": [
                {
                    "id": f"A-{i}",
                    "statement": text,
                    "origin": "inferred",
                    "source_refs": [],
                    "status": "proposed",
                    "blocks_execution": False,
                }
                for i, text in enumerate(draft["assumptions"], start=1)
            ],
            "open_question_refs": [],
            "risk": draft["risk"],
            "budget": root,
            "requested_capabilities": installed.capabilities,
            "verification_plan_ref": plan_ref,
            "context_bundle_ref": bundle_ref,
            "policy_ref": installed.policy_ref,
            "created_at": t,
        }
        contract_ref = self.goals.freeze_contract(
            service,
            contract,
            expected_version=self.store.head(scope, "goal", goal_id)["row_version"],
        )
        node = {
            "node_id": "node-" + installed.config.app_id,
            "work_id": new_id("work-" + installed.config.app_id),
            "target_ref": installed.binding_ref,
            "objective": draft["objective"],
            "strategy": "bounded_loop",
            "depends_on": [],
            "join": "all_required",
            "consumes": [],
            "produces": [{"name": PORT, "media_type": CHANGE_MEDIA, "required": True}],
            "acceptance_ids": [a["id"] for a in acceptance],
            "verification_profile_ref": acceptance[0]["verifier_ref"],
            "capabilities": installed.capabilities,
            "resource_claims": [
                {"resource": "sandbox:" + installed.config.app_id, "mode": "exclusive_write"}
            ],
            "budget": {**root, "max_tokens": root["max_tokens"] // root["max_attempts"]},
        }
        graph = {
            "schema_version": "3.0.0",
            "graph_id": new_id("graph"),
            "scope": scope.wire(),
            "revision": 1,
            "contract_ref": contract_ref,
            "previous_graph_ref": None,
            "replan_reason": None,
            "nodes": [node],
            "global_verification_ref": installed.global_ref,
            "compiler_version": "amplai-local-1",
            "created_at": t,
        }
        graph_ref = self.runtime.save_graph(service, graph, contract_ref)
        if draft.get("task_class") in TASK_CLASSES:
            # The planner's label, beside the graph: the approved 3.0.0 workgraph schema has no
            # task_class slot and cannot take one (D-077). Shown at approval; Observatory slices.
            self._put(
                TASK_CLASS_KIND,
                node["work_id"],
                {"scope": scope.wire(), "work_id": node["work_id"], "graph_ref": graph_ref,
                 "task_class": draft["task_class"], "source": "planner"},
            )  # fmt: skip
        return contract_ref, graph_ref

    def _save_plan(
        self, goal_id: str, record: dict[str, Any], event: tuple[str, dict[str, Any]] | None = None
    ) -> None:
        """Save the plan record; ``event`` goes on the goal's audit trail in the same tx.

        ``question.*`` and ``approval.*`` events are what the Observatory counts as human
        intervention and measures human wait and queue time from.
        """
        with self.store.tx() as db:
            try:
                version = self.store.head(self.scope, PLAN_KIND, goal_id, db=db)["row_version"]
            except RuntimeFault:
                version = 0  # cas with expected 0 creates the head
            self.store.cas(db, self.scope, PLAN_KIND, goal_id, version, record["status"], record)
            if event:
                self.store.event(db, self.scope, "goal", goal_id, *event)

    def plan_record(self, goal_id: str) -> dict[str, Any]:
        value: dict[str, Any] = self.store.head(self.scope, PLAN_KIND, goal_id)["data"]
        return value

    # -- approve / revoke --------------------------------------------------------------------
    def approve(self, operator: Actor, goal_id: str, *, hours: int = 2) -> dict[str, Any]:
        operator.require("execution.approve")
        if operator.kind != "human" or operator.scope != self.scope:
            raise RuntimeFault("APPROVER_KIND", "Only an authenticated human operator approves")
        plan = self.plan_record(goal_id)
        if plan.get("status") != "awaiting_approval":
            raise Hold(
                "PLAN_NOT_READY",
                "Goal has no contract awaiting approval",
                details=plan.get("status"),
            )
        installed = self.apps[plan["app"]]
        service, scope = self.actors.service, self.scope
        decision = {
            "decision_id": new_id("approval"),
            "scope": scope.wire(),
            "status": "approved",
            "revoked": False,
            "generation": 1,
            "issuer_subject_id": service.subject_id,
            "subject_id": service.subject_id,
            "contract_ref": plan["contract_ref"],
            "graph_ref": plan["graph_ref"],
            "capabilities": installed.capabilities,
            "approved_by": operator.wire(),
            "approved_at": now(),
            "publish": {
                "mode": self.publish_mode,
                "remote": installed.config.remote,
                "base_branch": installed.config.base_branch,
            },
        }
        decision_ref = self._put(APPROVAL_KIND, decision["decision_id"], decision)
        current = datetime.fromtimestamp(self.store.clock(), UTC)
        grant = {
            "schema_version": "3.0.0",
            "grant_id": new_id("grant"),
            "scope": scope.wire(),
            "issuer": service.wire(),
            "subject_id": service.subject_id,
            "contract_ref": plan["contract_ref"],
            "graph_ref": plan["graph_ref"],
            "policy_ref": installed.policy_ref,
            "generation": 1,
            "capabilities": installed.capabilities,
            "artifact_bounds": [],
            "max_uses": 32,
            "effect_key": None,
            "not_before": (current - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (current + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
            "decision_ref": decision_ref,
        }
        grant_ref = self.authority.issue(service, grant)
        profile = {
            "composition_ref": installed.composition_ref,
            "driver_profile_ref": self.codex["driver"],
            "model_profile_ref": self.codex["model"],
            "environment_ref": self.codex["environment"],
        }
        self.runtime.activate(
            service,
            plan["contract_ref"],
            plan["graph_ref"],
            grant_ref,
            profile,
            expected_version=self.store.head(scope, "goal", goal_id)["row_version"],
        )
        plan = {
            **plan,
            "status": "approved",
            "decision_ref": decision_ref,
            "grant_ref": grant_ref,
            "approved_at": now(),
        }
        self._save_plan(goal_id, plan, ("approval.granted", {"decision_ref": decision_ref}))
        return plan

    def revoke(self, operator: Actor, goal_id: str) -> None:
        operator.require("execution.approve")
        plan = self.plan_record(goal_id)
        ref = plan.get("decision_ref")
        if not ref:
            raise Hold("NOT_APPROVED", "Goal has no approval to revoke")
        decision = self.store.get(self.scope, APPROVAL_KIND, ref)
        state = {"revoked": True, "by": operator.wire(), "at": now()}
        with self.store.tx() as db:
            try:
                self.store.cas(
                    db, self.scope, APPROVAL_KIND + "-state", decision["decision_id"], 0,
                    "revoked", state,
                )  # fmt: skip
                if operator.kind == "human":  # the loop stopping its own goal is not one
                    self.store.event(
                        db, self.scope, "goal", goal_id, "approval.revoked", {"decision_ref": ref}
                    )
            except Exception as exc:  # already revoked is not an error
                if getattr(exc, "code", "") != "STALE_VERSION":
                    raise
