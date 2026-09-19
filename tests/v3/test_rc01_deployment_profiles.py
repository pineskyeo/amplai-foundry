"""V3-054 — Local/onprem/hybrid/offline/legacy profiles.

design/20, design/31, T-075/T-098/T-112.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.profiles import (
    PROFILES,
    DeploymentProfile,
    support_matrix,
    write_matrix,
)

REPO = Path(__file__).resolve().parents[2]
FULL = {
    "authority_endpoint_and_identity": {
        "endpoint": "https://authority.internal",
        "key_id": "auth-k1",
    },
    "project_actor_bindings": [
        {"subject_id": "ops", "scope": {"tenant_id": "t", "project_id": "p"}}
    ],
    "trusted_signing_keys": {"amplai": "ab" * 32},
    "approved_storage_roots": ["/srv/amplai/runtime"],
    "secret_and_egress_policy": {"egress": "deny_external"},
    "root_budget_limits": {"currency": "KRW", "cap_microunits": 1000000},
    "retention_policy": {"evidence_days": 365},
    "qualified_driver_model_matrix": [{"driver": "codex-cli", "model": "local-recipe-v1"}],
    "canary_policy_and_promotion_authority": {"fraction": 0.05, "approver": "release-reviewer"},
}


def test_profiles_match_design_31_and_defaults_are_not_authorization():
    dp = DeploymentProfile()
    assert set(PROFILES) == {
        "local-dev",
        "onprem",
        "hybrid",
        "offline",
        "legacy-app-client",
        "managed-optional",
    }
    assert dp.defaults["status"] == "proposed_technical_defaults_not_operational_authorization"
    assert dp.describe("offline")["egress_default"] == "deny_all"
    assert dp.describe("legacy-app-client")["required_evidence"][1].startswith(
        "uninstall independence"
    )
    with pytest.raises(RuntimeFault):
        dp.describe("cloud-first")


def test_t112_placeholders_keep_write_activation_fail_closed_but_status_readable():
    dp = DeploymentProfile()
    partial = {**FULL, "trusted_signing_keys": "<fill in>", "retention_policy": {}}
    status = dp.activation("onprem", partial)
    assert status["read_only_status"] == "allowed" and status["write_activation"] == "hold"
    assert status["missing_prerequisites"] == ["trusted_signing_keys", "retention_policy"]
    with pytest.raises(Hold) as exc:
        dp.require_write("onprem", partial)
    assert exc.value.code == "ACTIVATION_PREREQUISITES"
    assert dp.require_write("onprem", FULL)["write_activation"] == "allowed"
    missing_all = dp.activation("local-dev", {})
    assert len(missing_all["missing_prerequisites"]) == len(dp.required) == 9


def test_t075_no_cloud_fallback_without_policy():
    dp = DeploymentProfile()
    assert (
        dp.route("onprem", classification="confidential", local_available=True)["route"] == "local"
    )
    with pytest.raises(Hold) as exc:
        dp.route("onprem", classification="confidential", local_available=False)
    assert exc.value.code == "CLOUD_FALLBACK_PROHIBITED"
    with pytest.raises(Hold) as exc:
        dp.route("offline", classification="public", local_available=False)
    assert exc.value.code == "CLOUD_FALLBACK_PROHIBITED"
    with pytest.raises(Hold) as exc:
        dp.route("hybrid", classification="confidential", local_available=False)
    assert exc.value.code == "CLOUD_DATA_CLASS"
    assert dp.route("hybrid", classification="public", local_available=False)["route"] == "cloud"
    ok = dp.route(
        "hybrid",
        classification="confidential",
        local_available=False,
        cloud_data_policy={"confidential": True},
    )
    assert ok["approved_by_policy"] is True


def test_support_matrix_defaults_to_not_tested_and_needs_evidence_refs():
    rows = {r["profile"]: r for r in support_matrix()["rows"]}
    assert all(r["status"] == "not_tested" for r in rows.values())
    claimed = support_matrix({"local-dev": {"status": "pass", "evidence_refs": []}})
    assert {r["profile"]: r["status"] for r in claimed["rows"]}["local-dev"] == "not_tested", (
        "pass without evidence refs is not supported"
    )
    real = support_matrix(
        {
            "local-dev": {
                "status": "pass",
                "evidence_refs": ["specs/013-amplai-v3/evidence-trace.jsonl"],
                "python_matrix": ["3.11.15"],
            },
            "managed-optional": {"status": "disabled"},
        }
    )
    statuses = {r["profile"]: r["status"] for r in real["rows"]}
    assert statuses["local-dev"] == "supported" and statuses["managed-optional"] == "disabled"


def test_shipped_matrix_file_is_in_sync(tmp_path):
    shipped = REPO / "deployment" / "support-matrix.json"
    assert shipped.is_file(), "deployment/support-matrix.json is a V3-054 deliverable"
    data = json.loads(shipped.read_text())
    statuses = {r["profile"]: r["status"] for r in data["rows"]}
    assert statuses["local-dev"] == "supported"
    assert all(
        s in {"supported", "not_tested", "experimental", "disabled"} for s in statuses.values()
    )
    regenerated = write_matrix(
        tmp_path / "m.json", json.loads((REPO / "deployment" / "support-evidence.json").read_text())
    )
    assert regenerated.read_text() == shipped.read_text(), (
        "shipped matrix is generated from support-evidence.json"
    )
