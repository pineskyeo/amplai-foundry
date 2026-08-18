from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import pytest
from pydantic import SecretStr

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    AckBudget,
    AckOutcome,
    ActiveProposalStatus,
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
    IngressCommandView,
    IngressConfig,
    IngressDecisionWorker,
    IngressError,
    IngressLeaseConflictError,
    IngressService,
    IngressState,
    IngressWorkerResult,
    InteractionFeedback,
    ProviderEnvelope,
    SafeInteractionOutcome,
    SlackBlockActionAuthenticator,
    SlackInstallationPolicy,
    VerifiedProviderCommand,
    WorkerOutcome,
)
from amplai_foundry.governance.events import GovernanceEventError
from amplai_foundry.governance.ingress_worker import _DENIED_CODES
from amplai_foundry.governance.slack_http import _SAFE_INTERACTION_MESSAGES
from amplai_foundry.governance.store import (
    GovernanceCommitAmbiguousError,
    GovernanceStore,
    GovernanceStoreError,
    governance_transaction,
)

NOW = datetime(2026, 7, 31, 1, 2, 3, tzinfo=UTC)
TIMESTAMP = str(int(NOW.timestamp()))
SIGNING_SECRET = "slack-signing-secret-fixture"
INSTALLATION_REF = "T123:A123"
RAW_TOKEN = "0123456789abcdef0123456789abcdef"
TOKEN_ID = "TOK-0123456789ABCDEF"
PROPOSAL_ID = "PROP-20260730-ABCDEF12"
PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
AUTHORITY_PROJECT = ProjectRef(
    project_id="governance",
    namespace="org/default/project/governance",
)
MANAGER = ActorRef(actor_id="ACT-MANAGER-1", actor_type=ActorType.HUMAN)
USER = ActorRef(actor_id="ACT-USER-1", actor_type=ActorType.HUMAN)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C789",
    message_id="1722387600.000200",
)
OTHER_CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK,
    workspace_id="T123",
    channel_id="C999",
    message_id="1722387600.000300",
)


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "block_actions",
        "trigger_id": "123.456.fixture",
        "team": {"id": "T123", "domain": "example"},
        "enterprise": None,
        "user": {"id": "U456", "team_id": "T123"},
        "api_app_id": "A123",
        "container": {
            "type": "message",
            "channel_id": "C789",
            "message_ts": "1722387600.000200",
        },
        "actions": [_action()],
    }
    payload.update(changes)
    return payload


def _action(
    *,
    token_id: str = TOKEN_ID,
    raw_token: str = RAW_TOKEN,
    action: DecisionAction = DecisionAction.APPROVE,
    action_ts: str = "1722387723.000400",
) -> dict[str, object]:
    return {
        "type": "button",
        "action_id": action.value,
        "block_id": "proposal-actions",
        "action_ts": action_ts,
        "value": f"{token_id}.{raw_token}",
    }


