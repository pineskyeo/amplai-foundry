"""Authoritative, replay-safe activation boundary for Project Store Work.

The Slack adapter authenticates a human and verifies its hash-only token before
calling this service.  This module deliberately accepts ports: governance does
not import the Loop Kit and the Loop Kit does not obtain governance authority.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.models import (
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    ChannelProvider,
    ChannelRef,
    Digest,
)


class WorkActivationError(RuntimeError):
    """A request did not satisfy the fail-closed activation contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkActivationActionType(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class WorkActivationAction(BaseModel):
    """The signed Slack action after raw payload verification and normalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    project_ref: ProjectRef
    feature: str = Field(min_length=1, max_length=128)
    work_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    expected_digest: Digest
    action: WorkActivationActionType
    channel_ref: ChannelRef
    action_token: str = Field(min_length=8, max_length=256, repr=False)
    idempotency_key: str = Field(min_length=1)
    occurred_at: AwareDatetime


@dataclass(frozen=True, slots=True)
class WorkActivationSnapshot:
    work_id: str
    state: str
    revision: int
    digest: str


@dataclass(frozen=True, slots=True)
class WorkActivationScope:
    """One explicit project/provider/feature rollout boundary.

    An empty set is deliberately disabled.  A Slack installation therefore never
    becomes an activation authority simply because its signing secret exists.
    """

    project_ref: ProjectRef
    provider: ChannelProvider
    feature: str


class WorkActivationTokenState(StrEnum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class WorkActivationToken:
    """The safe portion of an activation credential; the raw value is absent."""

    token_id: str
    work_id: str
    expected_revision: int
    expected_digest: str
    action: WorkActivationActionType
    actor_id: str
    channel_ref: ChannelRef
    issued_at: datetime
    expires_at: datetime
    state: WorkActivationTokenState


@dataclass(frozen=True, slots=True)
class IssuedWorkActivationToken:
    record: WorkActivationToken
    raw_token: str = field(repr=False)

    def __repr__(self) -> str:
        return f"IssuedWorkActivationToken(record={self.record!r}, raw_token=<redacted>)"


class WorkActivationGateway(Protocol):
    """Project Store edge; activation is the only permitted mutation."""

    def get(self, work_id: str) -> WorkActivationSnapshot: ...

    def activate(
        self,
        work_id: str,
        *,
        actor_id: str,
        expected_revision: int,
        expected_digest: str,
    ) -> WorkActivationSnapshot: ...


class ActivationTokenVerifier(Protocol):
    """Hash-only, one-time verifier supplied by the authoritative Slack app."""

    def consume(
        self,
        raw_token: str,
        *,
        work_id: str,
        action: WorkActivationActionType,
        actor_id: str,
        channel_ref: ChannelRef,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkActivationResult:
    work_id: str
    state: str
    replayed: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _channel_json(channel: ChannelRef) -> str:
    return json.dumps(
        channel.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


class DurableWorkActivationLedger:
    """Small, append-only SQLite ledger for cross-store Work activation.

    Project Store is intentionally outside the Governance Store transaction
    boundary.  This ledger persists the one-time hash and replay receipt before
    the Project Store edge mutates a Work, which leaves a deterministic recovery
    record instead of relying on a Slack message or a process-local dictionary.
    """

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = path.expanduser().resolve(strict=False)
        self._clock = clock or _now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_activation_tokens (
                    token_id TEXT PRIMARY KEY NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    project_namespace TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    expected_digest TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    channel_json TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    resolved_at TEXT,
                    CHECK (state IN ('issued', 'consumed', 'expired', 'revoked')),
                    CHECK ((state = 'issued' AND resolved_at IS NULL) OR
                           (state != 'issued' AND resolved_at IS NOT NULL))
                );
                CREATE TABLE IF NOT EXISTS work_activation_results (
                    idempotency_key TEXT PRIMARY KEY NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    channel_json TEXT NOT NULL,
                    work_state TEXT NOT NULL,
                    token_id TEXT NOT NULL REFERENCES work_activation_tokens(token_id),
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_activation_commands (
                    idempotency_key TEXT PRIMARY KEY NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    channel_json TEXT NOT NULL,
                    token_id TEXT NOT NULL REFERENCES work_activation_tokens(token_id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_activation_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    channel_json TEXT NOT NULL,
                    outcome_code TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(idempotency_key, outcome_code)
                );
                CREATE TRIGGER IF NOT EXISTS work_activation_tokens_no_delete
                BEFORE DELETE ON work_activation_tokens
                BEGIN SELECT RAISE(ABORT, 'work activation tokens are durable'); END;
                CREATE TRIGGER IF NOT EXISTS work_activation_results_no_update
                BEFORE UPDATE ON work_activation_results
                BEGIN SELECT RAISE(ABORT, 'work activation results are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS work_activation_results_no_delete
                BEFORE DELETE ON work_activation_results
                BEGIN SELECT RAISE(ABORT, 'work activation results are durable'); END;
                """
            )

    def issue_tokens(
        self,
        *,
        snapshot: WorkActivationSnapshot,
        scope: WorkActivationScope,
        authority: AuthorityContext,
        ttl: timedelta = timedelta(minutes=15),
    ) -> tuple[IssuedWorkActivationToken, ...]:
        if ttl <= timedelta(0):
            raise ValueError("Token TTL은 0보다 커야 합니다.")
        if authority.actor_ref.actor_type is not ActorType.HUMAN:
            raise WorkActivationError("AUTHORITY_DENIED")
        if AuthorityPermission.ACTIVATION_MANAGE not in authority.permissions:
            raise WorkActivationError("AUTHORITY_DENIED")
        if authority.project_ref != scope.project_ref:
            raise WorkActivationError("AUTHORITY_DENIED")
        if snapshot.state != "DRAFT":
            raise WorkActivationError("WORK_NOT_DRAFT")
        issued_at = self._aware(self._clock())
        expires_at = issued_at + ttl
        rows: list[IssuedWorkActivationToken] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for action in WorkActivationActionType:
                    token_id, raw_token = self._token_id(), secrets.token_hex(16)
                    channel_json = _channel_json(authority.source.channel)
                    connection.execute(
                        """
                        INSERT INTO work_activation_tokens(
                            token_id, token_hash, project_namespace, project_id, feature,
                            work_id, expected_revision, expected_digest, action, actor_id,
                            channel_json, issued_at, expires_at, state, resolved_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', NULL)
                        """,
                        (
                            token_id,
                            self._hash(raw_token),
                            scope.project_ref.namespace,
                            scope.project_ref.project_id,
                            scope.feature,
                            snapshot.work_id,
                            snapshot.revision,
                            snapshot.digest,
                            action.value,
                            authority.actor_ref.actor_id,
                            channel_json,
                            self._timestamp(issued_at),
                            self._timestamp(expires_at),
                        ),
                    )
                    rows.append(
                        IssuedWorkActivationToken(
                            WorkActivationToken(
                                token_id,
                                snapshot.work_id,
                                snapshot.revision,
                                snapshot.digest,
                                action,
                                authority.actor_ref.actor_id,
                                authority.source.channel,
                                issued_at,
                                expires_at,
                                WorkActivationTokenState.ISSUED,
                            ),
                            raw_token,
                        )
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return tuple(rows)

    def action_for(
        self,
        *,
        token_id: str,
        raw_token: str,
        action: WorkActivationActionType,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> WorkActivationAction:
        """Recover card scope from a raw Slack credential without persisting it."""
        if not raw_token or len(raw_token.encode("utf-8")) > 64:
            raise WorkActivationError("ACTION_TOKEN_INVALID")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT project_namespace, project_id, feature, work_id, expected_revision,
                       expected_digest, actor_id, channel_json, action
                FROM work_activation_tokens
                WHERE token_id = ? AND token_hash = ?
                """,
                (token_id, self._hash(raw_token)),
            ).fetchone()
        if row is None or str(row[8]) != action.value:
            raise WorkActivationError("ACTION_TOKEN_INVALID")
        return WorkActivationAction(
            project_ref=ProjectRef(namespace=str(row[0]), project_id=str(row[1])),
            feature=str(row[2]),
            work_id=str(row[3]),
            expected_revision=int(row[4]),
            expected_digest=str(row[5]),
            action=action,
            channel_ref=ChannelRef.model_validate_json(str(row[7])),
            action_token=raw_token,
            idempotency_key=idempotency_key,
            occurred_at=self._aware(occurred_at),
        )

    def consume(self, raw_token: str, **kwargs: object) -> None:
        work_id = str(kwargs["work_id"])
        action = kwargs["action"]
        actor_id = str(kwargs["actor_id"])
        channel_ref = kwargs["channel_ref"]
        if not isinstance(action, WorkActivationActionType) or not isinstance(
            channel_ref, ChannelRef
        ):
            raise WorkActivationError("ACTION_TOKEN_INVALID")
        now = self._aware(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT token_id, state, expires_at FROM work_activation_tokens
                    WHERE token_hash = ? AND work_id = ? AND action = ? AND actor_id = ?
                      AND channel_json = ?
                    """,
                    (
                        self._hash(raw_token),
                        work_id,
                        action.value,
                        actor_id,
                        _channel_json(channel_ref),
                    ),
                ).fetchone()
                if row is None:
                    raise WorkActivationError("ACTION_TOKEN_INVALID")
                if str(row[1]) != WorkActivationTokenState.ISSUED.value:
                    raise WorkActivationError("ACTION_TOKEN_CONSUMED")
                if str(row[2]) <= self._timestamp(now):
                    connection.execute(
                        "UPDATE work_activation_tokens SET state = 'expired', "
                        "resolved_at = ? WHERE token_id = ?",
                        (self._timestamp(now), str(row[0])),
                    )
                    connection.commit()
                    raise WorkActivationError("ACTION_TOKEN_EXPIRED")
                connection.execute(
                    "UPDATE work_activation_tokens SET state = 'consumed', "
                    "resolved_at = ? WHERE token_id = ?",
                    (self._timestamp(now), str(row[0])),
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def begin(self, action: WorkActivationAction, authority: AuthorityContext) -> bool:
        """Persist an intent and consume its token atomically.

        `True` means an earlier process died after committing this intent.  The
        caller must reconcile the Project Store snapshot rather than rejecting
        the click because the token is already consumed.
        """
        fingerprint = self._fingerprint(action, authority)
        now = self._aware(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                command = connection.execute(
                    "SELECT request_fingerprint FROM work_activation_commands "
                    "WHERE idempotency_key = ?",
                    (action.idempotency_key,),
                ).fetchone()
                if command is not None:
                    if str(command[0]) != fingerprint:
                        raise WorkActivationError("IDEMPOTENCY_CONFLICT")
                    connection.commit()
                    return True
                token = connection.execute(
                    """
                    SELECT token_id, state, expires_at FROM work_activation_tokens
                    WHERE token_hash = ? AND work_id = ? AND action = ? AND actor_id = ?
                      AND channel_json = ?
                    """,
                    (
                        self._hash(action.action_token),
                        action.work_id,
                        action.action.value,
                        authority.actor_ref.actor_id,
                        _channel_json(action.channel_ref),
                    ),
                ).fetchone()
                if token is None:
                    raise WorkActivationError("ACTION_TOKEN_INVALID")
                if str(token[1]) != WorkActivationTokenState.ISSUED.value:
                    raise WorkActivationError("ACTION_TOKEN_CONSUMED")
                if str(token[2]) <= self._timestamp(now):
                    connection.execute(
                        "UPDATE work_activation_tokens SET state='expired', "
                        "resolved_at=? WHERE token_id=?",
                        (self._timestamp(now), str(token[0])),
                    )
                    connection.commit()
                    raise WorkActivationError("ACTION_TOKEN_EXPIRED")
                connection.execute(
                    """
                    INSERT INTO work_activation_commands(
                        idempotency_key, request_fingerprint, work_id, action, actor_id,
                        channel_json, token_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action.idempotency_key,
                        fingerprint,
                        action.work_id,
                        action.action.value,
                        authority.actor_ref.actor_id,
                        _channel_json(action.channel_ref),
                        str(token[0]),
                        self._timestamp(now),
                    ),
                )
                connection.execute(
                    "UPDATE work_activation_tokens SET state='consumed', "
                    "resolved_at=? WHERE token_id=?",
                    (self._timestamp(now), str(token[0])),
                )
                connection.commit()
                return False
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def result_for(
        self, action: WorkActivationAction, authority: AuthorityContext
    ) -> WorkActivationResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_fingerprint, work_id, action, actor_id, channel_json, work_state "
                "FROM work_activation_results WHERE idempotency_key = ?",
                (action.idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        expected = self._fingerprint(action, authority)
        if (
            str(row[0]) != expected
            or str(row[1]) != action.work_id
            or str(row[2]) != action.action.value
            or str(row[3]) != authority.actor_ref.actor_id
            or str(row[4]) != _channel_json(action.channel_ref)
        ):
            raise WorkActivationError("IDEMPOTENCY_CONFLICT")
        return WorkActivationResult(action.work_id, str(row[5]), replayed=True)

    def record_result(
        self,
        action: WorkActivationAction,
        authority: AuthorityContext,
        result: WorkActivationResult,
    ) -> None:
        with self._connect() as connection:
            token = connection.execute(
                "SELECT token_id FROM work_activation_commands WHERE idempotency_key = ?",
                (action.idempotency_key,),
            ).fetchone()
            if token is None:
                raise WorkActivationError("ACTION_TOKEN_INVALID")
            connection.execute(
                """
                INSERT INTO work_activation_results(
                    idempotency_key, request_fingerprint, work_id, action, actor_id,
                    channel_json, work_state, token_id, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.idempotency_key,
                    self._fingerprint(action, authority),
                    action.work_id,
                    action.action.value,
                    authority.actor_ref.actor_id,
                    _channel_json(action.channel_ref),
                    result.state,
                    str(token[0]),
                    self._timestamp(self._aware(self._clock())),
                ),
            )

    def record_denial(
        self,
        action: WorkActivationAction,
        authority: AuthorityContext,
        code: str,
    ) -> None:
        """Persist a credential-free, idempotent denial audit fact."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO work_activation_audit(
                    idempotency_key, work_id, action, actor_id, channel_json,
                    outcome_code, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.idempotency_key,
                    action.work_id,
                    action.action.value,
                    authority.actor_ref.actor_id,
                    _channel_json(action.channel_ref),
                    code,
                    self._timestamp(self._aware(self._clock())),
                ),
            )

    @staticmethod
    def _token_id() -> str:
        return f"TOK-{secrets.token_hex(8).upper()}"

    @staticmethod
    def _hash(raw_token: str) -> str:
        return f"sha256:{hashlib.sha256(raw_token.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _fingerprint(action: WorkActivationAction, authority: AuthorityContext) -> str:
        payload = {
            "project": action.project_ref.model_dump(mode="json"),
            "feature": action.feature,
            "work_id": action.work_id,
            "revision": action.expected_revision,
            "digest": action.expected_digest,
            "action": action.action.value,
            "actor_id": authority.actor_ref.actor_id,
            "channel": action.channel_ref.model_dump(mode="json"),
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp는 timezone-aware여야 합니다.")
        return value

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


class WorkActivationService:
    """Apply one human-approved activation without granting runner authority."""

    def __init__(
        self,
        gateway: WorkActivationGateway,
        tokens: ActivationTokenVerifier,
        *,
        enabled_providers: frozenset[ChannelProvider] = frozenset(),
        enabled_scopes: tuple[WorkActivationScope, ...] = (),
        ledger: DurableWorkActivationLedger | None = None,
    ) -> None:
        self.gateway = gateway
        self.tokens = tokens
        self.enabled_providers = enabled_providers
        self.enabled_scopes = enabled_scopes
        self.ledger = ledger
        self._results: dict[str, WorkActivationResult] = {}

    def apply(
        self, action: WorkActivationAction, authority: AuthorityContext
    ) -> WorkActivationResult:
        try:
            return self._apply(action, authority)
        except WorkActivationError as error:
            if self.ledger is not None:
                self.ledger.record_denial(action, authority, error.code)
            raise

    def _apply(
        self, action: WorkActivationAction, authority: AuthorityContext
    ) -> WorkActivationResult:
        replay = self._results.get(action.idempotency_key)
        if replay is not None:
            return WorkActivationResult(replay.work_id, replay.state, replayed=True)
        if self.ledger is not None:
            replay = self.ledger.result_for(action, authority)
            if replay is not None:
                return replay
        if action.channel_ref.provider not in self.enabled_providers:
            raise WorkActivationError("ACTIVATION_PROVIDER_DISABLED")
        if not any(
            scope.project_ref == action.project_ref
            and scope.provider is action.channel_ref.provider
            and scope.feature == action.feature
            for scope in self.enabled_scopes
        ):
            raise WorkActivationError("ACTIVATION_SCOPE_DISABLED")
        if authority.project_ref != action.project_ref:
            raise WorkActivationError("AUTHORITY_DENIED")
        if authority.actor_ref.actor_type is not ActorType.HUMAN:
            raise WorkActivationError("AUTHORITY_DENIED")
        if AuthorityPermission.ACTIVATION_MANAGE not in authority.permissions:
            raise WorkActivationError("AUTHORITY_DENIED")
        if authority.source.channel != action.channel_ref:
            raise WorkActivationError("ACTION_CHANNEL_MISMATCH")
        snapshot = self.gateway.get(action.work_id)
        pending_recovery = False
        if self.ledger is not None:
            # The intent is durable before the cross-store mutation.  A retry
            # owns the same intent even if a previous process stopped after the
            # token transition or after Project Store activation.
            pending_recovery = self.ledger.begin(action, authority)
            if pending_recovery and action.action is WorkActivationActionType.APPROVE:
                if snapshot.state in {"READY", "WAITING"}:
                    result = WorkActivationResult(action.work_id, snapshot.state)
                    self.ledger.record_result(action, authority, result)
                    return WorkActivationResult(result.work_id, result.state, replayed=True)
                if snapshot.state != "DRAFT":
                    raise WorkActivationError("ACTIVATION_RECOVERY_HOLD")
            elif pending_recovery:
                if snapshot.state != "DRAFT":
                    raise WorkActivationError("ACTIVATION_RECOVERY_HOLD")
                result = WorkActivationResult(action.work_id, snapshot.state)
                self.ledger.record_result(action, authority, result)
                return WorkActivationResult(result.work_id, result.state, replayed=True)
        if not pending_recovery and (
            snapshot.revision != action.expected_revision
            or snapshot.digest != action.expected_digest
        ):
            raise WorkActivationError("WORK_STALE")
        if snapshot.state != "DRAFT":
            raise WorkActivationError("WORK_NOT_DRAFT")
        if self.ledger is None:
            self.tokens.consume(
                action.action_token,
                work_id=action.work_id,
                action=action.action,
                actor_id=authority.actor_ref.actor_id,
                channel_ref=action.channel_ref,
            )
        if action.action is WorkActivationActionType.APPROVE:
            snapshot = self.gateway.activate(
                action.work_id,
                actor_id=authority.actor_ref.actor_id,
                expected_revision=action.expected_revision,
                expected_digest=action.expected_digest,
            )
        result = WorkActivationResult(action.work_id, snapshot.state)
        if self.ledger is not None:
            self.ledger.record_result(action, authority, result)
        self._results[action.idempotency_key] = result
        return result
