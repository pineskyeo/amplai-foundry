"""Durable, secret-free Work status projection boundary for Slack.

Project Store remains authoritative.  This module owns only an idempotent local
outbox: a delivery failure remains pending and never writes back to a Work.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class WorkStatusProjection(BaseModel):
    """Safe Slack-facing representation of one authoritative Work snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    revision: int = Field(ge=1)
    request_ref: str | None = None
    attempt: int = Field(ge=0, default=0)
    next_human_action: str | None = None
    evidence_refs: tuple[str, ...] = ()
    verification_summary: str | None = Field(default=None, max_length=240)

    @property
    def idempotency_key(self) -> str:
        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return f"work-status:{hashlib.sha256(canonical).hexdigest()}"


class SlackWorkStatusTransport(Protocol):
    """A Slack implementation updates a single authoritative status message."""

    def upsert_work_status(
        self, projection: WorkStatusProjection, message_ref: str | None
    ) -> str: ...


class SlackApiWorkStatusTransport:
    """Slack Web API adapter with a stable per-Work message identity.

    It needs only ``post_message`` and ``update_message`` so the existing
    hardened HTTP transport can supply both calls without this domain module
    importing credential or network code.
    """

    def __init__(
        self,
        transport: Any,
        *,
        channel_id: str,
        app_id: str,
        max_history_pages: int = 1,
    ) -> None:
        if not app_id.strip():
            raise ValueError("Slack work-status app_id must not be blank.")
        if max_history_pages < 1:
            raise ValueError("max_history_pages must be at least one.")
        self.transport = transport
        self.channel_id = channel_id
        self.app_id = app_id
        self.max_history_pages = max_history_pages

    @staticmethod
    def _text(projection: WorkStatusProjection) -> str:
        fields = [
            f"AMPLAI Work {projection.work_id}",
            f"State: {projection.state}",
            f"Attempt: {projection.attempt}",
        ]
        if projection.request_ref:
            fields.append(f"Request: {projection.request_ref}")
        if projection.next_human_action:
            fields.append(f"Next action: {projection.next_human_action}")
        if projection.evidence_refs:
            fields.append("Evidence: " + ", ".join(projection.evidence_refs))
        if projection.verification_summary:
            fields.append(f"Verification: {projection.verification_summary}")
        return "\n".join(fields)

    def _find_remote_message(self, projection: WorkStatusProjection) -> str | None:
        """Find a post whose local receipt was lost after a process crash.

        ``post_message`` can succeed just before the local outbox records its
        receipt.  Scan the bounded newest-first history for the exact
        idempotency key before posting again.  The app-id check prevents a
        different Slack app from claiming our Work status message.
        """
        cursor: str | None = None
        for _ in range(self.max_history_pages):
            page = self.transport.read_history(
                channel=self.channel_id,
                cursor=cursor,
                limit=100,
            )
            for message in page.messages:
                if message.metadata is None:
                    continue
                metadata = message.metadata
                if (
                    not isinstance(metadata, Mapping)
                    or metadata.get("event_type") != "amplai.work_status"
                ):
                    continue
                # A message that claims our event type but cannot prove the
                # configured app identity is not safe to ignore: doing so can
                # turn a lost local receipt into a duplicate remote post.
                if message.app_id != self.app_id:
                    raise RuntimeError("WORK_STATUS_METADATA_UNREADABLE")
                payload = metadata.get("event_payload")
                if not isinstance(payload, Mapping):
                    raise RuntimeError("WORK_STATUS_METADATA_UNREADABLE")
                if (
                    payload.get("work_id") == projection.work_id
                    and payload.get("idempotency_key") == projection.idempotency_key
                ):
                    return f"{self.channel_id}:{message.ts}"
            cursor = page.next_cursor
            if cursor is None:
                return None
        return None

    def upsert_work_status(self, projection: WorkStatusProjection, message_ref: str | None) -> str:
        text = self._text(projection)
        if message_ref:
            channel, separator, ts = message_ref.partition(":")
            if separator and channel and ts:
                self.transport.update_message(channel=channel, ts=ts, text=text)
                return message_ref
            raise ValueError("Slack work status message reference is malformed.")
        existing = self._find_remote_message(projection)
        if existing is not None:
            return existing
        sent = self.transport.post_message(
            channel=self.channel_id,
            payload={"text": text},
            marker={
                "event_type": "amplai.work_status",
                "event_payload": {
                    "work_id": projection.work_id,
                    "idempotency_key": projection.idempotency_key,
                },
            },
        )
        return f"{sent.channel}:{sent.ts}"