def _body(payload: dict[str, object] | None = None) -> bytes:
    encoded = json.dumps(
        payload or _payload(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return urlencode({"payload": encoded}).encode("utf-8")


def _signature(body: bytes, timestamp: str = TIMESTAMP) -> str:
    digest = hmac.new(
        SIGNING_SECRET.encode(),
        b"v0:" + timestamp.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def _envelope(
    body: bytes | None = None,
    *,
    timestamp: str = TIMESTAMP,
    signature: str | None = None,
    installation_ref: str = INSTALLATION_REF,
) -> ProviderEnvelope:
    raw_body = body or _body()
    return ProviderEnvelope(
        provider=ChannelProvider.SLACK,
        provider_installation_ref=installation_ref,
        raw_body=raw_body,
        headers={
            "X-Slack-Request-Timestamp": timestamp,
            "x-Slack-Signature": signature or _signature(raw_body, timestamp),
        },
    )


def _authenticator() -> SlackBlockActionAuthenticator:
    return SlackBlockActionAuthenticator(
        SlackInstallationPolicy(
            provider_installation_ref=INSTALLATION_REF,
            signing_secret=SecretStr(SIGNING_SECRET),
            api_app_id="A123",
            workspace_ids=frozenset({"T123"}),
        ),
        clock=lambda: NOW,
    )


class _StableEventAuthenticator:
    """A Provider whose event ID does not bind the raw body, like a Telegram update ID."""

    def verify(self, envelope: ProviderEnvelope) -> VerifiedProviderCommand:
        return VerifiedProviderCommand(
            external_event_id="TGM-000000000000001",
            external_actor_key="U456",
            channel_ref=CHANNEL,
            credential_id=TOKEN_ID,
            raw_credential=SecretStr(RAW_TOKEN),
            action=DecisionAction.APPROVE,
        )


def _monotonic(*values: float) -> Callable[[], float]:
    remaining = list(values)

    def _next() -> float:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _next


def _seed(
    store: GovernanceStore,
    *,
    token_expires_at: datetime | None = None,
    grant_decide: bool = True,
) -> None:
    _seed_bindings(store, grant_decide=grant_decide)
    _seed_proposal(store, expires_at=token_expires_at)


def _seed_bindings(store: GovernanceStore, *, grant_decide: bool = True) -> None:
    bindings = ActorBindingService(store, AUTHORITY_PROJECT, clock=lambda: NOW)
    bindings.bootstrap_manager(MANAGER)
    bindings.register_actor(
        USER,
        approval=BindingApproval(
            approval_id="APR-0000000000000001",
            approved_by=MANAGER,
            reason="register Slack integration actor",
        ),
    )
    if grant_decide:
        bindings.grant_permission(
            USER,
            PROJECT,
            AuthorityPermission.PROPOSAL_DECIDE,
            approval=BindingApproval(
                approval_id="APR-0000000000000002",
                approved_by=MANAGER,
                reason="grant Slack decision permission",
            ),
        )
    bindings.create_binding(
        BindingTarget(
            provider=ChannelProvider.SLACK,
            provider_installation_ref=INSTALLATION_REF,
            external_actor_id="U456",
            actor_ref=USER,
        ),
        approval=BindingApproval(
            approval_id="APR-0000000000000003",
            approved_by=MANAGER,
            reason="bind Slack immutable actor identity",
        ),
    )


def _seed_proposal(
    store: GovernanceStore,
    *,
    proposal_id: str = PROPOSAL_ID,
    token_id: str = TOKEN_ID,
    raw_token: str = RAW_TOKEN,
    expires_at: datetime | None = None,
    state_revision: int = 2,
    channel: ChannelRef = CHANNEL,
    action: DecisionAction = DecisionAction.APPROVE,
) -> None:
    channel_json = json.dumps(
        channel.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    definition_digest = f"sha256:{'1' * 64}"
    credential_hash = f"sha256:{hashlib.sha256(raw_token.encode()).hexdigest()}"
    token_expiry = expires_at or (NOW + timedelta(minutes=15))
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id, active_definition_digest,
                content_revision, state_revision, decision_epoch, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, 1, 'reviewed', ?, ?)
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
                proposal_id,
                definition_digest,
                state_revision,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO governance_action_tokens(
                token_id, token_hash, project_namespace, project_id, proposal_id,
                active_definition_digest, content_revision, state_revision,
                decision_epoch, allowed_action, allowed_actor_id, allowed_actor_type,
                bound_channel_json, issued_at, expires_at, state, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, 1,
                      ?, ?, 'human', ?, ?, ?, 'issued', NULL)
            """,
            (
                token_id,
                credential_hash,
                PROJECT.namespace,
                PROJECT.project_id,
                proposal_id,
                definition_digest,
                state_revision,
                action.value,
                USER.actor_id,
                channel_json,
                NOW.isoformat(),
                token_expiry.isoformat(),
            ),
        )


def _store(tmp_path: Path) -> GovernanceStore:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    return store


def _worker(
    store: GovernanceStore,
    ingress: IngressService,
    feedback: InteractionFeedback | None = None,
) -> IngressDecisionWorker:
    authority = AuthorityService(store, clock=lambda: NOW)
    return IngressDecisionWorker(
        store,
        ingress,
        DecisionService(store, authority, clock=lambda: NOW),
        feedback,
    )


class _CapturedFeedback:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, SafeInteractionOutcome]] = []

    def send(
        self,
        command: IngressCommandView,
        outcome: SafeInteractionOutcome,
    ) -> None:
        command_id = command.command_id
        self.calls.append((command_id, outcome))
        if self.fail:
            raise RuntimeError("feedback transport failed")


# A8 — durable commit before any success ack


def test_success_ack_is_returned_only_after_the_command_is_durably_committed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))

    response = boundary.submit(_envelope())

    assert response.success
    assert response.outcome is AckOutcome.ACCEPTED
    assert response.status_code == 200
    assert response.state is IngressState.PENDING
    assert response.command_id is not None
    stored = ingress.get(response.command_id)
    assert stored is not None
    assert stored.state is IngressState.PENDING


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (GovernanceStoreError("busy"), "INGRESS_UNAVAILABLE"),
        (GovernanceCommitAmbiguousError("ambiguous"), "GOVERNANCE_COMMIT_AMBIGUOUS"),
    ],
)
def test_store_failure_returns_non_success_ack_without_accepting_the_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))

    def _raise(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(store, "connect", _raise)
    response = boundary.submit(_envelope())

    assert not response.success
    assert response.outcome is AckOutcome.UNAVAILABLE
    assert response.status_code == 503
    assert response.error_code == expected_code
    assert response.command_id is None


def test_unexpected_error_fails_closed_instead_of_acking_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))

    def _raise(*args: object, **kwargs: object) -> object:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(store, "connect", _raise)
    response = boundary.submit(_envelope())

    assert not response.success
    assert response.status_code == 503
    assert response.error_code == "INGRESS_INTERNAL_ERROR"


# A13 — every rejection maps to a fail-closed status


@pytest.mark.parametrize(
    ("envelope_factory", "expected_status", "expected_code"),
    [
        (
            lambda: _envelope(signature="v0=" + "0" * 64),
            401,
            "SLACK_SIGNATURE_INVALID",
        ),
        (
            lambda: _envelope(timestamp=str(int(NOW.timestamp()) - 600)),
            401,
            "SLACK_TIMESTAMP_STALE",
        ),
        (
            lambda: _envelope(_body(_payload(api_app_id="A999"))),
            403,
            "SLACK_APP_DENIED",
        ),
        (
            lambda: _envelope(_body(_payload(type="view_submission"))),
            400,
            "SLACK_PAYLOAD_UNSUPPORTED",
        ),
        (
            lambda: _envelope(b"payload=%7B"),
            400,
            "SLACK_PAYLOAD_INVALID",
        ),
        (
            lambda: _envelope(installation_ref="T999:A999"),
            403,
            "SLACK_INSTALLATION_DENIED",
        ),
    ],
)
def test_rejected_requests_map_to_fail_closed_status(
    tmp_path: Path,
    envelope_factory: Callable[[], ProviderEnvelope],
    expected_status: int,
    expected_code: str,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))

    response = boundary.submit(envelope_factory())

    assert not response.success
    assert response.outcome is AckOutcome.REJECTED
    assert response.status_code == expected_status
    assert response.error_code == expected_code
    assert response.command_id is None


def test_oversized_body_is_rejected_before_any_deserialization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_raw_body_bytes=32),
        clock=lambda: NOW,
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))

    response = boundary.submit(_envelope())

    assert response.status_code == 413
    assert response.error_code == "INGRESS_PAYLOAD_TOO_LARGE"


# A12 — replay convergence and fingerprint conflict


def test_replayed_interaction_converges_on_the_first_durable_command(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    envelope = _envelope()

    first = boundary.submit(envelope)
    second = boundary.submit(envelope)

    assert first.outcome is AckOutcome.ACCEPTED
    assert second.outcome is AckOutcome.DUPLICATE
    assert second.success
    assert second.status_code == 200
    assert second.command_id == first.command_id


def test_a_modified_body_is_a_distinct_slack_event_rather_than_a_conflict(
    tmp_path: Path,
) -> None:
    # The Slack event ID binds the raw body digest, so a changed body cannot collide
    # with an earlier event ID. Conflict is only reachable for a Provider whose event
    # ID is body-independent.
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    first = boundary.submit(_envelope())

    forged = _payload()
    forged["trigger_id"] = "999.999.forged"
    second = boundary.submit(_envelope(_body(forged)))

    assert first.success
    assert second.success
    assert second.outcome is AckOutcome.ACCEPTED
    assert second.command_id != first.command_id


def test_a_reused_event_id_with_a_different_body_is_a_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _StableEventAuthenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success

    forged = _payload()
    forged["trigger_id"] = "999.999.forged"
    response = boundary.submit(_envelope(_body(forged)))

    assert not response.success
    assert response.outcome is AckOutcome.REJECTED
    assert response.status_code == 409
    assert response.error_code == "INGRESS_REPLAY_CONFLICT"


# A9 — the synchronous path stays inside the ack budget


def test_ingress_connection_timeout_must_be_shorter_than_the_ack_budget(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(busy_timeout_ms=2_500),
        clock=lambda: NOW,
    )

    BoundedIngressAck(ingress)

    with pytest.raises(ValueError, match="busy_timeout"):
        BoundedIngressAck(ingress, budget=AckBudget(total_ms=2_500))


def test_ack_budget_rejects_a_window_wider_than_the_slack_contract() -> None:
    with pytest.raises(ValueError, match="total_ms"):
        AckBudget(total_ms=3_001)


def test_success_is_downgraded_when_the_synchronous_path_exceeds_the_budget(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 3.0))

    response = boundary.submit(_envelope())

    assert not response.success
    assert response.outcome is AckOutcome.BUDGET_EXCEEDED
    assert response.status_code == 503
    assert response.error_code == "INGRESS_ACK_BUDGET_EXCEEDED"
    assert response.elapsed_ms >= 3_000
    assert response.command_id is not None
    stored = ingress.get(response.command_id)
    assert stored is not None


def test_budget_exceeded_command_converges_on_an_identical_redelivery(tmp_path: Path) -> None:
    # Slack does not redeliver interactive payloads — an over-budget request surfaces an
    # error to the user instead. This proves at-least-once redelivery of the SAME bytes
    # converges; the durable command is decided by the worker either way.
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    envelope = _envelope()
    slow = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 3.0))
    timed_out = slow.submit(envelope)
    assert timed_out.outcome is AckOutcome.BUDGET_EXCEEDED

    fast = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    retried = fast.submit(envelope)

    assert retried.success
    assert retried.outcome is AckOutcome.DUPLICATE
    assert retried.command_id == timed_out.command_id


# A10 — background decision handoff


def test_worker_decides_after_the_ack_and_completes_the_command(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    assert ack.success

    result = _worker(store, ingress).process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.COMPLETED
    assert result.command_id == ack.command_id
    assert result.state is IngressState.COMPLETED
    assert result.decision is not None
    assert result.decision.action is DecisionAction.APPROVE
    assert result.decision.proposal_ref.proposal_id == PROPOSAL_ID
    assert not result.decision.replayed


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        (DecisionAction.APPROVE, ActiveProposalStatus.APPROVED),
        (DecisionAction.REQUEST_CHANGES, ActiveProposalStatus.CHANGES_REQUESTED),
        (DecisionAction.REJECT, ActiveProposalStatus.REJECTED),
    ],
)
def test_each_signed_review_card_action_decides_once_and_enqueues_one_result(
    tmp_path: Path,
    action: DecisionAction,
    expected_status: ActiveProposalStatus,
) -> None:
    store = _store(tmp_path)
    _seed_bindings(store)
    _seed_proposal(store, action=action)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    body = _body(_payload(actions=[_action(action=action)]))

    ack = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05)).submit(_envelope(body))
    result = _worker(store, ingress).process_next("slack-worker")

    assert ack.success
    assert result is not None
    assert result.outcome is WorkerOutcome.COMPLETED
    assert result.decision is not None
    assert result.decision.action is action
    assert result.decision.proposal_status is expected_status
    with store.connect() as connection:
        result_rows = connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone()
        result_cards = connection.execute(
            "SELECT count(*) FROM governance_outbox_events "
            "WHERE destination_ref LIKE 'provider:slack:%'"
        ).fetchone()
    assert result_rows == (1,)
    assert result_cards == (1,)


def test_worker_is_idle_when_no_command_is_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)

    assert _worker(store, ingress).process_next("slack-worker") is None


def test_reclaimed_command_converges_on_the_first_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    now = [NOW]
    ingress = IngressService(store, _authenticator(), clock=lambda: now[0])
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    assert ack.command_id is not None
    worker = _worker(store, ingress)

    def _crash(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("killed before the ingress command was finalized")

    monkeypatch.setattr(ingress, "complete", _crash)
    crashed = worker.process_next("slack-worker-1")
    monkeypatch.undo()

    assert crashed is not None
    assert crashed.outcome is WorkerOutcome.FINALIZE_FAILED
    assert crashed.error_code is None
    assert crashed.finalize_error_code == "INGRESS_FINALIZE_FAILED"
    assert crashed.decision is not None
    assert not crashed.decision.replayed
    stranded = ingress.get(ack.command_id)
    assert stranded is not None
    assert stranded.state is IngressState.LEASED

    now[0] = NOW + timedelta(seconds=31)
    reclaimed = worker.process_next("slack-worker-2")

    assert reclaimed is not None
    assert reclaimed.outcome is WorkerOutcome.COMPLETED
    assert reclaimed.command_id == ack.command_id
    assert reclaimed.decision is not None
    assert reclaimed.decision.replayed
    assert reclaimed.decision.proposal_ref.proposal_id == PROPOSAL_ID


def test_expired_token_moves_the_command_to_recovery_hold(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, token_expires_at=NOW - timedelta(minutes=1))
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success

    result = _worker(store, ingress).process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD
    assert result.state is IngressState.RECOVERY_HOLD
    assert result.error_code == "ACTION_TOKEN_EXPIRED"
    assert result.decision is None


def test_terminal_feedback_runs_after_recovery_hold_is_durable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, token_expires_at=NOW - timedelta(minutes=1))
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    ack = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05)).submit(_envelope())
    feedback = _CapturedFeedback()

    result = _worker(store, ingress, feedback).process_next("slack-worker")

    assert result is not None
    assert result.state is IngressState.RECOVERY_HOLD
    assert feedback.calls == [(ack.command_id, SafeInteractionOutcome.EXPIRED)]


def test_feedback_failure_does_not_reopen_or_rollback_the_terminal_outcome(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed(store, token_expires_at=NOW - timedelta(minutes=1))
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    ack = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05)).submit(_envelope())
    feedback = _CapturedFeedback(fail=True)

    result = _worker(store, ingress, feedback).process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD
    assert result.feedback_error_code == "INTERACTION_FEEDBACK_FAILED"
    stored = ingress.get(str(ack.command_id))
    assert stored is not None
    assert stored.state is IngressState.RECOVERY_HOLD


def test_a_second_click_gets_already_completed_feedback_without_a_second_decision(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    assert _worker(store, ingress).process_next("slack-worker") is not None
    second_body = _body(_payload(actions=[_action(action_ts="1722387724.000500")]))
    second_ack = boundary.submit(_envelope(second_body))
    assert second_ack.success
    feedback = _CapturedFeedback()

    result = _worker(store, ingress, feedback).process_next("slack-worker")

    assert result is not None
    assert result.error_code == "ACTION_TOKEN_CONSUMED"
    assert feedback.calls == [(second_ack.command_id, SafeInteractionOutcome.ALREADY_COMPLETED)]
    with store.connect() as connection:
        decision_count = connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone()
    assert decision_count is not None
    assert int(decision_count[0]) == 1


def test_signed_action_from_a_disabled_actor_gets_denied_feedback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    ack = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05)).submit(_envelope())
    ActorBindingService(store, AUTHORITY_PROJECT, clock=lambda: NOW).disable_actor(
        USER,
        approval=BindingApproval(
            approval_id="APR-0000000000000004",
            approved_by=MANAGER,
            reason="disable actor for signed denial test",
        ),
    )
    feedback = _CapturedFeedback()

    result = _worker(store, ingress, feedback).process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD
    assert result.error_code == "ACTOR_DISABLED"
    assert feedback.calls == [(ack.command_id, SafeInteractionOutcome.DENIED)]


def test_signed_action_against_a_changed_snapshot_gets_stale_feedback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    ack = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05)).submit(_envelope())
    with store.connect() as connection, governance_transaction(connection):
        connection.execute(
            """
            UPDATE governance_active_proposals
            SET decision_epoch = decision_epoch + 1,
                state_revision = state_revision + 1,
                updated_at = ?
            WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
            """,
            (NOW.isoformat(), PROJECT.namespace, PROJECT.project_id, PROPOSAL_ID),
        )
    feedback = _CapturedFeedback()

    result = _worker(store, ingress, feedback).process_next("slack-worker")

    assert result is not None
    assert result.error_code == "PROPOSAL_STALE"
    assert feedback.calls == [(ack.command_id, SafeInteractionOutcome.STALE)]


def test_signed_revoked_action_gets_stale_feedback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    ack = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05)).submit(_envelope())
    with store.connect() as connection, governance_transaction(connection):
        DecisionService._revoke_token_ids_in_transaction(
            connection,
            (TOKEN_ID,),
            resolved_at=NOW,
        )
    feedback = _CapturedFeedback()

    result = _worker(store, ingress, feedback).process_next("slack-worker")

    assert result is not None
    assert result.error_code == "ACTION_TOKEN_REVOKED"
    assert feedback.calls == [(ack.command_id, SafeInteractionOutcome.STALE)]
    with store.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM governance_decision_results"
        ).fetchone() == (0,)


def test_worker_without_the_lease_reports_lease_loss(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    claim = ingress.claim_next("slack-worker-1")
    assert claim is not None

    result = _worker(store, ingress).process(claim, worker_id="slack-worker-2")

    assert result.outcome is WorkerOutcome.LEASE_LOST
    assert result.error_code == "INGRESS_LEASE_CONFLICT"
    held = ingress.get(claim.command_id)
    assert held is not None
    assert held.state is IngressState.LEASED
    assert held.lease_owner == "slack-worker-1"


def test_ack_response_and_worker_result_carry_no_raw_body_or_credential(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    body = _body()

    ack = boundary.submit(_envelope(body))
    result = _worker(store, ingress).process_next("slack-worker")

    assert result is not None
    serialized = ack.model_dump_json() + result.model_dump_json()
    assert RAW_TOKEN not in serialized
    assert body.decode() not in serialized
    assert SIGNING_SECRET not in serialized


@pytest.mark.parametrize(
    "envelope_factory",
    [
        lambda: _envelope(signature="v0=" + "0" * 64),
        lambda: _envelope(_body(_payload(api_app_id="A999"))),
        lambda: _envelope(b"payload=%7B"),
    ],
)
def test_rejected_responses_carry_no_raw_body_or_credential(
    tmp_path: Path,
    envelope_factory: Callable[[], ProviderEnvelope],
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))

    serialized = boundary.submit(envelope_factory()).model_dump_json()

    assert RAW_TOKEN not in serialized
    assert TOKEN_ID not in serialized
    assert SIGNING_SECRET not in serialized


def test_completed_decision_moves_the_proposal_and_consumes_the_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success

    result = _worker(store, ingress).process_next("slack-worker")

    assert result is not None
    assert result.decision is not None
    assert result.decision.proposal_status is ActiveProposalStatus.APPROVED
    with store.connect() as connection:
        state = connection.execute(
            "SELECT state FROM governance_action_tokens WHERE token_id = ?",
            (TOKEN_ID,),
        ).fetchone()
    assert state is not None
    assert str(state[0]) == "consumed"


def test_real_clock_measures_the_synchronous_path_without_injection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)

    response = BoundedIngressAck(ingress).submit(_envelope())

    assert response.success
    assert response.elapsed_ms >= 0
    assert response.elapsed_ms < AckBudget().total_ms


# A10 — retry, hold, lease and claim contracts


def test_transient_decision_failure_keeps_the_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RETRY
    assert result.state is IngressState.RETRY_WAIT
    assert result.error_code == "INGRESS_DECISION_UNAVAILABLE"
    assert result.command_id == ack.command_id
    held = ingress.get(str(ack.command_id))
    assert held is not None
    assert held.attempts == 1


def test_missing_project_permission_holds_instead_of_burning_retries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, grant_decide=False)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success

    result = _worker(store, ingress).process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD
    assert result.state is IngressState.RECOVERY_HOLD
    assert result.error_code in {"PROJECT_ACCESS_DENIED", "AUTHORITY_DENIED"}


def test_claim_failure_is_reported_instead_of_killing_the_worker_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress claim connection is busy")

    monkeypatch.setattr(ingress, "claim_next", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.CLAIM_FAILED
    assert result.error_code == "INGRESS_CLAIM_FAILED"
    assert result.command_id is None


def test_two_distinct_interactions_do_not_share_a_replay_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_bindings(store)
    _seed_proposal(store)
    _seed_proposal(
        store,
        proposal_id="PROP-20260730-BBBBBBBB",
        token_id="TOK-ABCDEF0123456789",
        raw_token="fedcba9876543210fedcba9876543210",
        channel=OTHER_CHANNEL,
    )
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    second_body = _body(
        _payload(
            container={
                "type": "message",
                "channel_id": OTHER_CHANNEL.channel_id,
                "message_ts": OTHER_CHANNEL.message_id,
            },
            actions=[
                _action(
                    token_id="TOK-ABCDEF0123456789",
                    raw_token="fedcba9876543210fedcba9876543210",
                    action_ts="1722387999.000900",
                )
            ],
        )
    )
    assert boundary.submit(_envelope(second_body)).success
    worker = _worker(store, ingress)

    first = worker.process_next("slack-worker")
    second = worker.process_next("slack-worker")

    assert first is not None
    assert second is not None
    assert first.outcome is WorkerOutcome.COMPLETED
    assert second.outcome is WorkerOutcome.COMPLETED
    assert first.decision is not None
    assert second.decision is not None
    assert not first.decision.replayed
    assert not second.decision.replayed
    assert first.decision.proposal_ref.proposal_id != second.decision.proposal_ref.proposal_id
    assert first.decision.token_id != second.decision.token_id


def test_same_channel_proposals_complete_without_a_projection_revision_conflict(
    tmp_path: Path,
) -> None:
    # Two proposals share one channel-wide Outbox sequence, but source revision
    # monotonicity is per Proposal. Equal revisions therefore complete independently.
    store = _store(tmp_path)
    _seed_bindings(store)
    _seed_proposal(store)
    _seed_proposal(
        store,
        proposal_id="PROP-20260730-CCCCCCCC",
        token_id="TOK-ABCDEF0123456789",
        raw_token="fedcba9876543210fedcba9876543210",
        state_revision=2,
    )
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    second_body = _body(
        _payload(
            actions=[
                _action(
                    token_id="TOK-ABCDEF0123456789",
                    raw_token="fedcba9876543210fedcba9876543210",
                    action_ts="1722387999.000900",
                )
            ]
        )
    )
    assert boundary.submit(_envelope(second_body)).success
    worker = _worker(store, ingress)

    results = [worker.process_next("slack-worker") for _ in range(2)]

    assert all(result is not None for result in results)
    for result in results:
        assert result is not None
        assert result.outcome is WorkerOutcome.COMPLETED
        assert result.error_code is None
        settled = ingress.get(str(result.command_id))
        assert settled is not None
        assert settled.state is IngressState.COMPLETED
    with store.connect() as connection:
        sequences = connection.execute(
            """
            SELECT destination_sequence FROM governance_outbox_events
            WHERE destination_ref LIKE 'provider:slack:%'
            ORDER BY destination_sequence
            """
        ).fetchall()
    assert sequences == [(1,), (2,)]


def test_stranded_commands_are_visible_to_an_operator(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, token_expires_at=NOW - timedelta(minutes=1))
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    assert not ingress.stranded()

    result = _worker(store, ingress).process_next("slack-worker")
    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD

    stranded = ingress.stranded()
    assert len(stranded) == 1
    assert stranded[0].command_id == ack.command_id
    assert stranded[0].state is IngressState.RECOVERY_HOLD
    assert stranded[0].last_error_code == "ACTION_TOKEN_EXPIRED"


def test_audit_integrity_failure_holds_instead_of_burning_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceEventError("AUDIT_HASH_CHAIN_INVALID")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD
    assert result.state is IngressState.RECOVERY_HOLD
    assert result.error_code == "AUDIT_HASH_CHAIN_INVALID"


def test_unclassified_failure_fails_closed_into_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise RuntimeError("unclassified worker failure")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RETRY
    assert result.state is IngressState.RETRY_WAIT
    assert result.error_code == "INGRESS_DECISION_FAILED"


def test_committed_decision_replays_after_the_actor_loses_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    now = [NOW]
    ingress = IngressService(store, _authenticator(), clock=lambda: now[0])
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    worker = _worker(store, ingress)

    def _crash(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("killed before the ingress command was finalized")

    monkeypatch.setattr(ingress, "complete", _crash)
    crashed = worker.process_next("slack-worker-1")
    monkeypatch.undo()
    assert crashed is not None
    assert crashed.outcome is WorkerOutcome.FINALIZE_FAILED

    with store.connect() as connection:
        connection.execute("DELETE FROM governance_actor_permissions")
    now[0] = NOW + timedelta(seconds=31)
    reclaimed = worker.process_next("slack-worker-2")

    assert reclaimed is not None
    assert reclaimed.outcome is WorkerOutcome.COMPLETED
    assert reclaimed.command_id == ack.command_id
    assert reclaimed.decision is not None
    assert reclaimed.decision.replayed
    assert reclaimed.decision.proposal_status is ActiveProposalStatus.APPROVED
    assert not ingress.stranded()


# T011 AC-01 / AC-04 — worker 가 마지막 시도를 관측하면 종결하고 reviewer 에게 알린다.
#
# **이 test 는 전에 반대를 요구했다.** 소진된 command 가 `retry_wait` 로 남았다가 sweep 이
# `dead_letter` 로 옮기는 것을 고정했는데, claim 조건이 `attempts < max_attempts` 라 그 row 는
# 다시 claim 되지 않고 feedback 도 영영 돌지 않았다. reviewer 는 HTTP 200 뒤 아무것도 못
# 받았다 (round 11 `R-3`). operator 가시성 요구는 그대로 두고 종점을 옮긴다.
def test_a_worker_observed_exhaustion_ends_as_a_recovery_hold_with_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    now = [NOW]
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=2),
        clock=lambda: now[0],
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    feedback = _CapturedFeedback()
    worker = _worker(store, ingress, feedback)

    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    for _ in range(2):
        assert worker.process_next("slack-worker") is not None
        now[0] = now[0] + timedelta(minutes=10)

    exhausted = ingress.get(str(ack.command_id))
    assert exhausted is not None
    assert exhausted.state is IngressState.RECOVERY_HOLD, "소진된 command 를 침묵으로 두지 않는다"

    # 예산이 남은 시도는 아무것도 알리지 않는다. 마지막 한 번만 알린다.
    assert feedback.calls == [(ack.command_id, SafeInteractionOutcome.UNAVAILABLE)]

    # operator 가시성은 그대로다. 이것이 옮기기 전 test 가 지키던 성질이다.
    stranded = ingress.stranded()
    assert len(stranded) == 1
    assert stranded[0].command_id == ack.command_id


# T011 AC-02 — 예산이 남은 재시도는 여전히 아무 outcome 도 알리지 않는다. 승격 조건이 한 칸
# 어긋나면 이 test 가 잡는다.
def test_a_retry_with_budget_left_announces_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=3),
        clock=lambda: NOW,
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    feedback = _CapturedFeedback()
    worker = _worker(store, ingress, feedback)

    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    assert worker.process_next("slack-worker") is not None

    still_open = ingress.get(str(ack.command_id))
    assert still_open is not None
    assert still_open.state is IngressState.RETRY_WAIT
    assert feedback.calls == []


# T011 AC-03 — 결과를 모르는 종점에는 아무것도 알리지 않는다.
#
# worker 가 claim 뒤 매번 죽으면 lease 만료로만 시도가 소진된다. 그 종점은 결과를 모른다 —
# decision 이 commit 됐는데 `complete` 전에 죽었으면 Result Card 는 이미 나갔다. 거기에
# `unavailable` 을 보내면 배달된 Card 와 모순된다. 침묵을 유지하고 operator 복구에 맡긴다.
def test_a_lease_expired_exhaustion_stays_silent_and_visible_to_the_operator(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    now = [NOW]
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=2, lease_duration=timedelta(seconds=30)),
        clock=lambda: now[0],
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    feedback = _CapturedFeedback()

    # worker 가 claim 만 하고 죽는 것을 흉내낸다. 아무것도 finalize 하지 않는다.
    for _ in range(2):
        assert ingress.claim_next("dying-worker") is not None
        now[0] = now[0] + timedelta(minutes=10)

    exhausted = ingress.get(str(ack.command_id))
    assert exhausted is not None
    assert exhausted.attempts == 2

    # sweep 이 돌면 dead letter 로 간다. worker 를 거치지 않으므로 승격 대상이 아니다.
    assert ingress.claim_next("next-worker") is None
    after_sweep = ingress.get(str(ack.command_id))
    assert after_sweep is not None
    assert after_sweep.state is IngressState.DEAD_LETTER

    assert feedback.calls == [], "결과를 모르는 종점에 결과를 선언하지 않는다"
    stranded = ingress.stranded()
    assert len(stranded) == 1
    assert stranded[0].command_id == ack.command_id
    assert stranded[0].state is IngressState.DEAD_LETTER


def test_claim_failure_keeps_the_underlying_ingress_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise IngressError("INGRESS_NOT_FOUND")

    monkeypatch.setattr(ingress, "claim_next", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.CLAIM_FAILED
    assert result.error_code == "INGRESS_NOT_FOUND"


def test_finalize_failure_preserves_the_denial_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store, token_expires_at=NOW - timedelta(minutes=1))
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    worker = _worker(store, ingress)

    def _crash(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("recovery hold write failed")

    monkeypatch.setattr(ingress, "recovery_hold", _crash)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.FINALIZE_FAILED
    assert result.error_code == "ACTION_TOKEN_EXPIRED"
    assert result.finalize_error_code == "INGRESS_FINALIZE_FAILED"
    assert result.decision is None


def _plant_result(
    store: GovernanceStore,
    *,
    idempotency_key: str,
    fingerprint: str,
    proposal_id: str = PROPOSAL_ID,
    action: str = "approve",
    proposal_status: str = "approved",
    token_id: str = TOKEN_ID,
) -> None:
    channel_json = json.dumps(
        CHANNEL.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_decision_results(
                idempotency_key, request_fingerprint, project_namespace, project_id,
                proposal_id, action, actor_id, actor_type, channel_json,
                proposal_status, active_definition_digest, content_revision,
                state_revision, decision_epoch, token_id, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'human', ?, ?, ?, 1, 3, 1, ?, ?)
            """,
            (
                idempotency_key,
                fingerprint,
                PROJECT.namespace,
                PROJECT.project_id,
                proposal_id,
                action,
                USER.actor_id,
                channel_json,
                proposal_status,
                f"sha256:{'1' * 64}",
                token_id,
                NOW.isoformat(),
            ),
        )


@pytest.mark.parametrize(
    "planted",
    [
        {"fingerprint": "f" * 64},
        {"proposal_id": "PROP-20260730-DDDDDDDD"},
        {"action": "reject", "proposal_status": "rejected"},
    ],
    ids=["fingerprint", "proposal", "action"],
)
def test_a_foreign_result_under_the_same_key_is_a_conflict(
    tmp_path: Path,
    planted: dict[str, str],
) -> None:
    store = _store(tmp_path)
    _seed_bindings(store)
    _seed_proposal(store)
    _seed_proposal(
        store,
        proposal_id="PROP-20260730-DDDDDDDD",
        token_id="TOK-ABCDEF0123456789",
        raw_token="fedcba9876543210fedcba9876543210",
        channel=OTHER_CHANNEL,
    )
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    command_id = str(ack.command_id)
    stored = ingress.get(command_id)
    assert stored is not None
    worker = _worker(store, ingress)
    _plant_result(
        store,
        idempotency_key=worker.idempotency_key(command_id),
        fingerprint=str(planted.get("fingerprint", stored.provider_fingerprint)),
        proposal_id=str(planted.get("proposal_id", PROPOSAL_ID)),
        action=str(planted.get("action", "approve")),
        proposal_status=str(planted.get("proposal_status", "approved")),
    )

    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD
    assert result.error_code == "IDEMPOTENCY_CONFLICT"
    assert result.decision is None


def test_a_command_with_retry_budget_left_is_not_stranded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=3),
        clock=lambda: NOW,
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    assert worker.process_next("slack-worker") is not None

    assert not ingress.stranded()


def test_an_expired_lease_on_the_last_attempt_is_stranded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    now = [NOW]
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=1),
        clock=lambda: now[0],
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    worker = _worker(store, ingress)

    def _crash(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("killed before the ingress command was finalized")

    monkeypatch.setattr(ingress, "complete", _crash)
    crashed = worker.process_next("slack-worker")
    monkeypatch.undo()
    assert crashed is not None
    assert crashed.outcome is WorkerOutcome.FINALIZE_FAILED

    now[0] = NOW + timedelta(seconds=31)
    stranded = ingress.stranded()

    assert len(stranded) == 1
    assert stranded[0].command_id == ack.command_id
    assert stranded[0].state is IngressState.LEASED
    committed = worker.committed_decision(str(ack.command_id))
    assert committed is not None
    assert committed.proposal_status is ActiveProposalStatus.APPROVED


def test_committed_decision_is_none_for_an_undecided_command(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())

    assert _worker(store, ingress).committed_decision(str(ack.command_id)) is None


def test_store_corruption_holds_instead_of_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        error = sqlite3.DatabaseError("database disk image is malformed")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        raise error

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD
    assert result.error_code == "INGRESS_STORE_CORRUPT"


def test_lease_loss_during_finalize_keeps_the_denial_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store, token_expires_at=NOW - timedelta(minutes=1))
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise IngressLeaseConflictError("INGRESS_LEASE_CONFLICT")

    monkeypatch.setattr(ingress, "recovery_hold", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.LEASE_LOST
    assert result.error_code == "ACTION_TOKEN_EXPIRED"
    assert result.finalize_error_code == "INGRESS_LEASE_CONFLICT"


def test_process_next_rejects_a_blank_worker_id_before_touching_ingress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    monkeypatch.setattr(ingress, "claim_next", lambda worker_id: None)

    with pytest.raises(ValueError, match="worker_id"):
        _worker(store, ingress).process_next("   ")


def test_a_live_lease_on_the_last_attempt_is_not_stranded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=1),
        clock=lambda: NOW,
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    worker = _worker(store, ingress)

    def _crash(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("killed before the ingress command was finalized")

    monkeypatch.setattr(ingress, "complete", _crash)
    assert worker.process_next("slack-worker") is not None

    held = ingress.get(str(ack.command_id))
    assert held is not None
    assert held.state is IngressState.LEASED
    assert held.attempts == 1
    assert held.lease_expires_at is not None
    assert held.lease_expires_at > NOW
    assert ingress.stranded() == ()


def test_a_transient_sqlite_error_retries_instead_of_holding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RETRY
    assert result.error_code == "INGRESS_DECISION_UNAVAILABLE"


def test_extended_corruption_codes_also_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        error = sqlite3.DatabaseError("database disk image is malformed")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT_INDEX
        raise error

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD
    assert result.error_code == "INGRESS_STORE_CORRUPT"


@pytest.mark.parametrize(
    "planted",
    [
        {"fingerprint": "f" * 64},
        {"token_id": "TOK-ABCDEF0123456789"},
        {"action": "reject", "proposal_status": "rejected"},
    ],
    ids=["fingerprint", "credential", "action"],
)
def test_committed_decision_ignores_a_foreign_result(
    tmp_path: Path,
    planted: dict[str, str],
) -> None:
    store = _store(tmp_path)
    _seed_bindings(store)
    _seed_proposal(store)
    _seed_proposal(
        store,
        proposal_id="PROP-20260730-DDDDDDDD",
        token_id="TOK-ABCDEF0123456789",
        raw_token="fedcba9876543210fedcba9876543210",
        channel=OTHER_CHANNEL,
    )
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    command_id = str(ack.command_id)
    command = ingress.get(command_id)
    assert command is not None
    worker = _worker(store, ingress)
    _plant_result(
        store,
        idempotency_key=worker.idempotency_key(command_id),
        fingerprint=str(planted.get("fingerprint", command.provider_fingerprint)),
        token_id=str(planted.get("token_id", TOKEN_ID)),
        action=str(planted.get("action", "approve")),
        proposal_status=str(planted.get("proposal_status", "approved")),
    )

    assert worker.committed_decision(command_id) is None


def test_committed_decision_is_none_when_the_command_row_is_gone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    worker = _worker(store, ingress)
    _plant_result(
        store,
        idempotency_key=worker.idempotency_key("CMD-DEADBEEFDEADBEEF"),
        fingerprint="f" * 64,
    )

    assert worker.committed_decision("CMD-DEADBEEFDEADBEEF") is None


def test_a_non_database_store_holds_instead_of_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        error = sqlite3.DatabaseError("file is not a database")
        error.sqlite_errorcode = sqlite3.SQLITE_NOTADB
        raise error

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    result = worker.process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.RECOVERY_HOLD
    assert result.error_code == "INGRESS_STORE_CORRUPT"


def test_result_for_rejects_a_blank_idempotency_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)

    with pytest.raises(ValueError, match="idempotency_key"):
        _worker(store, ingress).decisions.result_for("   ")


