"""Shared Work 018 test rig: the local execution product on a reference deployment.

Real pieces: store/runtime/goals/knowledge/verification/authority, GitWorkspaceManager on a
real git repo, the operator approval resolver, the compiler, PatchCommandVerifier.
Stand-ins (named as such): the planner returns a fixed draft; the verifier sandbox runs argv on
the host. Neither is counted as driver or container evidence (D-065).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from amplai_foundry.agent_drivers.cli import CodexCliDriver
from amplai_foundry.agent_drivers.ports import DriverRegistry
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.execution.codex import (
    AUTH,
    CodexProfileInputs,
    SeededCodexPort,
    install_codex_profile,
)
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.product import (
    Actors,
    AppConfig,
    LocalExecutionService,
    OperatorDecisions,
    VerifierCommand,
    app_capabilities,
)
from amplai_foundry.runtime.execution.worker import WorkCoordinator
from amplai_foundry.runtime.reference import PERMISSIONS
from amplai_foundry.sandbox.git_workspace import GitWorkspaceManager
from amplai_foundry.verification.runtime.patch_commands import (
    NonEmptyChangeCheck,
    SuiteVerifier,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE = "localhost:5000/amplai-worker-app-app@sha256:" + "c" * 64
CHECK = ("python3", "-c", "import app, sys; sys.exit(0 if app.value() == 2 else 1)")


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


def make_repo(root: Path) -> Path:
    repo = root / "app"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("def value():\n    return 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


class HostSandbox:
    """Runs argv on the host in the workspace. A protocol fixture, not a sandbox."""

    class profile:
        network = "none"

    def command(self, argv: list[str], workspace: Path, run_name: str, **_: Any) -> list[str]:
        script = (
            "import os,subprocess,sys;os.chdir(sys.argv[1]);sys.exit(subprocess.call(sys.argv[2:]))"
        )
        return [sys.executable, "-c", script, str(workspace), *argv]


DRAFT = {
    "summary": "Make value() return 2",
    "objective": "value() returns 2",
    "in_scope": ["app.py"],
    "non_goals": ["no other behaviour"],
    "constraints": ["keep the function name"],
    "acceptance": [{"statement": "value() returns 2", "verifier": "check"}],
    "risk": "low",
    "task_class": "logic_change",
    "assumptions": [],
    "questions": [],
}


class FixedPlanner:
    """Stand-in planner: returns a fixed draft and records what it was asked."""

    def __init__(self, draft: dict[str, Any] | None = None) -> None:
        self.draft_value = dict(draft or DRAFT)
        self.calls: list[dict[str, Any]] = []

    def draft(
        self, goal: str, app: str, verifiers: dict[str, str], workspace: Path
    ) -> dict[str, Any]:
        self.calls.append(
            {"goal": goal, "app": app, "verifiers": verifiers,
             "saw_base": (workspace / "app.py").read_text()}
        )  # fmt: skip
        return {"draft": json.loads(json.dumps(self.draft_value)), "usage": {"output_tokens": 1}}


@dataclass
class Rig:
    d: Any
    repo: Path
    service: LocalExecutionService
    workspaces: GitWorkspaceManager
    operator: Actor
    actors: Actors
    planner: FixedPlanner
    codex_refs: dict[str, dict[str, Any]]


def codex_inputs(root: Path) -> CodexProfileInputs:
    container = {"image": IMAGE, "uid": 65534, "gid": 65534, "memory": "2g", "cpus": 2.0,
                 "pids": 256, "tools": {"codex": "codex-cli 0.155.1"}}  # fmt: skip
    (root / "container.json").write_text(json.dumps(container))
    doc = {
        "container_image": IMAGE,
        "checked_at": "2026-09-28T00:00:00Z",
        "reports": {
            "codex-cli": {
                "status": "pass",
                "driver_version": "0.155.1",
                "model": "gpt-5.6-sol",
                "qualification_id": "qualification-codex-rig",
                "checks": [{"name": "exact_version", "outcome": "pass"}],
                "tool_use": {"outcome": "pass"},
            }
        },
    }
    (root / "qual.json").write_text(json.dumps(doc))
    return CodexProfileInputs(
        container_profile=root / "container.json",
        egress_profile=REPO_ROOT / "deployment" / "local-egress.json",
        egress_qualification=REPO_ROOT / "deployment" / "local-egress-qualification.json",
        qualification_report=root / "qual.json",
    )


def build_rig(
    d: Any, root: Path, planner: FixedPlanner | None = None, *, extra_verifier: bool = False
) -> Rig:
    repo = make_repo(root)
    codex_refs = install_codex_profile(
        d.store, d.scope, codex_inputs(root), app_capabilities("app")
    )
    d.authority.resolver = OperatorDecisions(d.store)
    workspaces = GitWorkspaceManager(root / "work", d.artifacts, {"app": repo})
    actors = Actors(
        service=Actor("amplai-service", d.scope, PERMISSIONS, "service", "local-service"),
        worker=d.worker,
        verifier=d.verifier,
    )
    operator = Actor("pinesky", d.scope, frozenset({"execution.approve", "runtime.read"}),
                     "human", "local-token")  # fmt: skip
    planner = planner or FixedPlanner()
    service = LocalExecutionService(
        store=d.store,
        runtime=d.runtime,
        goals=d.goals,
        knowledge=d.knowledge,
        authority=d.authority,
        verification=d.verification,
        workspaces=workspaces,
        planner=planner,
        actors=actors,
        codex_refs=codex_refs,
        verifier_factory=lambda app: SuiteVerifier(
            [(v.id, list(v.argv), v.timeout_seconds) for v in app.verifiers],
            workspaces=workspaces,
            scope=d.scope,
            sandbox=HostSandbox(),
        ),
        global_factory=lambda app: NonEmptyChangeCheck(workspaces, d.scope),
    )
    commands = [VerifierCommand("check", CHECK, "value() must return 2", 60)]
    if extra_verifier:
        commands.append(VerifierCommand("imports", ("python3", "-c", "import app"), "imports", 60))
    service.install(AppConfig("app", repo, tuple(commands)))
    return Rig(d, repo, service, workspaces, operator, actors, planner, codex_refs)


def submit(rig: Rig, text: str = "make value return 2") -> str:
    submitted = rig.d.goals.submit(rig.actors.service, text=text, key="k-" + text[:20])
    goal_id: str = submitted["goal_id"]
    return goal_id


def with_permissions(actor: Actor, *extra: str) -> Actor:
    return replace(actor, permissions=actor.permissions | frozenset(extra))


# Decides from the file it finds: base (1) → WRONG_FIRST ? 3 : 2; a repair copy (3) → 2.
AGENT = r"""
import json, pathlib, sys
ws, mode, home = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
assert (home / ".codex" / "auth.json").is_file(), "credential was not leased"
src = (ws / "app.py").read_text()
if mode == "crash":
    sys.exit(3)
