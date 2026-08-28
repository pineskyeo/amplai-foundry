from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActiveProposalRepository,
    ActiveProposalStatus,
    ActorBindingService,
    ActorRef,
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    AuthorityService,
    AuthoritySource,
    BindingApproval,
    BindingTarget,
    ChannelProvider,
    ChannelRef,
    DecisionAction,
    DecisionError,
    DecisionService,
    DirectAuthorityRequest,
    GovernanceEventError,
    ImmutableDefinitionObjectStore,
    OutboxConfig,
    OutboxDestination,
    OutboxDispatcher,
    OutboxLeaseConflictError,
    OutboxReconcileError,
    OutboxRetryableError,
    OutboxState,
    ProposalDefinitionManifest,
    ProposalRef,
    ProposalSubmissionService,
    YamlProjectionDestination,
    canonicalize_definition,
)
from amplai_foundry.governance import events as events_module
from amplai_foundry.governance.events import (
    DecisionProjectionPayload,
    GovernanceEventService,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
AUTHORITY_PROJECT = ProjectRef(
    project_id="governance",
    namespace="org/default/project/governance",
)
PROPOSAL = ProposalRef(project_ref=PROJECT, proposal_id="PROP-20260730-ABCDEF12")
ACTOR = ActorRef(actor_id="ACT-HUMAN-1", actor_type=ActorType.HUMAN)
MANAGER = ActorRef(actor_id="ACT-MANAGER-1", actor_type=ActorType.HUMAN)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000200",
)


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def _authority() -> AuthorityContext:
    return AuthorityContext(
        actor_ref=ACTOR,
        project_ref=PROJECT,
        permissions=frozenset(
            {
                AuthorityPermission.PROPOSAL_READ,
                AuthorityPermission.PROPOSAL_SUBMIT_REVIEW,
                AuthorityPermission.PROPOSAL_DECIDE,
            }
        ),
        source=AuthoritySource(request_id="request-1", channel=CHANNEL),
        authenticated_at=NOW,
    )


def test_non_slack_destinations_remain_distinct_per_chat() -> None:
    first_channel = ChannelRef(
        provider=ChannelProvider.TELEGRAM,
        chat_id="CHAT-1",
        thread_id="THREAD-1",
        message_id="MESSAGE-1",
    )
    second_channel = first_channel.model_copy(
        update={"chat_id": "CHAT-2", "message_id": "MESSAGE-2"}
    )
    first_authority = _authority().model_copy(
        update={
            "source": AuthoritySource(request_id="telegram-1", channel=first_channel),
        }
    )
    second_authority = _authority().model_copy(
        update={
            "source": AuthoritySource(request_id="telegram-2", channel=second_channel),
        }
    )

    first = GovernanceEventService._decision_destinations(PROPOSAL, first_authority)[1]
    second = GovernanceEventService._decision_destinations(PROPOSAL, second_authority)[1]

    assert first.destination_ref != second.destination_ref
    assert first == GovernanceEventService._legacy_provider_destination(PROPOSAL, first_channel)
    assert second == GovernanceEventService._legacy_provider_destination(PROPOSAL, second_channel)


def _approval(index: int) -> BindingApproval:
    return BindingApproval(
        approval_id=f"APR-{index:016X}",
        approved_by=MANAGER,
        reason="approve governance event fixture",
    )


def _authority_request() -> DirectAuthorityRequest:
    return DirectAuthorityRequest(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        external_actor_id="U123",
        project_ref=PROJECT,
        request_id="request-1",
        channel=CHANNEL,
    )


def _active_proposal(
    tmp_path: Path,
    *,
    store: GovernanceStore | None = None,
):
    governance = store or GovernanceStore(tmp_path / "governance.db")
    governance.initialize()
    objects = ImmutableDefinitionObjectStore(PROJECT, tmp_path)
    canonical = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=PROPOSAL,
            operations=({"marker": "initial", "type": "CREATE"},),
            base_revision="a13d92f",
            validation_policy_ref="policy/proposal-v3",
        )
    )
    object_ref = objects.put_definition_object(
        PROPOSAL,
        canonical.canonical_bytes,
        canonical.digest,
    )
    active = ActiveProposalRepository(governance, objects)
    view = active.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=object_ref,
    )
    return governance, active, view


def _decision_fixture(tmp_path: Path):
    store, active, draft = _active_proposal(tmp_path)
    bindings = ActorBindingService(store, AUTHORITY_PROJECT, clock=lambda: NOW)
    bindings.bootstrap_manager(MANAGER)
    bindings.register_actor(ACTOR, approval=_approval(1))
    for index, permission in enumerate(
        (
            AuthorityPermission.PROPOSAL_READ,
            AuthorityPermission.PROPOSAL_SUBMIT_REVIEW,
            AuthorityPermission.PROPOSAL_DECIDE,
        ),
        start=2,
    ):
        bindings.grant_permission(ACTOR, PROJECT, permission, approval=_approval(index))
    bindings.create_binding(
        BindingTarget(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:APP1",
            external_actor_id="U123",
            actor_ref=ACTOR,
        ),
        approval=_approval(5),
    )
    authority = AuthorityService(store, clock=lambda: NOW)
    ProposalSubmissionService(store, active, authority).submit_for_review(
        PROPOSAL,
        authority_request=_authority_request(),
        expected_state_revision=draft.state_revision,
    )
    service = DecisionService(
        store,
        authority,
        clock=lambda: NOW,
    )
    return store, active, service


