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

from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..contracts.identity import digest, sign
from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store
from .codex import put_record

if TYPE_CHECKING:
    from .product import InstalledApp

BASELINE_PREFIX = "local-baseline-"
POINTER_KIND, POINTER_ID = "release-pointer", "active"
CLASS_A_FIXED = ("driver_profile_ref", "model_profile_ref", "sandbox_profile_ref")
# a candidate derived from composition X is named X__<suffix>; only it may replace X
CANDIDATE_SEP = "__"
# IC-12 (Work 033 S7b): the environment sibling of installed composition X in task environment E
# is named X:env-<env12>[__<suffix>]. interfaces.md §3.2 writes "<installed id>@env-<env12>", but
# "@" is outside the record id pattern ($defs.id, ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$), so ":"
# separates it; "." is not used because model slugs contain it (codex.py model ids).
ENV_SEP = ":env-"
APP_ENVIRONMENT = "app"  # a corpus task's environment when it runs in its app's own image (§10.2)


def env12(environment_id: str) -> str:
    """The 12 hex digits naming a task environment in sibling ids (digest of its id)."""
    return digest(environment_id)[7:19]


def env_composition_id(installed_id: str, environment_id: str) -> str:
    """The id of the installed environment sibling of composition ``installed_id`` (§3.2)."""
    return installed_id + ENV_SEP + env12(environment_id)


def env_suite_id(app_id: str, tag: str) -> str:
    """The id of app ``app_id``'s suite verifier profile in the task environment whose env12 is
    ``tag`` (``LocalExecutionService.install``, §10.5 step 6)."""
    return f"{app_id}-suite{ENV_SEP}{tag}"


def latest(store: Store, scope: Scope, kind: str, object_id: str) -> dict[str, Any] | None:
    """The newest stored revision ref of ``object_id`` (None when there is none)."""
    refs = [r for r, _ in store.list_objects(scope, kind) if r["id"] == object_id]
    return max(refs, key=lambda r: r["revision"]) if refs else None


def env_installed(
    store: Store, scope: Scope, installed_id: str, tag: str, environment_ref: dict[str, Any]
) -> bool:
    """Whether the app of installed composition ``installed_id`` installed the task environment
    ``tag`` (env12) in ``environment_ref`` (IC-12, §10.5 step 6).

    A ``harness-composition`` record named ``<installed id>:env-<tag>`` is no such proof: any
    writer of compositions can put one (``manifest.put_composition`` applies the profile checks
    only). The proof is the app's newest ``app-binding`` revision, which only
    ``GoalService.apps.register`` writes (permission ``app.register``,
    ``runtime/goals/service.py:27-44``) and which ``LocalExecutionService.install`` writes with
    every task environment and its suite verifier profile (§10.5 step 6): it must list
    ``environment_ref`` in ``environment_refs`` and the suite profile ``<app>-suite:env-<tag>``
    whose ``environment_ref`` it is. The app is the one whose id plus ``-`` begins
    ``installed_id`` (``LocalExecutionService._composition_id``)."""
    newest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for ref, binding in store.list_objects(scope, "app-binding"):
        app_id = str(binding.get("app_id", ""))
        if not app_id or not installed_id.startswith(app_id + "-"):
            continue
        if app_id not in newest or ref["revision"] > newest[app_id][0]["revision"]:
            newest[app_id] = (ref, binding)
    for app_id, (_, binding) in newest.items():
        if environment_ref not in (binding.get("environment_refs") or []):
            continue
        for profile_ref in binding.get("verifier_profile_refs") or []:
            if profile_ref.get("id") != env_suite_id(app_id, tag):
                continue
            profile = store.get(scope, "verifier-profile", profile_ref)
            if profile.get("environment_ref") == environment_ref:
                return True
    return False


