from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from amplai_foundry.governance.filesystem import (
    FilesystemStatus,
    GovernanceFilesystemError,
    LocalFilesystemGuard,
)
from amplai_foundry.governance.migrations import GovernanceMigrationError
from amplai_foundry.governance.store import (
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


def test_initialize_creates_versioned_store_with_required_runtime_profile(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / ".amplai/runtime/governance.db")

    health = store.initialize()
    repeated = store.initialize()

    assert health.healthy
    assert health.schema_version == 1
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
    assert migrations == [(1, "governance-store-foundation")]
    assert metadata == ("amplai-governance",)


def test_network_filesystem_is_rejected_before_database_creation(tmp_path: Path) -> None:
    path = tmp_path / "governance.db"
    guard = LocalFilesystemGuard(FixedFilesystemProbe(local=False, kind="nfs"))
    store = GovernanceStore(path, filesystem_guard=guard)

    with pytest.raises(GovernanceFilesystemError, match="verified local filesystem"):
        store.initialize()

    assert not path.exists()


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


def test_migration_history_tampering_fails_closed(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    with store.connect() as connection, governance_transaction(connection):
        connection.execute(
            "UPDATE governance_schema_migrations SET checksum = 'tampered' WHERE version = 1"
        )

    with pytest.raises(GovernanceMigrationError, match="contract와 다릅니다"):
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
