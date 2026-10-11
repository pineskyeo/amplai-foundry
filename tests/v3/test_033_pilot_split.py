"""Work 033 split rules: ``stratified_by_domain_pilot_v1`` (operator decision 2026-10-08) and
``stratified_by_domain_append_v1`` (operator decision 2026-10-11).

Contract: specs/033-harness-taxonomy/interfaces.md §10.1, §10.2 and the clarifications "Pilot Split
Rule (2026-10-08)" and "Append Split Rule (2026-10-11)". Corpus version 2.0.0 was split with the
pilot rule (its file is kept as ``splits-2.0.0.json``); version 2.1.0 keeps every 2.0.0 assignment
and places only the new tasks. The committed corpus is read in place and copied to ``tmp_path``
for writes; no docker, no network.
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
APPEND = corpus_v2.APPEND_SPLIT_METHOD
APPEND_SEED = 20261011  # fixed before computing (interfaces "Append Split Rule (2026-10-11)")
PREVIOUS = V2_ROOT / "splits-2.0.0.json"  # the 2.0.0 splits.json, byte for byte
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


def _previous() -> dict[str, str]:
    assignments: dict[str, str] = json.loads(PREVIOUS.read_text())["assignments"]
    return assignments


def _v200_tasks() -> list[TaskV2]:
    """The tasks of corpus version 2.0.0: its main tasks (``splits-2.0.0.json``) and the
    regression set (never assigned)."""
    previous = _previous()
    return [t for t in corpus_v2.load(V2_ROOT).tasks if t.task_id in previous or t.set != "main"]


# -- the rule ------------------------------------------------------------------------------------


def test_pilot_rule_on_the_2_0_0_corpus_meets_the_minimums() -> None:
    tasks = _v200_tasks()
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


def test_v1_on_the_2_0_0_corpus_is_too_small() -> None:
    # the reason for the pilot rule: own tasks alone give validation < 24 under v1 (§10.2)
    with pytest.raises(CorpusError) as e:
        corpus_v2.assign_splits(_v200_tasks(), seed=PILOT_SEED)
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


# -- the append rule -----------------------------------------------------------------------------


def _grown(per_domain: int, new_per_domain: int, *, tb2_new: int = 0) -> list[TaskV2]:
    old = _fixture(per_domain)
    new = [_mk(f"{d}-n{i:03d}", d) for d in OWN_DOMAINS for i in range(new_per_domain)] + [
        _mk(f"tb2-n{i:03d}", "terminal", kind="tb2") for i in range(tb2_new)
    ]
    return old + new


def test_append_keeps_every_previous_assignment_and_places_only_new_tasks() -> None:
    previous = corpus_v2.assign_splits(_fixture(12), seed=PILOT_SEED, method=PILOT)
    tasks = _grown(12, 8, tb2_new=5)
    result = corpus_v2.append_splits(tasks, previous, seed=APPEND_SEED)
    assert {k: result[k] for k in previous} == previous
    for domain in OWN_DOMAINS:
        new = {k: v for k, v in result.items() if k.startswith(f"{domain}-n")}
        assert len(new) == 8
        # 30 % of the 8 new own tasks, rounded half up as _share: 2 holdout, the rest validation
        assert sorted(new.values()) == ["holdout"] * 2 + ["validation"] * 6, domain
    # TB2 tasks never go to holdout; no new task goes to development
    assert all(result[f"tb2-n{i:03d}"] == "validation" for i in range(5))
    assert set(result) == {t.task_id for t in tasks}


def test_append_is_deterministic_seeded_and_per_domain() -> None:
    previous = corpus_v2.assign_splits(_fixture(12), seed=PILOT_SEED, method=PILOT)
    tasks = _grown(12, 8)
    a = corpus_v2.append_splits(tasks, previous, seed=APPEND_SEED)
    assert corpus_v2.append_splits(list(reversed(tasks)), previous, seed=APPEND_SEED) == {
        t.task_id: a[t.task_id] for t in reversed(tasks)
    }
    others = [corpus_v2.append_splits(tasks, previous, seed=s) for s in range(1, 6)]
    assert any(o != a for o in others)
    # new tasks of one domain never change the placement of another domain's new tasks
    more = tasks + [_mk(f"bug-n{i:03d}", "bug") for i in range(8, 12)]
    b = corpus_v2.append_splits(more, previous, seed=APPEND_SEED)
    assert {k: v for k, v in b.items() if not k.startswith("bug-")} == {
        k: v for k, v in a.items() if not k.startswith("bug-")
    }


def test_append_refuses_a_dropped_or_bad_previous_task_and_keeps_the_minimums() -> None:
    previous = corpus_v2.assign_splits(_fixture(12), seed=PILOT_SEED, method=PILOT)
    tasks = _grown(12, 8)
    dropped = [t for t in tasks if t.task_id != "bug-000"]
    with pytest.raises(CorpusError) as e:
        corpus_v2.append_splits(dropped, previous, seed=APPEND_SEED)
    assert e.value.code == "TASK_SPLIT"
    with pytest.raises(CorpusError) as e:
        corpus_v2.append_splits(tasks, {**previous, "bug-000": "test"}, seed=APPEND_SEED)
    assert e.value.code == "TASK_SPLIT"
    tb2 = [*tasks, _mk("tb2-old", "terminal", kind="tb2")]
    with pytest.raises(CorpusError) as e:
        corpus_v2.append_splits(tb2, {**previous, "tb2-old": "holdout"}, seed=APPEND_SEED)
    assert e.value.code == "TASK_SPLIT"
    with pytest.raises(CorpusError) as e:
        corpus_v2.append_splits(_fixture(2), {}, seed=APPEND_SEED)
    assert e.value.code == "SPLIT_TOO_SMALL" and APPEND in str(e.value)
    with pytest.raises(CorpusError) as e:
        corpus_v2.assign_splits(tasks, seed=APPEND_SEED, method=APPEND)
    assert e.value.code == "TASK_SPLIT"  # the append rule needs the previous split


# -- the committed files ---------------------------------------------------------------------------


def test_the_2_0_0_splits_file_is_the_pilot_rule_with_the_fixed_seed() -> None:
    data = json.loads(PREVIOUS.read_text())
    assert data["seed"] == PILOT_SEED and data["method"] == PILOT
    expected = corpus_v2.assign_splits(_v200_tasks(), seed=PILOT_SEED, method=PILOT)
    assert data["assignments"] == expected
    assert list(data["assignments"]) == sorted(data["assignments"])
    assert _totals(expected) == {"holdout": 22, "validation": 25, "development": 24}


def test_the_committed_splits_json_is_the_append_rule_over_2_0_0() -> None:
    data = json.loads((V2_ROOT / "splits.json").read_text())
    manifest = json.loads((V2_ROOT / "manifest.json").read_text())
    assert manifest["version"] == "2.1.0"
    assert data["seed"] == APPEND_SEED == manifest["split_seed"]
    assert data["method"] == APPEND
    previous = _previous()
    # every 2.0.0 assignment is unchanged (§10.2 (2))
    assert {k: data["assignments"][k] for k in previous} == previous
    corpus = corpus_v2.load(V2_ROOT)
    main = [t for t in corpus.tasks if t.set == "main"]
    new = [t for t in main if t.task_id not in previous]
    assert new
    for domain in {t.domain for t in new}:
        mine = [t for t in new if t.domain == domain]
        own = [t for t in mine if t.source["kind"] == "own"]
        splits = [data["assignments"][t.task_id] for t in mine]
        assert splits.count("holdout") == (3 * len(own) + 5) // 10, domain  # 30 %, half up
        assert splits.count("validation") == len(mine) - splits.count("holdout"), domain
    expected = corpus_v2.append_splits(list(corpus.tasks), previous, seed=APPEND_SEED)
    assert data["assignments"] == expected
    assert list(data["assignments"]) == sorted(data["assignments"])
    assert corpus.split_seed == APPEND_SEED and corpus.split_method == APPEND
    assert {t.task_id: t.split for t in main} == expected


def test_write_splits_reproduces_the_committed_file(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path, with_splits=False)
    prev = root / "splits-2.0.0.json"
    result = corpus_v2.write_splits(root, seed=APPEND_SEED, method=APPEND, previous=prev)
    assert (root / "splits.json").read_bytes() == (V2_ROOT / "splits.json").read_bytes()
    assert json.loads((root / "manifest.json").read_text()) == json.loads(
        (V2_ROOT / "manifest.json").read_text()
    )
    assert result["seed"] == APPEND_SEED and result["method"] == APPEND
    assert sum(sum(d.values()) for d in result["counts"].values()) == len(
        json.loads((V2_ROOT / "splits.json").read_text())["assignments"]
    )
    # the same call again is a no-op; another seed or rule is refused (a re-split is a new version)
    assert corpus_v2.write_splits(root, seed=APPEND_SEED, method=APPEND, previous=prev) == result
    with pytest.raises(CorpusError) as e:
        corpus_v2.write_splits(root, seed=APPEND_SEED + 1, method=APPEND, previous=prev)
    assert e.value.code == "TASK_SPLIT"
    for method, previous in ((PILOT, None), (APPEND, None), (PILOT, prev)):
        with pytest.raises(CorpusError) as e:
            corpus_v2.write_splits(root, seed=APPEND_SEED, method=method, previous=previous)
        assert e.value.code in ("TASK_SPLIT", "SPLIT_TOO_SMALL")
    assert (root / "splits.json").read_bytes() == (V2_ROOT / "splits.json").read_bytes()


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_corpus_check_writes_splits(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path, with_splits=False)
    prev = str(root / "splits-2.0.0.json")
    done = _cli(
        "--corpus", str(root), "--write-splits", APPEND, "--seed", str(APPEND_SEED),
        "--previous", prev,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout)
    assert report["method"] == APPEND and report["seed"] == APPEND_SEED
    assert (root / "splits.json").read_bytes() == (V2_ROOT / "splits.json").read_bytes()
    assert _cli("--corpus", str(root), "--write-splits", APPEND).returncode == 2  # no seed
    assert _cli("--write-splits", PILOT, "--seed", "1").returncode == 2  # no corpus
    seed = str(APPEND_SEED)
    assert _cli("--corpus", str(root), "--write-splits", APPEND, "--seed", seed).returncode == 2
    assert (
        _cli(
            "--corpus", str(root), "--write-splits", PILOT, "--seed", seed, "--previous", prev
        ).returncode
        == 2
    )  # fmt: skip


# -- the freeze path -----------------------------------------------------------------------------


@pytest.fixture
def meta_ref(tmp_path: Path) -> Any:
    ref = MetaReference(tmp_path / "meta")
    try:
        yield ref
    finally:
        ref.close()


def test_freeze_reads_the_committed_splits_file(tmp_path: Path, meta_ref: MetaReference) -> None:
    root = _copy_corpus(tmp_path, with_splits=True)
    assignments = json.loads((root / "splits.json").read_text())["assignments"]
    main = corpus_v2.for_set(corpus_v2.load(root), "main")
    d = meta_ref.d
    refs = corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, main, holdout_use_limit=3)
    frozen = d.store.get(d.scope, "eval-corpus", refs["corpus_ref"])
    assert {c["case_id"]: c["split"] for c in frozen["cases"]} == assignments
    index = d.store.get(d.scope, "corpus-task-index", refs["task_index_ref"])
    assert index["splits"]["seed"] == APPEND_SEED and index["splits"]["method"] == APPEND
    tasks = list(main.tasks)
    assert index["splits"]["counts"] == corpus_v2.split_counts(tasks, assignments)
