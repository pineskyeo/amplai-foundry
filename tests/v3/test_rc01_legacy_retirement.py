"""V3-060 — Legacy retirement candidates and exact deletion proposal.

design/19 §4, design/25 §2, T-092.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplai_foundry.migration.retirement import RetirementPlanner
from amplai_foundry.runtime.errors import RuntimeFault

REPO = Path(__file__).resolve().parents[2]
PROPOSAL = REPO / "migration" / "retirement-proposal.json"


@pytest.fixture(scope="module")
def planner():
    rp = RetirementPlanner(REPO)
    return rp, rp._files()


def test_t092_legacy_gates_with_live_import_chain_is_blocked_not_deleted(planner):
    rp, files = planner
    c = rp.candidate(
        "src/amplai_foundry/governance/legacy_gates.py",
        owner="Authority Engineer",
        reason="guard parity not yet proven",
        replacement="src/amplai_foundry/runtime/contracts/gates.py",
        parity_evidence=[],
        backup_ref=None,
        required_test_ids=["T-092", "T-110"],
        files=files,
    )
    assert c["status"] == "blocked" and c["automatic_delete"] is False
    assert {"protected_prefix", "live_static_refs", "no_parity_evidence", "no_backup"} <= set(
        c["blockers"]
    )
    assert any(
        ref.startswith("src/amplai_foundry/governance/decisions.py")
        for ref in c["live_refs"]["static"]
    )
    assert c["old_digest"].startswith("sha256:")
    assert (REPO / "src/amplai_foundry/governance/legacy_gates.py").is_file(), "nothing was deleted"


def test_name_based_or_glob_retirement_is_refused(planner):
    rp, files = planner
    with pytest.raises(RuntimeFault):
        rp.candidate(
            "src/amplai_foundry/governance/legacy_*.py",
            owner="x",
            reason="name",
            replacement=None,
            parity_evidence=[],
            backup_ref=None,
            required_test_ids=["T-092"],
            files=files,
        )
    with pytest.raises(RuntimeFault):
        rp.candidate(
            "scripts/amplai_supervisor.py",
            owner="x",
            reason="y",
            replacement=None,
            parity_evidence=[],
            backup_ref=None,
            required_test_ids=[],
            files=files,
        )


def test_dynamic_entry_points_count_as_live_refs(planner):
    rp, files = planner
    c = rp.candidate(
        ".agents/skills/eli12/SKILL.md",
        owner="Migration Lead",
        reason="utility pack",
        replacement="optional utility pack",
        parity_evidence=["packs/spec/context/retirement-map.json"],
        backup_ref="git:HEAD",
        required_test_ids=["T-092", "T-098"],
        files=files,
    )
    assert "dynamic_entry_refs" in c["blockers"]
    assert any(
        ref.startswith("skills-lock.json")
        or ref.startswith(".ai-team/runtime/repository-profile.json")
        for ref in c["live_refs"]["dynamic_entry"]
    )


def test_candidate_with_no_refs_parity_and_backup_is_proposed_but_still_gated(tmp_path):
    root = tmp_path / "repo"
    (root / "old").mkdir(parents=True)
    (root / "old" / "thing.py").write_text("x = 1\n")
    (root / "new").mkdir()
    (root / "new" / "thing.py").write_text("x = 1\n")
    rp = RetirementPlanner(root)
    c = rp.candidate(
        "old/thing.py",
        owner="o",
        reason="moved",
        replacement="new/thing.py",
        parity_evidence=["tests/test_thing.py"],
        backup_ref="git:abc",
        required_test_ids=["T-092"],
    )
    assert c["status"] == "proposed" and c["blockers"] == []
    plan = rp.plan([c])
    assert plan["human_gate"] == "destructive_change" and plan["deletes_executed"] == 0
    assert plan["candidates"][0]["approval_required"] is True
    assert (root / "old" / "thing.py").is_file()


def test_shipped_proposal_matches_current_tree_and_design_dispositions(planner):
    rp, files = planner
    shipped = json.loads(PROPOSAL.read_text())
    assert shipped["deletes_executed"] == 0 and shipped["counts"]["proposed"] == 0
    for c in shipped["candidates"]:
        fresh = rp.candidate(
            c["path"],
            owner=c["owner"],
            reason=c["reason"],
            replacement=c["replacement"],
            parity_evidence=c["parity_evidence"],
            backup_ref=c["backup_ref"],
            required_test_ids=c["required_test_ids"],
            files=files,
        )
        assert fresh["status"] == "blocked", c["path"]
        assert fresh["old_digest"] == c["old_digest"], (
            f"{c['path']} changed since the proposal was written"
        )
    by_path = {c["path"]: c for c in shipped["candidates"]}
    assert (
        by_path["src/amplai_foundry/governance/legacy_gates.py"]["design_disposition"]
        == "keep_protected_extend"
    )
    assert (
        by_path[".agents/skills/grill-me/SKILL.md"]["design_disposition"]
        == "retire_public_after_merge"
    )
    assert all(c["design_delete_authorized"] is False for c in shipped["candidates"])
