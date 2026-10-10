"""Work 033 S9b: §14 Q16 answered with fake-port tests; M3 follow-up recovery and M4 candidates.

Contract: specs/033-harness-taxonomy/interfaces.md §5.1 M3 rule 4 and the M4 rules, §5.2 strategy 10
(`vote`), §14 Q16 (a) and (b), "Clarifications After S9 And S11" (S9b).

Real: store, runtime, worker coordinator (``WorkCoordinator.execute``, ``stop_and_snapshot``,
``abort``), the Codex-shaped CLI port with its credential seeding (``SeededCodexPort``), the driver
journal, the git workspace manager, the lease, the execution envelope and the steering service.
Stand-in: the "container" is the host-process script of the rc06 rig (the follow-up and candidate
turns write the files a per-call script asks for) and the hooks (``Hooks`` / ``Vote``). No docker,
provider, network or credential.

The Q16 answer lives in ``contract_notes`` (file:line evidence, checked against the source by
``test_the_contract_notes_cite_real_lines``). The remaining tests pin it:

(a) a crash inside a follow-up whose ``<dispatch_id>-f<k>`` journal exists: resumed or re-sent
    exactly once by the request digest, never a duplicate effect; held, with the reason, when the
    recovery is unsafe (``ORPHAN_SESSION``, ``FOLLOWUP_RECOVERY``, ``EXECUTION_RECONCILE``,
    ``EXECUTION_REPLAY``).
(b) candidates (M4): one after another from the same base, candidate sessions never bound (no
    ``SessionStore`` row, no ``runtime.start``), a pause during candidate i stops it and is taken
    over on the bound session, abort and a failed attempt stop every candidate process, the vote is
    not resumable after a winner other than 0 (``VOTE_SELECTED``), the selected patch reaches the
    run workspace before ``output_ready`` (``VOTE_APPLY`` when it does not), 80 % of the node
    budget stops generating, ``WORKER_HOOKS`` for a bad hook.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.execution.strategy_runner import CandidateResult
from amplai_foundry.runtime.execution.worker import (
    CANDIDATE_BUDGET,
    MAX_CANDIDATES,
    MAX_FOLLOWUPS,
)
from amplai_foundry.sandbox.git_workspace import PATCH_BINDING
from rc06_rig import rig_with_codex, submit, with_permissions

ROOT = Path(__file__).resolve().parents[2]
PORT = "change"
BASE = "def value():\n    return 1\n"
GOOD = "def value():\n    return 2\n"
WRONG = "def value():\n    return 3\n"
W = "src/amplai_foundry/runtime/execution/worker.py"
CLI = "src/amplai_foundry/agent_drivers/cli.py"

# ---------------------------------------------------------------------------------------------
# The Q16 answer (file:line evidence; every line is checked by the first test)
# ---------------------------------------------------------------------------------------------
contract_notes: dict[str, Any] = {
    "a": {
        "question": "Recovery of a worker execution interrupted inside a follow-up turn "
        "(<dispatch_id>-f<k> journal exists).",
        "answer": (
            "Recoverable. The follow-up is recorded in the head (followups, followup_dispatch_id) "
            "before the resume, so a replay of execute with the same request digest finds an "
            "'observing' head whose last follow-up is in flight (_in_followup) and recovers it "
            "instead of EXECUTION_RECONCILE. The driver journal of <dispatch_id>-f<k> decides: no "
            "journal (SESSION_NOT_FOUND) or 'prepared' means no native turn ran (CliDriver.start "
            "persists 'starting' before it spawns), so the kept message is sent once under the "
            "same id (prepare is idempotent for the same bytes); 'completed' is collected; a "
            "running process this port owns is observed to its end. Held with the reason: a "
            "process this port does not own (ORPHAN_SESSION), a turn that ended without "
            "completing, a message that was not kept, a run that is no longer running "
            "(FOLLOWUP_RECOVERY). Any other interrupted execution (first turn, vote candidates) "
            "stays EXECUTION_RECONCILE."
        ),
        "evidence": [
            (W, 408, "def _existing"),
            (W, 431, "def _in_followup"),
            (W, 607, "followup_dispatch_id=follow"),
            (W, 862, "def _kept_message"),
            (W, 897, "def _recover"),
            (W, 957, "port.poll(follow_id)"),
            (W, 969, 'journal in (None, "prepared")'),
            (CLI, 376, "ORPHAN_SESSION"),
            (CLI, 380, '"prepared"}, "starting"'),
            (CLI, 530, "ORPHAN_SESSION"),
        ],
    },
    "b": {
        "question": "How SessionStore.prepare/bind, runtime.start, steering pause/resume and "
        "effect reconciliation treat candidate sessions that are not bound to the run.",
        "answer": (
            "Candidates 1..k-1 are dispatches <dispatch_id>-c<i> with their own native sessions "
            "and scratch workspaces. The run alone is prepared and bound: SessionStore.prepare "
            "and bind run once, for the run's dispatch id, and runtime.start runs once for "
            "candidate 0's session (a candidate dispatch id has no claim row, so runtime.start "
            "would refuse it, DISPATCH_BINDING). A candidate has no driver-session row and the "
            "native broker accepts only the run's bound session (TOOL_SESSION), so it can raise "
            "no effect; effects stay keyed by the run. A candidate is never resumed: collected, "
            "its trace kept and destroyed (credential released) before the next starts. The run's "
            "driver_handle stays candidate 0's. A steering pause during candidate i stops "
            "candidate i (it is listed in the head before anything is prepared; "
            "_stop_candidates), then stop_and_snapshot pauses and checkpoints candidate 0's "
            "bound session with the run workspace as candidate 0 left it (so the steering "
            "checkpoint names the bound session, CHECKPOINT_SESSION passes) and the vote is "
            "dropped; abort and a failed attempt stop every candidate process too. After a "
            "winner other than 0 the run workspace no longer matches the bound session, so no "
            "pause is taken over (VOTE_SELECTED)."
        ),
        "evidence": [
            (W, 1095, "def _candidate("),
            (W, 1052, "def _stop_candidates"),
            (W, 1228, "def _apply_candidate"),
            (W, 1488, "self.sessions.prepare("),
            (W, 1519, "self.sessions.bind("),
            (W, 1520, "self.runtime.start("),
            (W, 1566, "def stop_and_snapshot"),
            (W, 1607, "VOTE_SELECTED"),
            (W, 1644, "def abort"),
            ("src/amplai_foundry/runtime/execution/service.py", 682, "DISPATCH_BINDING"),
            ("src/amplai_foundry/tool_broker/native.py", 88, "session_handle"),
            ("src/amplai_foundry/runtime/execution/steering.py", 208, "session_handle"),
        ],
    },
}


def test_the_contract_notes_cite_real_lines() -> None:
    assert set(contract_notes) == {"a", "b"}
    for part, note in contract_notes.items():
        assert note["answer"] and note["question"]
        assert len(note["evidence"]) >= 8, part
        for relpath, line, anchor in note["evidence"]:
            lines = (ROOT / relpath).read_text().splitlines()
            assert 1 <= line <= len(lines), (relpath, line)
            assert anchor in lines[line - 1], (relpath, line, anchor, lines[line - 1])


# ---------------------------------------------------------------------------------------------
# scaffolding: the scripted agent, hooks, setup
# ---------------------------------------------------------------------------------------------
AGENT = r"""
import json, pathlib, sys, time
ws, spec, home = pathlib.Path(sys.argv[1]), json.loads(sys.argv[2]), pathlib.Path(sys.argv[3])
with open(spec["log"], "a") as log:  # a line per spawned process (prepare is not a spawn)
    log.write(spec["name"] + "\n")
assert (home / ".codex" / "auth.json").is_file(), "credential was not leased"
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
if spec.get("sleep"):
    emit({"type": "thread.started", "thread_id": spec["session"]})
    time.sleep(spec["sleep"])
if spec.get("exit"):
    emit({"type": "thread.started", "thread_id": spec["session"]})
    sys.exit(spec["exit"])
for rel, text in spec["writes"].items():
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
(home / ".codex" / "auth.json").write_text('{"tokens": "refreshed"}')
emit({"type": "thread.started", "thread_id": spec["session"]})
done = {"type": "turn.completed"}
if spec["usage"] is not None:
    done["usage"] = {"input_tokens": spec["usage"][0], "output_tokens": spec["usage"][1]}
