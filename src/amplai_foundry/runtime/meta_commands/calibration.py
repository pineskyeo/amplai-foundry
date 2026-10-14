"""``amplai meta calibrate`` and ``amplai meta calibration show`` (Work 033 S11, interfaces.md §8.2,
§12.1).

``calibrate`` freezes a calibration plan of the named cells over every development and validation
case of the frozen corpus v2, issues the operator approval for its exact digest and runs it here
(``CalibrationService``); ``--max-trials`` bounds its worst case (cells x cases x max repeats).
``--set regression`` calibrates the frozen regression set (``amplai-regression-v1``, §10.6) on the
installed app its tasks name: the nightly drift baseline is the operator's newest such calibration.

D-128 (operator decision 2026-10-14): ``calibrate --import-from <plan-id>`` (repeatable) imports
the eligible completed round-1 trials of those plans into the new plan, approved with it; the
plan id and "imported N, new M" are printed to stderr before anything runs.
``calibration resume <plan-id>`` runs a stopped plan again under its own approval.
``settle-reservations`` settles the root-goal reservations of ended runs from their driver
journals' usage.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from . import (
    DEFAULT_CONFIG,
    AppOption,
    BasisOption,
    ConfigOption,
    CorpusOption,
    EvidenceOption,
    Guarded,
    ParallelOption,
    PerTrialOption,
    capped_parallel,
    opened_v2,
    qualify,
)


def register(meta: typer.Typer, guarded: Guarded) -> None:
    @meta.command("calibrate")
    def calibrate(
        cells: Annotated[str, typer.Option("--cells", help="comma-separated cell ids")],
        max_repeats: Annotated[int, typer.Option("--max-repeats", help="runs per (cell, task)")],
        max_trials: Annotated[int, typer.Option("--max-trials", help="worst-case trial bound")],
        max_tokens: Annotated[int, typer.Option("--max-tokens", help="calibration token budget")],
        max_wall_seconds: Annotated[int, typer.Option("--max-wall-seconds")],
        per_trial_tokens: PerTrialOption,
        basis: BasisOption,
        evidence: EvidenceOption,
        parallel: ParallelOption = 1,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
        corpus_set: Annotated[
            str,
            typer.Option("--set", help="main (default) or regression (the nightly drift baseline)"),
        ] = "main",
        import_from: Annotated[
            list[str] | None,
            typer.Option(
                "--import-from",
                help="a calibration plan id whose eligible round-1 trials this plan imports "
                "(repeatable, D-128)",
            ),
        ] = None,
        expect_imported: Annotated[
            int | None,
            typer.Option(
                "--expect-imported",
                help="the imported trial count a --dry-run showed; a different count is refused",
            ),
        ] = None,
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run",
                help="print the plan preview (imported N, new M, per cell, exclusions) and exit",
            ),
        ] = False,
    ) -> None:
        """Freeze, approve and run a calibration of the cells."""

        def announce(value: dict[str, Any]) -> None:
            typer.echo(json.dumps({"calibration_plan": value}, sort_keys=True), err=True)

        def call() -> Any:
            names = [c.strip() for c in cells.split(",") if c.strip()]
            with opened_v2(config, corpus, cell=names[0] if names else "codex-cli", app=app) as ops:
                workers = capped_parallel(ops.dep, parallel)
                qualify(ops, per_trial_tokens, basis, evidence)
                return ops.calibrate(
                    names, max_repeats=max_repeats, max_trials=max_trials, parallel=workers,
                    max_tokens=max_tokens, max_wall_seconds=max_wall_seconds,
                    corpus_set=corpus_set, import_from=list(import_from or []),
                    announce=announce, expect_imported=expect_imported, dry_run=dry_run,
                )  # fmt: skip

        guarded(call)

    @meta.command("settle-reservations")
    def settle_reservations(config: ConfigOption = DEFAULT_CONFIG) -> None:
        """Settle the reservations of ended runs from their driver journals' usage (D-128)."""

        def call() -> Any:
            with opened_v2(config, None) as ops:
                return ops.settle_ended_reservations()

        guarded(call)

    group = typer.Typer(help="Calibration runs: show one, or recover one whose process is gone")
    meta.add_typer(group, name="calibration")

    @group.command("show")
    def show(
        plan_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """The run state and the summary of a calibration plan."""

        def call() -> Any:
            with opened_v2(config, corpus, app=app) as ops:
                return ops.calibration_show(plan_id)

        guarded(call)

    @group.command("recover")
    def recover(
        plan_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Close a calibration run a previous process left running (it ends ``owner_lost``)."""

        def call() -> Any:
            with opened_v2(config, corpus, app=app) as ops:
                return ops.calibration_recover(plan_id)

        guarded(call)

    @group.command("resume")
    def resume(
        plan_id: str,
        per_trial_tokens: PerTrialOption,
        basis: BasisOption,
        evidence: EvidenceOption,
        parallel: ParallelOption = 1,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Run a stopped calibration plan again under its approval (D-128): completed graded
        trials stay, unfinished slots run again; anything behaviour-affecting that changed
        holds CALIBRATION_RESUME_CHANGED."""

        def call() -> Any:
            with opened_v2(config, corpus, app=app) as ops:
                workers = capped_parallel(ops.dep, parallel)
                qualify(ops, per_trial_tokens, basis, evidence)
                return ops.calibration_resume(plan_id, parallel=workers)

        guarded(call)
