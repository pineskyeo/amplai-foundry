"""The active release pointer of the local product (D-089, S2).

A release-set is a signed, approved set of compositions (design-reference/design/16_META_HARNESS.md
§7). The product boots a baseline release from the installed compositions and points
``release-pointer/active`` at it. Promotion and rollback move that pointer (meta_harness
``promote``/``rollback``). New plans then select among the active release's compositions. A
running goal keeps the composition fixed in its plan.

Only a class-A difference is taken from a release. A released composition replaces an installed
one only when it is derived from it (``<composition_id>__<suffix>``) and uses the same driver,
model and sandbox profiles. Anything else in a release is ignored, so the pointer cannot change a
driver, a model or an image, nor move one app onto another app's composition.
"""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..contracts.identity import digest, sign
from ..errors import RuntimeFault
from ..storage.store import Scope, Store
from .codex import put_record

BASELINE_PREFIX = "local-baseline-"
POINTER_KIND, POINTER_ID = "release-pointer", "active"
CLASS_A_FIXED = ("driver_profile_ref", "model_profile_ref", "sandbox_profile_ref")
# a candidate derived from composition X is named X__<suffix>; only it may replace X
CANDIDATE_SEP = "__"


def build(
    store: Store,
    scope: Scope,
    contracts: Any,
    release_id: str,
    components: list[dict[str, Any]],
    *,
    signer: Ed25519PrivateKey,
    key_id: str,
    basis: str,
) -> dict[str, Any]:
    """A signed, approved configuration-only release of ``components`` (content-addressed)."""

    def put(kind: str, suffix: str, value: dict[str, Any]) -> dict[str, Any]:
        return put_record(store, scope, contracts, kind, f"{release_id}-{suffix}", value)

    matrix = put("qualification-matrix", "matrix", {
        "matrix_id": f"{release_id}-matrix", "status": "pass",
        "qualification_scope": basis,
        "checks": ["each component's driver qualification report (container_qualify.py)"],
    })  # fmt: skip
    inputs = put("release-inputs", "inputs", {
        "inputs_id": f"{release_id}-inputs", "platform_version": "3.0.0", "code_change": False,
        "composition_refs": components,
    })  # fmt: skip
    inventory = put("baseline-inventory", "inventory", {
        "inventory_id": f"{release_id}-inventory", "inputs_ref": inputs,
    })  # fmt: skip
    backup = put("migration-backup", "backup", {
        "backup_id": f"{release_id}-backup", "inputs_ref": inputs,
    })  # fmt: skip
    rollback = put("rollback-plan", "rollback", {
        "rollback_id": f"{release_id}-rollback", "restore_authority": False, "inputs_ref": inputs,
    })  # fmt: skip
    migration = put("migration-plan", "migration", {
        "schema_version": "3.0.0", "migration_id": f"{release_id}-migration",
        "scope": scope.wire(), "source_release_ref": inputs, "target_release_ref": inputs,
        "baseline_inventory_ref": inventory,
        "operations": [{
            "operation_id": "keep-code", "kind": "keep", "path": "release-inputs.json",
            "expected_old_digest": inputs["digest"], "replacement_ref": None,
            "backup_ref": backup, "required_test_ids": ["T-001"], "approval_required": True,
        }],
        "active_run_policy": "drain", "rollback_plan_ref": rollback, "dry_run_required": True,
    })  # fmt: skip
    sbom = put("sbom", "sbom", {
        "sbom_id": f"{release_id}-sbom", "components": ["AMPLAI V3 local product"],
    })  # fmt: skip
    provenance = put("provenance", "provenance", {
        "provenance_id": f"{release_id}-provenance", "test_deployment": False,
        "source": "local product configuration (no code change)",
    })  # fmt: skip
    release = {
        "schema_version": "3.0.0",
        "release_id": release_id,
        "platform_version": "3.0.0",
        "kit_version": "3.0.0",
        "protocol_major": 3,
        "contract_schema_version": "3.0.0",
        "source_revision": "local-product",
        "component_refs": components,
        "qualification_matrix_ref": matrix,
        "migration_plan_ref": migration,
        "sbom_ref": sbom,
        "provenance_ref": provenance,
        "status": "approved",
    }
    return put_record(store, scope, contracts, "release-set", release_id,
                      sign(release, key_id, signer))  # fmt: skip


def bootstrap(
    store: Store,
    scope: Scope,
    contracts: Any,
    compositions: list[dict[str, Any]],
    *,
    signer: Ed25519PrivateKey,
    key_id: str,
) -> dict[str, Any]:
    """Point ``active`` at the baseline of the installed compositions, unless a promoted
    (non-baseline) release is active. Returns the active release ref."""
    components = sorted(compositions, key=lambda r: (r["id"], r["revision"]))
    release_id = BASELINE_PREFIX + digest(components)[7:19]
    baseline = build(
        store, scope, contracts, release_id, components, signer=signer, key_id=key_id,
        basis="installed local product compositions; each qualified by its driver report",
    )  # fmt: skip
    with store.tx() as db:
        try:
            head = store.head(scope, POINTER_KIND, POINTER_ID, db=db)
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            store.cas(db, scope, POINTER_KIND, POINTER_ID, 0, "active",
                      {"release_ref": baseline})  # fmt: skip
            return baseline
        current: dict[str, Any] = head["data"]["release_ref"]
        if current == baseline or not current["id"].startswith(BASELINE_PREFIX):
            return current  # unchanged, or a promoted release stays active
        # the operator changed the installed configuration: the new baseline becomes active
        store.cas(
            db, scope, POINTER_KIND, POINTER_ID, head["row_version"], "active",
            {"release_ref": baseline, "previous_ref": current},
        )  # fmt: skip
        store.event(db, scope, "release", release_id, "release.baseline_rebased",
                    {"release_ref": baseline, "previous_ref": current})  # fmt: skip
        return baseline


def effective(
    store: Store, scope: Scope, installed: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The installed compositions by driver id, with class-A replacements from the active
    release (same driver, model and sandbox profile)."""
    try:
        pointer = store.head(scope, POINTER_KIND, POINTER_ID)
    except RuntimeFault as exc:
        if exc.code != "NOT_FOUND":
            raise
        return dict(installed)
    release = store.get(scope, "release-set", pointer["data"]["release_ref"])
    released = [store.get(scope, "harness-composition", r) for r in release["component_refs"]]
    out = dict(installed)
    for driver_id, ref in installed.items():
        base = store.get(scope, "harness-composition", ref)
        for component_ref, value in zip(release["component_refs"], released, strict=True):
            if (
                component_ref != ref
                and value["composition_id"].startswith(base["composition_id"] + CANDIDATE_SEP)
                and all(value[k] == base[k] for k in CLASS_A_FIXED)
            ):
                out[driver_id] = component_ref
                break
    return out
