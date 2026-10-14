"""The ``service_manager`` module kind, interface v1 (Work 034 S3; spec C-8, U-2; D-118).

A service manager keeps AMPLAI's long-running processes and the nightly clock running on a host:
launchd on macOS, systemd on Linux (system unit first, user unit + linger second) and, for hosts
without systemd or sudo, the AMPLAI supervisor (``plan.md`` §3.5a.1). AMPLAI never installs a
unit: it prints the files and the commands, and the operator runs them (C-8, as
``meta nightly print-agent`` before, ``runtime/meta_commands/nightly.py:18-19``).

Interface v1 is that print-only part of the plan's interface (``render(units) -> files``) plus
the commands each lifecycle action would run, as argv lists that are printed and never run::

    unit_id(unit) -> str                         the manager's name of a unit
    render(units, host) -> tuple[RenderedFile]   the unit files, nothing written
    commands(action, units, host) -> tuple[tuple[str, ...]]
        action in ACTIONS: install, start, stop, timer_on, timer_off, status

The plan's executing operations (``status()``, ``stop(names)``, ``start(names)``, ``timer_off()``,
``timer_on()``, ``is_quiescent()``) belong to the freeze (U-2, S3 steps 2 and 9). They are not in
v1; they come with the freeze by raising this kind's interface version with a new suite.

Rules every implementation keeps (the suite checks each):

- A unit name is ``amplai-`` + ``[a-z0-9-]`` (plan §3.5a.1 "unit 이름은 ``amplai-`` 접두"); another
  name is ``Hold SERVICE_UNIT_NAME``.
- The executable (``argv[0]``) is an absolute path; else ``Hold SERVICE_EXEC_PATH``. A unit runs
  exactly the binary the operator named, never one a ``PATH`` lookup finds at run time.
- No value (argument, path, ``PATH``) holds a newline, carriage return or NUL: it would end the
  unit file's line and add a directive. ``Hold SERVICE_UNIT_VALUE``.
- A ``periodic`` unit has ``interval_seconds`` ≥ 60; a ``daemon`` unit has none.
  ``Hold SERVICE_UNIT_FORM``.
- Rendering is pure: the same input gives byte-equal files; nothing is written or run.
- An unknown action is ``RuntimeFault SERVICE_ACTION``.
- ``stop`` names every given unit; ``timer_off`` names every periodic unit and no daemon unit (the
  freeze stops the clock first, plan §3.5a.1 "동결 일반화").
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..errors import Hold, RuntimeFault
from ..modules.builtin import suite_id
from ..modules.conformance import ConformanceSuite
from ..modules.spec import INTERFACE_VERSIONS, Module

KIND = "service_manager"

Form = Literal["daemon", "periodic"]
Action = Literal["install", "start", "stop", "timer_on", "timer_off", "status"]
ACTIONS: tuple[str, ...] = ("install", "start", "stop", "timer_on", "timer_off", "status")

# plan.md §3.5a.1 managed units. The model gateway (S4c) has no command yet and the intake
# listener runs inside the server (S1g, D-133), so neither has a unit today.
SERVER = "amplai-server"
META_TICK = "amplai-meta-tick"
TICK_SECONDS = 300  # `amplai meta nightly tick` every five minutes (N-1, S1d)

_NAME = re.compile(r"amplai-[a-z0-9]+(?:-[a-z0-9]+)*")
_BAD = re.compile(r"[\r\n\x00]")


@dataclass(frozen=True)
class ServiceUnit:
    """One process the manager keeps: a ``daemon`` (restarted on failure) or a ``periodic`` job
    (run every ``interval_seconds``)."""

    name: str
    argv: tuple[str, ...]
    form: Form
    description: str
    interval_seconds: int | None = None
    # where launchd writes stdout/stderr; systemd sends them to journald (plan §3.5a.4)
    log_dir: str | None = None


@dataclass(frozen=True)
class HostContext:
    """What the operator tells the renderer about the host it renders for."""

    home: str  # the AMPLAI account's home (launchd agents, systemd user units)
    path_env: str  # PATH the units run with
    uid: int | None = None  # launchd ``gui/<uid>`` domain
    account: str | None = None  # systemd system unit ``User=`` (the AMPLAI account, Q-11)


@dataclass(frozen=True)
class RenderedFile:
    """A unit file: ``content`` to be written by the operator at ``destination``."""

    unit: str
    filename: str
    destination: str
    content: str


class ServiceManager(Module, Protocol):
    def unit_id(self, unit: ServiceUnit) -> str: ...

    def render(
        self, units: Sequence[ServiceUnit], host: HostContext
    ) -> tuple[RenderedFile, ...]: ...

    def commands(
        self, action: str, units: Sequence[ServiceUnit], host: HostContext
    ) -> tuple[tuple[str, ...], ...]: ...


# -- shared rules ---------------------------------------------------------------------------------


def check_units(units: Sequence[ServiceUnit], host: HostContext) -> None:
    """The v1 rules above, before anything is rendered."""
    seen: set[str] = set()
    for unit in units:
        if not _NAME.fullmatch(unit.name) or unit.name in seen:
            raise Hold(
                "SERVICE_UNIT_NAME",
                "A unit name is 'amplai-' + lowercase letters, digits and '-', once each",
                details={"unit": unit.name},
            )
        seen.add(unit.name)
        if not unit.argv or not Path(unit.argv[0]).is_absolute():
            raise Hold("SERVICE_EXEC_PATH", "A unit runs an absolute executable path",
                       details={"unit": unit.name})  # fmt: skip
        if unit.form == "periodic":
            ok = type(unit.interval_seconds) is int and unit.interval_seconds >= 60
        else:
            ok = unit.form == "daemon" and unit.interval_seconds is None
        if not ok:
            raise Hold("SERVICE_UNIT_FORM", "A periodic unit has an interval of at least 60 s; "
                       "a daemon unit has none", details={"unit": unit.name})  # fmt: skip
        values = (*unit.argv, unit.description, unit.log_dir or "")
        if any(_BAD.search(v) for v in values):
            raise Hold("SERVICE_UNIT_VALUE", "A unit value cannot hold a line break or NUL",
                       details={"unit": unit.name})  # fmt: skip
    values = (host.home, host.path_env, host.account or "")
    if any(_BAD.search(v) for v in values):
        raise Hold("SERVICE_UNIT_VALUE", "A host value cannot hold a line break or NUL")


def check_action(action: str) -> None:
    if action not in ACTIONS:
        raise RuntimeFault("SERVICE_ACTION", "unknown service action",
                           details={"action": action, "actions": list(ACTIONS)})  # fmt: skip


def amplai_units(
    amplai: str, server_config: str, meta_config: str | None, *, server_logs: str,
    meta_logs: str | None,
) -> tuple[ServiceUnit, ...]:  # fmt: skip
    """The units of plan §3.5a.1 that have a command today: the server (``ops local-serve``,
    notifier and S1g intake listener inside it) and, with a meta deployment, the tick."""
    units = [
        ServiceUnit(
            SERVER, (amplai, "ops", "local-serve", "--config", server_config), "daemon",
            "AMPLAI local product server (API, execution loop, notifier)", log_dir=server_logs,
        )
    ]  # fmt: skip
    if meta_config is not None:
        units.append(
            ServiceUnit(
                META_TICK, (amplai, "meta", "nightly", "tick", "--config", meta_config),
                "periodic", "AMPLAI nightly meta-harness tick", TICK_SECONDS, meta_logs,
            )
        )  # fmt: skip
    return tuple(units)


# -- conformance (M-2) ----------------------------------------------------------------------------

EXE = "/opt/amplai/bin/amplai"
HOST = HostContext(home="/home/amplai", path_env="/usr/bin:/bin", uid=501, account="amplai")


def conformance_units() -> tuple[ServiceUnit, ...]:
    return amplai_units(EXE, "/home/amplai/local/my config.json", "/home/amplai/meta/local.json",
                        server_logs="/home/amplai/logs", meta_logs="/home/amplai/logs")  # fmt: skip


def _fail(why: str) -> RuntimeFault:
    return RuntimeFault("CONFORMANCE_FAILED", why)


def _expect(code: str, call: Callable[[], object]) -> None:
    try:
        call()
    except RuntimeFault as fault:
        if fault.code != code:
            raise _fail(f"expected {code}, got {fault.code}") from None
        return
    raise _fail(f"expected {code}, nothing was refused")


def _expect_unit(code: str, unit: ServiceUnit, render: Callable[..., object],
                 commands: Callable[..., object]) -> None:  # fmt: skip
    _expect(code, lambda: render((unit,), HOST))
    _expect(code, lambda: commands("stop", (unit,), HOST))


def _named(command_lines: tuple[tuple[str, ...], ...], name: str) -> bool:
    return any(name in arg for argv in command_lines for arg in argv)


def _check(module: Module) -> None:
    ops = {}
    for name in ("unit_id", "render", "commands"):
        op = getattr(module, name, None)
        if not callable(op):
            raise _fail(f"a service manager has {name}")
        ops[name] = op
    unit_id, render, commands = ops["unit_id"], ops["render"], ops["commands"]
    units = conformance_units()
    server, tick = units
    files = render(units, HOST)
    if type(files) is not tuple or not all(type(f) is RenderedFile for f in files):
        raise _fail("render returns a tuple of RenderedFile")
    if render(units, HOST) != files:
        raise _fail("render is deterministic")
    if {f.unit for f in files} != {u.name for u in units}:
        raise _fail("render gives a file for every unit and none other")
    if len({f.destination for f in files}) != len(files):
        raise _fail("each file has its own destination")
    for file in files:
        if not Path(file.destination).is_absolute() or Path(file.destination).name != file.filename:
            raise _fail("a destination is an absolute path ending in the file name")
        if not file.content.strip():
            raise _fail("a rendered file has content")
    if not any(EXE in f.content for f in files if f.unit == server.name):
        raise _fail("the server unit runs the named executable")
    ids = {u.name: unit_id(u) for u in units}
    if len(set(ids.values())) != len(ids) or not all(
        isinstance(v, str) and v for v in ids.values()
    ):
        raise _fail("unit_id gives each unit its own name")
    # refusals before anything is rendered
    bad_name = ServiceUnit("server", server.argv, "daemon", "x")
    relative = ServiceUnit("amplai-x", ("amplai", "ops"), "daemon", "x")
    newline = ServiceUnit("amplai-x", (EXE, "--config", "a\nExecStartPre=/bin/sh"), "daemon", "x")
    no_interval = ServiceUnit("amplai-x", (EXE,), "periodic", "x")
    short = ServiceUnit("amplai-x", (EXE,), "periodic", "x", 5)
    for unit, code in ((bad_name, "SERVICE_UNIT_NAME"), (relative, "SERVICE_EXEC_PATH"),
                       (newline, "SERVICE_UNIT_VALUE"), (no_interval, "SERVICE_UNIT_FORM"),
                       (short, "SERVICE_UNIT_FORM")):  # fmt: skip
        _expect_unit(code, unit, render, commands)
    _expect("SERVICE_UNIT_NAME", lambda: render((server, server), HOST))
    _expect("SERVICE_UNIT_VALUE", lambda: render(units, HostContext("/h", "/bin\n", 1, "a")))
    _expect("SERVICE_ACTION", lambda: commands("restart", units, HOST))
    # commands: argv lists of text, never run
    for action in ACTIONS:
        lines = commands(action, units, HOST)
        if type(lines) is not tuple or not lines:
            raise _fail(f"{action} gives at least one command")
        if not all(type(a) is tuple and a and all(type(x) is str for x in a) for a in lines):
            raise _fail(f"{action} commands are tuples of text")
    if not all(_named(commands("stop", units, HOST), ids[u.name]) for u in units):
        raise _fail("stop names every unit")
    timer_off = commands("timer_off", units, HOST)
    if not _named(timer_off, ids[tick.name]) or _named(timer_off, ids[server.name]):
        raise _fail("timer_off names the periodic units and no daemon")
    if not _named(commands("stop", (server,), HOST), ids[server.name]) or _named(
        commands("stop", (server,), HOST), ids[tick.name]
    ):
        raise _fail("stop names only the given units")


SUITE = ConformanceSuite(
    suite_id=suite_id(KIND), kind=KIND, interface_version=INTERFACE_VERSIONS[KIND], check=_check
)