emit(done)
"""


class Turns:
    """The scripted agent: one process per turn. ``plan`` is keyed by the turn's role: ``run``
    (the first turn, candidate 0), ``f1``/``f2`` (follow-ups), ``c1``/``c2`` (candidates)."""

    def __init__(self, container: Any, log: Path) -> None:
        self.container = container
        self.log = log
        self.calls: list[dict[str, Any]] = []  # one per ``command`` (every prepare)
        self.plan: dict[str, dict[str, Any]] = {}
        container.command = self.command

    @staticmethod
    def role(name: str) -> str:
        tail = name.rsplit("-", 1)[-1]
        return tail if tail in {"f1", "f2", "c1", "c2"} else "run"

    def names(self) -> list[str]:
        """The processes that were actually spawned, in order (not the prepares)."""
        end = time.monotonic() + 5
        while True:  # the script logs as it starts: wait for every process the driver spawned
            names = self.log.read_text().split() if self.log.exists() else []
            if len(names) >= len(self.container.driver.processes) or time.monotonic() > end:
                return names
            time.sleep(0.02)

    def command(self, argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        resume = len(argv) > 5 and argv[4] == "resume"
        role = self.role(run_name)
        home = Path(kw["native_home"])
        app = workspace / "app.py"
        self.calls.append(
            {"name": run_name, "role": role, "resume": resume, "prompt": argv[-1],
             "workspace": workspace, "base_at_start": app.read_text() if app.exists() else None,
             "session": argv[5] if resume else None, "home": home,
             "auth_at_start": (home / AUTH).is_file()}
        )  # fmt: skip
        spec = {
            "writes": {"app.py": GOOD},
            "session": argv[5] if resume else "thread_" + run_name,
            "usage": [10, 5],
            "name": run_name,
            "log": str(self.log),
            **self.plan.get(role, {}),
        }
        return [sys.executable, "-c", AGENT, str(workspace), json.dumps(spec), str(home)]


class Hooks:
    """M3 ``TurnHooks``: ``messages[k]`` is the follow-up message after turn k (None ends)."""

    candidates = 1

    def __init__(self, messages: list[str | None], *, followups: int | None = None) -> None:
        self.messages = list(messages)
        self.max_followups = len(messages) if followups is None else followups
        self.seen: list[int] = []

    def spec_digest(self) -> str:
        return digest({"hooks": "s9b-test", "messages": self.messages})

    def after_turn(self, *, workspace: Path, turn: int, receipt: dict[str, Any]) -> str | None:
        self.seen.append(turn)
        return self.messages[turn] if turn < len(self.messages) else None

    def select(self, candidates: list[CandidateResult]) -> int:
        return 0


def size(workspace: Path) -> int:
    """The diff stand-in: bytes of every file of the workspace (the base is one file)."""
    return sum(len(p.read_text()) for p in workspace.iterdir() if p.is_file())


class Vote:
    """M4 hooks. ``evaluate`` is the host fast check (here: ``return 2`` in app.py); ``select``
    follows §5.2: most passing checks, then the smallest diff, then the lowest index."""

    max_followups = 0

    def __init__(self, k: int, *, tag: str = "s9b") -> None:
        self.candidates, self.tag = k, tag
        self.log: list[Any] = []  # ("eval", index, ...) in the order the worker asked
        self.on_evaluate: Any = None
        self.auth_at_eval: list[list[Path]] = []
        self.auth_files: Any = lambda: []
        self.abandoned = False
        self.selected: int | None = None

    def spec_digest(self) -> str:
        return digest({"hooks": self.tag, "k": self.candidates})

    def after_turn(self, *, workspace: Path, turn: int, receipt: dict[str, Any]) -> str | None:
        return None

    def evaluate(self, *, workspace: Path, index: int, receipt: dict[str, Any]) -> CandidateResult:
        self.log.append(("eval", index))
        self.auth_at_eval.append(self.auth_files())
        if self.on_evaluate is not None:
            self.on_evaluate(index, workspace)
        passed = "return 2" in (workspace / "app.py").read_text()
        return CandidateResult(index, size(workspace), {"check": passed}, receipt.get("usage"))

    def select(self, candidates: list[CandidateResult]) -> int:
        best = min(
            candidates,
            key=lambda c: (-sum(1 for ok in c.fast_checks.values() if ok), c.patch_lines, c.index),
        )
        self.selected = best.index
        return best.index

    def abandon(self) -> None:
        self.abandoned = True


class Crash(BaseException):
    """What a dead process leaves: no handler of ``execute`` (``except Exception``) runs."""


class Setup:
    def __init__(self, deployment: Any, tmp_path: Path) -> None:
        self.rig, self.loop, self.container = rig_with_codex(deployment, tmp_path, "right")
        self.d = self.rig.d
        self.turns = Turns(self.container, tmp_path / "spawned.log")
        self.coordinator = self.loop.coordinator
        self.driver = self.container.driver
        self.worker = self.rig.actors.worker
        self.count = 0
        self.goals: list[str] = []
        self.goal = ""
        self.cleanup: list[Any] = []

    def claimed(self) -> tuple[dict[str, Any], dict[str, Any]]:
        for earlier in self.goals:
            self.d.runtime.end_goal(
                self.rig.actors.service, earlier, outcome="cancelled", reason="next test goal"
            )
        self.goals.clear()
        self.count += 1
        goal = submit(self.rig, f"({self.count}) make value return 2")
        self.goals.append(goal)
        self.goal = goal
        plan = self.rig.service.plan(goal)
        self.rig.service.approve(self.rig.operator, goal)
        dispatch = self.d.runtime.claim(self.worker, goal_id=goal)
        assert dispatch is not None
        return dispatch, plan["base"]

    def execute(self, dispatch: dict[str, Any], base: dict[str, Any], hooks: Any, **kw: Any) -> Any:
        return self.coordinator.execute(
            self.worker, dispatch, prompt="Make value() return 2.", base_snapshot=base,
            output_paths={PORT: PATCH_BINDING}, hooks=hooks, **kw,
        )  # fmt: skip

    def port(self, dispatch: dict[str, Any]) -> Any:
        return self.coordinator.registry.resolve(
            self.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
        )

    def head(self, did: str) -> dict[str, Any]:
        head: dict[str, Any] = self.d.store.head(self.d.scope, "worker-execution", did)
        return head

    def run(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        run: dict[str, Any] = self.d.store.head(self.d.scope, "run", dispatch["run_id"])
        return run

    def auth_files(self) -> list[Path]:
        return sorted(self.driver.native_root.glob("*/" + str(AUTH)))

    def journal(self, handle: str) -> dict[str, Any]:
        record: dict[str, Any] = self.driver.journal.read(handle)
        return record

    def wait_journal(self, handle: str, states: set[str], seconds: float = 20.0) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                if self.journal(handle)["state"] in states:
                    return
            except RuntimeFault:
                pass
            time.sleep(0.05)
        raise AssertionError(f"{handle} never reached {states}")

    def change_patch(self, dispatch: dict[str, Any]) -> str:
        work = self.d.store.head(self.d.scope, "work", dispatch["node"]["work_id"])
        change = json.loads(self.d.artifacts.read(self.d.scope, work["data"]["outputs"][PORT]))
        return str(self.d.artifacts.read(self.d.scope, change["patch"]).decode())

    def reported(self, dispatch: dict[str, Any]) -> bool:
        """The run reached ``output_ready`` (its state is then ``verifying``)."""
        return self.run(dispatch)["state"] not in {"running", "pause_requested", "paused"}

    def run_usage(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        usage: dict[str, Any] = self.run(dispatch)["data"]["record"]["usage"]
        return usage


@pytest.fixture
def s(deployment: Any, tmp_path: Path) -> Any:
    setup = Setup(deployment, tmp_path)
    yield setup
    for stop in setup.cleanup:
        with contextlib.suppress(Exception):
            stop()


def crash_after_resume(port: Any) -> Any:
    """The follow-up process is started, then the worker dies before it records anything more."""
    original = port.resume

    def resume_then_die(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise Crash

    port.resume = resume_then_die
    return original


def crash_before_resume(port: Any) -> Any:
    """The worker dies after the follow-up was recorded and the turn checkpointed, before the
    resume prepared anything: the follow-up has no driver journal."""
    original = port.resume

    def die(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    port.resume = die
    return original


def kill_process(s: Setup, handle: str) -> None:
    proc = s.driver.processes.get(handle)
    if proc is not None and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


# =========================================================================================
# (a) M3 recovery: a crash inside a follow-up turn
# =========================================================================================
def interrupted(s: Setup, how: str, hooks: Hooks, **plan: Any) -> tuple[dict, dict, Any]:
    """Run ``execute`` until the worker 'dies' inside follow-up 1; returns (dispatch, base, port
    original resume)."""
    dispatch, base = s.claimed()
    port = s.port(dispatch)
    s.turns.plan["f1"] = plan
    restore = {"after": crash_after_resume, "before": crash_before_resume}[how](port)
    with pytest.raises(Crash):
        s.execute(dispatch, base, hooks)
    port.resume = restore
    return dispatch, base, restore


def test_a_follow_up_still_running_is_observed_to_its_end_and_sent_once(s: Setup) -> None:
    hooks = Hooks(["add a note"])
    dispatch, base, _ = interrupted(
        s, "after", hooks, sleep=1.0, writes={"app.py": GOOD, "notes.txt": "after review\n"}
    )
    did = dispatch["dispatch_id"]
    head = s.head(did)
    assert head["state"] == "observing" and head["data"]["followup_dispatch_id"] == did + "-f1"
    assert s.journal(did + "-f1")["state"] in {"starting", "running"}
    sent = s.turns.names()
    assert sent == [did, did + "-f1"]
    result = s.execute(dispatch, base, Hooks(["add a note"]))
    assert result["status"] == "verifying"
    # recovered, not sent again: still one process per turn, one effect
    assert s.turns.names() == sent
    done = s.head(did)
    assert done["state"] == "verifying"
    (entry,) = done["data"]["followups"]
    assert entry["dispatch_id"] == did + "-f1" and entry["prompt_digest"] == digest("add a note")
    assert entry["receipt_digest"].startswith("sha256:")
    (recovery,) = done["data"]["recoveries"]
    assert (recovery["dispatch_id"], recovery["action"]) == (did + "-f1", "observed")
    patch = s.change_patch(dispatch)
    assert patch.count("notes.txt") >= 1 and "return 2" in patch  # the follow-up's effect, once
    run = s.run(dispatch)
    assert run["state"] == "verifying"  # output_ready reported, the suite is next
    assert s.auth_files() == []  # no credential at rest after the recovered attempt
    # replay of the finished execution returns its stored result (no recovery again)
    assert s.execute(dispatch, base, Hooks(["add a note"])) == result
    assert s.turns.names() == sent


def test_a_completed_follow_up_is_collected_not_run_again(s: Setup) -> None:
    dispatch, base, _ = interrupted(s, "after", Hooks(["more"]))
    did = dispatch["dispatch_id"]
    s.wait_journal(did + "-f1", {"completed"})
    sent = s.turns.names()
    result = s.execute(dispatch, base, Hooks(["more"]))
    assert result["status"] == "verifying" and s.turns.names() == sent
    (recovery,) = s.head(did)["data"]["recoveries"]
    assert recovery["action"] == "collected" and recovery["journal"] == "completed"
    assert s.head(did)["data"]["followups"][0]["receipt_digest"] is not None


def test_a_follow_up_that_never_prepared_is_re_sent_once_with_the_kept_message(s: Setup) -> None:
    dispatch, base, _ = interrupted(s, "before", Hooks(["fix the thing"]))
    did = dispatch["dispatch_id"]
    # the resume never prepared the follow-up: no driver journal of <dispatch_id>-f1
    with pytest.raises(RuntimeFault) as no_journal:
        s.journal(did + "-f1")
    assert no_journal.value.code == "SESSION_NOT_FOUND"
    assert s.turns.names() == [did]
    result = s.execute(dispatch, base, Hooks(["fix the thing"]))
    assert result["status"] == "verifying"
    assert s.turns.names() == [did, did + "-f1"]  # sent exactly once, under the same id
    assert [c["prompt"] for c in s.turns.calls] == ["Make value() return 2.", "fix the thing"]
    assert s.turns.calls[1]["resume"] is True and s.turns.calls[1]["session"] == "thread_" + did
    (recovery,) = s.head(did)["data"]["recoveries"]
    assert (recovery["action"], recovery["journal"]) == ("resent", None)
    assert s.auth_files() == []


def test_a_follow_up_that_was_prepared_but_not_started_is_started_once(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    original_start, state = s.driver.start, {"armed": True}

    def die_at_first_resume_start(prepared: dict[str, Any]) -> str:
        if state["armed"] and prepared["dispatch_id"].endswith("-f1"):
            raise Crash  # prepare persisted 'prepared'; the start never ran
        return original_start(prepared)

    s.driver.start = die_at_first_resume_start
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks(["more"]))
    state["armed"] = False
    assert s.journal(did + "-f1")["state"] == "prepared" and s.turns.names() == [did]
    result = s.execute(dispatch, base, Hooks(["more"]))
    assert result["status"] == "verifying" and s.turns.names() == [did, did + "-f1"]
    (recovery,) = s.head(did)["data"]["recoveries"]
    assert (recovery["action"], recovery["journal"]) == ("resent", "prepared")


def test_a_second_crash_during_the_re_sent_follow_up_is_observed_not_sent_a_third_time(
    s: Setup,
) -> None:
    dispatch, base, _ = interrupted(s, "before", Hooks(["more"]))
    did = dispatch["dispatch_id"]
    port = s.port(dispatch)
    s.turns.plan["f1"] = {"sleep": 1.0}
    crash_after_resume(port)
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks(["more"]))  # the re-send spawned f1, then died again
    assert s.turns.names() == [did, did + "-f1"]
    # a fresh recovery must not use resume at all
    port.resume = lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent again"))
    result = s.execute(dispatch, base, Hooks(["more"]))
    assert result["status"] == "verifying" and s.turns.names() == [did, did + "-f1"]
    actions = [r["action"] for r in s.head(did)["data"]["recoveries"]]
    assert actions == ["resent", "observed"]


def test_recovery_goes_on_with_the_remaining_follow_up(s: Setup) -> None:
    hooks = Hooks(["one", "two"])
    dispatch, base, _ = interrupted(s, "after", hooks)
    did = dispatch["dispatch_id"]
    s.wait_journal(did + "-f1", {"completed"})
    again = Hooks(["one", "two"])
    result = s.execute(dispatch, base, again)
    assert result["status"] == "verifying"
    assert s.turns.names() == [did, did + "-f1", did + "-f2"]  # f2 sent once, after the recovery
    assert again.seen == [1]  # the hook read the recovered turn before follow-up 2
    assert [e["dispatch_id"] for e in s.head(did)["data"]["followups"]] == [
        did + "-f1",
        did + "-f2",
    ]
    assert s.auth_files() == []


# ---- unsafe recoveries are held with the reason -----------------------------------------
def test_a_process_this_port_does_not_own_holds_orphan_session(s: Setup) -> None:
    dispatch, base, _ = interrupted(s, "after", Hooks(["more"]), sleep=30)
    did = dispatch["dispatch_id"]
    # read the spawned turns while the driver still lists the follow-up, so names() waits until
    # its process has logged (a loaded runner logs it later than the pop below)
    sent = s.turns.names()
    assert sent == [did, did + "-f1"]
    proc = s.driver.processes.pop(did + "-f1")  # a restarted worker owns no process
    s.cleanup.append(lambda: os.killpg(proc.pid, signal.SIGKILL))
    with pytest.raises(Hold) as orphan:
        s.execute(dispatch, base, Hooks(["more"]))
    assert orphan.value.code == "ORPHAN_SESSION"
    # nothing was sent again: no new follow-up process, and the log holds the same two turns
    assert did + "-f1" not in s.driver.processes
    assert s.turns.names() == sent
    head = s.head(did)
    assert head["state"] == "held" and head["data"]["hold_code"] == "ORPHAN_SESSION"
    assert head["data"]["driver_handle"] == did + "-f1"  # a later abort targets the follow-up
    assert not s.reported(dispatch)


def test_a_follow_up_that_ended_without_completing_holds_followup_recovery(s: Setup) -> None:
    dispatch, base, _ = interrupted(s, "after", Hooks(["more"]), sleep=30)
    did = dispatch["dispatch_id"]
    assert (
        s.coordinator.registry.resolve(
            s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
        ).cancel(did + "-f1")["process_stopped"]
        is True
    )
    sent = s.turns.names()
    with pytest.raises(Hold) as ended:
        s.execute(dispatch, base, Hooks(["more"]))
    assert ended.value.code == "FOLLOWUP_RECOVERY"
    assert ended.value.details["state"] in {"cancelled", "paused", "failed"}
    assert s.turns.names() == sent
    head = s.head(did)
    assert head["state"] == "held" and head["data"]["hold_code"] == "FOLLOWUP_RECOVERY"
    assert not s.reported(dispatch)


def test_a_message_that_was_not_kept_is_never_re_sent(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.coordinator._keep_message = lambda scope, message: None  # type: ignore[method-assign]
    port = s.port(dispatch)
    crash_before_resume(port)
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks(["secret-ish"]))
    assert s.head(did)["data"]["followups"][0]["message_artifact"] is None
    with pytest.raises(Hold) as unkept:
        s.execute(dispatch, base, Hooks(["secret-ish"]))
    assert unkept.value.code == "FOLLOWUP_RECOVERY"
    assert s.turns.names() == [did]  # no follow-up process ever ran
    assert s.head(did)["state"] == "held"


def test_a_run_that_is_no_longer_running_is_not_sent_a_follow_up(s: Setup) -> None:
    dispatch, base, _ = interrupted(s, "before", Hooks(["more"]))
    did = dispatch["dispatch_id"]
    s.d.runtime.end_goal(
        s.rig.actors.service, s.goal, outcome="cancelled", reason="cancelled while down"
    )
    with pytest.raises((Hold, RuntimeFault, Conflict)) as stopped:
        s.execute(dispatch, base, Hooks(["more"]))
    assert stopped.value.code  # FOLLOWUP_RECOVERY (run state) or the lease/goal check, never a send
    assert s.turns.names() == [did]
    assert s.head(did)["state"] != "verifying"


def test_the_execution_in_flight_in_this_worker_is_not_recovered_under_it(s: Setup) -> None:
    dispatch, base, _ = interrupted(s, "before", Hooks(["more"]))
    did = dispatch["dispatch_id"]
    s.coordinator._claim_local(did)  # another thread of this worker is driving the dispatch
    try:
        with pytest.raises(Hold) as busy:
            s.execute(dispatch, base, Hooks(["more"]))
    finally:
        s.coordinator._release_local(did)
    assert busy.value.code == "EXECUTION_RECONCILE" and s.turns.names() == [did]


def test_a_changed_request_conflicts_instead_of_recovering(s: Setup) -> None:
    dispatch, base, _ = interrupted(s, "before", Hooks(["more"]))
    with pytest.raises(Conflict) as changed:
        s.execute(dispatch, base, Hooks(["other message"]))
    assert changed.value.code == "EXECUTION_REPLAY"
    with pytest.raises(Conflict) as dropped:
        s.execute(dispatch, base, None)
    assert dropped.value.code == "EXECUTION_REPLAY"
    assert s.turns.names() == [dispatch["dispatch_id"]]


def test_a_first_turn_interrupted_before_any_follow_up_is_still_held(s: Setup) -> None:
    dispatch, base = s.claimed()
    port = s.port(dispatch)
    original = port.poll

    def die(handle: str) -> Any:
        raise Crash

    port.poll = die
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks(["more"]))
    port.poll = original
    with pytest.raises(Hold) as again:
        s.execute(dispatch, base, Hooks(["more"]))
    assert again.value.code == "EXECUTION_RECONCILE" and len(s.turns.calls) == 1


def test_the_abort_path_stops_a_follow_up_whose_handle_was_recorded(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["f1"] = {"sleep": 30}
    port = s.port(dispatch)
    original = port.poll
    hits = {"n": 0}

    def die_while_following_up(handle: str) -> Any:
        if handle.endswith("-f1"):
            hits["n"] += 1
            if hits["n"] >= 2:
                raise Crash
        return original(handle)

    port.poll = die_while_following_up
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks(["more"]))
    port.poll = original
    assert s.head(did)["data"]["driver_handle"] == did + "-f1"
    assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True
    held = s.head(did)
    assert held["state"] == "held" and held["data"]["hold_code"] == "CONTROLLER_ABORT"
    assert s.journal(did + "-f1")["state"] in {"cancelled", "paused", "failed"}


@pytest.mark.parametrize("stop", ["abort", "pause"])
def test_a_follow_up_the_worker_started_but_never_recorded_is_stopped_by_abort_and_pause(
    s: Setup, stop: str
) -> None:
    """The worker dies right after ``port.resume`` returned: the follow-up is recorded
    (``followup_dispatch_id``, worker.py:583) but ``driver_handle`` still names the finished
    turn (it is written after the resume, worker.py:592). ``abort`` and ``stop_and_snapshot``
    stop that recorded in-flight follow-up as well as ``driver_handle``
    (``_unrecorded_followup``), or its process keeps running (and editing the workspace) after
    the controller reports it stopped. ``abort`` destroys it once stopped, as it does
    ``driver_handle``, which drops it from the driver's process table: the process is kept here."""
    dispatch, _base, _ = interrupted(s, "after", Hooks(["more"]), sleep=30)
    did = dispatch["dispatch_id"]
    s.cleanup.append(lambda: kill_process(s, did + "-f1"))
    head = s.head(did)["data"]
    assert head["followup_dispatch_id"] == did + "-f1" and head["driver_handle"] == did
    assert s.journal(did + "-f1")["state"] in {"starting", "running"}
    process = s.driver.processes[did + "-f1"]
    if stop == "abort":
        assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True
    else:
        assert (
            s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")[
                "process_stopped"
            ]
            is True
        )
    assert s.journal(did + "-f1")["state"] in {"cancelled", "paused"}  # stopped on purpose
    assert process.poll() is not None  # the process is gone
    if stop == "abort":
        assert s.head(did)["data"]["process_stopped"] is True


