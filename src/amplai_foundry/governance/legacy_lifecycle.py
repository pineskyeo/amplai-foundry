"""Append-only activation boundary for imported legacy migrations."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    AuthorityService,
    DirectAuthorityRequest,
)
from amplai_foundry.governance.events import GovernanceEventError, GovernanceEventService
from amplai_foundry.governance.legacy_migration import LegacyProposalImportService
from amplai_foundry.governance.models import ActorRef, ActorType, AuthorityPermission, Digest
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


class LegacyMigrationLifecycleError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LegacyMigrationLifecycleState(StrEnum):
    IMPORTED = "imported"
    STAGED_VERIFIED = "staged_verified"
    ACTIVATED = "activated"
    ROLLED_BACK = "rolled_back"
    RECOVERY_HOLD = "recovery_hold"


class LegacyMigrationActivationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(pattern=r"^LMC-[A-F0-9]{16}$")
    event_id: str = Field(pattern=r"^LME-[A-F0-9]{16}$")
    migration_id: str = Field(pattern=r"^MPL-[A-F0-9]{16}$")
    project_ref: ProjectRef
    state: LegacyMigrationLifecycleState = LegacyMigrationLifecycleState.ACTIVATED
    lifecycle_revision: int = Field(ge=2)
    verification_id: str = Field(pattern=r"^MVF-[A-F0-9]{16}$")
    report_digest: Digest
    actor_ref: ActorRef
    activated_at: AwareDatetime
    result_digest: Digest
    replayed: bool = False

    @model_validator(mode="after")
    def validate_result_digest(self) -> LegacyMigrationActivationResult:
        preimage = self.model_dump(
            mode="json",
            exclude={"result_digest", "replayed"},
        )
        if self.result_digest != _digest(preimage):
            raise ValueError("legacy activation result digest가 일치하지 않습니다.")
        return self


class LegacyMigrationActivationService:
    """Authenticate and atomically commit the irreversible migration activation cutoff."""

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

    def activate(
        self,
        migration_id: str,
        *,
        authority_request: DirectAuthorityRequest,
        expected_lifecycle_revision: int,
        reason: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> LegacyMigrationActivationResult:
        if expected_lifecycle_revision < 1:
            raise ValueError("expected_lifecycle_revision은 1 이상이어야 합니다.")
        if (
            len(reason.strip()) < 3
            or not idempotency_key.strip()
            or not request_fingerprint.strip()
        ):
            raise ValueError("activation reason과 idempotency identity가 필요합니다.")
        try:
            with self.store.connect() as connection, governance_transaction(connection):
                context = self.authority.authenticate(authority_request, connection=connection)
                if (
                    context.project_ref != authority_request.project_ref
                    or context.actor_ref.actor_type is not ActorType.HUMAN
                    or AuthorityPermission.ACTIVATION_MANAGE not in context.permissions
                ):
                    raise LegacyMigrationLifecycleError("AUTHORITY_DENIED")
                channel_json = _canonical_json(context.source.channel.model_dump(mode="json"))
                replay = self._replay(
                    connection,
                    migration_id,
                    context.project_ref,
                    idempotency_key,
                    request_fingerprint,
                    expected_lifecycle_revision,
                    actor_ref=context.actor_ref,
                    request_id=context.source.request_id,
                    channel_json=channel_json,
                    reason=reason.strip(),
                )
                if replay is not None:
                    return replay
                GovernanceEventService.reconcile_connection(connection)
                LegacyProposalImportService.reconcile_verification_roots(connection)
                head = connection.execute(
                    """
                    SELECT project_namespace, project_id, verification_id, report_digest,
                           state, lifecycle_revision, last_event_digest
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
                if state is LegacyMigrationLifecycleState.IMPORTED:
                    raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_NOT_VERIFIED")
                if state is LegacyMigrationLifecycleState.ROLLED_BACK:
                    raise LegacyMigrationLifecycleError(
                        "LEGACY_MIGRATION_ACTIVATION_AFTER_ROLLBACK"
                    )
                if state is LegacyMigrationLifecycleState.ACTIVATED:
                    raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ALREADY_ACTIVATED")
                if state is LegacyMigrationLifecycleState.RECOVERY_HOLD:
                    raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_RECOVERY_REQUIRED")
                if int(head[5]) != expected_lifecycle_revision:
                    raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_LIFECYCLE_CONFLICT")
                activated_at = self._aware(self._clock())
                timestamp = self._timestamp(activated_at)
                command_id = self._identifier("LMC")
                event_id = self._identifier("LME")
                command_values = (
                    command_id,
                    migration_id,
                    context.project_ref.namespace,
                    context.project_ref.project_id,
                    "activate",
                    expected_lifecycle_revision,
                    idempotency_key,
                    request_fingerprint,
                    context.actor_ref.actor_id,
                    context.actor_ref.actor_type.value,
                    context.source.request_id,
                    channel_json,
                    reason.strip(),
                    timestamp,
                )
                connection.execute(
                    """
                    INSERT INTO governance_legacy_migration_lifecycle_commands(
                        command_id, migration_id, project_namespace, project_id,
                        action, expected_lifecycle_revision, idempotency_key,
                        request_fingerprint, actor_id, actor_type, request_id,
                        channel_json, reason, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    command_values,
                )
                event_preimage = {
                    "command_id": command_id,
                    "event_id": event_id,
                    "migration_id": migration_id,
                    "lifecycle_sequence": expected_lifecycle_revision,
                    "before_state": state.value,
                    "after_state": LegacyMigrationLifecycleState.ACTIVATED.value,
                    "verification_id": str(head[2]),
                    "report_digest": str(head[3]),
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
                        migration_id,
                        expected_lifecycle_revision,
                        state.value,
                        LegacyMigrationLifecycleState.ACTIVATED.value,
                        str(head[2]),
                        str(head[3]),
                        head[6],
                        event_digest,
                        timestamp,
                    ),
                )
                result = self._result(
                    command_id=command_id,
                    event_id=event_id,
                    migration_id=migration_id,
                    project_ref=context.project_ref,
                    lifecycle_revision=expected_lifecycle_revision + 1,
                    verification_id=str(head[2]),
                    report_digest=str(head[3]),
                    actor_ref=context.actor_ref,
                    activated_at=activated_at,
                )
                result_json = _canonical_json(result.model_dump(mode="json"))
                connection.execute(
                    """
                    INSERT INTO governance_legacy_migration_lifecycle_results(
                        command_id, migration_id, result_json, result_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (command_id, migration_id, result_json, result.result_digest, timestamp),
                )
                updated = connection.execute(
                    """
                    UPDATE governance_legacy_migration_lifecycle_heads
                    SET state = 'activated', lifecycle_revision = lifecycle_revision + 1,
                        last_event_digest = ?, updated_at = ?
                    WHERE migration_id = ? AND state = 'staged_verified'
                      AND lifecycle_revision = ? AND verification_id = ?
                      AND report_digest = ?
                    """,
                    (
                        event_digest,
                        timestamp,
                        migration_id,
                        expected_lifecycle_revision,
                        str(head[2]),
                        str(head[3]),
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
                context = self.authority.authenticate(authority_request)
            except AuthorityResolutionError as error:
                raise LegacyMigrationLifecycleError(error.code) from error
            if (
                context.actor_ref.actor_type is not ActorType.HUMAN
                or AuthorityPermission.ACTIVATION_MANAGE not in context.permissions
            ):
                raise LegacyMigrationLifecycleError("AUTHORITY_DENIED") from None
            replay = self._reconcile_ambiguous(
                migration_id,
                context.project_ref,
                idempotency_key,
                request_fingerprint,
                expected_lifecycle_revision,
                actor_ref=context.actor_ref,
                request_id=context.source.request_id,
                channel_json=_canonical_json(context.source.channel.model_dump(mode="json")),
                reason=reason.strip(),
            )
            if replay is None:
                raise LegacyMigrationLifecycleError(
                    "LEGACY_MIGRATION_ACTIVATION_AMBIGUOUS"
                ) from None
            return replay
        except (GovernanceEventError, sqlite3.Error, ValueError) as error:
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ACTIVATION_CONFLICT") from error

    @staticmethod
    def reconcile_roots(connection: sqlite3.Connection) -> None:
        """Fail startup when lifecycle head, command, event, or result roots diverge."""

        if (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' "
                "AND name = 'governance_legacy_migration_lifecycle_heads'"
            ).fetchone()
            is None
        ):
            return
        heads = connection.execute(
            """
            SELECT h.migration_id, h.project_namespace, h.project_id,
                   h.verification_id, h.report_digest, h.state,
                   h.lifecycle_revision, h.last_event_digest,
                   m.status, v.report_digest
            FROM governance_legacy_migration_lifecycle_heads h
            JOIN governance_legacy_migrations m
              ON m.migration_id = h.migration_id
             AND m.project_namespace = h.project_namespace
             AND m.project_id = h.project_id
            LEFT JOIN governance_legacy_migration_verifications v
              ON v.verification_id = h.verification_id
             AND v.migration_id = h.migration_id
            ORDER BY h.migration_id
            """
        ).fetchall()
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM governance_legacy_migrations WHERE status = 'state_imported'"
        ).fetchone()
        if migration_count is None or len(heads) != int(migration_count[0]):
            raise GovernanceEventError("LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH")
        for head in heads:
            state = LegacyMigrationLifecycleState(str(head[5]))
            if str(head[8]) != "state_imported":
                raise GovernanceEventError("LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH")
            if head[3] is None:
                if (
                    state
                    not in {
                        LegacyMigrationLifecycleState.IMPORTED,
                        LegacyMigrationLifecycleState.ACTIVATED,
                    }
                    or head[4] is not None
                    or head[9] is not None
                ):
                    raise GovernanceEventError("LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH")
            elif str(head[4]) != str(head[9]):
                raise GovernanceEventError("LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH")
            rows = connection.execute(
                """
                SELECT c.command_id, c.action, c.expected_lifecycle_revision,
                       c.actor_id, c.actor_type, c.occurred_at,
                       e.event_id, e.lifecycle_sequence, e.before_state,
                       e.after_state, e.verification_id, e.report_digest,
                       e.previous_event_digest, e.event_digest, e.occurred_at,
                       r.result_json, r.result_digest, r.created_at
                FROM governance_legacy_migration_lifecycle_commands c
                LEFT JOIN governance_legacy_migration_lifecycle_events e
                  ON e.command_id = c.command_id
                LEFT JOIN governance_legacy_migration_lifecycle_results r
                  ON r.command_id = c.command_id
                WHERE c.migration_id = ?
                ORDER BY e.lifecycle_sequence
                """,
                (head[0],),
            ).fetchall()
            if not rows:
                if head[7] is not None or int(head[6]) not in {1, 2}:
                    raise GovernanceEventError("LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH")
                continue
            if len(rows) != 1 or state is not LegacyMigrationLifecycleState.ACTIVATED:
                raise GovernanceEventError("LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH")
            row = rows[0]
            required_indices = (*range(6, 12), *range(13, 18))
            if any(row[index] is None for index in required_indices):
                raise GovernanceEventError("LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH")
            try:
                result = LegacyMigrationActivationResult.model_validate_json(str(row[15]))
            except ValueError as error:
                raise GovernanceEventError("LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH") from error
            event_preimage = {
                "command_id": str(row[0]),
                "event_id": str(row[6]),
                "migration_id": str(head[0]),
                "lifecycle_sequence": int(row[7]),
                "before_state": str(row[8]),
                "after_state": str(row[9]),
                "verification_id": str(row[10]),
                "report_digest": str(row[11]),
                "previous_event_digest": row[12],
                "occurred_at": str(row[14]),
            }
            if (
                str(row[1]) != "activate"
                or int(row[2]) != int(row[7])
                or str(row[8]) != LegacyMigrationLifecycleState.STAGED_VERIFIED.value
                or str(row[9]) != LegacyMigrationLifecycleState.ACTIVATED.value
                or str(row[10]) != str(head[3])
                or str(row[11]) != str(head[4])
                or _digest(event_preimage) != str(row[13])
                or str(head[7]) != str(row[13])
                or int(head[6]) != int(row[7]) + 1
                or str(row[5]) != str(row[14])
                or str(row[16]) != result.result_digest
                or str(row[17]) != str(row[5])
                or result.command_id != str(row[0])
                or result.event_id != str(row[6])
                or result.migration_id != str(head[0])
                or result.project_ref.namespace != str(head[1])
                or result.project_ref.project_id != str(head[2])
                or result.actor_ref.actor_id != str(row[3])
                or result.actor_ref.actor_type.value != str(row[4])
                or result.lifecycle_revision != int(head[6])
                or result.verification_id != str(head[3])
                or result.report_digest != str(head[4])
            ):
                raise GovernanceEventError("LEGACY_MIGRATION_LIFECYCLE_ROOT_MISMATCH")

    def _reconcile_ambiguous(
        self,
        migration_id: str,
        project_ref: ProjectRef,
        idempotency_key: str,
        request_fingerprint: str,
        expected_lifecycle_revision: int,
        *,
        actor_ref: ActorRef,
        request_id: str,
        channel_json: str,
        reason: str,
    ) -> LegacyMigrationActivationResult | None:
        with self.store.connect() as connection:
            return self._replay(
                connection,
                migration_id,
                project_ref,
                idempotency_key,
                request_fingerprint,
                expected_lifecycle_revision,
                actor_ref=actor_ref,
                request_id=request_id,
                channel_json=channel_json,
                reason=reason,
            )

    @staticmethod
    def _replay(
        connection: sqlite3.Connection,
        migration_id: str,
        project_ref: ProjectRef,
        idempotency_key: str,
        request_fingerprint: str,
        expected_lifecycle_revision: int,
        *,
        actor_ref: ActorRef,
        request_id: str,
        channel_json: str,
        reason: str,
    ) -> LegacyMigrationActivationResult | None:
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
            migration_id,
            project_ref.namespace,
            project_ref.project_id,
            "activate",
            expected_lifecycle_revision,
            request_fingerprint,
            actor_ref.actor_id,
            actor_ref.actor_type.value,
            request_id,
            channel_json,
            reason,
        )
        if tuple(row[:11]) != expected or row[11] is None:
            raise LegacyMigrationLifecycleError("IDEMPOTENCY_CONFLICT")
        result = LegacyMigrationActivationResult.model_validate_json(str(row[11]))
        if result.result_digest != str(row[12]):
            raise LegacyMigrationLifecycleError("LEGACY_MIGRATION_ACTIVATION_EVIDENCE_MISMATCH")
        return result.model_copy(update={"replayed": True})

    @staticmethod
    def _result(
        *,
        command_id: str,
        event_id: str,
        migration_id: str,
        project_ref: ProjectRef,
        lifecycle_revision: int,
        verification_id: str,
        report_digest: str,
        actor_ref: ActorRef,
        activated_at: datetime,
    ) -> LegacyMigrationActivationResult:
        provisional = LegacyMigrationActivationResult.model_construct(
            command_id=command_id,
            event_id=event_id,
            migration_id=migration_id,
            project_ref=project_ref,
            state=LegacyMigrationLifecycleState.ACTIVATED,
            lifecycle_revision=lifecycle_revision,
            verification_id=verification_id,
            report_digest=report_digest,
            actor_ref=actor_ref,
            activated_at=activated_at,
            result_digest=f"sha256:{'0' * 64}",
            replayed=False,
        )
        preimage = provisional.model_dump(
            mode="json",
            exclude={"result_digest", "replayed"},
        )
        return LegacyMigrationActivationResult(
            command_id=command_id,
            event_id=event_id,
            migration_id=migration_id,
            project_ref=project_ref,
            lifecycle_revision=lifecycle_revision,
            verification_id=verification_id,
            report_digest=report_digest,
            actor_ref=actor_ref,
            activated_at=activated_at,
            result_digest=_digest(preimage),
        )

    @staticmethod
    def _identifier(prefix: str) -> str:
        return f"{prefix}-{secrets.token_hex(8).upper()}"

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp는 timezone-aware여야 합니다.")
        return value

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
