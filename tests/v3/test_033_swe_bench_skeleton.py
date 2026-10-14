"""Work 033 OD-15 wave 1 (D-136): SWE-bench Multilingual / SWE-Bench Pro adapter skeleton.

Contract: interfaces "Clarification: SWE-bench Adapter Skeleton (2026-10-15)". Synthetic rows in
the two published row shapes (Multilingual: test lists are lists; Pro: JSON-encoded strings). No
network, no dataset, no container."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness import corpus_v2
from amplai_foundry.runtime.errors import Hold, RuntimeFault

REPO = Path(__file__).resolve().parents[2]
SHA = "0123456789abcdef0123456789abcdef01234567"


def _ml(instance_id: str = "jqlang__jq-2681", repo: str = "jqlang/jq", **kw: Any) -> dict:
    row = {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": SHA,
        "patch": "diff --git a/src/x.c b/src/x.c\n",
        "test_patch": "diff --git a/tests/t b/tests/t\n",
        "problem_statement": "jq crashes on empty input.",
        "hints_text": "",
        "created_at": "2023-01-23 04:10:47",
        "version": "1.7",
        "FAIL_TO_PASS": ["tests/shtest"],
        "PASS_TO_PASS": ["tests/jqtest"],
        "image": "swebench/sweb.eval.x86_64.jqlang_1776_jq-2681:latest",
        "log_parser": "parse_log_jq",
        "eval_script": "cd /testbed\n",
        "eval_type": "pass_and_fail",
    }
    row.update(kw)
    return row


def _pro(instance_id: str = "instance_nodebb__nodebb-1", hard: bool = False, **kw: Any) -> dict:
    row = {
        "repo": "NodeBB/NodeBB",
        "instance_id": instance_id,
        "base_commit": SHA,
        "patch": "diff --git a/src/a.js b/src/a.js\n",
        "test_patch": "diff --git a/test/a.js b/test/a.js\n",
        "problem_statement": "Groups lose their owner.",
        "requirements": "",
        "interface": "",
        "repo_language": "js",
        "fail_to_pass": json.dumps(["test/groups.js | Groups owner"]),
        "pass_to_pass": json.dumps([]),
        "issue_specificity": "[]",
        "issue_categories": "[]",
        "before_repo_set_cmd": "git reset --hard",
        "selected_test_files_to_run": "[]",
        "dockerhub_tag": "tag",
        "docker_image": f"ghcr.io/scaleapi/swe-bench_pro-v2:{instance_id}",
        "hard": hard,
        "version": "2.0.0",
    }
    row.update(kw)
    return row


def _jsonl(tmp_path: Path, rows: list[dict], name: str = "rows.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("swe_adapter", REPO / "scripts/swe_adapter.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["swe_adapter"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


swe_bench = _script()


# -- sources -------------------------------------------------------------------------------------


def test_sources_record_the_checked_dataset_facts() -> None:
    ml, v2, hard = (swe_bench.SOURCES[k] for k in ("multilingual", "pro_v2", "pro_hard"))
    assert (ml.dataset, ml.rows, ml.dataset_license, ml.tests_encoding) == (
        "SWE-bench/SWE-bench_Multilingual",
        300,
        "MIT",
        "list",
    )
    assert (v2.dataset, v2.config, v2.rows) == ("ScaleAI/SWE-bench_Pro", "default", 642)
    assert (hard.config, hard.rows) == ("hard", 51)
    # the Pro card has no license field: never filled in by the adapter
    assert v2.dataset_license is None and hard.dataset_license is None
    assert {s.harness_license for s in swe_bench.SOURCES.values()} == {"MIT"}
    with pytest.raises(RuntimeFault):
        swe_bench.source("verified")


def test_the_corpus_contract_has_no_swe_source_yet() -> None:
    # The skeleton writes nothing into the corpus: adding a source kind, a grading and the
    # licenses is an operator decision (interfaces, SWE skeleton clarification).
    assert not any("swe" in k for k in corpus_v2.SOURCE_KINDS)
    assert not any("swe" in g for g in corpus_v2.GRADINGS)


# -- rows ----------------------------------------------------------------------------------------


def test_multilingual_row_reads_list_test_ids() -> None:
    instance = swe_bench.parse_row("multilingual", _ml())
    assert instance.fail_to_pass == ("tests/shtest",)
    assert instance.pass_to_pass == ("tests/jqtest",)
    assert instance.image == "swebench/sweb.eval.x86_64.jqlang_1776_jq-2681:latest"
    assert instance.language is None
    assert instance.extra["log_parser"] == "parse_log_jq"
    assert "FAIL_TO_PASS" not in instance.extra and "patch" not in instance.extra


def test_pro_row_reads_json_encoded_test_ids() -> None:
    instance = swe_bench.parse_row("pro_v2", _pro())
    assert instance.fail_to_pass == ("test/groups.js | Groups owner",)
    assert instance.pass_to_pass == ()
    assert instance.language == "js"
    assert instance.image == "ghcr.io/scaleapi/swe-bench_pro-v2:instance_nodebb__nodebb-1"
    assert instance.extra["hard"] is False


@pytest.mark.parametrize(
    ("key", "row"),
    [
        ("multilingual", _ml(base_commit="abc")),
        ("multilingual", _ml(repo="not a repo")),
        ("multilingual", _ml(problem_statement="")),
        ("multilingual", _ml(FAIL_TO_PASS=[])),
        ("multilingual", _ml(FAIL_TO_PASS=json.dumps(["t"]))),  # Multilingual lists are lists
        ("multilingual", _ml(PASS_TO_PASS=[1])),
        ("pro_v2", _pro(fail_to_pass=["t"])),  # Pro lists are JSON strings
        ("pro_v2", _pro(fail_to_pass="[not json")),
        ("pro_v2", {k: v for k, v in _pro().items() if k != "test_patch"}),
    ],
)
def test_malformed_rows_hold(key: str, row: dict) -> None:
    with pytest.raises(Hold) as caught:
        swe_bench.parse_row(key, row)
    assert caught.value.code == "SWE_SOURCE"


def test_read_rows_reads_jsonl_and_refuses_duplicates(tmp_path: Path) -> None:
    rows = swe_bench.read_rows("multilingual", _jsonl(tmp_path, [_ml(), _ml("jqlang__jq-1")]))
    assert [r.instance_id for r in rows] == ["jqlang__jq-2681", "jqlang__jq-1"]
    with pytest.raises(Hold):
        swe_bench.read_rows("multilingual", _jsonl(tmp_path, [_ml(), _ml()], "dup.jsonl"))
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n")
    with pytest.raises(Hold):
        swe_bench.read_rows("multilingual", bad)


# -- licenses ------------------------------------------------------------------------------------


def test_license_gate_needs_an_explicit_permissive_license_per_repo() -> None:
    rows = [
        swe_bench.parse_row("multilingual", _ml()),
        swe_bench.parse_row("multilingual", _ml("redis__redis-1", "redis/redis")),
        swe_bench.parse_row("multilingual", _ml("x__y-1", "x/y")),
    ]
    decisions = {("jqlang/jq", SHA): "MIT", ("redis/redis", SHA): "AGPL-3.0"}
    passed, held = swe_bench.license_gate(rows, decisions)
    assert [i.instance_id for i in passed] == ["jqlang__jq-2681"]
    assert held == [
        {
            "instance_id": "redis__redis-1",
            "reason": "license_not_permissive",
            "license": "AGPL-3.0",
        },
        {"instance_id": "x__y-1", "reason": "license_unknown"},
    ]
    with pytest.raises(Hold) as caught:
        swe_bench.require_licenses(rows, {("jqlang/jq", SHA): "MIT"})
    assert caught.value.code == "SWE_LICENSE"
    swe_bench.require_licenses(rows[:1], {("jqlang/jq", SHA): "MIT"})


def test_license_decisions_are_per_base_commit_of_one_repo() -> None:
    old, new = "a" * 40, "b" * 40
    rows = [
        swe_bench.parse_row("multilingual", _ml("r__r-1", "r/r", base_commit=old)),
        swe_bench.parse_row("multilingual", _ml("r__r-2", "r/r", base_commit=new)),
    ]
    # the repository relicensed between the two base commits
    passed, held = swe_bench.license_gate(rows, {("r/r", old): "MIT", ("r/r", new): "BUSL-1.1"})
    assert [i.instance_id for i in passed] == ["r__r-1"]
    assert held == [
        {"instance_id": "r__r-2", "reason": "license_not_permissive", "license": "BUSL-1.1"}
    ]
    # a decision for one commit does not carry over to another commit of the same repository
    _, held = swe_bench.license_gate(rows, {("r/r", old): "MIT"})
    assert held == [{"instance_id": "r__r-2", "reason": "license_unknown"}]


def test_license_decisions_file_shape() -> None:
    read = swe_bench.read_license_decisions
    assert read({"r/r": {SHA: "MIT"}}) == {("r/r", SHA): "MIT"}
    for bad in ({"r/r": "MIT"}, {"r/r": {"abc": "MIT"}}, {"r/r": {SHA: ""}}, ["r/r"]):
        with pytest.raises(Hold) as caught:
            read(bad)
        assert caught.value.code == "SWE_LICENSE"


def test_copyleft_pro_repositories_hold() -> None:
    row = swe_bench.parse_row("pro_hard", _pro(hard=True))
    _, held = swe_bench.license_gate([row], {("NodeBB/NodeBB", SHA): "GPL-3.0"})
    assert held[0]["reason"] == "license_not_permissive"


# -- unsaturated selection -----------------------------------------------------------------------


def test_resolution_counts_sum_submissions() -> None:
    counts = swe_bench.resolution_counts(
        [
            {"a": {"resolved": True, "cost": 0.1}, "b": {"resolved": False}},
            {"a": {"resolved": True}, "b": {"resolved": True}},
        ]
    )
    assert counts == {"a": (2, 2), "b": (1, 2)}
    with pytest.raises(Hold):
        swe_bench.resolution_counts([{"a": {"resolved": "yes"}}])


def test_select_unsaturated_uses_the_operator_threshold() -> None:
    rows = [swe_bench.parse_row("multilingual", _ml(f"r__r-{n}", "r/r")) for n in range(4)]
    counts = {"r__r-0": (0, 10), "r__r-1": (2, 10), "r__r-2": (9, 10)}
    selected, dropped = swe_bench.select_unsaturated(rows, counts, max_resolved_share=0.2)
    assert [i.instance_id for i in selected] == ["r__r-0", "r__r-1"]
    assert dropped == [
        {"instance_id": "r__r-2", "reason": "saturated", "resolved": 9, "submissions": 10},
        {"instance_id": "r__r-3", "reason": "no_results"},
    ]
    with pytest.raises(RuntimeFault):
        swe_bench.select_unsaturated(rows, counts, max_resolved_share=1.5)


# -- HARD-51 -------------------------------------------------------------------------------------


def test_hard_subset_matches_the_id_list_and_the_hard_column() -> None:
    rows = [
        swe_bench.parse_row("pro_v2", _pro("i-1", hard=True)),
        swe_bench.parse_row("pro_v2", _pro("i-2", hard=False)),
        swe_bench.parse_row("pro_v2", _pro("i-3", hard=True)),
    ]
    assert [i.instance_id for i in swe_bench.hard_subset(rows, ["i-3", "i-1", ""])] == [
        "i-1",
        "i-3",
    ]
    with pytest.raises(Hold):
        swe_bench.hard_subset(rows, ["i-1"])  # the hard column says i-3 too
    with pytest.raises(Hold):
        swe_bench.hard_subset(rows, ["i-1", "i-3", "i-9"])
    with pytest.raises(Hold):
        swe_bench.hard_subset(rows, ["i-1", "i-1", "i-3"])


# -- script --------------------------------------------------------------------------------------


def test_script_scan_licenses_select_hard(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    script = swe_bench
    rows = _jsonl(tmp_path, [_ml(), _ml("redis__redis-1", "redis/redis")])
    assert script.main(["scan", "--source", "multilingual", "--rows", str(rows)]) == 0
    scanned = json.loads(capsys.readouterr().out)
    assert scanned["rows"] == 2 and scanned["repos"] == {"jqlang/jq": 1, "redis/redis": 1}

    licenses = tmp_path / "licenses.json"
    licenses.write_text(
        json.dumps({"jqlang/jq": {SHA: "MIT"}, "redis/redis": {SHA: "BSD-3-Clause"}})
    )
    argv = ["licenses", "--source", "multilingual", "--rows", str(rows)]
    assert script.main([*argv, "--repo-licenses", str(licenses)]) == 0
    assert json.loads(capsys.readouterr().out)["held"] == []

    results = tmp_path / "details.json"
    results.write_text(
        json.dumps({"jqlang__jq-2681": {"resolved": True}, "redis__redis-1": {"resolved": False}})
    )
    argv = ["select", "--source", "multilingual", "--rows", str(rows), "--results", str(results)]
    assert script.main([*argv, "--max-resolved-share", "0.5"]) == 0
    assert json.loads(capsys.readouterr().out)["selected"] == ["redis__redis-1"]

    pro = _jsonl(tmp_path, [_pro("i-1", hard=True), _pro("i-2")], "pro.jsonl")
    ids = tmp_path / "hard51_ids.txt"
    ids.write_text("i-1\n")
    argv = ["hard", "--source", "pro_v2", "--rows", str(pro), "--ids", str(ids)]
    assert script.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"count": 1, "hard": ["i-1"]}

    assert script.main(["scan", "--source", "pro_v2", "--rows", str(tmp_path / "none")]) == 2
