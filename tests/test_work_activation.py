from __future__ import annotations

import hashlib
import hmac
import io
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest
from pydantic import SecretStr

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.decisions import DecisionAction
from amplai_foundry.governance.ingress import ProviderEnvelope
from amplai_foundry.governance.models import (
    ActorRef,
    ActorType,
    AuthorityContext,
    AuthorityPermission,
    AuthoritySource,
    ChannelProvider,
    ChannelRef,
)
from amplai_foundry.governance.slack import (
    SlackBlockActionAuthenticator,
    SlackInstallationPolicy,
)
from amplai_foundry.governance.slack_projection import (
    SlackHistoryMessage,
    SlackHistoryPage,
    SlackSendResult,
)
from amplai_foundry.governance.slack_work_activation import (
    SlackWorkActivationAuthenticator,
    SlackWorkActivationIngress,
)
from amplai_foundry.governance.work_activation import (
    DurableWorkActivationLedger,
    WorkActivationAction,
    WorkActivationActionType,
    WorkActivationError,
    WorkActivationResult,
    WorkActivationScope,
    WorkActivationService,
    WorkActivationSnapshot,
)
from amplai_foundry.governance.work_activation_card_outbox import (
    WorkActivationCardOutbox,
)
from amplai_foundry.governance.work_activation_http import (
    SlackWorkActivationWSGIApp,
    render_work_activation_card,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)
CHANNEL = ChannelRef(
    provider=ChannelProvider.SLACK, workspace_id="T1", channel_id="C1", message_id="1.0"
)


class Gateway:
    def __init__(self) -> None:
        self.work = WorkActivationSnapshot("CR-1-W001", "DRAFT", 1, "sha256:" + "a" * 64)

    def get(self, work_id: str) -> WorkActivationSnapshot:
        assert work_id == self.work.work_id
        return self.work

    def activate(
        self,
        work_id: str,
        *,
        actor_id: str,
        expected_revision: int,
        expected_digest: str,
    ) -> WorkActivationSnapshot:
        assert work_id == self.work.work_id and actor_id.startswith("ACT-")
        if (self.work.revision, self.work.digest) != (expected_revision, expected_digest):
            raise WorkActivationError("WORK_STALE")
        self.work = WorkActivationSnapshot(work_id, "READY", 2, "sha256:" + "b" * 64)
        return self.work


class Tokens:
    def __init__(self) -> None:
        self.calls = 0

    def consume(self, raw_token: str, **kwargs: object) -> None:
        assert raw_token == "opaque-token"
        self.calls += 1


def authority(
    *, activation: bool = True, actor_type: ActorType = ActorType.HUMAN
) -> AuthorityContext:
    permissions = {AuthorityPermission.PROPOSAL_READ}
    if activation:
        permissions.add(AuthorityPermission.ACTIVATION_MANAGE)
    return AuthorityContext(
        actor_ref=ActorRef(actor_id="ACT-1", actor_type=actor_type),
        project_ref=ProjectRef(namespace="org/default/project/a", project_id="a"),
        permissions=frozenset(permissions),
        source=AuthoritySource(request_id="CMD-1", channel=CHANNEL),
        authenticated_at=NOW,
    )


def action(**changes: object) -> WorkActivationAction:
    values: dict[str, object] = {
        "project_ref": ProjectRef(namespace="org/default/project/a", project_id="a"),
        "feature": "hermes-slack-orchestration",
        "work_id": "CR-1-W001",
        "expected_revision": 1,
        "expected_digest": "sha256:" + "a" * 64,
        "action": "approve",
        "channel_ref": CHANNEL,
        "action_token": "opaque-token",
        "idempotency_key": "slack:C1:click-1",
        "occurred_at": NOW,
    }
    values.update(changes)
    return WorkActivationAction.model_validate(values)


def service() -> tuple[WorkActivationService, Gateway, Tokens]:
    gateway, tokens = Gateway(), Tokens()
    service = WorkActivationService(
        gateway,
        tokens,
        enabled_providers=frozenset({ChannelProvider.SLACK}),
        enabled_scopes=(
            WorkActivationScope(
                ProjectRef(namespace="org/default/project/a", project_id="a"),
                ChannelProvider.SLACK,
                "hermes-slack-orchestration",
            ),
        ),
    )
    return service, gateway, tokens


