"""V3-061 — Final V3 conformance and requirement evidence closure (design/21, design/22)."""

from __future__ import annotations

import json
from pathlib import Path

from amplai_foundry.distribution.closure import ClosureLedger, catalog_mentions, executed_cases

REPO = Path(__file__).resolve().parents[2]
JUNIT = REPO / "specs" / "013-amplai-v3" / "junit" / "rc01-v3-e2e.xml"
LEDGER = REPO / "release" / "rc01-conformance-closure.json"


def test_ledger_ties_ids_only_to_executed_cases_in_function_bodies(tmp_path):
    (tmp_path / "v3").mkdir()
    (tmp_path / "v3" / "test_x.py").write_text(
        '"""prose mentions T-001 and T-002 in the docstring."""\n\n'
        "def test_a():\n    # covers T-001\n    assert True\n\n"
        "def test_b():\n    assert True\n"
    )
    mentions = catalog_mentions([tmp_path])
    assert mentions == {"T-001": [("v3.test_x", "test_a")]}, "docstring ids attribute nothing"
    junit = tmp_path / "j.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase classname="v3.test_x" name="test_a" time="0.1"/>'
        '<testcase classname="v3.test_x" name="test_b"><failure/></testcase>'
        '<testcase classname="v3.test_x" name="test_c"><skipped/></testcase>'
        "</testsuite></testsuites>"
    )
    cases = executed_cases([junit])
    assert cases["v3.test_x::test_a"]["outcome"] == "pass"
    assert cases["v3.test_x::test_b"]["outcome"] == "fail"
    assert cases["v3.test_x::test_c"]["outcome"] == "skipped"


def test_design_check_is_never_a_runtime_pass_and_status_is_rc_not_final():
    ledger = json.loads(LEDGER.read_text())
    # FINAL only through a committed human approval bound to the gate package (V3-062);
    # the shipped status must equal what the committed tree yields, never a hand edit.
    declared = ClosureLedger(REPO).final_declaration()
    expected = "final_declared" if declared else "rc_candidate_not_final"
    assert ledger["release_status"] == expected
    assert ledger["final_declaration"] == declared
    assert ledger["summary"]["tests"]["fail"] == 0
    # Every local_pass must come from an executed junit case, never from a design mention.
    # (This used to assert not_run > 0; all 112 ids now have executed evidence, so the
    # invariant is stated directly instead of through that proxy.)
    executed = executed_cases([JUNIT])
    assert executed, "the shipped junit really ran"
    for tid, entry in ledger["tests"].items():
        if entry["status"] == "local_pass":
            assert all(case in executed for case in entry["executed_cases"]), tid
    assert ledger["summary"]["tests"]["not_run"] == len(
        ledger["remaining_blockers"]["tests_not_run"]
    )
    for tid, entry in ledger["tests"].items():
        if entry["status"] == "local_pass":
            assert entry["executed_cases"], tid
            assert "not live" in entry["qualification"]
        else:
            assert entry["executed_cases"] == [] or entry["status"] == "fail"
    for req in ledger["requirements"]:
        if req["status"] == "fully_verified":
            assert req["tests_local_pass"] == req["tests_total"] > 0
        assert req["design_status"] == "designed_not_implemented", (
            "design status is carried, not upgraded"
        )
    status = json.loads(
        (
            REPO / "specs" / "015-external-qualification" / "external-qualification-status.json"
        ).read_text()
    )
    # R801: the status file must name exactly the DEV-03 items, not merely nine of something.
    dev03 = json.loads(
        (REPO / "specs" / "013-amplai-v3" / "dev03-delivery-evidence.json").read_text()
    )["external_qualification_pending"]
    assert {x["item"] for x in status["items"]} == set(dev03)
    assert len(status["items"]) == 9, "every DEV-03 external item is accounted for"
    # R702/R802: evidence must support the claimed status; checked before the ledger
    # comparison so a stale ledger cannot shadow these assertions.
    for x in status["items"]:
        evidence_path = REPO / x["evidence"]
        assert evidence_path.is_file(), x["item"]
        ev = json.loads(evidence_path.read_text())
        if x["status"] == "resolved":
            assert ev.get("outcome") in {"pass", "pass_with_known_failures"}, x["item"]
        if evidence_path.name == "unavailable.json":
            assert x["status"] in {"unavailable", "not_a_local_check"}, x["item"]
            assert any(e["item"] == x["item"] for e in ev["items"]), x["item"]
        if x["status"] == "partial":
            assert evidence_path.name != "unavailable.json", x["item"]
    unresolved = [x for x in status["items"] if x["status"] != "resolved"]
    assert len(ledger["remaining_blockers"]["external_qualification_pending"]) == len(unresolved)


