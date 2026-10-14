"""Work 034 S3 — the ``service_manager`` module kind: launchd and systemd render only (C-8, D-118).

- The kind has interface v1 (render + printed commands) and a conformance suite; launchd and
  systemd (system and user scope) are built-ins that pass it and only then can be enabled (M-2).
  The suite fails an implementation that breaks a v1 rule.
- launchd keeps today's labels: the tick plist equals the one ``meta nightly print-agent`` fills
  from the repository template; the server agent is ``ai.amplai.local-serve`` with ``RunAtLoad``
  and ``KeepAlive``; stop is ``bootout`` (``KeepAlive`` does not bring it back).
- systemd: system unit first (``User=``, ``/etc/systemd/system``), user unit + linger second;
  hardening lines of plan §3.5a.1; the tick timer ``OnCalendar=*:0/5``; values quoted.
- Nothing is written or run: rendering and every action's commands run with process creation
  disabled; the CLI prints and writes no file.
- Configuration: absent ``service`` is the OS default, ``null`` is no module, the entry is strict.
- Removal test (M-13): the core and the CLI import without both implementations.
"""

from __future__ import annotations

import json
import os
import plistlib
import pwd
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from test_034_s1a_intake_route import init_home
from typer.testing import CliRunner

from amplai_foundry.runtime import cli
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.local_deployment import (
    LocalConfig,
    ServiceEntry,
    default_service_manager,
    service_manager,
)
from amplai_foundry.runtime.meta_commands.nightly import print_agent
from amplai_foundry.runtime.modules import KINDS, ConformanceLedger, ModuleRegistry
from amplai_foundry.runtime.service_manager import (
    ACTIONS,
    SUITE,
    HostContext,
    RenderedFile,
    ServiceUnit,
    amplai_units,
    freeze_sequence,
    unfreeze_sequence,
)
from amplai_foundry.runtime.service_manager.launchd import LaunchdManager
from amplai_foundry.runtime.service_manager.launchd import spec as launchd_spec
from amplai_foundry.runtime.service_manager.systemd import SystemdManager, quote
from amplai_foundry.runtime.service_manager.systemd import spec as systemd_spec

EXE = "/opt/amplai/bin/amplai"
HOST = HostContext(home="/home/amplai", path_env="/usr/bin:/bin", uid=1001, account="amplai")


def units(meta: bool = True) -> tuple[ServiceUnit, ...]:
    return amplai_units(
        EXE, "/home/amplai/.amplai/local/local.json",
        "/home/amplai/.amplai/meta/local.json" if meta else None,
        server_logs="/home/amplai/.amplai/local/logs", meta_logs="/home/amplai/.amplai/meta/logs",
    )  # fmt: skip


MANAGERS = [LaunchdManager(), SystemdManager("system"), SystemdManager("user")]


# -- the kind and its conformance suite ----------------------------------------------------------


def test_service_manager_is_a_kind_with_two_built_in_in_process_modules() -> None:
    assert "service_manager" in KINDS
    for spec, module_id in ((launchd_spec(), "service_manager.launchd"),
                            (systemd_spec(), "service_manager.systemd")):  # fmt: skip
        assert (spec.kind, spec.module_id, spec.placement) == (
            "service_manager", module_id, "in_process",
        )  # fmt: skip
        assert spec.conformance_suite == SUITE.suite_id == "conformance.service_manager.v1"


@pytest.mark.parametrize("manager", MANAGERS, ids=["launchd", "systemd-system", "systemd-user"])
def test_each_manager_passes_the_suite_and_only_then_can_be_enabled(manager: Any) -> None:
    ledger = ConformanceLedger()
    ledger.register_suite(SUITE)
    registry = ModuleRegistry(ledger)
    with pytest.raises(Hold) as held:
        registry.enable(*manager.spec.key)
    assert held.value.code == "MODULE_UNQUALIFIED"
    result = ledger.run(manager.spec, manager)
    assert result.passed, result.failure
    assert registry.enable(*manager.spec.key) == manager.spec


class SkipsChecks(LaunchdManager):
    def render(self, units: Sequence[ServiceUnit], host: HostContext) -> tuple[RenderedFile, ...]:
        return tuple(RenderedFile(u.name, u.name, f"/x/{u.name}", " ".join(u.argv)) for u in units)


