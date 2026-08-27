"""SQLite authority foundation for mutable governance state."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from amplai_foundry.governance.filesystem import (
    FilesystemStatus,
    GovernanceFilesystemError,
    LocalFilesystemGuard,
)
from amplai_foundry.governance.migrations import MigrationRunner

# `connect()` 가 여는 동안 날 수 있는 실패 중 **정규화하는 것** (MGC-012-P5-T024, T029).
#
# **호출자를 세지 않으려고 경계에서 정규화한다.** `connect()` 호출 지점이 15개 module 62곳
# 이다. 호출자마다 `except` 절에 class 를 하나씩 더하는 방식은 그 62곳을 세는 일이고, 다섯
# 라운드 연속 실패한 바로 그 작업이다.
#
# **`sqlite3.Error` 는 일부러 뺐다 (round 15 `R-1`).** 처음에는 넣었는데, `legacy_*.py` 가
# `sqlite3.Error` 를 잡아 domain error 로 닫고 있었다. 정규화하면 그 봉쇄가 전부 사라지고
# raw `GovernanceStoreError` 가 module 경계를 넘는다. **test 가 없어서 suite 는 조용했다.**
#
# **이 자리의 "아홉" 은 두 가지를 섞고 있었다** (round 17 `A17-5`). wave 11 이 AST 로 갈라
# 셌다.
#
# **`connect()` 를 감싸며 `sqlite3.Error` 를 잡는 `try` — 열.**
#   `legacy_lifecycle:156`, `legacy_migration:822,1068,1113,1168,1665`,
#   `legacy_recovery:205,412`, `legacy_rollback:179,548`
#
# **`sqlite3.Error` 계열을 잡는 `except` handler — 열셋.**
#   `legacy_lifecycle:349`, `legacy_migration:1014,1092,1141,1240,1691,2298`,
#   `legacy_recovery:276,632,634`, `legacy_rollback:242,730,734`
#
# 옛 목록은 **앞쪽**을 센 것이고 당시 9였다 — `legacy_migration:1665` 가 그 뒤에 늘었다.
# round 17 이 실측한 13은 **뒤쪽**이다. 둘 다 아홉이 아니고, 파일 수(다섯)도 아니다.
# 한 줄 grep 은 `legacy_recovery:634` 의 여러 줄 `except` 를 놓쳐 12 를 내므로 AST 로 센다.
# `src` 전체로 넓히면 앞쪽은 **14** 다 (legacy 10 + `ingress:221` + `ingress_worker:201`
# + `store:265,286`).
#
# round 14 `P1-1` 이 요구한 것은 `GovernanceFilesystemError` 하나였다. 나머지는 "형제를
# 전수로 센다" 를 과하게 적용한 결과다. **세는 것은 옳았고 덮는 범위를 넓힌 것이 틀렸다.**
#
# 그래서 `_configure` 의 PRAGMA, `PRAGMA database_list`, `_raw_connection` 의 PRAGMA 에서
# 나는 `sqlite3.Error` 는 HEAD 처럼 그대로 나간다. `sqlite3.connect` 자체의 실패는
# `_raw_connection` 이 예전부터 감싸고 있고 그것은 유지한다.
#
# **`Exception` 으로 넓히지 않는다.** `AttributeError`·`TypeError` 같은 프로그래밍 오류는
# 그대로 시끄럽게 나가야 한다. 정규화가 결함을 durable 하게 숨기면 안 된다.
#
# `GovernanceStoreError` 는 `RuntimeError` 라 이 tuple 에 걸리지 않는다 — 이미 정규화된
# 것은 그대로 지나간다.
_OPEN_FAILURES: tuple[type[BaseException], ...] = (
    GovernanceFilesystemError,
    OSError,
)


class GovernanceStoreError(RuntimeError):
    """The Governance Store cannot satisfy its runtime contract."""


class GovernanceTransactionError(GovernanceStoreError):
    """A Governance Store transaction could not complete safely."""


class GovernanceCommitAmbiguousError(GovernanceTransactionError):
    """A failed COMMIT requires command-level result reconciliation."""


def is_store_corruption(error: BaseException) -> bool:
    """Say whether one failure means the store itself is unusable, not merely busy.

    이 구분이 재시도할지 멈출지를 정한다. `database is locked` 는 몇 초 뒤 풀리지만
    `SQLITE_CORRUPT` 는 다시 시도해도 같다. 손상만 즉시 terminal 로 보낸다.

    **정의는 저장소에 하나만 둔다.** 원래 `ingress_worker` 안에만 있어서 배달 경로는 같은
    예외에 다른 정책을 줬다 — ingress 는 재시도, 배달은 즉시 되돌릴 수 없는 hold 였다
    (round 11 `R-2`). 사본을 만들지 않는다.

    `sqlite_errorcode` 는 확장 code 를 담으므로 하위 byte 만 비교한다.
    """
    if not isinstance(error, sqlite3.DatabaseError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    return code & 0xFF in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


def is_transient_store_failure(error: BaseException) -> bool:
    """Say whether one failure is worth another attempt.

    `ingress_worker` 가 이미 쓰던 순서를 그대로 옮겼다 — 손상이면 terminal, 그 외
    store 관련 실패는 재시도. `disk I/O error`, `database or disk is full`,
    `readonly database` 는 전부 재시도 쪽이다. 그쪽이 보수적이다: 재시도로 잘못 분류해도
    예산을 소진하면 같은 dead letter 에 도달하지만, terminal 로 잘못 분류하면 destination
    이 즉시 멈추고 그 hold 는 되돌릴 수 없다.
    """
    if is_store_corruption(error):
        return False
    return isinstance(error, (GovernanceStoreError, sqlite3.Error))


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
                    if existing_store and current_version >= 24:
                        from amplai_foundry.governance.legacy_lifecycle import (
                            LegacyMigrationActivationService,
                        )

                        LegacyMigrationActivationService.reconcile_roots(connection)
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
        # 여는 단계의 모든 실패를 `GovernanceStoreError` 로 정규화한다 (T024).
        # `yield` 는 두 `try` 밖이다 — 호출자가 던진 예외까지 삼키면 안 된다.
        try:
            before = self.filesystem_guard.validate(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except _OPEN_FAILURES as error:
            raise GovernanceStoreError(
                f"Governance Store를 열기 전 검사에 실패했습니다: {error}"
            ) from error
        with self._raw_connection(busy_timeout_ms=effective_timeout) as connection:
            try:
                self._configure(connection, busy_timeout_ms=effective_timeout)
                after = self.filesystem_guard.validate(self.path)
                if before != after:
                    raise GovernanceStoreError(
                        "filesystem identity가 connection open 중 변경됐습니다: "
                        f"{before} -> {after}"
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
            except _OPEN_FAILURES as error:
                raise GovernanceStoreError(
                    f"Governance Store connection을 열 수 없습니다: {error}"
                ) from error
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
            # 이 PRAGMA 의 `sqlite3.Error` 는 정규화하지 않는다. T024 가 감쌌다가 round 15
            # `R-1` 로 되돌렸다 — `legacy_*.py` 의 `try` **열**이 `sqlite3.Error` 를 잡아
            # domain error 로 닫고 있었고 정규화가 그 봉쇄를 없앴다.
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
        if health.schema_version >= 24:
            from amplai_foundry.governance.legacy_lifecycle import (
                LegacyMigrationActivationService,
            )

            LegacyMigrationActivationService.reconcile_roots(connection)
        if health.schema_version >= 30:
            from amplai_foundry.governance.legacy_recovery import (
                LegacyMigrationForwardRecoveryExecutor,
            )

            LegacyMigrationForwardRecoveryExecutor.reconcile_roots(connection)

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
