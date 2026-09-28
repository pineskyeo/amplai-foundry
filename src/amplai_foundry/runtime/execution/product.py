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
from ...verification.runtime.design_check import DESIGN_ROOT, DesignDocumentCheck
from ..contracts.authority import Actor
from ..contracts.identity import digest, new_id, now
from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store
from .codex import put_record
from .planner_codex import TASK_CLASSES
from .steering import SteeringService

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
# Initial task-class baseline order, every class: operator decision 2026-09-28 (D-079).
ROUTER_ORDER = ("codex-cli", "claude-cli")
DESIGN_CHECK = "design"
DESIGN_CHECK_DESCRIPTION = (
    "design document check: only files under specs/design/<goal>/ change; design.md has "
    "the sections Goal, Current State, Options, Decision, Risks, Implementation Plan, "
    "Sources; at least 3 path:line citations that resolve in the repository"
)
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
    compositions: dict[str, dict[str, Any]] = field(default_factory=dict)  # driver id -> ref
    router_ref: dict[str, Any] | None = None
    design_ref: dict[str, Any] | None = None  # the design-document verifier profile (F)
    driver_refs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    planners: dict[str, Any] = field(default_factory=dict)


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
    if "work_items" in draft:
        cleaned["work_items"] = [
            {
                "app": item["app"],
                "objective": text(item.get("objective", "")) or text(draft.get("objective", "")),
                "in_scope": [text(v) for v in item.get("in_scope") or [] if text(v)],
                "acceptance": [
                    {**a, "statement": text(a["statement"])}
                    for a in item.get("acceptance") or []
                    if text(a["statement"])
                ],
                "after": [b for b in dict.fromkeys(item.get("after") or []) if b != item["app"]],
            }
            for item in draft["work_items"]
        ]
    cleaned["objective"] = text(draft.get("objective", ""))
    cleaned["summary"] = text(draft.get("summary", "")) or cleaned["objective"][:200]
    if not cleaned["objective"]:
        raise Hold("PLANNING_OBJECTIVE", "The draft has no objective")
    return cleaned


def app_capabilities(app_id: str) -> list[dict[str, Any]]:
    """The app's ceiling: implementation writes (work) and design-document writes (design)."""
    return [
        {
            "action": "workspace.write",
            "resource": "sandbox:" + app_id,
            "effect_class": "sandbox_write",
        },
        {
            "action": "workspace.design_write",
            "resource": "sandbox:" + app_id,
            "effect_class": "sandbox_write",
        },
    ]


