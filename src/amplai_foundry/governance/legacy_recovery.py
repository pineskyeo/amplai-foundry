"""Authenticated planning for post-activation legacy forward recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.active_proposals import ActiveProposalStatus
from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    AuthorityService,
    DirectAuthorityRequest,
)
from amplai_foundry.governance.events import GovernanceEventError, GovernanceEventService
from amplai_foundry.governance.legacy_gates import legacy_mutation_block
from amplai_foundry.governance.legacy_lifecycle import (
    LegacyMigrationActivationService,
    LegacyMigrationLifecycleError,
    LegacyMigrationLifecycleState,
)
from amplai_foundry.governance.legacy_migration import (
    LegacyApprovalDisposition,
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


class LegacyForwardRecoveryRoot(BaseModel):
    """One qualified v3 Proposal root eligible for forward-only correction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_ref: ProposalRef
    imported_definition_digest: Digest
    imported_content_revision: int = Field(ge=1)
    imported_state_revision: int = Field(ge=1)
    imported_decision_epoch: int = Field(ge=1)
    imported_status: ActiveProposalStatus
    approval_disposition: LegacyApprovalDisposition
    active_definition_digest: Digest
    content_revision: int = Field(ge=1)
    state_revision: int = Field(ge=1)
    decision_epoch: int = Field(ge=1)
    status: ActiveProposalStatus
    applied_revision: str | None = None