# ---- a restarted worker: new ports, the same journal, store and scoped credential ----------
class NamedBox:
    """The container stand-in a restarted worker reaches: it stops a process by its dispatch id
    (``docker stop <name>``) although its own driver spawned none (its process table is empty)."""

    def __init__(self, named: dict[str, Any]) -> None:
        self.named = dict(named)

    def command(self, *args: Any, **kw: Any) -> list[str]:
        raise AssertionError("a restarted worker sends no turn in these tests")

    def stop(self, name: str) -> None:
        proc = self.named.get(name)
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

    def stopped(self, name: str) -> bool:
        proc = self.named.get(name)
        return proc is not None and proc.poll() is not None

    def destroy(self, name: str) -> None:
        return None


def restart(s: Setup) -> None:
    """What a worker restart builds: a new ``CodexCliDriver`` and a new ``SeededCodexPort`` (no
    resumed handle, ``_resumed`` empty) over the same driver journal and scoped credential, in a
    new registry and coordinator on the same store. ``s.coordinator`` is replaced."""
    from amplai_foundry.agent_drivers.cli import CodexCliDriver
    from amplai_foundry.agent_drivers.ports import DriverRegistry
    from amplai_foundry.runtime.execution.codex import SeededCodexPort
    from amplai_foundry.runtime.execution.worker import WorkCoordinator

    old = s.driver
    driver = CodexCliDriver(
        old.version, NamedBox(old.processes), old.journal, model=old.model, qualified=True,  # type: ignore[arg-type]
    )  # fmt: skip
    registry = DriverRegistry(s.d.store)
    registry.register(s.d.actor, s.rig.codex_refs["driver"], SeededCodexPort(driver, s.rig.home))
    s.coordinator = WorkCoordinator(s.d.runtime, registry, s.rig.workspaces, poll_seconds=0.05)


