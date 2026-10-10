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
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from .errors import Hold, RuntimeFault
from .evidence.cas import ArtifactStore
from .execution import releases
from .execution.cells import (
    EFFORT_SYNTAX,
    LEGACY_EFFORT,
    PROVIDERS,
    Cell,
    CellInstaller,
    latest_probe,
)
from .execution.codex import (
    CodexProfileInputs,
    ScopedCredential,
    build_claude_port,
    build_codex_port,
    build_opencode_port,
    container_profile,
    measured_qualification,
)
from .execution.integration_queue import IntegrationQueue
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
    TaskEnvironment,
    VerifierCommand,
    app_capabilities,
)
from .execution.publish import GitPublisher
from .execution.service import Runtime
from .execution.strategy_runner import StrategyRunner
from .execution.worker import WorkCoordinator
from .goals.service import GoalService
from .keys import private_bytes, read_key
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
OPERATOR_PERMISSIONS = frozenset(
    {"goal.submit", "runtime.read", "execution.approve", "goal.steer", "goal.cancel"}
)


class VerifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    argv: list[str] = Field(min_length=1)
    description: str
    timeout_seconds: int = 900
    quick: bool = False  # usable as an L7 fast check (Work 033 §12.2, AppConfig.quick_verifiers)


class EnvironmentEntry(BaseModel):
    """A task environment of an app (IC-12, §12.2, §10.5 step 6): the task image's container
    profile and, per driver id, its ``container_qualify.py`` report in that image. Each installed
    cell of the app whose driver has a report here gets its records and port in the task image
    (Work 033 S7b, ``LocalProductDeployment._task_environments``)."""

    model_config = ConfigDict(extra="forbid")
    environment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    container_profile: str
    qualification_reports: dict[str, str] = Field(default_factory=dict)  # driver id -> path


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
    environments: list[EnvironmentEntry] = Field(default_factory=list)  # IC-12 (S7b)

    @model_validator(mode="after")
    def _environments(self) -> AppEntry:
        ids = [e.environment_id for e in self.environments]
        if len(set(ids)) != len(ids) or "app" in ids:
            raise ValueError(f"{self.app_id}: each task environment id once, never 'app'")
        return self

    def driver_report(self, driver_id: str) -> str | None:
        """The app's own qualification report for a driver (the legacy entries)."""
        return {
            "codex-cli": self.qualification_report,
            "claude-cli": self.claude_qualification_report,
            "opencode-server": self.opencode_qualification_report,
        }[driver_id]


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


class CellEntry(BaseModel):
    """A non-legacy cell (Work 033 S4, D-097, §12.2): driver, model and reasoning effort.

    The legacy ``codex``/``claude``/``opencode`` entries define the legacy cells (IC-07). An
    effort outside the driver's documented values is refused here (EFFORT_UNSUPPORTED); a
    documented one still needs an accepted ``amplai ops local-cell probe`` before it installs.
    """

    model_config = ConfigDict(extra="forbid")
    driver: Literal["codex-cli", "claude-cli", "opencode-server"]
    model: str
    effort: str
    # per model; absent = each app's own report for that driver (effort variants share it)
    qualification_reports: dict[str, str] | None = None
    enabled: bool = True

    def cell(self) -> Cell:
        reports = tuple(sorted((self.qualification_reports or {}).items()))
        cell_id = Cell.make_id(self.driver, self.model, self.effort, legacy=False)
        return Cell(cell_id, self.driver, self.model, self.effort, reports, legacy=False)

    @model_validator(mode="after")
    def _effort(self) -> CellEntry:
        if self.effort != LEGACY_EFFORT and self.effort not in EFFORT_SYNTAX[self.driver]:
            raise Hold(
                "EFFORT_UNSUPPORTED", f"{self.driver} does not document this effort",
                details={"effort": self.effort, "documented": list(EFFORT_SYNTAX[self.driver])},
            )  # fmt: skip
        self.cell()  # Hold MODEL_UNPINNED / EFFORT_UNSUPPORTED for an unusable model or effort
        return self


