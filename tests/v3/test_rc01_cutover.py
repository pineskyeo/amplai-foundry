"""V3-062 — Authorized cutover and independent release receipts.

design/19 §5-6, T-089/T-090/T-097.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from amplai_foundry.distribution.cutover import CutoverService
from amplai_foundry.meta_harness.reference import MetaReference
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault


@pytest.fixture
def meta(tmp_path):
    m = MetaReference(tmp_path / "meta")
    try:
        yield m
    finally:
        m.close()


def service(m):
    return CutoverService(
        m.d.store, m.d.contracts, approval_check=m.check, trusted_release_keys=m.meta.keys
    )


def targets_for(m, p):
    return [{"id": "alpha", "binding_ref": m.canary_target(p["cases"][0]["case_id"])}]


def test_cutover_without_human_decision_is_hold_and_changes_nothing(meta):
    m = meta
    p = m.prepare()
    svc = service(m)
    before = m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
    with pytest.raises(Hold) as exc:
        svc.cutover(
            m.reviewer,
            p["candidate_release_ref"],
            targets=targets_for(m, p),
            human_decision_ref=None,
            expected_active_ref=before,
        )
    assert exc.value.code == "CUTOVER_HUMAN_GATE" and exc.value.details == {
        "gate": "production_operation"
    }
    assert m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"] == before
    assert m.d.store.list_objects(m.d.scope, "cutover-receipt") == []


def test_cutover_needs_matching_approval_and_permission(meta):
    m = meta
    p = m.prepare()
    svc = service(m)
    active = m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
    targets = targets_for(m, p)
    wrong = m.approve("release.cutover", digest({"something": "else"}))
    with pytest.raises(Hold):
        svc.cutover(
            m.reviewer,
            p["candidate_release_ref"],
            targets=targets,
            human_decision_ref=wrong,
            expected_active_ref=active,
        )
    subject = digest(
        {
            "release_ref": p["candidate_release_ref"],
            "targets": targets,
            "expected_active_ref": active,
        }
    )
    ok = m.approve("release.cutover", subject)
    no_perm = replace(m.reviewer, permissions=frozenset({"runtime.read"}))
    with pytest.raises(RuntimeFault):
        svc.cutover(
            no_perm,
            p["candidate_release_ref"],
            targets=targets,
            human_decision_ref=ok,
            expected_active_ref=active,
        )
    receipt = svc.cutover(
        m.reviewer,
        p["candidate_release_ref"],
        targets=targets,
        human_decision_ref=ok,
        expected_active_ref=active,
    )
    assert receipt["overall"] == "complete" and receipt["authority_changed"] is False
    assert receipt["targets"] == [{"target": "alpha", "status": "pointer_switched"}]
    assert (
        m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
        == p["candidate_release_ref"]
    )
    reader = replace(m.reviewer, permissions=m.reviewer.permissions | {"runtime.read"})
    assert svc.status(reader)["cutovers"] == 1


def test_t089_active_release_cas_one_wins(meta):
    m = meta
    p = m.prepare()
    svc = service(m)
    active = m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
    targets = targets_for(m, p)
    ok = m.approve(
        "release.cutover",
        digest(
            {
                "release_ref": p["candidate_release_ref"],
                "targets": targets,
                "expected_active_ref": active,
            }
        ),
    )
    svc.cutover(
        m.reviewer,
        p["candidate_release_ref"],
        targets=targets,
        human_decision_ref=ok,
        expected_active_ref=active,
    )
    with pytest.raises(Conflict) as exc:
        svc.cutover(
            m.reviewer,
            p["candidate_release_ref"],
            targets=targets,
            human_decision_ref=ok,
            expected_active_ref=active,
        )
    assert exc.value.code == "RELEASE_CAS"


def test_unqualified_or_unapproved_release_set_is_hold(meta):
    m = meta
    p = m.prepare()
    svc = service(m)
    active = m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
    release = dict(m.d.store.get(m.d.scope, "release-set", p["candidate_release_ref"]))
    from amplai_foundry.runtime.contracts.identity import sign

    release.pop("signature")
    release["release_id"] = "candidate-unapproved"
    release["status"] = "candidate"
    ref = m.put("release-set", sign(release, "demo-release", m.d.signer))
    ok = m.approve(
        "release.cutover",
        digest({"release_ref": ref, "targets": targets_for(m, p), "expected_active_ref": active}),
    )
    with pytest.raises(Hold) as exc:
        svc.cutover(
            m.reviewer,
            ref,
            targets=targets_for(m, p),
            human_decision_ref=ok,
            expected_active_ref=active,
        )
    assert exc.value.code == "RELEASE_NOT_APPROVED"


def test_unknown_effect_blocks_cutover(meta):
    m = meta
    p = m.prepare()
    svc = service(m)
    active = m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
    targets = targets_for(m, p)
    with m.d.store.tx() as db:
        m.d.store.cas(db, m.d.scope, "effect", "unknown-fixture", 0, "unknown", {})
    ok = m.approve(
        "release.cutover",
        digest(
            {
                "release_ref": p["candidate_release_ref"],
                "targets": targets,
                "expected_active_ref": active,
            }
        ),
    )
    with pytest.raises(Hold) as exc:
        svc.cutover(
            m.reviewer,
            p["candidate_release_ref"],
            targets=targets,
            human_decision_ref=ok,
            expected_active_ref=active,
        )
    assert exc.value.code == "CUTOVER_UNKNOWN_EFFECT"


def test_t090_rollback_after_cutover_keeps_revocations_and_no_authority_restore(meta):
    m = meta
    p = m.prepare()
    result = m.execute(p, rollback=True)
    assert result["promotion"]["state"] == "promoted"
    assert result["rollback"]["state"] == "rolled_back"
    assert (
        result["rollback"]["authority_restored"] is False
        and result["rollback"]["data_restored"] is False
    )
    assert result["rollback"]["active_release_ref"] == p["baseline_release_ref"]


def test_t097_platform_and_kit_version_truth_are_independent(meta):
    m = meta
    p = m.prepare()
    release = m.d.store.get(m.d.scope, "release-set", p["candidate_release_ref"])
    truth = CutoverService.version_truth(release, installed_platform="3.0.0", installed_kit="2.4.0")
    assert truth["platform"]["matches"] is True and truth["kit"]["matches"] is False
    assert truth["coupled"] is False, "same numbers never imply coupling (design/01 §3)"
