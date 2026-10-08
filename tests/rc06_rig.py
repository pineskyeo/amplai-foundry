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

from amplai_foundry.agent_drivers.cli import ClaudeCodeDriver, CodexCliDriver
from amplai_foundry.agent_drivers.ports import DriverRegistry
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.execution.codex import (
    AUTH,
    CodexProfileInputs,
    OptionsCliPort,
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
        self.multi_value: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []

    def draft_multi(
        self, goal: str, apps: dict[str, dict[str, str]], workspaces: dict[str, Path]
    ) -> dict[str, Any]:
        self.calls.append(
            {"goal": goal, "apps": apps, "mode": "work",
             "saw_bases": {a: sorted(p.name for p in w.iterdir()) for a, w in workspaces.items()}}
        )  # fmt: skip
        return {"draft": json.loads(json.dumps(self.multi_value)), "usage": {"output_tokens": 1}}

    def draft(
        self, goal: str, app: str, verifiers: dict[str, str], workspace: Path, *, mode: str = "work"
    ) -> dict[str, Any]:
        self.calls.append(
            {"goal": goal, "app": app, "verifiers": verifiers, "mode": mode,
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


def codex_inputs(root: Path, *, enabled: bool = True) -> CodexProfileInputs:
    container = {"image": IMAGE, "uid": 65534, "gid": 65534, "memory": "2g", "cpus": 2.0,
                 "pids": 256, "tools": {"codex": "codex-cli 0.155.1",
                                        "claude": "2.1.278 (Claude Code)"}}  # fmt: skip
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
            },
            "claude-cli": {
                "status": "pass",
                "driver_version": "2.1.278",
                "model": "claude-sonnet-5",
                "qualification_id": "qualification-claude-rig",
                "checks": [{"name": "exact_version", "outcome": "pass"}],
                "tool_use": {"outcome": "pass"},
            },
        },
    }
    (root / "qual.json").write_text(json.dumps(doc))
    return CodexProfileInputs(
        container_profile=root / "container.json",
        egress_profile=REPO_ROOT / "deployment" / "local-egress.json",
        egress_qualification=REPO_ROOT / "deployment" / "local-egress-qualification.json",
        qualification_report=root / "qual.json",
        enabled=enabled,
    )


def claude_inputs(root: Path, *, enabled: bool = True) -> CodexProfileInputs:
    return replace(codex_inputs(root), provider="claude", model="claude-sonnet-5", enabled=enabled)


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
        design_min_sources=2,  # the rig repo has one two-line file to cite
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
prompt = sys.argv[4] if len(sys.argv) > 4 else ""
assert (home / ".codex" / "auth.json").is_file(), "credential was not leased"
if (mode == "steer" and "Operator steering" not in prompt) or (
    mode == "steer-twice" and "SECOND" not in prompt
):
    # a long first turn: bound session, then working until the operator's pause stops it
    import time
    sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": "thread_steer"}) + "\n")
    sys.stdout.flush()
    time.sleep(60)
    sys.exit(0)
src = (ws / "app.py").read_text() if (ws / "app.py").exists() else ""
if mode == "crash":
    sys.exit(3)
if mode.startswith("multi"):
    # two apps: the producer makes value() return 2; the consumer renders LABEL=value
    if (ws / "app.py").exists():
        (ws / "app.py").write_text("def value():\n    return 2\n")
    if (ws / "report.py").exists():
        sep = ":" if mode == "multi-bad" else "="
        (ws / "report.py").write_text(
            'LABEL = "value"\n\n\ndef render(v):\n    return LABEL + "%s" + str(v)\n' % sep
        )
elif mode.startswith("design"):
    doc = ws / "specs" / "design" / "g1" / "design.md"
    thin = mode == "design-thin-first" and not doc.exists()
    doc.parent.mkdir(parents=True, exist_ok=True)
    sources = "" if thin else "- app.py:1 defines value()\n- app.py:2 returns the constant\n"
    doc.write_text("# Design\n\n## Goal\nx\n## Current State\nvalue() in app.py:1\n"
                   "## Options\nx\n## Decision\nx\n## Risks\nx\n## Implementation Plan\nx\n"
                   "## Sources\n" + sources)
    if mode == "design-code":
        (ws / "app.py").write_text("def value():\n    return 2\n")
