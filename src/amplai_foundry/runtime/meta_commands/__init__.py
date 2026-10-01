"""``amplai meta`` command groups of Work 033 (interfaces.md §1.1, §12.1).

Every module of this package that defines ``register(meta, guarded)`` is registered by
``register_all`` (module discovery), so a slice adds its command group as a module of its own and
never edits a shared registration list. The existing Work 030 gates stay in ``runtime/meta_cli.py``.

Shared here: the options every group uses and ``opened_v2``, the deployment opened in-process
(as the Work 030 commands do, ``runtime/meta_cli.py``) with the corpus v2, its trial executor and
``LocalMetaOps``. Commands run as the operator identity (``local_deployment.meta_operator``); a
proposal or component is written by the proposer identity.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from ..errors import RuntimeFault

if TYPE_CHECKING:
    from ...meta_harness.corpus_v2 import CorpusV2
    from ..execution.meta_ops import LocalMetaOps
    from ..local_deployment import LocalProductDeployment

Guarded = Callable[[Callable[[], Any]], Any]
# the checkout this code runs from; §12.2 meta.corpus_root is relative to it
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG = Path("~/.amplai/local/local.json")
DEFAULT_CORPUS_ROOT = "specs/033-harness-taxonomy/corpus"  # §12.2 default of meta.corpus_root

ConfigOption = Annotated[Path, typer.Option("--config", help="the local product config")]
CorpusOption = Annotated[
    Path | None,
    typer.Option("--corpus", help="the corpus v2 root (default: meta.corpus_root of the config)"),
]
CellOption = Annotated[str, typer.Option("--cell", help="the cell id (a driver id, IC-07)")]
AppOption = Annotated[
    str | None,
    typer.Option(
        "--app",
        help="the installed app (default: the one the corpus main-set tasks name; "
        "required when they name several)",
    ),
]
ParallelOption = Annotated[
    int, typer.Option("--parallel", help="trials at once (1-4, at most meta.max_parallel_trials)")
]
PerTrialOption = Annotated[
    int, typer.Option("--per-trial-tokens", help="the qualified per-trial token ceiling")
]
BasisOption = Annotated[
    str, typer.Option("--basis", help="what the executor qualification rests on")
]
EvidenceOption = Annotated[list[str], typer.Option("--evidence")]


def register_all(meta: typer.Typer, guarded: Guarded) -> None:
    """Call ``register(meta, guarded)`` of every module of this package, in name order."""
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        register = getattr(module, "register", None)
        if callable(register):
            register(meta, guarded)


def load_corpus(root: Path) -> CorpusV2:
    """``corpus_v2.load``; a ``CorpusError`` becomes a RuntimeFault with its code."""
    from ...meta_harness import corpus_v2
    from ...meta_harness.local_corpus import CorpusError

    try:
        return corpus_v2.load(Path(root).expanduser())
    except CorpusError as exc:
        raise RuntimeFault(exc.code, str(exc)) from exc


def corpus_root(dep: LocalProductDeployment | None, override: Path | None) -> Path:
    """``--corpus``, else the config's ``meta.corpus_root``, else the §12.2 default; a relative
    path is relative to the checkout."""
    if override is not None:
        return Path(override).expanduser()
    meta = getattr(getattr(dep, "config", None), "meta", None)
    value = Path(meta.corpus_root if meta is not None else DEFAULT_CORPUS_ROOT).expanduser()
    return value if value.is_absolute() else REPO_ROOT / value


def capped_parallel(dep: LocalProductDeployment, requested: int) -> int:
    """§8.4: at most ``meta.max_parallel_trials`` (the config's; 1..4)."""
    if type(requested) is not int or not 1 <= requested <= 4:
        raise RuntimeFault("EVAL_PARALLEL", "--parallel is an integer from 1 to 4")
    meta = getattr(dep.config, "meta", None)
    return min(requested, meta.max_parallel_trials) if meta is not None else requested


@contextmanager
def opened_deployment(config: Path) -> Iterator[LocalProductDeployment]:
    from ..local_deployment import LocalProductDeployment

    dep = LocalProductDeployment(Path(config).expanduser(), start_loop=False)
    try:
        yield dep
    finally:
        dep.close()


@contextmanager
def opened_v2(
    config: Path, corpus: Path | None, *, cell: str = "codex-cli", app: str | None = None
) -> Iterator[Any]:
    """``LocalMetaOps`` on the deployment at ``config`` with the corpus v2 (closed afterwards).
    ``app`` (``--app``) is resolved before the command runs: Hold TARGET_UNKNOWN when it is not
    installed or no main-set app-environment task of the corpus names it."""
    from ...meta_harness.local_executor import LocalTrialExecutor
    from ..execution.loop import ExecutionLoop
    from ..execution.meta_ops import LocalMetaOps

    with opened_deployment(config) as dep:
        loaded = load_corpus(corpus_root(dep, corpus))
        loop = ExecutionLoop(dep.service, dep.coordinator, publisher=None)
        executor = LocalTrialExecutor(dep.service, loop, dep.goals, dep.operator(), loaded)
        ops: LocalMetaOps = LocalMetaOps(dep, loaded, executor, driver=cell, app_id=app)
        if app is not None:
            _ = ops.app  # a wrong --app is refused for every command
        yield ops


def qualify(ops: LocalMetaOps, per_trial_tokens: int, basis: str, evidence: list[str]) -> None:
    """Pin the trial executor's qualification for this command (as ``run-experiment``)."""
    ops.qualify_executor(basis, list(evidence), per_trial_tokens)
