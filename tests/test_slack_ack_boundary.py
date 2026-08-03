from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    IngressConfig,
    IngressDecisionWorker,
    IngressError,
    IngressLeaseConflictError,
    IngressService,
    IngressState,
    ProviderEnvelope,
    SlackBlockActionAuthenticator,
    SlackInstallationPolicy,
    VerifiedProviderCommand,
    WorkerOutcome,
)
from amplai_foundry.governance.events import GovernanceEventError
from amplai_foundry.governance.store import (
    GovernanceCommitAmbiguousError,
    GovernanceStore,
    GovernanceStoreError,
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
    action_ts: str = "1722387723.000400",
) -> dict[str, object]:
    return {
        "type": "button",
        "action_id": "approve",
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
                      'approve', ?, 'human', ?, ?, ?, 'issued', NULL)
            """,
            (
                token_id,
                credential_hash,
                PROJECT.namespace,
                PROJECT.project_id,
                proposal_id,
                definition_digest,
                state_revision,
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


def _worker(store: GovernanceStore, ingress: IngressService) -> IngressDecisionWorker:
    authority = AuthorityService(store, clock=lambda: NOW)
    return IngressDecisionWorker(
        store,
        ingress,
        DecisionService(store, authority, clock=lambda: NOW),
    )


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


def test_projection_conflict_does_not_strand_a_leased_command(tmp_path: Path) -> None:
    # Two proposals in one channel share an Outbox destination, so the second decision
    # can raise GovernanceEventError. That must not escape the worker.
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
    outcomes = {result.outcome for result in results if result is not None}
    assert WorkerOutcome.COMPLETED in outcomes
    assert WorkerOutcome.RETRY in outcomes
    conflicted = next(
        result for result in results if result is not None and result.outcome is WorkerOutcome.RETRY
    )
    assert conflicted.error_code == "OUTBOX_SOURCE_REVISION_CONFLICT"
    for result in results:
        assert result is not None
        settled = ingress.get(str(result.command_id))
        assert settled is not None
        assert settled.state is not IngressState.LEASED


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


def test_retry_exhausted_command_is_stranded_before_the_sweep(
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
    worker = _worker(store, ingress)

    def _raise(*args: object, **kwargs: object) -> object:
        raise GovernanceStoreError("ingress decision connection is busy")

    monkeypatch.setattr(worker.decisions, "decide_ingress_in_transaction", _raise)
    for _ in range(2):
        assert worker.process_next("slack-worker") is not None
        now[0] = now[0] + timedelta(minutes=10)

    exhausted = ingress.get(str(ack.command_id))
    assert exhausted is not None
    assert exhausted.state is IngressState.RETRY_WAIT
    assert exhausted.attempts == 2
    before_sweep = ingress.stranded()
    assert len(before_sweep) == 1
    assert before_sweep[0].command_id == ack.command_id

    assert worker.process_next("slack-worker") is None
    after_sweep = ingress.stranded()

    assert len(after_sweep) == 1
    assert after_sweep[0].command_id == ack.command_id
    assert after_sweep[0].state is IngressState.DEAD_LETTER


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
