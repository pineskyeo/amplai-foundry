from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActionTokenState,
    ActiveProposalRepository,
    ActiveProposalStatus,
    ActorBindingService,
    ActorRef,
    ActorType,
    AuthorityPermission,
    AuthorityService,
    BindingApproval,
    BindingTarget,
    ChannelProvider,
    ChannelRef,
    DecisionAction,
    DecisionError,
    DecisionService,
    DirectAuthorityRequest,
    ImmutableDefinitionObjectStore,
    IngressAuthorityRequest,
    ProposalDefinitionManifest,
    ProposalRef,
    canonicalize_definition,
)
from amplai_foundry.governance.object_store import DefinitionObjectRef
from amplai_foundry.governance.store import (
    GovernanceCommitAmbiguousError,
    GovernanceStore,
    governance_transaction,
)

PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
AUTHORITY_PROJECT = ProjectRef(
    project_id="governance",
    namespace="org/default/project/governance",
)
PROPOSAL = ProposalRef(project_ref=PROJECT, proposal_id="PROP-20260730-ABCDEF12")
ACTOR = ActorRef(actor_id="ACT-HUMAN-1", actor_type=ActorType.HUMAN)
OTHER_ACTOR = ActorRef(actor_id="ACT-HUMAN-2", actor_type=ActorType.HUMAN)
MANAGER = ActorRef(actor_id="ACT-MANAGER-1", actor_type=ActorType.HUMAN)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000200",
)
OTHER_CHANNEL = CHANNEL.model_copy(update={"message_id": "1710000000.000201"})
NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
FINGERPRINT = hashlib.sha256(b"provider-request-1").hexdigest()


def _approval(index: int) -> BindingApproval:
    return BindingApproval(
        approval_id=f"APR-{index:016X}",
        approved_by=MANAGER,
        reason="approve decision authority fixture",
    )


def _authority_request(
    *,
    external_actor_id: str = "U123",
    channel: ChannelRef = CHANNEL,
) -> DirectAuthorityRequest:
    return DirectAuthorityRequest(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        external_actor_id=external_actor_id,
        project_ref=PROJECT,
        request_id="CMD-0123456789ABCDEF",
        channel=channel,
    )


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class CommitAfterSuccessConnection:
    def __init__(self, connection: sqlite3.Connection, store: CommitAfterSuccessStore) -> None:
        self._connection = connection
        self._store = store

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def execute(self, statement: str, parameters: object = ()) -> sqlite3.Cursor:
        if parameters == ():
            cursor = self._connection.execute(statement)
        else:
            cursor = self._connection.execute(statement, parameters)  # type: ignore[arg-type]
        if statement == "COMMIT" and self._store.fail_next_commit:
            self._store.fail_next_commit = False
            raise sqlite3.OperationalError("injected after durable commit")
        return cursor


class CommitAfterSuccessStore(GovernanceStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_next_commit = True

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with super().connect() as connection:
            yield CommitAfterSuccessConnection(connection, self)  # type: ignore[misc]


def _definition(
    objects: ImmutableDefinitionObjectStore,
    marker: str,
) -> DefinitionObjectRef:
    canonical = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=PROPOSAL,
            operations=({"marker": marker, "type": "CREATE"},),
            base_revision="a13d92f",
            validation_policy_ref="policy/proposal-v3",
        )
    )
    return objects.put_definition_object(PROPOSAL, canonical.canonical_bytes, canonical.digest)


def _reviewed(
    tmp_path: Path,
) -> tuple[
    GovernanceStore,
    ImmutableDefinitionObjectStore,
    ActiveProposalRepository,
    DecisionService,
    MutableClock,
]:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    clock = MutableClock(NOW)
    bindings = ActorBindingService(store, AUTHORITY_PROJECT, clock=clock)
    bindings.bootstrap_manager(MANAGER)
    bindings.register_actor(ACTOR, approval=_approval(1))
    bindings.register_actor(OTHER_ACTOR, approval=_approval(2))
    bindings.grant_permission(
        ACTOR,
        PROJECT,
        AuthorityPermission.PROPOSAL_DECIDE,
        approval=_approval(3),
    )
    bindings.grant_permission(
        OTHER_ACTOR,
        PROJECT,
        AuthorityPermission.PROPOSAL_DECIDE,
        approval=_approval(4),
    )
    bindings.create_binding(
        BindingTarget(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:APP1",
            external_actor_id="U123",
            actor_ref=ACTOR,
        ),
        approval=_approval(5),
    )
    bindings.create_binding(
        BindingTarget(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:APP1",
            external_actor_id="U999",
            actor_ref=OTHER_ACTOR,
        ),
        approval=_approval(6),
    )
    objects = ImmutableDefinitionObjectStore(PROJECT, tmp_path)
    active = ActiveProposalRepository(store, objects)
    definition = _definition(objects, "initial")
    initial = active.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=definition,
    )
    active.submit_for_review(
        PROPOSAL,
        expected_state_revision=initial.state_revision,
    )
    authority = AuthorityService(store, clock=clock)
    return store, objects, active, DecisionService(store, authority, clock=clock), clock


