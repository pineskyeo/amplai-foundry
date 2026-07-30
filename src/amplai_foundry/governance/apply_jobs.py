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
from typing import Protocol, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    AuthorityService,
    DirectAuthorityRequest,
)
from amplai_foundry.governance.definitions import ProposalDefinitionManifest
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
    allowed_actor_ref: ActorRef
    bound_channel_ref: ChannelRef
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    state: ApplyGrantState
    resolved_at: AwareDatetime | None = None


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
                    self._timestamp(issued_at),
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
            connection.execute(
                """
                INSERT INTO governance_apply_grants(
                    grant_id, grant_hash, snapshot_id, project_namespace, project_id,
                    proposal_id, approved_snapshot_digest, content_revision,
                    state_revision, decision_epoch, allowed_actor_id,
                    allowed_actor_type, bound_channel_json, issued_at, expires_at,
                    state, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'human', ?, ?, ?, 'issued', NULL)
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
                    self._channel_json(authority.source.channel),
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
                   decision_epoch, allowed_actor_id, allowed_actor_type,
                   bound_channel_json, issued_at, expires_at, state, resolved_at
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
                "allowed_actor_ref": {"actor_id": row[8], "actor_type": row[9]},
                "bound_channel_ref": json.loads(str(row[10])),
                "issued_at": row[11],
                "expires_at": row[12],
                "state": row[13],
                "resolved_at": row[14],
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
