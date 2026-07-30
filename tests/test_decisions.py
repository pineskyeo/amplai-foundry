from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActionTokenState,
    ActiveProposalRepository,
    ActiveProposalStatus,
    ActorRef,
    ActorType,
    ChannelProvider,
    ChannelRef,
    DecisionAction,
    DecisionError,
    DecisionService,
    ImmutableDefinitionObjectStore,
    ProposalDefinitionManifest,
    ProposalRef,
    canonicalize_definition,
)
from amplai_foundry.governance.object_store import DefinitionObjectRef
from amplai_foundry.governance.store import GovernanceStore

PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
PROPOSAL = ProposalRef(project_ref=PROJECT, proposal_id="PROP-20260730-ABCDEF12")
ACTOR = ActorRef(actor_id="ACT-HUMAN-1", actor_type=ActorType.HUMAN)
OTHER_ACTOR = ActorRef(actor_id="ACT-HUMAN-2", actor_type=ActorType.HUMAN)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000200",
)
OTHER_CHANNEL = CHANNEL.model_copy(update={"message_id": "1710000000.000201"})
NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
FINGERPRINT = hashlib.sha256(b"provider-request-1").hexdigest()


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
]:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    objects = ImmutableDefinitionObjectStore(PROJECT, tmp_path)
    active = ActiveProposalRepository(store, objects)
    definition = _definition(objects, "initial")
    initial = active.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=definition,
    )
    active.transition_state(
        PROPOSAL,
        expected_status=ActiveProposalStatus.DRAFT,
        expected_state_revision=initial.state_revision,
        next_status=ActiveProposalStatus.REVIEWED,
    )
    return store, objects, active, DecisionService(store)


def _tokens_by_action(service: DecisionService) -> dict[DecisionAction, object]:
    return {
        token.record.allowed_action: token
        for token in service.issue_tokens(
            PROPOSAL,
            actor_ref=ACTOR,
            channel_ref=CHANNEL,
            now=NOW,
        )
    }


def test_issues_three_separate_hash_only_tokens_bound_to_reviewed_snapshot(
    tmp_path: Path,
) -> None:
    store, _objects, active, service = _reviewed(tmp_path)
    issued = service.issue_tokens(
        PROPOSAL,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        now=NOW,
    )
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


def test_success_consumes_token_and_replay_returns_first_result_before_live_validation(
    tmp_path: Path,
) -> None:
    _store, _objects, active, service = _reviewed(tmp_path)
    issued = service.issue_tokens(
        PROPOSAL,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        now=NOW,
    )
    approve = next(
        token for token in issued if token.record.allowed_action is DecisionAction.APPROVE
    )

    first = service.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        raw_token=approve.raw_token,
        idempotency_key="decision-1",
        request_fingerprint=FINGERPRINT,
        now=NOW + timedelta(seconds=1),
    )
    replay = service.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        raw_token=approve.raw_token,
        idempotency_key="decision-1",
        request_fingerprint=FINGERPRINT,
        now=NOW + timedelta(days=1),
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
    store, _objects, active, service = _reviewed(tmp_path)
    approve = next(
        token
        for token in service.issue_tokens(PROPOSAL, actor_ref=ACTOR, channel_ref=CHANNEL, now=NOW)
        if token.record.allowed_action is DecisionAction.APPROVE
    )
    service.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        raw_token=approve.raw_token,
        idempotency_key="decision-1",
        request_fingerprint=FINGERPRINT,
        now=NOW,
    )
    before = active.get(PROPOSAL)

    with pytest.raises(DecisionError, match="IDEMPOTENCY_CONFLICT"):
        service.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            actor_ref=ACTOR,
            channel_ref=CHANNEL,
            raw_token=approve.raw_token,
            idempotency_key="decision-1",
            request_fingerprint=hashlib.sha256(b"different").hexdigest(),
            now=NOW,
        )

    assert active.get(PROPOSAL) == before
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone() == (1,)


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
    _store, _objects, active, service = _reviewed(tmp_path)
    approve = next(
        token
        for token in service.issue_tokens(PROPOSAL, actor_ref=ACTOR, channel_ref=CHANNEL, now=NOW)
        if token.record.allowed_action is DecisionAction.APPROVE
    )
    before = active.get(PROPOSAL)
    kwargs = {
        "action": DecisionAction.APPROVE,
        "actor_ref": ACTOR,
        "channel_ref": CHANNEL,
        "now": NOW,
    }
    if mutation == "actor":
        kwargs["actor_ref"] = OTHER_ACTOR
    elif mutation == "channel":
        kwargs["channel_ref"] = OTHER_CHANNEL
    elif mutation == "action":
        kwargs["action"] = DecisionAction.REJECT
    elif mutation == "expired":
        kwargs["now"] = NOW + timedelta(minutes=16)

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


