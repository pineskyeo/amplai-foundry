"""Hash-chained audit and transaction-bound projection outbox."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol, cast

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


class DecisionProjectionPayload(BaseModel):
    """Secret-free projection payload derived from a persisted Decision result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(pattern=r"^(approve|reject|request_changes)$")
    active_definition_digest: Digest
    aggregate_ref: ProposalRef
    content_revision: int = Field(ge=1)
    decision_epoch: int = Field(ge=1)
    proposal_status: str = Field(pattern=r"^(approved|rejected|changes_requested)$")
    state_revision: int = Field(ge=2)


class LegacyMigrationProjectionPayload(BaseModel):
    """Secret-free authoritative projection for one imported legacy Proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aggregate_ref: ProposalRef
    approval_disposition: str = Field(
        pattern=r"^(not_required|legacy_audit_present|synthetic_required)$"
    )
    event_type: str = Field(
        pattern=r"^migration\.(legacy_approval|synthetic_approval|state_imported)$"
    )
    idempotency_key: str = Field(min_length=1)
    mapping_policy_version: int = Field(ge=1)
    migration_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    projection_destination_ref: str | None = Field(default=None, min_length=1)
    reason: str | None = None
    imported_state_revision: int = Field(ge=1)
    source_artifact_digest: Digest
    source_revision: int = Field(ge=1)
    source_state_revision: int = Field(ge=1)
    source_status: str
    target_status: str
    validation_policy_ref: str = Field(min_length=1)


class LegacyApprovalReviewProjectionPayload(BaseModel):
    """Secret-free projection for governed human resolution of a synthetic hold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    aggregate_ref: ProposalRef
    event_type: Literal["migration.synthetic_approval_reviewed"] = (
        "migration.synthetic_approval_reviewed"
    )
    idempotency_key: str = Field(min_length=1)
    migration_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    reason: str = Field(min_length=3, max_length=512)
    source_state_revision: int = Field(ge=2)
    before_status: str
    after_status: Literal["draft"] = "draft"


