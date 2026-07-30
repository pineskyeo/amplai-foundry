"""Versioned SQLite migrations for mutable governance state."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import cast


class GovernanceMigrationError(RuntimeError):
    """The Governance Store schema cannot be verified or migrated safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = f"{self.version}\n{self.name}\n" + "\n-- statement --\n".join(self.statements)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


INITIAL_MIGRATIONS = (
    Migration(
        version=1,
        name="governance-store-foundation",
        statements=(
            """
            CREATE TABLE governance_store_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID
            """,
            """
            INSERT INTO governance_store_metadata(key, value)
            VALUES ('store_kind', 'amplai-governance')
            """,
        ),
    ),
    Migration(
        version=2,
        name="active-proposal-cas",
        statements=(
            """
            CREATE TABLE governance_active_proposals (
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                active_definition_digest TEXT NOT NULL,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
                decision_epoch INTEGER NOT NULL CHECK (decision_epoch >= 1),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'draft',
                        'reviewed',
                        'changes_requested',
                        'approved',
                        'apply_requested',
                        'apply_failed',
                        'applied',
                        'rejected',
                        'superseded'
                    )
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_namespace, project_id, proposal_id),
                CHECK (
                    length(active_definition_digest) = 71
                    AND substr(active_definition_digest, 1, 7) = 'sha256:'
                    AND substr(active_definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE governance_definition_revisions (
                project_namespace TEXT NOT NULL,
                project_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                content_revision INTEGER NOT NULL CHECK (content_revision >= 1),
                definition_digest TEXT NOT NULL,
                previous_definition_digest TEXT,
                activated_from_status TEXT,
                activated_at TEXT NOT NULL,
                PRIMARY KEY (
                    project_namespace,
                    project_id,
                    proposal_id,
                    content_revision
                ),
                UNIQUE (
                    project_namespace,
                    project_id,
                    proposal_id,
                    definition_digest
                ),
                FOREIGN KEY (project_namespace, project_id, proposal_id)
                    REFERENCES governance_active_proposals(
                        project_namespace,
                        project_id,
                        proposal_id
                    )
                    ON DELETE RESTRICT,
                CHECK (
                    length(definition_digest) = 71
                    AND substr(definition_digest, 1, 7) = 'sha256:'
                    AND substr(definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                ),
                CHECK (
                    previous_definition_digest IS NULL
                    OR (
                        length(previous_definition_digest) = 71
                        AND substr(previous_definition_digest, 1, 7) = 'sha256:'
                        AND substr(previous_definition_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                CHECK (
                    activated_from_status IS NULL
                    OR activated_from_status IN ('draft', 'changes_requested')
                ),
                CHECK (
                    (content_revision = 1 AND previous_definition_digest IS NULL)
                    OR
                    (content_revision > 1 AND previous_definition_digest IS NOT NULL)
                )
            ) WITHOUT ROWID
            """,
        ),
    ),
)