class FreezeStopsServer(SystemdManager):
    def commands(
        self, action: str, units: Sequence[ServiceUnit], host: HostContext
    ) -> tuple[tuple[str, ...], ...]:
        if action == "timer_off":
            return super().commands("stop", units, host)
        return super().commands(action, units, host)


class Unstable(LaunchdManager):
    calls = 0

    def render(self, units: Sequence[ServiceUnit], host: HostContext) -> tuple[RenderedFile, ...]:
        Unstable.calls += 1
        files = super().render(units, host)
        return tuple(RenderedFile(f.unit, f.filename, f.destination, f.content + str(self.calls))
                     for f in files)  # fmt: skip


@pytest.mark.parametrize("broken", [SkipsChecks, FreezeStopsServer, Unstable])
def test_the_suite_fails_a_manager_that_breaks_a_v1_rule(broken: type) -> None:
    ledger = ConformanceLedger()
    ledger.register_suite(SUITE)
    module = broken()
    result = ledger.run(module.spec, module)
    assert not result.passed and result.failure is not None
    assert result.failure["code"] == "CONFORMANCE_FAILED"


# -- launchd ---------------------------------------------------------------------------------------


def files_by_name(files: tuple[RenderedFile, ...]) -> dict[str, RenderedFile]:
    return {f.filename: f for f in files}


def test_launchd_tick_equals_the_print_agent_plist() -> None:
    meta = Path("~/.amplai/meta/local.json").expanduser().absolute()
    rendered = amplai_units(EXE, "/srv/local.json", str(meta), server_logs="/srv/logs",
                            meta_logs=str(meta.parent / "logs"))  # fmt: skip
    host = HostContext(home=str(Path.home()), path_env="/usr/bin:/bin", uid=os.getuid())
    files = files_by_name(LaunchdManager().render(rendered, host))
    tick = files["ai.amplai.meta-nightly.plist"]
    expected = print_agent(amplai=EXE, config=meta, path_env="/usr/bin:/bin")
    assert plistlib.loads(tick.content.encode()) == plistlib.loads(expected.encode())
    assert tick.destination == str(
        Path.home() / "Library/LaunchAgents/ai.amplai.meta-nightly.plist"
    )


def test_launchd_server_agent_keeps_the_local_serve_label_and_keep_alive() -> None:
    files = files_by_name(LaunchdManager().render(units(), HOST))
    server = plistlib.loads(files["ai.amplai.local-serve.plist"].content.encode())
    assert server["Label"] == cli.LAUNCHD_LABEL == "ai.amplai.local-serve"
    assert server["ProgramArguments"] == [
        EXE, "ops", "local-serve", "--config", "/home/amplai/.amplai/local/local.json",
    ]  # fmt: skip
    assert server["RunAtLoad"] is True and server["KeepAlive"] is True
    assert server["StandardOutPath"] == "/home/amplai/.amplai/local/logs/local-serve.out.log"
    assert server["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}


def test_launchd_values_are_escaped_by_plistlib() -> None:
    odd = amplai_units(EXE, "/a/<b>&\"c'.json", None, server_logs="/l", meta_logs=None)
    (file,) = LaunchdManager().render(odd, HOST)
    assert plistlib.loads(file.content.encode())["ProgramArguments"][-1] == "/a/<b>&\"c'.json"


def test_launchd_commands_bootstrap_and_bootout_in_the_gui_domain() -> None:
    m, all_units = LaunchdManager(), units()
    plist = "/home/amplai/Library/LaunchAgents/"
    assert m.commands("install", all_units, HOST) == (
        ("launchctl", "bootstrap", "gui/1001", plist + "ai.amplai.local-serve.plist"),
        ("launchctl", "bootstrap", "gui/1001", plist + "ai.amplai.meta-nightly.plist"),
    )
    assert m.commands("stop", all_units, HOST) == (
        ("launchctl", "bootout", "gui/1001/ai.amplai.local-serve"),
        ("launchctl", "bootout", "gui/1001/ai.amplai.meta-nightly"),
    )
    assert m.commands("timer_off", all_units, HOST) == (
        ("launchctl", "bootout", "gui/1001/ai.amplai.meta-nightly"),
    )
    assert m.commands("status", all_units[:1], HOST) == (
        ("launchctl", "print", "gui/1001/ai.amplai.local-serve"),
    )
    with pytest.raises(Hold) as held:
        m.commands("stop", all_units, HostContext(home="/h", path_env=""))
    assert held.value.code == "SERVICE_HOST"


# -- systemd ---------------------------------------------------------------------------------------


def test_systemd_system_units_run_as_the_amplai_account_with_hardening() -> None:
    files = files_by_name(SystemdManager("system").render(units(), HOST))
    assert set(files) == {"amplai-server.service", "amplai-meta-tick.service",
                          "amplai-meta-tick.timer"}  # fmt: skip
    server = files["amplai-server.service"]
    assert server.destination == "/etc/systemd/system/amplai-server.service"
    lines = server.content.splitlines()
    for line in ("User=amplai", "Restart=on-failure", "NoNewPrivileges=yes", "PrivateTmp=yes",
                 "UMask=0077", "WantedBy=multi-user.target",
                 'Environment="PATH=/usr/bin:/bin"'):  # fmt: skip
        assert line in lines
    assert ('ExecStart="/opt/amplai/bin/amplai" "ops" "local-serve" "--config" '
            '"/home/amplai/.amplai/local/local.json"') in lines  # fmt: skip
    tick = files["amplai-meta-tick.service"].content.splitlines()
    assert "Type=oneshot" in tick and "Restart=on-failure" not in tick
    assert not any(line.startswith("WantedBy") for line in tick)  # the timer starts it
    timer = files["amplai-meta-tick.timer"].content.splitlines()
    assert "OnCalendar=*:0/5" in timer and "Unit=amplai-meta-tick.service" in timer
    assert "WantedBy=timers.target" in timer


def test_systemd_user_units_go_to_the_account_and_need_linger() -> None:
    m = SystemdManager("user")
    files = files_by_name(m.render(units(), HOST))
    server = files["amplai-server.service"]
    assert server.destination == "/home/amplai/.config/systemd/user/amplai-server.service"
    assert "WantedBy=default.target" in server.content.splitlines()
    assert not any(line.startswith("User=") for line in server.content.splitlines())
    assert m.commands("install", units(), HOST) == (
        ("loginctl", "enable-linger", "amplai"),
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", "amplai-server.service",
         "amplai-meta-tick.timer"),
    )  # fmt: skip


