"""Deployment profiles and activation prerequisites (design/20 §1-§3, design/31, V3-054).

A profile is a declared operating shape. Activation of *write* work needs every
prerequisite in ``contracts/runtime-defaults.json:required_activation_settings`` supplied
with a real value; a placeholder keeps the deployment read-only (T-112). No profile
falls back to a cloud route the policy did not allow (T-075), and none is marked
"supported" without a matrix entry that names actual evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.contracts.identity import now
from amplai_foundry.runtime.errors import Hold, RuntimeFault

PROFILES: dict[str, dict[str, Any]] = {
    "local-dev": {
        "control_plane": "modern Python>=3.11 local disk",
        "worker": "isolated CLI/session",
        "egress_default": "deny_external",
        "cloud_routes_allowed": False,
        "offline_read_only_allowed": True,
        "required_evidence": ["baseline driver conformance", "state/fault suite"],
    },
    "onprem": {
        "control_plane": "internal modern host, local DB/CAS",
        "worker": "local API or approved CLI worker",
        "egress_default": "deny_external",
        "cloud_routes_allowed": False,
        "offline_read_only_allowed": True,
        "required_evidence": ["internal auth/secret/backup/fleet test"],
    },
    "hybrid": {
        "control_plane": "onprem truth/authority",
        "worker": "approved cloud or local route",
        "egress_default": "policy_by_classification",
        "cloud_routes_allowed": True,
        "offline_read_only_allowed": True,
        "required_evidence": ["both routes conformance", "privacy policy"],
    },
    "offline": {
        "control_plane": "pinned local release + cached canonical",
        "worker": "qualified local model only",
        "egress_default": "deny_all",
        "cloud_routes_allowed": False,
        "offline_read_only_allowed": True,
        "required_evidence": ["cold start", "cache staleness", "deny tests"],
    },
    "legacy-app-client": {
        "control_plane": "separate modern CP",
        "worker": "thin client/installer on the old host",
        "egress_default": "deny_external",
        "cloud_routes_allowed": False,
        "offline_read_only_allowed": True,
        "required_evidence": ["actual OS/Python smoke", "uninstall independence (T-098)"],
    },
    "managed-optional": {
        "control_plane": "same Foundry authority",
        "worker": "managed API profile",
        "egress_default": "policy_by_classification",
        "cloud_routes_allowed": True,
        "offline_read_only_allowed": True,
        "required_evidence": ["exact provider capability/exit/effect tests"],
    },
}
PLACEHOLDER_MARKERS = ("<", "TODO", "CHANGEME", "example", "placeholder", "fixture")
# Data classes that may leave the host on a cloud route without a per-class policy entry.
CLOUD_DEFAULT_ALLOWED = frozenset({"public"})


def _is_placeholder(value: object) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return True
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return any(marker in text for marker in PLACEHOLDER_MARKERS)


class DeploymentProfile:
    def __init__(self, defaults_path: str | Path | None = None) -> None:
        root = (
            Path(defaults_path)
            if defaults_path
            else Path(__file__).parent / "contracts" / "data" / "runtime-defaults.json"
        )
        self.defaults = json.loads(Path(root).read_text())
        self.required: list[str] = list(self.defaults["required_activation_settings"])

    def describe(self, name: str) -> dict[str, Any]:
        if name not in PROFILES:
            raise RuntimeFault("PROFILE_UNKNOWN", f"Unknown deployment profile {name}")
        return {"profile": name, **PROFILES[name]}

    # -- activation ---------------------------------------------------------------
    def activation(self, name: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Read-only status is always allowed; write activation is fail-closed."""
        self.describe(name)
        missing = [k for k in self.required if _is_placeholder(settings.get(k))]
        unknown = sorted(
            set(settings) - set(self.required) - {"cloud_data_policy", "python_version"}
        )
        result = {
            "profile": name,
            "read_only_status": "allowed",
            "write_activation": "allowed" if not missing else "hold",
            "missing_prerequisites": missing,
            "unknown_settings": unknown,
            "evaluated_at": now(),
        }
        if missing:
            result["code"] = "ACTIVATION_PREREQUISITES"
        return result

    def require_write(self, name: str, settings: dict[str, Any]) -> dict[str, Any]:
        status = self.activation(name, settings)
        if status["write_activation"] != "allowed":
            raise Hold(
                "ACTIVATION_PREREQUISITES",
                "Write activation is fail-closed until every prerequisite is supplied",
                details={"missing": status["missing_prerequisites"]},
            )
        return status

    # -- routing ------------------------------------------------------------------
    def route(
        self,
        name: str,
        *,
        classification: str,
        local_available: bool,
        cloud_data_policy: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Model/provider route decision without a silent cloud fallback (T-075)."""
        profile = self.describe(name)
        if local_available:
            return {"route": "local", "classification": classification}
        if not profile["cloud_routes_allowed"]:
            raise Hold(
                "CLOUD_FALLBACK_PROHIBITED",
                "Local provider unavailable and this profile allows no cloud route",
                details={"profile": name, "classification": classification},
            )
        policy = {c: True for c in CLOUD_DEFAULT_ALLOWED}
        policy.update(cloud_data_policy or {})
        if not policy.get(classification, False):
            raise Hold(
                "CLOUD_DATA_CLASS",
                "This data class is not approved for the cloud route; no unapproved transmission",
                details={"profile": name, "classification": classification},
            )
        return {"route": "cloud", "classification": classification, "approved_by_policy": True}


def support_matrix(evidence: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Every combination is not-tested until actual evidence names it (design/31 §4)."""
    evidence = evidence or {}
    rows = []
    for name, profile in PROFILES.items():
        entry = evidence.get(name)
        status = "not_tested"
        if entry:
            if entry.get("status") == "pass" and entry.get("evidence_refs"):
                status = "supported"
            elif entry.get("status") == "experimental":
                status = "experimental"
            elif entry.get("status") == "disabled":
                status = "disabled"
        rows.append(
            {
                "profile": name,
                "status": status,
                "required_evidence": profile["required_evidence"],
                "evidence_refs": (entry or {}).get("evidence_refs", []),
                "python_matrix": (entry or {}).get("python_matrix", []),
            }
        )
    return {"schema_version": "3.0.0", "generated_at": now(), "rows": rows}


def write_matrix(path: str | Path, evidence: dict[str, dict[str, Any]] | None = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    matrix = support_matrix(evidence)
    matrix.pop("generated_at")
    target.write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + "\n")
    return target