class ApplyProjectionPayload(BaseModel):
    """Secret-free projection payload derived from a persisted Apply request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(pattern=r"^request_apply$")
    active_definition_digest: Digest
    aggregate_ref: ProposalRef
    approved_snapshot_digest: Digest
    content_revision: int = Field(ge=1)
    decision_epoch: int = Field(ge=1)
    expected_base_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    job_id: str = Field(min_length=1)
    proposal_status: str = Field(pattern=r"^apply_requested$")
    snapshot_id: str = Field(min_length=1)
    state_revision: int = Field(ge=3)


class ApplyJobProjectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    aggregate_ref: ProposalRef
    attempts: int = Field(ge=1)
    event_sequence: int = Field(ge=1)
    event_type: str
    fencing_token: int = Field(ge=1)
    job_id: str
    last_error_code: str | None = None
    lease_expires_at: AwareDatetime | None = None
    lease_owner: str | None = None
    publish_request_digest: Digest | None = None
    retry_at: AwareDatetime | None = None
    staged_artifact_digest: Digest | None = None
    status: str
    worker_id: str


class PublishResolutionProjectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    aggregate_ref: ProposalRef
    applied_revision: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    actual_ref: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    before_job_status: str | None = None
    error_code: str | None = None
    intent_id: str = Field(pattern=r"^PBI-[A-F0-9]{16}$")
    intent_status: str
    job_id: str
    job_status: str
    proposal_state_revision: int = Field(ge=3)
    proposal_status: str
    resolution_sequence: int = Field(ge=1)
    resolution_type: str
    stream_revision: int | None = Field(default=None, ge=1)


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
    destination_manifest_digest: Digest
    destination_count: int = Field(ge=1)
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

    def _append_decision_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        decision_result_key: str,
        authority: AuthorityContext,
        payload: DecisionProjectionPayload,
    ) -> tuple[AuditEventView, tuple[OutboxEventView, ...]]:
        if not connection.in_transaction:
            raise GovernanceEventError("GOVERNANCE_TRANSACTION_REQUIRED")
        if authority.project_ref != ref.project_ref:
            raise GovernanceEventError("AUTHORITY_DENIED")
        decision_result = connection.execute(
            """
            SELECT project_namespace, project_id, proposal_id, action, actor_id,
                   actor_type, proposal_status, active_definition_digest,
                   content_revision, state_revision, decision_epoch
            FROM governance_decision_results WHERE idempotency_key = ?
            """,
            (decision_result_key,),
        ).fetchone()
        expected_result = (
            *self._identity(ref),
            payload.action,
            authority.actor_ref.actor_id,
            authority.actor_ref.actor_type.value,
            payload.proposal_status,
            payload.active_definition_digest,
            payload.content_revision,
            payload.state_revision,
            payload.decision_epoch,
        )
        if decision_result is None or tuple(decision_result) != expected_result:
            raise GovernanceEventError("DECISION_AUDIT_SOURCE_MISMATCH")
        return self._append_verified_event_in_transaction(
            connection,
            ref,
            command_id=self._decision_command_id(decision_result_key),
            event_type=f"proposal.{payload.proposal_status}",
            actor_id=authority.actor_ref.actor_id,
            actor_type=authority.actor_ref.actor_type.value,
            policy_snapshot_id=self._policy_snapshot(authority),
            destinations=self._decision_destinations(ref, authority),
            before_state="reviewed",
            after_state=payload.proposal_status,
            definition_digest=payload.active_definition_digest,
            source_state_revision=payload.state_revision,
            payload=payload,
        )

    def _append_apply_request_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        apply_result_key: str,
        authority: AuthorityContext,
        payload: ApplyProjectionPayload,
    ) -> tuple[AuditEventView, tuple[OutboxEventView, ...]]:
        if not connection.in_transaction:
            raise GovernanceEventError("GOVERNANCE_TRANSACTION_REQUIRED")
        if authority.project_ref != ref.project_ref:
            raise GovernanceEventError("AUTHORITY_DENIED")
        source = connection.execute(
            """
            SELECT r.project_namespace, r.project_id, r.proposal_id, r.actor_id,
                   r.actor_type, r.proposal_status, r.snapshot_id, r.job_id,
                   s.definition_digest, s.snapshot_digest, s.content_revision,
                   s.state_revision + 1, s.decision_epoch, s.expected_base_revision
            FROM governance_apply_request_results r
            JOIN governance_approved_snapshots s ON s.snapshot_id = r.snapshot_id
              AND s.project_namespace = r.project_namespace
              AND s.project_id = r.project_id AND s.proposal_id = r.proposal_id
            WHERE r.idempotency_key = ?
            """,
            (apply_result_key,),
        ).fetchone()
        expected = (
            *self._identity(ref),
            authority.actor_ref.actor_id,
            authority.actor_ref.actor_type.value,
            payload.proposal_status,
            payload.snapshot_id,
            payload.job_id,
            payload.active_definition_digest,
            payload.approved_snapshot_digest,
            payload.content_revision,
            payload.state_revision,
            payload.decision_epoch,
            payload.expected_base_revision,
        )
        if source is None or tuple(source) != expected:
            raise GovernanceEventError("APPLY_AUDIT_SOURCE_MISMATCH")
        return self._append_verified_event_in_transaction(
            connection,
            ref,
            command_id=self._apply_command_id(apply_result_key),
            event_type="proposal.apply_requested",
            actor_id=authority.actor_ref.actor_id,
            actor_type=authority.actor_ref.actor_type.value,
            policy_snapshot_id=self._policy_snapshot(authority),
            destinations=self._decision_destinations(ref, authority),
            before_state="approved",
            after_state="apply_requested",
            definition_digest=payload.active_definition_digest,
            source_state_revision=payload.state_revision,
            payload=payload,
        )

    def _append_legacy_import_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        command_id: str,
        payload: LegacyMigrationProjectionPayload,
    ) -> tuple[AuditEventView, tuple[OutboxEventView, ...]]:
        if not connection.in_transaction:
            raise GovernanceEventError("GOVERNANCE_TRANSACTION_REQUIRED")
        payload_json = self._canonical_json(payload.model_dump(mode="json"))
        payload_digest = self._digest(payload_json.encode("utf-8"))
        row = connection.execute(
            """
            SELECT migration_id, project_namespace, project_id, proposal_id,
                   idempotency_key, event_type, actor_id, actor_type, occurred_at,
                   source_artifact_digest, reason, definition_digest,
                   source_state_revision, payload_digest, payload_json
            FROM governance_legacy_import_commands
            WHERE command_id = ?
            """,
            (command_id,),
        ).fetchone()
        if row is None or tuple(row[:6]) != (
            payload.migration_id,
            *self._identity(ref),
            payload.idempotency_key,
            payload.event_type,
        ):
            raise GovernanceEventError("LEGACY_MIGRATION_AUDIT_SOURCE_MISMATCH")
        if (
            str(row[9]) != payload.source_artifact_digest
            or row[10] != payload.reason
            or int(row[12]) != payload.source_state_revision
            or str(row[13]) != payload_digest
            or str(row[14]) != payload_json
        ):
            raise GovernanceEventError("LEGACY_MIGRATION_AUDIT_SOURCE_MISMATCH")
        result = self._append_verified_event_in_transaction(
            connection,
            ref,
            command_id=command_id,
            event_type=payload.event_type,
            actor_id=str(row[6]),
            actor_type=str(row[7]),
            policy_snapshot_id=self._digest(
                f"{payload.mapping_policy_version}:{payload.validation_policy_ref}".encode()
            ),
            destinations=(
                OutboxDestination(
                    destination_ref=payload.projection_destination_ref
                    or f"yaml:{ref.project_ref.namespace}:"
                    f"{ref.project_ref.project_id}:{ref.proposal_id}"
                ),
            ),
            before_state=payload.source_status,
            after_state=payload.target_status,
            definition_digest=str(row[11]),
            source_state_revision=payload.source_state_revision,
            payload=payload,
        )
        if self._timestamp(result[0].occurred_at) != str(row[8]):
            raise GovernanceEventError("LEGACY_MIGRATION_AUDIT_SOURCE_MISMATCH")
        return result

    def _append_legacy_approval_review_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        review_id: str,
        payload: LegacyApprovalReviewProjectionPayload,
    ) -> tuple[AuditEventView, tuple[OutboxEventView, ...]]:
        if not connection.in_transaction:
            raise GovernanceEventError("GOVERNANCE_TRANSACTION_REQUIRED")
        payload_json = self._canonical_json(payload.model_dump(mode="json"))
        payload_digest = self._digest(payload_json.encode("utf-8"))
        row = connection.execute(
            """
            SELECT migration_id, project_namespace, project_id, proposal_id,
                   idempotency_key, actor_id, actor_type, occurred_at, reason,
                   definition_digest, source_state_revision, payload_digest, payload_json
            FROM governance_legacy_approval_reviews
            WHERE review_id = ?
            """,
            (review_id,),
        ).fetchone()
        if row is None or tuple(row[:5]) != (
            payload.migration_id,
            *self._identity(ref),
            payload.idempotency_key,
        ):
            raise GovernanceEventError("LEGACY_APPROVAL_REVIEW_SOURCE_MISMATCH")
        if (
            str(row[8]) != payload.reason
            or int(row[10]) != payload.source_state_revision
            or str(row[11]) != payload_digest
            or str(row[12]) != payload_json
        ):
            raise GovernanceEventError("LEGACY_APPROVAL_REVIEW_SOURCE_MISMATCH")
        result = self._append_verified_event_in_transaction(
            connection,
            ref,
            command_id=review_id,
            event_type=payload.event_type,
            actor_id=str(row[5]),
            actor_type=str(row[6]),
            policy_snapshot_id=self._digest(b"legacy-approval-human-review:v1"),
            destinations=(
                OutboxDestination(
                    destination_ref=(
                        f"yaml:{ref.project_ref.namespace}:"
                        f"{ref.project_ref.project_id}:{ref.proposal_id}"
                    )
                ),
            ),
            before_state=payload.before_status,
            after_state=payload.after_status,
            definition_digest=str(row[9]),
            source_state_revision=payload.source_state_revision,
            payload=payload,
        )
        if self._timestamp(result[0].occurred_at) != str(row[7]):
            raise GovernanceEventError("LEGACY_APPROVAL_REVIEW_SOURCE_MISMATCH")
        return result

    def _append_apply_job_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        job_event_id: str,
        payload: ApplyJobProjectionPayload,
    ) -> tuple[AuditEventView, tuple[OutboxEventView, ...]]:
        if not connection.in_transaction:
            raise GovernanceEventError("GOVERNANCE_TRANSACTION_REQUIRED")
        row = connection.execute(
            """
            SELECT e.command_id, e.project_namespace, e.project_id, e.proposal_id,
                   e.event_type, e.worker_id, e.before_status, e.status,
                   e.payload_digest, e.payload_json, s.definition_digest
            FROM governance_apply_job_events e
            JOIN governance_approved_snapshots s ON s.snapshot_id = e.snapshot_id
            WHERE e.job_event_id = ?
            """,
            (job_event_id,),
        ).fetchone()
        payload_json = self._canonical_json(payload.model_dump(mode="json"))
        payload_digest = self._digest(payload_json.encode("utf-8"))
        if (
            row is None
            or tuple(str(value) for value in row[1:4]) != self._identity(ref)
            or str(row[4]) != payload.event_type
            or str(row[5]) != payload.worker_id
            or str(row[7]) != payload.status
            or str(row[8]) != payload_digest
            or str(row[9]) != payload_json
        ):
            raise GovernanceEventError("APPLY_JOB_AUDIT_SOURCE_MISMATCH")
        destinations = (
            OutboxDestination(
                destination_ref=f"apply-job:{payload.job_id}:state",
                supersession_key=f"apply-job-state:{payload.job_id}",
            ),
            OutboxDestination(
                destination_ref=f"apply-job:{payload.job_id}:observer",
            ),
        )
        return self._append_verified_event_in_transaction(
            connection,
            ref,
            command_id=str(row[0]),
            event_type=f"apply_job.{payload.event_type}",
            actor_id=payload.worker_id,
            actor_type="service",
            policy_snapshot_id=self._digest(
                self._canonical_json(
                    {
                        "fencing_token": payload.fencing_token,
                        "job_id": payload.job_id,
                        "worker_id": payload.worker_id,
                    }
                ).encode("utf-8")
            ),
            destinations=destinations,
            before_state=str(row[6]),
            after_state=payload.status,
            definition_digest=str(row[10]),
            source_state_revision=payload.event_sequence,
            payload=payload,
        )

    def _append_publish_resolution_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        resolution_event_id: str,
        coordinator_id: str,
        payload: PublishResolutionProjectionPayload,
    ) -> tuple[AuditEventView, tuple[OutboxEventView, ...]]:
        if not connection.in_transaction:
            raise GovernanceEventError("GOVERNANCE_TRANSACTION_REQUIRED")
        row = connection.execute(
            """
            SELECT e.command_id, e.project_namespace, e.project_id, e.proposal_id,
                   resolution_type, intent_status, job_status, proposal_status,
                   payload_digest, payload_json, e.claim_id, resolver_id,
                   r.before_job_status, r.stream_revision
            FROM governance_publish_resolution_events e
            JOIN governance_publish_resolution_roots r
              ON r.resolution_event_id = e.resolution_event_id
            WHERE e.resolution_event_id = ?
            """,
            (resolution_event_id,),
        ).fetchone()
        payload_json = self._canonical_json(payload.model_dump(mode="json"))
        payload_digest = self._digest(payload_json.encode("utf-8"))
        if (
            row is None
            or tuple(str(value) for value in row[1:4]) != self._identity(ref)
            or str(row[4]) != payload.resolution_type
            or str(row[5]) != payload.intent_status
            or str(row[6]) != payload.job_status
            or str(row[7]) != payload.proposal_status
            or str(row[8]) != payload_digest
            or str(row[9]) != payload_json
            or str(row[11]) != coordinator_id
            or payload.before_job_status is None
            or str(row[12]) != payload.before_job_status
            or payload.stream_revision is None
            or int(row[13]) != payload.stream_revision
        ):
            raise GovernanceEventError("PUBLISH_RESOLUTION_AUDIT_SOURCE_MISMATCH")
        destinations = (
            OutboxDestination(
                destination_ref=f"apply-job:{payload.job_id}:state",
                supersession_key=f"apply-job-state:{payload.job_id}",
            ),
            OutboxDestination(destination_ref=f"apply-job:{payload.job_id}:observer"),
        )
        return self._append_verified_event_in_transaction(
            connection,
            ref,
            command_id=str(row[0]),
            event_type=f"publish.{payload.resolution_type}",
            actor_id=str(row[11]),
            actor_type="service",
            policy_snapshot_id=self._digest(str(row[10]).encode("utf-8")),
            destinations=destinations,
            before_state=payload.before_job_status,
            after_state=payload.job_status,
            definition_digest=self._publish_definition_digest(connection, payload.intent_id),
            source_state_revision=payload.stream_revision,
            payload=payload,
        )

    @staticmethod
    def _publish_definition_digest(connection: sqlite3.Connection, intent_id: str) -> str:
        row = connection.execute(
            """
            SELECT s.definition_digest
            FROM governance_publish_intents i
            JOIN governance_approved_snapshots s ON s.snapshot_id = i.snapshot_id
            WHERE i.intent_id = ?
            """,
            (intent_id,),
        ).fetchone()
        if row is None:
            raise GovernanceEventError("PUBLISH_RESOLUTION_AUDIT_SOURCE_MISMATCH")
        return str(row[0])

    def _append_verified_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        command_id: str,
        event_type: str,
        actor_id: str,
        actor_type: str,
        policy_snapshot_id: str,
        destinations: Sequence[OutboxDestination],
        before_state: str,
        after_state: str,
        definition_digest: str,
        source_state_revision: int,
        payload: BaseModel,
    ) -> tuple[AuditEventView, tuple[OutboxEventView, ...]]:
        destination_manifest_digest = self._destination_manifest_digest(destinations)
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
        event_hash = self._audit_hash(
            event_id=event_id,
            command_id=command_id,
            event_type=event_type,
            ref=ref,
            aggregate_sequence=aggregate_sequence,
            actor_id=actor_id,
            actor_type=actor_type,
            policy_snapshot_id=policy_snapshot_id,
            before_state=before_state,
            after_state=after_state,
            definition_digest=definition_digest,
            destination_manifest_digest=destination_manifest_digest,
            destination_count=len(destinations),
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
                destination_manifest_digest, destination_count,
                previous_event_hash, event_hash, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                command_id,
                event_type,
                *self._identity(ref),
                aggregate_sequence,
                actor_id,
                actor_type,
                policy_snapshot_id,
                before_state,
                after_state,
                definition_digest,
                destination_manifest_digest,
                len(destinations),
                previous_hash,
                event_hash,
                timestamp,
            ),
        )
        payload_json = self._canonical_json(payload.model_dump(mode="json"))
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
        consumed_token_mismatch = connection.execute(
            """
            SELECT 1
            FROM governance_action_tokens t
            LEFT JOIN governance_decision_results r ON r.token_id = t.token_id
            WHERE t.state = 'consumed'
            GROUP BY t.token_id
            HAVING COUNT(r.idempotency_key) != 1
            LIMIT 1
            """
        ).fetchone()
        if consumed_token_mismatch is not None:
            raise GovernanceEventError("DECISION_RESULT_ROOT_MISMATCH")
        result_token_mismatch = connection.execute(
            """
            SELECT 1
            FROM governance_decision_results r
            LEFT JOIN governance_action_tokens t ON t.token_id = r.token_id
            WHERE t.token_id IS NULL OR t.state != 'consumed'
               OR t.project_namespace != r.project_namespace
               OR t.project_id != r.project_id
               OR t.proposal_id != r.proposal_id
               OR t.allowed_action != r.action
               OR t.allowed_actor_id != r.actor_id
               OR t.allowed_actor_type != r.actor_type
               OR t.bound_channel_json != r.channel_json
               OR t.active_definition_digest != r.active_definition_digest
               OR t.content_revision != r.content_revision
               OR t.state_revision + 1 != r.state_revision
               OR t.decision_epoch != r.decision_epoch
            LIMIT 1
            """
        ).fetchone()
        if result_token_mismatch is not None:
            raise GovernanceEventError("DECISION_RESULT_ROOT_MISMATCH")
        active_decision_mismatch = connection.execute(
            """
            SELECT 1
            FROM governance_active_proposals p
            WHERE p.status IN ('approved', 'rejected', 'changes_requested')
              AND (
                SELECT COUNT(*) FROM governance_decision_results r
                WHERE r.project_namespace = p.project_namespace
                  AND r.project_id = p.project_id
                  AND r.proposal_id = p.proposal_id
                  AND r.proposal_status = p.status
                  AND r.active_definition_digest = p.active_definition_digest
                  AND r.content_revision = p.content_revision
                  AND r.state_revision = p.state_revision
                  AND r.decision_epoch = p.decision_epoch
              ) != 1
            LIMIT 1
            """
        ).fetchone()
        if active_decision_mismatch is not None:
            raise GovernanceEventError("DECISION_RESULT_ROOT_MISMATCH")
        has_apply_tables = any(
            str(row[1]) == "grant_id"
            for row in connection.execute(
                "PRAGMA table_info(governance_apply_request_results)"
            ).fetchall()
        )
        consumed_grant_mismatch = (
            connection.execute(
                """
            SELECT 1
            FROM governance_apply_grants g
            LEFT JOIN governance_apply_request_results r ON r.grant_id = g.grant_id
            WHERE g.state = 'consumed'
            GROUP BY g.grant_id
            HAVING COUNT(r.idempotency_key) != 1
            LIMIT 1
            """
            ).fetchone()
            if has_apply_tables
            else None
        )
        if consumed_grant_mismatch is not None:
            raise GovernanceEventError("APPLY_RESULT_ROOT_MISMATCH")
        apply_result_mismatch = (
            connection.execute(
                """
            SELECT 1
            FROM governance_apply_request_results r
            LEFT JOIN governance_apply_grants g ON g.grant_id = r.grant_id
            LEFT JOIN governance_approved_snapshots s ON s.snapshot_id = r.snapshot_id
            LEFT JOIN governance_apply_jobs j ON j.job_id = r.job_id
            WHERE g.grant_id IS NULL OR s.snapshot_id IS NULL OR j.job_id IS NULL
               OR g.state != 'consumed'
               OR g.snapshot_id != r.snapshot_id
               OR g.project_namespace != r.project_namespace
               OR g.project_id != r.project_id OR g.proposal_id != r.proposal_id
               OR g.allowed_action != 'request_apply'
               OR g.allowed_actor_id != r.actor_id OR g.allowed_actor_type != r.actor_type
               OR g.bound_channel_json != r.channel_json
               OR j.snapshot_id != r.snapshot_id
               OR j.project_namespace != r.project_namespace
               OR j.project_id != r.project_id OR j.proposal_id != r.proposal_id
               OR j.approved_snapshot_digest != s.snapshot_digest
               OR j.expected_base_revision != s.expected_base_revision
            LIMIT 1
            """
            ).fetchone()
            if has_apply_tables
            else None
        )
        if apply_result_mismatch is not None:
            raise GovernanceEventError("APPLY_RESULT_ROOT_MISMATCH")
        orphan_apply_job = (
            connection.execute(
                """
                SELECT 1 FROM governance_apply_jobs j
                LEFT JOIN governance_apply_request_results r ON r.job_id = j.job_id
                GROUP BY j.job_id
                HAVING COUNT(r.idempotency_key) != 1
                LIMIT 1
                """
            ).fetchone()
            if has_apply_tables
            else None
        )
        if orphan_apply_job is not None:
            raise GovernanceEventError("APPLY_RESULT_ROOT_MISMATCH")
        orphan_approved_snapshot = (
            connection.execute(
                """
                SELECT 1 FROM governance_approved_snapshots s
                LEFT JOIN governance_apply_grants g ON g.snapshot_id = s.snapshot_id
                GROUP BY s.snapshot_id
                HAVING COUNT(g.grant_id) < 1
                LIMIT 1
                """
            ).fetchone()
            if has_apply_tables
            else None
        )
        if orphan_approved_snapshot is not None:
            raise GovernanceEventError("APPLY_RESULT_ROOT_MISMATCH")
        active_apply_mismatch = (
            connection.execute(
                """
            SELECT 1 FROM governance_active_proposals p
            WHERE p.status = 'apply_requested' AND (
                SELECT COUNT(*) FROM governance_apply_request_results r
                JOIN governance_approved_snapshots s ON s.snapshot_id = r.snapshot_id
                WHERE r.project_namespace = p.project_namespace
                  AND r.project_id = p.project_id AND r.proposal_id = p.proposal_id
                  AND s.definition_digest = p.active_definition_digest
                  AND s.content_revision = p.content_revision
                  AND s.state_revision + 1 = p.state_revision
                  AND s.decision_epoch = p.decision_epoch
            ) != 1
            LIMIT 1
            """
            ).fetchone()
            if has_apply_tables
            else None
        )
        if active_apply_mismatch is not None:
            raise GovernanceEventError("APPLY_RESULT_ROOT_MISMATCH")
        decision_commands: dict[str, tuple[str, int, str, int]] = {}
        has_legacy_import_commands = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'governance_legacy_import_commands'"
            ).fetchone()
            is not None
        )
        legacy_rows = (
            connection.execute(
                """
                SELECT c.command_id, c.migration_id, c.project_namespace,
                       c.project_id, c.proposal_id, c.idempotency_key,
                       c.request_fingerprint, c.event_type, c.actor_id,
                       c.actor_type, c.occurred_at, c.source_artifact_digest,
                       c.reason, c.definition_digest, c.source_state_revision,
                       c.payload_digest, c.payload_json,
                       i.source_status, i.source_revision, i.target_status,
                       i.state_revision, i.approval_disposition
                FROM governance_legacy_import_commands c
                JOIN governance_legacy_migration_items i
                  ON i.migration_id = c.migration_id
                 AND i.project_namespace = c.project_namespace
                 AND i.project_id = c.project_id
                 AND i.proposal_id = c.proposal_id
                ORDER BY c.migration_id, c.proposal_id
                """
            ).fetchall()
            if has_legacy_import_commands
            else ()
        )
        for legacy in legacy_rows:
            try:
                legacy_payload = LegacyMigrationProjectionPayload.model_validate_json(
                    str(legacy[16])
                )
            except ValueError as error:
                raise GovernanceEventError("LEGACY_MIGRATION_EVENT_ROOT_MISMATCH") from error
            ref = cls._proposal_ref(legacy[2], legacy[3], legacy[4])
            expected_destination = OutboxDestination(
                destination_ref=legacy_payload.projection_destination_ref
                or f"yaml:{ref.project_ref.namespace}:"
                f"{ref.project_ref.project_id}:{ref.proposal_id}"
            )
            expected_payload_digest = cls._digest(str(legacy[16]).encode("utf-8"))
            if (
                str(legacy[15]) != expected_payload_digest
                or legacy_payload.aggregate_ref != ref
                or legacy_payload.migration_id != str(legacy[1])
                or legacy_payload.idempotency_key != str(legacy[5])
                or legacy_payload.event_type != str(legacy[7])
                or legacy_payload.source_artifact_digest != str(legacy[11])
                or legacy_payload.reason != legacy[12]
                or legacy_payload.source_state_revision != int(legacy[14])
                or legacy_payload.source_status != str(legacy[17])
                or legacy_payload.source_revision != int(legacy[18])
                or legacy_payload.target_status != str(legacy[19])
                or legacy_payload.imported_state_revision != int(legacy[20])
                or legacy_payload.approval_disposition != str(legacy[21])
            ):
                raise GovernanceEventError("LEGACY_MIGRATION_EVENT_ROOT_MISMATCH")
            command_id = str(legacy[0])
            destinations = (expected_destination,)
            decision_commands[command_id] = (
                expected_payload_digest,
                int(legacy[14]),
                cls._destination_manifest_digest(destinations),
                1,
            )
            audits = connection.execute(
                "SELECT * FROM governance_audit_events WHERE command_id = ?",
                (command_id,),
            ).fetchall()
            if len(audits) != 1:
                raise GovernanceEventError("LEGACY_MIGRATION_AUDIT_MISMATCH")
            audit = cls._audit_view(cast(tuple[object, ...], audits[0]))
            expected_policy = cls._digest(
                (
                    f"{legacy_payload.mapping_policy_version}:"
                    f"{legacy_payload.validation_policy_ref}"
                ).encode()
            )
            if (
                audit.event_type != str(legacy[7])
                or audit.actor_id != str(legacy[8])
                or audit.actor_type != str(legacy[9])
                or audit.policy_snapshot_id != expected_policy
                or audit.before_state != str(legacy[17])
                or audit.after_state != str(legacy[19])
                or audit.definition_digest != str(legacy[13])
                or cls._timestamp(audit.occurred_at) != str(legacy[10])
                or audit.destination_manifest_digest != decision_commands[command_id][2]
                or audit.destination_count != 1
            ):
                raise GovernanceEventError("LEGACY_MIGRATION_AUDIT_MISMATCH")
            hold = connection.execute(
                """
                SELECT reason_code, source_artifact_digest
                FROM governance_legacy_approval_holds
                WHERE migration_id = ? AND project_namespace = ?
                  AND project_id = ? AND proposal_id = ?
                """,
                (legacy[1], legacy[2], legacy[3], legacy[4]),
            ).fetchone()
            if str(legacy[7]) == "migration.synthetic_approval":
                if hold is None or tuple(hold) != (
                    "legacy_approval_without_audit",
                    str(legacy[11]),
                ):
                    raise GovernanceEventError("LEGACY_APPROVAL_HOLD_MISMATCH")
            elif hold is not None:
                raise GovernanceEventError("LEGACY_APPROVAL_HOLD_MISMATCH")
        if has_legacy_import_commands:
            review_rows = connection.execute(
                """
                SELECT review_id, migration_id, project_namespace, project_id,
                       proposal_id, idempotency_key, request_fingerprint, actor_id,
                       actor_type, occurred_at, reason, request_id, channel_json,
                       definition_digest, source_state_revision, payload_digest, payload_json
                FROM governance_legacy_approval_reviews
                ORDER BY migration_id, proposal_id
                """
            ).fetchall()
            for review in review_rows:
                try:
                    review_payload = LegacyApprovalReviewProjectionPayload.model_validate_json(
                        str(review[16])
                    )
                except ValueError as error:
                    raise GovernanceEventError("LEGACY_APPROVAL_REVIEW_ROOT_MISMATCH") from error
                ref = cls._proposal_ref(review[2], review[3], review[4])
                destination = OutboxDestination(
                    destination_ref=f"yaml:{review[2]}:{review[3]}:{review[4]}"
                )
                expected_payload_digest = cls._digest(str(review[16]).encode("utf-8"))
                if (
                    str(review[15]) != expected_payload_digest
                    or review_payload.aggregate_ref != ref
                    or review_payload.migration_id != str(review[1])
                    or review_payload.idempotency_key != str(review[5])
                    or review_payload.reason != str(review[10])
                    or review_payload.source_state_revision != int(review[14])
                ):
                    raise GovernanceEventError("LEGACY_APPROVAL_REVIEW_ROOT_MISMATCH")
                command_id = str(review[0])
                manifest_digest = cls._destination_manifest_digest((destination,))
                decision_commands[command_id] = (
                    expected_payload_digest,
                    int(review[14]),
                    manifest_digest,
                    1,
                )
                audits = connection.execute(
                    "SELECT * FROM governance_audit_events WHERE command_id = ?",
                    (command_id,),
                ).fetchall()
                if len(audits) != 1:
                    raise GovernanceEventError("LEGACY_APPROVAL_REVIEW_AUDIT_MISMATCH")
                audit = cls._audit_view(cast(tuple[object, ...], audits[0]))
                if (
                    audit.event_type != review_payload.event_type
                    or audit.actor_id != str(review[7])
                    or audit.actor_type != str(review[8])
                    or audit.policy_snapshot_id != cls._digest(b"legacy-approval-human-review:v1")
                    or audit.before_state != review_payload.before_status
                    or audit.after_state != review_payload.after_status
                    or audit.definition_digest != str(review[13])
                    or cls._timestamp(audit.occurred_at) != str(review[9])
                    or audit.destination_manifest_digest != manifest_digest
                    or audit.destination_count != 1
                ):
                    raise GovernanceEventError("LEGACY_APPROVAL_REVIEW_AUDIT_MISMATCH")
            uncovered_item = connection.execute(
                """
                SELECT 1
                FROM governance_legacy_migration_items i
                LEFT JOIN governance_legacy_import_commands c
                  ON c.migration_id = i.migration_id
                 AND c.project_namespace = i.project_namespace
                 AND c.project_id = i.project_id AND c.proposal_id = i.proposal_id
                LEFT JOIN governance_legacy_event_backfill_pending p
                  ON p.migration_id = i.migration_id
                 AND p.project_namespace = i.project_namespace
                 AND p.project_id = i.project_id AND p.proposal_id = i.proposal_id
                WHERE (c.command_id IS NULL) = (p.migration_id IS NULL)
                LIMIT 1
                """
            ).fetchone()
            if uncovered_item is not None:
                raise GovernanceEventError("LEGACY_MIGRATION_EVENT_ROOT_MISMATCH")
            orphan_hold = connection.execute(
                """
                SELECT 1 FROM governance_legacy_approval_holds h
                LEFT JOIN governance_legacy_import_commands c
                  ON c.migration_id = h.migration_id
                 AND c.project_namespace = h.project_namespace
                 AND c.project_id = h.project_id
                 AND c.proposal_id = h.proposal_id
                 AND c.event_type = 'migration.synthetic_approval'
                WHERE c.command_id IS NULL LIMIT 1
                """
            ).fetchone()
            if orphan_hold is not None:
                raise GovernanceEventError("LEGACY_APPROVAL_HOLD_MISMATCH")
            idempotency_collision = connection.execute(
                """
                SELECT idempotency_key FROM (
                    SELECT idempotency_key FROM governance_legacy_import_commands
                    UNION ALL
                    SELECT idempotency_key FROM governance_legacy_approval_reviews
                    UNION ALL
                    SELECT idempotency_key FROM governance_decision_results
                    UNION ALL
                    SELECT idempotency_key FROM governance_apply_request_results
                ) GROUP BY idempotency_key HAVING COUNT(*) != 1 LIMIT 1
                """
            ).fetchone()
            if idempotency_collision is not None:
                raise GovernanceEventError("IDEMPOTENCY_CONFLICT")
        decision_rows = connection.execute(
            """
            SELECT idempotency_key, project_namespace, project_id, proposal_id,
                   action, actor_id, actor_type, proposal_status,
                   active_definition_digest, content_revision,
                   state_revision, decision_epoch, channel_json
            FROM governance_decision_results
            """
        ).fetchall()
        for decision in decision_rows:
            command_id = cls._decision_command_id(str(decision[0]))
            decision_payload = DecisionProjectionPayload(
                action=str(decision[4]),
                active_definition_digest=str(decision[8]),
                aggregate_ref=cls._proposal_ref(decision[1], decision[2], decision[3]),
                content_revision=int(decision[9]),
                decision_epoch=int(decision[11]),
                proposal_status=str(decision[7]),
                state_revision=int(decision[10]),
            )
            expected_payload_json = cls._canonical_json(decision_payload.model_dump(mode="json"))
            channel = cast(dict[str, object], json.loads(str(decision[12])))
            channel_digest = hashlib.sha256(
                cls._canonical_json(channel).encode("utf-8")
            ).hexdigest()
            expected_destinations = (
                OutboxDestination(
                    destination_ref=f"yaml:{decision[1]}:{decision[2]}:{decision[3]}"
                ),
                OutboxDestination(
                    destination_ref=f"provider:{channel['provider']}:{channel_digest}",
                    supersession_key=f"proposal-card:{decision[3]}",
                ),
            )
            decision_commands[command_id] = (
                cls._digest(expected_payload_json.encode("utf-8")),
                int(decision[10]),
                cls._destination_manifest_digest(expected_destinations),
                len(expected_destinations),
            )
            audits = connection.execute(
                """
                SELECT * FROM governance_audit_events
                WHERE command_id = ? AND project_namespace = ?
                  AND project_id = ? AND proposal_id = ?
                """,
                (command_id, decision[1], decision[2], decision[3]),
            ).fetchall()
            if len(audits) != 1:
                raise GovernanceEventError("DECISION_AUDIT_MISMATCH")
            audit = cls._audit_view(cast(tuple[object, ...], audits[0]))
            if (
                audit.event_type != f"proposal.{decision[7]}"
                or audit.actor_id != str(decision[5])
                or audit.actor_type != str(decision[6])
                or audit.before_state != "reviewed"
                or audit.after_state != str(decision[7])
                or audit.definition_digest != str(decision[8])
                or audit.destination_manifest_digest != decision_commands[command_id][2]
                or audit.destination_count != decision_commands[command_id][3]
            ):
                raise GovernanceEventError("DECISION_AUDIT_MISMATCH")
        apply_rows = (
            connection.execute(
                """
            SELECT r.idempotency_key, r.project_namespace, r.project_id, r.proposal_id,
                   r.actor_id, r.actor_type, r.proposal_status, r.snapshot_id, r.job_id,
                   r.channel_json, s.definition_digest, s.snapshot_digest,
                   s.content_revision, s.state_revision + 1, s.decision_epoch,
                   s.expected_base_revision
            FROM governance_apply_request_results r
            JOIN governance_approved_snapshots s ON s.snapshot_id = r.snapshot_id
              AND s.project_namespace = r.project_namespace
              AND s.project_id = r.project_id AND s.proposal_id = r.proposal_id
            """
            ).fetchall()
            if has_apply_tables
            else ()
        )
        for apply in apply_rows:
            command_id = cls._apply_command_id(str(apply[0]))
            ref = cls._proposal_ref(apply[1], apply[2], apply[3])
            payload = ApplyProjectionPayload(
                action="request_apply",
                active_definition_digest=str(apply[10]),
                aggregate_ref=ref,
                approved_snapshot_digest=str(apply[11]),
                content_revision=int(apply[12]),
                decision_epoch=int(apply[14]),
                expected_base_revision=str(apply[15]),
                job_id=str(apply[8]),
                proposal_status="apply_requested",
                snapshot_id=str(apply[7]),
                state_revision=int(apply[13]),
            )
            expected_payload_json = cls._canonical_json(payload.model_dump(mode="json"))
            channel = cast(dict[str, object], json.loads(str(apply[9])))
            channel_digest = hashlib.sha256(
                cls._canonical_json(channel).encode("utf-8")
            ).hexdigest()
            apply_destinations = (
                OutboxDestination(destination_ref=f"yaml:{apply[1]}:{apply[2]}:{apply[3]}"),
                OutboxDestination(
                    destination_ref=f"provider:{channel['provider']}:{channel_digest}",
                    supersession_key=f"proposal-card:{apply[3]}",
                ),
            )
            decision_commands[command_id] = (
                cls._digest(expected_payload_json.encode("utf-8")),
                int(apply[13]),
                cls._destination_manifest_digest(apply_destinations),
                len(apply_destinations),
            )
            audits = connection.execute(
                """
                SELECT * FROM governance_audit_events
                WHERE command_id = ? AND project_namespace = ?
                  AND project_id = ? AND proposal_id = ?
                """,
                (command_id, apply[1], apply[2], apply[3]),
            ).fetchall()
            if len(audits) != 1:
                raise GovernanceEventError("APPLY_AUDIT_MISMATCH")
            audit = cls._audit_view(cast(tuple[object, ...], audits[0]))
            if (
                audit.event_type != "proposal.apply_requested"
                or audit.actor_id != str(apply[4])
                or audit.actor_type != str(apply[5])
                or audit.before_state != "approved"
                or audit.after_state != "apply_requested"
                or audit.definition_digest != str(apply[10])
                or audit.destination_manifest_digest != decision_commands[command_id][2]
                or audit.destination_count != decision_commands[command_id][3]
            ):
                raise GovernanceEventError("APPLY_AUDIT_MISMATCH")
        has_job_events = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'governance_apply_job_events'"
            ).fetchone()
            is not None
        )
        job_event_rows = (
            connection.execute(
                """
            SELECT e.command_id, e.project_namespace, e.project_id, e.proposal_id,
                   e.event_type, e.worker_id, e.before_status, e.status,
                   e.attempts, e.fencing_token, e.lease_owner, e.lease_expires_at,
                   e.retry_at, e.staged_artifact_digest, e.publish_request_digest,
                   e.last_error_code, e.payload_digest, e.payload_json,
                   e.job_id, e.event_sequence, s.definition_digest
            FROM governance_apply_job_events e
            JOIN governance_approved_snapshots s ON s.snapshot_id = e.snapshot_id
            ORDER BY e.created_at, e.job_event_id
            """
            ).fetchall()
            if has_job_events
            else ()
        )
        for job_event in job_event_rows:
            payload_json = str(job_event[17])
            payload_digest = cls._digest(payload_json.encode("utf-8"))
            try:
                job_payload = ApplyJobProjectionPayload.model_validate_json(payload_json)
            except ValueError as error:
                raise GovernanceEventError("APPLY_JOB_EVENT_ROOT_MISMATCH") from error
            ref = cls._proposal_ref(job_event[1], job_event[2], job_event[3])
            if (
                str(job_event[16]) != payload_digest
                or job_payload.aggregate_ref != ref
                or job_payload.event_type != str(job_event[4])
                or job_payload.worker_id != str(job_event[5])
                or job_payload.status != str(job_event[7])
                or job_payload.attempts != int(job_event[8])
                or job_payload.fencing_token != int(job_event[9])
                or job_payload.lease_owner != job_event[10]
                or cls._optional_timestamp(job_payload.lease_expires_at) != job_event[11]
                or cls._optional_timestamp(job_payload.retry_at) != job_event[12]
                or job_payload.staged_artifact_digest != job_event[13]
                or job_payload.publish_request_digest != job_event[14]
                or job_payload.last_error_code != job_event[15]
                or job_payload.job_id != str(job_event[18])
                or job_payload.event_sequence != int(job_event[19])
            ):
                raise GovernanceEventError("APPLY_JOB_EVENT_ROOT_MISMATCH")
            job_destinations = (
                OutboxDestination(
                    destination_ref=f"apply-job:{job_event[18]}:state",
                    supersession_key=f"apply-job-state:{job_event[18]}",
                ),
                OutboxDestination(
                    destination_ref=f"apply-job:{job_event[18]}:observer",
                ),
            )
            command_id = str(job_event[0])
            decision_commands[command_id] = (
                payload_digest,
                job_payload.event_sequence,
                cls._destination_manifest_digest(job_destinations),
                2,
            )
            audits = connection.execute(
                "SELECT * FROM governance_audit_events WHERE command_id = ?",
                (command_id,),
            ).fetchall()
            if len(audits) != 1:
                raise GovernanceEventError("APPLY_JOB_AUDIT_MISMATCH")
            audit = cls._audit_view(cast(tuple[object, ...], audits[0]))
            if (
                audit.event_type != f"apply_job.{job_event[4]}"
                or audit.actor_id != str(job_event[5])
                or audit.actor_type != "service"
                or audit.before_state != str(job_event[6])
                or audit.after_state != str(job_event[7])
                or audit.definition_digest != str(job_event[20])
                or audit.destination_manifest_digest != decision_commands[command_id][2]
                or audit.destination_count != 2
            ):
                raise GovernanceEventError("APPLY_JOB_AUDIT_MISMATCH")
        has_publish_resolution_events = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'governance_publish_resolution_events'"
            ).fetchone()
            is not None
        )
        applied_revision_mismatch = (
            connection.execute(
                """
                SELECT 1 FROM governance_active_proposals
                WHERE (status = 'applied') != (applied_revision IS NOT NULL)
                   OR (
                       status = 'applied' AND (
                           SELECT COUNT(*)
                           FROM governance_publish_resolution_events e
                           JOIN governance_publish_results r
                             ON r.intent_id = e.intent_id
                           WHERE e.project_namespace = governance_active_proposals.project_namespace
                             AND e.project_id = governance_active_proposals.project_id
                             AND e.proposal_id = governance_active_proposals.proposal_id
                             AND e.resolution_type = 'published'
                             AND e.applied_revision = governance_active_proposals.applied_revision
                             AND r.outcome = 'published'
                             AND r.actual_ref = governance_active_proposals.applied_revision
                       ) != 1
                   )
                LIMIT 1
                """
            ).fetchone()
            if has_publish_resolution_events
            else None
        )
        if applied_revision_mismatch is not None:
            raise GovernanceEventError("PUBLISH_RESOLUTION_ROOT_MISMATCH")
        if has_publish_resolution_events:
            resolution_root_mismatch = connection.execute(
                """
                SELECT 1
                FROM governance_publish_resolution_events e
                LEFT JOIN governance_publish_resolution_roots r
                  ON r.resolution_event_id = e.resolution_event_id
                 AND r.intent_id = e.intent_id AND r.claim_id = e.claim_id
                 AND r.job_id = e.job_id
                 AND r.project_namespace = e.project_namespace
                 AND r.project_id = e.project_id AND r.proposal_id = e.proposal_id
                WHERE r.resolution_event_id IS NULL
                UNION ALL
                SELECT 1 FROM governance_publish_resolution_events
                WHERE resolution_type IN ('published', 'publish_conflict', 'failed', 'cancelled')
                GROUP BY intent_id HAVING COUNT(*) != 1
                LIMIT 1
                """
            ).fetchone()
            if resolution_root_mismatch is not None:
                raise GovernanceEventError("PUBLISH_RESOLUTION_ROOT_MISMATCH")
        resolution_rows = (
            connection.execute(
                """
                SELECT e.command_id, e.project_namespace, e.project_id, e.proposal_id,
                       e.resolution_type, e.intent_status, e.job_status, e.proposal_status,
                       e.payload_digest, e.payload_json, e.resolution_sequence,
                       e.intent_id, e.job_id, e.actual_ref, e.applied_revision,
                       e.error_code, e.claim_id, e.resolver_id, s.definition_digest,
                       r.before_job_status, r.stream_revision
                FROM governance_publish_resolution_events e
                JOIN governance_publish_resolution_roots r
                  ON r.resolution_event_id = e.resolution_event_id
                 AND r.intent_id = e.intent_id AND r.claim_id = e.claim_id
                 AND r.job_id = e.job_id
                 AND r.project_namespace = e.project_namespace
                 AND r.project_id = e.project_id AND r.proposal_id = e.proposal_id
                JOIN governance_publish_claims c
                  ON c.claim_id = e.claim_id AND c.intent_id = e.intent_id
                JOIN governance_publish_intents i
                  ON i.intent_id = e.intent_id AND i.job_id = e.job_id
                 AND i.project_namespace = e.project_namespace
                 AND i.project_id = e.project_id AND i.proposal_id = e.proposal_id
                JOIN governance_approved_snapshots s ON s.snapshot_id = i.snapshot_id
                ORDER BY e.intent_id, e.resolution_sequence
                """
            ).fetchall()
            if has_publish_resolution_events
            else ()
        )
        resolution_sequences: dict[str, int] = {}
        for resolution in resolution_rows:
            try:
                resolution_payload = PublishResolutionProjectionPayload.model_validate_json(
                    str(resolution[9])
                )
            except ValueError as error:
                raise GovernanceEventError("PUBLISH_RESOLUTION_ROOT_MISMATCH") from error
            ref = cls._proposal_ref(resolution[1], resolution[2], resolution[3])
            expected_sequence = resolution_sequences.get(str(resolution[11]), 0) + 1
            resolution_sequences[str(resolution[11])] = expected_sequence
            if (
                cls._digest(str(resolution[9]).encode("utf-8")) != str(resolution[8])
                or int(resolution[10]) != expected_sequence
                or resolution_payload.aggregate_ref != ref
                or resolution_payload.resolution_type != str(resolution[4])
                or resolution_payload.intent_status != str(resolution[5])
                or resolution_payload.job_status != str(resolution[6])
                or resolution_payload.proposal_status != str(resolution[7])
                or resolution_payload.resolution_sequence != int(resolution[10])
                or resolution_payload.intent_id != str(resolution[11])
                or resolution_payload.job_id != str(resolution[12])
                or resolution_payload.actual_ref != resolution[13]
                or resolution_payload.applied_revision != resolution[14]
                or resolution_payload.error_code != resolution[15]
                or (
                    resolution_payload.before_job_status is not None
                    and resolution_payload.before_job_status != str(resolution[19])
                )
                or (
                    resolution_payload.stream_revision is not None
                    and resolution_payload.stream_revision != int(resolution[20])
                )
            ):
                raise GovernanceEventError("PUBLISH_RESOLUTION_ROOT_MISMATCH")
            resolution_destinations = (
                OutboxDestination(
                    destination_ref=f"apply-job:{resolution_payload.job_id}:state",
                    supersession_key=f"apply-job-state:{resolution_payload.job_id}",
                ),
                OutboxDestination(
                    destination_ref=f"apply-job:{resolution_payload.job_id}:observer"
                ),
            )
            command_id = str(resolution[0])
            decision_commands[command_id] = (
                str(resolution[8]),
                int(resolution[20]),
                cls._destination_manifest_digest(resolution_destinations),
                2,
            )
            audits = connection.execute(
                "SELECT * FROM governance_audit_events WHERE command_id = ?",
                (command_id,),
            ).fetchall()
            if len(audits) != 1:
                raise GovernanceEventError("PUBLISH_RESOLUTION_AUDIT_MISMATCH")
            audit = cls._audit_view(cast(tuple[object, ...], audits[0]))
            if (
                audit.event_type != f"publish.{resolution[4]}"
                or audit.actor_id != str(resolution[17])
                or audit.actor_type != "service"
                or audit.before_state != str(resolution[19])
                or audit.after_state != str(resolution[6])
                or audit.definition_digest != str(resolution[18])
                or audit.destination_manifest_digest != decision_commands[command_id][2]
                or audit.destination_count != 2
            ):
                raise GovernanceEventError("PUBLISH_RESOLUTION_AUDIT_MISMATCH")
        if has_job_events:
            jobs = connection.execute(
                """
                SELECT job_id, status, attempts, fencing_token, lease_owner,
                       lease_expires_at, retry_at, staged_artifact_digest,
                       publish_request_digest, last_error_code
                FROM governance_apply_jobs
                """
            ).fetchall()
            for job in jobs:
                events = connection.execute(
                    """
                    SELECT e.status, e.attempts, e.fencing_token, e.lease_owner,
                           e.lease_expires_at, e.retry_at, e.staged_artifact_digest,
                           e.publish_request_digest, e.last_error_code,
                           e.before_status, e.event_type, e.event_sequence
                    FROM governance_apply_job_events e
                    WHERE e.job_id = ?
                    ORDER BY e.event_sequence DESC
                    """,
                    (job[0],),
                ).fetchall()
                if not events:
                    if tuple(job[1:]) != (
                        "queued",
                        0,
                        0,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ):
                        raise GovernanceEventError("APPLY_JOB_EVENT_ROOT_MISMATCH")
                    continue
                latest = events[0]
                if tuple(job[1:]) != tuple(latest[:9]):
                    resolution = connection.execute(
                        """
                        SELECT job_status, error_code
                        FROM governance_publish_resolution_events
                        WHERE job_id = ? ORDER BY resolution_sequence DESC LIMIT 1
                        """,
                        (job[0],),
                    ).fetchone()
                    if (
                        resolution is None
                        or str(job[1]) != str(resolution[0])
                        or tuple(job[2:9]) != tuple(latest[1:8])
                        or job[9] != resolution[1]
                    ):
                        raise GovernanceEventError("APPLY_JOB_EVENT_ROOT_MISMATCH")
                if str(job[1]) == "publish_pending":
                    artifact_count = connection.execute(
                        """
                        SELECT COUNT(*) FROM governance_staging_artifacts
                        WHERE job_id = ? AND fencing_token = ? AND artifact_digest = ?
                        """,
                        (job[0], job[3], job[7]),
                    ).fetchone()[0]
                    publish_count = connection.execute(
                        """
                        SELECT COUNT(*) FROM governance_publish_inputs
                        WHERE job_id = ? AND fencing_token = ?
                          AND publish_request_digest = ?
                        """,
                        (job[0], job[3], job[8]),
                    ).fetchone()[0]
                    if artifact_count != 1 or publish_count != 1:
                        raise GovernanceEventError("APPLY_ARTIFACT_ROOT_MISMATCH")
                chronological = tuple(reversed(events))
                previous_status = "queued"
                previous_attempts = 0
                previous_fence = 0
                for expected_sequence, current in enumerate(chronological, start=1):
                    status = str(current[0])
                    attempts = int(current[1])
                    fence = int(current[2])
                    before_status = str(current[9])
                    event_type = str(current[10])
                    if int(current[11]) != expected_sequence or before_status != previous_status:
                        raise GovernanceEventError("APPLY_JOB_EVENT_ROOT_MISMATCH")
                    if event_type == "claimed":
                        valid = (
                            before_status in {"queued", "retry_wait", "leased", "running"}
                            and status == "leased"
                            and attempts == previous_attempts + 1
                            and fence == previous_fence + 1
                        )
                    elif event_type == "started":
                        valid = before_status == "leased" and status == "running"
                    elif event_type == "heartbeat":
                        valid = before_status == status and status in {"leased", "running"}
                    elif event_type == "retry_scheduled":
                        valid = before_status in {"leased", "running"} and status == "retry_wait"
                    elif event_type == "publish_prepared":
                        valid = before_status == "running" and status == "publish_pending"
                    elif event_type == "dead_lettered":
                        valid = (
                            before_status in {"queued", "retry_wait", "leased", "running"}
                            and status == "dead_letter"
                        )
                    else:
                        valid = False
                    if event_type != "claimed" and (
                        attempts != previous_attempts or fence != previous_fence
                    ):
                        valid = False
                    if not valid:
                        raise GovernanceEventError("APPLY_JOB_EVENT_ROOT_MISMATCH")
                    previous_status = status
                    previous_attempts = attempts
                    previous_fence = fence
            for table, digest_column, bytes_column in (
                ("governance_staging_artifacts", "artifact_digest", "artifact_bytes"),
                (
                    "governance_publish_inputs",
                    "publish_request_digest",
                    "publish_request_bytes",
                ),
            ):
                roots = connection.execute(
                    f"SELECT job_id, fencing_token, {digest_column}, {bytes_column} FROM {table}"
                )
                for root in roots:
                    if cls._digest(bytes(root[3])) != str(root[2]):
                        raise GovernanceEventError("APPLY_ARTIFACT_ROOT_MISMATCH")
                    job = connection.execute(
                        """
                        SELECT fencing_token, staged_artifact_digest,
                               publish_request_digest, status
                        FROM governance_apply_jobs WHERE job_id = ?
                        """,
                        (root[0],),
                    ).fetchone()
                    expected_index = 1 if table == "governance_staging_artifacts" else 2
                    terminal_resolution = (
                        connection.execute(
                            """
                            SELECT 1 FROM governance_publish_resolution_events
                            WHERE job_id = ? AND job_status = ? LIMIT 1
                            """,
                            (root[0], job[3] if job is not None else None),
                        ).fetchone()
                        if has_publish_resolution_events
                        else None
                    )
                    if (
                        job is None
                        or int(job[0]) != int(root[1])
                        or str(job[expected_index]) != str(root[2])
                        or (str(job[3]) != "publish_pending" and terminal_resolution is None)
                    ):
                        raise GovernanceEventError("APPLY_ARTIFACT_ROOT_MISMATCH")
        has_publish_tables = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'governance_publish_intents'"
            ).fetchone()
            is not None
        )
        if has_publish_tables:
            intent_root_mismatch = connection.execute(
                """
                SELECT 1
                FROM governance_publish_intents i
                LEFT JOIN governance_project_publish_gates g
                  ON g.active_intent_id = i.intent_id
                LEFT JOIN governance_publish_results r
                  ON r.intent_id = i.intent_id
                WHERE (
                    i.status IN ('prepared', 'recovery_hold')
                    AND (
                        r.intent_id IS NOT NULL
                        OR g.project_namespace IS NULL
                        OR g.project_namespace != i.project_namespace
                        OR g.project_id != i.project_id
                        OR g.canonical_ref != i.canonical_ref
                        OR (i.status = 'prepared' AND g.state != 'locked')
                        OR (i.status = 'recovery_hold' AND g.state != 'recovery_hold')
                    )
                ) OR (
                    i.status IN ('published', 'publish_conflict', 'cancelled', 'failed')
                    AND (
                        g.project_namespace IS NOT NULL
                        OR r.intent_id IS NULL
                        OR r.job_id != i.job_id
                        OR r.snapshot_id != i.snapshot_id
                        OR r.project_namespace != i.project_namespace
                        OR r.project_id != i.project_id
                        OR r.proposal_id != i.proposal_id
                        OR r.fencing_token != i.fencing_token
                        OR r.expected_old_ref != i.expected_old_ref
                        OR r.candidate_commit != i.candidate_commit
                        OR r.outcome != i.status
                        OR r.error_code IS NOT i.last_error_code
                        OR r.resolved_at != i.resolved_at
                    )
                )
                LIMIT 1
                """
            ).fetchone()
            if intent_root_mismatch is not None:
                raise GovernanceEventError("PUBLISH_INTENT_ROOT_MISMATCH")
            gate_root_mismatch = connection.execute(
                """
                SELECT 1
                FROM governance_project_publish_gates g
                LEFT JOIN governance_publish_intents i
                  ON i.intent_id = g.active_intent_id
                WHERE (
                    g.state = 'unlocked' AND g.active_intent_id IS NOT NULL
                ) OR (
                    g.state IN ('locked', 'recovery_hold') AND (
                        i.intent_id IS NULL
                        OR i.project_namespace != g.project_namespace
                        OR i.project_id != g.project_id
                        OR i.canonical_ref != g.canonical_ref
                        OR (g.state = 'locked' AND i.status != 'prepared')
                        OR (g.state = 'recovery_hold' AND i.status != 'recovery_hold')
                    )
                )
                LIMIT 1
                """
            ).fetchone()
            if gate_root_mismatch is not None:
                raise GovernanceEventError("PUBLISH_GATE_ROOT_MISMATCH")
            has_publish_claims = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'governance_publish_claims'"
                ).fetchone()
                is not None
            )
            if has_publish_claims:
                claim_root_mismatch = connection.execute(
                    """
                    SELECT 1
                    FROM governance_publish_claims c
                    JOIN governance_publish_intents i ON i.intent_id = c.intent_id
                    LEFT JOIN governance_project_publish_gates g
                      ON g.active_intent_id = c.intent_id
                    WHERE c.project_namespace != i.project_namespace
                       OR c.project_id != i.project_id
                       OR (c.state = 'active' AND (
                            i.status NOT IN ('prepared', 'recovery_hold')
                            OR g.project_namespace IS NULL
                            OR (i.status = 'prepared' AND g.state != 'locked')
                            OR (i.status = 'recovery_hold' AND g.state != 'recovery_hold')
                            OR g.project_namespace != c.project_namespace
                            OR g.project_id != c.project_id
                       ))
                    LIMIT 1
                    """
                ).fetchone()
                if claim_root_mismatch is not None:
                    raise GovernanceEventError("PUBLISH_CLAIM_ROOT_MISMATCH")
                claim_sequences = connection.execute(
                    """
                    SELECT intent_id, claim_fencing_token
                    FROM governance_publish_claims
                    ORDER BY intent_id, claim_fencing_token
                    """
                ).fetchall()
                previous_intent: str | None = None
                expected_fence = 0
                for claim in claim_sequences:
                    intent_id = str(claim[0])
                    if intent_id != previous_intent:
                        previous_intent = intent_id
                        expected_fence = 1
                    else:
                        expected_fence += 1
                    if int(claim[1]) != expected_fence:
                        raise GovernanceEventError("PUBLISH_CLAIM_ROOT_MISMATCH")
            if has_publish_resolution_events:
                latest_resolutions = connection.execute(
                    """
                    SELECT e.intent_id, e.claim_id, e.resolution_type, e.actual_ref,
                           e.intent_status, e.job_id, e.job_status, e.project_namespace,
                           e.project_id, e.proposal_id, e.proposal_status,
                           e.proposal_state_revision, e.applied_revision, e.error_code,
                           c.state, i.status, j.status, j.last_error_code,
                           p.status, p.state_revision, p.applied_revision,
                           g.state, g.active_intent_id
                    FROM governance_publish_resolution_events e
                    JOIN (
                        SELECT intent_id, MAX(resolution_sequence) AS sequence
                        FROM governance_publish_resolution_events GROUP BY intent_id
                    ) latest
                      ON latest.intent_id = e.intent_id
                     AND latest.sequence = e.resolution_sequence
                    JOIN governance_publish_claims c ON c.claim_id = e.claim_id
                    JOIN governance_publish_intents i ON i.intent_id = e.intent_id
                    JOIN governance_apply_jobs j ON j.job_id = e.job_id
                    JOIN governance_active_proposals p
                      ON p.project_namespace = e.project_namespace
                     AND p.project_id = e.project_id AND p.proposal_id = e.proposal_id
                    JOIN governance_project_publish_gates g
                      ON g.project_namespace = e.project_namespace
                     AND g.project_id = e.project_id
                    """
                ).fetchall()
                for resolution in latest_resolutions:
                    resolution_type = str(resolution[2])
                    terminal = resolution_type in {
                        "published",
                        "publish_conflict",
                        "failed",
                        "cancelled",
                    }
                    expected_gate = (
                        ("recovery_hold", str(resolution[0]))
                        if resolution_type == "recovery_hold"
                        else ("locked", str(resolution[0]))
                        if resolution_type == "retry_released"
                        else ("unlocked", None)
                    )
                    result = connection.execute(
                        """
                        SELECT outcome, actual_ref, error_code
                        FROM governance_publish_results WHERE intent_id = ?
                        """,
                        (resolution[0],),
                    ).fetchone()
                    expected_result_error = (
                        None if resolution_type in {"published", "cancelled"} else resolution[13]
                    )
                    if (
                        str(resolution[14]) != "released"
                        or str(resolution[15]) != str(resolution[4])
                        or str(resolution[16]) != str(resolution[6])
                        or resolution[17] != resolution[13]
                        or str(resolution[18]) != str(resolution[10])
                        or int(resolution[19]) != int(resolution[11])
                        or resolution[20] != resolution[12]
                        or (str(resolution[21]), resolution[22]) != expected_gate
                        or (
                            terminal
                            and (
                                result is None
                                or str(result[0]) != str(resolution[4])
                                or result[1] != resolution[3]
                                or result[2] != expected_result_error
                            )
                        )
                        or (not terminal and result is not None)
                    ):
                        raise GovernanceEventError("PUBLISH_RESOLUTION_ROOT_MISMATCH")
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
                if view.command_id not in decision_commands:
                    raise GovernanceEventError("AUDIT_SOURCE_MISMATCH")
                if view.previous_event_hash != previous_hash:
                    raise GovernanceEventError("AUDIT_HASH_CHAIN_INVALID")
                expected_hash = cls._audit_hash_from_view(view)
                if view.event_hash != expected_hash:
                    raise GovernanceEventError("AUDIT_HASH_CHAIN_INVALID")
                outbox_rows = connection.execute(
                    """
                    SELECT destination_ref, supersession_key, payload_digest,
                           payload_json, source_state_revision
                    FROM governance_outbox_events
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                      AND aggregate_sequence = ?
                    ORDER BY destination_ref
                    """,
                    (*cls._identity(ref), expected_sequence),
                ).fetchall()
                destination_refs = tuple(str(row[0]) for row in outbox_rows)
                expected_payload_digest, expected_state_revision, _, _ = decision_commands[
                    view.command_id
                ]
                if any(
                    str(outbox_row[2]) != expected_payload_digest
                    or cls._digest(str(outbox_row[3]).encode("utf-8")) != expected_payload_digest
                    or int(outbox_row[4]) != expected_state_revision
                    for outbox_row in outbox_rows
                ):
                    raise GovernanceEventError("AUDIT_OUTBOX_PAYLOAD_MISMATCH")
                manifest_digest = cls._destination_manifest_digest(
                    tuple(
                        OutboxDestination(
                            destination_ref=str(outbox_row[0]),
                            supersession_key=(
                                str(outbox_row[1]) if outbox_row[1] is not None else None
                            ),
                        )
                        for outbox_row in outbox_rows
                    )
                )
                if (
                    len(destination_refs) != view.destination_count
                    or manifest_digest != view.destination_manifest_digest
                ):
                    raise GovernanceEventError("AUDIT_OUTBOX_MISMATCH")
                previous_hash = view.event_hash
            if len(events) != int(aggregate[3]) or previous_hash != aggregate[4]:
                raise GovernanceEventError("AUDIT_SEQUENCE_MISMATCH")
        destination_rows = connection.execute(
            """
            SELECT destination_ref, next_sequence, delivered_sequence, operator_hold
            FROM governance_outbox_destinations
            """
        ).fetchall()
        for destination_ref, next_sequence, delivered_sequence, operator_hold in destination_rows:
            rows = connection.execute(
                """
                SELECT destination_sequence, payload_digest, payload_json, state,
                       source_state_revision
                FROM governance_outbox_events
                WHERE destination_ref = ? ORDER BY destination_sequence
                """,
                (destination_ref,),
            ).fetchall()
            sequences = tuple(int(row[0]) for row in rows)
            previous_source_revision = 0
            for row in rows:
                if cls._digest(str(row[2]).encode("utf-8")) != str(row[1]):
                    raise GovernanceEventError("OUTBOX_PAYLOAD_INTEGRITY_FAILURE")
                if int(row[4]) <= previous_source_revision:
                    raise GovernanceEventError("OUTBOX_SOURCE_REVISION_CONFLICT")
                previous_source_revision = int(row[4])
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
            for sequence, _digest, _payload, state, _source_revision in rows:
                event_id_row = connection.execute(
                    """
                    SELECT event_id FROM governance_outbox_events
                    WHERE destination_ref = ? AND destination_sequence = ?
                    """,
                    (destination_ref, sequence),
                ).fetchone()
                if event_id_row is None:
                    raise GovernanceEventError("OUTBOX_SEQUENCE_GAP")
                event_id = str(event_id_row[0])
                dead_letters = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM governance_outbox_dead_letters WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()[0]
                )
                holds = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM governance_operator_holds
                        WHERE source_event_id = ? AND scope_ref = ? AND resolved_at IS NULL
                        """,
                        (event_id, destination_ref),
                    ).fetchone()[0]
                )
                expected = (
                    1
                    if str(state)
                    in {
                        OutboxState.DEAD_LETTER.value,
                        OutboxState.RECOVERY_HOLD.value,
                    }
                    else 0
                )
                if dead_letters != expected or holds != expected:
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
        previous = connection.execute(
            """
            SELECT source_state_revision FROM governance_outbox_events
            WHERE destination_ref = ? ORDER BY destination_sequence DESC LIMIT 1
            """,
            (destination.destination_ref,),
        ).fetchone()
        if previous is not None and source_state_revision <= int(previous[0]):
            raise GovernanceEventError("OUTBOX_SOURCE_REVISION_CONFLICT")
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
            destination_manifest_digest=view.destination_manifest_digest,
            destination_count=view.destination_count,
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
    def _decision_destinations(
        ref: ProposalRef,
        authority: AuthorityContext,
    ) -> tuple[OutboxDestination, OutboxDestination]:
        channel_json = GovernanceEventService._canonical_json(
            authority.source.channel.model_dump(mode="json", exclude_none=True)
        )
        channel_digest = hashlib.sha256(channel_json.encode("utf-8")).hexdigest()
        return (
            OutboxDestination(
                destination_ref=(
                    f"yaml:{ref.project_ref.namespace}:"
                    f"{ref.project_ref.project_id}:{ref.proposal_id}"
                )
            ),
            OutboxDestination(
                destination_ref=(
                    f"provider:{authority.source.channel.provider.value}:{channel_digest}"
                ),
                supersession_key=f"proposal-card:{ref.proposal_id}",
            ),
        )

    @staticmethod
    def _destination_manifest_digest(destinations: Sequence[OutboxDestination]) -> str:
        manifest = sorted(
            (
                {
                    "destination_ref": destination.destination_ref,
                    "supersession_key": destination.supersession_key,
                }
                for destination in destinations
            ),
            key=lambda item: str(item["destination_ref"]),
        )
        return GovernanceEventService._digest(
            GovernanceEventService._canonical_json({"destinations": manifest}).encode("utf-8")
        )

    @staticmethod
    def _decision_command_id(idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"decision:sha256:{digest}"

    @staticmethod
    def _apply_command_id(idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"apply:sha256:{digest}"

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
                "destination_manifest_digest": row[16],
                "destination_count": row[17],
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

    @staticmethod
    def _optional_timestamp(value: datetime | None) -> str | None:
        return GovernanceEventService._timestamp(value) if value is not None else None


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
            expired = connection.execute(
                """
                SELECT event_id, attempts FROM governance_outbox_events
                WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (now_text,),
            ).fetchall()
            for event_id, attempts in expired:
                retry_at = GovernanceEventService._timestamp(
                    now + timedelta(seconds=self._backoff_seconds(int(attempts)))
                )
                connection.execute(
                    """
                    UPDATE governance_outbox_events
                    SET state = 'retry_wait', lease_owner = NULL,
                        lease_expires_at = NULL, retry_at = ?,
                        last_error_code = 'OUTBOX_LEASE_EXPIRED'
                    WHERE event_id = ? AND state = 'leased' AND lease_expires_at <= ?
                    """,
                    (retry_at, event_id, now_text),
                )
            self._move_exhausted(connection, now_text)
            lifecycle_gate = ""
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
                    "AND name = 'governance_legacy_migration_lifecycle_heads'"
                ).fetchone()
                is not None
            ):
                lifecycle_gate = """
                  AND NOT EXISTS (
                    SELECT 1
                    FROM governance_legacy_migration_items migration_item
                    LEFT JOIN governance_legacy_migration_lifecycle_heads lifecycle
                      ON lifecycle.migration_id = migration_item.migration_id
                    WHERE migration_item.project_namespace = e.project_namespace
                      AND migration_item.project_id = e.project_id
                      AND migration_item.proposal_id = e.proposal_id
                      AND (lifecycle.state IS NULL OR lifecycle.state != 'activated')
                  )
                """
            row = connection.execute(
                f"""
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
                  AND (
                    e.attempts < ?
                    OR (
                      e.attempts = ?
                      AND e.last_error_code = 'OUTBOX_LEASE_EXPIRED'
                      AND e.claim_generation = e.attempts
                    )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM governance_outbox_events prior
                    WHERE prior.destination_ref = e.destination_ref
                      AND prior.destination_sequence < e.destination_sequence
                      AND prior.state NOT IN ('delivered', 'superseded')
                  )
                  {lifecycle_gate}
                ORDER BY e.created_at, e.destination_ref, e.destination_sequence
                LIMIT 1
                """,
                (
                    destination_ref,
                    destination_ref,
                    now_text,
                    self.config.max_attempts,
                    self.config.max_attempts,
                ),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE governance_outbox_events
                SET state = 'leased',
                    attempts = attempts + CASE WHEN attempts < ? THEN 1 ELSE 0 END,
                    claim_generation = claim_generation + 1,
                    lease_owner = ?, lease_expires_at = ?, retry_at = NULL
                WHERE event_id = ? AND claim_generation = ?
                  AND state IN ('pending', 'retry_wait')
                  AND (
                    attempts < ?
                    OR (
                      attempts = ?
                      AND last_error_code = 'OUTBOX_LEASE_EXPIRED'
                      AND claim_generation = attempts
                    )
                  )
                """,
                (
                    self.config.max_attempts,
                    dispatcher_id,
                    lease_expires,
                    row[0],
                    row[1],
                    self.config.max_attempts,
                    self.config.max_attempts,
                ),
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
            updated = connection.execute(
                """
                UPDATE governance_outbox_events SET state = 'superseded'
                WHERE event_id = ? AND state = 'pending'
                  AND NOT EXISTS (
                    SELECT 1 FROM governance_outbox_events later
                    WHERE later.destination_ref = ?
                      AND later.supersession_key = ?
                      AND later.state = 'pending'
                      AND later.destination_sequence > ?
                  )
                """,
                (
                    event_id,
                    current.destination_ref,
                    current.supersession_key,
                    replacement.destination_sequence,
                ),
            )
            if updated.rowcount != 1:
                raise GovernanceEventError("OUTBOX_SUPERSEDE_INVALID")
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
                if (
                    event.last_error_code == "OUTBOX_LEASE_EXPIRED"
                    and event.attempts >= self.config.max_attempts
                ):
                    return self.fail(
                        event.event_id,
                        dispatcher_id=dispatcher_id,
                        generation=event.claim_generation,
                        error_code="OUTBOX_POST_SEND_RECONCILE_REQUIRED",
                        unreconcilable=True,
                    )
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
              AND NOT (
                last_error_code = 'OUTBOX_LEASE_EXPIRED'
                AND claim_generation = attempts
                AND attempts = ?
              )
            """,
            (self.config.max_attempts, now, self.config.max_attempts),
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
