from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from pydantic import SecretStr

from amplai_foundry.governance import (
    ChannelProvider,
    DecisionAction,
    IngressError,
    ProviderEnvelope,
    SlackBlockActionAuthenticator,
    SlackInstallationPolicy,
)

NOW = datetime(2026, 7, 31, 1, 2, 3, tzinfo=UTC)
TIMESTAMP = str(int(NOW.timestamp()))
SIGNING_SECRET = "slack-signing-secret-fixture"
INSTALLATION_REF = "slack:A123:T123"
RAW_TOKEN = "0123456789abcdef0123456789abcdef"
TOKEN_ID = "TOK-0123456789ABCDEF"


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "block_actions",
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
        [{"type": "button", "action_id": "unknown", "action_ts": "1.1", "value": "x"}],
        [
            {
                "type": "static_select",
                "action_id": "approve",
                "action_ts": "1.1",
                "value": f"{TOKEN_ID}.{RAW_TOKEN}",
            }
        ],
        [
            {
                "type": "button",
                "action_id": "approve",
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