def _append(
    service: GovernanceEventService,
    store: GovernanceStore,
    *,
    command_id: str,
    state_revision: int,
    destinations: tuple[OutboxDestination, ...] = (),
):
    # Production destinations are derived from the persisted Decision authority.
    # The legacy argument remains only to keep scenario call sites concise.
    del destinations
    with store.connect() as connection, governance_transaction(connection):
        proposal = connection.execute(
            """
            SELECT active_definition_digest, content_revision, decision_epoch
            FROM governance_active_proposals
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (PROJECT.namespace, PROJECT.project_id, PROPOSAL.proposal_id),
        ).fetchone()
        assert proposal is not None
        token_id = f"TOK-{hashlib.sha256(command_id.encode()).hexdigest()[:16].upper()}"
        token_hash = f"sha256:{hashlib.sha256(token_id.encode()).hexdigest()}"
        channel_json = json.dumps(
            CHANNEL.model_dump(mode="json", exclude_none=True),
            separators=(",", ":"),
            sort_keys=True,
        )
        timestamp = NOW.isoformat().replace("+00:00", "Z")
        connection.execute(
            """
            INSERT INTO governance_action_tokens(
                token_id, token_hash, project_namespace, project_id, proposal_id,
                active_definition_digest, content_revision, state_revision,
                decision_epoch, allowed_action, allowed_actor_id, allowed_actor_type,
                bound_channel_json, issued_at, expires_at, state, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'approve', ?, 'human', ?, ?, ?,
                      'consumed', ?)
            """,
            (
                token_id,
                token_hash,
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL.proposal_id,
                proposal[0],
                proposal[1],
                state_revision - 1,
                proposal[2],
                ACTOR.actor_id,
                channel_json,
                timestamp,
                (NOW + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_decision_results(
                idempotency_key, request_fingerprint, project_namespace, project_id,
                proposal_id, action, actor_id, actor_type, channel_json,
                proposal_status, active_definition_digest, content_revision,
                state_revision, decision_epoch, token_id, processed_at
            ) VALUES (?, ?, ?, ?, ?, 'approve', ?, 'human', ?, 'approved',
                      ?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                hashlib.sha256(command_id.encode()).hexdigest(),
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL.proposal_id,
                ACTOR.actor_id,
                channel_json,
                proposal[0],
                proposal[1],
                state_revision,
                proposal[2],
                token_id,
                timestamp,
            ),
        )
        payload = DecisionProjectionPayload(
            action="approve",
            active_definition_digest=str(proposal[0]),
            aggregate_ref=PROPOSAL,
            content_revision=int(proposal[1]),
            decision_epoch=int(proposal[2]),
            proposal_status="approved",
            state_revision=state_revision,
        )
        return service._append_decision_in_transaction(
            connection,
            PROPOSAL,
            decision_result_key=command_id,
            authority=_authority(),
            payload=payload,
        )


def test_decision_atomically_appends_hash_audit_and_two_outbox_events(
    tmp_path: Path,
) -> None:
    store, active, decisions = _decision_fixture(tmp_path)
    token = next(
        item
        for item in decisions.issue_tokens(PROPOSAL, authority_request=_authority_request())
        if item.record.allowed_action is DecisionAction.APPROVE
    )
    result = decisions.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_authority_request(),
        raw_token=token.raw_token,
        idempotency_key="decision-atomic-1",
        request_fingerprint=hashlib.sha256(b"decision-atomic-1").hexdigest(),
    )

    assert result.proposal_status is ActiveProposalStatus.APPROVED
    assert active.get(PROPOSAL).status is ActiveProposalStatus.APPROVED  # type: ignore[union-attr]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT aggregate_sequence FROM governance_aggregate_sequences"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM governance_outbox_events").fetchone() == (
            2,
        )
        stored = "\n".join(
            str(value)
            for row in connection.execute(
                "SELECT payload_json, payload_digest FROM governance_outbox_events"
            ).fetchall()
            for value in row
        )
    assert token.raw_token not in stored

    replay = decisions.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_authority_request(),
        raw_token=token.raw_token,
        idempotency_key="decision-atomic-1",
        request_fingerprint=hashlib.sha256(b"decision-atomic-1").hexdigest(),
    )
    assert replay.replayed
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone() == (1,)


def test_audit_or_outbox_failure_rolls_back_decision(tmp_path: Path) -> None:
    store, active, decisions = _decision_fixture(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_outbox_failure
            BEFORE INSERT ON governance_outbox_events
            BEGIN
                SELECT RAISE(ABORT, 'injected outbox failure');
            END
            """
        )
    token = next(
        item
        for item in decisions.issue_tokens(PROPOSAL, authority_request=_authority_request())
        if item.record.allowed_action is DecisionAction.APPROVE
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected outbox failure"):
        decisions.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=token.raw_token,
            idempotency_key="decision-fail-1",
            request_fingerprint=hashlib.sha256(b"decision-fail-1").hexdigest(),
        )

    assert active.get(PROPOSAL).status is ActiveProposalStatus.REVIEWED  # type: ignore[union-attr]
    assert decisions.get_token(token.record.token_id).state.value == "issued"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_decision_results"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_aggregate_sequences"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM governance_outbox_events").fetchone() == (
            0,
        )


def test_audit_insert_failure_rolls_back_decision_and_token(tmp_path: Path) -> None:
    store, active, decisions = _decision_fixture(tmp_path)
    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_audit_failure
            BEFORE INSERT ON governance_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'injected audit failure');
            END
            """
        )
    token = next(
        item
        for item in decisions.issue_tokens(PROPOSAL, authority_request=_authority_request())
        if item.record.allowed_action is DecisionAction.APPROVE
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
        decisions.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=token.raw_token,
            idempotency_key="decision-audit-fail-1",
            request_fingerprint=hashlib.sha256(b"decision-audit-fail-1").hexdigest(),
        )

    assert active.get(PROPOSAL).status is ActiveProposalStatus.REVIEWED  # type: ignore[union-attr]
    assert decisions.get_token(token.record.token_id).state.value == "issued"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_decision_results"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_aggregate_sequences"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM governance_outbox_events").fetchone() == (
            0,
        )


def test_any_raw_action_token_is_rejected_as_idempotency_key(tmp_path: Path) -> None:
    store, active, decisions = _decision_fixture(tmp_path)
    tokens = decisions.issue_tokens(PROPOSAL, authority_request=_authority_request())
    approve = next(item for item in tokens if item.record.allowed_action is DecisionAction.APPROVE)
    sibling = next(item for item in tokens if item.record.allowed_action is DecisionAction.REJECT)

    with pytest.raises(DecisionError, match="IDEMPOTENCY_CONFLICT"):
        decisions.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=approve.raw_token,
            idempotency_key=f"prefix-{sibling.raw_token}-suffix",
            request_fingerprint=hashlib.sha256(b"raw-token-key").hexdigest(),
        )

    assert active.get(PROPOSAL).status is ActiveProposalStatus.REVIEWED  # type: ignore[union-attr]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_decision_results"
        ).fetchone() == (0,)
    for database_file in tmp_path.glob("governance.db*"):
        assert approve.raw_token.encode() not in database_file.read_bytes()
        assert sibling.raw_token.encode() not in database_file.read_bytes()


def test_audit_is_append_only_and_startup_reconciliation_fails_on_hash_mismatch(
    tmp_path: Path,
) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    audit, outbox = _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref="yaml:proposal"),),
    )
    _append(
        events,
        store,
        command_id="command-2",
        state_revision=3,
        destinations=(OutboxDestination(destination_ref="yaml:proposal"),),
    )
    events.reconcile()

    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE governance_audit_events SET after_state = 'rejected' WHERE event_id = ?",
                (audit.event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="outbox is durable"):
            connection.execute(
                "DELETE FROM governance_outbox_events WHERE event_id = ?",
                (outbox[0].event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM governance_audit_events WHERE event_id = ?",
                (audit.event_id,),
            )
        connection.execute(
            "UPDATE governance_aggregate_sequences SET last_event_hash = ?",
            (f"sha256:{'f' * 64}",),
        )
    with pytest.raises(GovernanceEventError, match="AUDIT_SEQUENCE_MISMATCH"):
        store.check_startup()


def test_startup_reconciliation_rejects_audit_outbox_mismatch(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _audit, outbox = _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref="yaml:proposal"),),
    )
    with store.connect() as connection:
        trigger_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'governance_outbox_events_no_delete'"
            ).fetchone()[0]
        )
        connection.execute("DROP TRIGGER governance_outbox_events_no_delete")
        connection.execute(
            "DELETE FROM governance_outbox_events WHERE event_id = ?",
            (outbox[0].event_id,),
        )
        connection.execute(trigger_sql)

    with pytest.raises(GovernanceEventError, match="AUDIT_OUTBOX_MISMATCH"):
        store.check_startup()


def test_startup_reconciliation_rejects_complete_decision_event_removal(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _append(events, store, command_id="command-remove", state_revision=2)
    with store.connect() as connection:
        connection.execute("DROP TRIGGER governance_outbox_events_no_delete")
        connection.execute("DROP TRIGGER governance_audit_events_no_delete")
        connection.execute("DROP TRIGGER governance_legacy_rollback_sequence_delete_guard")
        connection.execute("DELETE FROM governance_outbox_events")
        connection.execute("DELETE FROM governance_audit_events")
        connection.execute("DELETE FROM governance_aggregate_sequences")

    with pytest.raises(GovernanceEventError, match="DECISION_AUDIT_MISMATCH"):
        events.reconcile()


def test_consumed_token_requires_immutable_decision_result_root(tmp_path: Path) -> None:
    store, _active, decisions = _decision_fixture(tmp_path)
    approve = next(
        item
        for item in decisions.issue_tokens(PROPOSAL, authority_request=_authority_request())
        if item.record.allowed_action is DecisionAction.APPROVE
    )
    decisions.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_authority_request(),
        raw_token=approve.raw_token,
        idempotency_key="decision-root-removal",
        request_fingerprint=hashlib.sha256(b"decision-root-removal").hexdigest(),
    )
    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM governance_decision_results")
        connection.execute("DROP TRIGGER governance_decision_results_no_delete")
        connection.execute("DELETE FROM governance_decision_results")

    with pytest.raises(GovernanceEventError, match="DECISION_RESULT_ROOT_MISMATCH"):
        GovernanceEventService(store).reconcile()


def test_outbox_rejects_descending_source_revision_for_same_destination(
    tmp_path: Path,
) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _append(events, store, command_id="source-revision-3", state_revision=3)

    with pytest.raises(GovernanceEventError, match="OUTBOX_SOURCE_REVISION_CONFLICT"):
        _append(events, store, command_id="source-revision-2", state_revision=2)

    with store.connect() as connection:
        assert connection.execute(
            """
            SELECT destination_sequence, source_state_revision
            FROM governance_outbox_events
            WHERE destination_ref = ? ORDER BY destination_sequence
            """,
            (f"yaml:{PROJECT.namespace}:{PROJECT.project_id}:{PROPOSAL.proposal_id}",),
        ).fetchall() == [(1, 3)]
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_decision_results"
        ).fetchone() == (1,)


def test_action_token_issuance_is_immutable_and_channel_tamper_fails_startup(
    tmp_path: Path,
) -> None:
    store, _active, decisions = _decision_fixture(tmp_path)
    approve = next(
        item
        for item in decisions.issue_tokens(PROPOSAL, authority_request=_authority_request())
        if item.record.allowed_action is DecisionAction.APPROVE
    )
    decisions.decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_authority_request(),
        raw_token=approve.raw_token,
        idempotency_key="decision-token-immutable",
        request_fingerprint=hashlib.sha256(b"decision-token-immutable").hexdigest(),
    )
    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="issuance is immutable"):
            connection.execute(
                "UPDATE governance_action_tokens SET token_hash = ? WHERE token_id = ?",
                (f"sha256:{'f' * 64}", approve.record.token_id),
            )
        trigger_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'trigger' AND name = 'governance_action_tokens_immutable_issuance'
            """
        ).fetchone()
        assert trigger_sql is not None
        connection.execute("DROP TRIGGER governance_action_tokens_immutable_issuance")
        connection.execute(
            "UPDATE governance_action_tokens SET bound_channel_json = '{}' WHERE token_id = ?",
            (approve.record.token_id,),
        )
        connection.execute(str(trigger_sql[0]))

    with pytest.raises(GovernanceEventError, match="DECISION_RESULT_ROOT_MISMATCH"):
        store.check_startup()


