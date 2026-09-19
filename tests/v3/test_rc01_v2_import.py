"""V3-051 — Runtime/contract V2 import without authority upgrade.

design/19 §2, T-092/095/096/099-106.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from amplai_foundry.migration.v2 import V2Importer
from amplai_foundry.runtime.errors import Hold, RuntimeFault


def v2_store(root: Path) -> Path:
    cr = root / "changes" / "CR-X"
    for sub in ("work", "decisions", "evidence", "questions"):
        (cr / sub).mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps({"kind": "project", "project_id": "p", "schema_version": "1.0"})
    )
    (cr / "change.json").write_text(
        json.dumps(
            {"kind": "change", "change_id": "CR-X", "status": "ACTIVE", "schema_version": "1.0"}
        )
    )
    (cr / "work" / "CR-X-W001.json").write_text(
        json.dumps(
            {
                "kind": "work",
                "work_id": "CR-X-W001",
                "status": "DONE",
                "lease_id": None,
                "schema_version": "1.0",
            }
        )
    )
    (cr / "work" / "CR-X-W002.json").write_text(
        json.dumps(
            {
                "kind": "work",
                "work_id": "CR-X-W002",
                "status": "ACTIVE",
                "lease_id": "lease-old-7",
                "claimed_by": "worker-v2",
                "schema_version": "1.0",
            }
        )
    )
    (cr / "decisions" / "CR-X-D001.json").write_text(
        json.dumps(
            {
                "kind": "decision",
                "decision_id": "CR-X-D001",
                "authority": "HUMAN",
                "decided_by": "user",
                "schema_version": "1.0",
            }
        )
    )
    (cr / "evidence" / "CR-X-E001.json").write_text(
        json.dumps(
            {
                "kind": "evidence",
                "evidence_id": "CR-X-E001",
                "evidence_type": "verifier_result",
                "schema_version": "1.0",
            }
        )
    )
    return root


@pytest.fixture
def importer(deployment):
    d = deployment
    actor = replace(d.actor, permissions=d.actor.permissions | {"migration.apply"})
    return d, actor, V2Importer(d.store, d.artifacts)


def test_plan_preserves_raw_digests_and_flags_active_work_and_approvals(importer, tmp_path):
    _d, _actor, v2 = importer
    root = v2_store(tmp_path / "store")
    plan = v2.plan(root)
    assert plan["kinds"] == {"project": 1, "change": 1, "work": 2, "decision": 1, "evidence": 1}
    assert all(f["digest"].startswith("sha256:") for f in plan["files"])
    assert plan["active_work"] == [
        {
            "path": "changes/CR-X/work/CR-X-W002.json",
            "work_id": "CR-X-W002",
            "status": "ACTIVE",
            "lease_id": "lease-old-7",
            "disposition": "drain",
        }
    ]
    assert plan["legacy_approvals"][0]["v3_grant"] == "reapproval_required"
    assert plan["dry_run_required"] is True and plan["source_untouched"] is True
    assert (
        v2.plan(root, active_run_policy="cancel_reconcile")["active_work"][0]["disposition"]
        == "cancel_reconcile"
    )
    with pytest.raises(RuntimeFault):
        v2.plan(root, active_run_policy="carry_over")


def test_apply_requires_dry_run_of_the_exact_plan(importer, tmp_path):
    _d, actor, v2 = importer
    plan = v2.plan(v2_store(tmp_path / "store"))
    with pytest.raises(Hold) as exc:
        v2.apply(actor, plan)
    assert exc.value.code == "MIGRATION_DRY_RUN"
    other = v2.dry_run(v2.plan(v2_store(tmp_path / "store2")))
    with pytest.raises(Hold) as exc:
        v2.apply(actor, plan, dry_run_receipt=other)
    assert exc.value.code == "MIGRATION_DRY_RUN"
    no_permission = replace(_d.actor, permissions=frozenset({"goal.submit"}))
    with pytest.raises(RuntimeFault):
        v2.apply(no_permission, plan, dry_run_receipt=v2.dry_run(plan))


def test_t096_imported_history_never_becomes_lease_grant_or_trusted_verdict(importer, tmp_path):
    d, actor, v2 = importer
    root = v2_store(tmp_path / "store")
    plan = v2.plan(root)
    grants_before = len(d.store.list_objects(d.scope, "execution-grant"))
    with d.store._lock:
        leases_before = d.store.conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
    result = v2.apply(actor, plan, dry_run_receipt=v2.dry_run(plan))
    assert result["status"] == "imported_as_untrusted_history"
    assert result["grants_created"] == 0 and result["old_leases_activated"] is False
    assert result["active_work_disposed"] == ["CR-X-W002"]
    assert len(d.store.list_objects(d.scope, "execution-grant")) == grants_before
    with d.store._lock:
        assert d.store.conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == leases_before
    work = {r["legacy_id"]: r for r in v2.imported(actor, kind="work")}
    assert (
        work["CR-X-W002"]["lease_valid_in_v3"] is False
        and work["CR-X-W002"]["legacy_disposition"] == "drain"
    )
    assert work["CR-X-W001"]["active_execution"] is False
    decision = v2.imported(actor, kind="decision")[0]
    assert decision["legacy_authority"] == "HUMAN" and decision["authority"] is False
    assert decision["reapproval_required"] is True and decision["v3_grant_ref"] is None
    evidence = v2.imported(actor, kind="evidence")[0]
    assert evidence["trust"] == "legacy_imported" and evidence["verdict_trust"] == "none"
    raw = d.artifacts.read(d.scope, evidence["artifact"])
    assert json.loads(raw)["evidence_id"] == "CR-X-E001"
    events = [e for e in d.store.events(d.scope) if e["event_type"] == "legacy.imported"]
    assert len(events) == 6 and all(e["data"]["authority"] is False for e in events)


def test_source_is_untouched_and_preimage_drift_holds(importer, tmp_path):
    _d, actor, v2 = importer
    root = v2_store(tmp_path / "store")
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    plan = v2.plan(root)
    receipt = v2.dry_run(plan)
    (root / "changes" / "CR-X" / "work" / "CR-X-W001.json").write_text(
        '{"kind":"work","work_id":"CR-X-W001","status":"DONE","tampered":1}'
    )
    with pytest.raises(Hold) as exc:
        v2.apply(actor, plan, dry_run_receipt=receipt)
    assert exc.value.code == "MIGRATION_PREIMAGE"
    (root / "changes" / "CR-X" / "work" / "CR-X-W001.json").write_bytes(
        before[root / "changes" / "CR-X" / "work" / "CR-X-W001.json"]
    )
    first = v2.apply(actor, plan, dry_run_receipt=receipt)
    assert {p: p.read_bytes() for p in root.rglob("*.json")} == before
    again = v2.apply(actor, plan, dry_run_receipt=receipt)
    assert again["refs"] == first["refs"], "re-import of identical bytes is idempotent"


def test_symlink_and_escape_are_rejected(importer, tmp_path):
    _d, actor, v2 = importer
    root = v2_store(tmp_path / "store")
    secret = tmp_path / "outside.json"
    secret.write_text('{"kind":"decision","authority":"HUMAN"}')
    (root / "changes" / "CR-X" / "decisions" / "link.json").symlink_to(secret)
    plan = v2.plan(root)
    assert all("link.json" not in f["path"] for f in plan["files"])
    plan["files"].append(
        {
            "path": "../outside.json",
            "digest": "sha256:" + "0" * 64,
            "size_bytes": 1,
            "kind": "decision",
            "schema_version": "1.0",
        }
    )
    with pytest.raises(Hold) as exc:
        v2.apply(actor, plan, dry_run_receipt=v2.dry_run(plan))
    assert exc.value.code == "MIGRATION_PATH"


REAL_STORE = Path("/Users/pinesky/workspace/amplai-project")


@pytest.mark.skipif(
    not (REAL_STORE / "project.json").is_file(),
    reason="local Project Store not present on this host",
)
def test_real_project_store_plans_read_only(importer):
    """Read-only plan over the real V2 Project Store; nothing is applied."""
    _d, _actor, v2 = importer
    plan = v2.plan(REAL_STORE)
    assert plan["kinds"].get("change", 0) >= 1 and plan["kinds"].get("work", 0) >= 1
    assert all(a["v3_grant"] == "reapproval_required" for a in plan["legacy_approvals"])
    assert plan["source_untouched"] is True
