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

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ...sandbox.git_workspace import CHANGE_MEDIA, GitWorkspaceManager
from ...verification.runtime.design_check import DESIGN_ROOT, DesignDocumentCheck
from ..contracts.authority import Actor
from ..contracts.identity import ID, digest, new_id, now
from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store
from . import context_assembly, policies, prompts, releases
from .cells import LEGACY_CELLS, Cell, model_slug
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
# Initial task-class baseline order, every class: operator decision 2026-09-28 (D-079);
# OpenCode appended by the operator 2026-09-29 (Work 031 D4).
ROUTER_ORDER = ("codex-cli", "claude-cli", "opencode-server")
NON_PLANNING_DRIVERS = frozenset({"opencode-server"})
DESIGN_CHECK = "design"
DESIGN_CHECK_DESCRIPTION = (
    "design document check: only files under specs/design/<goal>/ change; design.md has "
    "the sections Goal, Current State, Options, Decision, Risks, Implementation Plan, "
    "Sources; at least 3 path:line citations that resolve in the repository"
)
PORT = "change"
# the composition fields that carry the manifest (interfaces.md §2.3); a cell sibling keeps them
CARRIER_FIELDS = (
    "prompt_bundle_ref",
    "context_policy_ref",
    "budget_policy_ref",
    "router_policy_ref",
)


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
    # Work 033 S3 (interfaces.md §3.3): facts for the env_bootstrap component (D-096)
    tool_versions: tuple[tuple[str, str], ...] = ()  # container profile "tools" (deployment/*.json)
    quick_verifiers: tuple[str, ...] = ()  # verifier ids usable as fast checks (L7)


def git_fresh_base(repo: Path, remote: str, branch: str) -> str:
    """The revision a plan starts from (D-086): the remote's branch fetched now, or the local
    branch when the checkout has no such remote. The fetch updates only the remote-tracking
    ref, so the operator's branches and checkout are never moved; a failed fetch stops the plan
    instead of planning on a stale base."""
    if remote.startswith("-") or branch.startswith("-"):
        raise Hold("BASE_REF", "Remote and branch names cannot start with '-'")
    known = subprocess.run(
        ["git", "remote", "get-url", remote], cwd=repo, capture_output=True, timeout=30,
        check=False,
    )  # fmt: skip
    if known.returncode != 0:
        return branch
    fetched = subprocess.run(
        ["git", "fetch", "--no-tags", "--quiet", remote,
         f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"],
        cwd=repo, capture_output=True, timeout=300, check=False,
    )  # fmt: skip
    if fetched.returncode != 0:
        raise Hold(
            "BASE_FETCH",
            f"Could not fetch {remote}/{branch}; plan again once the remote is reachable",
            details=fetched.stderr.decode(errors="replace")[-400:],
        )
    return f"refs/remotes/{remote}/{branch}"


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
    # cell id -> ref; the legacy cell id is the driver id (IC-07), so Work 030 keys stay valid
    compositions: dict[str, dict[str, Any]] = field(default_factory=dict)
    router_ref: dict[str, Any] | None = None
    design_ref: dict[str, Any] | None = None  # the design-document verifier profile (F)
    driver_refs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)  # by cell id
    planners: dict[str, Any] = field(default_factory=dict)  # cell id -> planner
    cells: dict[str, Cell] = field(default_factory=dict)  # the non-legacy cells (Work 033 S4)


PLANNER_MODES = ("fixed", "real")


@dataclass(frozen=True)
class TrialContext:
    """What a trial goal carries into its plan (Work 033 S8, interfaces.md §3.3, §8.3).

    ``write_scope`` "trial" makes every node of the goal claim ``sandbox:<app>:trial:<goal_id>``
    instead of ``sandbox:<app>`` (IC-03): trial goals never publish and work on their own
    workspace copy, so two trials of one app may run at once while published goals keep the one
    write claim per repository.
    """

    subject: dict[str, str]  # {"experiment_id"|"calibration_plan_id", "trial_id"}
    arm: str
    cell_id: str
    split: str
    capture_trace: bool
    planner_mode: Literal["fixed", "real"]
    environment_id: str  # "app" or a task environment id
    write_scope: Literal["trial"] = "trial"

    def __post_init__(self) -> None:
        bad = [
            name
            for name, ok in (
                ("subject", isinstance(self.subject, dict)
                 and all(isinstance(k, str) and isinstance(v, str)
                         for k, v in self.subject.items())),
                ("arm", isinstance(self.arm, str) and bool(self.arm)),
                ("cell_id", isinstance(self.cell_id, str) and bool(self.cell_id)),
                ("split", isinstance(self.split, str) and bool(self.split)),
                ("capture_trace", type(self.capture_trace) is bool),
                ("planner_mode", self.planner_mode in PLANNER_MODES),
                ("environment_id", isinstance(self.environment_id, str)
                 and bool(self.environment_id)),
                ("write_scope", self.write_scope == "trial"),
            )
            if not ok
        ]  # fmt: skip
        if bad:
            raise RuntimeFault("TRIAL_CONTEXT", "Invalid trial context fields", details=bad)

    def wire(self) -> dict[str, Any]:
        return {
            "subject": dict(self.subject),
            "arm": self.arm,
            "cell_id": self.cell_id,
            "split": self.split,
            "capture_trace": self.capture_trace,
            "planner_mode": self.planner_mode,
            "environment_id": self.environment_id,
            "write_scope": self.write_scope,
        }


