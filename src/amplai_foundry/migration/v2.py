"""V2 runtime/contract import without authority upgrade (design/19 §2, V3-051).

Every legacy object is preserved raw with its digest and enters V3 as
``legacy_imported`` history: never a lease, never a grant, never a trusted verdict.
Active V2 work is drained or cancel-reconciled — a V2 lease is not made valid in V3.
Old approvals keep their own scope; any new action needs a fresh V3 grant (T-096).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..runtime.contracts.authority import Actor
from ..runtime.contracts.identity import digest, digest_bytes, new_id, now
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.evidence.cas import ArtifactStore
from ..runtime.storage.store import Store

Ref = dict[str, Any]

EXCLUDED_PARTS = frozenset({".git", ".venv", "node_modules", "__pycache__"})
V2_KINDS = frozenset(
    {"project", "app_identity", "change", "work", "decision", "evidence", "question", "contract"}
)
# V2 Work statuses that still hold or expect a runtime slot (scripts/amplai_runtime.py).
ACTIVE_WORK = frozenset({"READY", "ACTIVE", "RUNNING", "WAITING", "BLOCKED", "HUMAN_REQUIRED"})
TERMINAL_WORK = frozenset({"DONE", "CANCELLED", "FAILED", "SUPERSEDED"})


class V2Importer:
    def __init__(self, store: Store, artifacts: ArtifactStore) -> None:
        self.store, self.artifacts = store, artifacts

    # -- inventory ---------------------------------------------------------------
    def _classify(self, path: Path, raw: bytes) -> tuple[str, dict[str, Any]]:
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise Hold("MIGRATION_JSON", f"{path.name} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise Hold("MIGRATION_SHAPE", f"{path.name} is not a JSON object")
        kind = str(value.get("kind") or "unknown")
        return kind, value

    def plan(self, root: str | Path, *, active_run_policy: str = "drain") -> dict[str, Any]:
        if active_run_policy not in {"drain", "cancel_reconcile"}:
            raise RuntimeFault(
                "MIGRATION_POLICY", "active_run_policy must be drain or cancel_reconcile"
            )
        root_path = Path(root).resolve()
        if not root_path.is_dir():
            raise Hold("MIGRATION_ROOT", "Legacy source root must be an existing directory")
        files: list[dict[str, Any]] = []
        active_work: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        kinds: dict[str, int] = {}
        for p in sorted(root_path.rglob("*.json")):
            rel = p.relative_to(root_path)
            if (
                p.is_symlink()
                or not p.resolve().is_relative_to(root_path)
                or any(x in EXCLUDED_PARTS for x in rel.parts)
            ):
                continue
            if p.stat().st_size > 16 * 1024 * 1024:
                raise Hold("MIGRATION_FILE_SIZE", "Narrow large legacy import explicitly")
            raw = p.read_bytes()
            kind, value = self._classify(p, raw)
            kinds[kind] = kinds.get(kind, 0) + 1
            entry = {
                "path": rel.as_posix(),
                "digest": digest_bytes(raw),
                "size_bytes": len(raw),
                "kind": kind,
                "schema_version": str(value.get("schema_version", "unknown")),
            }
            files.append(entry)
            if kind == "work":
                status = str(value.get("status", "unknown"))
                if status in ACTIVE_WORK or value.get("lease_id"):
                    active_work.append(
                        {
                            "path": entry["path"],
                            "work_id": value.get("work_id"),
                            "status": status,
                            "lease_id": value.get("lease_id"),
                            "disposition": active_run_policy,
                        }
                    )
            if kind == "decision" and value.get("authority"):
                approvals.append(
                    {
                        "path": entry["path"],
                        "decision_id": value.get("decision_id"),
                        "authority": value.get("authority"),
                        "v3_grant": "reapproval_required",
                    }
                )
        return {
            "schema_version": "3.0.0",
            "migration_id": new_id("migration"),
            "source_root": str(root_path),
            "files": files,
            "kinds": kinds,
            "active_work": active_work,
            "active_run_policy": active_run_policy,
            "legacy_approvals": approvals,
            "policy": "legacy_imported; no leases/grants/verdict promotion; reapproval required",
            "source_untouched": True,
            "dry_run_required": True,
            "created_at": now(),
        }

    # -- apply --------------------------------------------------------------------
    def apply(
        self, actor: Actor, plan: dict[str, Any], *, dry_run_receipt: Ref | None = None
    ) -> dict[str, Any]:
        actor.require("migration.apply")
        if plan.get("dry_run_required") and dry_run_receipt is None:
            raise Hold("MIGRATION_DRY_RUN", "Apply needs the dry-run receipt of this exact plan")
        if dry_run_receipt is not None and dry_run_receipt.get("plan_digest") != digest(
            {k: v for k, v in plan.items() if k != "plan_digest"}
        ):
            raise Hold("MIGRATION_DRY_RUN", "Dry-run receipt belongs to a different plan")
        root = Path(plan["source_root"]).resolve()
        refs: list[Ref] = []
        drained: list[str] = []
        for entry in plan["files"]:
            relative = Path(entry["path"])
            path = root / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or path.is_symlink()
                or not path.resolve().is_relative_to(root)
            ):
                raise Hold("MIGRATION_PATH", "Source path escaped the frozen import root")
            raw = path.read_bytes()
            if digest_bytes(raw) != entry["digest"]:
                raise Hold("MIGRATION_PREIMAGE", "Legacy source changed after planning")
            kind, value = self._classify(path, raw)
            import_id = "legacy-" + digest({"scope": actor.scope.wire(), "source": entry})[7:31]
            prior = [
                r
                for r, _ in self.store.list_objects(actor.scope, "legacy-import")
                if r["id"] == import_id
            ]
            if prior:
                # Identical source bytes were already imported: idempotent, no second artifact.
                refs.append(prior[0])
                continue
            artifact = self.artifacts.admit(
                actor.scope, raw, "application/json", trust="legacy_imported"
            )
            record: dict[str, Any] = {
                "import_id": import_id,
                "scope": actor.scope.wire(),
                "source": entry,
                "artifact": artifact,
                "legacy_kind": kind,
                "legacy_id": value.get(f"{kind}_id") or value.get("id"),
                "legacy_status": value.get("status"),
                "trust": "legacy_imported",
                "active_execution": False,
                "authority": False,
                "lease_valid_in_v3": False,
                "verdict_trust": "none",
                "migration_id": plan["migration_id"],
            }
            if kind == "work" and (
                str(value.get("status")) in ACTIVE_WORK or value.get("lease_id")
            ):
                record["legacy_disposition"] = plan["active_run_policy"]
                drained.append(str(value.get("work_id")))
            if kind == "decision":
                record["legacy_authority"] = value.get("authority")
                record["v3_grant_ref"] = None
                record["reapproval_required"] = True
            with self.store.tx() as db:
                # Same source bytes → same import_id → same record; re-import is idempotent.
                refs.append(
                    self.store.put(db, actor.scope, "legacy-import", record["import_id"], 1, record)
                )
                self.store.event(
                    db,
                    actor.scope,
                    "release",
                    plan["migration_id"],
                    "legacy.imported",
                    {"import_id": record["import_id"], "legacy_kind": kind, "authority": False},
                )
        return {
            "status": "imported_as_untrusted_history",
            "migration_id": plan["migration_id"],
            "refs": refs,
            "counts": dict(plan["kinds"]),
            "active_work_disposed": drained,
            "active_run_policy": plan["active_run_policy"],
            "source_untouched": True,
            "old_leases_activated": False,
            "grants_created": 0,
        }

    def dry_run(self, plan: dict[str, Any]) -> Ref:
        """Record that this exact plan was reviewed; nothing is written to the store."""
        return {
            "receipt_id": new_id("dry-run"),
            "plan_digest": digest({k: v for k, v in plan.items() if k != "plan_digest"}),
            "files": len(plan["files"]),
            "active_work": len(plan["active_work"]),
            "legacy_approvals": len(plan["legacy_approvals"]),
            "recorded_at": now(),
        }

    # -- queries -----------------------------------------------------------------
    def imported(self, actor: Actor, *, kind: str | None = None) -> list[dict[str, Any]]:
        out = []
        for ref, value in self.store.list_objects(actor.scope, "legacy-import"):
            if kind is None or value.get("legacy_kind") == kind:
                out.append({**value, "ref": ref})
        return out
