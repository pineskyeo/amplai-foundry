"""Approved-snapshot ApplyGrant issuance and ApplyJob contracts."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    AuthorityService,
    DirectAuthorityRequest,
)
from amplai_foundry.governance.definitions import ProposalDefinitionManifest
from amplai_foundry.governance.events import (
    ApplyJobProjectionPayload,
    ApplyProjectionPayload,
    GovernanceEventService,
)
from amplai_foundry.governance.legacy_gates import legacy_mutation_block
from amplai_foundry.governance.models import (
    ActorRef,
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    ChannelRef,
    Digest,
    ProposalRef,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction


def _system_now() -> datetime:
    return datetime.now(UTC)


class ApplyGovernanceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ApplyGrantState(StrEnum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ApplyJobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    STAGED = "staged"
    PUBLISH_PENDING = "publish_pending"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"
    RECOVERY_HOLD = "recovery_hold"


class ApprovedSnapshotView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    proposal_ref: ProposalRef
    definition_digest: Digest
    snapshot_digest: Digest
    content_revision: int = Field(ge=1)
    state_revision: int = Field(ge=2)
    decision_epoch: int = Field(ge=1)
    expected_base_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    approved_at: AwareDatetime


class ApplyGrantView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    snapshot_id: str
    proposal_ref: ProposalRef
    approved_snapshot_digest: Digest
    content_revision: int = Field(ge=1)
    state_revision: int = Field(ge=2)
    decision_epoch: int = Field(ge=1)
    allowed_action: Literal["request_apply"]
    allowed_actor_ref: ActorRef
    bound_channel_ref: ChannelRef
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    state: ApplyGrantState
    resolved_at: AwareDatetime | None = None


class ApplyRequestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_ref: ProposalRef
    snapshot_id: str
    grant_id: str
    job_id: str
    proposal_status: Literal["apply_requested"]
    processed_at: AwareDatetime
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class IssuedApplyGrant:
    record: ApplyGrantView
    raw_grant: str

    def __repr__(self) -> str:
        return f"IssuedApplyGrant(record={self.record!r}, raw_grant=<redacted>)"


class DefinitionReader(Protocol):
    def get_definition_object(self, ref: ProposalRef, digest: str) -> bytes: ...


class ApplyGrantService:
    """Internal issuer rooted in an accepted approved Decision result."""

    def __init__(
        self,
        store: GovernanceStore,
        authority_service: AuthorityService,
        definitions: DefinitionReader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.authority_service = authority_service
        self.definitions = definitions
        self._clock = clock or _system_now

    def _issue_from_approved_decision(
        self,
        decision_result_key: str,
        *,
        authority_request: DirectAuthorityRequest,
        ttl: timedelta = timedelta(minutes=15),
    ) -> IssuedApplyGrant:
        """Issue only for a persisted approved Decision selected by trusted card code."""

        if not decision_result_key.strip():
            raise ValueError("decision_result_key는 비어 있을 수 없습니다.")
        if ttl <= timedelta(0):
            raise ValueError("Grant TTL은 0보다 커야 합니다.")
        with self.store.connect() as connection:
            source = self._approved_decision_row(connection, decision_result_key)
        if source is None:
            raise ApplyGovernanceError("APPROVED_DECISION_NOT_FOUND")
        ref = self._proposal_ref(source[0], source[1], source[2])
        definition_digest = str(source[3])
        try:
            manifest = ProposalDefinitionManifest.model_validate_json(
                self.definitions.get_definition_object(ref, definition_digest)
            )
        except ValueError as error:
            raise ApplyGovernanceError("APPROVED_SNAPSHOT_INVALID") from error
        if manifest.proposal_ref != ref or manifest.definition_digest != definition_digest:
            raise ApplyGovernanceError("APPROVED_SNAPSHOT_INVALID")

        issued_at = self._aware(self._clock())
        expires_at = issued_at + ttl
        raw_grant = secrets.token_hex(16)
        grant_id = self._identifier("AGR")
        snapshot_digest = self._snapshot_digest(
            ref,
            definition_digest=definition_digest,
            content_revision=int(str(source[4])),
            state_revision=int(str(source[5])),
            decision_epoch=int(str(source[6])),
            expected_base_revision=manifest.base_revision,
        )
        with self.store.connect() as connection, governance_transaction(connection):
            authority = self._authenticate(authority_request, connection)
            self._require_apply_authority(authority, ref)
            self._require_no_legacy_approval_hold(connection, ref)
            current_source = self._approved_decision_row(connection, decision_result_key)
            if current_source is None or tuple(current_source) != tuple(source):
                raise ApplyGovernanceError("APPROVED_SNAPSHOT_STALE")
            active = connection.execute(
                """
                SELECT active_definition_digest, content_revision, state_revision,
                       decision_epoch, status
                FROM governance_active_proposals
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                """,
                self._identity(ref),
            ).fetchone()
            expected_active = (
                definition_digest,
                int(str(source[4])),
                int(str(source[5])),
                int(str(source[6])),
                "approved",
            )
            if active is None or tuple(active) != expected_active:
                raise ApplyGovernanceError("APPROVED_SNAPSHOT_STALE")
            snapshot_id = self._identifier("APS")
            connection.execute(
                """
                INSERT INTO governance_approved_snapshots(
                    snapshot_id, project_namespace, project_id, proposal_id,
                    definition_digest, snapshot_digest, content_revision,
                    state_revision, decision_epoch, expected_base_revision, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    project_namespace, project_id, proposal_id,
                    definition_digest, content_revision, decision_epoch
                ) DO NOTHING
                """,
                (
                    snapshot_id,
                    *self._identity(ref),
                    definition_digest,
                    snapshot_digest,
                    int(str(source[4])),
                    int(str(source[5])),
                    int(str(source[6])),
                    manifest.base_revision,
                    str(source[7]),
                ),
            )
            snapshot = connection.execute(
                """
                SELECT snapshot_id, snapshot_digest, state_revision,
                       expected_base_revision
                FROM governance_approved_snapshots
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                  AND definition_digest = ? AND content_revision = ?
                  AND decision_epoch = ?
                """,
                (
                    *self._identity(ref),
                    definition_digest,
                    int(str(source[4])),
                    int(str(source[6])),
                ),
            ).fetchone()
            if snapshot is None or tuple(snapshot[1:]) != (
                snapshot_digest,
                int(str(source[5])),
                manifest.base_revision,
            ):
                raise ApplyGovernanceError("APPROVED_SNAPSHOT_CONFLICT")
            channel_json = self._channel_json(authority.source.channel)
            connection.execute(
                """
                UPDATE governance_apply_grants
                SET state = 'expired', resolved_at = ?
                WHERE snapshot_id = ? AND allowed_action = 'request_apply'
                  AND allowed_actor_id = ? AND bound_channel_json = ?
                  AND state = 'issued' AND expires_at <= ?
                """,
                (
                    self._timestamp(issued_at),
                    snapshot[0],
                    authority.actor_ref.actor_id,
                    channel_json,
                    self._timestamp(issued_at),
                ),
            )
            already_issued = connection.execute(
                """
                SELECT 1 FROM governance_apply_grants
                WHERE snapshot_id = ? AND allowed_action = 'request_apply'
                  AND allowed_actor_id = ? AND bound_channel_json = ?
                  AND state = 'issued'
                """,
                (
                    snapshot[0],
                    authority.actor_ref.actor_id,
                    channel_json,
                ),
            ).fetchone()
            if already_issued is not None:
                raise ApplyGovernanceError("APPLY_GRANT_ALREADY_ISSUED")
            connection.execute(
                """
                INSERT INTO governance_apply_grants(
                    grant_id, grant_hash, snapshot_id, project_namespace, project_id,
                    proposal_id, approved_snapshot_digest, content_revision,
                    state_revision, decision_epoch, allowed_actor_id,
                    allowed_action, allowed_actor_type, bound_channel_json, issued_at, expires_at,
                    state, resolved_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'request_apply',
                    'human', ?, ?, ?, 'issued', NULL
                )
                """,
                (
                    grant_id,
                    self._secret_hash(raw_grant),
                    snapshot[0],
                    *self._identity(ref),
                    snapshot_digest,
                    int(str(source[4])),
                    int(str(source[5])),
                    int(str(source[6])),
                    authority.actor_ref.actor_id,
                    channel_json,
                    self._timestamp(issued_at),
                    self._timestamp(expires_at),
                ),
            )
            return IssuedApplyGrant(
                record=self._grant_view(connection, grant_id),
                raw_grant=raw_grant,
            )

    def get_grant(self, grant_id: str) -> ApplyGrantView:
        with self.store.connect() as connection:
            return self._grant_view(connection, grant_id)

    def get_snapshot(self, snapshot_id: str) -> ApprovedSnapshotView:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT project_namespace, project_id, proposal_id,
                       definition_digest, snapshot_digest, content_revision,
                       state_revision, decision_epoch, expected_base_revision, approved_at
                FROM governance_approved_snapshots WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ApplyGovernanceError("APPROVED_SNAPSHOT_NOT_FOUND")
        return ApprovedSnapshotView.model_validate(
            {
                "snapshot_id": snapshot_id,
                "proposal_ref": self._proposal_ref(row[0], row[1], row[2]),
                "definition_digest": row[3],
                "snapshot_digest": row[4],
                "content_revision": row[5],
                "state_revision": row[6],
                "decision_epoch": row[7],
                "expected_base_revision": row[8],
                "approved_at": row[9],
            }
        )

    def _authenticate(
        self,
        request: DirectAuthorityRequest,
        connection: sqlite3.Connection,
    ) -> AuthorityContext:
        try:
            return self.authority_service.authenticate(request, connection=connection)
        except AuthorityResolutionError as error:
            raise ApplyGovernanceError(error.code) from error

    @staticmethod
    def _require_apply_authority(authority: AuthorityContext, ref: ProposalRef) -> None:
        if (
            authority.project_ref != ref.project_ref
            or authority.actor_ref.actor_type is not ActorType.HUMAN
            or AuthorityPermission.PROPOSAL_REQUEST_APPLY not in authority.permissions
        ):
            raise ApplyGovernanceError("AUTHORITY_DENIED")

    @classmethod
    def _require_no_legacy_approval_hold(
        cls,
        connection: sqlite3.Connection,
        ref: ProposalRef,
    ) -> None:
        block = legacy_mutation_block(connection, ref)
        if block is not None:
            raise ApplyGovernanceError(block)

    @staticmethod
    def _approved_decision_row(
        connection: sqlite3.Connection,
        decision_result_key: str,
    ) -> tuple[object, ...] | None:
        return cast(
            tuple[object, ...] | None,
            connection.execute(
                """
                SELECT project_namespace, project_id, proposal_id,
                       active_definition_digest, content_revision, state_revision,
                       decision_epoch, processed_at
                FROM governance_decision_results
                WHERE idempotency_key = ? AND proposal_status = 'approved'
                  AND action = 'approve'
                """,
                (decision_result_key,),
            ).fetchone(),
        )

    @staticmethod
    def _grant_view(connection: sqlite3.Connection, grant_id: str) -> ApplyGrantView:
        row = connection.execute(
            """
            SELECT snapshot_id, project_namespace, project_id, proposal_id,
                   approved_snapshot_digest, content_revision, state_revision,
                   decision_epoch, allowed_action, allowed_actor_id,
                   allowed_actor_type, bound_channel_json, issued_at, expires_at,
                   state, resolved_at
            FROM governance_apply_grants WHERE grant_id = ?
            """,
            (grant_id,),
        ).fetchone()
        if row is None:
            raise ApplyGovernanceError("APPLY_GRANT_NOT_FOUND")
        return ApplyGrantView.model_validate(
            {
                "grant_id": grant_id,
                "snapshot_id": row[0],
                "proposal_ref": ApplyGrantService._proposal_ref(row[1], row[2], row[3]),
                "approved_snapshot_digest": row[4],
                "content_revision": row[5],
                "state_revision": row[6],
                "decision_epoch": row[7],
                "allowed_action": row[8],
                "allowed_actor_ref": {"actor_id": row[9], "actor_type": row[10]},
                "bound_channel_ref": json.loads(str(row[11])),
                "issued_at": row[12],
                "expires_at": row[13],
                "state": row[14],
                "resolved_at": row[15],
            }
        )

    @staticmethod
    def _snapshot_digest(
        ref: ProposalRef,
        *,
        definition_digest: str,
        content_revision: int,
        state_revision: int,
        decision_epoch: int,
        expected_base_revision: str,
    ) -> str:
        payload = {
            "content_revision": content_revision,
            "decision_epoch": decision_epoch,
            "definition_digest": definition_digest,
            "expected_base_revision": expected_base_revision,
            "proposal_ref": ref.model_dump(mode="json"),
            "state_revision": state_revision,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    @staticmethod
    def _secret_hash(raw_grant: str) -> str:
        return f"sha256:{hashlib.sha256(raw_grant.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _channel_json(channel: ChannelRef) -> str:
        return json.dumps(
            channel.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

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


class ApplyRequestService:
    """Atomically consume an ApplyGrant and create one queued ApplyJob."""

    def __init__(
        self,
        store: GovernanceStore,
        authority_service: AuthorityService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.authority_service = authority_service
        self._clock = clock or _system_now

    def request_apply(
        self,
        ref: ProposalRef,
        *,
        authority_request: DirectAuthorityRequest,
        raw_grant: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> ApplyRequestResult:
        self._validate_replay_key(idempotency_key, request_fingerprint)
        with self.store.connect() as connection, governance_transaction(connection):
            replay = self._result_row(connection, idempotency_key)
            if replay is not None:
                return self._replay_result(
                    replay,
                    ref=ref,
                    request_fingerprint=request_fingerprint,
                )
            self._validate_grant_material(raw_grant, idempotency_key)
            authority = self._authenticate(authority_request, connection)
            self._require_apply_authority(authority, ref)
            ApplyGrantService._require_no_legacy_approval_hold(connection, ref)
            channel_json = ApplyGrantService._channel_json(authority.source.channel)
            if self._contains_persisted_secret(connection, idempotency_key):
                raise ApplyGovernanceError("IDEMPOTENCY_CONFLICT")

            processed_at = ApplyGrantService._aware(self._clock())
            grant_hash = ApplyGrantService._secret_hash(raw_grant)
            grant = connection.execute(
                """
                SELECT grant_id, snapshot_id, project_namespace, project_id, proposal_id,
                       approved_snapshot_digest, content_revision, state_revision,
                       decision_epoch, allowed_action, allowed_actor_id,
                       allowed_actor_type, bound_channel_json, expires_at, state
                FROM governance_apply_grants WHERE grant_hash = ?
                """,
                (grant_hash,),
            ).fetchone()
            if grant is None:
                raise ApplyGovernanceError("APPLY_GRANT_INVALID")
            if tuple(str(value) for value in grant[2:5]) != ApplyGrantService._identity(ref):
                raise ApplyGovernanceError("APPLY_GRANT_INVALID")
            if str(grant[14]) != ApplyGrantState.ISSUED.value:
                raise ApplyGovernanceError("APPLY_GRANT_CONSUMED")
            if processed_at >= self._parse_timestamp(str(grant[13])):
                raise ApplyGovernanceError("APPLY_GRANT_EXPIRED")
            if str(grant[9]) != "request_apply":
                raise ApplyGovernanceError("APPLY_GRANT_INVALID")
            if (str(grant[10]), str(grant[11])) != (
                authority.actor_ref.actor_id,
                authority.actor_ref.actor_type.value,
            ):
                raise ApplyGovernanceError("APPLY_GRANT_ACTOR_MISMATCH")
            if str(grant[12]) != channel_json:
                raise ApplyGovernanceError("APPLY_GRANT_CHANNEL_MISMATCH")

            snapshot = connection.execute(
                """
                SELECT definition_digest, snapshot_digest, content_revision,
                       state_revision, decision_epoch, expected_base_revision
                FROM governance_approved_snapshots
                WHERE snapshot_id = ? AND project_namespace = ? AND project_id = ?
                  AND proposal_id = ?
                """,
                (grant[1], *ApplyGrantService._identity(ref)),
            ).fetchone()
            proposal = connection.execute(
                """
                SELECT active_definition_digest, content_revision, state_revision,
                       decision_epoch, status
                FROM governance_active_proposals
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                """,
                ApplyGrantService._identity(ref),
            ).fetchone()
            expected_proposal = (
                (snapshot[0], snapshot[2], snapshot[3], snapshot[4], "approved")
                if snapshot is not None
                else None
            )
            if (
                snapshot is None
                or tuple(snapshot[1:5]) != tuple(grant[5:9])
                or proposal is None
                or tuple(proposal) != expected_proposal
            ):
                raise ApplyGovernanceError("APPROVED_SNAPSHOT_STALE")
            active_job = connection.execute(
                """
                SELECT 1 FROM governance_apply_jobs
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                  AND status IN (
                    'queued', 'leased', 'running', 'retry_wait', 'staged',
                    'publish_pending', 'recovery_hold'
                  )
                """,
                ApplyGrantService._identity(ref),
            ).fetchone()
            if active_job is not None:
                raise ApplyGovernanceError("APPLY_ALREADY_RUNNING")

            timestamp = ApplyGrantService._timestamp(processed_at)
            updated = connection.execute(
                """
                UPDATE governance_active_proposals
                SET status = 'apply_requested', state_revision = state_revision + 1,
                    updated_at = ?
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                  AND status = 'approved' AND active_definition_digest = ?
                  AND content_revision = ? AND state_revision = ? AND decision_epoch = ?
                """,
                (timestamp, *ApplyGrantService._identity(ref), *proposal[:4]),
            )
            if updated.rowcount != 1:
                raise ApplyGovernanceError("APPROVED_SNAPSHOT_STALE")
            consumed = connection.execute(
                """
                UPDATE governance_apply_grants
                SET state = 'consumed', resolved_at = ?
                WHERE grant_id = ? AND state = 'issued'
                """,
                (timestamp, grant[0]),
            )
            if consumed.rowcount != 1:
                raise ApplyGovernanceError("APPLY_GRANT_CONSUMED")
            job_id = ApplyGrantService._identifier("JOB")
            connection.execute(
                """
                INSERT INTO governance_apply_jobs(
                    job_id, snapshot_id, project_namespace, project_id, proposal_id,
                    approved_snapshot_digest, expected_base_revision, status,
                    attempts, fencing_token, lease_owner, lease_expires_at, retry_at,
                    staged_artifact_digest, publish_request_digest, last_error_code,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0,
                    NULL, NULL, NULL, NULL, NULL, NULL, ?, ?
                )
                """,
                (
                    job_id,
                    grant[1],
                    *ApplyGrantService._identity(ref),
                    grant[5],
                    snapshot[5],
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO governance_apply_request_results(
                    idempotency_key, request_fingerprint, project_namespace, project_id,
                    proposal_id, snapshot_id, grant_id, job_id, actor_id, actor_type,
                    channel_json, proposal_status, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'apply_requested', ?)
                """,
                (
                    idempotency_key,
                    request_fingerprint,
                    *ApplyGrantService._identity(ref),
                    grant[1],
                    grant[0],
                    job_id,
                    authority.actor_ref.actor_id,
                    authority.actor_ref.actor_type.value,
                    channel_json,
                    timestamp,
                ),
            )
            next_state_revision = int(snapshot[3]) + 1
            events = GovernanceEventService(self.store, clock=self._clock)
            events._append_apply_request_in_transaction(
                connection,
                ref,
                apply_result_key=idempotency_key,
                authority=authority,
                payload=ApplyProjectionPayload(
                    action="request_apply",
                    active_definition_digest=str(snapshot[0]),
                    aggregate_ref=ref,
                    approved_snapshot_digest=str(snapshot[1]),
                    content_revision=int(snapshot[2]),
                    decision_epoch=int(snapshot[4]),
                    expected_base_revision=str(snapshot[5]),
                    job_id=job_id,
                    proposal_status="apply_requested",
                    snapshot_id=str(grant[1]),
                    state_revision=next_state_revision,
                ),
            )
            return ApplyRequestResult(
                proposal_ref=ref,
                snapshot_id=str(grant[1]),
                grant_id=str(grant[0]),
                job_id=job_id,
                proposal_status="apply_requested",
                processed_at=processed_at,
            )

    def _authenticate(
        self, request: DirectAuthorityRequest, connection: sqlite3.Connection
    ) -> AuthorityContext:
        try:
            return self.authority_service.authenticate(request, connection=connection)
        except AuthorityResolutionError as error:
            raise ApplyGovernanceError(error.code) from error

    @staticmethod
    def _require_apply_authority(authority: AuthorityContext, ref: ProposalRef) -> None:
        ApplyGrantService._require_apply_authority(authority, ref)

    @staticmethod
    def _result_row(connection: sqlite3.Connection, key: str) -> tuple[object, ...] | None:
        return cast(
            tuple[object, ...] | None,
            connection.execute(
                """
                SELECT request_fingerprint, project_namespace, project_id, proposal_id,
                       snapshot_id, grant_id, job_id, actor_id, actor_type, channel_json,
                       proposal_status, processed_at
                FROM governance_apply_request_results WHERE idempotency_key = ?
                """,
                (key,),
            ).fetchone(),
        )

    @staticmethod
    def _replay_result(
        row: tuple[object, ...],
        *,
        ref: ProposalRef,
        request_fingerprint: str,
    ) -> ApplyRequestResult:
        original_ref = ApplyGrantService._proposal_ref(row[1], row[2], row[3])
        if str(row[0]) != request_fingerprint or original_ref != ref:
            raise ApplyGovernanceError("IDEMPOTENCY_CONFLICT")
        return ApplyRequestResult(
            proposal_ref=original_ref,
            snapshot_id=str(row[4]),
            grant_id=str(row[5]),
            job_id=str(row[6]),
            proposal_status="apply_requested",
            processed_at=ApplyRequestService._parse_timestamp(str(row[11])),
            replayed=True,
        )

    @staticmethod
    def _validate_replay_key(key: str, fingerprint: str) -> None:
        if not key.strip():
            raise ValueError("idempotency_key는 비어 있을 수 없습니다.")
        if len(key) > 512:
            raise ValueError("idempotency_key는 512자를 초과할 수 없습니다.")
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("request_fingerprint는 lowercase SHA-256 hex여야 합니다.")

    @staticmethod
    def _validate_grant_material(raw_grant: str, key: str) -> None:
        if not raw_grant or len(raw_grant.encode("utf-8")) > 64:
            raise ApplyGovernanceError("APPLY_GRANT_INVALID")
        if raw_grant in key:
            raise ApplyGovernanceError("IDEMPOTENCY_CONFLICT")

    @staticmethod
    def _contains_persisted_secret(connection: sqlite3.Connection, value: str) -> bool:
        token_length = 32
        candidates = {
            value[index : index + token_length]
            for index in range(max(len(value) - token_length + 1, 0))
            if all(c in "0123456789abcdef" for c in value[index : index + token_length])
        }
        if not candidates:
            return False
        hashes = tuple(ApplyGrantService._secret_hash(candidate) for candidate in candidates)
        placeholders = ",".join("?" for _ in hashes)
        return (
            connection.execute(
                f"""
            SELECT 1 FROM (
                SELECT token_hash AS secret_hash FROM governance_action_tokens
                UNION ALL SELECT grant_hash AS secret_hash FROM governance_apply_grants
            ) WHERE secret_hash IN ({placeholders}) LIMIT 1
            """,
                hashes,
            ).fetchone()
            is not None
        )

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ApplyJobView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    snapshot_id: str
    proposal_ref: ProposalRef
    approved_snapshot_digest: Digest
    expected_base_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    status: ApplyJobState
    attempts: int = Field(ge=0)
    fencing_token: int = Field(ge=0)
    lease_owner: str | None = None
    lease_expires_at: AwareDatetime | None = None
    retry_at: AwareDatetime | None = None
    staged_artifact_digest: Digest | None = None
    publish_request_digest: Digest | None = None
    last_error_code: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ApplyJobService:
    """Lease-bound ApplyJob runtime with monotonic fencing and staging-only writes."""

    MAX_ROOT_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        store: GovernanceStore,
        *,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts는 1 이상이어야 합니다.")
        self.store = store
        self._clock = clock or _system_now
        self._max_attempts = max_attempts

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_ttl: timedelta = timedelta(minutes=5),
    ) -> ApplyJobView | None:
        self._validate_worker(worker_id)
        if lease_ttl <= timedelta(0):
            raise ValueError("lease TTL은 0보다 커야 합니다.")
        with self.store.connect() as connection, governance_transaction(connection):
            now = ApplyGrantService._aware(self._clock())
            timestamp = ApplyGrantService._timestamp(now)
            lease_expires = ApplyGrantService._timestamp(now + lease_ttl)
            row = connection.execute(
                """
                SELECT job_id, status, fencing_token, lease_expires_at, attempts
                FROM governance_apply_jobs
                WHERE status = 'queued'
                   OR (status = 'retry_wait' AND retry_at <= ?)
                   OR (status IN ('leased', 'running') AND lease_expires_at <= ?)
                ORDER BY CASE WHEN attempts < ? THEN 0 ELSE 1 END,
                         created_at, job_id
                LIMIT 1
                """,
                (timestamp, timestamp, self._max_attempts),
            ).fetchone()
            if row is None:
                return None
            if int(row[4]) >= self._max_attempts:
                dead_lettered = connection.execute(
                    """
                    UPDATE governance_apply_jobs
                    SET status = 'dead_letter', lease_owner = NULL,
                        lease_expires_at = NULL, retry_at = NULL,
                        last_error_code = 'APPLY_ATTEMPTS_EXHAUSTED', updated_at = ?
                    WHERE job_id = ? AND status = ? AND attempts = ?
                      AND fencing_token = ?
                      AND (lease_expires_at IS ? OR lease_expires_at = ?)
                    """,
                    (
                        timestamp,
                        row[0],
                        row[1],
                        row[4],
                        row[2],
                        row[3],
                        row[3],
                    ),
                )
                if dead_lettered.rowcount != 1:
                    raise ApplyGovernanceError("APPLY_JOB_CLAIM_CONFLICT")
                view = self._job_view(connection, str(row[0]))
                self._record_event(
                    connection,
                    view,
                    event_type="dead_lettered",
                    worker_id="system-recovery",
                    before_status=str(row[1]),
                    created_at=timestamp,
                )
                return None
            updated = connection.execute(
                """
                UPDATE governance_apply_jobs
                SET status = 'leased', attempts = attempts + 1,
                    fencing_token = fencing_token + 1, lease_owner = ?,
                    lease_expires_at = ?, retry_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = ? AND fencing_token = ?
                  AND (lease_expires_at IS ? OR lease_expires_at = ?)
                """,
                (
                    worker_id,
                    lease_expires,
                    timestamp,
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[3],
                ),
            )
            if updated.rowcount != 1:
                raise ApplyGovernanceError("APPLY_JOB_CLAIM_CONFLICT")
            view = self._job_view(connection, str(row[0]))
            self._record_event(
                connection,
                view,
                event_type="claimed",
                worker_id=worker_id,
                before_status=str(row[1]),
                created_at=timestamp,
            )
            return view

    def start(self, job_id: str, *, worker_id: str, fencing_token: int) -> ApplyJobView:
        return self._lease_update(
            job_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            expected_status=ApplyJobState.LEASED,
            next_status=ApplyJobState.RUNNING,
        )

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        lease_ttl: timedelta = timedelta(minutes=5),
    ) -> ApplyJobView:
        self._validate_worker(worker_id)
        if lease_ttl <= timedelta(0):
            raise ValueError("lease TTL은 0보다 커야 합니다.")
        with self.store.connect() as connection, governance_transaction(connection):
            now = ApplyGrantService._aware(self._clock())
            timestamp = ApplyGrantService._timestamp(now)
            expires = ApplyGrantService._timestamp(now + lease_ttl)
            updated = connection.execute(
                """
                UPDATE governance_apply_jobs
                SET lease_expires_at = CASE
                        WHEN lease_expires_at > ? THEN lease_expires_at ELSE ? END,
                    updated_at = ?
                WHERE job_id = ? AND status IN ('leased', 'running')
                  AND lease_owner = ? AND fencing_token = ? AND lease_expires_at > ?
                """,
                (expires, expires, timestamp, job_id, worker_id, fencing_token, timestamp),
            )
            if updated.rowcount != 1:
                raise ApplyGovernanceError("APPLY_JOB_FENCE_STALE")
            view = self._job_view(connection, job_id)
            self._record_event(
                connection,
                view,
                event_type="heartbeat",
                worker_id=worker_id,
                before_status=view.status.value,
                created_at=timestamp,
            )
            return view

    def stage_for_publish(
        self,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        artifact_bytes: bytes,
        publish_request_bytes: bytes,
    ) -> ApplyJobView:
        if not artifact_bytes or not publish_request_bytes:
            raise ValueError("staging artifact와 publish input은 비어 있을 수 없습니다.")
        if (
            len(artifact_bytes) > self.MAX_ROOT_BYTES
            or len(publish_request_bytes) > self.MAX_ROOT_BYTES
        ):
            raise ValueError("staging artifact와 publish input은 4 MiB를 초과할 수 없습니다.")
        artifact_digest = self._content_digest(artifact_bytes)
        publish_request_digest = self._content_digest(publish_request_bytes)
        with self.store.connect() as connection, governance_transaction(connection):
            now = ApplyGrantService._aware(self._clock())
            timestamp = ApplyGrantService._timestamp(now)
            connection.execute(
                """
                INSERT INTO governance_staging_artifacts(
                    job_id, fencing_token, artifact_digest, artifact_bytes, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, fencing_token, artifact_digest, artifact_bytes, timestamp),
            )
            connection.execute(
                """
                INSERT INTO governance_publish_inputs(
                    job_id, fencing_token, publish_request_digest,
                    publish_request_bytes, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    fencing_token,
                    publish_request_digest,
                    publish_request_bytes,
                    timestamp,
                ),
            )
            updated = connection.execute(
                """
                UPDATE governance_apply_jobs
                SET status = 'publish_pending', staged_artifact_digest = ?,
                    publish_request_digest = ?,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                  AND fencing_token = ? AND lease_expires_at > ?
                """,
                (
                    artifact_digest,
                    publish_request_digest,
                    timestamp,
                    job_id,
                    worker_id,
                    fencing_token,
                    timestamp,
                ),
            )
            if updated.rowcount != 1:
                raise ApplyGovernanceError("APPLY_JOB_FENCE_STALE")
            view = self._job_view(connection, job_id)
            self._record_event(
                connection,
                view,
                event_type="publish_prepared",
                worker_id=worker_id,
                before_status="running",
                created_at=timestamp,
            )
            return view

    def schedule_retry(
        self,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        retry_delay: timedelta,
        error_code: str,
    ) -> ApplyJobView:
        if retry_delay <= timedelta(0):
            raise ValueError("retry delay는 0보다 커야 합니다.")
        if not error_code.strip() or len(error_code) > 128:
            raise ValueError("error_code가 유효하지 않습니다.")
        with self.store.connect() as connection, governance_transaction(connection):
            now = ApplyGrantService._aware(self._clock())
            timestamp = ApplyGrantService._timestamp(now)
            retry_at = ApplyGrantService._timestamp(now + retry_delay)
            before = connection.execute(
                "SELECT status, attempts FROM governance_apply_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            exhausted = before is not None and int(before[1]) >= self._max_attempts
            next_status = "dead_letter" if exhausted else "retry_wait"
            next_retry_at = None if exhausted else retry_at
            updated = connection.execute(
                """
                UPDATE governance_apply_jobs
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    retry_at = ?, last_error_code = ?, updated_at = ?
                WHERE job_id = ? AND status IN ('leased', 'running')
                  AND lease_owner = ? AND fencing_token = ? AND lease_expires_at > ?
                """,
                (
                    next_status,
                    next_retry_at,
                    error_code,
                    timestamp,
                    job_id,
                    worker_id,
                    fencing_token,
                    timestamp,
                ),
            )
            if updated.rowcount != 1:
                raise ApplyGovernanceError("APPLY_JOB_FENCE_STALE")
            view = self._job_view(connection, job_id)
            self._record_event(
                connection,
                view,
                event_type="dead_lettered" if exhausted else "retry_scheduled",
                worker_id=worker_id,
                before_status=str(before[0]) if before is not None else "missing",
                created_at=timestamp,
            )
            return view

    def get_job(self, job_id: str) -> ApplyJobView:
        with self.store.connect() as connection:
            return self._job_view(connection, job_id)

    def _lease_update(
        self,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        expected_status: ApplyJobState,
        next_status: ApplyJobState,
    ) -> ApplyJobView:
        self._validate_worker(worker_id)
        with self.store.connect() as connection, governance_transaction(connection):
            timestamp = ApplyGrantService._timestamp(ApplyGrantService._aware(self._clock()))
            updated = connection.execute(
                """
                UPDATE governance_apply_jobs SET status = ?, updated_at = ?
                WHERE job_id = ? AND status = ? AND lease_owner = ?
                  AND fencing_token = ? AND lease_expires_at > ?
                """,
                (
                    next_status.value,
                    timestamp,
                    job_id,
                    expected_status.value,
                    worker_id,
                    fencing_token,
                    timestamp,
                ),
            )
            if updated.rowcount != 1:
                raise ApplyGovernanceError("APPLY_JOB_FENCE_STALE")
            view = self._job_view(connection, job_id)
            self._record_event(
                connection,
                view,
                event_type="started",
                worker_id=worker_id,
                before_status=expected_status.value,
                created_at=timestamp,
            )
            return view

    def _record_event(
        self,
        connection: sqlite3.Connection,
        view: ApplyJobView,
        *,
        event_type: str,
        worker_id: str,
        before_status: str,
        created_at: str,
    ) -> None:
        sequence_row = connection.execute(
            "SELECT COALESCE(MAX(event_sequence), 0) + 1 "
            "FROM governance_apply_job_events WHERE job_id = ?",
            (view.job_id,),
        ).fetchone()
        event_sequence = int(sequence_row[0])
        payload = ApplyJobProjectionPayload(
            aggregate_ref=view.proposal_ref,
            attempts=view.attempts,
            event_sequence=event_sequence,
            event_type=event_type,
            fencing_token=view.fencing_token,
            job_id=view.job_id,
            last_error_code=view.last_error_code,
            lease_expires_at=view.lease_expires_at,
            lease_owner=view.lease_owner,
            publish_request_digest=view.publish_request_digest,
            retry_at=view.retry_at,
            staged_artifact_digest=view.staged_artifact_digest,
            status=view.status.value,
            worker_id=worker_id,
        )
        payload_json = GovernanceEventService._canonical_json(payload.model_dump(mode="json"))
        payload_digest = GovernanceEventService._digest(payload_json.encode("utf-8"))
        job_event_id = ApplyGrantService._identifier("JEV")
        command_id = f"apply-job:{job_event_id}"
        connection.execute(
            """
            INSERT INTO governance_apply_job_events(
                job_event_id, command_id, job_id, event_sequence, snapshot_id, project_namespace,
                project_id, proposal_id, event_type, worker_id, before_status,
                status, attempts, fencing_token, lease_owner, lease_expires_at, retry_at,
                staged_artifact_digest, publish_request_digest, last_error_code,
                payload_digest, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_event_id,
                command_id,
                view.job_id,
                event_sequence,
                view.snapshot_id,
                *ApplyGrantService._identity(view.proposal_ref),
                event_type,
                worker_id,
                before_status,
                view.status.value,
                view.attempts,
                view.fencing_token,
                view.lease_owner,
                (
                    ApplyGrantService._timestamp(view.lease_expires_at)
                    if view.lease_expires_at is not None
                    else None
                ),
                (
                    ApplyGrantService._timestamp(view.retry_at)
                    if view.retry_at is not None
                    else None
                ),
                view.staged_artifact_digest,
                view.publish_request_digest,
                view.last_error_code,
                payload_digest,
                payload_json,
                created_at,
            ),
        )
        GovernanceEventService(self.store, clock=self._clock)._append_apply_job_in_transaction(
            connection,
            view.proposal_ref,
            job_event_id=job_event_id,
            payload=payload,
        )

    @staticmethod
    def _job_view(connection: sqlite3.Connection, job_id: str) -> ApplyJobView:
        row = connection.execute(
            "SELECT * FROM governance_apply_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ApplyGovernanceError("APPLY_JOB_NOT_FOUND")
        return ApplyJobView.model_validate(
            {
                "job_id": row[0],
                "snapshot_id": row[1],
                "proposal_ref": ApplyGrantService._proposal_ref(row[2], row[3], row[4]),
                "approved_snapshot_digest": row[5],
                "expected_base_revision": row[6],
                "status": row[7],
                "attempts": row[8],
                "fencing_token": row[9],
                "lease_owner": row[10],
                "lease_expires_at": row[11],
                "retry_at": row[12],
                "staged_artifact_digest": row[13],
                "publish_request_digest": row[14],
                "last_error_code": row[15],
                "created_at": row[16],
                "updated_at": row[17],
            }
        )

    @staticmethod
    def _validate_worker(worker_id: str) -> None:
        if not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("worker_id가 유효하지 않습니다.")

    @staticmethod
    def _validate_digest(value: str) -> None:
        if (
            len(value) != 71
            or not value.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in value[7:])
        ):
            raise ValueError("digest는 canonical SHA-256이어야 합니다.")

    @staticmethod
    def _content_digest(value: bytes) -> str:
        return f"sha256:{hashlib.sha256(value).hexdigest()}"
