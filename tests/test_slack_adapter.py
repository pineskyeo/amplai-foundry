from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from pydantic import SecretStr

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
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
    IngressAuthorityRequest,
    IngressError,
    IngressService,
    ProviderEnvelope,
    SlackBlockActionAuthenticator,
    SlackInstallationPolicy,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction

NOW = datetime(2026, 7, 31, 1, 2, 3, tzinfo=UTC)
TIMESTAMP = str(int(NOW.timestamp()))
SIGNING_SECRET = "slack-signing-secret-fixture"
INSTALLATION_REF = "T123:A123"
RAW_TOKEN = "0123456789abcdef0123456789abcdef"
TOKEN_ID = "TOK-0123456789ABCDEF"
PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
AUTHORITY_PROJECT = ProjectRef(
    project_id="governance",
    namespace="org/default/project/governance",
)
MANAGER = ActorRef(actor_id="ACT-MANAGER-1", actor_type=ActorType.HUMAN)
USER = ActorRef(actor_id="ACT-USER-1", actor_type=ActorType.HUMAN)


def _payload(**changes: object) -> dict[str, object]:
    # Shape follows Slack's block_actions message example and required-field table:
    # https://docs.slack.dev/reference/interaction-payloads/block_actions-payload/
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


def _authenticator(
    *,
    workspaces: frozenset[str] = frozenset({"T123"}),
    enterprises: frozenset[str] = frozenset(),
) -> SlackBlockActionAuthenticator:
    return SlackBlockActionAuthenticator(
        SlackInstallationPolicy(
            provider_installation_ref=INSTALLATION_REF,
            signing_secret=SecretStr(SIGNING_SECRET),
            api_app_id="A123",
            workspace_ids=workspaces,
            enterprise_ids=enterprises,
        ),
        clock=lambda: NOW,
    )