def test_stranded_rejects_a_non_positive_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)

    with pytest.raises(ValueError, match="limit"):
        ingress.stranded(limit=0)


def _observed_safe_outcomes() -> set[SafeInteractionOutcome]:
    """Collect what the classifier actually produces, by running it.

    **enum 을 걸러서 만들지 않는다.** 이전 판(`T007`)이 그렇게 해서 동어반복이었다 —
    `produced` 를 enum 자신에서 hardcoded 문자열 집합으로 만들고 다시 enum 과 비교했으므로
    producer 를 한 번도 보지 않았고, `ingress_worker.py` 의
    `return SafeInteractionOutcome.UNAVAILABLE` 을 지워도 통과했다 (round 11 `R-4`).

    여기서는 대표 입력을 분류기에 넣고 **나온 값**을 모은다. 어떤 값에 producer 가 없어지면
    그 값이 이 집합에서 빠지므로 아래 대조가 실패한다.

    `already_completed` 에는 producer 가 둘이다 — 여기서 쓰는 소비된 action 과, replay 된
    성공 decision. 후자는
    `test_a_second_click_gets_already_completed_feedback_without_a_second_decision` 이
    실제 두 번 클릭으로 덮는다. 여기서 재현하지 않는다.
    """
    denied_code = sorted(_DENIED_CODES)[0]
    inputs = [
        *(
            IngressWorkerResult(
                command_id="CMD-1",
                outcome=WorkerOutcome.RECOVERY_HOLD,
                error_code=code,
            )
            for code in (
                "ACTION_TOKEN_CONSUMED",
                "ACTION_TOKEN_EXPIRED",
                "PROPOSAL_STALE",
                "INVALID_PROPOSAL_STATE",
                "ACTION_TOKEN_REVOKED",
                denied_code,
                "INGRESS_DECISION_UNAVAILABLE",
            )
        ),
    ]
    observed = {IngressDecisionWorker._safe_outcome(result) for result in inputs}
    observed.discard(None)
    return cast("set[SafeInteractionOutcome]", observed)