class LegacyForwardRecoveryPlan(BaseModel):
    """Digest-bound snapshot that a later forward-recovery executor must consume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    migration_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    project_ref: ProjectRef
    expected_lifecycle_revision: int = Field(ge=1)
    activation_event_digest: Digest
    strategy: Literal["v3_definition_revision"] = "v3_definition_revision"
    roots: tuple[LegacyForwardRecoveryRoot, ...] = Field(min_length=1)
    recovery_root_digest: Digest
    planned_by: ActorRef
    planned_at: AwareDatetime

    @model_validator(mode="after")
    def validate_recovery_root_digest(self) -> LegacyForwardRecoveryPlan:
        if self.recovery_root_digest != _digest(self.digest_preimage()):
            raise ValueError("legacy forward recovery root digest가 일치하지 않습니다.")
        return self

    def digest_preimage(self) -> dict[str, object]:
        return {
            "activation_event_digest": self.activation_event_digest,
            "expected_lifecycle_revision": self.expected_lifecycle_revision,
            "migration_id": self.migration_id,
            "project_ref": self.project_ref.model_dump(mode="json"),
            "roots": [root.model_dump(mode="json") for root in self.roots],
            "strategy": self.strategy,
        }


class LegacyMigrationForwardRecoveryPlanner:
    """Prove an activated migration and freeze its requested v3 correction roots."""

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
        proposal_refs: Sequence[ProposalRef],
        *,
        authority_request: DirectAuthorityRequest,
        expected_lifecycle_revision: int,
    ) -> LegacyForwardRecoveryPlan:
        if expected_lifecycle_revision < 1:
            raise ValueError("expected_lifecycle_revision은 1 이상이어야 합니다.")
        requested = tuple(proposal_refs)
        if not requested or len(set(requested)) != len(requested):
            raise ValueError("forward recovery Proposal scope는 비어 있거나 중복될 수 없습니다.")
        try:
            with self.store.connect() as connection, governance_transaction(connection):
                context = self.authority.authenticate(authority_request, connection=connection)
                if (
                    context.project_ref != authority_request.project_ref
                    or context.actor_ref.actor_type is not ActorType.HUMAN
                    or AuthorityPermission.ACTIVATION_MANAGE not in context.permissions
                    or any(ref.project_ref != context.project_ref for ref in requested)
                ):
                    raise LegacyMigrationLifecycleError("AUTHORITY_DENIED")
                GovernanceEventService.reconcile_connection(connection)
                LegacyProposalImportService.reconcile_verification_roots(connection)
                LegacyMigrationActivationService.reconcile_roots(connection)
                head = connection.execute(
                    """
                    SELECT project_namespace, project_id, state, lifecycle_revision,
                           last_event_digest
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
                state = LegacyMigrationLifecycleState(str(head[2]))
                if state is LegacyMigrationLifecycleState.ROLLED_BACK:
                    raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_AFTER_ROLLBACK")
                if state is not LegacyMigrationLifecycleState.ACTIVATED:
                    raise LegacyMigrationLifecycleError(
                        "LEGACY_FORWARD_RECOVERY_REQUIRES_ACTIVATION"
                    )
                if int(head[3]) != expected_lifecycle_revision:
                    raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_LIFECYCLE_CONFLICT")
                if head[4] is None:
                    raise LegacyMigrationLifecycleError(
                        "LEGACY_FORWARD_RECOVERY_ACTIVATION_EVIDENCE_MISSING"
                    )
                roots = self._roots(
                    connection,
                    migration_id,
                    context.project_ref,
                    requested,
                )
                planned_at = self._aware(self._clock())
                preimage = {
                    "activation_event_digest": str(head[4]),
                    "expected_lifecycle_revision": expected_lifecycle_revision,
                    "migration_id": migration_id,
                    "project_ref": context.project_ref.model_dump(mode="json"),
                    "roots": [root.model_dump(mode="json") for root in roots],
                    "strategy": "v3_definition_revision",
                }
                return LegacyForwardRecoveryPlan(
                    migration_id=migration_id,
                    project_ref=context.project_ref,
                    expected_lifecycle_revision=expected_lifecycle_revision,
                    activation_event_digest=str(head[4]),
                    roots=roots,
                    recovery_root_digest=_digest(preimage),
                    planned_by=context.actor_ref,
                    planned_at=planned_at,
                )
        except LegacyMigrationLifecycleError:
            raise
        except AuthorityResolutionError as error:
            raise LegacyMigrationLifecycleError(error.code) from error
        except (GovernanceEventError, sqlite3.Error, ValueError) as error:
            raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_ROOT_MISMATCH") from error

    @staticmethod
    def _roots(
        connection: sqlite3.Connection,
        migration_id: str,
        project_ref: ProjectRef,
        requested: tuple[ProposalRef, ...],
    ) -> tuple[LegacyForwardRecoveryRoot, ...]:
        proposal_ids = tuple(sorted(ref.proposal_id for ref in requested))
        placeholders = ", ".join("?" for _ in proposal_ids)
        rows = connection.execute(
            f"""
            SELECT i.proposal_id, i.definition_digest, i.content_revision,
                   i.state_revision, i.decision_epoch, i.target_status,
                   i.approval_disposition,
                   a.active_definition_digest, a.content_revision, a.state_revision,
                   a.decision_epoch, a.status, a.applied_revision
            FROM governance_legacy_migration_items i
            JOIN governance_active_proposals a
              ON a.project_namespace = i.project_namespace
             AND a.project_id = i.project_id AND a.proposal_id = i.proposal_id
            WHERE i.migration_id = ? AND i.project_namespace = ? AND i.project_id = ?
              AND i.proposal_id IN ({placeholders})
            ORDER BY i.proposal_id
            """,
            (migration_id, project_ref.namespace, project_ref.project_id, *proposal_ids),
        ).fetchall()
        if len(rows) != len(proposal_ids) or tuple(str(row[0]) for row in rows) != proposal_ids:
            raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_SCOPE_MISMATCH")
        roots: list[LegacyForwardRecoveryRoot] = []
        for row in rows:
            ref = ProposalRef(project_ref=project_ref, proposal_id=str(row[0]))
            blocked = legacy_mutation_block(connection, ref)
            if blocked == "LEGACY_APPROVAL_REVIEW_REQUIRED":
                raise LegacyMigrationLifecycleError(blocked)
            status = ActiveProposalStatus(str(row[11]))
            LegacyMigrationForwardRecoveryPlanner._require_eligible_status(
                connection,
                ref,
                status=status,
                applied_revision=str(row[12]) if row[12] is not None else None,
            )
            roots.append(
                LegacyForwardRecoveryRoot(
                    proposal_ref=ref,
                    imported_definition_digest=str(row[1]),
                    imported_content_revision=int(row[2]),
                    imported_state_revision=int(row[3]),
                    imported_decision_epoch=int(row[4]),
                    imported_status=ActiveProposalStatus(
                        LegacyProposalImportService._runtime_status(LegacyTargetStatus(str(row[5])))
                    ),
                    approval_disposition=LegacyApprovalDisposition(str(row[6])),
                    active_definition_digest=str(row[7]),
                    content_revision=int(row[8]),
                    state_revision=int(row[9]),
                    decision_epoch=int(row[10]),
                    status=status,
                    applied_revision=str(row[12]) if row[12] is not None else None,
                )
            )
        return tuple(roots)

    @staticmethod
    def _require_eligible_status(
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        status: ActiveProposalStatus,
        applied_revision: str | None,
    ) -> None:
        if status not in {ActiveProposalStatus.DRAFT, ActiveProposalStatus.CHANGES_REQUESTED}:
            raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_STATE_NOT_ELIGIBLE")
        if applied_revision is not None:
            raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_DEPENDENT_STATE")
        identity = (ref.project_ref.namespace, ref.project_ref.project_id, ref.proposal_id)
        for table in (
            "governance_approved_snapshots",
            "governance_apply_grants",
            "governance_apply_jobs",
            "governance_publish_intents",
        ):
            if (
                connection.execute(
                    f"""SELECT 1 FROM {table}
                    WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                    LIMIT 1""",
                    identity,
                ).fetchone()
                is not None
            ):
                raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_DEPENDENT_STATE")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock은 timezone-aware datetime을 반환해야 합니다.")
        return value
