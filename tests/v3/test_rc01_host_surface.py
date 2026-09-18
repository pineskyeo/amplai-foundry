"""V3-035 — Core work/design host surface and SpecKit internalization.

design/13 §1, T-010/092/098.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from amplai_foundry.distribution.packs import EFFECT_RANK, PackRegistry
from amplai_foundry.packs.catalog import PackCatalog
from amplai_foundry.packs.spec_surface import (
    AUX_UTILITIES,
    PUBLIC_ENTRIES,
    SPEC_SKILLS,
    HostSurface,
)
from amplai_foundry.runtime.errors import Hold

REPO = Path(__file__).resolve().parents[2]


def test_exactly_two_public_entries_in_this_repository():
    result = HostSurface(REPO).verify()
    assert result["public_entries"] == sorted(PUBLIC_ENTRIES)
    assert set(SPEC_SKILLS) <= set(result["internal"])
    assert result["utilities"] == sorted(AUX_UTILITIES)


def test_spec_pack_projection_is_in_sync_and_deterministic(tmp_path):
    assert HostSurface(REPO).verify_spec_pack(REPO / "packs") == {
        "files": len(SPEC_SKILLS),
        "status": "in_sync",
    }
    (tmp_path / "spec").mkdir()
    shutil.copy(REPO / "packs" / "spec" / "PACK.json", tmp_path / "spec" / "PACK.json")
    first = HostSurface(REPO).render_spec_pack(tmp_path)
    bytes_a = (tmp_path / "spec" / "context" / "surface-map.json").read_bytes()
    second = HostSurface(REPO).render_spec_pack(tmp_path)
    assert (
        first == second
        and (tmp_path / "spec" / "context" / "surface-map.json").read_bytes() == bytes_a
    )
    target = tmp_path / "spec" / "skills" / "taskify" / "SKILL.md"
    target.write_bytes(target.read_bytes() + b"\nhand edit\n")
    with pytest.raises(Hold) as exc:
        HostSurface(REPO).verify_spec_pack(tmp_path)
    assert exc.value.code == "SURFACE_DRIFT"
    assert [d["file"] for d in exc.value.details["drifted"]] == ["skills/taskify/SKILL.md"]


def synthetic_repo(root: Path, *, extra_public=False, implicit_internal=False, broken_mirror=False):
    skills = root / ".agents" / "skills"
    mirror = root / ".claude" / "skills"
    for name, public in (
        ("work", True),
        ("design", True),
        ("code-review", False),
        ("foo", extra_public),
    ):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(
            f"---\nname: {name}\nuser-invocable: {'true' if public else 'false'}\n---\nbody\n"
        )
        if name == "code-review":
            (skills / name / "agents").mkdir()
            (skills / name / "agents" / "openai.yaml").write_text(
                f"allow_implicit_invocation: {'true' if implicit_internal else 'false'}\n"
            )
        mirror.mkdir(parents=True, exist_ok=True)
        if not (broken_mirror and name == "foo"):
            os.symlink(Path("../../.agents/skills") / name, mirror / name)
    return root


def test_t010_surface_negatives_public_leak_implicit_internal_and_mirror(tmp_path):
    with pytest.raises(Hold) as exc:
        HostSurface(synthetic_repo(tmp_path / "a", extra_public=True)).verify()
    assert exc.value.code == "PUBLIC_SURFACE" and exc.value.details == {
        "public": ["design", "foo", "work"]
    }
    with pytest.raises(Hold) as exc:
        HostSurface(synthetic_repo(tmp_path / "b", implicit_internal=True)).verify()
    assert exc.value.code == "INTERNAL_EXPOSED" and exc.value.details == {"skills": ["code-review"]}
    with pytest.raises(Hold) as exc:
        HostSurface(synthetic_repo(tmp_path / "c", broken_mirror=True)).verify()
    assert exc.value.code == "MIRROR_DRIFT" and exc.value.details == {"skills": ["foo"]}


def test_retirement_map_never_deletes_and_keeps_live_refs():
    rm = json.loads((REPO / "packs" / "spec" / "context" / "retirement-map.json").read_text())
    by = {e["skill"]: e for e in rm["entries"]}
    assert set(by) == set(AUX_UTILITIES) | set(SPEC_SKILLS)
    assert all(e["automatic_delete"] is False and e["old_digest"] for e in rm["entries"])
    assert all(by[n]["default_install"] is False for n in AUX_UTILITIES)
    assert all(by[n]["disposition"] == "internalized_into_packs_spec" for n in SPEC_SKILLS)
    assert any(ref.startswith("CLAUDE.md:") for ref in by["grilling"]["live_refs"])
    assert by["grill-me"]["replacement"] == "grilling disposition"


def test_spec_pack_registers_without_source_write_permissions(deployment):
    from dataclasses import replace

    d = deployment
    key = Ed25519PrivateKey.generate()
    manifest, _files = PackCatalog(REPO / "packs").load("spec")
    assert all(
        EFFECT_RANK[p["effect_class"]] <= EFFECT_RANK["sandbox_write"]
        for p in manifest["permissions"]
    )
    envelope = PackCatalog(REPO / "packs").seal("spec", key_id="k", private_key=key)
    actor = replace(d.actor, permissions=d.actor.permissions | {"pack.install"})
    registry = PackRegistry(
        d.store,
        d.contracts,
        {"amplai": {"k": key.public_key()}},
        tool_adapters=manifest["entry_capabilities"],
    )
    ref = registry.register(actor, envelope)
    assert d.store.get(d.scope, "capability-pack", ref)["pack_id"] == "spec"
    with pytest.raises(Hold) as exc:
        PackRegistry(d.store, d.contracts, {"amplai": {"k": key.public_key()}}).register(
            actor, envelope
        )
    assert exc.value.code == "PACK_TOOL_UNREGISTERED"
