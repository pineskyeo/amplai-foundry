"""Durable fenced-publish contracts for canonical Git publication."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Protocol, cast

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.legacy_gates import legacy_mutation_block
from amplai_foundry.governance.models import Digest, ProposalRef
from amplai_foundry.governance.store import GovernanceStore, governance_transaction

GitObjectId = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


def _validate_canonical_branch_ref(value: str) -> str:
    forbidden = frozenset("~^:?*[\\")
    suffix = value.removeprefix("refs/heads/")
    components = suffix.split("/")
    if (
        not suffix
        or any(
            ord(character) <= 0x20 or ord(character) == 0x7F or character in forbidden
            for character in value
        )
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(("/", "."))
        or any(component.startswith(".") or component.endswith(".lock") for component in components)
    ):
        raise ValueError("canonical_ref는 안전한 fully qualified branch ref여야 합니다.")
    return value


CanonicalBranchRef = Annotated[
    str,
    StringConstraints(
        min_length=12,
        max_length=255,
        pattern=r"^refs/heads/.+$",
    ),
    AfterValidator(_validate_canonical_branch_ref),
]
_CANONICAL_REF_ADAPTER = TypeAdapter(CanonicalBranchRef)
_GIT_OBJECT_ID_ADAPTER = TypeAdapter(GitObjectId)


class PublishGovernanceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PublishIntentState(StrEnum):
    PREPARED = "prepared"
    PUBLISHED = "published"
    PUBLISH_CONFLICT = "publish_conflict"
    CANCELLED = "cancelled"
    RECOVERY_HOLD = "recovery_hold"
    FAILED = "failed"


class PublishOutcome(StrEnum):
    PUBLISHED = "published"
    PUBLISH_CONFLICT = "publish_conflict"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PublishGateState(StrEnum):
    UNLOCKED = "unlocked"
    LOCKED = "locked"
    RECOVERY_HOLD = "recovery_hold"


class PublishIntentView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(pattern=r"^PBI-[A-F0-9]{16}$")
    job_id: str
    snapshot_id: str
    proposal_ref: ProposalRef
    fencing_token: int = Field(ge=1)
    approved_snapshot_digest: Digest
    expected_base_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    staged_artifact_digest: Digest
    publish_request_digest: Digest
    canonical_ref: CanonicalBranchRef
    expected_old_ref: GitObjectId
    candidate_commit: GitObjectId
    candidate_tree_digest: Digest
    status: PublishIntentState
    prepared_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    last_error_code: str | None = None


class ProjectPublishGateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_ref: ProjectRef
    canonical_ref: CanonicalBranchRef
    active_intent_id: str | None = Field(default=None, pattern=r"^PBI-[A-F0-9]{16}$")
    gate_revision: int = Field(ge=1)
    state: PublishGateState
    updated_at: AwareDatetime


class PublishResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(pattern=r"^PBI-[A-F0-9]{16}$")
    job_id: str
    snapshot_id: str
    proposal_ref: ProposalRef
    fencing_token: int = Field(ge=1)
    expected_old_ref: GitObjectId
    candidate_commit: GitObjectId
    actual_ref: GitObjectId
    outcome: PublishOutcome
    error_code: str | None = None
    resolved_at: AwareDatetime


class CandidateCommitEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_commit: GitObjectId
    parent_commit: GitObjectId
    candidate_tree_digest: Digest
    canonical_ref: CanonicalBranchRef


class PublishGitInspector(Protocol):
    """Read-only Git boundary used before a durable publish intent exists."""

    def read_ref(self, canonical_ref: str) -> str: ...

    def inspect_candidate(
        self,
        candidate_commit: str,
        *,
        artifact_bytes: bytes,
        publish_request_bytes: bytes,
    ) -> CandidateCommitEvidence: ...


class PublishPreparationService:
    """Atomically root a verified candidate and lock its Project publish gate."""

    def __init__(
        self,
        store: GovernanceStore,
        git: PublishGitInspector,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.git = git
        self._clock = clock or (lambda: datetime.now(UTC))

    def prepare(
        self,
        job_id: str,
        *,
        fencing_token: int,
        canonical_ref: str,
        candidate_commit: str,
    ) -> PublishIntentView:
        if not job_id.strip() or fencing_token < 1:
            raise ValueError("job_id와 fencing_token이 유효하지 않습니다.")
        try:
            checked_ref = _CANONICAL_REF_ADAPTER.validate_python(canonical_ref)
            checked_candidate = _GIT_OBJECT_ID_ADAPTER.validate_python(candidate_commit)
        except ValidationError as error:
            raise ValueError("canonical ref 또는 candidate commit이 유효하지 않습니다.") from error

        with self.store.connect() as connection:
            root = self._preparation_root(connection, job_id)
            self._require_legacy_ready(connection, root)
        self._verify_root(root, fencing_token=fencing_token)
        artifact_bytes = cast(bytes, root[10])
        publish_request_bytes = cast(bytes, root[12])
        evidence = self.git.inspect_candidate(
            checked_candidate,
            artifact_bytes=artifact_bytes,
            publish_request_bytes=publish_request_bytes,
        )
        current_ref = _GIT_OBJECT_ID_ADAPTER.validate_python(self.git.read_ref(checked_ref))
        if evidence.candidate_commit != checked_candidate:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_MISMATCH")
        if evidence.canonical_ref != checked_ref:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_MISMATCH")
        if len(current_ref) != len(checked_candidate) or current_ref == checked_candidate:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_INVALID")
        if not current_ref.startswith(str(root[6])):
            raise PublishGovernanceError("PUBLISH_BASE_REVISION_CONFLICT")
        if evidence.parent_commit != current_ref:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_BASE_MISMATCH")

        with self.store.connect() as connection, governance_transaction(connection):
            current = self._preparation_root(connection, job_id)
            self._require_legacy_ready(connection, current)
            self._verify_root(current, fencing_token=fencing_token)
            if current != root:
                raise PublishGovernanceError("PUBLISH_PREPARATION_STALE")
            project_namespace = str(current[1])
            project_id = str(current[2])
            gate = connection.execute(
                """
                SELECT canonical_ref, active_intent_id, gate_revision, state
                FROM governance_project_publish_gates
                WHERE project_namespace = ? AND project_id = ?
                """,
                (project_namespace, project_id),
            ).fetchone()
            if gate is not None and (
                str(gate[0]) != checked_ref
                or gate[1] is not None
                or str(gate[3]) != PublishGateState.UNLOCKED.value
            ):
                raise PublishGovernanceError("PUBLISH_GATE_LOCKED")
            if (
                connection.execute(
                    """
                SELECT 1 FROM governance_publish_intents
                WHERE project_namespace = ? AND project_id = ?
                  AND status IN ('prepared', 'recovery_hold')
                LIMIT 1
                """,
                    (project_namespace, project_id),
                ).fetchone()
                is not None
            ):
                raise PublishGovernanceError("PUBLISH_GATE_LOCKED")

            timestamp = self._timestamp(self._clock())
            intent_id = f"PBI-{secrets.token_hex(8).upper()}"
            connection.execute(
                """
                INSERT INTO governance_publish_intents(
                    intent_id, job_id, snapshot_id, project_namespace, project_id,
                    proposal_id, fencing_token, approved_snapshot_digest,
                    expected_base_revision, staged_artifact_digest, publish_request_digest,
                    canonical_ref, expected_old_ref, candidate_commit,
                    candidate_tree_digest, status, prepared_at, resolved_at,
                    last_error_code
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'prepared', ?, NULL, NULL
                )
                """,
                (
                    intent_id,
                    job_id,
                    *current[:6],
                    current[6],
                    current[8],
                    current[11],
                    checked_ref,
                    current_ref,
                    checked_candidate,
                    evidence.candidate_tree_digest,
                    timestamp,
                ),
            )
            if gate is None:
                connection.execute(
                    """
                    INSERT INTO governance_project_publish_gates(
                        project_namespace, project_id, canonical_ref, active_intent_id,
                        gate_revision, state, updated_at
                    ) VALUES (?, ?, ?, ?, 1, 'locked', ?)
                    """,
                    (project_namespace, project_id, checked_ref, intent_id, timestamp),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE governance_project_publish_gates
                    SET active_intent_id = ?, gate_revision = gate_revision + 1,
                        state = 'locked', updated_at = ?
                    WHERE project_namespace = ? AND project_id = ?
                      AND state = 'unlocked' AND active_intent_id IS NULL
                      AND gate_revision = ? AND canonical_ref = ?
                    """,
                    (
                        intent_id,
                        timestamp,
                        project_namespace,
                        project_id,
                        gate[2],
                        checked_ref,
                    ),
                )
                if updated.rowcount != 1:
                    raise PublishGovernanceError("PUBLISH_GATE_LOCKED")
            return self._intent_view(connection, intent_id)

    @staticmethod
    def _require_legacy_ready(
        connection: sqlite3.Connection,
        root: tuple[object, ...],
    ) -> None:
        ref = ProposalRef(
            project_ref=ProjectRef(project_id=str(root[2]), namespace=str(root[1])),
            proposal_id=str(root[3]),
        )
        block = legacy_mutation_block(connection, ref)
        if block is not None:
            raise PublishGovernanceError(block)

    @staticmethod
    def _preparation_root(connection: sqlite3.Connection, job_id: str) -> tuple[object, ...]:
        row = connection.execute(
            """
            SELECT j.snapshot_id, j.project_namespace, j.project_id, j.proposal_id,
                   j.fencing_token, j.approved_snapshot_digest,
                   j.expected_base_revision, j.status, j.staged_artifact_digest,
                   a.artifact_digest, a.artifact_bytes,
                   p.publish_request_digest, p.publish_request_bytes,
                   s.snapshot_digest, s.expected_base_revision, ap.status
            FROM governance_apply_jobs j
            JOIN governance_staging_artifacts a
              ON a.job_id = j.job_id AND a.fencing_token = j.fencing_token
            JOIN governance_publish_inputs p
              ON p.job_id = j.job_id AND p.fencing_token = j.fencing_token
            JOIN governance_approved_snapshots s
              ON s.snapshot_id = j.snapshot_id
             AND s.project_namespace = j.project_namespace
             AND s.project_id = j.project_id AND s.proposal_id = j.proposal_id
            JOIN governance_active_proposals ap
              ON ap.project_namespace = j.project_namespace
             AND ap.project_id = j.project_id AND ap.proposal_id = j.proposal_id
            WHERE j.job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise PublishGovernanceError("PUBLISH_JOB_NOT_FOUND")
        return cast(tuple[object, ...], row)

    @staticmethod
    def _verify_root(root: tuple[object, ...], *, fencing_token: int) -> None:
        if (
            cast(int, root[4]) != fencing_token
            or str(root[7]) != "publish_pending"
            or str(root[15]) != "apply_requested"
            or str(root[5]) != str(root[13])
            or str(root[6]) != str(root[14])
            or str(root[8]) != str(root[9])
            or str(root[8]) != PublishPreparationService._digest(cast(bytes, root[10]))
            or str(root[11]) != PublishPreparationService._digest(cast(bytes, root[12]))
        ):
            raise PublishGovernanceError("PUBLISH_PREPARATION_ROOT_MISMATCH")

    @staticmethod
    def _intent_view(connection: sqlite3.Connection, intent_id: str) -> PublishIntentView:
        row = connection.execute(
            "SELECT * FROM governance_publish_intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise PublishGovernanceError("PUBLISH_INTENT_NOT_FOUND")
        return PublishIntentView.model_validate(
            {
                "intent_id": row[0],
                "job_id": row[1],
                "snapshot_id": row[2],
                "proposal_ref": ProposalRef(
                    project_ref=ProjectRef(namespace=row[3], project_id=row[4]),
                    proposal_id=row[5],
                ),
                "fencing_token": row[6],
                "approved_snapshot_digest": row[7],
                "expected_base_revision": row[8],
                "staged_artifact_digest": row[9],
                "publish_request_digest": row[10],
                "canonical_ref": row[11],
                "expected_old_ref": row[12],
                "candidate_commit": row[13],
                "candidate_tree_digest": row[14],
                "status": row[15],
                "prepared_at": row[16],
                "resolved_at": row[17],
                "last_error_code": row[18],
            }
        )

    @staticmethod
    def _digest(value: bytes) -> str:
        return f"sha256:{hashlib.sha256(value).hexdigest()}"

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock은 timezone-aware datetime을 반환해야 합니다.")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
