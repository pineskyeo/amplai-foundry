"""``amplai meta dashboard --out DIR [--feed-json]`` (Work 033 S15, interfaces.md §8.10, §11.2,
§12.1).

Builds the static dashboard pages from the stored records of the deployment at ``--config``.
The build is read-only: it opens the deployment's store with ``Store(readonly=True)`` instead of
booting the deployment (a boot installs cells and moves the release pointer), so it takes no owner
lock and can run while the server or the nightly runner holds the store, and it writes nothing
but the page files under ``--out``. ``--feed-json`` also writes ``feed.json``, the feed the pages
are rendered from (never stored; no trace text, hidden tests or leak-index tokens).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from ..errors import Hold, RuntimeFault
from . import DEFAULT_CONFIG, ConfigOption, Guarded

OutOption = Annotated[Path, typer.Option("--out", help="the directory the pages are written to")]
FeedJsonOption = Annotated[
    bool, typer.Option("--feed-json", help="also write feed.json next to the pages")
]


def build(config: Path, out: Path, *, feed_json: bool = False) -> dict[str, Any]:
    """Build the pages of the deployment at ``config`` into ``out``; the written file names."""
    from ...meta_harness import dashboard
    from ..local_deployment import LocalConfig
    from ..storage.store import Scope, Store

    path = Path(config).expanduser().absolute()
    try:
        cfg = LocalConfig.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RuntimeFault(
            "LOCAL_INPUT", f"--config is not a readable local config: {exc}"
        ) from exc
    if cfg.schema_version != "local-1":
        raise Hold("CONFIG_VERSION", "Unknown local product configuration")
    root = Path(cfg.runtime_root).expanduser()
    root = (root if root.is_absolute() else path.parent / root).absolute()
    if not (root / "runtime.sqlite3").is_file():
        raise RuntimeFault("NOT_FOUND", "No runtime store at the configured runtime root")
    scope = Scope.parse(cfg.scope)
    with Store(root, readonly=True) as store:
        nightly = cfg.meta.nightly if cfg.meta is not None else None
        feed = dashboard.build_feed(
            store, scope, nightly=nightly.model_dump() if nightly is not None else None
        )
    written = dashboard.write_site(feed, Path(out).expanduser(), feed_json=feed_json)
    return {
        "out": str(Path(out).expanduser().absolute()),
        "pages": sorted(p.name for p in written),
        "cells": [c.cell_id for c in feed.cells],
        "experiments": [e.experiment_id for e in feed.experiments],
        "records_read": feed.sources,
    }


def register(meta: typer.Typer, guarded: Guarded) -> None:
    @meta.command("dashboard")
    def dashboard_command(
        out: OutOption,
        feed_json: FeedJsonOption = False,
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Write the static dashboard pages (matrix, cells, lineage, approvals, corpus, experiments,
        layers, strategies, judges, budget, evaluation) from the stored records, read-only."""

        def call() -> Any:
            return build(config, out, feed_json=feed_json)

        guarded(call)
