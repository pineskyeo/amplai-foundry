"""Single-writer local SQLite control plane with scoped immutable objects and CAS heads.

Every mutation uses a short BEGIN IMMEDIATE transaction. Provider and filesystem work
must be performed outside that context. SQLite online backup is used instead of copying
live database files. All reads require an explicit project scope.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts.identity import canonical, digest, new_id, now
from ..errors import Conflict, Hold, RuntimeFault


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    project_id: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(s, str) or not s or len(s) > 128
            for s in (self.tenant_id, self.project_id)
        ):
            raise RuntimeFault("INVALID_SCOPE", "Tenant and project identifiers must be nonempty")

    @classmethod
    def parse(cls, value: dict[str, Any]) -> Scope:
        if set(value) != {"tenant_id", "project_id"}:
            raise RuntimeFault("INVALID_SCOPE", "Scope fields do not match the protocol")
        return cls(**value)

    def keys(self) -> tuple[str, str]:
        return self.tenant_id, self.project_id

    def wire(self) -> dict[str, str]:
        return {"tenant_id": self.tenant_id, "project_id": self.project_id}


DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects (
 tenant TEXT NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
 revision INTEGER NOT NULL,digest TEXT NOT NULL,data BLOB NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(tenant,project,kind,id,revision));
CREATE TABLE IF NOT EXISTS heads (
 tenant TEXT NOT NULL,project TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,
 state TEXT NOT NULL,row_version INTEGER NOT NULL,data BLOB NOT NULL,
 PRIMARY KEY(tenant,project,kind,id));
CREATE TABLE IF NOT EXISTS refs (
 tenant TEXT NOT NULL,project TEXT NOT NULL,source_kind TEXT NOT NULL,source_id TEXT NOT NULL,
 source_revision INTEGER NOT NULL,target_id TEXT NOT NULL,target_revision INTEGER NOT NULL,
 target_digest TEXT NOT NULL,
 PRIMARY KEY(tenant,project,source_kind,source_id,source_revision,target_id,target_revision));
CREATE TABLE IF NOT EXISTS commands (
 tenant TEXT NOT NULL,project TEXT NOT NULL,actor TEXT NOT NULL,operation TEXT NOT NULL,
 key TEXT NOT NULL,request_digest TEXT NOT NULL,result BLOB NOT NULL,
 retention_class TEXT NOT NULL DEFAULT 'command',created_at TEXT NOT NULL,
 PRIMARY KEY(tenant,project,actor,operation,key));
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT NOT NULL,project TEXT NOT NULL,
 aggregate_type TEXT NOT NULL,aggregate_id TEXT NOT NULL,aggregate_seq INTEGER NOT NULL,
 event_id TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,data BLOB NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(tenant,project,aggregate_type,aggregate_id,aggregate_seq));
CREATE INDEX IF NOT EXISTS scoped_events ON events(tenant,project,seq);
CREATE TABLE IF NOT EXISTS outbox (
 event_seq INTEGER PRIMARY KEY REFERENCES events(seq),status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0,ack_at TEXT);
CREATE TABLE IF NOT EXISTS inbox (
 tenant TEXT NOT NULL,project TEXT NOT NULL,producer TEXT NOT NULL,message_id TEXT NOT NULL,
 digest TEXT NOT NULL,result BLOB NOT NULL,PRIMARY KEY(tenant,project,producer,message_id));
CREATE TABLE IF NOT EXISTS artifacts (
 tenant TEXT NOT NULL,project TEXT NOT NULL,id TEXT NOT NULL,digest TEXT NOT NULL,
 media_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,classification TEXT NOT NULL,
 trust TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(tenant,project,id));
CREATE TABLE IF NOT EXISTS leases (
 tenant TEXT NOT NULL,project TEXT NOT NULL,work_id TEXT NOT NULL,run_id TEXT NOT NULL,
 lease_id TEXT NOT NULL,worker_id TEXT NOT NULL,fence INTEGER NOT NULL,epoch INTEGER NOT NULL,
 expires REAL NOT NULL,heartbeat_seq INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,project,work_id),UNIQUE(tenant,project,run_id));
CREATE TABLE IF NOT EXISTS resources (
 tenant TEXT NOT NULL,project TEXT NOT NULL,resource TEXT NOT NULL,run_id TEXT NOT NULL,
 mode TEXT NOT NULL,
 PRIMARY KEY(tenant,project,resource,run_id));
CREATE TABLE IF NOT EXISTS reservations (
 tenant TEXT NOT NULL,project TEXT NOT NULL,goal_id TEXT NOT NULL,run_id TEXT NOT NULL,
 tokens INTEGER NOT NULL,cost INTEGER,started REAL NOT NULL,seconds INTEGER NOT NULL,
 status TEXT NOT NULL,
 usage BLOB,PRIMARY KEY(tenant,project,run_id));
CREATE TABLE IF NOT EXISTS grant_uses (
 tenant TEXT NOT NULL,project TEXT NOT NULL,grant_id TEXT NOT NULL,generation INTEGER NOT NULL,
 effect_key TEXT NOT NULL,payload_digest TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(tenant,project,grant_id,effect_key));
CREATE TABLE IF NOT EXISTS effect_keys (
 tenant TEXT NOT NULL,project TEXT NOT NULL,key TEXT NOT NULL,request_digest TEXT NOT NULL,
 effect_id TEXT NOT NULL,
 PRIMARY KEY(tenant,project,key));
CREATE TABLE IF NOT EXISTS worker_dispatch (
 tenant TEXT NOT NULL,project TEXT NOT NULL,dispatch_id TEXT NOT NULL,run_id TEXT NOT NULL,
 worker_id TEXT NOT NULL,payload_digest TEXT NOT NULL,state TEXT NOT NULL,result BLOB,
 PRIMARY KEY(tenant,project,dispatch_id));
"""


