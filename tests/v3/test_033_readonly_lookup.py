"""Operator decision (C), 2026-10-08, for the read-only turns (planner, reviewer, investigators,
judges, proposer; ``runtime/execution/readonly_turn.py``).

Review finding of the integrity change: the read-only Codex argv had no `-c web_search="disabled"`
and these turns, which decode their own events, were never scanned, so a real-planner turn could
web-search the solution and hand it to the implementer in the plan with nothing recorded. Now:

- a Codex read-only turn turns hosted web search off like every executor dispatch (golden G4 is
  amended by exactly that pair; ``test_033_s4_readonly_turn.py`` ``codex_oracle``);
- every read-only turn's decoded events go through ``answer_lookup.scan_turn``; the findings ride
  in the turn's usage under ``answer_lookup``, so the plan record's ``planner_usage`` and the
  ``aux_usage`` entries keep them, and ``LocalTrialExecutor._counters`` fails the trial on them.

The Codex ``web_search`` item shape below is synthetic: no stored 0.155.1 raw stream holds one
(§14 Q14); the detector matches a non-command item by its type and name fields. The Claude
``WebSearch`` tool use is the documented tool name (``claude --help``: ``--tools``/
``--disallowedTools`` take tool names). Stand-ins: the recording sandbox, credential and fake
``subprocess.run`` of ``test_033_s4_readonly_turn.py``; the end-to-end case runs on the S8 rig
(real executor, loop, worker and driver over a scripted host process). No docker, no provider.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_033_s4_readonly_turn import Credential, FakeRun, Sandbox, jsonl
from test_033_s8_executor import QuestionPlanner, bound, make_world

from amplai_foundry.agent_drivers.answer_lookup import WITHHELD, scan_turn
from amplai_foundry.agent_drivers.cli import CODEX_WEB_SEARCH_OFF
from amplai_foundry.meta_harness.local_executor import LocalTrialExecutor
from amplai_foundry.runtime.execution import readonly_turn
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.execution.readonly_turn import (
    LOOKUP_KEY,
    SCHEMA_MOUNT,
    ClaudeReadOnlyTurn,
    CodexReadOnlyTurn,
)

MODEL = "pinned-model-1"
SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
LEASED = "leased-access-token-0123456789abcdefghijklmnop"  # a credential value, not a secret
WEB_ITEM = {
    "type": "item.completed",
    "item": {"id": "ws_1", "type": "web_search", "query": "amplai-bench-app lint_orders"},
}


def command_item(command: Any, item_id: str = "c1") -> dict[str, Any]:
    return {
        "type": "item.completed",
        "item": {"id": item_id, "type": "command_execution", "command": command,
                 "aggregated_output": "", "exit_code": 0, "status": "completed"},
    }  # fmt: skip


def codex_stream(*items: dict[str, Any], usage: dict[str, Any] | None = None) -> bytes:
    done = [{"type": "turn.completed", "usage": usage}] if usage is not None else []
    return jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        *items,
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"answer": "ok"}'}},
        *done,
    )


def claude_stream(*blocks: dict[str, Any]) -> bytes:
    return jsonl(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": list(blocks)}},
        {"type": "result", "subtype": "success", "is_error": False,
         "structured_output": {"answer": "ok"},
         "usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 40}},
    )  # fmt: skip


class SeedingCredential(Credential):
    """Leases an ``auth.json`` holding ``LEASED`` into the turn's home, as the real lease does."""

    def seed(self, home: Path) -> None:
        super().seed(home)
        (home / AUTH).parent.mkdir(parents=True)
        (home / AUTH).write_text(json.dumps({"tokens": {"access_token": LEASED}}))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(run: FakeRun) -> FakeRun:
        monkeypatch.setattr(readonly_turn.subprocess, "run", run)
        return run

    return install


def codex(tmp_path: Path, credential: Credential | None = None) -> CodexReadOnlyTurn:
    return CodexReadOnlyTurn(
        Sandbox(), credential or Credential(), tmp_path / "runs",  # type: ignore[arg-type]
        model=MODEL, effort=None, cell_id="codex-cli",
    )  # fmt: skip


def claude(tmp_path: Path) -> ClaudeReadOnlyTurn:
    return ClaudeReadOnlyTurn(
        Sandbox(), "claude-token-not-a-secret", tmp_path / "runs",  # type: ignore[arg-type]
        model=MODEL, effort=None, cell_id="claude-cli",
    )  # fmt: skip


def rules(found: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(f["kind"], f["rule"]) for f in found]


# -- the argv -----------------------------------------------------------------------------------
@pytest.mark.parametrize("effort", [None, "high"])
def test_every_codex_read_only_turn_turns_web_search_off(tmp_path: Path, effort: Any) -> None:
    turn = CodexReadOnlyTurn(
        Sandbox(), Credential(), tmp_path / "runs", model=MODEL,  # type: ignore[arg-type]
        effort=effort, cell_id="codex-cli",
    )  # fmt: skip
    argv = turn.argv("p")
    at = argv.index("--skip-git-repo-check")
    assert argv[at - 2 : at] == list(CODEX_WEB_SEARCH_OFF)  # the last config override
    assert argv.count("-c") == (2 if effort else 1)