class RolesEntry(BaseModel):
    """Cell lists per role (§12.2, L3). Accepted and checked from S4 on; the execution loop
    refuses route-policy roles until it honours them (loop.py ``_router_unsupported``), so they
    do not reach the router yet."""

    model_config = ConfigDict(extra="forbid")
    planner: list[str] | None = Field(default=None, min_length=1, max_length=64)
    reviewer: list[str] | None = Field(default=None, min_length=1, max_length=64)
    proposer: list[str] | None = Field(default=None, min_length=1, max_length=64)


class NightlyEntry(BaseModel):
    """= ``NightlyConfig`` (§3.13); the canonical shape is the ``nightly-plan`` record (§2.14)."""

    model_config = ConfigDict(extra="forbid")
    budget_trials: int = Field(default=150, ge=0)
    shares: dict[str, float] = Field(
        default_factory=lambda: {
            "drift": 0.1, "screening_design": 0.0, "search": 0.6, "confirmation": 0.3,
        }
    )  # fmt: skip
    cells: list[str] = Field(default_factory=list)
    max_parallel: int = Field(default=2, ge=1, le=4)
    pilot: bool = True
    stop_at: str = Field(default="07:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    drift_tasks: list[str] = Field(default_factory=list)
    pilot_nights: int = Field(default=3, ge=0)
    keep_operator_share: float = Field(default=0.5, ge=0, le=1)
    # clarification after S12: a night ends at min(stop_at, start + max_hours)
    max_hours: float = Field(default=8.0, gt=0, le=24)

    @model_validator(mode="after")
    def _shares(self) -> NightlyEntry:
        # §2.14: drift + search + confirmation = 1; 0 <= screening_design <= search
        s = self.shares
        if set(s) != {"drift", "screening_design", "search", "confirmation"} or any(
            not 0 <= v <= 1 for v in s.values()
        ):
            raise ValueError("shares are drift, screening_design, search, confirmation in 0..1")
        if abs(s["drift"] + s["search"] + s["confirmation"] - 1) > 1e-9:
            raise ValueError("drift + search + confirmation = 1 (§2.14)")
        if s["screening_design"] > s["search"]:
            raise ValueError("screening_design <= search (§2.14)")
        return self


class MetaEntry(BaseModel):
    """Meta-harness settings (§12.2); read by S11-S13, accepted from S4 on."""

    model_config = ConfigDict(extra="forbid")
    corpus_root: str = "specs/033-harness-taxonomy/corpus"
    evaluator_version: str = "eval-2"
    max_parallel_trials: int = Field(default=2, ge=1, le=4)
    trace_capture: bool = True  # trials only
    stage_template: str = "default_v1"
    nightly: NightlyEntry | None = None


DataClass = Literal["public", "internal", "confidential", "restricted"]
JEV_DEFAULT: tuple[DataClass, ...] = ("public",)


class JevEntry(BaseModel):
    """The Jev judge stub's config (§6.6); access and API are 확인 필요 (§14 Q9)."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    endpoint: str | None = None
    token_file: str | None = None
    data_classes_allowed: list[DataClass] = Field(default_factory=lambda: list(JEV_DEFAULT))


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
    # Work 033 S4 (§12.2): every key optional, so a local-1 file without them behaves as today
    cells: list[CellEntry] = Field(default_factory=list)
    roles: RolesEntry | None = None
    meta: MetaEntry | None = None
    jev: JevEntry | None = None

    def legacy_cells(self) -> dict[str, str]:
        """The legacy cells (IC-07): configured driver id -> its model."""
        out = {"codex-cli": self.codex.model}
        if self.claude is not None:
            out["claude-cli"] = self.claude.model
        if self.opencode is not None:
            out["opencode-server"] = self.opencode.model
        return out

    @model_validator(mode="after")
    def _cells(self) -> LocalConfig:
        legacy = self.legacy_cells()
        seen: set[str] = set()
        for entry in self.cells:
            cell = entry.cell()
            if entry.driver not in legacy:
                raise ValueError(f"{cell.cell_id}: configure the {entry.driver} driver first")
            if cell.cell_id in seen or (
                entry.effort == LEGACY_EFFORT and entry.model == legacy[entry.driver]
            ):
                raise ValueError(f"{cell.cell_id}: the cell is already configured")
            seen.add(cell.cell_id)
            unknown = set(entry.qualification_reports or {}) - {a.app_id for a in self.apps}
            if unknown:
                raise ValueError(f"{cell.cell_id}: unknown apps {sorted(unknown)}")
        known = set(legacy) | seen
        named: list[str] = []
        if self.roles is not None:
            for listed in (self.roles.planner, self.roles.reviewer, self.roles.proposer):
                named += listed or []
        if self.meta is not None and self.meta.nightly is not None:
            named += self.meta.nightly.cells
        if set(named) - known:
            raise ValueError(f"unknown cells {sorted(set(named) - known)}")
        return self


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
        per_app: dict[str, tuple[dict[str, Any], dict[str, Any], list[Cell]]] = {}
        per_app_envs: dict[str, list[TaskEnvironment]] = {}
        # task environments or cells an app's environment did not install, with the reason (IC-12)
        self.environment_skips: list[dict[str, Any]] = []
        cells = CellInstaller(self.store, self.scope)
        recorded: set[str] = set()  # cells whose harness-cell record this boot wrote
        # configured cells an app did not install, with the reason (read by operators and tests)
        self.cell_skips: list[dict[str, Any]] = []
        installed_cells: set[str] = set()

        def install_cell(
            cell: Cell, inputs: CodexProfileInputs, caps: list[dict[str, Any]], app_id: str
        ) -> dict[str, Any] | None:
            """A cell's records; its harness-cell record comes from the first app that installs
            it (§2.4 names one image per cell; apps in the same image reuse it).

            An effort cell installs only in the image and driver version its latest probe ran
            on (§2.4: one image per cell, one probe per cell id). An app in another image skips
            it (None, reason recorded) instead of holding the whole boot; a cell no app can
            install still holds EFFORT_UNPROBED after the loop, as before."""
            found = latest_probe(self.store, self.scope, cell.cell_id)
            probe = found[1] if found is not None else None
            if not cell.legacy and cell.effort != LEGACY_EFFORT and probe is not None:
                measured = measured_qualification(replace(inputs, model=cell.model))
                ran_on = (probe.get("image"), probe.get("driver_version"))
                if ran_on != (measured["image"], measured["driver_version"]):
                    self.cell_skips.append({
                        "cell_id": cell.cell_id, "app": app_id, "code": "EFFORT_UNPROBED",
                        "reason": "the cell's probe ran on another image or driver version",
                        "probe_image": probe.get("image"), "image": measured["image"],
                    })  # fmt: skip
                    return None
            installed_cells.add(cell.cell_id)
            if cell.cell_id in recorded:
                refs, _ = cells.profile(cell, inputs, caps, probe=probe)
                return refs
            recorded.add(cell.cell_id)
            return cells.install(cell, inputs, caps, probe=probe)

        def register(refs: dict[str, Any], build: Any) -> None:
            if digest(refs["driver"]) not in registered:  # apps and effort variants share
                registry.register(admin, refs["driver"], build())
                registered.add(digest(refs["driver"]))

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
            codex_refs = install_cell(
                legacy_cell("codex-cli", inputs, entry.app_id), inputs, caps, entry.app_id
            )
            assert codex_refs is not None  # a legacy cell is never skipped
            register(codex_refs, lambda i=inputs: build_codex_port(i, root / "journal", credential))
            agent_profile = container_profile(inputs)
            drivers = {"codex-cli": codex_refs}
            # IC-12 (S7b): each installed cell with the inputs it was installed from
            installed_inputs: dict[str, tuple[Cell, CodexProfileInputs]] = {
                "codex-cli": (legacy_cell("codex-cli", inputs, entry.app_id), inputs),
            }
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
                claude_refs = install_cell(
                    legacy_cell("claude-cli", claude, entry.app_id), claude, caps, entry.app_id
                )
                assert claude_refs is not None
                register(
                    claude_refs, lambda c=claude: build_claude_port(c, root / "journal", token)
                )
                drivers["claude-cli"] = claude_refs
                installed_inputs["claude-cli"] = (
                    legacy_cell("claude-cli", claude, entry.app_id), claude,
                )  # fmt: skip
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
                opencode_refs = install_cell(
                    legacy_cell("opencode-server", opencode, entry.app_id), opencode, caps,
                    entry.app_id,
                )  # fmt: skip
                assert opencode_refs is not None
                register(
                    opencode_refs,
                    lambda o=opencode: build_opencode_port(
                        o, root / "journal", self.local(cfg.opencode.credential_home)
                    ),
                )
                drivers["opencode-server"] = opencode_refs
                installed_inputs["opencode-server"] = (
                    legacy_cell("opencode-server", opencode, entry.app_id), opencode,
                )  # fmt: skip
            app_cells: list[Cell] = []
            for cell_entry in cfg.cells:
                # Work 033 S4: the configured cells in this app's image, each with the model's
                # qualification report (D-097), a port per model and a planner per cell
                report = (
                    (cell_entry.qualification_reports or {}).get(entry.app_id)
                    if cell_entry.qualification_reports is not None
                    else entry.driver_report(cell_entry.driver)
                )
                cell = cell_entry.cell()
                if report is None:  # this cell is not qualified in this app's image
                    self.cell_skips.append({
                        "cell_id": cell.cell_id, "app": entry.app_id, "code": None,
                        "reason": "no qualification report for this app",
                    })  # fmt: skip
                    continue
                # `ops local-driver <d> --disable` is the per-driver kill switch: a cell is
                # eligible only while both its own flag and its driver's legacy entry are
                # enabled. §12.2 leaves the combination open; S4 security review chose this rule
                # (to be recorded in the interfaces.md Clarifications section).
                # No legacy entry means no switch was set (local-driver refuses to switch one).
                driver_entry = {
                    "codex-cli": cfg.codex, "claude-cli": cfg.claude,
                    "opencode-server": cfg.opencode,
                }[cell.driver_id]  # fmt: skip
                cell_inputs = replace(
                    inputs, provider=cell.provider, model=cell.model,
                    qualification_report=self.local(report),
                    enabled=cell_entry.enabled
                    and (driver_entry is None or driver_entry.enabled),
                )  # fmt: skip
                refs = install_cell(cell, cell_inputs, caps, entry.app_id)
                if refs is None:
                    continue  # probed in another image (recorded in cell_skips)
                register(refs, lambda ci=cell_inputs: self._port(cfg, ci, root, credential, token))
                drivers[cell.cell_id] = refs
                app_cells.append(cell)
                installed_inputs[cell.cell_id] = (cell, cell_inputs)
                effort = None if cell.effort == LEGACY_EFFORT else cell.effort
                sandbox = ContainerSandbox(container_profile(cell_inputs))
                if cell.driver_id == "codex-cli":
                    planners[cell.cell_id] = CodexPlanner(
                        sandbox, credential, root / "plans", model=cell.model, effort=effort,
                        cell_id=cell.cell_id,
                    )  # fmt: skip
                elif cell.driver_id == "claude-cli" and token:
                    planners[cell.cell_id] = ClaudePlanner(
                        sandbox, token, root / "plans", model=cell.model, effort=effort,
                        cell_id=cell.cell_id,
                    )  # fmt: skip
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
            per_app[entry.app_id] = (drivers, planners, app_cells)
            per_app_envs[entry.app_id] = self._task_environments(
                entry, installed_inputs, cells,
                lambda refs, ei: register(
                    refs, lambda e=ei: self._port(cfg, e, root, credential, token)
                ),
            )  # fmt: skip
            if first_codex is None:
                first_codex, first_planner = codex_refs, planners["codex-cli"]
        assert first_codex is not None
        stale = sorted(
            {s["cell_id"] for s in self.cell_skips if s["code"] == "EFFORT_UNPROBED"}
            - installed_cells
        )
        if stale:
            # no app runs the image its probe ran on: re-probe, as a single-app boot always held
            raise Hold(
                "EFFORT_UNPROBED", "The probe ran on another image or driver version",
                details=[s for s in self.cell_skips if s["cell_id"] in stale],
            )  # fmt: skip
        self.coordinator = WorkCoordinator(
            self.runtime, registry, self.workspaces, poll_seconds=2.0, max_seconds=1800,
            trace_sink=self._trace_sink(cfg),
        )  # fmt: skip
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
            drivers, planners, app_cells = per_app[entry.app_id]
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
                    tool_versions=tool_versions(self.local(entry.container_profile)),
                    quick_verifiers=tuple(v.id for v in entry.verifiers if v.quick),
                ),
                driver_refs=drivers,
                planners=planners,
                cells=app_cells,
                environments=per_app_envs[entry.app_id],
            )
        # Work 033 S9 (§3.6): the execution strategies with their read-only turns, one per cell
        self.service.strategies = StrategyRunner(
            self.service,
            read_only_turns([per_app[entry.app_id][1] for entry in cfg.apps]),
            IntegrationQueue(self.workspaces, self.scope),
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

    def _task_environments(
        self,
        entry: AppEntry,
        installed: dict[str, tuple[Cell, CodexProfileInputs]],
        cells: CellInstaller,
        register: Callable[[dict[str, Any], CodexProfileInputs], None],
    ) -> list[TaskEnvironment]:
        """IC-12 (Work 033 S7b, §10.5 step 6, §12.2): per configured task environment of the
        app, each installed cell's records in the task image (``CellInstaller.profile``: the
        environment, qualification, driver and model records of that image, no ``harness-cell``
        record) and its port, and the app's suite verifier running in the task image with
        network none. A cell whose driver has no report for the environment, or whose records
        cannot be installed there (``DRIVER_UNQUALIFIED``; ``EFFORT_UNPROBED`` / ``EFFORT_REFUSED``:
        an effort cell needs an accepted probe of (cell, this environment) whose image and driver
        version are this environment's, ``ops local-cell probe CELL --environment ENV``; probes
        never run at boot), is skipped with the reason in ``environment_skips``; an
        environment no cell qualifies in is skipped as a whole (its trials then hold
        ENVIRONMENT_UNQUALIFIED before any claim)."""
        out: list[TaskEnvironment] = []
        caps = app_capabilities(entry.app_id)
        for env in entry.environments:
            profile_path = self.local(env.container_profile)
            env_drivers: dict[str, dict[str, Any]] = {}
            for cell_id, (cell, base_inputs) in installed.items():
                report = env.qualification_reports.get(cell.driver_id)
                skip = {"app": entry.app_id, "environment_id": env.environment_id,
                        "cell_id": cell_id}  # fmt: skip
                if report is None:
                    self.environment_skips.append(
                        {**skip, "code": None, "reason": "no qualification report for this driver"}
                    )
                    continue
                env_inputs = replace(
                    base_inputs, container_profile=profile_path,
                    qualification_report=self.local(report),
                )  # fmt: skip
                # an effort cell counts only the probe of (cell, this environment): never the
                # app-image probe nor another environment's (S7b); the probe's image and driver
                # version must still equal this environment's (``CellInstaller.profile``)
                found = (
                    None
                    if cell.effort == LEGACY_EFFORT
                    else latest_probe(self.store, self.scope, cell.cell_id, env.environment_id)
                )
                try:
                    refs, _measured = cells.profile(
                        cell, env_inputs, caps, probe=found[1] if found is not None else None
                    )
                except Hold as exc:
                    reason = str(exc)[:240]
                    if exc.code in ("EFFORT_UNPROBED", "EFFORT_REFUSED"):
                        reason += (
                            f"; run amplai ops local-cell probe {cell_id}"
                            f" --environment {env.environment_id}"
                        )
                    self.environment_skips.append({**skip, "code": exc.code, "reason": reason})
                    continue
                register(refs, env_inputs)
                env_drivers[cell_id] = refs
            if not env_drivers:
                self.environment_skips.append({
                    "app": entry.app_id, "environment_id": env.environment_id, "cell_id": None,
                    "code": "ENVIRONMENT_UNQUALIFIED", "reason": "no cell is qualified in it",
                })  # fmt: skip
                continue
            # the verifier runs in the task image with network none (as ``verify_sandboxes``)
            c = json.loads(profile_path.read_text())
            sandbox = ContainerSandbox(
                ContainerProfile(
                    c["image"], uid=c["uid"], gid=c["gid"], memory=c["memory"], cpus=c["cpus"],
                    pids=c["pids"], network="none",
                )
            )  # fmt: skip
            out.append(
                TaskEnvironment(
                    env.environment_id,
                    next(iter(env_drivers.values()))["environment"],
                    env_drivers,
                    SuiteVerifier(
                        [(v.id, list(v.argv), v.timeout_seconds) for v in entry.verifiers],
                        workspaces=self.workspaces,
                        scope=self.scope,
                        sandbox=sandbox,
                    ),
                )
            )
        return out

    def _trace_sink(self, cfg: LocalConfig) -> Callable[[str, dict[str, Any]], None] | None:
        """The coordinator's trace sink (Work 033 S13, §9.1-§9.3): admits a run's sanitized
        trace when the run's goal plan carries a trial with ``capture_trace`` (trial goals only,
        D-100); ``meta.trace_capture`` false turns it off. ``self.service`` is read at call time
        (it is built after the coordinator)."""
        if cfg.meta is not None and not cfg.meta.trace_capture:
            return None
        from ..meta_harness.traces import trace_sink

        return trace_sink(
            self.store, self.scope, self.artifacts,
            lambda goal_id: self.service.plan_record(goal_id),
        )  # fmt: skip

    def _port(
        self,
        cfg: LocalConfig,
        inputs: CodexProfileInputs,
        root: Path,
        credential: ScopedCredential,
        token: str | None,
    ) -> Any:
        """The port of a configured cell whose model has its own driver record (§2.4)."""
        if inputs.provider == "codex":
            return build_codex_port(inputs, root / "journal", credential)
        if inputs.provider == "claude":
            if not token:
                raise Hold("AUTH_TOKEN_REQUIRED", "Claude cells need the claude token file")
            return build_claude_port(inputs, root / "journal", token)
        if cfg.opencode is None:
            raise Hold("CELL_UNKNOWN", "OpenCode cells need the opencode entry")
        return build_opencode_port(
            inputs, root / "journal", self.local(cfg.opencode.credential_home)
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
        try:  # a cancel that landed after the read above stays (PLAN_ENDED)
            self.service._save_plan(
                goal_id, {"goal_id": goal_id, "status": "planning"}, unless_ended=True
            )
        except Hold as exc:
            with self._planning_lock:
                self._planning.discard(goal_id)
            if exc.code != "PLAN_ENDED":
                raise
            return self.service.plan_record(goal_id)

        def run() -> None:
            try:
                self.service.plan(goal_id)
            except Exception as exc:
                code = str(getattr(exc, "code", type(exc).__name__))
                try:  # a goal cancelled while it was planned keeps its cancel (PLAN_ENDED)
                    self.service._save_plan(
                        goal_id,
                        {"goal_id": goal_id, "status": "plan_failed",
                         "reason": f"{code}: {exc}"[:600],
                         "details": str(getattr(exc, "details", ""))[:1500]},
                        ("planning.failed", {"code": code[:128]}),
                        unless_ended=True,
                    )  # fmt: skip
                except Hold as held:
                    if held.code != "PLAN_ENDED":
                        raise
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


def read_only_turns(planners_by_app: list[dict[str, Any]]) -> Callable[[str], Any]:
    """The ``StrategyRunner``'s turn factory (Work 033 S9, interfaces.md §3.6).

    A cell's read-only turn is its planner's (``CodexPlanner.turn`` / ``ClaudePlanner.turn``,
    ``readonly_turn.py``) in the first configured app that has the cell, so it runs in that app's
    qualified image with the cell's model and effort, on the read-only copy the runner mounts. A
    cell without a planner (OpenCode never plans, Work 031 O1) has no read-only turn: Hold
    TURN_FAILED, which the runner records like a failed turn."""
    turns: dict[str, Any] = {}
    for planners in planners_by_app:
        for cell_id, planner in planners.items():
            turn = getattr(planner, "turn", None)
            if turn is not None:
                turns.setdefault(cell_id, turn)

    def turn_of(cell_id: str) -> Any:
        found = turns.get(cell_id)
        if found is None:
            raise Hold(
                "TURN_FAILED", "No read-only turn is configured for this cell",
                details={"cell_id": cell_id},
            )  # fmt: skip
        return found

    return turn_of


def legacy_cell(driver_id: str, inputs: CodexProfileInputs, app_id: str) -> Cell:
    """The legacy cell of a configured driver entry (IC-07: cell id = driver id)."""
    return Cell(
        driver_id, driver_id, inputs.model, LEGACY_EFFORT,  # type: ignore[arg-type]
        ((app_id, str(inputs.qualification_report)),), legacy=True,
    )  # fmt: skip


def tool_versions(profile: Path) -> tuple[tuple[str, str], ...]:
    """The container profile's ``tools`` (deployment/*.json) for env_bootstrap (§3.3, D-096)."""
    tools = json.loads(profile.read_text()).get("tools") or {}
    if not isinstance(tools, dict):
        return ()
    return tuple((str(k), str(v)) for k, v in tools.items())


