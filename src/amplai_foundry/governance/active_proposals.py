"""Qualified active definition CAS and Proposal state machine."""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from typing import Protocol, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.authority import AuthorityService, DirectAuthorityRequest
from amplai_foundry.governance.legacy_gates import (
    legacy_definition_revision_block,
    legacy_mutation_block,
)
from amplai_foundry.governance.models import AuthorityPermission, Digest, ProposalRef
from amplai_foundry.governance.object_store import DefinitionObjectRef
from amplai_foundry.governance.store import GovernanceStore, governance_transaction


class ActiveProposalError(RuntimeError):
    """The active Proposal aggregate cannot satisfy a governed command."""


class ActiveProposalNotFoundError(ActiveProposalError):
    """The qualified Proposal does not exist or is not visible."""


class DefinitionCASConflictError(ActiveProposalError):
    """The expected active definition or state revision is stale."""


class InvalidProposalTransitionError(ActiveProposalError):
    """The requested Proposal state transition is not allowed."""


class ActiveProposalStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    APPLY_REQUESTED = "apply_requested"
    APPLY_FAILED = "apply_failed"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


_STATE_ONLY_TRANSITIONS = {
    ActiveProposalStatus.DRAFT: frozenset(
        {ActiveProposalStatus.REVIEWED, ActiveProposalStatus.SUPERSEDED}
    ),
    ActiveProposalStatus.REVIEWED: frozenset(
        {
            ActiveProposalStatus.APPROVED,
            ActiveProposalStatus.REJECTED,
            ActiveProposalStatus.CHANGES_REQUESTED,
        }
    ),
    ActiveProposalStatus.APPROVED: frozenset({ActiveProposalStatus.APPLY_REQUESTED}),
    ActiveProposalStatus.APPLY_REQUESTED: frozenset(
        {ActiveProposalStatus.APPLIED, ActiveProposalStatus.APPLY_FAILED}
    ),
    ActiveProposalStatus.APPLY_FAILED: frozenset({ActiveProposalStatus.APPLY_REQUESTED}),
}
_DEFINITION_REVISION_STATUSES = frozenset(
    {ActiveProposalStatus.DRAFT, ActiveProposalStatus.CHANGES_REQUESTED}
)
_LEGACY_FORWARD_RECOVERY_CAPABILITY = object()


def state_transition_allowed(
    current: ActiveProposalStatus,
    next_status: ActiveProposalStatus,
) -> bool:
    """Return whether a state-only transition is allowed by the v3 lifecycle."""

    return next_status in _STATE_ONLY_TRANSITIONS.get(current, frozenset())


class ActiveProposalView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_ref: ProposalRef
    active_definition_digest: Digest
    content_revision: int = Field(ge=1)
    state_revision: int = Field(ge=1)
    decision_epoch: int = Field(ge=1)
    status: ActiveProposalStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class DefinitionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_ref: ProposalRef
    content_revision: int = Field(ge=1)
    definition_digest: Digest
    previous_definition_digest: Digest | None = None
    activated_from_status: ActiveProposalStatus | None = None
    activated_at: AwareDatetime


class DefinitionObjectReader(Protocol):
    def get_definition_object(self, ref: ProposalRef, digest: str) -> bytes: ...