def test_startup_reconciliation_rejects_destination_sequence_gap(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref="yaml:proposal"),),
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE governance_outbox_destinations SET next_sequence = next_sequence + 1"
        )

    with pytest.raises(GovernanceEventError, match="OUTBOX_SEQUENCE_GAP"):
        store.check_startup()


def test_destination_ordering_and_fencing_reject_stale_dispatcher(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    clock = MutableClock()
    events = GovernanceEventService(store, clock=clock)
    destination = OutboxDestination(destination_ref="provider:slack:C456")
    _audit, first_outbox = _append(
        events, store, command_id="command-1", state_revision=2, destinations=(destination,)
    )
    _append(events, store, command_id="command-2", state_revision=3, destinations=(destination,))
    provider_ref = next(
        event.destination_ref for event in first_outbox if event.supersession_key is not None
    )
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(lease_seconds=5),
        clock=clock,
    )

    first = dispatcher.claim_next("worker-a", destination_ref=provider_ref)
    assert first is not None and first.destination_sequence == 1
    assert dispatcher.claim_next("worker-b", destination_ref=provider_ref) is None
    clock.advance(timedelta(seconds=6))
    assert dispatcher.claim_next("worker-b", destination_ref=provider_ref) is None
    clock.advance(timedelta(seconds=5))
    reclaimed = dispatcher.claim_next("worker-b", destination_ref=provider_ref)
    assert reclaimed is not None and reclaimed.event_id == first.event_id
    assert reclaimed.claim_generation == first.claim_generation + 1
    with pytest.raises(OutboxLeaseConflictError, match="OUTBOX_LEASE_CONFLICT"):
        dispatcher.mark_delivered(
            first.event_id,
            dispatcher_id="worker-a",
            generation=first.claim_generation,
            remote_receipt="old",
        )
    delivered = dispatcher.mark_delivered(
        reclaimed.event_id,
        dispatcher_id="worker-b",
        generation=reclaimed.claim_generation,
        remote_receipt="receipt-1",
    )
    assert (
        dispatcher.mark_delivered(
            reclaimed.event_id,
            dispatcher_id="worker-b",
            generation=reclaimed.claim_generation,
            remote_receipt="receipt-1",
        )
        == delivered
    )
    second = dispatcher.claim_next("worker-b", destination_ref=provider_ref)
    assert second is not None and second.destination_sequence == 2


def test_competing_dispatchers_claim_one_fenced_lease(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _audit, outbox = _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref="provider:slack:C456"),),
    )

    provider_ref = next(
        event.destination_ref for event in outbox if event.supersession_key is not None
    )
    barrier = threading.Barrier(2)

    def claim(worker: str):
        barrier.wait()
        return OutboxDispatcher(store, clock=lambda: NOW).claim_next(
            worker, destination_ref=provider_ref
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(claim, ("worker-a", "worker-b")))
    claimed = tuple(result for result in results if result is not None)
    assert len(claimed) == 1
    assert claimed[0].claim_generation == 1


def test_supersede_skips_only_replaceable_pending_predecessor(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    destination = OutboxDestination(
        destination_ref="provider:slack:C456",
        supersession_key="proposal-card",
    )
    _audit1, first = _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(destination,),
    )
    _audit2, second = _append(
        events,
        store,
        command_id="command-2",
        state_revision=3,
        destinations=(destination,),
    )
    dispatcher = OutboxDispatcher(store, clock=lambda: NOW)

    first_provider = next(event for event in first if event.supersession_key is not None)
    second_provider = next(event for event in second if event.supersession_key is not None)
    superseded = dispatcher.supersede_pending(
        first_provider.event_id,
        replacement_event_id=second_provider.event_id,
    )
    assert superseded.state is OutboxState.SUPERSEDED
    claimed = dispatcher.claim_next("worker")
    assert claimed is not None and claimed.event_id == second_provider.event_id


def test_supersede_requires_latest_pending_replacement(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    batches = tuple(
        _append(events, store, command_id=f"command-{index}", state_revision=index + 1)[1]
        for index in range(1, 4)
    )
    providers = tuple(
        next(event for event in batch if event.supersession_key is not None) for batch in batches
    )
    dispatcher = OutboxDispatcher(store, clock=lambda: NOW)

    with pytest.raises(GovernanceEventError, match="OUTBOX_SUPERSEDE_INVALID"):
        dispatcher.supersede_pending(
            providers[0].event_id,
            replacement_event_id=providers[1].event_id,
        )
    superseded = dispatcher.supersede_pending(
        providers[0].event_id,
        replacement_event_id=providers[2].event_id,
    )
    assert superseded.state is OutboxState.SUPERSEDED


def test_retry_exhaustion_moves_event_to_dlq_and_destination_hold(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    clock = MutableClock()
    events = GovernanceEventService(store, clock=clock)
    _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref="provider:slack:C456"),),
    )
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(max_attempts=2, retry_base_seconds=1, retry_cap_seconds=1),
        clock=clock,
    )
    first = dispatcher.claim_next("worker")
    assert first is not None
    retry = dispatcher.fail(
        first.event_id,
        dispatcher_id="worker",
        generation=first.claim_generation,
        error_code="REMOTE_500",
    )
    assert retry.state is OutboxState.RETRY_WAIT
    clock.advance(timedelta(seconds=1))
    second = dispatcher.claim_next("worker")
    assert second is not None
    dead = dispatcher.fail(
        second.event_id,
        dispatcher_id="worker",
        generation=second.claim_generation,
        error_code="REMOTE_500",
    )
    assert dead.state is OutboxState.DEAD_LETTER
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_outbox_dead_letters"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM governance_operator_holds").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT operator_hold FROM governance_outbox_destinations"
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM governance_outbox_dead_letters")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM governance_operator_holds")


