"""Work 033 S4 (AC-03 unit part): argv capture per driver, effort refusal, driver options.

interfaces.md 3.4 argv contract; measured CLI facts in
specs/033-harness-taxonomy/runs/cli-effort-facts.md (Codex `-c key=value` on `exec` and
`exec resume`, Claude `--effort <level>`, OpenCode `--variant` only on the CLI; its HTTP field is
확인 필요, 14 Q3, so OpenCode has no effort axis). Stand-in: a recording sandbox; no docker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.agent_drivers.cli import CliDriver
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution import policies
from amplai_foundry.runtime.execution.cells import (
    EFFORT_SYNTAX,
    DispatchOptions,
    normalized_digest,
)

MODEL = "pinned-model-1"
SESSION = "0f3a9c1e-7b52-4d0a-9d55-2a3c1b6e8f10"
CLAUDE_TOOLS = "Read,Edit,Write,Glob,Grep,Bash"
WEB_OFF = ["-c", 'web_search="disabled"']  # decision (C): on every Codex argv
ALL_CLAUDE_OPTIONS = frozenset({"max_turns", "append_system_prompt", "allowed_tools"})


class RecordingSandbox:
    """Returns the argv it was given, so `prepare` output shows what would be spawned."""

    class profile:
        uid, gid = 65534, 65534

    def command(self, argv: list[str], workspace: Path, name: str, **kw: Any) -> list[str]:
        return ["docker-stand-in", name, *argv]


def make_driver(tmp_path: Path, provider: str, auth: str = "api_key") -> CliDriver:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "token"} if auth == "oauth_token" else {}
    return CliDriver(
        provider, provider, "pinned-version", RecordingSandbox(),  # type: ignore[arg-type]
        SessionJournal(tmp_path / f"journal-{provider}-{auth}"),
        model=MODEL, qualified=True, environment=env, auth=auth,
    )  # fmt: skip


def hold_code(fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(Hold) as held:
        fn(*args, **kwargs)
    return str(held.value.code)


def opts(effort: str | None, **kw: Any) -> DispatchOptions:
    return DispatchOptions(MODEL, effort, **kw)


# -- Codex ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("auth", ["api_key", "oauth_token"])
def test_codex_exec_carries_the_effort_as_a_config_override(tmp_path: Path, auth: str) -> None:
    driver = make_driver(tmp_path, "codex", auth)
    assert driver.argv("Fix it.", options=opts("high")) == [
        "codex", "--ask-for-approval", "never", "exec", "--json", "--model", MODEL,
        "-c", "model_reasoning_effort=high", *WEB_OFF,
        "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "Fix it.",
    ]  # fmt: skip


def test_codex_exec_resume_carries_the_effort_too(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "codex")
    assert driver.argv("go on", session=SESSION, options=opts("xhigh")) == [
        "codex", "--ask-for-approval", "never", "exec", "resume", SESSION, "--json",
        "--model", MODEL, "-c", "model_reasoning_effort=xhigh", *WEB_OFF,
        "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "go on",
    ]  # fmt: skip


@pytest.mark.parametrize("effort", EFFORT_SYNTAX["codex-cli"])
@pytest.mark.parametrize("session", [None, SESSION], ids=["exec", "resume"])
def test_every_documented_codex_effort_appears_exactly_once(
    tmp_path: Path, effort: str, session: str | None
) -> None:
    argv = make_driver(tmp_path, "codex").argv("p", session=session, options=opts(effort))
    assert argv.count("-c") == 2  # the effort and decision (C)'s web search override
    assert argv[argv.index("-c") + 1] == f"model_reasoning_effort={effort}"
    assert argv[argv.index("-c") + 2 : argv.index("-c") + 4] == WEB_OFF
    assert argv[-1] == "p"  # the prompt stays the last element
    assert "--effort" not in argv  # Claude's flag never reaches Codex


def test_codex_without_an_effort_has_only_the_web_search_override(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "codex")
    for options in (None, DispatchOptions.default(MODEL)):
        for argv in (driver.argv("p", options=options),
                     driver.argv("p", session=SESSION, options=options)):  # fmt: skip
            assert argv.count("-c") == 1
            i = argv.index("-c")
            assert argv[i : i + 3] == [*WEB_OFF, "--skip-git-repo-check"]


def test_codex_config_overrides_follow_the_effort_and_need_the_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the allowlist starts empty (policies.py, 14 Q1): no key is admitted before a probe turn
    assert not policies.CODEX_CONFIG_ALLOWLIST
    code = hold_code(opts, "high", codex_config=(("some_key", "1"),))
    assert code == "DRIVER_OPTIONS_UNSUPPORTED"
    # effort is the cell's, never a driver option
    code = hold_code(opts, "high", codex_config=(("model_reasoning_effort", "low"),))
    assert code == "DRIVER_OPTIONS_UNSUPPORTED"
    monkeypatch.setattr(policies, "CODEX_CONFIG_ALLOWLIST", frozenset({"some_key"}))
    driver = make_driver(tmp_path, "codex")
    argv = driver.argv("p", options=opts("high", codex_config=(("some_key", "1"),)))
    i = argv.index("--json")
    assert argv[i : i + 7] == [
        "--json", "--model", MODEL, "-c", "model_reasoning_effort=high", "-c", "some_key=1",
    ]  # fmt: skip
    # a config override alone (no effort) is still `-c k=v` before --skip-git-repo-check
    alone = driver.argv("p", options=opts(None, codex_config=(("some_key", "1"),)))
    j = alone.index("-c")
    assert alone[j : j + 5] == ["-c", "some_key=1", *WEB_OFF, "--skip-git-repo-check"]


def test_codex_refuses_claude_driver_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # admitted by the (patched) allowlist, so the port's own refusal is what is tested
    monkeypatch.setattr(policies, "CLAUDE_OPTION_ALLOWLIST", ALL_CLAUDE_OPTIONS)
    driver = make_driver(tmp_path, "codex")
    for extra in ({"max_turns": 3}, {"append_system_prompt": "x"}, {"allowed_tools": ("Read",)}):
        code = hold_code(driver.argv, "p", options=opts("high", **extra))
        assert code == "DRIVER_OPTIONS_UNSUPPORTED"


def test_codex_still_refuses_an_output_schema_with_options(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "codex")
    code = hold_code(driver.argv, "p", output_schema={"type": "object"}, options=opts("low"))
    assert code == "SCHEMA_FILE_REQUIRED"


# -- Claude --------------------------------------------------------------------------------------
ISOLATION = {
    "api_key": ["--bare"],
    "oauth_token": [
        "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands", "--no-chrome",
    ],
}  # fmt: skip


@pytest.mark.parametrize("auth", ["api_key", "oauth_token"])
def test_claude_carries_the_effort_after_the_model(tmp_path: Path, auth: str) -> None:
    driver = make_driver(tmp_path, "claude", auth)
    assert driver.argv("Fix it.", options=opts("high")) == [
        "claude", *ISOLATION[auth], "-p", "Fix it.", "--output-format", "stream-json",
        "--verbose", "--model", MODEL, "--effort", "high", "--allowedTools", CLAUDE_TOOLS,
    ]  # fmt: skip


@pytest.mark.parametrize("effort", EFFORT_SYNTAX["claude-cli"])
def test_every_documented_claude_effort_appears_exactly_once(tmp_path: Path, effort: str) -> None:
    argv = make_driver(tmp_path, "claude").argv("p", session=SESSION, options=opts(effort))
    assert argv.count("--effort") == 1 and argv[argv.index("--effort") + 1] == effort
    assert argv[-2:] == ["--resume", SESSION]  # resume stays where the oracle puts it
    assert "-c" not in argv


def test_claude_effort_keeps_the_json_schema_argument(tmp_path: Path) -> None:
    schema = {"type": "object"}
    argv = make_driver(tmp_path, "claude").argv("p", output_schema=schema, options=opts("max"))
    assert argv[-2:] == ["--json-schema", '{"type":"object"}']
    assert argv[argv.index("--effort") + 1] == "max"


def test_claude_ultracode_is_not_a_documented_effort() -> None:
    # cli-effort-facts.md: `ultracode` turns on workflow orchestration and is excluded
    assert "ultracode" not in EFFORT_SYNTAX["claude-cli"]


def test_claude_driver_options_follow_the_contract_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # argv shape for the options the spec names (3.4); 14 Q13 keeps them out of production use
    # until probed, so the allowlist is patched here the way the Codex test patches its own.
    monkeypatch.setattr(policies, "CLAUDE_OPTION_ALLOWLIST", ALL_CLAUDE_OPTIONS)
    argv = make_driver(tmp_path, "claude", "oauth_token").argv(
        "p",
        options=opts(
            "high", max_turns=7, append_system_prompt="Be terse.", allowed_tools=("Read", "Grep")
        ),
    )
    i = argv.index("--model")
    assert argv[i : i + 10] == [
        "--model", MODEL, "--effort", "high", "--max-turns", "7",
        "--append-system-prompt", "Be terse.", "--allowedTools", "Read,Grep",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "extra",
    [{"max_turns": 7}, {"append_system_prompt": "Be terse."}, {"allowed_tools": ("Read",)}],
)
def test_claude_options_hold_while_the_allowlist_is_empty(extra: dict[str, Any]) -> None:
    # 14 Q13: `--max-turns` is not quoted in `claude --help` 2.1.278 and no option has had a
    # probe turn, so a directly built DispatchOptions (not only resolve_options) holds, and so
    # does an execution record replayed through from_wire (resume_exact).
    assert not policies.CLAUDE_OPTION_ALLOWLIST
    assert hold_code(DispatchOptions, MODEL, "high", **extra) == "DRIVER_OPTIONS_UNSUPPORTED"
    wire = DispatchOptions(MODEL, "high").wire()
    wire.update({k: list(v) if isinstance(v, tuple) else v for k, v in extra.items()})
    assert hold_code(DispatchOptions.from_wire, wire) == "DRIVER_OPTIONS_UNSUPPORTED"


def test_claude_refuses_codex_config_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policies, "CODEX_CONFIG_ALLOWLIST", frozenset({"k"}))
    driver = make_driver(tmp_path, "claude")
    code = hold_code(driver.argv, "p", options=opts("high", codex_config=(("k", "v"),)))
    assert code == "DRIVER_OPTIONS_UNSUPPORTED"


# -- refusals that never substitute --------------------------------------------------------------
@pytest.mark.parametrize(
    "effort", ["", "High", "--effort", "high;rm -rf", "a b", "x" * 40, "provider-default", "1x"]
)
def test_a_malformed_effort_is_refused_not_normalised(effort: str) -> None:
    assert hold_code(DispatchOptions, MODEL, effort) == "EFFORT_UNSUPPORTED"


@pytest.mark.parametrize("model", ["latest", "default", "auto", "", "-m", "a b", "../x"])
def test_an_unpinned_or_malformed_model_is_refused(model: str) -> None:
    assert hold_code(DispatchOptions, model, None) == "MODEL_UNPINNED"


@pytest.mark.parametrize(
    "extra",
    [
        {"max_turns": 0}, {"max_turns": 501}, {"max_turns": True},
        {"append_system_prompt": ""}, {"append_system_prompt": "x" * 2001},
        {"append_system_prompt": "--dangerously-skip-permissions"},
        {"append_system_prompt": "-x"}, {"append_system_prompt": "a\x00b"},
        {"allowed_tools": ()}, {"allowed_tools": ("Read", "Read")}, {"allowed_tools": ("Nope",)},
    ],
)  # fmt: skip
def test_driver_options_outside_their_values_are_refused(
    extra: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the value checks hold even for an option the allowlist admits
    monkeypatch.setattr(policies, "CLAUDE_OPTION_ALLOWLIST", ALL_CLAUDE_OPTIONS)
    assert hold_code(DispatchOptions, MODEL, None, **extra) == "DRIVER_OPTIONS_UNSUPPORTED"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_options_for_another_model_hold_model_not_qualified_for_port(
    tmp_path: Path, provider: str
) -> None:
    driver = make_driver(tmp_path, provider)
    other = DispatchOptions("another-model", "high")
    assert hold_code(driver.argv, "p", options=other) == "MODEL_NOT_QUALIFIED_FOR_PORT"
    # default options of another model are refused as well: the port is per model
    code = hold_code(driver.argv, "p", options=DispatchOptions.default("another-model"))
    assert code == "MODEL_NOT_QUALIFIED_FOR_PORT"


# -- OpenCode: no effort axis --------------------------------------------------------------------
def test_opencode_has_no_effort_axis() -> None:
    from amplai_foundry.runtime.execution.cells import Cell, validate_effort

    assert EFFORT_SYNTAX["opencode-server"] == ()  # HTTP variant field unverified (14 Q3)
    cell_id = Cell.make_id("opencode-server", "opencode/model-x", "high", legacy=False)
    assert cell_id == "opencode-server.opencode.model-x.high"
    cell = Cell(cell_id, "opencode-server", "opencode/model-x", "high", (), legacy=False)
    assert hold_code(validate_effort, cell, None) == "EFFORT_UNSUPPORTED"
    probe = {"cell_id": cell_id, "driver_id": "opencode-server", "model": "opencode/model-x",
             "effort": "high", "outcome": "accepted"}  # fmt: skip
    assert hold_code(validate_effort, cell, probe) == "EFFORT_UNSUPPORTED"  # even when probed


def test_opencode_ports_take_no_dispatch_options() -> None:
    from amplai_foundry.agent_drivers.ports import CliPort, OpenCodePort, RecipePort

    # amended by the operator decision of 2026-10-08 (web tools off in tests): the production
    # OpenCode port (PerDispatchOpenCodePort) takes options for the trial flag only, and holds an
    # effort or a driver option (tests/v3/test_033_web_tools_off.py)
    for port in (OpenCodePort, RecipePort, CliPort):
        assert getattr(port, "accepts_options", False) is not True, port.__name__


# -- prepare / checkpoint / resume bind the options ----------------------------------------------
def prepared(driver: CliDriver, tmp_path: Path, did: str, **kw: Any) -> dict[str, Any]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return driver.prepare({"dispatch_id": did}, "prompt", ws, **kw)


def test_prepare_spawns_the_effort_argv_and_journals_the_binding(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "codex")
    options = opts("high")
    record = prepared(driver, tmp_path, "d-high", options=options)
    command = record["command"]
    assert command[command.index("-c") : command.index("-c") + 2] == [
        "-c", "model_reasoning_effort=high",
    ]  # fmt: skip
    journal = driver.journal.read("d-high")
    assert journal["effort"] == "high" and journal["options_digest"] == options.digest()


def test_a_default_dispatch_keeps_todays_journal_bytes(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "codex")
    for did, options in (("d-none", None), ("d-default", DispatchOptions.default(MODEL))):
        prepared(driver, tmp_path, did, options=options)
        journal = driver.journal.read(did)
        assert "effort" not in journal and "options_digest" not in journal
    assert normalized_digest(None) is None
    assert normalized_digest(DispatchOptions.default(MODEL)) is None
    assert normalized_digest(opts("high")) == opts("high").digest()


def test_the_options_digest_separates_effort_and_driver_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policies, "CLAUDE_OPTION_ALLOWLIST", ALL_CLAUDE_OPTIONS)
    digests = {
        opts(None).digest(), opts("low").digest(), opts("high").digest(),
        opts("high", capture_trace=True).digest(), opts("high", max_turns=3).digest(),
        DispatchOptions("other-model", "high").digest(),
    }  # fmt: skip
    assert len(digests) == 6
    assert opts("high").digest() == opts("high").digest()
    assert DispatchOptions.from_wire(opts("high", max_turns=3).wire()) == opts("high", max_turns=3)


def stopped_checkpoint(driver: CliDriver, did: str) -> dict[str, Any]:
    driver.journal.update(
        did, state="paused", process_stopped=True, session_handle=SESSION,
        failure="checkpoint_pause",
    )  # fmt: skip
    return driver.checkpoint(did)


def test_checkpoint_reports_effort_and_resume_holds_a_different_one(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "codex")
    prepared(driver, tmp_path, "d-first", options=opts("high"))
    cp = stopped_checkpoint(driver, "d-first")
    assert cp["effort"] == "high" and cp["options_digest"] == opts("high").digest()
    ws = tmp_path / "ws"
    for wrong in (opts("low"), opts(None), None, opts("high", capture_trace=True)):
        code = hold_code(driver.resume, {"dispatch_id": "d-next"}, "go", ws, cp, options=wrong)
        assert code == "RESUME_PROFILE"
    code = hold_code(
        driver.resume, {"dispatch_id": "d-next"}, "go", ws, cp,
        options=DispatchOptions("another-model", "high"),
    )  # fmt: skip
    assert code == "MODEL_NOT_QUALIFIED_FOR_PORT"


def test_resume_under_the_same_effort_passes_the_profile_check(tmp_path: Path) -> None:
    """Same options as the paused dispatch: the profile check passes and the driver tries to
    spawn (the recording sandbox is no container, so the spawn is refused with DRIVER_SPAWN)."""
    driver = make_driver(tmp_path, "codex")
    record = prepared(driver, tmp_path, "d-same", options=opts("high"))
    cp = stopped_checkpoint(driver, "d-same")
    assert cp["workspace"] == record["workspace"]
    code = hold_code(
        driver.resume, {"dispatch_id": "d-same-2"}, "go", tmp_path / "ws", cp, options=opts("high")
    )  # fmt: skip
    assert code == "DRIVER_SPAWN"


def test_a_default_checkpoint_resumes_only_without_effort(tmp_path: Path) -> None:
    driver = make_driver(tmp_path, "codex")
    prepared(driver, tmp_path, "d-plain")
    cp = stopped_checkpoint(driver, "d-plain")
    assert cp["effort"] is None and cp["options_digest"] is None
    code = hold_code(
        driver.resume, {"dispatch_id": "d-n"}, "go", tmp_path / "ws", cp, options=opts("high")
    )  # fmt: skip
    assert code == "RESUME_PROFILE"
