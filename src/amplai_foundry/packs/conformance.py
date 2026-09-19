"""Pack conformance (design/13 §3, design/14): interface + verifier definitions + eval cases.

A pack ships *definitions* (templates) for verifier profiles; the installer binds
environment and owner at install time and the result must validate against
``verifier-profile.schema.json``. Runners a host does not provide leave the pack
``disabled_unqualified`` for optional definitions and fail closed for required ones.
Nothing here executes a verifier or approves a product.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

from amplai_foundry.runtime.errors import Hold, RuntimeFault

Ref = dict[str, Any]

# Runner id → verifier kind it implements. Availability is decided per host at install.
RUNNERS: dict[str, str] = {
    "json_schema": "schema",
    "structured_command": "deterministic",
    "visual_render": "artifact_visual",
    "document_freshness": "artifact_visual",
    "artifact_inspection": "artifact_visual",
    "model_critique": "model_assisted",
    "human_review": "human",
    "shacl": "schema",
}
DEFAULT_RUNNERS: frozenset[str] = frozenset(
    {
        "json_schema",
        "structured_command",
        "document_freshness",
        "artifact_inspection",
        "human_review",
    }
)
DEFINITION_FIELDS = {
    "definition_id",
    "version",
    "kind",
    "runner",
    "allowed_command_ids",
    "required_capabilities",
    "protected",
    "golden_governance",
    "optional",
    "notes",
}
CASE_FIELDS = {"case_id", "title", "given", "when", "expected", "verifier_definition", "split"}
INTERFACE_FIELDS = {
    "pack_id",
    "role",
    "inputs",
    "outputs",
    "not_core",
    "auto_approval",
    "promotes_to_canonical",
    "core_coupling",
    "human_gate",
}
EFFECT_RANK = {
    "pure_read": 0,
    "sandbox_write": 1,
    "external_idempotent_write": 2,
    "external_nonidempotent_write": 3,
    "production_control": 4,
}


def _load_json(files: dict[str, bytes], name: str) -> Any:
    try:
        return json.loads(files[name])
    except ValueError as exc:
        raise RuntimeFault("PACK_JSON", f"{name} is not valid JSON") from exc


def _capability_within(cap: dict[str, str], granted: list[dict[str, str]]) -> bool:
    for g in granted:
        same_action = g["action"] == cap["action"]
        resource_ok = g["resource"] == cap["resource"] or (
            g["resource"].endswith("*") and cap["resource"].startswith(g["resource"][:-1])
        )
        if (
            same_action
            and resource_ok
            and EFFECT_RANK[cap["effect_class"]] <= EFFECT_RANK[g["effect_class"]]
        ):
            return True
    return False


class PackConformance:
    def __init__(
        self, contracts: Any, *, installed_runners: Collection[str] = DEFAULT_RUNNERS
    ) -> None:
        self.contracts = contracts
        self.installed = frozenset(installed_runners)
        unknown = sorted(self.installed - set(RUNNERS))
        if unknown:
            raise RuntimeFault("PACK_RUNNER_UNKNOWN", "Unknown runner ids: " + ", ".join(unknown))

    # -- pieces -------------------------------------------------------------------
    def interface(self, manifest: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any]:
        if "context/interface.json" not in files:
            raise Hold("PACK_INTERFACE_MISSING", "Every pack declares context/interface.json")
        interface = _load_json(files, "context/interface.json")
        if not isinstance(interface, dict) or set(interface) != INTERFACE_FIELDS:
            raise Hold(
                "PACK_INTERFACE_FIELDS",
                "interface.json must carry exactly the declared fields",
                details={"expected": sorted(INTERFACE_FIELDS)},
            )
        if interface["pack_id"] != manifest["pack_id"]:
            raise Hold("PACK_INTERFACE_ID", "interface.json pack_id differs from the manifest")
        if interface["auto_approval"] is not False:
            raise Hold(
                "PACK_AUTO_APPROVAL",
                "A pack never approves a product by itself; approval is a verdict or a human gate",
            )
        if interface["promotes_to_canonical"] is not False:
            raise Hold(
                "PACK_CANONICAL_PROMOTION",
                "Pack results are candidates; canonical promotion stays with governance",
            )
        if interface["core_coupling"] is not False:
            raise Hold("PACK_CORE_COUPLING", "Domain packs must not couple into the core runtime")
        return interface

    def definitions(
        self, manifest: dict[str, Any], files: dict[str, bytes]
    ) -> dict[str, dict[str, Any]]:
        names = sorted(
            n for n in files if n.startswith("verifiers/definitions/") and n.endswith(".json")
        )
        out: dict[str, dict[str, Any]] = {}
        for name in names:
            definition = _load_json(files, name)
            if not isinstance(definition, dict) or set(definition) != DEFINITION_FIELDS:
                raise Hold(
                    "PACK_VERIFIER_FIELDS",
                    f"{name} must carry exactly the definition fields",
                    details={"expected": sorted(DEFINITION_FIELDS)},
                )
            runner = definition["runner"]
            if runner not in RUNNERS:
                raise Hold("PACK_RUNNER_UNKNOWN", f"{name}: unknown runner {runner}")
            if RUNNERS[runner] != definition["kind"]:
                raise Hold(
                    "PACK_VERIFIER_KIND",
                    f"{name}: runner {runner} implements {RUNNERS[runner]}, "
                    f"not {definition['kind']}",
                )
            if definition["kind"] == "deterministic" and not definition["allowed_command_ids"]:
                raise Hold(
                    "PACK_VERIFIER_COMMANDS",
                    f"{name}: deterministic verifiers need an explicit command allowlist",
                )
            if definition["kind"] == "artifact_visual" and definition["golden_governance"] not in {
                "human_approved_baseline",
                "none",
            }:
                raise Hold(
                    "PACK_GOLDEN_GOVERNANCE",
                    f"{name}: golden baselines are human approved revisions or absent",
                )
            for cap in definition["required_capabilities"]:
                if not _capability_within(cap, manifest["permissions"]):
                    raise Hold(
                        "PACK_VERIFIER_CAPABILITY",
                        f"{name}: verifier needs a capability the pack did not request",
                        details={"capability": cap},
                    )
            if definition["definition_id"] in out:
                raise Hold(
                    "PACK_VERIFIER_DUPLICATE", f"duplicate definition {definition['definition_id']}"
                )
            out[definition["definition_id"]] = definition
        return out

    def cases(
        self, files: dict[str, bytes], definitions: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        names = sorted(n for n in files if n.startswith("eval/cases/") and n.endswith(".json"))
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for name in names:
            case = _load_json(files, name)
            if not isinstance(case, dict) or set(case) != CASE_FIELDS:
                raise Hold("PACK_CASE_FIELDS", f"{name} must carry exactly the case fields")
            if case["case_id"] in seen:
                raise Hold("PACK_CASE_DUPLICATE", f"duplicate case {case['case_id']}")
            if case["split"] not in {"dev", "validation", "holdout"}:
                raise Hold("PACK_CASE_SPLIT", f"{name}: split must be dev/validation/holdout")
            if case["verifier_definition"] not in definitions:
                raise Hold(
                    "PACK_CASE_VERIFIER",
                    f"{name}: case binds an unknown verifier definition",
                    details={"verifier_definition": case["verifier_definition"]},
                )
            seen.add(case["case_id"])
            out.append(case)
        return out

    def materialize(
        self,
        definition: dict[str, Any],
        *,
        environment_ref: Ref,
        owner_subject_id: str,
        tool_refs: list[Ref] | None = None,
    ) -> dict[str, Any]:
        """Bind a definition into a verifier-profile object; validated against the schema."""
        profile = {
            "schema_version": "3.0.0",
            "profile_id": definition["definition_id"],
            "version": definition["version"],
            "kind": definition["kind"],
            "tool_refs": tool_refs or [],
            "allowed_command_ids": list(definition["allowed_command_ids"]),
            "required_capabilities": list(definition["required_capabilities"]),
            "environment_ref": environment_ref,
            "rubric_ref": None,
            "golden_refs": [],
            "protected": bool(definition["protected"]),
            "owner_subject_id": owner_subject_id,
        }
        self.contracts.validate("verifier-profile", profile)
        return profile

    # -- report -------------------------------------------------------------------
    def check(self, manifest: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any]:
        interface = self.interface(manifest, files)
        definitions = self.definitions(manifest, files)
        cases = self.cases(files, definitions)
        placeholder = {
            "id": "environment-at-install",
            "revision": 1,
            "digest": "sha256:" + "0" * 64,
        }
        runners: dict[str, str] = {}
        disabled: list[str] = []
        for definition_id, definition in definitions.items():
            self.materialize(definition, environment_ref=placeholder, owner_subject_id="installer")
            available = definition["runner"] in self.installed
            if available:
                runners[definition_id] = "available"
            elif definition["optional"]:
                runners[definition_id] = "disabled_unqualified"
                disabled.append(definition_id)
            else:
                raise Hold(
                    "PACK_RUNNER_UNAVAILABLE",
                    "A required verifier runner is not installed on this host; not a skipped PASS",
                    details={"definition_id": definition_id, "runner": definition["runner"]},
                )
        return {
            "pack_id": manifest["pack_id"],
            "role": interface["role"],
            "definitions": sorted(definitions),
            "cases": [c["case_id"] for c in cases],
            "runners": runners,
            "disabled_unqualified": disabled,
            "human_gate": interface["human_gate"],
            "status": "conformant",
            "qualification": (
                "contract conformance of install/permission/definition/case; "
                "not engine completeness"
            ),
        }
