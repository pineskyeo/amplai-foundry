"""Work 030 S4 (D-089) — the demo-app task corpus is fair, complete and does not leak its tests.

Every task must fail its hidden tests on the base tree and pass them on its reference solution,
and the agent-facing text must not carry the hidden test content.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from amplai_foundry.meta_harness import local_corpus

ROOT = Path(__file__).resolve().parents[2] / "specs" / "030-meta-harness-live" / "corpus"


@pytest.fixture(scope="module")
def corpus() -> local_corpus.Corpus:
    return local_corpus.load(ROOT)


def test_the_corpus_has_twenty_tasks_in_three_difficulties(corpus: local_corpus.Corpus) -> None:
    assert len(corpus.tasks) == 20
    assert Counter(t.difficulty for t in corpus.tasks) == {"small": 7, "medium": 7, "large": 6}
    assert [t.task_id[0] for t in corpus.tasks] == sorted(
        (t.task_id[0] for t in corpus.tasks), key="sml".index
    )
    assert corpus.app_id == "amplai-demo-app" and len(corpus.base_commit) == 40


def test_the_base_snapshot_is_the_pinned_demo_app_tree(corpus: local_corpus.Corpus) -> None:
    files = sorted(
        str(p.relative_to(corpus.base))
        for p in corpus.base.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    assert files == [
        ".gitignore",
        "README.md",
        "demo_app/__init__.py",
        "demo_app/version_report.py",
        "tests/integration/check_foundry_version.py",
        "tests/test_version_report.py",
    ]
    assert "def summarize" in (corpus.base / "demo_app" / "version_report.py").read_text()


def test_every_task_is_fair(corpus: local_corpus.Corpus, tmp_path: Path) -> None:
    results = local_corpus.validate(corpus, tmp_path)
    unfair = {task_id: row for task_id, row in results.items() if not row["fair"]}
    assert not unfair, unfair


def test_what_the_agent_reads_never_carries_the_hidden_tests(corpus: local_corpus.Corpus) -> None:
    for task in corpus.tasks:
        text = task.contract_text()
        assert "hidden" not in text.lower(), task.task_id
        assert not re.search(r"\btest_", text), task.task_id
        assert task.objective.strip() and task.acceptance, task.task_id
        for name, data in task.hidden.items():
            assert name.startswith("tests/hidden/" + task.task_id + "/")
            # no whole hidden assertion line appears in the contract
            for line in data.decode().splitlines():
                stripped = line.strip()
                if stripped.startswith("assert ") and len(stripped) > 30:
                    assert stripped not in text, (task.task_id, stripped)


def test_the_manifest_lists_exactly_the_task_directories(corpus: local_corpus.Corpus) -> None:
    on_disk = {p.name for p in (ROOT / "tasks").iterdir() if p.is_dir()}
    assert on_disk == {t.task_id for t in corpus.tasks}
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["task_ids"] == [t.task_id for t in corpus.tasks]
