"""Check that corpus tasks are fair (Work 030 S4; corpus v2 mode Work 033 S5).

    .venv/bin/python scripts/corpus_check.py [--repeats N] [task_id ...]
    .venv/bin/python scripts/corpus_check.py --corpus specs/033-harness-taxonomy/corpus \
        [--repeats N] [--domain D ...] [task_id ...]

Without ``--corpus`` the Work 030 demo-app corpus (its manifest's task list) is checked, as
before. ``--repeats N`` judges every task N times and fails a task whose verdict changes between
runs (flaky grading, D-094). A task is fair when the hidden tests fail on the base tree and pass
on its reference solution, and the visible tests pass on both.

With ``--corpus`` the corpus v2 loader scans the task directories (interfaces §10); ``--domain``
narrows to domains. Ambiguity tasks that expect a question also need the ambiguity proof: each
reference passes its own hidden suite and fails the other's (§10.3). ``tb2_tests`` tasks are
listed as SKIP: their fairness is the TB2 admission run in the task image (§10.5).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from amplai_foundry.meta_harness import corpus_v2, local_corpus

ROOT = Path(__file__).resolve().parents[1] / "specs" / "030-meta-harness-live" / "corpus"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check corpus task fairness.")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--corpus", type=Path, help="a corpus v2 root (Work 033)")
    parser.add_argument("--domain", action="append", choices=corpus_v2.DOMAINS)
    parser.add_argument("task_ids", nargs="*")
    return parser


def _work030(task_ids: list[str], repeats: int) -> int:
    corpus = local_corpus.load(ROOT, task_ids or None)
    with tempfile.TemporaryDirectory(prefix="amplai-corpus-") as scratch:
        results = local_corpus.validate(corpus, Path(scratch), repeats=repeats)
    bad = 0
    for task_id, row in results.items():
        mark = "ok  " if row["fair"] else ("FLAKY" if row["flaky"] else "FAIL")
        print(f"{mark} {task_id}: base={row['base']} reference={row['reference']}")
        if not row["fair"]:
            bad += 1
            print("     ", row["detail"])
    print(f"{len(results) - bad}/{len(results)} fair")
    return 1 if bad else 0


def _v2(root: Path, domains: list[str] | None, task_ids: list[str], repeats: int) -> int:
    corpus = corpus_v2.load(root)
    known = {t.task_id for t in corpus.tasks}
    unknown = sorted(set(task_ids) - known)
    if unknown:
        print(f"unknown task ids: {unknown}")
        return 2
    tasks = tuple(
        t
        for t in corpus.tasks
        if (not domains or t.domain in domains) and (not task_ids or t.task_id in task_ids)
    )
    corpus = corpus_v2.CorpusV2(
        corpus.corpus_id, corpus.version, corpus.root, corpus.bases, tasks, corpus.split_seed
    )
    with tempfile.TemporaryDirectory(prefix="amplai-corpus-v2-") as scratch:
        results = corpus_v2.validate(corpus, Path(scratch), repeats=repeats)
    bad = skipped = 0
    for task in tasks:
        row = results[task.task_id]
        if row["fair"] is None:
            skipped += 1
            print(f"SKIP  {task.task_id} [{task.domain}]: {row['detail']}")
            continue
        mark = "ok   " if row["fair"] else ("FLAKY" if row["flaky"] else "FAIL ")
        print(
            f"{mark} {task.task_id} [{task.domain}]: base={row['base']} "
            f"reference={row['reference']}"
        )
        if "ambiguity" in row:
            print(f"      ambiguity proof: {row['ambiguity']}")
        if not row["fair"]:
            bad += 1
            print("      ", row["detail"])
    checked = len(results) - skipped
    print(
        f"{checked - bad}/{checked} fair" + (f", {skipped} skipped (tb2_tests)" if skipped else "")
    )
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if args.repeats < 1:
        print("--repeats must be at least 1")
        return 2
    if args.corpus is None:
        if args.domain:
            print("--domain needs --corpus (the Work 030 corpus has no domains)")
            return 2
        return _work030(args.task_ids, args.repeats)
    return _v2(args.corpus, args.domain, args.task_ids, args.repeats)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
