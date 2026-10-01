"""``amplai meta``: the operator's meta-harness gates as commands (Work 030 S6, D-089).

Each command opens the local product's deployment, runs one gate as the human operator (the
proposal as the meta-proposer identity) and prints what happened. A gate that is not commanded does
not happen, and there is no command that chains them.

The commands work on the deployment in-process, so stop ``amplai ops local-serve`` first or use a
deployment of its own (``amplai ops local-init --home ...``): two processes must not own one store.

Work 033: ``approve-canary``, ``run-canary``, ``promote`` and ``rollback`` serve a corpus v2
proposal as well (``opened_gate``: the corpus v2, the stage plan's cell and app); a Work 030
proposal keeps the Work 030 corpus and options.

Work 033 S11: the corpus v2 command groups (stages, components, calibration, corpus) and the
human operator's ``reconcile`` (IC-18, provisional) live in ``runtime/meta_commands/`` and are
registered by module discovery; the gates above stay.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer

T = TypeVar("T")
DEFAULT_CONFIG = Path("~/.amplai/local/local.json")
# the corpus ships in the source tree; a checkout or the server clone both have it
DEFAULT_CORPUS = Path(__file__).resolve().parents[3] / "specs" / "030-meta-harness-live" / "corpus"

ConfigOption = Annotated[Path, typer.Option("--config", help="the local product config")]
DriverOption = Annotated[str, typer.Option("--driver", help="the driver whose prompt changes")]
CorpusOption = Annotated[Path, typer.Option("--corpus", help="the demo-app task corpus")]
# the canary gates (approve-canary, run-canary, promote, rollback) serve both kinds of proposal:
# unset, a Work 030 proposal takes the defaults above and a corpus v2 proposal its stage plan's
GateDriverOption = Annotated[
    str | None,
    typer.Option(
        "--driver",
        help="the driver whose prompt changes (Work 030; default codex-cli); a corpus v2 "
        "proposal's cell is its stage plan's",
    ),
]
GateCorpusOption = Annotated[
    Path | None,
    typer.Option(
        "--corpus",
        help="the task corpus (default: the demo-app corpus for a Work 030 proposal, "
        "meta.corpus_root for a corpus v2 proposal)",
    ),
]
GateAppOption = Annotated[
    str | None,
    typer.Option("--app", help="a corpus v2 proposal's app (default: its stage plan's)"),
]


def trial_traces(dep: Any) -> Any:
    """The ``TraceService`` a trial executor captures with (Work 033 S13, §9.1): trial goals
    capture unless ``meta.trace_capture`` is false (§12.2; absent ``meta`` keeps the default)."""
    from ..meta_harness.traces import TraceService

    meta = getattr(dep.config, "meta", None)
    if meta is not None and not meta.trace_capture:
        return None
    return TraceService(dep.store, dep.scope, dep.artifacts)


@contextmanager
def opened(config: Path, driver: str, corpus_root: Path) -> Iterator[Any]:
    """The operator's meta gates on the deployment at ``config`` (closed afterwards)."""
    from ..meta_harness import local_corpus
    from ..meta_harness.local_executor import LocalTrialExecutor
    from .execution.loop import ExecutionLoop
    from .execution.meta_ops import LocalMetaOps
    from .local_deployment import LocalProductDeployment

    dep = LocalProductDeployment(Path(config).expanduser(), start_loop=False)
    try:
        corpus = local_corpus.load(Path(corpus_root).expanduser())
        loop = ExecutionLoop(dep.service, dep.coordinator, publisher=None)
        executor = LocalTrialExecutor(
            dep.service, loop, dep.goals, dep.operator(), corpus, traces=trial_traces(dep)
        )
        yield LocalMetaOps(dep, corpus, executor, driver=driver)
    finally:
        dep.close()