def test_systemd_commands_stop_the_clock_and_never_restart() -> None:
    m, all_units = SystemdManager("system"), units()
    assert m.commands("timer_off", all_units, HOST) == (  # a tick in progress ends too
        ("systemctl", "stop", "amplai-meta-tick.timer", "amplai-meta-tick.service"),
    )
    assert m.commands("stop", all_units, HOST) == (
        ("systemctl", "stop", "amplai-meta-tick.timer", "amplai-meta-tick.service",
         "amplai-server.service"),
    )  # fmt: skip
    assert m.commands("start", all_units, HOST) == (
        ("systemctl", "start", "amplai-server.service", "amplai-meta-tick.timer"),
    )
    assert m.commands("timer_off", all_units[:1], HOST) == ()  # no clock among the units
    assert not any("restart" in a for line in (m.commands(x, all_units, HOST) for x in ACTIONS)
                   for argv in line for a in argv)  # fmt: skip


def test_a_system_unit_needs_the_amplai_account() -> None:
    no_account = HostContext(home="/home/amplai", path_env="/usr/bin")
    for call in (
        lambda: SystemdManager("system").render(units(), no_account),
        lambda: SystemdManager("system").commands("stop", units(), no_account),
        lambda: SystemdManager("user").commands("install", units(), no_account),
    ):
        with pytest.raises(Hold) as held:
            call()
        assert held.value.code == "SERVICE_ACCOUNT"  # fmt: skip


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("/a b/c", '"/a b/c"'),
        ("100%", '"100%%"'),
        ("$HOME", '"$$HOME"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("a\\b", '"a\\\\b"'),
    ],
)
def test_systemd_quotes_every_word(value: str, quoted: str) -> None:
    assert quote(value) == quoted


def test_environment_keeps_dollar_and_description_escapes_percent() -> None:
    host = HostContext(home="/home/amplai", path_env="/opt/$x/bin:/b%c", uid=1, account="amplai")
    unit = ServiceUnit("amplai-x", (EXE, "$HOME", "50%"), "periodic", "load 50%", 300)
    files = files_by_name(SystemdManager("system").render((unit,), host))
    service = files["amplai-x.service"].content.splitlines()
    assert 'Environment="PATH=/opt/$x/bin:/b%%c"' in service  # Environment= does not expand $
    assert 'ExecStart="/opt/amplai/bin/amplai" "$$HOME" "50%%"' in service
    assert "Description=load 50%%" in service
    assert "Description=load 50%% (every 5 min)" in files["amplai-x.timer"].content.splitlines()
    assert quote("$X", expand=False) == '"$X"'