def test_verified_raw_block_action_normalizes_without_interaction_payload_id() -> None:
    body = _body()
    command = _authenticator().verify(_envelope(body))

    expected_fingerprint = hashlib.sha256(
        json.dumps(
            (
                INSTALLATION_REF,
                "U456",
                "message:C789:1722387600.000200",
                "approve",
                "1722387723.000400",
                f"sha256:{hashlib.sha256(body).hexdigest()}",
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert command.external_event_id == f"SLK-{expected_fingerprint.upper()}"
    assert command.external_actor_key == "U456"
    assert command.channel_ref.workspace_id == "T123"
    assert command.channel_ref.channel_id == "C789"
    assert command.channel_ref.message_id == "1722387600.000200"
    assert command.credential_id == TOKEN_ID
    assert command.raw_credential.get_secret_value() == RAW_TOKEN
    assert command.action is DecisionAction.APPROVE
    assert RAW_TOKEN not in repr(command)


def test_signature_is_bound_to_exact_raw_body_and_compared_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body()
    calls: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def record_compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(hmac, "compare_digest", record_compare)
    assert _authenticator().verify(_envelope(body)).action is DecisionAction.APPROVE
    assert len(calls) == 1

    changed = body + b"%20"
    with pytest.raises(IngressError, match="SLACK_SIGNATURE_INVALID"):
        _authenticator().verify(_envelope(changed, signature=_signature(body)))


@pytest.mark.parametrize(
    ("timestamp", "signature", "code"),
    (
        ("not-an-int", "v0=" + "0" * 64, "SLACK_TIMESTAMP_INVALID"),
        ("9" * 100, "v0=" + "0" * 64, "SLACK_TIMESTAMP_INVALID"),
        (
            str(int((NOW - timedelta(minutes=6)).timestamp())),
            None,
            "SLACK_TIMESTAMP_STALE",
        ),
        (TIMESTAMP, "v1=" + "0" * 64, "SLACK_SIGNATURE_INVALID"),
    ),
)
def test_timestamp_and_signature_fail_closed(
    timestamp: str,
    signature: str | None,
    code: str,
) -> None:
    body = _body()
    effective_signature = signature or _signature(body, timestamp)
    with pytest.raises(IngressError, match=code):
        _authenticator().verify(_envelope(body, timestamp=timestamp, signature=effective_signature))


def test_missing_or_duplicate_case_insensitive_headers_fail_closed() -> None:
    body = _body()
    missing = ProviderEnvelope(
        provider=ChannelProvider.SLACK,
        provider_installation_ref=INSTALLATION_REF,
        raw_body=body,
        headers={},
    )
    with pytest.raises(IngressError, match="SLACK_SIGNATURE_MISSING"):
        _authenticator().verify(missing)

    duplicate = ProviderEnvelope(
        provider=ChannelProvider.SLACK,
        provider_installation_ref=INSTALLATION_REF,
        raw_body=body,
        headers={
            "X-Slack-Request-Timestamp": TIMESTAMP,
            "x-slack-request-timestamp": TIMESTAMP,
            "X-Slack-Signature": _signature(body),
        },
    )
    with pytest.raises(IngressError, match="SLACK_HEADER_INVALID"):
        _authenticator().verify(duplicate)


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"api_app_id": "A-DENIED"}, "SLACK_APP_DENIED"),
        ({"team": {"id": "T-DENIED"}}, "SLACK_INSTALLATION_DENIED"),
        ({"enterprise": {"id": "E-DENIED"}}, "SLACK_INSTALLATION_DENIED"),
        ({"type": "view_submission"}, "SLACK_PAYLOAD_UNSUPPORTED"),
        ({"actions": []}, "SLACK_PAYLOAD_INVALID"),
        ({"user": {"id": "", "team_id": "T123"}}, "SLACK_PAYLOAD_INVALID"),
        ({"user": {"team_id": "T123"}}, "SLACK_PAYLOAD_INVALID"),
        ({"user": "U456"}, "SLACK_PAYLOAD_INVALID"),
        ({"trigger_id": ""}, "SLACK_PAYLOAD_INVALID"),
        (
            {
                "actions": [
                    {
                        "type": "button",
                        "action_id": "approve",
                        "action_ts": "1722387723.000400",
                        "value": f"{TOKEN_ID}.{RAW_TOKEN}",
                    }
                ]
            },
            "SLACK_PAYLOAD_INVALID",
        ),
        (
            {"container": {"type": "view", "view_id": "V123"}},
            "SLACK_PAYLOAD_UNSUPPORTED",
        ),
    ),
)
def test_app_installation_and_payload_contract_fail_closed(
    changes: dict[str, object],
    code: str,
) -> None:
    body = _body(_payload(**changes))
    with pytest.raises(IngressError, match=code):
        _authenticator().verify(_envelope(body))


def test_org_installation_uses_user_workspace_and_enterprise_allowlist() -> None:
    body = _body(
        _payload(
            team=None,
            enterprise={"id": "E123"},
            user={"id": "U456", "team_id": "T123"},
        )
    )
    command = _authenticator(enterprises=frozenset({"E123"})).verify(_envelope(body))
    assert command.channel_ref.workspace_id == "T123"


@pytest.mark.parametrize(
    "actions",
    (
        [
            {
                "type": "button",
                "action_id": "unknown",
                "block_id": "proposal-actions",
                "action_ts": "1.1",
                "value": "x",
            }
        ],
        [
            {
                "type": "static_select",
                "action_id": "approve",
                "block_id": "proposal-actions",
                "action_ts": "1.1",
                "value": f"{TOKEN_ID}.{RAW_TOKEN}",
            }
        ],
        [
            {
                "type": "button",
                "action_id": "approve",
                "block_id": "proposal-actions",
                "action_ts": "1.1",
                "value": "proposal=AMPLAI&action=approve",
            }
        ],
    ),
)
def test_action_and_opaque_credential_contract_rejects_unsupported_input(
    actions: list[dict[str, str]],
) -> None:
    body = _body(_payload(actions=actions))
    with pytest.raises(
        IngressError, match=r"SLACK_(ACTION_UNSUPPORTED|PAYLOAD_UNSUPPORTED|CREDENTIAL_INVALID)"
    ):
        _authenticator().verify(_envelope(body))