elif mode == "always-wrong":
    (ws / "app.py").write_text("def value():\n    return 3\n")
else:
    new = 2 if mode != "wrong-first" or "return 3" in src else 3
    (ws / "app.py").write_text("def value():\n    return %d\n" % new)
(home / ".codex" / "auth.json").write_text('{"tokens": "refreshed"}')
sid = "thread_steer" if mode.startswith("steer") else "thread_" + mode  # one session
sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": sid}) + "\n")
sys.stdout.write(json.dumps({"type": "turn.completed",
                             "usage": {"input_tokens": 10, "output_tokens": 5}}) + "\n")
"""


# Claude stream-json dialect of AGENT (same file decisions, no credential file: token by env).
CLAUDE_AGENT = r"""
import json, os, pathlib, sys
ws, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), "token was not passed by env"
if mode == "crash":
    sys.exit(3)
new = 3 if mode == "always-wrong" else 2
(ws / "app.py").write_text("def value():\n    return %d\n" % new)
sid = "sess_claude_" + mode
sys.stdout.write(json.dumps({"type": "system", "subtype": "init", "session_id": sid}) + "\n")
sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                             "session_id": sid, "result": "DONE",
                             "usage": {"input_tokens": 10, "output_tokens": 5}}) + "\n")
"""


class ScriptContainer:
    """Host-process stand-in for ContainerSandbox; never represented as a sandbox."""

    def __init__(self, mode: str, dialect: str = "codex") -> None:
        self.mode, self.dialect = mode, dialect
        self.driver: Any = None
        self.prompts: list[str] = []

    def command(self, argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        if self.dialect == "claude":
            self.prompts.append(argv[argv.index("-p") + 1])
            return [sys.executable, "-c", CLAUDE_AGENT, str(workspace), self.mode]
        self.prompts.append(argv[-1])
        return [
            sys.executable, "-c", AGENT, str(workspace), self.mode, str(kw["native_home"]),
            argv[-1],
        ]  # fmt: skip

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


def rig_with_two_drivers(
    deployment: Any, tmp_path: Path, *, codex_enabled: bool = True
) -> tuple[Any, Any, ScriptContainer, ScriptContainer, FixedPlanner]:
    """Codex and Claude both qualified (stand-in containers); Codex optionally disabled."""
    rig = build_rig(deployment, tmp_path)
    d = deployment
    codex_refs = install_codex_profile(
        d.store, d.scope, codex_inputs(tmp_path, enabled=codex_enabled), app_capabilities("app")
    )
    claude_refs = install_codex_profile(
        d.store, d.scope, claude_inputs(tmp_path), app_capabilities("app")
    )
    claude_planner = FixedPlanner()
    service = rig.service
    service.codex = codex_refs
    service.drivers = {"codex-cli": codex_refs, "claude-cli": claude_refs}
    service.planners = {"claude-cli": claude_planner}
    service.install(AppConfig("app", rig.repo, service.apps["app"].config.verifiers))
    codex_box = ScriptContainer("right")
    claude_box = ScriptContainer("right", dialect="claude")
    journal = SessionJournal(tmp_path / "journal")
    codex = CodexCliDriver("0.155.1", codex_box, journal, model="gpt-5.6-sol", qualified=True)  # type: ignore[arg-type]
    claude = ClaudeCodeDriver(
        "2.1.278", claude_box, journal, model="claude-sonnet-5", qualified=True,  # type: ignore[arg-type]
        environment={"CLAUDE_CODE_OAUTH_TOKEN": "test-token-not-a-secret"}, auth="oauth_token",
    )  # fmt: skip
    codex_box.driver, claude_box.driver = codex, claude
    home = tmp_path / "scoped-codex"
    (home / ".codex").mkdir(parents=True)
    (home / AUTH).write_text('{"tokens": "original"}')
    registry = DriverRegistry(d.store)
    registry.register(d.actor, codex_refs["driver"], SeededCodexPort(codex, home))
    # the production Claude port (build_claude_port): it takes the dispatch options, so a trial
    # reaches the driver with its web tools off (operator decision 2026-10-08)
    registry.register(d.actor, claude_refs["driver"], OptionsCliPort(claude))
    coordinator = WorkCoordinator(d.runtime, registry, rig.workspaces, poll_seconds=0.05)
    published: list[str] = []

    def recording(goal_id: str) -> dict[str, Any]:
        published.append(goal_id)
        return {"branch": "amplai/" + goal_id}

    loop = ExecutionLoop(service, coordinator, publisher=recording)
    rig.published = published  # type: ignore[attr-defined]
    return rig, loop, codex_box, claude_box, claude_planner


# Consumer app of the two-app rig and its cross-app integration command (D-081).
CCHECK = ("python3", "-c", "import report, sys; sys.exit(0 if report.LABEL == 'value' else 1)")
INTEGRATION = [
    "python3", "-c",
    "import sys; sys.path[:0] = ['/amplai-input/apps/app', '/amplai-input/apps/consumer']; "
    "import app, report; sys.exit(0 if report.render(app.value()) == 'value=2' else 1)",
]  # fmt: skip
MULTI_DRAFT = {
    "summary": "value() returns 2 and the consumer renders it",
    "objective": "value() returns 2; report renders LABEL=value",
    "non_goals": ["no other behaviour"],
    "constraints": ["keep the function names"],
    "work_items": [
        {"app": "app", "objective": "value() returns 2", "in_scope": ["app.py"],
         "acceptance": [{"statement": "value() returns 2", "verifier": "check"}], "after": []},
        {"app": "consumer", "objective": "report renders the value", "in_scope": ["report.py"],
         "acceptance": [{"statement": "LABEL is value", "verifier": "ccheck"}],
         "after": ["app"]},
    ],
    "risk": "low",
    "task_class": "new_feature",
    "assumptions": [],
    "questions": [],
}  # fmt: skip


class MountSandbox(HostSandbox):
    """Host stand-in that resolves /amplai-input/apps/<app> mounts to their host copies."""

    def command(self, argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        mounts = kw.get("readonly_mounts") or {}
        resolved = []
        for arg in argv:
            for target, source in mounts.items():
                arg = arg.replace(target, str(source))
            resolved.append(arg)
        return super().command(resolved, workspace, run_name)


def rig_two_apps(
    deployment: Any, tmp_path: Path, mode: str = "multi", publisher: Any = None
) -> tuple[Any, Any, ScriptContainer, Path]:
    from amplai_foundry.verification.runtime.integration import IntegrationCheck

    rig, loop, container = rig_with_codex(deployment, tmp_path, mode, publisher=publisher)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    git(consumer, "init", "-q", "-b", "main")
    (consumer / "report.py").write_text('LABEL = "old"\n')
    git(consumer, "add", "-A")
    git(consumer, "commit", "-q", "-m", "base")
    rig.workspaces.repos["consumer"] = consumer
    # both apps run in the rig's one image: its profile covers both (as local_deployment does)
    d = deployment
    shared = install_codex_profile(
        d.store, d.scope, codex_inputs(tmp_path),
        app_capabilities("app") + app_capabilities("consumer"),
    )  # fmt: skip
    rig.service.codex = shared
    rig.service.drivers = {"codex-cli": shared}
    loop.coordinator.registry.register(
        d.actor, shared["driver"], SeededCodexPort(container.driver, rig.home)
    )
    rig.service.install(AppConfig("app", rig.repo, rig.service.apps["app"].config.verifiers))
    rig.service.integration_factory = lambda apps: IntegrationCheck(
        [("both", INTEGRATION, 60, ["app", "consumer"])],
        workspaces=rig.workspaces, scope=deployment.scope, sandbox=MountSandbox(),
    )  # fmt: skip
    rig.service.install(
        AppConfig("consumer", consumer, (VerifierCommand("ccheck", CCHECK, "LABEL is value", 60),))
    )
    rig.planner.multi_value = json.loads(json.dumps(MULTI_DRAFT))
    return rig, loop, container, consumer