def recorded_follow_up(s: Setup) -> tuple[dict[str, Any], dict[str, Any]]:
    """The worker dies while it observes follow-up 1, whose handle it recorded
    (``driver_handle`` names ``<dispatch_id>-f1``, still running)."""
    dispatch, base = s.claimed()
    s.turns.plan["f1"] = {"sleep": 30}
    port = s.port(dispatch)
    original, hits = port.poll, {"n": 0}

    def die_while_following_up(handle: str) -> Any:
        if handle.endswith("-f1"):
            hits["n"] += 1
            if hits["n"] >= 2:
                raise Crash
        return original(handle)

    port.poll = die_while_following_up
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks(["more"]))
    port.poll = original
    return dispatch, base


@pytest.mark.parametrize("stop", ["recover", "abort", "pause"])
def test_a_restarted_worker_leaves_no_credential_in_the_bound_session_home(
    s: Setup, stop: str
) -> None:
    """M3 rule 2: no credential at rest between turns. Follow-up ``<did>-f1`` runs in the bound
    turn's home ``native_root/<did>`` (``SeededCodexPort.resume`` seeds ``checkpoint
    ["native_home"]``). A port built by a restarted worker seeded nothing, so its stop of the
    follow-up releases the home the follow-up's driver journal records (``SeededCodexPort.
    _release``), which is ``native_root/<did>``, never a guessed ``native_root/<did>-f1``: the
    ORPHAN_SESSION recovery hold, ``abort`` and ``stop_and_snapshot`` alike."""
    if stop == "recover":
        dispatch, base, _ = interrupted(s, "after", Hooks(["more"]), sleep=30)
    else:
        dispatch, base = recorded_follow_up(s)
    did = dispatch["dispatch_id"]
    s.cleanup.append(lambda: kill_process(s, did + "-f1"))
    s.wait_journal(did + "-f1", {"running"})
    sent = s.turns.names()  # waits until the follow-up's process has started
    assert sent == [did, did + "-f1"]
    bound = s.driver.native_root / did / AUTH
    assert s.auth_files() == [bound]  # leased for the running follow-up
    process = s.driver.processes[did + "-f1"]
    restart(s)
    if stop == "recover":
        with pytest.raises(Hold) as orphan:
            s.execute(dispatch, base, Hooks(["more"]))
        assert orphan.value.code == "ORPHAN_SESSION"
        head = s.head(did)["data"]
        assert s.head(did)["state"] == "held" and head["hold_code"] == "ORPHAN_SESSION"
        assert head["process_stopped"] is True
    elif stop == "abort":
        assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True
        assert s.head(did)["data"]["hold_code"] == "CONTROLLER_ABORT"
    else:
        stopped = s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
        assert stopped["process_stopped"] is True
    assert process.poll() is not None  # the follow-up's process is gone
    assert s.journal(did + "-f1")["state"] in {"cancelled", "paused"}
    assert not bound.exists() and s.auth_files() == []  # nothing left at rest
    assert s.turns.names() == sent  # and nothing was sent again