def cell_of(cfg: LocalConfig, cell_id: str) -> tuple[CellEntry | None, Cell]:
    """A configured cell by id: (its entry, the cell); a legacy cell has no entry."""
    legacy = cfg.legacy_cells()
    if cell_id in legacy:
        return None, Cell(
            cell_id, cell_id, legacy[cell_id], LEGACY_EFFORT, (), legacy=True,  # type: ignore[arg-type]
        )  # fmt: skip
    for entry in cfg.cells:
        cell = entry.cell()
        if cell.cell_id == cell_id:
            return entry, cell
    raise Hold("CELL_UNKNOWN", "No configured cell has this id", details=cell_id)


def probe_local_cell(
    config_path: Path,
    cell_id: str,
    *,
    app_id: str | None = None,
    environment_id: str | None = None,
    turn_factory: Any = None,
) -> dict[str, Any]:
    """``amplai ops local-cell probe``: one effort probe turn, stored as ``cell-effort-probe``.

    The turn runs read-only on an empty scratch directory (IC-14) in the app's qualified image
    with the cell's model and effort. It opens the store directly, so the local server must be
    stopped (the store has one owner). ``turn_factory(cell, inputs, scratch_root)`` replaces the
    real container turn in tests.

    One probe per cell id in the app image (§2.4): the latest probe decides the image the cell
    installs in; apps in other images skip the cell at boot (``LocalProductDeployment.cell_skips``).
    ``app_id`` picks the app, and so the image, to probe in; without it the first app with a report.

    ``environment_id`` (S7b) runs the turn in that task environment's image instead (its
    container profile and its report for the cell's driver, ``EnvironmentEntry``) and records
    the probe under (cell, environment); the app-image probe is untouched. ``app_id`` then picks
    the app whose environment it is; without it the first app with that environment and a report.
    Hold ENVIRONMENT_UNQUALIFIED when no such environment exists. Probes never run at boot.
    """
    from .contracts.identity import new_id
    from .execution.cells import PROBE_PROMPT, PROBE_SCHEMA, run_probe, store_probe
    from .execution.codex import measured_qualification
    from .execution.readonly_turn import ClaudeReadOnlyTurn, CodexReadOnlyTurn

    path = Path(config_path).expanduser().absolute()
    cfg = LocalConfig.model_validate_json(path.read_bytes())

    def local(value: str) -> Path:
        p = Path(value).expanduser()
        return (p if p.is_absolute() else path.parent / p).absolute()

    entry, cell = cell_of(cfg, cell_id)
    if entry is None or cell.effort == LEGACY_EFFORT:
        raise Hold("EFFORT_UNSUPPORTED", "Only a cell with an effort is probed")
    chosen: tuple[AppEntry, str, str] | None = None  # app, container profile, report
    for candidate in cfg.apps:
        if app_id not in (None, candidate.app_id):
            continue
        if environment_id is not None:
            env = next(
                (e for e in candidate.environments if e.environment_id == environment_id), None
            )
            env_report = env.qualification_reports.get(cell.driver_id) if env else None
            if env is not None and env_report is not None:
                chosen = (candidate, env.container_profile, env_report)
                break
            continue
        report = (
            (entry.qualification_reports or {}).get(candidate.app_id)
            if entry.qualification_reports is not None
            else candidate.driver_report(cell.driver_id)
        )
        if report is not None:
            chosen = (candidate, candidate.container_profile, report)
            break
    if chosen is None and environment_id is not None:
        raise Hold(
            "ENVIRONMENT_UNQUALIFIED",
            "No app has this task environment with a report for the cell's driver",
            details={"environment_id": environment_id, "driver_id": cell.driver_id},
        )
    if chosen is None:
        raise Hold("DRIVER_UNQUALIFIED", "No app has a qualification report for this cell")
    app, profile, report = chosen
    inputs = CodexProfileInputs(
        container_profile=local(profile),
        egress_profile=local(cfg.codex.egress_profile),
        egress_qualification=local(cfg.codex.egress_qualification),
        qualification_report=local(report),
        model=cell.model,
        provider=PROVIDERS[cell.driver_id],
    )
    measured = measured_qualification(inputs)  # Hold DRIVER_UNQUALIFIED: the model's report
    scratch_root = local(cfg.workspace_root) / "probes"
    turn: Any
    argv: list[str]
    if turn_factory is not None:
        turn = turn_factory(cell, inputs, scratch_root)
        argv = list(getattr(turn, "probe_argv", [cell.cell_id]))
    elif cell.driver_id == "codex-cli":
        codex_turn = CodexReadOnlyTurn(
            ContainerSandbox(container_profile(inputs)),
            ScopedCredential(local(cfg.codex.credential_home)), scratch_root / "runs",
            model=cell.model, effort=cell.effort, cell_id=cell.cell_id,
        )  # fmt: skip
        turn, argv = codex_turn, codex_turn.argv(PROBE_PROMPT, offline=True)
    else:
        if cfg.claude is None:
            raise Hold("AUTH_TOKEN_REQUIRED", "Claude cells need the claude token file")
        claude_turn = ClaudeReadOnlyTurn(
            ContainerSandbox(container_profile(inputs)), claude_token(local(cfg.claude.token_file)),
            scratch_root / "runs", model=cell.model, effort=cell.effort, cell_id=cell.cell_id,
        )  # fmt: skip
        turn, argv = claude_turn, claude_turn.argv(PROBE_PROMPT, PROBE_SCHEMA, offline=True)
    scope = Scope.parse(cfg.scope)
    # opened before the turn: a store another process owns fails here, not after a spent turn
    store = Store(local(cfg.runtime_root))
    try:
        scratch = scratch_root / new_id("probe")
        scratch.mkdir(parents=True, mode=0o700)
        value = run_probe(
            scope, cell, turn, scratch, driver_version=measured["driver_version"],
            image=measured["image"], argv=argv, environment_id=environment_id,
        )  # fmt: skip
        ref = store_probe(store, scope, value)
    finally:
        store.close()
    out = {
        "cell_id": cell.cell_id,
        "app": app.app_id,
        "outcome": value["outcome"],
        "probe_ref": ref,
        "next": "restart amplai ops local-serve" if value["outcome"] == "accepted" else None,
    }
    if environment_id is not None:
        out["environment_id"] = environment_id
        out["image"] = measured["image"]
    return out


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
