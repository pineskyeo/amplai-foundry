"""systemd service manager: system units first, user units + linger second (Work 034 S3; C-8,
D-118, ``plan.md`` §3.5a.1).

Renders unit files and prints the ``systemctl`` commands; it writes and runs nothing. The
operator installs them (a system unit needs root once).

- ``scope="system"`` (first choice): files for ``/etc/systemd/system/``, ``User=<account>`` (the
  AMPLAI account; required, Q-11), ``WantedBy=multi-user.target``, commands ``systemctl ...``.
- ``scope="user"`` (second choice): files for ``<home>/.config/systemd/user/``,
  ``WantedBy=default.target``, commands ``systemctl --user ...``; install first runs
  ``loginctl enable-linger <account>`` so the units run without a login session. Whether the
  account may enable linger itself depends on the host's polkit (확인 필요, plan §3.5a.1).

Every service has ``NoNewPrivileges=yes``, ``PrivateTmp=yes`` and ``UMask=0077`` (plan §3.5a.1).
Stronger sandboxing (``ProtectSystem=strict``) is left out until the container qualification
shows it keeps the engine socket reachable (확인 필요). Whether ``PrivateTmp=`` works in a user
unit depends on the host's user-namespace support (확인 필요). stdout and stderr go to journald.

- ``daemon`` → ``<name>.service`` with ``Restart=on-failure``.
- ``periodic`` → ``<name>.service`` (``Type=oneshot``) + ``<name>.timer`` with
  ``OnCalendar=*:0/<minutes>`` (the tick: ``*:0/5``). The interval must be whole minutes that
  divide an hour; another is ``Hold SERVICE_UNIT_FORM``.

Commands: install = daemon-reload + ``enable --now`` (the daemon's service, the periodic unit's
timer); start acts on the daemon's service and the periodic unit's timer; stop and timer_off
stop the periodic unit's timer and its service (a tick in progress ends too); timer_on starts the
timers; status = ``systemctl status --no-pager``. ``systemctl stop`` does not trigger
``Restart=`` (plan §3.5a.1).

Quoting: every ``ExecStart=`` argument is one double-quoted word with ``\\`` and ``"`` escaped,
``%`` as ``%%`` (specifiers) and ``$`` as ``$$`` (``ExecStart=`` expands ``$VAR``). The
``Environment=`` assignment is quoted the same way except ``$``: systemd.exec(5) ``Environment=``
performs no variable expansion, so ``$`` stays literal (man page text as cited in the PR #97
review; not re-read on this machine). ``Description=`` writes ``%`` as ``%%``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from ..errors import Hold
from ..modules.builtin import BUILTIN_MODULES
from ..modules.spec import ModuleSpec
from .interface import HostContext, RenderedFile, ServiceUnit, check_action, check_units

MODULE_ID = "service_manager.systemd"
Scope = Literal["system", "user"]
SYSTEM_DIR = "/etc/systemd/system"


def spec() -> ModuleSpec:
    (found,) = (s for s in BUILTIN_MODULES if s.module_id == MODULE_ID)
    return found


def quote(value: str, *, expand: bool = True) -> str:
    """One double-quoted word for systemd. ``expand``: the setting expands ``$VAR``
    (``ExecStart=``), so ``$`` is written ``$$``; ``Environment=`` does not expand."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if expand:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def _text(value: str) -> str:
    """A free-text setting (``Description=``): ``%`` is a specifier, so it is written ``%%``."""
    return value.replace("%", "%%")


def _minutes(unit: ServiceUnit) -> int:
    seconds = unit.interval_seconds or 0
    minutes = seconds // 60
    if seconds % 60 or not minutes or 60 % minutes:
        raise Hold("SERVICE_UNIT_FORM", "A systemd timer interval is whole minutes in an hour",
                   details={"unit": unit.name, "interval_seconds": seconds})  # fmt: skip
    return minutes