# -- freeze and unfreeze: each unit once, clock first ---------------------------------------------


def test_launchd_freeze_and_unfreeze_name_each_agent_once() -> None:
    m, all_units = LaunchdManager(), units()
    plist = "/home/amplai/Library/LaunchAgents/"
    assert freeze_sequence(m, all_units, HOST) == (
        ("launchctl", "bootout", "gui/1001/ai.amplai.meta-nightly"),
        ("launchctl", "bootout", "gui/1001/ai.amplai.local-serve"),
    )
    assert unfreeze_sequence(m, all_units, HOST) == (
        ("launchctl", "bootstrap", "gui/1001", plist + "ai.amplai.local-serve.plist"),
        ("launchctl", "bootstrap", "gui/1001", plist + "ai.amplai.meta-nightly.plist"),
    )


@pytest.mark.parametrize(("scope", "ctl"), [("system", ("systemctl",)),
                                            ("user", ("systemctl", "--user"))])  # fmt: skip
def test_systemd_freeze_and_unfreeze_name_each_unit_once(scope: Any, ctl: tuple[str, ...]) -> None:
    m, all_units = SystemdManager(scope), units()
    assert freeze_sequence(m, all_units, HOST) == (
        (*ctl, "stop", "amplai-meta-tick.timer", "amplai-meta-tick.service"),
        (*ctl, "stop", "amplai-server.service"),
    )
    assert unfreeze_sequence(m, all_units, HOST) == (
        (*ctl, "start", "amplai-server.service"),
        (*ctl, "start", "amplai-meta-tick.timer"),
    )


def test_freeze_without_a_meta_deployment_stops_only_the_server() -> None:
    assert freeze_sequence(LaunchdManager(), units(meta=False), HOST) == (
        ("launchctl", "bootout", "gui/1001/ai.amplai.local-serve"),
    )
    assert unfreeze_sequence(SystemdManager("system"), units(meta=False), HOST) == (
        ("systemctl", "start", "amplai-server.service"),
    )


# -- host values ----------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["\t", "\x1b[0m", "\x7f", "\x0b", "\r"])
@pytest.mark.parametrize("manager", MANAGERS[:2], ids=["launchd", "systemd"])
def test_every_control_character_is_refused(manager: Any, bad: str) -> None:
    for unit in (ServiceUnit("amplai-x", (EXE, "a" + bad), "daemon", "x"),
                 ServiceUnit("amplai-x", (EXE,), "daemon", "x" + bad)):  # fmt: skip
        with pytest.raises(Hold) as held:
            manager.render((unit,), HOST)
        assert held.value.code == "SERVICE_UNIT_VALUE"
    with pytest.raises(Hold) as held:
        manager.render(units(), HostContext("/home/a", "/bin" + bad, 1, "amplai"))
    assert held.value.code == "SERVICE_UNIT_VALUE"


@pytest.mark.parametrize("account", ["root", "Amplai", "a b", "-x", "a" * 33, "a:b", "1abc"])
@pytest.mark.parametrize("manager", MANAGERS, ids=["launchd", "systemd-system", "systemd-user"])
def test_the_account_is_a_posix_user_name_and_not_root(manager: Any, account: str) -> None:
    with pytest.raises(Hold) as held:
        manager.render(units(), HostContext("/home/a", "/bin", 1, account))
    assert held.value.code == "SERVICE_ACCOUNT"


@pytest.mark.parametrize("manager", MANAGERS, ids=["launchd", "systemd-system", "systemd-user"])
def test_a_relative_home_is_refused(manager: Any) -> None:
    with pytest.raises(Hold) as held:
        manager.render(units(), HostContext("home/amplai", "/bin", 1, "amplai"))
    assert held.value.code == "SERVICE_HOST"


@pytest.mark.parametrize("seconds", [90, 420, 7200])
def test_a_systemd_timer_interval_is_whole_minutes_dividing_an_hour(seconds: int) -> None:
    unit = ServiceUnit("amplai-x", (EXE,), "periodic", "x", seconds)
    with pytest.raises(Hold) as held:
        SystemdManager("system").render((unit,), HOST)
    assert held.value.code == "SERVICE_UNIT_FORM"
    assert LaunchdManager().render((unit,), HOST)  # launchd takes any interval of 60 s or more