def test_approved_human_action_activates_once_and_replays() -> None:
    target, gateway, tokens = service()
    first = target.apply(action(), authority())
    replay = target.apply(action(), authority())
    assert first == WorkActivationResult("CR-1-W001", "READY")
    assert replay.replayed and gateway.work.state == "READY" and tokens.calls == 1


@pytest.mark.parametrize(
    "bad_authority", [authority(activation=False), authority(actor_type=ActorType.AGENT)]
)
def test_missing_human_activation_authority_preserves_draft(
    bad_authority: AuthorityContext,
) -> None:
    target, gateway, tokens = service()
    with pytest.raises(WorkActivationError, match="AUTHORITY_DENIED"):
        target.apply(action(), bad_authority)
    assert gateway.work.state == "DRAFT" and tokens.calls == 0


def test_disabled_provider_and_stale_card_preserve_draft() -> None:
    target, gateway, tokens = service()
    target.enabled_providers = frozenset()
    with pytest.raises(WorkActivationError, match="ACTIVATION_PROVIDER_DISABLED"):
        target.apply(action(), authority())
    target.enabled_providers = frozenset({ChannelProvider.SLACK})
    with pytest.raises(WorkActivationError, match="WORK_STALE"):
        target.apply(action(expected_revision=2), authority())
    assert gateway.work.state == "DRAFT" and tokens.calls == 0


def test_durable_denial_writes_a_secret_free_audit_row(tmp_path: Path) -> None:
    ledger = DurableWorkActivationLedger(tmp_path / "activation.sqlite", clock=lambda: NOW)
    target = WorkActivationService(
        Gateway(),
        ledger,
        enabled_providers=frozenset(),
        ledger=ledger,
    )
    with pytest.raises(WorkActivationError, match="ACTIVATION_PROVIDER_DISABLED"):
        target.apply(action(), authority())
    with sqlite3.connect(ledger.path) as connection:
        row = connection.execute(
            "SELECT outcome_code, actor_id, channel_json FROM work_activation_audit"
        ).fetchone()
    assert row is not None and row[0] == "ACTIVATION_PROVIDER_DISABLED"
    assert "opaque-token" not in " ".join(str(value) for value in row)


def test_durable_hash_only_token_survives_service_restart(tmp_path: Path) -> None:
    ledger = DurableWorkActivationLedger(tmp_path / "activation.sqlite", clock=lambda: NOW)
    gateway = Gateway()
    scope = WorkActivationScope(
        ProjectRef(namespace="org/default/project/a", project_id="a"),
        ChannelProvider.SLACK,
        "hermes-slack-orchestration",
    )
    issued = ledger.issue_tokens(
        snapshot=gateway.work,
        scope=scope,
        authority=authority(),
    )
    approve = next(token for token in issued if token.record.action.value == "approve")
    card = render_work_activation_card(work_id=gateway.work.work_id, tokens=issued)
    assert {element["action_id"] for element in card["blocks"][1]["elements"]} == {
        "amplai_work_approve",
        "amplai_work_reject",
        "amplai_work_request_changes",
    }
    durable_action = ledger.action_for(
        token_id=approve.record.token_id,
        raw_token=approve.raw_token,
        action=approve.record.action,
        idempotency_key="slack:C1:durable-1",
        occurred_at=NOW,
    )
    target = WorkActivationService(
        gateway,
        ledger,
        enabled_providers=frozenset({ChannelProvider.SLACK}),
        enabled_scopes=(scope,),
        ledger=ledger,
    )
    assert target.apply(durable_action, authority()).state == "READY"
    restarted = WorkActivationService(
        gateway,
        ledger,
        enabled_providers=frozenset({ChannelProvider.SLACK}),
        enabled_scopes=(scope,),
        ledger=ledger,
    )
    assert restarted.apply(durable_action, authority()).replayed
    with sqlite3.connect(ledger.path) as connection:
        rows = connection.execute("SELECT * FROM work_activation_tokens")
        stored = " ".join(str(value) for row in rows for value in row)
    assert approve.raw_token not in stored