# T012 AC-01 / AC-02 — 열거된 outcome 마다 실제 producer 가 있다.
#
# producer 를 하나 지우면 그 값이 관측 집합에서 빠져 이 대조가 실패한다. 이전 판은 지워도
# 통과했다. 도달 불가 값을 남기면 다음 사람이 이미 보내고 있다고 읽거나 producer 를 붙여
# 같은 사건을 두 번 알린다 (D-034).
def test_every_safe_outcome_value_has_a_real_producer() -> None:
    observed = _observed_safe_outcomes()

    assert observed == set(SafeInteractionOutcome)
    assert len(observed) == len(SafeInteractionOutcome)
    assert not hasattr(SafeInteractionOutcome, "COMPLETED")
    assert set(_SAFE_INTERACTION_MESSAGES) == set(SafeInteractionOutcome)


# T012 AC-03 — 계약이 열거한 집합과 코드가 만드는 집합의 **크기와 원소**를 둘 다 센다.
#
# `R-5` 가 4 대 5 불일치를 남기고 일치했다고 기록한 사례다. 크기를 세지 않은 대조는
# 대조가 아니다.
def test_the_contract_enumerates_exactly_the_outcomes_the_code_produces() -> None:
    contract = Path(__file__).resolve().parents[1] / (
        "specs/003-slack-proposal-card/contracts/interaction-feedback.md"
    )
    text = contract.read_text(encoding="utf-8")
    listed = set(re.findall(r"^\| `([a-z_]+)` \|", text, flags=re.MULTILINE))
    produced = {outcome.value for outcome in _observed_safe_outcomes()}

    assert listed == produced
    assert len(listed) == len(produced)


