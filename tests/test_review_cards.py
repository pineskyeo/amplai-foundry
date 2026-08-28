from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from pydantic import SecretStr

import amplai_foundry.governance.slack_cards as slack_cards_module
import amplai_foundry.governance.slack_projection as slack_projection_module
from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActionTokenState,
    ActiveProposalRepository,
    ActorBindingService,
    ActorRef,
    ActorType,
    AuthorityPermission,
    AuthorityService,
    BindingApproval,
    BindingTarget,
    BoundedIngressAck,
    ChannelProvider,
    ChannelRef,
    DecisionAction,
    DecisionService,
    DirectAuthorityRequest,
    ImmutableDefinitionObjectStore,
    IngressDecisionWorker,
    IngressService,
    OutboxConfig,
    OutboxDispatcher,
    OutboxReconcileError,
    OutboxRetryableError,
    OutboxState,
    PreparedReviewActionSet,
    ProposalDefinitionManifest,
    ProposalRef,
    ProposalSubmissionService,
    ProviderEnvelope,
    ReviewActionSetService,
    ReviewCardError,
    ReviewCardService,
    ReviewProjectionPayload,
    SlackBlockActionAuthenticator,
    SlackInstallationPolicy,
    SlackProposalCardRenderer,
    canonicalize_definition,
)
from amplai_foundry.governance.events import GovernanceEventError, GovernanceEventService
from amplai_foundry.governance.slack_cards import SlackCardRenderingError
from amplai_foundry.governance.slack_projection import (
    SlackHistoryMessage,
    SlackHistoryPage,
    SlackProjectionDestination,
    SlackProjectionSearchCapError,
    SlackProjectionTerminalError,
    SlackSendResult,
    SlackTransportError,
)
from amplai_foundry.governance.store import GovernanceStore, GovernanceStoreError

NOW = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
AUTHORITY_PROJECT = ProjectRef(
    project_id="governance",
    namespace="org/default/project/governance",
)
PROPOSAL = ProposalRef(project_ref=PROJECT, proposal_id="PROP-20260812-ABCDEF12")
MANAGER = ActorRef(actor_id="ACT-MANAGER-1", actor_type=ActorType.HUMAN)
REVIEWER = ActorRef(actor_id="ACT-REVIEWER-1", actor_type=ActorType.HUMAN)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C456",
    message_id="1710000000.000100",
)
FINGERPRINT = hashlib.sha256(b"review-card-request").hexdigest()
SIGNING_SECRET = "review-card-signing-secret"


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value


def _approval(index: int) -> BindingApproval:
    return BindingApproval(
        approval_id=f"APR-{index:016X}",
        approved_by=MANAGER,
        reason="approve review card fixture",
    )


def _request(*, channel: ChannelRef = CHANNEL) -> DirectAuthorityRequest:
    return DirectAuthorityRequest(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        external_actor_id="U123",
        project_ref=PROJECT,
        request_id="review-card-request",
        channel=channel,
    )


def _seed(
    tmp_path: Path,
    *,
    operation_count: int = 4,
) -> tuple[
    GovernanceStore,
    ImmutableDefinitionObjectStore,
    ActiveProposalRepository,
    AuthorityService,
    MutableClock,
]:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    clock = MutableClock()
    bindings = ActorBindingService(store, AUTHORITY_PROJECT, clock=clock)
    bindings.bootstrap_manager(MANAGER)
    bindings.register_actor(REVIEWER, approval=_approval(1))
    bindings.grant_permission(
        REVIEWER,
        PROJECT,
        AuthorityPermission.PROPOSAL_SUBMIT_REVIEW,
        approval=_approval(2),
    )
    bindings.grant_permission(
        REVIEWER,
        PROJECT,
        AuthorityPermission.PROPOSAL_DECIDE,
        approval=_approval(3),
    )
    bindings.create_binding(
        BindingTarget(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:APP1",
            external_actor_id="U123",
            actor_ref=REVIEWER,
        ),
        approval=_approval(4),
    )
    objects = ImmutableDefinitionObjectStore(PROJECT, tmp_path)
    operations = tuple(
        {
            "operation_id": f"OP-{index + 1:03d}",
            "type": "CREATE" if index % 2 == 0 else "IGNORE",
            "title": f"Operation title {index + 1}",
        }
        for index in range(operation_count)
    )
    definition = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=PROPOSAL,
            operations=operations,
            base_revision="a13d92f",
            validation_policy_ref="policy/proposal-v3",
        )
    )
    object_ref = objects.put_definition_object(
        PROPOSAL,
        definition.canonical_bytes,
        definition.digest,
    )
    active = ActiveProposalRepository(store, objects)
    draft = active.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=object_ref,
    )
    authority = AuthorityService(store, clock=clock)
    ProposalSubmissionService(store, active, authority).submit_for_review(
        PROPOSAL,
        authority_request=_request(),
        expected_state_revision=draft.state_revision,
    )
    return store, objects, active, authority, clock


def _review_request(
    tmp_path: Path,
) -> tuple[
    GovernanceStore,
    ActiveProposalRepository,
    AuthorityService,
    MutableClock,
    object,
]:
    store, objects, active, authority, clock = _seed(tmp_path)
    result = ReviewCardService(store, authority, objects, clock=clock).request(
        PROPOSAL,
        reviewer_request=_request(),
        idempotency_key="review-card-1",
        request_fingerprint=FINGERPRINT,
    )
    return store, active, authority, clock, result


def _signed_envelope(*, action: DecisionAction, value: str) -> ProviderEnvelope:
    timestamp = str(int(NOW.timestamp()))
    payload = {
        "type": "block_actions",
        "trigger_id": "123.456.review-card",
        "team": {"id": "T123", "domain": "example"},
        "enterprise": None,
        "user": {"id": "U123", "team_id": "T123"},
        "api_app_id": "APP1",
        "container": {
            "type": "message",
            "channel_id": "C456",
            "message_ts": "1710000000.000200",
        },
        "actions": [
            {
                "type": "button",
                "action_id": action.value,
                "block_id": "proposal-actions",
                "action_ts": "1710000001.000300",
                "value": value,
            }
        ],
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    body = urlencode({"payload": encoded}).encode("utf-8")
    signature = hmac.new(
        SIGNING_SECRET.encode(),
        b"v0:" + timestamp.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return ProviderEnvelope(
        provider=ChannelProvider.SLACK,
        provider_installation_ref="T123:APP1",
        raw_body=body,
        headers={
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": f"v0={signature}",
        },
    )


def test_request_records_one_secret_free_review_event(tmp_path: Path) -> None:
    store, _active, _authority, _clock, raw_result = _review_request(tmp_path)
    result = raw_result
    assert hasattr(result, "payload")
    payload = result.payload

    assert payload.operation_count == 4
    assert payload.operation_counts == {"CREATE": 2, "IGNORE": 2}
    assert payload.operation_titles == (
        "Operation title 1",
        "Operation title 2",
        "Operation title 3",
    )
    assert payload.remaining_operation_count == 1
    assert payload.expires_at == NOW + timedelta(hours=24)
    with store.connect() as connection:
        command_count = connection.execute(
            "SELECT count(*) FROM governance_review_card_commands"
        ).fetchone()
        audit_count = connection.execute("SELECT count(*) FROM governance_audit_events").fetchone()
        outbox_count = connection.execute(
            "SELECT count(*) FROM governance_outbox_events"
        ).fetchone()
        payload_json = connection.execute(
            "SELECT payload_json FROM governance_review_card_commands"
        ).fetchone()
    assert command_count == (1,)
    assert audit_count == (1,)
    assert outbox_count == (1,)
    assert payload_json is not None
    serialized = str(payload_json[0])
    assert "reason" not in serialized
    assert "evidence" not in serialized
    assert "draft_path" not in serialized
    assert ".amplai/" not in serialized
    assert result.outbox_event_id


@pytest.mark.parametrize("operation_count", [1, 3, 4, 100])
def test_operation_summary_preserves_totals_and_stable_preview(
    tmp_path: Path,
    operation_count: int,
) -> None:
    store, objects, _active, authority, clock = _seed(
        tmp_path,
        operation_count=operation_count,
    )
    result = ReviewCardService(store, authority, objects, clock=clock).request(
        PROPOSAL,
        reviewer_request=_request(),
        idempotency_key=f"summary-{operation_count}",
        request_fingerprint=hashlib.sha256(str(operation_count).encode()).hexdigest(),
    )

    assert result.payload.operation_count == operation_count
    assert sum(result.payload.operation_counts.values()) == operation_count
    assert result.payload.operation_titles == tuple(
        f"Operation title {index}" for index in range(1, min(operation_count, 3) + 1)
    )
    assert result.payload.remaining_operation_count == max(0, operation_count - 3)


@pytest.mark.parametrize(
    "updates",
    [
        {"operation_counts": {"UNSUPPORTED_OPERATION": 4}},
        {
            "operation_titles": ("   ", "Operation title 2", "Operation title 3"),
        },
        {
            "operation_titles": ("x" * 257, "Operation title 2", "Operation title 3"),
        },
    ],
)
def test_public_review_payload_rejects_malformed_operation_summaries(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    _store, _active, _authority, _clock, result = _review_request(tmp_path)
    raw = result.payload.model_dump(mode="json")
    raw.update(updates)

    with pytest.raises(ValueError):
        ReviewProjectionPayload.model_validate(raw)


def test_public_review_payload_normalizes_operation_titles(tmp_path: Path) -> None:
    _store, _active, _authority, _clock, result = _review_request(tmp_path)
    raw = result.payload.model_dump(mode="json")
    raw["operation_titles"] = ("  First title  ", "Second title", "Third title")

    payload = ReviewProjectionPayload.model_validate(raw)

    assert payload.operation_titles[0] == "First title"


def test_identical_request_replays_without_another_event(tmp_path: Path) -> None:
    store, objects, _active, authority, clock = _seed(tmp_path)
    service = ReviewCardService(store, authority, objects, clock=clock)
    first = service.request(
        PROPOSAL,
        reviewer_request=_request(),
        idempotency_key="review-card-replay",
        request_fingerprint=FINGERPRINT,
    )
    second = service.request(
        PROPOSAL,
        reviewer_request=_request(),
        idempotency_key="review-card-replay",
        request_fingerprint=FINGERPRINT,
    )

    assert not first.replayed
    assert second.replayed
    assert second.model_copy(update={"replayed": False}) == first
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM governance_audit_events").fetchone() == (1,)


def test_same_slack_channel_orders_two_proposals_without_revision_collision(
    tmp_path: Path,
) -> None:
    store, objects, active, authority, clock = _seed(tmp_path)
    review_cards = ReviewCardService(store, authority, objects, clock=clock)
    first = review_cards.request(
        PROPOSAL,
        reviewer_request=_request(),
        idempotency_key="review-card-first-proposal",
        request_fingerprint=hashlib.sha256(b"review-card-first-proposal").hexdigest(),
    )
    first_event = OutboxDispatcher(store).get(first.outbox_event_id)
    second_ref = PROPOSAL.model_copy(update={"proposal_id": "PROP-20260812-1234ABCD"})
    definition = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=second_ref,
            operations=(
                {
                    "operation_id": "OP-001",
                    "type": "CREATE",
                    "title": "Second Proposal operation",
                },
            ),
            base_revision="a13d92f",
            validation_policy_ref="policy/proposal-v3",
        )
    )
    object_ref = objects.put_definition_object(
        second_ref,
        definition.canonical_bytes,
        definition.digest,
    )
    second_draft = active.activate_definition_revision(
        second_ref,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=object_ref,
    )
    ProposalSubmissionService(store, active, authority).submit_for_review(
        second_ref,
        authority_request=_request(),
        expected_state_revision=second_draft.state_revision,
    )
    second = review_cards.request(
        second_ref,
        reviewer_request=_request(),
        idempotency_key="review-card-second-proposal",
        request_fingerprint=hashlib.sha256(b"review-card-second-proposal").hexdigest(),
    )
    second_event = OutboxDispatcher(store).get(second.outbox_event_id)
    prepared = ReviewActionSetService(store, authority, clock=clock).prepare(second_event)
    approve = next(
        item for item in prepared.issued if item.record.allowed_action is DecisionAction.APPROVE
    )
    DecisionService(store, authority, clock=clock).decide(
        second_ref,
        action=DecisionAction.APPROVE,
        authority_request=_request(),
        raw_token=approve.raw_token,
        idempotency_key="decide-second-proposal",
        request_fingerprint=hashlib.sha256(b"decide-second-proposal").hexdigest(),
    )

    with store.connect() as connection:
        rows = connection.execute(
            """
            SELECT proposal_id, destination_ref, destination_sequence,
                   source_state_revision
            FROM governance_outbox_events
            WHERE destination_ref LIKE 'provider:slack:%'
            ORDER BY destination_sequence
            """
        ).fetchall()
    assert rows == [
        (PROPOSAL.proposal_id, first_event.destination_ref, 1, 2),
        (second_ref.proposal_id, second_event.destination_ref, 2, 2),
        (second_ref.proposal_id, second_event.destination_ref, 3, 3),
    ]
    GovernanceEventService(store).reconcile()


def test_same_snapshot_reviewer_and_slack_channel_rejects_another_message_intent(
    tmp_path: Path,
) -> None:
    store, objects, _active, authority, clock = _seed(tmp_path)
    service = ReviewCardService(store, authority, objects, clock=clock)
    service.request(
        PROPOSAL,
        reviewer_request=_request(),
        idempotency_key="review-card-channel-scope-1",
        request_fingerprint="1" * 64,
    )
    another_message = CHANNEL.model_copy(update={"message_id": "1710000000.000999"})

    with pytest.raises(ReviewCardError, match="REVIEW_CARD_ALREADY_REQUESTED"):
        service.request(
            PROPOSAL,
            reviewer_request=_request(channel=another_message),
            idempotency_key="review-card-channel-scope-2",
            request_fingerprint="2" * 64,
        )

    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_review_card_commands"
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM governance_outbox_events").fetchone() == (
            1,
        )