MOUNTINFO = Path("/proc/self/mountinfo")
NETWORK_FILESYSTEMS = frozenset(
    {"nfs", "nfs4", "cifs", "smbfs", "9p", "ceph", "glusterfs", "fuse.sshfs"}
)


def _local_filesystem(path: Path) -> dict[str, str]:
    """Reject known network mounts (design/09 §2, T-104) and report what was verified.

    Linux exposes mountinfo; elsewhere the store opens but says so explicitly, and the
    deployment doctor owns local-disk qualification. Silence is never a PASS.
    """
    if not MOUNTINFO.exists():
        return {"filesystem": "unverified_no_mountinfo", "fstype": "unknown"}
    resolved = str(path.resolve())
    found: list[tuple[int, str]] = []
    for line in MOUNTINFO.read_text().splitlines():
        parts = line.split()
        if "-" not in parts or len(parts) < 5:
            continue
        sep = parts.index("-")
        if sep + 1 >= len(parts):
            continue
        mount = parts[4].replace("\\040", " ")
        if resolved == mount or resolved.startswith(mount.rstrip("/") + "/"):
            found.append((len(mount), parts[sep + 1]))
    fstype = max(found)[1] if found else "unknown"
    if fstype in NETWORK_FILESYSTEMS:
        raise Hold(
            "UNSUPPORTED_STORAGE",
            "SQLite control-plane storage must be local, not a network mount",
            details={"fstype": fstype, "path": resolved},
        )
    return {"filesystem": "local" if found else "unverified_no_mount_match", "fstype": fstype}