# ---- a process started before its handle was recorded ----------------------------------------
def crash_around_start(port: Any, spawn: bool) -> Any:
    """The worker dies right after ``port.start`` returned (``spawn``: the process runs) or right
    before it (nothing spawned): ``driver_handle`` was never recorded (``launching``)."""
    original = port.start

    def start_then_die(*args: Any, **kwargs: Any) -> Any:
        if spawn:
            original(*args, **kwargs)
        raise Crash

    port.start = start_then_die
    return original


@pytest.mark.parametrize("stop", ["abort", "pause", "restarted_abort", "restarted_pause"])
def test_a_process_started_before_its_handle_was_recorded_is_stopped_by_dispatch_id(
    s: Setup, stop: str
) -> None:
    """``abort`` and ``stop_and_snapshot`` read ``driver_handle``; a worker that died between
    ``port.start`` and recording the handle left it null with a running process. Both stop that
    process by the dispatch id whose driver journal exists (``_stop_spawned``): cancelled to a
    confirmed stop, destroyed, the credential released (also by a restarted worker's port, which
    finds the process by name), nothing sent again. A pause still cannot be taken over
    (SPAWN_UNKNOWN: no bound session to checkpoint)."""
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["run"] = {"sleep": 30}
    port = s.port(dispatch)
    original = crash_around_start(port, spawn=True)
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks([]))
    port.start = original
    s.cleanup.append(lambda: kill_process(s, did))
    head = s.head(did)
    assert head["state"] == "launching" and head["data"]["driver_handle"] is None
    s.wait_journal(did, {"running"})
    assert s.turns.names() == [did]
    process = s.driver.processes[did]
    assert s.auth_files() == [s.driver.native_root / did / AUTH]  # leased for the running turn
    if stop.startswith("restarted"):
        restart(s)
    if stop.endswith("pause"):
        with pytest.raises(Hold) as held:
            s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
        assert held.value.code == "SPAWN_UNKNOWN"
        assert held.value.details == {"process_stopped": True}
        after = s.head(did)
        assert after["state"] == "held" and after["data"]["hold_code"] == "SPAWN_UNKNOWN"
        # the controller's abort that follows (loop._abort) confirms the stop again
        assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True
    else:
        assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True
    held_head = s.head(did)
    assert held_head["state"] == "held" and held_head["data"]["process_stopped"] is True
    assert held_head["data"]["hold_code"] == "CONTROLLER_ABORT"
    assert process.poll() is not None  # no spawned process is left
    assert s.journal(did)["state"] == "cancelled"
    assert s.auth_files() == []  # no credential at rest
    assert s.turns.names() == [did]  # nothing was sent again
    with pytest.raises(Hold) as again:  # and a replay never spawns it a second time
        s.execute(dispatch, base, Hooks([]))
    assert again.value.code == "EXECUTION_RECONCILE"


def test_a_start_that_never_spawned_is_closed_so_it_can_never_start(s: Setup) -> None:
    """The worker died before ``port.start`` spawned anything: the journal is ``prepared``.
    ``abort`` cancels it (``prepared`` -> ``cancelled``), so no later start can spawn it."""
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    port = s.port(dispatch)
    original = crash_around_start(port, spawn=False)
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks([]))
    port.start = original
    assert s.journal(did)["state"] == "prepared" and s.head(did)["state"] == "launching"
    assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True
    assert s.journal(did)["state"] == "cancelled"
    assert s.head(did)["data"]["hold_code"] == "CONTROLLER_ABORT"
    assert s.turns.names() == [] and s.auth_files() == []


@pytest.mark.parametrize("spawn", [True, False])
def test_a_start_that_raised_is_stopped_by_dispatch_id_and_never_held_unstopped(
    s: Setup, spawn: bool
) -> None:
    """``port.start`` raised (an exception the worker handles, not a process stop) after
    ``launching`` and ``prepared_digest`` were recorded, so no handle came back: ``_fail`` stops
    the process it may have spawned by the dispatch id (``_stop_spawned``), so the head never
    ends ``held`` with ``process_stopped`` False while a process may live."""
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["run"] = {"sleep": 30}
    port = s.port(dispatch)
    original = port.start

    spawned: list[Any] = []

    def start_then_raise(*args: Any, **kwargs: Any) -> Any:
        if spawn:
            original(*args, **kwargs)
            spawned.append(s.driver.processes[did])
            s.wait_journal(did, {"running"})  # the process lives when start raises
        raise RuntimeFault("DRIVER_START", "the start acknowledgement was lost")

    port.start = start_then_raise
    s.cleanup.append(lambda: kill_process(s, did))
    with pytest.raises(RuntimeFault) as failed:
        s.execute(dispatch, base, Hooks([]))
    port.start = original
    assert failed.value.code == "DRIVER_START"
    head = s.head(did)
    assert head["state"] == "held" and head["data"]["hold_code"] == "DRIVER_START"
    assert head["data"]["driver_handle"] is None and head["data"]["prepared_digest"]
    assert head["data"]["process_stopped"] is True
    assert s.journal(did)["state"] == "cancelled"  # a closed journal can never start again
    if spawn:
        assert spawned[0].poll() is not None  # no spawned process is left
    else:
        assert s.turns.names() == []
    assert s.auth_files() == []  # no credential at rest


def test_an_execution_that_never_reached_start_has_nothing_to_stop(s: Setup) -> None:
    """Before ``launching`` (no ``prepared_digest``) nothing can have been spawned: ``abort``
    reports the stop without touching the port, as before."""
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    port = s.port(dispatch)
    original = port.prepare

    def die(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    port.prepare = die
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks([]))
    port.prepare = original
    assert s.head(did)["state"] == "preparing"
    cancelled: list[str] = []

    def cancel(handle: str) -> dict[str, Any]:
        cancelled.append(handle)
        return {"process_stopped": True}

    port.cancel = cancel
    assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True
    assert cancelled == [] and s.head(did)["state"] == "preparing"


