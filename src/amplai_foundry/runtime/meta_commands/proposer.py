"""``amplai meta proposer run | dream | sweep | score`` (Work 033 S13, interfaces.md §9.4-§9.8,
§12.1).

``run`` drafts component edits with the cheap proposer cell (``roles.proposer[0]`` of
``local.json``), refines the top ones with the strong cell (``roles.proposer[1]``) and submits
them as draft proposals as the proposer identity; it reads development data only. ``dream``
consolidates the night's development traces into a ``memory_notes`` candidate (one read-only turn
of ``--turn-cell``, default ``roles.proposer[0]``). ``sweep`` proposes leave-one-out variants of
the champion's non-v1 components (e.g. after a model snapshot change) and shows the §9.8 removal
verdict of the open ones (``proposer.removal_verdict``; no gate consumes it yet). ``score``
writes the ``prediction-score`` records due for a proposal after its evaluated stages (§9.6; the
focused and holdout scores are operator-only and this command runs as the operator); it is not
listed in §12.1 and exists because no stage hook writes them yet (reported). None of them screens,
approves or promotes anything.

The read-only turns are the cells' planner turns (``LocalProductDeployment``: the
``StrategyRunner``'s ``turns`` factory) on an empty scratch directory (IC-14). The leak gate is
opened by the host-side leak reader on the frozen corpus v2's leak index.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from ..errors import Hold
from . import (
    DEFAULT_CONFIG,
    AppOption,
    CellOption,
    ConfigOption,
    CorpusOption,
    Guarded,
    opened_deployment,
    opened_v2,
)


def proposer_cells(ops: Any) -> tuple[str, str]:
    """(cheap, strong) = ``roles.proposer[0]``, ``roles.proposer[1]`` (§9.4, §12.2)."""
    roles = getattr(ops.dep.config, "roles", None)
    cells = list(getattr(roles, "proposer", None) or [])
    if len(cells) < 2:
        raise Hold(
            "CELL_UNKNOWN",
            "local.json roles.proposer names the cheap and the strong proposer cell",
            details={"roles.proposer": cells},
        )
    return cells[0], cells[1]


def turn_of(ops: Any, cell_id: str) -> Any:
    """The cell's read-only turn (Hold TURN_FAILED for a cell without one)."""
    strategies = getattr(ops.dep.service, "strategies", None)
    turns = getattr(strategies, "turns", None)
    if turns is None:
        raise Hold("TURN_FAILED", "No read-only turns are configured", details={"cell": cell_id})
    return turns(cell_id)


def ensemble(ops: Any) -> Any:
    """The proposer ensemble of the deployment (§9.4)."""
    from ...meta_harness.archive import EliteArchive
    from ...meta_harness.leak_gate import LeakGate
    from ...meta_harness.proposer import ProposerEnsemble
    from ...meta_harness.traces import TraceService

    cheap, strong = proposer_cells(ops)
    return ProposerEnsemble(
        ops,
        breadth=turn_of(ops, cheap),
        depth=turn_of(ops, strong),
        traces=TraceService(ops.store, ops.scope, ops.dep.artifacts),
        archive=EliteArchive(ops.store, ops.scope),
        leak_gate=LeakGate(ops.local.leak_reader, ops.store, ops.frozen_corpus()["leak_index_ref"]),
        components=ops.components,
    )


def register(meta: typer.Typer, guarded: Guarded) -> None:
    group = typer.Typer(help="Proposer: draft component candidates, dream notes, sweep removals")
    meta.add_typer(group, name="proposer")

    @group.command("run")
    def run(
        cell: CellOption,
        drafts: Annotated[int, typer.Option("--drafts", help="edits the cheap cell drafts")] = 6,
        refine: Annotated[int, typer.Option("--refine", help="drafts the strong cell refines")] = 2,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Draft, deduplicate, leak-gate, refine and submit component candidates of the cell."""

        def call() -> Any:
            with opened_v2(config, corpus, cell=cell, app=app) as ops:
                proposer = ensemble(ops)
                proposer.run(cell_id=cell, drafts=drafts, refine=refine)
                return proposer.last

        guarded(call)

    @group.command("dream")
    def dream(
        cell: CellOption,
        night: Annotated[str, typer.Option("--night", help="the night, YYYY-MM-DD")],
        turn_cell: Annotated[
            str | None,
            typer.Option("--turn-cell", help="the cell of the turn (default roles.proposer[0])"),
        ] = None,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Consolidate the night's development traces into a memory_notes candidate."""
        from ...meta_harness import proposer

        def call() -> Any:
            with opened_v2(config, corpus, cell=cell, app=app) as ops:
                cell_of_turn = turn_cell or proposer_cells(ops)[0]
                proposal_id = proposer.dream(
                    ops, cell_id=cell, night=night, turn=turn_of(ops, cell_of_turn)
                )
                return {"proposal_id": proposal_id, "night": night, "cell_id": cell}

        guarded(call)

    @group.command("sweep")
    def sweep(
        cell: CellOption,
        reason: Annotated[str, typer.Option("--reason", help="why the sweep runs")],
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Propose leave-one-out variants of the champion's non-v1 components (§9.8)."""
        from ...meta_harness import proposer

        def call() -> Any:
            with opened_v2(config, corpus, cell=cell, app=app) as ops:
                targets = proposer.removal_targets(ops, cell)
                proposals = proposer.removal_sweep(ops, cell_id=cell, reason=reason)
                return {
                    "cell_id": cell,
                    "targets": [{"slot": t["slot"], "kind": t["kind"]} for t in targets],
                    "proposals": proposals,
                    # §9.8 criterion of every open sweep proposal (the IC-24 removal gate)
                    "open": proposer.sweep_proposals(ops, cell),
                }

        guarded(call)

    @group.command("score")
    def score(
        proposal_id: str,
        stage: Annotated[
            str | None,
            typer.Option(
                "--stage", help="screening|focused|holdout (default: every evaluated one)"
            ),
        ] = None,
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Score the proposal's prediction after its evaluated stages (operator)."""
        from ...meta_harness import proposer

        def call() -> Any:
            with opened_deployment(config) as dep:
                if stage is None:
                    refs = proposer.score_evaluated(dep.store, dep.scope, proposal_id)
                    if not refs:
                        raise Hold(
                            "META_STATE",
                            "The proposal has no prediction or no evaluated scored stage",
                            details=proposal_id,
                        )
                else:
                    refs = {
                        stage: proposer.score_predictions(dep.store, dep.scope, proposal_id, stage)
                    }
                return {
                    "proposal_id": proposal_id,
                    "scores": {
                        name: {
                            "score_ref": ref,
                            **{
                                k: dep.store.get(dep.scope, proposer.SCORE_KIND, ref).get(k)
                                for k in ("task_level", "bucket_level", "scored_at")
                            },
                        }
                        for name, ref in refs.items()
                    },
                }

        guarded(call)
