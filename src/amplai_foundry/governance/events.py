"""Hash-chained audit and transaction-bound projection outbox."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from amplai_foundry.governance.models import AuthorityContext, Digest, ProposalRef
from amplai_foundry.governance.store import GovernanceStore, governance_transaction


def _system_now() -> datetime:
    return datetime.now(UTC)


class GovernanceEventError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OutboxLeaseConflictError(GovernanceEventError):
    pass


class OutboxReconcileError(GovernanceEventError):
    pass


class OutboxState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"
    DEAD_LETTER = "dead_letter"
    RECOVERY_HOLD = "recovery_hold"


class OutboxDestination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    destination_ref: str = Field(min_length=1)
    supersession_key: str | None = None


class AuditEventView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    command_id: str
    event_type: str
    proposal_ref: ProposalRef
    aggregate_sequence: int = Field(ge=1)
    actor_id: str
    actor_type: str
    policy_snapshot_id: str
    before_state: str
    after_state: str
    definition_digest: Digest
    previous_event_hash: Digest | None = None
    event_hash: Digest
    occurred_at: AwareDatetime


class OutboxEventView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    proposal_ref: ProposalRef
    aggregate_sequence: int = Field(ge=1)
    destination_ref: str
    destination_sequence: int = Field(ge=1)
    source_state_revision: int = Field(ge=1)
    supersession_key: str | None = None
    payload_digest: Digest
    payload: dict[str, object]
    state: OutboxState
    attempts: int = Field(ge=0)
    claim_generation: int = Field(ge=0)
    lease_owner: str | None = None
    lease_expires_at: AwareDatetime | None = None
    retry_at: AwareDatetime | None = None
    delivered_at: AwareDatetime | None = None
    remote_receipt: str | None = None
    last_error_code: str | None = None
    created_at: AwareDatetime


class ProjectionDestination(Protocol):
    destination_ref: str

    def reconcile(self, event: OutboxEventView) -> str | None: ...

    def send(self, event: OutboxEventView) -> str: ...


class GovernanceEventService:
    """Append audit and projection events inside a caller-owned transaction."""

    def __init__(
        self,
        store: GovernanceStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or _system_now

    def append_decision_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        command_id: str,
        event_type: str,
        authority: AuthorityContext,
        before_state: str,
        after_state: str,
        definition_digest: str,
        source_state_revision: int,
        payload: Mapping[str, object],
        destinations: Sequence[OutboxDestination],
    ) -> tuple[AuditEventView, tuple[OutboxEventView, ...]]:
        if not connection.in_transaction:
            raise GovernanceEventError("GOVERNANCE_TRANSACTION_REQUIRED")
        if authority.project_ref != ref.project_ref:
            raise GovernanceEventError("AUTHORITY_DENIED")
        if not destinations:
            raise GovernanceEventError("OUTBOX_DESTINATION_REQUIRED")
        occurred_at = self._aware(self._clock())
        timestamp = self._timestamp(occurred_at)
        previous = connection.execute(
            """
            SELECT aggregate_sequence, last_event_hash
            FROM governance_aggregate_sequences
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            self._identity(ref),
        ).fetchone()
        aggregate_sequence = int(previous[0]) + 1 if previous is not None else 1
        previous_hash = str(previous[1]) if previous is not None else None
        event_id = self._identifier("EVT")
        policy_snapshot_id = self._policy_snapshot(authority)
        event_hash = self._audit_hash(
            event_id=event_id,
            command_id=command_id,
            event_type=event_type,
            ref=ref,
            aggregate_sequence=aggregate_sequence,
            actor_id=authority.actor_ref.actor_id,
            actor_type=authority.actor_ref.actor_type.value,
            policy_snapshot_id=policy_snapshot_id,
            before_state=before_state,
            after_state=after_state,
            definition_digest=definition_digest,
            previous_event_hash=previous_hash,
            occurred_at=timestamp,
        )
        if previous is None:
            connection.execute(
                """
                INSERT INTO governance_aggregate_sequences(
                    project_namespace, project_id, proposal_id,
                    aggregate_sequence, last_event_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (*self._identity(ref), aggregate_sequence, event_hash),
            )
        else:
            updated = connection.execute(
                """
                UPDATE governance_aggregate_sequences
                SET aggregate_sequence = ?, last_event_hash = ?
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                  AND aggregate_sequence = ? AND last_event_hash IS ?
                """,
                (
                    aggregate_sequence,
                    event_hash,
                    *self._identity(ref),
                    int(previous[0]),
                    previous_hash,
                ),
            )
            if updated.rowcount != 1:
                raise GovernanceEventError("AUDIT_SEQUENCE_CONFLICT")
        connection.execute(
            """
            INSERT INTO governance_audit_events(
                event_id, command_id, event_type, project_namespace, project_id,
                proposal_id, aggregate_sequence, actor_id, actor_type,
                policy_snapshot_id, before_state, after_state, definition_digest,
                previous_event_hash, event_hash, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                command_id,
                event_type,
                *self._identity(ref),
                aggregate_sequence,
                authority.actor_ref.actor_id,
                authority.actor_ref.actor_type.value,
                policy_snapshot_id,
                before_state,
                after_state,
                definition_digest,
                previous_hash,
                event_hash,
                timestamp,
            ),
        )
        payload_json = self._canonical_json(dict(payload))
        payload_digest = self._digest(payload_json.encode("utf-8"))
        outbox: list[OutboxEventView] = []
        seen_destinations: set[str] = set()
        for destination in destinations:
            if destination.destination_ref in seen_destinations:
                raise GovernanceEventError("OUTBOX_DESTINATION_DUPLICATE")
            seen_destinations.add(destination.destination_ref)
            outbox.append(
                self._enqueue(
                    connection,
                    ref,
                    aggregate_sequence=aggregate_sequence,
                    source_state_revision=source_state_revision,
                    destination=destination,
                    payload_json=payload_json,
                    payload_digest=payload_digest,
                    created_at=timestamp,
                )
            )
        return self.get_audit(event_id, connection=connection), tuple(outbox)

    def get_audit(
        self,
        event_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> AuditEventView:
        if connection is None:
            with self.store.connect() as opened:
                row = opened.execute(
                    "SELECT * FROM governance_audit_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM governance_audit_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise GovernanceEventError("AUDIT_EVENT_NOT_FOUND")
        return self._audit_view(cast(tuple[object, ...], row))

    def reconcile(self) -> None:
        with self.store.connect() as connection:
            self.reconcile_connection(connection)

    @classmethod
    def reconcile_connection(cls, connection: sqlite3.Connection) -> None:
        orphan_audit = connection.execute(
            """
            SELECT 1
            FROM governance_audit_events a
            LEFT JOIN governance_aggregate_sequences s
              ON s.project_namespace = a.project_namespace
             AND s.project_id = a.project_id
             AND s.proposal_id = a.proposal_id
            WHERE s.proposal_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if orphan_audit is not None:
            raise GovernanceEventError("AUDIT_SEQUENCE_MISMATCH")
        aggregates = connection.execute(
            """
            SELECT project_namespace, project_id, proposal_id,
                   aggregate_sequence, last_event_hash
            FROM governance_aggregate_sequences
            ORDER BY project_namespace, project_id, proposal_id
            """
        ).fetchall()
        for aggregate in aggregates:
            ref = cls._proposal_ref(aggregate[0], aggregate[1], aggregate[2])
            events = connection.execute(
                """
                SELECT * FROM governance_audit_events
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                ORDER BY aggregate_sequence
                """,
                cls._identity(ref),
            ).fetchall()
            previous_hash: str | None = None
            for expected_sequence, row in enumerate(events, start=1):
                view = cls._audit_view(cast(tuple[object, ...], row))
                if view.aggregate_sequence != expected_sequence:
                    raise GovernanceEventError("AUDIT_SEQUENCE_GAP")
                if view.previous_event_hash != previous_hash:
                    raise GovernanceEventError("AUDIT_HASH_CHAIN_INVALID")
                expected_hash = cls._audit_hash_from_view(view)
                if view.event_hash != expected_hash:
                    raise GovernanceEventError("AUDIT_HASH_CHAIN_INVALID")
                outbox_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM governance_outbox_events
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                      AND aggregate_sequence = ?
                    """,
                    (*cls._identity(ref), expected_sequence),
                ).fetchone()
                if outbox_count is None or int(outbox_count[0]) < 1:
                    raise GovernanceEventError("AUDIT_OUTBOX_MISMATCH")
                previous_hash = view.event_hash
            if len(events) != int(aggregate[3]) or previous_hash != aggregate[4]:
                raise GovernanceEventError("AUDIT_SEQUENCE_MISMATCH")
        destinations = connection.execute(
            """
            SELECT destination_ref, next_sequence, delivered_sequence, operator_hold
            FROM governance_outbox_destinations
            """
        ).fetchall()
        for destination_ref, next_sequence, delivered_sequence, operator_hold in destinations:
            rows = connection.execute(
                """
                SELECT destination_sequence, payload_digest, payload_json, state
                FROM governance_outbox_events
                WHERE destination_ref = ? ORDER BY destination_sequence
                """,
                (destination_ref,),
            ).fetchall()
            sequences = tuple(int(row[0]) for row in rows)
            for row in rows:
                if cls._digest(str(row[2]).encode("utf-8")) != str(row[1]):
                    raise GovernanceEventError("OUTBOX_PAYLOAD_INTEGRITY_FAILURE")
                if int(row[0]) <= int(delivered_sequence) and str(row[3]) not in {
                    OutboxState.DELIVERED.value,
                    OutboxState.SUPERSEDED.value,
                }:
                    raise GovernanceEventError("OUTBOX_DESTINATION_CURSOR_INVALID")
            if sequences != tuple(range(1, int(next_sequence))):
                raise GovernanceEventError("OUTBOX_SEQUENCE_GAP")
            has_blocking_hold = connection.execute(
                """
                SELECT 1 FROM governance_outbox_events
                WHERE destination_ref = ?
                  AND state IN ('dead_letter', 'recovery_hold')
                LIMIT 1
                """,
                (destination_ref,),
            ).fetchone()
            if (has_blocking_hold is not None) != bool(operator_hold):
                raise GovernanceEventError("OUTBOX_OPERATOR_HOLD_MISMATCH")

    def _enqueue(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        aggregate_sequence: int,
        source_state_revision: int,
        destination: OutboxDestination,
        payload_json: str,
        payload_digest: str,
        created_at: str,
    ) -> OutboxEventView:
        connection.execute(
            """
            INSERT INTO governance_outbox_destinations(
                destination_ref, next_sequence, delivered_sequence,
                operator_hold, updated_at
            ) VALUES (?, 1, 0, 0, ?)
            ON CONFLICT(destination_ref) DO NOTHING
            """,
            (destination.destination_ref, created_at),
        )
        row = connection.execute(
            "SELECT next_sequence FROM governance_outbox_destinations WHERE destination_ref = ?",
            (destination.destination_ref,),
        ).fetchone()
        if row is None:
            raise GovernanceEventError("OUTBOX_DESTINATION_NOT_FOUND")
        destination_sequence = int(row[0])
        updated = connection.execute(
            """
            UPDATE governance_outbox_destinations
            SET next_sequence = next_sequence + 1, updated_at = ?
            WHERE destination_ref = ? AND next_sequence = ?
            """,
            (created_at, destination.destination_ref, destination_sequence),
        )
        if updated.rowcount != 1:
            raise GovernanceEventError("OUTBOX_SEQUENCE_CONFLICT")
        event_id = self._identifier("OBX")
        connection.execute(
            """
            INSERT INTO governance_outbox_events(
                event_id, project_namespace, project_id, proposal_id,
                aggregate_sequence, destination_ref, destination_sequence,
                source_state_revision, supersession_key, payload_digest, payload_json,
                state, attempts, claim_generation, lease_owner, lease_expires_at,
                retry_at, delivered_at, remote_receipt, last_error_code, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0,
                NULL, NULL, NULL, NULL, NULL, NULL, ?
            )
            """,
            (
                event_id,
                *self._identity(ref),
                aggregate_sequence,
                destination.destination_ref,
                destination_sequence,
                source_state_revision,
                destination.supersession_key,
                payload_digest,
                payload_json,
                created_at,
            ),
        )
        return OutboxDispatcher._view_row(
            cast(
                tuple[object, ...],
                connection.execute(
                    "SELECT * FROM governance_outbox_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone(),
            )
        )

    @staticmethod
    def _audit_hash_from_view(view: AuditEventView) -> str:
        return GovernanceEventService._audit_hash(
            event_id=view.event_id,
            command_id=view.command_id,
            event_type=view.event_type,
            ref=view.proposal_ref,
            aggregate_sequence=view.aggregate_sequence,
            actor_id=view.actor_id,
            actor_type=view.actor_type,
            policy_snapshot_id=view.policy_snapshot_id,
            before_state=view.before_state,
            after_state=view.after_state,
            definition_digest=view.definition_digest,
            previous_event_hash=view.previous_event_hash,
            occurred_at=GovernanceEventService._timestamp(view.occurred_at),
        )

    @staticmethod
    def _audit_hash(**values: object) -> str:
        ref = cast(ProposalRef, values.pop("ref"))
        payload = {
            **values,
            "proposal_ref": ref.model_dump(mode="json"),
        }
        return GovernanceEventService._digest(
            GovernanceEventService._canonical_json(payload).encode("utf-8")
        )

    @staticmethod
    def _policy_snapshot(authority: AuthorityContext) -> str:
        return GovernanceEventService._digest(
            GovernanceEventService._canonical_json(
                {
                    "actor_ref": authority.actor_ref.model_dump(mode="json"),
                    "permissions": sorted(permission.value for permission in authority.permissions),
                    "project_ref": authority.project_ref.model_dump(mode="json"),
                }
            ).encode("utf-8")
        )

    @staticmethod
    def _audit_view(row: tuple[object, ...]) -> AuditEventView:
        return AuditEventView.model_validate(
            {
                "event_id": row[0],
                "command_id": row[1],
                "event_type": row[2],
                "proposal_ref": {
                    "project_ref": {"namespace": row[3], "project_id": row[4]},
                    "proposal_id": row[5],
                },
                "aggregate_sequence": row[6],
                "actor_id": row[7],
                "actor_type": row[8],
                "policy_snapshot_id": row[9],
                "before_state": row[10],
                "after_state": row[11],
                "definition_digest": row[12],
                "previous_event_hash": row[13],
                "event_hash": row[14],
                "occurred_at": row[15],
            }
        )

    @staticmethod
    def _canonical_json(payload: Mapping[str, object]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _digest(payload: bytes) -> str:
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    @staticmethod
    def _identifier(prefix: str) -> str:
        return f"{prefix}-{secrets.token_hex(8).upper()}"

    @staticmethod
    def _identity(ref: ProposalRef) -> tuple[str, str, str]:
        return ref.project_ref.namespace, ref.project_ref.project_id, ref.proposal_id

    @staticmethod
    def _proposal_ref(namespace: object, project_id: object, proposal_id: object) -> ProposalRef:
        from amplai_foundry.domain.identity import ProjectRef

        return ProposalRef(
            project_ref=ProjectRef(namespace=str(namespace), project_id=str(project_id)),
            proposal_id=str(proposal_id),
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp는 timezone-aware여야 합니다.")
        return value

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class OutboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_seconds: int = Field(default=30, ge=1)
    max_attempts: int = Field(default=5, ge=1)
    retry_base_seconds: int = Field(default=5, ge=1)
    retry_cap_seconds: int = Field(default=300, ge=1)


class OutboxDispatcher:
    """Deliver one ordered destination stream with fenced leases."""

    def __init__(
        self,
        store: GovernanceStore,
        *,
        config: OutboxConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.config = config or OutboxConfig()
        self._clock = clock or _system_now

    def claim_next(
        self,
        dispatcher_id: str,
        *,
        destination_ref: str | None = None,
    ) -> OutboxEventView | None:
        if not dispatcher_id.strip():
            raise ValueError("dispatcher_id는 비어 있을 수 없습니다.")
        now = GovernanceEventService._aware(self._clock())
        now_text = GovernanceEventService._timestamp(now)
        lease_expires = GovernanceEventService._timestamp(
            now + timedelta(seconds=self.config.lease_seconds)
        )
        with self.store.connect() as connection, governance_transaction(connection):
            connection.execute(
                """
                UPDATE governance_outbox_events
                SET state = 'retry_wait', lease_owner = NULL, lease_expires_at = NULL,
                    retry_at = ?, last_error_code = 'OUTBOX_LEASE_EXPIRED'
                WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (now_text, now_text),
            )
            self._move_exhausted(connection, now_text)
            row = connection.execute(
                """
                SELECT e.event_id, e.claim_generation
                FROM governance_outbox_events e
                JOIN governance_outbox_destinations d
                  ON d.destination_ref = e.destination_ref
                WHERE d.operator_hold = 0
                  AND (? IS NULL OR e.destination_ref = ?)
                  AND (
                    e.state = 'pending'
                    OR (e.state = 'retry_wait' AND e.retry_at <= ?)
                  )
                  AND e.attempts < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM governance_outbox_events prior
                    WHERE prior.destination_ref = e.destination_ref
                      AND prior.destination_sequence < e.destination_sequence
                      AND prior.state NOT IN ('delivered', 'superseded')
                  )
                ORDER BY e.created_at, e.destination_ref, e.destination_sequence
                LIMIT 1
                """,
                (destination_ref, destination_ref, now_text, self.config.max_attempts),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE governance_outbox_events
                SET state = 'leased', attempts = attempts + 1,
                    claim_generation = claim_generation + 1,
                    lease_owner = ?, lease_expires_at = ?, retry_at = NULL
                WHERE event_id = ? AND claim_generation = ?
                  AND state IN ('pending', 'retry_wait') AND attempts < ?
                """,
                (dispatcher_id, lease_expires, row[0], row[1], self.config.max_attempts),
            )
            if updated.rowcount != 1:
                raise OutboxLeaseConflictError("OUTBOX_LEASE_CONFLICT")
            return self._get_in_connection(connection, str(row[0]))

    def mark_delivered(
        self,
        event_id: str,
        *,
        dispatcher_id: str,
        generation: int,
        remote_receipt: str,
    ) -> OutboxEventView:
        if not remote_receipt.strip():
            raise ValueError("remote_receipt는 비어 있을 수 없습니다.")
        now = GovernanceEventService._aware(self._clock())
        now_text = GovernanceEventService._timestamp(now)
        with self.store.connect() as connection, governance_transaction(connection):
            existing = self._get_in_connection(connection, event_id)
            if existing.state is OutboxState.DELIVERED:
                if existing.remote_receipt == remote_receipt:
                    return existing
                raise OutboxLeaseConflictError("OUTBOX_DELIVERY_RESULT_CONFLICT")
            event = self._require_lease(
                connection,
                event_id,
                dispatcher_id=dispatcher_id,
                generation=generation,
                now=now,
            )
            updated = connection.execute(
                """
                UPDATE governance_outbox_events
                SET state = 'delivered', lease_owner = NULL, lease_expires_at = NULL,
                    delivered_at = ?, remote_receipt = ?, last_error_code = NULL
                WHERE event_id = ? AND state = 'leased' AND lease_owner = ?
                  AND claim_generation = ? AND lease_expires_at > ?
                """,
                (
                    now_text,
                    remote_receipt,
                    event_id,
                    dispatcher_id,
                    generation,
                    now_text,
                ),
            )
            if updated.rowcount != 1:
                raise OutboxLeaseConflictError("OUTBOX_LEASE_CONFLICT")
            connection.execute(
                """
                UPDATE governance_outbox_destinations
                SET delivered_sequence = ?, updated_at = ?
                WHERE destination_ref = ? AND delivered_sequence < ?
                """,
                (
                    event.destination_sequence,
                    now_text,
                    event.destination_ref,
                    event.destination_sequence,
                ),
            )
            return self._get_in_connection(connection, event_id)

    def fail(
        self,
        event_id: str,
        *,
        dispatcher_id: str,
        generation: int,
        error_code: str,
        unreconcilable: bool = False,
    ) -> OutboxEventView:
        now = GovernanceEventService._aware(self._clock())
        now_text = GovernanceEventService._timestamp(now)
        with self.store.connect() as connection, governance_transaction(connection):
            event = self._require_lease(
                connection,
                event_id,
                dispatcher_id=dispatcher_id,
                generation=generation,
                now=now,
            )
            exhausted = event.attempts >= self.config.max_attempts
            if exhausted or unreconcilable:
                self._dead_letter(connection, event, error_code=error_code, now=now_text)
            else:
                retry_at = GovernanceEventService._timestamp(
                    now + timedelta(seconds=self._backoff_seconds(event.attempts))
                )
                connection.execute(
                    """
                    UPDATE governance_outbox_events
                    SET state = 'retry_wait', lease_owner = NULL, lease_expires_at = NULL,
                        retry_at = ?, last_error_code = ?
                    WHERE event_id = ? AND state = 'leased' AND lease_owner = ?
                      AND claim_generation = ?
                    """,
                    (retry_at, error_code, event_id, dispatcher_id, generation),
                )
            return self._get_in_connection(connection, event_id)

    def supersede_pending(self, event_id: str, *, replacement_event_id: str) -> OutboxEventView:
        with self.store.connect() as connection, governance_transaction(connection):
            current = self._get_in_connection(connection, event_id)
            replacement = self._get_in_connection(connection, replacement_event_id)
            if (
                current.state is not OutboxState.PENDING
                or replacement.state is not OutboxState.PENDING
                or current.destination_ref != replacement.destination_ref
                or current.supersession_key is None
                or current.supersession_key != replacement.supersession_key
                or current.destination_sequence >= replacement.destination_sequence
            ):
                raise GovernanceEventError("OUTBOX_SUPERSEDE_INVALID")
            connection.execute(
                "UPDATE governance_outbox_events SET state = 'superseded' WHERE event_id = ?",
                (event_id,),
            )
            return self._get_in_connection(connection, event_id)

    def deliver_next(
        self,
        dispatcher_id: str,
        destination: ProjectionDestination,
    ) -> OutboxEventView | None:
        event = self.claim_next(dispatcher_id, destination_ref=destination.destination_ref)
        if event is None:
            return None
        try:
            receipt = destination.reconcile(event)
            if receipt is None:
                receipt = destination.send(event)
        except OutboxReconcileError as error:
            return self.fail(
                event.event_id,
                dispatcher_id=dispatcher_id,
                generation=event.claim_generation,
                error_code=error.code,
                unreconcilable=True,
            )
        except Exception:
            return self.fail(
                event.event_id,
                dispatcher_id=dispatcher_id,
                generation=event.claim_generation,
                error_code="OUTBOX_DELIVERY_FAILED",
            )
        return self.mark_delivered(
            event.event_id,
            dispatcher_id=dispatcher_id,
            generation=event.claim_generation,
            remote_receipt=receipt,
        )

    def get(self, event_id: str) -> OutboxEventView:
        with self.store.connect() as connection:
            return self._get_in_connection(connection, event_id)

    def _move_exhausted(self, connection: sqlite3.Connection, now: str) -> None:
        rows = connection.execute(
            """
            SELECT event_id FROM governance_outbox_events
            WHERE state = 'retry_wait' AND attempts >= ? AND retry_at <= ?
            """,
            (self.config.max_attempts, now),
        ).fetchall()
        for row in rows:
            self._dead_letter(
                connection,
                self._get_in_connection(connection, str(row[0])),
                error_code="OUTBOX_ATTEMPTS_EXHAUSTED",
                now=now,
            )

    def _dead_letter(
        self,
        connection: sqlite3.Connection,
        event: OutboxEventView,
        *,
        error_code: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE governance_outbox_events
            SET state = 'dead_letter', lease_owner = NULL, lease_expires_at = NULL,
                retry_at = NULL, last_error_code = ?
            WHERE event_id = ?
            """,
            (error_code, event.event_id),
        )
        connection.execute(
            """
            INSERT INTO governance_outbox_dead_letters(
                dead_letter_id, event_id, destination_ref, destination_sequence,
                attempts, error_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                GovernanceEventService._identifier("DLQ"),
                event.event_id,
                event.destination_ref,
                event.destination_sequence,
                event.attempts,
                error_code,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_operator_holds(
                hold_id, scope_kind, scope_ref, reason_code,
                source_event_id, created_at, resolved_at
            ) VALUES (?, 'outbox_destination', ?, ?, ?, ?, NULL)
            """,
            (
                GovernanceEventService._identifier("HLD"),
                event.destination_ref,
                error_code,
                event.event_id,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE governance_outbox_destinations
            SET operator_hold = 1, updated_at = ? WHERE destination_ref = ?
            """,
            (now, event.destination_ref),
        )

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        *,
        dispatcher_id: str,
        generation: int,
        now: datetime,
    ) -> OutboxEventView:
        event = self._get_in_connection(connection, event_id)
        if (
            event.state is not OutboxState.LEASED
            or event.lease_owner != dispatcher_id
            or event.claim_generation != generation
            or event.lease_expires_at is None
            or event.lease_expires_at <= now
        ):
            raise OutboxLeaseConflictError("OUTBOX_LEASE_CONFLICT")
        return event

    def _backoff_seconds(self, attempts: int) -> int:
        return int(
            min(
                self.config.retry_base_seconds * (2 ** max(attempts - 1, 0)),
                self.config.retry_cap_seconds,
            )
        )

    @staticmethod
    def _get_in_connection(
        connection: sqlite3.Connection,
        event_id: str,
    ) -> OutboxEventView:
        row = connection.execute(
            "SELECT * FROM governance_outbox_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise GovernanceEventError("OUTBOX_EVENT_NOT_FOUND")
        return OutboxDispatcher._view_row(cast(tuple[object, ...], row))

    @staticmethod
    def _view_row(row: tuple[object, ...]) -> OutboxEventView:
        return OutboxEventView.model_validate(
            {
                "event_id": row[0],
                "proposal_ref": {
                    "project_ref": {"namespace": row[1], "project_id": row[2]},
                    "proposal_id": row[3],
                },
                "aggregate_sequence": row[4],
                "destination_ref": row[5],
                "destination_sequence": row[6],
                "source_state_revision": row[7],
                "supersession_key": row[8],
                "payload_digest": row[9],
                "payload": json.loads(str(row[10])),
                "state": row[11],
                "attempts": row[12],
                "claim_generation": row[13],
                "lease_owner": row[14],
                "lease_expires_at": row[15],
                "retry_at": row[16],
                "delivered_at": row[17],
                "remote_receipt": row[18],
                "last_error_code": row[19],
                "created_at": row[20],
            }
        )
