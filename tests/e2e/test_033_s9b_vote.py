"""Work 033 S9b: `vote` (M4 candidates) end to end through the execution loop.

Contract: specs/033-harness-taxonomy/interfaces.md §5.1 M4 and its rules, §5.2 strategy 10
(`vote`), §5.3 (usage of separate sessions is summed), §14 Q16 (b), "Clarifications After S9 And
S11" (S9b), plan.md §8 (vote).

Real: the local execution product and loop of the rc06 rig (store, runtime, verification,
workspaces, operator approval, steering), `ManifestService.materialize` for candidate
compositions, the real `VoteHooks` (the app's quick verifier `check` is the host fast check) and
the Codex-shaped CLI port with its credential seeding. Stand-in: the "container" is a host script
per turn that writes the files a per-role plan asks for (candidate 0 is the run's own turn,
`c1`/`c2` are candidates). No docker, provider, network or credential.

Covered: vote is no longer held before the claim; k candidates one after another (k <= 3) from
the same base, the host fast checks decide (most passing checks, smallest diff, lowest index) and
the selected patch is the run's change before `output_ready`; the suite runs once on it (one
attempt, the checks never give a verdict); 80 % of the node budget stops generating; a candidate
that does not complete is left out; candidate sessions are released (no credential at rest,
scratch workspaces removed); a steering pause during candidate i stops it and is taken over on
the bound session (the vote is dropped); the strategy metrics count candidates; `repair_loop` and
its records are unchanged (G1); negative cases (no quick verifiers, k out of range, fast_checks
beside vote).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.execution.product import AppConfig
from e2e.test_033_s3_context import TOOLS, Components, node_budget, pinned, setup
from rc06_rig import with_permissions

GOOD = "def value():\n    return 2\n"
WRONG = "def value():\n    return 3\n"
BASE = "def value():\n    return 1\n"

AGENT = r"""
import json, pathlib, sys, time
ws, spec, home = pathlib.Path(sys.argv[1]), json.loads(sys.argv[2]), pathlib.Path(sys.argv[3])
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
    (ws / rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(text)
(home / ".codex" / "auth.json").write_text('{"tokens": "refreshed"}')
emit({"type": "thread.started", "thread_id": spec["session"]})
done = {"type": "turn.completed"}
if spec["usage"] is not None:
    done["usage"] = {"input_tokens": spec["usage"][0], "output_tokens": spec["usage"][1]}
emit(done)
"""


class Agent:
    """One process per turn; ``plan`` is keyed by role: ``run`` (candidate 0), ``resume`` (a steered
    turn), ``c1``, ``c2`` (candidates)."""

    def __init__(self, container: Any) -> None:
        self.container = container
        self.plan: dict[str, dict[str, Any]] = {}
        self.turns: list[dict[str, Any]] = []
        container.command = self.command

    def command(self, argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        tail = run_name.rsplit("-", 1)[-1]
        resume = len(argv) > 5 and argv[4] == "resume"
        role = "resume" if resume else tail if tail in {"c1", "c2"} else "run"
        app = workspace / "app.py"
        self.turns.append(
            {"name": run_name, "role": role, "resume": resume, "prompt": argv[-1],
             "workspace": workspace, "base_at_start": app.read_text() if app.exists() else None}
        )  # fmt: skip
        self.container.prompts.append(argv[-1])
        spec = {
            "writes": {"app.py": GOOD},
            "session": argv[5] if resume else "thread_" + run_name,
            "usage": [10, 5],
            **self.plan.get(role, {}),
        }
        return [
            sys.executable, "-c", AGENT, str(workspace), json.dumps(spec), str(kw["native_home"])
        ]  # fmt: skip

    def roles(self) -> list[str]:
        return [t["role"] for t in self.turns]


def vote_world(
    deployment: Any, tmp_path: Path, k: int = 2, **components: Any
) -> tuple[Any, Any, Any, Components, Agent, str]:
    rig, loop, container, comps = setup(deployment, tmp_path, "right")
    agent = Agent(container)
    composition = comps.candidate(
        execution_strategy={"enabled": ["vote"], "params": {"vote": {"k": k}}}, **components
    )
    goal = pinned(rig, composition, f"(vote k={k}) make value return 2")
    return rig, loop, container, comps, agent, goal


def metrics(rig: Any, goal: str) -> dict[str, Any]:
    value: dict[str, Any] = rig.service.plan_record(goal)["strategy_metrics"]
    return value


def journal_auth(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "journal").rglob("auth.json"))


# =========================================================================================
# vote runs: end to end
# =========================================================================================
def test_vote_is_no_longer_held_before_the_claim(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container, _c, agent, goal = vote_world(deployment, tmp_path)
    plan = rig.service.plan_record(goal)
    assert plan["strategy"]["strategy"] == "vote" and plan["strategy"]["refused"] is None
    assert plan["strategy"]["used_prior"] is False
    loop._policies(plan)  # nothing is refused (it raised COMPONENT_CONTENT before S9b)
    record = loop.run_goal(goal)
    assert record["status"] in {"verified", "published"}
    assert record["status"] != "held" and agent.turns  # a claim happened and candidates ran


def test_the_host_fast_checks_pick_the_candidate_and_the_suite_runs_once(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _container, _c, agent, goal = vote_world(deployment, tmp_path, k=3)
    agent.plan["run"] = {"writes": {"app.py": WRONG}}  # candidate 0 fails the quick check
    agent.plan["c1"] = {"writes": {"app.py": GOOD, "extra.txt": "x" * 400}}  # passes, larger
    agent.plan["c2"] = {"writes": {"app.py": GOOD}}  # passes, smaller: wins
    record = loop.run_goal(goal)
    assert record["status"] in {"verified", "published"}
    (attempt,) = record["attempts"]  # one attempt: the vote is inside it
    assert attempt["outcome"] == "pass" and attempt["candidates"] == 3 and attempt["selected"] == 2
    results = {r["index"]: r for r in attempt["candidate_results"]}
    assert [results[i]["fast_checks"] for i in range(3)] == [
        {"check": False}, {"check": True}, {"check": True},
    ]  # fmt: skip
    assert results[2]["patch_lines"] < results[1]["patch_lines"]
    assert all(results[i]["state"] in {"collected", "released"} for i in range(3))
    # the suite verified the selected change once: one verdict per acceptance, outcome pass
    assert [v["outcome"] for v in attempt["verdicts"]] == ["pass"]
    change = json.dumps(attempt["change"])
    assert "extra.txt" not in change and "return 3" not in change
    # one attempt only: vote fixes the node's attempts at 1
    graph = rig.d.store.get(rig.d.scope, "workgraph", rig.service.plan_record(goal)["graph_ref"])
    assert graph["nodes"][0]["budget"]["max_attempts"] == 1
    assert agent.roles() == ["run", "c1", "c2"]


def test_candidates_run_one_after_another_from_the_same_base(
    deployment: Any, tmp_path: Path
) -> None:
    _rig, loop, _container, _c, agent, goal = vote_world(deployment, tmp_path, k=3)
    agent.plan["run"] = {"writes": {"app.py": WRONG}}
    loop.run_goal(goal)
    assert [t["base_at_start"] for t in agent.turns] == [BASE, BASE, BASE]
    assert len({str(t["workspace"]) for t in agent.turns}) == 3  # candidates have own workspaces
    assert [t["prompt"] for t in agent.turns][1:] == [agent.turns[0]["prompt"]] * 2  # same task
    assert agent.turns[0]["prompt"].strip()
    # no candidate session was resumed and nothing was published from a candidate scratch tree
    assert not any(t["resume"] for t in agent.turns)
    # scratch workspaces are gone, no credential rests in any session home
    for turn in agent.turns[1:]:
        assert not Path(turn["workspace"]).exists()
    assert journal_auth(tmp_path) == []


def test_candidate_sessions_are_released_and_the_run_keeps_one_bound_session(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container, _c, _agent, goal = vote_world(deployment, tmp_path, k=2)
    record = loop.run_goal(goal)
    assert record["status"] in {"verified", "published"}
    with rig.d.store._lock:
        sessions = rig.d.store.conn.execute(
            "SELECT id FROM heads WHERE tenant=? AND project=? AND kind='driver-session'",
            rig.d.scope.keys(),
        ).fetchall()
    assert len(sessions) == 1  # candidate 1 has no driver-session row
    assert journal_auth(tmp_path) == []
    # every spawned process is over
    assert all(p.poll() is not None for p in container.driver.processes.values())


def test_the_strategy_metrics_count_candidates(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container, _c, _agent, goal = vote_world(deployment, tmp_path, k=3)
    loop.run_goal(goal)
    m = metrics(rig, goal)
    assert m["strategy"] == "vote"
    assert (m["candidates"], m["candidate_turns"]) == (3, 2)
    # agent_calls adds the plan-time planner turn that reported usage (strategy_runner metrics)
    assert (m["turns"], m["agent_calls"], m["attempts_used"], m["followups"]) == (3, 4, 1, 0)
    assert m["vote_selected"] in ([0], [1], [2])
    assert m["aux_turns"] == 0 and m["reviewer_rounds"] == 0 and m["escalations"] == 0


def test_a_vote_trial_row_counts_every_candidate_as_an_agent_call(
    deployment: Any, tmp_path: Path
) -> None:
    """The §2.10 trial row of a vote goal: ``agent_calls`` equals the strategy record's (each
    candidate is its own process, §5.2 strategy 10), as ``turns`` already does."""
    from amplai_foundry.meta_harness.trial_metrics import TrialMetrics
    from amplai_foundry.runtime.contracts.identity import canonical

    rig, loop, _container, _c, agent, goal = vote_world(deployment, tmp_path, k=3)
    record = loop.run_goal(goal)
    assert agent.roles() == ["run", "c1", "c2"]
    plan = rig.service.plan_record(goal)
    m = plan["strategy_metrics"]
    # the receipt the trial executor admits names the goal; the planner counts in both records
    receipt = {
        "goal_id": goal, "goal_status": record["status"],
        "planner": {"mode": "real" if plan.get("planner_usage") is not None else "fixed"},
    }  # fmt: skip
    ref = rig.d.artifacts.admit(rig.d.scope, canonical(receipt), "application/json",
                                trust="verifier")  # fmt: skip
    trial = {
        "trial_id": "trial-vote", "task_id": "vote", "task_class": "bug", "arm": "candidate",
        "success": True, "artifact_refs": [ref], "input_tokens": None, "output_tokens": None,
        "elapsed_ms": 1000.0, "split": "development",
    }  # fmt: skip
    row = TrialMetrics(rig.service, tables=[]).trial(trial)
    assert row["turns"] == m["turns"] == 3
    assert row["agent_calls"] == m["agent_calls"]


def test_the_run_usage_sums_every_candidate_turn(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container, _c, agent, goal = vote_world(deployment, tmp_path, k=2)
    agent.plan["run"] = {"usage": [10, 5]}
    agent.plan["c1"] = {"usage": [20, 7]}
    record = loop.run_goal(goal)
    run = rig.d.store.head(rig.d.scope, "run", record["attempts"][0]["run_id"])
    usage = run["data"]["record"]["usage"]
    assert (usage["input_tokens"], usage["output_tokens"]) == (30, 12)  # separate sessions: summed


# =========================================================================================
# the 80 % stop, a candidate that does not complete
# =========================================================================================
def test_generation_stops_at_80_percent_of_the_node_budget(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container, _c, agent, goal = vote_world(deployment, tmp_path, k=3)
    plan = rig.service.plan_record(goal)
    cap = node_budget(rig, plan)["max_tokens"]
    agent.plan["run"] = {"usage": [int(cap * 0.8), 0]}
    record = loop.run_goal(goal)
    assert record["status"] in {"verified", "published"}
    (attempt,) = record["attempts"]
    assert attempt["candidates"] == 1 and attempt["selected"] == 0
    assert attempt["candidates_stopped"] == {
        "before": 1, "tokens": int(cap * 0.8), "node_max_tokens": cap,
    }  # fmt: skip
    assert agent.roles() == ["run"]  # no candidate process
    m = metrics(rig, goal)
    assert (m["candidates"], m["candidate_turns"], m["turns"]) == (1, 0, 1)


def test_a_candidate_that_does_not_complete_is_left_out_and_the_vote_goes_on(
    deployment: Any, tmp_path: Path
) -> None:
    _rig, loop, _container, _c, agent, goal = vote_world(deployment, tmp_path, k=2)
    agent.plan["run"] = {"writes": {"app.py": WRONG}}
    agent.plan["c1"] = {"exit": 3}
    record = loop.run_goal(goal)
    (attempt,) = record["attempts"]
    states = {r["index"]: r["state"] for r in attempt["candidate_results"]}
    assert states == {0: "collected", 1: "failed"}
    assert attempt["selected"] == 0  # the only evaluated candidate; the suite then fails it
    assert attempt["outcome"] == "fail"  # the suite, not the fast check, decided
    assert attempt["candidate_results"][1]["error"] == "DRIVER_BOUNDARY"
    assert journal_auth(tmp_path) == []


def test_a_candidate_launched_without_a_recorded_handle_still_counts_as_a_turn(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``port.start`` of candidate 1 spawned its process and then raised, so no handle was
    recorded: the candidate was launched (``launched`` recorded before ``port.start``), so the
    attempt's ``candidates`` and the strategy metrics count its turn (no undercount)."""
    from amplai_foundry.agent_drivers.ports import CliPort

    real = CliPort.start

    def start_then_lose_the_handle(self: Any, prepared: dict[str, Any]) -> str:
        handle: str = real(self, prepared)
        if str(prepared.get("dispatch_id", "")).endswith("-c1"):
            raise RuntimeFault("DRIVER_BOUNDARY", "the start acknowledgement was lost")
        return handle

    monkeypatch.setattr(CliPort, "start", start_then_lose_the_handle)
    rig, loop, container, _c, _agent, goal = vote_world(deployment, tmp_path, k=2)
    record = loop.run_goal(goal)
    (attempt,) = record["attempts"]
    results = {r["index"]: r for r in attempt["candidate_results"]}
    assert results[1]["state"] == "failed" and results[1]["error"] == "DRIVER_BOUNDARY"
    assert attempt["candidates"] == 2 and attempt["selected"] == 0
    m = metrics(rig, goal)
    assert (m["candidates"], m["candidate_turns"]) == (2, 1)
    assert m["turns"] == 2
    assert all(p.poll() is not None for p in container.driver.processes.values())
    assert journal_auth(tmp_path) == []


