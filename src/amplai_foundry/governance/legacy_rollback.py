"""Exact-root planning for pre-activation legacy migration rollback."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    AuthorityService,
    DirectAuthorityRequest,
)
from amplai_foundry.governance.events import GovernanceEventError, GovernanceEventService
from amplai_foundry.governance.legacy_lifecycle import (
    LegacyMigrationActivationService,
    LegacyMigrationLifecycleError,
    LegacyMigrationLifecycleState,
)
from amplai_foundry.governance.legacy_migration import (
    LegacyProposalImportService,
    LegacyTargetStatus,
)
from amplai_foundry.governance.models import (
    ActorRef,
    ActorType,
    AuthorityPermission,
    Digest,
    ProposalRef,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction


def _system_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode()).hexdigest()}"


class LegacyRollbackRoot(BaseModel):
    """One authoritative Proposal root that rollback may remove."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_ref: ProposalRef
    definition_digest: Digest
    content_revision: int = Field(ge=1)
    state_revision: int = Field(ge=1)
    decision_epoch: int = Field(ge=1)
    audit_event_id: str
    aggregate_sequence: int = Field(ge=1)
    event_hash: Digest
    outbox_event_id: str
    destination_ref: str
    destination_sequence: int = Field(ge=1)


class LegacyMigrationRollbackPlan(BaseModel):
    """Authenticated, read-only proof of the exact pre-activation rollback set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    migration_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    project_ref: ProjectRef
    expected_lifecycle_revision: int = Field(ge=1)
    verification_id: str = Field(pattern=r"^MVF-[A-F0-9]{16}$")
    report_digest: Digest
    roots: tuple[LegacyRollbackRoot, ...] = Field(min_length=1)
    rollback_root_digest: Digest
    planned_by: ActorRef
    planned_at: AwareDatetime

    @model_validator(mode="after")
    def validate_rollback_root_digest(self) -> LegacyMigrationRollbackPlan:
        preimage = {
            "expected_lifecycle_revision": self.expected_lifecycle_revision,
            "migration_id": self.migration_id,
            "project_ref": self.project_ref.model_dump(mode="json"),
            "report_digest": self.report_digest,
            "roots": [root.model_dump(mode="json") for root in self.roots],
            "verification_id": self.verification_id,
        }
        if self.rollback_root_digest != _digest(preimage):
            raise ValueError("legacy rollback root digest가 일치하지 않습니다.")
        return self


class LegacyMigrationRollbackPlanner:
    """Prove rollback eligibility and freeze the exact removable DB root set."""

    def __init__(
        self,
        store: GovernanceStore,
        authority: AuthorityService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.authority = authority
        self._clock = clock or _system_now

    def plan(
        self,
        migration_id: str,
        *,
        authority_request: DirectAuthorityRequest,
        expected_lifecycle_revision: int,
    ) -> LegacyMigrationRollbackPlan:
        if expected_lifecycle_revision < 1:
            raise ValueError("expected_lifecycle_revision은 1 이상이어야 합니다.")
        try:
            with self.store.connect() as connection, governance_transaction(connection):
                context = self.authority.authenticate(authority_request, connection=connection)
                if (
                    context.project_ref != authority_request.project_ref
                    or context.actor_ref.actor_type is not ActorType.HUMAN
                    or AuthorityPermission.ACTIVATION_MANAGE not in context.permissions
                ):
                    raise LegacyMigrationLifecycleError("AUTHORITY_DENIED")
                GovernanceEventService.reconcile_connection(connection)
                LegacyProposalImportService.reconcile_verification_roots(connection)
                LegacyMigrationActivationService.reconcile_roots(connection)
                head = connection.execute(
                    """
                    SELECT project_namespace, project_id, verification_id, report_digest,
                           state, lifecycle_revision
                    FROM governance_legacy_migration_lifecycle_heads
                    WHERE migration_id = ?
                    """,
                    (migration_id,),
                ).fetchone()
                if head is None:
                    raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_NOT_IMPORTED")
                if (str(head[0]), str(head[1])) != (
                    context.project_ref.namespace,
                    context.project_ref.project_id,
                ):
                    raise LegacyMigrationLifecycleError("PROJECT_SCOPE_MISMATCH")
                state = LegacyMigrationLifecycleState(str(head[4]))
                self._require_rollback_state(
                    state,
                    actual_revision=int(head[5]),
                    expected_revision=expected_lifecycle_revision,
                )
                roots = self._exact_roots(
                    connection,
                    migration_id,
                    context.project_ref,
                )
                planned_at = self._aware(self._clock())
                preimage = {
                    "expected_lifecycle_revision": expected_lifecycle_revision,
                    "migration_id": migration_id,
                    "project_ref": context.project_ref.model_dump(mode="json"),
                    "report_digest": str(head[3]),
                    "roots": [root.model_dump(mode="json") for root in roots],
                    "verification_id": str(head[2]),
                }
                return LegacyMigrationRollbackPlan(
                    migration_id=migration_id,
                    project_ref=context.project_ref,
                    expected_lifecycle_revision=expected_lifecycle_revision,
                    verification_id=str(head[2]),
                    report_digest=str(head[3]),
                    roots=roots,
                    rollback_root_digest=_digest(preimage),
                    planned_by=context.actor_ref,
                    planned_at=planned_at,
                )
        except LegacyMigrationLifecycleError:
            raise
        except AuthorityResolutionError as error:
            raise LegacyMigrationLifecycleError(error.code) from error
        except (GovernanceEventError, sqlite3.Error, ValueError) as error:
            raise LegacyMigrationLifecycleError(
                "LEGACY_MIGRATION_ROLLBACK_ROOT_MISMATCH"
            ) from error

    @staticmethod
    def _require_rollback_state(
        state: LegacyMigrationLifecycleState,
        *,
        actual_revision: int,
        expected_revision: int,
    ) -> None:
        if state is LegacyMigrationLifecycleState.IMPORTED:
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_NOT_VERIFIED")
        if state is LegacyMigrationLifecycleState.ACTIVATED:
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_AFTER_ACTIVATION")
        if state is LegacyMigrationLifecycleState.ROLLED_BACK:
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ALREADY_ROLLED_BACK")
        if state is LegacyMigrationLifecycleState.RECOVERY_HOLD:
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_RECOVERY_REQUIRED")
        if actual_revision != expected_revision:
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_LIFECYCLE_CONFLICT")

    @staticmethod
    def _exact_roots(
        connection: sqlite3.Connection,
        migration_id: str,
        project_ref: ProjectRef,
    ) -> tuple[LegacyRollbackRoot, ...]:
        rows = connection.execute(
            """
            SELECT i.proposal_id, i.definition_digest, i.content_revision,
                   i.state_revision, i.decision_epoch,
                   a.active_definition_digest, a.content_revision, a.state_revision,
                   a.decision_epoch, a.status,
                   d.definition_digest, d.previous_definition_digest,
                   d.activated_from_status,
                   c.command_id, e.event_id, e.aggregate_sequence, e.event_hash,
                   o.event_id, o.destination_ref, o.destination_sequence, o.state,
                   o.attempts, o.claim_generation,
                   s.aggregate_sequence, s.last_event_hash,
                   x.next_sequence, x.delivered_sequence, x.operator_hold,
                   i.target_status
            FROM governance_legacy_migration_items i
            JOIN governance_active_proposals a
              ON a.project_namespace = i.project_namespace
             AND a.project_id = i.project_id AND a.proposal_id = i.proposal_id
            JOIN governance_definition_revisions d
              ON d.project_namespace = i.project_namespace
             AND d.project_id = i.project_id AND d.proposal_id = i.proposal_id
             AND d.content_revision = i.content_revision
            JOIN governance_legacy_import_commands c
              ON c.migration_id = i.migration_id
             AND c.project_namespace = i.project_namespace
             AND c.project_id = i.project_id AND c.proposal_id = i.proposal_id
            JOIN governance_audit_events e ON e.command_id = c.command_id
            JOIN governance_outbox_events o
              ON o.project_namespace = e.project_namespace
             AND o.project_id = e.project_id AND o.proposal_id = e.proposal_id
             AND o.aggregate_sequence = e.aggregate_sequence
            JOIN governance_aggregate_sequences s
              ON s.project_namespace = e.project_namespace
             AND s.project_id = e.project_id AND s.proposal_id = e.proposal_id
            JOIN governance_outbox_destinations x ON x.destination_ref = o.destination_ref
            WHERE i.migration_id = ? AND i.project_namespace = ? AND i.project_id = ?
            ORDER BY i.proposal_id
            """,
            (migration_id, project_ref.namespace, project_ref.project_id),
        ).fetchall()
        proposal_count = connection.execute(
            """
            SELECT proposal_count FROM governance_legacy_migrations
            WHERE migration_id = ? AND project_namespace = ? AND project_id = ?
            """,
            (migration_id, project_ref.namespace, project_ref.project_id),
        ).fetchone()
        if proposal_count is None or len(rows) != int(proposal_count[0]):
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_ROOT_MISMATCH")
        roots: list[LegacyRollbackRoot] = []
        for row in rows:
            proposal_id = str(row[0])
            runtime_status = LegacyProposalImportService._runtime_status(
                LegacyTargetStatus(str(row[28]))
            )
            expected_active = (str(row[1]), int(row[2]), int(row[3]), int(row[4]), runtime_status)
            if (
                tuple(row[5:10]) != expected_active
                or str(row[10]) != str(row[1])
                or row[11] is not None
                or row[12] is not None
                or str(row[20]) != "pending"
                or int(row[21]) != 0
                or int(row[22]) != 0
                or int(row[23]) != int(row[15])
                or str(row[24]) != str(row[16])
                or int(row[25]) != int(row[19]) + 1
                or int(row[26]) != 0
                or int(row[27]) != 0
                or LegacyMigrationRollbackPlanner._proposal_root_counts(
                    connection,
                    project_ref,
                    proposal_id,
                )
                != (1, 1, 1, 0)
            ):
                raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_ROOT_MISMATCH")
            roots.append(
                LegacyRollbackRoot(
                    proposal_ref=ProposalRef(
                        project_ref=project_ref,
                        proposal_id=proposal_id,
                    ),
                    definition_digest=str(row[1]),
                    content_revision=int(row[2]),
                    state_revision=int(row[3]),
                    decision_epoch=int(row[4]),
                    audit_event_id=str(row[14]),
                    aggregate_sequence=int(row[15]),
                    event_hash=str(row[16]),
                    outbox_event_id=str(row[17]),
                    destination_ref=str(row[18]),
                    destination_sequence=int(row[19]),
                )
            )
        return tuple(roots)

    @staticmethod
    def _proposal_root_counts(
        connection: sqlite3.Connection,
        project_ref: ProjectRef,
        proposal_id: str,
    ) -> tuple[int, int, int, int]:
        identity = (project_ref.namespace, project_ref.project_id, proposal_id)
        return (
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM governance_definition_revisions
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?""",
                    identity,
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM governance_audit_events
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?""",
                    identity,
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM governance_outbox_events
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?""",
                    identity,
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM governance_legacy_approval_reviews
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?""",
                    identity,
                ).fetchone()[0]
            ),
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock은 timezone-aware datetime을 반환해야 합니다.")
        return value