def _issue(service: DecisionService):
    return service.issue_tokens(PROPOSAL, authority_request=_authority_request())


def test_issues_three_separate_hash_only_tokens_bound_to_reviewed_snapshot(
    tmp_path: Path,
) -> None:
    store, _objects, active, service, _clock = _reviewed(tmp_path)
    issued = _issue(service)
    current = active.get(PROPOSAL)
    assert current is not None

    assert {token.record.allowed_action for token in issued} == set(DecisionAction)
    assert len({token.raw_token for token in issued}) == 3
    assert all(len(token.raw_token.encode()) <= 64 for token in issued)
    assert all("raw_token=<redacted>" in repr(token) for token in issued)
    assert all(token.record.state is ActionTokenState.ISSUED for token in issued)
    assert all(token.record.allowed_actor_ref == ACTOR for token in issued)
    assert all(token.record.bound_channel_ref == CHANNEL for token in issued)
    assert all(
        token.record.active_definition_digest == current.active_definition_digest
        for token in issued
    )
    assert all(token.record.state_revision == current.state_revision for token in issued)

    database_bytes = store.path.read_bytes()
    assert all(token.raw_token.encode() not in database_bytes for token in issued)
    with store.connect() as connection:
        stored_hashes = {
            str(row[0])
            for row in connection.execute(
                "SELECT token_hash FROM governance_action_tokens"
            ).fetchall()
        }
    assert stored_hashes == {
        f"sha256:{hashlib.sha256(token.raw_token.encode()).hexdigest()}" for token in issued
    }
    with store.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE governance_action_tokens SET token_id = ? WHERE token_id = ?",
            ("TOK-A!!!!!!!!!!!!!!!", issued[0].record.token_id),
        )


def test_unmapped_actor_cannot_issue_decision_tokens(tmp_path: Path) -> None:
    _store, _objects, _active, service, _clock = _reviewed(tmp_path)

    with pytest.raises(DecisionError, match="ACTOR_UNMAPPED"):
        service.issue_tokens(
            PROPOSAL,
            authority_request=_authority_request(external_actor_id="U-UNMAPPED"),
        )


def test_disabled_actor_cannot_consume_previously_issued_token(tmp_path: Path) -> None:
    store, _objects, active, service, clock = _reviewed(tmp_path)
    approve = next(
        token for token in _issue(service) if token.record.allowed_action is DecisionAction.APPROVE
    )
    ActorBindingService(store, AUTHORITY_PROJECT, clock=clock).disable_actor(
        ACTOR,
        approval=_approval(7),
    )
    before = active.get(PROPOSAL)

    with pytest.raises(DecisionError, match="ACTOR_DISABLED"):
        service.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=approve.raw_token,
            idempotency_key="disabled-actor",
            request_fingerprint=FINGERPRINT,
        )

    assert active.get(PROPOSAL) == before
    assert service.get_token(approve.record.token_id).state is ActionTokenState.ISSUED


def test_success_consumes_token_and_replay_returns_first_result_before_live_validation(
    tmp_path: Path,
) -> None:
    _store, _objects, active, service, clock = _reviewed(tmp_path)
    issued = _issue(service)
    approve = next(
        token for token in issued if token.record.allowed_action is DecisionAction.APPROVE
    )

    first = service.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_authority_request(),
        raw_token=approve.raw_token,
        idempotency_key="decision-1",
        request_fingerprint=FINGERPRINT,
    )
    clock.value = NOW + timedelta(days=1)
    replay = service.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_authority_request(),
        raw_token=approve.raw_token,
        idempotency_key="decision-1",
        request_fingerprint=FINGERPRINT,
    )

    assert first.proposal_status is ActiveProposalStatus.APPROVED
    assert replay.model_copy(update={"replayed": False}) == first
    assert replay.replayed
    assert service.get_token(approve.record.token_id).state is ActionTokenState.CONSUMED
    current = active.get(PROPOSAL)
    assert current is not None
    assert current.status is ActiveProposalStatus.APPROVED
    assert current.state_revision == approve.record.state_revision + 1


