"""``amplai meta trace list | show`` (Work 033 S13, interfaces.md §9.3, §12.1): operator only.

Both run as the human operator (``local_deployment.meta_operator``), who reads traces of every
split (``corpus.read``; holdout also ``corpus.holdout.evaluate``). ``list`` prints record fields
and counts, never trace text; ``show`` prints one trace to the operator's terminal. Traces are
never exported (§9.3). No corpus is needed.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ..errors import RuntimeFault
from . import DEFAULT_CONFIG, CellOption, ConfigOption, Guarded, opened_deployment

LIST_FIELDS = (
    "run_id",
    "goal_id",
    "experiment_id",
    "task_id",
    "split",
    "arm",
    "cell_id",
    "driver_id",
    "events",
    "dropped_event_types",
    "truncated_outputs",
    "sanitizer_version",
    "captured_at",
)


def register(meta: typer.Typer, guarded: Guarded) -> None:
    from ...meta_harness.traces import TRACE_KIND, TraceService

    group = typer.Typer(help="Trial traces (operator only): list, show")
    meta.add_typer(group, name="trace")

    @group.command("list")
    def list_traces(
        cell: CellOption,
        split: Annotated[str, typer.Option("--split", help="development|validation|holdout")] = (
            "development"
        ),
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """The cell's traces of one split (fields and counts, no text)."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                service = TraceService(dep.store, dep.scope, dep.artifacts)
                refs = service.list(dep.meta_operator(), cell_id=cell, split=split)
                rows = [
                    {
                        "trace_id": ref["id"],
                        **{
                            k: dep.store.get(dep.scope, TRACE_KIND, ref).get(k) for k in LIST_FIELDS
                        },
                    }
                    for ref in refs
                ]
                return {"traces": rows, "counts": service.counts()}

        guarded(call)

    @group.command("show")
    def show(trace_id: str, config: ConfigOption = DEFAULT_CONFIG) -> None:
        """One trace: its record and its sanitized body."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                service = TraceService(dep.store, dep.scope, dep.artifacts)
                found = [ref for ref, _ in service.records() if ref["id"] == trace_id]
                if not found:
                    raise RuntimeFault("NOT_FOUND", "No trace with this id", details=trace_id)
                return service.read(dep.meta_operator(), found[0])

        guarded(call)
