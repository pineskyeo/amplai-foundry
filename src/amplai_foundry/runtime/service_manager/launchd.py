"""launchd service manager: per-user LaunchAgents on macOS (Work 034 S3; C-8, D-118).

Renders one agent plist per unit and prints the ``launchctl`` commands; it writes and runs
nothing. Labels keep the names the existing agents use, so an installed agent keeps working:

- ``amplai-server`` → ``ai.amplai.local-serve`` (``RunAtLoad``, ``KeepAlive``; the agent of
  ``docs/v3/USING_AMPLAI_WORK.ko.md:59`` and the ``local-update`` default label,
  ``runtime/cli.py:927``);
- ``amplai-meta-tick`` → ``ai.amplai.meta-nightly`` (``StartInterval`` 300; the agent
  ``meta nightly print-agent`` fills from
  ``deployment/launchd/ai.amplai.meta-nightly.plist.template``).
- any other ``amplai-<x>`` → ``ai.amplai.<x>``.

The plist is built with ``plistlib`` (values escaped by it), not read from the repository, so it
renders the same from an installed wheel. Logs go to ``<log_dir>/<label suffix>.{out,err}.log``.

Commands (``gui/<uid>`` domain, the agent file at its destination):

- install, start, timer_on: ``launchctl bootstrap gui/<uid> <file>``
- stop, timer_off: ``launchctl bootout gui/<uid>/<label>``. ``bootout`` unloads the agent, so
  ``KeepAlive`` does not start it again (a ``kill`` or ``kickstart`` would).
- status: ``launchctl print gui/<uid>/<label>``
"""

from __future__ import annotations

import plistlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..errors import Hold
from ..modules.builtin import BUILTIN_MODULES
from ..modules.spec import ModuleSpec
from .interface import (
    META_TICK,
    SERVER,
    HostContext,
    RenderedFile,
    ServiceUnit,
    check_action,
    check_units,
)

MODULE_ID = "service_manager.launchd"
LABELS = {SERVER: "ai.amplai.local-serve", META_TICK: "ai.amplai.meta-nightly"}


def spec() -> ModuleSpec:
    (found,) = (s for s in BUILTIN_MODULES if s.module_id == MODULE_ID)
    return found


class LaunchdManager:
    def __init__(self) -> None:
        self._spec = spec()

    @property
    def spec(self) -> ModuleSpec:
        return self._spec

    def unit_id(self, unit: ServiceUnit) -> str:
        return LABELS.get(unit.name, "ai.amplai." + unit.name.removeprefix("amplai-"))

    def _destination(self, unit: ServiceUnit, host: HostContext) -> str:
        return str(Path(host.home) / "Library" / "LaunchAgents" / (self.unit_id(unit) + ".plist"))

    def _plist(self, unit: ServiceUnit, host: HostContext) -> bytes:
        label = self.unit_id(unit)
        value: dict[str, Any] = {"Label": label, "ProgramArguments": list(unit.argv)}
        if unit.form == "periodic":
            value["StartInterval"] = unit.interval_seconds
        else:
            value["RunAtLoad"] = True
            value["KeepAlive"] = True
        if unit.log_dir:
            stem = str(Path(unit.log_dir) / label.removeprefix("ai.amplai."))
            value["StandardOutPath"] = stem + ".out.log"
            value["StandardErrorPath"] = stem + ".err.log"
        value["EnvironmentVariables"] = {"PATH": host.path_env}
        return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)

    def render(self, units: Sequence[ServiceUnit], host: HostContext) -> tuple[RenderedFile, ...]:
        check_units(units, host)
        return tuple(
            RenderedFile(
                unit.name,
                self.unit_id(unit) + ".plist",
                self._destination(unit, host),
                self._plist(unit, host).decode(),
            )
            for unit in units
        )

    def commands(
        self, action: str, units: Sequence[ServiceUnit], host: HostContext
    ) -> tuple[tuple[str, ...], ...]:
        check_action(action)
        check_units(units, host)
        if host.uid is None:
            raise Hold("SERVICE_HOST", "launchd commands need the account's uid (gui/<uid>)")
        domain = f"gui/{host.uid}"
        chosen = [u for u in units if u.form == "periodic"] if action.startswith("timer") else units
        out: list[tuple[str, ...]] = []
        for unit in chosen:
            if action in ("install", "start", "timer_on"):
                out.append(("launchctl", "bootstrap", domain, self._destination(unit, host)))
            elif action in ("stop", "timer_off"):
                out.append(("launchctl", "bootout", f"{domain}/{self.unit_id(unit)}"))
            else:
                out.append(("launchctl", "print", f"{domain}/{self.unit_id(unit)}"))
        return tuple(out)


def subject(workdir: Path) -> LaunchdManager:
    """The module for its conformance suite: rendering needs no transport."""
    del workdir
    return LaunchdManager()
