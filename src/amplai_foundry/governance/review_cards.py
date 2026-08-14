"""Governed Review Card intent and memory-only action-set preparation."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from amplai_foundry.governance.active_proposals import ActiveProposalStatus
from amplai_foundry.governance.authority import (
    AuthorityResolutionError,
    AuthorityService,
    DirectAuthorityRequest,
)
from amplai_foundry.governance.decisions import (
    DecisionAction,
    DecisionError,
    DecisionService,
    IssuedActionToken,
)
from amplai_foundry.governance.definitions import ProposalDefinitionManifest
from amplai_foundry.governance.events import (
    GovernanceEventService,
    OutboxEventView,
    ReviewProjectionPayload,
)
from amplai_foundry.governance.models import (
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    ChannelProvider,
    ChannelRef,
    ProposalRef,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction
from amplai_foundry.proposals.models import OperationType


def _system_now() -> datetime:
    return datetime.now(UTC)


class DefinitionObjectReader(Protocol):
    def get_definition_object(self, ref: ProposalRef, digest: str) -> bytes: ...


class ReviewCardError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReviewCardRequestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: str
    payload: ReviewProjectionPayload
    audit_event_id: str
    outbox_event_id: str
    replayed: bool = False


class ReviewActionSetView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    generation: int = Field(ge=1)
    approve_token_id: str
    request_changes_token_id: str
    reject_token_id: str
    state: str = Field(pattern=r"^(issued|consumed|expired|revoked)$")
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    resolved_at: AwareDatetime | None = None


@dataclass(frozen=True, slots=True)
class PreparedReviewActionSet:
    """Raw credentials exist only in this memory-only transport handoff."""

    view: ReviewActionSetView
    issued: tuple[IssuedActionToken, ...]

    def __repr__(self) -> str:
        return f"PreparedReviewActionSet(view={self.view!r}, issued=<redacted>)"

    def button_value(self, action: DecisionAction) -> str:
        for issued in self.issued:
            if issued.record.allowed_action is action:
                return f"{issued.record.token_id}.{issued.raw_token}"
        raise ReviewCardError("REVIEW_ACTION_SET_INVALID")


class ReviewCardService:
    """Record one reviewed snapshot intent and its secret-free Slack outbox event."""

    ACTION_TTL = timedelta(hours=24)

    def __init__(
        self,
        store: GovernanceStore,
        authority_service: AuthorityService,
        definitions: DefinitionObjectReader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.authority_service = authority_service
        self.definitions = definitions
        self._clock = clock or _system_now

    def request(
        self,
        ref: ProposalRef,
        *,
        reviewer_request: DirectAuthorityRequest,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> ReviewCardRequestResult:
        self._validate_replay_key(idempotency_key, request_fingerprint)
        if reviewer_request.provider is not ChannelProvider.SLACK:
            raise ReviewCardError("REVIEW_CARD_PROVIDER_UNSUPPORTED")
        with self.store.connect() as replay_connection:
            replay = self._replay(replay_connection, idempotency_key, request_fingerprint)
        if replay is not None:
            return replay
        snapshot = self._snapshot(ref)
        definition_bytes = self.definitions.get_definition_object(ref, str(snapshot[0]))
        try:
            definition = ProposalDefinitionManifest.model_validate_json(definition_bytes)
        except ValueError as error:
            raise ReviewCardError("REVIEW_CARD_DEFINITION_INVALID") from error
        requested_at = self._aware(self._clock())
        expires_at = requested_at + self.ACTION_TTL
        with self.store.connect() as connection, governance_transaction(connection):
            authority = self._authenticate(reviewer_request, connection)
            if (
                authority.project_ref != ref.project_ref
                or authority.actor_ref.actor_type is not ActorType.HUMAN
                or AuthorityPermission.PROPOSAL_DECIDE not in authority.permissions
            ):
                raise ReviewCardError("AUTHORITY_DENIED")
            replay = self._replay(connection, idempotency_key, request_fingerprint)
            if replay is not None:
                return replay
            current = self._snapshot_in_connection(connection, ref)
            if (
                current != snapshot
                or ActiveProposalStatus(str(current[4])) is not ActiveProposalStatus.REVIEWED
            ):
                raise ReviewCardError("PROPOSAL_STALE")
            if definition.proposal_ref != ref or definition.definition_digest != str(current[0]):
                raise ReviewCardError("REVIEW_CARD_DEFINITION_INVALID")
            self._require_snapshot_available(
                connection,
                ref,
                active_definition_digest=str(current[0]),
                content_revision=int(str(current[1])),
                state_revision=int(str(current[2])),
                decision_epoch=int(str(current[3])),
            )
            payload = self._payload(
                ref,
                current,
                definition,
                reviewer_actor_id=authority.actor_ref.actor_id,
                reviewer_external_key=reviewer_request.external_actor_id,
                channel=authority.source.channel,
                expires_at=expires_at,
            )
            payload_json = GovernanceEventService._canonical_json(payload.model_dump(mode="json"))
            payload_root = GovernanceEventService._digest(payload_json.encode("utf-8"))
            self._require_global_idempotency_available(connection, idempotency_key)
            try:
                connection.execute(
                    """
                    INSERT INTO governance_review_card_commands(
                        idempotency_key, request_fingerprint,
                        project_namespace, project_id, proposal_id,
                        active_definition_digest, content_revision, state_revision,
                        decision_epoch, reviewer_actor_id, reviewer_actor_type,
                        reviewer_external_key, provider_installation_ref, channel_json,
                        expires_at, payload_digest, payload_json, requested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        request_fingerprint,
                        *self._identity(ref),
                        payload.active_definition_digest,
                        payload.content_revision,
                        payload.state_revision,
                        payload.decision_epoch,
                        authority.actor_ref.actor_id,
                        authority.actor_ref.actor_type.value,
                        reviewer_request.external_actor_id,
                        reviewer_request.provider_installation_ref,
                        GovernanceEventService._canonical_json(
                            authority.source.channel.model_dump(mode="json")
                        ),
                        self._timestamp(expires_at),
                        payload_root,
                        payload_json,
                        self._timestamp(requested_at),
                    ),
                )
            except sqlite3.IntegrityError as error:
                if "governance_review_card_one_snapshot" in str(error):
                    raise ReviewCardError("REVIEW_CARD_ALREADY_REQUESTED") from None
                raise
            audit, outbox = GovernanceEventService(
                self.store,
                clock=self._clock,
            )._append_review_card_in_transaction(
                connection,
                ref,
                request_key=idempotency_key,
                authority=authority,
                payload=payload,
            )
            if len(outbox) != 1:
                raise ReviewCardError("REVIEW_CARD_OUTBOX_INVALID")
            return ReviewCardRequestResult(
                idempotency_key=idempotency_key,
                payload=payload,
                audit_event_id=audit.event_id,
                outbox_event_id=outbox[0].event_id,
            )

    def _snapshot(self, ref: ProposalRef) -> tuple[object, ...]:
        with self.store.connect() as connection:
            return self._snapshot_in_connection(connection, ref)

    @staticmethod
    def _snapshot_in_connection(
        connection: sqlite3.Connection,
        ref: ProposalRef,
    ) -> tuple[object, ...]:
        row = connection.execute(
            """
            SELECT active_definition_digest, content_revision, state_revision,
                   decision_epoch, status
            FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            ReviewCardService._identity(ref),
        ).fetchone()
        if row is None:
            raise ReviewCardError("PROPOSAL_NOT_FOUND")
        if ActiveProposalStatus(str(row[4])) is not ActiveProposalStatus.REVIEWED:
            raise ReviewCardError("INVALID_PROPOSAL_STATE")
        return cast(tuple[object, ...], row)

    def _authenticate(
        self,
        request: DirectAuthorityRequest,
        connection: sqlite3.Connection,
    ) -> AuthorityContext:
        try:
            return self.authority_service.authenticate(request, connection=connection)
        except AuthorityResolutionError as error:
            raise ReviewCardError(error.code) from error

    @staticmethod
    def _payload(
        ref: ProposalRef,
        snapshot: tuple[object, ...],
        definition: ProposalDefinitionManifest,
        *,
        reviewer_actor_id: str,
        reviewer_external_key: str,
        channel: ChannelRef,
        expires_at: datetime,
    ) -> ReviewProjectionPayload:
        counts: Counter[str] = Counter()
        titles: list[str] = []
        for operation in definition.operations:
            try:
                operation_type = OperationType(str(operation["type"])).value
                raw_title = operation["title"]
            except (KeyError, ValueError) as error:
                raise ReviewCardError("REVIEW_CARD_DEFINITION_INVALID") from error
            if not isinstance(raw_title, str) or not raw_title.strip():
                raise ReviewCardError("REVIEW_CARD_DEFINITION_INVALID")
            counts[operation_type] += 1
            if len(titles) < 3:
                titles.append(ReviewCardService._truncate(raw_title.strip(), 256))
        return ReviewProjectionPayload(
            aggregate_ref=ref,
            active_definition_digest=str(snapshot[0]),
            content_revision=int(str(snapshot[1])),
            state_revision=int(str(snapshot[2])),
            decision_epoch=int(str(snapshot[3])),
            reviewer_actor_id=reviewer_actor_id,
            reviewer_external_key=reviewer_external_key,
            bound_channel_ref=channel,
            expires_at=expires_at,
            operation_count=len(definition.operations),
            operation_counts=dict(sorted(counts.items())),
            operation_titles=tuple(titles),
            remaining_operation_count=len(definition.operations) - len(titles),
        )

    @staticmethod
    def _replay(
        connection: sqlite3.Connection,
        idempotency_key: str,
        fingerprint: str,
    ) -> ReviewCardRequestResult | None:
        row = connection.execute(
            """
            SELECT c.request_fingerprint, c.payload_json, a.event_id, o.event_id
            FROM governance_review_card_commands c
            JOIN governance_audit_events a
              ON a.command_id = ?
             AND a.project_namespace = c.project_namespace
             AND a.project_id = c.project_id AND a.proposal_id = c.proposal_id
            JOIN governance_outbox_events o
              ON o.project_namespace = a.project_namespace
             AND o.project_id = a.project_id AND o.proposal_id = a.proposal_id
             AND o.aggregate_sequence = a.aggregate_sequence
            WHERE c.idempotency_key = ?
            """,
            (GovernanceEventService._review_card_command_id(idempotency_key), idempotency_key),
        ).fetchone()
        if row is None:
            exists = connection.execute(
                "SELECT request_fingerprint FROM governance_review_card_commands "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if exists is not None:
                raise ReviewCardError("REVIEW_CARD_ROOT_MISMATCH")
            return None
        if str(row[0]) != fingerprint:
            raise ReviewCardError("IDEMPOTENCY_CONFLICT")
        return ReviewCardRequestResult(
            idempotency_key=idempotency_key,
            payload=ReviewProjectionPayload.model_validate_json(str(row[1])),
            audit_event_id=str(row[2]),
            outbox_event_id=str(row[3]),
            replayed=True,
        )

    @staticmethod
    def _require_global_idempotency_available(
        connection: sqlite3.Connection,
        key: str,
    ) -> None:
        collision = connection.execute(
            """
            SELECT 1 FROM (
                SELECT idempotency_key FROM governance_decision_results
                UNION ALL SELECT idempotency_key FROM governance_apply_request_results
            ) WHERE idempotency_key = ? LIMIT 1
            """,
            (key,),
        ).fetchone()
        if collision is not None:
            raise ReviewCardError("IDEMPOTENCY_CONFLICT")

    @staticmethod
    def _require_snapshot_available(
        connection: sqlite3.Connection,
        ref: ProposalRef,
        *,
        active_definition_digest: str,
        content_revision: int,
        state_revision: int,
        decision_epoch: int,
    ) -> None:
        collision = connection.execute(
            """
            SELECT 1 FROM governance_review_card_commands
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
              AND active_definition_digest = ? AND state_revision = ?
              AND content_revision = ? AND decision_epoch = ?
            LIMIT 1
            """,
            (
                *ReviewCardService._identity(ref),
                active_definition_digest,
                state_revision,
                content_revision,
                decision_epoch,
            ),
        ).fetchone()
        if collision is not None:
            raise ReviewCardError("REVIEW_CARD_ALREADY_REQUESTED")

    @staticmethod
    def _validate_replay_key(key: str, fingerprint: str) -> None:
        if not key.strip() or len(key) > 512:
            raise ValueError("idempotency_key가 올바르지 않습니다.")
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("request_fingerprint는 lowercase SHA-256 hex여야 합니다.")

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    @staticmethod
    def _identity(ref: ProposalRef) -> tuple[str, str, str]:
        return ref.project_ref.namespace, ref.project_ref.project_id, ref.proposal_id

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp는 timezone-aware여야 합니다.")
        return value

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ReviewActionSetService:
    """Prepare or abandon one action generation after reconciliation decides."""

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

    def prepare(self, event: OutboxEventView) -> PreparedReviewActionSet:
        credentials: tuple[tuple[DecisionAction, str, str], ...] = ()
        issued: tuple[IssuedActionToken, ...] = ()
        prepared: PreparedReviewActionSet | None = None
        try:
            try:
                payload = ReviewProjectionPayload.model_validate(event.payload)
            except ValueError as error:
                raise ReviewCardError("REVIEW_CARD_PAYLOAD_INVALID") from error
            now = ReviewCardService._aware(self._clock())
            action_expires_at = now + ReviewCardService.ACTION_TTL
            with self.store.connect() as connection, governance_transaction(connection):
                command = self._command_for_event(connection, event, payload)
                request = DirectAuthorityRequest(
                    provider=ChannelProvider.SLACK,
                    provider_installation_ref=str(command[0]),
                    external_actor_id=payload.reviewer_external_key,
                    project_ref=payload.aggregate_ref.project_ref,
                    request_id=event.event_id,
                    channel=payload.bound_channel_ref,
                )
                try:
                    authority = self.authority_service.authenticate(request, connection=connection)
                except AuthorityResolutionError as error:
                    raise ReviewCardError(error.code) from error
                active = connection.execute(
                    """
                    SELECT generation, approve_token_id, request_changes_token_id, reject_token_id
                    FROM governance_review_action_sets
                    WHERE event_id = ? AND state = 'issued'
                    """,
                    (event.event_id,),
                ).fetchone()
                if active is not None:
                    token_ids = tuple(str(value) for value in active[1:4])
                    DecisionService._revoke_token_ids_in_transaction(
                        connection,
                        token_ids,
                        resolved_at=now,
                    )
                    connection.execute(
                        """
                        UPDATE governance_review_action_sets
                        SET state = 'revoked', resolved_at = ?
                        WHERE event_id = ? AND generation = ? AND state = 'issued'
                        """,
                        (ReviewCardService._timestamp(now), event.event_id, int(active[0])),
                    )
                latest = connection.execute(
                    "SELECT max(generation) FROM governance_review_action_sets WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                generation = (
                    int(latest[0]) if latest is not None and latest[0] is not None else 0
                ) + 1
                decisions = DecisionService(
                    self.store,
                    self.authority_service,
                    clock=self._clock,
                )
                try:
                    credentials = decisions._issue_tokens_in_transaction(
                        connection,
                        payload.aggregate_ref,
                        authority=authority,
                        issued_at=now,
                        expires_at=action_expires_at,
                    )
                except DecisionError as error:
                    raise ReviewCardError(error.code) from error
                action_token_ids = {action: token_id for action, token_id, _raw in credentials}
                connection.execute(
                    """
                    INSERT INTO governance_review_action_sets(
                        event_id, generation, approve_token_id,
                        request_changes_token_id, reject_token_id,
                        state, issued_at, expires_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, 'issued', ?, ?, NULL)
                    """,
                    (
                        event.event_id,
                        generation,
                        action_token_ids[DecisionAction.APPROVE],
                        action_token_ids[DecisionAction.REQUEST_CHANGES],
                        action_token_ids[DecisionAction.REJECT],
                        ReviewCardService._timestamp(now),
                        ReviewCardService._timestamp(action_expires_at),
                    ),
                )
                issued = tuple(
                    IssuedActionToken(
                        record=decisions._get_token_in_connection(connection, token_id),
                        raw_token=raw,
                    )
                    for _action, token_id, raw in credentials
                )
                prepared = PreparedReviewActionSet(
                    view=ReviewActionSetView(
                        event_id=event.event_id,
                        generation=generation,
                        approve_token_id=action_token_ids[DecisionAction.APPROVE],
                        request_changes_token_id=action_token_ids[DecisionAction.REQUEST_CHANGES],
                        reject_token_id=action_token_ids[DecisionAction.REJECT],
                        state="issued",
                        issued_at=now,
                        expires_at=action_expires_at,
                    ),
                    issued=issued,
                )
            assert prepared is not None
            return prepared
        except BaseException as error:
            DecisionService._clear_exception_frames(error)
            credentials = ()
            issued = ()
            prepared = None
            raise
        finally:
            credentials = ()
            issued = ()
            prepared = None

    def abandon(self, event_id: str, generation: int) -> None:
        now = ReviewCardService._aware(self._clock())
        with self.store.connect() as connection, governance_transaction(connection):
            row = connection.execute(
                """
                SELECT generation, approve_token_id, request_changes_token_id, reject_token_id
                FROM governance_review_action_sets
                WHERE event_id = ? AND generation = ? AND state = 'issued'
                """,
                (event_id, generation),
            ).fetchone()
            if row is None:
                return
            DecisionService._revoke_token_ids_in_transaction(
                connection,
                tuple(str(value) for value in row[1:4]),
                resolved_at=now,
            )
            connection.execute(
                """
                UPDATE governance_review_action_sets
                SET state = 'revoked', resolved_at = ?
                WHERE event_id = ? AND generation = ? AND state = 'issued'
                """,
                (ReviewCardService._timestamp(now), event_id, generation),
            )

    def get(self, event_id: str, generation: int) -> ReviewActionSetView:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT event_id, generation, approve_token_id,
                       request_changes_token_id, reject_token_id,
                       state, issued_at, expires_at, resolved_at
                FROM governance_review_action_sets
                WHERE event_id = ? AND generation = ?
                """,
                (event_id, generation),
            ).fetchone()
        if row is None:
            raise ReviewCardError("REVIEW_ACTION_SET_NOT_FOUND")
        return ReviewActionSetView.model_validate(
            dict(zip(ReviewActionSetView.model_fields, row, strict=True))
        )

    @staticmethod
    def _command_for_event(
        connection: sqlite3.Connection,
        event: OutboxEventView,
        payload: ReviewProjectionPayload,
    ) -> tuple[object, ...]:
        request_key = ReviewActionSetService._request_key_for_event(connection, event.event_id)
        row = connection.execute(
            """
            SELECT c.provider_installation_ref, c.payload_digest, c.payload_json
            FROM governance_review_card_commands c
            JOIN governance_audit_events a
              ON a.command_id = ?
             AND a.project_namespace = c.project_namespace
             AND a.project_id = c.project_id AND a.proposal_id = c.proposal_id
            JOIN governance_outbox_events o
              ON o.project_namespace = a.project_namespace
             AND o.project_id = a.project_id AND o.proposal_id = a.proposal_id
             AND o.aggregate_sequence = a.aggregate_sequence
            WHERE c.idempotency_key = ? AND o.event_id = ?
            """,
            (
                GovernanceEventService._review_card_command_id(request_key),
                request_key,
                event.event_id,
            ),
        ).fetchone()
        if row is None:
            raise ReviewCardError("REVIEW_CARD_ROOT_MISMATCH")
        canonical = GovernanceEventService._canonical_json(payload.model_dump(mode="json"))
        digest = GovernanceEventService._digest(canonical.encode("utf-8"))
        if str(row[1]) != digest or str(row[2]) != canonical or event.payload_digest != digest:
            raise ReviewCardError("REVIEW_CARD_ROOT_MISMATCH")
        return cast(tuple[object, ...], row)

    @staticmethod
    def _request_key_for_event(connection: sqlite3.Connection, event_id: str) -> str:
        rows = connection.execute(
            "SELECT idempotency_key FROM governance_review_card_commands"
        ).fetchall()
        audit = connection.execute(
            """
            SELECT a.command_id
            FROM governance_audit_events a
            JOIN governance_outbox_events o
              ON o.project_namespace = a.project_namespace
             AND o.project_id = a.project_id AND o.proposal_id = a.proposal_id
             AND o.aggregate_sequence = a.aggregate_sequence
            WHERE o.event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if audit is None:
            raise ReviewCardError("REVIEW_CARD_ROOT_MISMATCH")
        for candidate in rows:
            key = str(candidate[0])
            if GovernanceEventService._review_card_command_id(key) == str(audit[0]):
                return key
        raise ReviewCardError("REVIEW_CARD_ROOT_MISMATCH")