def mode_capabilities(app_id: str, mode: str) -> list[dict[str, Any]]:
    """What one goal may request: design mode never gets implementation writes (design/03:59)."""
    action = "workspace.design_write" if mode == "design" else "workspace.write"
    return [c for c in app_capabilities(app_id) if c["action"] == action]


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
        driver_refs: dict[str, dict[str, dict[str, Any]]] | None = None,
        planners: dict[str, Any] | None = None,
        design_min_sources: int = 3,
        integration_factory: Any = None,
    ) -> None:
        self.store, self.runtime, self.goals, self.knowledge = store, runtime, goals, knowledge
        self.authority, self.verification, self.workspaces = authority, verification, workspaces
        self.planner, self.actors, self.codex = planner, actors, codex_refs
        # candidate compositions per driver id (D-079); Codex alone when nothing else is given
        self.drivers = driver_refs or {"codex-cli": codex_refs}
        self.planners = dict(planners or {})  # driver id -> planner; Codex is self.planner
        self.design_min_sources = design_min_sources
        self.integration_factory = integration_factory  # multi-app goal check (D-081)
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
    def install(
        self,
        app: AppConfig,
        *,
        driver_refs: dict[str, dict[str, dict[str, Any]]] | None = None,
        planners: dict[str, Any] | None = None,
    ) -> InstalledApp:
        scope, a = self.scope, app.app_id
        caps = app_capabilities(a)
        drivers = driver_refs or self.drivers
        env_ref = (drivers.get("codex-cli") or self.codex)["environment"]
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
        design_ref = self._put(
            "verifier-profile",
            f"{a}-design",
            {**profile, "profile_id": f"{a}-design", "allowed_command_ids": [DESIGN_CHECK]},
        )
        if digest(design_ref) not in self.verification.runners:
            self.verification.register(
                design_ref,
                DesignDocumentCheck(
                    self.workspaces, self.scope, min_sources=self.design_min_sources
                ),
            )
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
        # Task-class baseline policy (design/16:16): operator decision 2026-09-28, D-079.
        router_ref = self._put(
            "router-policy",
            f"{a}-router",
            {
                "policy_id": f"{a}-router",
                "scope": scope.wire(),
                "kind": "task_class_baseline",
                "order": {"*": list(ROUTER_ORDER)},
                "source": "operator decision 2026-09-28 (D-079): Codex first, Claude fallback",
            },
        )
        compositions = {}
        for driver_id, refs in drivers.items():
            name = f"{a}-{driver_id.split('-')[0]}"
            compositions[driver_id] = self._put(
                "harness-composition",
                name,
                {
                    "schema_version": "3.0.0",
                    "composition_id": name,
                    "revision": 1,
                    "model_profile_ref": refs["model"],
                    "driver_profile_ref": refs["driver"],
                    "sandbox_profile_ref": refs["environment"],
                    "pack_refs": [],
                    "prompt_bundle_ref": policy_ref,
                    "router_policy_ref": router_ref,
                    "context_policy_ref": policy_ref,
                    "verification_policy_ref": policy_ref,
                    "budget_policy_ref": policy_ref,
                    "protocol_major": 3,
                    "qualification_ref": refs["qualification"],
                    "created_at": "2026-09-28T00:00:00Z",
                },
            )
        composition_ref = compositions.get("codex-cli") or next(iter(compositions.values()))
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
            "verifier_profile_refs": [suite_ref, design_ref],
            "data_classification": "internal",
            "requested_capabilities_ceiling": caps,
            "registry_revision": 1,
        }
        binding_ref = self._register_app(binding)
        installed = InstalledApp(
            app, binding_ref, verifier_refs, global_ref, policy_ref, invariant_ref,
            composition_ref, caps, compositions, router_ref, design_ref,
            dict(drivers), dict(planners or {}),
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

    # -- composition selection (design/16:14-16, D-079) ---------------------------------------
    def select_composition(
        self, installed: InstalledApp, task_class: str | None = None
    ) -> dict[str, Any]:
        """Filter candidates on data class, qualification and capabilities, then rank them by
        the task-class baseline policy. The operator sees the choice; nobody picks a driver."""
        from ...meta_harness.composition import CompositionService

        if installed.router_ref is None:
            raise Hold("ROUTER_POLICY", "The app has no router policy")
        policy = self.store.get(self.scope, "router-policy", installed.router_ref)
        order = policy["order"].get(task_class or "*") or policy["order"]["*"]
        candidates = [installed.compositions[d] for d in order if d in installed.compositions]
        scores = {ref["digest"]: float(len(candidates) - i) for i, ref in enumerate(candidates)}
        required = {c["action"] for c in installed.capabilities}
        compositions = CompositionService(self.store, self.runtime.contracts)
        rows = compositions.explain(
            self.scope, candidates, classification="internal", required_actions=required
        )
        chosen = compositions.select(
            self.scope, candidates, classification="internal", required_actions=required,
            scores=scores,
        )  # fmt: skip
        row = next(r for r in rows if r["ref"] == chosen)
        return {
            "ref": chosen,
            "driver_id": row["driver_id"],
            "model": row["model"],
            "task_class": task_class,
            "rank": candidates.index(chosen) + 1,
            "policy_ref": installed.router_ref,
            "candidates": [
                {k: r[k] for k in ("driver_id", "model", "eligible", "reasons")} for r in rows
            ],
        }

    # -- plan --------------------------------------------------------------------------------
    def plan(self, goal_id: str) -> dict[str, Any]:
        """Draft (selected driver, read-only) → compile → freeze contract → save graph.

        One app or several (D-081): every draft is normalised to work items (one per app that
        must change, with the apps it comes after); a one-app goal is exactly one item.
        """
        service, scope = self.actors.service, self.scope
        goal = self.store.head(scope, "goal", goal_id)
        intent = self.store.get(scope, "intent-envelope", goal["data"]["intent_ref"])
        targets = self._targets(intent)
        installed = targets[0]
        app = installed.config
        mode = intent.get("mode", "work")
        multi = len(targets) > 1
        if multi and mode != "work":
            raise Hold("DESIGN_ONE_APP", "A design goal targets exactly one app")
        if multi and len({digest(self._environment(t)) for t in targets}) != 1:
            # one activated profile runs every node of a goal (driver, model, image)
            raise Hold("MULTI_APP_IMAGE", "The apps of one goal must share one worker image")
        bases = {
            t.config.app_id: self.workspaces.base_snapshot(
                scope, t.config.app_id, t.config.base_branch
            )
            for t in targets
        }
        base = bases[app.app_id]
        base_value = self._base(base)
        # a design goal changes documents only: the app suite on the base says nothing about it
        base_checks = (
            {a: self.base_check(self.apps[a], self._base(b)) for a, b in bases.items()}
            if mode == "work"
            else {}
        )
        base_check = base_checks.get(app.app_id)
        verifiers = (
            {DESIGN_CHECK: DESIGN_CHECK_DESCRIPTION}
            if mode == "design"
            else {v.id: v.description for v in app.verifiers}
        )
        planning = self.select_composition(installed)
        planner = self._planner(installed, planning["driver_id"])
        workspaces = {
            a: self.workspaces.materialize(scope, new_id("plan-ws"), b) for a, b in bases.items()
        }
        try:
            if multi:
                drafted = planner.draft_multi(
                    intent["text"],
                    {t.config.app_id: {v.id: v.description for v in t.config.verifiers}
                     for t in targets},
                    workspaces,
                )  # fmt: skip
            else:
                drafted = planner.draft(
                    intent["text"], app.app_id, verifiers, workspaces[app.app_id], mode=mode
                )
        finally:
            for workspace in workspaces.values():
                self.workspaces.discard(workspace)
        draft = _clean_draft(drafted["draft"])
        items = (
            draft["work_items"]
            if multi
            else [
                {"app": app.app_id, "objective": draft["objective"],
                 "in_scope": draft["in_scope"], "acceptance": draft["acceptance"], "after": []}
            ]
        )  # fmt: skip
        record: dict[str, Any] = {
            "goal_id": goal_id,
            "scope": scope.wire(),
            "app": app.app_id,
            "base": base,
            "base_commit": base_value["commit"],
            "draft": draft,
            "planner_usage": drafted.get("usage"),
            "planned_with": {k: planning[k] for k in ("driver_id", "model")},
            "mode": mode,
            **({"design_dir": f"{DESIGN_ROOT}{goal_id}/"} if mode == "design" else {}),
            **(
                {
                    "apps": list(bases),
                    "bases": bases,
                    "base_checks": base_checks,
                    "base_commits": {a: self._base(b)["commit"] for a, b in bases.items()},
                }
                if multi
                else {}
            ),
            "work_items": items,
            "base_check": base_check,
            "created_at": now(),
        }
        if draft["questions"]:
            record.update(status="needs_answers", contract_ref=None, graph_ref=None)
            self._save_plan(goal_id, record, ("question.asked", {"count": len(draft["questions"])}))
            return record
        entries = []
        for a, b in bases.items():
            commit = self._base(b)["commit"]
            fact = self.knowledge.record_observation(
                scope,
                f"git:{a}@{commit}",
                f"Planner draft on {a} base commit {commit}: " + draft["summary"],
            )
            entries.append(
                {
                    "ref": fact,
                    "kind": "repo_fact",
                    "trust": "observed",
                    "mandatory": True,
                    "freshness": "current",
                    "superseded_by": None,
                    "excerpt": draft["summary"][:500],
                    "source_locator": f"git:{a}@{commit}",
                }
            )
        policy_ref = self._policy_for([t.config.app_id for t in targets])
        if multi:
            record["policy_ref"] = policy_ref
        readiness = self.knowledge.readiness(
            scope, {area: [e["ref"] for e in entries] for area in READINESS_AREAS}
        )
        resolution_ref, resolution = self.goals.resolve(service, goal_id, readiness)
        if resolution["status"] != "resolved":
            raise Hold("RESOLUTION_HOLD", "Goal could not be resolved", details=resolution)
        bundle_ref, _ = self.knowledge.bundle(
            scope,
            bundle_id=new_id("context"),
            # every target app's invariants govern the goal (validation checks all of them)
            core_refs=[policy_ref, *(t.invariant_ref for t in targets)],
            entries=entries,
            invariant_registry_ref=installed.invariant_ref,
            token_budget=8192,
            assembled_at=now(),
        )
        contract_ref, graph_ref, acceptance_map = self._compile(
            goal_id, intent, resolution_ref, resolution, bundle_ref, installed, draft,
            mode=mode, items=items, policy_ref=policy_ref,
        )  # fmt: skip
        record["acceptance_map"] = acceptance_map
        record["composition"] = self.select_composition(installed, draft.get("task_class"))
        record.update(status="awaiting_approval", contract_ref=contract_ref, graph_ref=graph_ref)
        self._save_plan(goal_id, record, ("approval.requested", {"contract_ref": contract_ref}))
        return record

    def replan(self, goal_id: str, reason: str, steering_id: str) -> dict[str, Any]:
        """Draft and freeze the next contract revision after the operator's replan (D-082).

        The goal is blocked by the revision steering event (its attempt stopped at a process
        boundary). The planner drafts again from the same bases with the operator's reason; the
        contract keeps the resolution, targets, policy and protected constraints, and the graph
        names its predecessor. Nothing runs until the operator approves the revision.
        """
        scope = self.scope
        plan = self.plan_record(goal_id)
        previous = self.store.get(scope, "goal-contract", plan["contract_ref"])
        intent = self.store.get(scope, "intent-envelope", previous["intent_ref"])
        targets = self._targets(intent)
        installed = targets[0]
        app = installed.config
        mode = plan.get("mode", "work")
        bases = plan.get("bases") or {plan["app"]: plan["base"]}
        before = plan["draft"]
        request = (
            intent["text"]
            + "\n\nOperator replan request (the plan must change accordingly): "
            + reason
            + "\nPrevious objective: "
            + before["objective"]
        )
        planning = self.select_composition(installed)
        planner = self._planner(installed, planning["driver_id"])
        workspaces = {
            a: self.workspaces.materialize(scope, new_id("plan-ws"), b) for a, b in bases.items()
        }
        try:
            if len(targets) > 1:
                drafted = planner.draft_multi(
                    request,
                    {t.config.app_id: {v.id: v.description for v in t.config.verifiers}
                     for t in targets},
                    workspaces,
                )  # fmt: skip
            else:
                verifiers = (
                    {DESIGN_CHECK: DESIGN_CHECK_DESCRIPTION}
                    if mode == "design"
                    else {v.id: v.description for v in app.verifiers}
                )
                drafted = planner.draft(
                    request, app.app_id, verifiers, workspaces[app.app_id], mode=mode
                )
        finally:
            for workspace in workspaces.values():
                self.workspaces.discard(workspace)
        draft = _clean_draft(drafted["draft"])
        if draft["questions"]:
            raise Hold("REPLAN_QUESTIONS", "The replan needs answers", details=draft["questions"])
        items = (
            draft["work_items"]
            if len(targets) > 1
            else [
                {"app": app.app_id, "objective": draft["objective"],
                 "in_scope": draft["in_scope"], "acceptance": draft["acceptance"], "after": []}
            ]
        )  # fmt: skip
        revision = previous["revision"] + 1
        contract_ref, graph_ref, acceptance_map = self._compile(
            goal_id, intent, previous["resolution_ref"], {"target_refs": previous["targets"]},
            previous["context_bundle_ref"], installed, draft, mode=mode, items=items,
            policy_ref=previous["policy_ref"], revision=revision,
            previous_graph_ref=plan["graph_ref"], replan_reason=reason[:4000],
        )  # fmt: skip
        record = {
            **{k: v for k, v in plan.items() if k not in {"decision_ref", "grant_ref",
                                                          "approved_at", "finished_at"}},
            "draft": draft,
            "work_items": items,
            "acceptance_map": acceptance_map,
            "planner_usage": drafted.get("usage"),
            "planned_with": {k: planning[k] for k in ("driver_id", "model")},
            "composition": self.select_composition(installed, draft.get("task_class")),
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "revision": revision,
            "replan": {
                "reason": reason,
                "steering_id": steering_id,
                "previous_contract_ref": plan["contract_ref"],
                "previous_graph_ref": plan["graph_ref"],
            },
            "previous_attempts": [
                *(plan.get("previous_attempts") or []),
                *(plan.get("attempts") or []),
            ],
            "attempts": [],
            "status": "awaiting_approval",
            "reason": None,
            "updated_at": now(),
        }  # fmt: skip
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

    def _targets(self, intent: dict[str, Any]) -> list[InstalledApp]:
        """The apps a goal names (--app, in order); the only app when exactly one is installed."""
        hints = list(dict.fromkeys(intent.get("target_hints") or []))
        if not hints:
            if len(self.apps) == 1:
                return list(self.apps.values())
            raise Hold("TARGET_REQUIRED", "Name the target app(s) with --app")
        unknown = [h for h in hints if h not in self.apps]
        if unknown:
            raise Hold("TARGET_UNKNOWN", "Not an installed app", details=unknown)
        return [self.apps[h] for h in hints]

    def _target(self, intent: dict[str, Any]) -> InstalledApp:
        targets = self._targets(intent)
        if len(targets) != 1:
            raise Hold("TARGET_REQUIRED", "Name exactly one registered app (--app)")
        return targets[0]

    def _compile(
        self,
        goal_id: str,
        intent: dict[str, Any],
        resolution_ref: dict[str, Any],
        resolution: dict[str, Any],
        bundle_ref: dict[str, Any],
        installed: InstalledApp,
        draft: dict[str, Any],
        *,
        mode: str = "work",
        items: list[dict[str, Any]] | None = None,
        policy_ref: dict[str, Any] | None = None,
        revision: int = 1,
        previous_graph_ref: dict[str, Any] | None = None,
        replan_reason: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, str]]]:
        """Deterministic contract + graph: one node per work item (a one-app goal is one)."""
        scope, service = self.scope, self.actors.service
        policy_ref = policy_ref or installed.policy_ref
        root = self.budget.wire()
        draft = _clean_draft(draft)
        items = items or [
            {"app": installed.config.app_id, "objective": draft["objective"],
             "in_scope": draft["in_scope"], "acceptance": draft["acceptance"], "after": []}
        ]  # fmt: skip
        design = mode == "design"
        evidence = "design-verification" if design else "command-verification"
        apps = [item["app"] for item in items]
        if len(set(apps)) != len(apps) or any(a not in self.apps for a in apps):
            raise Hold("PLANNING_TARGET", "Work items must name distinct installed apps")
        if any(b not in apps or b == item["app"] for item in items for b in item["after"]):
            raise Hold("PLANNING_ORDER", "A work item can only come after another item's app")
        bindings: list[dict[str, Any]] = []
        acceptance: list[dict[str, Any]] = []
        node_acceptance: list[list[str]] = []
        acceptance_map: dict[str, dict[str, str]] = {}
        for item in items:
            target = self.apps[item["app"]]
            refs = {DESIGN_CHECK: target.design_ref} if design else target.verifier_refs
            if not item["acceptance"] or any(a["verifier"] not in refs for a in item["acceptance"]):
                raise Hold("PLANNING_VERIFIER", "Every acceptance needs one installed verifier")
            ids = []
            for entry in item["acceptance"]:
                ac = f"AC-{len(acceptance) + 1}"
                vref = refs[entry["verifier"]]
                assert vref is not None
                timeout = (
                    300
                    if design
                    else next(
                        v.timeout_seconds
                        for v in target.config.verifiers
                        if v.id == entry["verifier"]
                    )
                )
                rule = (
                    "only specs/design/<dir>/ changes; design.md has every required section and "
                    "resolved path:line sources"
                    if design
                    else f"every installed {item['app']} suite command exits 0 on base + patch; "
                    + f"{entry['verifier']} demonstrates this statement"
                )
                bindings.append(
                    {
                        "acceptance_id": ac,
                        "verifier_ref": vref,
                        "subject_selector": PORT,
                        "environment_ref": self._environment(target),
                        "required_evidence_types": [evidence],
                        "decision_rule": rule,
                        "independent_review": True,
                        "timeout_seconds": timeout,
                    }
                )
                acceptance.append(
                    {
                        "id": ac,
                        "statement": entry["statement"],
                        "facet": "functional",
                        "mandatory": True,
                        "verifier_ref": vref,
                        "required_evidence_types": [evidence],
                        "success_rule": (
                            "the design document check passes on base + patch"
                            if design
                            else f"suite passes ({entry['verifier']} shows it) on base + patch"
                        ),
                        "human_acceptance_required": False,
                    }
                )
                acceptance_map[ac] = {
                    "app": item["app"],
                    "verifier": entry["verifier"],
                    "statement": entry["statement"],
                }
                ids.append(ac)
            node_acceptance.append(ids)
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
                "policy_ref": policy_ref,
            },
        )
        t = now()
        constraints = (
            [
                {
                    "id": f"C-{i}",
                    "statement": text,
                    "source_refs": [policy_ref],
                    "protected": False,
                }
                for i, text in enumerate(draft["constraints"], start=1)
            ]
            + [
                {
                    "id": "C-SANDBOX",
                    "statement": "Changes happen only in the sandbox copy; the patch is the result",
                    "source_refs": [policy_ref],
                    "protected": True,
                }
            ]
            + (
                [
                    {
                        "id": "C-DESIGN",
                        "statement": "Design mode: only documents under "
                        + DESIGN_ROOT
                        + "<goal>/ change; no source, test or configuration file (design/03:59)",
                        "source_refs": [policy_ref],
                        "protected": True,
                    }
                ]
                if design
                else []
            )
        )
        capabilities = [c for a in apps for c in mode_capabilities(a, mode)]
        contract = {
            "schema_version": "3.0.0",
            "goal_id": goal_id,
            "scope": scope.wire(),
            "revision": revision,
            "intent_ref": self.store.head(scope, "goal", goal_id)["data"]["intent_ref"],
            "resolution_ref": resolution_ref,
            "mode": mode,
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
            "requested_capabilities": capabilities,
            "verification_plan_ref": plan_ref,
            "context_bundle_ref": bundle_ref,
            "policy_ref": policy_ref,
            "created_at": t,
        }
        contract_ref = self.goals.freeze_contract(
            service,
            contract,
            expected_version=self.store.head(scope, "goal", goal_id)["row_version"],
        )
        nodes = []
        for item, ids in zip(items, node_acceptance, strict=True):
            target = self.apps[item["app"]]
            a = target.config.app_id
            nodes.append(
                {
                    "node_id": "node-" + a,
                    "work_id": new_id("work-" + a),
                    "target_ref": target.binding_ref,
                    "objective": item["objective"],
                    "strategy": "bounded_loop",
                    "depends_on": ["node-" + b for b in item["after"]],
                    "join": "all_required",
                    # the downstream agent sees the verified upstream change (D-081)
                    "consumes": [
                        {
                            "name": "upstream-" + b,
                            "from_node": "node-" + b,
                            "external_ref": None,
                            "media_type": CHANGE_MEDIA,
                            "output_name": PORT,
                        }
                        for b in item["after"]
                    ],
                    "produces": [{"name": PORT, "media_type": CHANGE_MEDIA, "required": True}],
                    "acceptance_ids": ids,
                    "verification_profile_ref": acceptance[int(ids[0][3:]) - 1]["verifier_ref"],
                    "capabilities": mode_capabilities(a, mode),
                    "resource_claims": [{"resource": "sandbox:" + a, "mode": "exclusive_write"}],
                    "budget": {
                        **root,
                        "max_tokens": root["max_tokens"] // (root["max_attempts"] * len(items)),
                    },
                }
            )
        graph = {
            "schema_version": "3.0.0",
            "graph_id": new_id("graph"),
            "scope": scope.wire(),
            "revision": revision,
            "contract_ref": contract_ref,
            "previous_graph_ref": previous_graph_ref,
            "replan_reason": replan_reason,
            "nodes": nodes,
            "global_verification_ref": (
                installed.global_ref if len(items) == 1 else self._global_for(apps)
            ),
            "compiler_version": "amplai-local-1",
            "created_at": t,
        }
        graph_ref = self.runtime.save_graph(service, graph, contract_ref)
        if draft.get("task_class") in TASK_CLASSES:
            # The planner's label, beside the graph: the approved 3.0.0 workgraph schema has no
            # task_class slot and cannot take one (D-077). Shown at approval; Observatory slices.
            for node in nodes:
                self._put(
                    TASK_CLASS_KIND,
                    node["work_id"],
                    {"scope": scope.wire(), "work_id": node["work_id"], "graph_ref": graph_ref,
                     "task_class": draft["task_class"], "source": "planner"},
                )  # fmt: skip
        return contract_ref, graph_ref, acceptance_map

    def _environment(self, installed: InstalledApp) -> dict[str, Any]:
        refs = installed.driver_refs.get("codex-cli") or self.codex
        env: dict[str, Any] = refs["environment"]
        return env

    def _planner(self, installed: InstalledApp, driver_id: str) -> Any:
        planner = installed.planners.get(driver_id) or (
            self.planner if driver_id == "codex-cli" else self.planners.get(driver_id)
        )
        if planner is None:
            raise Hold("PLANNER_UNAVAILABLE", "No planner for " + driver_id)
        return planner

    def _policy_for(self, apps: list[str]) -> dict[str, Any]:
        """The app's policy; for several apps one goal policy whose ceiling is their union."""
        if len(apps) == 1:
            return self.apps[apps[0]].policy_ref
        key = "__".join(sorted(apps))
        return self._put(
            "policy",
            f"{key}-policy",
            {
                "policy_id": f"{key}-policy",
                "scope": self.scope.wire(),
                "production": False,
                "requested_ceiling": [c for a in sorted(apps) for c in app_capabilities(a)],
                "classification": "internal",
            },
        )

    def _global_for(self, apps: list[str]) -> dict[str, Any]:
        """The goal-level check of a multi-app graph: non-empty changes + integration commands."""
        key = "__".join(sorted(apps))  # ids allow letters, digits and . _ : -
        ref = self._put(
            "global-verifier",
            f"{key}-global",
            {
                "global_id": f"{key}-global",
                "scope": self.scope.wire(),
                "rule": "every node produced a non-empty change; the configured integration "
                "commands pass on every app's base + patch together",
                "owner": self.actors.verifier.subject_id,
            },
        )
        if digest(ref) not in self.verification.global_runners:
            factory = self.integration_factory
            runner = (
                factory(sorted(apps))
                if factory is not None
                else self.global_factory(self.apps[apps[0]].config)
            )
            self.verification.register_global(ref, runner)
        return ref

    def _save_plan(
        self,
        goal_id: str,
        record: dict[str, Any],
        event: tuple[str, dict[str, Any]] | list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        """Save the plan record; ``event`` (one or a list) goes on the goal's audit trail in
        the same tx.

        ``question.*`` and ``approval.*`` events are what the Observatory counts as human
        intervention and measures human wait and queue time from; ``publication.*`` events are
        what happened to the draft PR.
        """
        events = [event] if isinstance(event, tuple) else list(event or [])
        with self.store.tx() as db:
            try:
                version = self.store.head(self.scope, PLAN_KIND, goal_id, db=db)["row_version"]
            except RuntimeFault:
                version = 0  # cas with expected 0 creates the head
            self.store.cas(db, self.scope, PLAN_KIND, goal_id, version, record["status"], record)
            for event_type, payload in events:
                self.store.event(db, self.scope, "goal", goal_id, event_type, payload)

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
        goal_apps = plan.get("apps") or [plan["app"]]
        caps = [c for a in goal_apps for c in mode_capabilities(a, plan.get("mode", "work"))]
        chosen = plan.get("composition")
        if chosen:
            # fixed from here on (design/16:16); eligibility may have changed since planning
            again = self.select_composition(installed, chosen.get("task_class"))
            if again["ref"] != chosen["ref"]:
                raise Hold(
                    "COMPOSITION_CHANGED",
                    "The selected composition changed since planning; plan the goal again",
                    details={"planned": chosen["driver_id"], "now": again["driver_id"]},
                )
            composition = self.store.get(scope, "harness-composition", chosen["ref"])
            profile = {
                "composition_ref": chosen["ref"],
                "driver_profile_ref": composition["driver_profile_ref"],
                "model_profile_ref": composition["model_profile_ref"],
                "environment_ref": composition["sandbox_profile_ref"],
            }
        else:  # planned before composition selection existed (Work 018): Codex
            codex = installed.driver_refs.get("codex-cli") or self.codex
            profile = {
                "composition_ref": installed.composition_ref,
                "driver_profile_ref": codex["driver"],
                "model_profile_ref": codex["model"],
                "environment_ref": codex["environment"],
            }
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
            "capabilities": caps,
            "approved_by": operator.wire(),
            "approved_at": now(),
            "publish": {
                "mode": self.publish_mode,
                "remote": installed.config.remote,
                "base_branch": installed.config.base_branch,
                "apps": {
                    a: {
                        "remote": self.apps[a].config.remote,
                        "base_branch": self.apps[a].config.base_branch,
                    }
                    for a in goal_apps
                },
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
            "policy_ref": plan.get("policy_ref") or installed.policy_ref,
            "generation": 1,
            "capabilities": caps,
            "artifact_bounds": [],
            "max_uses": 32,
            "effect_key": None,
            "not_before": (current - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (current + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
            "decision_ref": decision_ref,
        }
        grant_ref = self.authority.issue(service, grant)
        self.runtime.activate(
            service,
            plan["contract_ref"],
            plan["graph_ref"],
            grant_ref,
            profile,
            expected_version=self.store.head(scope, "goal", goal_id)["row_version"],
        )
        replan = plan.get("replan") or {}
        if replan.get("steering_id"):
            # the admitted revision makes the operator's revision steering effective (D-082)
            SteeringService(self.runtime).apply_revision(service, replan["steering_id"])
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
