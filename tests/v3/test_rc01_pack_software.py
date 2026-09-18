"""V3-036 — Software/review/debug capability pack.

design/13 §3, design/14 §2-3, T-031/036/062/063.
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


def load(pack_id="software"):
    return PackCatalog(REPO / "packs").load(pack_id)


def test_software_pack_is_conformant_with_protected_regression_profile():
    manifest, files = load()
    report = PackConformance(Contracts()).check(manifest, files)
    assert report["status"] == "conformant" and report["pack_id"] == "software"
    assert {"software-build", "software-test", "software-lint", "software-abi"} <= set(
        report["definitions"]
    )
    test_def = json.loads(files["verifiers/definitions/software-test.json"])
    assert test_def["protected"] is True and test_def["allowed_command_ids"] == ["test-junit"]
    assert report["runners"]["software-review"] == "disabled_unqualified", (
        "model critique is optional, never assumed"
    )
    assert {"sw-001", "sw-002", "sw-003", "sw-004"} == set(report["cases"])
    assert "self-graded PASS" in json.loads(files["context/interface.json"])["not_core"]


def test_t062_deterministic_verifier_without_command_allowlist_is_refused():
    manifest, files = load()
    files = dict(files)
    broken = json.loads(files["verifiers/definitions/software-test.json"])
    broken["allowed_command_ids"] = []
    files["verifiers/definitions/software-test.json"] = json.dumps(broken).encode()
    with pytest.raises(Hold) as exc:
        PackConformance(Contracts()).check(manifest, files)
    assert exc.value.code == "PACK_VERIFIER_COMMANDS"


def test_verifier_cannot_need_more_capability_than_the_pack_requested():
    manifest, files = load()
    files = dict(files)
    greedy = json.loads(files["verifiers/definitions/software-build.json"])
    greedy["required_capabilities"].append(
        {"action": "deploy", "resource": "prod:*", "effect_class": "production_control"}
    )
    files["verifiers/definitions/software-build.json"] = json.dumps(greedy).encode()
    with pytest.raises(Hold) as exc:
        PackConformance(Contracts()).check(manifest, files)
    assert exc.value.code == "PACK_VERIFIER_CAPABILITY"


def test_definitions_materialize_into_schema_valid_verifier_profiles():
    manifest, files = load()
    pc = PackConformance(Contracts())
    env = {"id": "env-1", "revision": 1, "digest": "sha256:" + "1" * 64}
    for definition in pc.definitions(manifest, files).values():
        profile = pc.materialize(
            definition, environment_ref=env, owner_subject_id="verifier-service"
        )
        assert profile["schema_version"] == "3.0.0" and profile["environment_ref"] == env
        assert profile["kind"] == definition["kind"]


def test_case_binding_and_interface_guards():
    manifest, files = load()
    files = dict(files)
    orphan = json.loads(files["eval/cases/sw-001.json"])
    orphan["verifier_definition"] = "does-not-exist"
    files["eval/cases/sw-001.json"] = json.dumps(orphan).encode()
    with pytest.raises(Hold) as exc:
        PackConformance(Contracts()).check(manifest, files)
    assert exc.value.code == "PACK_CASE_VERIFIER"
    files = dict(load()[1])
    iface = json.loads(files["context/interface.json"])
    iface["auto_approval"] = True
    files["context/interface.json"] = json.dumps(iface).encode()
    with pytest.raises(Hold) as exc:
        PackConformance(Contracts(), installed_runners=DEFAULT_RUNNERS).check(manifest, files)
    assert exc.value.code == "PACK_AUTO_APPROVAL"
