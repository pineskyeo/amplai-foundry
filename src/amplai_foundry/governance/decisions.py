"""Hash-only ActionToken issuance and atomic Proposal decision replay."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from amplai_foundry.governance.active_proposals import ActiveProposalStatus
from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    AuthorityService,
    DirectAuthorityRequest,
    IngressAuthorityRequest,
)
from amplai_foundry.governance.events import (
    DecisionProjectionPayload,
    GovernanceEventService,
)
from amplai_foundry.governance.legacy_gates import legacy_mutation_block
from amplai_foundry.governance.models import (
    ActorRef,
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    ChannelProvider,
    ChannelRef,
    Digest,
    ProposalRef,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction


class DecisionError(RuntimeError):
    """A governed decision command failed closed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DecisionAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class ActionTokenState(StrEnum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


_DECISION_STATUS = {
    DecisionAction.APPROVE: ActiveProposalStatus.APPROVED,
    DecisionAction.REJECT: ActiveProposalStatus.REJECTED,
    DecisionAction.REQUEST_CHANGES: ActiveProposalStatus.CHANGES_REQUESTED,
}


def _system_now() -> datetime:
    return datetime.now(UTC)


def clear_exception_frames(error: BaseException) -> None:
    """Drop every frame an exception chain still holds (MGC-012-P5-T022).

    이 함수는 credential 을 든 frame 을 예외 chain 에서 지운다. `_send` 의 `request` local 은
    `Authorization: Bearer <token>` 을 든 채이고, renderer 의 local 은 button 의 raw
    ActionToken 을 든 채다. traceback 이 살아 있으면 `--showlocals` 렌더링과 log 로 그것들이
    나간다.

    **정의는 저장소에 하나만 둔다.** 원래 `slack_http`, `decisions`, `slack_projection`,
    `slack_cards` 에 네 벌로 있었고 본문이 완전히 같았다 (`ast.unparse` digest 로 확인).
    사본이 넷이면 하나를 고칠 때 나머지 셋이 뒤처지고, 그것이 round 11 `R-1` 이 실측한
    사고다. `D-038` 항목 4 가 알려진 한계로 기록해 둔 것을 닫는다.

    **여기 두는 이유는 import 방향이다.** 네 module 중 `decisions` 만이 나머지 셋의
    (직접 또는 간접) 의존 대상이다 — `slack_cards -> decisions`,
    `slack_projection -> slack_cards`, `slack_http -> slack_projection`. 반대 방향은 없으므로
    순환이 생기지 않는다. 이름을 공개형으로 둔 것은 `classify_interruption`,
    `raise_sanitized_interruption` 과 같은 성격의 module 간 helper 이기 때문이다. 이 셋은
    `governance/__init__.py` 로 export 하지 않는다 — 공개 API 표면을 넓힐 이유가 없다.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__traceback__ is not None:
            traceback.clear_frames(current.__traceback__)
            current.__traceback__ = None
        next_error = current.__cause__ or current.__context__
        current.__cause__ = None
        current.__context__ = None
        current = next_error


class ActionTokenView(BaseModel):
    """Durable token metadata. The raw credential is deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token_id: str = Field(pattern=r"^TOK-[A-F0-9]{16}$")
    proposal_ref: ProposalRef
    active_definition_digest: Digest
    content_revision: int = Field(ge=1)
    state_revision: int = Field(ge=1)
    decision_epoch: int = Field(ge=1)
    allowed_action: DecisionAction
    allowed_actor_ref: ActorRef
    bound_channel_ref: ChannelRef
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    state: ActionTokenState
    resolved_at: AwareDatetime | None = None


@dataclass(frozen=True, slots=True)
class IssuedActionToken:
    """One-time issuance response; repr never exposes the raw credential."""

    record: ActionTokenView
    raw_token: str = field(repr=False)

    def __repr__(self) -> str:
        return f"IssuedActionToken(record={self.record!r}, raw_token=<redacted>)"


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_ref: ProposalRef
    action: DecisionAction
    proposal_status: ActiveProposalStatus
    active_definition_digest: Digest
    content_revision: int = Field(ge=1)
    state_revision: int = Field(ge=2)
    decision_epoch: int = Field(ge=1)
    token_id: str
    processed_at: AwareDatetime
    replayed: bool = False


class DecisionService:
    """Issue scoped credentials and atomically execute governed decisions."""

    def __init__(
        self,
        store: GovernanceStore,
        authority_service: AuthorityService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.authority_service = authority_service
        self._clock = clock or _system_now

    def issue_tokens(
        self,
        ref: ProposalRef,
        *,
        authority_request: DirectAuthorityRequest,
        ttl: timedelta = timedelta(minutes=15),
    ) -> tuple[IssuedActionToken, ...]:
        if ttl <= timedelta(0):
            raise ValueError("Token TTL은 0보다 커야 합니다.")
        credentials: tuple[tuple[DecisionAction, str, str], ...] = ()
        issued: tuple[IssuedActionToken, ...] = ()
        try:
            with self.store.connect() as connection, governance_transaction(connection):
                authority = self._authenticate(authority_request, connection=connection)
                self._require_decision_authority(authority, ref)
                credentials = self._issue_tokens_in_transaction(
                    connection,
                    ref,
                    authority=authority,
                    issued_at=self._aware(self._clock()),
                    ttl=ttl,
                )
                issued = tuple(
                    IssuedActionToken(
                        record=self._get_token_in_connection(connection, token_id),
                        raw_token=raw_token,
                    )
                    for _action, token_id, raw_token in credentials
                )
            return issued
        except BaseException as error:
            clear_exception_frames(error)
            credentials = ()
            issued = ()
            raise
        finally:
            credentials = ()
            issued = ()

    def _issue_tokens_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        authority: AuthorityContext,
        issued_at: datetime,
        ttl: timedelta | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[tuple[DecisionAction, str, str], ...]:
        """Issue one complete action set inside a caller-owned transaction."""

        if not connection.in_transaction:
            raise DecisionError("GOVERNANCE_TRANSACTION_REQUIRED")
        self._require_decision_authority(authority, ref)
        self._require_no_legacy_approval_hold(connection, ref)
        resolved_issued_at = self._aware(issued_at)
        if (ttl is None) == (expires_at is None):
            raise ValueError("Token ttl 또는 expires_at 중 하나만 필요합니다.")
        resolved_expiry = (
            resolved_issued_at + ttl if ttl is not None else self._aware(cast(datetime, expires_at))
        )
        if resolved_expiry <= resolved_issued_at:
            raise ValueError("Token expiry는 issue 시각보다 뒤여야 합니다.")
        proposal = self._proposal_row(connection, ref)
        if proposal is None:
            raise DecisionError("PROPOSAL_NOT_FOUND")
        if ActiveProposalStatus(str(proposal[4])) is not ActiveProposalStatus.REVIEWED:
            raise DecisionError("INVALID_PROPOSAL_STATE")
        credentials = tuple(
            (action, self._token_id(), secrets.token_hex(16)) for action in DecisionAction
        )
        channel_json = self._channel_json(authority.source.channel)
        for action, token_id, raw_token in credentials:
            connection.execute(
                """
                INSERT INTO governance_action_tokens(
                    token_id, token_hash, project_namespace, project_id, proposal_id,
                    active_definition_digest, content_revision, state_revision,
                    decision_epoch, allowed_action, allowed_actor_id, allowed_actor_type,
                    bound_channel_json, issued_at, expires_at, state, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', NULL)
                """,
                (
                    token_id,
                    self._token_hash(raw_token),
                    *self._identity(ref),
                    proposal[0],
                    proposal[1],
                    proposal[2],
                    proposal[3],
                    action.value,
                    authority.actor_ref.actor_id,
                    authority.actor_ref.actor_type.value,
                    channel_json,
                    self._timestamp(resolved_issued_at),
                    self._timestamp(resolved_expiry),
                ),
            )
        return credentials

    @staticmethod
    def _revoke_token_ids_in_transaction(
        connection: sqlite3.Connection,
        token_ids: tuple[str, ...],
        *,
        resolved_at: datetime,
    ) -> int:
        if not connection.in_transaction:
            raise DecisionError("GOVERNANCE_TRANSACTION_REQUIRED")
        if not token_ids:
            return 0
        placeholders = ",".join("?" for _ in token_ids)
        result = connection.execute(
            f"""
            UPDATE governance_action_tokens
            SET state = 'revoked', resolved_at = ?
            WHERE state = 'issued' AND token_id IN ({placeholders})
            """,
            (DecisionService._timestamp(resolved_at), *token_ids),
        )
        return result.rowcount

    def decide(
        self,
        ref: ProposalRef,
        *,
        action: DecisionAction,
        authority_request: DirectAuthorityRequest,
        raw_token: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> DecisionResult:
        try:
            self._validate_command(idempotency_key, request_fingerprint, raw_token)
            credential_hash = self._token_hash(raw_token)
            # The raw credential has no purpose after hashing. Clear this public frame
            # before any authority, database, audit, or outbox failure can unwind.
            raw_token = ""
            with self.store.connect() as connection, governance_transaction(connection):
                authority = self._authenticate(authority_request, connection=connection)
                self._require_decision_authority(authority, ref)
                return self._decide_verified_hash_in_transaction(
                    connection,
                    ref,
                    action=action,
                    authority=authority,
                    credential_hash=credential_hash,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
        except BaseException as error:
            # `decide()` remains a supported raw-token boundary for direct callers.
            # Remove all credential-bearing source frames while preserving the public
            # exception type/code and process-level interruption class.
            clear_exception_frames(error)
            raw_token = ""
            raise
        finally:
            raw_token = ""

    def decide_ingress_in_transaction(
        self,
        connection: sqlite3.Connection,
        authority_request: IngressAuthorityRequest,
        *,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> DecisionResult:
        """Resolve durable ingress scope and execute under one caller-owned transaction."""

        if not connection.in_transaction:
            raise DecisionError("GOVERNANCE_TRANSACTION_REQUIRED")
        row = connection.execute(
            """
            SELECT t.proposal_id, i.action, i.credential_hash
            FROM governance_ingress_commands i
            JOIN governance_action_tokens t
              ON t.token_id = i.credential_id
             AND t.token_hash = i.credential_hash
             AND t.allowed_action = i.action
            WHERE i.command_id = ?
            """,
            (authority_request.command_id,),
        ).fetchone()
        if row is None:
            raise DecisionError("ACTION_TOKEN_INVALID")
        proposal_id = str(row[0])
        action = DecisionAction(str(row[1]))
        credential_hash = str(row[2])
        self._validate_verified_command(idempotency_key, request_fingerprint, credential_hash)
        # Replay precedence: an existing result is returned before an Authority exists,
        # so a reclaim after a committed decision cannot be re-denied by a later
        # binding or permission change.
        replay = self._result_row(connection, idempotency_key)
        if replay is not None:
            return self._replay_ingress_result(
                replay,
                proposal_id=proposal_id,
                action=action,
                request_fingerprint=request_fingerprint,
            )
        authority = self._authenticate(authority_request, connection=connection)
        ref = ProposalRef(project_ref=authority.project_ref, proposal_id=proposal_id)
        self._require_decision_authority(authority, ref)
        return self._decide_verified_hash_in_transaction(
            connection,
            ref,
            action=action,
            authority=authority,
            credential_hash=credential_hash,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )

    def _decide_verified_hash_in_transaction(
        self,
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        action: DecisionAction,
        authority: AuthorityContext,
        credential_hash: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> DecisionResult:
        """Connection-bound decision primitive for ingress/audit atomic composition."""

        if not connection.in_transaction:
            raise DecisionError("GOVERNANCE_TRANSACTION_REQUIRED")
        self._validate_verified_command(
            idempotency_key,
            request_fingerprint,
            credential_hash,
        )
        if self._contains_persisted_secret(connection, idempotency_key):
            raise DecisionError("IDEMPOTENCY_CONFLICT")
        processed_at = self._aware(self._clock())
        channel_json = self._channel_json(authority.source.channel)
        actor_ref = authority.actor_ref
        replay = self._result_row(connection, idempotency_key)
        if replay is not None:
            return self._replay_result(
                replay,
                ref=ref,
                action=action,
                actor_ref=actor_ref,
                channel_json=channel_json,
                request_fingerprint=request_fingerprint,
            )
        self._require_no_legacy_approval_hold(connection, ref)

        token = connection.execute(
            """
            SELECT token_id, project_namespace, project_id, proposal_id,
                   active_definition_digest, content_revision, state_revision,
                   decision_epoch, allowed_action, allowed_actor_id,
                   allowed_actor_type, bound_channel_json, issued_at, expires_at,
                   state, resolved_at
            FROM governance_action_tokens
            WHERE token_hash = ?
            """,
            (credential_hash,),
        ).fetchone()
        if token is None:
            raise DecisionError("ACTION_TOKEN_INVALID")
        if tuple(str(value) for value in token[1:4]) != self._identity(ref):
            raise DecisionError("ACTION_TOKEN_INVALID")
        token_state = ActionTokenState(str(token[14]))
        if token_state is ActionTokenState.CONSUMED:
            raise DecisionError("ACTION_TOKEN_CONSUMED")
        if token_state is ActionTokenState.REVOKED:
            raise DecisionError("ACTION_TOKEN_REVOKED")
        if token_state is ActionTokenState.EXPIRED:
            raise DecisionError("ACTION_TOKEN_EXPIRED")
        if token_state is not ActionTokenState.ISSUED:
            raise DecisionError("ACTION_TOKEN_INVALID")
        if processed_at >= self._parse_timestamp(str(token[13])):
            raise DecisionError("ACTION_TOKEN_EXPIRED")
        if str(token[8]) != action.value:
            raise DecisionError("ACTION_TOKEN_INVALID")
        if (str(token[9]), str(token[10])) != (
            actor_ref.actor_id,
            actor_ref.actor_type.value,
        ):
            raise DecisionError("ACTION_ACTOR_MISMATCH")
        if not self._channel_binding_matches(str(token[11]), authority.source.channel):
            raise DecisionError("ACTION_CHANNEL_MISMATCH")

        proposal = self._proposal_row(connection, ref)
        if proposal is None:
            raise DecisionError("PROPOSAL_NOT_FOUND")
        if ActiveProposalStatus(str(proposal[4])) is not ActiveProposalStatus.REVIEWED:
            raise DecisionError("INVALID_PROPOSAL_STATE")
        if tuple(proposal[:4]) != tuple(token[4:8]):
            raise DecisionError("PROPOSAL_STALE")

        next_status = _DECISION_STATUS[action]
        updated = connection.execute(
            """
            UPDATE governance_active_proposals
            SET status = ?, state_revision = state_revision + 1,
                updated_at = ?
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
              AND status = 'reviewed' AND active_definition_digest = ?
              AND content_revision = ? AND state_revision = ? AND decision_epoch = ?
            """,
            (
                next_status.value,
                self._timestamp(processed_at),
                *self._identity(ref),
                *token[4:8],
            ),
        )
        if updated.rowcount != 1:
            raise DecisionError("PROPOSAL_STALE")
        consumed = connection.execute(
            """
            UPDATE governance_action_tokens
            SET state = 'consumed', resolved_at = ?
            WHERE token_id = ? AND state = 'issued'
            """,
            (self._timestamp(processed_at), token[0]),
        )
        if consumed.rowcount != 1:
            raise DecisionError("ACTION_TOKEN_CONSUMED")
        # Historical migration fixtures intentionally execute this service against an
        # older schema. Review action sets were introduced in schema 31, so the
        # compatibility path must remain a no-op until that table exists.
        review_sets_exist = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'governance_review_action_sets'"
        ).fetchone()
        if review_sets_exist is not None:
            review_action_set = connection.execute(
                """
                SELECT approve_token_id, request_changes_token_id, reject_token_id
                FROM governance_review_action_sets
                WHERE state = 'issued' AND (
                    approve_token_id = ? OR request_changes_token_id = ? OR reject_token_id = ?
                )
                """,
                (token[0], token[0], token[0]),
            ).fetchone()
            if review_action_set is not None:
                sibling_ids = tuple(
                    str(token_id)
                    for token_id in review_action_set
                    if str(token_id) != str(token[0])
                )
                self._revoke_token_ids_in_transaction(
                    connection,
                    sibling_ids,
                    resolved_at=processed_at,
                )
            connection.execute(
                """
                UPDATE governance_review_action_sets
                SET state = 'consumed', resolved_at = ?
                WHERE state = 'issued' AND (
                    approve_token_id = ? OR request_changes_token_id = ? OR reject_token_id = ?
                )
                """,
                (self._timestamp(processed_at), token[0], token[0], token[0]),
            )
        next_state_revision = int(token[6]) + 1
        connection.execute(
            """
            INSERT INTO governance_decision_results(
                idempotency_key, request_fingerprint, project_namespace, project_id,
                proposal_id, action, actor_id, actor_type, channel_json,
                proposal_status, active_definition_digest, content_revision,
                state_revision, decision_epoch, token_id, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                request_fingerprint,
                *self._identity(ref),
                action.value,
                actor_ref.actor_id,
                actor_ref.actor_type.value,
                channel_json,
                next_status.value,
                token[4],
                token[5],
                next_state_revision,
                token[7],
                token[0],
                self._timestamp(processed_at),
            ),
        )
        event_payload = DecisionProjectionPayload(
            action=action.value,
            active_definition_digest=str(token[4]),
            aggregate_ref=ref,
            content_revision=int(token[5]),
            decision_epoch=int(token[7]),
            proposal_status=next_status.value,
            state_revision=next_state_revision,
        )
        GovernanceEventService(self.store, clock=self._clock)._append_decision_in_transaction(
            connection,
            ref,
            decision_result_key=idempotency_key,
            authority=authority,
            payload=event_payload,
        )
        return DecisionResult(
            proposal_ref=ref,
            action=action,
            proposal_status=next_status,
            active_definition_digest=str(token[4]),
            content_revision=int(token[5]),
            state_revision=next_state_revision,
            decision_epoch=int(token[7]),
            token_id=str(token[0]),
            processed_at=processed_at,
        )

    def _authenticate(
        self,
        request: DirectAuthorityRequest | IngressAuthorityRequest,
        *,
        connection: sqlite3.Connection,
    ) -> AuthorityContext:
        try:
            return self.authority_service.authenticate(request, connection=connection)
        except AuthorityResolutionError as error:
            raise DecisionError(error.code) from error

    @staticmethod
    def _require_decision_authority(authority: AuthorityContext, ref: ProposalRef) -> None:
        if authority.project_ref != ref.project_ref:
            raise DecisionError("AUTHORITY_DENIED")
        if authority.actor_ref.actor_type is not ActorType.HUMAN:
            raise DecisionError("AUTHORITY_DENIED")
        if AuthorityPermission.PROPOSAL_DECIDE not in authority.permissions:
            raise DecisionError("AUTHORITY_DENIED")

    @classmethod
    def _require_no_legacy_approval_hold(
        cls,
        connection: sqlite3.Connection,
        ref: ProposalRef,
    ) -> None:
        block = legacy_mutation_block(connection, ref)
        if block is not None:
            raise DecisionError(block)

    def result_for(
        self,
        idempotency_key: str,
        *,
        expected_fingerprint: str | None = None,
    ) -> DecisionResult | None:
        """Read a committed decision by replay key without touching Proposal or Token.

        `expected_fingerprint` binds the answer to the request that produced it, the same
        way the replay path binds. A stored row under this key that was produced by a
        different request answers None.
        """

        if not idempotency_key.strip():
            raise ValueError("idempotency_key는 비어 있을 수 없습니다.")
        with self.store.connect() as connection:
            row = self._result_row(connection, idempotency_key)
        if row is None:
            return None
        if expected_fingerprint is not None and str(row[0]) != expected_fingerprint:
            return None
        return self._replayed_decision(row, self._proposal_ref(row[1], row[2], row[3]))

    def get_token(self, token_id: str) -> ActionTokenView:
        with self.store.connect() as connection:
            return self._get_token_in_connection(connection, token_id)

    @staticmethod
    def _get_token_in_connection(
        connection: sqlite3.Connection,
        token_id: str,
    ) -> ActionTokenView:
        row = connection.execute(
            """
            SELECT project_namespace, project_id, proposal_id,
                   active_definition_digest, content_revision, state_revision,
                   decision_epoch, allowed_action, allowed_actor_id,
                   allowed_actor_type, bound_channel_json, issued_at, expires_at,
                   state, resolved_at
            FROM governance_action_tokens WHERE token_id = ?
            """,
            (token_id,),
        ).fetchone()
        if row is None:
            raise DecisionError("ACTION_TOKEN_INVALID")
        ref = DecisionService._proposal_ref(row[0], row[1], row[2])
        return ActionTokenView.model_validate(
            {
                "token_id": token_id,
                "proposal_ref": ref,
                "active_definition_digest": row[3],
                "content_revision": row[4],
                "state_revision": row[5],
                "decision_epoch": row[6],
                "allowed_action": row[7],
                "allowed_actor_ref": {
                    "actor_id": row[8],
                    "actor_type": row[9],
                },
                "bound_channel_ref": json.loads(str(row[10])),
                "issued_at": row[11],
                "expires_at": row[12],
                "state": row[13],
                "resolved_at": row[14],
            }
        )

    def expire_tokens(self) -> int:
        """Resolve elapsed issued tokens without touching Proposal state."""

        expired_at = self._aware(self._clock())
        with self.store.connect() as connection, governance_transaction(connection):
            result = connection.execute(
                """
                UPDATE governance_action_tokens
                SET state = 'expired', resolved_at = ?
                WHERE state = 'issued' AND expires_at <= ?
                """,
                (self._timestamp(expired_at), self._timestamp(expired_at)),
            )
            connection.execute(
                """
                UPDATE governance_review_action_sets
                SET state = 'expired', resolved_at = ?
                WHERE state = 'issued' AND expires_at <= ?
                """,
                (self._timestamp(expired_at), self._timestamp(expired_at)),
            )
            return result.rowcount

    @staticmethod
    def _proposal_row(
        connection: sqlite3.Connection,
        ref: ProposalRef,
    ) -> tuple[object, ...] | None:
        return cast(
            tuple[object, ...] | None,
            connection.execute(
                """
                SELECT active_definition_digest, content_revision, state_revision,
                       decision_epoch, status
                FROM governance_active_proposals
                WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
                """,
                DecisionService._identity(ref),
            ).fetchone(),
        )

    @staticmethod
    def _result_row(
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> tuple[object, ...] | None:
        return cast(
            tuple[object, ...] | None,
            connection.execute(
                """
                SELECT request_fingerprint, project_namespace, project_id, proposal_id,
                       action, actor_id, actor_type, channel_json, proposal_status,
                       active_definition_digest, content_revision, state_revision,
                       decision_epoch, token_id, processed_at
                FROM governance_decision_results WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone(),
        )

    @staticmethod
    def _replay_result(
        row: tuple[object, ...],
        *,
        ref: ProposalRef,
        action: DecisionAction,
        actor_ref: ActorRef,
        channel_json: str,
        request_fingerprint: str,
    ) -> DecisionResult:
        original_ref = DecisionService._proposal_ref(row[1], row[2], row[3])
        if (
            str(row[0]) != request_fingerprint
            or original_ref != ref
            or str(row[4]) != action.value
            or (str(row[5]), str(row[6])) != (actor_ref.actor_id, actor_ref.actor_type.value)
            or str(row[7]) != channel_json
        ):
            raise DecisionError("IDEMPOTENCY_CONFLICT")
        return DecisionService._replayed_decision(row, original_ref)

    @staticmethod
    def _replay_ingress_result(
        row: tuple[object, ...],
        *,
        proposal_id: str,
        action: DecisionAction,
        request_fingerprint: str,
    ) -> DecisionResult:
        """Bind the replay to durable ingress identity instead of a resolved Authority."""

        original_ref = DecisionService._proposal_ref(row[1], row[2], row[3])
        if (
            str(row[0]) != request_fingerprint
            or original_ref.proposal_id != proposal_id
            or str(row[4]) != action.value
        ):
            raise DecisionError("IDEMPOTENCY_CONFLICT")
        return DecisionService._replayed_decision(row, original_ref)

    @staticmethod
    def _replayed_decision(row: tuple[object, ...], ref: ProposalRef) -> DecisionResult:
        return DecisionResult(
            proposal_ref=ref,
            action=DecisionAction(str(row[4])),
            proposal_status=ActiveProposalStatus(str(row[8])),
            active_definition_digest=str(row[9]),
            content_revision=int(str(row[10])),
            state_revision=int(str(row[11])),
            decision_epoch=int(str(row[12])),
            token_id=str(row[13]),
            processed_at=DecisionService._parse_timestamp(str(row[14])),
            replayed=True,
        )

    @staticmethod
    def _validate_command(idempotency_key: str, fingerprint: str, raw_token: str) -> None:
        DecisionService._validate_replay_key(idempotency_key, fingerprint)
        if not raw_token or len(raw_token.encode("utf-8")) > 64:
            raise DecisionError("ACTION_TOKEN_INVALID")
        if raw_token in idempotency_key:
            raise DecisionError("IDEMPOTENCY_CONFLICT")

    @staticmethod
    def _validate_verified_command(
        idempotency_key: str,
        fingerprint: str,
        credential_hash: str,
    ) -> None:
        DecisionService._validate_replay_key(idempotency_key, fingerprint)
        if (
            len(credential_hash) != 71
            or not credential_hash.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in credential_hash[7:])
        ):
            raise DecisionError("ACTION_TOKEN_INVALID")

    @staticmethod
    def _validate_replay_key(idempotency_key: str, fingerprint: str) -> None:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key는 비어 있을 수 없습니다.")
        if len(idempotency_key) > 512:
            raise ValueError("idempotency_key는 512자를 초과할 수 없습니다.")
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("request_fingerprint는 lowercase SHA-256 hex여야 합니다.")

    @staticmethod
    def _identity(ref: ProposalRef) -> tuple[str, str, str]:
        return ref.project_ref.namespace, ref.project_ref.project_id, ref.proposal_id

    @staticmethod
    def _proposal_ref(namespace: object, project_id: object, proposal_id: object) -> ProposalRef:
        from amplai_foundry.domain.identity import ProjectRef

        return ProposalRef(
            project_ref=ProjectRef(namespace=str(namespace), project_id=str(project_id)),
            proposal_id=str(proposal_id),
        )

    @staticmethod
    def _channel_json(channel: ChannelRef) -> str:
        return json.dumps(
            channel.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _channel_binding_matches(bound_json: str, actual: ChannelRef) -> bool:
        actual_json = DecisionService._channel_json(actual)
        if bound_json == actual_json:
            return True
        try:
            bound = ChannelRef.model_validate(json.loads(bound_json))
        except (ValueError, TypeError):
            return False
        if (
            bound.provider is not ChannelProvider.SLACK
            or actual.provider is not ChannelProvider.SLACK
        ):
            return False
        return bound.workspace_id == actual.workspace_id and bound.channel_id == actual.channel_id

    @staticmethod
    def _token_id() -> str:
        return f"TOK-{secrets.token_hex(8).upper()}"

    @staticmethod
    def _token_hash(raw_token: str) -> str:
        return f"sha256:{hashlib.sha256(raw_token.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _contains_persisted_secret(connection: sqlite3.Connection, value: str) -> bool:
        """Match every token-shaped substring without persisting raw credentials."""

        token_length = 32  # secrets.token_hex(16)
        candidates = {
            value[index : index + token_length]
            for index in range(max(len(value) - token_length + 1, 0))
            if all(
                character in "0123456789abcdef" for character in value[index : index + token_length]
            )
        }
        if not candidates:
            return False
        hashes = tuple(DecisionService._token_hash(candidate) for candidate in candidates)
        placeholders = ",".join("?" for _ in hashes)
        return (
            connection.execute(
                f"""
                SELECT 1 FROM (
                    SELECT token_hash AS secret_hash FROM governance_action_tokens
                    UNION ALL
                    SELECT grant_hash AS secret_hash FROM governance_apply_grants
                ) WHERE secret_hash IN ({placeholders})
                LIMIT 1
                """,
                hashes,
            ).fetchone()
            is not None
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp는 timezone-aware여야 합니다.")
        return value

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
