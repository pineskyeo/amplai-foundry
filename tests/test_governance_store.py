from __future__ import annotations

import multiprocessing
import sqlite3
import time
from pathlib import Path

import pytest

from amplai_foundry.governance.filesystem import (
    FilesystemStatus,
    GovernanceFilesystemError,
    LocalFilesystemGuard,
    _parse_darwin_mounts,
    _parse_linux_mountinfo,
)
from amplai_foundry.governance.migrations import (
    INITIAL_MIGRATIONS,
    GovernanceMigrationError,
    Migration,
    MigrationRunner,
)
from amplai_foundry.governance.store import (
    GovernanceCommitAmbiguousError,
    GovernanceStore,
    GovernanceStoreError,
    GovernanceTransactionError,
    governance_transaction,
)


class FixedFilesystemProbe:
    def __init__(self, *, local: bool, kind: str = "testfs") -> None:
        self.local = local
        self.kind = kind

    def inspect(self, path: Path) -> FilesystemStatus:
        return FilesystemStatus(kind=self.kind, mount_point=path.parent, local=self.local)


class CommitFailingConnection:
    def __init__(self) -> None:
        self.in_transaction = False

    def execute(self, statement: str) -> None:
        if statement == "BEGIN IMMEDIATE":
            self.in_transaction = True
            return
        if statement == "COMMIT":
            raise sqlite3.OperationalError("ambiguous commit")
        if statement == "ROLLBACK":
            self.in_transaction = False


class BlockingV2MigrationRunner(MigrationRunner):
    def __init__(self, marker: Path) -> None:
        super().__init__()
        self.marker = marker

    def apply_pending(self, connection: sqlite3.Connection) -> int:
        def trace(statement: str) -> None:
            if "CREATE TABLE governance_definition_revisions" in statement:
                self.marker.write_text("ready", encoding="utf-8")
                time.sleep(60)

        connection.set_trace_callback(trace)
        return super().apply_pending(connection)


def _run_blocking_v2_migration(database: str, marker: str) -> None:
    store = GovernanceStore(
        Path(database),
        migration_runner=BlockingV2MigrationRunner(Path(marker)),
    )
    store.initialize()


def _run_blocking_migration(database: str, marker: str) -> None:
    checkpoint = Path(marker)
    migration = Migration(
        version=3,
        name="hard-kill-fixture",
        statements=(
            "CREATE TABLE hard_kill_partial (id INTEGER PRIMARY KEY)",
            "SELECT migration_checkpoint()",
        ),
    )
    runner = MigrationRunner((*INITIAL_MIGRATIONS, migration))
    store = GovernanceStore(Path(database), migration_runner=runner)
    with store.connect() as connection:

        def block_until_killed() -> int:
            checkpoint.write_text("ready", encoding="utf-8")
            time.sleep(60)
            return 0

        connection.create_function("migration_checkpoint", 0, block_until_killed)
        with governance_transaction(connection):
            runner.apply_pending(connection)


def test_initialize_creates_versioned_store_with_required_runtime_profile(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / ".amplai/runtime/governance.db")

    health = store.initialize()
    repeated = store.initialize()

    assert health.healthy
    assert health.schema_version == 2
    assert health.journal_mode == "wal"
    assert health.synchronous == 2
    assert health.foreign_keys
    assert not health.trusted_schema
    assert repeated.schema_version == health.schema_version
    with store.connect() as connection:
        migrations = connection.execute(
            "SELECT version, name FROM governance_schema_migrations ORDER BY version"
        ).fetchall()
        metadata = connection.execute(
            "SELECT value FROM governance_store_metadata WHERE key = 'store_kind'"
        ).fetchone()
    assert migrations == [
        (1, "governance-store-foundation"),
        (2, "active-proposal-cas"),
    ]
    assert metadata == ("amplai-governance",)


def test_network_filesystem_is_rejected_before_database_creation(tmp_path: Path) -> None:
    path = tmp_path / "governance.db"
    guard = LocalFilesystemGuard(FixedFilesystemProbe(local=False, kind="nfs"))
    store = GovernanceStore(path, filesystem_guard=guard)

    with pytest.raises(GovernanceFilesystemError, match="verified local filesystem"):
        store.initialize()

    assert not path.exists()


def test_version_one_store_upgrades_after_preflight_schema_verification(tmp_path: Path) -> None:
    path = tmp_path / "governance.db"
    version_one = GovernanceStore(
        path,
        migration_runner=MigrationRunner((INITIAL_MIGRATIONS[0],)),
    )
    assert version_one.initialize().schema_version == 1

    upgraded = GovernanceStore(path)
    assert upgraded.initialize().schema_version == 2
    with upgraded.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "governance_active_proposals" in tables
    assert "governance_definition_revisions" in tables