class SystemdManager:
    def __init__(self, scope: Scope = "system") -> None:
        if scope not in ("system", "user"):
            raise Hold("SERVICE_SCOPE", "systemd scope is 'system' or 'user'")
        self.scope: Scope = scope
        self._spec = spec()

    @property
    def spec(self) -> ModuleSpec:
        return self._spec

    def unit_id(self, unit: ServiceUnit) -> str:
        return unit.name

    def _dir(self, host: HostContext) -> Path:
        if self.scope == "system":
            return Path(SYSTEM_DIR)
        return Path(host.home) / ".config" / "systemd" / "user"

    def _check(self, units: Sequence[ServiceUnit], host: HostContext) -> None:
        check_units(units, host)
        if self.scope == "system" and not host.account:
            raise Hold("SERVICE_ACCOUNT", "A system unit runs as the AMPLAI account; name it")
        for unit in units:
            if unit.form == "periodic":
                _minutes(unit)

    def _service(self, unit: ServiceUnit, host: HostContext) -> str:
        lines = ["[Unit]", f"Description={_text(unit.description)}", "", "[Service]"]
        if unit.form == "periodic":
            lines.append("Type=oneshot")
        if self.scope == "system":
            lines.append(f"User={host.account}")
        lines += [
            f"Environment={quote('PATH=' + host.path_env, expand=False)}",
            "ExecStart=" + " ".join(quote(a) for a in unit.argv),
        ]
        if unit.form == "daemon":
            lines.append("Restart=on-failure")
        lines += ["NoNewPrivileges=yes", "PrivateTmp=yes", "UMask=0077"]
        if unit.form == "daemon":
            target = "multi-user.target" if self.scope == "system" else "default.target"
            lines += ["", "[Install]", f"WantedBy={target}"]
        return "\n".join(lines) + "\n"

    def _timer(self, unit: ServiceUnit) -> str:
        return "\n".join([
            "[Unit]", f"Description={_text(unit.description)} (every {_minutes(unit)} min)", "",
            "[Timer]", f"OnCalendar=*:0/{_minutes(unit)}", f"Unit={unit.name}.service", "",
            "[Install]", "WantedBy=timers.target",
        ]) + "\n"  # fmt: skip

    def render(self, units: Sequence[ServiceUnit], host: HostContext) -> tuple[RenderedFile, ...]:
        self._check(units, host)
        out: list[RenderedFile] = []
        for unit in units:
            for suffix, content in ((".service", self._service(unit, host)),) + (
                ((".timer", self._timer(unit)),) if unit.form == "periodic" else ()
            ):
                name = unit.name + suffix
                out.append(RenderedFile(unit.name, name, str(self._dir(host) / name), content))
        return tuple(out)

    def commands(
        self, action: str, units: Sequence[ServiceUnit], host: HostContext
    ) -> tuple[tuple[str, ...], ...]:
        check_action(action)
        self._check(units, host)
        ctl = ("systemctl",) if self.scope == "system" else ("systemctl", "--user")
        timers = [u.name + ".timer" for u in units if u.form == "periodic"]
        daemons = [u.name + ".service" for u in units if u.form == "daemon"]
        ticks = [u.name + ".service" for u in units if u.form == "periodic"]
        out: list[tuple[str, ...]] = []
        if action == "install":
            if self.scope == "user":
                if not host.account:
                    raise Hold("SERVICE_ACCOUNT", "linger needs the AMPLAI account; name it")
                out.append(("loginctl", "enable-linger", host.account))
            out.append((*ctl, "daemon-reload"))
            if daemons or timers:
                out.append((*ctl, "enable", "--now", *daemons, *timers))
        elif action == "start" and (daemons or timers):
            out.append((*ctl, "start", *daemons, *timers))
        elif action == "stop" and (daemons or timers):
            out.append((*ctl, "stop", *timers, *ticks, *daemons))
        elif action == "timer_on" and timers:
            out.append((*ctl, "start", *timers))
        elif action == "timer_off" and timers:
            out.append((*ctl, "stop", *timers, *ticks))
        elif action == "status" and (daemons or timers):
            out.append((*ctl, "status", "--no-pager", *daemons, *timers))
        return tuple(out)


def subject(workdir: Path) -> SystemdManager:
    """The module for its conformance suite (system scope; the tests also run the user scope)."""
    del workdir
    return SystemdManager("system")