def test_durable_activation_card_outbox_issues_then_delivers_without_storing_raw_tokens(
    tmp_path: Path,
) -> None:
    ledger = DurableWorkActivationLedger(tmp_path / "activation.sqlite", clock=lambda: NOW)
    gateway = Gateway()
    scope = WorkActivationScope(
        ProjectRef(namespace="org/default/project/a", project_id="a"),
        ChannelProvider.SLACK,
        "hermes-slack-orchestration",
    )
    outbox = WorkActivationCardOutbox(tmp_path / "activation-card-outbox.sqlite")

    class Transport:
        def __init__(self) -> None:
            self.messages: list[SlackHistoryMessage] = []
            self.payloads: list[object] = []
            self.updated_payloads: list[object] = []

        def post_message(self, *, channel: str, payload: object, marker: object) -> SlackSendResult:
            self.payloads.append(payload)
            self.messages.append(
                SlackHistoryMessage(ts="1722387600.000200", metadata=marker, app_id="A1")
            )
            return SlackSendResult(channel=channel, ts="1722387600.000200")

        def read_history(self, *, channel: str, cursor: str | None, limit: int) -> SlackHistoryPage:
            del channel, cursor, limit
            return SlackHistoryPage(messages=tuple(self.messages))

        def update_card(self, *, channel: str, ts: str, payload: object) -> None:
            del channel, ts
            self.updated_payloads.append(payload)

    intent = outbox.enqueue(snapshot=gateway.work, scope=scope, authority=authority())
    transport = Transport()
    delivered = outbox.deliver_next(
        gateway=gateway, ledger=ledger, transport=transport, app_id="A1"
    )
    assert delivered is not None and delivered.idempotency_key == intent.idempotency_key
    assert outbox.pending_count() == 0
    assert len(transport.payloads) == 1
    with sqlite3.connect(outbox.path) as connection:
        stored = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM work_activation_card_outbox")
            for value in row
        )
    rendered = str(transport.updated_payloads[0])
    assert "TOK-" in rendered and "TOK-" not in stored
    approve_value = str(transport.updated_payloads[0]["blocks"][1]["elements"][0]["value"])
    token_id, raw_token = approve_value.split(".", 1)
    recovered = ledger.action_for(
        token_id=token_id,
        raw_token=raw_token,
        action=WorkActivationActionType.APPROVE,
        idempotency_key="slack:C1:activation-card",
        occurred_at=NOW,
    )
    assert recovered.channel_ref.message_id == "1722387600.000200"


def test_activation_card_outbox_recovers_a_lost_receipt_without_second_post(tmp_path: Path) -> None:
    ledger = DurableWorkActivationLedger(tmp_path / "activation.sqlite", clock=lambda: NOW)
    gateway = Gateway()
    scope = WorkActivationScope(
        ProjectRef(namespace="org/default/project/a", project_id="a"),
        ChannelProvider.SLACK,
        "hermes-slack-orchestration",
    )
    outbox = WorkActivationCardOutbox(tmp_path / "activation-card-outbox.sqlite")

    class CrashAfterPost:
        def __init__(self) -> None:
            self.messages: list[SlackHistoryMessage] = []
            self.posts = 0
            self.crash = True

        def post_message(self, *, channel: str, payload: object, marker: object) -> SlackSendResult:
            del payload
            self.posts += 1
            self.messages.append(
                SlackHistoryMessage(ts="1722387600.000201", metadata=marker, app_id="A1")
            )
            if self.crash:
                self.crash = False
                raise RuntimeError("receipt persistence interrupted")
            return SlackSendResult(channel=channel, ts="1722387600.000201")

        def read_history(self, *, channel: str, cursor: str | None, limit: int) -> SlackHistoryPage:
            del channel, cursor, limit
            return SlackHistoryPage(messages=tuple(self.messages))

        def update_card(self, *, channel: str, ts: str, payload: object) -> None:
            del channel, ts, payload

    outbox.enqueue(snapshot=gateway.work, scope=scope, authority=authority())
    transport = CrashAfterPost()
    with pytest.raises(RuntimeError, match="receipt persistence interrupted"):
        outbox.deliver_next(gateway=gateway, ledger=ledger, transport=transport, app_id="A1")
    assert outbox.pending_count() == 1
    assert outbox.deliver_next(gateway=gateway, ledger=ledger, transport=transport, app_id="A1")
    assert transport.posts == 1 and outbox.pending_count() == 0


