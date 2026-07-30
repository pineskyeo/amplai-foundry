"""Exact-root planning for pre-activation legacy migration rollback."""

from __future__ import annotations

import hashlib
import json
import secrets
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
    LegacyMigrationRollbackResult,
)
from amplai_foundry.governance.legacy_migration import (
    LegacyApprovalDisposition,
    LegacyProposalImportService,
    LegacyTargetStatus,
)
from amplai_foundry.governance.models import (
    ActorRef,
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    Digest,
    ProposalRef,
)
from amplai_foundry.governance.store import (
    GovernanceCommitAmbiguousError,
    GovernanceStore,
    governance_transaction,
)


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
    destination_existed_before: bool
    previous_destination_next_sequence: int | None = Field(default=None, ge=1)
    previous_destination_delivered_sequence: int | None = Field(default=None, ge=0)
    previous_destination_operator_hold: int | None = Field(default=None, ge=0, le=1)
    previous_destination_updated_at: str | None = None
    approval_hold_reason_code: str | None = None
    approval_hold_source_artifact_digest: Digest | None = None
    approval_hold_created_at: str | None = None

    @model_validator(mode="after")
    def validate_destination_provenance(self) -> LegacyRollbackRoot:
        previous = (
            self.previous_destination_next_sequence,
            self.previous_destination_delivered_sequence,
            self.previous_destination_operator_hold,
            self.previous_destination_updated_at,
        )
        if self.destination_existed_before:
            previous_next = self.previous_destination_next_sequence
            previous_delivered = self.previous_destination_delivered_sequence
            if (
                previous_next is None
                or previous_delivered is None
                or self.previous_destination_operator_hold is None
                or self.previous_destination_updated_at is None
            ):
                raise ValueError("existing destination rollback provenance가 불완전합니다.")
            if previous_delivered >= previous_next:
                raise ValueError("existing destination rollback cursor가 유효하지 않습니다.")
        elif any(value is not None for value in previous):
            raise ValueError("new destination에는 previous provenance가 없어야 합니다.")
        approval_hold = (
            self.approval_hold_reason_code,
            self.approval_hold_source_artifact_digest,
            self.approval_hold_created_at,
        )
        if any(value is not None for value in approval_hold) and not all(
            value is not None for value in approval_hold
        ):
            raise ValueError("legacy approval hold rollback provenance가 불완전합니다.")
        if self.approval_hold_reason_code not in {None, "legacy_approval_without_audit"}:
            raise ValueError("legacy approval hold reason이 유효하지 않습니다.")
        return self


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
                   o.attempts, o.claim_generation, o.lease_owner,
                   o.lease_expires_at, o.retry_at, o.delivered_at,
                   o.remote_receipt, o.last_error_code,
                   s.aggregate_sequence, s.last_event_hash,
                   x.next_sequence, x.delivered_sequence, x.operator_hold,
                   a.applied_revision, i.target_status,
                   p.destination_ref, p.existed_before, p.previous_next_sequence,
                   p.previous_delivered_sequence, p.previous_operator_hold,
                   p.previous_updated_at, p.captured_at,
                   t.destination_ref, t.captured_at, t.attestation_version,
                   i.approval_disposition, i.proposal_artifact_digest,
                   h.reason_code, h.source_artifact_digest, h.created_at
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
            JOIN governance_legacy_import_destination_roots p
              ON p.migration_id = i.migration_id
             AND p.project_namespace = i.project_namespace
             AND p.project_id = i.project_id AND p.proposal_id = i.proposal_id
            JOIN governance_legacy_import_destination_attestations t
              ON t.migration_id = p.migration_id
             AND t.project_namespace = p.project_namespace
             AND t.project_id = p.project_id AND t.proposal_id = p.proposal_id
            LEFT JOIN governance_legacy_approval_holds h
              ON h.migration_id = i.migration_id
             AND h.project_namespace = i.project_namespace
             AND h.project_id = i.project_id AND h.proposal_id = i.proposal_id
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
                LegacyTargetStatus(str(row[35]))
            )
            approval_disposition = LegacyApprovalDisposition(str(row[46]))
            expected_hold = ("legacy_approval_without_audit", str(row[47]))
            actual_hold = (row[48], row[49])
            expected_active = (str(row[1]), int(row[2]), int(row[3]), int(row[4]), runtime_status)
            if (
                tuple(row[5:10]) != expected_active
                or str(row[10]) != str(row[1])
                or row[11] is not None
                or row[12] is not None
                or str(row[20]) != "pending"
                or int(row[21]) != 0
                or int(row[22]) != 0
                or any(row[index] is not None for index in range(23, 29))
                or int(row[29]) != int(row[15])
                or str(row[30]) != str(row[16])
                or int(row[31]) != int(row[19]) + 1
                or int(row[32]) != 0
                or int(row[33]) != 0
                or row[34] is not None
                or str(row[36]) != str(row[18])
                or str(row[43]) != str(row[18])
                or str(row[42]) != str(row[44])
                or int(row[45]) != 1
                or (
                    approval_disposition is LegacyApprovalDisposition.SYNTHETIC_REQUIRED
                    and (tuple(actual_hold) != expected_hold or row[50] is None)
                )
                or (
                    approval_disposition is not LegacyApprovalDisposition.SYNTHETIC_REQUIRED
                    and any(row[index] is not None for index in range(48, 51))
                )
                or (bool(row[37]) and int(row[38]) != int(row[19]))
                or (not bool(row[37]) and int(row[19]) != 1)
                or LegacyMigrationRollbackPlanner._proposal_root_counts(
                    connection,
                    project_ref,
                    proposal_id,
                )
                != (1, 1, 1, 0)
                or LegacyMigrationRollbackPlanner._has_unplanned_dependents(
                    connection,
                    project_ref,
                    proposal_id,
                )
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
                    destination_existed_before=bool(row[37]),
                    previous_destination_next_sequence=(
                        int(row[38]) if row[38] is not None else None
                    ),
                    previous_destination_delivered_sequence=(
                        int(row[39]) if row[39] is not None else None
                    ),
                    previous_destination_operator_hold=(
                        int(row[40]) if row[40] is not None else None
                    ),
                    previous_destination_updated_at=(str(row[41]) if row[41] is not None else None),
                    approval_hold_reason_code=(str(row[48]) if row[48] is not None else None),
                    approval_hold_source_artifact_digest=(
                        str(row[49]) if row[49] is not None else None
                    ),
                    approval_hold_created_at=(str(row[50]) if row[50] is not None else None),
                )
            )
        return tuple(roots)

    @staticmethod
    def _has_unplanned_dependents(
        connection: sqlite3.Connection,
        project_ref: ProjectRef,
        proposal_id: str,
    ) -> bool:
        planned_tables = {
            "governance_active_proposals",
            "governance_aggregate_sequences",
            "governance_audit_events",
            "governance_definition_revisions",
            "governance_legacy_approval_holds",
            "governance_legacy_approval_reviews",
            "governance_legacy_event_backfill_pending",
            "governance_legacy_import_commands",
            "governance_legacy_import_destination_attestations",
            "governance_legacy_import_destination_roots",
            "governance_legacy_migration_items",
            "governance_outbox_events",
        }
        tables = connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type = 'table' AND name LIKE 'governance_%'
            ORDER BY name
            """
        ).fetchall()
        identity = (project_ref.namespace, project_ref.project_id, proposal_id)
        required_columns = {"project_namespace", "project_id", "proposal_id"}
        for table_row in tables:
            table = str(table_row[0])
            if table in planned_tables:
                continue
            if not table.replace("_", "").isalnum():
                return True
            columns = {
                str(column[1])
                for column in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not required_columns.issubset(columns):
                continue
            dependent = connection.execute(
                f"""SELECT 1 FROM {table}
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                LIMIT 1""",
                identity,
            ).fetchone()
            if dependent is not None:
                return True
        return False

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


class LegacyMigrationRollbackExecutor:
    """Atomically append rollback evidence and remove only the authenticated plan roots."""

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

    def execute(
        self,
        plan: LegacyMigrationRollbackPlan,
        *,
        authority_request: DirectAuthorityRequest,
        reason: str,
        idempotency_key: str,
    ) -> LegacyMigrationRollbackResult:
        if len(reason.strip()) < 3 or not idempotency_key.strip():
            raise ValueError("rollback reason과 idempotency key가 필요합니다.")
        try:
            with self.store.connect() as connection, governance_transaction(connection):
                context = self.authority.authenticate(authority_request, connection=connection)
                self._require_authority(context, authority_request, plan.project_ref)
                channel_json = _canonical_json(context.source.channel.model_dump(mode="json"))
                GovernanceEventService.reconcile_connection(connection)
                LegacyProposalImportService.reconcile_verification_roots(connection)
                LegacyMigrationActivationService.reconcile_roots(connection)
                replay = self._replay(
                    connection,
                    plan,
                    idempotency_key=idempotency_key,
                    actor_ref=context.actor_ref,
                    request_id=context.source.request_id,
                    channel_json=channel_json,
                    reason=reason.strip(),
                )
                if replay is not None:
                    return replay
                head = connection.execute(
                    """
                    SELECT project_namespace, project_id, verification_id, report_digest,
                           state, lifecycle_revision, last_event_digest
                    FROM governance_legacy_migration_lifecycle_heads
                    WHERE migration_id = ?
                    """,
                    (plan.migration_id,),
                ).fetchone()
                if head is None:
                    raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_NOT_IMPORTED")
                if (str(head[0]), str(head[1])) != (
                    context.project_ref.namespace,
                    context.project_ref.project_id,
                ):
                    raise LegacyMigrationLifecycleError("PROJECT_SCOPE_MISMATCH")
                LegacyMigrationRollbackPlanner._require_rollback_state(
                    LegacyMigrationLifecycleState(str(head[4])),
                    actual_revision=int(head[5]),
                    expected_revision=plan.expected_lifecycle_revision,
                )
                roots = LegacyMigrationRollbackPlanner._exact_roots(
                    connection,
                    plan.migration_id,
                    context.project_ref,
                )
                self._require_exact_plan(plan, head, roots)
                rolled_back_at = LegacyMigrationRollbackPlanner._aware(self._clock())
                timestamp = self._timestamp(rolled_back_at)
                command_id = self._identifier("LMC")
                event_id = self._identifier("LME")
                fingerprint = plan.rollback_root_digest.removeprefix("sha256:")
                connection.execute(
                    """
                    INSERT INTO governance_legacy_migration_lifecycle_commands(
                        command_id, migration_id, project_namespace, project_id,
                        action, expected_lifecycle_revision, idempotency_key,
                        request_fingerprint, actor_id, actor_type, request_id,
                        channel_json, reason, occurred_at
                    ) VALUES (?, ?, ?, ?, 'rollback', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command_id,
                        plan.migration_id,
                        context.project_ref.namespace,
                        context.project_ref.project_id,
                        plan.expected_lifecycle_revision,
                        idempotency_key,
                        fingerprint,
                        context.actor_ref.actor_id,
                        context.actor_ref.actor_type.value,
                        context.source.request_id,
                        channel_json,
                        reason.strip(),
                        timestamp,
                    ),
                )
                event_preimage = {
                    "command_id": command_id,
                    "event_id": event_id,
                    "migration_id": plan.migration_id,
                    "lifecycle_sequence": plan.expected_lifecycle_revision,
                    "before_state": LegacyMigrationLifecycleState.STAGED_VERIFIED.value,
                    "after_state": LegacyMigrationLifecycleState.ROLLED_BACK.value,
                    "verification_id": plan.verification_id,
                    "report_digest": plan.report_digest,
                    "previous_event_digest": head[6],
                    "occurred_at": timestamp,
                }
                event_digest = _digest(event_preimage)
                connection.execute(
                    """
                    INSERT INTO governance_legacy_migration_lifecycle_events(
                        event_id, command_id, migration_id, lifecycle_sequence,
                        before_state, after_state, verification_id, report_digest,
                        previous_event_digest, event_digest, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        command_id,
                        plan.migration_id,
                        plan.expected_lifecycle_revision,
                        LegacyMigrationLifecycleState.STAGED_VERIFIED.value,
                        LegacyMigrationLifecycleState.ROLLED_BACK.value,
                        plan.verification_id,
                        plan.report_digest,
                        head[6],
                        event_digest,
                        timestamp,
                    ),
                )
                for root in roots:
                    self._insert_scope(
                        connection,
                        command_id=command_id,
                        migration_id=plan.migration_id,
                        rollback_root_digest=plan.rollback_root_digest,
                        root=root,
                        created_at=timestamp,
                    )
                result = self._result(
                    command_id=command_id,
                    event_id=event_id,
                    plan=plan,
                    actor_ref=context.actor_ref,
                    rolled_back_at=rolled_back_at,
                )
                connection.execute(
                    """
                    INSERT INTO governance_legacy_migration_lifecycle_results(
                        command_id, migration_id, result_json, result_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        command_id,
                        plan.migration_id,
                        _canonical_json(result.model_dump(mode="json")),
                        result.result_digest,
                        timestamp,
                    ),
                )
                self._before_root_removal(plan)
                for root in roots:
                    self._remove_root(connection, root, migration_id=plan.migration_id)
                    self._after_root_removed(root)
                updated = connection.execute(
                    """
                    UPDATE governance_legacy_migration_lifecycle_heads
                    SET state = 'rolled_back', lifecycle_revision = lifecycle_revision + 1,
                        last_event_digest = ?, updated_at = ?
                    WHERE migration_id = ? AND state = 'staged_verified'
                      AND lifecycle_revision = ? AND verification_id = ?
                      AND report_digest = ?
                    """,
                    (
                        event_digest,
                        timestamp,
                        plan.migration_id,
                        plan.expected_lifecycle_revision,
                        plan.verification_id,
                        plan.report_digest,
                    ),
                )
                if updated.rowcount != 1:
                    raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_LIFECYCLE_CONFLICT")
                return result
        except LegacyMigrationLifecycleError:
            raise
        except AuthorityResolutionError as error:
            raise LegacyMigrationLifecycleError(error.code) from error
        except GovernanceCommitAmbiguousError:
            try:
                return self._reconcile_ambiguous(
                    plan,
                    authority_request=authority_request,
                    reason=reason.strip(),
                    idempotency_key=idempotency_key,
                )
            except LegacyMigrationLifecycleError:
                raise
            except AuthorityResolutionError as error:
                raise LegacyMigrationLifecycleError(error.code) from error
            except (GovernanceEventError, sqlite3.Error, ValueError) as error:
                raise LegacyMigrationLifecycleError(
                    "LEGACY_MIGRATION_ROLLBACK_AMBIGUOUS"
                ) from error
        except (GovernanceEventError, sqlite3.Error, ValueError) as error:
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_CONFLICT") from error

    @staticmethod
    def _require_authority(
        context: AuthorityContext, request: DirectAuthorityRequest, project: ProjectRef
    ) -> None:
        if (
            context.project_ref != request.project_ref
            or context.project_ref != project
            or context.actor_ref.actor_type is not ActorType.HUMAN
            or AuthorityPermission.ACTIVATION_MANAGE not in context.permissions
        ):
            raise LegacyMigrationLifecycleError("AUTHORITY_DENIED")

    @staticmethod
    def _require_exact_plan(
        plan: LegacyMigrationRollbackPlan,
        head: sqlite3.Row | tuple[object, ...],
        roots: tuple[LegacyRollbackRoot, ...],
    ) -> None:
        preimage = {
            "expected_lifecycle_revision": plan.expected_lifecycle_revision,
            "migration_id": plan.migration_id,
            "project_ref": plan.project_ref.model_dump(mode="json"),
            "report_digest": str(head[3]),
            "roots": [root.model_dump(mode="json") for root in roots],
            "verification_id": str(head[2]),
        }
        if (
            plan.verification_id != str(head[2])
            or plan.report_digest != str(head[3])
            or plan.roots != roots
            or plan.rollback_root_digest != _digest(preimage)
        ):
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_PLAN_STALE")

    @staticmethod
    def _insert_scope(
        connection: sqlite3.Connection,
        *,
        command_id: str,
        migration_id: str,
        rollback_root_digest: str,
        root: LegacyRollbackRoot,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO governance_legacy_rollback_scopes(
                command_id, migration_id, project_namespace, project_id, proposal_id,
                rollback_root_digest, definition_digest, content_revision,
                state_revision, decision_epoch, audit_event_id, aggregate_sequence,
                event_hash, outbox_event_id, destination_ref, destination_sequence,
                destination_existed_before, previous_destination_next_sequence,
                previous_destination_delivered_sequence,
                previous_destination_operator_hold, previous_destination_updated_at,
                approval_hold_reason_code, approval_hold_source_artifact_digest,
                approval_hold_created_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                migration_id,
                root.proposal_ref.project_ref.namespace,
                root.proposal_ref.project_ref.project_id,
                root.proposal_ref.proposal_id,
                rollback_root_digest,
                root.definition_digest,
                root.content_revision,
                root.state_revision,
                root.decision_epoch,
                root.audit_event_id,
                root.aggregate_sequence,
                root.event_hash,
                root.outbox_event_id,
                root.destination_ref,
                root.destination_sequence,
                int(root.destination_existed_before),
                root.previous_destination_next_sequence,
                root.previous_destination_delivered_sequence,
                root.previous_destination_operator_hold,
                root.previous_destination_updated_at,
                root.approval_hold_reason_code,
                root.approval_hold_source_artifact_digest,
                root.approval_hold_created_at,
                created_at,
            ),
        )

    @staticmethod
    def _remove_root(
        connection: sqlite3.Connection,
        root: LegacyRollbackRoot,
        *,
        migration_id: str,
    ) -> None:
        identity = (
            root.proposal_ref.project_ref.namespace,
            root.proposal_ref.project_ref.project_id,
            root.proposal_ref.proposal_id,
        )
        statements = (
            ("DELETE FROM governance_outbox_events WHERE event_id = ?", (root.outbox_event_id,)),
            ("DELETE FROM governance_audit_events WHERE event_id = ?", (root.audit_event_id,)),
            (
                "DELETE FROM governance_aggregate_sequences WHERE project_namespace = ? "
                "AND project_id = ? AND proposal_id = ?",
                identity,
            ),
            (
                "DELETE FROM governance_definition_revisions WHERE project_namespace = ? "
                "AND project_id = ? AND proposal_id = ? AND content_revision = ?",
                (*identity, root.content_revision),
            ),
            (
                "DELETE FROM governance_active_proposals WHERE project_namespace = ? "
                "AND project_id = ? AND proposal_id = ?",
                identity,
            ),
        )
        for sql, values in statements:
            if connection.execute(sql, values).rowcount != 1:
                raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_ROOT_MISMATCH")
        if root.approval_hold_reason_code is not None:
            deleted_hold = connection.execute(
                """
                DELETE FROM governance_legacy_approval_holds
                WHERE migration_id = ? AND project_namespace = ? AND project_id = ?
                  AND proposal_id = ? AND reason_code = ?
                  AND source_artifact_digest = ? AND created_at = ?
                """,
                (
                    migration_id,
                    root.proposal_ref.project_ref.namespace,
                    root.proposal_ref.project_ref.project_id,
                    root.proposal_ref.proposal_id,
                    root.approval_hold_reason_code,
                    root.approval_hold_source_artifact_digest,
                    root.approval_hold_created_at,
                ),
            )
            if deleted_hold.rowcount != 1:
                raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_ROOT_MISMATCH")
        if root.destination_existed_before:
            restored = connection.execute(
                """
                UPDATE governance_outbox_destinations
                SET next_sequence = ?, delivered_sequence = ?, operator_hold = ?, updated_at = ?
                WHERE destination_ref = ? AND next_sequence = ?
                  AND delivered_sequence = 0 AND operator_hold = 0
                """,
                (
                    root.previous_destination_next_sequence,
                    root.previous_destination_delivered_sequence,
                    root.previous_destination_operator_hold,
                    root.previous_destination_updated_at,
                    root.destination_ref,
                    root.destination_sequence + 1,
                ),
            )
            if restored.rowcount != 1:
                raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_ROOT_MISMATCH")
        elif (
            connection.execute(
                """
            DELETE FROM governance_outbox_destinations
            WHERE destination_ref = ? AND next_sequence = ?
              AND delivered_sequence = 0 AND operator_hold = 0
            """,
                (root.destination_ref, root.destination_sequence + 1),
            ).rowcount
            != 1
        ):
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_ROOT_MISMATCH")

    @staticmethod
    def _result(
        *,
        command_id: str,
        event_id: str,
        plan: LegacyMigrationRollbackPlan,
        actor_ref: ActorRef,
        rolled_back_at: datetime,
    ) -> LegacyMigrationRollbackResult:
        provisional = LegacyMigrationRollbackResult.model_construct(
            command_id=command_id,
            event_id=event_id,
            migration_id=plan.migration_id,
            project_ref=plan.project_ref,
            state=LegacyMigrationLifecycleState.ROLLED_BACK,
            lifecycle_revision=plan.expected_lifecycle_revision + 1,
            verification_id=plan.verification_id,
            report_digest=plan.report_digest,
            rollback_root_digest=plan.rollback_root_digest,
            removed_proposal_count=len(plan.roots),
            actor_ref=actor_ref,
            rolled_back_at=rolled_back_at,
            result_digest=f"sha256:{'0' * 64}",
            replayed=False,
        )
        preimage = provisional.model_dump(mode="json", exclude={"result_digest", "replayed"})
        return LegacyMigrationRollbackResult(
            **preimage,
            result_digest=_digest(preimage),
        )

    def _reconcile_ambiguous(
        self,
        plan: LegacyMigrationRollbackPlan,
        *,
        authority_request: DirectAuthorityRequest,
        reason: str,
        idempotency_key: str,
    ) -> LegacyMigrationRollbackResult:
        context = self.authority.authenticate(authority_request)
        self._require_authority(context, authority_request, plan.project_ref)
        with self.store.connect() as connection:
            GovernanceEventService.reconcile_connection(connection)
            LegacyProposalImportService.reconcile_verification_roots(connection)
            LegacyMigrationActivationService.reconcile_roots(connection)
            result = self._replay(
                connection,
                plan,
                idempotency_key=idempotency_key,
                actor_ref=context.actor_ref,
                request_id=context.source.request_id,
                channel_json=_canonical_json(context.source.channel.model_dump(mode="json")),
                reason=reason,
            )
        if result is None:
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_AMBIGUOUS")
        return result

    @staticmethod
    def _replay(
        connection: sqlite3.Connection,
        plan: LegacyMigrationRollbackPlan,
        *,
        idempotency_key: str,
        actor_ref: ActorRef,
        request_id: str,
        channel_json: str,
        reason: str,
    ) -> LegacyMigrationRollbackResult | None:
        row = connection.execute(
            """
            SELECT c.migration_id, c.project_namespace, c.project_id, c.action,
                   c.expected_lifecycle_revision, c.request_fingerprint,
                   c.actor_id, c.actor_type, c.request_id, c.channel_json, c.reason,
                   r.result_json, r.result_digest
            FROM governance_legacy_migration_lifecycle_commands c
            LEFT JOIN governance_legacy_migration_lifecycle_results r
              ON r.command_id = c.command_id
            WHERE c.idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        expected = (
            plan.migration_id,
            plan.project_ref.namespace,
            plan.project_ref.project_id,
            "rollback",
            plan.expected_lifecycle_revision,
            plan.rollback_root_digest.removeprefix("sha256:"),
            actor_ref.actor_id,
            actor_ref.actor_type.value,
            request_id,
            channel_json,
            reason,
        )
        if tuple(row[:11]) != expected or row[11] is None:
            raise LegacyMigrationLifecycleError("IDEMPOTENCY_CONFLICT")
        result = LegacyMigrationRollbackResult.model_validate_json(str(row[11]))
        if result.result_digest != str(row[12]):
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ROLLBACK_EVIDENCE_MISMATCH")
        return result.model_copy(update={"replayed": True})

    @staticmethod
    def _identifier(prefix: str) -> str:
        return f"{prefix}-{secrets.token_hex(8).upper()}"

    def _before_root_removal(self, plan: LegacyMigrationRollbackPlan) -> None:
        """Failure-injection seam after durable evidence writes and before deletion."""

    def _after_root_removed(self, root: LegacyRollbackRoot) -> None:
        """Failure-injection seam after one complete authoritative root removal."""

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
