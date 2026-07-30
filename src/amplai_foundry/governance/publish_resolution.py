"""Authoritative database finalization and recovery for fenced Git publication."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from amplai_foundry.governance.events import (
    GovernanceEventService,
    PublishResolutionProjectionPayload,
)
from amplai_foundry.governance.git_publish import (
    FencedGitPublishCoordinator,
    GitPublishAmbiguousError,
)
from amplai_foundry.governance.publish import (
    PublishGitInspector,
    PublishGovernanceError,
    PublishIntentState,
    PublishIntentView,
    PublishPreparationService,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction


class PublishResolutionType(StrEnum):
    PUBLISHED = "published"
    PUBLISH_CONFLICT = "publish_conflict"
    RECOVERY_HOLD = "recovery_hold"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRY_RELEASED = "retry_released"


class PublishResolutionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution_event_id: str = Field(pattern=r"^PRE-[A-F0-9]{16}$")
    intent_id: str = Field(pattern=r"^PBI-[A-F0-9]{16}$")
    resolution_sequence: int = Field(ge=1)
    resolution_type: PublishResolutionType
    actual_ref: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    intent_status: str
    job_status: str
    proposal_status: str
    proposal_state_revision: int = Field(ge=3)
    applied_revision: str | None = Field(
        default=None,
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    )
    error_code: str | None = None
    created_at: AwareDatetime


class PublishResolutionService:
    """Resolve a durable publish claim from the currently observed canonical ref."""

    def __init__(
        self,
        store: GovernanceStore,
        git: PublishGitInspector,
        *,
        coordinator_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not coordinator_id.strip() or len(coordinator_id) > 128:
            raise ValueError("coordinator_id가 유효하지 않습니다.")
        self.store = store
        self.git = git
        self.coordinator_id = coordinator_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def recover(self, intent_id: str) -> PublishResolutionView:
        existing = self._existing_terminal_resolution(intent_id)
        if existing is not None:
            return existing
        _intent, claim_id = self._ensure_claim(intent_id)
        return self._commit_resolution(
            intent_id,
            claim_id=claim_id,
            resolution_type=PublishResolutionType.RETRY_RELEASED,
            error_code=None,
        )

    def fail_if_unchanged(self, intent_id: str, *, error_code: str) -> PublishResolutionView:
        self._validate_error_code(error_code)
        existing = self._existing_terminal_resolution(intent_id)
        if existing is not None:
            return existing
        _intent, claim_id = self._ensure_claim(intent_id)
        return self._commit_resolution(
            intent_id,
            claim_id=claim_id,
            resolution_type=PublishResolutionType.FAILED,
            error_code=error_code,
        )

    def cancel_if_unchanged(self, intent_id: str) -> PublishResolutionView:
        existing = self._existing_terminal_resolution(intent_id)
        if existing is not None:
            return existing
        _intent, claim_id = self._ensure_claim(intent_id)
        return self._commit_resolution(
            intent_id,
            claim_id=claim_id,
            resolution_type=PublishResolutionType.CANCELLED,
            error_code="PUBLISH_CANCELLED",
        )

    def _ensure_claim(self, intent_id: str) -> tuple[PublishIntentView, str]:
        with self.store.connect() as connection, governance_transaction(connection):
            intent = PublishPreparationService._intent_view(connection, intent_id)
            root = self._root(connection, intent_id)
            if (
                intent.status not in {PublishIntentState.PREPARED, PublishIntentState.RECOVERY_HOLD}
                or root is None
                or str(root[11]) not in {"publish_pending", "recovery_hold"}
                or str(root[13]) != "apply_requested"
                or str(root[16])
                != ("locked" if intent.status is PublishIntentState.PREPARED else "recovery_hold")
                or str(root[17]) != intent_id
            ):
                raise PublishGovernanceError("PUBLISH_INTENT_NOT_RECOVERABLE")
            if root[18] is not None:
                return intent, str(root[18])
            sequence = connection.execute(
                """
                SELECT COALESCE(MAX(claim_fencing_token), 0) + 1
                FROM governance_publish_claims WHERE intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            claim_id = f"PCL-{secrets.token_hex(8).upper()}"
            connection.execute(
                """
                INSERT INTO governance_publish_claims(
                    claim_id, intent_id, project_namespace, project_id,
                    coordinator_id, claim_fencing_token, state, claimed_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL)
                """,
                (
                    claim_id,
                    intent_id,
                    intent.proposal_ref.project_ref.namespace,
                    intent.proposal_ref.project_ref.project_id,
                    self.coordinator_id,
                    int(str(sequence[0])),
                    self._timestamp(self._clock()),
                ),
            )
            return intent, claim_id

    def _commit_resolution(
        self,
        intent_id: str,
        *,
        claim_id: str,
        resolution_type: PublishResolutionType,
        error_code: str | None,
    ) -> PublishResolutionView:
        created_at = self._clock()
        timestamp = self._timestamp(created_at)
        with self.store.connect() as connection, governance_transaction(connection):
            intent = PublishPreparationService._intent_view(connection, intent_id)
            root = self._root(connection, intent_id)
            if root is None or str(root[18]) != claim_id or str(root[19]) != "active":
                raise PublishGovernanceError("PUBLISH_CLAIM_STALE")
            try:
                actual_ref: str | None = self.git.read_ref(intent.canonical_ref)
            except PublishGovernanceError as error:
                actual_ref = None
                resolution_type = PublishResolutionType.RECOVERY_HOLD
                error_code = error.code
            else:
                if actual_ref == intent.candidate_commit:
                    resolution_type = PublishResolutionType.PUBLISHED
                    error_code = None
                elif actual_ref != intent.expected_old_ref:
                    resolution_type = PublishResolutionType.PUBLISH_CONFLICT
                    error_code = "PUBLISH_BASE_REF_CONFLICT"
            if actual_ref is not None and len(actual_ref) != len(intent.candidate_commit):
                raise PublishGovernanceError("PUBLISH_GIT_REF_INVALID")
            current_proposal_status = str(root[13])
            current_proposal_revision = int(str(root[14]))
            before_job_status = str(root[11])
            intent_status, job_status, proposal_status, applied_revision = self._targets(
                resolution_type,
                candidate=intent.candidate_commit,
            )
            proposal_revision = current_proposal_revision

            released = connection.execute(
                """
                UPDATE governance_publish_claims
                SET state = 'released', resolved_at = ?
                WHERE claim_id = ? AND state = 'active'
                """,
                (timestamp, claim_id),
            )
            if released.rowcount != 1:
                raise PublishGovernanceError("PUBLISH_CLAIM_STALE")

            intent_error = (
                None
                if resolution_type
                in {PublishResolutionType.PUBLISHED, PublishResolutionType.CANCELLED}
                else error_code
            )
            self._transition_intent(
                connection,
                intent_id,
                current_status=intent.status,
                next_status=intent_status,
                error_code=intent_error,
                timestamp=timestamp,
            )
            if resolution_type in {
                PublishResolutionType.PUBLISHED,
                PublishResolutionType.PUBLISH_CONFLICT,
                PublishResolutionType.FAILED,
                PublishResolutionType.CANCELLED,
            }:
                if actual_ref is None:
                    raise PublishGovernanceError("PUBLISH_GIT_REF_INVALID")
                self._insert_result(
                    connection,
                    intent_id,
                    actual_ref=actual_ref,
                    outcome=intent_status,
                    error_code=intent_error,
                    resolved_at=timestamp,
                )

            updated_job = connection.execute(
                """
                UPDATE governance_apply_jobs
                SET status = ?, last_error_code = ?, updated_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, retry_at = NULL
                WHERE job_id = ? AND status IN ('publish_pending', 'recovery_hold')
                """,
                (job_status, error_code, timestamp, intent.job_id),
            )
            if updated_job.rowcount != 1:
                raise PublishGovernanceError("PUBLISH_JOB_STALE")
            if proposal_status != current_proposal_status:
                proposal_revision += 1
                updated = connection.execute(
                    """
                    UPDATE governance_active_proposals
                    SET status = ?, state_revision = state_revision + 1,
                        applied_revision = ?, updated_at = ?
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                      AND status = ? AND state_revision = ?
                    """,
                    (
                        proposal_status,
                        applied_revision,
                        timestamp,
                        intent.proposal_ref.project_ref.namespace,
                        intent.proposal_ref.project_ref.project_id,
                        intent.proposal_ref.proposal_id,
                        current_proposal_status,
                        current_proposal_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise PublishGovernanceError("PUBLISH_PROPOSAL_STALE")

            gate_state = (
                "recovery_hold"
                if resolution_type is PublishResolutionType.RECOVERY_HOLD
                else "locked"
                if resolution_type is PublishResolutionType.RETRY_RELEASED
                else "unlocked"
            )
            active_intent_id = intent_id if gate_state != "unlocked" else None
            updated_gate = connection.execute(
                """
                UPDATE governance_project_publish_gates
                SET state = ?, active_intent_id = ?, gate_revision = gate_revision + 1,
                    updated_at = ?
                WHERE project_namespace = ? AND project_id = ? AND active_intent_id = ?
                """,
                (
                    gate_state,
                    active_intent_id,
                    timestamp,
                    intent.proposal_ref.project_ref.namespace,
                    intent.proposal_ref.project_ref.project_id,
                    intent_id,
                ),
            )
            if updated_gate.rowcount != 1:
                raise PublishGovernanceError("PUBLISH_GATE_STALE")

            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(resolution_sequence), 0) + 1
                    FROM governance_publish_resolution_events WHERE intent_id = ?
                    """,
                    (intent_id,),
                ).fetchone()[0]
            )
            job_event_sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(event_sequence), 0)
                    FROM governance_apply_job_events WHERE job_id = ?
                    """,
                    (intent.job_id,),
                ).fetchone()[0]
            )
            stream_revision = job_event_sequence + sequence
            payload = PublishResolutionProjectionPayload(
                aggregate_ref=intent.proposal_ref,
                applied_revision=applied_revision,
                actual_ref=actual_ref,
                before_job_status=before_job_status,
                error_code=error_code,
                intent_id=intent_id,
                intent_status=intent_status,
                job_id=intent.job_id,
                job_status=job_status,
                proposal_state_revision=proposal_revision,
                proposal_status=proposal_status,
                resolution_sequence=sequence,
                resolution_type=resolution_type.value,
                stream_revision=stream_revision,
            )
            payload_json = GovernanceEventService._canonical_json(payload.model_dump(mode="json"))
            payload_digest = GovernanceEventService._digest(payload_json.encode("utf-8"))
            resolution_event_id = f"PRE-{secrets.token_hex(8).upper()}"
            command_id = f"publish-resolution:{resolution_event_id}"
            connection.execute(
                """
                INSERT INTO governance_publish_resolution_events(
                    resolution_event_id, command_id, intent_id, resolution_sequence,
                    claim_id, resolver_id, project_namespace, project_id, proposal_id, job_id,
                    resolution_type, actual_ref, intent_status, job_status,
                    proposal_status, proposal_state_revision, applied_revision,
                    error_code, payload_digest, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution_event_id,
                    command_id,
                    intent_id,
                    sequence,
                    claim_id,
                    self.coordinator_id,
                    intent.proposal_ref.project_ref.namespace,
                    intent.proposal_ref.project_ref.project_id,
                    intent.proposal_ref.proposal_id,
                    intent.job_id,
                    resolution_type.value,
                    actual_ref,
                    intent_status,
                    job_status,
                    proposal_status,
                    proposal_revision,
                    applied_revision,
                    error_code,
                    payload_digest,
                    payload_json,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO governance_publish_resolution_roots(
                    resolution_event_id, intent_id, claim_id, job_id,
                    project_namespace, project_id, proposal_id,
                    before_job_status, stream_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution_event_id,
                    intent_id,
                    claim_id,
                    intent.job_id,
                    intent.proposal_ref.project_ref.namespace,
                    intent.proposal_ref.project_ref.project_id,
                    intent.proposal_ref.proposal_id,
                    before_job_status,
                    stream_revision,
                ),
            )
            GovernanceEventService(
                self.store,
                clock=self._clock,
            )._append_publish_resolution_in_transaction(
                connection,
                intent.proposal_ref,
                resolution_event_id=resolution_event_id,
                coordinator_id=self.coordinator_id,
                payload=payload,
            )
            return PublishResolutionView(
                resolution_event_id=resolution_event_id,
                intent_id=intent_id,
                resolution_sequence=sequence,
                resolution_type=resolution_type,
                actual_ref=actual_ref,
                intent_status=intent_status,
                job_status=job_status,
                proposal_status=proposal_status,
                proposal_state_revision=proposal_revision,
                applied_revision=applied_revision,
                error_code=error_code,
                created_at=created_at,
            )

    @staticmethod
    def _targets(
        resolution_type: PublishResolutionType,
        *,
        candidate: str,
    ) -> tuple[str, str, str, str | None]:
        return {
            PublishResolutionType.PUBLISHED: ("published", "succeeded", "applied", candidate),
            PublishResolutionType.PUBLISH_CONFLICT: (
                "publish_conflict",
                "recovery_hold",
                "apply_requested",
                None,
            ),
            PublishResolutionType.RECOVERY_HOLD: (
                "recovery_hold",
                "recovery_hold",
                "apply_requested",
                None,
            ),
            PublishResolutionType.FAILED: ("failed", "dead_letter", "apply_failed", None),
            PublishResolutionType.CANCELLED: (
                "cancelled",
                "dead_letter",
                "apply_failed",
                None,
            ),
            PublishResolutionType.RETRY_RELEASED: (
                "prepared",
                "publish_pending",
                "apply_requested",
                None,
            ),
        }[resolution_type]

    @staticmethod
    def _transition_intent(
        connection: sqlite3.Connection,
        intent_id: str,
        *,
        current_status: PublishIntentState,
        next_status: str,
        error_code: str | None,
        timestamp: str,
    ) -> None:
        if current_status.value == next_status and next_status != "recovery_hold":
            return
        resolved_at = (
            timestamp
            if next_status in {"published", "publish_conflict", "cancelled", "failed"}
            else None
        )
        updated = connection.execute(
            """
            UPDATE governance_publish_intents
            SET status = ?, resolved_at = ?, last_error_code = ?
            WHERE intent_id = ? AND status = ?
            """,
            (next_status, resolved_at, error_code, intent_id, current_status.value),
        )
        if updated.rowcount != 1:
            raise PublishGovernanceError("PUBLISH_INTENT_STALE")

    def _existing_terminal_resolution(self, intent_id: str) -> PublishResolutionView | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT resolution_event_id, intent_id, resolution_sequence,
                       resolution_type, actual_ref, intent_status, job_status,
                       proposal_status, proposal_state_revision, applied_revision,
                       error_code, created_at
                FROM governance_publish_resolution_events
                WHERE intent_id = ? AND resolution_type IN (
                    'published', 'publish_conflict', 'failed', 'cancelled'
                )
                """,
                (intent_id,),
            ).fetchone()
        if row is None:
            return None
        return PublishResolutionView(
            resolution_event_id=str(row[0]),
            intent_id=str(row[1]),
            resolution_sequence=int(row[2]),
            resolution_type=PublishResolutionType(str(row[3])),
            actual_ref=cast(str | None, row[4]),
            intent_status=str(row[5]),
            job_status=str(row[6]),
            proposal_status=str(row[7]),
            proposal_state_revision=int(row[8]),
            applied_revision=cast(str | None, row[9]),
            error_code=cast(str | None, row[10]),
            created_at=datetime.fromisoformat(str(row[11]).replace("Z", "+00:00")),
        )

    @staticmethod
    def _insert_result(
        connection: sqlite3.Connection,
        intent_id: str,
        *,
        actual_ref: str,
        outcome: str,
        error_code: str | None,
        resolved_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO governance_publish_results(
                intent_id, job_id, snapshot_id, project_namespace, project_id,
                proposal_id, fencing_token, expected_old_ref, candidate_commit,
                actual_ref, outcome, error_code, resolved_at
            )
            SELECT intent_id, job_id, snapshot_id, project_namespace, project_id,
                   proposal_id, fencing_token, expected_old_ref, candidate_commit,
                   ?, ?, ?, ?
            FROM governance_publish_intents WHERE intent_id = ?
            """,
            (actual_ref, outcome, error_code, resolved_at, intent_id),
        )

    @staticmethod
    def _root(connection: sqlite3.Connection, intent_id: str) -> tuple[object, ...] | None:
        row = connection.execute(
            """
            SELECT i.intent_id, i.job_id, i.snapshot_id, i.project_namespace,
                   i.project_id, i.proposal_id, i.fencing_token, i.expected_old_ref,
                   i.candidate_commit, i.canonical_ref, i.status,
                   j.status, j.last_error_code, p.status, p.state_revision,
                   p.applied_revision, g.state, g.active_intent_id,
                   c.claim_id, c.state
            FROM governance_publish_intents i
            JOIN governance_apply_jobs j ON j.job_id = i.job_id
            JOIN governance_active_proposals p
              ON p.project_namespace = i.project_namespace
             AND p.project_id = i.project_id AND p.proposal_id = i.proposal_id
            JOIN governance_project_publish_gates g
              ON g.project_namespace = i.project_namespace AND g.project_id = i.project_id
            LEFT JOIN governance_publish_claims c
              ON c.intent_id = i.intent_id AND c.state = 'active'
            WHERE i.intent_id = ?
            """,
            (intent_id,),
        ).fetchone()
        return cast(tuple[object, ...] | None, row)

    @staticmethod
    def _validate_error_code(error_code: str) -> None:
        if not error_code.strip() or len(error_code) > 128:
            raise ValueError("error_code가 유효하지 않습니다.")

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock은 timezone-aware datetime을 반환해야 합니다.")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class FencedGitPublishWorkflow:
    """Public publish boundary that always converges Git evidence into DB truth."""

    def __init__(
        self,
        coordinator: FencedGitPublishCoordinator,
        resolution: PublishResolutionService,
    ) -> None:
        self.coordinator = coordinator
        self.resolution = resolution

    def publish(self, intent_id: str) -> PublishResolutionView:
        existing = self.resolution._existing_terminal_resolution(intent_id)
        if existing is not None:
            return existing
        try:
            self.coordinator.publish_prepared_ref(intent_id)
        except GitPublishAmbiguousError:
            pass
        except PublishGovernanceError as error:
            if error.code != "PUBLISH_GIT_COMMAND_FAILED":
                raise
        return self.resolution.recover(intent_id)