@pytest.mark.parametrize(
    "body",
    (
        b"not-form-data",
        b"payload=",
        b"payload=%7Bbad-json",
        b"payload=%7B%22type%22%3A%22block_actions%22%2C%22type%22%3A%22block_actions%22%7D",
        b"payload=%7B%7D&payload=%7B%7D",
        b"payload=%7B%22value%22%3ANaN%7D",
        b"\xff",
    ),
)
def test_malformed_form_json_and_duplicate_fields_fail_closed(body: bytes) -> None:
    with pytest.raises(IngressError, match="SLACK_PAYLOAD_INVALID"):
        _authenticator().verify(_envelope(body))


def test_percent_encoded_invalid_utf8_actor_fails_an_otherwise_valid_payload() -> None:
    body = _body()
    invalid_actor_body = body.replace(b"U456", b"%FF", 1)
    assert invalid_actor_body != body

    with pytest.raises(IngressError, match="SLACK_PAYLOAD_INVALID"):
        _authenticator().verify(_envelope(invalid_actor_body))


def test_non_slack_provider_and_wrong_installation_are_denied() -> None:
    envelope = _envelope()
    with pytest.raises(IngressError, match="SLACK_INSTALLATION_DENIED"):
        _authenticator().verify(_envelope(installation_ref="slack:A123:T-DENIED"))
    with pytest.raises(IngressError, match="SLACK_PROVIDER_MISMATCH"):
        _authenticator().verify(
            ProviderEnvelope(
                provider=ChannelProvider.TELEGRAM,
                provider_installation_ref=envelope.provider_installation_ref,
                raw_body=envelope.raw_body,
                headers=envelope.headers,
            )
        )


def test_verified_slack_command_flows_through_ingress_and_worker_authority(
    tmp_path: Path,
) -> None:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
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
    channel = ChannelRef(
        provider=ChannelProvider.SLACK,
        workspace_id="T123",
        channel_id="C789",
        message_id="1722387600.000200",
    )
    channel_json = json.dumps(
        channel.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    definition_digest = f"sha256:{'1' * 64}"
    credential_hash = f"sha256:{hashlib.sha256(RAW_TOKEN.encode()).hexdigest()}"
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO governance_active_proposals(
                project_namespace, project_id, proposal_id, active_definition_digest,
                content_revision, state_revision, decision_epoch, status,
                created_at, updated_at
            ) VALUES (?, ?, 'PROP-20260730-ABCDEF12', ?, 1, 2, 1, 'reviewed', ?, ?)
            """,
            (
                PROJECT.namespace,
                PROJECT.project_id,
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
            ) VALUES (?, ?, ?, ?, 'PROP-20260730-ABCDEF12', ?, 1, 2, 1,
                      'approve', ?, 'human', ?, ?, ?, 'issued', NULL)
            """,
            (
                TOKEN_ID,
                credential_hash,
                PROJECT.namespace,
                PROJECT.project_id,
                definition_digest,
                USER.actor_id,
                channel_json,
                NOW.isoformat(),
                (NOW + timedelta(minutes=15)).isoformat(),
            ),
        )

    body = _body()
    ingress = IngressService(store, _authenticator(), clock=lambda: NOW)
    ack = ingress.accept(_envelope(body))
    assert ack.accepted
    claim = ingress.claim_next("slack-worker")
    assert claim is not None
    with store.connect() as connection, governance_transaction(connection):
        authority = AuthorityService(store, clock=lambda: NOW).authenticate(
            IngressAuthorityRequest(
                command_id=claim.command_id,
                worker_id="slack-worker",
                generation=claim.claim_generation,
            ),
            connection=connection,
        )

    assert authority.actor_ref == USER
    assert authority.project_ref == PROJECT
    assert authority.source.channel.provider is ChannelProvider.SLACK
    assert authority.source.channel == channel
    durable_bytes = b"".join(
        path.read_bytes()
        for path in store.path.parent.glob(f"{store.path.name}*")
        if path.is_file()
    )
    assert body not in durable_bytes
    assert RAW_TOKEN.encode() not in durable_bytes