@pytest.mark.parametrize(
    ("table", "trigger"),
    (
        ("governance_outbox_dead_letters", "governance_outbox_dead_letters_no_delete"),
        ("governance_operator_holds", "governance_operator_holds_no_delete"),
    ),
)
def test_startup_rejects_deleted_dead_letter_or_operator_hold(
    tmp_path: Path,
    table: str,
    trigger: str,
) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _audit, outbox = _append(events, store, command_id="command-dlq", state_revision=2)
    provider = next(event for event in outbox if event.supersession_key is not None)
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(max_attempts=1),
        clock=lambda: NOW,
    )
    claimed = dispatcher.claim_next("worker", destination_ref=provider.destination_ref)
    assert claimed is not None
    dispatcher.fail(
        claimed.event_id,
        dispatcher_id="worker",
        generation=claimed.claim_generation,
        error_code="REMOTE_500",
    )
    with store.connect() as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"DELETE FROM {table}")

    with pytest.raises(GovernanceEventError, match="OUTBOX_OPERATOR_HOLD_MISMATCH"):
        events.reconcile()


def test_post_send_crash_reconciles_without_duplicate_delivery(tmp_path: Path) -> None:
    class Remote:
        def __init__(self, destination_ref: str) -> None:
            self.destination_ref = destination_ref
            self.receipts: dict[str, str] = {}
            self.sends = 0

        def reconcile(self, event):
            return self.receipts.get(event.event_id)

        def send(self, event):
            self.sends += 1
            receipt = f"remote:{event.event_id}"
            self.receipts[event.event_id] = receipt
            return receipt

    store, _active, _draft = _active_proposal(tmp_path)
    clock = MutableClock()
    events = GovernanceEventService(store, clock=clock)
    _audit, outbox = _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref="provider:slack:C456"),),
    )
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(lease_seconds=5),
        clock=clock,
    )
    provider = next(event for event in outbox if event.supersession_key is not None)
    remote = Remote(provider.destination_ref)
    claimed = dispatcher.claim_next("crashed", destination_ref=provider.destination_ref)
    assert claimed is not None
    remote.send(claimed)
    clock.advance(timedelta(seconds=6))

    assert dispatcher.deliver_next("recovery", remote) is None
    clock.advance(timedelta(seconds=5))
    delivered = dispatcher.deliver_next("recovery", remote)
    assert delivered is not None and delivered.state is OutboxState.DELIVERED
    assert remote.sends == 1


def test_final_attempt_post_send_crash_reconciles_before_dlq(tmp_path: Path) -> None:
    class Remote:
        def __init__(self, destination_ref: str) -> None:
            self.destination_ref = destination_ref
            self.receipts: dict[str, str] = {}
            self.sends = 0
            self.reconciles = 0

        def reconcile(self, event):
            self.reconciles += 1
            return self.receipts.get(event.event_id)

        def send(self, event):
            self.sends += 1
            receipt = f"remote:{event.event_id}"
            self.receipts[event.event_id] = receipt
            return receipt

    store, _active, _draft = _active_proposal(tmp_path)
    clock = MutableClock()
    _audit, outbox = _append(
        GovernanceEventService(store, clock=clock),
        store,
        command_id="command-final-crash",
        state_revision=2,
    )
    provider = next(event for event in outbox if event.supersession_key is not None)
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(lease_seconds=5, max_attempts=1, retry_base_seconds=10),
        clock=clock,
    )
    remote = Remote(provider.destination_ref)
    claimed = dispatcher.claim_next("crashed", destination_ref=provider.destination_ref)
    assert claimed is not None
    remote.send(claimed)
    clock.advance(timedelta(seconds=6))
    assert dispatcher.deliver_next("recovery", remote) is None
    clock.advance(timedelta(seconds=10))

    delivered = dispatcher.deliver_next("recovery", remote)
    assert delivered is not None and delivered.state is OutboxState.DELIVERED
    assert delivered.attempts == 1
    assert remote.sends == 1
    assert remote.reconciles == 1


def test_repeated_final_recovery_crash_is_bounded_to_dlq(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    clock = MutableClock()
    _audit, outbox = _append(
        GovernanceEventService(store, clock=clock),
        store,
        command_id="command-recovery-crash",
        state_revision=2,
    )
    provider = next(event for event in outbox if event.supersession_key is not None)
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(
            lease_seconds=1,
            max_attempts=1,
            retry_base_seconds=1,
            retry_cap_seconds=1,
        ),
        clock=clock,
    )
    first = dispatcher.claim_next("initial", destination_ref=provider.destination_ref)
    assert first is not None and first.claim_generation == 1
    clock.advance(timedelta(seconds=2))
    assert dispatcher.claim_next("recovery", destination_ref=provider.destination_ref) is None
    clock.advance(timedelta(seconds=1))
    recovery = dispatcher.claim_next("recovery", destination_ref=provider.destination_ref)
    assert recovery is not None and recovery.claim_generation == 2
    assert recovery.attempts == 1
    clock.advance(timedelta(seconds=2))
    assert dispatcher.claim_next("recovery-2", destination_ref=provider.destination_ref) is None
    clock.advance(timedelta(seconds=1))
    assert dispatcher.claim_next("recovery-2", destination_ref=provider.destination_ref) is None

    dead = dispatcher.get(provider.event_id)
    assert dead.state is OutboxState.DEAD_LETTER
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_outbox_dead_letters"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM governance_operator_holds").fetchone() == (
            1,
        )


def test_lowered_max_attempts_does_not_strand_expired_lease(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    clock = MutableClock()
    _audit, outbox = _append(
        GovernanceEventService(store, clock=clock),
        store,
        command_id="command-lowered-max",
        state_revision=2,
    )
    provider = next(event for event in outbox if event.supersession_key is not None)
    original = OutboxDispatcher(
        store,
        config=OutboxConfig(
            lease_seconds=1,
            max_attempts=3,
            retry_base_seconds=1,
            retry_cap_seconds=1,
        ),
        clock=clock,
    )
    first = original.claim_next("worker-1", destination_ref=provider.destination_ref)
    assert first is not None
    original.fail(
        first.event_id,
        dispatcher_id="worker-1",
        generation=first.claim_generation,
        error_code="REMOTE_500",
    )
    clock.advance(timedelta(seconds=1))
    second = original.claim_next("worker-2", destination_ref=provider.destination_ref)
    assert second is not None and (second.attempts, second.claim_generation) == (2, 2)

    restarted = OutboxDispatcher(
        store,
        config=OutboxConfig(
            lease_seconds=1,
            max_attempts=1,
            retry_base_seconds=1,
            retry_cap_seconds=1,
        ),
        clock=clock,
    )
    clock.advance(timedelta(seconds=2))
    assert restarted.claim_next("worker-3", destination_ref=provider.destination_ref) is None
    clock.advance(timedelta(seconds=1))
    assert restarted.claim_next("worker-3", destination_ref=provider.destination_ref) is None

    dead = restarted.get(provider.event_id)
    assert dead.state is OutboxState.DEAD_LETTER
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_outbox_dead_letters"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM governance_operator_holds").fetchone() == (
            1,
        )