def test_a_completed_result_without_a_decision_sends_no_safe_feedback() -> None:
    """`decision` 이 없으면 replay 판정을 할 수 없으므로 아무것도 알리지 않는다.

    **round 13 `PBC-3` 는 이 test 를 "동어반복" 으로 보고 삭제를 지시했다. 그 판정은
    틀렸다.** 성분을 하나씩 지워 재면 이렇게 갈린다.

    | 지운 성분 | 죽는 test |
    |---|---|
    | `result.decision is not None` | **이 test 만** |
    | `result.decision.replayed` | `test_a_real_first_successful_decision_sends_no_feedback` 만 |

    둘은 같은 `if` 의 **다른 성분**을 잡는다. 이 test 를 지우면
    `result.decision is not None` 이 무방비가 되고, 그러면 `decision` 이 없는 완료 결과에서
    `None.replayed` 로 `AttributeError` 가 나 worker 가 죽는다.

    이름과 docstring 만 실제로 잡는 것에 맞게 고쳤다. 원래 이름
    `test_a_first_successful_decision_sends_no_safe_feedback` 은 replay 조건을 잡는 것처럼
    읽혔고 그것이 `PBC-3` 오판의 원인이다.
    """
    result = IngressWorkerResult(
        command_id="ICMD-1",
        outcome=WorkerOutcome.COMPLETED,
        decision=None,
        error_code=None,
    )

    assert IngressDecisionWorker._safe_outcome(result) is None