# =========================================================================================
# (b) M4 candidates: sessions, sequence, the same base, selection
# =========================================================================================
def traced_port(s: Setup, dispatch: dict[str, Any], log: list[Any]) -> Any:
    port = s.port(dispatch)
    for name in ("start", "collect", "destroy", "prepare"):
        original = getattr(port, name)

        def traced(*args: Any, _name: str = name, _original: Any = original, **kw: Any) -> Any:
            first = args[0]
            log.append((_name, first["dispatch_id"] if isinstance(first, dict) else first))
            return _original(*args, **kw)

        setattr(port, name, traced)
    return port


def vote(s: Setup, k: int, **kw: Any) -> Vote:
    hooks = Vote(k, **kw)
    hooks.auth_files = s.auth_files
    return hooks


def test_candidates_run_one_after_another_from_the_same_base_and_release_each_credential(
    s: Setup,
) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    log: list[Any] = []
    port = traced_port(s, dispatch, log)
    hooks = vote(s, 3)
    hooks.on_evaluate = lambda index, workspace: log.append(("eval", index))
    s.turns.plan["run"] = {"writes": {"app.py": WRONG}}
    s.execute(dispatch, base, hooks)
    compact = [(n, h) for n, h in log if n in {"start", "collect", "destroy", "eval"}]
    assert compact == [
        ("start", did), ("collect", did), ("eval", 0),
        ("start", did + "-c1"), ("collect", did + "-c1"), ("destroy", did + "-c1"), ("eval", 1),
        ("start", did + "-c2"), ("collect", did + "-c2"), ("destroy", did + "-c2"), ("eval", 2),
        ("destroy", did),
    ]  # fmt: skip
    assert s.turns.names() == [did, did + "-c1", did + "-c2"]
    # the same base for every candidate (candidate 0 had already changed the run workspace)
    assert [c["base_at_start"] for c in s.turns.calls] == [BASE, BASE, BASE]
    # candidate dispatch = the run's with only the id replaced; own scratch workspaces
    prepared = [e for e in log if e[0] == "prepare"]
    assert [e[1] for e in prepared] == [did, did + "-c1", did + "-c2"]
    workspaces = [c["workspace"] for c in s.turns.calls]
    assert len({str(w) for w in workspaces}) == 3
    # no credential at rest while a hook ran (each collect released it); none at the end
    assert hooks.auth_at_eval == [[], [], []] and s.auth_files() == []
    del port


