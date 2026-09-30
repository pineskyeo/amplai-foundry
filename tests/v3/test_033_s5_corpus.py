"""Work 033 S5 (D-098) - corpus v2 loader, splits, graders, ambiguity proof, freeze, W030 import.

Contract: specs/033-harness-taxonomy/interfaces.md §2.7, §3.11, §10. Synthetic corpora are built
in ``tmp_path`` (tiny base app, real pytest grading through ``local_corpus.judge``); no docker,
no network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness import corpus_v2, local_corpus
from amplai_foundry.meta_harness.corpus_v2 import TaskV2
from amplai_foundry.meta_harness.local_corpus import CorpusError
from amplai_foundry.meta_harness.reference import MetaReference
from amplai_foundry.runtime.errors import Hold, RuntimeFault

REPO = Path(__file__).resolve().parents[2]
V2_ROOT = REPO / "specs" / "033-harness-taxonomy" / "corpus"
W030_ROOT = REPO / "specs" / "030-meta-harness-live" / "corpus"
SHA = "9e574bcd0270ad8ef6e27aa6f6b757d7c032c06b"

# -- synthetic corpus ----------------------------------------------------------------------------

BASE = {
    "app/__init__.py": "",
    "app/calc.py": "def add(a, b):\n    return a - b\n",
    "app/scale.py": "def scale(x):\n    return x\n",
    "tests/test_visible.py": "from app.calc import add\n\n\ndef test_import():\n    assert add\n",
}
REF_ADD = {"app/calc.py": "def add(a, b):\n    return a + b\n"}
HIDDEN_ADD = (
    "from app.calc import add\n\n\n"
    "def test_add_positive():\n    assert add(2, 3) == 5\n\n\n"
    "def test_add_negative():\n    assert add(-2, -3) == -5\n"
)
DOUBLE = {"app/scale.py": "def scale(x):\n    return x * 2\n"}
TRIPLE = {"app/scale.py": "def scale(x):\n    return x * 3\n"}
HIDDEN_DOUBLE = (
    "from app.scale import scale\n\n\ndef test_scale_double():\n    assert scale(4) == 8\n"
)
HIDDEN_TRIPLE = (
    "from app.scale import scale\n\n\ndef test_scale_triple():\n    assert scale(4) == 12\n"
)


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _meta(task_id: str, **over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "task_id": task_id,
        "version": 2,
        "domain": "bug",
        "subdomain": None,
        "set": "main",
        "split": None,
        "source": {"kind": "own", "ref": "authored", "author": "test", "created": "2026-09-30"},
        "license": "LicenseRef-amplai-internal",
        "base": "bench",
        "environment": "app",
        "grading": "pytest_hidden",
        "objective": "Make add return the sum of its arguments.",
        "acceptance": ["add(2, 3) is 5", "add(-2, -3) is -5"],
        "hidden_map": {"test_add_positive": 0, "test_add_negative": 1},
        "ambiguity": None,
        "difficulty_declared": None,
    }
    value.update(over)
    return value


def _task_dir(
    root: Path,
    task_id: str,
    meta: dict[str, Any],
    *,
    hidden: dict[str, str] | None = None,
    reference: dict[str, str] | None = None,
    hidden_alt: dict[str, str] | None = None,
    reference_alt: dict[str, str] | None = None,
    parent: str = "tasks",
) -> Path:
    folder = root / parent / task_id
    folder.mkdir(parents=True)
    (folder / "task.json").write_text(json.dumps(meta))
    tests_dir = "tests" if parent == "tb2" else "hidden"
    ref_dir = "solution" if parent == "tb2" else "reference"
    for sub, files in (
        (tests_dir, {"test_add.py": HIDDEN_ADD} if hidden is None else hidden),
        (ref_dir, {name: text for name, text in (reference or REF_ADD).items()}),
        ("hidden_alt", hidden_alt or {}),
        ("reference_alt", reference_alt or {}),
    ):
        if files:
            _write(folder / sub, files)
    return folder


def _manifest(root: Path, **over: Any) -> None:
    value: dict[str, Any] = {
        "corpus_id": "synth-v2",
        "version": "1.0.0",
        "split_seed": 7,
        "bases": {
            "bench": {"dir": "bases/bench", "base_commit": SHA},
            "demo": {"dir": "bases/demo", "base_commit": SHA},
            "tb2": {"dir": "bases/tb2", "commits_file": "bases/tb2.commits.json"},
        },
    }
    value.update(over)
    (root / "manifest.json").write_text(json.dumps(value))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "corpus"
    path.mkdir()
    _manifest(path)
    _write(path / "bases" / "bench", BASE)
    return path


def _ambiguity_ask() -> dict[str, Any]:
    return {
        "expected": "ask",
        "must_mention_any": ["Twice", "triple"],
        "interpretations": ["double the input", "triple the input"],
    }


def _add_ambiguity(root: Path, *, proven: bool = True) -> None:
    """One ``expected: ask`` task and one ``proceed`` control."""
    meta = _meta(
        "amb-01-scale",
        domain="ambiguity",
        grading="planner_questions",
        objective="Scale the input.",
        acceptance=["scale changes the input"],
        hidden_map={
            "test_scale_double": 0,
            ("test_scale_triple" if proven else "test_scale_again"): 0,
        },
        ambiguity=_ambiguity_ask(),
    )
    _task_dir(
        root,
        "amb-01-scale",
        meta,
        hidden={"test_scale.py": HIDDEN_DOUBLE},
        reference=DOUBLE,
        # unproven: the alternative reference also satisfies the first suite
        hidden_alt={
            "test_scale_alt.py": HIDDEN_TRIPLE
            if proven
            else HIDDEN_DOUBLE.replace("test_scale_double", "test_scale_again")
        },
        reference_alt=TRIPLE if proven else DOUBLE,
    )
    control = _meta(
        "amb-02-add",
        domain="ambiguity",
        grading="planner_questions",
        ambiguity={"expected": "proceed", "must_mention_any": [], "interpretations": ["a", "b"]},
    )
    _task_dir(root, "amb-02-add", control)


def _load(root: Path) -> corpus_v2.CorpusV2:
    return corpus_v2.load(root)


def _code(exc: pytest.ExceptionInfo[Any]) -> str:
    return str(exc.value.code)


# -- loader --------------------------------------------------------------------------------------


def test_load_scans_task_directories_without_a_task_list(root: Path) -> None:
    for name in ("bug-01-b", "bug-02-a"):
        _task_dir(root, name, _meta(name))
    corpus = _load(root)
    assert corpus.corpus_id == "synth-v2" and corpus.version == "1.0.0"
    assert [t.task_id for t in corpus.tasks] == ["bug-01-b", "bug-02-a"]  # sorted scan
    assert corpus.split_seed == 7
    assert set(corpus.bases) == {"bench", "demo", "tb2"}
    task = corpus.task("bug-01-b")
    assert task.split is None and task.set == "main" and task.base_id == "bench"
    assert set(task.hidden) == {"tests/hidden/bug-01-b/test_add.py"}
    assert set(task.reference) == {"app/calc.py"}
    assert task.hidden_map == {"test_add_positive": 0, "test_add_negative": 1}
    # a task added later is found with no manifest edit
    _task_dir(root, "bug-03-c", _meta("bug-03-c"))
    assert len(_load(root).tasks) == 3


def test_contract_text_equals_the_work030_contract_text(root: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    task = _load(root).task("bug-01-b")
    old = local_corpus.CorpusTask("x", "small", task.objective, task.acceptance, {}, {})
    assert task.contract_text() == old.contract_text()
    assert task.contract_text().endswith("Acceptance:\n- add(2, 3) is 5\n- add(-2, -3) is -5")


def test_unknown_task_id_is_task_unknown(root: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    with pytest.raises(CorpusError) as e:
        _load(root).task("nope")
    assert e.value.code == "TASK_UNKNOWN"


def test_load_reads_the_tb2_directory(root: Path) -> None:
    meta = _meta(
        "tb2-hello",
        domain="terminal",
        subdomain="scripting",
        source={
            "kind": "tb2",
            "ref": "terminal-bench-2/hello",
            "author": "tb2",
            "created": "2026-09-30",
        },
        license="Apache-2.0",
        base="tb2",
        environment="tb2-hello",
        grading="tb2_tests",
        hidden_map={},
    )
    _task_dir(
        root, "tb2-hello", meta, parent="tb2", hidden={"test_x.py": "def test_x():\n    pass\n"}
    )
    task = _load(root).task("tb2-hello")
    assert task.tb2 == {"dir": "tb2/tb2-hello"} and task.grading == "tb2_tests"
    assert set(task.hidden) == {"tests/test_x.py"} and set(task.reference) == {"app/calc.py"}


def _refused(root: Path, code: str) -> None:
    with pytest.raises(CorpusError) as e:
        _load(root)
    assert e.value.code == code


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"domain": "cooking"}, "TASK_DOMAIN"),
        ({"split": "holdout"}, "TASK_SPLIT"),  # authors never set split
        ({"license": "MIT"}, "TASK_LICENSE"),
        ({"base": "ghost"}, "TASK_BASE"),
        ({"grading": "tb2_tests"}, "TASK_GRADING"),
        ({"grading": "planner_questions"}, "TASK_GRADING"),  # no ambiguity object
        ({"hidden_map": {"test_add_positive": 0}}, "TASK_HIDDEN_MAP"),  # unmapped function
        ({"hidden_map": {"test_add_positive": 0, "test_add_negative": 5}}, "TASK_HIDDEN_MAP"),
        ({"hidden_map": {"test_add_positive": 0, "test_ghost": 1}}, "TASK_HIDDEN_MAP"),
        ({"version": 1}, "TASK_META"),
        ({"difficulty_declared": "hard"}, "TASK_META"),  # difficulty is measured (D-098)
        ({"acceptance": []}, "TASK_META"),
        ({"objective": "  "}, "TASK_META"),
        ({"set": "other"}, "TASK_META"),
        (
            {"source": {"kind": "tb2", "ref": "r", "author": "a", "created": "2026-09-30"}},
            "TASK_META",
        ),
        (
            {"source": {"kind": "own", "ref": "r", "author": "a", "created": "30/09/2026"}},
            "TASK_META",
        ),
        (
            {"ambiguity": {"expected": "ask"}, "grading": "planner_questions"},
            "TASK_AMBIGUITY",
        ),  # ambiguity iff domain ambiguity
    ],
)
def test_load_refuses_invalid_task_json(root: Path, over: dict[str, Any], code: str) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b", **over))
    _refused(root, code)


def test_load_refuses_a_missing_or_extra_task_json_field(root: Path) -> None:
    meta = _meta("bug-01-b")
    del meta["hidden_map"]
    _task_dir(root, "bug-01-b", meta)
    _refused(root, "TASK_META")


def test_load_refuses_task_id_that_differs_from_the_directory(root: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-99-x"))
    _refused(root, "TASK_META")


def test_load_refuses_a_task_directory_without_task_json(root: Path) -> None:
    (root / "tasks" / "empty").mkdir(parents=True)
    _refused(root, "TASK_META")


def test_load_refuses_a_task_without_hidden_tests(root: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"), hidden={})
    _refused(root, "TASK_HIDDEN")


def test_load_refuses_a_duplicate_id_across_tasks_and_tb2(root: Path) -> None:
    _task_dir(root, "same-id", _meta("same-id"))
    meta = _meta(
        "same-id",
        domain="terminal",
        source={"kind": "tb2", "ref": "r", "author": "a", "created": "2026-09-30"},
        license="Apache-2.0",
        base="tb2",
        grading="tb2_tests",
        hidden_map={},
    )
    _task_dir(root, "same-id", meta, parent="tb2", hidden={"t.py": "def test_t():\n    pass\n"})
    _refused(root, "TASK_DUPLICATE")


def test_tb2_task_needs_apache_license_and_tb2_directory(root: Path) -> None:
    src = {"kind": "tb2", "ref": "r", "author": "a", "created": "2026-09-30"}
    _task_dir(
        root,
        "tb2-a",
        _meta(
            "tb2-a",
            source=src,
            license="LicenseRef-amplai-internal",
            base="tb2",
            grading="tb2_tests",
            domain="terminal",
            hidden_map={},
        ),
        parent="tb2",
        hidden={"t.py": "def test_t():\n    pass\n"},
    )
    _refused(root, "TASK_LICENSE")
    shutil.rmtree(root / "tb2")
    # kind tb2 under tasks/ is refused
    _task_dir(
        root,
        "tb2-b",
        _meta(
            "tb2-b",
            source=src,
            license="Apache-2.0",
            base="tb2",
            grading="tb2_tests",
            domain="terminal",
            hidden_map={},
        ),
    )
    _refused(root, "TASK_META")


def test_manifest_problems(root: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    _manifest(root, bases={})
    _refused(root, "TASK_BASE")
    _manifest(root, split_seed="7")
    _refused(root, "TASK_SPLIT")
    (root / "manifest.json").write_text("{not json")
    _refused(root, "TASK_META")
    (root / "manifest.json").write_text(json.dumps({"version": "1"}))
    _refused(root, "TASK_META")


def test_splits_json_is_applied_to_main_tasks(root: Path) -> None:
    for name in ("bug-01-b", "bug-02-a"):
        _task_dir(root, name, _meta(name))
    (root / "splits.json").write_text(
        json.dumps(
            {
                "seed": 7,
                "method": "stratified_by_domain_v1",
                "assignments": {"bug-01-b": "holdout", "bug-02-a": "validation"},
            }
        )
    )
    corpus = _load(root)
    assert {t.task_id: t.split for t in corpus.tasks} == {
        "bug-01-b": "holdout",
        "bug-02-a": "validation",
    }


@pytest.mark.parametrize(
    "splits",
    [
        {"seed": 8, "method": "stratified_by_domain_v1", "assignments": {}},  # seed != manifest
        {"seed": 7, "method": "other", "assignments": {}},
        {"seed": 7, "method": "stratified_by_domain_v1", "assignments": {"ghost": "holdout"}},
        {"seed": 7, "method": "stratified_by_domain_v1", "assignments": {"bug-01-b": "test"}},
        {"seed": 7, "method": "stratified_by_domain_v1"},
    ],
)
def test_bad_splits_json_is_task_split(root: Path, splits: dict[str, Any]) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    (root / "splits.json").write_text(json.dumps(splits))
    _refused(root, "TASK_SPLIT")


def test_holdout_for_a_non_own_task_is_refused_on_load(root: Path) -> None:
    src = {"kind": "tb2", "ref": "r", "author": "a", "created": "2026-09-30"}
    _task_dir(
        root,
        "tb2-a",
        _meta(
            "tb2-a",
            source=src,
            license="Apache-2.0",
            base="tb2",
            grading="tb2_tests",
            domain="terminal",
            hidden_map={},
        ),
        parent="tb2",
        hidden={"t.py": "def test_t():\n    pass\n"},
    )
    (root / "splits.json").write_text(
        json.dumps(
            {"seed": 7, "method": "stratified_by_domain_v1", "assignments": {"tb2-a": "holdout"}}
        )
    )
    _refused(root, "TASK_SPLIT")


# -- ambiguity object ----------------------------------------------------------------------------


def test_ambiguity_tasks_load_with_both_references(root: Path) -> None:
    _add_ambiguity(root)
    corpus = _load(root)
    ask, proceed = corpus.task("amb-01-scale"), corpus.task("amb-02-add")
    assert ask.ambiguity is not None and ask.ambiguity["expected"] == "ask"
    assert ask.reference_alt and ask.hidden_alt
    assert proceed.ambiguity is not None and not proceed.reference_alt


def test_ask_task_without_alternatives_is_task_ambiguity(root: Path) -> None:
    meta = _meta(
        "amb-01-scale",
        domain="ambiguity",
        grading="planner_questions",
        ambiguity=_ambiguity_ask(),
    )
    _task_dir(root, "amb-01-scale", meta)
    _refused(root, "TASK_AMBIGUITY")


def test_proceed_control_with_alternatives_is_task_ambiguity(root: Path) -> None:
    meta = _meta(
        "amb-02-add",
        domain="ambiguity",
        grading="planner_questions",
        ambiguity={"expected": "proceed", "must_mention_any": [], "interpretations": ["a", "b"]},
    )
    _task_dir(root, "amb-02-add", meta, reference_alt=DOUBLE)
    _refused(root, "TASK_AMBIGUITY")


def test_alternatives_on_a_non_ambiguity_task_are_refused(root: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"), reference_alt=DOUBLE)
    _refused(root, "TASK_AMBIGUITY")


@pytest.mark.parametrize(
    "ambiguity",
    [
        {"expected": "maybe", "must_mention_any": ["x"], "interpretations": ["a", "b"]},
        {"expected": "ask", "must_mention_any": ["x"], "interpretations": ["only one"]},
        {"expected": "ask", "must_mention_any": [""], "interpretations": ["a", "b"]},
        {"expected": "ask", "must_mention_any": ["x"], "interpretations": ["a", "b"], "extra": 1},
    ],
)
def test_malformed_ambiguity_object(root: Path, ambiguity: dict[str, Any]) -> None:
    meta = _meta(
        "amb-01-x",
        domain="ambiguity",
        grading="planner_questions",
        ambiguity=ambiguity,
        hidden_map={"test_add_positive": 0, "test_add_negative": 1, "test_scale_triple": 0},
    )
    _task_dir(root, "amb-01-x", meta, reference_alt=DOUBLE, hidden_alt={"t.py": HIDDEN_TRIPLE})
    _refused(root, "TASK_AMBIGUITY")


def test_grading_planner_questions_iff_ambiguity_object(root: Path) -> None:
    meta = _meta(
        "amb-01-x",
        domain="ambiguity",
        grading="pytest_hidden",
        ambiguity={"expected": "proceed", "must_mention_any": [], "interpretations": ["a", "b"]},
    )
    _task_dir(root, "amb-01-x", meta)
    _refused(root, "TASK_GRADING")


# -- assign_splits -------------------------------------------------------------------------------


def _mk(
    task_id: str,
    domain: str,
    *,
    kind: str = "own",
    set_name: str = "main",
    split: str | None = None,
) -> TaskV2:
    return TaskV2(
        task_id,
        domain,
        split,
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


OWN_DOMAINS = ("bug", "feature", "refactor", "cli_ops", "data", "ambiguity")


def _fixture_tasks(per_domain: int, *, tb2: int = 0) -> list[TaskV2]:
    tasks = [_mk(f"{d}-{i:03d}", d) for d in OWN_DOMAINS for i in range(per_domain)]
    tasks += [_mk(f"tb2-{i:03d}", "terminal", kind="tb2") for i in range(tb2)]
    return tasks


def _counts(tasks: list[TaskV2], result: dict[str, str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for t in tasks:
        by = out.setdefault(t.domain, {})
        by[result[t.task_id]] = by.get(result[t.task_id], 0) + 1
    return out


def test_assign_splits_meets_the_minimums_and_stratifies_by_domain() -> None:
    tasks = _fixture_tasks(20)  # 120 own tasks (spec §10.2: validation >= 24 needs own >= ~115)
    result = corpus_v2.assign_splits(tasks, seed=11)
    assert set(result) == {t.task_id for t in tasks}
    assert set(result.values()) == {"development", "validation", "holdout"}
    totals = {s: sum(1 for v in result.values() if v == s) for s in corpus_v2.SPLITS}
    assert totals["holdout"] >= 16 and totals["validation"] >= 24
    for domain, counts in _counts(tasks, result).items():
        # 30 % of 20 own = 6 holdout; 30 % of the remaining 14 = 4 validation (rounding half up)
        assert counts == {"holdout": 6, "validation": 4, "development": 10}, domain


def test_assign_splits_is_deterministic_and_seed_dependent() -> None:
    tasks = _fixture_tasks(20)
    a = corpus_v2.assign_splits(tasks, seed=11)
    assert corpus_v2.assign_splits(list(reversed(tasks)), seed=11) == a  # order independent
    assert corpus_v2.assign_splits(tasks, seed=12) != a


def test_assign_splits_adding_a_domain_does_not_reshuffle_the_others() -> None:
    base = _fixture_tasks(20)
    a = corpus_v2.assign_splits(base, seed=3)
    extra = base + [_mk(f"terminal-x{i}", "terminal") for i in range(10)]
    b = corpus_v2.assign_splits(extra, seed=3)
    assert {k: v for k, v in b.items() if k in a} == a  # per-domain Random (documented in src)


def test_tb2_tasks_are_never_holdout_and_lift_validation() -> None:
    tasks = _fixture_tasks(10, tb2=40)  # 60 own + 40 TB2 reaches validation 24 (§10.2)
    result = corpus_v2.assign_splits(tasks, seed=5)
    assert all(result[t.task_id] != "holdout" for t in tasks if t.source["kind"] == "tb2")
    assert sum(1 for v in result.values() if v == "validation") >= 24
    assert sum(1 for v in result.values() if v == "holdout") >= 16


def test_own_tasks_alone_give_split_too_small() -> None:
    # §10.2 arithmetic: about 60 own tasks give holdout 18 but validation about 12 < 24
    with pytest.raises(CorpusError) as e:
        corpus_v2.assign_splits(_fixture_tasks(10), seed=5)
    assert e.value.code == "SPLIT_TOO_SMALL"
    assert "holdout" in str(e.value) and "validation" in str(e.value)  # counts are reported


def test_split_too_small_on_a_tiny_corpus_and_on_empty() -> None:
    for tasks in (_fixture_tasks(2), []):
        with pytest.raises(CorpusError) as e:
            corpus_v2.assign_splits(tasks, seed=1)
        assert e.value.code == "SPLIT_TOO_SMALL"


def test_assign_splits_leaves_the_regression_set_unassigned() -> None:
    tasks = _fixture_tasks(20) + [
        _mk(f"work030-{i}", "regression", kind="work030", set_name="regression") for i in range(20)
    ]
    result = corpus_v2.assign_splits(tasks, seed=2)
    assert not any(k.startswith("work030-") for k in result)


def test_assign_splits_refuses_duplicate_ids_and_a_non_integer_seed() -> None:
    tasks = _fixture_tasks(20)
    with pytest.raises(CorpusError) as e:
        corpus_v2.assign_splits([*tasks, tasks[0]], seed=1)
    assert e.value.code == "TASK_DUPLICATE"
    for bad in ("1", 1.5, None, True):
        with pytest.raises(CorpusError) as e:
            corpus_v2.assign_splits(tasks, seed=bad)  # type: ignore[arg-type]
        assert e.value.code == "TASK_SPLIT"


# -- graders -------------------------------------------------------------------------------------


def _workspace(tmp_path: Path, overlay: dict[str, str]) -> Path:
    work = tmp_path / "ws"
    _write(work, BASE)
    _write(work, overlay)
    return work


def test_grade_pytest_hidden_fails_on_base_and_passes_on_reference(
    root: Path, tmp_path: Path
) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    task = _load(root).task("bug-01-b")
    base = corpus_v2.grade(task, _workspace(tmp_path / "a", {}))
    assert (base.hidden_passed, base.visible_passed, base.success) == (False, True, False)
    fixed = corpus_v2.grade(task, _workspace(tmp_path / "b", REF_ADD))
    assert fixed.success
    # the hidden tests are removed from the graded workspace
    assert not (tmp_path / "b" / "ws" / "tests" / "hidden").exists()


def test_grade_reports_a_broken_visible_suite(root: Path, tmp_path: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    task = _load(root).task("bug-01-b")
    work = _workspace(
        tmp_path, {**REF_ADD, "tests/test_visible.py": "def test_x():\n    assert 0\n"}
    )
    outcome = corpus_v2.grade(task, work)
    assert outcome.hidden_passed and not outcome.visible_passed and not outcome.success


def test_grade_planner_questions_ask(root: Path, tmp_path: Path) -> None:
    _add_ambiguity(root)
    ask = _load(root).task("amb-01-scale")
    ws = _workspace(tmp_path, {})
    good = corpus_v2.grade(ask, ws, planner_questions=["Do you want it TWICE as large?"])
    assert good.success  # case-insensitive match on must_mention_any; no test suite run
    assert not corpus_v2.grade(ask, ws, planner_questions=["What colour?"]).success
    assert not corpus_v2.grade(ask, ws, planner_questions=[]).success  # did not ask


def test_grade_planner_questions_proceed(root: Path, tmp_path: Path) -> None:
    _add_ambiguity(root)
    control = _load(root).task("amb-02-add")
    assert not corpus_v2.grade(
        control, _workspace(tmp_path / "q", {}), planner_questions=["Why?"]
    ).success
    assert corpus_v2.grade(
        control, _workspace(tmp_path / "ok", REF_ADD), planner_questions=[]
    ).success
    # proceeded without asking but the hidden tests fail
    assert not corpus_v2.grade(
        control, _workspace(tmp_path / "bad", {}), planner_questions=[]
    ).success


def test_grade_planner_questions_needs_the_questions(root: Path, tmp_path: Path) -> None:
    _add_ambiguity(root)
    ask = _load(root).task("amb-01-scale")
    with pytest.raises(CorpusError) as e:
        corpus_v2.grade(ask, _workspace(tmp_path, {}))
    assert e.value.code == "TASK_GRADING"


def test_grade_tb2_tests_runs_in_the_task_image_not_here(root: Path, tmp_path: Path) -> None:
    meta = _meta(
        "tb2-a",
        domain="terminal",
        source={"kind": "tb2", "ref": "r", "author": "a", "created": "2026-09-30"},
        license="Apache-2.0",
        base="tb2",
        grading="tb2_tests",
        hidden_map={},
    )
    _task_dir(root, "tb2-a", meta, parent="tb2", hidden={"t.py": "def test_t():\n    pass\n"})
    with pytest.raises(CorpusError) as e:
        corpus_v2.grade(_load(root).task("tb2-a"), tmp_path)
    assert e.value.code == "TASK_GRADING"


# -- fairness and ambiguity proof ----------------------------------------------------------------


def test_validate_fair_task_and_ambiguity_proof(root: Path, tmp_path: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    _add_ambiguity(root)
    corpus = _load(root)
    rows = corpus_v2.validate(corpus, tmp_path / "scratch", repeats=2)
    assert rows["bug-01-b"]["fair"] is True and rows["bug-01-b"]["flaky"] is False
    assert rows["bug-01-b"]["base"] == {"hidden_passed": False, "visible_passed": True}
    assert rows["bug-01-b"]["repeats"] == 2
    ask = rows["amb-01-scale"]
    assert ask["fair"] is True and ask["ambiguity"]["proven"] is True
    assert ask["ambiguity"]["reference_on_hidden_alt"]["hidden_passed"] is False
    assert ask["ambiguity"]["reference_alt_on_hidden"]["hidden_passed"] is False
    assert "ambiguity" not in rows["amb-02-add"] or rows["amb-02-add"]["fair"] is not None


def test_validate_unproven_ambiguity_is_unfair(root: Path, tmp_path: Path) -> None:
    _add_ambiguity(root, proven=False)
    rows = corpus_v2.validate(_load(root), tmp_path / "scratch")
    ask = rows["amb-01-scale"]
    assert ask["ambiguity"]["proven"] is False and ask["fair"] is False


def test_validate_unfair_task_when_reference_does_not_fix_the_base(
    root: Path, tmp_path: Path
) -> None:
    _task_dir(
        root,
        "bug-01-b",
        _meta("bug-01-b"),
        reference={"app/calc.py": "def add(a, b):\n    return 0\n"},
    )
    row = corpus_v2.validate(_load(root), tmp_path / "scratch")["bug-01-b"]
    assert row["fair"] is False and row["reference"]["hidden_passed"] is False


def test_validate_task_that_passes_on_the_base_is_unfair(root: Path, tmp_path: Path) -> None:
    _task_dir(
        root,
        "bug-01-b",
        _meta("bug-01-b", hidden_map={"test_true": 0}),
        hidden={"test_t.py": "def test_true():\n    assert True\n"},
    )
    row = corpus_v2.validate(_load(root), tmp_path / "scratch")["bug-01-b"]
    assert row["base"]["hidden_passed"] is True and row["fair"] is False


def test_validate_skips_tb2_and_refuses_bad_arguments(root: Path, tmp_path: Path) -> None:
    meta = _meta(
        "tb2-a",
        domain="terminal",
        source={"kind": "tb2", "ref": "r", "author": "a", "created": "2026-09-30"},
        license="Apache-2.0",
        base="tb2",
        grading="tb2_tests",
        hidden_map={},
    )
    _task_dir(root, "tb2-a", meta, parent="tb2", hidden={"t.py": "def test_t():\n    pass\n"})
    corpus = _load(root)
    row = corpus_v2.validate(corpus, tmp_path / "s")["tb2-a"]
    assert row["fair"] is None and row["flaky"] is False
    with pytest.raises(CorpusError) as e:
        corpus_v2.validate(corpus, tmp_path / "s", repeats=0)
    assert e.value.code == "REPEATS"


def test_validate_missing_base_tree_is_task_base(root: Path, tmp_path: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    shutil.rmtree(root / "bases" / "bench")
    with pytest.raises(CorpusError) as e:
        corpus_v2.validate(_load(root), tmp_path / "s")
    assert e.value.code == "TASK_BASE"


# -- base commit, for_set, freeze ----------------------------------------------------------------


def test_base_commit_sources(root: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    corpus = _load(root)
    task = corpus.task("bug-01-b")
    assert corpus_v2.base_commit(corpus, task) == SHA  # manifest base_commit
    (root / "bases" / "bench.commit").write_text(SHA[::-1] + "\n")
    file_based = replace(
        corpus,
        bases={
            **corpus.bases,
            "bench": {"dir": "bases/bench", "commit_file": "bases/bench.commit"},
        },
    )
    assert corpus_v2.base_commit(file_based, task) == SHA[::-1]
    tb2_task = replace(task, base_id="tb2")
    with pytest.raises(CorpusError) as e:  # commits_file missing
        corpus_v2.base_commit(corpus, tb2_task)
    assert e.value.code == "TASK_BASE"
    (root / "bases" / "tb2.commits.json").write_text(json.dumps({"bug-01-b": SHA}))
    assert corpus_v2.base_commit(corpus, tb2_task) == SHA
    (root / "bases" / "tb2.commits.json").write_text(json.dumps({"bug-01-b": "zz"}))
    with pytest.raises(CorpusError) as e:
        corpus_v2.base_commit(corpus, tb2_task)
    assert e.value.code == "TASK_BASE"


def test_for_set_and_the_regression_corpus_id(root: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    corpus = _load(root)
    assert [t.task_id for t in corpus_v2.for_set(corpus, "main").tasks] == ["bug-01-b"]
    reg = corpus_v2.for_set(corpus, "regression")
    assert reg.corpus_id == "amplai-regression-v1" and reg.tasks == ()
    with pytest.raises(CorpusError) as e:
        corpus_v2.for_set(corpus, "weird")
    assert e.value.code == "TASK_META"


@pytest.fixture
def meta_ref(tmp_path: Path):
    ref = MetaReference(tmp_path / "meta")
    try:
        yield ref
    finally:
        ref.close()


def _frozen_corpus(root: Path, n: int = 3) -> corpus_v2.CorpusV2:
    splits = ["development", "validation", "holdout"]
    for i in range(n):
        name = f"bug-{i:02d}-x"
        # distinct hidden bytes per task (CorpusService refuses identical case artifacts)
        hidden = HIDDEN_ADD + f"\n\ndef test_extra_{i}():\n    assert add(1, 1) == 2\n"
        meta = _meta(
            name, hidden_map={"test_add_positive": 0, "test_add_negative": 1, f"test_extra_{i}": 0}
        )
        _task_dir(root, name, meta, hidden={"test_add.py": hidden})
    corpus = _load(root)
    return replace(
        corpus,
        tasks=tuple(replace(t, split=splits[i % 3]) for i, t in enumerate(corpus.tasks)),
    )


def test_freeze_writes_corpus_task_index_and_leak_index(
    root: Path, meta_ref: MetaReference
) -> None:
    corpus = _frozen_corpus(root)
    d = meta_ref.d
    refs = corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, corpus, holdout_use_limit=3)
    assert set(refs) == {"corpus_ref", "task_index_ref", "leak_index_ref"}
    frozen = d.store.get(d.scope, "eval-corpus", refs["corpus_ref"])
    assert frozen["corpus_id"] == "synth-v2-1.0.0"
    assert {c["case_id"]: c["split"] for c in frozen["cases"]} == {
        "bug-00-x": "development",
        "bug-01-x": "validation",
        "bug-02-x": "holdout",
    }
    assert {c["task_class"] for c in frozen["cases"]} == {"bug"}  # task_class = domain
    index = d.store.get(d.scope, "corpus-task-index", refs["task_index_ref"])
    assert index["schema"] == "amplai.corpus-task-index.v1"
    assert index["splits"]["seed"] == 7 and index["splits"]["method"] == "stratified_by_domain_v1"
    assert index["splits"]["counts"] == {
        "development": {"bug": 1},
        "validation": {"bug": 1},
        "holdout": {"bug": 1},
    }
    row = index["tasks"][0]
    assert row["reference_diff"] == {"files": 1, "lines": 2}  # one line replaced: -1 / +1
    assert row["acceptance_count"] == 2 and row["grading"] == "pytest_hidden"
    leak = d.store.get(d.scope, "leak-index", refs["leak_index_ref"])
    assert leak["schema"] == "amplai.leak-index.v1"
    assert {t["case_id"] for t in leak["tokens"]} == {"bug-01-x", "bug-02-x"}  # not development


def test_a_new_version_is_the_next_revision_of_the_index_ids(
    root: Path, meta_ref: MetaReference
) -> None:
    # §2.7 ids taskindex-<corpus_id> / leak-<corpus_id>; §2.0 a new version is a new revision.
    # The eval-corpus id keeps the version (CorpusService.freeze writes revision 1).
    corpus = _frozen_corpus(root)
    d = meta_ref.d
    first = corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, corpus, holdout_use_limit=3)
    second = corpus_v2.freeze(
        meta_ref.reviewer,
        d.store,
        d.artifacts,
        replace(corpus, version="1.1.0"),
        holdout_use_limit=3,
    )
    assert (first["corpus_ref"]["id"], first["corpus_ref"]["revision"]) == ("synth-v2-1.0.0", 1)
    assert (second["corpus_ref"]["id"], second["corpus_ref"]["revision"]) == ("synth-v2-1.1.0", 1)
    for key, object_id in (
        ("task_index_ref", "taskindex-synth-v2"),
        ("leak_index_ref", "leak-synth-v2"),
    ):
        assert (first[key]["id"], first[key]["revision"]) == (object_id, 1)
        assert (second[key]["id"], second[key]["revision"]) == (object_id, 2)
    index = d.store.get(d.scope, "corpus-task-index", second["task_index_ref"])
    assert index["corpus_version"] == "1.1.0" and index["corpus_ref"] == second["corpus_ref"]
    leak = d.store.get(d.scope, "leak-index", second["leak_index_ref"])
    assert leak["corpus_ref"] == second["corpus_ref"]
    # the first revision is kept (nothing is overwritten)
    assert d.store.get(d.scope, "corpus-task-index", first["task_index_ref"])["corpus_version"] == (
        "1.0.0"
    )


def test_case_payload_holds_a_digest_never_the_hidden_files(root: Path) -> None:
    corpus = _frozen_corpus(root)
    task = corpus.tasks[1]
    payload = corpus_v2.case_payload(corpus, task)
    assert payload["hidden_digest"] == corpus_v2.hidden_digest(task)
    assert payload["base_commit"] == SHA and payload["contract_text"] == task.contract_text()
    assert "test_add_positive" not in json.dumps(payload)


def test_index_for_a_proposer_sees_development_rows_only(
    root: Path, meta_ref: MetaReference
) -> None:
    corpus = _frozen_corpus(root)
    d = meta_ref.d
    refs = corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, corpus, holdout_use_limit=3)
    all_rows = corpus_v2.index_for(meta_ref.reviewer, d.store, refs["task_index_ref"])
    assert {r["split"] for r in all_rows} == {"development", "validation", "holdout"}
    proposer = replace(meta_ref.proposer, permissions=frozenset({"harness.propose", "corpus.read"}))
    rows = corpus_v2.index_for(proposer, d.store, refs["task_index_ref"])
    assert [r["split"] for r in rows] == ["development"]
    with pytest.raises(RuntimeFault) as e:  # corpus.read is required
        corpus_v2.index_for(meta_ref.proposer, d.store, refs["task_index_ref"])
    assert e.value.code == "FORBIDDEN"


def test_freeze_refusals(root: Path, meta_ref: MetaReference) -> None:
    corpus = _frozen_corpus(root)
    d = meta_ref.d
    # a proposer holding corpus.manage still cannot freeze (CorpusService rule, corpus.py:35-40)
    both = replace(
        meta_ref.reviewer, permissions=meta_ref.reviewer.permissions | {"harness.propose"}
    )
    with pytest.raises(Hold) as h:
        corpus_v2.freeze(both, d.store, d.artifacts, corpus, holdout_use_limit=1)
    assert h.value.code == "CORPUS_PROPOSER"
    with pytest.raises(RuntimeFault) as f:
        corpus_v2.freeze(meta_ref.proposer, d.store, d.artifacts, corpus, holdout_use_limit=1)
    assert f.value.code == "FORBIDDEN"
    # a task without a split
    unsplit = replace(corpus, tasks=(replace(corpus.tasks[0], split=None), *corpus.tasks[1:]))
    with pytest.raises(CorpusError) as e:
        corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, unsplit, holdout_use_limit=1)
    assert e.value.code == "TASK_SPLIT"
    # the main corpus needs the manifest split_seed
    seedless = replace(corpus, split_seed=None)
    with pytest.raises(CorpusError) as e:
        corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, seedless, holdout_use_limit=1)
    assert e.value.code == "TASK_SPLIT"
    # mixed sets are refused: one set per corpus
    mixed = replace(
        corpus,
        tasks=(
            *corpus.tasks,
            replace(corpus.tasks[0], task_id="reg-x", set="regression", split="validation"),
        ),
    )
    with pytest.raises(CorpusError) as e:
        corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, mixed, holdout_use_limit=1)
    assert e.value.code == "TASK_SPLIT"
    # holdout only for own tasks
    tb2_hold = replace(corpus.tasks[2], source={**corpus.tasks[2].source, "kind": "tb2"})
    bad = replace(corpus, tasks=(*corpus.tasks[:2], tb2_hold))
    with pytest.raises(CorpusError) as e:
        corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, bad, holdout_use_limit=1)
    assert e.value.code == "TASK_SPLIT"


def test_regression_set_freezes_as_its_own_corpus_all_validation(
    root: Path, meta_ref: MetaReference
) -> None:
    corpus = _frozen_corpus(root)
    regression_tasks = tuple(
        replace(t, set="regression", split="validation", domain="regression") for t in corpus.tasks
    )
    reg = replace(corpus, corpus_id=corpus_v2.REGRESSION_CORPUS_ID, tasks=regression_tasks)
    d = meta_ref.d
    refs = corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, reg, holdout_use_limit=1)
    index = d.store.get(d.scope, "corpus-task-index", refs["task_index_ref"])
    assert index["splits"]["counts"] == {"validation": {"regression": 3}}
    # a regression set under the main corpus id is refused
    with pytest.raises(CorpusError) as e:
        corpus_v2.freeze(
            meta_ref.reviewer,
            d.store,
            d.artifacts,
            replace(reg, corpus_id="synth-v2"),
            holdout_use_limit=1,
        )
    assert e.value.code == "TASK_SPLIT"


def test_validate_task_index_shape() -> None:
    good = {
        "schema": "amplai.corpus-task-index.v1",
        "corpus_ref": {"k": "v"},
        "tasks": [{"split": "development", "domain": "bug"}],
        "splits": {"seed": 1, "method": "m", "counts": {}},
    }
    corpus_v2.validate_task_index(good)
    for bad in (
        {**good, "schema": "x"},
        {**good, "tasks": [{"split": "test", "domain": "bug"}]},
        {**good, "tasks": [{"split": "holdout", "domain": "cooking"}]},
        {**good, "splits": {"seed": 1}},
        {k: v for k, v in good.items() if k != "corpus_ref"},
    ):
        with pytest.raises(RuntimeFault) as e:
            corpus_v2.validate_task_index(bad)
        assert e.value.code == "TASK_INDEX"


def test_hidden_test_functions_finds_test_defs_in_python_files_only() -> None:
    files = {
        "a/test_a.py": (
            b"def test_one():\n    pass\n\nasync def test_two():\n    pass\n"
            b"\ndef helper():\n    pass\n"
        ),
        "a/notes.txt": b"def test_not_python():\n",
    }
    assert corpus_v2.hidden_test_functions(files) == {"test_one", "test_two"}


# -- Work 030 regression import ------------------------------------------------------------------


@pytest.fixture(scope="module")
def w030() -> local_corpus.Corpus:
    return local_corpus.load(W030_ROOT)


def test_import_work030_creates_twenty_regression_tasks(
    tmp_path: Path, w030: local_corpus.Corpus
) -> None:
    dest = tmp_path / "v2"
    dest.mkdir()
    ids = corpus_v2.import_work030(W030_ROOT, dest)
    assert ids == [f"work030-{t.task_id}" for t in w030.tasks] and len(ids) == 20
    _manifest(dest, split_seed=None)
    (dest / "manifest.json").write_text(
        json.dumps(
            {
                "corpus_id": "imp",
                "version": "1",
                "split_seed": None,
                "bases": {"demo": {"dir": "bases/demo", "base_commit": SHA}},
            }
        )
    )
    corpus = _load(dest)
    assert len(corpus.tasks) == 20
    for task, old in zip(
        corpus.tasks, sorted(w030.tasks, key=lambda t: f"work030-{t.task_id}"), strict=True
    ):
        assert task.task_id == f"work030-{old.task_id}"
        assert (task.set, task.domain, task.base_id, task.split) == (
            "regression",
            "regression",
            "demo",
            "validation",
        )
        assert task.source["kind"] == "work030" and task.license == "LicenseRef-amplai-internal"
        assert task.grading == "pytest_hidden" and task.hidden_map == {}
        assert task.contract_text() == old.contract_text()  # what the agent is told is unchanged
        assert set(task.reference) == set(old.reference)
        # hidden files move to tests/hidden/<new id>/ (grading runs that directory)
        assert set(task.hidden) == {
            n.replace(f"tests/hidden/{old.task_id}/", f"tests/hidden/{task.task_id}/")
            for n in old.hidden
        }
    assert (dest / "bases" / "demo" / "demo_app" / "version_report.py").is_file()


def test_import_work030_refuses_an_existing_task_and_a_differing_base(tmp_path: Path) -> None:
    dest = tmp_path / "v2"
    dest.mkdir()
    corpus_v2.import_work030(W030_ROOT, dest)
    with pytest.raises(CorpusError) as e:
        corpus_v2.import_work030(W030_ROOT, dest)
    assert e.value.code == "TASK_DUPLICATE"
    shutil.rmtree(dest / "tasks")
    (dest / "bases" / "demo" / "demo_app" / "version_report.py").write_text("# changed\n")
    with pytest.raises(CorpusError) as e:
        corpus_v2.import_work030(W030_ROOT, dest)
    assert e.value.code == "TASK_BASE"


def test_the_committed_v2_corpus_holds_the_twenty_regression_tasks(
    w030: local_corpus.Corpus,
) -> None:
    corpus = _load(V2_ROOT)
    regression = [t for t in corpus.tasks if t.task_id.startswith("work030-")]
    assert sorted(t.task_id for t in regression) == sorted(
        f"work030-{t.task_id}" for t in w030.tasks
    )
    assert all(t.set == "regression" and t.split == "validation" for t in regression)
    assert corpus.corpus_id == "amplai-bench-v2" and corpus.bases["demo"]["base_commit"] == SHA
    # the demo base is byte-identical to the Work 030 base
    assert corpus_v2._files(V2_ROOT / "bases" / "demo") == corpus_v2._files(W030_ROOT / "base")
    assert json.loads((W030_ROOT / "manifest.json").read_text())["base_commit"] == SHA


def test_the_committed_regression_tasks_are_fair(tmp_path: Path) -> None:
    corpus = corpus_v2.for_set(_load(V2_ROOT), "regression")
    only = ("work030-s01-semver-parse", "work030-m08-summarize-missing")
    picked = replace(corpus, tasks=tuple(t for t in corpus.tasks if t.task_id in only))
    assert len(picked.tasks) == 2
    rows = corpus_v2.validate(picked, tmp_path)
    assert {k: v["fair"] for k, v in rows.items()} == {k: True for k in only}


# -- scripts/corpus_check.py ---------------------------------------------------------------------


def _check(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "corpus_check.py"), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def test_corpus_check_keeps_working_for_the_work030_corpus() -> None:
    done = _check("s01-semver-parse", "s02-semver-compare")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "2/2 fair" in done.stdout and "ok   s01-semver-parse" in done.stdout


def test_corpus_check_work030_options() -> None:
    assert _check("--repeats", "2", "s03-stage-rank").returncode == 0
    assert _check("--repeats", "0").returncode == 2
    assert _check("--domain", "bug").returncode == 2  # Work 030 corpus has no domains


def test_corpus_check_v2_mode(root: Path) -> None:
    _task_dir(root, "bug-01-b", _meta("bug-01-b"))
    _add_ambiguity(root)
    done = _check("--corpus", str(root), "--repeats", "2")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "3/3 fair" in done.stdout and "ambiguity proof" in done.stdout
    only = _check("--corpus", str(root), "--domain", "bug")
    assert "1/1 fair" in only.stdout and "amb-01-scale" not in only.stdout
    assert _check("--corpus", str(root), "no-such-task").returncode == 2


def test_corpus_check_v2_reports_an_unfair_task(root: Path) -> None:
    _task_dir(
        root,
        "bug-01-b",
        _meta("bug-01-b"),
        reference={"app/calc.py": "def add(a, b):\n    return 0\n"},
    )
    done = _check("--corpus", str(root))
    assert done.returncode == 1 and "FAIL " in done.stdout


def test_corpus_check_v2_on_the_committed_corpus_subset() -> None:
    done = _check("--corpus", str(V2_ROOT), "work030-s01-semver-parse")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1/1 fair" in done.stdout