def test_stale_activation_card_is_terminal_and_allows_a_newer_intent(tmp_path: Path) -> None:
    ledger = DurableWorkActivationLedger(tmp_path / "activation.sqlite", clock=lambda: NOW)
    gateway = Gateway()
    scope = WorkActivationScope(
        ProjectRef(namespace="org/default/project/a", project_id="a"),
        ChannelProvider.SLACK,
        "hermes-slack-orchestration",
    )
    outbox = WorkActivationCardOutbox(tmp_path / "activation-card-outbox.sqlite")

    class Transport:
        def post_message(self, *, channel: str, payload: object, marker: object) -> SlackSendResult:
            del payload, marker
            return SlackSendResult(channel=channel, ts="1722387600.000202")

        def read_history(self, *, channel: str, cursor: str | None, limit: int) -> SlackHistoryPage:
            del channel, cursor, limit
            return SlackHistoryPage(messages=())

        def update_card(self, *, channel: str, ts: str, payload: object) -> None:
            del channel, ts, payload

    outbox.enqueue(snapshot=gateway.work, scope=scope, authority=authority())
    gateway.work = WorkActivationSnapshot("CR-1-W001", "DRAFT", 2, "sha256:" + "b" * 64)
    with pytest.raises(WorkActivationError, match="WORK_STALE"):
        outbox.deliver_next(gateway=gateway, ledger=ledger, transport=Transport(), app_id="A1")
    outbox.enqueue(snapshot=gateway.work, scope=scope, authority=authority())
    assert outbox.deliver_next(gateway=gateway, ledger=ledger, transport=Transport(), app_id="A1")
    with sqlite3.connect(outbox.path) as connection:
        rows = connection.execute(
            "SELECT delivery_state, superseded_at FROM work_activation_card_outbox "
            "ORDER BY expected_revision"
        ).fetchall()
    assert rows[0][0] == "delivered" and rows[0][1] is not None


def test_activation_card_readback_fails_closed_for_our_malformed_marker(tmp_path: Path) -> None:
    gateway = Gateway()
    scope = WorkActivationScope(
        ProjectRef(namespace="org/default/project/a", project_id="a"),
        ChannelProvider.SLACK,
        "hermes-slack-orchestration",
    )
    outbox = WorkActivationCardOutbox(tmp_path / "activation-card-outbox.sqlite")
    intent = outbox.enqueue(snapshot=gateway.work, scope=scope, authority=authority())

    class Transport:
        def read_history(self, *, channel: str, cursor: str | None, limit: int) -> SlackHistoryPage:
            del channel, cursor, limit
            return SlackHistoryPage(
                messages=(
                    SlackHistoryMessage(
                        ts="1722387600.000203",
                        metadata={"event_type": "amplai.work_activation_card"},
                        app_id="A1",
                    ),
                )
            )

    with pytest.raises(RuntimeError, match="ACTIVATION_CARD_METADATA_UNREADABLE"):
        outbox._find_remote(Transport(), intent, app_id="A1")


def test_durable_intent_recovers_after_gateway_crash(tmp_path: Path) -> None:
    class CrashingGateway(Gateway):
        def __init__(self) -> None:
            super().__init__()
            self.crash = True

        def activate(
            self,
            work_id: str,
            *,
            actor_id: str,
            expected_revision: int,
            expected_digest: str,
        ) -> WorkActivationSnapshot:
            if self.crash:
                self.crash = False
                raise RuntimeError("crash-after-intent")
            return super().activate(
                work_id,
                actor_id=actor_id,
                expected_revision=expected_revision,
                expected_digest=expected_digest,
            )

    ledger = DurableWorkActivationLedger(tmp_path / "activation.sqlite", clock=lambda: NOW)
    gateway = CrashingGateway()
    scope = WorkActivationScope(
        ProjectRef(namespace="org/default/project/a", project_id="a"),
        ChannelProvider.SLACK,
        "hermes-slack-orchestration",
    )
    issued = ledger.issue_tokens(snapshot=gateway.work, scope=scope, authority=authority())
    token = next(item for item in issued if item.record.action.value == "approve")
    durable_action = ledger.action_for(
        token_id=token.record.token_id,
        raw_token=token.raw_token,
        action=token.record.action,
        idempotency_key="slack:C1:crash-1",
        occurred_at=NOW,
    )
    service = WorkActivationService(
        gateway,
        ledger,
        enabled_providers=frozenset({ChannelProvider.SLACK}),
        enabled_scopes=(scope,),
        ledger=ledger,
    )
    with pytest.raises(RuntimeError, match="crash-after-intent"):
        service.apply(durable_action, authority())
    assert service.apply(durable_action, authority()).state == "READY"


