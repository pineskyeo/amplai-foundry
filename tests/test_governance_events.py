from __future__ import annotations

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
    OutboxState,
    ProposalDefinitionManifest,
    ProposalRef,
    ProposalSubmissionService,
    YamlProjectionDestination,
    canonicalize_definition,
)
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
