"""Authenticated planning for post-activation legacy forward recovery."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.active_proposals import (
    ActiveProposalError,
    ActiveProposalRepository,
    ActiveProposalStatus,
    DefinitionObjectReader,
)
from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    AuthorityService,
    DirectAuthorityRequest,
)
from amplai_foundry.governance.events import (
    GovernanceEventError,
    GovernanceEventService,
    LegacyForwardRecoveryProjectionPayload,
)
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
    AuthorityContext,
    AuthorityPermission,
    Digest,
    ProposalRef,
)
from amplai_foundry.governance.object_store import DefinitionObjectRef, DefinitionObjectStoreError
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


class LegacyForwardRecoveryExecutionRoot(BaseModel):
    """One atomically committed definition correction and its projection evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str = Field(pattern=r"^LFI-[A-F0-9]{16}$")
    proposal_ref: ProposalRef
    previous_definition_digest: Digest
    previous_content_revision: int = Field(ge=1)
    previous_state_revision: int = Field(ge=1)
    previous_decision_epoch: int = Field(ge=1)
    previous_status: ActiveProposalStatus
    next_definition_digest: Digest
    next_content_revision: int = Field(ge=2)
    next_state_revision: int = Field(ge=2)
    next_decision_epoch: int = Field(ge=2)
    next_status: Literal["draft"] = "draft"
    audit_event_id: str
    outbox_event_ids: tuple[str, ...] = Field(min_length=1)


