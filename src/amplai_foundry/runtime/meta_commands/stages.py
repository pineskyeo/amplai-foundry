"""``amplai meta review | search | approve-stage | stages | reconcile`` (Work 033 S11,
interfaces.md §8.1, §12.1, IC-18).

``search`` plans a proposal's stages once (``StageRunner.plan``: the cell, the root budget of
IC-16) and runs the stages without an operator gate; it stops at every gate: the class-B review,
``approve-stage focused`` and ``approve-stage holdout``. ``approve-stage`` freezes, approves (the
operator, the exact digest) and runs that stage in this process. None of them chains a gate.
``approve-stage P --stage focused --queue`` (IC-10) approves the exact focused experiment a night
queued (``--digest`` may name it) and freezes it; nothing runs: the next night's confirmation phase
runs it. Without ``--per-trial-tokens``/``--basis``/``--evidence`` a run uses the qualification of
the operator's newest IC-29 record for the cell (``LocalMetaOps.restore_qualification``).
``--app`` names the installed app when the corpus main set names several.

``reconcile`` (IC-18, provisional) is the human operator's path for a stage experiment left
``running`` by an ended process (``--stage``: marked interrupted, never re-run, the stage aborted)
or an unresolved allocation of a proposal root (``--allocation`` with ``--tokens``, ``--cost`` and
the stopped-process usage ``--receipt``). It needs no corpus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from ..errors import RuntimeFault
from ..execution.meta_ops import STAGE_MAX_ATTEMPTS
from . import (
    DEFAULT_CONFIG,
    AppOption,
    BasisOption,
    CellOption,
    ConfigOption,
    CorpusOption,
    EvidenceOption,
    Guarded,
    ParallelOption,
    PerTrialOption,
    capped_parallel,
    opened_deployment,
    opened_v2,
    qualify,
)


def root_budget(*, max_tokens: int, max_wall_seconds: int, parallel: int) -> dict[str, Any]:
    """The proposal's root budget (§8.1, IC-16): 10 stage experiments, wall time from the first
    stage freeze (operator waits included, at most 604800 s), ``max_parallel_works`` = the trial
    concurrency; cost is not compared (D-088)."""
    return {
        "max_wall_seconds": max_wall_seconds,
        "max_attempts": STAGE_MAX_ATTEMPTS,
        "max_tokens": max_tokens,
        "max_cost_microunits": 0,
        "currency": "USD",
        "max_parallel_works": parallel,
        "max_delegation_depth": 0,
    }


def register(meta: typer.Typer, guarded: Guarded) -> None:
    @meta.command("review")
    def review(
        proposal_id: str,
        outcome: Annotated[str, typer.Option("--outcome", help="pass or fail")],
        note: Annotated[str, typer.Option("--note", help="what was reviewed")],
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Record the operator's class-B code review; fail rejects the proposal."""

        def call() -> Any:
            if outcome not in ("pass", "fail"):
                raise RuntimeFault("REVIEW_OUTCOME", "--outcome is pass or fail")
            with opened_v2(config, corpus, app=app) as ops:
                return ops.review(proposal_id, outcome="pass" if outcome == "pass" else "fail",
                                  note=note)  # fmt: skip

        guarded(call)

    @meta.command("search")
    def search(
        proposal_id: str,
        cell: CellOption,
        max_tokens: Annotated[int, typer.Option("--max-tokens", help="root token budget")],
        max_wall_seconds: Annotated[
            int, typer.Option("--max-wall-seconds", help="root wall time (<= 604800)")
        ],
        per_trial_tokens: PerTrialOption,
        basis: BasisOption,
        evidence: EvidenceOption,
        parallel: ParallelOption = 1,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Plan the stages and run those without an operator gate; stop at each gate."""

        def call() -> Any:
            with opened_v2(config, corpus, cell=cell, app=app) as ops:
                workers = capped_parallel(ops.dep, parallel)
                qualify(ops, per_trial_tokens, basis, evidence)
                return ops.search(
                    proposal_id,
                    cell_id=cell,
                    root_budget=root_budget(max_tokens=max_tokens,
                                            max_wall_seconds=max_wall_seconds,
                                            parallel=workers),
                    parallel=workers,
                )  # fmt: skip

        guarded(call)

    @meta.command("approve-stage")
    def approve_stage(
        proposal_id: str,
        stage: Annotated[str, typer.Option("--stage", help="focused or holdout")],
        per_trial_tokens: Annotated[
            int | None,
            typer.Option("--per-trial-tokens", help="the qualified per-trial token ceiling"),
        ] = None,
        basis: Annotated[
            str | None, typer.Option("--basis", help="what the executor qualification rests on")
        ] = None,
        evidence: Annotated[list[str] | None, typer.Option("--evidence")] = None,
        queue: Annotated[
            bool,
            typer.Option(
                "--queue",
                help="approve the focused experiment a night queued (exact digest); run nothing",
            ),
        ] = False,
        digest: Annotated[
            str | None, typer.Option("--digest", help="with --queue: the queued subject digest")
        ] = None,
        parallel: ParallelOption = 1,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Operator gate of a stage: freeze, approve and run it in this process (or, with
        --queue, approve the experiment a night queued for the next night)."""

        def call() -> Any:
            given = [per_trial_tokens is not None, basis is not None, bool(evidence)]
            if queue and any(given):
                raise RuntimeFault(
                    "LOCAL_INPUT", "--queue runs nothing: it takes no executor qualification"
                )
            if any(given) and not all(given):
                raise RuntimeFault(
                    "LOCAL_INPUT", "--per-trial-tokens, --basis and --evidence go together"
                )
            with opened_v2(config, corpus, app=app) as ops:
                workers = capped_parallel(ops.dep, parallel)
                if all(given):
                    assert per_trial_tokens is not None and basis is not None
                    qualify(ops, per_trial_tokens, basis, list(evidence or []))
                return ops.approve_stage(proposal_id, stage, parallel=workers, queue=queue,
                                         subject_digest=digest)  # fmt: skip

        guarded(call)

    @meta.command("stages")
    def stages(
        proposal_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
        app: AppOption = None,
    ) -> None:
        """Where the proposal's stages are and which gate waits."""

        def call() -> Any:
            with opened_v2(config, corpus, app=app) as ops:
                return ops.stages(proposal_id)

        guarded(call)

    @meta.command("reconcile")
    def reconcile(
        proposal_id: str,
        stage: Annotated[
            str | None,
            typer.Option("--stage", help="a stage whose experiment an ended process left running"),
        ] = None,
        allocation: Annotated[
            str | None,
            typer.Option("--allocation", help="a reserved or unknown allocation of the root"),
        ] = None,
        tokens: Annotated[
            int | None, typer.Option("--tokens", help="the allocation's final tokens")
        ] = None,
        cost: Annotated[
            int | None, typer.Option("--cost", help="the allocation's final cost (microunits)")
        ] = None,
        receipt: Annotated[
            Path | None,
            typer.Option("--receipt", help="the stopped-process usage receipt (JSON object)"),
        ] = None,
        config: ConfigOption = DEFAULT_CONFIG,
    ) -> None:
        """Human operator only (IC-18): end an interrupted stage experiment or settle an
        unresolved allocation of a proposal root."""

        def call() -> Any:
            from ..execution import meta_ops

            value = (
                json.loads(Path(receipt).expanduser().read_text()) if receipt is not None else None
            )
            with opened_deployment(config) as dep:
                return meta_ops.reconcile(
                    dep, dep.meta_operator(), proposal_id, stage=stage, allocation_id=allocation,
                    tokens=tokens, cost=cost, receipt=value,
                )  # fmt: skip

        guarded(call)
