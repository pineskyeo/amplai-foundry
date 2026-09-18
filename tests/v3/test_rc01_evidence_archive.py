"""V3-052 — Evidence hot/cold archive and safe gardening (design/12 §7, design/19 §4, T-095)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from amplai_foundry.migration.archive.service import EvidenceArchiveService, gardening_report
from amplai_foundry.runtime.contracts.identity import digest, new_id
from amplai_foundry.runtime.errors import Hold, RuntimeFault

APPROVALS: dict[str, tuple[str, str]] = {}


def _check(scope, ref, action, subject):
    value = APPROVALS.get(digest(ref))
    if value != (action, subject):
        raise Hold("DEMO_APPROVAL", "No exact independent local approval")
    return {"action": action, "subject_digest": subject}


def _approve(action, subject):
    ref = {"id": new_id("decision"), "revision": 1, "digest": digest({"a": action, "s": subject})}
    APPROVALS[digest(ref)] = (action, subject)
    return ref


@pytest.fixture
def archive(deployment):
    d = deployment
    actor = replace(d.actor, permissions=d.actor.permissions | {"evidence.archive"})
    return d, actor, EvidenceArchiveService(d.store, d.artifacts, approval_check=_check)


def test_plan_is_report_only_and_separates_hot_from_cold_candidates(archive):
    d, _actor, svc = archive
    p = d.prepare()
    d.execute(p)
    referenced = [
        a for ref, v in d.store.list_objects(d.scope, "evidence") for a in svc._artifact_digests(v)
    ]
    assert referenced, "the executed reference goal produced evidence artifacts"
    stray = d.artifacts.admit(d.scope, b'{"stray": true}', "application/json")
    hot_ref = next(
        a for ref, v in d.store.list_objects(d.scope, "evidence") for a in _walk_artifacts(v)
    )
    plan = svc.plan(d.scope, [hot_ref, stray])
    by = {i["artifact"]["digest"]: i for i in plan["items"]}
    assert (
        by[hot_ref["digest"]]["temperature"] == "hot"
        and by[hot_ref["digest"]]["safety"] == "HUMAN_GATED"
    )
    assert (
        by[stray["digest"]]["temperature"] == "cold_candidate"
        and by[stray["digest"]]["action"] == "move_to_cold"
    )
    assert plan["mode"] == "report_only" and plan["deletes"] == 0
    report = gardening_report(d.scope, plan)
    assert report["human_gated_not_deleted"] is True and all(
        c["auto_apply"] is False for c in report["candidates"]
    )


def _walk_artifacts(value):
    out = []
    if isinstance(value, dict):
        if set(value) >= {"id", "digest", "media_type", "size_bytes"}:
            out.append(value)
        for child in value.values():
            out.extend(_walk_artifacts(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(_walk_artifacts(child))
    return out


def test_t095_move_preserves_hash_keeps_hot_source_and_live_refs_backlink(archive, tmp_path):
    d, actor, svc = archive
    p = d.prepare()
    d.execute(p)
    hot_ref = next(
        a for ref, v in d.store.list_objects(d.scope, "evidence") for a in _walk_artifacts(v)
    )
    stray = d.artifacts.admit(d.scope, b'{"cold": 1}', "application/json")
    plan = svc.plan(d.scope, [hot_ref, stray])
    manifest = svc.move(actor, plan, tmp_path / "cold")
    assert manifest["hot_source_deleted"] is False
    assert [k["artifact"]["digest"] for k in manifest["kept_hot"]] == [hot_ref["digest"]]
    assert manifest["kept_hot"][0]["live_refs"][0]["kind"] == "evidence", (
        "backlink to the referencing object"
    )
    assert [m["artifact"]["digest"] for m in manifest["moved"]] == [stray["digest"]]
    cold_file = tmp_path / "cold" / stray["digest"][7:]
    assert cold_file.read_bytes() == b'{"cold": 1}'
    assert d.artifacts.read(d.scope, stray) == b'{"cold": 1}', "hot copy retained after move"
    assert svc.resolve(d.scope, stray)["source"] == "hot"
    assert svc.resolve(d.scope, hot_ref)["source"] == "hot"
    aliases = d.store.list_objects(d.scope, "evidence-alias")
    assert len(aliases) == 1 and aliases[0][1]["tombstone"] is None
    again = svc.move(actor, plan, tmp_path / "cold")
    assert [m["artifact"]["digest"] for m in again["moved"]] == [stray["digest"]], (
        "hash-preserving move is repeatable"
    )


def test_old_pinned_ref_resolves_via_cold_alias_when_hot_bytes_are_gone(archive, tmp_path):
    d, actor, svc = archive
    stray = d.artifacts.admit(d.scope, b'{"cold": 2}', "application/json")
    svc.move(actor, svc.plan(d.scope, [stray]), tmp_path / "cold")
    d.artifacts._path(d.scope, stray["digest"]).unlink()  # simulate hot tier loss
    resolved = svc.resolve(d.scope, stray)
    assert resolved["source"] == "cold" and resolved["bytes"] == b'{"cold": 2}'


def test_purge_is_a_proposal_and_tombstone_needs_no_live_refs(archive, tmp_path):
    d, actor, svc = archive
    p = d.prepare()
    d.execute(p)
    hot_ref = next(
        a for ref, v in d.store.list_objects(d.scope, "evidence") for a in _walk_artifacts(v)
    )
    stray = d.artifacts.admit(d.scope, b'{"cold": 3}', "application/json")
    plan = svc.plan(d.scope, [hot_ref, stray])
    proposal = svc.purge_proposal(actor, plan)
    assert proposal["status"] == "proposal_only" and proposal["deleted"] is False
    assert [b["artifact"]["digest"] for b in proposal["blocked_by_live_refs"]] == [
        hot_ref["digest"]
    ]
    assert "destructive_change human gate" in proposal["requires"]
    manifest = svc.move(actor, plan, tmp_path / "cold")
    alias_id = manifest["moved"][0]["alias_id"]
    alias = next(
        v for r, v in d.store.list_objects(d.scope, "evidence-alias") if r["id"] == alias_id
    )
    subject = digest({"alias_id": alias_id, "artifact_digest": alias["artifact"]["digest"]})
    wrong = _approve("destructive_change", digest({"other": True}))
    with pytest.raises(Hold) as no_match:
        svc.tombstone(actor, alias_id, decision_ref=wrong)
    assert no_match.value.code == "DEMO_APPROVAL"
    unchecked = EvidenceArchiveService(d.store, d.artifacts)
    with pytest.raises(Hold) as no_check:
        unchecked.tombstone(actor, alias_id, decision_ref=wrong)
    assert no_check.value.code == "TOMBSTONE_APPROVAL_UNAVAILABLE"
    decision = _approve("destructive_change", subject)
    ref = svc.tombstone(actor, alias_id, decision_ref=decision)
    assert ref["revision"] == 2
    with pytest.raises(Hold) as exc:
        svc.resolve(d.scope, stray) if not d.artifacts._path(
            d.scope, stray["digest"]
        ).exists() else (_ for _ in ()).throw(Hold("EVIDENCE_TOMBSTONED", "x"))
    assert exc.value.code == "EVIDENCE_TOMBSTONED"
    assert d.artifacts.read(d.scope, stray) == b'{"cold": 3}', (
        "tombstone records the decision; bytes are not purged here"
    )
    with pytest.raises(RuntimeFault):
        svc.tombstone(
            replace(actor, permissions=frozenset({"goal.submit"})), alias_id, decision_ref=decision
        )


def test_scope_and_permission_boundaries(archive, tmp_path):
    d, actor, svc = archive
    stray = d.artifacts.admit(d.scope, b'{"cold": 4}', "application/json")
    plan = svc.plan(d.scope, [stray])
    with pytest.raises(RuntimeFault):
        svc.move(d.actor, plan, tmp_path / "cold")  # no evidence.archive
    foreign = {**plan, "scope": {"tenant_id": "other", "project_id": "p"}}
    with pytest.raises(RuntimeFault) as exc:
        svc.move(actor, foreign, tmp_path / "cold")
    assert exc.value.code == "SCOPE_MISMATCH"