class Store:
    def __init__(
        self, root: str | Path, *, readonly: bool = False, clock: Callable[[], float] = time.time
    ):
        self.root = Path(root).resolve()
        self.clock = clock
        self.readonly = readonly
        self.storage_qualification = _local_filesystem(self.root)
        if not readonly:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "runtime.sqlite3"
        self._lock = threading.RLock()
        self._local = threading.local()
        self._owner = None
        if not readonly:
            # The owner lock handle must outlive this block; close() releases it.
            self._owner = open(self.root / "owner.lock", "a+b")  # noqa: SIM115
            try:
                fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._owner.close()
                self._owner = None
                raise Hold("ACTIVE_OWNER", "Another control plane owns this local store") from exc
        try:
            self.conn = sqlite3.connect(
                f"file:{self.path}?mode=ro" if readonly else self.path,
                uri=readonly,
                timeout=10,
                isolation_level=None,
                check_same_thread=False,
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=10000")
            if not readonly:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.executescript(DDL)
                self._migrate_commands_operation()
            version = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_major'"
            ).fetchone()
            if version and version[0] != "3":
                self.close()
                raise Hold("SCHEMA_VERSION", "Store schema is incompatible; no automatic downgrade")
        except BaseException:
            # Release the owner lock when opening fails; nothing else holds it.
            self.close()
            raise
        self.epoch = 0
        if not readonly:
            with self.tx() as db:
                db.execute("INSERT OR IGNORE INTO meta VALUES('schema_major','3')")
                row = db.execute("SELECT value FROM meta WHERE key='owner_epoch'").fetchone()
                self.epoch = int(row[0]) + 1 if row else 1
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES('owner_epoch',?)", (str(self.epoch),)
                )
        else:
            row = self.conn.execute("SELECT value FROM meta WHERE key='owner_epoch'").fetchone()
            self.epoch = int(row[0]) if row else 0

    def _migrate_commands_operation(self) -> None:
        """Rebuild a pre-3.0.0.dev4 ``commands`` table whose key lacked ``operation``.

        Schema major stays 3; this is an additive logical migration performed in one
        transaction. Old rows keep their digests under operation ``command``.
        """
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(commands)").fetchall()}
        if "operation" in columns:
            return
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                # Individual execute() calls: executescript() would COMMIT the open
                # transaction first and silently break the atomic guarantee.
                for statement in (
                    """CREATE TABLE commands_v2 (
                     tenant TEXT NOT NULL,project TEXT NOT NULL,actor TEXT NOT NULL,
                     operation TEXT NOT NULL,key TEXT NOT NULL,request_digest TEXT NOT NULL,
                     result BLOB NOT NULL,retention_class TEXT NOT NULL DEFAULT 'command',
                     created_at TEXT NOT NULL,
                     PRIMARY KEY(tenant,project,actor,operation,key))""",
                    """INSERT INTO commands_v2
                     SELECT tenant,project,actor,'command',key,request_digest,result,
                            'command',created_at
                     FROM commands""",
                    "DROP TABLE commands",
                    "ALTER TABLE commands_v2 RENAME TO commands",
                ):
                    self.conn.execute(statement)
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            raise Hold("READ_ONLY", "This store is read-only")
        if getattr(self._local, "in_tx", False):
            raise RuntimeFault(
                "NESTED_TRANSACTION", "Pass the current transaction to lower-level operations"
            )
        with self._lock:
            self._local.in_tx = True
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            finally:
                self._local.in_tx = False

    def assert_outside_tx(self) -> None:
        if getattr(self._local, "in_tx", False):
            raise RuntimeFault(
                "IO_IN_TRANSACTION", "External IO is forbidden inside a control-plane transaction"
            )

    def put(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        kind: str,
        object_id: str,
        revision: int,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        if value.get("scope", scope.wire()) != scope.wire():
            raise RuntimeFault("SCOPE_MISMATCH", "Payload scope differs from authenticated scope")
        raw = canonical(value)
        ref = {"id": object_id, "revision": revision, "digest": digest(value)}
        previous = db.execute(
            "SELECT digest FROM objects "
            "WHERE tenant=? AND project=? AND kind=? AND id=? AND revision=?",
            (*scope.keys(), kind, object_id, revision),
        ).fetchone()
        if previous:
            if previous[0] != ref["digest"]:
                raise Conflict("IMMUTABLE_REVISION", "A revision may not be overwritten")
            return ref
        db.execute(
            "INSERT INTO objects VALUES(?,?,?,?,?,?,?,?)",
            (*scope.keys(), kind, object_id, revision, ref["digest"], raw, now()),
        )

        def collect(obj: object) -> Iterator[dict[str, Any]]:
            if isinstance(obj, dict):
                if set(obj) == {"id", "revision", "digest"}:
                    yield obj
                else:
                    for item in obj.values():
                        yield from collect(item)
            elif isinstance(obj, list):
                for item in obj:
                    yield from collect(item)

        for target in collect(value):
            db.execute(
                "INSERT OR IGNORE INTO refs VALUES(?,?,?,?,?,?,?,?)",
                (
                    *scope.keys(),
                    kind,
                    object_id,
                    revision,
                    target["id"],
                    target["revision"],
                    target["digest"],
                ),
            )
        return ref

    def get(
        self, scope: Scope, kind: str, ref: dict[str, Any], *, db: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        with self._lock:
            row = (
                (db or self.conn)
                .execute(
                    "SELECT digest,data FROM objects "
                    "WHERE tenant=? AND project=? AND kind=? AND id=? AND revision=?",
                    (*scope.keys(), kind, ref["id"], ref["revision"]),
                )
                .fetchone()
            )
        if not row:
            raise RuntimeFault("NOT_FOUND", "Object not found in this scope")
        if row["digest"] != ref["digest"]:
            raise Conflict("REFERENCE_DIGEST", "Immutable reference digest differs")
        value: dict[str, Any] = json.loads(row["data"])
        if digest(value) != row["digest"]:
            raise Hold(
                "OBJECT_INTEGRITY", "Stored immutable object bytes do not match their digest"
            )
        return value

    def list_objects(self, scope: Scope, kind: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,revision,digest,data FROM objects "
                "WHERE tenant=? AND project=? AND kind=? ORDER BY id,revision",
                (*scope.keys(), kind),
            ).fetchall()
        return [
            (
                {"id": r["id"], "revision": r["revision"], "digest": r["digest"]},
                json.loads(r["data"]),
            )
            for r in rows
        ]

    def head(
        self, scope: Scope, kind: str, object_id: str, *, db: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        with self._lock:
            row = (
                (db or self.conn)
                .execute(
                    "SELECT state,row_version,data FROM heads "
                    "WHERE tenant=? AND project=? AND kind=? AND id=?",
                    (*scope.keys(), kind, object_id),
                )
                .fetchone()
            )
        if not row:
            raise RuntimeFault("NOT_FOUND", "Aggregate not found in this scope")
        return {
            "state": row["state"],
            "row_version": row["row_version"],
            "data": json.loads(row["data"]),
        }

    def cas(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        kind: str,
        object_id: str,
        expected: int,
        state: str,
        value: dict[str, Any],
    ) -> int:
        if expected == 0:
            try:
                db.execute(
                    "INSERT INTO heads VALUES(?,?,?,?,?,1,?)",
                    (*scope.keys(), kind, object_id, state, canonical(value)),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("STALE_VERSION", "Aggregate already exists") from exc
            return 1
        result = db.execute(
            "UPDATE heads SET state=?,data=?,row_version=row_version+1 "
            "WHERE tenant=? AND project=? AND kind=? AND id=? AND row_version=?",
            (state, canonical(value), *scope.keys(), kind, object_id, expected),
        )
        if result.rowcount != 1:
            raise Conflict("STALE_VERSION", "If-Match version is no longer current")
        return expected + 1

    def event(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        kind: str,
        object_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        seq = db.execute(
            "SELECT COALESCE(MAX(aggregate_seq),0)+1 FROM events "
            "WHERE tenant=? AND project=? AND aggregate_type=? AND aggregate_id=?",
            (*scope.keys(), kind, object_id),
        ).fetchone()[0]
        cursor = db.execute(
            "INSERT INTO events(tenant,project,aggregate_type,aggregate_id,aggregate_seq,"
            "event_id,event_type,data,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                *scope.keys(),
                kind,
                object_id,
                seq,
                new_id("evt"),
                event_type,
                canonical(payload),
                now(),
            ),
        )
        event_seq = cursor.lastrowid
        if event_seq is None:
            raise RuntimeFault("EVENT_SEQUENCE", "SQLite reported no sequence for the new event")
        db.execute("INSERT INTO outbox(event_seq) VALUES(?)", (event_seq,))
        return event_seq

    def events(self, scope: Scope, *, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE tenant=? AND project=? AND seq>? ORDER BY seq LIMIT ?",
                (*scope.keys(), after, min(max(limit, 1), 1000)),
            ).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def command(
        self,
        scope: Scope,
        actor: str,
        key: str,
        payload: dict[str, Any],
        run: Callable[[sqlite3.Connection], dict[str, Any]],
        *,
        operation: str = "command",
        retention_class: str = "command",
    ) -> dict[str, Any]:
        """Scoped idempotent command (design/09 §4, INV-23).

        Key = (scope, actor, operation, key). Same payload digest returns the committed
        response; a different payload is ``IDEMPOTENCY_CONFLICT``. TTL expiry never
        re-enables a dangerous write, so nothing here deletes rows.
        """
        if not key or len(key) > 256:
            raise RuntimeFault("IDEMPOTENCY_REQUIRED", "A bounded idempotency key is required")
        if not operation or len(operation) > 128:
            raise RuntimeFault("IDEMPOTENCY_OPERATION", "A bounded operation name is required")
        if retention_class not in {"command", "effect"}:
            raise RuntimeFault("IDEMPOTENCY_RETENTION", "Unknown idempotency retention class")
        fingerprint = digest(payload)
        with self.tx() as db:
            row = db.execute(
                "SELECT request_digest,result FROM commands "
                "WHERE tenant=? AND project=? AND actor=? AND operation=? AND key=?",
                (*scope.keys(), actor, operation, key),
            ).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise Conflict(
                        "IDEMPOTENCY_CONFLICT",
                        "The key was previously used for a different payload",
                    )
                cached: dict[str, Any] = json.loads(row[1])
                return cached
            result = run(db)
            db.execute(
                "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    *scope.keys(),
                    actor,
                    operation,
                    key,
                    fingerprint,
                    canonical(result),
                    retention_class,
                    now(),
                ),
            )
            return result

    def backup(self, destination: str | Path) -> dict[str, Any]:
        self.assert_outside_tx()
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise Conflict("BACKUP_EXISTS", "Do not overwrite an existing snapshot")
        with self._lock, sqlite3.connect(target) as backup:
            self.conn.backup(backup)
            check = backup.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeFault("BACKUP_CORRUPT", check)
        return {"path": str(target), "owner_epoch": self.epoch, "integrity": check}

    def close(self) -> None:
        if getattr(self, "conn", None):
            self.conn.close()
            del self.conn
        if self._owner:
            fcntl.flock(self._owner, fcntl.LOCK_UN)
            self._owner.close()
            self._owner = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