def write_resource(app_id: str, goal_id: str, *, trial: bool) -> str:
    """The resource a node of ``goal_id`` claims exclusively (IC-03): ``sandbox:<app>`` for every
    goal that may publish, ``sandbox:<app>:trial:<goal_id>`` for an experiment trial goal."""
    return f"sandbox:{app_id}:trial:{goal_id}" if trial else "sandbox:" + app_id


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
        base_fetcher: Callable[[Path, str, str], str] = git_fresh_base,
    ) -> None:
        self.store, self.runtime, self.goals, self.knowledge = store, runtime, goals, knowledge
        self.base_fetcher = base_fetcher  # plan base freshness (D-086)
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
        # Work 033 S9: the execution strategies (interfaces.md §3.6); the deployment sets one with
        # its read-only turn factory, else strategy_runner() builds one without auxiliary turns
        self.strategies: Any = None

    def strategy_runner(self) -> Any:
        """The ``StrategyRunner`` of this service (Work 033 S9). Without a configured one, the
        runner has no read-only turns: an auxiliary turn holds TURN_FAILED (``AUX_BUDGET`` first
        when the cap is reached)."""
        if self.strategies is None:
            from .integration_queue import IntegrationQueue
            from .strategy_runner import StrategyRunner

            def no_turns(cell_id: str) -> Any:
                raise Hold(
                    "TURN_FAILED", "No read-only turn is configured for this cell",
                    details={"cell_id": cell_id},
                )  # fmt: skip

            self.strategies = StrategyRunner(
                self, no_turns, IntegrationQueue(self.workspaces, self.scope)
            )
        return self.strategies

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
        cells: list[Cell] | None = None,
    ) -> InstalledApp:
        """Install ``app`` with one composition per cell (Work 033 S4, §2.4).

        ``driver_refs`` and ``planners`` are keyed by cell id; a legacy key is a driver id
        (IC-07) and needs no ``Cell``, every other key names one of ``cells`` (Hold
        CELL_UNKNOWN otherwise).
        """
        scope, a = self.scope, app.app_id
        caps = app_capabilities(a)
        drivers = driver_refs or self.drivers
        known = {c.cell_id: c for c in cells or []}
        unknown = sorted(k for k in drivers if k not in LEGACY_CELLS and k not in known)
        if unknown:
            raise Hold(
                "CELL_UNKNOWN", "Every non-legacy driver ref needs its cell", details=unknown
            )
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
        # the class-A prompt surface each composition pins (D-089 S1); the baseline is the
        # IMPLEMENTER text the loop used before bundles, byte for byte
        prompt_ref = self._put(
            prompts.KIND,
            prompts.BASELINE_ID,
            prompts.bundle(
                prompts.BASELINE_ID, prompts.IMPLEMENTER_BASELINE, "built-in baseline (Work 018)"
            ),
        )
        # The v1 manifest (D-096, interfaces.md §2.3): context, budget and router carriers name
        # the baseline components, which reproduce today's behaviour. The router keeps the id
        # <app>-router in the layered shape; its task-class order is the operator's (D-079).
        # the legacy order first (D-079, Work 031 D4), then the app's other cells in config order:
        # the first eligible cell stays the one selected today (Work 033 S4)
        route_order = [*ROUTER_ORDER, *(k for k in drivers if k not in LEGACY_CELLS)]
        carriers = self._baseline_carriers(a, prompt_ref, route_order)
        router_ref = carriers["router_policy_ref"]
        compositions = {}
        for cell_id, refs in drivers.items():
            name = self._composition_id(a, cell_id, known.get(cell_id))
            compositions[cell_id] = self._put_composition(
                name,
                {
                    "schema_version": "3.0.0",
                    "composition_id": name,
                    "model_profile_ref": refs["model"],
                    "driver_profile_ref": refs["driver"],
                    "sandbox_profile_ref": refs["environment"],
                    "pack_refs": [],
                    "prompt_bundle_ref": prompt_ref,
                    "router_policy_ref": router_ref,
                    "context_policy_ref": carriers["context_policy_ref"],
                    # the protected record stays the verification policy (class C, §2.3)
                    "verification_policy_ref": policy_ref,
                    "budget_policy_ref": carriers["budget_policy_ref"],
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
            {k: c for k, c in known.items() if k in drivers},
        )  # fmt: skip
        self.apps[a] = installed
        return installed

    @staticmethod
    def _composition_id(app_id: str, cell_id: str, cell: Cell | None) -> str:
        """``<app>-<driver short>`` for a legacy cell (unchanged), else
        ``<app>-<driver short>-<model_slug>-<effort>`` (interfaces.md §2.4)."""
        if cell is None:
            return f"{app_id}-{cell_id.split('-')[0]}"
        short = cell.driver_id.split("-")[0]
        return f"{app_id}-{short}-{model_slug(cell.model)}-{cell.effort}"

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

    def _baseline_carriers(
        self, app_id: str, prompt_ref: dict[str, Any], route_order: list[str] | None = None
    ) -> dict[str, Any]:
        """Baseline components and the carriers of the v1 manifest (idempotent per boot).

        An app whose cells extend the legacy order gets its own baseline route policy,
        ``route_policy.baseline.<app>`` (clarification after W0-W1: the shared
        ``route_policy.baseline`` would otherwise alternate versions between apps with different
        cells); an app with the legacy cells only keeps the shared one, as installed by S3.
        """
        from ...meta_harness.components import ComponentService
        from ...meta_harness.manifest import ManifestService

        components = ComponentService(self.store, self.scope)
        manifests = ManifestService(self.store, self.scope, self.runtime.contracts, components)
        manifest = manifests.baseline(
            self.actors.service,
            app_id,
            prompt_bundle_ref=prompt_ref,
            route_order=list(ROUTER_ORDER),
        )
        if route_order is not None and list(route_order) != list(ROUTER_ORDER):
            own = components.register(
                self.actors.service,
                component_id=f"route_policy.baseline.{app_id}",
                kind="route_policy",
                content={"order": {"*": list(route_order)}, "roles": {}},
                source="baseline",
                rationale="v1 order (D-079, Work 031 D4) and the app's other cells (Work 033 S4)",
            )
            manifest = manifests.change(manifest, route_policy=own)
        return manifests.write(self.actors.service, manifest, app_id=app_id)

    def _put_composition(self, name: str, value: dict[str, Any]) -> dict[str, Any]:
        """Write a composition whose ``revision`` field is the store revision it is written at
        (interfaces.md §4.1, row product.py:387); the latest one when nothing else changed.

        §14 Q12: the only reader of the field is ``CompositionService.register``, which stores a
        candidate derived from this value at that revision; no reader compares the two.
        """
        kind = "harness-composition"
        existing = [r for r, _ in self.store.list_objects(self.scope, kind) if r["id"] == name]
        latest = max(existing, key=lambda r: r["revision"]) if existing else None
        if latest and digest({**value, "revision": latest["revision"]}) == latest["digest"]:
            return latest
        return self._put(kind, name, {**value, "revision": latest["revision"] + 1 if latest else 1})

    def _budget_policy(self, composition_ref: dict[str, Any]) -> policies.BudgetPolicy:
        """The budget policy of a composition, under the deployment ceiling (D-096, IC-21)."""
        composition = self.store.get(self.scope, "harness-composition", composition_ref)
        return policies.budget_policy(
            self.store, self.scope, composition, ceiling=self.budget.wire()
        )

    def _repo_facts(self, app: AppConfig, workspace: Path) -> dict[str, Any]:
        """Facts the env_bootstrap component may show (interfaces.md §4.1, row product.py:552-569):
        the base tree, read from the plan workspace before it is discarded, and tool versions."""
        tree, truncated = context_assembly.repo_tree(workspace)
        return {
            "tree": tree,
            "tree_truncated": truncated,
            "tool_versions": [list(pair) for pair in app.tool_versions],
        }

    # -- composition selection (design/16:14-16, D-079) ---------------------------------------
    def select_composition(
        self,
        installed: InstalledApp,
        task_class: str | None = None,
        *,
        pin: dict[str, Any] | None = None,
        router_ref: dict[str, Any] | None = None,
        role: Literal["planner", "executor", "reviewer", "proposer"] = "executor",
    ) -> dict[str, Any]:
        """Filter candidates on data class, qualification and capabilities, then rank them by
        the task-class baseline policy. The operator sees the choice; nobody picks a driver.

        ``pin`` fixes the composition (an offline experiment arm or a canary goal, D-089). It must
        be an installed composition or its class-A candidate, and it still has to be eligible.

        Work 033 S4 (§3.3): candidates are cells; the router is ``router_ref`` or the one the
        app's effective compositions name (``releases.router_ref``); ``role`` reads the route
        policy's cell list for that role when it has one, else the task-class order."""
        from ...meta_harness.composition import CompositionService

        # the layered router (D-096) or a legacy task_class_baseline one, same order semantics
        router = router_ref or releases.router_ref(self.store, self.scope, installed)
        policy = policies.router_policy(self.store, self.scope, router)
        order = policy.roles.get(role) if role != "executor" else None
        order = order or policy.order.get(task_class or "*") or policy.order["*"]
        # the active release may carry a promoted class-A candidate of a composition (D-089 S2)
        available = releases.effective(self.store, self.scope, installed.compositions)
        if pin is not None:
            if releases.pin_allowed(self.store, self.scope, installed.compositions, pin) is None:
                raise Hold(
                    "COMPOSITION_PIN",
                    "A pinned composition must be an installed one or its class-A candidate",
                )
            candidates = [pin]
        else:
            candidates = [available[d] for d in order if d in available]
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
            "pinned": pin is not None,
            "policy_ref": router,
            "candidates": [
                {k: r[k] for k in ("driver_id", "model", "eligible", "reasons")} for r in rows
            ],
            "cell_id": releases.pin_allowed(self.store, self.scope, installed.compositions, chosen),
            "router_ref": router,
            "role": role,
        }

    # -- plan --------------------------------------------------------------------------------
    def plan(
        self,
        goal_id: str,
        *,
        composition: dict[str, Any] | None = None,
        planner: Any | None = None,
        revision: str | None = None,
        trial: TrialContext | None = None,
    ) -> dict[str, Any]:
        """Draft (selected driver, read-only) → compile → freeze contract → save graph.

        ``composition`` pins the composition for this goal, ``planner`` replaces the model planner
        and ``revision`` starts from that exact commit instead of the fetched base (an offline
        experiment trial: a fixed corpus contract on a fixed base, no model, D-089).

        ``trial`` marks an experiment trial goal (Work 033 S8): the plan record keeps it as
        ``record["trial"]`` and every node claims the per-trial write resource (IC-03).

        One app or several (D-081): every draft is normalised to work items (one per app that
        must change, with the apps it comes after); a one-app goal is exactly one item.
        """
        if trial is not None and not isinstance(trial, TrialContext):
            raise RuntimeFault("TRIAL_CONTEXT", "A trial goal carries a TrialContext")
        service, scope = self.actors.service, self.scope
        goal = self.store.head(scope, "goal", goal_id)
        intent = self.store.get(scope, "intent-envelope", goal["data"]["intent_ref"])
        targets = self._targets(intent)
        installed = targets[0]
        app = installed.config
        mode = intent.get("mode", "work")
        multi = len(targets) > 1
        if multi and (mode != "work" or revision is not None):
            raise Hold("DESIGN_ONE_APP", "A design or pinned-revision goal targets exactly one app")
        if multi and len({digest(self._environment(t)) for t in targets}) != 1:
            # one activated profile runs every node of a goal (driver, model, image)
            raise Hold("MULTI_APP_IMAGE", "The apps of one goal must share one worker image")
        # fetched now: a clone nobody pulls would plan every goal on an old base (D-086)
        bases = {
            t.config.app_id: self.workspaces.base_snapshot(
                scope, t.config.app_id,
                revision
                or self.base_fetcher(t.config.repo, t.config.remote, t.config.base_branch),
            )
            for t in targets
        }  # fmt: skip
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
        planning = self.select_composition(installed, pin=composition, role="planner")
        planner = planner or self._planner(installed, planning["cell_id"] or planning["driver_id"])
        runner = self.strategy_runner()
        planner_cell = planning["cell_id"] or planning["driver_id"]
        target_apps = [t.config.app_id for t in targets]
        # M7 (Work 033 S9): a strategy that needs a plan schema variant has the planner draft it
        variant, variant_args = self._plan_variant(
            runner, installed, composition, mode=mode, apps=target_apps,
            trial=trial is not None, planner=planner, planner_cell=planner_cell,
        )  # fmt: skip
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
                    intent["text"], app.app_id, verifiers, workspaces[app.app_id], mode=mode,
                    **variant_args,
                )  # fmt: skip
            # before the discard: the base facts env_bootstrap may show (D-096)
            repo_facts = {
                a: self._repo_facts(self.apps[a].config, w) for a, w in workspaces.items()
            }
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
            "repo_facts": repo_facts,
            **({"trial": trial.wire()} if trial is not None else {}),
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
        # selected before compiling: the composition's budget policy sets the root and node
        # budgets (interfaces.md §2.3, IC-21)
        chosen = self.select_composition(installed, draft.get("task_class"), pin=composition)
        budget = self._budget_policy(chosen["ref"])
        # Work 033 S9 (§3.6, §4.1 row product.py:647-654): the strategy, then its work items
        strategy, items, node_apps, extra = self._strategy_items(
            runner, goal_id, installed, chosen, budget, draft, items, base=base, mode=mode,
            apps=target_apps, trial=trial is not None, planner=planner,
            planner_cell=planner_cell, drafted=variant, verifier_ids=list(verifiers),
        )  # fmt: skip
        contract_ref, graph_ref, acceptance_map = self._compile(
            goal_id, intent, resolution_ref, resolution, bundle_ref, installed, draft,
            mode=mode, items=items, policy_ref=policy_ref, budget=budget,
            trial_scope=trial is not None,
            node_attempts=self._node_attempts(runner, strategy, budget),
        )  # fmt: skip
        record.update(strategy=strategy, work_items=items, node_apps=node_apps, **extra)
        record["acceptance_map"] = acceptance_map
        record["composition"] = chosen
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
        was = plan.get("composition") or {}
        pin = was["ref"] if was.get("pinned") else None
        planning = self.select_composition(installed, pin=pin, role="planner")
        planner = self._planner(installed, planning["cell_id"] or planning["driver_id"])
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
        # as in plan: the selected composition's budget policy sets the node budgets
        chosen = self.select_composition(installed, draft.get("task_class"), pin=pin)
        budget = self._budget_policy(chosen["ref"])
        # Work 033 S9: the replan drafts one item per app (v1 schema), so a strategy that splits
        # one app's change (M5/M7) does not carry over; that revision runs on the v1 prior
        strategy = self._replanned_strategy(plan, chosen)
        contract_ref, graph_ref, acceptance_map = self._compile(
            goal_id, intent, previous["resolution_ref"], {"target_refs": previous["targets"]},
            previous["context_bundle_ref"], installed, draft, mode=mode, items=items,
            policy_ref=previous["policy_ref"], revision=revision,
            previous_graph_ref=plan["graph_ref"], replan_reason=reason[:4000],
            budget=budget,
            trial_scope=bool(plan.get("trial")),  # a trial revision keeps its write scope (IC-03)
            node_attempts=self._node_attempts(self.strategy_runner(), strategy, budget),
        )  # fmt: skip
        record = {
            **{k: v for k, v in plan.items() if k not in {"decision_ref", "grant_ref",
                                                          "approved_at", "finished_at"}},
            "draft": draft,
            "work_items": items,
            "acceptance_map": acceptance_map,
            "planner_usage": drafted.get("usage"),
            "planned_with": {k: planning[k] for k in ("driver_id", "model")},
            "composition": chosen,
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "revision": revision,
            **({"strategy": strategy} if strategy is not None else {}),
            "node_apps": {"node-" + i["app"]: i["app"] for i in items},
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

    # -- strategies (Work 033 S9, interfaces.md §3.6, §5) -------------------------------------
    def _plan_variant(
        self,
        runner: Any,
        installed: InstalledApp,
        composition: dict[str, Any] | None,
        *,
        mode: str,
        apps: list[str],
        trial: bool,
        planner: Any,
        planner_cell: str,
    ) -> tuple[str | None, dict[str, Any]]:
        """The plan schema variant (M7) the planner drafts, and its draft arguments.

        Only a one-app work goal whose planner drafts variants (``planner.VARIANTS``) can: the
        strategy is read from the composition selected without a task class (the draft's class
        may select another one; ``_strategy_items`` then asks one more turn or falls back).
        Without a variant the planner is called exactly as before S9."""
        if len(apps) != 1 or mode != "work" or not getattr(planner, "VARIANTS", ()):
            return None, {}
        try:
            provisional = self.select_composition(installed, pin=composition)
            budget = self._budget_policy(provisional["ref"])
        except (Hold, RuntimeFault):
            return None, {}  # held again after the draft, at the same point as before S9
        early = runner.choose(
            installed=installed, composition=provisional, budget=budget, mode=mode, apps=apps,
            trial=trial, planner=planner, planner_cell=planner_cell,
        )  # fmt: skip
        variant = early.get("variant")
        if not variant:
            return None, {}
        params = early.get("params") or {}
        return variant, {"variant": variant, "max_parts": int(params.get("max_nodes", 4))}

    def _strategy_items(
        self,
        runner: Any,
        goal_id: str,
        installed: InstalledApp,
        chosen: dict[str, Any],
        budget: policies.BudgetPolicy,
        draft: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        base: dict[str, Any],
        mode: str,
        apps: list[str],
        trial: bool,
        planner: Any,
        planner_cell: str,
        drafted: str | None,
        verifier_ids: list[str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str], dict[str, Any]]:
        """The plan record's ``strategy``, the work items, ``node_apps`` and the strategy's plan
        fields (``plan_steps``, ``aux_usage``, ``aux_overrun``).

        A plan-time auxiliary turn that cannot run (``AUX_BUDGET``, ``TURN_*``) refuses a trial's
        strategy (the loop holds it before any claim) and puts a real goal on the v1 prior."""
        from .strategy_runner import PRIOR, AuxLedger, PlanContext, StrategyChoice

        strategy: dict[str, Any] = runner.choose(
            installed=installed, composition=chosen, budget=budget, mode=mode, apps=apps,
            trial=trial, planner=planner, planner_cell=planner_cell, after_draft=True,
            drafted=drafted,
        )  # fmt: skip
        ledger = AuxLedger(int(strategy["aux_cap"]))
        default: tuple[list[dict[str, Any]], dict[str, str]] = (
            items, {"node-" + i["app"]: i["app"] for i in items}
        )  # fmt: skip
        steps: list[dict[str, Any]] = []
        if strategy.get("refused"):
            new_items, node_apps = default
        else:
            context = PlanContext(goal_id, base, trial, ledger, verifier_ids)
            choice = StrategyChoice.of({"strategy": strategy})
            try:
                new_items, node_apps = runner.items(draft, choice, apps[0], context=context)
                steps = runner.steps(draft, choice, context=context)
            except (Hold, RuntimeFault) as exc:
                new_items, node_apps = default
                why = f"{strategy['strategy']}: {exc.code}: {exc.message}"[:600]
                if trial:
                    strategy["refused"] = why
                else:
                    cell = strategy["roles"].get("executor", "")
                    strategy.update(
                        strategy=PRIOR, params={}, used_prior=True, variant=None,
                        cascade=[cell], eligibility={**strategy["eligibility"], "plan": why},
                    )  # fmt: skip
        extra: dict[str, Any] = {"aux_usage": ledger.entries, "aux_overrun": ledger.overrun}
        if steps:
            extra["plan_steps"] = steps
        return strategy, new_items, node_apps, extra

    def _node_attempts(
        self, runner: Any, strategy: dict[str, Any] | None, budget: policies.BudgetPolicy
    ) -> int | None:
        """A strategy's own node attempt count (``single`` 1, ``best_of_n`` n), else None."""
        if not isinstance(strategy, dict):
            return None
        from .strategy_runner import StrategyChoice

        root = policies.root_budget(self.budget.wire(), budget.limits)
        value: int | None = runner.node_attempts(StrategyChoice.of({"strategy": strategy}), root)
        return value

    @staticmethod
    def _replanned_strategy(plan: dict[str, Any], chosen: dict[str, Any]) -> dict[str, Any] | None:
        """A replanned revision's strategy: unchanged, except that one splitting one app's change
        (workgraph_split, orchestrator, plan_execute) falls back to the v1 prior."""
        from .strategy_runner import ONE_APP, PRIOR

        strategy = plan.get("strategy")
        if not isinstance(strategy, dict):
            return None
        if strategy.get("strategy") not in ONE_APP:
            return strategy
        cell = chosen.get("cell_id") or chosen.get("driver_id") or ""
        return {
            **strategy, "strategy": PRIOR, "params": {}, "used_prior": True, "variant": None,
            "cascade": [cell],
            "eligibility": {**(strategy.get("eligibility") or {}),
                            "replan": "a replan drafts one work item per app"},
        }  # fmt: skip

    def escalate(self, goal_id: str, *, to_cell: str, reason: str) -> dict[str, Any]:
        """M6 cascade escalation (IC-05, interfaces.md §3.3): revision + 1 with the same draft
        and work items, pinned to the cell sibling of the goal's composition on ``to_cell``.

        The goal failed its attempts on its cell (the loop ended it ``escalation_pending``); the
        new revision waits for approval like a replan: a production goal needs the operator, a
        trial is approved by its experiment's operator identity (``LocalTrialExecutor``).
        Hold ESCALATION_STATE outside ``escalation_pending``, ESCALATION_LIMIT past the cascade's
        ``max_escalations``, CELL_UNKNOWN for a cell the app has not installed."""
        scope = self.scope
        plan = self.plan_record(goal_id)
        if plan.get("status") != "escalation_pending":
            raise Hold(
                "ESCALATION_STATE", "Only a goal that failed its attempts on its cell escalates",
                details=plan.get("status"),
            )  # fmt: skip
        strategy = dict(plan.get("strategy") or {})
        done = list(plan.get("escalations") or [])
        limit = int((strategy.get("params") or {}).get("max_escalations", 1))
        if strategy.get("strategy") != "cascade" or len(done) >= limit:
            raise Hold(
                "ESCALATION_LIMIT", "No escalation is left for this goal",
                details={"strategy": strategy.get("strategy"), "done": len(done), "limit": limit},
            )  # fmt: skip
        installed = self.apps[plan["app"]]
        if to_cell not in installed.compositions:
            raise Hold("CELL_UNKNOWN", "The cell is not installed for this app", details=to_cell)
        was = plan["composition"]
        sibling = self._cell_sibling(installed, was["ref"], to_cell)
        previous = self.store.get(scope, "goal-contract", plan["contract_ref"])
        intent = self.store.get(scope, "intent-envelope", previous["intent_ref"])
        chosen = self.select_composition(installed, was.get("task_class"), pin=sibling)
        budget = self._budget_policy(chosen["ref"])
        revision = previous["revision"] + 1
        contract_ref, graph_ref, acceptance_map = self._compile(
            goal_id, intent, previous["resolution_ref"], {"target_refs": previous["targets"]},
            previous["context_bundle_ref"], installed, plan["draft"],
            mode=plan.get("mode", "work"), items=plan["work_items"],
            policy_ref=previous["policy_ref"], revision=revision,
            previous_graph_ref=plan["graph_ref"],
            replan_reason=f"escalation to {to_cell}: {reason}"[:4000], budget=budget,
            trial_scope=bool(plan.get("trial")),  # a trial revision keeps its write scope (IC-03)
            node_attempts=self._node_attempts(self.strategy_runner(), strategy, budget),
        )  # fmt: skip
        entry = {
            "from_cell": was.get("cell_id"), "to_cell": to_cell, "reason": reason[:600],
            "previous_contract_ref": plan["contract_ref"],
            "previous_graph_ref": plan["graph_ref"], "at": now(),
        }  # fmt: skip
        roles = {**(strategy.get("roles") or {}), "executor": to_cell, "reviewer": to_cell}
        record = {
            **{k: v for k, v in plan.items() if k not in {"decision_ref", "grant_ref",
                                                          "approved_at", "finished_at"}},
            "composition": chosen,
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "acceptance_map": acceptance_map,
            "revision": revision,
            "strategy": {**strategy, "roles": roles},
            "escalation": entry,
            "escalations": [*done, entry],
            "previous_attempts": [
                *(plan.get("previous_attempts") or []),
                *(plan.get("attempts") or []),
            ],
            "attempts": [],
            "status": "awaiting_approval",
            "reason": None,
            "updated_at": now(),
        }  # fmt: skip
        self._save_plan(
            goal_id, record,
            ("approval.requested", {"contract_ref": contract_ref, "escalation": to_cell}),
        )  # fmt: skip
        return record

    def _cell_sibling(
        self, installed: InstalledApp, composition_ref: dict[str, Any], cell_id: str
    ) -> dict[str, Any]:
        """The goal composition's carriers on ``cell_id``'s installed composition (IC-05).

        The rule of ``ManifestService.cell_sibling`` (interfaces.md §3.2), which
        ``meta_harness/manifest.py`` does not provide yet: the same four carrier refs on the
        installed composition of the cell, id = that composition's id plus the source's candidate
        suffix. A source whose carriers equal the target's gives the target itself.
        ``pin_allowed`` admits the result (id prefix and the cell's own profiles)."""
        target_ref = installed.compositions.get(cell_id)
        if target_ref is None:
            raise Hold("CELL_UNKNOWN", "The cell is not installed for this app", details=cell_id)
        source = self.store.get(self.scope, "harness-composition", composition_ref)
        target = self.store.get(self.scope, "harness-composition", target_ref)
        if all(source[k] == target[k] for k in CARRIER_FIELDS):
            return target_ref
        source_cell = releases.pin_allowed(
            self.store, self.scope, installed.compositions, composition_ref
        )
        if source_cell is None:
            raise Hold("CELL_UNKNOWN", "The goal composition is not one of this app's cells")
        base = self.store.get(
            self.scope, "harness-composition", installed.compositions[source_cell]
        )
        suffix = source["composition_id"][len(base["composition_id"]) :]
        if not suffix:  # an installed composition whose carriers differ from the target's
            carriers = {k: source[k] for k in CARRIER_FIELDS}
            suffix = releases.CANDIDATE_SEP + "sibling-" + digest(carriers)[7:19]
        name = target["composition_id"] + suffix
        value = {**target, **{k: source[k] for k in CARRIER_FIELDS}, "composition_id": name}
        return self._put_composition(name, value)

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
        budget: policies.BudgetPolicy | None = None,
        trial_scope: bool = False,
        node_attempts: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, str]]]:
        """Deterministic contract + graph: one node per work item (a one-app goal is one).

        Work 033 S9 (M5): items from ``StrategyRunner.items`` carry ``node_id``
        (``node-<app>.s<k>`` / ``.p<k>`` / ``.int``) and ``after_nodes``; only those may name one
        app twice. ``node_attempts`` is a strategy's own attempt count (``single`` 1,
        ``best_of_n`` n); None keeps the attempt policy's.

        ``budget`` is the goal composition's budget policy (v1 without one): the root keeps
        min(limits, deployment ceiling) per field; each node takes the attempt policy's attempts
        and (root tokens - aux_max_tokens) // (attempts x nodes) (interfaces.md §2.3, IC-21).

        ``trial_scope``: the nodes of an experiment trial goal claim the per-trial write resource
        (IC-03); the requested capabilities stay those of the app (``sandbox:<app>``).
        """
        scope, service = self.scope, self.actors.service
        policy_ref = policy_ref or installed.policy_ref
        ceiling = self.budget.wire()
        budget = budget or policies.v1_budget(ceiling)
        root = policies.root_budget(ceiling, budget.limits)
        draft = _clean_draft(draft)
        items = items or [
            {"app": installed.config.app_id, "objective": draft["objective"],
             "in_scope": draft["in_scope"], "acceptance": draft["acceptance"], "after": []}
        ]  # fmt: skip
        design = mode == "design"
        evidence = "design-verification" if design else "command-verification"
        apps = [item["app"] for item in items]
        named = [item.get("node_id") for item in items]
        if any(named):
            # same-app items exist only as a strategy's nodes (§4.1 row product.py:849-852)
            ids = [str(n) for n in named if n]
            if (
                len(ids) != len(items)
                or len(set(ids)) != len(ids)
                or any(a not in self.apps for a in apps)
                or any(
                    not ID.fullmatch(n) or not n.startswith(f"node-{item['app']}.")
                    for n, item in zip(ids, items, strict=True)
                )
            ):
                raise Hold("PLANNING_TARGET", "Strategy work items need distinct node ids")
            earlier = [set(ids[:i]) for i in range(len(ids))]
            if any(
                d not in earlier[i] for i, item in enumerate(items)
                for d in item.get("after_nodes") or []
            ):  # fmt: skip
                raise Hold("PLANNING_ORDER", "A part can only come after an earlier part")
        elif len(set(apps)) != len(apps) or any(a not in self.apps for a in apps):
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
        attempt_policy = (
            budget.attempt_policy
            if node_attempts is None
            else {**budget.attempt_policy, "max_attempts": node_attempts}
        )
        nodes = []
        for item, ids in zip(items, node_acceptance, strict=True):
            target = self.apps[item["app"]]
            a = target.config.app_id
            depends = list(item.get("after_nodes") or []) or ["node-" + b for b in item["after"]]
            nodes.append(
                {
                    "node_id": item.get("node_id") or "node-" + a,
                    "work_id": new_id("work-" + a),
                    "target_ref": target.binding_ref,
                    "objective": item["objective"],
                    "strategy": "bounded_loop",
                    "depends_on": depends,
                    "join": "all_required",
                    # the downstream agent sees the verified upstream change (D-081); a same-app
                    # producer's change is the downstream node's base instead (M5, the loop)
                    "consumes": [
                        {
                            "name": "upstream-" + d[len("node-") :],
                            "from_node": d,
                            "external_ref": None,
                            "media_type": CHANGE_MEDIA,
                            "output_name": PORT,
                        }
                        for d in depends
                    ],
                    "produces": [{"name": PORT, "media_type": CHANGE_MEDIA, "required": True}],
                    "acceptance_ids": ids,
                    "verification_profile_ref": acceptance[int(ids[0][3:]) - 1]["verifier_ref"],
                    "capabilities": mode_capabilities(a, mode),
                    "resource_claims": [
                        {
                            "resource": write_resource(a, goal_id, trial=trial_scope),
                            "mode": "exclusive_write",
                        }
                    ],
                    "budget": policies.node_budget(root, attempt_policy, budget.limits, len(items)),
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
                installed.global_ref
                if len(set(apps)) == 1
                else self._global_for(list(dict.fromkeys(apps)))
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

    def _planner(self, installed: InstalledApp, cell_id: str) -> Any:
        """The planner of a cell (legacy cell id = driver id, IC-07); an OpenCode cell's goals
        are planned by the first available planner in router order (Work 031 O1)."""
        planner = installed.planners.get(cell_id) or (
            self.planner if cell_id == "codex-cli" else self.planners.get(cell_id)
        )
        driver_id = cell_id.split(".", 1)[0]  # IC-07: <driver>[.<model_slug>.<effort>]
        if planner is None and driver_id in NON_PLANNING_DRIVERS:
            # Work 031 O1 (operator 2026-09-29): OpenCode never plans; the first available
            # planner in router order does, whichever driver executes.
            for other in ROUTER_ORDER:
                planner = installed.planners.get(other) or (
                    self.planner if other == "codex-cli" else self.planners.get(other)
                )
                if planner is not None:
                    break
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
        goal = self.store.head(scope, "goal", goal_id)["data"]
        if (
            goal.get("active_contract_ref") == plan["contract_ref"]
            and goal.get("active_graph_ref") == plan["graph_ref"]
        ):
            # an earlier approval activated this contract and failed after it (review F6): its
            # grant and decision stand; only the remaining steps run, never a second grant
            grant = self.store.get(scope, "execution-grant", goal["grant_ref"])
            return self._approved(goal_id, plan, grant["decision_ref"], goal["grant_ref"])
        goal_apps = plan.get("apps") or [plan["app"]]
        caps = [c for a in goal_apps for c in mode_capabilities(a, plan.get("mode", "work"))]
        chosen = plan.get("composition")
        if chosen:
            # fixed from here on (design/16:16); eligibility may have changed since planning
            again = self.select_composition(
                installed,
                chosen.get("task_class"),
                pin=chosen["ref"] if chosen.get("pinned") else None,
            )
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
        return self._approved(goal_id, plan, decision_ref, grant_ref)

    def _approved(
        self,
        goal_id: str,
        plan: dict[str, Any],
        decision_ref: dict[str, Any],
        grant_ref: dict[str, Any],
    ) -> dict[str, Any]:
        steering_id: str = (plan.get("replan") or {}).get("steering_id") or ""
        if steering_id and self.store.head(self.scope, "steering", steering_id)["state"] == (
            "queued"
        ):
            # the admitted revision makes the operator's revision steering effective (D-082)
            SteeringService(self.runtime).apply_revision(self.actors.service, steering_id)
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
