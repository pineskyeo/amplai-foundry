"""``amplai meta corpus check | freeze | import-work030`` (Work 033 S11; library S5,
interfaces.md §10, §12.1).

``check`` judges every task (``corpus_v2.validate``: hidden tests fail on the base and pass on the
reference, visible tests pass on both, plus the ambiguity proof); an unfair or flaky task is
``Hold CORPUS_UNFAIR`` with the per-task rows. ``freeze`` freezes one set (``main`` or the
``regression`` set, §10.6) as the operator: corpus, task index and leak index. ``import-work030``
copies the Work 030 tasks as regression tasks.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer

from ..errors import Hold, RuntimeFault
from . import (
    DEFAULT_CONFIG,
    REPO_ROOT,
    ConfigOption,
    CorpusOption,
    Guarded,
    corpus_root,
    load_corpus,
    opened_deployment,
)

WORK030_CORPUS = REPO_ROOT / "specs" / "030-meta-harness-live" / "corpus"


def register(meta: typer.Typer, guarded: Guarded) -> None:
    from ...meta_harness import corpus_v2
    from ...meta_harness.local_corpus import CorpusError

    group = typer.Typer(help="Corpus v2: check fairness, freeze a set, import Work 030 tasks")
    meta.add_typer(group, name="corpus")

    @group.command("check")
    def check(
        repeats: Annotated[int, typer.Option("--repeats", help="judge each task N times")] = 1,
        domain: Annotated[
            list[str] | None, typer.Option("--domain", help="only these domains")
        ] = None,
        corpus: CorpusOption = None,
    ) -> None:
        """Judge every task's fairness (base fails, reference passes)."""

        def call() -> Any:
            if repeats < 1:
                raise RuntimeFault("REPEATS", "--repeats is at least 1")
            unknown = sorted(set(domain or []) - set(corpus_v2.DOMAINS))
            if unknown:
                raise RuntimeFault("TASK_META", "Unknown domains", details=unknown)
            loaded = load_corpus(corpus_root(None, corpus))
            tasks = tuple(t for t in loaded.tasks if not domain or t.domain in domain)
            narrowed = corpus_v2.CorpusV2(
                loaded.corpus_id, loaded.version, loaded.root, loaded.bases, tasks,
                loaded.split_seed,
            )  # fmt: skip
            try:
                with tempfile.TemporaryDirectory(prefix="amplai-corpus-v2-") as scratch:
                    results = corpus_v2.validate(narrowed, Path(scratch), repeats=repeats)
            except CorpusError as exc:
                raise RuntimeFault(exc.code, str(exc)) from exc
            rows = {
                task_id: {
                    "fair": row.get("fair"), "flaky": row.get("flaky"),
                    "detail": row.get("detail"), "proven": (row.get("ambiguity") or {}).get(
                        "proven"),
                }
                for task_id, row in results.items()
            }  # fmt: skip
            bad = sorted(t for t, row in rows.items() if row["fair"] is False)
            summary = {
                "checked": sum(row["fair"] is not None for row in rows.values()),
                "skipped": sorted(t for t, row in rows.items() if row["fair"] is None),
                "unfair": bad,
                "repeats": repeats,
            }
            if bad:
                raise Hold("CORPUS_UNFAIR", f"{len(bad)} task(s) are not fair",
                           details={**summary, "tasks": {t: rows[t] for t in bad}})  # fmt: skip
            return {**summary, "tasks": rows}

        guarded(call)

    @group.command("freeze")
    def freeze(
        holdout_use_limit: Annotated[
            int, typer.Option("--holdout-use-limit", help="holdout uses across versions")
        ],
        set_name: Annotated[str, typer.Option("--set", help="main or regression (§10.6)")] = "main",
        config: ConfigOption = DEFAULT_CONFIG,
        corpus: CorpusOption = None,
    ) -> None:
        """Freeze one set: the corpus record, its task index and its leak index."""

        def call() -> Any:
            with opened_deployment(config) as dep:
                loaded = load_corpus(corpus_root(dep, corpus))
                try:
                    one = corpus_v2.for_set(loaded, set_name)
                    return corpus_v2.freeze(
                        dep.meta_operator(), dep.store, dep.artifacts, one,
                        holdout_use_limit=holdout_use_limit,
                    )  # fmt: skip
                except CorpusError as exc:
                    raise RuntimeFault(exc.code, str(exc)) from exc

        guarded(call)

    @group.command("import-work030")
    def import_work030(
        source: Annotated[Path, typer.Option("--source", help="the Work 030 corpus")] = (
            WORK030_CORPUS
        ),
        corpus: CorpusOption = None,
    ) -> None:
        """Copy the Work 030 tasks as regression tasks of the corpus v2."""

        def call() -> Any:
            try:
                imported = corpus_v2.import_work030(
                    Path(source).expanduser(), corpus_root(None, corpus)
                )
            except CorpusError as exc:
                raise RuntimeFault(exc.code, str(exc)) from exc
            return {"imported": imported}

        guarded(call)
