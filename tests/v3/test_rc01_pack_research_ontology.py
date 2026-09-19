"""V3-039 — Research/ontology/domain pack adapters.

design/12 §5, design/13 §3, T-016/019/020/098.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplai_foundry.packs.catalog import PackCatalog
from amplai_foundry.packs.conformance import DEFAULT_RUNNERS, PackConformance
from amplai_foundry.runtime.contracts.registry import Contracts
from amplai_foundry.runtime.errors import Hold

REPO = Path(__file__).resolve().parents[2]


def load(pack_id):
    return PackCatalog(REPO / "packs").load(pack_id)


def test_research_pack_never_promotes_to_canonical_and_pins_sources():
    manifest, files = load("research")
    report = PackConformance(Contracts()).check(manifest, files)
    assert report["status"] == "conformant"
    assert json.loads(files["context/interface.json"])["promotes_to_canonical"] is False
    assert {c for c in report["cases"]} == {"rs-001", "rs-002"}
    assert manifest["permissions"] == [
        {"action": "workspace.read", "resource": "sandbox:*", "effect_class": "pure_read"}
    ]
    files = dict(files)
    iface = json.loads(files["context/interface.json"])
    iface["promotes_to_canonical"] = True
    files["context/interface.json"] = json.dumps(iface).encode()
    with pytest.raises(Hold) as exc:
        PackConformance(Contracts()).check(manifest, files)
    assert exc.value.code == "PACK_CANONICAL_PROMOTION"


def test_ontology_pack_shacl_is_optional_and_disabled_until_installed():
    manifest, files = load("ontology")
    report = PackConformance(Contracts()).check(manifest, files)
    assert report["status"] == "conformant"
    assert report["runners"] == {
        "ontology-contradictions": "available",
        "ontology-shacl": "disabled_unqualified",
        "ontology-validate": "available",
    }
    with_shacl = PackConformance(Contracts(), installed_runners=DEFAULT_RUNNERS | {"shacl"}).check(
        manifest, files
    )
    assert with_shacl["disabled_unqualified"] == []
    shacl = json.loads(files["verifiers/definitions/ontology-shacl.json"])
    assert shacl["optional"] is True and "SHACL 1.2" in shacl["notes"]
    assert "graph database requirement" in json.loads(files["context/interface.json"])["not_core"]


def test_semiconductor_dc_pack_is_context_only_with_no_core_coupling():
    manifest, files = load("semiconductor-dc")
    report = PackConformance(Contracts()).check(manifest, files)
    assert report["status"] == "conformant"
    assert all(p["effect_class"] == "pure_read" for p in manifest["permissions"])
    iface = json.loads(files["context/interface.json"])
    assert iface["core_coupling"] is False and iface["human_gate"]["kind"] == "required"
    assert "ownership.md" in {Path(k).name for k in files}
    files = dict(files)
    iface["core_coupling"] = True
    files["context/interface.json"] = json.dumps(iface).encode()
    with pytest.raises(Hold) as exc:
        PackConformance(Contracts()).check(manifest, files)
    assert exc.value.code == "PACK_CORE_COUPLING"


def test_every_shipped_pack_loads_and_conforms_on_a_full_host():
    catalog = PackCatalog(REPO / "packs")
    full = PackConformance(
        Contracts(),
        installed_runners=DEFAULT_RUNNERS | {"visual_render", "shacl", "model_critique"},
    )
    for pack_id in catalog.list_ids():
        manifest, files = catalog.load(pack_id)
        report = full.check(manifest, files)
        assert report["status"] == "conformant" and report["disabled_unqualified"] == [], pack_id
