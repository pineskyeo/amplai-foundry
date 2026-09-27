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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..agent_drivers.ports import DriverRegistry
from ..control_plane.api_v3.server import ApiServices, BearerAuthenticator, create_app
from ..knowledge_runtime.service import KnowledgeService
from ..sandbox.container import ContainerProfile, ContainerSandbox
from ..sandbox.git_workspace import GitWorkspaceManager
from ..verification.runtime.patch_commands import NonEmptyChangeCheck, PatchCommandVerifier
from ..verification.runtime.service import VerificationService
from .contracts.authority import Actor, Authority
from .contracts.registry import Contracts
from .deployment import private_bytes, read_key
from .errors import Hold, RuntimeFault
from .evidence.cas import ArtifactStore
from .execution.codex import (
    CodexProfileInputs,
    ScopedCredential,
    build_codex_port,
    container_profile,
    install_codex_profile,
)
from .execution.loop import ExecutionLoop
from .execution.planner_codex import CodexPlanner
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
OPERATOR_PERMISSIONS = frozenset({"goal.submit", "runtime.read", "execution.approve"})


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
    apps: list[AppEntry] = Field(min_length=1, max_length=1)
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
        self.artifacts = ArtifactStore(self.store)
        signer = read_key(self.local(cfg.signing_key_file))
        verifier_signer = read_key(self.local(cfg.verifier_key_file))
        if (
            signer.public_key().public_bytes_raw()
            == verifier_signer.public_key().public_bytes_raw()
        ):
            raise Hold("KEY_SEPARATION", "Execution and verification need distinct keys")
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
        self._compose(cfg)
        self._planning: set[str] = set()
        self._planning_lock = threading.Lock()
        self.app = self._app(start_loop)

    def local(self, value: str) -> Path:
        p = Path(value).expanduser()
        return (p if p.is_absolute() else self.path.parent / p).absolute()

    def _compose(self, cfg: LocalConfig) -> None:
        entry = cfg.apps[0]
        repo = self.local(entry.repo)
        inputs = CodexProfileInputs(
            container_profile=self.local(entry.container_profile),
            egress_profile=self.local(cfg.codex.egress_profile),
            egress_qualification=self.local(cfg.codex.egress_qualification),
            qualification_report=self.local(entry.qualification_report),
            model=cfg.codex.model,
        )
        codex_refs = install_codex_profile(
            self.store, self.scope, inputs, app_capabilities(entry.app_id)
        )
        credential = ScopedCredential(self.local(cfg.codex.credential_home))
        root = self.local(cfg.workspace_root)
        self.workspaces = GitWorkspaceManager(root / "work", self.artifacts, {entry.app_id: repo})
        registry = DriverRegistry(self.store)
        admin = self.actors.service
        registry.register(
            admin, codex_refs["driver"], build_codex_port(inputs, root / "journal", credential)
        )
        self.coordinator = WorkCoordinator(
            self.runtime, registry, self.workspaces, poll_seconds=2.0, max_seconds=1800
        )
        agent_profile = container_profile(inputs)
        verify_profile = ContainerProfile(
            agent_profile.image,
            uid=agent_profile.uid,
            gid=agent_profile.gid,
            memory=agent_profile.memory,
            cpus=agent_profile.cpus,
            pids=agent_profile.pids,
            network="none",
        )
        verify_sandbox = ContainerSandbox(verify_profile)
        planner = CodexPlanner(
            ContainerSandbox(agent_profile), credential, root / "plans", model=cfg.codex.model
        )
        self.service = LocalExecutionService(
            store=self.store,
            runtime=self.runtime,
            goals=self.goals,
            knowledge=self.knowledge,
            authority=self.authority,
            verification=self.verification,
            workspaces=self.workspaces,
            planner=planner,
            actors=self.actors,
            codex_refs=codex_refs,
            verifier_factory=lambda app, v: PatchCommandVerifier(
                v.id, list(v.argv), workspaces=self.workspaces, scope=self.scope,
                sandbox=verify_sandbox, timeout_seconds=v.timeout_seconds,
            ),
            global_factory=lambda app: NonEmptyChangeCheck(self.workspaces, self.scope),
            publish_mode=cfg.publish_mode,
        )  # fmt: skip
        self.service.install(
            AppConfig(
                entry.app_id,
                repo,
                tuple(
                    VerifierCommand(v.id, tuple(v.argv), v.description, v.timeout_seconds)
                    for v in entry.verifiers
                ),
                tuple(entry.aliases),
                entry.base_branch,
                entry.remote,
            )
        )
        publisher = GitPublisher(self.service) if cfg.publish_mode != "none" else None
        self.loop = ExecutionLoop(self.service, self.coordinator, publisher=publisher)

    # -- operator authentication -----------------------------------------------------------------
    def operator(self) -> Actor:
        return Actor(
            self.config.operator_subject, self.scope, OPERATOR_PERMISSIONS, "human",
            "local-operator-token",
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


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("goal_id", "status", "app", "base_commit", "reason", "attempts", "publication")
    out = {k: record[k] for k in keys if k in record}
    draft = record.get("draft")
    if draft:
        out["summary"] = draft.get("summary")
        out["questions"] = draft.get("questions")
    return out
