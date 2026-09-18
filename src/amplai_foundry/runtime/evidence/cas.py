"""Scoped content-addressed artifact store with atomic, verified admission."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from ..contracts.identity import canonical, digest_bytes, new_id, now
from ..errors import Conflict, Hold, RuntimeFault
from ..storage.store import Scope, Store

SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        rb"(?i)(?:api[_-]?key|access[_-]?token|password)\s*[=:]\s*[\'\"]?[A-Za-z0-9_+/.-]{24,}"
    ),
    re.compile(rb"(?<![a-zA-Z0-9])(?:sk-proj-|sk-ant-api03-)[A-Za-z0-9_-]{24,}"),
)


def scan_secrets(data: bytes) -> list[str]:
    return [
        f"secret-pattern-{i + 1}"
        for i, pattern in enumerate(SECRET_PATTERNS)
        if pattern.search(data)
    ]


class ArtifactStore:
    def __init__(self, store: Store, *, max_bytes: int = 32 * 1024 * 1024):
        self.store, self.max_bytes = store, max_bytes
        self.root = store.root / "artifacts"
        if not store.readonly:
            self.root.mkdir(mode=0o700, exist_ok=True)

    def _directory(self, scope: Scope) -> Path:
        key = hashlib.sha256(canonical(scope.wire())).hexdigest()
        target = self.root / key
        if target.is_symlink():
            raise RuntimeFault("SYMLINK_ESCAPE", "Artifact scope directory is a symlink")
        if not self.store.readonly:
            target.mkdir(exist_ok=True, mode=0o700)
        return target

    def _path(self, scope: Scope, expected_digest: str) -> Path:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest):
            raise RuntimeFault("BAD_DIGEST", "Artifact digest format is invalid")
        path = self._directory(scope) / expected_digest[7:]
        if path.is_symlink():
            raise RuntimeFault("SYMLINK_ESCAPE", "Artifact file is a symlink")
        return path

    def admit(
        self,
        scope: Scope,
        data: bytes,
        media_type: str,
        *,
        classification: str = "internal",
        expected_digest: str | None = None,
        trust: str = "worker",
    ) -> dict:
        self.store.assert_outside_tx()
        if not isinstance(data, bytes) or len(data) > self.max_bytes:
            raise RuntimeFault("ARTIFACT_SIZE", "Artifact exceeds its configured maximum")
        if not re.fullmatch(r"[\w.+-]+/[\w.+-]+(?:;[ -~]+)?", media_type):
            raise RuntimeFault("MEDIA_TYPE", "Malformed media type")
        if classification not in {"public", "internal", "confidential", "restricted"}:
            raise RuntimeFault("CLASSIFICATION", "Unknown data class")
        if trust not in {"worker", "verifier", "legacy_imported", "operator"}:
            raise RuntimeFault("TRUST_CLASS", "Unknown producer class")
        if media_type.split(";")[0] == "application/json":
            try:
                json.loads(data)
            except (ValueError, UnicodeDecodeError) as exc:
                raise RuntimeFault("MEDIA_CONTENT", "JSON artifact is not valid JSON") from exc
        findings = scan_secrets(data)
        if findings:
            raise Hold(
                "SECRET_DETECTED",
                "Artifact quarantined before storage; raw secret is not logged",
                details=findings,
            )
        value_digest = digest_bytes(data)
        if expected_digest and value_digest != expected_digest:
            raise Conflict("ARTIFACT_DIGEST", "Claimed digest does not match the bytes")
        path = self._path(scope, value_digest)
        if path.exists():
            if digest_bytes(path.read_bytes()) != value_digest:
                raise RuntimeFault("CAS_CORRUPT", "Existing CAS bytes have changed")
        else:
            fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                directory = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        result = {
            "id": new_id("artifact"),
            "digest": value_digest,
            "media_type": media_type,
            "size_bytes": len(data),
        }
        # If the DB commit fails the bytes are only an orphan, never an admitted false reference.
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    *scope.keys(),
                    result["id"],
                    value_digest,
                    media_type,
                    len(data),
                    classification,
                    trust,
                    now(),
                ),
            )
            self.store.event(db, scope, "run", result["id"], "artifact.admitted", result)
        return result

    def read(self, scope: Scope, artifact: dict, *, trusted: bool = False) -> bytes:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM artifacts WHERE tenant=? AND project=? AND id=?",
                (*scope.keys(), artifact["id"]),
            ).fetchone()
        if not row:
            raise RuntimeFault("NOT_FOUND", "Artifact is not admitted in this project")
        for name in ("digest", "media_type", "size_bytes"):
            if artifact.get(name) != row[name]:
                raise Conflict("ARTIFACT_REFERENCE", "Artifact metadata differs from registry")
        if trusted and row["trust"] not in {"verifier", "operator"}:
            raise Hold(
                "UNTRUSTED_EVIDENCE", "Worker/legacy evidence is not an independent verifier result"
            )
        path = self._path(scope, row["digest"])
        if not path.is_file():
            raise Hold("ARTIFACT_MISSING", "Registered artifact bytes are unavailable")
        raw = path.read_bytes()
        if digest_bytes(raw) != row["digest"] or len(raw) != row["size_bytes"]:
            raise Hold("ARTIFACT_CORRUPT", "Artifact content verification failed")
        return raw

    def inventory(self, scope: Scope) -> dict:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT digest FROM artifacts WHERE tenant=? AND project=?", scope.keys()
            ).fetchall()
        registered = {r[0][7:] for r in rows}
        on_disk = {
            p.name
            for p in self._directory(scope).iterdir()
            if p.is_file() and not p.name.startswith(".")
        }
        return {
            "missing": sorted(registered - on_disk),
            "orphans": sorted(on_disk - registered),
            "deletion_mode": "report_only",
        }
