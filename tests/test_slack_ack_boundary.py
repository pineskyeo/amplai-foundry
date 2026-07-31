from __future__ import annotations

import hashlib
import hmac
import json
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
    IngressService,
    IngressState,
    ProviderEnvelope,
    SlackBlockActionAuthenticator,
    SlackInstallationPolicy,
    VerifiedProviderCommand,
    WorkerOutcome,
)
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
        "actions": [
            {
                "type": "button",
                "action_id": "approve",
                "block_id": "proposal-actions",
                "action_ts": "1722387723.000400",
                "value": f"{TOKEN_ID}.{RAW_TOKEN}",
            }
        ],
    }
    payload.update(changes)
    return payload


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


def _seed(store: GovernanceStore, *, token_expires_at: datetime | None = None) -> None:
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
    channel_json = json.dumps(
        CHANNEL.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    definition_digest = f"sha256:{'1' * 64}"
    credential_hash = f"sha256:{hashlib.sha256(RAW_TOKEN.encode()).hexdigest()}"
    expires_at = token_expires_at or (NOW + timedelta(minutes=15))
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id, active_definition_digest,
                content_revision, state_revision, decision_epoch, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 2, 1, 'reviewed', ?, ?)
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                definition_digest,
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
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 2, 1,
                      'approve', ?, 'human', ?, ?, ?, 'issued', NULL)
            """,
            (
                TOKEN_ID,
                credential_hash,
                PROJECT.namespace,
                PROJECT.project_id,
                PROPOSAL_ID,
                definition_digest,
                USER.actor_id,
                channel_json,
                NOW.isoformat(),
                expires_at.isoformat(),
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


def test_budget_exceeded_command_converges_when_slack_retries(tmp_path: Path) -> None:
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
    with pytest.raises(GovernanceStoreError):
        worker.process_next("slack-worker-1")
    monkeypatch.undo()

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
