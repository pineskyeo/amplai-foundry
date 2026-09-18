"""V3-050 — Kit installer v3 plan/dry-run/apply/receipt.

design/13 §5, design/19 §5, T-092-T-098.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from amplai_foundry.distribution.installer import PROTECTED_PATHS, KitInstaller, release_truth
from amplai_foundry.distribution.packs import PackRegistry, seal_pack
from amplai_foundry.runtime.errors import Conflict, Hold

KEY = Ed25519PrivateKey.generate()
REPO = Path(__file__).resolve().parents[2]


def manifest(pack_id="kit", version="3.0.0", **overrides):
    value = {
        "schema_version": "3.0.0",
        "pack_id": pack_id,
        "version": version,
        "publisher": "amplai",
        "protocol_major": 3,
        "entry_capabilities": ["work.entry"],
        "dependencies": [],
        "permissions": [
            {"action": "workspace.read", "resource": "sandbox:*", "effect_class": "pure_read"}
        ],
        "model_requirements": [],
        "context_refs": [],
        "verifier_refs": [],
        "eval_case_ids": [],
        "license": "Proprietary",
    }
    value.update(overrides)
    return value


def sealed(files, *, pack_id="kit", version="3.0.0"):
    m = {**manifest(pack_id, version), "owned_paths": sorted(files)}
    return seal_pack(m, files, key_id="k1", private_key=KEY)


@pytest.fixture
def kit(deployment, tmp_path):
    d = deployment
    actor = replace(d.actor, permissions=d.actor.permissions | {"pack.install"})
    registry = PackRegistry(
        d.store, d.contracts, {"amplai": {"k1": KEY.public_key()}}, tool_adapters={"work.entry"}
    )
    app = tmp_path / "app"
    app.mkdir()
    (app / "src").mkdir()
    (app / "src" / "app.py").write_text("print('domain')\n")
    return d, actor, KitInstaller(registry), app


V1 = {".agents/skills/work/SKILL.md": b"# work v1\n", ".ai-team/runtime/policy.json": b"{}\n"}
V2 = {
    ".agents/skills/work/SKILL.md": b"# work v2\n",
    ".agents/skills/design/SKILL.md": b"# design\n",
}


def test_plan_dry_run_apply_receipt_then_update_and_remove(kit):
    _d, actor, installer, app = kit
    plan = installer.plan(app, sealed(V1))
    assert [c["action"] for c in plan["changes"]] == ["add", "add"] and plan[
        "operation"
    ] == "install"
    assert (app / ".agents").exists() is False, "dry-run writes nothing"
    receipt = installer.apply(actor, app, sealed(V1), plan)
    assert receipt["owned_files"].keys() == set(V1) and receipt["operation"] == "install"
    assert (
        json.loads((app / ".ai-team" / "install-v3" / "kit.json").read_bytes())["receipt_id"]
        == receipt["receipt_id"]
    )
    plan2 = installer.plan(app, sealed(V2, version="3.1.0"))
    actions = {c["path"]: c["action"] for c in plan2["changes"]}
    assert actions == {
        ".agents/skills/work/SKILL.md": "replace",
        ".agents/skills/design/SKILL.md": "add",
        ".ai-team/runtime/policy.json": "remove",
    }
    installer.apply(actor, app, sealed(V2, version="3.1.0"), plan2)
    assert not (app / ".ai-team" / "runtime" / "policy.json").exists()
    removal = installer.plan_removal(app, "kit")
    assert removal["operation"] == "remove" and {c["action"] for c in removal["changes"]} == {
        "remove"
    }
    out = installer.apply(actor, app, None, removal)
    assert (
        out["operation"] == "remove"
        and not (app / ".agents" / "skills" / "work" / "SKILL.md").exists()
    )
    assert not (app / ".ai-team" / "install-v3" / "kit.json").exists()
    assert (app / "src" / "app.py").read_text() == "print('domain')\n", (
        "T-098: app source untouched"
    )


def test_t094_owned_file_changed_by_user_holds_with_expected_old_digest(kit):
    _d, actor, installer, app = kit
    installer.apply(actor, app, sealed(V1), installer.plan(app, sealed(V1)))
    target = app / ".agents" / "skills" / "work" / "SKILL.md"
    target.write_text("# user edit\n")
    with pytest.raises(Hold) as exc:
        installer.plan(app, sealed(V2, version="3.1.0"))
    assert exc.value.code == "LOCAL_MODIFICATION"
    assert exc.value.details["path"] == ".agents/skills/work/SKILL.md" and exc.value.details[
        "expected_old_digest"
    ].startswith("sha256:")
    assert target.read_text() == "# user edit\n"


def test_t093_missing_optional_owned_file_is_reported_not_corruption(kit):
    _d, actor, installer, app = kit
    installer.apply(actor, app, sealed(V1), installer.plan(app, sealed(V1)))
    (app / ".ai-team" / "runtime" / "policy.json").unlink()
    plan = installer.plan(app, sealed(V1))
    assert plan["warnings"] == [{"path": ".ai-team/runtime/policy.json", "kind": "missing_owned"}]
    assert (
        next(c for c in plan["changes"] if c["path"] == ".ai-team/runtime/policy.json")["action"]
        == "add"
    )


def test_overrides_are_preserved_and_never_owned(kit):
    _d, actor, installer, app = kit
    (app / ".agents" / "overrides").mkdir(parents=True)
    (app / ".agents" / "overrides" / "work.md").write_text("local override\n")
    plan = installer.plan(app, sealed(V1))
    assert plan["overrides_preserved"] == [".agents/overrides/work.md"]
    installer.apply(actor, app, sealed(V1), plan)
    assert (app / ".agents" / "overrides" / "work.md").read_text() == "local override\n"
    with pytest.raises(Hold) as exc:
        installer.plan(
            app, sealed({".agents/overrides/work.md": b"pack wants this\n"}, pack_id="bad")
        )
    assert exc.value.code == "INSTALL_OVERRIDE_NAMESPACE"


def test_protected_paths_are_gated_before_apply(kit):
    _d, actor, installer, app = kit
    for path in (".ai-team/app.json", ".ai-team/policy/approvals.jsonl", ".ai-team/local/x.json"):
        assert any(path == p or path.startswith(p) for p in PROTECTED_PATHS)
    bundle = sealed({".ai-team/app.json": b"{}"}, pack_id="evil")
    plan = installer.plan(app, bundle)
    with pytest.raises(Hold) as exc:
        installer.apply(actor, app, bundle, plan)
    assert exc.value.code == "INSTALL_PROTECTED" and exc.value.details == {
        "paths": [".ai-team/app.json"]
    }
    assert not (app / ".ai-team" / "app.json").exists()


def test_signed_plan_and_receipt_cas_and_preimage_guards(kit):
    _d, actor, installer, app = kit
    bundle = sealed(V1)
    plan = installer.plan(app, bundle)
    tampered = {**plan, "changes": [{**c, "after": "sha256:" + "0" * 64} for c in plan["changes"]]}
    with pytest.raises(Hold) as exc:
        installer.apply(actor, app, bundle, tampered)
    assert exc.value.code == "INSTALL_PLAN"
    other = sealed({".agents/skills/work/SKILL.md": b"# other\n"})
    with pytest.raises(Hold):
        installer.apply(actor, app, other, plan)
    (app / ".agents" / "skills" / "work").mkdir(parents=True)
    (app / ".agents" / "skills" / "work" / "SKILL.md").write_text("raced\n")
    with pytest.raises(Conflict) as exc2:
        installer.apply(actor, app, bundle, plan)
    assert exc2.value.code == "INSTALL_PREIMAGE"


def test_crash_mid_apply_rolls_back_to_exact_preimages_with_rollback_receipt(kit):
    _d, actor, installer, app = kit
    installer.apply(actor, app, sealed(V1), installer.plan(app, sealed(V1)))
    before = {p: (app / p).read_bytes() for p in V1}
    plan = installer.plan(app, sealed(V2, version="3.1.0"))

    def boom(stage):
        if stage == "file:1":
            raise OSError("power loss")

    with pytest.raises(OSError):
        installer.apply(actor, app, sealed(V2, version="3.1.0"), plan, inject_failure=boom)
    # The journal blocks any new apply until the operator reconciles.
    with pytest.raises(Hold) as exc:
        installer.apply(actor, app, sealed(V2, version="3.1.0"), plan)
    assert exc.value.code == "INSTALL_RECOVERY"
    out = installer.recover(actor, app)
    assert (
        out["status"] == "rolled_back_to_preimages" and out["receipt"]["restored_to"] == "preimages"
    )
    assert {p: (app / p).read_bytes() for p in V1} == before
    assert not (app / ".agents" / "skills" / "design" / "SKILL.md").exists()
    receipts = sorted((app / ".ai-team" / "install-v3" / "receipts").glob("rollback-*.json"))
    assert len(receipts) == 1
    assert installer.recover(actor, app) == {"status": "clean"}


def test_multi_target_reports_per_target_and_partial_never_complete(kit, tmp_path):
    _d, actor, installer, _app = kit
    good = tmp_path / "good"
    good.mkdir()
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".agents" / "skills" / "work").mkdir(parents=True)
    (bad / ".agents" / "skills" / "work" / "SKILL.md").write_text("user file\n")
    bundle = sealed(V1)
    good_plan = installer.plan(good, bundle)
    bad_plan = {**good_plan, "root": str(bad.absolute())}
    bad_plan["plan_digest"] = __import__(
        "amplai_foundry.runtime.contracts.identity", fromlist=["digest"]
    ).digest({k: v for k, v in bad_plan.items() if k != "plan_digest"})
    result = installer.apply_many(actor, [(good, bundle, good_plan), (bad, bundle, bad_plan)])
    assert result["overall"] == "partial" and result["counts"] == {"installed": 1, "failed": 1}
    assert result["targets"][str(good.absolute())]["status"] == "installed"
    assert result["targets"][str(bad.absolute())]["code"] in {"INSTALL_PREIMAGE", "UNOWNED_FILE"}


def test_t097_release_truth_reports_the_real_kit_discrepancy_without_editing():
    report = release_truth(REPO / "tools" / "amplai-loop-kit")
    assert report["facts"]["VERSION"] == "2.4.0" and report["facts"]["README.md"] == "2.5.0"
    assert report["status"] == "discrepancy" and report["authoritative"] is None
    assert (REPO / "tools" / "amplai-loop-kit" / "VERSION").read_text().strip() == "2.4.0", (
        "report changes nothing"
    )