if mode == "always-wrong":
    new = 3
elif mode == "wrong-first":
    new = 2 if "return 3" in src else 3
else:
    new = 2
(ws / "app.py").write_text("def value():\n    return %d\n" % new)
(home / ".codex" / "auth.json").write_text('{"tokens": "refreshed"}')
sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": "thread_" + mode}) + "\n")
sys.stdout.write(json.dumps({"type": "turn.completed",
                             "usage": {"input_tokens": 10, "output_tokens": 5}}) + "\n")
"""


class ScriptContainer:
    """Host-process stand-in for ContainerSandbox; never represented as a sandbox."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.driver: Any = None
        self.prompts: list[str] = []

    def command(self, argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        self.prompts.append(argv[-1])
        return [sys.executable, "-c", AGENT, str(workspace), self.mode, str(kw["native_home"])]

    def stop(self, name: str) -> None:
        p = self.driver.processes.get(name)
        if p and p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=5)

    def stopped(self, name: str) -> bool:
        p = self.driver.processes.get(name)
        return p is not None and p.poll() is not None

    def destroy(self, name: str) -> None:
        return None


def rig_with_codex(
    deployment: Any, tmp_path: Path, mode: str, publisher: Any = None
) -> tuple[Any, Any, ScriptContainer]:
    rig = build_rig(deployment, tmp_path)
    container = ScriptContainer(mode)
    driver = CodexCliDriver(
        "0.155.1",
        container,  # type: ignore[arg-type]  # host stand-in, see module docstring
        SessionJournal(tmp_path / "journal"),
        model="gpt-5.6-sol",
        qualified=True,
    )
    container.driver = driver
    home = tmp_path / "scoped-codex"
    (home / ".codex").mkdir(parents=True)
    (home / AUTH).write_text('{"tokens": "original"}')
    registry = DriverRegistry(deployment.store)
    registry.register(deployment.actor, rig.codex_refs["driver"], SeededCodexPort(driver, home))
    coordinator = WorkCoordinator(deployment.runtime, registry, rig.workspaces, poll_seconds=0.05)
    published: list[str] = []

    def recording(goal_id: str) -> dict[str, Any]:
        published.append(goal_id)
        return {"branch": "amplai/" + goal_id}

    loop = ExecutionLoop(rig.service, coordinator, publisher=publisher or recording)
    rig.published = published  # type: ignore[attr-defined]
    rig.home = home  # type: ignore[attr-defined]
    return rig, loop, container


def approved(rig: Any) -> str:
    goal = submit(rig)
    rig.service.plan(goal)
    rig.service.approve(rig.operator, goal)
    return goal


def checkout(repo: Path) -> tuple[str, str, str]:
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "status", "--porcelain"),
        git(repo, "branch", "--list"),
    )
