"""``amplai meta``: the operator's meta-harness gates as commands (Work 030 S6, D-089).

Each command opens the local product's deployment, runs one gate as the human operator (the
proposal as the meta-proposer identity) and prints what happened. A gate that is not commanded does
not happen, and there is no command that chains them.

The commands work on the deployment in-process, so stop ``amplai ops local-serve`` first or use a
deployment of its own (``amplai ops local-init --home ...``): two processes must not own one store.

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

    @meta.command("approve-canary")
    def approve_canary(
        proposal_id: str,
        tasks: Annotated[str, typer.Option("--tasks", help="comma-separated corpus task ids")],
        max_trial_tokens: Annotated[int, typer.Option("--max-trial-tokens")],
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Approve a canary of those low-risk tasks (run-canary starts it)."""
        run(
            config, driver, corpus,
            lambda ops: ops.approve_canary(
                proposal_id, tasks.split(","), max_trial_tokens=max_trial_tokens
            ),
        )  # fmt: skip

    @meta.command("run-canary")
    def run_canary(
        proposal_id: str,
        per_trial_tokens: Annotated[int, typer.Option("--per-trial-tokens")],
        basis: Annotated[str, typer.Option("--basis")],
        evidence: Annotated[list[str], typer.Option("--evidence")],
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Start the approved canary and run its tasks; request promotion if all pass."""

        def gate(ops: Any) -> Any:
            ops.qualify_executor(basis, list(evidence), per_trial_tokens)
            return ops.run_canary(proposal_id)

        run(config, driver, corpus, gate)

    @meta.command("promote")
    def promote(
        proposal_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Point the active release at the candidate (new plans use it; running goals do not)."""
        run(config, driver, corpus, lambda ops: ops.promote(proposal_id))

    @meta.command("rollback")
    def rollback(
        proposal_id: str,
        config: ConfigOption = DEFAULT_CONFIG,
        driver: DriverOption = "codex-cli",
        corpus: CorpusOption = DEFAULT_CORPUS,
    ) -> None:
        """Return the active release to the one the promotion named."""
        run(config, driver, corpus, lambda ops: ops.rollback(proposal_id))

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
