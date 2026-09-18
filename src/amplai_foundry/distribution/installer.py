"""Ownership-aware Kit installation: plan → dry-run gate → backup → atomic apply → verify → receipt.

design/13 §5 and design/19 §5. The installer changes owned paths only, stops with a
conflict when an owned file's digest moved under the user, never treats an absent
optional file as corruption, keeps app-local overrides in their own namespace, and
leaves a durable journal so an interrupted install rolls back to exact preimages. A
multi-target install reports each target separately; partial success is never "all".
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, ClassVar

from ..runtime.contracts.identity import canonical, digest, digest_bytes, new_id, now
from ..runtime.errors import Conflict, Hold
from .packs import PackRegistry, safe_path

Ref = dict[str, Any]
Plan = dict[str, Any]

# Paths a pack may never own, whatever the ALLOWED_ROOTS say (authority/identity/local state).
PROTECTED_PATHS: tuple[str, ...] = (
    ".ai-team/app.json",
    ".ai-team/policy/approvals.jsonl",
    ".ai-team/local/",
    ".ai-team/install-v3/",
)
# App-local overrides live beside owned files but are never installed, compared or removed.
OVERRIDE_NAMESPACES: tuple[str, ...] = (".agents/overrides/", ".ai-team/overrides/")


def file_digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


class KitInstaller:
    ALLOWED_ROOTS: ClassVar[set[str]] = {
        ".ai-team",
        ".agents",
        ".claude",
        ".codex",
        ".opencode",
        "AGENTS.md",
        "CLAUDE.md",
    }

    def __init__(
        self, registry: PackRegistry, *, protected_paths: Iterable[str] = PROTECTED_PATHS
    ) -> None:
        self.registry = registry
        self.protected = tuple(protected_paths)

    # -- paths -------------------------------------------------------------------
    def _path(self, root: Path, name: str) -> Path:
        relative = safe_path(name)
        if relative.parts[0] not in self.ALLOWED_ROOTS:
            raise Hold(
                "INSTALL_SCOPE", "Kit may not overwrite application source or arbitrary home paths"
            )
        if any(name.startswith(ns) for ns in OVERRIDE_NAMESPACES):
            raise Hold(
                "INSTALL_OVERRIDE_NAMESPACE",
                "App-local overrides are user territory; a pack cannot own them",
                details={"path": name},
            )
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise Hold(
                    "INSTALL_SYMLINK", "Kit installation refuses all symlink path components"
                )
        if current.exists() and not current.is_file():
            raise Hold("INSTALL_TYPE", "Owned payload target must be a regular file")
        return current

    @staticmethod
    def _atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".v3-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _metadata(self, root: str | Path) -> tuple[Path, Path]:
        root_path = Path(root).absolute()
        if root_path.is_symlink() or not root_path.is_dir():
            raise Hold("INSTALL_ROOT", "An existing non-symlink repository root is required")
        meta = root_path / ".ai-team" / "install-v3"
        for parent in [root_path / ".ai-team", meta]:
            if parent.is_symlink():
                raise Hold("INSTALL_SYMLINK", "Kit metadata cannot use symlinks")
        return root_path, meta

    @staticmethod
    def _receipt_path(meta: Path, pack_id: str) -> Path:
        if "/" in pack_id or ".." in pack_id:
            raise Hold("PACK_ID_PATH", "Pack ID cannot be used as an unsafe filename")
        return meta / (pack_id + ".json")

    # -- plan / dry-run ------------------------------------------------------------
    def _plan(
        self,
        root: Path,
        meta: Path,
        pack_id: str,
        version: str,
        files: dict[str, bytes],
        *,
        bundle_digest: str | None,
    ) -> Plan:
        receipt_path = self._receipt_path(meta, pack_id)
        receipt = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else None
        previous: dict[str, str] = receipt["owned_files"] if receipt else {}
        changes: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        for name in sorted(set(files) | set(previous)):
            path = self._path(root, name)
            before = file_digest(path) if path.exists() else None
            owned = previous.get(name)
            if owned and before is None:
                # T-093: an absent owned file is reported, not declared corruption.
                warnings.append({"path": name, "kind": "missing_owned"})
            elif owned and before != owned:
                raise Hold(
                    "LOCAL_MODIFICATION",
                    "User modified an owned file; reconcile before install",
                    details={"path": name, "expected_old_digest": owned, "actual": before},
                )
            if not owned and before is not None:
                # A coincidentally identical unowned file is still not silently claimed.
                raise Hold(
                    "UNOWNED_FILE", "Existing user content is not Kit-owned", details={"path": name}
                )
            after = digest_bytes(files[name]) if name in files else None
            changes.append(
                {
                    "path": name,
                    "before": before,
                    "after": after,
                    "action": "remove"
                    if after is None
                    else "add"
                    if before is None
                    else "keep"
                    if before == after
                    else "replace",
                }
            )
        result: Plan = {
            "schema_version": "3.0.0",
            "plan_id": new_id("install"),
            "root": str(root),
            "pack_id": pack_id,
            "version": version,
            "operation": "install" if files else "remove",
            "bundle_digest": bundle_digest,
            "previous_receipt_digest": digest_bytes(receipt_path.read_bytes()) if receipt else None,
            "changes": changes,
            "warnings": warnings,
            "overrides_preserved": self._overrides(root),
            "created_at": now(),
            "requested_permissions_are_grants": False,
        }
        result["plan_digest"] = digest(result)
        return result

    def _overrides(self, root: Path) -> list[str]:
        found: list[str] = []
        for ns in OVERRIDE_NAMESPACES:
            base = root / ns
            if base.is_dir() and not base.is_symlink():
                found.extend(
                    sorted(str(p.relative_to(root)) for p in base.rglob("*") if p.is_file())
                )
        return found

    def plan(self, root: str | Path, bundle: dict[str, Any]) -> Plan:
        root_path, meta = self._metadata(root)
        manifest, files = self.registry.inspect(bundle)
        return self._plan(
            root_path,
            meta,
            manifest["pack_id"],
            manifest["version"],
            files,
            bundle_digest=digest(bundle),
        )

    def plan_removal(self, root: str | Path, pack_id: str) -> Plan:
        root_path, meta = self._metadata(root)
        receipt_path = self._receipt_path(meta, pack_id)
        if not receipt_path.exists():
            raise Hold("INSTALL_RECEIPT_MISSING", "Nothing to remove: no receipt for this pack")
        version = str(json.loads(receipt_path.read_bytes())["version"])
        return self._plan(root_path, meta, pack_id, version, {}, bundle_digest=None)

    def gate(self, plan: Plan) -> Plan:
        """Policy gate between dry-run and apply: protected paths are never installable."""
        blocked = sorted(
            c["path"]
            for c in plan["changes"]
            if any(c["path"] == p or c["path"].startswith(p) for p in self.protected)
        )
        if blocked:
            raise Hold(
                "INSTALL_PROTECTED",
                "Plan touches protected authority/identity/local paths",
                details={"paths": blocked},
            )
        return plan

    # -- apply -------------------------------------------------------------------
    def apply(
        self,
        actor: Any,
        root: str | Path,
        bundle: dict[str, Any] | None,
        plan: Plan,
        *,
        inject_failure: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        actor.require("pack.install")
        root_path, meta = self._metadata(root)
        expected_bundle = digest(bundle) if bundle is not None else None
        if (
            plan.get("root") != str(root_path)
            or plan.get("bundle_digest") != expected_bundle
            or plan.get("plan_digest")
            != digest({k: v for k, v in plan.items() if k != "plan_digest"})
        ):
            raise Hold(
                "INSTALL_PLAN", "The explicit install plan does not match root or signed bundle"
            )
        self.gate(plan)
        if bundle is not None:
            manifest, files = self.registry.inspect(bundle)
            pack_id = str(manifest["pack_id"])
        else:
            files, pack_id = {}, str(plan["pack_id"])
        meta.mkdir(parents=True, exist_ok=True)
        lock = open(meta / "owner.lock", "a+b")  # noqa: SIM115 - held across the journal
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Hold("INSTALL_BUSY", "Another installer owns this repository") from exc
            pending = meta / "pending.json"
            if pending.exists():
                raise Hold("INSTALL_RECOVERY", "Reconcile a previous incomplete install first")
            receipt_path = self._receipt_path(meta, pack_id)
            previous = receipt_path.read_bytes() if receipt_path.exists() else None
            if (digest_bytes(previous) if previous else None) != plan["previous_receipt_digest"]:
                raise Conflict("INSTALL_RECEIPT_CAS", "Kit receipt changed since planning")
            for change in plan["changes"]:
                path = self._path(root_path, change["path"])
                actual = file_digest(path) if path.exists() else None
                if actual != change["before"]:
                    raise Conflict("INSTALL_PREIMAGE", "File changed after dry-run")
            backups = meta / "backups" / plan["plan_id"]
            backups.mkdir(parents=True, mode=0o700)
            for i, change in enumerate(plan["changes"]):
                if change["before"] is not None:
                    shutil.copy2(self._path(root_path, change["path"]), backups / str(i))
            if previous:
                self._atomic(backups / "receipt.json", previous)
            journal: dict[str, Any] = {
                "plan": plan,
                "backup_dir": str(backups.relative_to(root_path)),
                "previous_receipt_exists": previous is not None,
                "status": "prepared",
            }
            self._atomic(pending, canonical(journal))
            if inject_failure:
                inject_failure("prepared")
            for i, change in enumerate(plan["changes"]):
                path = self._path(root_path, change["path"])
                if change["action"] == "remove":
                    if path.exists():
                        path.unlink()
                elif change["action"] != "keep":
                    self._atomic(path, files[change["path"]])
                if inject_failure:
                    inject_failure("file:" + str(i))
            owned = {name: digest_bytes(content) for name, content in files.items()}
            for name, expected in owned.items():
                if file_digest(self._path(root_path, name)) != expected:
                    raise Hold("INSTALL_POSTIMAGE", "Installed bytes differ from signed payload")
            receipt: dict[str, Any] = {
                "schema_version": "3.0.0",
                "receipt_id": new_id("receipt"),
                "operation": plan["operation"],
                "pack_id": pack_id,
                "version": plan["version"],
                "bundle_digest": expected_bundle,
                "plan_digest": plan["plan_digest"],
                "owned_files": owned,
                "warnings": plan["warnings"],
                "overrides_preserved": plan["overrides_preserved"],
                "installed_by": actor.subject_id,
                "installed_at": now(),
            }
            if files:
                self._atomic(receipt_path, canonical(receipt))
            else:
                if receipt_path.exists():
                    receipt_path.unlink()
                self._atomic(
                    meta / "receipts" / f"removal-{plan['plan_id']}.json", canonical(receipt)
                )
            journal["status"] = "committed"
            self._atomic(pending, canonical(journal))
            if inject_failure:
                inject_failure("committed")
            pending.unlink()
            return receipt
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def apply_many(
        self, actor: Any, targets: Iterable[tuple[str | Path, dict[str, Any] | None, Plan]]
    ) -> dict[str, Any]:
        """Per-target receipts (design/19 §5): partial success is reported, never rolled up."""
        results: dict[str, dict[str, Any]] = {}
        for root, bundle, plan in targets:
            key = str(Path(root).absolute())
            try:
                results[key] = {
                    "status": "installed",
                    "receipt": self.apply(actor, root, bundle, plan),
                }
            except (Hold, Conflict) as exc:
                results[key] = {"status": "failed", "code": exc.code, "message": exc.message}
        counts = {"installed": 0, "failed": 0}
        for item in results.values():
            counts[item["status"]] += 1
        return {
            "targets": results,
            "counts": counts,
            "overall": "complete" if counts["failed"] == 0 else "partial",
        }

    # -- recovery ----------------------------------------------------------------
    def recover(self, actor: Any, root: str | Path) -> dict[str, Any]:
        actor.require("pack.install")
        root_path, meta = self._metadata(root)
        pending = meta / "pending.json"
        if not pending.exists():
            return {"status": "clean"}
        with open(meta / "owner.lock", "a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Hold("INSTALL_BUSY", "Installer is active") from exc
            journal = json.loads(pending.read_bytes())
            plan = journal["plan"]
            backups = root_path / journal["backup_dir"]
            if root_path not in backups.resolve().parents or backups.is_symlink():
                raise Hold("INSTALL_BACKUP", "Unsafe recovery backup path")
            if journal["status"] == "committed":
                pending.unlink()
                return {"status": "committed_receipt_preserved"}
            for i, change in reversed(list(enumerate(plan["changes"]))):
                path = self._path(root_path, change["path"])
                actual = file_digest(path) if path.exists() else None
                if actual not in {change["before"], change["after"]}:
                    raise Hold(
                        "RECOVERY_CONCURRENT_EDIT",
                        "External edit found; automatic rollback will not erase it",
                    )
                if change["before"] is None:
                    if path.exists():
                        path.unlink()
                else:
                    source = backups / str(i)
                    if file_digest(source) != change["before"]:
                        raise Hold("BACKUP_INTEGRITY", "Install preimage backup differs")
                    self._atomic(path, source.read_bytes())
            receipt_path = self._receipt_path(meta, plan["pack_id"])
            if journal["previous_receipt_exists"]:
                self._atomic(receipt_path, (backups / "receipt.json").read_bytes())
            elif receipt_path.exists():
                receipt_path.unlink()
            rollback = {
                "schema_version": "3.0.0",
                "receipt_id": new_id("rollback"),
                "plan_id": plan["plan_id"],
                "pack_id": plan["pack_id"],
                "restored_to": "preimages",
                "recovered_by": actor.subject_id,
                "recovered_at": now(),
            }
            self._atomic(
                meta / "receipts" / f"rollback-{plan['plan_id']}.json", canonical(rollback)
            )
            pending.unlink()
            return {
                "status": "rolled_back_to_preimages",
                "plan_id": plan["plan_id"],
                "receipt": rollback,
            }


def release_truth(kit_root: str | Path) -> dict[str, Any]:
    """T-097: version facts must agree before shipping; nothing here edits them."""
    root = Path(kit_root)
    facts: dict[str, str | None] = {}
    version_file = root / "VERSION"
    facts["VERSION"] = version_file.read_text().strip() if version_file.is_file() else None
    readme = root / "README.md"
    if readme.is_file():
        first = readme.read_text(encoding="utf-8").splitlines()[:1]
        parts = first[0].split() if first else []
        facts["README.md"] = next((p for p in parts if p[:1].isdigit()), None)
    changelog = root / "CHANGELOG.md"
    if changelog.is_file():
        heads = [
            ln for ln in changelog.read_text(encoding="utf-8").splitlines() if ln.startswith("## ")
        ]
        facts["CHANGELOG.md"] = heads[0][3:].split()[0] if heads else None
    manifest = root / "manifest.json"
    if manifest.is_file():
        value = json.loads(manifest.read_bytes()).get("version")
        facts["manifest.json"] = str(value) if value is not None else None
    distinct = sorted({v for v in facts.values() if v})
    return {
        "facts": facts,
        "distinct_versions": distinct,
        "status": "consistent" if len(distinct) <= 1 else "discrepancy",
        "authoritative": None,
        "note": (
            "Choose the authoritative release with proof before shipping; "
            "this report changes nothing"
        ),
    }