def test_when_every_candidate_fails_the_suite_the_attempt_fails_it_is_not_retried(
    deployment: Any, tmp_path: Path
) -> None:
    """The fast checks never give a verdict, and a vote has one attempt: the suite decides."""
    rig, loop, _container, _c, agent, goal = vote_world(deployment, tmp_path, k=2)
    agent.plan["run"] = {"writes": {"app.py": WRONG}}
    agent.plan["c1"] = {"writes": {"app.py": WRONG}}
    record = loop.run_goal(goal)
    (attempt,) = record["attempts"]
    assert attempt["outcome"] == "fail" and attempt["selected"] == 0  # lowest index on a tie
    assert record["status"] == "failed" and rig.published == []
    assert agent.roles() == ["run", "c1"]  # no second attempt
    assert metrics(rig, goal)["attempts_used"] == 1


# =========================================================================================
# steering pause during candidate i (§14 Q16 (b))
# =========================================================================================
def wait_for(predicate: Any, what: str, seconds: float = 30.0) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(what)


def test_a_steering_pause_during_candidate_one_is_taken_over_on_the_bound_session(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container, _c, agent, goal = vote_world(deployment, tmp_path, k=2)
    agent.plan["c1"] = {"sleep": 60}
    agent.plan["run"] = {"writes": {"app.py": WRONG}}
    operator = with_permissions(rig.operator, "goal.steer")
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)), daemon=True)
    runner.start()
    wait_for(lambda: "c1" in agent.roles(), "candidate 1 never started")
    started = time.time()
    assert loop.steer(operator, goal, "value() must return 2")["status"] == "steering"
    runner.join(60)
    assert not runner.is_alive() and time.time() - started < 50  # not candidate 1's 60 s turn
    assert result["status"] in {"verified", "published"}
    (attempt,) = result["attempts"]
    assert attempt["outcome"] == "pass" and attempt["steered"] is True
    # the vote was dropped: nothing was selected, and the steered turn resumed the BOUND session
    # (candidate 0's thread) with the operator's message, in the run workspace
    assert attempt.get("selected") is None
    resumed = [t for t in agent.turns if t["resume"]]
    assert len(resumed) == 1 and "Operator steering" in resumed[0]["prompt"]
    assert "value() must return 2" in resumed[0]["prompt"]
    assert resumed[0]["role"] == "resume"
    states = {r["index"]: r["state"] for r in attempt["candidate_results"]}
    assert states[1] == "stopped"
    assert attempt["candidate_results"][1]["error"] == "EXECUTION_PAUSED"
    assert journal_auth(tmp_path) == []  # candidate 1's credential was released by its stop
    assert not Path(agent.turns[1]["workspace"]).exists()
    assert all(p.poll() is not None for p in container.driver.processes.values())
    assert [s["text"] for s in rig.service.plan_record(goal)["steering"]] == [
        "value() must return 2"
    ]


