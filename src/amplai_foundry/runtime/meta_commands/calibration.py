"""``amplai meta calibrate`` and ``amplai meta calibration show`` (Work 033 S11, interfaces.md §8.2,
§12.1).

``calibrate`` freezes a calibration plan of the named cells over every development and validation
case of the frozen corpus v2, issues the operator approval for its exact digest and runs it here
(``CalibrationService``); ``--max-trials`` bounds its worst case (cells x cases x max repeats).
``--set regression`` calibrates the frozen regression set (``amplai-regression-v1``, §10.6) on the
installed app its tasks name: the nightly drift baseline is the operator's newest such calibration.
"""

from __future__ import annotations

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
    ) -> None:
        """Freeze, approve and run a calibration of the cells."""

        def call() -> Any:
            names = [c.strip() for c in cells.split(",") if c.strip()]
            with opened_v2(config, corpus, cell=names[0] if names else "codex-cli", app=app) as ops:
                workers = capped_parallel(ops.dep, parallel)
                qualify(ops, per_trial_tokens, basis, evidence)
                return ops.calibrate(
                    names, max_repeats=max_repeats, max_trials=max_trials, parallel=workers,
                    max_tokens=max_tokens, max_wall_seconds=max_wall_seconds,
                    corpus_set=corpus_set,
                )  # fmt: skip

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
