"""Work 018 — in-process end-to-end: approved goal → Codex-shaped CLI turn → verify → finish.

Real: CliDriver + SeededCodexPort (CliPort) through WorkCoordinator, the git workspace and
host-computed change, the patch verifier, operator approval, the execution loop and its
budgets. Stand-in: the "container" runs a script on the host that edits the workspace and
prints a Codex JSONL stream. This proves the wiring, not Codex or container conformance
(D-065); the real Codex runs are recorded under specs/018-v3-real-execution/runs/.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.agent_drivers.cli import CodexCliDriver
from amplai_foundry.agent_drivers.ports import DriverRegistry
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.codex import AUTH, SeededCodexPort
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.worker import WorkCoordinator
from rc06_rig import build_rig, git, submit

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


def rig_with_codex(deployment: Any, tmp_path: Path, mode: str) -> tuple[Any, Any, ScriptContainer]:
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

    def publisher(goal_id: str) -> dict[str, Any]:
        published.append(goal_id)
        return {"branch": "amplai/" + goal_id}

    loop = ExecutionLoop(rig.service, coordinator, publisher=publisher)
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


def test_approved_goal_runs_verifies_and_publishes(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    before = checkout(rig.repo)
    goal = approved(rig)
    record = loop.run_goal(goal)
    assert record["status"] == "published" and rig.published == [goal]
    assert [a["outcome"] for a in record["attempts"]] == ["pass"]
    d = rig.d
    assert d.store.head(d.scope, "goal", goal)["state"] == "verified"
    change = json.loads(d.artifacts.read(d.scope, record["attempts"][0]["change"]))
    patch = d.artifacts.read(d.scope, change["patch"])
    assert b"+    return 2" in patch
    # the operator's checkout, branches and main never moved; the token refresh came back
    assert checkout(rig.repo) == before
    assert (rig.home / AUTH).read_text() == '{"tokens": "refreshed"}'
    # every run workspace was discarded
    assert list(rig.workspaces.root.glob("run-*")) == []


def test_a_failed_acceptance_is_repaired_with_feedback(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "wrong-first")
    record = loop.run_goal(approved(rig))
    assert [a["outcome"] for a in record["attempts"]] == ["fail", "pass"]
    assert record["status"] == "published"
    first, second = container.prompts
    assert "did not pass" not in first
    assert "did not pass" in second and "AC-1" in second


def test_attempts_stop_at_the_design_budget(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "always-wrong")
    record = loop.run_goal(approved(rig))
    # first attempt included (runtime-defaults max_verifier_repair_attempts_including_initial)
    assert [a["outcome"] for a in record["attempts"]] == ["fail", "fail", "fail"]
    assert record["status"] == "failed" and rig.published == []


def test_a_driver_failure_stops_without_retry_or_publish(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "crash")
    before = checkout(rig.repo)
    goal = approved(rig)
    record = loop.run_goal(goal)
    assert record["status"] == "held" and len(record["attempts"]) == 1
    assert record["attempts"][0]["outcome"] == "driver_failed" and rig.published == []
    # the approval is revoked, so nothing can claim this goal again
    with pytest.raises(Hold):
        rig.d.runtime.claim(rig.d.worker, goal_id=goal)
    assert checkout(rig.repo) == before


def test_cancel_and_timeout_stop_before_any_attempt(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "right")
    goal = approved(rig)
    loop.cancel(rig.operator, goal)
    assert loop.run_goal(goal)["status"] == "cancelled" and container.prompts == []
    goal2 = submit(rig, "second goal for the timeout")
    rig.service.plan(goal2)
    rig.service.approve(rig.operator, goal2)
    loop.clock = lambda: 10.0**10  # far past approved_at + max_wall_seconds
    assert loop.run_goal(goal2)["status"] == "timed_out" and container.prompts == []


def test_the_background_loop_picks_up_approved_goals(deployment: Any, tmp_path: Path) -> None:
    import time

    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    loop.idle = 0.05
    goal = approved(rig)
    loop.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline and rig.service.plan_record(goal)["status"] != "published":
            time.sleep(0.1)
    finally:
        loop.stop()
    assert rig.service.plan_record(goal)["status"] == "published"
