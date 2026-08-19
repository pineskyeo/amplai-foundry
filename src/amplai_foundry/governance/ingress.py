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


# 읽을 수 없는 durable row 를 다루는 세 조각 (MGC-012-P5-T027, `D-045`).
#
# **왜 필요한가.** `_view` 는 `json.loads`(`:492`)와 `model_validate` 를 durable column 에
# 돌린다. 둘 다 `ValueError` 를 낸다. `process_next` 의
# `except (GovernanceStoreError, IngressError, sqlite3.Error)` 는 그것을 안 잡으므로 raw 로
# 나갔고, `_view` 가 claim transaction 안이라 claim 이 rollback 되어 row 가
# `pending`·`attempts=0` 으로 돌아갔다. `claim_next` 는 `received_at` 순으로 고르므로 그
# row 가 매번 다시 뽑혀 **뒤에 들어온 command 가 하나도 처리되지 않았다** (round 15 `F-1`).
#
# **왜 좁게 잡는가.** `Exception` 으로 넓히면 프로그래밍 오류가 durable dead letter 로
# 숨는다. 읽기 실패만 잡는다 — `ValidationError` 는 `ValueError` 의 subclass 이고
# `json.JSONDecodeError` 도 그렇다. `row` index 오류(`IndexError`)와 형 오류(`TypeError`)는
# **구현 결함이지 데이터 손상이 아니므로 그대로 올린다.**
_UNREADABLE_ROW: tuple[type[BaseException], ...] = (ValueError,)

UNREADABLE_COMMAND = "INGRESS_COMMAND_UNREADABLE"