def test_same_snapshot_rejects_another_channel_intent(tmp_path: Path) -> None:
    store, objects, _active, authority, clock = _seed(tmp_path)
    service = ReviewCardService(store, authority, objects, clock=clock)
    service.request(
        PROPOSAL,
        reviewer_request=_request(),
        idempotency_key="review-card-original-channel",
        request_fingerprint="3" * 64,
    )
    another_channel = CHANNEL.model_copy(
        update={"channel_id": "C999", "message_id": "1710000000.999999"}
    )

    with pytest.raises(ReviewCardError, match="REVIEW_CARD_ALREADY_REQUESTED"):
        service.request(
            PROPOSAL,
            reviewer_request=_request(channel=another_channel),
            idempotency_key="review-card-other-channel",
            request_fingerprint="4" * 64,
        )

    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_review_card_commands"
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM governance_outbox_events").fetchone() == (
            1,
        )


def test_prepare_issues_three_hash_only_24_hour_actions(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    event = OutboxDispatcher(store).get(result.outbox_event_id)
    prepared = ReviewActionSetService(store, authority, clock=clock).prepare(event)

    assert prepared.view.generation == 1
    assert prepared.view.state == "issued"
    assert prepared.view.expires_at == NOW + timedelta(hours=24)
    assert {item.record.allowed_action for item in prepared.issued} == set(DecisionAction)
    assert all("raw_token=<redacted>" in repr(item) for item in prepared.issued)
    assert "issued=<redacted>" in repr(prepared)
    database_bytes = store.path.read_bytes()
    assert all(item.raw_token.encode() not in database_bytes for item in prepared.issued)


def test_confirmed_absence_revokes_old_generation_before_replacement(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    event = OutboxDispatcher(store).get(result.outbox_event_id)
    action_sets = ReviewActionSetService(store, authority, clock=clock)
    first = action_sets.prepare(event)
    second = action_sets.prepare(event)

    assert first.view.generation == 1
    assert second.view.generation == 2
    assert action_sets.get(event.event_id, 1).state == "revoked"
    decisions = DecisionService(store, authority, clock=clock)
    assert all(
        decisions.get_token(item.record.token_id).state is ActionTokenState.REVOKED
        for item in first.issued
    )
    assert all(item.record.state is ActionTokenState.ISSUED for item in second.issued)


def test_delayed_review_delivery_still_issues_a_fresh_24_hour_action_set(
    tmp_path: Path,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    clock.value = NOW + timedelta(hours=23)
    event = OutboxDispatcher(store).get(result.outbox_event_id)
    prepared = ReviewActionSetService(store, authority, clock=clock).prepare(event)

    assert prepared.view.issued_at == clock.value
    assert prepared.view.expires_at == clock.value + timedelta(hours=24)
    assert all(
        token.record.expires_at - token.record.issued_at == timedelta(hours=24)
        for token in prepared.issued
    )


class CapturingTransport:
    def __init__(self) -> None:
        self.posted: list[tuple[Mapping[str, object], Mapping[str, object]]] = []

    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        self.posted.append((payload, marker))
        return SlackSendResult(channel=channel, ts="1710000000.000200")

    def read_history(
        self,
        *,
        channel: str,
        cursor: str | None,
        limit: int,
    ) -> SlackHistoryPage:
        messages = tuple(
            SlackHistoryMessage(
                ts="1710000000.000200",
                metadata=marker,
                app_id="APP1",
            )
            for _payload, marker in self.posted
        )
        return SlackHistoryPage(messages=messages)


class TerminalTransport(CapturingTransport):
    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        raise SlackTransportError("denied", error_code="invalid_auth")


class InterruptingTransport(CapturingTransport):
    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        raise KeyboardInterrupt("interrupted while the Review Card was being posted")


class WrongChannelTransport(CapturingTransport):
    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        self.posted.append((payload, marker))
        return SlackSendResult(channel="C-OTHER", ts="1710000000.000200")


class EchoingFailureTransport(CapturingTransport):
    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        card = payload["blocks"][0]  # type: ignore[index]
        raw_value = str(card["actions"][0]["value"])
        raise SlackTransportError(
            f"provider echoed {raw_value}",
            error_code=raw_value,
            transport_exception=raw_value,
        )


class FailingAbandonActionSetService(ReviewActionSetService):
    def abandon(self, event_id: str, generation: int) -> None:
        raise OSError("injected action-set cleanup failure")


class FixedPreparedActionSetService(ReviewActionSetService):
    def __init__(
        self,
        delegate: ReviewActionSetService,
        prepared: object,
    ) -> None:
        self.delegate = delegate
        self.prepared = prepared

    def prepare(self, event: object) -> object:
        return self.prepared

    def abandon(self, event_id: str, generation: int) -> None:
        self.delegate.abandon(event_id, generation)


class FailingReviewRenderer(SlackProposalCardRenderer):
    def render_review(self, payload: object, action_set: object) -> Mapping[str, object]:
        raise SlackCardRenderingError("injected render failure")


class AmbiguousTransport(CapturingTransport):
    def read_history(
        self,
        *,
        channel: str,
        cursor: str | None,
        limit: int,
    ) -> SlackHistoryPage:
        return SlackHistoryPage(messages=(), next_cursor="more")


class CrashBeforePostTransport(CapturingTransport):
    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        raise KeyboardInterrupt


def test_remote_acceptance_reconciles_without_reissuing_actions(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    pending = OutboxDispatcher(store).get(result.outbox_event_id)
    leased = pending.model_copy(
        update={
            "state": OutboxState.LEASED,
            "attempts": 1,
            "claim_generation": 1,
        }
    )
    transport = CapturingTransport()
    action_sets = ReviewActionSetService(store, authority, clock=clock)
    destination = SlackProjectionDestination(
        transport,
        destination_ref=leased.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )

    assert destination.send(leased) == "slack:C456:1710000000.000200"
    retry = leased.model_copy(
        update={
            "attempts": 2,
            "claim_generation": 2,
            "last_error_code": "OUTBOX_LEASE_EXPIRED",
        }
    )

    assert destination.reconcile(retry) == "slack:C456:1710000000.000200"
    assert len(transport.posted) == 1
    assert action_sets.get(leased.event_id, 1).state == "issued"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_review_action_sets"
        ).fetchone() == (1,)


def test_dispatcher_restart_replaces_tokens_after_crash_before_post(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    pending = OutboxDispatcher(store).get(result.outbox_event_id)
    action_sets = ReviewActionSetService(store, authority, clock=clock)
    crashing = SlackProjectionDestination(
        CrashBeforePostTransport(),
        destination_ref=pending.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(
            lease_seconds=1,
            max_attempts=3,
            retry_base_seconds=1,
            retry_cap_seconds=1,
        ),
        clock=clock,
    )

    with pytest.raises(KeyboardInterrupt):
        dispatcher.deliver_next("dispatcher-before-post", crashing)

    assert action_sets.get(result.outbox_event_id, 1).state == "issued"
    clock.value += timedelta(seconds=2)
    assert dispatcher.deliver_next("dispatcher-reclaim", crashing) is None
    clock.value += timedelta(seconds=1)
    recovered_transport = CapturingTransport()
    recovered = SlackProjectionDestination(
        recovered_transport,
        destination_ref=pending.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )

    delivered = dispatcher.deliver_next("dispatcher-reclaim", recovered)

    assert delivered is not None
    assert delivered.state is OutboxState.DELIVERED
    assert len(recovered_transport.posted) == 1
    assert action_sets.get(result.outbox_event_id, 1).state == "revoked"
    assert action_sets.get(result.outbox_event_id, 2).state == "issued"


def test_dispatcher_restart_recovers_remote_acceptance_before_local_mark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    pending = OutboxDispatcher(store).get(result.outbox_event_id)
    action_sets = ReviewActionSetService(store, authority, clock=clock)
    transport = CapturingTransport()
    destination = SlackProjectionDestination(
        transport,
        destination_ref=pending.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )
    config = OutboxConfig(
        lease_seconds=1,
        max_attempts=3,
        retry_base_seconds=1,
        retry_cap_seconds=1,
    )
    dispatcher = OutboxDispatcher(store, config=config, clock=clock)

    def _crash_after_remote_acceptance(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("crash before local receipt persistence")

    monkeypatch.setattr(dispatcher, "mark_delivered", _crash_after_remote_acceptance)
    with pytest.raises(GovernanceStoreError):
        dispatcher.deliver_next("dispatcher-accepted", destination)
    monkeypatch.undo()

    assert len(transport.posted) == 1
    assert action_sets.get(result.outbox_event_id, 1).state == "issued"
    clock.value += timedelta(seconds=2)
    restarted = OutboxDispatcher(store, config=config, clock=clock)
    assert restarted.deliver_next("dispatcher-restarted", destination) is None
    clock.value += timedelta(seconds=1)

    delivered = restarted.deliver_next("dispatcher-restarted", destination)

    assert delivered is not None
    assert delivered.state is OutboxState.DELIVERED
    assert delivered.remote_receipt == "slack:C456:1710000000.000200"
    assert len(transport.posted) == 1
    assert action_sets.get(result.outbox_event_id, 1).state == "issued"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_review_action_sets"
        ).fetchone() == (1,)


def test_terminal_post_failure_revokes_the_undelivered_action_set(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    pending = OutboxDispatcher(store).get(result.outbox_event_id)
    event = pending.model_copy(
        update={"state": OutboxState.LEASED, "attempts": 1, "claim_generation": 1}
    )
    action_sets = ReviewActionSetService(store, authority, clock=clock)
    destination = SlackProjectionDestination(
        TerminalTransport(),
        destination_ref=event.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )

    with pytest.raises(SlackProjectionTerminalError):
        destination.send(event)

    assert action_sets.get(event.event_id, 1).state == "revoked"


def test_response_channel_mismatch_dead_letters_and_revokes_without_replacement(
    tmp_path: Path,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event_id = raw_result.outbox_event_id
    pending = OutboxDispatcher(store).get(event_id)
    action_sets = ReviewActionSetService(store, authority, clock=clock)
    transport = WrongChannelTransport()
    destination = SlackProjectionDestination(
        transport,
        destination_ref=pending.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )
    dispatcher = OutboxDispatcher(
        store,
        clock=clock,
        config=OutboxConfig(max_attempts=3),
    )

    failed = dispatcher.deliver_next("wrong-channel-worker", destination)

    assert failed is not None
    assert failed.state is OutboxState.DEAD_LETTER
    assert failed.last_error_code == "SLACK_RESPONSE_CHANNEL_MISMATCH"
    assert action_sets.get(event_id, 1).state == "revoked"
    decisions = DecisionService(store, authority, clock=clock)
    with store.connect() as connection:
        token_ids = connection.execute(
            """
            SELECT approve_token_id, request_changes_token_id, reject_token_id
            FROM governance_review_action_sets WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        action_set_count = connection.execute(
            "SELECT count(*) FROM governance_review_action_sets WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    assert token_ids is not None
    assert all(
        decisions.get_token(str(token_id)).state is ActionTokenState.REVOKED
        for token_id in token_ids
    )

    assert dispatcher.deliver_next("wrong-channel-worker", destination) is None
    assert len(transport.posted) == 1
    assert action_set_count == (1,)


def test_stale_attempt_cleanup_cannot_revoke_a_newer_generation(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = (
        OutboxDispatcher(store)
        .get(raw_result.outbox_event_id)
        .model_copy(update={"state": OutboxState.LEASED, "attempts": 1, "claim_generation": 1})
    )
    action_sets = ReviewActionSetService(store, authority, clock=clock)
    stale_prepared = action_sets.prepare(event)
    replacement = action_sets.prepare(
        event.model_copy(
            update={
                "attempts": 2,
                "claim_generation": 2,
                "last_error_code": "OUTBOX_LEASE_EXPIRED",
            }
        )
    )
    stale_service = FixedPreparedActionSetService(action_sets, stale_prepared)
    destination = SlackProjectionDestination(
        TerminalTransport(),
        destination_ref=event.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=stale_service,  # type: ignore[arg-type]
    )

    with pytest.raises(SlackProjectionTerminalError):
        destination.send(event)

    assert action_sets.get(event.event_id, stale_prepared.view.generation).state == "revoked"
    assert action_sets.get(event.event_id, replacement.view.generation).state == "issued"


def test_review_failure_traceback_locals_contain_no_raw_action_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    pending = OutboxDispatcher(store).get(result.outbox_event_id)
    event = pending.model_copy(
        update={"state": OutboxState.LEASED, "attempts": 1, "claim_generation": 1}
    )
    token_counter = 0
    raw_counter = 0

    def _token_hex(length: int) -> str:
        nonlocal token_counter, raw_counter
        if length == 8:
            token_counter += 1
            return f"{token_counter:016x}"
        raw_counter += 1
        return "feedface" * 3 + f"{raw_counter:08x}"

    monkeypatch.setattr(secrets, "token_hex", _token_hex)
    destination = SlackProjectionDestination(
        TerminalTransport(),
        destination_ref=event.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=ReviewActionSetService(store, authority, clock=clock),
    )

    with pytest.raises(SlackProjectionTerminalError) as caught:
        destination.send(event)

    rendered: list[str] = [str(caught.value), repr(caught.value)]
    current: BaseException | None = caught.value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        traceback_cursor = current.__traceback__
        while traceback_cursor is not None:
            rendered.append(repr(traceback_cursor.tb_frame.f_locals))
            traceback_cursor = traceback_cursor.tb_next
        current = current.__cause__ or current.__context__
    assert "feedface" * 3 not in "\n".join(rendered)


@pytest.mark.parametrize(
    "failure_mode",
    [
        "terminal_cleanup",
        "render_cleanup",
        "interrupt",
        "marker_failure",
        "marker_interrupt",
        "provider_echo",
    ],
)
def test_every_review_failure_boundary_scrubs_raw_action_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    pending = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    event = pending.model_copy(
        update={"state": OutboxState.LEASED, "attempts": 1, "claim_generation": 1}
    )
    token_counter = 0
    raw_counter = 0

    def _token_hex(length: int) -> str:
        nonlocal token_counter, raw_counter
        if length == 8:
            token_counter += 1
            return f"{token_counter:016x}"
        raw_counter += 1
        return "decafbad" * 3 + f"{raw_counter:08x}"

    monkeypatch.setattr(secrets, "token_hex", _token_hex)
    if failure_mode in {"marker_failure", "marker_interrupt"}:

        def _interrupt_marker(_event: object) -> Mapping[str, object]:
            if failure_mode == "marker_failure":
                raise RuntimeError("marker construction failed")
            raise KeyboardInterrupt("interrupted while the marker was being built")

        monkeypatch.setattr(slack_projection_module, "build_slack_marker", _interrupt_marker)
    failing_cleanup = failure_mode in {"terminal_cleanup", "render_cleanup"}
    action_sets = (
        FailingAbandonActionSetService(store, authority, clock=clock)
        if failing_cleanup
        else ReviewActionSetService(store, authority, clock=clock)
    )
    transport = {
        "terminal_cleanup": TerminalTransport(),
        "render_cleanup": CapturingTransport(),
        "interrupt": InterruptingTransport(),
        "marker_failure": CapturingTransport(),
        "marker_interrupt": CapturingTransport(),
        "provider_echo": EchoingFailureTransport(),
    }[failure_mode]
    renderer = FailingReviewRenderer() if failure_mode == "render_cleanup" else None
    destination = SlackProjectionDestination(
        transport,
        destination_ref=event.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        card_renderer=renderer,
        review_action_sets=action_sets,
    )

    with pytest.raises(BaseException) as caught:
        destination.send(event)

    rendered: list[str] = [str(caught.value), repr(caught.value)]
    current: BaseException | None = caught.value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        traceback_cursor = current.__traceback__
        while traceback_cursor is not None:
            if "/src/amplai_foundry/" in traceback_cursor.tb_frame.f_code.co_filename:
                rendered.append(repr(traceback_cursor.tb_frame.f_locals))
            traceback_cursor = traceback_cursor.tb_next
        current = current.__cause__ or current.__context__
    assert "decafbad" * 3 not in "\n".join(rendered)

    if failure_mode in {"terminal_cleanup", "render_cleanup"}:
        assert isinstance(caught.value, OutboxReconcileError)
        assert caught.value.code == "REVIEW_ACTION_SET_CLEANUP_FAILED"
    elif failure_mode == "marker_failure":
        assert isinstance(caught.value, OutboxReconcileError)
        assert caught.value.code == "SLACK_MARKER_BUILD_FAILED"
        assert action_sets.get(event.event_id, 1).state == "revoked"
    elif failure_mode in {"interrupt", "marker_interrupt"}:
        assert isinstance(caught.value, KeyboardInterrupt)
        assert str(caught.value) == "Slack Review Card delivery interrupted."
        if failure_mode == "marker_interrupt":
            assert action_sets.get(event.event_id, 1).state == "revoked"
    else:
        assert isinstance(caught.value, SlackProjectionTerminalError)
        assert caught.value.code.startswith("SLACK_PROJECTION_TERMINAL_ERROR:unrecognized_")


def test_review_failure_showlocals_child_is_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_mode = os.environ.get("AMPLAI_REVIEW_TRACEBACK_CHILD")
    if failure_mode not in {
        "terminal",
        "terminal_cleanup",
        "render_cleanup",
        "interrupt",
        "marker_failure",
        "marker_interrupt",
        "provider_echo",
    }:
        return
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    pending = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    event = pending.model_copy(
        update={"state": OutboxState.LEASED, "attempts": 1, "claim_generation": 1}
    )
    token_counter = 0
    raw_counter = 0

    def _token_hex(length: int) -> str:
        nonlocal token_counter, raw_counter
        if length == 8:
            token_counter += 1
            return f"{token_counter:016x}"
        raw_counter += 1
        return "cafebabe" * 3 + f"{raw_counter:08x}"

    monkeypatch.setattr(secrets, "token_hex", _token_hex)
    if failure_mode in {"marker_failure", "marker_interrupt"}:

        def _interrupt_marker(_event: object) -> Mapping[str, object]:
            if failure_mode == "marker_failure":
                raise RuntimeError("marker construction failed")
            raise KeyboardInterrupt("interrupted while the marker was being built")

        monkeypatch.setattr(slack_projection_module, "build_slack_marker", _interrupt_marker)
    failing_cleanup = failure_mode in {"terminal_cleanup", "render_cleanup"}
    action_sets = (
        FailingAbandonActionSetService(store, authority, clock=clock)
        if failing_cleanup
        else ReviewActionSetService(store, authority, clock=clock)
    )
    transport = {
        "terminal": TerminalTransport(),
        "terminal_cleanup": TerminalTransport(),
        "render_cleanup": CapturingTransport(),
        "interrupt": InterruptingTransport(),
        "marker_failure": CapturingTransport(),
        "marker_interrupt": CapturingTransport(),
        "provider_echo": EchoingFailureTransport(),
    }[failure_mode]
    renderer = FailingReviewRenderer() if failure_mode == "render_cleanup" else None
    destination = SlackProjectionDestination(
        transport,
        destination_ref=event.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        card_renderer=renderer,
        review_action_sets=action_sets,
    )
    try:
        destination.send(event)
    except BaseException as error:
        raise AssertionError("intentional sanitized traceback failure") from error
    raise AssertionError("review failure was expected")


@pytest.mark.parametrize(
    "failure_mode",
    [
        "terminal",
        "terminal_cleanup",
        "render_cleanup",
        "interrupt",
        "marker_failure",
        "marker_interrupt",
        "provider_echo",
    ],
)
def test_pytest_showlocals_output_contains_no_raw_action_credential(
    failure_mode: str,
) -> None:
    environment = os.environ.copy()
    environment["AMPLAI_REVIEW_TRACEBACK_CHILD"] = failure_mode
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__)),
            "-k",
            "review_failure_showlocals_child_is_secret_free",
            "--showlocals",
            "-q",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    diagnostics = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "cafebabe" * 3 not in diagnostics


def test_review_destination_channel_mismatch_issues_no_action_set(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    pending = OutboxDispatcher(store).get(result.outbox_event_id)
    event = pending.model_copy(
        update={"state": OutboxState.LEASED, "attempts": 1, "claim_generation": 1}
    )
    action_sets = ReviewActionSetService(store, authority, clock=clock)
    destination = SlackProjectionDestination(
        CapturingTransport(),
        destination_ref=event.destination_ref,
        channel="C-WRONG",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )

    with pytest.raises(OutboxReconcileError, match="REVIEW_CARD_CHANNEL_MISMATCH"):
        destination.send(event)

    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_review_action_sets"
        ).fetchone() == (0,)


def test_ambiguous_reconciliation_creates_no_action_generation(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    pending = OutboxDispatcher(store).get(result.outbox_event_id)
    retry = pending.model_copy(
        update={
            "state": OutboxState.LEASED,
            "attempts": 2,
            "claim_generation": 2,
            "last_error_code": "OUTBOX_LEASE_EXPIRED",
        }
    )
    destination = SlackProjectionDestination(
        AmbiguousTransport(),
        destination_ref=retry.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        max_history_pages=1,
        review_action_sets=ReviewActionSetService(store, authority, clock=clock),
    )

    with pytest.raises(SlackProjectionSearchCapError):
        destination.reconcile(retry)

    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_review_action_sets"
        ).fetchone() == (0,)


def test_review_card_has_exactly_three_confirmed_actions(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    event = OutboxDispatcher(store).get(result.outbox_event_id)
    prepared = ReviewActionSetService(store, authority, clock=clock).prepare(event)

    rendered = SlackProposalCardRenderer().render_review(result.payload, prepared)
    card = rendered["blocks"][0]  # type: ignore[index]
    actions = card["actions"]

    assert [action["action_id"] for action in actions] == [
        "approve",
        "request_changes",
        "reject",
    ]
    assert all("confirm" in action for action in actions)
    assert actions[0]["style"] == "primary"
    assert "style" not in actions[1]
    assert actions[2]["style"] == "danger"
    subtitle = str(card["subtitle"]["text"])
    assert PROJECT.namespace in subtitle
    assert PROJECT.project_id in subtitle
    for rendered_action in actions:
        confirmation = str(rendered_action["confirm"]["text"]["text"])
        assert "shown" in confirmation
        assert PROPOSAL.proposal_id in confirmation
        assert f"content r{result.payload.content_revision}" in confirmation
        assert f"state r{result.payload.state_revision}" in confirmation


def test_review_card_presentation_is_recursively_immutable(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    prepared = ReviewActionSetService(store, authority, clock=clock).prepare(event)
    rendered = SlackProposalCardRenderer().render_review(raw_result.payload, prepared)
    card = rendered["blocks"][0]  # type: ignore[index]

    with pytest.raises(TypeError):
        rendered["text"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        card["actions"] = ()  # type: ignore[index]
    with pytest.raises(AttributeError):
        card["actions"].append({})  # type: ignore[union-attr]


def test_review_card_displays_the_exact_action_set_expiration(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    clock.value = NOW.replace(second=37, microsecond=123456)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    prepared = ReviewActionSetService(store, authority, clock=clock).prepare(event)

    rendered = SlackProposalCardRenderer().render_review(raw_result.payload, prepared)
    card = rendered["blocks"][0]  # type: ignore[index]
    exact_expiration = prepared.view.expires_at.isoformat().replace("+00:00", "Z")

    assert exact_expiration in str(card["subtext"]["text"])
    assert exact_expiration in str(rendered["text"])


def test_review_fallback_reserves_exact_expiration_for_maximum_reviewer_identity(
    tmp_path: Path,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    clock.value = NOW.replace(second=37, microsecond=123456)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    prepared = ReviewActionSetService(store, authority, clock=clock).prepare(event)
    payload = raw_result.payload.model_copy(update={"reviewer_external_key": "U" * 128})

    rendered = SlackProposalCardRenderer().render_review(payload, prepared)
    fallback = str(rendered["text"])
    exact_expiration = prepared.view.expires_at.isoformat().replace("+00:00", "Z")

    assert len(fallback) <= 200
    assert raw_result.payload.aggregate_ref.proposal_id in fallback
    assert exact_expiration in fallback


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        (DecisionAction.APPROVE, "approved"),
        (DecisionAction.REQUEST_CHANGES, "changes_requested"),
        (DecisionAction.REJECT, "rejected"),
    ],
)
def test_production_rendered_button_value_completes_a_signed_governed_round_trip(
    tmp_path: Path,
    action: DecisionAction,
    expected_status: str,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    event = OutboxDispatcher(store).get(result.outbox_event_id)
    prepared = ReviewActionSetService(store, authority, clock=clock).prepare(event)
    rendered = SlackProposalCardRenderer().render_review(result.payload, prepared)
    card = rendered["blocks"][0]
    button = next(
        candidate for candidate in card["actions"] if candidate["action_id"] == action.value
    )
    authenticator = SlackBlockActionAuthenticator(
        SlackInstallationPolicy(
            provider_installation_ref="T123:APP1",
            signing_secret=SecretStr(SIGNING_SECRET),
            api_app_id="APP1",
            workspace_ids=frozenset({"T123"}),
        ),
        clock=clock,
    )
    ingress = IngressService(store, authenticator, clock=clock)
    ack = BoundedIngressAck(ingress).submit(
        _signed_envelope(action=action, value=str(button["value"]))
    )
    worker = IngressDecisionWorker(
        store,
        ingress,
        DecisionService(store, authority, clock=clock),
    )

    decision = worker.process_next("signed-review-worker")

    assert ack.success
    assert decision is not None
    assert decision.decision is not None
    assert decision.decision.action is action
    assert decision.decision.proposal_status.value == expected_status
    with store.connect() as connection:
        result_rows = connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone()
        result_cards = connection.execute(
            "SELECT count(*) FROM governance_outbox_events "
            "WHERE destination_ref LIKE 'provider:slack:%' "
            "AND payload_json LIKE '%\"proposal_status\"%'"
        ).fetchone()
    assert result_rows == (1,)
    assert result_cards == (1,)
    with store.connect() as connection:
        provider_events = connection.execute(
            """
            SELECT event_id, destination_ref, destination_sequence, payload_json
            FROM governance_outbox_events
            WHERE destination_ref LIKE 'provider:slack:%'
            ORDER BY destination_sequence
            """
        ).fetchall()
    assert len(provider_events) == 2
    review_event, result_event = provider_events
    assert str(review_event[0]) == event.event_id
    assert str(review_event[1]) == str(result_event[1]) == event.destination_ref
    assert (int(str(review_event[2])), int(str(result_event[2]))) == (1, 2)
    assert '"proposal_status"' in str(result_event[3])
    dispatcher = OutboxDispatcher(store, clock=clock)
    claimed = dispatcher.claim_next("ordered-review-worker", destination_ref=event.destination_ref)
    assert claimed is not None and claimed.event_id == event.event_id
    assert (
        dispatcher.claim_next("blocked-result-worker", destination_ref=event.destination_ref)
        is None
    )
    assert len(card["body"]["text"]) <= 200
    assert len(card["subtext"]["text"]) <= 200


def test_card_action_from_same_channel_different_message_decides_once(tmp_path: Path) -> None:
    store, active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    event = OutboxDispatcher(store).get(result.outbox_event_id)
    prepared = ReviewActionSetService(store, authority, clock=clock).prepare(event)
    approve = next(
        item for item in prepared.issued if item.record.allowed_action is DecisionAction.APPROVE
    )
    click_channel = CHANNEL.model_copy(update={"message_id": "1710000000.999999"})

    decision = DecisionService(store, authority, clock=clock).decide(
        PROPOSAL,
        action=DecisionAction.APPROVE,
        authority_request=_request(channel=click_channel),
        raw_token=approve.raw_token,
        idempotency_key="review-decision-1",
        request_fingerprint=hashlib.sha256(b"review-decision-1").hexdigest(),
    )

    assert decision.proposal_status.value == "approved"
    assert active.get(PROPOSAL).status.value == "approved"  # type: ignore[union-attr]
    assert (
        ReviewActionSetService(store, authority, clock=clock)
        .get(
            event.event_id,
            1,
        )
        .state
        == "consumed"
    )
    decisions = DecisionService(store, authority, clock=clock)
    sibling_states = {
        decisions.get_token(item.record.token_id).state
        for item in prepared.issued
        if item.record.allowed_action is not DecisionAction.APPROVE
    }
    assert sibling_states == {ActionTokenState.REVOKED}


def test_review_event_and_action_set_pass_integrity_reconciliation(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    event = OutboxDispatcher(store).get(result.outbox_event_id)
    ReviewActionSetService(store, authority, clock=clock).prepare(event)

    GovernanceEventService(store).reconcile()


def test_command_and_action_set_schema_are_guarded(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    result = raw_result
    event = OutboxDispatcher(store).get(result.outbox_event_id)
    ReviewActionSetService(store, authority, clock=clock).prepare(event)

    with store.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE governance_review_card_commands SET reviewer_external_key = 'U999'"
        )
    with store.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM governance_review_action_sets")


class OperationalErrorPrepareActionSetService(ReviewActionSetService):
    """`prepare()` 가 SQLite 동시 writer 에서 흔한 실패를 던진다.

    `sqlite3.OperationalError` 는 `ReviewCardError` 도 `SlackCardRenderingError` 도
    `ValueError` 도 아니라 `send` 의 `except BaseException` 으로 떨어진다.
    """

    def prepare(self, event: object) -> object:
        raise sqlite3.OperationalError("database is locked")


class CorruptStorePrepareActionSetService(ReviewActionSetService):
    """`prepare` 가 store 손상을 알린다.

    `sqlite_errorcode` 를 직접 세운다. 손상된 파일을 진짜로 만들 수 없으므로 판별기가 읽는
    값을 그대로 준다 — 판별기는 그 하위 byte 만 본다.
    """

    def prepare(self, event: object) -> object:
        error = sqlite3.DatabaseError("database disk image is malformed")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        raise error


class TypeErrorReviewRenderer(SlackProposalCardRenderer):
    """`render_review` 가 분류되지 않은 Exception 을 던진다."""

    def render_review(self, payload: object, action_set: object) -> Mapping[str, object]:
        raise TypeError("injected unclassified render failure")


class KeyboardInterruptPrepareActionSetService(ReviewActionSetService):
    def prepare(self, event: object) -> object:
        raise KeyboardInterrupt


def _leased_review_event(store: object, event_id: str) -> object:
    return (
        OutboxDispatcher(store)  # type: ignore[arg-type]
        .get(event_id)
        .model_copy(update={"state": OutboxState.LEASED, "attempts": 1, "claim_generation": 1})
    )


def _destination_hold(store: object, destination_ref: str) -> tuple[int] | None:
    with store.connect() as connection:  # type: ignore[attr-defined]
        return connection.execute(
            "SELECT operator_hold FROM governance_outbox_destinations WHERE destination_ref = ?",
            (destination_ref,),
        ).fetchone()


# T010 AC-01 — 일시적 store 실패는 예산을 쓴다.
#
# **이 test 는 wave 4 에서 반대를 요구했다.** round 10 `C-1` 이 "dead letter + operator
# hold" 를 선언했고 그대로 구현·고정한 결과, sqlite lock 한 번에 destination 전체가 즉시
# 영구 정지했다 (round 11 `R-2`). 같은 저장소의 `ingress_worker` 는 같은 예외를 재시도로
# 분류한다. 기대를 지우는 것이 아니라 옮긴다 — 아래 AC-02 가 소진 후 결말을 계속 요구한다.
def test_a_transient_prepare_failure_retries_instead_of_holding(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event_id = raw_result.outbox_event_id
    pending = OutboxDispatcher(store).get(event_id)
    action_sets = OperationalErrorPrepareActionSetService(store, authority, clock=clock)
    destination = SlackProjectionDestination(
        CapturingTransport(),
        destination_ref=pending.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )
    dispatcher = OutboxDispatcher(store, clock=clock, config=OutboxConfig(max_attempts=3))

    failed = dispatcher.deliver_next("transient-worker", destination)

    assert failed is not None
    assert failed.state is OutboxState.RETRY_WAIT, "일시적 실패가 destination 을 멈추면 안 된다"
    # 원인을 남긴다. generic `OUTBOX_DELIVERY_FAILED` 로 덮이면 dead letter 가 원인을
    # 가리키지 않는다 — round 11 `R-1` 이 그 형태를 결함으로 셌다.
    assert failed.last_error_code == "REVIEW_CARD_PREPARE_UNAVAILABLE"
    assert _destination_hold(store, pending.destination_ref) == (0,)


# T010 AC-02 — 예산을 소진하면 여전히 dead letter 와 hold 에 도달하고, 그 dead letter 가
# 원인 code 를 가진다. 재분류가 안전망을 없애지 않는다는 것이 이 test 의 요지다.
def test_a_transient_prepare_failure_still_dead_letters_once_the_budget_is_spent(
    tmp_path: Path,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event_id = raw_result.outbox_event_id
    pending = OutboxDispatcher(store).get(event_id)
    action_sets = OperationalErrorPrepareActionSetService(store, authority, clock=clock)
    destination = SlackProjectionDestination(
        CapturingTransport(),
        destination_ref=pending.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )
    dispatcher = OutboxDispatcher(store, clock=clock, config=OutboxConfig(max_attempts=3))

    failed = None
    for _ in range(3):
        clock.value = clock.value + timedelta(minutes=10)
        failed = dispatcher.deliver_next("transient-worker", destination)

    assert failed is not None
    assert failed.state is OutboxState.DEAD_LETTER
    assert failed.last_error_code == "REVIEW_CARD_PREPARE_UNAVAILABLE"
    assert _destination_hold(store, pending.destination_ref) == (1,)


# T010 AC-03 — store 손상은 예산을 쓰지 않는다. 다시 시도해도 같기 때문이다.
def test_a_corrupt_store_dead_letters_on_the_first_attempt(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event_id = raw_result.outbox_event_id
    pending = OutboxDispatcher(store).get(event_id)
    action_sets = CorruptStorePrepareActionSetService(store, authority, clock=clock)
    destination = SlackProjectionDestination(
        CapturingTransport(),
        destination_ref=pending.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )
    dispatcher = OutboxDispatcher(store, clock=clock, config=OutboxConfig(max_attempts=3))

    failed = dispatcher.deliver_next("corrupt-worker", destination)

    assert failed is not None
    assert failed.state is OutboxState.DEAD_LETTER
    assert failed.attempts == 1, "손상은 재시도가 확정적으로 무의미하다"
    assert failed.last_error_code == "REVIEW_CARD_PREPARE_FAILED"
    assert _destination_hold(store, pending.destination_ref) == (1,)


def test_ordinary_render_exception_dead_letters_and_revokes_its_generation(
    tmp_path: Path,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event_id = raw_result.outbox_event_id
    pending = OutboxDispatcher(store).get(event_id)
    action_sets = ReviewActionSetService(store, authority, clock=clock)
    destination = SlackProjectionDestination(
        CapturingTransport(),
        destination_ref=pending.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
        card_renderer=TypeErrorReviewRenderer(),
    )
    dispatcher = OutboxDispatcher(store, clock=clock, config=OutboxConfig(max_attempts=3))

    failed = dispatcher.deliver_next("render-exception-worker", destination)

    assert failed is not None
    assert failed.state is OutboxState.DEAD_LETTER
    assert failed.last_error_code == "REVIEW_CARD_PREPARE_FAILED"
    assert action_sets.get(event_id, 1).state == "revoked"


# T010 AC-05 — 어느 분류로 가든 `send` 가 던지는 것은 `Exception` 하위여야 하고 원본
# 메시지를 흘리면 안 된다. 분류가 바뀌어도 이 두 성질은 유지된다.
@pytest.mark.parametrize(
    ("service", "expected_type", "expected_code"),
    [
        (
            OperationalErrorPrepareActionSetService,
            OutboxRetryableError,
            "REVIEW_CARD_PREPARE_UNAVAILABLE",
        ),
        (
            CorruptStorePrepareActionSetService,
            OutboxReconcileError,
            "REVIEW_CARD_PREPARE_FAILED",
        ),
    ],
)
def test_a_prepare_failure_raises_a_catchable_error_without_leaking_its_cause(
    tmp_path: Path,
    service: type,
    expected_type: type,
    expected_code: str,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    leased = _leased_review_event(store, raw_result.outbox_event_id)
    action_sets = service(store, authority, clock=clock)
    destination = SlackProjectionDestination(
        CapturingTransport(),
        destination_ref=leased.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )

    with pytest.raises(expected_type) as caught:
        destination.send(leased)

    assert caught.value.code == expected_code
    assert isinstance(caught.value, Exception)
    assert caught.value.__cause__ is None
    assert "database is locked" not in str(caught.value)
    assert "database disk image is malformed" not in str(caught.value)


def test_process_interruption_during_prepare_still_propagates(tmp_path: Path) -> None:
    """평범한 Exception 만 재분류한다. 진짜 interruption 은 그대로 올라간다."""
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    leased = _leased_review_event(store, raw_result.outbox_event_id)
    action_sets = KeyboardInterruptPrepareActionSetService(store, authority, clock=clock)
    destination = SlackProjectionDestination(
        CapturingTransport(),
        destination_ref=leased.destination_ref,
        channel="C456",
        app_id="APP1",
        max_attempts=3,
        review_action_sets=action_sets,
    )

    with pytest.raises(KeyboardInterrupt):
        destination.send(leased)


def test_sanitized_interruption_fallback_is_catchable(tmp_path: Path) -> None:
    """호출자가 판별을 빠뜨려도 bare `BaseException` 으로 새지 않는다.

    모든 호출자가 `"exception"` 을 먼저 걸러내므로 이 분기는 도달하지 않는다. 그래도
    fallback 이 `Exception` 하위여야 `deliver_next` 의 generic handler 가 잡는다.
    """
    with pytest.raises(RuntimeError):
        SlackProjectionDestination._raise_sanitized_interruption("exception")


def _prepared_for(store: object, authority: object, clock: object, event_id: str) -> object:
    event = OutboxDispatcher(store).get(event_id)  # type: ignore[arg-type]
    return ReviewActionSetService(store, authority, clock=clock).prepare(event)  # type: ignore[arg-type]


def test_render_review_scrubs_frames_on_its_real_failure_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실제 `render_review` 경로가 raw credential 을 traceback 에서 지운다.

    기존 scrub test 는 전부 `FailingReviewRenderer` 로 `render_review` 를 통째로
    override 해서 실제 `except BaseException: _clear_exception_frames(error)` 가 한 번도
    실행되지 않았다. `_clear_exception_frames` 를 no-op 으로 만들어도 전 suite 가 초록이었고
    (round 10 regression `C-13`), no-op 상태에서는 `_render_review` frame 의 `card` local 이
    raw ActionToken 세 개를 그대로 들고 있는 것이 실측됐다.

    실패는 card 가 완성된 **뒤**에 나야 한다. 그래서 `_render_review` 의 마지막 호출인
    `_freeze_mapping` 에서 낸다 (FR-018, SC-005).
    """
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    prepared = _prepared_for(store, authority, clock, raw_result.outbox_event_id)
    raw_values = [
        prepared.button_value(action)  # type: ignore[attr-defined]
        for action in DecisionAction
    ]
    assert all(raw_values)

    def _boom(value: Mapping[str, object]) -> Mapping[str, object]:
        raise RuntimeError("injected failure after the card is built")

    monkeypatch.setattr(slack_cards_module, "_freeze_mapping", _boom)

    with pytest.raises(RuntimeError) as caught:
        SlackProposalCardRenderer().render_review(raw_result.payload, prepared)  # type: ignore[arg-type]

    frames = []
    tb = caught.value.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame)
        tb = tb.tb_next

    # scrub 이 살아 있으면 `render_review` 와 `_render_review` frame 이 traceback 에서
    # 사라지고 이 test 의 frame 만 남는다. no-op 이면 세 frame 이 모두 남는다.
    assert [frame.f_code.co_name for frame in frames] == [
        "test_render_review_scrubs_frames_on_its_real_failure_path"
    ]

    # 이 test 자신의 frame 은 비교용 sentinel 을 local 로 들고 있으므로 제외한다. 검사
    # 대상은 renderer 가 남긴 frame 이다.
    own_code = test_render_review_scrubs_frames_on_its_real_failure_path.__code__
    leaked = [
        (frame.f_code.co_name, name)
        for frame in frames
        if frame.f_code is not own_code
        for name, value in frame.f_locals.items()
        for raw in raw_values
        if raw and raw in repr(value)
    ]
    assert leaked == []


def test_render_review_rejects_a_non_issued_action_set(tmp_path: Path) -> None:
    """취소되거나 소비된 action set 은 살아 있는 Card 로 렌더되지 않는다 (FR-014)."""
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    prepared = _prepared_for(store, authority, clock, raw_result.outbox_event_id)

    for dead_state in ("revoked", "consumed", "expired"):
        doctored = PreparedReviewActionSet(
            view=prepared.view.model_copy(update={"state": dead_state}),  # type: ignore[attr-defined]
            issued=prepared.issued,  # type: ignore[attr-defined]
        )
        with pytest.raises(SlackCardRenderingError) as caught:
            SlackProposalCardRenderer().render_review(raw_result.payload, doctored)
        assert caught.value.reason == "action_set_state_mismatch"


def test_render_review_rejects_an_action_set_bound_to_another_reviewer(tmp_path: Path) -> None:
    """다른 reviewer 에 묶인 token 은 Card 에 실리지 않는다 (FR-009, FR-010)."""
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    prepared = _prepared_for(store, authority, clock, raw_result.outbox_event_id)
    other_reviewer = raw_result.payload.model_copy(
        update={"reviewer_actor_id": f"{raw_result.payload.reviewer_actor_id}-other"}
    )

    with pytest.raises(SlackCardRenderingError) as caught:
        SlackProposalCardRenderer().render_review(other_reviewer, prepared)  # type: ignore[arg-type]
    assert caught.value.reason == "action_set_binding_mismatch"


def test_render_review_rejects_an_action_set_bound_to_another_channel(tmp_path: Path) -> None:
    """다른 channel 에 묶인 token 은 Card 에 실리지 않는다 (FR-009)."""
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    prepared = _prepared_for(store, authority, clock, raw_result.outbox_event_id)
    bound = raw_result.payload.bound_channel_ref
    other_channel = raw_result.payload.model_copy(
        update={"bound_channel_ref": bound.model_copy(update={"channel_id": "C999"})}
    )

    with pytest.raises(SlackCardRenderingError) as caught:
        SlackProposalCardRenderer().render_review(other_channel, prepared)  # type: ignore[arg-type]
    assert caught.value.reason == "action_set_binding_mismatch"


def test_action_set_preparation_rejects_a_mismatched_event_payload_digest(
    tmp_path: Path,
) -> None:
    """검증되지 않은 payload 에는 raw credential 을 발급하지 않는다.

    `_command_for_event` 의 대조는 `prepare()` 가 token 세 장을 만들기 **직전**의
    fail-closed 지점이다. 세 절 중 하나라도 어긋나면 발급 전에 멈춘다. 이 검사를 통째로
    무력화해도 전 suite 가 초록이었고 `REVIEW_CARD_ROOT_MISMATCH` 는 `tests/` 전체에 한 번도
    나오지 않았다 (round 10 regression `C-10`).

    durable command row 자체는 immutable trigger 가 지킨다. 그래서 여기서는 그 row 와
    어긋나는 event 를 들이민다.
    """
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    tampered = event.model_copy(update={"payload_digest": "0" * 64})

    with pytest.raises(ReviewCardError) as caught:
        ReviewActionSetService(store, authority, clock=clock).prepare(tampered)

    assert caught.value.code == "REVIEW_CARD_ROOT_MISMATCH"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_review_action_sets"
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM governance_action_tokens").fetchone() == (
            0,
        )


def test_action_set_preparation_rejects_a_payload_that_differs_from_the_command(
    tmp_path: Path,
) -> None:
    """event payload 가 기록된 canonical 과 다르면 발급 전에 멈춘다."""
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    altered = dict(event.payload)
    altered["reviewer_external_key"] = "U999"
    tampered = event.model_copy(update={"payload": altered})

    with pytest.raises(ReviewCardError) as caught:
        ReviewActionSetService(store, authority, clock=clock).prepare(tampered)

    assert caught.value.code == "REVIEW_CARD_ROOT_MISMATCH"
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM governance_action_tokens").fetchone() == (
            0,
        )


def _review_payload_dict(payload: object) -> dict[str, object]:
    return dict(payload.model_dump(mode="json"))  # type: ignore[attr-defined]


def test_review_payload_rejects_a_non_slack_channel(tmp_path: Path) -> None:
    """Review Card 는 Slack channel 에만 묶인다 (FR-005)."""
    _store, _active, _authority, _clock, raw_result = _review_request(tmp_path)
    raw = _review_payload_dict(raw_result.payload)
    # 그 자체로는 유효한 Telegram ChannelRef 다. 거부는 ReviewProjectionPayload 의
    # provider 검사에서 나야 한다.
    raw["bound_channel_ref"] = ChannelRef(
        provider=ChannelProvider.TELEGRAM,
        chat_id="12345",
        message_id="1710000000.000100",
    ).model_dump(mode="json")

    with pytest.raises(ValueError, match="Slack"):
        ReviewProjectionPayload.model_validate(raw)


def test_review_payload_rejects_a_broken_operation_count_total(tmp_path: Path) -> None:
    """operation_counts 합계와 operation_count 가 어긋나면 거부된다 (FR-006, SC-006)."""
    _store, _active, _authority, _clock, raw_result = _review_request(tmp_path)
    raw = _review_payload_dict(raw_result.payload)
    raw["operation_count"] = int(raw["operation_count"]) + 1  # type: ignore[arg-type]
    raw["remaining_operation_count"] = int(raw["operation_count"]) - len(
        raw["operation_titles"]  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="합계"):
        ReviewProjectionPayload.model_validate(raw)


def test_review_payload_rejects_a_broken_remaining_operation_count(tmp_path: Path) -> None:
    """remaining_operation_count 가 preview 와 어긋나면 거부된다 (FR-006, FR-020)."""
    _store, _active, _authority, _clock, raw_result = _review_request(tmp_path)
    raw = _review_payload_dict(raw_result.payload)
    raw["remaining_operation_count"] = int(raw["remaining_operation_count"]) + 1  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="remaining"):
        ReviewProjectionPayload.model_validate(raw)


def test_a_replayed_request_with_a_different_fingerprint_conflicts(tmp_path: Path) -> None:
    """같은 key 에 다른 fingerprint 는 충돌이다 (FR-015, SC-003).

    `IDEMPOTENCY_CONFLICT` 는 `tests/test_review_cards.py` 에 한 번도 나오지 않았고 이
    binding 을 무력화해도 전 suite 가 초록이었다 (round 10 regression `C-6`).
    """
    store, _active, authority, clock, _raw_result = _review_request(tmp_path)
    objects = ImmutableDefinitionObjectStore(store)
    service = ReviewCardService(store, authority, objects, clock=clock)

    with pytest.raises(ReviewCardError) as caught:
        service.request(
            PROPOSAL,
            reviewer_request=_request(),
            idempotency_key="review-card-1",
            request_fingerprint=hashlib.sha256(b"a-different-request").hexdigest(),
        )

    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_review_card_commands"
        ).fetchone() == (1,)


def test_render_review_rejects_an_action_set_without_exactly_three_actions(
    tmp_path: Path,
) -> None:
    """정확히 세 action 이 아니면 거부된다 (FR-007)."""
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    prepared = _prepared_for(store, authority, clock, raw_result.outbox_event_id)

    two_actions = PreparedReviewActionSet(
        view=prepared.view,  # type: ignore[attr-defined]
        issued=prepared.issued[:2],  # type: ignore[attr-defined]
    )
    with pytest.raises(SlackCardRenderingError) as caught:
        SlackProposalCardRenderer().render_review(raw_result.payload, two_actions)
    assert caught.value.reason == "action_set_count_mismatch"


def test_render_review_rejects_three_actions_that_are_not_the_contract_set(
    tmp_path: Path,
) -> None:
    """세 개여도 계약과 다른 집합이면 거부된다 (FR-007)."""
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    prepared = _prepared_for(store, authority, clock, raw_result.outbox_event_id)
    duplicated = PreparedReviewActionSet(
        view=prepared.view,  # type: ignore[attr-defined]
        issued=(prepared.issued[0], prepared.issued[0], prepared.issued[1]),  # type: ignore[attr-defined]
    )

    with pytest.raises(SlackCardRenderingError) as caught:
        SlackProposalCardRenderer().render_review(raw_result.payload, duplicated)
    assert caught.value.reason == "action_set_action_mismatch"


def test_render_review_rejects_an_expiry_that_does_not_match_the_view(tmp_path: Path) -> None:
    """token 의 만료와 action set view 의 만료가 어긋나면 거부된다 (FR-008)."""
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    prepared = _prepared_for(store, authority, clock, raw_result.outbox_event_id)
    shifted = PreparedReviewActionSet(
        view=prepared.view.model_copy(  # type: ignore[attr-defined]
            update={"expires_at": prepared.view.expires_at + timedelta(seconds=1)}  # type: ignore[attr-defined]
        ),
        issued=prepared.issued,  # type: ignore[attr-defined]
    )

    with pytest.raises(SlackCardRenderingError) as caught:
        SlackProposalCardRenderer().render_review(raw_result.payload, shifted)
    assert caught.value.reason == "action_set_binding_mismatch"


def test_review_body_stays_inside_the_rendered_limit(tmp_path: Path) -> None:
    """긴 operation title 3개가 와도 body 가 한도 안으로 잘린다 (FR-020, SC-006)."""
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    prepared = _prepared_for(store, authority, clock, raw_result.outbox_event_id)
    raw = _review_payload_dict(raw_result.payload)
    # `_review_body` 는 title 마다 `per_title` 로 먼저 좁힌다. 그래서 title 만 길게 해서는
    # 최종 절단이 필요 없다. 고정부(operation counts 요약과 remaining 줄)가 이미 한도를
    # 넘어야 마지막 `_truncate` 가 유일한 방어선이 된다.
    raw["operation_counts"] = {
        "CREATE": 1000000,
        "UPDATE": 1000000,
        "LINK": 1000000,
        "MERGE": 1000000,
        "SPLIT": 1000000,
        "SUPERSEDE": 1000000,
        "CONFLICT": 1000000,
        "IGNORE": 1000000,
    }
    raw["operation_count"] = 8000000
    raw["operation_titles"] = ["T" * 200, "U" * 200, "V" * 200]
    raw["remaining_operation_count"] = 8000000 - 3
    long_payload = ReviewProjectionPayload.model_validate(raw)
    rebound = PreparedReviewActionSet(
        view=prepared.view,  # type: ignore[attr-defined]
        issued=prepared.issued,  # type: ignore[attr-defined]
    )

    rendered = SlackProposalCardRenderer().render_review(long_payload, rebound)

    body = str(rendered["blocks"][0]["body"]["text"])  # type: ignore[index]
    assert len(body) <= 200
    assert "Content revision:" in body


def test_reconcile_rejects_a_duplicated_review_card_audit(tmp_path: Path) -> None:
    """한 review card command 에 audit event 는 정확히 하나다.

    `len(audits) != 1` 은 0건뿐 아니라 **2건 이상**도 막는다. 그 guard 를 `< 1` 로 약화해도
    전 suite 가 초록이었다 (round 10 regression M19b). audit table 은 UPDATE 와 DELETE 를
    trigger 로 막지만 INSERT 는 막지 않으므로 중복 삽입이 실제 위험이다.
    """
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    ReviewActionSetService(store, authority, clock=clock).prepare(event)
    with store.connect() as connection:
        columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(governance_audit_events)").fetchall()
        ]
        original = connection.execute(
            "SELECT * FROM governance_audit_events WHERE event_type = ?",
            ("proposal.review_card_requested",),
        ).fetchone()
        assert original is not None
        duplicate = list(original)
        duplicate[columns.index("event_id")] = "AUD-duplicate-review-card"
        duplicate[columns.index("aggregate_sequence")] = (
            int(original[columns.index("aggregate_sequence")]) + 1000
        )
        connection.execute(
            f"INSERT INTO governance_audit_events VALUES ({','.join('?' for _ in columns)})",
            duplicate,
        )
        connection.commit()

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert "REVIEW_CARD_AUDIT_MISMATCH" in str(caught.value)


# ---------------------------------------------------------------------------
# MGC-012-P5-T013 — 무결성 대조 성분과 outbox 개수 (round 11 R-7, R-8)
# ---------------------------------------------------------------------------
#
# 세 guard 모두 오늘 맞게 동작한다. 없는 것은 그 정확성을 붙잡는 test 다. round 11 mutation
# 이 M39, M40, 그리고 개수 검사 약화에서 살아남았다.
#
# **성분 하나만 어긋나게 한다.** 여러 곳을 동시에 망가뜨리면 다른 guard 가 먼저 걸려 무엇이
# 잡았는지 알 수 없고, 그러면 겨냥한 guard 를 고정한 것이 아니다. round 11 이 `T006`
# evidence 의 "무결성 대조는 전부 도달 불가" 를 부분 오류로 정정한 이유가 이것이다.
#
# UPDATE 를 막는 trigger 를 먼저 지운다. trigger 는 정상 경로를 지키고, 무결성 대조는
# **파일이 밖에서 바뀐 경우**를 지킨다. 후자를 재현하려면 전자를 치워야 한다.


def _drop_trigger(connection: object, name: str) -> None:
    connection.execute(f"DROP TRIGGER {name}")  # type: ignore[attr-defined]


# T013 AC-02 — 감사 대조의 actor 성분. 이 성분을 지우면 다른 reviewer 이름으로 기록된
# review card 요청이 정상으로 통과한다.
def test_reconcile_rejects_a_review_card_audit_bound_to_another_actor(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    ReviewActionSetService(store, authority, clock=clock).prepare(event)

    with store.connect() as connection:
        _drop_trigger(connection, "governance_audit_events_no_update")
        changed = connection.execute(
            "UPDATE governance_audit_events SET actor_id = ? WHERE event_type = ?",
            ("ACT-someone-else", "proposal.review_card_requested"),
        )
        assert changed.rowcount == 1, "전제: 바꿀 audit row 가 정확히 하나다"
        connection.commit()

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert "REVIEW_CARD_AUDIT_MISMATCH" in str(caught.value)


# T013 AC-01 — root 대조의 canonical payload 성분.
#
# **digest column 은 건드리지 않는다.** digest 는 payload 에서 다시 계산한 값과 비교되므로
# 저장된 JSON 만 바꾸면 digest 두 성분은 여전히 맞고 이 성분 하나만 어긋난다. 그래서 이
# test 가 겨냥한 것을 정확히 겨냥한다.
def test_prepare_rejects_a_tampered_stored_payload_json(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)

    with store.connect() as connection:
        stored = connection.execute(
            "SELECT payload_digest, payload_json FROM governance_review_card_commands"
        ).fetchone()
        assert stored is not None
        _drop_trigger(connection, "governance_review_card_commands_no_update")
        # 같은 내용, 다른 문자열. canonical form 이 아니게 만든다.
        connection.execute(
            "UPDATE governance_review_card_commands SET payload_json = ?",
            (str(stored[1]) + " ",),
        )
        connection.commit()
        after = connection.execute(
            "SELECT payload_digest FROM governance_review_card_commands"
        ).fetchone()
    assert after == (stored[0],), "digest 성분은 그대로 둔다. 어긋나는 것은 JSON 하나뿐이다"

    with pytest.raises(ReviewCardError) as caught:
        ReviewActionSetService(store, authority, clock=clock).prepare(event)

    assert caught.value.code == "REVIEW_CARD_ROOT_MISMATCH"


# T013 AC-03 — 요청당 outbox event 는 정확히 하나다.
#
# `!= 1` 은 0건뿐 아니라 2건 이상도 막는다. 약화하면 한 review 요청이 두 Card 를 배달할 수
# 있고, 그러면 reviewer 가 같은 snapshot 에 대해 유효한 Card 를 둘 보게 된다 (FR-013, FR-015).
def test_request_refuses_when_the_append_produces_more_than_one_outbox_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 첫 요청은 정상 경로를 태워 fixture 를 만든다. guard 는 두 번째 요청에서 본다.
    _review_request(tmp_path)
    original = GovernanceEventService._append_review_card_in_transaction

    def _two_events(self, connection, ref, *, request_key, authority, payload):  # type: ignore[no-untyped-def]
        audit, outbox = original(
            self, connection, ref, request_key=request_key, authority=authority, payload=payload
        )
        return audit, (*outbox, outbox[0])

    monkeypatch.setattr(GovernanceEventService, "_append_review_card_in_transaction", _two_events)

    with pytest.raises(ReviewCardError) as caught:
        _review_request(tmp_path / "second")

    assert caught.value.code == "REVIEW_CARD_OUTBOX_INVALID"


# ---------------------------------------------------------------------------
# round 12 RL-3·RL-4·RL-5 를 닫은 wave (workstream wave 6) — regression lens 가 찾은 공백
# ---------------------------------------------------------------------------
#
# **이 wave 는 task manifest 가 없다** (round 13 `CT-3`). 원래 주석은 `MGC-012-P5-T017` 을
# 달았는데 그 ID 는 그때 발급된 적이 없고, 지금은 전혀 다른 task 의 ID 다. round 참조로
# 바꾼다. 존재하지 않는 manifest 를 가리키는 주석은 다음 읽는 사람을 없는 파일로 보낸다.
#
# `T013` 이 각 검사의 성분을 **하나씩** 고정하면서 같은 검사의 형제 성분을 세지 않았다.
# `F-2` 와 같은 실수다 — 세되 끝까지 세지 않았다. 여기서 나머지를 채운다.
#
# **그 "나머지" 도 끝이 아니었다.** round 13 `F-4` 가 같은 `if` 의 둘을 더 잡았고, T018 이
# 전수를 세니 아홉 블록 69 성분 중 66 이 무방비였다. 아래 절이 그 후속이다.


# RL-3 — root 대조의 저장된 digest 성분.
#
# `T013` 은 저장된 JSON 성분만 고정했다. digest column 만 어긋난 경우는 무방비였다.
# **JSON 은 그대로 둔다** — digest 하나만 어긋나야 겨냥한 성분이 잡은 것이 증명된다.
def test_prepare_rejects_a_tampered_stored_payload_digest(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)

    with store.connect() as connection:
        before = connection.execute(
            "SELECT payload_digest, payload_json FROM governance_review_card_commands"
        ).fetchone()
        assert before is not None
        _drop_trigger(connection, "governance_review_card_commands_no_update")
        connection.execute(
            "UPDATE governance_review_card_commands SET payload_digest = ?",
            # 형식 CHECK 를 만족하는 **다른** digest 다. 형식을 깨면 CHECK 가 먼저 잡아
            # 겨냥한 성분을 고정한 것이 아니게 된다.
            ("sha256:" + "f" * 64,),
        )
        connection.commit()
        after = connection.execute(
            "SELECT payload_json FROM governance_review_card_commands"
        ).fetchone()
    assert after == (before[1],), "JSON 성분은 그대로 둔다. 어긋나는 것은 digest 하나뿐이다"

    with pytest.raises(ReviewCardError) as caught:
        ReviewActionSetService(store, authority, clock=clock).prepare(event)

    assert caught.value.code == "REVIEW_CARD_ROOT_MISMATCH"


# RL-4 — 감사 대조의 actor_type 성분. `T013` 이 고정한 actor_id 의 형제다.
def test_reconcile_rejects_a_review_card_audit_with_a_wrong_actor_type(tmp_path: Path) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    ReviewActionSetService(store, authority, clock=clock).prepare(event)

    with store.connect() as connection:
        _drop_trigger(connection, "governance_audit_events_no_update")
        changed = connection.execute(
            "UPDATE governance_audit_events SET actor_type = ? WHERE event_type = ?",
            ("service", "proposal.review_card_requested"),
        )
        assert changed.rowcount == 1
        connection.commit()

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert "REVIEW_CARD_AUDIT_MISMATCH" in str(caught.value)


# RL-5 — 감사 대조의 destination_count 성분.
#
# 이 값이 어긋나면 review card 요청이 실제와 다른 수의 destination 을 주장한 것으로 남는다.
def test_reconcile_rejects_a_review_card_audit_with_a_wrong_destination_count(
    tmp_path: Path,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    ReviewActionSetService(store, authority, clock=clock).prepare(event)

    with store.connect() as connection:
        _drop_trigger(connection, "governance_audit_events_no_update")
        changed = connection.execute(
            "UPDATE governance_audit_events SET destination_count = ? WHERE event_type = ?",
            (2, "proposal.review_card_requested"),
        )
        assert changed.rowcount == 1
        connection.commit()

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert "REVIEW_CARD_AUDIT_MISMATCH" in str(caught.value)


# ---------------------------------------------------------------------------
# MGC-012-P5-T018 — review Card 감사 대조의 **모든** 성분 (round 13 F-4)
# ---------------------------------------------------------------------------
#
# round 12 는 `RL-3`~`RL-5` 로 성분 셋을 지목하고 **공통 뿌리로 명시**했다. wave 6 은 그 셋만
# 고정했다. round 13 `F-4` 가 남은 둘(`definition_digest`, `destination_manifest_digest`)을
# 다시 잡았다. **같은 실수가 두 라운드 연속이다.**
#
# 이번에는 세지 않고 넘기지 않았다. `events.py:1254-1263` 의 `if` 성분을 전수 세고 하나씩
# 지워 재니 9개 중 이렇게 갈렸다.
#
#     killed   3  actor_id(L1257), actor_type(L1258), destination_count(L1263)
#     survived 6  event_type(L1255), proposal_ref(L1256), before_state(L1259),
#                 after_state(L1260), definition_digest(L1261),
#                 destination_manifest_digest(L1262)
#
# 아래가 그 여섯이다. `F-4` 가 지목한 둘은 그중 둘일 뿐이었다.
#
# **다른 검사가 먼저 걸리면 고정이 아니다.** audit row 를 고치면 hash chain 도 깨지므로
# `AUDIT_HASH_CHAIN_INVALID` 가 대신 잡을 수 있다. 그래서 각 test 는 코드가
# `REVIEW_CARD_AUDIT_MISMATCH` 인지 확인한다 — 겨냥한 대조가 잡았다는 증거다.


_REVIEW_AUDIT_COMPONENTS = [
    ("event_type", "proposal.review_card_rejected"),
    ("before_state", "draft"),
    ("after_state", "approved"),
    ("definition_digest", "sha256:" + "a" * 64),
    ("destination_manifest_digest", "sha256:" + "b" * 64),
]


@pytest.mark.parametrize(
    ("column", "wrong_value"),
    _REVIEW_AUDIT_COMPONENTS,
    ids=[column for column, _ in _REVIEW_AUDIT_COMPONENTS],
)
def test_reconcile_rejects_a_review_card_audit_with_any_wrong_component(
    tmp_path: Path, column: str, wrong_value: str
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    ReviewActionSetService(store, authority, clock=clock).prepare(event)

    with store.connect() as connection:
        _drop_trigger(connection, "governance_audit_events_no_update")
        changed = connection.execute(
            f"UPDATE governance_audit_events SET {column} = ? WHERE event_type = ?",
            (wrong_value, "proposal.review_card_requested"),
        )
        assert changed.rowcount == 1, "겨냥한 감사 row 하나만 바꾼다"
        connection.commit()

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert "REVIEW_CARD_AUDIT_MISMATCH" in str(caught.value), (
        f"{column} 을 바꿨는데 다른 검사가 먼저 잡았다면 이 성분을 고정한 것이 아니다: "
        f"{caught.value}"
    )


# T018 — 같은 `if` 의 `proposal_ref` 성분. 위 parametrize 와 분리한 이유는 저장 형태가
# 다르기 때문이다. `proposal_ref` 는 한 column 이 아니라 `project_namespace`, `project_id`,
# `proposal_id` 셋으로 저장되고 그 셋에 외래키가 걸려 있다. 존재하지 않는 proposal 을
# 가리키게 하려면 외래키를 잠시 꺼야 한다.
#
# 이것이 인위적으로 보일 수 있으나 막으려는 상태는 실재한다 — 저장소에 proposal 이 여럿일 때
# audit row 가 **다른** proposal 을 가리키면 외래키는 만족하면서 대조는 어긋난다. 여기서는
# 그 상태를 최소로 재현한다.
def test_reconcile_rejects_a_review_card_audit_pointing_at_another_proposal(
    tmp_path: Path,
) -> None:
    store, _active, authority, clock, raw_result = _review_request(tmp_path)
    event = OutboxDispatcher(store).get(raw_result.outbox_event_id)
    ReviewActionSetService(store, authority, clock=clock).prepare(event)

    with store.connect() as connection:
        _drop_trigger(connection, "governance_audit_events_no_update")
        connection.execute("PRAGMA foreign_keys = OFF")
        changed = connection.execute(
            "UPDATE governance_audit_events SET proposal_id = ? WHERE event_type = ?",
            ("PROP-20260817-0000BEEF", "proposal.review_card_requested"),
        )
        assert changed.rowcount == 1
        connection.commit()

    with pytest.raises(GovernanceEventError) as caught:
        GovernanceEventService(store).reconcile()

    assert "REVIEW_CARD_AUDIT_MISMATCH" in str(caught.value), (
        f"proposal_ref 성분을 고정한 것이 아니다: {caught.value}"
    )
