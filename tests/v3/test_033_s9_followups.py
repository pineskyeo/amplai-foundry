"""Work 033 S9: follow-up turns (M3, IC-04), the auxiliary-turn cap (IC-21) and §14 Q16.

Contract: specs/033-harness-taxonomy/interfaces.md §0 IC-04 and IC-21, §3.6 ``TurnHooks`` and
``StrategyRunner``, §5.1 M3 rules 1-5 and the M4 rules, §5.3, §14 Q4 and Q16.

Real: store, runtime, worker coordinator (``WorkCoordinator.execute``), the Codex-shaped CLI port
with its credential seeding (``SeededCodexPort``), the journal, the git workspace, the lease and
the execution envelope. Stand-in: the "container" is the host-process script of the rc06 rig that
writes the files a per-call script asks for. No docker, provider, network or credential.

Covered (M3 rules of §5.1):
1. dispatch id ``<dispatch_id>-f<k>`` (k = 1, 2), the new dispatch dict is the run's with only the
   id replaced, never a steering ``resume-<digest>`` id;
2. order collect -> checkpoint -> resume -> poll -> collect, the credential released at every
   collect (none at rest while a hook runs) and seeded again at every resume, ``destroy`` only
   after the last turn;
3. ``assert_execution_live`` and lease heartbeats before and across follow-ups, ``SESSION_REBIND``
   on a checkpoint, a stream or a receipt that names another session;
4. the worker-execution head records ``followups``; the request digest includes
   ``hooks.spec_digest()`` (a replay with other hooks conflicts);
5. usage of follow-ups is summed only after §14 Q4: the last receipt only for a Codex session.

§14 Q16, answered in S9b ("Clarifications After S12, S15 And S9b"; the full answer and its
fake-port tests are ``tests/v3/test_033_s9b_q16.py``): (a) a worker execution interrupted inside a
follow-up is recovered from the follow-up's driver journal, never sent twice (pinned here for the
case this file always had: the process was started, so it is observed or collected); a first turn
interrupted before any follow-up is still held (``EXECUTION_RECONCILE``). (b) Hooks asking for
candidates go to the vote path (M4): hooks that are not a vote (no ``evaluate``) are refused
(``WORKER_HOOKS``) before anything starts. The tests below that pin what a candidate dispatch
would do *outside* that path (through ``execute``, or as a second worker-execution row of the
run) keep showing why candidates run as journals listed in the head, never as rows.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.agent_drivers.ports import UNKNOWN_USAGE
from amplai_foundry.runtime.contracts.identity import ID, digest
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution import worker as worker_module
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.execution.strategy_runner import (
    AuxLedger,
    CandidateResult,
    ReviewHooks,
    StrategyRunner,
    usage_tokens,
)
from amplai_foundry.runtime.execution.worker import MAX_FOLLOWUPS, sum_usage
from amplai_foundry.sandbox.git_workspace import PATCH_BINDING
from rc06_rig import rig_with_codex, submit

PORT = "change"
GOOD = "def value():\n    return 2\n"
AGENT = r"""
import json, pathlib, sys, time
ws, spec, home = pathlib.Path(sys.argv[1]), json.loads(sys.argv[2]), pathlib.Path(sys.argv[3])
assert (home / ".codex" / "auth.json").is_file(), "credential was not leased"
if spec.get("sleep"):
    sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": spec["session"]}) + "\n")
    sys.stdout.flush()
    time.sleep(spec["sleep"])