def test_a_recovery_hold_carrying_a_replayed_decision_is_not_called_already_completed(
    tmp_path: Path,
) -> None:
    """같은 `if` 의 세 번째 성분 — `outcome is COMPLETED` — 을 고정한다.

    round 13 이 이 성분을 세지 않았다. 지워도 108건이 전부 통과했다. 지우면 replay 된
    결정을 실은 recovery hold 가 `already_completed` 로 보고되어, 실패한 명령이 성공한 것처럼
    알려진다.

    "정의 사본 + 호출 지점 + **같은 검사의 모든 성분**" 규칙이 겨냥하는 자리다.
    """
    # 진짜 결정을 한 번 만들어 그 모양을 쓴다. 손으로 지어낸 `DecisionResult` 는 실제
    # 필드 조합과 어긋날 수 있고, 그러면 이 test 가 없는 상태를 검사하게 된다.
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    completed = _worker(store, ingress, _CapturedFeedback()).process_next("slack-worker")
    assert completed is not None
    assert completed.decision is not None

    replayed = completed.model_copy(
        update={
            "outcome": WorkerOutcome.RECOVERY_HOLD,
            "decision": completed.decision.model_copy(update={"replayed": True}),
            "error_code": "SOMETHING_UNMAPPED",
        }
    )

    assert IngressDecisionWorker._safe_outcome(replayed) is SafeInteractionOutcome.UNAVAILABLE