def test_shipped_ledger_matches_current_tree_and_junit():
    ext = json.loads(
        (REPO / "specs" / "013-amplai-v3" / "dev03-delivery-evidence.json").read_text()
    )["external_qualification_pending"]
    closure = ClosureLedger(REPO)
    fresh = closure.build(
        [JUNIT], external_pending=ext, final_declaration=closure.final_declaration()
    )
    shipped = json.loads(LEDGER.read_text())
    assert fresh["summary"] == shipped["summary"]
    assert fresh["release_status"] == shipped["release_status"]
    assert fresh["final_declaration"] == shipped["final_declaration"]
    assert fresh["executed_case_count"] == shipped["executed_case_count"]
    assert {k: v["status"] for k, v in fresh["tests"].items()} == {
        k: v["status"] for k, v in shipped["tests"].items()
    }
    status_view = json.loads((REPO / "eval" / "test-catalog-status.json").read_text())
    assert {k: v["status"] for k, v in status_view["tests"].items()} == {
        k: v["status"] for k, v in shipped["tests"].items()
    }


def test_every_catalog_requirement_and_task_id_is_present():
    ledger = json.loads(LEDGER.read_text())
    assert (
        len(ledger["tests"]) == 112
        and len(ledger["requirements"]) == 35
        and len(ledger["tasks"]) == 62
    )
    counts = ledger["summary"]
    assert (
        counts["tests"]["local_pass"] + counts["tests"]["not_run"] + counts["tests"]["fail"] == 112
    )
    assert (
        counts["requirements"]["fully_verified"]
        + counts["requirements"]["partial"]
        + counts["requirements"]["not_run"]
        == 35
    )


def _git(cwd, *args):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_final_declaration_counts_only_a_committed_approval_bound_to_the_package(tmp_path):
    import hashlib

    repo = tmp_path / "repo"
    (repo / ".ai-team" / "policy").mkdir(parents=True)
    (repo / "specs" / "015-external-qualification").mkdir(parents=True)
    package = repo / "specs" / "015-external-qualification" / "human-gate-package.md"
    package.write_text("# gate\n")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    approvals = repo / ".ai-team" / "policy" / "approvals.jsonl"
    approvals.write_text("")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "base")
    closure = ClosureLedger(repo, design_dir=str(REPO / "design-reference"))
    assert closure.final_declaration() is None
    line = json.dumps(
        {
            "gate": "production_operation",
            "proposal_id": "V3-062",
            "approved_by": "human",
            "approved_at": "2026-09-21T00:00:00Z",
            "proposal_sha256": digest,
        }
    )
    approvals.write_text(line + "\n")
    assert closure.final_declaration() is None  # working tree only: not an approval
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qam", "approve")
    declared = closure.final_declaration()
    assert declared is not None and declared["approved_by"] == "human"
    # the binding is to the committed package bytes: a changed package voids it
    package.write_text("# gate v2\n")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qam", "edit")
    assert closure.final_declaration() is None
    ledger = ClosureLedger(REPO).build([JUNIT], external_pending=[], final_declaration=None)
    assert ledger["release_status"] == "rc_candidate_not_final"
    ledger = ClosureLedger(REPO).build([JUNIT], external_pending=["x"], final_declaration=declared)
    assert ledger["release_status"] == "final_declared"
    assert ledger["remaining_blockers"]["external_qualification_pending"] == ["x"]