def installed_env_of(
    store: Store, scope: Scope, installed_id: str, value: dict[str, Any]
) -> dict[str, Any] | None:
    """The installed environment composition (latest revision) whose sibling ``value`` is, or
    None. ``value`` is named ``<installed_id>:env-<env12>[__<suffix>]``; the composition of that
    name counts only when the app installed its task environment in its sandbox
    (``env_installed``), so a record merely named like it is no installed one."""
    name = str(value.get("composition_id", ""))
    if not name.startswith(installed_id + ENV_SEP):
        return None
    tag = name[len(installed_id + ENV_SEP) :].split(CANDIDATE_SEP, 1)[0]
    if len(tag) != 12 or any(c not in "0123456789abcdef" for c in tag):
        return None
    ref = latest(store, scope, "harness-composition", installed_id + ENV_SEP + tag)
    if ref is None:
        return None
    found: dict[str, Any] = store.get(scope, "harness-composition", ref)
    if not env_installed(store, scope, installed_id, tag, found["sandbox_profile_ref"]):
        return None
    return found


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
    """The installed compositions by cell id (the legacy cell id is the driver id, IC-07), with
    class-A replacements from the active release (same driver, model and sandbox profile)."""
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


def pin_allowed(
    store: Store, scope: Scope, installed: dict[str, dict[str, Any]], ref: dict[str, Any]
) -> str | None:
    """The cell id whose installed composition ``ref`` is, or is a candidate of (§3.3).

    A candidate is named ``<installed id>__<suffix>`` and keeps the driver, model and sandbox
    profiles of the composition it derives from. With one installed composition per cell this
    also admits a cell sibling (``ManifestService.cell_sibling``: the cell's installed id plus
    the source suffix, the cell's own profiles).

    IC-12 (Work 033 S7b): an environment sibling ``<installed id>:env-<env12>[__<suffix>]`` is
    admitted for the cell when the installed environment composition of that name exists (the
    product installs it from the task environment's qualified records, and the app's newest
    app-binding lists that environment and its suite profile, ``env_installed``) and the sibling
    keeps its driver, model and sandbox profiles. Anything else is None.
    """
    try:
        value = store.get(scope, "harness-composition", ref)
    except RuntimeFault:
        return None
    for driver_id, base_ref in installed.items():
        if ref == base_ref:
            return driver_id
        base = store.get(scope, "harness-composition", base_ref)
        if value["composition_id"].startswith(base["composition_id"] + CANDIDATE_SEP) and all(
            value[k] == base[k] for k in CLASS_A_FIXED
        ):
            return driver_id
        env = installed_env_of(store, scope, base["composition_id"], value)
        if env is not None and all(value[k] == env[k] for k in CLASS_A_FIXED):
            return driver_id
    return None


class_a_driver = pin_allowed  # the Work 030 name stays for its callers (§3.3)


def router_ref(store: Store, scope: Scope, installed: InstalledApp) -> dict[str, Any]:
    """The ``router_policy_ref`` of the app's effective compositions (§3.3).

    Hold ROUTER_POLICY without a router (as ``select_composition`` did), ROUTER_INCONSISTENT when
    the effective compositions name different routers.

    A promoted class-A candidate is a copy of the composition it derives from, router included.
    When the installed router changes afterwards (a cell added or removed changes the app's
    route order, ``product._baseline_carriers``), the candidate still names the old one. This
    holds rather than preferring either router; the details name the promoted release and the
    remedy. Rebasing candidates or skipping them is an open decision (S4 review, not taken here).
    """
    if installed.router_ref is None:
        raise Hold("ROUTER_POLICY", "The app has no router policy")
    refs = {
        digest(value): value
        for value in (
            store.get(scope, "harness-composition", ref).get("router_policy_ref")
            for ref in effective(store, scope, installed.compositions).values()
        )
        if value is not None
    }
    if len(refs) > 1:
        details: dict[str, Any] = {"routers": sorted(v["id"] for v in refs.values())}
        try:
            release = store.head(scope, POINTER_KIND, POINTER_ID)["data"]["release_ref"]
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
            release = None
        if release is not None and not release["id"].startswith(BASELINE_PREFIX):
            details["active_release"] = release["id"]
            details["remedy"] = (
                "the promoted release names the router installed before the configuration "
                "changed; amplai meta rollback <proposal> returns to the installed compositions"
            )
        raise Hold(
            "ROUTER_INCONSISTENT", "The app's compositions name different routers",
            details=details,
        )  # fmt: skip
    ref: dict[str, Any] = next(iter(refs.values()), installed.router_ref)
    return ref
