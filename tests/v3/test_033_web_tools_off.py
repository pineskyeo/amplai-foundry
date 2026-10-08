"""Operator decision 2026-10-08 (supersedes IC-35): every trial dispatch runs with web tools off.

Covers the driver contract (``agent_drivers/offline.py``): each current driver's trial argv or
config carries its web-off settings, a driver without a declaration is held
DRIVER_WEB_UNDECLARED before any claim, and a real (non-trial) goal is unchanged. The G2/G4 trial
vectors are in ``test_033_golden_argv.py``; the loop-level hold of a port without options is in
``test_033_s13_traces_acl.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from amplai_foundry.agent_drivers import offline
from amplai_foundry.agent_drivers.cli import CliDriver
from amplai_foundry.agent_drivers.opencode_launcher import DockerOpenCodeLauncher, server_argv
from amplai_foundry.agent_drivers.opencode_port import (
    AUTH,
    CATALOG,
    PendingDockerLauncher,
    PerDispatchOpenCodePort,
    ScopedOpenCodeHome,
)
from amplai_foundry.agent_drivers.ports import CliPort, RecipePort
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.cells import DispatchOptions
from amplai_foundry.runtime.execution.codex import OptionsCliPort
from amplai_foundry.runtime.execution.readonly_turn import ClaudeReadOnlyTurn, CodexReadOnlyTurn
from amplai_foundry.runtime.execution.strategy_runner import AuxLedger, StrategyRunner

MODEL = "pinned-model-1"


def driver(tmp_path: Path, provider: str, auth: str = "oauth_token") -> CliDriver:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "token"} if auth == "oauth_token" else {}
    return CliDriver(
        provider, provider, "pinned-version", None,  # type: ignore[arg-type]
        SessionJournal(tmp_path / f"journal-{provider}-{auth}"), model=MODEL, qualified=True,
        environment=env, auth=auth,
    )  # fmt: skip


def hold_code(fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(Hold) as caught:
        fn(*args, **kwargs)
    return caught.value.code


TRIAL = DispatchOptions(MODEL, None, offline=True)


# -- the flag ---------------------------------------------------------------------------------
def test_the_trial_flag_keeps_old_wires_and_digests() -> None:
    plain = DispatchOptions(MODEL, None, capture_trace=True)
    assert "offline" not in plain.wire()  # options recorded before the flag keep their digest
    assert TRIAL.wire()["offline"] is True and not TRIAL.is_default()
    assert DispatchOptions.from_wire(TRIAL.wire()) == TRIAL
    assert DispatchOptions.from_wire(plain.wire()).offline is False
    assert hold_code(DispatchOptions, MODEL, None, offline=1) == "DRIVER_OPTIONS_UNSUPPORTED"


# -- Claude Code ------------------------------------------------------------------------------
@pytest.mark.parametrize("auth", ["oauth_token", "api_key"])
def test_a_claude_trial_denies_the_network_tools_and_a_real_goal_is_unchanged(
    tmp_path: Path, auth: str
) -> None:
    d = driver(tmp_path, "claude", auth)
    real = d.argv("P", options=DispatchOptions.default(MODEL))
    assert real == d.argv("P") and "--disallowedTools" not in real
    trial = d.argv("P", options=TRIAL)
    at = trial.index("--disallowedTools")
    assert trial[at + 1].split(",") == list(offline.CLAUDE_WEB_TOOLS)
    assert {"WebSearch", "WebFetch"} <= set(offline.CLAUDE_WEB_TOOLS)
    assert trial.count("--strict-mcp-config") == 1  # no MCP server in either auth form
    declared = offline.require_port(OptionsCliPort(d))
    assert declared["argv"] == [t for t in trial if t not in real]


# -- Codex ------------------------------------------------------------------------------------
@pytest.mark.parametrize("session", [None, "S1"])
def test_a_codex_trial_turns_the_networked_features_and_user_config_off(
    tmp_path: Path, session: str | None
) -> None:
    d = driver(tmp_path, "codex")
    real = d.argv("P", session=session)
    assert real == d.argv("P", session=session, options=DispatchOptions.default(MODEL))
    assert "--ignore-user-config" not in real and real.count("-c") == 1  # web search off only
    trial = d.argv("P", session=session, options=TRIAL)
    at = trial.index("--skip-git-repo-check")
    off = [*offline.CODEX_FEATURES_OFF, *offline.CODEX_IGNORE_CONFIG]
    assert trial[at - len(off) - 2 : at] == ["-c", 'web_search="disabled"', *off]
    assert offline.require_port(OptionsCliPort(d))["argv"] == ["-c", 'web_search="disabled"', *off]


def test_a_trial_cannot_turn_a_networked_codex_feature_back_on() -> None:
    from amplai_foundry.agent_drivers.cli import CODEX_WEB_KEYS

    for name in offline.CODEX_NETWORK_FEATURES:
        assert f"features.{name}" in CODEX_WEB_KEYS


# -- read-only turns --------------------------------------------------------------------------
def test_the_read_only_turns_declare_their_web_off_arguments(tmp_path: Path) -> None:
    codex = CodexReadOnlyTurn(
        None,
        None,
        tmp_path / "c",
        model=MODEL,
        effort=None,
        cell_id="codex-cli",  # type: ignore[arg-type]
    )
    claude = ClaudeReadOnlyTurn(
        None,
        "token",
        tmp_path / "k",
        model=MODEL,
        effort=None,
        cell_id="claude-cli",  # type: ignore[arg-type]
    )
    # Codex: right before --skip-git-repo-check (after web search off); Claude: right after
    # --allowedTools <list>
    plain, trial = codex.argv("P"), codex.argv("P", offline=True)
    at = plain.index("--skip-git-repo-check")
    assert trial == [*plain[:at], *offline.require(codex)["argv"], *plain[at:]]
    plain, trial = claude.argv("P", {}), claude.argv("P", {}, offline=True)
    at = plain.index("--allowedTools") + 2
    assert trial == [*plain[:at], *offline.require(claude)["argv"], *plain[at:]]


class Turn:
    cell_id = "fake-cell"

    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    def run(self, **kw: Any) -> Any:
        self.kwargs.append({k: v for k, v in kw.items() if k not in {"prompt", "schema"}})
        return SimpleNamespace(output={"ok": True}, usage=None, seconds=0.0, events_digest="d")


class DeclaredTurn(Turn):
    offline_tools: ClassVar[dict[str, list[str]]] = {"argv": ["--no-web"]}


def aux(turn: Any, *, trial: bool) -> AuxLedger:
    runner = StrategyRunner(SimpleNamespace(store=None, scope=None), lambda c: turn, queue=None)  # type: ignore[arg-type]
    ledger = AuxLedger(10_000)
    runner._aux_turn(
        ledger, role="reviewer", purpose="review", cell_id="fake-cell", prompt="p", schema={},
        workspace=Path("."), offline=trial,
    )  # fmt: skip
    return ledger


def test_a_trial_aux_turn_runs_offline_and_an_undeclared_one_is_held_before_it_runs() -> None:
    declared = DeclaredTurn()
    aux(declared, trial=True)
    aux(declared, trial=False)
    assert [k.get("offline") for k in declared.kwargs] == [True, None]  # real: called as before
    bare = Turn()
    with pytest.raises(Hold) as caught:
        aux(bare, trial=True)
    assert caught.value.code == "DRIVER_WEB_UNDECLARED" and bare.kwargs == []
    aux(bare, trial=False)  # a real goal never asks
    assert bare.kwargs == [{"workspace": Path(".")}]


# -- OpenCode ---------------------------------------------------------------------------------
class Sandbox:
    engine = "docker"
    profile = SimpleNamespace(image="image@sha256:x")

    def command(self, argv: list[str], workspace: Path, name: str, **kw: Any) -> list[str]:
        return ["docker", "run", "--rm", "--name", name, self.profile.image, *argv]


def test_the_opencode_server_of_a_trial_gets_the_web_deny_config(tmp_path: Path) -> None:
    sandbox: Any = Sandbox()
    plain = server_argv(sandbox, tmp_path, "n", tmp_path, tmp_path)
    trial = server_argv(sandbox, tmp_path, "n", tmp_path, tmp_path, offline=True)
    at = plain.index(sandbox.profile.image)  # the env entries go before the image
    added = trial[at : at + 4]
    assert trial == [*plain[:at], *added, *plain[at:]]
    assert added[0] == added[2] == "--env" and added[3] == "OPENCODE_DISABLE_PROJECT_CONFIG=1"
    assert added[1].startswith("OPENCODE_CONFIG_CONTENT=")
    config = json.loads(added[1].split("=", 1)[1])
    assert config["permission"] == {"webfetch": "deny", "websearch": "deny"}
    for agent in ("general", "explore"):
        assert config["agent"][agent]["permission"] == {"webfetch": "deny", "websearch": "deny"}
    assert DockerOpenCodeLauncher.offline_tools["env"] == offline.OPENCODE_OFFLINE_ENV


class Launched(Exception):
    pass


class RecordingLauncher:
    offline_tools: ClassVar[dict[str, Any]] = {"env": {"X": "1"}}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def launch(self, dispatch_id: str, workspace: Path, native_home: Path, **kw: Any) -> Any:
        self.calls.append(kw)
        raise Launched  # the server itself is out of scope here


def opencode_port(tmp_path: Path, launcher: Any) -> PerDispatchOpenCodePort:
    home = tmp_path / "scoped"
    for rel in (AUTH, CATALOG):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("{}")
    return PerDispatchOpenCodePort(
        version="1.17.13", launcher=launcher, credential=ScopedOpenCodeHome(home),
        journal=SessionJournal(tmp_path / "journal"), native_root=tmp_path / "native",
        provider_id="opencode-go", model_id="m",
    )  # fmt: skip


def test_the_opencode_port_passes_the_trial_flag_to_its_launcher(tmp_path: Path) -> None:
    launcher = RecordingLauncher()
    port = opencode_port(tmp_path, launcher)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert port.accepts_options is True and offline.require_port(port) == {"env": {"X": "1"}}
    for options in (None, DispatchOptions.default(MODEL), TRIAL):
        with pytest.raises(Launched):
            port.prepare({"dispatch_id": "dispatch-a"}, "p", ws, options=options)
    assert launcher.calls == [{}, {}, {"offline": True}]  # a real goal: called as before
    effort = DispatchOptions(MODEL, "high")
    assert hold_code(port.prepare, {"dispatch_id": "d"}, "p", ws, options=effort) == (
        "DRIVER_OPTIONS_UNSUPPORTED"
    )


def test_an_opencode_launcher_without_a_declaration_makes_the_port_undeclared(
    tmp_path: Path,
) -> None:
    port = opencode_port(tmp_path, PendingDockerLauncher())
    assert hold_code(offline.require_port, port) == "DRIVER_WEB_UNDECLARED"


# -- the contract -----------------------------------------------------------------------------
class FutureDriverPort:
    """A driver added later that has not said how its web tools are off."""

    driver_id = "future-cli"
    accepts_options = True


def test_a_driver_without_a_declaration_is_held(tmp_path: Path) -> None:
    assert hold_code(offline.require_port, FutureDriverPort()) == "DRIVER_WEB_UNDECLARED"
    # a declaration that turns nothing off is no declaration
    for bad in ({}, {"argv": []}, {"env": {}}, {"other": ["x"]}, {"no_tools": "x", "argv": ["y"]}):
        port = FutureDriverPort()
        port.offline_tools = bad  # type: ignore[attr-defined]
        assert hold_code(offline.require_port, port) == "DRIVER_WEB_UNDECLARED", bad
    # a plain CliPort cannot pass the trial flag to its driver: undeclared
    assert hold_code(offline.require_port, CliPort(driver(tmp_path, "codex"))) == (
        "DRIVER_WEB_UNDECLARED"
    )

    # declared settings on a port that takes no options cannot be applied: held too
    class NoOptions(OptionsCliPort):
        accepts_options = False

    assert hold_code(offline.require_port, NoOptions(driver(tmp_path, "claude"))) == (
        "DRIVER_WEB_UNDECLARED"
    )
    # a model-less port declares it has nothing to turn off
    assert offline.require_port(RecipePort(SessionJournal(tmp_path / "j"))) == {
        "no_tools": "deterministic recipe; no model agent"
    }


def test_the_trial_executor_holds_an_undeclared_driver_before_any_goal() -> None:
    from amplai_foundry.meta_harness.local_executor import LocalTrialExecutor

    ports = {"declared": OptionsCliPort.__new__(OptionsCliPort), "future": FutureDriverPort()}
    ports["declared"].driver = SimpleNamespace(offline_tools={"argv": ["--off"]})  # type: ignore[attr-defined]
    executor: Any = LocalTrialExecutor.__new__(LocalTrialExecutor)
    executor.scope = "scope"
    executor.store = SimpleNamespace(get=lambda scope, kind, ref: {"driver_profile_ref": ref})
    executor.loop = SimpleNamespace(coordinator=SimpleNamespace(registry=SimpleNamespace(
        installed=lambda scope, ref: ports.get(ref)
    )))  # fmt: skip
    executor._require_offline("declared")
    executor._require_offline("not-installed")  # left to the dispatch (DRIVER_NOT_INSTALLED)
    assert hold_code(executor._require_offline, "future") == "DRIVER_WEB_UNDECLARED"


# -- qualification argv -----------------------------------------------------------------------
def test_the_claude_qualification_argv_denies_the_network_tools() -> None:
    """Operator decision 2026-10-08: every test turn runs with the web tools off, the container
    qualification too (`scripts/container_qualify.py`; the Codex vector is checked against the
    trial dispatch in `test_rc06_foundation.py`)."""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "container_qualify.py"
    spec = importlib.util.spec_from_file_location("container_qualify_web_off", path)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    turns = SimpleNamespace(driver="claude", model=MODEL)
    argv = script.ContainerTurns.argv(turns, "P", tools="Bash")
    at = argv.index("--allowedTools") + 2
    assert argv[at : at + 2] == list(offline.CLAUDE_OFFLINE)
    assert "--strict-mcp-config" in argv