# -- nothing is written or run ---------------------------------------------------------------------


def test_rendering_and_commands_never_start_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a service manager ran a process")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, refuse)
    monkeypatch.setattr(os, "system", refuse)
    for m in MANAGERS:
        m.render(units(), HOST)
        for action in ACTIONS:
            m.commands(action, units(), HOST)


def run_cli(*args: str) -> Any:
    return CliRunner().invoke(cli.app, list(args))


def test_service_print_renders_units_and_commands_and_writes_nothing(
    tmp_path: Path, accounts: dict[str, Any]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = run_cli(
        "ops", "service", "print", "--manager", "systemd", "--scope", "user",
        "--account", "remoteacct", "--amplai", EXE, "--home", str(home), "--path-env", "/usr/bin",
        "--config", str(tmp_path / "srv/local.json"),
        "--meta-config", str(tmp_path / "meta/local.json"),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["manager"] == "service_manager.systemd@1" and body["scope"] == "user"
    assert body["installs_nothing"] is True
    assert [f["filename"] for f in body["files"]] == [
        "amplai-server.service", "amplai-meta-tick.service", "amplai-meta-tick.timer",
    ]  # fmt: skip
    assert set(body["commands"]) == set(ACTIONS)
    assert body["commands"]["timer_off"] == [
        "systemctl --user stop amplai-meta-tick.timer amplai-meta-tick.service"
    ]
    assert list(home.iterdir()) == [] and sorted(p.name for p in tmp_path.iterdir()) == ["home"]


def test_service_print_raw_prints_one_file_for_redirecting(tmp_path: Path) -> None:
    common = ("ops", "service", "print", "--manager", "launchd", "--amplai", EXE,
              "--config", str(tmp_path / "local.json"), "--no-meta")  # fmt: skip
    result = run_cli(*common, "--raw", "ai.amplai.local-serve.plist")
    assert result.exit_code == 0, result.output
    assert plistlib.loads(result.output.encode())["Label"] == "ai.amplai.local-serve"
    missing = run_cli(*common, "--raw", "ai.amplai.meta-nightly.plist")  # --no-meta: no tick
    assert missing.exit_code == 3 and json.loads(missing.output)["code"] == "SERVICE_FILE"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--manager", "supervisor"), "SERVICE_MANAGER_UNKNOWN"),
        (("--manager", "systemd"), "SERVICE_ACCOUNT"),  # system scope without --account
        (("--manager", "systemd", "--scope", "global", "--account", "a"), "SERVICE_SCOPE"),
    ],
)
def test_service_print_holds(tmp_path: Path, args: tuple[str, ...], code: str) -> None:
    result = run_cli("ops", "service", "print", "--amplai", EXE, "--no-meta",
                     "--config", str(tmp_path / "local.json"), *args)  # fmt: skip
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["code"] == code


def test_the_tick_never_renders_with_the_server_config(tmp_path: Path) -> None:
    same = str(tmp_path / "local.json")
    result = run_cli("ops", "service", "print", "--manager", "launchd", "--amplai", EXE,
                     "--config", same, "--meta-config", same)  # fmt: skip
    assert result.exit_code == 3 and json.loads(result.output)["code"] == "NIGHT_DEPLOYMENT"


# -- whose uid and home: the account's, not the invoking user's (sudo) ----------------------------