def test_unreconcilable_remote_state_moves_to_dlq_and_operator_hold(tmp_path: Path) -> None:
    class DivergedRemote:
        def __init__(self, destination_ref: str) -> None:
            self.destination_ref = destination_ref

        def reconcile(self, event):
            raise OutboxReconcileError("OUTBOX_REMOTE_DIVERGED")

        def send(self, event):
            raise AssertionError("diverged remote must not be sent again")

    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _audit, outbox = _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref="provider:slack:C456"),),
    )
    provider = next(event for event in outbox if event.supersession_key is not None)
    result = OutboxDispatcher(store, clock=lambda: NOW).deliver_next(
        "worker",
        DivergedRemote(provider.destination_ref),
    )
    assert result is not None and result.state is OutboxState.DEAD_LETTER
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM governance_operator_holds").fetchone() == (
            1,
        )


def test_yaml_projection_rejects_reverse_order_and_applies_sequence_cas(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    destination_ref = "yaml:proposal"
    _audit1, first = _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref=destination_ref),),
    )
    _audit2, second = _append(
        events,
        store,
        command_id="command-2",
        state_revision=3,
        destinations=(OutboxDestination(destination_ref=destination_ref),),
    )
    projection = YamlProjectionDestination(
        tmp_path / "projection/proposal.yaml",
        destination_ref=first[0].destination_ref,
    )

    with pytest.raises(GovernanceEventError, match="YAML_SEQUENCE_CAS_CONFLICT"):
        projection.send(second[0])
    first_receipt = projection.send(first[0])
    assert projection.reconcile(first[0]) == first_receipt
    second_receipt = projection.send(second[0])
    assert projection.reconcile(second[0]) == second_receipt


def test_yaml_projection_uses_per_destination_sequence_across_aggregate_gaps(
    tmp_path: Path,
) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    live_ref = "yaml:proposal"
    history_ref = "migration-history:proposal"
    _audit1, first_events = _append(
        events,
        store,
        command_id="command-live-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref=live_ref),),
    )
    _audit2, history_events = _append(
        events,
        store,
        command_id="command-history",
        state_revision=3,
        destinations=(OutboxDestination(destination_ref=history_ref),),
    )
    _audit3, second_events = _append(
        events,
        store,
        command_id="command-live-2",
        state_revision=4,
        destinations=(OutboxDestination(destination_ref=live_ref),),
    )
    first = next(event for event in first_events if event.destination_ref.startswith("yaml:"))
    live_ref = first.destination_ref
    live = YamlProjectionDestination(
        tmp_path / "projection/live.yaml",
        destination_ref=live_ref,
    )
    history_projection = YamlProjectionDestination(
        tmp_path / "projection/history.yaml",
        destination_ref=history_ref,
    )
    history = next(
        event for event in history_events if event.destination_ref.startswith("yaml:")
    ).model_copy(update={"destination_ref": history_ref, "destination_sequence": 1})
    second = next(
        event for event in second_events if event.destination_ref.startswith("yaml:")
    ).model_copy(update={"destination_sequence": 2})

    live.send(first)
    live.send(second)
    history_projection.send(history)

    assert live.reconcile(second) is not None
    assert history_projection.reconcile(history) is not None
    assert (
        yaml.safe_load((tmp_path / "projection/live.yaml").read_text())["destination_sequence"] == 2
    )


def test_yaml_projection_concurrent_duplicate_is_idempotent(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    destination_ref = "yaml:proposal"
    _audit, outbox = _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref=destination_ref),),
    )
    projection = YamlProjectionDestination(
        tmp_path / "projection/proposal.yaml",
        destination_ref=outbox[0].destination_ref,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(projection.send, (outbox[0], outbox[0])))
    assert receipts[0] == receipts[1]
    assert projection.reconcile(outbox[0]) == receipts[0]


def test_yaml_projection_reconcile_rejects_self_inconsistent_record(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _audit, outbox = _append(events, store, command_id="command-yaml", state_revision=2)
    yaml_event = next(event for event in outbox if event.destination_ref.startswith("yaml:"))
    path = tmp_path / "projection/proposal.yaml"
    projection = YamlProjectionDestination(path, destination_ref=yaml_event.destination_ref)
    projection.send(yaml_event)
    record = yaml.safe_load(path.read_text(encoding="utf-8"))
    record["payload"]["proposal_status"] = "rejected"
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")

    with pytest.raises(OutboxReconcileError, match="YAML_PROJECTION_DIVERGED"):
        projection.reconcile(yaml_event)


# D-022 — 두 사전 검증 실패는 `OutboxReconcileError` 다. 부모인 `GovernanceEventError` 로
# 던지면 `deliver_next` 의 unreconcilable 경로를 못 타고 generic handler 로 떨어져 원인이
# `OUTBOX_DELIVERY_FAILED` 상수로 덮인다. 두 조건은 event row 의 불변 column 에서 나오므로
# 재시도가 확정적으로 무의미하다. 부모 관계 때문에 타입 검사만으로는 방향이 안 잡혀
# `type(...) is` 로 못박는다.
def test_yaml_projection_pre_send_validation_is_unreconcilable(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _audit, outbox = _append(events, store, command_id="command-yaml", state_revision=2)
    yaml_event = next(event for event in outbox if event.destination_ref.startswith("yaml:"))
    projection = YamlProjectionDestination(
        tmp_path / "projection/proposal.yaml",
        destination_ref=yaml_event.destination_ref,
    )

    with pytest.raises(OutboxReconcileError) as mismatch:
        projection.send(yaml_event.model_copy(update={"destination_ref": "yaml:other"}))
    assert type(mismatch.value) is OutboxReconcileError
    assert mismatch.value.code == "OUTBOX_DESTINATION_MISMATCH"

    with pytest.raises(OutboxReconcileError) as integrity:
        projection.send(yaml_event.model_copy(update={"payload": {"tampered": True}}))
    assert type(integrity.value) is OutboxReconcileError
    assert integrity.value.code == "OUTBOX_PAYLOAD_INTEGRITY_FAILURE"
    assert not (tmp_path / "projection/proposal.yaml").exists()


def test_the_used_tokens_own_raw_value_is_rejected_as_idempotency_key(tmp_path: Path) -> None:
    """이번 호출의 raw token 이 idempotency key 에 섞이면 거부된다.

    `test_any_raw_action_token_is_rejected_as_idempotency_key` 는 **sibling** token 의 raw
    값만 넣는다. 그 test 는 `_contains_persisted_secret` scan 을 지킨다. 같은 token 의 raw
    값을 넣는 경우는 `decisions.py` 의 `raw_token in idempotency_key` guard 가 막는데,
    그 guard 를 지워도 아무 test 가 실패하지 않았다 (round 10 regression `C-12`).

    이 guard 가 없으면 raw credential 이 섞인 key 가 그대로
    `governance_decision_results.idempotency_key` 로 영구 저장된다 (FR-018, SC-005).
    """
    store, active, decisions = _decision_fixture(tmp_path)
    tokens = decisions.issue_tokens(PROPOSAL, authority_request=_authority_request())
    approve = next(item for item in tokens if item.record.allowed_action is DecisionAction.APPROVE)

    with pytest.raises(DecisionError, match="IDEMPOTENCY_CONFLICT"):
        decisions.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=approve.raw_token,
            idempotency_key=f"prefix-{approve.raw_token}-suffix",
            request_fingerprint=hashlib.sha256(b"own-raw-token-key").hexdigest(),
        )

    assert active.get(PROPOSAL).status is ActiveProposalStatus.REVIEWED  # type: ignore[union-attr]
    assert decisions.get_token(approve.record.token_id).state.value == "issued"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_decision_results"
        ).fetchone() == (0,)
        stored_keys = connection.execute(
            "SELECT idempotency_key FROM governance_decision_results"
        ).fetchall()
    assert all(approve.raw_token not in str(row[0]) for row in stored_keys)