def test_a_retry_sends_no_safe_feedback() -> None:
    """retry 는 아직 끝나지 않은 명령이라 결과를 알리지 않는다 (D-034)."""
    result = IngressWorkerResult(
        command_id="ICMD-2",
        outcome=WorkerOutcome.RETRY,
        decision=None,
        error_code="INGRESS_DECISION_UNAVAILABLE",
    )

    assert IngressDecisionWorker._safe_outcome(result) is None


def test_a_recovery_hold_without_a_known_code_reports_unavailable() -> None:
    """hold 는 종결이므로 `unavailable` 로 알린다."""
    result = IngressWorkerResult(
        command_id="ICMD-3",
        outcome=WorkerOutcome.RECOVERY_HOLD,
        decision=None,
        error_code="SOMETHING_UNMAPPED",
    )

    assert IngressDecisionWorker._safe_outcome(result) is SafeInteractionOutcome.UNAVAILABLE


# ---------------------------------------------------------------------------
# round 12 F-1 을 닫은 wave (workstream wave 6) — 이미 결정된 명령의 소진은 침묵한다
#
# **이 wave 는 task manifest 가 없다** (round 13 `CT-3`). 원래 주석은 `MGC-012-P5-T015` 를
# 달았는데 그 ID 는 그때 발급된 적이 없고, 지금은 전혀 다른 task 의 ID 다. round 참조로
# 바꾼다. 존재하지 않는 manifest 를 가리키는 주석은 다음 읽는 사람을 없는 파일로 보낸다.
# ---------------------------------------------------------------------------


# F-1 AC-01 — 앞선 attempt 가 decision 을 commit 했으면 소진돼도 실패를 알리지 않는다.
#
# 시나리오: reviewer 가 승인을 누른다 → 1차 시도가 결정을 **기록하는 데 성공**하고 Result
# Card 를 대기열에 올리지만 완료 도장을 찍는 마지막 쓰기가 실패한다 → lease 가 만료되고
# 재claim 된다 → 마지막 시도가 실패한다.
#
# 승격이 `committed_decision()` 을 안 보면 reviewer 는 "승인됨" Card 와 "처리하지 못했습니다"
# 를 둘 다 받는다. `D-038` 항목 6 이 lease 만료 종점에서 거부한 바로 그 모순이다.
def test_an_exhausted_command_whose_decision_committed_announces_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    now = [NOW]
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=2, lease_duration=timedelta(seconds=30)),
        clock=lambda: now[0],
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    feedback = _CapturedFeedback()
    worker = _worker(store, ingress, feedback)

    # 1차 시도: 결정은 commit 되지만 완료 도장이 실패한다.
    def _finalize_fails(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress completion write is busy")

    monkeypatch.setattr(worker.ingress, "complete", _finalize_fails)
    first = worker.process_next("slack-worker")
    assert first is not None
    assert worker.committed_decision(str(ack.command_id)) is not None, (
        "전제: 결정이 실제로 기록됐다"
    )
    monkeypatch.undo()

    # 2차(마지막) 시도: 결정 경로가 실패한다.
    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    now[0] = now[0] + timedelta(minutes=10)
    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    second = worker.process_next("slack-worker")
    assert second is not None

    settled = ingress.get(str(ack.command_id))
    assert settled is not None
    assert settled.state is IngressState.RECOVERY_HOLD, "종결은 유지한다"
    assert settled.last_error_code == "INGRESS_DECISION_COMMITTED_UNRECONCILED"

    assert feedback.calls == [], "배달된 Result Card 와 모순되는 실패 통지를 보내지 않는다"
    assert worker.committed_decision(str(ack.command_id)) is not None

    # operator 가시성은 유지한다. 결정은 됐는데 장부가 안 맞으니 사람이 봐야 한다.
    stranded = ingress.stranded()
    assert [view.command_id for view in stranded] == [ack.command_id]


# F-1 AC-02 — 결정이 기록되지 않은 소진은 여전히 unavailable 을 알린다. 두 종점을 가른다.
def test_an_exhausted_command_without_a_decision_still_announces_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    now = [NOW]
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=2),
        clock=lambda: now[0],
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    feedback = _CapturedFeedback()
    worker = _worker(store, ingress, feedback)

    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    for _ in range(2):
        assert worker.process_next("slack-worker") is not None
        now[0] = now[0] + timedelta(minutes=10)

    assert worker.committed_decision(str(ack.command_id)) is None
    assert feedback.calls == [(ack.command_id, SafeInteractionOutcome.UNAVAILABLE)]


# F-1 AC-03 — 침묵 code 는 recovery hold 에서만 침묵시킨다. 다른 종결을 삼키지 않는다.
def test_the_silent_hold_code_does_not_swallow_other_outcomes() -> None:
    silent = IngressWorkerResult(
        command_id="CMD-1",
        outcome=WorkerOutcome.RECOVERY_HOLD,
        error_code="INGRESS_DECISION_COMMITTED_UNRECONCILED",
    )
    assert IngressDecisionWorker._safe_outcome(silent) is None

    noisy = IngressWorkerResult(
        command_id="CMD-1",
        outcome=WorkerOutcome.RECOVERY_HOLD,
        error_code="INGRESS_DECISION_UNAVAILABLE",
    )
    assert IngressDecisionWorker._safe_outcome(noisy) is SafeInteractionOutcome.UNAVAILABLE


