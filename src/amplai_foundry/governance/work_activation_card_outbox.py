"""Durable, secret-free delivery of human Work Activation Cards.

The outbox records an already-authorized card intent, never a raw action token.
Tokens are minted only by the delivery worker immediately before the Slack API
call.  If that call succeeds but the local receipt is lost, the worker finds
the marker in Slack history instead of sending a second card.
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
from typing import Protocol

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.models import (
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    AuthoritySource,
    ChannelProvider,
    ChannelRef,
)
from amplai_foundry.governance.slack_projection import SlackHistoryPage, SlackSendResult
from amplai_foundry.governance.work_activation import (
    DurableWorkActivationLedger,
    WorkActivationError,
    WorkActivationGateway,
    WorkActivationScope,
    WorkActivationSnapshot,
)
from amplai_foundry.governance.work_activation_http import render_work_activation_card


class WorkActivationCardTransport(Protocol):
    def post_message(
        self, *, channel: str, payload: Mapping[str, object], marker: Mapping[str, object]
    ) -> SlackSendResult: ...

    def read_history(self, *, channel: str, cursor: str | None, limit: int) -> SlackHistoryPage: ...

    def update_card(self, *, channel: str, ts: str, payload: Mapping[str, object]) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkActivationCardIntent:
    idempotency_key: str
    snapshot: WorkActivationSnapshot
    scope: WorkActivationScope
    authority: AuthorityContext


class WorkActivationCardOutbox:
    """One durable Slack card intent per DRAFT Work snapshot and human authority."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_activation_card_outbox (
                    idempotency_key TEXT PRIMARY KEY NOT NULL,
                    work_id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    expected_digest TEXT NOT NULL,
                    project_namespace TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    channel_json TEXT NOT NULL,
                    delivery_state TEXT NOT NULL CHECK (
                        delivery_state IN ('pending', 'running', 'delivered')
                    ),
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    message_ref TEXT,
                    superseded_at TEXT,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS work_activation_card_one_snapshot
                ON work_activation_card_outbox(
                    work_id, expected_revision, expected_digest, actor_id
                );
                CREATE INDEX IF NOT EXISTS work_activation_card_pending
                ON work_activation_card_outbox(delivery_state, created_at);
                CREATE TRIGGER IF NOT EXISTS work_activation_card_no_delete
                BEFORE DELETE ON work_activation_card_outbox
                BEGIN SELECT RAISE(ABORT, 'work activation card is durable'); END;
                """
            )
            self._migrate_superseded_at(connection)

    def enqueue(
        self,
        *,
        snapshot: WorkActivationSnapshot,
        scope: WorkActivationScope,
        authority: AuthorityContext,
    ) -> WorkActivationCardIntent:
        self._validate(snapshot, scope, authority)
        key = self._key(snapshot, scope, authority)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO work_activation_card_outbox(
                    idempotency_key, work_id, expected_revision, expected_digest,
                    project_namespace, project_id, feature, provider, actor_id,
                    channel_json, delivery_state, lease_token, lease_expires_at,
                    attempts, message_ref, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 0, NULL, ?, NULL)
                """,
                (
                    key,
                    snapshot.work_id,
                    snapshot.revision,
                    snapshot.digest,
                    scope.project_ref.namespace,
                    scope.project_ref.project_id,
                    scope.feature,
                    scope.provider.value,
                    authority.actor_ref.actor_id,
                    self._channel_json(authority.source.channel),
                    self._timestamp(),
                ),
            )
        return WorkActivationCardIntent(key, snapshot, scope, authority)

    def deliver_next(
        self,
        *,
        gateway: WorkActivationGateway,
        ledger: DurableWorkActivationLedger,
        transport: WorkActivationCardTransport,
        app_id: str,
        lease_seconds: int = 60,
    ) -> WorkActivationCardIntent | None:
        if not app_id.strip() or lease_seconds < 1:
            raise ValueError("Activation Card delivery configuration is invalid.")
        row, lease_token = self._claim(lease_seconds)
        if row is None or lease_token is None:
            return None
        intent = self._intent_from_row(row)
        try:
            current = gateway.get(intent.snapshot.work_id)
            if current != intent.snapshot:
                raise WorkActivationError("WORK_STALE")
            message_ref = self._find_remote(transport, intent, app_id=app_id)
            if message_ref is None:
                channel_id = self._channel_id(intent.authority.source.channel)
                sent = transport.post_message(
                    channel=channel_id,
                    payload={
                        "text": "AMPLAI Work activation is being prepared: "
                        + intent.snapshot.work_id
                    },
                    marker={
                        "event_type": "amplai.work_activation_card",
                        "event_payload": {
                            "work_id": intent.snapshot.work_id,
                            "idempotency_key": intent.idempotency_key,
                        },
                    },
                )
                message_ref = f"{sent.channel}:{sent.ts}"
            channel_id, separator, message_id = message_ref.partition(":")
            if not separator or not channel_id or not message_id:
                raise RuntimeError("Activation Card Slack receipt is malformed.")
            issued = ledger.issue_tokens(
                snapshot=intent.snapshot,
                scope=intent.scope,
                authority=self._authority_for_message(intent.authority, message_id),
            )
            transport.update_card(
                channel=channel_id,
                ts=message_id,
                payload=render_work_activation_card(work_id=intent.snapshot.work_id, tokens=issued),
            )
            self._mark_delivered(intent.idempotency_key, lease_token, message_ref)
            return intent
        except WorkActivationError as error:
            if error.code == "WORK_STALE":
                self._mark_superseded(intent.idempotency_key, lease_token)
            else:
                self._release(intent.idempotency_key, lease_token)
            raise
        except BaseException:
            self._release(intent.idempotency_key, lease_token)
            raise

    def pending_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM work_activation_card_outbox "
                    "WHERE delivery_state='pending'"
                ).fetchone()[0]
            )

    def _claim(self, lease_seconds: int) -> tuple[sqlite3.Row | None, str | None]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE work_activation_card_outbox
                SET delivery_state='pending', lease_token=NULL, lease_expires_at=NULL
                WHERE delivery_state='running' AND lease_expires_at <= ?
                """,
                (self._timestamp(),),
            )
            row = connection.execute(
                """
                SELECT * FROM work_activation_card_outbox WHERE delivery_state='pending'
                ORDER BY created_at, idempotency_key LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None, None
            token = secrets.token_hex(24)
            cursor = connection.execute(
                """
                UPDATE work_activation_card_outbox
                SET delivery_state='running', lease_token=?, lease_expires_at=?, attempts=attempts+1
                WHERE idempotency_key=? AND delivery_state='pending'
                """,
                (token, self._lease_expiry(lease_seconds), str(row["idempotency_key"])),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None, None
            connection.commit()
            return row, token

    @staticmethod
    def _intent_from_row(row: sqlite3.Row) -> WorkActivationCardIntent:
        channel = ChannelRef.model_validate_json(str(row["channel_json"]))
        scope = WorkActivationScope(
            project_ref=ProjectRef(
                namespace=str(row["project_namespace"]), project_id=str(row["project_id"])
            ),
            provider=ChannelProvider(str(row["provider"])),
            feature=str(row["feature"]),
        )
        # The outbox stores only a previously authorized intent.  Rehydrate its
        # typed record for the delivery boundary; it never accepts actor data
        # from Hermes or an HTTP request.
        authority = AuthorityContext.model_validate(
            {
                "actor_ref": {"actor_id": str(row["actor_id"]), "actor_type": "human"},
                "project_ref": scope.project_ref.model_dump(mode="json"),
                "permissions": [AuthorityPermission.ACTIVATION_MANAGE.value],
                "source": {
                    "request_id": f"activation-card:{row['idempotency_key']}",
                    "channel": channel.model_dump(mode="json"),
                },
                "authenticated_at": datetime.now(UTC),
            }
        )
        return WorkActivationCardIntent(
            idempotency_key=str(row["idempotency_key"]),
            snapshot=WorkActivationSnapshot(
                str(row["work_id"]),
                "DRAFT",
                int(row["expected_revision"]),
                str(row["expected_digest"]),
            ),
            scope=scope,
            authority=authority,
        )

    @staticmethod
    def _validate(
        snapshot: WorkActivationSnapshot,
        scope: WorkActivationScope,
        authority: AuthorityContext,
    ) -> None:
        if (
            snapshot.state != "DRAFT"
            or authority.actor_ref.actor_type is not ActorType.HUMAN
            or AuthorityPermission.ACTIVATION_MANAGE not in authority.permissions
            or authority.project_ref != scope.project_ref
            or authority.source.channel.provider is not scope.provider
        ):
            raise WorkActivationError("AUTHORITY_DENIED")

    def _find_remote(
        self,
        transport: WorkActivationCardTransport,
        intent: WorkActivationCardIntent,
        *,
        app_id: str,
    ) -> str | None:
        channel_id = self._channel_id(intent.authority.source.channel)
        page = transport.read_history(channel=channel_id, cursor=None, limit=100)
        for message in page.messages:
            metadata = message.metadata
            if not isinstance(metadata, Mapping):
                continue
            if metadata.get("event_type") != "amplai.work_activation_card":
                continue
            if message.app_id != app_id:
                raise RuntimeError("ACTIVATION_CARD_METADATA_UNREADABLE")
            payload = metadata.get("event_payload")
            if not isinstance(payload, Mapping):
                raise RuntimeError("ACTIVATION_CARD_METADATA_UNREADABLE")
            if (
                payload.get("work_id") == intent.snapshot.work_id
                and payload.get("idempotency_key") == intent.idempotency_key
            ):
                return f"{channel_id}:{message.ts}"
        return None

    def _mark_delivered(self, key: str, token: str, message_ref: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE work_activation_card_outbox
                SET delivery_state='delivered', message_ref=?, delivered_at=?,
                    lease_token=NULL, lease_expires_at=NULL
                WHERE idempotency_key=? AND delivery_state='running' AND lease_token=?
                """,
                (message_ref, self._timestamp(), key, token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Activation Card delivery lease was lost.")

    def _release(self, key: str, token: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE work_activation_card_outbox
                SET delivery_state='pending', lease_token=NULL, lease_expires_at=NULL
                WHERE idempotency_key=? AND delivery_state='running' AND lease_token=?
                """,
                (key, token),
            )

    def _mark_superseded(self, key: str, token: str) -> None:
        """Terminally retire a stale card so it cannot starve newer intents."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE work_activation_card_outbox
                SET delivery_state='delivered', superseded_at=?,
                    lease_token=NULL, lease_expires_at=NULL
                WHERE idempotency_key=? AND delivery_state='running' AND lease_token=?
                """,
                (self._timestamp(), key, token),
            )

    @staticmethod
    def _channel_json(channel: ChannelRef) -> str:
        return json.dumps(channel.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _key(
        snapshot: WorkActivationSnapshot,
        scope: WorkActivationScope,
        authority: AuthorityContext,
    ) -> str:
        payload = json.dumps(
            {
                "work_id": snapshot.work_id,
                "revision": snapshot.revision,
                "digest": snapshot.digest,
                "project": scope.project_ref.model_dump(mode="json"),
                "feature": scope.feature,
                "actor_id": authority.actor_ref.actor_id,
                "channel": authority.source.channel.model_dump(mode="json"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return "activation-card:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _channel_id(channel: ChannelRef) -> str:
        if not channel.channel_id:
            raise WorkActivationError("ACTIVATION_CHANNEL_INVALID")
        return channel.channel_id

    @staticmethod
    def _authority_for_message(authority: AuthorityContext, message_id: str) -> AuthorityContext:
        return authority.model_copy(
            update={
                "source": AuthoritySource(
                    request_id=authority.source.request_id,
                    channel=authority.source.channel.model_copy(update={"message_id": message_id}),
                )
            }
        )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def _lease_expiry(cls, seconds: int) -> str:
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _migrate_superseded_at(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(work_activation_card_outbox)")
        }
        if "superseded_at" not in columns:
            connection.execute(
                "ALTER TABLE work_activation_card_outbox ADD COLUMN superseded_at TEXT"
            )