def test_the_raw_token_is_rejected_even_as_the_entire_idempotency_key(tmp_path: Path) -> None:
    _store, _active, decisions = _decision_fixture(tmp_path)
    tokens = decisions.issue_tokens(PROPOSAL, authority_request=_authority_request())
    approve = next(item for item in tokens if item.record.allowed_action is DecisionAction.APPROVE)

    with pytest.raises(DecisionError, match="IDEMPOTENCY_CONFLICT"):
        decisions.decide(
            PROPOSAL,
            action=DecisionAction.APPROVE,
            authority_request=_authority_request(),
            raw_token=approve.raw_token,
            idempotency_key=approve.raw_token,
            request_fingerprint=hashlib.sha256(b"exact-raw-token-key").hexdigest(),
        )

    assert decisions.get_token(approve.record.token_id).state.value == "issued"


# ---------------------------------------------------------------------------
# MGC-012-P5-T010 — 재시도 가능한 배달 실패는 예산을 쓰고 원인을 남긴다 (round 11 R-2)
# ---------------------------------------------------------------------------


class _RetryableRemote:
    """재시도 가능한 실패를 자기 code 와 함께 알리는 destination."""

    def __init__(self, destination_ref: str) -> None:
        self.destination_ref = destination_ref
        self.sends = 0

    def reconcile(self, event):
        return None

    def send(self, event):
        self.sends += 1
        raise OutboxRetryableError("DEMO_TRANSIENT_CAUSE")


def _one_provider_event(tmp_path: Path):
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _audit, outbox = _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref="provider:slack:C456"),),
    )
    provider = next(event for event in outbox if event.supersession_key is not None)
    return store, provider


# T010 AC-01 — 예산을 쓴다. destination 은 멈추지 않는다.
def test_a_retryable_delivery_failure_spends_its_budget(tmp_path: Path) -> None:
    store, provider = _one_provider_event(tmp_path)
    destination = _RetryableRemote(provider.destination_ref)

    result = OutboxDispatcher(store, clock=lambda: NOW).deliver_next("worker", destination)

    assert result is not None
    assert result.state is OutboxState.RETRY_WAIT
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM governance_outbox_dead_letters"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM governance_operator_holds").fetchone() == (
            0,
        )


# T010 AC-01 — 원인 code 를 보존한다.
#
# `except Exception` 경로도 재시도로 흐르지만 예외를 보지 않고 `OUTBOX_DELIVERY_FAILED` 를
# 박는다. round 11 `R-1` 이 원인 아닌 dead letter code 를 결함으로 셌으므로 같은 형태를
# 만들지 않는다. 이 비교가 그 mutation 을 죽인다.
def test_a_retryable_delivery_failure_keeps_its_own_cause_code(tmp_path: Path) -> None:
    store, provider = _one_provider_event(tmp_path)
    destination = _RetryableRemote(provider.destination_ref)

    result = OutboxDispatcher(store, clock=lambda: NOW).deliver_next("worker", destination)

    assert result is not None
    assert result.last_error_code == "DEMO_TRANSIENT_CAUSE"
    assert result.last_error_code != "OUTBOX_DELIVERY_FAILED"


# T010 AC-02 — 예산을 소진하면 여전히 dead letter 와 operator hold 에 도달하고, 그 dead
# letter 가 원인 code 를 가진다. 재분류가 안전망을 없애지 않는다.
def test_a_retryable_delivery_failure_dead_letters_once_the_budget_is_spent(
    tmp_path: Path,
) -> None:
    store, provider = _one_provider_event(tmp_path)
    destination = _RetryableRemote(provider.destination_ref)
    clock = MutableClock()
    dispatcher = OutboxDispatcher(store, clock=clock, config=OutboxConfig(max_attempts=3))

    result = None
    for _ in range(3):
        clock.advance(timedelta(minutes=10))
        result = dispatcher.deliver_next("worker", destination)

    assert result is not None
    assert result.state is OutboxState.DEAD_LETTER
    assert destination.sends == 3, "예산만큼 실제로 시도한다"
    with store.connect() as connection:
        dead = connection.execute(
            "SELECT error_code FROM governance_outbox_dead_letters"
        ).fetchall()
        holds = connection.execute("SELECT reason_code FROM governance_operator_holds").fetchall()
    assert dead == [("DEMO_TRANSIENT_CAUSE",)]
    assert holds == [("DEMO_TRANSIENT_CAUSE",)]


# ---------------------------------------------------------------------------
# MGC-012-P5-T023 — 여덟 감사 대조가 helper 하나를 쓴다
# ---------------------------------------------------------------------------
#
# 착수 전 실측: `reconcile_connection` 의 감사 대조는 여덟 블록 72성분이었고 **62개가
# 무방비**였다. 블록을 통째로 무력화(`if False and (...)`)해도 전 suite 가 통과한 블록이
# 여섯이다 — `LEGACY_MIGRATION`, `LEGACY_APPROVAL_REVIEW`, `DECISION`, `APPLY`,
# `APPLY_JOB`, `PUBLISH_RESOLUTION`. `REVIEW_CARD` 만 T018 이 9/9 를 고정해 뒀다.
#
# 성분을 하나씩 test 로 덮는 대신(그것이 superseded 된 T020 이었다) 비교를 helper 하나로
# 모았다. 아래 셋이 그 구조를 고정한다.
#
#   1. helper 자신 — 주어진 필드 하나만 어긋나도 지정된 code 로 거부한다.
#   2. or-chain 이 되돌아오지 않는다 (AST).
#   3. 각 호출 지점이 기대하는 필드 집합과 code 가 기록과 같다 (AST).
#
# 2 와 3 이 함께 72성분을 덮는다. mapping 에서 key 를 빼면 3 이 실패하고, 비교를 무력화하면
# 1 과 아래 실행 test 들이 실패한다.


def _audit_row(store: GovernanceStore, event_type: str) -> tuple[object, ...]:
    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM governance_audit_events WHERE event_type = ?",
            (event_type,),
        ).fetchone()
    assert row is not None, f"{event_type} 감사 row 가 없다"
    return tuple(row)


_HELPER_FIELDS = (
    ("event_type", "proposal.tampered"),
    ("actor_id", "ACT-OTHER"),
    ("actor_type", "service"),
    ("policy_snapshot_id", "sha256:" + "c" * 64),
    ("before_state", "draft"),
    ("after_state", "superseded"),
    ("definition_digest", "sha256:" + "d" * 64),
    ("destination_manifest_digest", "sha256:" + "e" * 64),
    ("destination_count", 7),
)


@pytest.mark.parametrize(
    ("field", "wrong"), _HELPER_FIELDS, ids=[name for name, _ in _HELPER_FIELDS]
)
def test_the_audit_helper_rejects_every_field_it_is_given(
    tmp_path: Path, field: str, wrong: object
) -> None:
    """helper 는 `expected` 의 **모든** key 를 본다. 하나라도 어긋나면 주어진 code 로 막는다."""
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _append(events, store, command_id="command-helper", state_revision=2)
    audit = GovernanceEventService._audit_view(_audit_row(store, "proposal.approved"))

    truthful = {
        "event_type": audit.event_type,
        "actor_id": audit.actor_id,
        "actor_type": audit.actor_type,
        "policy_snapshot_id": audit.policy_snapshot_id,
        "before_state": audit.before_state,
        "after_state": audit.after_state,
        "definition_digest": audit.definition_digest,
        "destination_manifest_digest": audit.destination_manifest_digest,
        "destination_count": audit.destination_count,
    }
    GovernanceEventService._assert_audit_matches(audit, truthful, "SHOULD_NOT_RAISE")

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService._assert_audit_matches(
            audit, {**truthful, field: wrong}, "HELPER_TEST_CODE"
        )
    assert "HELPER_TEST_CODE" in str(caught.value)


