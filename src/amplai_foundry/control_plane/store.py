"""Durable SQLite store for the AMPLAI Platform 0.4 Control Plane.

This is deliberately a separate bounded context from the existing Governance
Store.  Platform 0.4 can therefore be adopted without rewriting the proven
proposal/authority tables or the repository-local Loop Kit Project Store.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from amplai_foundry.control_plane.errors import ConflictError

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_after(seconds: int) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ControlPlaneStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cp_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cp_api_tokens(
                    token_id TEXT PRIMARY KEY,
                    token_digest TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    permissions_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS cp_idempotency(
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, project_id, route, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS cp_objects(
                    canonical_ref TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('decision','evidence')),
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    origin_store TEXT NOT NULL,
                    origin_ref TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, project_id, kind, origin_store, origin_ref)
                );
                CREATE TABLE IF NOT EXISTS cp_jobs(
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','done','dead')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    result_json TEXT,
                    last_error TEXT,
                    not_before TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cp_jobs_ready
                    ON cp_jobs(status, not_before, created_at);
                CREATE TABLE IF NOT EXISTS cp_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    schema_version INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    aggregate_ref TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    causation_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cp_outbox(
                    outbox_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL REFERENCES cp_events(event_id),
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    aggregate_ref TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','delivered','dead')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    last_error TEXT,
                    receipt TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cp_outbox_ready
                    ON cp_outbox(status, destination, created_at);
                CREATE TABLE IF NOT EXISTS cp_projections(
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, project_id, name)
                );
                CREATE TABLE IF NOT EXISTS cp_connector_configs(
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    connector_kind TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    secret_ref TEXT,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, project_id, destination)
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO cp_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    @staticmethod
    def idempotency_lookup(
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        project_id: str,
        route: str,
        key: str,
        request_digest: str,
    ) -> tuple[int, dict[str, Any]] | None:
        row = connection.execute(
            """
            SELECT request_digest, response_json, status_code
            FROM cp_idempotency
            WHERE tenant_id=? AND project_id=? AND route=? AND idempotency_key=?
            """,
            (tenant_id, project_id, route, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise ConflictError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST")
        return int(row["status_code"]), json.loads(str(row["response_json"]))

    @staticmethod
    def idempotency_store(
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        project_id: str,
        route: str,
        key: str,
        request_digest: str,
        response: dict[str, Any],
        status_code: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO cp_idempotency(
                tenant_id, project_id, route, idempotency_key,
                request_digest, response_json, status_code, created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                tenant_id,
                project_id,
                route,
                key,
                request_digest,
                canonical_json(response),
                status_code,
                utc_now(),
            ),
        )
