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


class GovernanceCommitAmbiguousError(GovernanceTransactionError):
    """A failed COMMIT requires command-level result reconciliation."""


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
    except sqlite3.Error as error:
        raise GovernanceTransactionError("Governance transaction을 시작할 수 없습니다.") from error
    try:
        yield connection
    except BaseException as original_error:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error as rollback_failure:
                transaction_error = GovernanceTransactionError(
                    "Governance transaction rollback에 실패했습니다."
                )
                transaction_error.add_note(f"original error: {original_error!r}")
                raise transaction_error from rollback_failure
        raise
    try:
        connection.execute("COMMIT")
    except sqlite3.Error as commit_error:
        rollback_error: sqlite3.Error | None = None
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error as error:
                rollback_error = error
        ambiguous = GovernanceCommitAmbiguousError(
            "Governance COMMIT 결과가 불명확합니다. command result를 조회해야 합니다."
        )
        if rollback_error is not None:
            ambiguous.add_note(f"rollback error: {rollback_error!r}")
        raise ambiguous from commit_error


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
        existing_store = self.path.is_file() and self.path.stat().st_size > 0
        try:
            with self._raw_connection() as connection:
                if existing_store:
                    self._verify_integrity(connection)
                    current_version = self.migration_runner.verify(connection)
                    self.migration_runner.verify_schema(connection, current_version)
                self._configure(connection)
                with governance_transaction(connection):
                    if existing_store and current_version >= 21:
                        from amplai_foundry.governance.legacy_migration import (
                            LegacyProposalImportService,
                        )

                        LegacyProposalImportService.reconcile_verification_roots(connection)
                    self.migration_runner.apply_pending(connection)
                health = self._health(connection, filesystem)
                self._reconcile_events(connection, health)
                return health
        except sqlite3.Error as error:
            raise GovernanceStoreError(
                f"Governance Store를 초기화할 수 없습니다: {error}"
            ) from error

    @contextmanager
    def connect(self, *, busy_timeout_ms: int | None = None) -> Iterator[sqlite3.Connection]:
        effective_timeout = (
            self.config.busy_timeout_ms if busy_timeout_ms is None else busy_timeout_ms
        )
        if effective_timeout < 0:
            raise ValueError("busy_timeout_ms는 0 이상이어야 합니다.")
        before = self.filesystem_guard.validate(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._raw_connection(busy_timeout_ms=effective_timeout) as connection:
            self._configure(connection, busy_timeout_ms=effective_timeout)
            after = self.filesystem_guard.validate(self.path)
            if before != after:
                raise GovernanceStoreError(
                    f"filesystem identity가 connection open 중 변경됐습니다: {before} -> {after}"
                )
            database_row = connection.execute("PRAGMA database_list").fetchone()
            database_path = (
                Path(str(database_row[2])).resolve(strict=False)
                if database_row is not None
                else None
            )
            if database_path != self.path:
                raise GovernanceStoreError(
                    f"opened database path가 요청과 다릅니다: {database_path} != {self.path}"
                )
            yield connection

    @contextmanager
    def _raw_connection(
        self, *, busy_timeout_ms: int | None = None
    ) -> Iterator[sqlite3.Connection]:
        effective_timeout = (
            self.config.busy_timeout_ms if busy_timeout_ms is None else busy_timeout_ms
        )
        try:
            connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=effective_timeout / 1_000,
            )
        except sqlite3.Error as error:
            raise GovernanceStoreError(f"Governance Store에 연결할 수 없습니다: {error}") from error
        try:
            connection.execute(f"PRAGMA busy_timeout = {effective_timeout}")
            yield connection
        finally:
            connection.close()

    def check_startup(self) -> GovernanceStoreHealth:
        if not self.path.is_file():
            raise GovernanceStoreError(f"Governance Store가 존재하지 않습니다: {self.path}")
        filesystem = self.filesystem_guard.validate(self.path)
        try:
            with self.connect() as connection:
                health = self._health(connection, filesystem)
                self._reconcile_events(connection, health)
                return health
        except sqlite3.Error as error:
            raise GovernanceStoreError(f"Governance Store startup check 실패: {error}") from error

    @staticmethod
    def _reconcile_events(
        connection: sqlite3.Connection,
        health: GovernanceStoreHealth,
    ) -> None:
        if health.schema_version < 6:
            return
        from amplai_foundry.governance.events import GovernanceEventService

        GovernanceEventService.reconcile_connection(connection)
        if health.schema_version >= 21:
            from amplai_foundry.governance.legacy_migration import LegacyProposalImportService

            LegacyProposalImportService.reconcile_verification_roots(connection)

    def _configure(
        self,
        connection: sqlite3.Connection,
        *,
        busy_timeout_ms: int | None = None,
    ) -> None:
        effective_timeout = (
            self.config.busy_timeout_ms if busy_timeout_ms is None else busy_timeout_ms
        )
        connection.execute(f"PRAGMA busy_timeout = {effective_timeout}")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA foreign_keys = ON")
        journal_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        journal_mode = str(journal_row[0]).lower() if journal_row is not None else ""
        if journal_mode != "wal":
            raise GovernanceStoreError(f"journal_mode=WAL을 활성화할 수 없습니다: {journal_mode}")
        connection.execute("PRAGMA synchronous = FULL")
        self._verify_pragmas(connection, busy_timeout_ms=effective_timeout)

    @staticmethod
    def _pragma_value(connection: sqlite3.Connection, name: str) -> str | int:
        row = connection.execute(f"PRAGMA {name}").fetchone()
        if row is None:
            raise GovernanceStoreError(f"PRAGMA 값을 확인할 수 없습니다: {name}")
        value = row[0]
        if not isinstance(value, (str, int)):
            raise GovernanceStoreError(f"PRAGMA 값의 type이 올바르지 않습니다: {name}")
        return value

    def _verify_pragmas(
        self,
        connection: sqlite3.Connection,
        *,
        busy_timeout_ms: int | None = None,
    ) -> None:
        effective_timeout = (
            self.config.busy_timeout_ms if busy_timeout_ms is None else busy_timeout_ms
        )
        expected = {
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
            "trusted_schema": 0,
            "busy_timeout": effective_timeout,
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
        self._verify_integrity(connection)
        schema_version = self.migration_runner.verify(connection)
        if schema_version != self.migration_runner.latest_version:
            raise GovernanceStoreError(
                "Governance schema version 불일치: "
                f"expected={self.migration_runner.latest_version} actual={schema_version}"
            )
        self.migration_runner.verify_schema(connection, schema_version)
        self._probe_wal_write(connection)
        health = GovernanceStoreHealth(
            path=self.path,
            filesystem=filesystem,
            schema_version=schema_version,
            integrity_ok=True,
            journal_mode=str(self._pragma_value(connection, "journal_mode")).lower(),
            synchronous=int(self._pragma_value(connection, "synchronous")),
            foreign_keys=bool(self._pragma_value(connection, "foreign_keys")),
            trusted_schema=bool(self._pragma_value(connection, "trusted_schema")),
        )
        if not health.healthy:
            raise GovernanceStoreError(f"Governance Store runtime profile 불일치: {health}")
        return health

    @staticmethod
    def _verify_integrity(connection: sqlite3.Connection) -> None:
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity_rows != [("ok",)]:
            raise GovernanceStoreError(f"SQLite integrity check 실패: {integrity_rows}")
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_rows:
            raise GovernanceStoreError(
                f"SQLite foreign key integrity check 실패: {foreign_key_rows}"
            )

    @staticmethod
    def _probe_wal_write(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            raise GovernanceStoreError("WAL write probe는 active transaction 밖에서 실행합니다.")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE governance_store_metadata
                SET value = value
                WHERE key = 'store_kind'
                """
            )
            connection.execute("ROLLBACK")
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise GovernanceStoreError("WAL/SHM write probe 실패") from error
