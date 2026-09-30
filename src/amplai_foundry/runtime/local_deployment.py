"""Local single-operator product deployment (Work 018; D-066, D-070).

A composition root separate from ``deployment.py`` (which binds the multi-user Foundry
authority). Here one human operator authenticates with a 0600 token file and approves each
goal; execution authority comes from those approvals (``OperatorDecisions``), never from
credential presence. Everything the operator can do goes through the existing V3 API plus
``/api/v3/local`` routes:

    POST /api/v3/intents                        submit (existing route)
    POST /api/v3/local/goals/{id}/plan          read-only Codex plan (background, 202)
    GET  /api/v3/local/goals[/{id}]             plan / attempts / verification / publication
    POST /api/v3/local/goals/{id}/approve       human approval → grant → activation
    POST /api/v3/local/goals/{id}/cancel        revoke + stop

The execution loop runs in this process (the store has a single owner).
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..agent_drivers.ports import DriverRegistry
from ..control_plane.api_v3.server import ApiServices, BearerAuthenticator, create_app
from ..knowledge_runtime.service import KnowledgeService
from ..sandbox.container import ContainerProfile, ContainerSandbox
from ..sandbox.git_workspace import GitWorkspaceManager
from ..verification.runtime.integration import IntegrationCheck
from ..verification.runtime.patch_commands import NonEmptyChangeCheck, SuiteVerifier
from ..verification.runtime.service import VerificationService
from .contracts.authority import Actor, Authority
from .contracts.identity import digest
from .contracts.registry import Contracts
from .deployment import private_bytes, read_key
from .errors import Hold, RuntimeFault
from .evidence.cas import ArtifactStore
from .execution import releases
from .execution.codex import (
    CodexProfileInputs,
    ScopedCredential,
    build_claude_port,
    build_codex_port,
    build_opencode_port,
    container_profile,
    install_codex_profile,
    install_driver_profile,
)
from .execution.loop import ExecutionLoop
from .execution.meta_local import META_OPERATOR_PERMISSIONS, LocalMeta
from .execution.outcomes import PullRequestTracker
from .execution.planner_codex import ClaudePlanner, CodexPlanner
from .execution.product import (
    PLAN_KIND,
    Actors,
    AppConfig,
    LocalExecutionService,
    OperatorDecisions,
    VerifierCommand,
    app_capabilities,
)
from .execution.publish import GitPublisher
from .execution.service import Runtime
from .execution.worker import WorkCoordinator
from .goals.service import GoalService
from .storage.store import Scope, Store

SERVICE_PERMISSIONS = frozenset(
    {
        "goal.submit",
        "goal.resolve",
        "contract.propose",
        "graph.propose",
        "goal.activate",
        "app.register",
        "grant.issue",
        "goal.steer",
        "runtime.admin",
        "runtime.read",
        "execution.approve",
    }
)
OPERATOR_PERMISSIONS = frozenset({"goal.submit", "runtime.read", "execution.approve", "goal.steer"})


class VerifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    argv: list[str] = Field(min_length=1)
    description: str
    timeout_seconds: int = 900


class AppEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str
    repo: str
    container_profile: str  # deployment/local-container-app-<app>.json
    qualification_report: str  # container_qualify.py output for that image
    # Claude CLI qualification in the same image (Work 019 E); absent = Codex only
    claude_qualification_report: str | None = None
    # OpenCode in the same image (specs/031-opencode-driver); absent = no OpenCode candidate
    opencode_qualification_report: str | None = None
    verifiers: list[VerifierConfig] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    base_branch: str = "main"
    remote: str = "origin"


class CodexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_home: str  # scripts/sandbox_up.sh --codex-home DIR
    egress_profile: str
    egress_qualification: str
    model: str = "gpt-5.6-sol"
    enabled: bool = True  # operator switch: a disabled model is filtered out of selection


class ClaudeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_file: str  # 0600 file with CLAUDE_CODE_OAUTH_TOKEN=... (created by the operator)
    model: str = "claude-sonnet-5"
    enabled: bool = True


class OpenCodeEntry(BaseModel):
    """The third driver candidate (specs/031-opencode-driver): never plans, router order last."""

    model_config = ConfigDict(extra="forbid")
    credential_home: str  # scripts/sandbox_up.sh --opencode-home DIR (scoped copy)
    model: str = "opencode-go/glm-5.3-flash"  # the model the container qualification measured
    enabled: bool = True


class IntegrationEntry(BaseModel):
    """A cross-app check run on every named app's base + patch together (D-081)."""

    model_config = ConfigDict(extra="forbid")
    id: str
    apps: list[str] = Field(min_length=2)
    argv: list[str] = Field(min_length=1)  # apps are at /amplai-input/apps/<app> (read-only)
    description: str
    timeout_seconds: int = 900


class LocalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "local-1"
    runtime_root: str
    workspace_root: str  # under $HOME (colima shares $HOME only)
    scope: dict[str, str]
    operator_subject: str
    operator_token_file: str
    signing_key_file: str
    verifier_key_file: str
    codex: CodexEntry
    claude: ClaudeEntry | None = None
    opencode: OpenCodeEntry | None = None
    apps: list[AppEntry] = Field(min_length=1, max_length=4)
    integrations: list[IntegrationEntry] = Field(default_factory=list)
    publish_mode: str = "draft_pr"


class LocalProductDeployment:
    def __init__(self, config_path: Path, *, start_loop: bool = True) -> None:
        self.path = Path(config_path).expanduser().absolute()
        self.config = LocalConfig.model_validate_json(self.path.read_bytes())
        if self.config.schema_version != "local-1":
            raise Hold("CONFIG_VERSION", "Unknown local product configuration")
        cfg = self.config
        self.scope = Scope.parse(cfg.scope)
        self.contracts = Contracts()
        self.store = Store(self.local(cfg.runtime_root))
        # A Hold while composing (an unqualified driver, a missing key) must not leave the
        # store's owner lock held in this process: a CLI or a test may open it again.
        try:
            self._build(cfg, start_loop)
        except BaseException:
            self.store.close()
            raise

    def _build(self, cfg: LocalConfig, start_loop: bool) -> None:
        self.artifacts = ArtifactStore(self.store)
        signer = read_key(self.local(cfg.signing_key_file))
        verifier_signer = read_key(self.local(cfg.verifier_key_file))
        if (
            signer.public_key().public_bytes_raw()
            == verifier_signer.public_key().public_bytes_raw()
        ):
            raise Hold("KEY_SEPARATION", "Execution and verification need distinct keys")
        self._release_signer = signer  # signs the local baseline release (D-089 S2)
        self.authority = Authority(
            self.store,
            self.contracts,
            {"local-authority": signer.public_key()},
            OperatorDecisions(self.store),
            signer=signer,
            key_id="local-authority",
        )
        self.runtime = Runtime(self.store, self.contracts, self.authority, self.artifacts)
        self.goals = GoalService(self.store, self.contracts)
        self.knowledge = KnowledgeService(self.store, self.contracts)
        self.verification = VerificationService(
            self.runtime,
            signer=verifier_signer,
            key_id="local-verifier",
            trusted_keys={"local-verifier": verifier_signer.public_key()},
        )
        self.actors = Actors(
            service=Actor("amplai-local-service", self.scope, SERVICE_PERMISSIONS, "service",
                          "local-service"),
            worker=Actor("amplai-local-worker", self.scope, frozenset({"worker.execute"}),
                         "service", "local-worker"),
            verifier=Actor("amplai-local-verifier", self.scope, frozenset({"verifier.run"}),
                           "service", "local-verifier"),
        )  # fmt: skip
        # the meta-harness on this product's store; the release key is the local authority's (S3)
        self.meta_local = LocalMeta(
            self.store, self.contracts, self.artifacts, self.scope, signer.public_key()
        )
        self._compose(cfg)
        self._planning: set[str] = set()
        self._planning_lock = threading.Lock()
        self.app = self._app(start_loop)

    def local(self, value: str) -> Path:
        p = Path(value).expanduser()
        return (p if p.is_absolute() else self.path.parent / p).absolute()

    def _compose(self, cfg: LocalConfig) -> None:
        """Every configured app with its own image, drivers, planners and verifier sandbox."""
        root = self.local(cfg.workspace_root)
        repos = {entry.app_id: self.local(entry.repo) for entry in cfg.apps}
        self.workspaces = GitWorkspaceManager(root / "work", self.artifacts, repos)
        registry = DriverRegistry(self.store)
        admin = self.actors.service
        credential = ScopedCredential(self.local(cfg.codex.credential_home))
        token = claude_token(self.local(cfg.claude.token_file)) if cfg.claude else None
        registered: set[str] = set()
        per_app: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        verify_sandboxes: dict[str, ContainerSandbox] = {}
        first_codex: dict[str, Any] | None = None
        first_planner: Any = None
        # one environment/driver record per image, covering every app that runs in it: a
        # multi-app goal runs all its nodes in one activated profile (D-081)
        image_caps: dict[str, list[dict[str, Any]]] = {}
        for entry in cfg.apps:
            image = json.loads(self.local(entry.container_profile).read_text())["image"]
            caps = image_caps.setdefault(image, [])
            # config order, each capability once (an app listed twice adds nothing)
            caps.extend(c for c in app_capabilities(entry.app_id) if c not in caps)
        for entry in cfg.apps:
            image = json.loads(self.local(entry.container_profile).read_text())["image"]
            caps = image_caps[image]
            inputs = CodexProfileInputs(
                container_profile=self.local(entry.container_profile),
                egress_profile=self.local(cfg.codex.egress_profile),
                egress_qualification=self.local(cfg.codex.egress_qualification),
                qualification_report=self.local(entry.qualification_report),
                model=cfg.codex.model,
                enabled=cfg.codex.enabled,
            )
            codex_refs = install_codex_profile(self.store, self.scope, inputs, caps)
            if digest(codex_refs["driver"]) not in registered:  # apps may share an image
                registry.register(
                    admin, codex_refs["driver"],
                    build_codex_port(inputs, root / "journal", credential),
                )  # fmt: skip
                registered.add(digest(codex_refs["driver"]))
            agent_profile = container_profile(inputs)
            drivers = {"codex-cli": codex_refs}
            planners: dict[str, Any] = {
                "codex-cli": CodexPlanner(
                    ContainerSandbox(agent_profile),
                    credential,
                    root / "plans",
                    model=cfg.codex.model,
                )
            }
            if cfg.claude is not None and token and entry.claude_qualification_report:
                # the fallback composition (D-079): same image, its own measured qualification
                claude = replace(
                    inputs,
                    provider="claude",
                    model=cfg.claude.model,
                    qualification_report=self.local(entry.claude_qualification_report),
                    enabled=cfg.claude.enabled,
                )
                claude_refs = install_driver_profile(self.store, self.scope, claude, caps)
                if digest(claude_refs["driver"]) not in registered:
                    registry.register(
                        admin, claude_refs["driver"],
                        build_claude_port(claude, root / "journal", token),
                    )  # fmt: skip
                    registered.add(digest(claude_refs["driver"]))
                drivers["claude-cli"] = claude_refs
                planners["claude-cli"] = ClaudePlanner(
                    ContainerSandbox(container_profile(claude)), token, root / "plans",
                    model=cfg.claude.model,
                )  # fmt: skip
            if cfg.opencode is not None and entry.opencode_qualification_report:
                # the third composition (031 D4): same image, its own measured qualification,
                # one server per dispatch with a per-run password (D-091)
                opencode = replace(
                    inputs,
                    provider="opencode",
                    model=cfg.opencode.model,
                    qualification_report=self.local(entry.opencode_qualification_report),
                    enabled=cfg.opencode.enabled,
                )
                opencode_refs = install_driver_profile(self.store, self.scope, opencode, caps)
                if digest(opencode_refs["driver"]) not in registered:
                    registry.register(
                        admin, opencode_refs["driver"],
                        build_opencode_port(
                            opencode, root / "journal", self.local(cfg.opencode.credential_home)
                        ),
                    )  # fmt: skip
                    registered.add(digest(opencode_refs["driver"]))
                drivers["opencode-server"] = opencode_refs
            verify_sandboxes[entry.app_id] = ContainerSandbox(
                ContainerProfile(
                    agent_profile.image,
                    uid=agent_profile.uid,
                    gid=agent_profile.gid,
                    memory=agent_profile.memory,
                    cpus=agent_profile.cpus,
                    pids=agent_profile.pids,
                    network="none",
                )
            )
            per_app[entry.app_id] = (drivers, planners)
            if first_codex is None:
                first_codex, first_planner = codex_refs, planners["codex-cli"]
        assert first_codex is not None
        self.coordinator = WorkCoordinator(
            self.runtime, registry, self.workspaces, poll_seconds=2.0, max_seconds=1800
        )
        integrations = [
            (i.id, list(i.argv), i.timeout_seconds, list(i.apps)) for i in cfg.integrations
        ]

        def integration(apps: list[str]) -> IntegrationCheck:
            # the first goal app's image runs the integration commands (network none)
            return IntegrationCheck(
                integrations, workspaces=self.workspaces, scope=self.scope,
                sandbox=verify_sandboxes[apps[0]],
            )  # fmt: skip

        self.service = LocalExecutionService(
            store=self.store,
            runtime=self.runtime,
            goals=self.goals,
            knowledge=self.knowledge,
            authority=self.authority,
            verification=self.verification,
            workspaces=self.workspaces,
            planner=first_planner,
            actors=self.actors,
            codex_refs=first_codex,
            verifier_factory=lambda app: SuiteVerifier(
                [(v.id, list(v.argv), v.timeout_seconds) for v in app.verifiers],
                workspaces=self.workspaces, scope=self.scope,
                sandbox=verify_sandboxes[app.app_id],
            ),
            global_factory=lambda app: NonEmptyChangeCheck(self.workspaces, self.scope),
            publish_mode=cfg.publish_mode,
            integration_factory=integration,
        )  # fmt: skip
        for entry in cfg.apps:
            drivers, planners = per_app[entry.app_id]
            self.service.install(
                AppConfig(
                    entry.app_id,
                    repos[entry.app_id],
                    tuple(
                        VerifierCommand(v.id, tuple(v.argv), v.description, v.timeout_seconds)
                        for v in entry.verifiers
                    ),
                    tuple(entry.aliases),
                    entry.base_branch,
                    entry.remote,
                ),
                driver_refs=drivers,
                planners=planners,
            )
        # the baseline release of what is installed; a promoted release stays active (D-089 S2)
        releases.bootstrap(
            self.store, self.scope, self.contracts,
            [ref for app in self.service.apps.values() for ref in app.compositions.values()],
            signer=self._release_signer, key_id="local-authority",
        )  # fmt: skip
        publisher = GitPublisher(self.service) if cfg.publish_mode != "none" else None
        self.tracker = PullRequestTracker(self.service) if publisher is not None else None
        self.loop = ExecutionLoop(
            self.service, self.coordinator, publisher=publisher, tracker=self.tracker
        )

    # -- operator authentication -----------------------------------------------------------------
    def operator(self) -> Actor:
        return Actor(
            self.config.operator_subject, self.scope, OPERATOR_PERMISSIONS, "human",
            "local-operator-token",
        )  # fmt: skip

    def meta_operator(self) -> Actor:
        """The same human operator with the meta-harness reviewer permissions (never a proposer)."""
        return Actor(
            self.config.operator_subject, self.scope,
            OPERATOR_PERMISSIONS | META_OPERATOR_PERMISSIONS, "human", "local-operator-token",
        )  # fmt: skip

    def authenticate(self, authorization: str | None) -> Actor:
        # Read on every request: rotating the token file revokes the old one.
        token = private_bytes(self.local(self.config.operator_token_file)).decode().strip()
        bindings = {hashlib.sha256(token.encode()).hexdigest(): "operator"}
        actor: Actor = BearerAuthenticator(bindings, lambda _b: self.operator())(
            authorization or ""
        )
        return actor

    # -- planning in the background --------------------------------------------------------------
    def request_plan(self, goal_id: str) -> dict[str, Any]:
        with self._planning_lock:
            if goal_id in self._planning:
                return {"goal_id": goal_id, "status": "planning"}
            try:
                existing = self.service.plan_record(goal_id)
                if existing.get("status") not in {"plan_failed"}:
                    return existing
            except RuntimeFault:
                pass
            self._planning.add(goal_id)
        self.service._save_plan(goal_id, {"goal_id": goal_id, "status": "planning"})

        def run() -> None:
            try:
                self.service.plan(goal_id)
            except Exception as exc:
                self.service._save_plan(
                    goal_id,
                    {"goal_id": goal_id, "status": "plan_failed",
                     "reason": f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"[:600],
                     "details": str(getattr(exc, "details", ""))[:1500]},
                )  # fmt: skip
            finally:
                with self._planning_lock:
                    self._planning.discard(goal_id)

        threading.Thread(target=run, name="amplai-plan-" + goal_id, daemon=True).start()
        return {"goal_id": goal_id, "status": "planning"}

    def list_plans(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT data FROM heads WHERE tenant=? AND project=? AND kind=? "
                "ORDER BY rowid DESC LIMIT ?",
                (*self.scope.keys(), PLAN_KIND, limit),
            ).fetchall()
        return [_summary(json.loads(r["data"])) for r in rows]

    # -- HTTP --------------------------------------------------------------------------------
    def _app(self, start_loop: bool) -> Any:
        from fastapi import APIRouter, Depends, Header

        services = ApiServices(
            self.runtime, self.goals, self.authenticate, verification=self.verification
        )
        app = create_app(services)
        router = APIRouter(prefix="/api/v3/local")

        def actor(authorization: str | None = Header(default=None)) -> Actor:
            return self.authenticate(authorization)

        @router.post("/goals/{goal_id}/plan", status_code=202)
        def plan(goal_id: str, a: Actor = Depends(actor)) -> Any:
            a.require("runtime.read")
            return self.request_plan(goal_id)

        @router.get("/goals")
        def goals(a: Actor = Depends(actor)) -> Any:
            a.require("runtime.read")
            return self.list_plans()

        @router.get("/goals/{goal_id}")
        def goal(goal_id: str, a: Actor = Depends(actor)) -> Any:
            a.require("runtime.read")
            return self.service.plan_record(goal_id)

        @router.post("/goals/{goal_id}/approve")
        def approve(goal_id: str, a: Actor = Depends(actor)) -> Any:
            return _summary(self.service.approve(a, goal_id))

        @router.post("/goals/{goal_id}/cancel")
        def cancel(goal_id: str, a: Actor = Depends(actor)) -> Any:
            return _summary(self.loop.cancel(a, goal_id))

        @router.post("/goals/{goal_id}/steer")
        def steer(goal_id: str, body: dict[str, Any], a: Actor = Depends(actor)) -> Any:
            return self.loop.steer(a, goal_id, str(body.get("text", "")))

        @router.post("/goals/{goal_id}/replan")
        def replan(goal_id: str, body: dict[str, Any], a: Actor = Depends(actor)) -> Any:
            return self.loop.replan(a, goal_id, str(body.get("reason", "")))

        @router.post("/publications/sync")
        def pr_sync(a: Actor = Depends(actor)) -> Any:
            a.require("execution.approve")
            if self.tracker is None:
                raise Hold("PUBLISH_DISABLED", "Publication is off in this deployment")
            return {"recorded": self.tracker.sync()}

        app.include_router(router)
        if start_loop:
            original = app.router.lifespan_context

            @asynccontextmanager
            async def lifespan(application: Any) -> Any:
                self.loop.start()
                try:
                    async with original(application) as state:
                        yield state
                finally:
                    self.loop.stop()

            app.router.lifespan_context = lifespan
        return app

    def close(self) -> None:
        self.loop.stop()
        self.store.close()


def claude_token(path: Path) -> str:
    """CLAUDE_CODE_OAUTH_TOKEN from the operator's 0600 env file; the value is never logged."""
    for line in private_bytes(path).decode().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        if key.strip() == "CLAUDE_CODE_OAUTH_TOKEN" and value.strip():
            return value.strip().strip('"').strip("'")
    raise Hold("AUTH_TOKEN_REQUIRED", "The Claude token file has no CLAUDE_CODE_OAUTH_TOKEN")


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("goal_id", "status", "app", "base_commit", "reason", "attempts", "publication")
    out = {k: record[k] for k in keys if k in record}
    draft = record.get("draft")
    if draft:
        out["summary"] = draft.get("summary")
        out["questions"] = draft.get("questions")
    return out
