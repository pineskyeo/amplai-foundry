"""Durable, secret-minimized Provider ingress with fenced worker leases."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, ValidationError

from amplai_foundry.governance.decisions import DecisionAction
from amplai_foundry.governance.models import ChannelProvider, ChannelRef, Digest
from amplai_foundry.governance.store import (
    GovernanceCommitAmbiguousError,
    GovernanceStore,
    GovernanceStoreError,
    GovernanceTransactionError,
    governance_transaction,
)


def _system_now() -> datetime:
    return datetime.now(UTC)


class IngressError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class IngressLeaseConflictError(IngressError):
    pass


_COMMAND_COLUMNS = (
    "command_id, provider, provider_installation_ref, "
    "provider_fingerprint, raw_body_digest, external_event_id, "
    "external_actor_key, channel_json, credential_kind, credential_id, "
    "action, received_at, state, attempts, "
    "claim_generation, lease_owner, lease_expires_at, retry_at, "
    "completed_at, last_error_code"
)


class IngressState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"
    RETRY_WAIT = "retry_wait"
    RECOVERY_HOLD = "recovery_hold"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class ProviderEnvelope:
    provider: ChannelProvider
    provider_installation_ref: str
    raw_body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


class VerifiedProviderCommand(BaseModel):
    """Authenticator output. The raw credential exists only in process memory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_event_id: str = Field(min_length=1)
    external_actor_key: str = Field(min_length=1)
    channel_ref: ChannelRef
    credential_id: str = Field(pattern=r"^TOK-[A-F0-9]{16}$")
    raw_credential: SecretStr
    action: DecisionAction


class ProviderAuthenticator(Protocol):
    def verify(self, envelope: ProviderEnvelope) -> VerifiedProviderCommand: ...


@dataclass(frozen=True, slots=True)
class IngressConfig:
    max_raw_body_bytes: int = 1_048_576
    busy_timeout_ms: int = 2_500
    lease_duration: timedelta = timedelta(seconds=30)
    max_attempts: int = 5
    retry_base: timedelta = timedelta(seconds=5)
    retry_cap: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.max_raw_body_bytes < 1:
            raise ValueError("max_raw_body_bytes는 1 이상이어야 합니다.")
        if not 0 <= self.busy_timeout_ms <= 2_500:
            raise ValueError("ingress busy_timeout_ms는 0..2500 범위여야 합니다.")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration은 0보다 커야 합니다.")
        if self.max_attempts < 1:
            raise ValueError("max_attempts는 1 이상이어야 합니다.")
        if self.retry_base <= timedelta(0) or self.retry_cap < self.retry_base:
            raise ValueError("retry backoff 설정이 올바르지 않습니다.")


class IngressAck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    command_id: str | None = None
    duplicate: bool = False
    state: IngressState | None = None
    error_code: str | None = None


class IngressCommandView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    provider: ChannelProvider
    provider_installation_ref: str
    provider_fingerprint: str
    raw_body_digest: Digest
    external_event_id: str
    external_actor_key: str
    channel_ref: ChannelRef
    credential_kind: str
    credential_id: str
    action: DecisionAction
    received_at: AwareDatetime
    state: IngressState
    attempts: int = Field(ge=0)
    claim_generation: int = Field(ge=0)
    lease_owner: str | None = None
    lease_expires_at: AwareDatetime | None = None
    retry_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    last_error_code: str | None = None