@contextmanager
def opened_gate(
    config: Path,
    proposal_id: str,
    *,
    driver: str | None = None,
    corpus: Path | None = None,
    app: str | None = None,
) -> Iterator[tuple[Any, bool]]:
    """``(LocalMetaOps, v2)`` for a canary gate of ``proposal_id`` on the deployment at
    ``config`` (closed afterwards).

    A proposal with a stage plan (``meta_ops.stage_plan``) is a corpus v2 proposal: the corpus v2
    (``--corpus``, else ``meta.corpus_root``), the plan's cell and the plan's app (``--app`` when
    the plan predates S7b), the trial executor with trace capture as ``opened_v2``. Hold
    CELL_UNKNOWN when ``--driver`` names another cell and TARGET_UNKNOWN when ``--app`` names
    another app than the plan. Any other proposal opens as ``opened`` does (the Work 030 corpus,
    ``--driver`` default codex-cli); ``--app`` names nothing there (RuntimeFault LOCAL_INPUT)."""
    from ..meta_harness import local_corpus
    from ..meta_harness.local_executor import LocalTrialExecutor
    from .errors import Hold, RuntimeFault
    from .execution.loop import ExecutionLoop
    from .execution.meta_ops import LocalMetaOps, stage_plan
    from .local_deployment import LocalProductDeployment
    from .meta_commands import corpus_root, load_corpus

    dep = LocalProductDeployment(Path(config).expanduser(), start_loop=False)
    try:
        plan = stage_plan(dep.store, dep.scope, proposal_id)
        loaded: Any
        if plan is None:
            if app is not None:
                raise RuntimeFault("LOCAL_INPUT", "--app names a corpus v2 proposal's app")
            loaded = local_corpus.load(Path(corpus or DEFAULT_CORPUS).expanduser())
            cell, app_id = driver or "codex-cli", None
        else:
            cell, app_id = str(plan["cell_id"]), plan.get("app_id") or app
            if driver is not None and driver != cell:
                raise Hold(
                    "CELL_UNKNOWN",
                    "The proposal's stage plan names another cell",
                    details={"driver": driver, "cell_id": cell},
                )
            if app is not None and app != app_id:
                raise Hold(
                    "TARGET_UNKNOWN",
                    "The proposal's stage plan names another app",
                    details={"app": app, "app_id": app_id},
                )
            loaded = load_corpus(corpus_root(dep, corpus))
        loop = ExecutionLoop(dep.service, dep.coordinator, publisher=None)
        executor = LocalTrialExecutor(
            dep.service, loop, dep.goals, dep.operator(), loaded, traces=trial_traces(dep)
        )
        ops = LocalMetaOps(dep, loaded, executor, driver=cell, app_id=app_id)
        if app_id is not None:
            _ = ops.app  # an app that is not installed or not named by the corpus is refused
        yield ops, plan is not None
    finally:
        dep.close()


