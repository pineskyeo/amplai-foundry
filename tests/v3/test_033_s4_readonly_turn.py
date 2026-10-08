"""Work 033 S4: read-only structured turns (interfaces.md 3.5) and the planners built on them.

The turn bodies were extracted from `CodexPlanner.draft` and `ClaudePlanner.draft`; with
`effort=None` argv and prompts equal the frozen oracles (G2, G4), with an effort the flag sits
where 3.4 puts it (Codex `-c model_reasoning_effort=E` after `--model`, Claude `--effort E` after
`--model`; cli-effort-facts.md). Stand-ins: a recording sandbox and a fake `subprocess.run`
(no docker, no provider, no credential file).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution import readonly_turn
from amplai_foundry.runtime.execution.planner_codex import (
    ClaudePlanner,
    CodexPlanner,
    plan_schema,
)
from amplai_foundry.runtime.execution.readonly_turn import (
    SCHEMA_MOUNT,
    ClaudeReadOnlyTurn,
    CodexReadOnlyTurn,
)
from golden033 import argv_oracle
from rc06_rig import DRAFT

MODEL = "pinned-model-1"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}
PROMPT = "Read the repo.\nReply with JSON."


class Sandbox:
    """Records `command` calls and returns a recognisable stand-in command."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def command(self, argv: list[str], workspace: Path, name: str, **kw: Any) -> list[str]:
        self.calls.append({"argv": argv, "workspace": workspace, "name": name, **kw})
        return ["stand-in", name]


class Credential:
    def __init__(self) -> None:
        self.events: list[str] = []

    def seed(self, home: Path) -> None:
        self.events.append("seed")
        assert home.is_dir()

    def release(self, home: Path) -> None:
        self.events.append("release")


class FakeRun:
    """Replaces `subprocess.run`: one scripted turn process, every docker call recorded."""

    def __init__(
        self,
        stdout: bytes = b"",
        *,
        returncode: int = 0,
        stderr: bytes = b"",
        timeout: bool = False,
    ) -> None:
        self.stdout, self.returncode, self.stderr, self.timeout = (
            stdout,
            returncode,
            stderr,
            timeout,
        )
        self.turn_calls: list[dict[str, Any]] = []
        self.docker: list[list[str]] = []

    def __call__(self, command: list[str], **kw: Any) -> Any:
        if command[0] == "docker":
            self.docker.append(command)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        self.turn_calls.append({"command": command, **kw})
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kw.get("timeout", 0))
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


def jsonl(*events: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def codex_stream(answer: Any, usage: dict[str, Any] | None = None) -> bytes:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": usage or {"input_tokens": 10, "output_tokens": 4}},
    )


def claude_stream(structured: Any, *, is_error: bool = False) -> bytes:
    return jsonl(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "result", "subtype": "success", "is_error": is_error,
            "structured_output": structured,
            "usage": {"input_tokens": 7, "output_tokens": 2, "cache_read_input_tokens": 99},
        },
    )  # fmt: skip


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(run: FakeRun) -> FakeRun:
        monkeypatch.setattr(readonly_turn.subprocess, "run", run)
        return run

    return install


def codex_turn(tmp_path: Path, **kw: Any) -> tuple[CodexReadOnlyTurn, Sandbox, Credential]:
    sandbox, credential = Sandbox(), Credential()
    turn = CodexReadOnlyTurn(
        sandbox, credential, tmp_path / "runs", model=MODEL,  # type: ignore[arg-type]
        effort=kw.pop("effort", None), cell_id=kw.pop("cell_id", "codex-cli"), **kw,
    )  # fmt: skip
    return turn, sandbox, credential


def claude_turn(tmp_path: Path, **kw: Any) -> tuple[ClaudeReadOnlyTurn, Sandbox]:
    sandbox = Sandbox()
    turn = ClaudeReadOnlyTurn(
        sandbox,  # type: ignore[arg-type]
        kw.pop("token", "token-not-a-secret"),
        tmp_path / "runs",
        model=MODEL,
        effort=kw.pop("effort", None),
        cell_id=kw.pop("cell_id", "claude-cli"),
        **kw,
    )
    return turn, sandbox