class IngressService:
    """Authenticate, durably accept, lease, and finalize Provider commands."""

    def __init__(
        self,
        store: GovernanceStore,
        authenticator: ProviderAuthenticator,
        *,
        config: IngressConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.authenticator = authenticator
        self.config = config or IngressConfig()
        self._clock = clock or _system_now

    def accept(self, envelope: ProviderEnvelope) -> IngressAck:
        if len(envelope.raw_body) > self.config.max_raw_body_bytes:
            raise IngressError("INGRESS_PAYLOAD_TOO_LARGE")
        if not envelope.provider_installation_ref.strip():
            raise IngressError("PROVIDER_INSTALLATION_INVALID")

        try:
            verified = self.authenticator.verify(envelope)
        except ValidationError as error:
            raise IngressError("PROVIDER_PAYLOAD_INVALID") from error
        if verified.channel_ref.provider is not envelope.provider:
            raise IngressError("PROVIDER_SCOPE_MISMATCH")
        raw_credential = verified.raw_credential.get_secret_value()
        if verified.credential_id == raw_credential:
            raise IngressError("PROVIDER_PAYLOAD_INVALID")
        received_at = self._aware(self._clock())
        body_digest = self._digest(envelope.raw_body)
        credential_hash = self._digest(raw_credential.encode("utf-8"))
        fingerprint = self._fingerprint(
            envelope.provider,
            envelope.provider_installation_ref,
            verified.external_event_id,
            body_digest,
        )
        command_id = self._command_id()
        channel_json = self._channel_json(verified.channel_ref)
        ack: IngressAck
        try:
            with (
                self.store.connect(busy_timeout_ms=self.config.busy_timeout_ms) as connection,
                governance_transaction(connection),
            ):
                existing = connection.execute(
                    """
                    SELECT command_id, provider_fingerprint, state
                    FROM governance_ingress_commands
                    WHERE provider = ? AND provider_installation_ref = ?
                      AND external_event_id = ?
                    """,
                    (
                        envelope.provider.value,
                        envelope.provider_installation_ref,
                        verified.external_event_id,
                    ),
                ).fetchone()
                if existing is not None:
                    if str(existing[1]) != fingerprint:
                        raise IngressError("INGRESS_REPLAY_CONFLICT")
                    ack = IngressAck(
                        accepted=True,
                        command_id=str(existing[0]),
                        duplicate=True,
                        state=IngressState(str(existing[2])),
                    )
                else:
                    token = connection.execute(
                        """
                        SELECT 1 FROM governance_action_tokens
                        WHERE token_id = ? AND token_hash = ? AND allowed_action = ?
                        """,
                        (
                            verified.credential_id,
                            credential_hash,
                            verified.action.value,
                        ),
                    ).fetchone()
                    if token is None:
                        raise IngressError("ACTION_TOKEN_INVALID")
                    connection.execute(
                        """
                        INSERT INTO governance_ingress_commands(
                            command_id, provider, provider_installation_ref,
                            provider_fingerprint, raw_body_digest, external_event_id,
                            external_actor_key, channel_json, credential_kind,
                            credential_id, credential_hash, action, received_at, state,
                            attempts, claim_generation, lease_owner, lease_expires_at,
                            retry_at, completed_at, last_error_code
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, 'action_token', ?, ?, ?, ?, 'pending',
                            0, 0, NULL, NULL, NULL, NULL, NULL
                        )
                        """,
                        (
                            command_id,
                            envelope.provider.value,
                            envelope.provider_installation_ref,
                            fingerprint,
                            body_digest,
                            verified.external_event_id,
                            verified.external_actor_key,
                            channel_json,
                            verified.credential_id,
                            credential_hash,
                            verified.action.value,
                            self._timestamp(received_at),
                        ),
                    )
                    ack = IngressAck(
                        accepted=True,
                        command_id=command_id,
                        state=IngressState.PENDING,
                    )
        except GovernanceCommitAmbiguousError:
            return IngressAck(accepted=False, error_code="GOVERNANCE_COMMIT_AMBIGUOUS")
        except (GovernanceStoreError, GovernanceTransactionError, sqlite3.Error):
            return IngressAck(accepted=False, error_code="INGRESS_UNAVAILABLE")
        return ack

    def claim_next(self, worker_id: str) -> IngressCommandView | None:
        if not worker_id.strip():
            raise ValueError("worker_id는 비어 있을 수 없습니다.")
        now = self._aware(self._clock())
        lease_expires = now + self.config.lease_duration
        with self.store.connect() as connection, governance_transaction(connection):
            connection.execute(
                """
                UPDATE governance_ingress_commands
                SET state = 'retry_wait', lease_owner = NULL, lease_expires_at = NULL,
                    retry_at = ?, last_error_code = 'INGRESS_LEASE_EXPIRED'
                WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (self._timestamp(now), self._timestamp(now)),
            )
            connection.execute(
                """
                UPDATE governance_ingress_commands
                SET state = 'dead_letter', retry_at = NULL
                WHERE state = 'retry_wait' AND attempts >= ? AND retry_at <= ?
                """,
                (self.config.max_attempts, self._timestamp(now)),
            )
            row = connection.execute(
                """
                SELECT command_id, claim_generation
                FROM governance_ingress_commands
                WHERE
                    state = 'pending'
                    OR (state = 'retry_wait' AND attempts < ? AND retry_at <= ?)
                ORDER BY received_at, command_id
                LIMIT 1
                """,
                (
                    self.config.max_attempts,
                    self._timestamp(now),
                ),
            ).fetchone()
            if row is None:
                return None
            command_id = str(row[0])
            generation = int(row[1])
            updated = connection.execute(
                """
                UPDATE governance_ingress_commands
                SET state = 'leased', attempts = attempts + 1,
                    claim_generation = claim_generation + 1,
                    lease_owner = ?, lease_expires_at = ?, retry_at = NULL
                WHERE command_id = ? AND claim_generation = ?
                  AND (
                    state = 'pending'
                    OR (state = 'retry_wait' AND attempts < ? AND retry_at <= ?)
                  )
                """,
                (
                    worker_id,
                    self._timestamp(lease_expires),
                    command_id,
                    generation,
                    self.config.max_attempts,
                    self._timestamp(now),
                ),
            )
            if updated.rowcount != 1:
                raise IngressLeaseConflictError("INGRESS_LEASE_CONFLICT")
            claimed = self._select(connection, command_id)
            if claimed is None:
                raise IngressError("INGRESS_NOT_FOUND")
            return self._view(claimed)

    def complete(self, command_id: str, *, worker_id: str, generation: int) -> IngressCommandView:
        return self._finalize(
            command_id,
            worker_id=worker_id,
            generation=generation,
            next_state=IngressState.COMPLETED,
        )

    def retry(
        self,
        command_id: str,
        *,
        worker_id: str,
        generation: int,
        error_code: str,
    ) -> IngressCommandView:
        return self._finalize(
            command_id,
            worker_id=worker_id,
            generation=generation,
            next_state=IngressState.RETRY_WAIT,
            error_code=error_code,
        )

    def recovery_hold(
        self,
        command_id: str,
        *,
        worker_id: str,
        generation: int,
        error_code: str,
    ) -> IngressCommandView:
        return self._finalize(
            command_id,
            worker_id=worker_id,
            generation=generation,
            next_state=IngressState.RECOVERY_HOLD,
            error_code=error_code,
        )

    def get(self, command_id: str) -> IngressCommandView | None:
        with self.store.connect() as connection:
            row = self._select(connection, command_id)
        return self._view(row) if row is not None else None

    def stranded(self, *, limit: int = 100) -> tuple[IngressCommandView, ...]:
        """List commands no worker will claim again. Operator recovery reads this."""

        if limit < 1:
            raise ValueError("limit은 1 이상이어야 합니다.")
        with self.store.connect() as connection:
            rows = connection.execute(
                f"SELECT {_COMMAND_COLUMNS} "
                "FROM governance_ingress_commands "
                "WHERE state IN ('dead_letter', 'recovery_hold') "
                "ORDER BY received_at, command_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._view(cast(tuple[object, ...], row)) for row in rows)

    def _finalize(
        self,
        command_id: str,
        *,
        worker_id: str,
        generation: int,
        next_state: IngressState,
        error_code: str | None = None,
    ) -> IngressCommandView:
        if next_state not in {
            IngressState.COMPLETED,
            IngressState.RETRY_WAIT,
            IngressState.RECOVERY_HOLD,
        }:
            raise ValueError("leased command의 finalize state가 올바르지 않습니다.")
        now = self._aware(self._clock())
        with self.store.connect() as connection, governance_transaction(connection):
            row = self._select(connection, command_id)
            if row is None:
                raise IngressError("INGRESS_NOT_FOUND")
            view = self._view(row)
            if (
                view.state is not IngressState.LEASED
                or view.lease_owner != worker_id
                or view.claim_generation != generation
                or view.lease_expires_at is None
                or view.lease_expires_at <= now
            ):
                raise IngressLeaseConflictError("INGRESS_LEASE_CONFLICT")
            retry_at = (
                self._timestamp(now + self._backoff(view.attempts))
                if next_state is IngressState.RETRY_WAIT
                else None
            )
            completed_at = self._timestamp(now) if next_state is IngressState.COMPLETED else None
            updated = connection.execute(
                """
                UPDATE governance_ingress_commands
                SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                    retry_at = ?, completed_at = ?, last_error_code = ?
                WHERE command_id = ? AND state = 'leased'
                  AND lease_owner = ? AND claim_generation = ? AND lease_expires_at > ?
                """,
                (
                    next_state.value,
                    retry_at,
                    completed_at,
                    error_code,
                    command_id,
                    worker_id,
                    generation,
                    self._timestamp(now),
                ),
            )
            if updated.rowcount != 1:
                raise IngressLeaseConflictError("INGRESS_LEASE_CONFLICT")
            result = self._select(connection, command_id)
            if result is None:
                raise IngressError("INGRESS_NOT_FOUND")
            return self._view(result)

    def _backoff(self, attempts: int) -> timedelta:
        multiplier = 2 ** max(attempts - 1, 0)
        delay = timedelta(seconds=self.config.retry_base.total_seconds() * multiplier)
        return min(delay, self.config.retry_cap)

    @staticmethod
    def _select(connection: sqlite3.Connection, command_id: str) -> tuple[object, ...] | None:
        return cast(
            tuple[object, ...] | None,
            connection.execute(
                f"SELECT {_COMMAND_COLUMNS} FROM governance_ingress_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone(),
        )

    @staticmethod
    def _view(row: tuple[object, ...]) -> IngressCommandView:
        return IngressCommandView.model_validate(
            {
                "command_id": row[0],
                "provider": row[1],
                "provider_installation_ref": row[2],
                "provider_fingerprint": row[3],
                "raw_body_digest": row[4],
                "external_event_id": row[5],
                "external_actor_key": row[6],
                "channel_ref": json.loads(str(row[7])),
                "credential_kind": row[8],
                "credential_id": row[9],
                "action": row[10],
                "received_at": row[11],
                "state": row[12],
                "attempts": row[13],
                "claim_generation": row[14],
                "lease_owner": row[15],
                "lease_expires_at": row[16],
                "retry_at": row[17],
                "completed_at": row[18],
                "last_error_code": row[19],
            }
        )

    @staticmethod
    def _fingerprint(
        provider: ChannelProvider,
        installation_ref: str,
        external_event_id: str,
        body_digest: str,
    ) -> str:
        payload = json.dumps(
            {
                "body_digest": body_digest,
                "external_event_id": external_event_id,
                "provider": provider.value,
                "provider_installation_ref": installation_ref,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _digest(payload: bytes) -> str:
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    @staticmethod
    def _channel_json(channel: ChannelRef) -> str:
        return json.dumps(
            channel.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _command_id() -> str:
        return f"CMD-{secrets.token_hex(8).upper()}"

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp는 timezone-aware여야 합니다.")
        return value

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