def test_a_pause_nobody_takes_over_stops_every_process_of_a_vote(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container, _c, agent, goal = vote_world(deployment, tmp_path, k=2)
    agent.plan["c1"] = {"sleep": 60}
    operator = with_permissions(rig.operator, "goal.steer")
    result: dict[str, Any] = {}
    runner = threading.Thread(target=lambda: result.update(loop.run_goal(goal)), daemon=True)
    runner.start()
    wait_for(lambda: "c1" in agent.roles(), "candidate 1 never started")
    d = rig.d
    # a pause the loop did not request (another client of the runtime)
    loop.steering.receive(
        operator, goal, "pause", "from elsewhere", key="other-client",
        expected_contract_ref=d.store.head(d.scope, "goal", goal)["data"]["active_contract_ref"],
    )  # fmt: skip
    runner.join(40)
    assert not runner.is_alive()
    assert result["status"] == "held"
    assert [a["outcome"] for a in result["attempts"]] == ["driver_failed"]
    assert all(p.poll() is not None for p in container.driver.processes.values())
    assert journal_auth(tmp_path) == []


# =========================================================================================
# negative cases: refused or ineligible
# =========================================================================================
@pytest.mark.parametrize("k", [1, 4, 0])
def test_vote_k_outside_two_to_three_is_refused(deployment: Any, tmp_path: Path, k: int) -> None:
    _rig, _loop, _container, comps = setup(deployment, tmp_path, "right")
    with pytest.raises(RuntimeFault) as bad:
        comps.candidate(execution_strategy={"enabled": ["vote"], "params": {"vote": {"k": k}}})
    assert bad.value.code == "COMPONENT_CONTENT"


def test_vote_without_quick_verifiers_runs_the_prior_for_a_real_goal(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container, _comps = setup(deployment, tmp_path, "right")
    installed = rig.service.apps["app"]
    rig.service.install(
        AppConfig("app", rig.repo, installed.config.verifiers, tool_versions=TOOLS)
    )  # no quick verifiers: nothing but the diff size would decide the vote
    comps = Components(rig)
    agent = Agent(container)
    composition = comps.candidate(
        execution_strategy={"enabled": ["vote"], "params": {"vote": {"k": 2}}}
    )
    goal = pinned(rig, composition, "(no quick verifiers) make value return 2")
    plan = rig.service.plan_record(goal)
    assert plan["strategy"]["declared"] == "vote" and plan["strategy"]["used_prior"] is True
    assert plan["strategy"]["strategy"] == "repair_loop"
    assert (
        plan["strategy"]["refused"] is None
    )  # ineligible, not refused: a real goal runs the prior
    assert plan["strategy"]["eligibility"]["vote"].startswith("vote needs quick_verifiers")
    record = loop.run_goal(goal)
    assert record["status"] in {"verified", "published"}
    assert agent.roles() == ["run"]  # the prior ran one turn; no candidate
    assert metrics(rig, goal)["candidates"] == 0


def test_fast_checks_beside_vote_are_held_before_any_claim(deployment: Any, tmp_path: Path) -> None:
    """One hook per attempt: the vote's own hook decides, L7 fast checks are not stacked."""
    rig, loop, container, _c = setup(deployment, tmp_path, "right")
    comps = Components(rig)
    agent = Agent(container)
    composition = comps.candidate(
        execution_strategy={"enabled": ["vote"], "params": {"vote": {"k": 2}}},
        fast_checks={"enabled": True, "checks": ["check"]},
    )
    goal = pinned(rig, composition, "(vote + fast checks) make value return 2")
    record = loop.run_goal(goal)
    assert record["status"] == "held" and record["attempts"] == []
    assert "COMPONENT_CONTENT" in record["reason"] and "vote" in record["reason"]
    assert agent.turns == [] and rig.published == []
    assert rig.d.store.head(rig.d.scope, "goal", goal)["state"] == "failed"


# =========================================================================================
# repair_loop and G1 unchanged
# =========================================================================================
def test_repair_loop_is_unchanged_by_vote(deployment: Any, tmp_path: Path) -> None:
    """The v1 default (`repair_loop`): attempts on the previous patch, no candidate fields in any
    record, no candidate in the metrics; the golden G1 test file pins the prompt bytes."""
    rig, loop, container = _wrong_first(deployment, tmp_path)
    goal = _approved(rig)
    record = loop.run_goal(goal)
    assert record["status"] in {"verified", "published"}
    assert [a["outcome"] for a in record["attempts"]] == ["fail", "pass"]
    assert all("candidates" not in a and "selected" not in a for a in record["attempts"])
    assert all("candidate_results" not in a for a in record["attempts"])
    m = metrics(rig, goal)
    assert m["strategy"] == "repair_loop" and m["attempts_used"] == 2
    assert (m["candidates"], m["candidate_turns"], m["vote_selected"]) == (0, 0, [])
    assert (m["turns"], m["agent_calls"], m["followups"]) == (2, 3, 0)  # + the planner turn
    assert len(container.prompts) == 2


def _wrong_first(deployment: Any, tmp_path: Path) -> tuple[Any, Any, Any]:
    from rc06_rig import rig_with_codex

    return rig_with_codex(deployment, tmp_path, "wrong-first")


def _approved(rig: Any) -> str:
    from rc06_rig import approved

    return approved(rig)