def test_hard_kill_between_actual_v2_ddl_statements_reopens_at_v1_then_upgrades(
    tmp_path: Path,
) -> None:
    path = tmp_path / "governance.db"
    marker = tmp_path / "v2-second-ddl-ready"
    version_one_runner = MigrationRunner((INITIAL_MIGRATIONS[0],))
    GovernanceStore(path, migration_runner=version_one_runner).initialize()
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_run_blocking_v2_migration,
        args=(str(path), str(marker)),
    )
    process.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and process.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "v2 migration이 second DDL checkpoint에 도달하지 못했습니다."

    process.kill()
    process.join(timeout=5)
    assert not process.is_alive()

    version_one = GovernanceStore(path, migration_runner=version_one_runner)
    assert version_one.check_startup().schema_version == 1
    with version_one.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        versions = connection.execute(
            "SELECT version FROM governance_schema_migrations ORDER BY version"
        ).fetchall()
    assert "governance_active_proposals" not in tables
    assert "governance_definition_revisions" not in tables
    assert versions == [(1,)]

    assert GovernanceStore(path).initialize().schema_version == 2


def test_linux_mount_parser_uses_longest_mount_and_fails_closed() -> None:
    payload = "\n".join(
        (
            "1 0 8:1 / / rw,relatime - ext4 /dev/root rw",
            "2 1 0:42 / /workspace rw,relatime - nfs server:/workspace rw",
            "malformed",
        )
    )

    root = _parse_linux_mountinfo(payload, Path("/var/lib/amplai/governance.db"))
    nested = _parse_linux_mountinfo(payload, Path("/workspace/amplai/governance.db"))

    assert root.local and root.kind == "ext4"
    assert not nested.local and nested.kind == "nfs"
    with pytest.raises(GovernanceFilesystemError, match="mount 정보를"):
        _parse_linux_mountinfo("malformed", Path("/workspace/governance.db"))


def test_darwin_mount_parser_requires_local_flag_and_rejects_remote_mount() -> None:
    payload = "\n".join(
        (
            "/dev/disk3s1 on / (apfs, sealed, local, journaled)",
            "server:/team on /Volumes/team (nfs, nodev, nosuid)",
        )
    )

    root = _parse_darwin_mounts(payload, Path("/Users/pinesky/governance.db"))
    remote = _parse_darwin_mounts(payload, Path("/Volumes/team/governance.db"))

    assert root.local and root.kind == "apfs"
    assert not remote.local and remote.kind == "nfs"


def test_transaction_commits_and_rolls_back_without_nested_boundaries(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()

    with store.connect() as connection:
        with governance_transaction(connection):
            connection.execute(
                "INSERT INTO governance_store_metadata(key, value) VALUES ('commit', 'yes')"
            )
        with (
            pytest.raises(RuntimeError, match="force rollback"),
            governance_transaction(connection),
        ):
            connection.execute(
                "INSERT INTO governance_store_metadata(key, value) VALUES ('rollback', 'no')"
            )
            raise RuntimeError("force rollback")
        with (
            governance_transaction(connection),
            pytest.raises(GovernanceTransactionError, match="nested"),
            governance_transaction(connection),
        ):
            pass
        rows = connection.execute(
            "SELECT key, value FROM governance_store_metadata ORDER BY key"
        ).fetchall()

    assert ("commit", "yes") in rows
    assert ("rollback", "no") not in rows


def test_commit_failure_uses_typed_ambiguous_result() -> None:
    connection = CommitFailingConnection()

    with (
        pytest.raises(GovernanceCommitAmbiguousError, match="결과가 불명확"),
        governance_transaction(connection),  # type: ignore[arg-type]
    ):
        pass

    assert not connection.in_transaction


def test_migration_entrypoint_requires_active_transaction(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()

    with (
        store.connect() as connection,
        pytest.raises(GovernanceMigrationError, match="active transaction"),
    ):
        store.migration_runner.apply_pending(connection)


def test_failed_migration_rolls_back_schema_and_history(tmp_path: Path) -> None:
    path = tmp_path / "governance.db"
    GovernanceStore(path).initialize()
    failing = Migration(
        version=3,
        name="failing-fixture",
        statements=(
            "CREATE TABLE migration_partial (id INTEGER PRIMARY KEY)",
            "INSERT INTO missing_table(id) VALUES (1)",
        ),
    )
    upgrade = GovernanceStore(
        path,
        migration_runner=MigrationRunner((*INITIAL_MIGRATIONS, failing)),
    )

    with pytest.raises(GovernanceStoreError, match="초기화할 수 없습니다"):
        upgrade.initialize()

    with sqlite3.connect(path) as connection:
        partial = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'migration_partial'"
        ).fetchone()
        versions = connection.execute(
            "SELECT version FROM governance_schema_migrations ORDER BY version"
        ).fetchall()
    assert partial is None
    assert versions == [(1,), (2,)]


def test_hard_kill_during_migration_reopens_at_previous_schema(tmp_path: Path) -> None:
    path = tmp_path / "governance.db"
    marker = tmp_path / "migration-ready"
    GovernanceStore(path).initialize()
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_run_blocking_migration, args=(str(path), str(marker)))
    process.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and process.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "migration subprocess가 checkpoint에 도달하지 못했습니다."

    process.kill()
    process.join(timeout=5)
    assert not process.is_alive()

    assert GovernanceStore(path).check_startup().schema_version == 2
    with sqlite3.connect(path) as connection:
        partial = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'hard_kill_partial'"
        ).fetchone()
    assert partial is None


