"""``amplai ops service print`` and ``amplai ops upgrade plan`` (Work 034 S3; C-8, U-2, D-110,
D-118).

Both only read and print. ``service print`` renders the units of the selected service manager
and the commands of each action; it installs and runs nothing (C-8). ``upgrade plan`` prints the
read-only upgrade plan (``runtime/upgrade/plan.py``).

The service manager is ``--manager`` over the config's ``service.manager`` over the OS default
(``local_deployment.service_manager``, the composition root that may import an implementation).
``--config`` and ``--meta-config`` are the paths the units run with; they are read here only when
they exist on this machine, so units for another host can be rendered with that host's paths.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
from pathlib import Path
from typing import Annotated, Any

import typer

from .errors import Hold, RuntimeFault

DEFAULT_CONFIG = Path("~/.amplai/local/local.json")
DEFAULT_META_CONFIG = Path("~/.amplai/meta/local.json")  # meta_commands/nightly.py §8.9
MANAGERS = {"launchd": "service_manager.launchd", "systemd": "service_manager.systemd"}

ConfigOption = Annotated[Path, typer.Option("--config", help="the product local.json")]
MetaOption = Annotated[
    Path, typer.Option("--meta-config", help="the separate meta deployment's local.json")
]
NoMetaOption = Annotated[bool, typer.Option("--no-meta", help="no nightly tick unit")]
ManagerOption = Annotated[
    str | None, typer.Option("--manager", help="launchd | systemd (default: config, then OS)")
]
ScopeOption = Annotated[str | None, typer.Option("--scope", help="systemd: system | user")]
AmplaiOption = Annotated[
    str | None, typer.Option("--amplai", help="the amplai executable (default: PATH)")
]
HomeOption = Annotated[
    Path | None, typer.Option("--home", help="the AMPLAI account's home (default: yours)")
]
PathOption = Annotated[
    str | None, typer.Option("--path-env", help="PATH the units run with (default: yours)")
]
AccountOption = Annotated[
    str | None, typer.Option("--account", help="systemd: the AMPLAI account (User=, linger)")
]


def _load(path: Path) -> Any | None:
    from .local_deployment import LocalConfig

    path = path.expanduser()
    return LocalConfig.model_validate_json(path.read_bytes()) if path.is_file() else None


def _manager(config: Path, manager: str | None, scope: str | None) -> Any:
    from .local_deployment import ServiceEntry, service_manager

    if manager is not None and manager not in MANAGERS:
        raise Hold("SERVICE_MANAGER_UNKNOWN", "Use --manager launchd or systemd",
                   details={"manager": manager})  # fmt: skip
    loaded = _load(config)
    entry = loaded.service if loaded is not None else ServiceEntry()
    return service_manager(entry, MANAGERS[manager] if manager else None, scope)


def _units(config: Path, meta_config: Path | None, amplai: str | None) -> Any:
    from .service_manager import amplai_units

    binary = amplai or shutil.which("amplai")
    if not binary:
        raise Hold("AMPLAI_PATH", "No amplai on PATH; pass --amplai")
    server = config.expanduser().absolute()
    meta = meta_config.expanduser().absolute() if meta_config is not None else None
    if meta == server:  # the tick refuses it at run time too (meta nightly, §8.8)
        raise Hold("NIGHT_DEPLOYMENT", "The tick runs on a separate meta deployment's config",
                   details={"config": str(meta)})  # fmt: skip
    return amplai_units(
        str(Path(binary).expanduser().absolute()), str(server), str(meta) if meta else None,
        server_logs=str(server.parent / "logs"),
        meta_logs=str(meta.parent / "logs") if meta else None,
    )  # fmt: skip


def _host(
    account: str | None, home: Path | None = None, path_env: str | None = None,
) -> Any:  # fmt: skip
    """The host the units run on: uid and home of the account they run as, never of whoever runs
    this command (under ``sudo`` that is root, and ``gui/0`` or ``/root`` would be wrong).

    - ``--account`` known on this machine: its uid and home (``pwd``); a ``--home`` that is not
      that home is ``Hold SERVICE_HOST``.
    - ``--account`` not known here (rendering for another host): no uid (launchd commands then
      hold ``SERVICE_HOST``) and ``--home`` is required (``Hold SERVICE_HOST``).
    - no ``--account``: the invoking user; when that is root (uid 0) ``Hold SERVICE_ACCOUNT``.

    ``--home`` is taken as given (``~`` expanded); the interface refuses a relative one."""
    from .service_manager import HostContext

    given = str(home.expanduser()) if home is not None else None
    uid: int | None
    if account is not None:
        try:
            entry = pwd.getpwnam(account)
        except KeyError:
            if given is None:
                raise Hold("SERVICE_HOST", "The account is not on this machine; pass its --home",
                           details={"account": account}) from None  # fmt: skip
            uid = None
        else:
            uid = int(entry.pw_uid)
            if given is not None and Path(given) != Path(entry.pw_dir):
                raise Hold("SERVICE_HOST", "--home is not the account's home",
                           details={"account": account, "home": given,
                                    "account_home": entry.pw_dir})  # fmt: skip
            given = given or str(entry.pw_dir)
    else:
        uid = os.getuid()
        if uid == 0:
            raise Hold("SERVICE_ACCOUNT", "Running as root: name the AMPLAI account with --account")
    return HostContext(
        home=given if given is not None else str(Path.home()),
        path_env=path_env if path_env is not None else os.environ.get("PATH", ""),
        uid=uid, account=account,
    )  # fmt: skip


def _raise(fault: RuntimeFault) -> Any:
    raise fault


def _lines(commands: tuple[tuple[str, ...], ...]) -> list[str]:
    return [shlex.join(argv) for argv in commands]


def render_service(
    config: Path, meta_config: Path | None, *, manager: str | None, scope: str | None,
    amplai: str | None, account: str | None, home: Path | None = None,
    path_env: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    from .service_manager import ACTIONS

    chosen = _manager(config, manager, scope)
    units, host = _units(config, meta_config, amplai), _host(account, home, path_env)
    files = chosen.render(units, host)
    return {
        "manager": chosen.spec.ref,
        "scope": getattr(chosen, "scope", None),
        "installs_nothing": True,
        "files": [{"unit": f.unit, "filename": f.filename, "destination": f.destination,
                   "content": f.content} for f in files],
        "commands": {action: _lines(chosen.commands(action, units, host)) for action in ACTIONS},
    }  # fmt: skip


def freeze_commands(
    config: Path, meta_config: Path | None, *, manager: str | None, scope: str | None,
    amplai: str | None, account: str | None, home: Path | None = None,
) -> dict[str, Any]:  # fmt: skip
    """The service manager part of the freeze and unfreeze (U-2; plan §3.5a.1 order: clock off,
    then the daemons stopped; unfreeze in reverse; each unit named once), printed only. A hold is
    reported, not raised."""
    from .service_manager import freeze_sequence, unfreeze_sequence

    try:
        chosen = _manager(config, manager, scope)
        units, host = _units(config, meta_config, amplai), _host(account, home)
        return {
            "manager": chosen.spec.ref,
            "freeze": _lines(freeze_sequence(chosen, units, host)),
            "unfreeze": _lines(unfreeze_sequence(chosen, units, host)),
            "runs_nothing": True,
        }
    except RuntimeFault as fault:
        return {"hold": fault.code, "message": fault.message}


def register(ops: typer.Typer, guarded: Any) -> None:
    service = typer.Typer(help="Service manager units (C-8): print only, never installed")
    upgrade = typer.Typer(help="Upgrade with accumulated data kept (S3, D-110)")
    ops.add_typer(service, name="service")
    ops.add_typer(upgrade, name="upgrade")

    @service.command("print")
    def service_print(
        config: ConfigOption = DEFAULT_CONFIG,
        meta_config: MetaOption = DEFAULT_META_CONFIG,
        no_meta: NoMetaOption = False,
        manager: ManagerOption = None,
        scope: ScopeOption = None,
        amplai: AmplaiOption = None,
        account: AccountOption = None,
        home: HomeOption = None,
        path_env: PathOption = None,
        raw: Annotated[
            str | None, typer.Option("--raw", help="print only this rendered file's content")
        ] = None,
    ) -> None:
        """Render the server and tick units and print every action's commands; install nothing."""
        meta = None if no_meta else meta_config

        def show() -> dict[str, Any]:
            return render_service(config, meta, manager=manager, scope=scope, amplai=amplai,
                                  account=account, home=home, path_env=path_env)  # fmt: skip

        if raw is None:
            guarded(show)
            return

        def pick() -> str:
            files = {f["filename"]: f["content"] for f in show()["files"]}
            if raw not in files:
                raise Hold("SERVICE_FILE", "No such rendered file",
                           details={"files": sorted(files)})  # fmt: skip
            return str(files[raw])

        try:
            text = pick()
        except RuntimeFault as fault:
            held = fault
            guarded(lambda: _raise(held))  # prints the hold and exits non-zero
            return
        typer.echo(text, nl=False)

    @upgrade.command("plan")
    def upgrade_plan_cmd(
        config: ConfigOption = DEFAULT_CONFIG,
        meta_config: MetaOption = DEFAULT_META_CONFIG,
        manager: ManagerOption = None,
        scope: ScopeOption = None,
        amplai: AmplaiOption = None,
        account: AccountOption = None,
        home: HomeOption = None,
    ) -> None:
        """Print the ordered upgrade and rollback with today's state; change nothing."""
        from .upgrade.plan import upgrade_plan

        def show() -> dict[str, Any]:
            cfg = _load(config)
            if cfg is None:
                raise Hold("CONFIG_MISSING", "No product local.json at --config",
                           details={"config": str(config)})  # fmt: skip
            meta = _load(meta_config)
            meta_path = meta_config if meta is not None else None
            return upgrade_plan(cfg, meta, service=lambda: freeze_commands(
                config, meta_path, manager=manager, scope=scope, amplai=amplai, account=account,
                home=home,
            ))  # fmt: skip

        guarded(show)