# -- the scan of a read-only turn ---------------------------------------------------------------
def test_a_codex_turn_that_web_searches_reports_it_in_its_usage(tmp_path: Path, fake: Any) -> None:
    usage = {"input_tokens": 120, "cached_input_tokens": 100, "output_tokens": 9}
    fake(FakeRun(codex_stream(WEB_ITEM, usage=usage)))
    result = codex(tmp_path).run(prompt="p", schema=SCHEMA, workspace=tmp_path)
    assert result.output == {"answer": "ok"}
    found = result.usage[LOOKUP_KEY]  # type: ignore[index]
    assert rules(found) == [("web_search", "codex item web_search")]
    assert "amplai-bench-app lint_orders" in found[0]["evidence"]
    # the provider's counts are kept as reported beside the findings
    assert {k: v for k, v in result.usage.items() if k != LOOKUP_KEY} == usage  # type: ignore[union-attr]


def test_a_clean_turn_returns_exactly_the_usage_it_returned_before(
    tmp_path: Path, fake: Any
) -> None:
    usage = {"input_tokens": 10, "output_tokens": 4}
    ordinary = command_item(["/usr/bin/bash", "-lc", "rg -n lint && git log --oneline -3"])
    fake(FakeRun(codex_stream(ordinary, usage=usage)))
    assert codex(tmp_path).run(prompt="p", schema=SCHEMA, workspace=tmp_path).usage == usage
    fake(FakeRun(claude_stream({"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}})))
    got = claude(tmp_path).run(prompt="p", schema=SCHEMA, workspace=tmp_path).usage
    assert got == {"input_tokens": 45, "output_tokens": 2, "cache_read_input_tokens": 40}


def test_a_turn_without_usage_keeps_its_counts_unknown_beside_a_finding(
    tmp_path: Path, fake: Any
) -> None:
    fake(FakeRun(codex_stream(command_item("git fsck --unreachable"))))  # no turn.completed
    usage = codex(tmp_path).run(prompt="p", schema=SCHEMA, workspace=tmp_path).usage
    assert usage is not None
    assert (usage["input_tokens"], usage["output_tokens"]) == (None, None)
    assert rules(usage[LOOKUP_KEY]) == [("git_history", "git fsck")]


def test_the_turns_own_mounts_are_not_outside_the_workspace(tmp_path: Path, fake: Any) -> None:
    # a multi-app planner turn reads the other apps at /amplai-input/apps/<app> (planner_codex.py)
    other = tmp_path / "other"
    other.mkdir()
    own = command_item(f"find /amplai-input/apps/b -name '*.py' && ls -R {SCHEMA_MOUNT}")
    root = command_item("find / -name '*amplai*'", "c2")
    fake(FakeRun(codex_stream(own, root, usage={"input_tokens": 1, "output_tokens": 1})))
    usage = (
        codex(tmp_path)
        .run(prompt="p", schema=SCHEMA, workspace=tmp_path, mounts={"/amplai-input/apps/b": other})
        .usage
    )
    assert rules(usage[LOOKUP_KEY]) == [("outside_workspace_search", "find /")]  # type: ignore[index]
    # without the mounts, the same searches are outside
    assert rules(scan_turn("codex", [own])) == [
        ("outside_workspace_search", "find /amplai-input/apps/b"),
        ("outside_workspace_search", f"ls {SCHEMA_MOUNT}"),
    ]


def test_a_claude_turn_that_web_searches_reports_it_in_its_usage(tmp_path: Path, fake: Any) -> None:
    fake(FakeRun(claude_stream(
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "amplai bench solution"}},
        {"type": "tool_use", "name": "Glob", "input": {"pattern": "/home/agent/**/*.py"}},
    )))  # fmt: skip
    usage = claude(tmp_path).run(prompt="p", schema=SCHEMA, workspace=tmp_path).usage
    assert usage is not None
    assert rules(usage[LOOKUP_KEY]) == [
        ("web_search", "claude WebSearch"),
        ("outside_workspace_search", "claude Glob /home/agent/**/*.py"),
    ]
    assert usage["input_tokens"] == 45 and usage["cache_read_input_tokens"] == 40


def test_evidence_holding_the_leased_credential_is_withheld(tmp_path: Path, fake: Any) -> None:
    fake(FakeRun(codex_stream(command_item(f"grep -r {LEASED} /home"),
                              usage={"input_tokens": 1, "output_tokens": 1})))  # fmt: skip
    turn = codex(tmp_path, SeedingCredential())
    usage = turn.run(prompt="p", schema=SCHEMA, workspace=tmp_path).usage
    (finding,) = usage[LOOKUP_KEY]  # type: ignore[index]
    assert finding["kind"] == "outside_workspace_search" and finding["evidence"] == WITHHELD
    assert LEASED not in json.dumps(usage)


def test_a_failed_turn_returns_no_usage_and_no_findings(tmp_path: Path, fake: Any) -> None:
    # documented gap (readonly_turn.py docstring): a turn that fails keeps nothing
    fake(FakeRun(codex_stream(WEB_ITEM, usage={"input_tokens": 1, "output_tokens": 1}),
                 returncode=1))  # fmt: skip
    with pytest.raises(Exception, match="did not complete"):
        codex(tmp_path).run(prompt="p", schema=SCHEMA, workspace=tmp_path)


# -- the trial: the executor reads the plan record's read-only turns ------------------------------
class LookingPlanner(QuestionPlanner):
    """A real-planner stand-in whose draft comes from a real ``CodexReadOnlyTurn`` (its process
    is a scripted stream) that web-searches before it replies with the plan."""

    def __init__(self, turn: CodexReadOnlyTurn) -> None:
        super().__init__([])
        self.turn = turn

    def draft(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        draft = super().draft(*args, **kwargs)["draft"]
        result = self.turn.run(prompt="plan it", schema={"type": "object"}, workspace=args[3])
        assert result.output == draft
        return {"draft": result.output, "usage": result.usage}


def planner_stream(draft: dict[str, Any]) -> bytes:
    return jsonl(
        {"type": "thread.started", "thread_id": "t-plan"},
        WEB_ITEM,
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(draft)}},
        {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}},
    )