def test_the_audit_helper_compares_occurred_at_in_its_stored_form(tmp_path: Path) -> None:
    """`occurred_at` 만 변환을 거친다. view 는 `datetime`, row 는 문자열이다."""
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _append(events, store, command_id="command-occurred", state_revision=2)
    audit = GovernanceEventService._audit_view(_audit_row(store, "proposal.approved"))

    stored = GovernanceEventService._timestamp(audit.occurred_at)
    GovernanceEventService._assert_audit_matches(audit, {"occurred_at": stored}, "UNUSED")

    with pytest.raises(GovernanceEventError, match="OCCURRED_AT_CODE"):
        GovernanceEventService._assert_audit_matches(
            audit, {"occurred_at": "2000-01-01T00:00:00.000000Z"}, "OCCURRED_AT_CODE"
        )


def test_the_audit_helper_ignores_fields_the_call_site_did_not_name(tmp_path: Path) -> None:
    """비대칭은 의도다. 주지 않은 필드는 보지 않는다 — 그 사실을 고정한다."""
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _append(events, store, command_id="command-partial", state_revision=2)
    audit = GovernanceEventService._audit_view(_audit_row(store, "proposal.approved"))

    GovernanceEventService._assert_audit_matches(audit, {"event_type": audit.event_type}, "UNUSED")


# --- AST — 구조 자체를 고정한다 ------------------------------------------------
#
# 아래 둘이 T020(60성분을 하나씩 test 로 덮기)을 대체한다. mapping 에서 key 를 하나 빼면
# `..._expects_its_recorded_field_set` 이 실패하고, or-chain 으로 되돌리면
# `..._uses_the_shared_helper` 가 실패한다. 성분을 세는 일이 사라진다.

_EXPECTED_AUDIT_CALL_SITES: dict[str, frozenset[str]] = {
    "REVIEW_CARD_AUDIT_MISMATCH": frozenset(
        {
            "event_type",
            "proposal_ref",
            "actor_id",
            "actor_type",
            "before_state",
            "after_state",
            "definition_digest",
            "destination_manifest_digest",
            "destination_count",
        }
    ),
    "LEGACY_MIGRATION_AUDIT_MISMATCH": frozenset(
        {
            "event_type",
            "actor_id",
            "actor_type",
            "policy_snapshot_id",
            "before_state",
            "after_state",
            "definition_digest",
            "occurred_at",
            "destination_manifest_digest",
            "destination_count",
        }
    ),
    "LEGACY_APPROVAL_REVIEW_AUDIT_MISMATCH": frozenset(
        {
            "event_type",
            "actor_id",
            "actor_type",
            "policy_snapshot_id",
            "before_state",
            "after_state",
            "definition_digest",
            "occurred_at",
            "destination_manifest_digest",
            "destination_count",
        }
    ),
    "LEGACY_FORWARD_RECOVERY_AUDIT_MISMATCH": frozenset(
        {
            "event_type",
            "proposal_ref",
            "actor_id",
            "actor_type",
            "policy_snapshot_id",
            "before_state",
            "after_state",
            "definition_digest",
            "occurred_at",
            "destination_manifest_digest",
            "destination_count",
        }
    ),
    "DECISION_AUDIT_MISMATCH": frozenset(
        {
            "event_type",
            "actor_id",
            "actor_type",
            "before_state",
            "after_state",
            "definition_digest",
            "destination_manifest_digest",
            "destination_count",
        }
    ),
    "APPLY_AUDIT_MISMATCH": frozenset(
        {
            "event_type",
            "actor_id",
            "actor_type",
            "before_state",
            "after_state",
            "definition_digest",
            "destination_manifest_digest",
            "destination_count",
        }
    ),
    "APPLY_JOB_AUDIT_MISMATCH": frozenset(
        {
            "event_type",
            "actor_id",
            "actor_type",
            "before_state",
            "after_state",
            "definition_digest",
            "destination_manifest_digest",
            "destination_count",
        }
    ),
    "PUBLISH_RESOLUTION_AUDIT_MISMATCH": frozenset(
        {
            "event_type",
            "actor_id",
            "actor_type",
            "before_state",
            "after_state",
            "definition_digest",
            "destination_manifest_digest",
            "destination_count",
        }
    ),
}


def _reconcile_ast() -> ast.FunctionDef:
    source = Path(events_module.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "reconcile_connection":
            return node
    raise AssertionError("reconcile_connection 을 찾지 못했다")


def _audit_call_sites() -> dict[str, frozenset[str]]:
    sites: dict[str, frozenset[str]] = {}
    for node in ast.walk(_reconcile_ast()):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Attribute) and target.attr == "_assert_audit_matches"):
            continue
        assert len(node.args) == 3, "helper 는 (audit, expected, code) 셋을 받는다"
        mapping, code = node.args[1], node.args[2]
        assert isinstance(mapping, ast.Dict), "`expected` 는 literal mapping 이어야 눈에 보인다"
        assert isinstance(code, ast.Constant), "code 는 literal 이어야 한 눈에 읽힌다"
        keys = frozenset(key.value for key in mapping.keys if isinstance(key, ast.Constant))
        assert len(keys) == len(mapping.keys), "mapping key 는 전부 문자열 literal 이다"
        assert str(code.value) not in sites, f"error code 가 두 지점에 있다: {code.value}"
        sites[str(code.value)] = keys
    return sites


def test_every_audit_comparison_uses_the_shared_helper() -> None:
    """`or` 사슬로 되돌아가지 않는다.

    되돌아가면 다시 성분을 세야 하고, 그 세기가 다섯 라운드 연속 틀렸다.
    """
    chains = [
        node
        for node in ast.walk(_reconcile_ast())
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and isinstance(node.test.op, ast.Or)
        and any(
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "audit"
            for sub in ast.walk(node.test)
        )
    ]
    assert chains == [], f"감사 대조 or-chain 이 되살아났다: {[n.lineno for n in chains]}"
    assert len(_audit_call_sites()) == len(_EXPECTED_AUDIT_CALL_SITES)


def test_each_audit_comparison_expects_its_recorded_field_set() -> None:
    """호출 지점의 `expected` mapping 에서 필드를 빼면 여기서 걸린다.

    이 하나가 72성분을 덮는다. 예전에는 성분마다 test 가 필요했고 62개가 비어 있었다.
    """
    assert _audit_call_sites() == _EXPECTED_AUDIT_CALL_SITES


# --- 실행 test — 각 호출 지점이 자기 code 로 막는다 -------------------------------
#
# 위의 AST test 는 mapping 을 **읽어서** 고정한다. 그 mapping 이 실제로 그 code 로 거부하는지는
# 실행해야 안다. 착수 전 실측에서 여섯 지점은 통째로 무력화해도 실패하는 test 가 0건이었다.
#
# state 를 만드는 helper 는 다른 test module 에서 **수정 없이 import** 한다. 같은 fixture 를
# 두 벌 만들면 그것이 다음 형제가 된다 — `test_slack_http` 가 이미 이 방식을 쓴다.
import test_apply_jobs as apply_fixtures  # noqa: E402


def _tamper_audit(store: GovernanceStore, event_type: str, column: str, value: object) -> None:
    with store.connect() as connection:
        connection.execute("DROP TRIGGER governance_audit_events_no_update")
        changed = connection.execute(
            f"UPDATE governance_audit_events SET {column} = ? WHERE event_type = ?",
            (value, event_type),
        )
        assert changed.rowcount == 1, f"{event_type} 감사 row 하나만 바꾼다"
        connection.commit()