def hold_code(fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(Hold) as held:
        fn(*args, **kwargs)
    return str(held.value.code)


# -- argv -----------------------------------------------------------------------------------------
def codex_oracle(model: str, prompt: str) -> list[str]:
    """The frozen c9f896a planner argv (G4) with operator decision (C)'s one pair, 2026-10-08:
    `-c web_search="disabled"` right before `--skip-git-repo-check` (as the executor, G2)."""
    frozen = argv_oracle.codex_planner_argv(model, prompt)
    at = frozen.index("--skip-git-repo-check")
    return [*frozen[:at], "-c", 'web_search="disabled"', *frozen[at:]]


@pytest.mark.parametrize("effort", [None, "provider-default"])
def test_codex_turn_argv_equals_the_planner_oracle_without_effort(
    tmp_path: Path, effort: str | None
) -> None:
    turn, _, _ = codex_turn(tmp_path, effort=effort)
    assert turn.argv(PROMPT) == codex_oracle(MODEL, PROMPT)
    assert turn.effort is None  # provider-default is "no flag" (2.4)


@pytest.mark.parametrize("effort", ["low", "high", "xhigh"])
def test_codex_turn_argv_carries_the_effort_after_the_model(tmp_path: Path, effort: str) -> None:
    turn, _, _ = codex_turn(tmp_path, effort=effort)
    want = codex_oracle(MODEL, PROMPT)
    at = want.index("--model") + 2
    assert turn.argv(PROMPT) == [*want[:at], "-c", f"model_reasoning_effort={effort}", *want[at:]]


@pytest.mark.parametrize("effort", [None, "provider-default"])
def test_claude_turn_argv_equals_the_planner_oracle_without_effort(
    tmp_path: Path, effort: str | None
) -> None:
    turn, _ = claude_turn(tmp_path, effort=effort)
    assert turn.argv(PROMPT, SCHEMA) == argv_oracle.claude_planner_argv(MODEL, PROMPT, SCHEMA)


@pytest.mark.parametrize("effort", ["low", "max"])
def test_claude_turn_argv_carries_the_effort_after_the_model(tmp_path: Path, effort: str) -> None:
    turn, _ = claude_turn(tmp_path, effort=effort)
    want = argv_oracle.claude_planner_argv(MODEL, PROMPT, SCHEMA)
    at = want.index("--model") + 2
    assert turn.argv(PROMPT, SCHEMA) == [*want[:at], "--effort", effort, *want[at:]]


def test_claude_turn_tools_are_read_only_by_default_and_configurable(tmp_path: Path) -> None:
    default, _ = claude_turn(tmp_path)
    argv = default.argv(PROMPT, SCHEMA)
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob,Grep"
    custom, _ = claude_turn(tmp_path, tools="Read")
    assert (
        custom.argv(PROMPT, SCHEMA)[custom.argv(PROMPT, SCHEMA).index("--allowedTools") + 1]
        == "Read"
    )


@pytest.mark.parametrize("effort", ["High", "--x", "a b", "high;ls", "x" * 40, ""])
def test_a_malformed_effort_is_refused_at_construction(tmp_path: Path, effort: str) -> None:
    assert hold_code(codex_turn, tmp_path, effort=effort) == "EFFORT_UNSUPPORTED"
    assert hold_code(claude_turn, tmp_path, effort=effort) == "EFFORT_UNSUPPORTED"


def test_a_claude_turn_needs_its_token(tmp_path: Path) -> None:
    assert hold_code(claude_turn, tmp_path, token="") == "AUTH_TOKEN_REQUIRED"


def test_turn_storage_cannot_traverse_links(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(Hold) as held:
        CodexReadOnlyTurn(
            Sandbox(), Credential(), tmp_path / "link", model=MODEL,  # type: ignore[arg-type]
            effort=None, cell_id="c",
        )  # fmt: skip
    assert held.value.code == "TURN_FAILED"


# -- Codex run
def test_a_codex_turn_returns_the_validated_reply_and_usage(tmp_path: Path, fake: Any) -> None:
    run = fake(FakeRun(codex_stream({"answer": "ok"})))
    turn, sandbox, credential = codex_turn(tmp_path, effort="high")
    work = tmp_path / "ws"
    work.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    result = turn.run(
        prompt=PROMPT, schema=SCHEMA, workspace=work, mounts={"/amplai-input/x": extra}
    )
    assert result.output == {"answer": "ok"}
    assert result.usage == {"input_tokens": 10, "output_tokens": 4}
    assert result.events_digest.startswith("sha256:") and result.seconds >= 0
    (call,) = sandbox.calls
    assert call["argv"] == turn.argv(PROMPT) and "model_reasoning_effort=high" in call["argv"]
    assert call["workspace"] == work and call["workspace_readonly"] is True  # docker mount (D-073)
    mounts = call["readonly_mounts"]
    assert set(mounts) == {SCHEMA_MOUNT, "/amplai-input/x"} and mounts["/amplai-input/x"] == extra
    assert json.loads(mounts[SCHEMA_MOUNT].read_text()) == SCHEMA
    assert turn.cell_id == "codex-cli"
    # the credential was leased for the turn and released after it; the container was removed
    assert credential.events == ["seed", "release"]
    assert run.docker[-1][:3] == ["docker", "rm", "-f"] and run.turn_calls[0]["timeout"] == 900


def test_each_codex_turn_uses_its_own_run_directory(tmp_path: Path, fake: Any) -> None:
    fake(FakeRun(codex_stream({"answer": "ok"})))
    turn, sandbox, _ = codex_turn(tmp_path)
    for _ in range(2):
        turn.run(prompt=PROMPT, schema=SCHEMA, workspace=tmp_path)
    homes = {c["native_home"] for c in sandbox.calls}
    assert len(homes) == 2 and all(h.parent.parent == (tmp_path / "runs") for h in homes)


@pytest.mark.parametrize(
    ("stdout", "rc", "code"),
    [
        (codex_stream({"answer": 1}), 0, "TURN_OUTPUT"),  # schema mismatch
        (codex_stream({"other": "x"}), 0, "TURN_OUTPUT"),
        (codex_stream("not json"), 0, "TURN_OUTPUT"),
        (codex_stream(["a list"]), 0, "TURN_OUTPUT"),  # not an object
        (codex_stream({"answer": "ok"}), 1, "TURN_FAILED"),  # process failed
        (jsonl({"type": "turn.completed", "usage": {}}), 0, "TURN_FAILED"),  # no agent message
        (b"", 0, "TURN_FAILED"),
    ],
)
def test_codex_turn_failures_carry_their_code(
    tmp_path: Path, fake: Any, stdout: bytes, rc: int, code: str
) -> None:
    run = fake(FakeRun(stdout, returncode=rc, stderr=b"boom"))
    turn, _, credential = codex_turn(tmp_path)
    assert hold_code(turn.run, prompt=PROMPT, schema=SCHEMA, workspace=tmp_path) == code
    assert credential.events == ["seed", "release"]  # released on every path
    assert run.docker[-1][:3] == ["docker", "rm", "-f"]


def test_a_failed_codex_turn_reports_the_tail_of_stderr_only(tmp_path: Path, fake: Any) -> None:
    fake(FakeRun(b"", returncode=2, stderr=b"x" * 1000 + b"END"))
    turn, _, _ = codex_turn(tmp_path)
    with pytest.raises(Hold) as held:
        turn.run(prompt=PROMPT, schema=SCHEMA, workspace=tmp_path)
    assert held.value.details["rc"] == 2 and held.value.details["stderr"].endswith("END")
    assert len(held.value.details["stderr"]) == 400


def test_a_codex_turn_that_times_out_kills_the_container(tmp_path: Path, fake: Any) -> None:
    run = fake(FakeRun(timeout=True))
    turn, sandbox, credential = codex_turn(tmp_path, timeout_seconds=7)
    assert hold_code(turn.run, prompt=PROMPT, schema=SCHEMA, workspace=tmp_path) == "TURN_TIMEOUT"
    name = sandbox.calls[0]["name"]
    assert ["docker", "kill", name] in run.docker and ["docker", "rm", "-f", name] in run.docker
    assert run.turn_calls[0]["timeout"] == 7
    assert credential.events == ["seed", "release"]


# -- Claude run
def test_a_claude_turn_returns_structured_output_and_token_counts(
    tmp_path: Path, fake: Any
) -> None:
    run = fake(FakeRun(claude_stream({"answer": "ok"})))
    turn, sandbox = claude_turn(tmp_path, effort="max")
    result = turn.run(prompt=PROMPT, schema=SCHEMA, workspace=tmp_path)
    assert result.output == {"answer": "ok"}
    # input counts the cache read as a Codex turn does; the cache class is kept by name
    assert result.usage == {"input_tokens": 7 + 99, "output_tokens": 2,
                            "cache_read_input_tokens": 99}  # fmt: skip
    (call,) = sandbox.calls
    assert call["argv"] == turn.argv(PROMPT, SCHEMA) and "--effort" in call["argv"]
    assert call["env_names"] == ["CLAUDE_CODE_OAUTH_TOKEN"]  # the token by name, not in argv
    assert "token-not-a-secret" not in json.dumps(call["argv"])
    assert run.turn_calls[0]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "token-not-a-secret"
    assert call["workspace_readonly"] is True and call["readonly_mounts"] is None
    assert turn.cell_id == "claude-cli"


@pytest.mark.parametrize(
    ("stdout", "rc", "code"),
    [
        (claude_stream({"answer": "ok"}), 1, "TURN_FAILED"),
        (claude_stream({"answer": "ok"}, is_error=True), 0, "TURN_FAILED"),
        (jsonl({"type": "system", "subtype": "init"}), 0, "TURN_FAILED"),  # no result event
        (claude_stream(None), 0, "TURN_OUTPUT"),
        (claude_stream("text"), 0, "TURN_OUTPUT"),
        (claude_stream({"answer": 5}), 0, "TURN_OUTPUT"),
    ],
)
def test_claude_turn_failures_carry_their_code(
    tmp_path: Path, fake: Any, stdout: bytes, rc: int, code: str
) -> None:
    run = fake(FakeRun(stdout, returncode=rc))
    turn, _ = claude_turn(tmp_path)
    assert hold_code(turn.run, prompt=PROMPT, schema=SCHEMA, workspace=tmp_path) == code
    assert run.docker[-1][:3] == ["docker", "rm", "-f"]


def test_a_claude_turn_timeout(tmp_path: Path, fake: Any) -> None:
    run = fake(FakeRun(timeout=True))
    turn, sandbox = claude_turn(tmp_path)
    assert hold_code(turn.run, prompt=PROMPT, schema=SCHEMA, workspace=tmp_path) == "TURN_TIMEOUT"
    assert ["docker", "kill", sandbox.calls[0]["name"]] in run.docker


# -- planners keep their public behaviour and codes
class ScriptedTurn:
    cell_id = "scripted"

    def __init__(self, result: Any) -> None:
        self.result, self.calls = result, []  # type: ignore[var-annotated]

    def run(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if isinstance(self.result, Hold):
            raise self.result
        return self.result


def planners(tmp_path: Path) -> list[CodexPlanner]:
    return [
        CodexPlanner(Sandbox(), Credential(), tmp_path / "p1", model=MODEL),
        ClaudePlanner(Sandbox(), "token", tmp_path / "p2", model=MODEL),  # type: ignore[arg-type]
    ]


@pytest.mark.parametrize(
    ("turn_code", "planner_code"),
    [
        ("TURN_TIMEOUT", "PLANNER_TIMEOUT"),
        ("TURN_FAILED", "PLANNER_FAILED"),
        ("TURN_OUTPUT", "PLANNER_OUTPUT"),
        ("SOMETHING_ELSE", "SOMETHING_ELSE"),  # other codes pass through unchanged
    ],
)
def test_planner_codes_map_from_turn_codes(
    tmp_path: Path, turn_code: str, planner_code: str
) -> None:
    for planner in planners(tmp_path):
        hold = Hold(turn_code, "Read-only turn exceeded its time budget")
        planner.turn = ScriptedTurn(hold)  # type: ignore[assignment]
        with pytest.raises(Hold) as held:
            planner.draft("goal", "app", {"unit": "u"}, tmp_path)
        assert held.value.code == planner_code
        if turn_code != "SOMETHING_ELSE":  # a mapped code speaks of the planner, not the turn
            assert "Read-only turn" not in held.value.message


def test_a_planner_reply_without_a_plan_is_planner_output(tmp_path: Path) -> None:
    from amplai_foundry.runtime.execution.readonly_turn import TurnResult

    empty = {"summary": "s", "acceptance": [], "work_items": [], "questions": []}
    for planner in planners(tmp_path):
        scripted = ScriptedTurn(TurnResult(empty, None, 0.0, "sha256:" + "0" * 64))
        planner.turn = scripted  # type: ignore[assignment]
        assert hold_code(planner.draft, "g", "app", {"unit": "u"}, tmp_path) == "PLANNER_OUTPUT"


def test_a_planner_draft_keeps_its_result_shape_and_inputs(tmp_path: Path) -> None:
    from amplai_foundry.runtime.execution.readonly_turn import TurnResult

    reply = {"summary": "s", "acceptance": [{"statement": "x", "verifier": "unit"}]}
    for planner in planners(tmp_path):
        scripted = ScriptedTurn(TurnResult(reply, {"output_tokens": 3}, 1.5, "sha256:" + "0" * 64))
        planner.turn = scripted  # type: ignore[assignment]
        got = planner.draft("goal", "app", {"unit": "u"}, tmp_path, mode="design")
        assert got == {"draft": reply, "usage": {"output_tokens": 3}, "seconds": 1.5}
        call = scripted.calls[0]
        assert call["prompt"] == planner.prompt("goal", "app", {"unit": "u"}, "design")
        assert call["schema"] == plan_schema(["unit"]) and call["workspace"] == tmp_path


def test_the_planner_cell_defaults_and_effort(tmp_path: Path) -> None:
    codex, claude = planners(tmp_path)
    assert (codex.cell_id, claude.cell_id) == ("codex-cli", "claude-cli")
    assert codex.argv(PROMPT) == codex_oracle(MODEL, PROMPT)
    assert claude.claude_argv(PROMPT, SCHEMA) == argv_oracle.claude_planner_argv(
        MODEL, PROMPT, SCHEMA
    )
    effortful = CodexPlanner(
        Sandbox(), Credential(), tmp_path / "p3", model=MODEL,  # type: ignore[arg-type]
        effort="high", cell_id="codex-cli.pinned-model-1.high",
    )  # fmt: skip
    assert "model_reasoning_effort=high" in effortful.argv(PROMPT)
    assert effortful.cell_id == "codex-cli.pinned-model-1.high" and effortful.turn.cell_id == (
        "codex-cli.pinned-model-1.high"
    )
    claude_high = ClaudePlanner(
        Sandbox(), "token", tmp_path / "p4", model=MODEL, effort="high",  # type: ignore[arg-type]
        cell_id="claude-cli.pinned-model-1.high",
    )  # fmt: skip
    assert claude_high.claude_argv(PROMPT, SCHEMA).count("--effort") == 1


def test_a_planner_end_to_end_through_the_turn(tmp_path: Path, fake: Any) -> None:
    fake(FakeRun(codex_stream(DRAFT)))
    planner = CodexPlanner(Sandbox(), Credential(), tmp_path / "e2e", model=MODEL)
    got = planner.draft("goal", "app", {"check": "c"}, tmp_path)
    assert got["draft"] == DRAFT and got["usage"] == {"input_tokens": 10, "output_tokens": 4}
    # an acceptance bound to a verifier the app does not have fails the plan schema
    wrong = {**DRAFT, "acceptance": [{"statement": "x", "verifier": "nope"}]}
    fake(FakeRun(codex_stream(wrong)))
    assert hold_code(planner.draft, "goal", "app", {"check": "c"}, tmp_path) == "PLANNER_OUTPUT"