def test_idempotency_conflict_precedes_consumed_token_and_changes_nothing(tmp_path: Path) -> None:
    store, _objects, active, service, _clock = _reviewed(tmp_path)
    approve = next(
        token for token in _issue(service) if token.record.allowed_action is DecisionAction.APPROVE
    )
    service.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_authority_request(),
        raw_token=approve.raw_token,
        idempotency_key="decision-1",
        request_fingerprint=FINGERPRINT,
    )
    before = active.get(PROPOSAL)

    with pytest.raises(DecisionError, match="IDEMPOTENCY_CONFLICT"):
        service.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=approve.raw_token,
            idempotency_key="decision-1",
            request_fingerprint=hashlib.sha256(b"different").hexdigest(),
        )
    assert active.get(PROPOSAL) == before
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone() == (1,)


def test_verified_hash_decision_bypass_is_not_public(tmp_path: Path) -> None:
    _store, _objects, _active, service, _clock = _reviewed(tmp_path)

    assert not hasattr(service, "decide_verified_hash_in_transaction")


def test_ingress_decision_derives_project_actor_and_channel_from_durable_records(
    tmp_path: Path,
) -> None:
    store, _objects, active, service, _clock = _reviewed(tmp_path)
    approve = next(
        token for token in _issue(service) if token.record.allowed_action is DecisionAction.APPROVE
    )
    command_id = "CMD-0123456789ABCDEF"
    credential_hash = f"sha256:{hashlib.sha256(approve.raw_token.encode()).hexdigest()}"
    channel_json = json.dumps(
        CHANNEL.model_dump(mode="json", exclude_none=True),
        separators=(",", ":"),
        sort_keys=True,
    )
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_ingress_commands(
                command_id, provider, provider_installation_ref, provider_fingerprint,
                raw_body_digest, external_event_id, external_actor_key, channel_json,
                credential_kind, credential_id, credential_hash, action, received_at,
                state, attempts, claim_generation, lease_owner, lease_expires_at,
                retry_at, completed_at, last_error_code
            ) VALUES (?, 'slack', 'T123:APP1', ?, ?, 'EVT-INGRESS', 'U123', ?,
                      'action_token', ?, ?, 'approve', ?, 'leased', 1, 1,
                      'worker', ?, NULL, NULL, NULL)
            """,
            (
                command_id,
                hashlib.sha256(b"provider-fingerprint").hexdigest(),
                f"sha256:{hashlib.sha256(b'body').hexdigest()}",
                channel_json,
                approve.record.token_id,
                credential_hash,
                NOW.isoformat(),
                (NOW + timedelta(minutes=1)).isoformat(),
            ),
        )

    with store.connect() as connection, governance_transaction(connection):
        result = service.decide_ingress_in_transaction(
            connection,
            IngressAuthorityRequest(
                command_id=command_id,
                worker_id="worker",
                generation=1,
            ),
            idempotency_key="ingress-decision",
            request_fingerprint=FINGERPRINT,
        )

    assert result.proposal_ref == PROPOSAL
    assert result.proposal_status is ActiveProposalStatus.APPROVED
    current = active.get(PROPOSAL)
    assert current is not None and current.status is ActiveProposalStatus.APPROVED


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("actor", "ACTION_ACTOR_MISMATCH"),
        ("channel", "ACTION_CHANNEL_MISMATCH"),
        ("action", "ACTION_TOKEN_INVALID"),
        ("expired", "ACTION_TOKEN_EXPIRED"),
    ],
)
def test_failed_decision_does_not_mutate_proposal_or_token(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    _store, _objects, active, service, clock = _reviewed(tmp_path)
    approve = next(
        token for token in _issue(service) if token.record.allowed_action is DecisionAction.APPROVE
    )
    before = active.get(PROPOSAL)
    kwargs = {
        "action": DecisionAction.APPROVE,
        "authority_request": _authority_request(),
    }
    if mutation == "actor":
        kwargs["authority_request"] = _authority_request(external_actor_id="U999")
    elif mutation == "channel":
        kwargs["authority_request"] = _authority_request(channel=OTHER_CHANNEL)
    elif mutation == "action":
        kwargs["action"] = DecisionAction.REJECT
    elif mutation == "expired":
        clock.value = NOW + timedelta(minutes=16)

    with pytest.raises(DecisionError, match=code):
        service.decide(
            PROPOSAL,
            raw_token=approve.raw_token,
            idempotency_key=f"failure-{mutation}",
            request_fingerprint=FINGERPRINT,
            **kwargs,  # type: ignore[arg-type]
        )

    assert active.get(PROPOSAL) == before
    assert service.get_token(approve.record.token_id).state is ActionTokenState.ISSUED


def test_after_commit_ambiguity_reconciles_by_durable_result_replay(tmp_path: Path) -> None:
    store, _objects, active, service, clock = _reviewed(tmp_path)
    approve = next(
        token for token in _issue(service) if token.record.allowed_action is DecisionAction.APPROVE
    )
    ambiguous_store = CommitAfterSuccessStore(store.path)
    ambiguous = DecisionService(
        ambiguous_store,
        AuthorityService(ambiguous_store, clock=clock),
        clock=clock,
    )

    with pytest.raises(GovernanceCommitAmbiguousError):
        ambiguous.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=approve.raw_token,
            idempotency_key="commit-ambiguous",
            request_fingerprint=FINGERPRINT,
        )

    replay = service.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_authority_request(),
        raw_token=approve.raw_token,
        idempotency_key="commit-ambiguous",
        request_fingerprint=FINGERPRINT,
    )
    assert replay.replayed
    current = active.get(PROPOSAL)
    assert current is not None
    assert current.status is ActiveProposalStatus.APPROVED
    assert current.state_revision == approve.record.state_revision + 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone() == (1,)


def test_result_insert_failure_rolls_back_proposal_and_token(tmp_path: Path) -> None:
    store, _objects, active, service, _clock = _reviewed(tmp_path)
    approve = next(
        token for token in _issue(service) if token.record.allowed_action is DecisionAction.APPROVE
    )
    before = active.get(PROPOSAL)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_decision_result
            BEFORE INSERT ON governance_decision_results
            BEGIN
                SELECT RAISE(ABORT, 'injected result failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected result failure"):
        service.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=approve.raw_token,
            idempotency_key="rollback",
            request_fingerprint=FINGERPRINT,
        )

    assert active.get(PROPOSAL) == before
    assert service.get_token(approve.record.token_id).state is ActionTokenState.ISSUED


def test_stale_reviewed_snapshot_fails_without_mutation(tmp_path: Path) -> None:
    store, _objects, active, service, _clock = _reviewed(tmp_path)
    approve = next(
        token for token in _issue(service) if token.record.allowed_action is DecisionAction.APPROVE
    )
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE governance_active_proposals
            SET state_revision = state_revision + 1
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (PROJECT.namespace, PROJECT.project_id, PROPOSAL.proposal_id),
        )
    before = active.get(PROPOSAL)

    with pytest.raises(DecisionError, match="PROPOSAL_STALE"):
        service.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=approve.raw_token,
            idempotency_key="stale",
            request_fingerprint=FINGERPRINT,
        )

    assert active.get(PROPOSAL) == before
    assert service.get_token(approve.record.token_id).state is ActionTokenState.ISSUED


def test_expiration_sweeper_changes_only_elapsed_issued_tokens(tmp_path: Path) -> None:
    _store, _objects, active, service, clock = _reviewed(tmp_path)
    issued = _issue(service)
    before = active.get(PROPOSAL)

    clock.value = NOW + timedelta(minutes=14)
    assert service.expire_tokens() == 0
    clock.value = NOW + timedelta(minutes=15)
    assert service.expire_tokens() == 3
    clock.value = NOW + timedelta(minutes=16)
    assert service.expire_tokens() == 0
    assert active.get(PROPOSAL) == before
    assert {service.get_token(token.record.token_id).state for token in issued} == {
        ActionTokenState.EXPIRED
    }


def test_concurrent_use_of_one_token_has_one_winner_and_one_state_increment(
    tmp_path: Path,
) -> None:
    store, _objects, active, service, _clock = _reviewed(tmp_path)
    approve = next(
        token for token in _issue(service) if token.record.allowed_action is DecisionAction.APPROVE
    )

    def decide(key: str) -> str:
        try:
            service.decide(
                PROPOSAL,
                action=DecisionAction.APPROVE,
                authority_request=_authority_request(),
                raw_token=approve.raw_token,
                idempotency_key=key,
                request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
            )
        except DecisionError as error:
            return error.code
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(decide, ("race-1", "race-2")))

    assert sorted(outcomes) == ["ACTION_TOKEN_CONSUMED", "accepted"]
    current = active.get(PROPOSAL)
    assert current is not None
    assert current.state_revision == approve.record.state_revision + 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone() == (1,)


def test_concurrent_same_command_returns_one_result_and_one_replay(tmp_path: Path) -> None:
    store, _objects, active, service, _clock = _reviewed(tmp_path)
    approve = next(
        token for token in _issue(service) if token.record.allowed_action is DecisionAction.APPROVE
    )

    def decide() -> bool:
        return service.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=approve.raw_token,
            idempotency_key="same-command",
            request_fingerprint=FINGERPRINT,
        ).replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        replay_flags = tuple(executor.map(lambda _index: decide(), range(2)))

    assert sorted(replay_flags) == [False, True]
    current = active.get(PROPOSAL)
    assert current is not None
    assert current.state_revision == approve.record.state_revision + 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone() == (1,)


def test_definition_revision_revokes_remaining_tokens_from_previous_epoch(
    tmp_path: Path,
) -> None:
    _store, objects, active, service, _clock = _reviewed(tmp_path)
    issued = _issue(service)
    request_changes = next(
        token for token in issued if token.record.allowed_action is DecisionAction.REQUEST_CHANGES
    )
    service.decide(
        PROPOSAL,
        action=DecisionAction.REQUEST_CHANGES,
        authority_request=_authority_request(),
        raw_token=request_changes.raw_token,
        idempotency_key="request-changes",
        request_fingerprint=FINGERPRINT,
    )
    before_revision = active.get(PROPOSAL)
    assert before_revision is not None
    second = _definition(objects, "second")

    revised = active.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=before_revision.active_definition_digest,
        expected_state_revision=before_revision.state_revision,
        next_object_ref=second,
    )

    assert revised.decision_epoch == before_revision.decision_epoch + 1
    states = {
        token.record.allowed_action: service.get_token(token.record.token_id).state
        for token in issued
    }
    assert states[DecisionAction.REQUEST_CHANGES] is ActionTokenState.CONSUMED
    assert states[DecisionAction.APPROVE] is ActionTokenState.REVOKED
    assert states[DecisionAction.REJECT] is ActionTokenState.REVOKED


def test_definition_revision_failure_rolls_back_token_revocation(tmp_path: Path) -> None:
    store, objects, active, service, _clock = _reviewed(tmp_path)
    issued = _issue(service)
    request_changes = next(
        token for token in issued if token.record.allowed_action is DecisionAction.REQUEST_CHANGES
    )
    service.decide(
        PROPOSAL,
        action=DecisionAction.REQUEST_CHANGES,
        authority_request=_authority_request(),
        raw_token=request_changes.raw_token,
        idempotency_key="request-changes",
        request_fingerprint=FINGERPRINT,
    )
    before = active.get(PROPOSAL)
    assert before is not None
    second = _definition(objects, "rollback-second")
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_second_revision
            BEFORE INSERT ON governance_definition_revisions
            WHEN NEW.content_revision = 2
            BEGIN
                SELECT RAISE(ABORT, 'injected revision failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected revision failure"):
        active.activate_definition_revision(
            PROPOSAL,
            expected_active_digest=before.active_definition_digest,
            expected_state_revision=before.state_revision,
            next_object_ref=second,
        )

    assert active.get(PROPOSAL) == before
    states = {
        token.record.allowed_action: service.get_token(token.record.token_id).state
        for token in issued
    }
    assert states[DecisionAction.APPROVE] is ActionTokenState.ISSUED
    assert states[DecisionAction.REJECT] is ActionTokenState.ISSUED