# `_publish_pending_job_fixture` 하나가 decision, apply, apply_job 세 지점의 감사 row 를
# 남긴다. 세 지점 모두 착수 전 무방비였다.
_EXECUTED_AUDIT_SITES = [
    ("proposal.approved", "actor_id", "ACT-OTHER-HUMAN", "DECISION_AUDIT_MISMATCH"),
    ("proposal.approved", "before_state", "draft", "DECISION_AUDIT_MISMATCH"),
    ("proposal.apply_requested", "actor_id", "ACT-OTHER-HUMAN", "APPLY_AUDIT_MISMATCH"),
    ("proposal.apply_requested", "after_state", "applied", "APPLY_AUDIT_MISMATCH"),
    ("apply_job.claimed", "actor_type", "human", "APPLY_JOB_AUDIT_MISMATCH"),
    ("apply_job.started", "after_state", "queued", "APPLY_JOB_AUDIT_MISMATCH"),
]


@pytest.mark.parametrize(
    ("event_type", "column", "wrong_value", "expected_code"),
    _EXECUTED_AUDIT_SITES,
    ids=[f"{event}-{column}" for event, column, _, _ in _EXECUTED_AUDIT_SITES],
)
def test_reconcile_rejects_a_tampered_audit_with_the_code_of_its_own_call_site(
    tmp_path: Path,
    event_type: str,
    column: str,
    wrong_value: str,
    expected_code: str,
) -> None:
    """대조가 잡았다는 증거는 **그 지점의 code** 다.

    audit row 를 고치면 hash chain 도 깨져 `AUDIT_HASH_CHAIN_INVALID` 가 대신 잡을 수 있다.
    code 를 확인하지 않으면 겨냥한 대조를 고정한 것이 아니다 (T018 이 쓴 방식).
    """
    store, _job_id = apply_fixtures._publish_pending_job_fixture(tmp_path)
    _tamper_audit(store, event_type, column, wrong_value)

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert expected_code in str(caught.value), (
        f"{event_type}.{column} 을 바꿨는데 다른 검사가 먼저 잡았다: {caught.value}"
    )


# legacy migration import 는 `LEGACY_MIGRATION_AUDIT_MISMATCH` 지점의 감사 row 를 남긴다.
# 그 지점도 착수 전 무방비였다 (블록 전체를 무력화해도 실패 0건).
import test_legacy_migration as legacy_fixtures  # noqa: E402
from amplai_foundry.governance import legacy_migration as legacy_approval_review  # noqa: E402


def _legacy_imported_store(tmp_path: Path) -> GovernanceStore:
    root = legacy_fixtures._legacy_tree(tmp_path / "project", revision=7)
    store, _objects, dry_run, service, backup = legacy_fixtures._import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=legacy_fixtures._freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.prepare(plan, backup)
    service.import_state(plan, backup)
    return store


_LEGACY_AUDIT_SITES = [
    ("actor_id", "ACT-OTHER-HUMAN"),
    ("actor_type", "agent"),
    ("policy_snapshot_id", "sha256:" + "f" * 64),
    ("after_state", "approved"),
    ("occurred_at", "2000-01-01T00:00:00.000000Z"),
]


@pytest.mark.parametrize(
    ("column", "wrong_value"),
    _LEGACY_AUDIT_SITES,
    ids=[column for column, _ in _LEGACY_AUDIT_SITES],
)
def test_reconcile_rejects_a_tampered_legacy_migration_audit(
    tmp_path: Path, column: str, wrong_value: str
) -> None:
    store = _legacy_imported_store(tmp_path)
    with store.connect() as connection:
        event_type = connection.execute(
            "SELECT event_type FROM governance_audit_events LIMIT 1"
        ).fetchone()[0]
    _tamper_audit(store, str(event_type), column, wrong_value)

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert "LEGACY_MIGRATION_AUDIT_MISMATCH" in str(caught.value), (
        f"{column} 을 바꿨는데 다른 검사가 먼저 잡았다: {caught.value}"
    )


# publish resolution 은 실제 git repository 를 세우고 CAS 를 돌려야 감사 row 가 생긴다.
# `test_apply_jobs` 가 그 fixture 를 이미 갖고 있다.
_PUBLISH_AUDIT_SITES = [
    ("actor_id", "ACT-OTHER-SERVICE"),
    ("actor_type", "human"),
    ("before_state", "queued"),
    ("definition_digest", "sha256:" + "9" * 64),
]


@pytest.mark.parametrize(
    ("column", "wrong_value"),
    _PUBLISH_AUDIT_SITES,
    ids=[column for column, _ in _PUBLISH_AUDIT_SITES],
)
def test_reconcile_rejects_a_tampered_publish_resolution_audit(
    tmp_path: Path, column: str, wrong_value: str
) -> None:
    store, repository, _git, prepared, _base, _candidate = (
        apply_fixtures._real_prepared_publish_fixture(tmp_path)
    )
    coordinator = apply_fixtures.FencedGitPublishCoordinator(
        store,
        repository,
        coordinator_id="publisher-audit-probe",
        clock=lambda: apply_fixtures.NOW,
    )
    assert (
        coordinator.publish_prepared_ref(prepared.intent_id) is apply_fixtures.GitCASOutcome.UPDATED
    )
    apply_fixtures.PublishResolutionService(
        store,
        apply_fixtures.SubprocessGitCandidateInspector(repository),
        coordinator_id="recovery-audit-probe",
        clock=lambda: apply_fixtures.NOW,
    ).recover(prepared.intent_id)

    _tamper_audit(store, "publish.published", column, wrong_value)

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert "PUBLISH_RESOLUTION_AUDIT_MISMATCH" in str(caught.value), (
        f"{column} 을 바꿨는데 다른 검사가 먼저 잡았다: {caught.value}"
    )


# 마지막 지점. `legacy_approval_review_required` 로 들어온 항목을 사람이 검토하면
# `migration.synthetic_approval_reviewed` 감사 row 가 생기고
# `LEGACY_APPROVAL_REVIEW_AUDIT_MISMATCH` 대조가 그것을 본다. 여기도 착수 전 무방비였다.
_LEGACY_REVIEW_AUDIT_SITES = [
    ("actor_id", "ACT-OTHER-HUMAN"),
    ("actor_type", "service"),
    ("policy_snapshot_id", "sha256:" + "7" * 64),
    ("before_state", "approved"),
    ("occurred_at", "2000-01-01T00:00:00.000000Z"),
]


@pytest.mark.parametrize(
    ("column", "wrong_value"),
    _LEGACY_REVIEW_AUDIT_SITES,
    ids=[column for column, _ in _LEGACY_REVIEW_AUDIT_SITES],
)
def test_reconcile_rejects_a_tampered_legacy_approval_review_audit(
    tmp_path: Path, column: str, wrong_value: str
) -> None:
    root = legacy_fixtures._legacy_tree(tmp_path / "project", status="approved")
    store, _objects, dry_run, service, backup = legacy_fixtures._import_fixture(
        tmp_path / "fixture",
        root,
    )
    plan = dry_run.create_plan(
        freeze=legacy_fixtures._freeze(root),
        base_revision="a13d92f",
        validation_policy_ref="policy/migration/v1",
    )
    service.prepare(plan, backup)
    service.import_state(plan, backup)
    legacy_approval_review.LegacyApprovalReviewService(
        store,
        AuthorityService(store, clock=lambda: legacy_fixtures.NOW),
        clock=lambda: legacy_fixtures.NOW,
    ).resolve(
        plan.proposals[0].proposal_ref,
        authority_request=legacy_fixtures._migration_authority(store),
        reason="human reviewed untrusted legacy approval",
        idempotency_key="audit-probe:legacy-review:1",
        request_fingerprint="b" * 64,
    )

    _tamper_audit(store, "migration.synthetic_approval_reviewed", column, wrong_value)

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert "LEGACY_APPROVAL_REVIEW_AUDIT_MISMATCH" in str(caught.value), (
        f"{column} 을 바꿨는데 다른 검사가 먼저 잡았다: {caught.value}"
    )
