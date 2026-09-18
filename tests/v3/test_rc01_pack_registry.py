"""V3-034 — Versioned capability pack registry (design/13 §2/§5, T-074/076/077/078/093/094/097/111).

Signed pack + dependency + permission + schema registry with an app-independent install
boundary. Requested permissions are never grants; unknown publishers, elevated
permissions, unregistered tool adapters, executable payload without attestation and
version conflicts fail before any file is written.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from amplai_foundry.distribution.installer import KitInstaller
from amplai_foundry.distribution.packs import PackRegistry, seal_pack
from amplai_foundry.packs.catalog import PackCatalog
from amplai_foundry.runtime.contracts.identity import digest_bytes
from amplai_foundry.runtime.errors import Hold, RuntimeFault

KEY = Ed25519PrivateKey.generate()
PUBLISHERS = {"amplai": {"amplai-k1": KEY.public_key()}}
ADAPTERS = {"software.build", "software.test"}


def manifest(pack_id="software", version="1.0.0", **overrides):
    value = {
        "schema_version": "3.0.0",
        "pack_id": pack_id,
        "version": version,
        "publisher": "amplai",
        "protocol_major": 3,
        "entry_capabilities": ["software.build"],
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


def sealed(m, files, *, key_id="amplai-k1", private_key=KEY):
    """Direct seal helper: owned_paths must equal the payload, as the catalog would set."""
    return seal_pack(
        {**m, "owned_paths": sorted(files)}, files, key_id=key_id, private_key=private_key
    )


def write_pack(root: Path, pack_id="software", *, files=None, manifest_value=None):
    directory = root / pack_id
    (directory / "skills").mkdir(parents=True)
    files = (
        files
        if files is not None
        else {"skills/build.md": b"# build\n", "context/notes.md": b"notes\n"}
    )
    for name, data in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (directory / "PACK.json").write_text(json.dumps(manifest_value or manifest(pack_id)))
    return directory


@pytest.fixture
def installer_actor(deployment):
    return replace(deployment.actor, permissions=deployment.actor.permissions | {"pack.install"})


def registry(d, **kwargs):
    kwargs.setdefault("tool_adapters", ADAPTERS)
    return PackRegistry(d.store, d.contracts, PUBLISHERS, **kwargs)


# ---------------------------------------------------------------- catalog layout


def test_t093_optional_directories_absent_is_not_corruption(tmp_path):
    write_pack(tmp_path, files={"skills/only.md": b"x\n"})
    m, files = PackCatalog(tmp_path).load("software")
    assert m["owned_paths"] == ["skills/only.md"] and list(files) == ["skills/only.md"]
    assert PackCatalog.layout_of(files)["templates"] == []
    assert PackCatalog(tmp_path).list_ids() == ["software"]


def test_catalog_rejects_missing_manifest_layout_escape_and_ownership_drift(tmp_path):
    write_pack(tmp_path)
    (tmp_path / "software" / "PACK.json").unlink()
    with pytest.raises(Hold) as exc:
        PackCatalog(tmp_path).load("software")
    assert exc.value.code == "PACK_MANIFEST_MISSING"

    write_pack(tmp_path, "docs", files={"skills/a.md": b"a\n", "bin/run": b"x\n"})
    with pytest.raises(Hold) as exc:
        PackCatalog(tmp_path).load("docs")
    assert exc.value.code == "PACK_LAYOUT" and exc.value.details == {"path": "bin/run"}

    write_pack(tmp_path, "drift", manifest_value=manifest("drift", owned_paths=["skills/build.md"]))
    with pytest.raises(Hold) as exc:
        PackCatalog(tmp_path).load("drift")
    assert exc.value.code == "PACK_OWNERSHIP" and exc.value.details["undeclared"] == [
        "context/notes.md"
    ]

    write_pack(tmp_path, "link")
    os.symlink(tmp_path / "software" / "PACK.json", tmp_path / "link" / "skills" / "escape.md")
    with pytest.raises(Hold) as exc:
        PackCatalog(tmp_path).load("link")
    assert exc.value.code == "PACK_SYMLINK"


# ---------------------------------------------------------------- signed registry


def test_seal_register_is_idempotent_and_records_no_grant(deployment, installer_actor, tmp_path):
    d = deployment
    write_pack(tmp_path)
    envelope = PackCatalog(tmp_path).seal("software", key_id="amplai-k1", private_key=KEY)
    r = registry(d)
    ref = r.register(installer_actor, envelope)
    assert r.register(installer_actor, envelope) == ref
    stored = d.store.get(d.scope, "capability-pack", ref)
    assert stored["owned_paths"] == ["context/notes.md", "skills/build.md"]
    events = [e for e in d.store.events(d.scope) if e["event_type"] == "pack.registered"]
    assert len(events) == 1 and events[0]["data"]["requested_permissions_are_grants"] is False


def test_t111_tampered_payload_and_unknown_publisher_reject(deployment, installer_actor, tmp_path):
    d = deployment
    write_pack(tmp_path)
    envelope = PackCatalog(tmp_path).seal("software", key_id="amplai-k1", private_key=KEY)
    tampered = json.loads(json.dumps(envelope))
    tampered["files"]["skills/build.md"]["content_base64"] = base64.b64encode(b"# evil\n").decode()
    with pytest.raises(RuntimeFault):
        registry(d).register(installer_actor, tampered)
    stranger = Ed25519PrivateKey.generate()
    foreign = sealed(
        manifest(publisher="stranger"),
        {"skills/build.md": b"x\n"},
        key_id="s1",
        private_key=stranger,
    )
    with pytest.raises(Hold) as exc:
        registry(d).register(installer_actor, foreign)
    assert exc.value.code == "PACK_PUBLISHER"
    assert d.store.list_objects(d.scope, "capability-pack") == []


def test_version_mutation_and_version_conflict_hold(deployment, installer_actor, tmp_path):
    d = deployment
    r = registry(d)
    base1 = r.register(
        installer_actor,
        sealed(manifest("base", "1.0.0"), {"skills/a.md": b"1\n"}),
    )
    with pytest.raises(Hold) as exc:
        r.register(
            installer_actor,
            sealed(
                manifest("base", "1.0.0", license="MIT"),
                {"skills/a.md": b"1\n"},
            ),
        )
    assert exc.value.code == "PACK_VERSION_MUTATION"
    base2 = r.register(
        installer_actor,
        sealed(manifest("base", "2.0.0"), {"skills/a.md": b"2\n"}),
    )
    x = r.register(
        installer_actor,
        sealed(
            manifest("x", dependencies=[base1]),
            {"skills/x.md": b"x\n"},
        ),
    )
    y = r.register(
        installer_actor,
        sealed(
            manifest("y", dependencies=[base2]),
            {"skills/y.md": b"y\n"},
        ),
    )
    with pytest.raises(Hold) as exc:
        r.register(
            installer_actor,
            sealed(
                manifest("z", dependencies=[x, y]),
                {"skills/z.md": b"z\n"},
            ),
        )
    assert exc.value.code == "PACK_VERSION_CONFLICT"
    assert exc.value.details == {"pack_id": "base", "versions": ["1.0.0", "2.0.0"]}


# ---------------------------------------------------------------- permissions / tools / payload


def test_t077_permissions_stay_inside_ceiling_and_are_never_grants(deployment, installer_actor):
    d = deployment
    r = registry(d)
    prod = manifest(
        permissions=[
            {"action": "deploy", "resource": "prod:*", "effect_class": "production_control"}
        ]
    )
    with pytest.raises(Hold) as exc:
        r.register(
            installer_actor,
            sealed(prod, {"skills/a.md": b"a\n"}),
        )
    assert exc.value.code == "PACK_PERMISSION_ELEVATED"
    ceiling = [
        {"action": "api.call", "resource": "api:*", "effect_class": "external_idempotent_write"}
    ]
    ok = manifest(
        "apiok",
        permissions=[
            {"action": "api.call", "resource": "api:x", "effect_class": "external_idempotent_write"}
        ],
    )
    assert r.register(
        installer_actor,
        sealed(ok, {"skills/a.md": b"a\n"}),
        permission_ceiling=ceiling,
    )
    too_much = manifest(
        "apino",
        permissions=[
            {
                "action": "api.call",
                "resource": "api:x",
                "effect_class": "external_nonidempotent_write",
            }
        ],
    )
    with pytest.raises(Hold) as exc:
        r.register(
            installer_actor,
            sealed(too_much, {"skills/a.md": b"a\n"}),
            permission_ceiling=ceiling,
        )
    assert exc.value.code == "PACK_PERMISSION_ELEVATED"


def test_entry_capabilities_must_be_registered_adapters_not_paths(deployment, installer_actor):
    d = deployment
    shell = manifest("shell", entry_capabilities=["shell.exec", "software.build"])
    with pytest.raises(Hold) as exc:
        registry(d).register(
            installer_actor,
            sealed(shell, {"skills/a.md": b"a\n"}),
        )
    assert exc.value.code == "PACK_TOOL_UNREGISTERED" and exc.value.details == {
        "entry_capabilities": ["shell.exec"]
    }
    with pytest.raises(Hold):
        registry(d, tool_adapters=()).register(
            installer_actor,
            sealed(manifest(), {"skills/a.md": b"a\n"}),
        )


def test_executable_payload_needs_attestation_and_secrets_are_refused(deployment, installer_actor):
    d = deployment
    script = b"#!/bin/sh\necho hi\n"
    env = sealed(manifest("tool"), {"skills/run.sh": script})
    with pytest.raises(Hold) as exc:
        registry(d).register(installer_actor, env)
    assert exc.value.code == "PACK_EXECUTABLE_UNATTESTED"
    assert registry(d, attested_digests={digest_bytes(script)}).register(installer_actor, env)
    leak = sealed(
        manifest("leak"),
        {"context/c.md": b"api_key = " + b"a" * 40 + b"\n"},
    )
    with pytest.raises(Hold) as exc:
        registry(d).register(installer_actor, leak)
    assert exc.value.code == "PACK_SECRET" and exc.value.details == {"path": "context/c.md"}


def test_t078_missing_install_authority_is_rejected(deployment):
    env = sealed(manifest(), {"skills/a.md": b"a\n"})
    with pytest.raises(RuntimeFault):
        registry(deployment).register(
            replace(deployment.actor, permissions=frozenset({"goal.submit"})), env
        )


# ---------------------------------------------------------------- install boundary


def test_t094_app_independent_boundary_and_owned_file_conflict(deployment, tmp_path):
    d = deployment
    r = registry(d)
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "src").mkdir()
    (app_root / "src" / "main.py").write_text("print('app')\n")
    into_source = sealed(manifest("bad"), {"src/config.md": b"pwned\n"})
    with pytest.raises(Hold) as exc:
        KitInstaller(r).plan(app_root, into_source)
    assert exc.value.code == "INSTALL_SCOPE"
    assert (app_root / "src" / "main.py").read_text() == "print('app')\n"
    good = sealed(
        manifest("good"),
        {".agents/skills/x/SKILL.md": b"# x\n"},
    )
    plan = KitInstaller(r).plan(app_root, good)
    assert [c["action"] for c in plan["changes"]] == ["add"] and plan[
        "requested_permissions_are_grants"
    ] is False
    (app_root / ".agents" / "skills" / "x").mkdir(parents=True)
    (app_root / ".agents" / "skills" / "x" / "SKILL.md").write_text("user wrote this\n")
    with pytest.raises(Hold) as exc:
        KitInstaller(r).plan(app_root, good)
    assert exc.value.code == "UNOWNED_FILE"
    assert (app_root / ".agents" / "skills" / "x" / "SKILL.md").read_text() == "user wrote this\n"