def test_migration_history_tampering_fails_closed(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    with store.connect() as connection, governance_transaction(connection):
        connection.execute(
            "UPDATE governance_schema_migrations SET checksum = 'tampered' WHERE version = 1"
        )

    with pytest.raises(GovernanceMigrationError, match="contract와 다릅니다"):
        store.check_startup()


def test_required_schema_drift_fails_startup_check(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    with store.connect() as connection, governance_transaction(connection):
        connection.execute("DROP TABLE governance_store_metadata")

    with pytest.raises(GovernanceMigrationError, match="schema shape"):
        store.check_startup()


def test_required_schema_constraint_drift_fails_startup_check(tmp_path: Path) -> None:
    path = tmp_path / "governance.db"
    store = GovernanceStore(path)
    store.initialize()
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'governance_active_proposals'"
        ).fetchone()
        assert row is not None
        drifted = str(row[0]).replace(
            "content_revision INTEGER NOT NULL CHECK (content_revision >= 1)",
            "content_revision INTEGER NOT NULL",
        )
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE name = 'governance_active_proposals'",
            (drifted,),
        )
        connection.execute("PRAGMA writable_schema = OFF")

    with pytest.raises(GovernanceMigrationError, match="schema SQL"):
        store.check_startup()


def test_orphan_definition_revision_fails_startup_foreign_key_check(tmp_path: Path) -> None:
    path = tmp_path / "governance.db"
    store = GovernanceStore(path)
    store.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO governance_definition_revisions(
                project_namespace, project_id, proposal_id, content_revision,
                definition_digest, previous_definition_digest, activated_from_status,
                activated_at
            )
            VALUES (?, ?, ?, 1, ?, NULL, NULL, '2026-07-30T00:00:00Z')
            """,
            (
                "org/default/project/missing",
                "missing",
                "PROP-20260730-ABCDEF12",
                f"sha256:{'1' * 64}",
            ),
        )

    with pytest.raises(GovernanceStoreError, match="foreign key integrity"):
        store.check_startup()


def test_future_schema_version_fails_closed(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    with store.connect() as connection, governance_transaction(connection):
        connection.execute(
            """
            INSERT INTO governance_schema_migrations(version, name, checksum, applied_at)
            VALUES (99, 'future', 'future', '2026-07-30T00:00:00Z')
            """
        )

    with pytest.raises(GovernanceMigrationError, match="future schema"):
        store.initialize()


def test_startup_check_requires_existing_healthy_database(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / "governance.db")

    with pytest.raises(GovernanceStoreError, match="존재하지 않습니다"):
        store.check_startup()

    store.initialize()
    assert store.check_startup().healthy


def test_invalid_busy_timeout_is_rejected() -> None:
    from amplai_foundry.governance.store import GovernanceStoreConfig

    with pytest.raises(ValueError, match="0 이상"):
        GovernanceStoreConfig(busy_timeout_ms=-1)


def test_corrupt_database_is_reported_as_store_error(tmp_path: Path) -> None:
    path = tmp_path / "governance.db"
    path.write_bytes(b"not-a-sqlite-database")
    store = GovernanceStore(path)

    with pytest.raises((GovernanceStoreError, sqlite3.DatabaseError)):
        store.check_startup()