@pytest.fixture
def accounts(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """This machine's accounts as ``pwd`` sees them: only ``amplai`` (uid 1500)."""
    known = {"amplai": SimpleNamespace(pw_uid=1500, pw_dir="/srv/amplai")}

    def getpwnam(name: str) -> Any:
        if name not in known:
            raise KeyError(name)
        return known[name]

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    return known


def launchd_print(tmp_path: Path, *args: str) -> Any:
    return run_cli("ops", "service", "print", "--manager", "launchd", "--amplai", EXE,
                   "--config", str(tmp_path / "local.json"),
                   "--meta-config", str(tmp_path / "meta/local.json"), *args)  # fmt: skip


def test_under_sudo_without_an_account_nothing_is_rendered(
    tmp_path: Path, accounts: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0)
    result = launchd_print(tmp_path)
    assert result.exit_code == 3 and json.loads(result.output)["code"] == "SERVICE_ACCOUNT"


def test_the_account_gives_the_uid_and_home_even_under_sudo(
    tmp_path: Path, accounts: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0)
    result = launchd_print(tmp_path, "--account", "amplai")
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["commands"]["stop"][0] == "launchctl bootout gui/1500/ai.amplai.local-serve"
    assert body["files"][0]["destination"] == (
        "/srv/amplai/Library/LaunchAgents/ai.amplai.local-serve.plist"
    )


def test_home_must_match_a_known_account_and_is_required_for_an_unknown_one(
    tmp_path: Path, accounts: dict[str, Any]
) -> None:
    other = launchd_print(tmp_path, "--account", "amplai", "--home", "/home/else")
    assert other.exit_code == 3 and json.loads(other.output)["code"] == "SERVICE_HOST"
    unknown = launchd_print(tmp_path, "--account", "remoteacct")
    assert unknown.exit_code == 3 and json.loads(unknown.output)["code"] == "SERVICE_HOST"
    # an account of another host: files render with its --home, launchd commands need its uid
    remote = launchd_print(tmp_path, "--account", "remoteacct", "--home", "/home/remoteacct")
    assert remote.exit_code == 3 and json.loads(remote.output)["code"] == "SERVICE_HOST"
    systemd = run_cli("ops", "service", "print", "--manager", "systemd", "--amplai", EXE,
                      "--account", "remoteacct", "--home", "/home/remoteacct", "--no-meta",
                      "--config", str(tmp_path / "local.json"))  # fmt: skip
    assert systemd.exit_code == 0, systemd.output
    relative = launchd_print(tmp_path, "--account", "remoteacct", "--home", "rel/home")
    assert relative.exit_code == 3 and json.loads(relative.output)["code"] == "SERVICE_HOST"


# -- configuration -------------------------------------------------------------------------------


def config(home: Path, **change: Any) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((home / "local.json").read_text())
    value.update(change)
    return value


def test_absent_service_is_the_os_default_and_null_is_none(tmp_path: Path) -> None:
    home = init_home(tmp_path)
    assert "service" not in json.loads((home / "local.json").read_text())  # local-init output
    assert LocalConfig.model_validate(config(home)).service == ServiceEntry()
    assert LocalConfig.model_validate(config(home, service=None)).service is None
    with pytest.raises(Hold) as held:
        service_manager(None)
    assert held.value.code == "SERVICE_MANAGER_OFF"
    chosen = service_manager(ServiceEntry(manager="service_manager.systemd", systemd_scope="user"))
    assert isinstance(chosen, SystemdManager) and chosen.scope == "user"
    assert isinstance(service_manager(None, "service_manager.launchd"), LaunchdManager)


@pytest.mark.parametrize(
    "entry",
    [{"manager": "service_manager.supervisor"}, {"manager": "launchd"},
     {"systemd_scope": "global"}, {"manager": "service_manager.systemd", "unit": "x"}],
)  # fmt: skip
def test_a_service_entry_is_strict(tmp_path: Path, entry: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LocalConfig.model_validate(config(init_home(tmp_path), service=entry))


def test_the_os_default_is_launchd_on_macos_and_systemd_when_it_is_pid_1(tmp_path: Path) -> None:
    comm = tmp_path / "comm"
    assert default_service_manager("darwin", comm) == "service_manager.launchd"
    comm.write_text("systemd\n")
    assert default_service_manager("linux", comm) == "service_manager.systemd"
    comm.write_text("init\n")
    for platform, pid1 in (("linux", comm), ("linux", tmp_path / "none"), ("win32", comm)):
        with pytest.raises(RuntimeFault) as held:
            default_service_manager(platform, pid1)
        assert held.value.code == "SERVICE_MANAGER_UNAVAILABLE"  # the supervisor is not built


# -- removal test (M-13) ---------------------------------------------------------------------------


def test_the_core_and_cli_import_without_the_service_manager_modules() -> None:
    code = (
        "import sys\n"
        "sys.modules['amplai_foundry.runtime.service_manager.launchd'] = None\n"
        "sys.modules['amplai_foundry.runtime.service_manager.systemd'] = None\n"
        "import amplai_foundry.runtime.local_deployment\n"
        "import amplai_foundry.runtime.cli\n"
        "import amplai_foundry.runtime.upgrade.plan\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()[-2000:]
