"""Evidence hot/cold archive with a live-ref index, hash-preserving moves and tombstones.

design/12 §7, design/19 §4, T-095. The default is report-only: ``plan`` finds live
references, ``move`` copies bytes to the cold tier under the same digest and records
an alias, and ``purge_proposal`` never deletes. Anything an active ContextBundle,
contract or experiment still references stays hot and keeps a backlink.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import canonical, digest, digest_bytes, new_id, now
from amplai_foundry.runtime.contracts.semantics import walk_refs
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.evidence.cas import ArtifactStore
from amplai_foundry.runtime.storage.store import Scope, Store

Ref = dict[str, Any]

# Object kinds whose references keep evidence hot (design/09 §7: active contract/context/
# release/experiment referenced digests are excluded from GC).
LIVE_KINDS: tuple[str, ...] = (
    "context-bundle",
    "goal-contract",
    "verification-plan",
    "eval-experiment",
    "eval-report",
    "release-set",
    "evidence",
    "verdict",
)


class EvidenceArchiveService:
    def __init__(
        self,
        store: Store,
        artifacts: ArtifactStore,
        approval_check: Callable[[Scope, Ref, str, str], Any] | None = None,
    ) -> None:
        self.store, self.artifacts = store, artifacts
        # Same contract as CutoverService: (scope, decision_ref, action, subject_digest).
        self.approval_check = approval_check

    # -- live reference index -----------------------------------------------------
    def live_refs(self, scope: Scope) -> dict[str, list[dict[str, Any]]]:
        """digest → objects that reference it (only kinds that keep evidence hot)."""
        index: dict[str, list[dict[str, Any]]] = {}
        for kind in LIVE_KINDS:
            for ref, value in self.store.list_objects(scope, kind):
                for target in walk_refs(value):
                    index.setdefault(target["digest"], []).append(
                        {"kind": kind, "id": ref["id"], "revision": ref["revision"]}
                    )
                for artifact in self._artifact_digests(value):
                    index.setdefault(artifact, []).append(
                        {"kind": kind, "id": ref["id"], "revision": ref["revision"]}
                    )
        return index

    @staticmethod
    def _artifact_digests(value: object) -> list[str]:
        out: list[str] = []
        if isinstance(value, dict):
            if set(value) >= {"id", "digest", "media_type", "size_bytes"}:
                out.append(str(value["digest"]))
            for child in value.values():
                out.extend(EvidenceArchiveService._artifact_digests(child))
        elif isinstance(value, list):
            for child in value:
                out.extend(EvidenceArchiveService._artifact_digests(child))
        return out

    # -- plan (report-only) --------------------------------------------------------
    def plan(self, scope: Scope, artifact_refs: list[Ref]) -> dict[str, Any]:
        index = self.live_refs(scope)
        items: list[dict[str, Any]] = []
        for ref in artifact_refs:
            self.artifacts.read(scope, ref)  # scope + digest verified
            holders = index.get(ref["digest"], [])
            items.append(
                {
                    "artifact": ref,
                    "live_refs": holders,
                    "temperature": "hot" if holders else "cold_candidate",
                    "action": "keep_hot_with_backlink" if holders else "move_to_cold",
                    "safety": "HUMAN_GATED" if holders else "EVIDENCE_REQUIRED",
                }
            )
        return {
            "schema_version": "3.0.0",
            "plan_id": new_id("archive-plan"),
            "scope": scope.wire(),
            "items": items,
            "mode": "report_only",
            "deletes": 0,
            "created_at": now(),
        }

    # -- move (hash preserving) ----------------------------------------------------
    def move(self, actor: Actor, plan: dict[str, Any], destination: str | Path) -> dict[str, Any]:
        actor.require("evidence.archive")
        if plan.get("scope") != actor.scope.wire():
            raise RuntimeFault("SCOPE_MISMATCH", "Archive plan belongs to another scope")
        root = Path(destination).absolute()
        if root.is_symlink():
            raise Hold("ARCHIVE_PATH", "Cold archive cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        index = self.live_refs(actor.scope)
        moved: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for item in plan["items"]:
            ref = item["artifact"]
            holders = index.get(ref["digest"], [])
            if holders:
                # Re-checked at move time: a new live reference keeps it hot (T-095).
                kept.append({"artifact": ref, "live_refs": holders})
                continue
            raw = self.artifacts.read(actor.scope, ref)
            target = root / ref["digest"][7:]
            if target.is_symlink():
                raise Hold("ARCHIVE_PATH", "Archive object cannot be a symlink")
            if target.exists():
                if digest_bytes(target.read_bytes()) != ref["digest"]:
                    raise Hold("ARCHIVE_CORRUPT", "Cold archive object differs")
            else:
                with open(target, "xb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
            alias_id = (
                "alias-" + digest({"scope": actor.scope.wire(), "digest": ref["digest"]})[7:31]
            )
            existing = [
                (r, v)
                for r, v in self.store.list_objects(actor.scope, "evidence-alias")
                if r["id"] == alias_id
            ]
            if existing:
                # Hash-preserving move is repeatable: the alias already records this object.
                moved.append(max(existing, key=lambda pair: pair[0]["revision"])[1])
                continue
            alias = {
                "alias_id": alias_id,
                "scope": actor.scope.wire(),
                "artifact": ref,
                "cold_path": str(target),
                "hot_retained": True,
                "tombstone": None,
                "moved_at": now(),
            }
            with self.store.tx() as db:
                self.store.put(db, actor.scope, "evidence-alias", alias["alias_id"], 1, alias)
                self.store.event(
                    db,
                    actor.scope,
                    "release",
                    plan["plan_id"],
                    "evidence.archived",
                    {"artifact": ref, "cold_path": str(target), "hot_deleted": False},
                )
            moved.append(alias)
        manifest = {
            "archive_id": new_id("archive"),
            "plan_id": plan["plan_id"],
            "scope": actor.scope.wire(),
            "moved": moved,
            "kept_hot": kept,
            "hot_source_deleted": False,
            "created_at": now(),
        }
        (root / (manifest["archive_id"] + ".json")).write_bytes(canonical(manifest))
        return manifest

    # -- resolve through alias ------------------------------------------------------
    def resolve(self, scope: Scope, ref: Ref) -> dict[str, Any]:
        """An old pinned ref still resolves: hot bytes, or the cold copy via its alias."""
        try:
            raw = self.artifacts.read(scope, ref)
            return {"source": "hot", "bytes": raw}
        except RuntimeFault as hot_error:
            for _alias_ref, alias in self.store.list_objects(scope, "evidence-alias"):
                if alias["artifact"]["digest"] == ref["digest"]:
                    if alias.get("tombstone"):
                        raise Hold(
                            "EVIDENCE_TOMBSTONED",
                            "Evidence was purged under an approved decision; "
                            "only the tombstone remains",
                            details={"tombstone": alias["tombstone"]},
                        ) from hot_error
                    cold = Path(alias["cold_path"])
                    if cold.is_file() and digest_bytes(cold.read_bytes()) == ref["digest"]:
                        return {
                            "source": "cold",
                            "bytes": cold.read_bytes(),
                            "alias": alias["alias_id"],
                        }
            raise

    # -- purge is a proposal ---------------------------------------------------------
    def purge_proposal(self, actor: Actor, plan: dict[str, Any]) -> dict[str, Any]:
        actor.require("evidence.archive")
        index = self.live_refs(actor.scope)
        blocked = [i for i in plan["items"] if index.get(i["artifact"]["digest"])]
        return {
            "status": "proposal_only",
            "plan_id": plan["plan_id"],
            "blocked_by_live_refs": [
                {"artifact": i["artifact"], "live_refs": index[i["artifact"]["digest"]]}
                for i in blocked
            ],
            "requires": [
                "legal_hold_check",
                "reachability_check",
                "retention_decision",
                "verified_cold_copy",
                "destructive_change human gate",
            ],
            "deleted": False,
        }

    def tombstone(self, actor: Actor, alias_id: str, *, decision_ref: Ref) -> dict[str, Any]:
        """Record an approved purge as a tombstone; bytes are not removed here."""
        actor.require("evidence.archive")
        latest = None
        for ref, alias in self.store.list_objects(actor.scope, "evidence-alias"):
            if ref["id"] == alias_id and (
                latest is None or ref["revision"] > latest[0]["revision"]
            ):
                latest = (ref, alias)
        if latest is None:
            raise RuntimeFault("NOT_FOUND", "Unknown evidence alias")
        index = self.live_refs(actor.scope)
        if index.get(latest[1]["artifact"]["digest"]):
            raise Hold(
                "EVIDENCE_LIVE",
                "Live references block the tombstone",
                details={"live_refs": index[latest[1]["artifact"]["digest"]]},
            )
        if self.approval_check is None:
            raise Hold(
                "TOMBSTONE_APPROVAL_UNAVAILABLE",
                "A tombstone needs an independent destructive_change approval check",
            )
        subject = digest({"alias_id": alias_id, "artifact_digest": latest[1]["artifact"]["digest"]})
        self.approval_check(actor.scope, decision_ref, "destructive_change", subject)
        value = {
            **latest[1],
            "tombstone": {
                "decision_ref": decision_ref,
                "recorded_at": now(),
                "by": actor.subject_id,
            },
        }
        with self.store.tx() as db:
            return self.store.put(
                db, actor.scope, "evidence-alias", alias_id, latest[0]["revision"] + 1, value
            )


def gardening_report(scope: Scope, plan: dict[str, Any]) -> dict[str, Any]:
    """Report-only view in the shape .ai-team/policy/gardening.json expects."""
    candidates = [
        {
            "path": i["artifact"]["digest"],
            "candidate_type": "evidence_archive",
            "safety": i["safety"],
            "evidence": {"live_refs": i["live_refs"], "reference_scan": "complete"},
            "action": i["action"],
            "auto_apply": False,
        }
        for i in plan["items"]
    ]
    return {
        "scope": scope.wire(),
        "mode": "report_only",
        "candidates": candidates,
        "human_gated_not_deleted": all(c["auto_apply"] is False for c in candidates),
        "generated_at": now(),
    }
