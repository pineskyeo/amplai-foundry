"""SQLite authority foundation for mutable governance state."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from amplai_foundry.governance.filesystem import (
    FilesystemStatus,
    LocalFilesystemGuard,
)
from amplai_foundry.governance.migrations import MigrationRunner


class GovernanceStoreError(RuntimeError):
    """The Governance Store cannot satisfy its runtime contract."""


class GovernanceTransactionError(GovernanceStoreError):
    """A Governance Store transaction could not complete safely."""


@dataclass(frozen=True, slots=True)
class GovernanceStoreConfig:
    busy_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if self.busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms는 0 이상이어야 합니다.")


@dataclass(frozen=True, slots=True)
class GovernanceStoreHealth:
    path: Path
    filesystem: FilesystemStatus
    schema_version: int
    integrity_ok: bool
    journal_mode: str
    synchronous: int
    foreign_keys: bool
    trusted_schema: bool

    @property
    def healthy(self) -> bool:
        return (
            self.integrity_ok
            and self.journal_mode == "wal"
            and self.synchronous == 2
            and self.foreign_keys
            and not self.trusted_schema
        )


@contextmanager
def governance_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a short write transaction without nesting implicit boundaries."""

    if connection.in_transaction:
        raise GovernanceTransactionError("nested Governance transaction은 금지합니다.")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


class GovernanceStore:
    def __init__(
        self,
        path: Path,
        *,
        config: GovernanceStoreConfig | None = None,
        filesystem_guard: LocalFilesystemGuard | None = None,
        migration_runner: MigrationRunner | None = None,
    ) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self.config = config or GovernanceStoreConfig()
        self.filesystem_guard = filesystem_guard or LocalFilesystemGuard()
        self.migration_runner = migration_runner or MigrationRunner()

    def initialize(self) -> GovernanceStoreHealth:
        filesystem = self.filesystem_guard.validate(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect() as connection:
                with governance_transaction(connection):
                    self.migration_runner.apply_pending(connection)
                return self._health(connection, filesystem)
        except sqlite3.Error as error:
            raise GovernanceStoreError(
                f"Governance Store를 초기화할 수 없습니다: {error}"
            ) from error

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        filesystem = self.filesystem_guard.validate(self.path)
        if not filesystem.local:
            raise GovernanceStoreError("local filesystem 검증에 실패했습니다.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=0,
            )
        except sqlite3.Error as error:
            raise GovernanceStoreError(f"Governance Store에 연결할 수 없습니다: {error}") from error
        try:
            self._configure(connection)
            yield connection
        finally:
            connection.close()

    def check_startup(self) -> GovernanceStoreHealth:
        if not self.path.is_file():
            raise GovernanceStoreError(f"Governance Store가 존재하지 않습니다: {self.path}")
        filesystem = self.filesystem_guard.validate(self.path)
        try:
            with self.connect() as connection:
                return self._health(connection, filesystem)
        except sqlite3.Error as error:
            raise GovernanceStoreError(f"Governance Store startup check 실패: {error}") from error

    def _configure(self, connection: sqlite3.Connection) -> None:
        journal_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        journal_mode = str(journal_row[0]).lower() if journal_row is not None else ""
        if journal_mode != "wal":
            raise GovernanceStoreError(f"journal_mode=WAL을 활성화할 수 없습니다: {journal_mode}")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute(f"PRAGMA busy_timeout = {self.config.busy_timeout_ms}")
        self._verify_pragmas(connection)

    @staticmethod
    def _pragma_value(connection: sqlite3.Connection, name: str) -> str | int:
        row = connection.execute(f"PRAGMA {name}").fetchone()
        if row is None:
            raise GovernanceStoreError(f"PRAGMA 값을 확인할 수 없습니다: {name}")
        value = row[0]
        if not isinstance(value, (str, int)):
            raise GovernanceStoreError(f"PRAGMA 값의 type이 올바르지 않습니다: {name}")
        return value

    def _verify_pragmas(self, connection: sqlite3.Connection) -> None:
        expected = {
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
            "trusted_schema": 0,
            "busy_timeout": self.config.busy_timeout_ms,
        }
        actual = {
            "journal_mode": str(self._pragma_value(connection, "journal_mode")).lower(),
            "synchronous": int(self._pragma_value(connection, "synchronous")),
            "foreign_keys": int(self._pragma_value(connection, "foreign_keys")),
            "trusted_schema": int(self._pragma_value(connection, "trusted_schema")),
            "busy_timeout": int(self._pragma_value(connection, "busy_timeout")),
        }
        if actual != expected:
            raise GovernanceStoreError(
                f"required PRAGMA 불일치: expected={expected} actual={actual}"
            )

    def _health(
        self,
        connection: sqlite3.Connection,
        filesystem: FilesystemStatus,
    ) -> GovernanceStoreHealth:
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        integrity_ok = integrity_rows == [("ok",)]
        if not integrity_ok:
            raise GovernanceStoreError(f"SQLite integrity check 실패: {integrity_rows}")
        schema_version = self.migration_runner.verify(connection)
        if schema_version != self.migration_runner.latest_version:
            raise GovernanceStoreError(
                "Governance schema version 불일치: "
                f"expected={self.migration_runner.latest_version} actual={schema_version}"
            )
        health = GovernanceStoreHealth(
            path=self.path,
            filesystem=filesystem,
            schema_version=schema_version,
            integrity_ok=integrity_ok,
            journal_mode=str(self._pragma_value(connection, "journal_mode")).lower(),
            synchronous=int(self._pragma_value(connection, "synchronous")),
            foreign_keys=bool(self._pragma_value(connection, "foreign_keys")),
            trusted_schema=bool(self._pragma_value(connection, "trusted_schema")),
        )
        if not health.healthy:
            raise GovernanceStoreError(f"Governance Store runtime profile 불일치: {health}")
        return health
