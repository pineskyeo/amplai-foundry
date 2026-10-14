"""``amplai checks``: run the registered checks of a moment (Work 034 OD-18; spec M-16..M-20).

    amplai checks run --when ci [--target kind:git_host] [--skip check.tool_pytest]
                      [--base-ref origin/main] [--record-dir DIR]
    amplai checks list [--when ci]
    amplai checks lock

Run from the repository root (``--root`` otherwise). ``run`` exits 0 when every required check
passed, 3 on Hold ``CHECK_FAILED`` (the failed checks' ids and results are printed), and writes
its record under ``--record-dir`` (default ``<root>/.check-runs``) either way. Without
``--base-ref`` a loosening against main is not checked, and a warning on stderr says so (no
implicit ``origin/main``: a local one may be stale or absent). ``lock`` writes
``checks.lock.json`` from the registry.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer

T = TypeVar("T")

RootOption = Annotated[Path, typer.Option("--root", help="the repository checkout")]


def register(app: typer.Typer, guarded: Callable[[Callable[[], T]], T]) -> None:
    checks = typer.Typer(
        help="Reliability checks (lint, tests, scans) as modules", no_args_is_help=True
    )
    app.add_typer(checks, name="checks")

    @checks.command("run")
    def run(
        when: Annotated[str, typer.Option("--when", help="ci, enable, pack_install or upgrade")],
        target: Annotated[
            list[str] | None,
            typer.Option("--target", help="core, kind:<kind>, kind:* or pack:<id>; repeatable"),
        ] = None,
        skip: Annotated[
            list[str] | None,
            typer.Option("--skip", help="a check another step runs; repeatable, never protected"),
        ] = None,
        base_ref: Annotated[
            str | None,
            typer.Option("--base-ref", help="the git ref of the main-branch check lock (M-19)"),
        ] = None,
        record_dir: Annotated[
            Path | None, typer.Option("--record-dir", help="where the run record goes (M-20)")
        ] = None,
        root: RootOption = Path("."),
    ) -> None:
        def call() -> Any:
            from .checks.runner import run_checks

            base = root.resolve()
            summary = run_checks(
                when,
                root=base,
                record_dir=(record_dir or base / ".check-runs").absolute(),
                targets=tuple(target or ()),
                skip=tuple(skip or ()),
                base_ref=base_ref,
            )
            for warning in summary["warnings"]:
                typer.echo(f"warning: {warning}", err=True)
            return summary

        guarded(call)

    @checks.command("list")
    def list_checks(
        when: Annotated[str | None, typer.Option("--when", help="only this moment")] = None,
    ) -> None:
        def call() -> Any:
            from .checks.builtin import BUILTIN_CHECKS

            return [
                {"check_id": c.check_id, **c.entry(), "description": c.description}
                for c in BUILTIN_CHECKS
                if when is None or when in c.when
            ]

        guarded(call)

    @checks.command("lock")
    def lock(root: RootOption = Path(".")) -> None:
        def call() -> Any:
            from .checks.builtin import BUILTIN_CHECKS, CI_EXTERNAL
            from .checks.lock import LOCK_FILE, entry_digest, lock_bytes, lock_document

            document = lock_document(BUILTIN_CHECKS, CI_EXTERNAL)
            path = root.resolve() / LOCK_FILE
            path.write_bytes(lock_bytes(document))
            return {"lock": str(path), "registry_digest": entry_digest(document),
                    "checks": sorted(document["checks"])}  # fmt: skip

        guarded(call)
