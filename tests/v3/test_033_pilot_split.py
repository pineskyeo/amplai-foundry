"""Work 033 pilot split rule ``stratified_by_domain_pilot_v1`` (operator decision 2026-10-08).

Contract: specs/033-harness-taxonomy/interfaces.md §10.1, §10.2 and the clarification "Pilot Split
Rule (2026-10-08)". The committed corpus is read in place and copied to ``tmp_path`` for writes;
no docker, no network.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from meta_world import MetaReference

from amplai_foundry.meta_harness import corpus_v2
from amplai_foundry.meta_harness.corpus_v2 import TaskV2
from amplai_foundry.meta_harness.local_corpus import CorpusError

REPO = Path(__file__).resolve().parents[2]
V2_ROOT = REPO / "specs" / "033-harness-taxonomy" / "corpus"
SCRIPT = REPO / "scripts" / "corpus_check.py"
PILOT = corpus_v2.PILOT_SPLIT_METHOD
PILOT_SEED = 20261008  # fixed before computing (interfaces "Pilot Split Rule (2026-10-08)")
OWN_DOMAINS = ("bug", "feature", "refactor", "cli_ops", "data", "ambiguity")


def _mk(task_id: str, domain: str, *, kind: str = "own", set_name: str = "main") -> TaskV2:
    return TaskV2(
        task_id,
        domain,
        None,
        set_name,  # type: ignore[arg-type]
        {"kind": kind, "ref": "r", "author": "a", "created": "2026-09-30"},
        "LicenseRef-amplai-internal",
        "bench",
        "app",
        "pytest_hidden",
        "objective",
        ("a",),
        {},
        {},
        {},
        {},
        None,
        None,
    )


def _fixture(per_domain: int, *, tb2: int = 0) -> list[TaskV2]:
    tasks = [_mk(f"{d}-{i:03d}", d) for d in OWN_DOMAINS for i in range(per_domain)]
    return tasks + [_mk(f"tb2-{i:03d}", "terminal", kind="tb2") for i in range(tb2)]


def _totals(assignments: dict[str, str]) -> dict[str, int]:
    return {s: sum(1 for v in assignments.values() if v == s) for s in corpus_v2.SPLITS}


def _digest(assignments: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(assignments.items())).encode()).hexdigest()


def _copy_corpus(tmp_path: Path, *, with_splits: bool) -> Path:
    root = tmp_path / "corpus"
    shutil.copytree(V2_ROOT, root)
    if not with_splits:
        (root / "splits.json").unlink()
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["split_seed"] = None
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return root


# -- the rule ------------------------------------------------------------------------------------


def test_pilot_rule_on_the_real_corpus_meets_the_minimums() -> None:
    corpus = corpus_v2.load(V2_ROOT)
    tasks = list(corpus.tasks)
    result = corpus_v2.assign_splits(tasks, seed=PILOT_SEED, method=PILOT)
    totals = _totals(result)
    assert totals["holdout"] >= corpus_v2.HOLDOUT_MIN
    assert totals["validation"] >= corpus_v2.VALIDATION_MIN
    main = [t for t in tasks if t.set == "main"]
    assert set(result) == {t.task_id for t in main}  # the regression set is not assigned
    for domain in {t.domain for t in main}:
        own = [t for t in main if t.domain == domain and t.source["kind"] == "own"]
        mine = {t.task_id: result[t.task_id] for t in main if t.domain == domain}
        holdout = sum(1 for v in mine.values() if v == "holdout")
        validation = sum(1 for v in mine.values() if v == "validation")
        assert holdout == (3 * len(own) + 5) // 10, domain  # 30 % of own, half up (as v1)
        assert validation == (len(mine) - holdout + 1) // 2, domain  # 50 % of the rest, half up


def test_v1_on_the_real_corpus_is_still_too_small() -> None:
    # the reason for the pilot rule: own tasks alone give validation < 24 under v1 (§10.2)
    with pytest.raises(CorpusError) as e:
        corpus_v2.assign_splits(list(corpus_v2.load(V2_ROOT).tasks), seed=PILOT_SEED)
    assert e.value.code == "SPLIT_TOO_SMALL" and corpus_v2.SPLIT_METHOD in str(e.value)


def test_v1_is_unchanged() -> None:
    # digests computed with the stratified_by_domain_v1 code of commit f901a2a (before this rule)
    tasks = _fixture(20)
    v1 = corpus_v2.assign_splits(tasks, seed=11)
    assert corpus_v2.assign_splits(tasks, seed=11, method=corpus_v2.SPLIT_METHOD) == v1
    assert _digest(v1) == "7ba5e55f5ffddf134507d84f41a31f8d74fe6a7e12daac54f072f7601980e5a4"
    with_tb2 = corpus_v2.assign_splits(_fixture(10, tb2=40), seed=5)
    assert _digest(with_tb2) == "57d378f98ad0c33dfd60b904929235084312e855e1eeae3ba14294a2358786b5"


def test_pilot_shares_the_v1_holdout_and_moves_development_to_validation() -> None:
    tasks = _fixture(20)
    v1 = corpus_v2.assign_splits(tasks, seed=11)
    pilot = corpus_v2.assign_splits(tasks, seed=11, method=PILOT)
    assert {k for k, v in pilot.items() if v == "holdout"} == {
        k for k, v in v1.items() if v == "holdout"
    }
    v1_validation = {k for k, v in v1.items() if v == "validation"}
    assert v1_validation <= {k for k, v in pilot.items() if v == "validation"}
    # per domain: 6 holdout of 20 own; 50 % of the remaining 14 = 7 validation
    assert _totals(pilot) == {"holdout": 36, "validation": 42, "development": 42}


def test_pilot_adding_a_domain_leaves_existing_assignments_unchanged() -> None:
    base = _fixture(12)
    a = corpus_v2.assign_splits(base, seed=PILOT_SEED, method=PILOT)
    extra = base + [_mk(f"terminal-x{i}", "terminal") for i in range(10)]
    b = corpus_v2.assign_splits(extra, seed=PILOT_SEED, method=PILOT)
    assert {k: v for k, v in b.items() if k in a} == a
    # tasks added to one domain never reshuffle another domain
    grown = base + [_mk(f"bug-{i:03d}", "bug") for i in range(12, 20)]
    c = corpus_v2.assign_splits(grown, seed=PILOT_SEED, method=PILOT)
    assert {k: v for k, v in c.items() if k in a and not k.startswith("bug-")} == {
        k: v for k, v in a.items() if not k.startswith("bug-")
    }


def test_pilot_keeps_the_minimum_check_and_the_tb2_rule() -> None:
    with pytest.raises(CorpusError) as e:
        corpus_v2.assign_splits(_fixture(4), seed=1, method=PILOT)
    assert e.value.code == "SPLIT_TOO_SMALL" and PILOT in str(e.value)
    result = corpus_v2.assign_splits(_fixture(10, tb2=20), seed=3, method=PILOT)
    assert all(result[f"tb2-{i:03d}"] != "holdout" for i in range(20))


def test_unknown_split_method_is_task_split() -> None:
    with pytest.raises(CorpusError) as e:
        corpus_v2.assign_splits(_fixture(20), seed=1, method="stratified_by_domain_v9")
    assert e.value.code == "TASK_SPLIT"


# -- the committed splits.json -------------------------------------------------------------------


def test_the_committed_splits_json_is_the_pilot_rule_with_the_fixed_seed() -> None:
    data = json.loads((V2_ROOT / "splits.json").read_text())
    manifest = json.loads((V2_ROOT / "manifest.json").read_text())
    assert data["seed"] == PILOT_SEED == manifest["split_seed"]
    assert data["method"] == PILOT
    corpus = corpus_v2.load(V2_ROOT)
    expected = corpus_v2.assign_splits(list(corpus.tasks), seed=PILOT_SEED, method=PILOT)
    assert data["assignments"] == expected
    assert list(data["assignments"]) == sorted(data["assignments"])
    assert corpus.split_seed == PILOT_SEED and corpus.split_method == PILOT
    assert {t.task_id: t.split for t in corpus.tasks if t.set == "main"} == expected


def test_write_splits_reproduces_the_committed_file(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path, with_splits=False)
    result = corpus_v2.write_splits(root, seed=PILOT_SEED, method=PILOT)
    assert (root / "splits.json").read_bytes() == (V2_ROOT / "splits.json").read_bytes()
    assert json.loads((root / "manifest.json").read_text()) == json.loads(
        (V2_ROOT / "manifest.json").read_text()
    )
    assert result["seed"] == PILOT_SEED and result["method"] == PILOT
    assert sum(sum(d.values()) for d in result["counts"].values()) == len(
        json.loads((V2_ROOT / "splits.json").read_text())["assignments"]
    )
    # the same call again is a no-op; another seed or rule is refused (a re-split is a new version)
    assert corpus_v2.write_splits(root, seed=PILOT_SEED, method=PILOT) == result
    for seed, method in ((PILOT_SEED + 1, PILOT), (PILOT_SEED, corpus_v2.SPLIT_METHOD)):
        with pytest.raises(CorpusError) as e:
            corpus_v2.write_splits(root, seed=seed, method=method)
        assert e.value.code in ("TASK_SPLIT", "SPLIT_TOO_SMALL")
    assert (root / "splits.json").read_bytes() == (V2_ROOT / "splits.json").read_bytes()


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_corpus_check_writes_splits(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path, with_splits=False)
    done = _cli("--corpus", str(root), "--write-splits", PILOT, "--seed", str(PILOT_SEED))
    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout)
    assert report["method"] == PILOT and report["seed"] == PILOT_SEED
    assert (root / "splits.json").read_bytes() == (V2_ROOT / "splits.json").read_bytes()
    assert _cli("--corpus", str(root), "--write-splits", PILOT).returncode == 2  # no seed
    assert _cli("--write-splits", PILOT, "--seed", "1").returncode == 2  # no corpus


# -- the freeze path -----------------------------------------------------------------------------


@pytest.fixture
def meta_ref(tmp_path: Path) -> Any:
    ref = MetaReference(tmp_path / "meta")
    try:
        yield ref
    finally:
        ref.close()


def test_freeze_reads_the_pilot_splits_file(tmp_path: Path, meta_ref: MetaReference) -> None:
    root = _copy_corpus(tmp_path, with_splits=True)
    assignments = json.loads((root / "splits.json").read_text())["assignments"]
    main = corpus_v2.for_set(corpus_v2.load(root), "main")
    d = meta_ref.d
    refs = corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, main, holdout_use_limit=3)
    frozen = d.store.get(d.scope, "eval-corpus", refs["corpus_ref"])
    assert {c["case_id"]: c["split"] for c in frozen["cases"]} == assignments
    index = d.store.get(d.scope, "corpus-task-index", refs["task_index_ref"])
    assert index["splits"]["seed"] == PILOT_SEED and index["splits"]["method"] == PILOT
    tasks = list(main.tasks)
    assert index["splits"]["counts"] == corpus_v2.split_counts(tasks, assignments)