class LegacyForwardRecoveryResult(BaseModel):
    """Digest-bound durable result of one atomic recovery command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recovery_id: str = Field(pattern=r"^LFR-[A-F0-9]{16}$")
    migration_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    project_ref: ProjectRef
    recovery_root_digest: Digest
    expected_lifecycle_revision: int = Field(ge=1)
    activation_event_digest: Digest
    roots: tuple[LegacyForwardRecoveryExecutionRoot, ...] = Field(min_length=1)
    recovered_proposal_count: int = Field(ge=1)
    actor_ref: ActorRef
    recovered_at: AwareDatetime
    result_digest: Digest
    replayed: bool = False

    @model_validator(mode="after")
    def validate_result_digest(self) -> LegacyForwardRecoveryResult:
        preimage = self.model_dump(mode="json", exclude={"result_digest", "replayed"})
        if self.recovered_proposal_count != len(self.roots) or self.result_digest != _digest(
            preimage
        ):
            raise ValueError("legacy forward recovery result digest가 일치하지 않습니다.")
        return self


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
                GovernanceEventService.reconcile_connection(connection)
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


class LegacyMigrationForwardRecoveryExecutor:
    """Atomically activate corrected definitions and append durable recovery evidence."""

    def __init__(
        self,
        store: GovernanceStore,
        authority: AuthorityService,
        definitions: DefinitionObjectReader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.authority = authority
        self.definitions = definitions
        self._clock = clock or _system_now

    def execute(
        self,
        plan: LegacyForwardRecoveryPlan,
        next_objects: Sequence[DefinitionObjectRef],
        *,
        authority_request: DirectAuthorityRequest,
        reason: str,
        idempotency_key: str,
    ) -> LegacyForwardRecoveryResult:
        if (
            len(reason.strip()) < 3
            or len(reason) > 512
            or not idempotency_key.strip()
            or len(idempotency_key) > 512
        ):
            raise ValueError("forward recovery reason과 idempotency key가 필요합니다.")
        objects = self._objects(plan, next_objects)
        fingerprint = self._request_fingerprint(plan, objects)
        try:
            for ref, next_object in objects:
                self.definitions.get_definition_object(ref, next_object.digest)
            with self.store.connect() as connection, governance_transaction(connection):
                context = self.authority.authenticate(authority_request, connection=connection)
                self._require_authority(context, authority_request, plan.project_ref)
                channel_json = _canonical_json(context.source.channel.model_dump(mode="json"))
                LegacyProposalImportService.reconcile_verification_roots(connection)
                LegacyMigrationActivationService.reconcile_roots(connection)
                GovernanceEventService.reconcile_connection(connection)
                self.reconcile_roots(connection)
                replay = self._replay(
                    connection,
                    plan,
                    fingerprint=fingerprint,
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
                    SELECT project_namespace, project_id, state, lifecycle_revision,
                           last_event_digest
                    FROM governance_legacy_migration_lifecycle_heads
                    WHERE migration_id = ?
                    """,
                    (plan.migration_id,),
                ).fetchone()
                if head is None:
                    raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_NOT_IMPORTED")
                self._require_head(plan, context, head)
                current_roots = LegacyMigrationForwardRecoveryPlanner._roots(
                    connection,
                    plan.migration_id,
                    context.project_ref,
                    tuple(root.proposal_ref for root in plan.roots),
                )
                self._require_exact_plan(plan, current_roots)
                recovered_at = LegacyMigrationForwardRecoveryPlanner._aware(self._clock())
                timestamp = self._timestamp(recovered_at)
                recovery_id = self._identifier("LFR")
                connection.execute(
                    """
                    INSERT INTO governance_legacy_forward_recovery_commands(
                        recovery_id, migration_id, project_namespace, project_id,
                        idempotency_key, request_fingerprint, recovery_root_digest,
                        expected_lifecycle_revision, activation_event_digest,
                        actor_id, actor_type, request_id, channel_json, reason, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recovery_id,
                        plan.migration_id,
                        plan.project_ref.namespace,
                        plan.project_ref.project_id,
                        idempotency_key,
                        fingerprint,
                        plan.recovery_root_digest,
                        plan.expected_lifecycle_revision,
                        plan.activation_event_digest,
                        context.actor_ref.actor_id,
                        context.actor_ref.actor_type.value,
                        context.source.request_id,
                        channel_json,
                        reason.strip(),
                        timestamp,
                    ),
                )
                repository = ActiveProposalRepository(self.store, self.definitions)
                event_service = GovernanceEventService(self.store, clock=lambda: recovered_at)
                execution_roots: list[LegacyForwardRecoveryExecutionRoot] = []
                for root, (_ref, next_object) in zip(
                    current_roots,
                    objects,
                    strict=True,
                ):
                    row = repository._select(connection, root.proposal_ref)
                    if row is None:
                        raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_PLAN_STALE")
                    current = repository._view(row, root.proposal_ref)
                    recovery_scope = repository._begin_legacy_forward_recovery_scope(
                        connection,
                        recovery_id=recovery_id,
                        migration_id=plan.migration_id,
                        proposal_refs=(root.proposal_ref,),
                    )
                    try:
                        updated = repository._activate_next_for_legacy_forward_recovery(
                            connection,
                            current,
                            root.active_definition_digest,
                            root.state_revision,
                            next_object.digest,
                            recovery_id=recovery_id,
                            migration_id=plan.migration_id,
                            recovery_scope=recovery_scope,
                        )
                    finally:
                        repository._end_legacy_forward_recovery_scope(recovery_scope)
                    item_id = self._identifier("LFI")
                    payload = LegacyForwardRecoveryProjectionPayload(
                        aggregate_ref=root.proposal_ref,
                        migration_id=plan.migration_id,
                        recovery_id=recovery_id,
                        recovery_root_digest=plan.recovery_root_digest,
                        reason=reason.strip(),
                        previous_definition_digest=root.active_definition_digest,
                        previous_content_revision=root.content_revision,
                        previous_state_revision=root.state_revision,
                        previous_decision_epoch=root.decision_epoch,
                        previous_status=root.status.value,
                        next_definition_digest=updated.active_definition_digest,
                        next_content_revision=updated.content_revision,
                        next_state_revision=updated.state_revision,
                        next_decision_epoch=updated.decision_epoch,
                    )
                    payload_json = _canonical_json(payload.model_dump(mode="json"))
                    payload_digest = _digest(payload.model_dump(mode="json"))
                    connection.execute(
                        """
                        INSERT INTO governance_legacy_forward_recovery_items(
                            item_id, recovery_id, migration_id, project_namespace,
                            project_id, proposal_id, previous_definition_digest,
                            previous_content_revision, previous_state_revision,
                            previous_decision_epoch, previous_status,
                            next_definition_digest, next_content_revision,
                            next_state_revision, next_decision_epoch, next_status,
                            payload_digest, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item_id,
                            recovery_id,
                            plan.migration_id,
                            root.proposal_ref.project_ref.namespace,
                            root.proposal_ref.project_ref.project_id,
                            root.proposal_ref.proposal_id,
                            root.active_definition_digest,
                            root.content_revision,
                            root.state_revision,
                            root.decision_epoch,
                            root.status.value,
                            updated.active_definition_digest,
                            updated.content_revision,
                            updated.state_revision,
                            updated.decision_epoch,
                            updated.status.value,
                            payload_digest,
                            payload_json,
                            timestamp,
                        ),
                    )
                    audit, outbox = event_service._append_legacy_forward_recovery_in_transaction(
                        connection,
                        root.proposal_ref,
                        item_id=item_id,
                        payload=payload,
                    )
                    execution_roots.append(
                        LegacyForwardRecoveryExecutionRoot(
                            item_id=item_id,
                            proposal_ref=root.proposal_ref,
                            previous_definition_digest=root.active_definition_digest,
                            previous_content_revision=root.content_revision,
                            previous_state_revision=root.state_revision,
                            previous_decision_epoch=root.decision_epoch,
                            previous_status=root.status,
                            next_definition_digest=updated.active_definition_digest,
                            next_content_revision=updated.content_revision,
                            next_state_revision=updated.state_revision,
                            next_decision_epoch=updated.decision_epoch,
                            audit_event_id=audit.event_id,
                            outbox_event_ids=tuple(event.event_id for event in outbox),
                        )
                    )
                result = self._result(
                    recovery_id=recovery_id,
                    plan=plan,
                    roots=tuple(execution_roots),
                    actor_ref=context.actor_ref,
                    recovered_at=recovered_at,
                )
                connection.execute(
                    """
                    INSERT INTO governance_legacy_forward_recovery_results(
                        recovery_id, migration_id, result_json, result_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        recovery_id,
                        plan.migration_id,
                        _canonical_json(result.model_dump(mode="json")),
                        result.result_digest,
                        timestamp,
                    ),
                )
                GovernanceEventService.reconcile_connection(connection)
                self.reconcile_roots(connection)
                return result
        except LegacyMigrationLifecycleError:
            raise
        except AuthorityResolutionError as error:
            raise LegacyMigrationLifecycleError(error.code) from error
        except GovernanceCommitAmbiguousError:
            try:
                return self._reconcile_ambiguous(
                    plan,
                    fingerprint=fingerprint,
                    authority_request=authority_request,
                    reason=reason.strip(),
                    idempotency_key=idempotency_key,
                )
            except LegacyMigrationLifecycleError:
                raise
            except AuthorityResolutionError as error:
                raise LegacyMigrationLifecycleError(error.code) from error
            except (GovernanceEventError, sqlite3.Error, ValueError) as error:
                raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_AMBIGUOUS") from error
        except (
            ActiveProposalError,
            DefinitionObjectStoreError,
            GovernanceEventError,
            sqlite3.Error,
            ValueError,
        ) as error:
            raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_CONFLICT") from error

    @staticmethod
    def _objects(
        plan: LegacyForwardRecoveryPlan,
        next_objects: Sequence[DefinitionObjectRef],
    ) -> tuple[tuple[ProposalRef, DefinitionObjectRef], ...]:
        mapping: dict[ProposalRef, DefinitionObjectRef] = {}
        for value in next_objects:
            if value.object_kind != "definition" or value.proposal_ref in mapping:
                raise ValueError("forward recovery definition scope가 유효하지 않습니다.")
            mapping[value.proposal_ref] = value
        expected = tuple(root.proposal_ref for root in plan.roots)
        if set(mapping) != set(expected):
            raise ValueError("forward recovery definition scope가 plan과 일치하지 않습니다.")
        return tuple((ref, mapping[ref]) for ref in expected)

    @staticmethod
    def _request_fingerprint(
        plan: LegacyForwardRecoveryPlan,
        objects: tuple[tuple[ProposalRef, DefinitionObjectRef], ...],
    ) -> str:
        return _digest(
            {
                "next_definitions": [
                    {
                        "definition_digest": value.digest,
                        "proposal_ref": ref.model_dump(mode="json"),
                    }
                    for ref, value in objects
                ],
                "recovery_root_digest": plan.recovery_root_digest,
            }
        ).removeprefix("sha256:")

    @staticmethod
    def _require_authority(
        context: AuthorityContext,
        request: DirectAuthorityRequest,
        project_ref: ProjectRef,
    ) -> None:
        if (
            context.project_ref != request.project_ref
            or context.project_ref != project_ref
            or context.actor_ref.actor_type is not ActorType.HUMAN
            or AuthorityPermission.ACTIVATION_MANAGE not in context.permissions
        ):
            raise LegacyMigrationLifecycleError("AUTHORITY_DENIED")

    @staticmethod
    def _require_head(
        plan: LegacyForwardRecoveryPlan,
        context: AuthorityContext,
        head: sqlite3.Row | tuple[object, ...],
    ) -> None:
        if (str(head[0]), str(head[1])) != (
            context.project_ref.namespace,
            context.project_ref.project_id,
        ):
            raise LegacyMigrationLifecycleError("PROJECT_SCOPE_MISMATCH")
        if (
            LegacyMigrationLifecycleState(str(head[2]))
            is not LegacyMigrationLifecycleState.ACTIVATED
        ):
            raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_REQUIRES_ACTIVATION")
        if int(str(head[3])) != plan.expected_lifecycle_revision:
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_LIFECYCLE_CONFLICT")
        if str(head[4]) != plan.activation_event_digest:
            raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_PLAN_STALE")

    @staticmethod
    def _require_exact_plan(
        plan: LegacyForwardRecoveryPlan,
        roots: tuple[LegacyForwardRecoveryRoot, ...],
    ) -> None:
        if plan.roots != roots or plan.recovery_root_digest != _digest(plan.digest_preimage()):
            raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_PLAN_STALE")

    @staticmethod
    def _result(
        *,
        recovery_id: str,
        plan: LegacyForwardRecoveryPlan,
        roots: tuple[LegacyForwardRecoveryExecutionRoot, ...],
        actor_ref: ActorRef,
        recovered_at: datetime,
    ) -> LegacyForwardRecoveryResult:
        provisional = LegacyForwardRecoveryResult.model_construct(
            recovery_id=recovery_id,
            migration_id=plan.migration_id,
            project_ref=plan.project_ref,
            recovery_root_digest=plan.recovery_root_digest,
            expected_lifecycle_revision=plan.expected_lifecycle_revision,
            activation_event_digest=plan.activation_event_digest,
            roots=roots,
            recovered_proposal_count=len(roots),
            actor_ref=actor_ref,
            recovered_at=recovered_at,
            result_digest=f"sha256:{'0' * 64}",
            replayed=False,
        )
        preimage = provisional.model_dump(mode="json", exclude={"result_digest", "replayed"})
        return LegacyForwardRecoveryResult(
            recovery_id=recovery_id,
            migration_id=plan.migration_id,
            project_ref=plan.project_ref,
            recovery_root_digest=plan.recovery_root_digest,
            expected_lifecycle_revision=plan.expected_lifecycle_revision,
            activation_event_digest=plan.activation_event_digest,
            roots=roots,
            recovered_proposal_count=len(roots),
            actor_ref=actor_ref,
            recovered_at=recovered_at,
            result_digest=_digest(preimage),
        )

    @staticmethod
    def _replay(
        connection: sqlite3.Connection,
        plan: LegacyForwardRecoveryPlan,
        *,
        fingerprint: str,
        idempotency_key: str,
        actor_ref: ActorRef,
        request_id: str,
        channel_json: str,
        reason: str,
    ) -> LegacyForwardRecoveryResult | None:
        row = connection.execute(
            """
            SELECT c.migration_id, c.project_namespace, c.project_id,
                   c.request_fingerprint, c.recovery_root_digest,
                   c.expected_lifecycle_revision, c.activation_event_digest,
                   c.actor_id, c.actor_type, c.request_id, c.channel_json, c.reason,
                   r.result_json, r.result_digest
            FROM governance_legacy_forward_recovery_commands c
            LEFT JOIN governance_legacy_forward_recovery_results r
              ON r.recovery_id = c.recovery_id
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
            fingerprint,
            plan.recovery_root_digest,
            str(plan.expected_lifecycle_revision),
            plan.activation_event_digest,
            actor_ref.actor_id,
            actor_ref.actor_type.value,
            request_id,
            channel_json,
            reason,
        )
        if tuple(str(value) for value in row[:12]) != expected or row[12] is None:
            raise LegacyMigrationLifecycleError("IDEMPOTENCY_CONFLICT")
        try:
            result = LegacyForwardRecoveryResult.model_validate_json(str(row[12]))
        except ValueError as error:
            raise LegacyMigrationLifecycleError(
                "LEGACY_FORWARD_RECOVERY_RESULT_MISMATCH"
            ) from error
        if result.result_digest != str(row[13]):
            raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_RESULT_MISMATCH")
        return result.model_copy(update={"replayed": True})

    def _reconcile_ambiguous(
        self,
        plan: LegacyForwardRecoveryPlan,
        *,
        fingerprint: str,
        authority_request: DirectAuthorityRequest,
        reason: str,
        idempotency_key: str,
    ) -> LegacyForwardRecoveryResult:
        with self.store.connect() as connection:
            context = self.authority.authenticate(authority_request, connection=connection)
            self._require_authority(context, authority_request, plan.project_ref)
            LegacyProposalImportService.reconcile_verification_roots(connection)
            LegacyMigrationActivationService.reconcile_roots(connection)
            GovernanceEventService.reconcile_connection(connection)
            self.reconcile_roots(connection)
            replay = self._replay(
                connection,
                plan,
                fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                actor_ref=context.actor_ref,
                request_id=context.source.request_id,
                channel_json=_canonical_json(context.source.channel.model_dump(mode="json")),
                reason=reason,
            )
            if replay is None:
                raise LegacyMigrationLifecycleError("LEGACY_FORWARD_RECOVERY_AMBIGUOUS")
            return replay

    @staticmethod
    def reconcile_roots(connection: sqlite3.Connection) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
                "AND name = 'governance_legacy_forward_recovery_commands'"
            ).fetchone()
            is None
        ):
            return
        commands = connection.execute(
            """
            SELECT c.recovery_id, c.migration_id, c.project_namespace, c.project_id,
                   c.request_fingerprint, c.recovery_root_digest,
                   c.expected_lifecycle_revision, c.activation_event_digest,
                   c.actor_id, c.actor_type, c.occurred_at,
                   r.result_json, r.result_digest
            FROM governance_legacy_forward_recovery_commands c
            LEFT JOIN governance_legacy_forward_recovery_results r
              ON r.recovery_id = c.recovery_id AND r.migration_id = c.migration_id
            ORDER BY c.recovery_id
            """
        ).fetchall()
        for command in commands:
            if command[11] is None:
                raise GovernanceEventError("LEGACY_FORWARD_RECOVERY_RESULT_MISMATCH")
            try:
                result = LegacyForwardRecoveryResult.model_validate_json(str(command[11]))
            except ValueError as error:
                raise GovernanceEventError("LEGACY_FORWARD_RECOVERY_RESULT_MISMATCH") from error
            rows = connection.execute(
                """
                SELECT i.item_id, i.proposal_id, i.previous_definition_digest,
                       i.previous_content_revision, i.previous_state_revision,
                       i.previous_decision_epoch, i.previous_status,
                       i.next_definition_digest, i.next_content_revision,
                       i.next_state_revision, i.next_decision_epoch, i.next_status,
                       a.event_id, o.event_id
                FROM governance_legacy_forward_recovery_items i
                JOIN governance_definition_revisions d
                  ON d.project_namespace = i.project_namespace
                 AND d.project_id = i.project_id AND d.proposal_id = i.proposal_id
                 AND d.content_revision = i.next_content_revision
                 AND d.definition_digest = i.next_definition_digest
                 AND d.previous_definition_digest = i.previous_definition_digest
                 AND d.activated_from_status = i.previous_status
                JOIN governance_audit_events a ON a.command_id = i.item_id
                JOIN governance_outbox_events o
                  ON o.project_namespace = a.project_namespace
                 AND o.project_id = a.project_id AND o.proposal_id = a.proposal_id
                 AND o.aggregate_sequence = a.aggregate_sequence
                WHERE i.recovery_id = ?
                ORDER BY i.proposal_id, o.event_id
                """,
                (command[0],),
            ).fetchall()
            grouped: dict[str, list[tuple[object, ...]]] = {}
            for row in rows:
                grouped.setdefault(str(row[0]), []).append(tuple(row))
            roots = tuple(
                LegacyForwardRecoveryExecutionRoot.model_validate(
                    {
                        "item_id": item_rows[0][0],
                        "proposal_ref": {
                            "project_ref": {
                                "namespace": str(command[2]),
                                "project_id": str(command[3]),
                            },
                            "proposal_id": str(item_rows[0][1]),
                        },
                        "previous_definition_digest": item_rows[0][2],
                        "previous_content_revision": item_rows[0][3],
                        "previous_state_revision": item_rows[0][4],
                        "previous_decision_epoch": item_rows[0][5],
                        "previous_status": item_rows[0][6],
                        "next_definition_digest": item_rows[0][7],
                        "next_content_revision": item_rows[0][8],
                        "next_state_revision": item_rows[0][9],
                        "next_decision_epoch": item_rows[0][10],
                        "next_status": item_rows[0][11],
                        "audit_event_id": item_rows[0][12],
                        "outbox_event_ids": tuple(str(row[13]) for row in item_rows),
                    }
                )
                for item_rows in sorted(
                    grouped.values(),
                    key=lambda values: str(values[0][1]),
                )
            )
            expected_fingerprint = _digest(
                {
                    "next_definitions": [
                        {
                            "definition_digest": root.next_definition_digest,
                            "proposal_ref": root.proposal_ref.model_dump(mode="json"),
                        }
                        for root in roots
                    ],
                    "recovery_root_digest": str(command[5]),
                }
            ).removeprefix("sha256:")
            if (
                not roots
                or result.recovery_id != str(command[0])
                or result.migration_id != str(command[1])
                or result.project_ref
                != ProjectRef(namespace=str(command[2]), project_id=str(command[3]))
                or result.recovery_root_digest != str(command[5])
                or result.expected_lifecycle_revision != int(str(command[6]))
                or result.activation_event_digest != str(command[7])
                or result.actor_ref
                != ActorRef(actor_id=str(command[8]), actor_type=ActorType(str(command[9])))
                or LegacyMigrationForwardRecoveryExecutor._timestamp(result.recovered_at)
                != str(command[10])
                or result.roots != roots
                or result.result_digest != str(command[12])
                or expected_fingerprint != str(command[4])
            ):
                raise GovernanceEventError("LEGACY_FORWARD_RECOVERY_RESULT_MISMATCH")

    @staticmethod
    def _identifier(prefix: str) -> str:
        return f"{prefix}-{secrets.token_hex(8).upper()}"

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
