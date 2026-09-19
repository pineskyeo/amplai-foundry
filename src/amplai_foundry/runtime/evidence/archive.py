"""Hot/cold evidence indexing and explicit content-preserving archive copies."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..contracts.authority import Actor
from ..contracts.identity import canonical, digest, digest_bytes, new_id, now
from ..errors import Hold, RuntimeFault
from ..storage.store import Scope, Store
from .cas import ArtifactStore


class EvidenceArchive:
    def __init__(self, store: Store, artifacts: ArtifactStore) -> None:
        self.store, self.artifacts = store, artifacts

    def classify(self, scope: Scope, ref: dict[str, Any], *, role: str) -> None:
        if role not in {"canonical", "active", "completed", "evidence_archive"}:
            raise RuntimeFault("KNOWLEDGE_TEMPERATURE", "Unknown context visibility class")
        from ..contracts.semantics import resolve_ref

        resolve_ref(self.store, scope, ref)
        with self.store.tx() as db:
            oid = digest(ref)[7:]
            try:
                h = self.store.head(scope, "visibility", oid, db=db)
            except RuntimeFault as e:
                if e.code != "NOT_FOUND":
                    raise
                h = {"row_version": 0}
            self.store.cas(
                db, scope, "visibility", oid, h["row_version"], role, {"ref": ref, "role": role}
            )

    def visible(self, scope: Scope, *, include_history: bool = False) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT state,data FROM heads WHERE tenant=? AND project=? AND kind='visibility'",
                scope.keys(),
            ).fetchall()
        return [
            json.loads(r["data"])["ref"]
            for r in rows
            if include_history or r["state"] in {"canonical", "active"}
        ]

    def copy_cold(
        self, actor: Actor, artifact_refs: list[dict[str, Any]], destination: str | Path
    ) -> dict[str, Any]:
        actor.require("evidence.archive")
        root = Path(destination).absolute()
        if root.is_symlink():
            raise Hold("ARCHIVE_PATH", "Cold archive cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        entries: list[dict[str, Any]] = []
        for ref in artifact_refs:
            raw = self.artifacts.read(actor.scope, ref)
            path = root / ref["digest"][7:]
            if path.is_symlink():
                raise Hold("ARCHIVE_PATH", "Archive object cannot be a symlink")
            if path.exists():
                if digest_bytes(path.read_bytes()) != ref["digest"]:
                    raise Hold("ARCHIVE_CORRUPT", "Cold archive object differs")
            else:
                with open(path, "xb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
            entries.append({"artifact": ref, "object": path.name})
        manifest: dict[str, Any] = {
            "archive_id": new_id("archive"),
            "scope": actor.scope.wire(),
            "entries": entries,
            "created_at": now(),
            "hot_source_deleted": False,
        }
        (root / (manifest["archive_id"] + ".json")).write_bytes(canonical(manifest))
        return manifest

    def deletion_proposal(
        self, actor: Actor, artifact_refs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        actor.require("evidence.archive")
        # Defaults intentionally never delete. Applied independent retention decisions
        # can call a separately bound object-storage lifecycle adapter.
        return {
            "status": "proposal_only",
            "artifact_refs": artifact_refs,
            "requires": [
                "legal_hold_check",
                "reachability_check",
                "retention_decision",
                "verified_cold_copy",
            ],
            "deleted": False,
        }