# RL-1 — 첫 성공 결정은 통지를 만들지 않는다. **실제 클릭으로** 고정한다.
#
# 기존 `test_a_first_successful_decision_sends_no_safe_feedback` 은 `decision=None` 을 넣어
# `RECOVERY_HOLD` 관문에서 먼저 걸리므로 `result.decision.replayed` 조건에 닿지 않는다.
# round 12 regression lens 가 그 조건을 지워도 1245건이 통과하는 것을 보였다 (`RL-1`).
#
# 그 조건이 없으면 첫 성공 결정이 `already_completed` 를 보내고, reviewer 는 "승인됨" Card 와
# "이미 처리됨" 을 둘 다 받는다. `D-034` 가 막으려던 이중 통지다.
def test_a_real_first_successful_decision_sends_no_feedback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    assert boundary.submit(_envelope()).success
    feedback = _CapturedFeedback()

    result = _worker(store, ingress, feedback).process_next("slack-worker")

    assert result is not None
    assert result.outcome is WorkerOutcome.COMPLETED
    # 이 두 줄이 전제다 — 진짜 decision 이 있고, replay 가 아니다. 그래야 이 test 가
    # `and result.decision.replayed` 조건을 실제로 지나간다.
    assert result.decision is not None
    assert result.decision.replayed is False

    assert feedback.calls == [], "성공 통지는 Result Card 가 한다 (D-034, FR-024)"


# ---------------------------------------------------------------------------
# MGC-012-P5-T016 — 결정 장부를 읽지 못해도 worker loop 를 죽이지 않는다
# (round 13 P1-1, D-042)
# ---------------------------------------------------------------------------
#
# `_settle_exhausted_retry` 는 승격 전에 `committed_decision()` 을 읽는다. 그 호출이
# `_finalize` 의 `try` **밖**에 있었고, 안에서 connection 두 개를 새로 연다. `_finalize` 의
# 다른 store 호출은 전부 감싸져 있다 — **이 읽기 하나만 무방비였다.**
#
# **실패 모드가 상관돼 있다.** 재시도를 소진시킨 그 조건(store busy)이 이 읽기도 실패시킨다.
# 드물어서 넘길 수 있는 종류가 아니다. round 13 이 실측한 결과다.
#
#     attempt2 RAISED OUT OF process_next: OperationalError database is locked
#     state after: leased  attempts: 2  last_error: INGRESS_DECISION_UNAVAILABLE
#     feedback calls: []
#     reclaim attempt: None
#
# `D-042` 가 방향을 정했다. 읽기가 실패하면 **결정이 기록됐을 수 있다는 보수적 가정**으로
# 침묵한다. 모르는 상태에서 `unavailable` 을 보내면 이미 배달된 Result Card 와 모순될 수
# 있고 그 통지는 되돌릴 수 없다. 침묵은 `stranded()` 로 회수할 수 있다.


def _exhausted_worker_with_unreadable_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> tuple[IngressService, _CapturedFeedback, str]:
    """마지막 attempt 에서 결정 경로와 결정 장부가 **함께** 실패하는 worker 를 만든다."""
    store = _store(tmp_path)
    _seed(store)
    now = [NOW]
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=2),
        clock=lambda: now[0],
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    feedback = _CapturedFeedback()
    worker = _worker(store, ingress, feedback)

    def _decision_busy(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _decision_busy)

    # 첫 시도는 예산을 쓴다. 장부는 아직 읽을 수 있다.
    assert worker.process_next("slack-worker") is not None
    now[0] = now[0] + timedelta(minutes=10)

    # 마지막 시도에서 장부까지 못 읽는다. 같은 store 가 busy 하니 같이 실패한다.
    def _ledger_busy(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(worker, "committed_decision", _ledger_busy)
    result = worker.process_next("slack-worker")
    assert result is not None, "예외가 process_next 밖으로 나가면 여기 닿지 못한다"
    return ingress, feedback, str(ack.command_id)


# T016 AC-01/AC-02/AC-03/AC-04 — 장부를 못 읽어도 종결하고 침묵한다.
@pytest.mark.parametrize(
    "failure",
    [
        GovernanceStoreError("ingress decision connection is busy"),
        sqlite3.OperationalError("database is locked"),
    ],
    ids=["governance-store-error", "sqlite-operational-error"],
)
def test_an_unreadable_decision_ledger_settles_the_command_instead_of_escaping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    ingress, feedback, command_id = _exhausted_worker_with_unreadable_ledger(
        tmp_path, monkeypatch, failure
    )

    settled = ingress.get(command_id)
    assert settled is not None
    # AC-02 — `leased` 로 남으면 claim 조건이 `attempts < max_attempts` 라 다시 잡히지 않는다.
    assert settled.state is IngressState.RECOVERY_HOLD, (
        "leased 로 남으면 그 command 는 영영 다시 claim 되지 않는다"
    )
    assert settled.last_error_code == "INGRESS_DECISION_COMMITTED_UNRECONCILED"

    # AC-03 — 모르는 상태에서 통지하지 않는다. 되돌릴 수 없는 모순을 만들 수 있다.
    assert feedback.calls == [], "결정 기록 여부를 모르면 아무것도 알리지 않는다"

    # AC-04 — operator 가 회수할 수 있다. 침묵의 대가를 이것이 갚는다.
    stranded = ingress.stranded()
    assert [entry.command_id for entry in stranded] == [command_id]


# T016 AC-06 — 승격된 recovery hold 가 **원인 code 를 durable 에 남긴다** (round 13 `F-3`).
#
# `_settle_exhausted_retry` 의 마지막 `return ..., error_code` 를 `..., None` 으로 바꿔도
# 전 suite 가 통과했다. `_transition` 이 `code = error_code or "INGRESS_DECISION_FAILED"` 를
# 쓰므로 durable `last_error_code` 가 조용히 generic 이 되고, `stranded()` 를 보는 operator
# 에게서 진짜 원인이 사라진다.
#
# **feedback 으로는 이것을 잡을 수 없다.** `_safe_outcome` 은 mapping 되지 않은 code 와
# `None` 을 똑같이 `unavailable` 로 처리하므로 통지는 그대로다. durable 값을 직접 봐야 한다.
# round 12 evidence 의 "`R-2` 의 주 경로 — 원인 code 가 살아남는다" 를 round 13 이 반증했다.
def test_an_observed_exhaustion_keeps_the_cause_code_in_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _seed(store)
    now = [NOW]
    ingress = IngressService(
        store,
        _authenticator(),
        config=IngressConfig(max_attempts=2),
        clock=lambda: now[0],
    )
    boundary = BoundedIngressAck(ingress, monotonic=_monotonic(0.0, 0.05))
    ack = boundary.submit(_envelope())
    worker = _worker(store, ingress, _CapturedFeedback())

    def _decision_busy(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _decision_busy)
    for _ in range(2):
        assert worker.process_next("slack-worker") is not None
        now[0] = now[0] + timedelta(minutes=10)

    settled = ingress.get(str(ack.command_id))
    assert settled is not None
    assert settled.state is IngressState.RECOVERY_HOLD
    assert settled.last_error_code == "INGRESS_DECISION_UNAVAILABLE", (
        "승격이 원인 code 를 버리면 _transition 의 or 절이 generic 으로 덮어써서 "
        "stranded() 를 보는 operator 가 왜 멈췄는지 알 수 없다"
    )

    # operator 가 보는 목록에도 그 code 가 그대로 있다.
    stranded = ingress.stranded()
    assert [entry.last_error_code for entry in stranded] == ["INGRESS_DECISION_UNAVAILABLE"]
