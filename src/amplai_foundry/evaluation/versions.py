"""`evaluator-version` records (Work 033 S2; interfaces.md §2.15, §7.8).

An evaluator version names the analysis and service code by digest. It is written only after a
passing requalification (`evaluator-requalification` with `all_equal`, written by
`scripts/evaluator_requalify.py`) and by a human actor that is not a proposer. Stage plans pin
it as `analysis-plan.policy.evaluator_version_ref`; `check_current` is the comparison the stage
runner uses to refuse a plan whose pinned version differs from the running code
(`EVALUATOR_CHANGED`, §7.8).

Digests (§2.15): `analysis_code_digest` is the sha256 of the bytes of `analysis.py` followed by
`sequential.py`; `service_code_digest` is the sha256 of the bytes of `service.py` followed by
`calibration.py` (the modules that freeze, run and calibrate). Both read the files of this
package as installed.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import now
from amplai_foundry.runtime.contracts.semantics import resolve_ref
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.storage.store import Scope, Store

VERSION = "eval-2"
KIND = "evaluator-version"
SCHEMA = "amplai.evaluator-version.v1"
REQUALIFICATION_KIND = "evaluator-requalification"
ANALYSIS_FILES = ("analysis.py", "sequential.py")
SERVICE_FILES = ("service.py", "calibration.py")
DIGEST_FIELDS = ("analysis_code_digest", "service_code_digest")
FIELDS = frozenset(
    {
        "schema",
        "scope",
        "version",
        "analysis_code_digest",
        "service_code_digest",
        "corpus_ref",
        "stage_templates",
        "requalification_ref",
        "approved_by",
        "at",
    }
)
_PACKAGE = Path(__file__).resolve().parent
_VERSION = re.compile(r"^eval-([1-9][0-9]{0,8})$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def files_digest(names: tuple[str, ...], *, root: Path = _PACKAGE) -> str:
    """sha256 over the bytes of `names` (files of this package) in the given order."""
    h = hashlib.sha256()
    for name in names:
        h.update((root / name).read_bytes())
    return "sha256:" + h.hexdigest()


def code_digests(*, root: Path = _PACKAGE) -> dict[str, str]:
    """The digests of the running evaluator code (§2.15)."""
    return {
        "analysis_code_digest": files_digest(ANALYSIS_FILES, root=root),
        "service_code_digest": files_digest(SERVICE_FILES, root=root),
    }


def version_id(version: str) -> str:
    """`eval-<n>` is stored as `evaluator-<n>` (§2.15)."""
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        raise RuntimeFault("EVALUATOR_VERSION", "An evaluator version is named eval-<n>")
    return "evaluator-" + match.group(1)


def _is_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "revision", "digest"}
        and isinstance(value["id"], str)
        and type(value["revision"]) is int
        and isinstance(value["digest"], str)
    )


def validate_version(value: dict[str, Any]) -> None:
    """§2.0: a new internal kind is validated before `put` (shape of §2.15)."""
    ok = (
        isinstance(value, dict)
        and set(value) == FIELDS
        and value["schema"] == SCHEMA
        and isinstance(value["scope"], dict)
        and isinstance(value["version"], str)
        and _VERSION.fullmatch(value["version"]) is not None
        and all(
            isinstance(value[k], str) and _DIGEST.fullmatch(value[k]) is not None
            for k in DIGEST_FIELDS
        )
        and _is_ref(value["corpus_ref"])
        and _is_ref(value["requalification_ref"])
        and isinstance(value["stage_templates"], dict)
        and isinstance(value["approved_by"], dict)
        and value["approved_by"].get("kind") == "human"
        and isinstance(value["at"], str)
    )
    if not ok:
        raise RuntimeFault("EVALUATOR_VERSION", "Invalid evaluator-version record")


def _latest(store: Store, scope: Scope, object_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [
        (ref, value) for ref, value in store.list_objects(scope, KIND) if ref["id"] == object_id
    ]
    if not rows:
        raise RuntimeFault("NOT_FOUND", "No evaluator-version with this id")
    ref = max(rows, key=lambda row: row[0]["revision"])[0]
    return ref, store.get(scope, KIND, ref)  # digest-checked read


def write_version(
    store: Store,
    actor: Actor,
    *,
    corpus_ref: dict[str, Any],
    requalification_ref: dict[str, Any],
    stage_templates: dict[str, Any] | None = None,
    version: str = VERSION,
) -> dict[str, Any]:
    """Write `version` with the digests of the running code after a passing requalification.

    - The approver is a human actor without `harness.propose` (`FORBIDDEN` otherwise).
    - `requalification_ref` must name an `evaluator-requalification` of this scope for `version`
      whose `all_equal` is true (`EVALUATOR_UNQUALIFIED` otherwise).
    - A stored `version` with other code digests is `EVALUATOR_CHANGED`: changed code is a new
      evaluator version (§11, D-105), never a silent rewrite. The same digests with the same
      inputs return the stored ref; other inputs are a new store revision of the same id (§2.0).
    """
    if actor.kind != "human" or "harness.propose" in actor.permissions:
        raise RuntimeFault("FORBIDDEN", "Only a human non-proposer actor approves an evaluator")
    scope = actor.scope
    object_id = version_id(version)
    kind, requal = resolve_ref(store, scope, requalification_ref)
    if (
        kind != REQUALIFICATION_KIND
        or requal.get("scope") != scope.wire()
        or requal.get("evaluator_version") != version
        or requal.get("all_equal") is not True
    ):
        raise Hold(
            "EVALUATOR_UNQUALIFIED",
            "An evaluator version needs a passing requalification of that version",
        )
    kind, _ = resolve_ref(store, scope, corpus_ref)
    if kind != "eval-corpus":
        raise RuntimeFault("EVALUATOR_VERSION", "corpus_ref names another record kind")
    templates = dict(stage_templates or {})
    digests = code_digests()
    revision = 1
    try:
        ref, stored = _latest(store, scope, object_id)
    except RuntimeFault as exc:
        if exc.code != "NOT_FOUND":
            raise
    else:
        if any(stored[k] != digests[k] for k in DIGEST_FIELDS):
            raise Hold(
                "EVALUATOR_CHANGED",
                f"{version} is stored with other code digests; changed code is a new version",
            )
        if (
            stored["corpus_ref"] == corpus_ref
            and stored["requalification_ref"] == requalification_ref
            and stored["stage_templates"] == templates
        ):
            return ref
        revision = ref["revision"] + 1
    value = {
        "schema": SCHEMA,
        "scope": scope.wire(),
        "version": version,
        **digests,
        "corpus_ref": corpus_ref,
        "stage_templates": templates,
        "requalification_ref": requalification_ref,
        "approved_by": actor.wire(),
        "at": now(),
    }
    validate_version(value)
    with store.tx() as db:
        return store.put(db, scope, KIND, object_id, revision, value)


def read_version(store: Store, scope: Scope, ref: dict[str, Any]) -> dict[str, Any]:
    """The validated record `ref` names (`EVALUATOR_UNQUALIFIED` for another kind)."""
    kind, value = resolve_ref(store, scope, ref)
    if kind != KIND:
        raise Hold("EVALUATOR_UNQUALIFIED", "The reference names another record kind")
    validate_version(value)
    return value


def check_current(value: dict[str, Any], *, root: Path = _PACKAGE) -> None:
    """`EVALUATOR_CHANGED` when the running code digests differ from the pinned version."""
    current = code_digests(root=root)
    if any(value.get(k) != current[k] for k in DIGEST_FIELDS):
        raise Hold(
            "EVALUATOR_CHANGED",
            f"The running evaluator code differs from the pinned {value.get('version')}",
        )


def current_version_ref(store: Store, scope: Scope) -> dict[str, Any] | None:
    """The latest stored evaluator version whose digests equal the running code, or None."""
    current = code_digests()
    latest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for ref, value in store.list_objects(scope, KIND):
        if ref["id"] not in latest or ref["revision"] > latest[ref["id"]][0]["revision"]:
            latest[ref["id"]] = (ref, value)
    matching = [
        (int(value["version"].split("-", 1)[1]), ref)
        for ref, value in latest.values()
        if isinstance(value.get("version"), str)
        and _VERSION.fullmatch(value["version"])
        and all(value.get(k) == current[k] for k in DIGEST_FIELDS)
    ]
    if not matching:
        return None
    _, ref = max(matching, key=lambda row: (row[0], row[1]["revision"]))
    store.get(scope, KIND, ref)  # digest-checked read
    return ref