class MigrationRunner:
    def __init__(self, migrations: tuple[Migration, ...] = INITIAL_MIGRATIONS) -> None:
        versions = tuple(migration.version for migration in migrations)
        if versions != tuple(range(1, len(migrations) + 1)):
            raise GovernanceMigrationError("migration version은 1부터 연속이어야 합니다.")
        self.migrations = migrations

    @property
    def latest_version(self) -> int:
        return self.migrations[-1].version if self.migrations else 0

    @staticmethod
    def _ensure_history_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS governance_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _history_table_exists(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'governance_schema_migrations'
            """
        ).fetchone()
        return row is not None

    def current_version(self, connection: sqlite3.Connection) -> int:
        return self.verify(connection)

    def _applied_rows(self, connection: sqlite3.Connection) -> list[tuple[int, str, str]]:
        if not self._history_table_exists(connection):
            raise GovernanceMigrationError("migration history table이 없습니다.")
        return cast(
            list[tuple[int, str, str]],
            connection.execute(
                """
                SELECT version, name, checksum
                FROM governance_schema_migrations
                ORDER BY version
                """
            ).fetchall(),
        )

    def _verify_rows(self, applied_rows: list[tuple[int, str, str]]) -> int:
        known = {migration.version: migration for migration in self.migrations}
        for version, _name, _checksum in applied_rows:
            if version not in known:
                raise GovernanceMigrationError(
                    f"지원하지 않는 future schema version입니다: {version}"
                )
        applied_versions = tuple(row[0] for row in applied_rows)
        if applied_versions != tuple(range(1, len(applied_versions) + 1)):
            raise GovernanceMigrationError(
                f"migration history version이 연속적이지 않습니다: {applied_versions}"
            )
        for version_value, name_value, checksum_value in applied_rows:
            version = version_value
            migration = known[version]
            if str(name_value) != migration.name or str(checksum_value) != migration.checksum:
                raise GovernanceMigrationError(
                    f"migration history가 현재 contract와 다릅니다: version={version}"
                )
        return applied_versions[-1] if applied_versions else 0

    def verify(self, connection: sqlite3.Connection) -> int:
        return self._verify_rows(self._applied_rows(connection))

    def verify_schema(self, connection: sqlite3.Connection, schema_version: int) -> None:
        expected_columns = {
            "governance_schema_migrations": (
                ("version", "INTEGER", 0, 1),
                ("name", "TEXT", 1, 0),
                ("checksum", "TEXT", 1, 0),
                ("applied_at", "TEXT", 1, 0),
            ),
            "governance_store_metadata": (
                ("key", "TEXT", 1, 1),
                ("value", "TEXT", 1, 0),
            ),
        }
        if schema_version >= 2:
            expected_columns.update(
                {
                    "governance_active_proposals": (
                        ("project_namespace", "TEXT", 1, 1),
                        ("project_id", "TEXT", 1, 2),
                        ("proposal_id", "TEXT", 1, 3),
                        ("active_definition_digest", "TEXT", 1, 0),
                        ("content_revision", "INTEGER", 1, 0),
                        ("state_revision", "INTEGER", 1, 0),
                        ("decision_epoch", "INTEGER", 1, 0),
                        ("status", "TEXT", 1, 0),
                        ("created_at", "TEXT", 1, 0),
                        ("updated_at", "TEXT", 1, 0),
                    ),
                    "governance_definition_revisions": (
                        ("project_namespace", "TEXT", 1, 1),
                        ("project_id", "TEXT", 1, 2),
                        ("proposal_id", "TEXT", 1, 3),
                        ("content_revision", "INTEGER", 1, 4),
                        ("definition_digest", "TEXT", 1, 0),
                        ("previous_definition_digest", "TEXT", 0, 0),
                        ("activated_from_status", "TEXT", 0, 0),
                        ("activated_at", "TEXT", 1, 0),
                    ),
                }
            )
        for table, expected in expected_columns.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual = tuple(
                (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows
            )
            if actual != expected:
                raise GovernanceMigrationError(
                    "required schema shape 불일치: "
                    f"table={table} expected={expected} actual={actual}"
                )
        expected_create_sql = {
            statement.split("CREATE TABLE ", 1)[1].split(maxsplit=1)[0]: " ".join(statement.split())
            for migration in self.migrations
            if migration.version <= schema_version
            for statement in migration.statements
            if "CREATE TABLE " in statement
        }
        for table, expected_sql in expected_create_sql.items():
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            actual_sql = " ".join(str(row[0]).split()) if row is not None else ""
            if actual_sql != expected_sql:
                raise GovernanceMigrationError(f"required schema SQL 불일치: table={table}")
        metadata = connection.execute(
            "SELECT value FROM governance_store_metadata WHERE key = 'store_kind'"
        ).fetchone()
        if metadata != ("amplai-governance",):
            raise GovernanceMigrationError("Governance Store metadata가 올바르지 않습니다.")
        if schema_version >= 2:
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(governance_definition_revisions)"
            ).fetchall()
            if len(foreign_keys) != 3 or any(
                str(row[2]) != "governance_active_proposals" for row in foreign_keys
            ):
                raise GovernanceMigrationError(
                    "definition revision foreign key가 올바르지 않습니다."
                )

    def apply_pending(self, connection: sqlite3.Connection) -> int:
        if not connection.in_transaction:
            raise GovernanceMigrationError(
                "migration entrypoint는 active transaction이 필요합니다."
            )
        self._ensure_history_table(connection)
        current = self._verify_rows(self._applied_rows(connection))
        for migration in self.migrations:
            if migration.version <= current:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO governance_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (migration.version, migration.name, migration.checksum),
            )
        return self.latest_version