def test_installation_policy_copies_mutable_action_mapping_and_redacts_secret() -> None:
    actions = {"approve": DecisionAction.APPROVE}
    policy = SlackInstallationPolicy(
        provider_installation_ref=INSTALLATION_REF,
        signing_secret=SecretStr(SIGNING_SECRET),
        api_app_id="A123",
        workspace_ids=frozenset({"T123"}),
        action_ids=actions,
    )
    actions.clear()

    assert policy.action_ids == {"approve": DecisionAction.APPROVE}
    assert SIGNING_SECRET not in repr(policy)


@pytest.mark.parametrize(
    ("installation_ref", "workspaces"),
    (
        ("slack:A123:T123", frozenset({"T123"})),
        ("T999:A123", frozenset({"T123"})),
        ("T123:A123", frozenset({"T123", "T999"})),
    ),
)
def test_installation_policy_rejects_noncanonical_or_multi_workspace_identity(
    installation_ref: str,
    workspaces: frozenset[str],
) -> None:
    with pytest.raises(ValueError):
        SlackInstallationPolicy(
            provider_installation_ref=installation_ref,
            signing_secret=SecretStr(SIGNING_SECRET),
            api_app_id="A123",
            workspace_ids=workspaces,
        )


def test_authenticator_enforces_raw_body_and_json_resource_budgets() -> None:
    body = _body()
    limited = SlackBlockActionAuthenticator(
        SlackInstallationPolicy(
            provider_installation_ref=INSTALLATION_REF,
            signing_secret=SecretStr(SIGNING_SECRET),
            api_app_id="A123",
            workspace_ids=frozenset({"T123"}),
            max_raw_body_bytes=len(body) - 1,
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(IngressError, match="SLACK_PAYLOAD_TOO_LARGE"):
        limited.verify(_envelope(body))

    nested_json = '{"bomb":' + "[" * 1_100 + "0" + "]" * 1_100 + "}"
    nested_body = urlencode({"payload": nested_json}).encode()
    with pytest.raises(IngressError, match="SLACK_PAYLOAD_INVALID"):
        _authenticator().verify(_envelope(nested_body))

    nested: object = 0
    for _ in range(40):
        nested = [nested]
    depth_body = _body(_payload(extra_depth=nested))
    with pytest.raises(IngressError, match="SLACK_PAYLOAD_INVALID"):
        _authenticator().verify(_envelope(depth_body))

    node_payload = _payload(extra_nodes=list(range(2_100)))
    node_body = _body(node_payload)
    with pytest.raises(IngressError, match="SLACK_PAYLOAD_INVALID"):
        _authenticator().verify(_envelope(node_body))

    string_limited = SlackBlockActionAuthenticator(
        SlackInstallationPolicy(
            provider_installation_ref=INSTALLATION_REF,
            signing_secret=SecretStr(SIGNING_SECRET),
            api_app_id="A123",
            workspace_ids=frozenset({"T123"}),
            max_json_string_bytes=64,
        ),
        clock=lambda: NOW,
    )
    string_body = _body(_payload(extra_string="한" * 22))
    with pytest.raises(IngressError, match="SLACK_PAYLOAD_INVALID"):
        string_limited.verify(_envelope(string_body))


def test_slack_official_signing_vector_matches_documented_signature() -> None:
    # Vector from https://docs.slack.dev/authentication/verifying-requests-from-slack/
    secret = "8f742231b10e8888abcd99yyyzzz85a5"
    timestamp = "1531420618"
    body = (
        b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow"
        b"&channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA"
        b"&user_name=roadrunner&command=%2Fwebhook-collect&text="
        b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F"
        b"397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
        b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
    )
    authenticator = SlackBlockActionAuthenticator(
        SlackInstallationPolicy(
            provider_installation_ref=INSTALLATION_REF,
            signing_secret=SecretStr(secret),
            api_app_id="A123",
            workspace_ids=frozenset({"T123"}),
        ),
        clock=lambda: datetime.fromtimestamp(int(timestamp), tz=UTC),
    )

    verified_timestamp = authenticator._verify_signature(
        {
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": (
                "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
            ),
        },
        body,
    )
    assert verified_timestamp == int(timestamp)
