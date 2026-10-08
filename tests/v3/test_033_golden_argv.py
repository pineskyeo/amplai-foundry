"""Work 033 S0, golden G2 (interfaces.md 4.2): driver and planner argv are frozen.

`CliDriver.argv` must equal the frozen oracle (`tests/golden033/argv_oracle.py`, a copy of
`agent_drivers/cli.py:95-151`, `planner_codex.py:220-225,343-349` at c9f896a) for Codex and
Claude, session none/exact, output schema none/set, auth api_key/oauth.

Once S4 adds `DispatchOptions` (`runtime/execution/cells.py`, interfaces.md 3.4), the same matrix
also runs with `options=None` and `options=DispatchOptions.default(model)`; before then those two
variants are skipped, not silently passed.

Operator decision (C), 2026-10-08 (interfaces.md, clarification "Answer Lookup And Workspace
Objects"): every Codex argv also carries `-c web_search="disabled"` right before
`--skip-git-repo-check`, the executor's (G2) and the read-only planner turn's (G4). The oracle
stays the frozen c9f896a copy; `with_web_search_off` adds exactly that one pair to its Codex
vectors, so any other change still fails G2 and G4.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.agent_drivers.cli import CODEX_WEB_SEARCH_OFF, CliDriver
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.planner_codex import ClaudePlanner, CodexPlanner
from golden033 import argv_oracle

MODEL = "pinned-model-1"
SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
SESSIONS = [None, "0f3a9c1e-7b52-4d0a-9d55-2a3c1b6e8f10"]
SCHEMAS = [None, SCHEMA]
PROMPTS = ["Fix it.", "multi\nline 'quoted' \"prompt\" with unicode 한국어 and --flag-looking text"]


def make_driver(tmp_path: Path, provider: str, auth: str) -> CliDriver:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "token"} if auth == "oauth_token" else {}
    return CliDriver(
        provider,
        provider,  # binary
        "pinned-version",
        None,  # type: ignore[arg-type]  # argv never touches the sandbox
        SessionJournal(tmp_path / f"journal-{provider}-{auth}"),
        model=MODEL,
        qualified=True,
        environment=env,
        auth=auth,
    )


def default_options(kind: str) -> dict[str, Any]:
    """Extra keyword arguments for the options variants; skips until DispatchOptions exists."""
    if kind == "absent":
        return {}
    try:
        from amplai_foundry.runtime.execution.cells import DispatchOptions
    except ImportError:
        pytest.skip("DispatchOptions arrives with S4 (interfaces.md 3.4)")
    if "options" not in inspect.signature(CliDriver.argv).parameters:
        pytest.skip("CliDriver.argv takes options from S4")
    return {"options": None if kind == "none" else DispatchOptions.default(MODEL)}


def with_web_search_off(provider: str, frozen: tuple[str, Any]) -> tuple[str, Any]:
    """The oracle's outcome with decision (C)'s pair before `--skip-git-repo-check` (Codex)."""
    kind, value = frozen
    if provider != "codex" or kind != "ok":
        return frozen
    i = value.index("--skip-git-repo-check")
    return kind, [*value[:i], "-c", 'web_search="disabled"', *value[i:]]


def outcome(fn: Any, *args: Any, **kwargs: Any) -> tuple[str, Any]:
    """Value, or the Hold code: an error is part of the frozen behaviour."""
    try:
        return "ok", fn(*args, **kwargs)
    except Hold as hold:
        return "hold", hold.code


@pytest.mark.parametrize("options", ["absent", "none", "default"])
@pytest.mark.parametrize("prompt", PROMPTS)
@pytest.mark.parametrize("schema", SCHEMAS, ids=["no-schema", "schema"])
@pytest.mark.parametrize("session", SESSIONS, ids=["no-session", "exact-session"])
@pytest.mark.parametrize(
    ("provider", "auth"),
    [
        ("codex", "api_key"),
        ("codex", "oauth_token"),
        ("claude", "api_key"),
        ("claude", "oauth_token"),
    ],
)
def test_the_driver_argv_equals_the_oracle(
    tmp_path: Path,
    provider: str,
    auth: str,
    session: str | None,
    schema: dict[str, Any] | None,
    prompt: str,
    options: str,
) -> None:
    extra = default_options(options)
    driver = make_driver(tmp_path, provider, auth)
    got = outcome(driver.argv, prompt, session=session, output_schema=schema, **extra)
    want = with_web_search_off(provider, outcome(
        argv_oracle.argv, provider, provider, MODEL, auth, prompt, session=session,
        output_schema=schema,
    ))  # fmt: skip
    assert got == want
    # Codex refuses a schema (a pinned file is required); every other cell yields an argv.
    assert (got[0] == "hold") == (provider == "codex" and schema is not None)


@pytest.mark.parametrize("session", ["latest", "continue", "-x", "", "a\nb", "s" * 513])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_an_unpinned_session_is_refused_as_the_oracle_refuses(
    tmp_path: Path, provider: str, session: str
) -> None:
    driver = make_driver(tmp_path, provider, "api_key")
    got = outcome(driver.argv, "p", session=session)
    want = outcome(argv_oracle.argv, provider, provider, MODEL, "api_key", "p", session=session)
    assert got == want == ("hold", "SESSION_UNPINNED")


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_a_prompt_over_one_mebibyte_is_refused_as_the_oracle_refuses(
    tmp_path: Path, provider: str
) -> None:
    driver = make_driver(tmp_path, provider, "api_key")
    for size, code in ((1024 * 1024, "ok"), (1024 * 1024 + 1, "hold")):
        prompt = "x" * size
        got = outcome(driver.argv, prompt)
        want = with_web_search_off(
            provider, outcome(argv_oracle.argv, provider, provider, MODEL, "api_key", prompt)
        )
        assert got[0] == want[0] == code
        if code == "ok":
            assert got == want


def test_literal_argv_shapes_pin_the_oracle_itself(tmp_path: Path) -> None:
    """Literals, so an edit that changes both the loop and the oracle copy still fails."""
    codex = make_driver(tmp_path, "codex", "api_key").argv("P", session="S1")
    assert codex == [
        "codex", "--ask-for-approval", "never", "exec", "resume", "S1", "--json", "--model", MODEL,
        "-c", 'web_search="disabled"',
        "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "P",
    ]  # fmt: skip
    assert CODEX_WEB_SEARCH_OFF == ("-c", 'web_search="disabled"')
    api = make_driver(tmp_path, "claude", "api_key").argv("P", output_schema={"t": 1})
    assert api == [
        "claude", "--bare", "-p", "P", "--output-format", "stream-json", "--verbose",
        "--model", MODEL, "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash",
        "--json-schema", '{"t":1}',
    ]  # fmt: skip
    oauth = make_driver(tmp_path, "claude", "oauth_token").argv("P", session="S1")
    assert oauth == [
        "claude", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
        "--no-chrome", "-p", "P", "--output-format", "stream-json", "--verbose",
        "--model", MODEL, "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash", "--resume", "S1",
    ]  # fmt: skip


@pytest.mark.parametrize("prompt", PROMPTS)
def test_the_planner_argv_equals_the_oracle(tmp_path: Path, prompt: str) -> None:
    codex = CodexPlanner(None, None, tmp_path / "codex-runs", model=MODEL)  # type: ignore[arg-type]
    assert ("ok", codex.argv(prompt)) == with_web_search_off(
        "codex", ("ok", argv_oracle.codex_planner_argv(MODEL, prompt))
    )
    claude = ClaudePlanner(None, "token", tmp_path / "claude-runs", model=MODEL)  # type: ignore[arg-type]
    assert claude.claude_argv(prompt, SCHEMA) == argv_oracle.claude_planner_argv(
        MODEL, prompt, SCHEMA
    )
    argv = claude.claude_argv(prompt, SCHEMA)
    assert argv[argv.index("--allowedTools") : argv.index("--allowedTools") + 2] == [
        "--allowedTools",
        "Read,Glob,Grep",
    ]
    assert codex.argv(prompt)[-3:] == ["--output-schema", "/amplai-input/plan-schema.json", prompt]
    assert claude.claude_argv(prompt, SCHEMA)[-2:] == [
        "--json-schema",
        '{"type":"object","properties":{"a":{"type":"string"}},"required":["a"]}',
    ]


# -- operator decision 2026-10-08 (supersedes IC-35): web tools off in tests ---------------------
# A trial dispatch (DispatchOptions.offline) and a trial read-only turn (offline=True) carry the
# driver's web-off arguments; every vector above (a real goal) is unchanged. The amendment adds
# exactly these arguments, at exactly these places, to the frozen oracle's vectors:
# - Claude: `--disallowedTools <network tools>` right after `--allowedTools <list>` (the API-key
#   argv also `--strict-mcp-config`; the OAuth argv already has it);
# - Codex: the networked features off and `--ignore-user-config` right after decision (C)'s pair.
def with_trial_web_off(provider: str, auth: str, frozen: tuple[str, Any]) -> tuple[str, Any]:
    from amplai_foundry.agent_drivers import offline

    kind, value = with_web_search_off(provider, frozen)
    if kind != "ok":
        return kind, value
    if provider == "codex":
        i = value.index("--skip-git-repo-check")
        return kind, [*value[:i], *offline.CODEX_FEATURES_OFF, *offline.CODEX_IGNORE_CONFIG,
                      *value[i:]]  # fmt: skip
    i = value.index("--allowedTools") + 2
    extra = [*offline.CLAUDE_OFFLINE, *(offline.CLAUDE_NO_MCP if auth == "api_key" else ())]
    return kind, [*value[:i], *extra, *value[i:]]


@pytest.mark.parametrize("schema", SCHEMAS, ids=["no-schema", "schema"])
@pytest.mark.parametrize("session", SESSIONS, ids=["no-session", "exact-session"])
@pytest.mark.parametrize(
    ("provider", "auth"),
    [("codex", "api_key"), ("codex", "oauth_token"), ("claude", "api_key"),
     ("claude", "oauth_token")],
)  # fmt: skip
def test_a_trial_argv_is_the_oracle_plus_exactly_the_web_off_arguments(
    tmp_path: Path, provider: str, auth: str, session: str | None, schema: dict[str, Any] | None
) -> None:
    from amplai_foundry.runtime.execution.cells import DispatchOptions

    driver = make_driver(tmp_path, provider, auth)
    trial = DispatchOptions(MODEL, None, offline=True)
    got = outcome(driver.argv, "P", session=session, output_schema=schema, options=trial)
    want = with_trial_web_off(provider, auth, outcome(
        argv_oracle.argv, provider, provider, MODEL, auth, "P", session=session,
        output_schema=schema,
    ))  # fmt: skip
    assert got == want


def test_a_trial_argv_pins_the_web_off_literals(tmp_path: Path) -> None:
    from amplai_foundry.runtime.execution.cells import DispatchOptions

    trial = DispatchOptions(MODEL, None, offline=True)
    oauth = make_driver(tmp_path, "claude", "oauth_token").argv("P", options=trial)
    at = oauth.index("--allowedTools")
    assert oauth[at : at + 4] == [
        "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash",
        "--disallowedTools", "WebSearch,WebFetch,RemoteTrigger,DesignSync,PushNotification",
    ]  # fmt: skip
    assert oauth.count("--strict-mcp-config") == 1
    api = make_driver(tmp_path, "claude", "api_key").argv("P", options=trial)
    assert api[api.index("--disallowedTools") + 2] == "--strict-mcp-config"
    codex = make_driver(tmp_path, "codex", "api_key").argv("P", session="S1", options=trial)
    assert codex[9:] == [
        "-c", 'web_search="disabled"',
        "-c", "features.apps=false", "-c", "features.browser_use=false",
        "-c", "features.browser_use_external=false",
        "-c", "features.browser_use_full_cdp_access=false", "-c", "features.computer_use=false",
        "-c", "features.in_app_browser=false", "-c", "features.remote_plugin=false",
        "-c", "features.skill_mcp_dependency_install=false",
        "--ignore-user-config",
        "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "P",
    ]  # fmt: skip


@pytest.mark.parametrize("prompt", PROMPTS)
def test_a_trial_planner_argv_is_the_g4_oracle_plus_exactly_the_web_off_arguments(
    tmp_path: Path, prompt: str
) -> None:
    from amplai_foundry.runtime.execution.readonly_turn import ClaudeReadOnlyTurn, CodexReadOnlyTurn

    codex = CodexPlanner(None, None, tmp_path / "codex-runs", model=MODEL)  # type: ignore[arg-type]
    assert isinstance(codex.turn, CodexReadOnlyTurn)
    assert ("ok", codex.turn.argv(prompt, offline=True)) == with_trial_web_off(
        "codex", "oauth_token", ("ok", argv_oracle.codex_planner_argv(MODEL, prompt))
    )
    claude = ClaudePlanner(None, "token", tmp_path / "claude-runs", model=MODEL)  # type: ignore[arg-type]
    assert isinstance(claude.turn, ClaudeReadOnlyTurn)
    # the read-only Claude argv is the OAuth form: --strict-mcp-config is already in it
    assert ("ok", claude.turn.argv(prompt, SCHEMA, offline=True)) == with_trial_web_off(
        "claude", "oauth_token", ("ok", argv_oracle.claude_planner_argv(MODEL, prompt, SCHEMA))
    )
    # without the flag the planner argv is G4 as before
    assert codex.turn.argv(prompt) == codex.argv(prompt)
    assert claude.turn.argv(prompt, SCHEMA) == claude.claude_argv(prompt, SCHEMA)