def test_a_planner_turn_that_web_searches_fails_the_trial(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = codex(tmp_path / "planner")
    planner = LookingPlanner(turn)
    stdout = planner_stream(planner.draft_value)
    real_run = subprocess.run

    def selective(command: list[str], **kw: Any) -> Any:
        # only the planner turn's process and its docker cleanup are scripted; the S8 rig's own
        # processes (git, the agent stand-in) run for real
        if command[:1] == ["stand-in"]:
            return subprocess.CompletedProcess(command, 0, stdout, b"")
        if command[:2] in (["docker", "rm"], ["docker", "kill"]) and str(command[-1]).startswith(
            "amplai-plan-"
        ):
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return real_run(command, **kw)

    monkeypatch.setattr(readonly_turn.subprocess, "run", selective)
    world = make_world(deployment, tmp_path, planner=planner)
    obs = world.run("amb-02-proceed")  # a real-planner task graded by its hidden tests
    receipt = world.receipt(obs)
    assert receipt["planner"]["mode"] == "real"
    assert receipt["goal_status"] == "verified" and receipt["hidden_passed"] is True
    assert obs.success is False and receipt["success"] is False  # solved, but the planner looked
    assert (obs.safety_failures, obs.unknown_effects) == (0, 0)  # not a safety failure
    (entry,) = receipt["answer_lookup"]
    assert (entry["turn"], entry["kind"], entry["rule"]) == (
        "planner", "web_search", "codex item web_search",
    )  # fmt: skip
    assert "run_id" not in entry  # a read-only turn, not a run
    assert receipt["detail"] == "answer lookup: codex item web_search"
    assert receipt["planner"]["usage"][LOOKUP_KEY] == [
        {k: v for k, v in entry.items() if k != "turn"}
    ]
    assert world.proof(obs)["counters"]["answer_lookup"] == 1 and obs.answer_lookup == 1
    # the planner turn's counts are still counted (one run + the planner turn)
    assert (obs.input_tokens, obs.output_tokens) == (10 + 7, 5 + 3)
    bound(world, obs, world.baseline, "amb-02-proceed", 0)


def test_an_auxiliary_turns_finding_is_counted_with_its_role(
    deployment: Any, tmp_path: Path
) -> None:
    finding = {"kind": "web_search", "rule": "claude WebSearch", "evidence": "WebSearch q",
               "source": "assistant/tool_use/WebSearch"}  # fmt: skip
    clean = {"role": "investigator", "node_id": "node-app", "tokens": 3,
             "usage": {"input_tokens": 2, "output_tokens": 1}}  # fmt: skip
    looked = {"role": "reviewer", "node_id": "node-app", "tokens": 3,
              "usage": {"input_tokens": 2, "output_tokens": 1, LOOKUP_KEY: [finding]}}  # fmt: skip
    never = {"role": "lead", "node_id": None, "tokens": 0, "usage": None}
    plan = {"aux_usage": [clean, looked, never], "planner_usage": {"input_tokens": 1}}
    assert LocalTrialExecutor._turn_lookups(plan) == [
        {"turn": "aux:reviewer", "node_id": "node-app", **finding}
    ]
    world = make_world(deployment, tmp_path)
    counters = world.executor._counters(plan)
    assert counters["answer_lookup"] == [{"turn": "aux:reviewer", "node_id": "node-app", **finding}]
    assert counters["detail"]["answer_lookup"] == 1
    assert (counters["safety_failures"], counters["unknown_effects"]) == (0, 0)
