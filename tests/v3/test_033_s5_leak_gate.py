"""Work 033 S5 (D-098) - leak index build, scan, findings and the proposer ACL.

Contract: specs/033-harness-taxonomy/interfaces.md §2.7, §3.11, §10.4. Corpora are synthetic
(tmp_path); ``freeze`` writes the ``leak-index`` record into a real reference store.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from meta_world import MetaReference

from amplai_foundry.meta_harness import corpus_v2, leak_gate
from amplai_foundry.meta_harness.leak_gate import LeakGate, LeakHit
from amplai_foundry.runtime.errors import Hold, RuntimeFault

SHA = "9e574bcd0270ad8ef6e27aa6f6b757d7c032c06b"

BASE = {
    "app/__init__.py": "",
    "app/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_visible.py": "from app.calc import add\n\n\ndef test_import():\n    assert add\n",
}
# reference-only identifiers: `zqx_carry_fold` and `NovelAccumulator` appear neither in the base
# tree nor in the task text; `add`, `a`, `b` do (base), `total` appears in the task text.
REFERENCE = {
    "app/calc.py": (
        "def add(a, b):\n"
        "    # comment_only_word is a comment, not an identifier\n"
        "    total = NovelAccumulator().zqx_carry_fold(a, b)\n"
        "    return total\n\n\n"
        "class NovelAccumulator:\n"
        "    def zqx_carry_fold(self, a, b):\n"
        "        return a + b\n"
    )
}


def _hidden(i: int) -> str:
    return (
        "from app.calc import add\n\n\n"
        f"def test_secret_case_{i}():\n    assert add({i}, 1) == {i + 1}\n\n\n"
        f"def test_edge_zero_{i}():\n    assert add(0, {i}) == {i}\n"
    )


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _build(root: Path, splits: dict[str, str]) -> corpus_v2.CorpusV2:
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "corpus_id": "leak-v2",
                "version": "1.0.0",
                "split_seed": 1,
                "bases": {"bench": {"dir": "bases/bench", "base_commit": SHA}},
            }
        )
    )
    _write(root / "bases" / "bench", BASE)
    for i, name in enumerate(sorted(splits)):
        meta = {
            "task_id": name,
            "version": 2,
            "domain": "bug",
            "subdomain": None,
            "set": "main",
            "split": None,
            "source": {"kind": "own", "ref": "r", "author": "a", "created": "2026-09-30"},
            "license": "LicenseRef-amplai-internal",
            "base": "bench",
            "environment": "app",
            "grading": "pytest_hidden",
            "objective": "Make add return the running total of both arguments.",
            "acceptance": ["add(1, 1) is 2"],
            "hidden_map": {f"test_secret_case_{i}": 0, f"test_edge_zero_{i}": 0},
            "ambiguity": None,
            "difficulty_declared": None,
        }
        folder = root / "tasks" / name
        folder.mkdir(parents=True)
        (folder / "task.json").write_text(json.dumps(meta))
        _write(folder / "hidden", {f"test_hidden_file_{i}.py": _hidden(i)})
        _write(folder / "reference", REFERENCE)
    corpus = corpus_v2.load(root)
    return replace(corpus, tasks=tuple(replace(t, split=splits[t.task_id]) for t in corpus.tasks))


REF_STATEMENT = (
    "Leak gate: a reference only identifier of a validation or holdout task appears at /content"
)
SPLITS = {
    "bug-a-dev": "development",
    "bug-b-val": "validation",
    "bug-c-hold": "holdout",
}


@pytest.fixture
def corpus(tmp_path: Path) -> corpus_v2.CorpusV2:
    root = tmp_path / "corpus"
    root.mkdir()
    return _build(root, SPLITS)


@pytest.fixture
def meta_ref(tmp_path: Path):
    ref = MetaReference(tmp_path / "meta")
    try:
        yield ref
    finally:
        ref.close()


def _tokens(index: dict[str, Any], kind: str | None = None) -> set[tuple[str, str]]:
    return {(t["token"], t["case_id"]) for t in index["tokens"] if kind in (None, t["kind"])}


# -- build_index ---------------------------------------------------------------------------------


def test_build_index_covers_validation_and_holdout_only(corpus: corpus_v2.CorpusV2) -> None:
    index = leak_gate.build_index(corpus, {t.task_id: t.split for t in corpus.tasks if t.split})
    assert isinstance(index["built_at"], str)
    assert {t["case_id"] for t in index["tokens"]} == {"bug-b-val", "bug-c-hold"}
    assert {t["split"] for t in index["tokens"]} == {"validation", "holdout"}
    assert all(t["kind"] in leak_gate.TOKEN_KINDS for t in index["tokens"])
    # nothing of the development task
    assert not any("dev" in t["case_id"] or t["token"] == "bug-a-dev" for t in index["tokens"])


def test_build_index_splits_argument_decides_not_the_task_field(corpus: corpus_v2.CorpusV2) -> None:
    index = leak_gate.build_index(corpus, {"bug-a-dev": "validation"})
    assert {t["case_id"] for t in index["tokens"]} == {"bug-a-dev"}
    assert leak_gate.build_index(corpus, {})["tokens"] == []


def test_build_index_token_kinds(corpus: corpus_v2.CorpusV2) -> None:
    index = leak_gate.build_index(corpus, {"bug-b-val": "validation"})
    assert ("bug-b-val", "bug-b-val") in _tokens(index, "task_id")
    hidden = _tokens(index, "hidden_test_name")
    # file names (basename) and `def test_...` names of the hidden files
    assert ("test_hidden_file_1.py", "bug-b-val") in hidden
    assert ("test_secret_case_1", "bug-b-val") in hidden
    assert ("test_edge_zero_1", "bug-b-val") in hidden
    ref_only = {token for token, _ in _tokens(index, "reference_only_identifier")}
    assert {"zqx_carry_fold", "NovelAccumulator"} <= ref_only
    # identifiers of the base tree or the task text, keywords, comments and strings are not tokens
    for known in ("add", "a", "b", "return", "def", "class", "total", "comment_only_word"):
        assert known not in ref_only, known


def test_non_keyword_common_words_of_a_reference_are_tokens() -> None:
    # 10.4 names no stop list: src documents that only keywords are filtered, so `self` of a
    # reference class is a token (reported to the spec owner as a false-positive risk).
    assert "self" not in leak_gate.KEYWORDS and "class" in leak_gate.KEYWORDS


def test_task_text_words_are_not_reference_only(tmp_path: Path) -> None:
    root = tmp_path / "c"
    root.mkdir()
    corpus = _build(root, {"bug-b-val": "validation"})
    task = corpus.tasks[0]
    # `zqx_carry_fold` named in the objective is public, so it is no longer reference-only
    stated = replace(task, objective=task.objective + " Use zqx_carry_fold.")
    index = leak_gate.build_index(replace(corpus, tasks=(stated,)), {"bug-b-val": "validation"})
    ref_only = {token for token, _ in _tokens(index, "reference_only_identifier")}
    assert "zqx_carry_fold" not in ref_only and "NovelAccumulator" in ref_only


def test_build_index_covers_hidden_alt_and_reference_alt(tmp_path: Path) -> None:
    root = tmp_path / "c"
    root.mkdir()
    corpus = _build(root, {"bug-b-val": "validation"})
    task = corpus.tasks[0]
    alt = replace(
        task,
        hidden_alt={
            "tests/hidden/bug-b-val/test_alt_file.py": b"def test_alt_only_name():\n    pass\n"
        },
        reference_alt={"app/alt.py": b"def alt_only_identifier():\n    return 1\n"},
    )
    index = leak_gate.build_index(replace(corpus, tasks=(alt,)), {"bug-b-val": "validation"})
    assert ("test_alt_file.py", "bug-b-val") in _tokens(index, "hidden_test_name")
    assert ("test_alt_only_name", "bug-b-val") in _tokens(index, "hidden_test_name")
    assert ("alt_only_identifier", "bug-b-val") in _tokens(index, "reference_only_identifier")


def test_unparseable_reference_python_still_indexes_its_words(tmp_path: Path) -> None:
    root = tmp_path / "c"
    root.mkdir()
    corpus = _build(root, {"bug-b-val": "validation"})
    task = corpus.tasks[0]
    broken = replace(task, reference={"app/broken.py": b"def (:\n  weirdname_x = (\n"})
    index = leak_gate.build_index(replace(corpus, tasks=(broken,)), {"bug-b-val": "validation"})
    assert "weirdname_x" in {t for t, _ in _tokens(index, "reference_only_identifier")}


# -- validate_index ------------------------------------------------------------------------------


def _record(**over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "amplai.leak-index.v1",
        "corpus_ref": {"digest": "x"},
        "built_at": "2026-09-30T00:00:00Z",
        "tokens": [{"token": "t", "kind": "task_id", "case_id": "c", "split": "holdout"}],
    }
    value.update(over)
    return value


def test_validate_index_accepts_a_good_record() -> None:
    leak_gate.validate_index(_record())
    leak_gate.validate_index(_record(tokens=[]))


@pytest.mark.parametrize(
    "over",
    [
        {"schema": "other"},
        {"corpus_ref": "x"},
        {"built_at": 5},
        {"tokens": "x"},
        {"tokens": [{"token": "", "kind": "task_id", "case_id": "c", "split": "holdout"}]},
        {"tokens": [{"token": "t", "kind": "bogus", "case_id": "c", "split": "holdout"}]},
        {"tokens": [{"token": "t", "kind": "task_id", "case_id": "c", "split": "development"}]},
        {"tokens": [{"token": "t", "kind": "task_id", "case_id": "c"}]},
        {"tokens": [{"token": "t", "kind": "task_id", "case_id": "c", "split": "holdout", "x": 1}]},
    ],
)
def test_validate_index_refuses_malformed_records(over: dict[str, Any]) -> None:
    with pytest.raises(RuntimeFault) as e:
        leak_gate.validate_index(_record(**over))
    assert e.value.code == "LEAK_INDEX"


# -- LeakGate ------------------------------------------------------------------------------------


@pytest.fixture
def frozen(corpus: corpus_v2.CorpusV2, meta_ref: MetaReference) -> dict[str, Any]:
    d = meta_ref.d
    refs = corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, corpus, holdout_use_limit=2)
    return refs


@pytest.fixture
def gate(frozen: dict[str, Any], meta_ref: MetaReference) -> LeakGate:
    return LeakGate(meta_ref.reviewer, meta_ref.d.store, frozen["leak_index_ref"])


def test_planted_leaks_are_found_with_json_pointers(gate: LeakGate) -> None:
    proposal = {
        "summary": "harmless change to context assembly",
        "components": [
            {"id": "ctx", "content": "Remember: call zqx_carry_fold(a, b) when adding."},
            {"id": "notes", "content": "see test_secret_case_2 for the edge"},
        ],
    }
    hits = gate.scan(proposal)
    got = {(h.token, h.kind, h.case_id, h.where) for h in hits}
    assert (
        "zqx_carry_fold",
        "reference_only_identifier",
        "bug-b-val",
        "/components/0/content",
    ) in got
    assert (
        "test_secret_case_1",
        "hidden_test_name",
        "bug-c-hold",
        "/components/1/content",
    ) not in got
    # hidden names of bug-b-val carry the index 1 (tasks are numbered in sorted order)
    assert any(h.kind == "hidden_test_name" and h.where == "/components/1/content" for h in hits)
    assert all(isinstance(h, LeakHit) for h in hits)


def test_task_ids_match_case_insensitively_on_word_boundaries(gate: LeakGate) -> None:
    assert [h.case_id for h in gate.scan({"t": "Use BUG-B-VAL as a hint"})] == ["bug-b-val"]
    assert gate.scan({"t": "notbug-b-val"}) == []  # boundary before
    assert gate.scan({"t": "bug-b-valid"}) == []  # boundary after
    assert gate.scan({"t": "bug-b-val_x"}) == []  # underscore is a word char
    assert [h.where for h in gate.scan({"t": "(bug-b-val)"})] == ["/t"]


def test_identifiers_match_case_sensitively(gate: LeakGate) -> None:
    assert gate.scan({"t": "novelaccumulator ZQX_CARRY_FOLD"}) == []
    # both indexed tasks (validation, holdout) carry the identifier: one hit per case
    hits = gate.scan({"t": "NovelAccumulator"})
    assert {(h.kind, h.case_id) for h in hits} == {
        ("reference_only_identifier", "bug-b-val"),
        ("reference_only_identifier", "bug-c-hold"),
    }
    assert gate.scan({"t": "MyNovelAccumulatorThing"}) == []  # word boundaries


def test_development_tokens_and_public_words_do_not_hit(gate: LeakGate) -> None:
    clean = {
        "a": "bug-a-dev is a development task id",
        "b": "def add(a, b): return a + b",
        "c": ["total", "comment_only_word", "return"],
        "d": {"nested": [{"deep": "test_visible.py"}]},
        "n": 5,
        "none": None,
    }
    assert gate.scan(clean) == []
    assert gate.scan("") == [] and gate.scan([]) == [] and gate.scan({}) == []


def test_scan_walks_keys_lists_tuples_and_deep_values(gate: LeakGate) -> None:
    hits = gate.scan({"bug-c-hold": 1, "list": ["x", ("y", "test_edge_zero_1")]})
    wheres = {h.where for h in hits}
    assert "/bug-c-hold" in wheres  # keys are scanned
    assert "/list/1/1" in wheres  # tuple elements
    assert gate.scan({"a/b": "bug-b-val"})[0].where == "/a~1b"  # RFC 6901 escaping
    assert gate.scan({"a~b": "bug-b-val"})[0].where == "/a~0b"
    assert gate.scan("bug-b-val")[0].where == ""  # root string


def test_scan_deduplicates_identical_hits(gate: LeakGate) -> None:
    hits = gate.scan({"t": "bug-b-val bug-b-val BUG-B-VAL"})
    assert len(hits) == len(set(hits)) == 1


def test_hidden_file_names_are_indexed(gate: LeakGate) -> None:
    hits = gate.scan({"t": "test_hidden_file_1.py"})
    assert [(h.kind, h.case_id) for h in hits] == [("hidden_test_name", "bug-b-val")]


def test_findings_are_blocking_leak_gate_and_never_carry_the_token(
    gate: LeakGate, meta_ref: MetaReference, frozen: dict[str, Any]
) -> None:
    proposal = {"content": "zqx_carry_fold and bug-b-val"}
    findings = gate.findings(meta_ref.d.scope, proposal)
    assert len(findings) == 3  # the identifier belongs to two indexed tasks, the id to one
    assert {f["code"] for f in findings} == {"LEAK_GATE"}
    assert {f["severity"] for f in findings} == {"blocking"}
    assert len({f["id"] for f in findings}) == 3 and all(
        f["id"].startswith("leakgate-") for f in findings
    )
    for f in findings:
        assert f["source_refs"] == [frozen["leak_index_ref"]]
        assert "/content" in f["statement"]
        for secret in ("zqx_carry_fold", "bug-b-val", "bug-c-hold"):
            assert secret not in json.dumps(f)
    assert sorted(f["statement"] for f in findings).count(REF_STATEMENT) == 2
    assert {f["statement"] for f in findings} == {
        REF_STATEMENT,
        "Leak gate: a task id of a validation or holdout task appears at /content",
    }


def test_findings_empty_for_a_clean_proposal_and_wrong_scope_refused(
    gate: LeakGate, meta_ref: MetaReference
) -> None:
    assert gate.findings(meta_ref.d.scope, {"content": "improve the retry wording"}) == []
    other = replace(meta_ref.d.scope, project_id="some-other-project")
    with pytest.raises(RuntimeFault) as e:
        gate.findings(other, {"content": "x"})
    assert e.value.code == "SCOPE_MISMATCH"


# -- ACL and index integrity ---------------------------------------------------------------------


def test_leak_index_acl_for_a_proposer_actor(
    frozen: dict[str, Any], meta_ref: MetaReference
) -> None:
    d = meta_ref.d
    for proposer in (
        meta_ref.proposer,
        # even with every read permission a harness.propose identity is refused
        replace(meta_ref.reviewer, permissions=meta_ref.reviewer.permissions | {"harness.propose"}),
    ):
        with pytest.raises(Hold) as e:
            LeakGate(proposer, d.store, frozen["leak_index_ref"])
        assert e.value.code == "LEAK_INDEX_ACL" and e.value.outcome == "hold"


def test_acl_is_checked_before_the_store_is_read(meta_ref: MetaReference) -> None:
    bogus = {"kind": "leak-index", "id": "nope", "revision": 1, "digest": "sha256:0"}
    with pytest.raises(Hold) as e:
        LeakGate(meta_ref.proposer, meta_ref.d.store, bogus)
    assert e.value.code == "LEAK_INDEX_ACL"


def test_a_malformed_stored_index_is_leak_index(
    meta_ref: MetaReference, frozen: dict[str, Any]
) -> None:
    d = meta_ref.d
    bad = {
        "schema": "amplai.leak-index.v1",
        "corpus_ref": {},
        "built_at": "x",
        "tokens": [{"token": 1}],
    }
    with d.store.tx() as db:
        ref = d.store.put(db, d.scope, "leak-index", "leak-bad", 1, bad)
    with pytest.raises(RuntimeFault) as e:
        LeakGate(meta_ref.reviewer, d.store, ref)
    assert e.value.code == "LEAK_INDEX"


def test_freeze_index_is_built_from_the_assigned_splits(
    frozen: dict[str, Any], meta_ref: MetaReference
) -> None:
    d = meta_ref.d
    leak = d.store.get(d.scope, "leak-index", frozen["leak_index_ref"])
    leak_gate.validate_index(leak)
    assert leak["corpus_ref"] == frozen["corpus_ref"]
    assert {t["case_id"] for t in leak["tokens"]} == {"bug-b-val", "bug-c-hold"}
    # the task index (visible to proposers as development rows) holds no leak-index token
    index = d.store.get(d.scope, "corpus-task-index", frozen["task_index_ref"])
    assert "zqx_carry_fold" not in json.dumps(index) and "test_secret_case" not in json.dumps(index)


def test_an_empty_index_scans_clean(meta_ref: MetaReference, tmp_path: Path) -> None:
    root = tmp_path / "c"
    root.mkdir()
    corpus = _build(root, {"bug-a-dev": "development", "bug-b-dev": "development"})
    d = meta_ref.d
    # freeze needs a holdout-free valid corpus; development-only is accepted by the index builder
    refs = corpus_v2.freeze(meta_ref.reviewer, d.store, d.artifacts, corpus, holdout_use_limit=1)
    gate = LeakGate(meta_ref.reviewer, d.store, refs["leak_index_ref"])
    assert gate.scan({"t": "bug-a-dev zqx_carry_fold"}) == []
    assert gate.findings(d.scope, {"t": "anything"}) == []


def test_only_distinctive_reference_names_are_leak_tokens() -> None:
    """§10.4 as refined 2026-10-01: plain words, builtins, stdlib and dunder names are no tokens."""
    source = (
        b"import json\n"
        b"LATEST_STAGE = 3\n"
        b"def parse_version(text):\n"
        b"    result = len(text)\n"
        b"    total = sorted([result])\n"
        b"    return semverKey(total)\n"
        b"def __init__(self):\n"
        b"    pass\n"
    )
    names = leak_gate._reference_identifiers({"reference/app.py": source})
    assert {"LATEST_STAGE", "parse_version", "semverKey"} <= names
    assert not names & {"result", "total", "json", "len", "sorted", "text", "__init__", "self"}
