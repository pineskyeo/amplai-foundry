from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
    DecisionService,
    DirectAuthorityRequest,
    GovernanceEventError,
    GovernanceEventService,
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
    destinations: tuple[OutboxDestination, ...],
):
    with store.connect() as connection, governance_transaction(connection):
        return service.append_decision_in_transaction(
            connection,
            PROPOSAL,
            command_id=command_id,
            event_type="proposal.approved",
            authority=_authority(),
            before_state="reviewed",
            after_state="approved",
            definition_digest=f"sha256:{'a' * 64}",
            source_state_revision=state_revision,
            payload={"state_revision": state_revision, "status": "approved"},
            destinations=destinations,
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
        assert connection.execute("SELECT COUNT(*) FROM governance_audit_events").fetchone() == (0,)


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
        connection.execute("DROP TRIGGER governance_outbox_events_no_delete")
        connection.execute(
            "DELETE FROM governance_outbox_events WHERE event_id = ?",
            (outbox[0].event_id,),
        )
        connection.execute(
            """
            CREATE TRIGGER governance_outbox_events_no_delete
            BEFORE DELETE ON governance_outbox_events
            BEGIN
                SELECT RAISE(ABORT, 'governance outbox is durable');
            END
            """
        )

    with pytest.raises(GovernanceEventError, match="AUDIT_OUTBOX_MISMATCH"):
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
    _append(events, store, command_id="command-1", state_revision=2, destinations=(destination,))
    _append(events, store, command_id="command-2", state_revision=3, destinations=(destination,))
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(lease_seconds=5),
        clock=clock,
    )

    first = dispatcher.claim_next("worker-a")
    assert first is not None and first.destination_sequence == 1
    assert dispatcher.claim_next("worker-b") is None
    clock.advance(timedelta(seconds=6))
    reclaimed = dispatcher.claim_next("worker-b")
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
    second = dispatcher.claim_next("worker-b")
    assert second is not None and second.destination_sequence == 2


def test_competing_dispatchers_claim_one_fenced_lease(tmp_path: Path) -> None:
    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref="provider:slack:C456"),),
    )

    def claim(worker: str):
        return OutboxDispatcher(store, clock=lambda: NOW).claim_next(worker)

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

    superseded = dispatcher.supersede_pending(
        first[0].event_id,
        replacement_event_id=second[0].event_id,
    )
    assert superseded.state is OutboxState.SUPERSEDED
    claimed = dispatcher.claim_next("worker")
    assert claimed is not None and claimed.event_id == second[0].event_id


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


def test_post_send_crash_reconciles_without_duplicate_delivery(tmp_path: Path) -> None:
    class Remote:
        destination_ref = "provider:slack:C456"

        def __init__(self) -> None:
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
    _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref=Remote.destination_ref),),
    )
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(lease_seconds=5),
        clock=clock,
    )
    remote = Remote()
    claimed = dispatcher.claim_next("crashed")
    assert claimed is not None
    remote.send(claimed)
    clock.advance(timedelta(seconds=6))

    delivered = dispatcher.deliver_next("recovery", remote)
    assert delivered is not None and delivered.state is OutboxState.DELIVERED
    assert remote.sends == 1


def test_unreconcilable_remote_state_moves_to_dlq_and_operator_hold(tmp_path: Path) -> None:
    class DivergedRemote:
        destination_ref = "provider:slack:C456"

        def reconcile(self, event):
            raise OutboxReconcileError("OUTBOX_REMOTE_DIVERGED")

        def send(self, event):
            raise AssertionError("diverged remote must not be sent again")

    store, _active, _draft = _active_proposal(tmp_path)
    events = GovernanceEventService(store, clock=lambda: NOW)
    _append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
        destinations=(OutboxDestination(destination_ref=DivergedRemote.destination_ref),),
    )

    result = OutboxDispatcher(store, clock=lambda: NOW).deliver_next(
        "worker",
        DivergedRemote(),
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
        destination_ref=destination_ref,
    )

    with pytest.raises(GovernanceEventError, match="YAML_SEQUENCE_CAS_CONFLICT"):
        projection.send(second[0])
    first_receipt = projection.send(first[0])
    assert projection.reconcile(first[0]) == first_receipt
    second_receipt = projection.send(second[0])
    assert projection.reconcile(second[0]) == second_receipt
