"""Legacy import preserves history but never upgrades it into V3 evidence or grants."""

from __future__ import annotations

import json
from pathlib import Path

from ..runtime.contracts.identity import digest, new_id
from ..runtime.errors import Hold


class V2Importer:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    def plan(self, root):
        root = Path(root).resolve()
        files = []
        for p in sorted(root.rglob("*.json")):
            if (
                p.is_symlink()
                or not p.resolve().is_relative_to(root)
                or any(x in {".git", ".venv", "node_modules"} for x in p.relative_to(root).parts)
            ):
                continue
            if p.stat().st_size > 16 * 1024 * 1024:
                raise Hold("MIGRATION_FILE_SIZE", "Narrow large legacy import explicitly")
            raw = p.read_bytes()
            from ..runtime.contracts.identity import digest_bytes

            files.append(
                {
                    "path": p.relative_to(root).as_posix(),
                    "digest": digest_bytes(raw),
                    "size_bytes": len(raw),
                }
            )
        return {
            "migration_id": new_id("migration"),
            "source_root": str(root),
            "files": files,
            "policy": "legacy_imported; no leases/grants/verdict promotion",
            "source_untouched": True,
        }

    def apply(self, actor, plan):
        actor.require("migration.apply")
        root = Path(plan["source_root"]).resolve()
        refs = []
        from ..runtime.contracts.identity import digest_bytes

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
            json.loads(raw)
            artifact = self.artifacts.admit(
                actor.scope, raw, "application/json", trust="legacy_imported"
            )
            value = {
                "import_id": "legacy-"
                + digest({"scope": actor.scope.wire(), "source": entry})[7:31],
                "scope": actor.scope.wire(),
                "source": entry,
                "artifact": artifact,
                "trust": "legacy_imported",
                "active_execution": False,
                "authority": False,
                "migration_id": plan["migration_id"],
            }
            with self.store.tx() as db:
                refs.append(
                    self.store.put(db, actor.scope, "legacy-import", value["import_id"], 1, value)
                )
        return {
            "status": "imported_as_untrusted_history",
            "refs": refs,
            "source_untouched": True,
            "old_leases_activated": False,
        }
