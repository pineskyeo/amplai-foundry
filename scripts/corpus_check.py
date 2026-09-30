"""Check that the demo-app corpus tasks are fair (Work 030 S4).

    .venv/bin/python scripts/corpus_check.py [--repeats N] [task_id ...]

With no task ids the whole corpus in manifest.json is checked. ``--repeats N`` judges every
task N times and fails a task whose verdict changes between runs (flaky grading, D-094).
A task is fair when the hidden tests fail on the base tree and pass on its reference
solution, and the visible tests pass on both.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from amplai_foundry.meta_harness import local_corpus

ROOT = Path(__file__).resolve().parents[1] / "specs" / "030-meta-harness-live" / "corpus"


def main(argv: list[str]) -> int:
    repeats = 1
    if argv[:1] == ["--repeats"]:
        repeats, argv = int(argv[1]), argv[2:]
    corpus = local_corpus.load(ROOT, argv or None)
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