@dataclass(frozen=True, slots=True)
class WorkStatusDelivery:
    idempotency_key: str
    projection: WorkStatusProjection


class WorkStatusOutbox:
    """Persist Work status deliveries until the injected Slack transport accepts them."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_status_outbox (
                    idempotency_key TEXT PRIMARY KEY NOT NULL,
                    work_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    request_ref TEXT,
                    attempt INTEGER NOT NULL,
                    next_human_action TEXT,
                    evidence_refs_json TEXT NOT NULL,
                    verification_summary TEXT,
                    delivery_state TEXT NOT NULL CHECK (
                        delivery_state IN ('pending', 'running', 'delivered')
                    ),
                    message_ref TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    superseded_at TEXT,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS work_status_outbox_pending
                ON work_status_outbox(delivery_state, created_at);
                CREATE TRIGGER IF NOT EXISTS work_status_outbox_no_delete
                BEFORE DELETE ON work_status_outbox
                BEGIN SELECT RAISE(ABORT, 'work status delivery is durable'); END;
                """
            )
            self._migrate_delivery_lease(connection)

    def enqueue(self, projection: WorkStatusProjection) -> WorkStatusDelivery:
        key = projection.idempotency_key
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO work_status_outbox(
                    idempotency_key, work_id, state, revision, request_ref, attempt,
                    next_human_action, evidence_refs_json, verification_summary,
                    delivery_state, message_ref, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
                """,
                (
                    key,
                    projection.work_id,
                    projection.state,
                    projection.revision,
                    projection.request_ref,
                    projection.attempt,
                    projection.next_human_action,
                    json.dumps(projection.evidence_refs, separators=(",", ":")),
                    projection.verification_summary,
                    self._timestamp(),
                ),
            )
            # A newer snapshot makes every still-pending prior snapshot
            # obsolete. Keep its durable row for audit, but never let it
            # overwrite the newer Slack state later.
            connection.execute(
                """
                UPDATE work_status_outbox
                SET delivery_state='delivered', delivered_at=?, superseded_at=?,
                    lease_token=NULL, lease_expires_at=NULL
                WHERE work_id=? AND delivery_state='pending' AND revision < ?
                """,
                (self._timestamp(), self._timestamp(), projection.work_id, projection.revision),
            )
            # Out-of-order old events are also terminally obsolete when a
            # newer snapshot was already delivered or is in flight.
            connection.execute(
                """
                UPDATE work_status_outbox
                SET delivery_state='delivered', delivered_at=?, superseded_at=?,
                    lease_token=NULL, lease_expires_at=NULL
                WHERE idempotency_key=? AND delivery_state='pending' AND EXISTS (
                    SELECT 1 FROM work_status_outbox AS newer
                    WHERE newer.work_id=? AND newer.revision > ?
                )
                """,
                (
                    self._timestamp(),
                    self._timestamp(),
                    key,
                    projection.work_id,
                    projection.revision,
                ),
            )
        return WorkStatusDelivery(key, projection)

    def deliver_next(
        self, transport: SlackWorkStatusTransport, *, lease_seconds: int = 60
    ) -> WorkStatusDelivery | None:
        """Lease one delivery before any remote read or write.

        Without this state transition two worker processes can both decide that
        no remote receipt exists and each post one message.  A worker crash is
        recoverable after its bounded lease; the Slack adapter then reads the
        marker before it considers a replacement post.
        """
        if lease_seconds < 1:
            raise ValueError("Slack work-status lease must be positive.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_leases(connection)
            row = connection.execute(
                """
                SELECT idempotency_key, work_id, state, revision, request_ref, attempt,
                       next_human_action, evidence_refs_json, verification_summary
                FROM work_status_outbox AS candidate
                WHERE delivery_state = 'pending'
                  AND NOT EXISTS (
                    SELECT 1 FROM work_status_outbox AS active
                    WHERE active.work_id=candidate.work_id AND active.delivery_state='running'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM work_status_outbox AS newer
                    WHERE newer.work_id=candidate.work_id AND newer.revision > candidate.revision
                  )
                ORDER BY created_at, idempotency_key LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            lease_token = secrets.token_hex(24)
            cursor = connection.execute(
                """
                UPDATE work_status_outbox
                SET delivery_state='running', lease_token=?, lease_expires_at=?, attempts=attempts+1
                WHERE idempotency_key=? AND delivery_state='pending'
                """,
                (lease_token, self._lease_expiry(lease_seconds), str(row[0])),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            prior = connection.execute(
                """
                SELECT message_ref FROM work_status_outbox
                WHERE work_id=? AND delivery_state='delivered' AND message_ref IS NOT NULL
                ORDER BY revision DESC, delivered_at DESC LIMIT 1
                """,
                (str(row[1]),),
            ).fetchone()
            connection.commit()
        delivery = WorkStatusDelivery(
            str(row[0]),
            WorkStatusProjection(
                work_id=str(row[1]),
                state=str(row[2]),
                revision=int(row[3]),
                request_ref=str(row[4]) if row[4] is not None else None,
                attempt=int(row[5]),
                next_human_action=str(row[6]) if row[6] is not None else None,
                evidence_refs=tuple(json.loads(str(row[7]))),
                verification_summary=str(row[8]) if row[8] is not None else None,
            ),
        )
        prior_message_ref = str(prior[0]) if prior is not None else None
        try:
            message_ref = transport.upsert_work_status(delivery.projection, prior_message_ref)
        except BaseException:
            self._release_lease(delivery.idempotency_key, lease_token)
            raise
        if not message_ref.strip():
            self._release_lease(delivery.idempotency_key, lease_token)
            raise ValueError("Slack work status transport returned an empty message reference.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE work_status_outbox
                SET delivery_state = 'delivered', message_ref = ?, delivered_at = ?,
                    lease_token=NULL, lease_expires_at=NULL
                WHERE idempotency_key = ? AND delivery_state = 'running' AND lease_token = ?
                """,
                (message_ref, self._timestamp(), delivery.idempotency_key, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Slack work-status delivery lease was lost.")
        return delivery

    def pending_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM work_status_outbox WHERE delivery_state = 'pending'"
                ).fetchone()[0]
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _migrate_delivery_lease(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(work_status_outbox)")
        }
        for name, declaration in (
            ("lease_token", "TEXT"),
            ("lease_expires_at", "TEXT"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("verification_summary", "TEXT"),
            ("superseded_at", "TEXT"),
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE work_status_outbox ADD COLUMN {name} {declaration}"
                )

    def _recover_expired_leases(self, connection: sqlite3.Connection) -> None:
        # A crashed old delivery must not come back from its expired lease once
        # a newer authoritative snapshot exists (including one already sent).
        connection.execute(
            """
            UPDATE work_status_outbox AS stale
            SET delivery_state='delivered', delivered_at=?, superseded_at=?,
                lease_token=NULL, lease_expires_at=NULL
            WHERE stale.delivery_state='running' AND stale.lease_expires_at IS NOT NULL
              AND stale.lease_expires_at <= ? AND EXISTS (
                  SELECT 1 FROM work_status_outbox AS newer
                  WHERE newer.work_id=stale.work_id AND newer.revision > stale.revision
              )
            """,
            (self._timestamp(), self._timestamp(), self._timestamp()),
        )
        connection.execute(
            """
            UPDATE work_status_outbox
            SET delivery_state='pending', lease_token=NULL, lease_expires_at=NULL
            WHERE delivery_state='running' AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            """,
            (self._timestamp(),),
        )

    def _release_lease(self, idempotency_key: str, lease_token: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE work_status_outbox AS stale
                SET delivery_state='delivered', delivered_at=?, superseded_at=?,
                    lease_token=NULL, lease_expires_at=NULL
                WHERE stale.idempotency_key=? AND stale.delivery_state='running'
                  AND stale.lease_token=? AND EXISTS (
                    SELECT 1 FROM work_status_outbox AS newer
                    WHERE newer.work_id=stale.work_id AND newer.revision > stale.revision
                )
                """,
                (self._timestamp(), self._timestamp(), idempotency_key, lease_token),
            )
            connection.execute(
                """
                UPDATE work_status_outbox
                SET delivery_state='pending', lease_token=NULL, lease_expires_at=NULL
                WHERE idempotency_key=? AND delivery_state='running' AND lease_token=?
                """,
                (idempotency_key, lease_token),
            )

    @classmethod
    def _lease_expiry(cls, lease_seconds: int) -> str:
        return (
            (datetime.now(UTC) + timedelta(seconds=lease_seconds))
            .isoformat()
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