def test_result_insert_failure_rolls_back_proposal_and_token(tmp_path: Path) -> None:
    store, _objects, active, service = _reviewed(tmp_path)
    approve = next(
        token
        for token in service.issue_tokens(PROPOSAL, actor_ref=ACTOR, channel_ref=CHANNEL, now=NOW)
        if token.record.allowed_action is DecisionAction.APPROVE
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
            actor_ref=ACTOR,
            channel_ref=CHANNEL,
            raw_token=approve.raw_token,
            idempotency_key="rollback",
            request_fingerprint=FINGERPRINT,
            now=NOW,
        )

    assert active.get(PROPOSAL) == before
    assert service.get_token(approve.record.token_id).state is ActionTokenState.ISSUED


def test_stale_reviewed_snapshot_fails_without_mutation(tmp_path: Path) -> None:
    store, _objects, active, service = _reviewed(tmp_path)
    approve = next(
        token
        for token in service.issue_tokens(PROPOSAL, actor_ref=ACTOR, channel_ref=CHANNEL, now=NOW)
        if token.record.allowed_action is DecisionAction.APPROVE
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
            actor_ref=ACTOR,
            channel_ref=CHANNEL,
            raw_token=approve.raw_token,
            idempotency_key="stale",
            request_fingerprint=FINGERPRINT,
            now=NOW,
        )

    assert active.get(PROPOSAL) == before
    assert service.get_token(approve.record.token_id).state is ActionTokenState.ISSUED


def test_expiration_sweeper_changes_only_elapsed_issued_tokens(tmp_path: Path) -> None:
    _store, _objects, active, service = _reviewed(tmp_path)
    issued = service.issue_tokens(
        PROPOSAL,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        now=NOW,
    )
    before = active.get(PROPOSAL)

    assert service.expire_tokens(now=NOW + timedelta(minutes=14)) == 0
    assert service.expire_tokens(now=NOW + timedelta(minutes=15)) == 3
    assert service.expire_tokens(now=NOW + timedelta(minutes=16)) == 0
    assert active.get(PROPOSAL) == before
    assert {service.get_token(token.record.token_id).state for token in issued} == {
        ActionTokenState.EXPIRED
    }


def test_concurrent_use_of_one_token_has_one_winner_and_one_state_increment(
    tmp_path: Path,
) -> None:
    store, _objects, active, service = _reviewed(tmp_path)
    approve = next(
        token
        for token in service.issue_tokens(PROPOSAL, actor_ref=ACTOR, channel_ref=CHANNEL, now=NOW)
        if token.record.allowed_action is DecisionAction.APPROVE
    )

    def decide(key: str) -> str:
        try:
            service.decide(
                PROPOSAL,
                action=DecisionAction.APPROVE,
                actor_ref=ACTOR,
                channel_ref=CHANNEL,
                raw_token=approve.raw_token,
                idempotency_key=key,
                request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
                now=NOW,
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
    store, _objects, active, service = _reviewed(tmp_path)
    approve = next(
        token
        for token in service.issue_tokens(PROPOSAL, actor_ref=ACTOR, channel_ref=CHANNEL, now=NOW)
        if token.record.allowed_action is DecisionAction.APPROVE
    )

    def decide() -> bool:
        return service.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            actor_ref=ACTOR,
            channel_ref=CHANNEL,
            raw_token=approve.raw_token,
            idempotency_key="same-command",
            request_fingerprint=FINGERPRINT,
            now=NOW,
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
    _store, objects, active, service = _reviewed(tmp_path)
    issued = service.issue_tokens(
        PROPOSAL,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        now=NOW,
    )
    request_changes = next(
        token for token in issued if token.record.allowed_action is DecisionAction.REQUEST_CHANGES
    )
    service.decide(
        PROPOSAL,
        action=DecisionAction.REQUEST_CHANGES,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        raw_token=request_changes.raw_token,
        idempotency_key="request-changes",
        request_fingerprint=FINGERPRINT,
        now=NOW,
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
    store, objects, active, service = _reviewed(tmp_path)
    issued = service.issue_tokens(
        PROPOSAL,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        now=NOW,
    )
    request_changes = next(
        token for token in issued if token.record.allowed_action is DecisionAction.REQUEST_CHANGES
    )
    service.decide(
        PROPOSAL,
        action=DecisionAction.REQUEST_CHANGES,
        actor_ref=ACTOR,
        channel_ref=CHANNEL,
        raw_token=request_changes.raw_token,
        idempotency_key="request-changes",
        request_fingerprint=FINGERPRINT,
        now=NOW,
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