class ActiveProposalRepository:
    """SQLite repository whose writes use qualified CAS predicates."""

    def __init__(self, store: GovernanceStore, definitions: DefinitionObjectReader) -> None:
        self.store = store
        self.definitions = definitions

    def get(self, ref: ProposalRef) -> ActiveProposalView | None:
        with self.store.connect() as connection:
            row = self._select(connection, ref)
            return self._view(row, ref) if row is not None else None

    def list(self, project_ref: ProjectRef) -> tuple[ActiveProposalView, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT active_definition_digest, content_revision, state_revision,
                       decision_epoch, status, created_at, updated_at, proposal_id
                FROM governance_active_proposals
                WHERE project_namespace = ? AND project_id = ?
                ORDER BY proposal_id
                """,
                (project_ref.namespace, project_ref.project_id),
            ).fetchall()
        return tuple(
            self._view(
                cast(tuple[object, ...], row[:7]),
                ProposalRef(project_ref=project_ref, proposal_id=str(row[7])),
            )
            for row in rows
        )

    def list_definition_revisions(self, ref: ProposalRef) -> tuple[DefinitionRevision, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT content_revision, definition_digest, previous_definition_digest,
                       activated_from_status, activated_at
                FROM governance_definition_revisions
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                ORDER BY content_revision
                """,
                self._identity(ref),
            ).fetchall()
        return tuple(
            DefinitionRevision.model_validate(
                {
                    "proposal_ref": ref,
                    "content_revision": row[0],
                    "definition_digest": row[1],
                    "previous_definition_digest": row[2],
                    "activated_from_status": row[3],
                    "activated_at": row[4],
                }
            )
            for row in rows
        )

    def activate_definition_revision(
        self,
        ref: ProposalRef,
        *,
        expected_active_digest: str | None,
        expected_state_revision: int,
        next_object_ref: DefinitionObjectRef,
    ) -> ActiveProposalView:
        if expected_state_revision < 0:
            raise ValueError("expected_state_revision은 0 이상이어야 합니다.")
        if next_object_ref.object_kind != "definition" or next_object_ref.proposal_ref != ref:
            raise ActiveProposalNotFoundError("PROPOSAL_NOT_FOUND")

        self.definitions.get_definition_object(ref, next_object_ref.digest)

        with self.store.connect() as connection, governance_transaction(connection):
            current_row = self._select(connection, ref)
            if current_row is None:
                return self._activate_initial(
                    connection,
                    ref,
                    expected_active_digest,
                    expected_state_revision,
                    next_object_ref.digest,
                )
            block = legacy_mutation_block(connection, ref)
            if block is not None:
                raise ActiveProposalError(block)
            current = self._view(current_row, ref)
            return self._activate_next(
                connection,
                current,
                expected_active_digest,
                expected_state_revision,
                next_object_ref.digest,
            )

    def _activate_initial(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        expected_active_digest: str | None,
        expected_state_revision: int,
        next_digest: str,
    ) -> ActiveProposalView:
        if expected_active_digest is not None or expected_state_revision != 0:
            raise DefinitionCASConflictError("DEFINITION_CAS_CONFLICT")
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id, active_definition_digest,
                content_revision, state_revision, decision_epoch, status,
                created_at, updated_at
            )
            VALUES (
                ?, ?, ?, ?, 1, 1, 1, 'draft',
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            """,
            (*self._identity(ref), next_digest),
        )
        self._insert_revision(connection, ref, 1, next_digest, None, None)
        row = self._select(connection, ref)
        if row is None:
            raise ActiveProposalNotFoundError("PROPOSAL_NOT_FOUND")
        return self._view(row, ref)

    def _activate_next(
        self,
        connection: sqlite3.Connection,
        current: ActiveProposalView,
        expected_active_digest: str | None,
        expected_state_revision: int,
        next_digest: str,
        *,
        _legacy_recovery_capability: object | None = None,
    ) -> ActiveProposalView:
        recovery_block = legacy_definition_revision_block(connection, current.proposal_ref)
        if (
            recovery_block is not None
            and _legacy_recovery_capability is not _LEGACY_FORWARD_RECOVERY_CAPABILITY
        ):
            raise ActiveProposalError(recovery_block)
        if (
            current.active_definition_digest != expected_active_digest
            or current.state_revision != expected_state_revision
        ):
            raise DefinitionCASConflictError("DEFINITION_CAS_CONFLICT")
        if current.status not in _DEFINITION_REVISION_STATUSES:
            raise InvalidProposalTransitionError(
                f"DEFINITION_REVISION_NOT_ALLOWED:{current.status}"
            )
        if current.active_definition_digest == next_digest:
            raise DefinitionCASConflictError("DEFINITION_UNCHANGED")
        prior = connection.execute(
            """
            SELECT 1 FROM governance_definition_revisions
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
              AND definition_digest = ?
            """,
            (*self._identity(current.proposal_ref), next_digest),
        ).fetchone()
        if prior is not None:
            raise DefinitionCASConflictError("DEFINITION_ALREADY_ACTIVATED")

        updated = connection.execute(
            """
            UPDATE governance_active_proposals
            SET active_definition_digest = ?,
                content_revision = content_revision + 1,
                state_revision = state_revision + 1,
                decision_epoch = decision_epoch + 1,
                status = 'draft',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
              AND active_definition_digest = ? AND state_revision = ?
            """,
            (
                next_digest,
                *self._identity(current.proposal_ref),
                expected_active_digest,
                expected_state_revision,
            ),
        )
        if updated.rowcount != 1:
            raise DefinitionCASConflictError("DEFINITION_CAS_CONFLICT")
        next_content_revision = current.content_revision + 1
        connection.execute(
            """
            UPDATE governance_action_tokens
            SET state = 'revoked',
                resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
              AND decision_epoch <= ? AND state = 'issued'
            """,
            (*self._identity(current.proposal_ref), current.decision_epoch),
        )
        self._insert_revision(
            connection,
            current.proposal_ref,
            next_content_revision,
            next_digest,
            current.active_definition_digest,
            current.status,
        )
        row = self._select(connection, current.proposal_ref)
        if row is None:
            raise ActiveProposalNotFoundError("PROPOSAL_NOT_FOUND")
        return self._view(row, current.proposal_ref)

    def _activate_next_for_legacy_forward_recovery(
        self,
        connection: sqlite3.Connection,
        current: ActiveProposalView,
        expected_active_digest: str,
        expected_state_revision: int,
        next_digest: str,
        *,
        recovery_id: str,
        migration_id: str,
    ) -> ActiveProposalView:
        """Activate one revision through the authenticated recovery transaction only."""

        capability = connection.execute(
            """
            SELECT 1
            FROM governance_legacy_forward_recovery_commands c
            JOIN governance_legacy_migration_items i
              ON i.migration_id = c.migration_id
             AND i.project_namespace = c.project_namespace
             AND i.project_id = c.project_id
            WHERE c.recovery_id = ? AND c.migration_id = ?
              AND c.project_namespace = ? AND c.project_id = ?
              AND i.proposal_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM governance_legacy_forward_recovery_items r
                  WHERE r.recovery_id = c.recovery_id
                    AND r.project_namespace = i.project_namespace
                    AND r.project_id = i.project_id
                    AND r.proposal_id = i.proposal_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM governance_legacy_forward_recovery_results r
                  WHERE r.recovery_id = c.recovery_id
              )
            """,
            (
                recovery_id,
                migration_id,
                current.proposal_ref.project_ref.namespace,
                current.proposal_ref.project_ref.project_id,
                current.proposal_ref.proposal_id,
            ),
        ).fetchone()
        if capability is None:
            raise ActiveProposalError("LEGACY_FORWARD_RECOVERY_REQUIRED")
        return self._activate_next(
            connection,
            current,
            expected_active_digest,
            expected_state_revision,
            next_digest,
            _legacy_recovery_capability=_LEGACY_FORWARD_RECOVERY_CAPABILITY,
        )

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        ref: ProposalRef,
        content_revision: int,
        digest: str,
        previous_digest: str | None,
        activated_from_status: ActiveProposalStatus | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO governance_definition_revisions(
                project_namespace, project_id, proposal_id, content_revision,
                definition_digest, previous_definition_digest, activated_from_status,
                activated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (
                *ActiveProposalRepository._identity(ref),
                content_revision,
                digest,
                previous_digest,
                activated_from_status.value if activated_from_status is not None else None,
            ),
        )

    @staticmethod
    def _identity(ref: ProposalRef) -> tuple[str, str, str]:
        return (
            ref.project_ref.namespace,
            ref.project_ref.project_id,
            ref.proposal_id,
        )

    @staticmethod
    def _select(
        connection: sqlite3.Connection,
        ref: ProposalRef,
    ) -> tuple[object, ...] | None:
        return cast(
            tuple[object, ...] | None,
            connection.execute(
                """
                SELECT active_definition_digest, content_revision, state_revision,
                       decision_epoch, status, created_at, updated_at
                FROM governance_active_proposals
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                """,
                ActiveProposalRepository._identity(ref),
            ).fetchone(),
        )

    @staticmethod
    def _view(
        row: tuple[object, ...],
        ref: ProposalRef,
    ) -> ActiveProposalView:
        return ActiveProposalView.model_validate(
            {
                "proposal_ref": ref,
                "active_definition_digest": row[0],
                "content_revision": row[1],
                "state_revision": row[2],
                "decision_epoch": row[3],
                "status": row[4],
                "created_at": row[5],
                "updated_at": row[6],
            }
        )


class ProposalSubmissionService:
    """Authorize and atomically submit a draft Proposal for review."""

    def __init__(
        self,
        store: GovernanceStore,
        repository: ActiveProposalRepository,
        authority_service: AuthorityService,
    ) -> None:
        self.store = store
        self.repository = repository
        self.authority_service = authority_service

    def submit_for_review(
        self,
        ref: ProposalRef,
        *,
        authority_request: DirectAuthorityRequest,
        expected_state_revision: int,
    ) -> ActiveProposalView:
        if expected_state_revision < 1:
            raise ValueError("expected_state_revision은 1 이상이어야 합니다.")
        if authority_request.project_ref != ref.project_ref:
            raise ActiveProposalError("PROJECT_SCOPE_MISMATCH")
        with self.store.connect() as connection, governance_transaction(connection):
            authority = self.authority_service.authenticate(
                authority_request,
                connection=connection,
            )
            if AuthorityPermission.PROPOSAL_SUBMIT_REVIEW not in authority.permissions:
                raise ActiveProposalError("AUTHORITY_DENIED")
            row = self.repository._select(connection, ref)
            if row is None:
                raise ActiveProposalNotFoundError("PROPOSAL_NOT_FOUND")
            current = self.repository._view(row, ref)
            block = legacy_mutation_block(connection, ref)
            if block is not None:
                raise ActiveProposalError(block)
            if (
                current.status is not ActiveProposalStatus.DRAFT
                or current.state_revision != expected_state_revision
            ):
                raise DefinitionCASConflictError("PROPOSAL_STATE_STALE")
            updated = connection.execute(
                """
                UPDATE governance_active_proposals
                SET status = 'reviewed',
                    state_revision = state_revision + 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                  AND status = 'draft' AND state_revision = ?
                """,
                (*ActiveProposalRepository._identity(ref), expected_state_revision),
            )
            if updated.rowcount != 1:
                raise DefinitionCASConflictError("PROPOSAL_STATE_STALE")
            result = self.repository._select(connection, ref)
            if result is None:
                raise ActiveProposalNotFoundError("PROPOSAL_NOT_FOUND")
            return self.repository._view(result, ref)