def test_signed_slack_work_click_reaches_only_its_bound_human(tmp_path: Path) -> None:
    signing_secret = "fixture-secret"
    timestamp = str(int(NOW.timestamp()))
    channel = ChannelRef(
        provider=ChannelProvider.SLACK,
        workspace_id="T123",
        channel_id="C789",
        message_id="1722387600.000200",
    )
    signed_authority = AuthorityContext(
        actor_ref=ActorRef(actor_id="ACT-U456", actor_type=ActorType.HUMAN),
        project_ref=ProjectRef(namespace="org/default/project/a", project_id="a"),
        permissions=frozenset({AuthorityPermission.ACTIVATION_MANAGE}),
        source=AuthoritySource(request_id="CMD-1", channel=channel),
        authenticated_at=NOW,
    )
    gateway = Gateway()
    ledger = DurableWorkActivationLedger(tmp_path / "activation.sqlite", clock=lambda: NOW)
    scope = WorkActivationScope(
        signed_authority.project_ref, ChannelProvider.SLACK, "hermes-slack-orchestration"
    )
    issued = ledger.issue_tokens(snapshot=gateway.work, scope=scope, authority=signed_authority)
    approve = next(token for token in issued if token.record.action.value == "approve")
    payload = {
        "type": "block_actions",
        "trigger_id": "123.456.fixture",
        "team": {"id": "T123"},
        "enterprise": None,
        "user": {"id": "U456", "team_id": "T123"},
        "api_app_id": "A123",
        "container": {
            "type": "message",
            "channel_id": "C789",
            "message_ts": channel.message_id,
        },
        "actions": [
            {
                "type": "button",
                "action_id": "amplai_work_approve",
                "block_id": "work-actions",
                "action_ts": "1722387723.000400",
                "value": f"{approve.record.token_id}.{approve.raw_token}",
            }
        ],
    }
    body = urlencode({"payload": json.dumps(payload, separators=(",", ":"))}).encode("utf-8")
    signature = (
        "v0="
        + hmac.new(
            signing_secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
    )
    authenticator = SlackWorkActivationAuthenticator(
        SlackBlockActionAuthenticator(
            SlackInstallationPolicy(
                provider_installation_ref="T123:A123",
                signing_secret=SecretStr(signing_secret),
                api_app_id="A123",
                workspace_ids=frozenset({"T123"}),
                action_ids={
                    "amplai_work_approve": DecisionAction.APPROVE,
                    "amplai_work_reject": DecisionAction.REJECT,
                    "amplai_work_request_changes": DecisionAction.REQUEST_CHANGES,
                },
            ),
            clock=lambda: NOW,
        )
    )
    service = WorkActivationService(
        gateway,
        ledger,
        enabled_providers=frozenset({ChannelProvider.SLACK}),
        enabled_scopes=(scope,),
        ledger=ledger,
    )

    class Resolver:
        def resolve(self, **kwargs: object) -> AuthorityContext:
            assert kwargs["external_actor_id"] == "U456"
            return signed_authority

    result = SlackWorkActivationIngress(authenticator, ledger, service, Resolver()).apply(
        ProviderEnvelope(
            provider=ChannelProvider.SLACK,
            provider_installation_ref="T123:A123",
            raw_body=body,
            headers={
                "X-Slack-Request-Timestamp": timestamp,
                "X-Slack-Signature": signature,
            },
        ),
    )
    assert result.state == "READY"


def test_work_activation_wsgi_route_accepts_only_its_narrow_path() -> None:
    class Ingress:
        def __init__(self) -> None:
            self.envelope = None

        def apply(self, envelope):
            self.envelope = envelope
            return WorkActivationResult("CR-1-W001", "READY")

    ingress = Ingress()
    app = SlackWorkActivationWSGIApp(ingress, provider_installation_ref="T123:A123")
    started: list[tuple[str, list[tuple[str, str]]]] = []
    body = b"payload=%7B%7D"
    response = app(
        {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/slack/work-activation",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_X_SLACK_REQUEST_TIMESTAMP": "1",
            "HTTP_X_SLACK_SIGNATURE": "v0=" + "0" * 64,
        },
        lambda status, headers: started.append((status, headers)),
    )
    assert started[0][0] == "200 OK"
    assert json.loads(b"".join(response)) == {
        "work_id": "CR-1-W001",
        "state": "READY",
        "replayed": False,
    }
    assert ingress.envelope.provider_installation_ref == "T123:A123"