class _UnreadableCommand(Exception):
    """durable row 하나를 `IngressCommandView` 로 만들지 못했다. 내부 신호다.

    `governance_transaction` 이 이것으로 정상 rollback 하게 두고, 치우는 write 는 그 밖에서
    새 transaction 으로 한다. 안에서 치우면 같은 rollback 이 치우기까지 되돌린다.

    **`claim_generation` 을 함께 나른다** (round 16 `FR-1`). rollback 과 치우는 write
    사이에 다른 worker 가 같은 row 를 정상 claim 할 수 있다. generation 을 조건으로 걸어야
    그 살아 있는 lease 를 지우지 않는다.
    """

    def __init__(self, command_id: str, claim_generation: int) -> None:
        self.command_id = command_id
        self.claim_generation = claim_generation
        super().__init__(command_id)


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
        """Claim the oldest claimable command, skipping rows that cannot be read.

        읽을 수 없는 row 는 `dead_letter` 로 옮기고 다음 row 로 간다 (`D-045`). 그 전에는
        `_view` 가 던진 `ValueError` 가 `process_next` 를 뚫고 나갔고, `_view` 가
        transaction 안이라 claim 이 rollback 되어 row 가 `pending`·`attempts=0` 으로
        돌아갔다. `received_at` 순으로 다시 뽑히므로 **뒤에 들어온 command 가 하나도
        처리되지 않았다** (round 15 `F-1`, P0). `pending` 이라 `stranded()` 에도 안 걸려
        operator 가 볼 방법이 없었다.

        치우는 write 는 claim transaction **밖**이다. 안에서 하면 그 transaction 의
        rollback 이 치우기까지 되돌린다.

        **loop 는 진행을 보장해야 끝난다** (round 16 `S-1`, P0). 치우는 write 가 실제로
        row 를 바꾸지 못하면 같은 row 가 다시 뽑혀 영원히 돈다. 이미 본 `command_id` 를
        기억해 두 번째로 만나면 멈춘다 — 반복 상한보다 정확하다. 상한은 손상이 많을 때
        정상 진행을 자르지만, 이 조건은 **진짜로 진행이 없을 때만** 멈춘다.
        """
        if not worker_id.strip():
            raise ValueError("worker_id는 비어 있을 수 없습니다.")
        cleared: set[str] = set()
        while True:
            try:
                return self._claim_one(worker_id)
            except _UnreadableCommand as unreadable:
                if unreadable.command_id in cleared:
                    # 치웠는데 또 나왔다. 진행이 없다. 도는 대신 닫는다.
                    raise IngressError(UNREADABLE_COMMAND) from unreadable
                cleared.add(unreadable.command_id)
                try:
                    self._dead_letter_unreadable(unreadable.command_id, unreadable.claim_generation)
                except (GovernanceStoreError, sqlite3.Error) as error:
                    # 치우지 못했다 (round 16 `FR-2`). 큐는 여전히 막혀 있고 row 는
                    # `pending` 이라 `stranded()` 에도 안 보인다 — `D-045` 가 "Rejected" 로
                    # 거절한 상태다. 그것을 **조용히** 두지 않는다. 호출자가 아는 형으로
                    # 닫아 `process_next` 가 `CLAIM_FAILED` 로 기록하게 한다.
                    raise IngressError(UNREADABLE_COMMAND) from error

    def _claim_one(self, worker_id: str) -> IngressCommandView | None:
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
            try:
                return self._view(claimed)
            except _UNREADABLE_ROW as error:
                # `governance_transaction` 이 이 예외로 정상 rollback 하게 둔다. claim 은
                # 확정되지 않고, 치우는 write 는 밖에서 새 transaction 으로 한다.
                #
                # `generation` 은 이 claim 이 쓴 값(`+1`)이다. rollback 되므로 durable
                # 값은 `generation` 으로 되돌아간다 — 치우는 write 가 그것을 조건으로 건다.
                raise _UnreadableCommand(command_id, generation) from error

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

    def _dead_letter_unreadable(self, command_id: str, claim_generation: int) -> None:
        """읽을 수 없는 row 를 치워 큐를 푼다 (`D-045`).

        claim transaction **밖**이다. 안에서 하면 그 rollback 이 이 write 를 되돌린다.
        사용자 통지는 없다 — `UNREADABLE_COMMAND` 는 error code 이지 safe outcome 이 아니다.

        **`claim_generation` 과 claim 가능 상태를 조건으로 건다** (round 16 `FR-1`).
        rollback 과 이 write 사이에 다른 worker 가 같은 row 를 정상 claim 할 수 있다.
        조건이 없으면 그 **살아 있는 lease 를 지운다** — 그 worker 는 governed decision 을
        이미 commit 했을 수 있고, 그러면 결정은 났는데 장부는 "읽을 수 없었다" 로 남는다.
        row 가 그 사이 `completed` 가 됐다면 `completed_at` CHECK 를 위반해
        `IntegrityError` 까지 났다.

        조건이 안 맞아 0행이면 **그것이 옳은 결과다.** 다른 worker 가 가져간 것이므로 실패로
        다루지 않는다. 다만 그 사실을 호출자에게 알려야 loop 가 진행을 판정할 수 있다.
        """
        with self.store.connect() as connection, governance_transaction(connection):
            connection.execute(
                """
                UPDATE governance_ingress_commands
                SET state = 'dead_letter', lease_owner = NULL, lease_expires_at = NULL,
                    retry_at = NULL, last_error_code = ?
                WHERE command_id = ? AND claim_generation = ?
                  AND state IN ('pending', 'retry_wait')
                """,
                (UNREADABLE_COMMAND, command_id, claim_generation),
            )

    def is_unreadable(self, command_id: str) -> bool:
        """Say whether this command exists but cannot be turned into a view.

        **`get()` 의 `None` 하나로는 부족하다** (round 16 `C16-1`). 계약이
        (`contracts/interaction-feedback.md:47-54`) "*we know a decision landed*" 와
        "*we cannot tell*" 을 나누고, `D-042` 는 후자를 침묵의 근거로 쓴다. operator 조회가
        그 둘을 하나로 뭉개면 읽을 수 없는 row 에 "결정 없음" 이라는 **거짓 음성**을 낸다.

        `get()` 의 signature 는 바꾸지 않았다. 유일한 src caller 인
        `IngressDecisionWorker.committed_decision`(`ingress_worker.py:310`)은 `None` 을
        "결정 없음" 으로 쓰는 것이 맞다 — 결정을 못 읽었으면 없는 것으로 보수적으로 다루는
        것이 `D-042` 다. 구분이 필요한 것은 **operator 조회**뿐이라 그쪽에 조회를 하나 둔다.
        """
        with self.store.connect() as connection:
            row = self._select(connection, command_id)
        if row is None:
            return False
        try:
            self._view(row)
        except _UNREADABLE_ROW:
            return True
        return False

    def get(self, command_id: str) -> IngressCommandView | None:
        """Return one command, or None when it is absent **or unreadable**.

        읽을 수 없는 row 에서 raw `ValueError` 를 올리지 않는다. 둘을 구분해야 하는
        호출자는 `is_unreadable()` 을 쓴다.
        """
        with self.store.connect() as connection:
            row = self._select(connection, command_id)
        if row is None:
            return None
        try:
            return self._view(row)
        except _UNREADABLE_ROW:
            return None

    def stranded(self, *, limit: int = 100) -> tuple[IngressCommandView, ...]:
        """List commands no worker will claim again. Operator recovery reads this.

        An attempt-exhausted command is included before the `claim_next` sweep converts
        it to `dead_letter`, whether it waits in `retry_wait` or still holds an expired
        lease, so a stopped worker fleet cannot hide it.

        **읽을 수 없는 row 는 건너뛴다.** 예전에는 그런 row 하나가 `ValueError` 로 목록
        전체를 없앴다 (round 15 `F-2`) — operator 회수 도구가 존재하는 이유가 무너진다.
        건너뛴 것은 `unreadable()` 가 낸다. 조용히 빠뜨리지 않는다.
        """

        readable, _unreadable = self._stranded_rows(limit=limit)
        return readable

    def unreadable(self, *, limit: int = 100) -> tuple[str, ...]:
        """List the stranded rows that cannot be turned into a view.

        `stranded()` 가 건너뛴 것들이다. `IngressCommandView` 로 못 만드는 row 라 id 만
        낸다 — 빈 값을 채운 가짜 view 는 operator 에게 거짓을 보이는 것이다.
        """

        _readable, unreadable = self._stranded_rows(limit=limit)
        return unreadable

    def _stranded_rows(
        self, *, limit: int
    ) -> tuple[tuple[IngressCommandView, ...], tuple[str, ...]]:
        if limit < 1:
            raise ValueError("limit은 1 이상이어야 합니다.")
        now = self._timestamp(self._aware(self._clock()))
        with self.store.connect() as connection:
            rows = connection.execute(
                f"SELECT {_COMMAND_COLUMNS} "
                "FROM governance_ingress_commands "
                "WHERE state IN ('dead_letter', 'recovery_hold') "
                "   OR (state = 'retry_wait' AND attempts >= ?) "
                "   OR (state = 'leased' AND attempts >= ? AND lease_expires_at <= ?) "
                "ORDER BY received_at, command_id LIMIT ?",
                (self.config.max_attempts, self.config.max_attempts, now, limit),
            ).fetchall()
        readable: list[IngressCommandView] = []
        unreadable: list[str] = []
        for row in rows:
            typed = cast("tuple[object, ...]", row)
            try:
                readable.append(self._view(typed))
            except _UNREADABLE_ROW:
                unreadable.append(str(typed[0]))
        return tuple(readable), tuple(unreadable)

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
            # claim 시점에는 읽혔다. 그 사이 손상됐다면 raw `ValueError` 로 내보내지 않고
            # 호출자가 이미 잡는 형으로 닫는다 — `_finalize` 의 except tuple 이 그것이다.
            try:
                view = self._view(row)
            except _UNREADABLE_ROW as error:
                raise IngressError(UNREADABLE_COMMAND) from error
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
            try:
                return self._view(result)
            except _UNREADABLE_ROW as error:
                raise IngressError(UNREADABLE_COMMAND) from error

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