for rel, text in spec["writes"].items():
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
(home / ".codex" / "auth.json").write_text('{"tokens": "refreshed"}')
sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": spec["session"]}) + "\n")
sys.stdout.write(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": spec["usage"][0], "output_tokens": spec["usage"][1]}}) + "\n")
"""


class Turns:
    """The scripted agent: one process per turn, the first and every resumed one."""

    def __init__(self, container: Any) -> None:
        self.container = container
        self.calls: list[dict[str, Any]] = []
        self.plan: dict[int, dict[str, Any]] = {}  # call index -> overrides of the spec
        container.command = self.command

    def command(self, argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        resume = len(argv) > 5 and argv[4] == "resume"
        index = len(self.calls)
        home = Path(kw["native_home"])
        self.calls.append(
            {"index": index, "name": run_name, "resume": resume, "prompt": argv[-1],
             "session": argv[5] if resume else None, "home": home,
             "auth_at_start": (home / AUTH).is_file()}
        )  # fmt: skip
        spec = {
            "writes": {"app.py": GOOD},
            "session": argv[5] if resume else "thread_" + run_name,
            "usage": [10, 5],
            **self.plan.get(index, {}),
        }
        return [sys.executable, "-c", AGENT, str(workspace), json.dumps(spec), str(home)]


class Hooks:
    """``TurnHooks``: ``messages[k]`` is the follow-up message after turn k (None ends)."""

    candidates = 1

    def __init__(self, messages: list[str | None], *, followups: int | None = None) -> None:
        self.messages = list(messages)
        self.max_followups = len(messages) if followups is None else followups
        self.seen: list[dict[str, Any]] = []
        self.on_turn: Any = None  # a callable run inside after_turn (what a reviewer turn does)
        self.abandoned = False

    def spec_digest(self) -> str:
        return digest({"hooks": "s9-test", "messages": self.messages})

    def after_turn(self, *, workspace: Path, turn: int, receipt: dict[str, Any]) -> str | None:
        self.seen.append({"turn": turn, "workspace": workspace, "receipt": receipt,
                          "app": (workspace / "app.py").read_text(),
                          "files": sorted(p.name for p in workspace.iterdir()
                                          if p.name != ".git")})  # fmt: skip
        if self.on_turn is not None:
            self.on_turn(turn, workspace)
        return self.messages[turn] if turn < len(self.messages) else None

    def select(self, candidates: list[CandidateResult]) -> int:
        return 0

    def abandon(self) -> None:
        self.abandoned = True


class Setup:
    def __init__(self, deployment: Any, tmp_path: Path) -> None:
        self.rig, self.loop, self.container = rig_with_codex(deployment, tmp_path, "right")
        self.d = self.rig.d
        self.turns = Turns(self.container)
        self.coordinator = self.loop.coordinator
        self.driver = self.container.driver
        self.worker = self.rig.actors.worker
        self.count = 0
        self.goals: list[str] = []

    def claimed(self) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """An approved goal with one claimed run: (goal id, dispatch, the plan's base). The goal
        of an earlier call is ended first (it holds ``sandbox:app``: write concurrency 1)."""
        for earlier in self.goals:
            self.d.runtime.end_goal(
                self.rig.actors.service, earlier, outcome="cancelled", reason="next test goal"
            )
        self.goals.clear()
        self.count += 1
        goal = submit(self.rig, f"({self.count}) make value return 2")
        self.goals.append(goal)
        plan = self.rig.service.plan(goal)
        self.rig.service.approve(self.rig.operator, goal)
        dispatch = self.d.runtime.claim(self.worker, goal_id=goal)
        assert dispatch is not None
        return goal, dispatch, plan["base"]

    def execute(self, dispatch: dict[str, Any], base: dict[str, Any], hooks: Any, **kw: Any) -> Any:
        return self.coordinator.execute(
            self.worker, dispatch, prompt="Make value() return 2.", base_snapshot=base,
            output_paths={PORT: PATCH_BINDING}, hooks=hooks, **kw,
        )  # fmt: skip

    def head(self, dispatch_id: str) -> dict[str, Any]:
        head: dict[str, Any] = self.d.store.head(self.d.scope, "worker-execution", dispatch_id)
        return head

    def auth_files(self) -> list[Path]:
        return sorted(self.driver.native_root.glob("*/" + str(AUTH)))


@pytest.fixture
def s(deployment: Any, tmp_path: Path) -> Setup:
    return Setup(deployment, tmp_path)


# =========================================================================================
# M3 rule 1: the dispatch id and the new dispatch dict
# =========================================================================================
def test_follow_up_dispatch_ids_are_the_run_dispatch_id_plus_f_k(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    hooks = Hooks(["first change", "second change"])
    s.execute(dispatch, base, hooks)
    assert [c["name"] for c in s.turns.calls] == [did, f"{did}-f1", f"{did}-f2"]
    assert [c["resume"] for c in s.turns.calls] == [False, True, True]
    assert all(ID.fullmatch(c["name"]) for c in s.turns.calls)  # the id pattern (identity.py:20)
    assert not any(c["name"].startswith("resume-") for c in s.turns.calls)  # steering's ids
    assert [c["prompt"] for c in s.turns.calls[1:]] == ["first change", "second change"]
    # every follow-up resumes the one native session of the first turn
    assert {c["session"] for c in s.turns.calls[1:]} == {"thread_" + did}
    # the hook saw the finished turn before each follow-up, and a last time after the second
    assert [h["turn"] for h in hooks.seen] == [0, 1]
    assert all(h["receipt"]["provider_completed"] is True for h in hooks.seen)


def test_the_follow_up_dispatch_is_the_run_dispatch_with_only_the_id_replaced(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    original, resumed = port.resume, []

    def resume(new_dispatch: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        resumed.append((new_dispatch, args[0]))
        return original(new_dispatch, *args, **kwargs)

    port.resume = resume
    s.execute(dispatch, base, Hooks(["more"]))
    # same run id, lease and fencing token, the profile and node too: only the id differs
    ((sent, message),) = resumed
    assert sent == {**dispatch, "dispatch_id": did + "-f1"} and message == "more"
    assert sent["lease"] == dispatch["lease"] and sent["run_id"] == dispatch["run_id"]
    first, follow = (s.driver.journal.read(h) for h in (did, did + "-f1"))
    assert follow["state"] == "completed" and follow["session_handle"] == first["session_handle"]
    assert (follow["workspace"], follow["native_home"]) == (
        first["workspace"],
        first["native_home"],
    )
    head = s.head(did)["data"]
    assert head["dispatch"] == dispatch and head["run_id"] == dispatch["run_id"]
    assert s.head(did)["state"] == "verifying"
    with pytest.raises(RuntimeFault) as no_head:  # no worker-execution row of its own
        s.head(did + "-f1")
    assert no_head.value.code == "NOT_FOUND"


def test_the_head_records_each_follow_up_with_its_prompt_and_receipt_digests(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.execute(dispatch, base, Hooks(["one", "two"]))
    entries = s.head(did)["data"]["followups"]
    assert [e["dispatch_id"] for e in entries] == [f"{did}-f1", f"{did}-f2"]
    assert [e["prompt_digest"] for e in entries] == [digest("one"), digest("two")]
    assert all(e["receipt_digest"].startswith("sha256:") and e["usage"] for e in entries)


def test_the_follow_up_edits_the_same_workspace_before_one_output_ready(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    s.turns.plan[1] = {"writes": {"app.py": GOOD, "notes.txt": "after review\n"}}
    hooks = Hooks(["add a note"])
    result = s.execute(dispatch, base, hooks)
    assert result["status"] == "verifying" and result["run_id"] == dispatch["run_id"]
    # the change is collected once, after the last turn, and holds both turns' files
    assert hooks.seen[0]["app"] == GOOD and hooks.seen[0]["files"] == ["app.py"]  # before the note
    work = s.d.store.head(s.d.scope, "work", dispatch["node"]["work_id"])
    assert work["state"] == "verifying"
    change = json.loads(s.d.artifacts.read(s.d.scope, work["data"]["outputs"][PORT]))
    patch = s.d.artifacts.read(s.d.scope, change["patch"]).decode()
    assert "notes.txt" in patch and "return 2" in patch


def test_no_follow_up_without_hooks_or_without_a_message(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    s.execute(dispatch, base, None)
    assert len(s.turns.calls) == 1 and "followups" not in s.head(dispatch["dispatch_id"])["data"]
    _goal, second, base = s.claimed()
    quiet = Hooks([None, "never sent"])  # the hook ends the loop at the first None
    s.execute(second, base, quiet)
    assert len(s.turns.calls) == 2 and [h["turn"] for h in quiet.seen] == [0]
    _goal, third, base = s.claimed()
    blank = Hooks(["   \n"])  # a blank message is no message
    s.execute(third, base, blank)
    assert len(s.turns.calls) == 3


def test_a_hook_that_is_not_asked_is_not_part_of_the_request(s: Setup) -> None:
    """max_followups 0 asks nothing: the hooks digest stays out of the request (replay, §3.4)."""
    _goal, dispatch, base = s.claimed()
    s.execute(dispatch, base, Hooks([], followups=0))
    assert len(s.turns.calls) == 1
    again = s.execute(dispatch, base, None)  # the same request without hooks replays its result
    assert again["run_id"] == dispatch["run_id"] and len(s.turns.calls) == 1


# =========================================================================================
# M3 rule 2: collect -> checkpoint -> resume -> poll -> collect; the credential
# =========================================================================================
def test_the_credential_is_released_at_every_collect_and_seeded_at_every_resume(
    s: Setup,
) -> None:
    _goal, dispatch, base = s.claimed()
    at_rest: list[list[Path]] = []
    hooks = Hooks(["one", "two"])
    hooks.on_turn = lambda turn, workspace: at_rest.append(s.auth_files())
    s.execute(dispatch, base, hooks)
    # while a hook runs (between a collect and the next resume) no credential is on disk
    assert at_rest == [[], []]
    # each turn started with the credential in its native home (the script asserts it too)
    assert [c["auth_at_start"] for c in s.turns.calls] == [True, True, True]
    assert {c["home"] for c in s.turns.calls} == {s.driver.native_root / dispatch["dispatch_id"]}
    # nothing at rest afterwards, and the refreshed token went back to the operator's home
    assert s.auth_files() == []
    assert (s.rig.home / AUTH).read_text() == '{"tokens": "refreshed"}'


def test_the_order_is_collect_checkpoint_resume_poll_collect_and_destroy_only_at_the_end(
    s: Setup,
) -> None:
    _goal, dispatch, base = s.claimed()
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    log: list[str] = []
    for name in ("collect", "checkpoint", "resume", "poll", "destroy", "start", "cancel"):
        original = getattr(port, name)

        def traced(*args: Any, _name: str = name, _original: Any = original, **kw: Any) -> Any:
            log.append(_name)
            return _original(*args, **kw)

        setattr(port, name, traced)
    hooks = Hooks(["more"])
    hooks.on_turn = lambda turn, workspace: log.append("hook")
    s.execute(dispatch, base, hooks)
    compact = [a for i, a in enumerate(log) if a != "poll" or log[i - 1] != "poll"]
    # the hook is asked once per follow-up it may send (max_followups), so after the last
    # follow-up it is not asked again; both handles are destroyed after the last turn
    assert compact == [
        "start", "poll", "collect",  # turn 1
        "hook", "checkpoint", "resume", "poll", "collect",  # the follow-up
        "destroy", "destroy",
    ]  # fmt: skip
    assert "cancel" not in log


def test_every_handle_is_destroyed_after_the_last_turn_and_none_before(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    destroyed: list[tuple[str, list[str]]] = []
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    original = port.destroy

    def destroy(handle: str) -> Any:
        destroyed.append((handle, [c["name"] for c in s.turns.calls]))
        return original(handle)

    port.destroy = destroy
    s.execute(dispatch, base, Hooks(["one", "two"]))
    did = dispatch["dispatch_id"]
    assert [h for h, _ in destroyed] == [f"{did}-f2", f"{did}-f1", did]  # last turn first
    assert all(started == [did, f"{did}-f1", f"{did}-f2"] for _, started in destroyed)


# =========================================================================================
# M3 rule 3: liveness, heartbeats, SESSION_REBIND
# =========================================================================================
def test_a_follow_up_is_not_sent_once_the_execution_is_no_longer_live(s: Setup) -> None:
    goal, dispatch, base = s.claimed()
    hooks = Hooks(["never sent"])
    # the operator's goal ends while the hook runs (as a cancel does): the lease dies with it
    hooks.on_turn = lambda turn, workspace: s.d.runtime.end_goal(
        s.rig.actors.service, goal, outcome="cancelled", reason="cancelled during review"
    )
    with pytest.raises((Hold, RuntimeFault, Conflict)) as stopped:
        s.execute(dispatch, base, hooks)
    assert stopped.value.code
    assert len(s.turns.calls) == 1  # the follow-up was never prepared
    head = s.head(dispatch["dispatch_id"])
    # which check sees it first is a race with end_goal's own transactions: the goal is no longer
    # active (EXECUTION_PAUSED: the worker leaves the process to the controller, state
    # pause_requested) or the lease is gone (held); a follow-up is sent in neither case
    assert head["state"] in {"held", "pause_requested"}
    assert head["data"]["hold_code"] == stopped.value.code
    assert not (s.driver.journal.root / (dispatch["dispatch_id"] + "-f1.json")).exists()


def test_the_worker_beats_the_lease_every_thirty_seconds_across_turns(
    s: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _goal, dispatch, _base = s.claimed()
    beats: list[int] = []
    s.d.runtime.heartbeat = lambda *a, sequence, **k: beats.append(sequence)  # type: ignore[method-assign]
    clock = {"now": 1000.0}
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: clock["now"])
    beat = {"sequence": 0, "last": 0.0}
    s.coordinator._beat(s.worker, dispatch, beat)
    assert beats == [1] and beat["last"] == 1000.0  # the first call beats
    clock["now"] += 29.9
    s.coordinator._beat(s.worker, dispatch, beat)
    assert beats == [1]  # not yet
    clock["now"] += 0.2
    s.coordinator._beat(s.worker, dispatch, beat)
    assert beats == [1, 2]  # the sequence goes on across turns and hooks


def test_a_follow_up_whose_checkpoint_names_another_session_holds_session_rebind(
    s: Setup,
) -> None:
    _goal, dispatch, base = s.claimed()
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    original = port.checkpoint
    port.checkpoint = lambda handle: {**original(handle), "session_handle": "thread_other"}
    with pytest.raises(Hold) as rebind:
        s.execute(dispatch, base, Hooks(["more"]))
    assert rebind.value.code == "SESSION_REBIND"
    assert len(s.turns.calls) == 1  # no resume was attempted
    head = s.head(dispatch["dispatch_id"])
    assert head["state"] == "held" and head["data"]["hold_code"] == "SESSION_REBIND"


def test_a_follow_up_stream_that_changes_the_session_holds_session_rebind(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    # the resumed process reports another thread id (the checkpoint was the bound session)
    s.turns.plan[1] = {"session": "thread_hijacked"}
    with pytest.raises(Hold) as rebind:
        s.execute(dispatch, base, Hooks(["more"]))
    # the driver's own normalizer refuses a stream whose session differs from the expected one,
    # or the worker does; either way the hold is a session boundary and nothing is output_ready
    assert rebind.value.code in {"SESSION_REBIND", "DRIVER_BOUNDARY"}, rebind.value.code
    head = s.head(did)
    assert head["state"] == "held" and head["data"].get("result") is None
    assert [c["name"] for c in s.turns.calls] == [did, did + "-f1"]


def test_a_follow_up_receipt_that_names_another_session_holds_session_rebind(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    original = port.collect
    seen = {"n": 0}

    def collect(handle: str) -> dict[str, Any]:
        seen["n"] += 1
        receipt = dict(original(handle))
        return {**receipt, "session_handle": "thread_other"} if seen["n"] == 2 else receipt

    port.collect = collect
    with pytest.raises(Hold) as rebind:
        s.execute(dispatch, base, Hooks(["more"]))
    assert rebind.value.code == "SESSION_REBIND"
    assert s.head(dispatch["dispatch_id"])["state"] == "held"


def test_a_follow_up_that_stops_without_completing_holds_driver_boundary(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    original = port.poll
    seen = {"resumed": False}

    def poll(handle: str) -> dict[str, Any]:
        seen["resumed"] = seen["resumed"] or handle.endswith("-f1")
        observation = dict(original(handle))
        return {**observation, "state": "failed"} if handle.endswith("-f1") else observation

    port.poll = poll
    with pytest.raises(Hold) as boundary:
        s.execute(dispatch, base, Hooks(["more"]))
    assert boundary.value.code == "DRIVER_BOUNDARY"
    assert boundary.value.details == {"state": "failed", "followup": 1}
    head = s.head(dispatch["dispatch_id"])
    assert head["state"] == "held" and head["data"]["hold_code"] == "DRIVER_BOUNDARY"
    assert head["data"]["followups"][0]["receipt_digest"] is None  # it never produced a receipt


def test_a_follow_up_receipt_without_positive_completion_holds_collect_boundary(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    original = port.collect
    seen = {"n": 0}

    def collect(handle: str) -> dict[str, Any]:
        seen["n"] += 1
        receipt = dict(original(handle))
        return {**receipt, "provider_completed": False} if seen["n"] == 2 else receipt

    port.collect = collect
    with pytest.raises(Hold) as boundary:
        s.execute(dispatch, base, Hooks(["more"]))
    assert boundary.value.code == "COLLECT_BOUNDARY"


# =========================================================================================
# M3 rule 4: the request digest, replay, the hooks contract
# =========================================================================================
def test_a_replay_with_other_hooks_conflicts_and_the_same_hooks_return_the_result(
    s: Setup,
) -> None:
    _goal, dispatch, base = s.claimed()
    hooks = Hooks(["more"])
    result = s.execute(dispatch, base, hooks)
    calls = len(s.turns.calls)
    assert s.execute(dispatch, base, Hooks(["more"])) == result  # same spec digest: a replay
    assert len(s.turns.calls) == calls  # nothing was sent again
    with pytest.raises(Conflict) as changed:
        s.execute(dispatch, base, Hooks(["other message"]))
    assert changed.value.code == "EXECUTION_REPLAY"
    with pytest.raises(Conflict) as dropped:
        s.execute(dispatch, base, None)  # hooks that could resume are part of the request
    assert dropped.value.code == "EXECUTION_REPLAY"
    assert len(s.turns.calls) == calls


@pytest.mark.parametrize("followups", [3, -1, True, "2", 1.5])
def test_a_hook_asking_for_more_than_two_follow_ups_is_refused_before_anything_starts(
    s: Setup, followups: Any
) -> None:
    _goal, dispatch, base = s.claimed()
    hooks = Hooks(["a"], followups=followups)
    with pytest.raises(RuntimeFault) as refused:
        s.execute(dispatch, base, hooks)
    assert refused.value.code == "WORKER_HOOKS" and MAX_FOLLOWUPS == 2
    assert s.turns.calls == []
    with pytest.raises(RuntimeFault) as no_head:
        s.head(dispatch["dispatch_id"])
    assert no_head.value.code == "NOT_FOUND"  # refused before the execution row


def test_a_follow_up_message_must_be_text(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    hooks = Hooks([None])
    hooks.messages = [42]  # type: ignore[list-item]
    with pytest.raises(RuntimeFault) as refused:
        s.execute(dispatch, base, hooks)
    assert refused.value.code == "WORKER_HOOKS" and len(s.turns.calls) == 1
    assert s.head(dispatch["dispatch_id"])["state"] == "held"


def test_a_hook_that_raises_ends_the_attempt_held_with_its_code(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    hooks = Hooks(["more"])

    def boom(turn: int, workspace: Path) -> None:
        raise Hold("REVIEW_FAILED", "the reviewer turn broke")

    hooks.on_turn = boom
    with pytest.raises(Hold) as raised:
        s.execute(dispatch, base, hooks)
    assert raised.value.code == "REVIEW_FAILED" and len(s.turns.calls) == 1
    head = s.head(dispatch["dispatch_id"])
    assert head["state"] == "held" and head["data"]["hold_code"] == "REVIEW_FAILED"


def test_a_hook_that_outlives_the_deadline_is_abandoned(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    release = threading.Event()
    hooks = Hooks(["never sent"])
    hooks.on_turn = lambda turn, workspace: release.wait(timeout=60)
    started = time.monotonic()
    with pytest.raises(Hold) as late:  # the first turn takes well under the deadline
        s.execute(dispatch, base, hooks, deadline_seconds=10)
    release.set()
    assert late.value.code == "WORKER_DEADLINE" and time.monotonic() - started < 50
    assert [h["turn"] for h in hooks.seen] == [0]  # the hook had started when the deadline hit
    assert hooks.abandoned is True  # its late result is dropped (ReviewHooks.abandon)
    assert len(s.turns.calls) == 1


# =========================================================================================
# M3 rule 5 / §14 Q4: usage of a resumed session
# =========================================================================================
def run_usage(s: Setup, dispatch: dict[str, Any]) -> dict[str, Any]:
    run = s.d.store.head(s.d.scope, "run", dispatch["run_id"])
    usage: dict[str, Any] = run["data"]["record"]["usage"]
    return usage


def test_a_resumed_codex_session_keeps_the_last_receipts_usage_until_q4_is_settled(
    s: Setup,
) -> None:
    _goal, dispatch, base = s.claimed()
    s.turns.plan[0] = {"usage": [10, 5]}
    s.turns.plan[1] = {"usage": [30, 7]}
    s.execute(dispatch, base, Hooks(["more"]))
    usage = run_usage(s, dispatch)
    # not 40/12: whether a resumed Codex turn reports the thread's total is §14 Q4
    assert (usage["input_tokens"], usage["output_tokens"]) == (30, 7)


def test_a_run_without_follow_ups_reports_its_one_receipt(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    s.execute(dispatch, base, Hooks([None]))
    usage = run_usage(s, dispatch)
    assert (usage["input_tokens"], usage["output_tokens"]) == (10, 5)


def test_other_drivers_sum_the_turns_of_a_run() -> None:
    receipts = [
        {"usage": {"input_tokens": 10, "output_tokens": 5, "cost_microunits": 3,
                   "status": "measured"}},
        {"usage": {"input_tokens": 30, "output_tokens": 7, "cost_microunits": 4,
                   "status": "measured"}},
    ]  # fmt: skip
    total = sum_usage(receipts)["usage"]
    assert (total["input_tokens"], total["output_tokens"]) == (40, 12)
    assert (total["cost_microunits"], total["status"]) == (7, "measured")
    estimated = sum_usage([receipts[0], {"usage": {**receipts[1]["usage"], "status": "estimated"}}])
    assert estimated["usage"]["status"] == "estimated"
    unknown = sum_usage([receipts[0], {"usage": {"input_tokens": None, "output_tokens": None}}])
    assert unknown == UNKNOWN_USAGE  # one unknown turn makes the run's usage unknown
    one_cost = sum_usage(
        [receipts[0], {"usage": {**receipts[1]["usage"], "cost_microunits": None}}]
    )
    assert one_cost["usage"]["cost_microunits"] is None


# =========================================================================================
# §14 Q16 (a): an execution interrupted inside a follow-up is recovered, never re-sent
# =========================================================================================
class Crash(BaseException):
    """What a dead process leaves: no handler of ``execute`` (``except Exception``) runs."""


def test_an_execution_interrupted_inside_a_follow_up_is_recovered_and_never_re_sent(
    s: Setup,
) -> None:
    """§14 Q16 (a) as answered in S9b: the follow-up's process was started before the worker
    died, so its driver journal exists (not ``prepared``): the replay observes or collects it and
    the attempt goes on to ``output_ready``; the message is never sent a second time."""
    _goal, dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    original = port.resume

    def resume_then_die(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)  # the follow-up process is started ...
        raise Crash  # ... and the worker dies before it records the handle

    port.resume = resume_then_die
    hooks = Hooks(["more"])
    with pytest.raises(Crash):
        s.execute(dispatch, base, hooks)
    # what a restart finds: the execution row is not terminal, the follow-up is recorded, and the
    # journal of <dispatch_id>-f1 exists (the process was spawned)
    head = s.head(did)
    assert head["state"] == "observing" and head["data"]["followup_dispatch_id"] == did + "-f1"
    assert [e["dispatch_id"] for e in head["data"]["followups"]] == [did + "-f1"]
    journal = s.driver.journal.read(did + "-f1")["state"]
    assert journal in {"running", "completed", "starting"}
    sent = [c["name"] for c in s.turns.calls]
    assert sent == [did, did + "-f1"]
    port.resume = original
    # M3 rule 4 / §14 Q16 (a): the same request recovers the follow-up instead of replaying it
    result = s.execute(dispatch, base, Hooks(["more"]))
    assert result["status"] == "verifying"
    assert [c["name"] for c in s.turns.calls] == sent  # the message was sent exactly once
    done = s.head(did)
    assert done["state"] == "verifying"
    (recovery,) = done["data"]["recoveries"]
    assert recovery["dispatch_id"] == did + "-f1"
    assert recovery["action"] in {"observed", "collected"}  # never "resent": a turn had started
    (entry,) = done["data"]["followups"]
    assert entry["prompt_digest"] == digest("more") and entry["receipt_digest"] is not None
    run = s.d.store.head(s.d.scope, "run", dispatch["run_id"])
    assert run["state"] != "running"  # output_ready was reported once, from the recovered turn
    # a replay of the finished execution returns its stored result and sends nothing
    assert s.execute(dispatch, base, Hooks(["more"])) == result
    assert [c["name"] for c in s.turns.calls] == sent


def test_a_first_turn_interrupted_before_any_follow_up_is_held_the_same_way(s: Setup) -> None:
    _goal, dispatch, base = s.claimed()
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    original = port.poll

    def die(handle: str) -> Any:
        raise Crash

    port.poll = die
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks(["more"]))
    port.poll = original
    sent = len(s.turns.calls)
    with pytest.raises(Hold) as again:
        s.execute(dispatch, base, Hooks(["more"]))
    assert again.value.code == "EXECUTION_RECONCILE" and len(s.turns.calls) == sent


def test_a_crash_inside_a_follow_up_leaves_a_process_the_abort_path_can_stop(s: Setup) -> None:
    """``abort`` (the controller's way to stop what no pause took over) acts on the follow-up's
    handle, the last one the head recorded."""
    _goal, dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan[1] = {"sleep": 30}
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    original = port.poll
    state = {"hits": 0}

    def die_while_following_up(handle: str) -> Any:
        if handle.endswith("-f1"):
            state["hits"] += 1
            if state["hits"] >= 2:
                raise Crash
        return original(handle)

    port.poll = die_while_following_up
    with pytest.raises(Crash):
        s.execute(dispatch, base, Hooks(["more"]))
    port.poll = original
    head = s.head(did)
    assert head["state"] == "observing" and head["data"]["driver_handle"] == did + "-f1"
    assert s.coordinator.abort(s.worker, dispatch["run_id"]) is True
    held = s.head(did)
    assert held["state"] == "held" and held["data"]["hold_code"] == "CONTROLLER_ABORT"
    assert held["data"]["process_stopped"] is True
    assert s.driver.journal.read(did + "-f1")["state"] in {"cancelled", "paused", "failed"}


# =========================================================================================
# §14 Q16 (b): candidates (M4) only through vote hooks; what an unbound session would meet
# =========================================================================================
@pytest.mark.parametrize("candidates", [2, 3, 0])
def test_hooks_asking_for_candidates_are_refused_before_anything_starts(
    s: Setup, candidates: int
) -> None:
    """Since S9b ``vote`` runs, but only with vote hooks (``evaluate`` and ``select``, 1..3
    candidates, no follow-ups): these hooks ask for 2, 3 or 0 candidates without being a vote,
    so the worker refuses them (``WORKER_HOOKS``) before any row, workspace or process exists."""
    _goal, dispatch, base = s.claimed()
    hooks = Hooks([])
    hooks.candidates = candidates
    with pytest.raises(RuntimeFault) as refused:
        s.execute(dispatch, base, hooks)
    assert refused.value.code == "WORKER_HOOKS" and "Q16" not in refused.value.message
    assert s.turns.calls == []
    with pytest.raises(RuntimeFault) as no_head:
        s.head(dispatch["dispatch_id"])
    assert no_head.value.code == "NOT_FOUND"


def test_a_candidate_dispatch_of_a_running_run_cannot_go_through_execute(s: Setup) -> None:
    """Q16 (b): candidate i of a vote run would be another dispatch of the run's lease while the
    run is live (here: during the first turn's hook). ``execute`` materializes the workspace at
    the run id, which the first turn holds, so it holds ``WORKSPACE_EXISTS`` and starts no process;
    it has already written its worker-execution row (state ``preparing``), which is not terminal.
    M4 therefore needs its own scratch workspaces, as §5.1 says, and a decision on such rows."""
    _goal, dispatch, base = s.claimed()
    candidate = json.loads(json.dumps(dispatch))
    candidate["dispatch_id"] = dispatch["dispatch_id"] + "-c1"
    seen: dict[str, Any] = {}

    def candidate_turn(turn: int, workspace: Path) -> None:
        try:
            s.coordinator.execute(
                s.worker, candidate, prompt="Make value() return 2.", base_snapshot=base,
                output_paths={PORT: PATCH_BINDING},
            )  # fmt: skip
        except (Hold, RuntimeFault, Conflict) as exc:
            seen["code"] = exc.code
        seen["started"] = [c["name"] for c in s.turns.calls]

    hooks = Hooks([None], followups=1)
    hooks.on_turn = candidate_turn
    s.execute(dispatch, base, hooks)
    assert seen["code"] == "WORKSPACE_EXISTS"
    assert seen["started"] == [dispatch["dispatch_id"]]  # no candidate process was started
    stuck = s.head(candidate["dispatch_id"])
    assert stuck["state"] == "preparing" and stuck["data"]["run_id"] == dispatch["run_id"]
    # the run itself went on to verification: the failed candidate did not touch it
    assert s.head(dispatch["dispatch_id"])["state"] == "verifying"
    # but the run now has two durable executions, which the steering and abort paths refuse
    with pytest.raises(Hold) as stop:
        s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
    assert stop.value.code == "WORKER_SESSION_MISSING"
    with pytest.raises(Hold) as abort:
        s.coordinator.abort(s.worker, dispatch["run_id"])
    assert abort.value.code == "SESSION_LOOKUP"


def test_the_stop_and_abort_paths_need_exactly_one_durable_execution_per_run(s: Setup) -> None:
    """Q16 (b): ``stop_and_snapshot`` (steering pause) and ``abort`` read the one worker-execution
    row of a run. A candidate execution row of the same run (M4 rule: journals listed in the head
    under ``candidates`` is the spec; a separate row would be the alternative) makes both hold."""
    _goal, dispatch, base = s.claimed()
    s.execute(dispatch, base, Hooks([None]))
    did = dispatch["dispatch_id"]
    head = s.head(did)
    twin = f"{did}-c1"
    with s.d.store.tx() as db:  # a second worker-execution row for the same run
        s.d.store.cas(db, s.d.scope, "worker-execution", twin, 0, "observing",
                      {**head["data"], "driver_handle": twin})  # fmt: skip
    with pytest.raises(Hold) as stop:
        s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
    assert stop.value.code == "WORKER_SESSION_MISSING"
    with pytest.raises(Hold) as abort:
        s.coordinator.abort(s.worker, dispatch["run_id"])
    assert abort.value.code == "SESSION_LOOKUP"


def test_a_steering_pause_stops_the_follow_up_handle_not_the_first_turn(s: Setup) -> None:
    """The steering path (``stop_and_snapshot`` -> ``port.pause`` -> ``checkpoint``) acts on
    ``driver_handle``, which a follow-up moves to its own handle: a pause during a follow-up
    pauses that process. Resuming it is the controller's ``resume_exact``, which re-sends the
    run's first dispatch with a ``resume-<digest>`` id; it does not know the follow-up (§14 Q16)."""
    _goal, dispatch, base = s.claimed()
    did = dispatch["dispatch_id"]
    s.turns.plan[1] = {"sleep": 30}
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            s.execute(dispatch, base, Hooks(["more"]))
        except BaseException as exc:  # recorded for the assertions
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:  # wait until the follow-up process has its session
        try:
            if s.head(did)["data"].get("driver_handle") == did + "-f1" and s.driver.journal.read(
                did + "-f1"
            ).get("session_handle"):
                break
        except RuntimeFault:
            pass
        time.sleep(0.05)
    assert s.head(did)["data"]["driver_handle"] == did + "-f1"
    paused = s.coordinator.stop_and_snapshot(s.worker, dispatch["run_id"], "pause")
    thread.join(timeout=20)
    assert paused["process_stopped"] is True and paused["session_handle"] == "thread_" + did
    head = s.head(did)
    assert head["state"] == "paused"  # the execute call left its paused state in place
    assert head["data"]["driver_checkpoint"]["dispatch_id"] == did + "-f1"
    assert s.driver.journal.read(did + "-f1")["state"] == "paused"
    assert s.driver.journal.read(did)["state"] == "completed"  # turn 1 was not touched
    assert isinstance(box.get("error"), Hold)  # DRIVER_BOUNDARY: the stream was stopped under it


def start_candidate(
    s: Setup, dispatch: dict[str, Any], base: dict[str, Any], i: int
) -> tuple[Any, str, Path]:
    """Candidate ``i`` as the M4 rules of §5.1 describe it (M4 itself is not built): dispatch
    ``<dispatch_id>-c<i>``, a scratch workspace materialized from the run's base, its own native
    session, nothing bound to the run. Returns once its stream has named that session."""
    port = s.coordinator.registry.resolve(
        s.d.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
    )
    candidate = json.loads(json.dumps(dispatch))
    candidate["dispatch_id"] = f"{dispatch['dispatch_id']}-c{i}"
    workspace = s.coordinator.workspaces.materialize(s.d.scope, candidate["dispatch_id"], base)
    handle = port.start(port.prepare(candidate, "Make value() return 2.", workspace))
    deadline = time.monotonic() + 20
    while not s.driver.journal.read(handle).get("session_handle"):
        assert time.monotonic() < deadline, "the candidate never named its session"
        time.sleep(0.05)
    return port, handle, workspace


def test_a_pause_during_candidate_i_stops_candidate_0_and_leaves_candidate_i_running(
    s: Setup,
) -> None:
    """Q16 (b), the M4 layout of §5.1 (candidate journals listed beside the head, the run's
    ``driver_handle`` stays candidate 0's): a steering pause while candidate i runs.

    ``stop_and_snapshot`` (what ``SteeringService.quiesce`` calls; called directly here, as in the
    follow-up pause test) acts on ``driver_handle`` only: it pauses candidate 0's finished turn,
    returns the bound session, so the controller's session check (``steering.py:191``) passes and
    the pause counts as confirmed, while candidate i's process keeps running."""
    _goal, dispatch, base = s.claimed()
    did, run_id = dispatch["dispatch_id"], dispatch["run_id"]
    s.turns.plan[1] = {"sleep": 30}  # process 1 is candidate 1
    seen: dict[str, Any] = {}

    def candidate_during_the_attempt(turn: int, workspace: Path) -> None:
        port, handle, _scratch = start_candidate(s, dispatch, base, 1)
        try:
            seen["paused"] = s.coordinator.stop_and_snapshot(s.worker, run_id, "pause")
            seen["head"] = s.head(did)
            seen["candidate"] = s.driver.journal.read(handle)
            seen["first"] = s.driver.journal.read(did)
            seen["run"] = s.d.store.head(s.d.scope, "run", run_id)["data"]
        finally:
            port.cancel(handle)  # what nothing on the steering path does

    hooks = Hooks([None], followups=1)
    hooks.on_turn = candidate_during_the_attempt
    s.execute(dispatch, base, hooks)
    paused = seen["paused"]
    assert paused["process_stopped"] is True
    # the bound session (candidate 0's): the controller would accept this checkpoint
    assert paused["session_handle"] == "thread_" + did == seen["run"]["session_handle"]
    head = seen["head"]
    assert head["state"] == "paused" and head["data"]["driver_checkpoint"]["dispatch_id"] == did
    assert seen["first"]["state"] == "paused"  # candidate 0's completed turn, relabelled
    # candidate i was not stopped: its process ran on through a "confirmed" pause
    assert seen["candidate"]["state"] == "running"
    assert seen["candidate"]["process_stopped"] is False
    assert seen["candidate"]["session_handle"] == f"thread_{did}-c1"


def test_a_pause_of_candidate_i_as_the_runs_handle_names_an_unbound_session_and_workspace(
    s: Setup,
) -> None:
    """Q16 (b), the alternative layout (candidate i's handle written as the run's
    ``driver_handle``, as a follow-up does): the pause stops candidate i, but its checkpoint names
    candidate i's own session, which is not the run's (``steering.py:191`` holds
    ``CHECKPOINT_SESSION``), and the controller's snapshot is taken from the run workspace while
    the driver checkpoint names the scratch workspace, so a resume of it holds
    ``RESUME_WORKSPACE`` (``cli.py`` resume)."""
    _goal, dispatch, base = s.claimed()
    did, run_id = dispatch["dispatch_id"], dispatch["run_id"]
    s.turns.plan[1] = {"sleep": 30}
    seen: dict[str, Any] = {}

    def candidate_as_the_handle(turn: int, workspace: Path) -> None:
        port, handle, scratch = start_candidate(s, dispatch, base, 1)
        s.coordinator._update(s.worker, did, "observing", driver_handle=handle)
        try:
            seen["paused"] = s.coordinator.stop_and_snapshot(s.worker, run_id, "pause")
            seen["head"] = s.head(did)
            seen["candidate"] = s.driver.journal.read(handle)
            seen["run"] = s.d.store.head(s.d.scope, "run", run_id)["data"]
            seen["scratch"], seen["run_workspace"] = scratch, workspace
            follow = json.loads(json.dumps(dispatch))
            follow["dispatch_id"] = "resume-candidate-check"
            try:
                port.resume(follow, "steer", workspace, seen["head"]["data"]["driver_checkpoint"])
            except Hold as exc:
                seen["resume"] = exc.code
        finally:
            port.cancel(handle)

    hooks = Hooks([None], followups=1)
    hooks.on_turn = candidate_as_the_handle
    s.execute(dispatch, base, hooks)
    paused = seen["paused"]
    assert paused["process_stopped"] is True
    assert seen["candidate"]["state"] == "paused" and seen["candidate"]["process_stopped"] is True
    assert paused["session_handle"] == f"thread_{did}-c1" != seen["run"]["session_handle"]
    checkpoint = seen["head"]["data"]["driver_checkpoint"]
    assert seen["head"]["data"]["workspace"] == str(seen["run_workspace"])  # snapshot source
    assert checkpoint["workspace"] == str(seen["scratch"]) != seen["head"]["data"]["workspace"]
    assert seen["resume"] == "RESUME_WORKSPACE"


# =========================================================================================
# the auxiliary-turn ledger (IC-21 AUX_BUDGET)
# =========================================================================================
def entry(tokens: int | None, role: str = "reviewer") -> dict[str, Any]:
    return {"role": role, "purpose": "review", "tokens": tokens}


def test_the_ledger_allows_a_turn_below_the_cap_and_holds_at_it() -> None:
    ledger = AuxLedger(100)
    ledger.check("reviewer")  # nothing spent
    ledger.add(entry(99))
    ledger.check("reviewer")  # below the cap
    ledger.add(entry(1))
    with pytest.raises(Hold) as at_cap:
        ledger.check("reviewer")
    assert at_cap.value.code == "AUX_BUDGET"
    assert at_cap.value.details == {"role": "reviewer", "cap": 100, "tokens": 100,
                                    "unknown_usage": False}  # fmt: skip
    assert ledger.overrun is False  # exactly the cap is not an overrun


def test_the_last_turn_may_pass_the_cap_by_its_own_usage_and_the_overrun_is_recorded() -> None:
    ledger = AuxLedger(100)
    ledger.check("reviewer")
    ledger.add(entry(150))  # a read-only turn has no ceiling of its own
    assert ledger.overrun is True and ledger.totals() == (150, False)
    with pytest.raises(Hold) as held:
        ledger.check("investigator")
    assert held.value.code == "AUX_BUDGET" and held.value.details["role"] == "investigator"


def test_a_cap_of_zero_holds_the_first_turn() -> None:
    with pytest.raises(Hold) as held:
        AuxLedger(0).check("planner")
    assert held.value.code == "AUX_BUDGET" and held.value.details["cap"] == 0


def test_a_turn_with_unknown_usage_stops_every_later_turn_whatever_the_cap() -> None:
    ledger = AuxLedger(10_000_000)
    ledger.add(entry(5))
    ledger.add(entry(None))
    assert ledger.totals() == (5, True)
    with pytest.raises(Hold) as held:
        ledger.check("reviewer")
    assert held.value.code == "AUX_BUDGET" and held.value.details["unknown_usage"] is True


def test_the_ledger_starts_from_the_recorded_entries_and_copies_them() -> None:
    recorded = [entry(40), entry(60)]
    ledger = AuxLedger(100, recorded)
    assert ledger.totals() == (100, False)
    ledger.add(entry(1))
    assert len(recorded) == 2  # the plan record's list is not the ledger's
    with pytest.raises(Hold):
        ledger.check("reviewer")


def test_the_ledger_is_safe_for_the_parallel_investigators() -> None:
    ledger = AuxLedger(10**9)
    threads = [threading.Thread(target=lambda: [ledger.add(entry(1)) for _ in range(200)])
               for _ in range(8)]  # fmt: skip
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.totals() == (1600, False) and len(ledger.entries) == 1600


@pytest.mark.parametrize(
    ("usage", "tokens"),
    [
        ({"input_tokens": 3, "output_tokens": 4}, 7),
        ({"input_tokens": 0, "output_tokens": 0}, 0),
        ({"input_tokens": 3}, None),
        ({"input_tokens": 3, "output_tokens": None}, None),
        ({"input_tokens": True, "output_tokens": 4}, None),  # a bool is not a count
        ({"input_tokens": -1, "output_tokens": 4}, None),
        ({"input_tokens": 3.5, "output_tokens": 4}, None),
        (None, None),
        ("n/a", None),
    ],
)
def test_a_turns_tokens_are_input_plus_output_or_unknown(usage: Any, tokens: int | None) -> None:
    assert usage_tokens(usage) == tokens


def test_review_hooks_allow_one_or_two_rounds_only() -> None:
    runner: Any = object.__new__(StrategyRunner)
    for rounds in (0, 3, -1):
        with pytest.raises(RuntimeFault) as refused:
            ReviewHooks(runner, "goal", {"node_id": "n"}, cell_id="codex-cli", max_rounds=rounds)
        assert refused.value.code == "COMPONENT_CONTENT"
    hooks = ReviewHooks(runner, "goal", {"node_id": "n"}, cell_id="codex-cli", max_rounds=2)
    assert (hooks.max_followups, hooks.candidates, hooks.rounds, hooks.sent) == (2, 1, 0, 0)
    assert hooks.select([CandidateResult(0, 1, {}, None)]) == 0
    assert (
        hooks.spec_digest()
        == ReviewHooks(
            runner, "other-goal", {"node_id": "other"}, cell_id="codex-cli", max_rounds=2
        ).spec_digest()
    )  # the digest names the strategy, reviewer cell and rounds, not the goal
    assert (
        hooks.spec_digest()
        != ReviewHooks(
            runner, "goal", {"node_id": "n"}, cell_id="claude-cli", max_rounds=2
        ).spec_digest()
    )
    assert (
        hooks.spec_digest()
        != ReviewHooks(
            runner, "goal", {"node_id": "n"}, cell_id="codex-cli", max_rounds=1
        ).spec_digest()
    )
    assert MAX_FOLLOWUPS == 2
