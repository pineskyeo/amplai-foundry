"""Review round 2 follow-ups R102 R106 R107 R108 (failure lens, specs/013 trace round 2)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from amplai_foundry.control_plane.hermes import HermesIdentityMap
from amplai_foundry.distribution.cutover import CutoverService
from amplai_foundry.meta_harness.reference import MetaReference
from amplai_foundry.migration.v2 import V2Importer
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Hold


@pytest.fixture
def meta(tmp_path):
    m = MetaReference(tmp_path / "meta")
    try:
        yield m
    finally:
        m.close()


class _ExplodingInstaller:
    def plan(self, root, bundle):
        return {"plan_id": "p1"}

    def apply(self, actor, root, bundle, plan):
        raise OSError(28, "No space left on device")


def test_r102_cutover_receipt_commits_with_pointer_switch_even_when_install_io_fails(meta):
    m = meta
    p = m.prepare()
    svc = CutoverService(
        m.d.store,
        m.d.contracts,
        approval_check=m.check,
        trusted_release_keys=m.meta.keys,
        installer=_ExplodingInstaller(),
    )
    active = m.d.store.head(m.d.scope, "release-pointer", "active")["data"]["release_ref"]
    targets = [
        {
            "id": "alpha",
            "binding_ref": m.canary_target(p["cases"][0]["case_id"]),
            "root": "/tmp/never-used",
            "bundle": {},
        }
    ]
    subject = digest(
        {
            "release_ref": p["candidate_release_ref"],
            "targets": targets,
            "expected_active_ref": active,
        }
    )
    ok = m.approve("release.cutover", subject)
    receipt = svc.cutover(
        m.reviewer,
        p["candidate_release_ref"],
        targets=targets,
        human_decision_ref=ok,
        expected_active_ref=active,
    )
    assert receipt["overall"] == "partial"
    assert receipt["targets"][0]["code"] == "INSTALL_IO"
    reader = replace(m.reviewer, permissions=m.reviewer.permissions | {"runtime.read"})
    status = svc.status(reader)
    assert status["active_release_ref"] == p["candidate_release_ref"]
    assert status["cutovers"] == 1 and status["last"]["overall"] == "partial"
    revisions = [r["revision"] for r, _ in m.d.store.list_objects(m.d.scope, "cutover-receipt")]
    assert sorted(revisions) == [1, 2]  # draft (with the pointer tx) then final


def test_r106_recover_removes_mkstemp_residue_and_reports_it(deployment, tmp_path):
    from amplai_foundry.distribution.installer import KitInstaller
    from amplai_foundry.distribution.packs import PackRegistry

    d = deployment
    installer = KitInstaller(PackRegistry(d.store, d.contracts, {}))
    root = tmp_path / "app"
    meta = root / ".ai-team" / "install-v3"
    meta.mkdir(parents=True)
    (meta / ".v3-abc123").write_bytes(b"half written")
    actor = replace(d.actor, permissions=d.actor.permissions | {"pack.install"})
    result = installer.recover(actor, root)
    assert result["status"] == "clean"
    assert result["temp_removed"] == [".ai-team/install-v3/.v3-abc123"]
    assert not (meta / ".v3-abc123").exists()


def test_r107_v2_apply_verifies_plan_digest_even_without_dry_run_flag(deployment, tmp_path):
    d = deployment
    src = tmp_path / "legacy"
    src.mkdir()
    (src / "note.json").write_text('{"kind": "note", "text": "hi"}')
    actor = replace(d.actor, permissions=d.actor.permissions | {"migration.apply"})
    importer = V2Importer(d.store, d.artifacts)
    plan = importer.plan(src)
    assert plan["plan_digest"] == digest({k: v for k, v in plan.items() if k != "plan_digest"})
    tampered = {k: v for k, v in plan.items() if k != "dry_run_required"}
    with pytest.raises(Hold) as exc:
        importer.apply(actor, tampered)
    assert exc.value.code == "MIGRATION_PLAN_DIGEST"
    assert d.store.list_objects(d.scope, "legacy-import") == []


def test_r108_hermes_bind_rejects_permissions_the_operator_does_not_hold(deployment):
    d = deployment
    identities = HermesIdentityMap(d.store)
    operator = replace(d.actor, permissions=frozenset({"runtime.admin", "goal.submit"}))
    escalated = replace(
        d.actor, permissions=frozenset({"goal.submit", "runtime.admin", "pack.install"})
    )
    with pytest.raises(Hold) as exc:
        identities.bind(operator, "slack", "U9", escalated)
    assert exc.value.code == "IDENTITY_PERMISSION_SUBSET"
    assert exc.value.details == {"excess": ["pack.install"]}
    assert d.store.list_objects(d.scope, "hermes-identity") == []
    subset = replace(d.actor, permissions=frozenset({"goal.submit"}))
    ref = identities.bind(operator, "slack", "U9", subset)
    assert ref["revision"] == 1


def test_r401_recover_takes_owner_lock_before_removing_residue(deployment, tmp_path):
    import fcntl

    from amplai_foundry.distribution.installer import KitInstaller
    from amplai_foundry.distribution.packs import PackRegistry

    d = deployment
    installer = KitInstaller(PackRegistry(d.store, d.contracts, {}))
    root = tmp_path / "app"
    meta = root / ".ai-team" / "install-v3"
    meta.mkdir(parents=True)
    temp = meta / ".v3-inflight"
    temp.write_bytes(b"about to be os.replace()d by a running apply()")
    actor = replace(d.actor, permissions=d.actor.permissions | {"pack.install"})
    with open(meta / "owner.lock", "a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # simulate the running apply()
        with pytest.raises(Hold) as exc:
            installer.recover(actor, root)
        assert exc.value.code == "INSTALL_BUSY"
        assert temp.exists()  # nothing was removed while another owner held the lock
    result = installer.recover(actor, root)
    assert result["temp_removed"] == [".ai-team/install-v3/.v3-inflight"]
    assert not temp.exists()


def test_r402_resolve_never_mints_more_than_the_binding_operator_held(deployment):
    from amplai_foundry.runtime.contracts.identity import now

    d = deployment
    identities = HermesIdentityMap(d.store)
    operator = replace(d.actor, permissions=frozenset({"runtime.admin", "goal.submit"}))
    bound = replace(d.actor, permissions=frozenset({"goal.submit"}))
    identities.bind(operator, "slack", "U-ok", bound)
    assert identities.resolve(d.scope, "slack", "U-ok").actor.permissions == {"goal.submit"}
    # a legacy record written before the rule (no operator_permissions) fails closed
    legacy = {
        "channel": "slack",
        "external_user_id": "U-old",
        "subject_id": "old",
        "kind": "human",
        "permissions": ["goal.submit", "pack.install"],
        "authn_context_ref": None,
        "verified_by": "someone",
        "bound_at": now(),
    }
    with d.store.tx() as db:
        d.store.put(db, d.scope, HermesIdentityMap.KIND, "slack:U-old", 1, legacy)
    with pytest.raises(Hold) as exc:
        identities.resolve(d.scope, "slack", "U-old")
    assert exc.value.code == "IDENTITY_REBIND_REQUIRED"


def test_r501_resolve_clamps_stored_permissions_wider_than_the_operator_held(deployment):
    """A tampered or widened record never mints beyond operator_permissions (mutation R501)."""
    from amplai_foundry.runtime.contracts.identity import now

    d = deployment
    identities = HermesIdentityMap(d.store)
    widened = {
        "channel": "slack",
        "external_user_id": "U-wide",
        "subject_id": "wide",
        "kind": "human",
        "permissions": ["goal.submit", "pack.install", "runtime.admin"],
        "operator_permissions": ["goal.submit"],
        "authn_context_ref": None,
        "verified_by": "someone",
        "bound_at": now(),
    }
    with d.store.tx() as db:
        d.store.put(db, d.scope, HermesIdentityMap.KIND, "slack:U-wide", 1, widened)
    resolved = identities.resolve(d.scope, "slack", "U-wide")
    assert resolved.actor.permissions == frozenset({"goal.submit"})
    assert "pack.install" not in resolved.actor.permissions
    assert "runtime.admin" not in resolved.actor.permissions