def register(app: typer.Typer, guarded: Callable[[Callable[[], T]], T]) -> None:
    meta = typer.Typer(help="Meta-harness gates: propose, screen, experiment, canary, promote")
    app.add_typer(meta, name="meta")

    def run(config: Path, driver: str, corpus: Path, gate: Callable[[Any], Any]) -> None:
        def call() -> Any:
            with opened(config, driver, corpus) as ops:
                return gate(ops)

        guarded(call)

    @meta.command("propose")
    def propose(
        suffix: Annotated[str, typer.Option("--suffix", help="names the candidate")],
        prompt_file: Annotated[
            Path, typer.Option("--prompt-file", help="the implementer role text, one line each")
        ],
        hypothesis: Annotated[str, typer.Option("--hypothesis")],
        benefit: Annotated[str, typer.Option("--benefit")],
        observation: Annotated[str, typer.Option("--observation")],
        risk: Annotated[list[str], typer.Option("--risk")],
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Submit a class-A candidate of the IMPLEMENTER prompt as the meta-proposer."""
        lines = [x for x in prompt_file.expanduser().read_text().splitlines() if x.strip()]
        run(
            config, driver, corpus,
            lambda ops: {"proposal_id": ops.propose(
                suffix=suffix, implementer_lines=lines, hypothesis=hypothesis,
                expected_benefit=benefit, observation=observation, risks=list(risk),
            )},
        )  # fmt: skip

    @meta.command("screen")
    def screen(
        proposal_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Check the candidate against the protected surfaces."""
        run(config, driver, corpus, lambda ops: ops.screen(proposal_id))

    @meta.command("approve-experiment")
    def approve_experiment(
        proposal_id: str,
        max_tokens: Annotated[int, typer.Option("--max-tokens", help="token budget of the run")],
        max_wall_seconds: Annotated[int, typer.Option("--max-wall-seconds")],
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Freeze the corpus and the pre-registered analysis, then approve the experiment."""
        run(
            config, driver, corpus,
            lambda ops: ops.approve_experiment(
                proposal_id, max_tokens=max_tokens, max_wall_seconds=max_wall_seconds
            ),
        )  # fmt: skip

    @meta.command("run-experiment")
    def run_experiment(
        proposal_id: str,
        per_trial_tokens: Annotated[int, typer.Option("--per-trial-tokens")],
        basis: Annotated[
            str, typer.Option("--basis", help="what the executor qualification rests on")
        ],
        evidence: Annotated[list[str], typer.Option("--evidence")],
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Run every trial (real goals, publication off) and record the verdict."""

        def gate(ops: Any) -> Any:
            ops.qualify_executor(basis, list(evidence), per_trial_tokens)
            return ops.run_experiment(proposal_id)

        run(config, driver, corpus, gate)

    # Work 033: the canary gates open the deployment for the proposal (``opened_gate``): a
    # corpus v2 proposal (it has a stage plan) on the corpus v2 with the plan's cell and app, a
    # Work 030 proposal on the Work 030 corpus as before (same options and defaults).
    def gated(
        proposal_id: str,
        config: Path,
        driver: str | None,
        corpus: Path | None,
        app_id: str | None,
        gate: Callable[[Any, bool], Any],
    ) -> None:
        def call() -> Any:
            with opened_gate(
                config, proposal_id, driver=driver, corpus=corpus, app=app_id
            ) as found:
                return gate(*found)

        guarded(call)

    @meta.command("approve-canary")
    def approve_canary(
        proposal_id: str,
        tasks: Annotated[str, typer.Option("--tasks", help="comma-separated corpus task ids")],
        max_trial_tokens: Annotated[int, typer.Option("--max-trial-tokens")],
        config: ConfigOption = DEFAULT_CONFIG,
        driver: GateDriverOption = None,
        corpus: GateCorpusOption = None,
        app_id: GateAppOption = None,
    ) -> None:
        """Approve a canary of those low-risk tasks (run-canary starts it). A corpus v2
        proposal's tasks are development or validation tasks of its app, never holdout ones."""
        gated(
            proposal_id, config, driver, corpus, app_id,
            lambda ops, _v2: ops.approve_canary(
                proposal_id, tasks.split(","), max_trial_tokens=max_trial_tokens
            ),
        )  # fmt: skip

    @meta.command("run-canary")
    def run_canary(
        proposal_id: str,
        per_trial_tokens: Annotated[int | None, typer.Option("--per-trial-tokens")] = None,
        basis: Annotated[str | None, typer.Option("--basis")] = None,
        evidence: Annotated[list[str] | None, typer.Option("--evidence")] = None,
        config: ConfigOption = DEFAULT_CONFIG,
        driver: GateDriverOption = None,
        corpus: GateCorpusOption = None,
        app_id: GateAppOption = None,
    ) -> None:
        """Start the approved canary and run its tasks; request promotion if all pass. A Work
        030 proposal needs --per-trial-tokens, --basis and --evidence; a corpus v2 proposal
        without them uses the operator's newest executor qualification of its cell (IC-29)."""

        def gate(ops: Any, v2: bool) -> Any:
            from .errors import Hold, RuntimeFault

            if per_trial_tokens is not None and basis is not None and evidence:
                ops.qualify_executor(basis, list(evidence), per_trial_tokens)
            elif per_trial_tokens is not None or basis is not None or evidence or not v2:
                raise RuntimeFault(
                    "LOCAL_INPUT", "--per-trial-tokens, --basis and --evidence go together "
                    "(a Work 030 proposal's canary needs them)",
                )  # fmt: skip
            elif ops.restore_qualification() is None:
                raise Hold(
                    "QUALIFIED_EXECUTOR_REQUIRED",
                    "No executor-qualification of a human operator for this cell (IC-29); "
                    "give --per-trial-tokens, --basis and --evidence",
                    details={"cell_id": ops.driver},
                )
            return ops.run_canary(proposal_id)

        gated(proposal_id, config, driver, corpus, app_id, gate)

    @meta.command("promote")
    def promote(
        proposal_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        driver: GateDriverOption = None,
        corpus: GateCorpusOption = None,
        app_id: GateAppOption = None,
    ) -> None:
        """Point the active release at the candidate (new plans use it; running goals do not)."""
        gated(
            proposal_id, config, driver, corpus, app_id, lambda ops, _v2: ops.promote(proposal_id)
        )

    @meta.command("rollback")
    def rollback(
        proposal_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        driver: GateDriverOption = None,
        corpus: GateCorpusOption = None,
        app_id: GateAppOption = None,
    ) -> None:
        """Return the active release to the one the promotion named."""
        gated(
            proposal_id, config, driver, corpus, app_id, lambda ops, _v2: ops.rollback(proposal_id)
        )

    @meta.command("reject")
    def reject(
        proposal_id: str,
        reason: Annotated[str, typer.Option("--reason")],
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """End a candidate that should not go on (the reason is required)."""
        run(config, driver, corpus, lambda ops: {"state": ops.reject(proposal_id, reason)})

    @meta.command("abort")
    def abort(
        proposal_id: str,
        reason: Annotated[str, typer.Option("--reason")],
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Stop an approved experiment or canary; the active release does not change."""
        run(config, driver, corpus, lambda ops: ops.abort(proposal_id, reason))

    @meta.command("report")
    def report(
        proposal_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Metrics of the candidate's experiment beyond the verdict (descriptive, D-094)."""
        run(config, driver, corpus, lambda ops: ops.report(proposal_id))

    @meta.command("status")
    def status(
        proposal_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Where the candidate is and which release is active."""
        run(config, driver, corpus, lambda ops: ops.status(proposal_id))

    # Work 033 S11 (interfaces.md §1.1, §12.1): the command groups of runtime/meta_commands/,
    # found by module discovery so parallel slices add modules without editing this file.
    from .meta_commands import register_all

    register_all(meta, guarded)
