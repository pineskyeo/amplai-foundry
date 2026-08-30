"""Lease-based transactional outbox delivery."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime

from amplai_foundry.control_plane.connectors import ConnectorRegistry
from amplai_foundry.control_plane.errors import LeaseError, NotFoundError
from amplai_foundry.control_plane.models import OutboxItem
from amplai_foundry.control_plane.store import ControlPlaneStore, utc_after, utc_now


def _expired(value: str | None) -> bool:
    if not value:
        return True
    return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(UTC)


def _item(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        outbox_id=str(row["outbox_id"]),
        tenant_id=str(row["tenant_id"]),
        project_id=str(row["project_id"]),
        destination=str(row["destination"]),
        event_type=str(row["event_type"]),
        aggregate_ref=str(row["aggregate_ref"]),
        payload=json.loads(str(row["payload_json"])),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        lease_token=str(row["lease_token"]) if row["lease_token"] else None,
        lease_expires_at=str(row["lease_expires_at"]) if row["lease_expires_at"] else None,
        last_error=str(row["last_error"]) if row["last_error"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class OutboxQueue:
    def __init__(self, store: ControlPlaneStore) -> None:
        self.store = store

    def _recover_expired(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT * FROM cp_outbox WHERE status='running' AND lease_expires_at IS NOT NULL"
        ).fetchall()
        for row in rows:
            if not _expired(str(row["lease_expires_at"])):
                continue
            attempts = int(row["attempts"])
            status = "pending" if attempts < int(row["max_attempts"]) else "dead"
            connection.execute(
                """
                UPDATE cp_outbox SET status=?, lease_token=NULL, lease_expires_at=NULL,
                    last_error='lease expired', updated_at=? WHERE outbox_id=?
                """,
                (status, utc_now(), row["outbox_id"]),
            )

    def claim_next(
        self, *, destination: str | None = None, lease_seconds: int = 60
    ) -> OutboxItem | None:
        with self.store.transaction() as connection:
            self._recover_expired(connection)
            clause = ""
            params: list[object] = []
            if destination is not None:
                clause = " AND destination=?"
                params.append(destination)
            row = connection.execute(
                "SELECT * FROM cp_outbox WHERE status='pending'"
                + clause
                + " ORDER BY created_at,outbox_id LIMIT 1",
                tuple(params),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(24)
            cursor = connection.execute(
                """
                UPDATE cp_outbox SET status='running', attempts=attempts+1,
                    lease_token=?, lease_expires_at=?, updated_at=?
                WHERE outbox_id=? AND status='pending'
                """,
                (token, utc_after(lease_seconds), utc_now(), row["outbox_id"]),
            )
            if cursor.rowcount != 1:
                return None
            current = connection.execute(
                "SELECT * FROM cp_outbox WHERE outbox_id=?", (row["outbox_id"],)
            ).fetchone()
            assert current is not None
            return _item(current)

    def delivered(self, outbox_id: str, lease_token: str, receipt: str) -> OutboxItem:
        with self.store.transaction() as connection:
            self._require_lease(connection, outbox_id, lease_token)
            connection.execute(
                """
                UPDATE cp_outbox SET status='delivered', lease_token=NULL,
                    lease_expires_at=NULL, receipt=?, last_error=NULL, updated_at=?
                WHERE outbox_id=?
                """,
                (receipt, utc_now(), outbox_id),
            )
            current = connection.execute(
                "SELECT * FROM cp_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            assert current is not None
            return _item(current)

    def failed(self, outbox_id: str, lease_token: str, error: str) -> OutboxItem:
        with self.store.transaction() as connection:
            row = self._require_lease(connection, outbox_id, lease_token)
            status = "pending" if int(row["attempts"]) < int(row["max_attempts"]) else "dead"
            connection.execute(
                """
                UPDATE cp_outbox SET status=?, lease_token=NULL,
                    lease_expires_at=NULL, last_error=?, updated_at=? WHERE outbox_id=?
                """,
                (status, error, utc_now(), outbox_id),
            )
            current = connection.execute(
                "SELECT * FROM cp_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            assert current is not None
            return _item(current)

    @staticmethod
    def _require_lease(connection: sqlite3.Connection, outbox_id: str, token: str) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM cp_outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("OUTBOX_NOT_FOUND")
        if row["status"] != "running" or row["lease_token"] != token:
            raise LeaseError("OUTBOX_LEASE_MISMATCH")
        if _expired(str(row["lease_expires_at"]) if row["lease_expires_at"] else None):
            raise LeaseError("OUTBOX_LEASE_EXPIRED")
        return row


class OutboxDispatcher:
    def __init__(self, queue: OutboxQueue, connectors: ConnectorRegistry) -> None:
        self.queue = queue
        self.connectors = connectors

    def run_once(self, *, destination: str | None = None) -> OutboxItem | None:
        item = self.queue.claim_next(destination=destination)
        if item is None:
            return None
        assert item.lease_token is not None
        try:
            receipt = self.connectors.get(item.destination).send(item)
        except Exception as error:
            return self.queue.failed(item.outbox_id, item.lease_token, str(error))
        return self.queue.delivered(item.outbox_id, item.lease_token, receipt)