def test_candidate_sessions_are_never_bound_to_the_run(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    started: list[str] = []
    original = s.d.runtime.start

    def spy(worker: Any, disp: dict[str, Any], session: str) -> Any:
        started.append(disp["dispatch_id"] + ":" + session)
        return original(worker, disp, session)

    s.d.runtime.start = spy  # type: ignore[method-assign]
    s.execute(dispatch, base, vote(s, 3))
    assert started == [did + ":thread_" + did]  # runtime.start once, candidate 0's session
    with s.d.store._lock:
        rows = s.d.store.conn.execute(
            "SELECT id FROM heads WHERE tenant=? AND project=? AND kind='driver-session'",
            s.d.scope.keys(),
        ).fetchall()
    assert [r["id"] for r in rows] == [did]  # no driver-session row for a candidate
    assert s.run(dispatch)["data"]["session_handle"] == "thread_" + did
    # a candidate dispatch has no claim row: runtime.start would refuse it
    other, _ = s.claimed()
    candidate = json.loads(json.dumps(other))
    candidate["dispatch_id"] = other["dispatch_id"] + "-c1"
    with pytest.raises(RuntimeFault) as refused:
        original(s.worker, candidate, "thread_candidate")
    assert refused.value.code == "DISPATCH_BINDING"


def test_the_head_lists_the_candidates_their_journals_and_the_selection(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["run"] = {"writes": {"app.py": WRONG}}
    s.execute(dispatch, base, vote(s, 2))
    data = s.head(did)["data"]
    assert data["selected"] == 1  # candidate 1 passes the fast check, candidate 0 does not
    assert data["candidates_stopped"] is None
    zero, one = data["candidates"]
    assert (zero["index"], zero["dispatch_id"], zero["state"]) == (0, did, "collected")
    assert (one["index"], one["dispatch_id"], one["state"]) == (1, did + "-c1", "released")
    assert zero["fast_checks"] == {"check": False} and one["fast_checks"] == {"check": True}
    assert one["session"] == "thread_" + did + "-c1" and one["native_home"]
    assert one["receipt_digest"].startswith("sha256:") and one["usage"]
    assert not Path(one["workspace"]).exists()  # the scratch workspace is discarded


def test_the_selected_patch_is_applied_before_output_ready(s: Setup) -> None:
    dispatch, base = s.claimed()
    s.turns.plan["run"] = {"writes": {"app.py": WRONG}}
    seen: dict[str, Any] = {}
    collect = s.rig.workspaces.collect

    def spy(scope: Any, workspace: Path, *args: Any, **kw: Any) -> Any:
        seen["app"] = (workspace / "app.py").read_text()
        seen["run_state"] = s.run(dispatch)["state"]
        seen["selected"] = s.head(dispatch["dispatch_id"])["data"].get("selected")
        return collect(scope, workspace, *args, **kw)

    s.rig.workspaces.collect = spy
    result = s.execute(dispatch, base, vote(s, 2))
    assert result["status"] == "verifying"
    # at the collect, before output_ready: the run workspace already holds candidate 1's change
    assert seen == {"app": GOOD, "run_state": "running", "selected": 1}
    patch = s.change_patch(dispatch)
    assert "+    return 2" in patch and "return 3" not in patch
    assert s.run(dispatch)["state"] == "verifying"  # output_ready was reported


def test_the_smallest_diff_then_the_lowest_index_decide_among_equal_checks(s: Setup) -> None:
    dispatch, base = s.claimed()
    s.turns.plan["run"] = {"writes": {"app.py": GOOD, "notes.txt": "x" * 200}}
    s.turns.plan["c1"] = {"writes": {"app.py": GOOD}}
    s.turns.plan["c2"] = {"writes": {"app.py": GOOD}}
    hooks = vote(s, 3)
    s.execute(dispatch, base, hooks)
    assert hooks.selected == 1  # all pass; 0 is larger; 1 and 2 are equal, the lower index wins
    patch = s.change_patch(dispatch)
    assert "notes.txt" not in patch and "+    return 2" in patch
    assert s.head(dispatch["dispatch_id"])["data"]["selected"] == 1


def test_candidate_zero_wins_without_touching_the_run_workspace(s: Setup) -> None:
    dispatch, base = s.claimed()
    s.turns.plan["c1"] = {"writes": {"app.py": WRONG}}
    s.execute(dispatch, base, vote(s, 2))
    assert s.head(dispatch["dispatch_id"])["data"]["selected"] == 0
    assert "return 2" in s.change_patch(dispatch)


# ---- the 80 % budget stop -------------------------------------------------------------
def node_tokens(dispatch: dict[str, Any]) -> int:
    value = dispatch["node"]["budget"]["max_tokens"]
    assert type(value) is int
    return value


def test_no_candidate_starts_once_the_run_reached_80_percent_of_the_node_budget(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    cap = node_tokens(dispatch)
    assert CANDIDATE_BUDGET == 0.8
    s.turns.plan["run"] = {"usage": [int(cap * 0.8), 0]}  # exactly 80 %: stop
    s.execute(dispatch, base, vote(s, 3))
    assert s.turns.names() == [did]  # no candidate process
    data = s.head(did)["data"]
    assert data["candidates_stopped"] == {
        "before": 1, "tokens": int(cap * 0.8), "node_max_tokens": cap,
    }  # fmt: skip
    assert [e["index"] for e in data["candidates"]] == [0] and data["selected"] == 0


def test_the_budget_is_checked_before_every_candidate(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    cap = node_tokens(dispatch)
    half = int(cap * 0.4)
    s.turns.plan["run"] = {"usage": [half, 0]}  # 40 %: candidate 1 may start
    s.turns.plan["c1"] = {"usage": [half, 0]}  # 80 % after it: candidate 2 may not
    s.execute(dispatch, base, vote(s, 3))
    assert s.turns.names() == [did, did + "-c1"]
    stopped = s.head(did)["data"]["candidates_stopped"]
    assert stopped["before"] == 2 and stopped["tokens"] == 2 * half
    # 79 % leaves room for the next one
    dispatch2, base2 = s.claimed()
    s.turns.calls.clear()
    below = int(cap * 0.79)
    s.turns.plan["run"] = {"usage": [below, 0]}
    s.turns.plan["c1"] = {"usage": [1, 0]}
    s.turns.plan["c2"] = {"usage": [1, 0]}
    s.execute(dispatch2, base2, vote(s, 3))
    d2 = dispatch2["dispatch_id"]
    assert [n for n in s.turns.names() if n.startswith(d2)] == [
        d2,
        d2 + "-c1",
        d2 + "-c2",
    ]  # 79 % (+1 token) is below 80 %
    assert s.head(d2)["data"]["candidates_stopped"] is None


def test_unknown_usage_stops_generating_and_makes_the_run_usage_unknown(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["run"] = {"usage": None}
    s.execute(dispatch, base, vote(s, 2))
    assert s.turns.names() == [did]
    assert s.head(did)["data"]["candidates_stopped"]["tokens"] is None


def test_the_run_usage_sums_every_candidate_turn(s: Setup) -> None:
    dispatch, base = s.claimed()
    s.turns.plan["run"] = {"usage": [10, 5]}
    s.turns.plan["c1"] = {"usage": [20, 7]}
    s.execute(dispatch, base, vote(s, 2))
    usage = s.run_usage(dispatch)
    assert (usage["input_tokens"], usage["output_tokens"]) == (30, 12)  # separate sessions: summed


# ---- candidate failures ---------------------------------------------------------------
def test_a_candidate_that_ends_without_completing_is_left_out_of_the_vote(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["run"] = {"writes": {"app.py": WRONG}}
    s.turns.plan["c1"] = {"exit": 3}
    hooks = vote(s, 3)
    s.execute(dispatch, base, hooks)
    data = s.head(did)["data"]
    one = data["candidates"][1]
    assert one["state"] == "failed" and one["error"] == "DRIVER_BOUNDARY"
    assert one["process_stopped"] is True and "fast_checks" not in one
    assert not Path(one["workspace"]).exists()
    assert [x[1] for x in hooks.log] == [0]  # never evaluated
    # the left-out turn ran and its usage is not known: the run's usage is unknown (§5.3), and
    # unknown use stops generating (§5.1 M4 "80 %" cannot be shown), so candidate 2 never starts
    assert s.run_usage(dispatch)["input_tokens"] is None
    stopped = data["candidates_stopped"]
    assert stopped["before"] == 2 and stopped["tokens"] is None
    assert s.turns.names() == [did, did + "-c1"]
    assert data["selected"] == 0 and s.reported(dispatch)  # the evaluated candidates decide


def test_a_candidate_that_names_the_bound_session_holds_session_rebind(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["c1"] = {"session": "thread_" + did}
    with pytest.raises(Hold) as rebind:
        s.execute(dispatch, base, vote(s, 2))
    assert rebind.value.code == "SESSION_REBIND"
    head = s.head(did)
    assert head["state"] == "held" and head["data"]["hold_code"] == "SESSION_REBIND"
    assert head["data"]["candidates"][1]["error"] == "SESSION_REBIND"
    assert head["data"]["candidates"][1]["process_stopped"] is True
    assert not s.reported(dispatch)
    assert s.auth_files() == []


def test_a_hook_that_fails_ends_the_attempt_and_stops_everything(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    hooks = vote(s, 2)

    def boom(index: int, workspace: Path) -> None:
        if index == 1:
            raise Hold("FAST_CHECK_FAILED", "the host check broke")

    hooks.on_evaluate = boom
    with pytest.raises(Hold) as raised:
        s.execute(dispatch, base, hooks)
    assert raised.value.code == "FAST_CHECK_FAILED"
    head = s.head(did)
    assert head["state"] == "held" and head["data"]["hold_code"] == "FAST_CHECK_FAILED"
    assert head["data"].get("selected") is None
    assert not s.reported(dispatch)
    assert s.auth_files() == []


# ---- hooks contract (WORKER_HOOKS) ------------------------------------------------------
@pytest.mark.parametrize("candidates", [4, 0, -1, True, "2", 2.0])
def test_hooks_asking_for_other_than_one_to_three_candidates_are_refused(
    s: Setup, candidates: Any
) -> None:
    dispatch, base = s.claimed()
    hooks = Vote(2)
    hooks.candidates = candidates
    with pytest.raises(RuntimeFault) as refused:
        s.execute(dispatch, base, hooks)
    assert refused.value.code == "WORKER_HOOKS" and MAX_CANDIDATES == 3
    assert s.turns.calls == []
    with pytest.raises(RuntimeFault) as no_head:
        s.head(dispatch["dispatch_id"])
    assert no_head.value.code == "NOT_FOUND"


def test_a_vote_takes_no_follow_up_turns(s: Setup) -> None:
    dispatch, base = s.claimed()
    hooks = Vote(2)
    hooks.max_followups = 1
    with pytest.raises(RuntimeFault) as refused:
        s.execute(dispatch, base, hooks)
    assert refused.value.code == "WORKER_HOOKS" and MAX_FOLLOWUPS == 2 and s.turns.calls == []


def test_a_vote_needs_evaluate_and_select(s: Setup) -> None:
    dispatch, base = s.claimed()
    for missing in ("evaluate", "select"):
        hooks = Vote(2)
        setattr(hooks, missing, None)
        with pytest.raises(RuntimeFault) as refused:
            s.execute(dispatch, base, hooks)
        assert refused.value.code == "WORKER_HOOKS"
    assert s.turns.calls == []


def test_a_select_of_an_unevaluated_candidate_holds_worker_hooks(s: Setup) -> None:
    dispatch, base = s.claimed()
    hooks = vote(s, 2)
    hooks.select = lambda candidates: 5  # type: ignore[method-assign]
    with pytest.raises(RuntimeFault) as refused:
        s.execute(dispatch, base, hooks)
    assert refused.value.code == "WORKER_HOOKS"
    head = s.head(dispatch["dispatch_id"])
    assert head["state"] == "held" and head["data"].get("selected") is None


@pytest.mark.parametrize(
    "bad",
    [
        lambda i: object(),
        lambda i: CandidateResult(i + 1, 0, {"check": True}, None),  # another index
        lambda i: CandidateResult(i, -1, {"check": True}, None),
        lambda i: CandidateResult(i, 0, {"check": "yes"}, None),  # type: ignore[dict-item]
        lambda i: CandidateResult(i, True, {}, None),  # type: ignore[arg-type]
    ],
)
def test_a_malformed_candidate_evaluation_holds_worker_hooks(s: Setup, bad: Any) -> None:
    dispatch, base = s.claimed()
    hooks = vote(s, 2)
    hooks.evaluate = lambda *, workspace, index, receipt: bad(index)  # type: ignore[method-assign]
    with pytest.raises(RuntimeFault) as refused:
        s.execute(dispatch, base, hooks)
    assert refused.value.code == "WORKER_HOOKS"
    assert s.head(dispatch["dispatch_id"])["state"] == "held"


def test_a_run_workspace_that_does_not_hold_the_selection_holds_vote_apply(s: Setup) -> None:
    dispatch, base = s.claimed()
    s.turns.plan["run"] = {"writes": {"app.py": WRONG}}
    calls = {"n": 0}
    original = s.coordinator._content

    def content(scope: Any, ref: Any) -> Any:
        calls["n"] += 1
        value = original(scope, ref)
        return value if calls["n"] == 1 else {"differs": calls["n"]}

    s.coordinator._content = content  # type: ignore[method-assign]
    with pytest.raises(Hold) as apply:
        s.execute(dispatch, base, vote(s, 2))
    assert apply.value.code == "VOTE_APPLY"
    assert s.head(dispatch["dispatch_id"])["state"] == "held"
    assert not s.reported(dispatch)  # never reported


def test_replay_of_a_finished_vote_returns_its_result_and_a_changed_vote_conflicts(
    s: Setup,
) -> None:
    dispatch, base = s.claimed()
    result = s.execute(dispatch, base, vote(s, 2))
    sent = s.turns.names()
    assert s.execute(dispatch, base, vote(s, 2)) == result and s.turns.names() == sent
    with pytest.raises(Conflict) as changed:
        s.execute(dispatch, base, vote(s, 3))  # k is part of the request digest
    assert changed.value.code == "EXECUTION_REPLAY"
    with pytest.raises(Conflict) as dropped:
        s.execute(dispatch, base, None)
    assert dropped.value.code == "EXECUTION_REPLAY"
    assert s.turns.names() == sent


def test_a_crash_inside_a_candidate_is_held_never_recovered_or_re_sent(s: Setup) -> None:
    """The M3 recovery covers follow-ups only; an execution with candidates stays
    EXECUTION_RECONCILE (a candidate's native session is not the run's)."""
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["c1"] = {"sleep": 30}
    port = s.port(dispatch)
    original = port.poll

    def die_in_candidate(handle: str) -> Any:
        if handle.endswith("-c1"):
            raise Crash
        return original(handle)

    port.poll = die_in_candidate
    with pytest.raises(Crash):
        s.execute(dispatch, base, vote(s, 2))
    port.poll = original
    sent = s.turns.names()
    assert sent == [did, did + "-c1"]
    with pytest.raises(Hold) as again:
        s.execute(dispatch, base, vote(s, 2))
    assert again.value.code == "EXECUTION_RECONCILE" and s.turns.names() == sent
    # and the abort path stops the candidate's process as well as the run's handle
    assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True
    held = s.head(did)
    assert held["state"] == "held" and held["data"]["hold_code"] == "CONTROLLER_ABORT"
    assert held["data"]["process_stopped"] is True
    assert held["data"]["candidates"][1]["process_stopped"] is True
    assert s.journal(did + "-c1")["state"] in {"cancelled", "paused", "failed"}
    assert s.auth_files() == []


# =========================================================================================
# (b) steering pause during candidate i
# =========================================================================================
def pause_goal(s: Setup, dispatch: dict[str, Any]) -> None:
    operator = with_permissions(s.rig.operator, "goal.steer")
    goal = s.goal
    ref = s.d.store.head(s.d.scope, "goal", goal)["data"]["active_contract_ref"]
    s.loop.steering.receive(
        operator, goal, "pause", "pause during a vote", key=f"pause-{dispatch['dispatch_id']}",
        expected_contract_ref=ref,
    )  # fmt: skip


def run_in_thread(s: Setup, dispatch: dict[str, Any], base: dict[str, Any], hooks: Any) -> Any:
    box: dict[str, Any] = {}

    def go() -> None:
        try:
            box["result"] = s.execute(dispatch, base, hooks)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    box["thread"] = thread
    return box


@pytest.mark.parametrize("i", [1, 2])
def test_a_pause_during_candidate_i_stops_it_and_checkpoints_the_bound_session(
    s: Setup, i: int
) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["run"] = {"writes": {"app.py": WRONG}}
    s.turns.plan[f"c{i}"] = {"sleep": 30}
    box = run_in_thread(s, dispatch, base, vote(s, 3))
    s.wait_journal(f"{did}-c{i}", {"running"})
    end = time.monotonic() + 20
    while not s.journal(f"{did}-c{i}").get("session_handle"):  # it named its own session
        assert time.monotonic() < end
        time.sleep(0.05)
    pause_goal(s, dispatch)
    box["thread"].join(30)
    assert not box["thread"].is_alive()
    assert getattr(box["error"], "code", None) == "EXECUTION_PAUSED"
    head = s.head(did)
    # the worker leaves the run for the controller; candidate i was stopped on the way out
    assert head["state"] == "pause_requested" and head["data"]["hold_code"] == "EXECUTION_PAUSED"
    entry = head["data"]["candidates"][i]
    assert entry["state"] == "stopped" and entry["error"] == "EXECUTION_PAUSED"
    assert entry["process_stopped"] is True
    assert s.journal(f"{did}-c{i}")["state"] in {"cancelled", "paused"}
    assert not Path(entry["workspace"]).exists()  # the scratch workspace is discarded
    # what the controller then does (SteeringService.quiesce -> stop_and_snapshot)
    paused = s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
    assert paused["process_stopped"] is True
    # the bound session (candidate 0's): the steering checkpoint check passes (steering.py:191)
    assert paused["session_handle"] == "thread_" + did == s.run(dispatch)["data"]["session_handle"]
    after = s.head(did)
    assert after["state"] == "paused" and after["data"]["driver_checkpoint"]["dispatch_id"] == did
    assert after["data"].get("selected") is None  # the vote is dropped
    # the run workspace is candidate 0's tree, not a candidate scratch tree
    snapshot = json.loads(s.d.artifacts.read(s.d.scope, paused["workspace_diff_artifact"]))
    patch = s.d.artifacts.read(s.d.scope, snapshot["patch"]).decode()
    assert "+    return 3" in patch
    assert s.auth_files() == []  # no credential at rest at the boundary


def test_a_pause_while_a_host_check_runs_is_taken_over_and_the_hook_is_abandoned(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    release = threading.Event()
    hooks = vote(s, 2)
    hooks.on_evaluate = lambda index, workspace: release.wait(30)
    box = run_in_thread(s, dispatch, base, hooks)
    end = time.monotonic() + 20
    while not hooks.log:
        assert time.monotonic() < end
        time.sleep(0.05)
    pause_goal(s, dispatch)
    box["thread"].join(30)
    release.set()
    assert getattr(box["error"], "code", None) == "EXECUTION_PAUSED"
    assert hooks.abandoned is True
    assert s.head(did)["state"] == "pause_requested"
    assert s.turns.names() == [did]  # no candidate started after the pause
    paused = s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
    assert paused["process_stopped"] is True and paused["session_handle"] == "thread_" + did


def test_a_pause_that_cannot_confirm_a_candidate_stop_is_not_confirmed(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["c1"] = {"sleep": 30}
    port = s.port(dispatch)
    original = port.poll

    def die_in_candidate(handle: str) -> Any:
        if handle.endswith("-c1"):
            raise Crash
        return original(handle)

    port.poll = die_in_candidate
    with pytest.raises(Crash):
        s.execute(dispatch, base, vote(s, 2))
    port.poll = original
    cancel = port.cancel

    def no_answer(handle: str, **kw: Any) -> dict[str, Any]:
        if handle.endswith("-c1"):
            return {"process_stopped": False}  # e.g. docker did not answer
        return cancel(handle, **kw)  # type: ignore[no-any-return]

    port.cancel = no_answer
    unconfirmed = s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
    assert unconfirmed == {"process_stopped": False}  # the controller then leaves it queued
    data = s.head(did)["data"]
    assert data["candidates"][1]["process_stopped"] is False
    assert s.journal(did)["state"] == "completed"  # candidate 0 was not paused: nothing taken over
    port.cancel = cancel
    confirmed = s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
    assert confirmed["process_stopped"] is True and confirmed["session_handle"] == "thread_" + did
    assert s.head(did)["data"]["candidates"][1]["process_stopped"] is True
    assert s.auth_files() == []


def test_no_pause_is_taken_over_after_a_winner_other_than_zero(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["run"] = {"writes": {"app.py": WRONG}}
    collect = s.rig.workspaces.collect

    def die(*args: Any, **kw: Any) -> Any:
        raise Crash  # the worker dies after the selection was applied, before output_ready

    s.rig.workspaces.collect = die
    with pytest.raises(Crash):
        s.execute(dispatch, base, vote(s, 2))
    s.rig.workspaces.collect = collect
    data = s.head(did)["data"]
    assert data["selected"] == 1 and s.run(dispatch)["state"] == "running"
    with pytest.raises(Hold) as selected:
        s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
    assert selected.value.code == "VOTE_SELECTED"
    assert s.head(did)["state"] == "observing"  # untouched: nothing was paused
    # the abort path (never leave a process running) still works
    assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True


def test_a_pause_after_winner_zero_is_still_taken_over(s: Setup) -> None:
    dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan["c1"] = {"writes": {"app.py": WRONG}}
    collect = s.rig.workspaces.collect

    def die(*args: Any, **kw: Any) -> Any:
        raise Crash

    s.rig.workspaces.collect = die
    with pytest.raises(Crash):
        s.execute(dispatch, base, vote(s, 2))
    s.rig.workspaces.collect = collect
    assert s.head(did)["data"]["selected"] == 0
    paused = s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
    assert paused["process_stopped"] is True and paused["session_handle"] == "thread_" + did
    assert s.head(did)["state"] == "paused"
