"""Fail-closed Slack Block Actions authentication and normalization."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from urllib.parse import parse_qsl

from pydantic import SecretStr

from amplai_foundry.governance.decisions import DecisionAction
from amplai_foundry.governance.ingress import (
    IngressError,
    ProviderEnvelope,
    VerifiedProviderCommand,
)
from amplai_foundry.governance.models import ChannelProvider, ChannelRef

_SIGNATURE_PATTERN = re.compile(r"^v0=[0-9a-f]{64}$")
_TOKEN_ID_PATTERN = re.compile(r"^TOK-[A-F0-9]{16}$")
_DEFAULT_ACTIONS = MappingProxyType(
    {
        "approve": DecisionAction.APPROVE,
        "reject": DecisionAction.REJECT,
        "request_changes": DecisionAction.REQUEST_CHANGES,
    }
)


def _system_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SlackInstallationPolicy:
    """One Slack app installation's authentication and allowlist policy."""

    provider_installation_ref: str
    signing_secret: SecretStr = field(repr=False)
    api_app_id: str
    workspace_ids: frozenset[str]
    enterprise_ids: frozenset[str] = frozenset()
    max_clock_skew: timedelta = timedelta(minutes=5)
    max_raw_body_bytes: int = 1_048_576
    max_json_depth: int = 32
    max_json_nodes: int = 2_048
    max_json_string_bytes: int = 65_536
    action_ids: Mapping[str, DecisionAction] = field(
        default_factory=lambda: _DEFAULT_ACTIONS,
        repr=False,
    )

    def __post_init__(self) -> None:
        workspace_ids = frozenset(self.workspace_ids)
        enterprise_ids = frozenset(self.enterprise_ids)
        action_ids = MappingProxyType(dict(self.action_ids))
        if (
            not self.provider_installation_ref.strip()
            or not self.signing_secret.get_secret_value()
            or not self.api_app_id.strip()
            or not workspace_ids
            or any(not value.strip() for value in (*workspace_ids, *enterprise_ids))
        ):
            raise ValueError("Slack installation policy 필수 값이 비어 있습니다.")
        if self.max_clock_skew <= timedelta(0):
            raise ValueError("Slack max_clock_skew는 0보다 커야 합니다.")
        if (
            not 1 <= self.max_raw_body_bytes <= 1_048_576
            or self.max_json_depth < 1
            or self.max_json_nodes < 1
            or self.max_json_string_bytes < 1
        ):
            raise ValueError("Slack parser budget이 올바르지 않습니다.")
        if not action_ids:
            raise ValueError("Slack action ID mapping이 필요합니다.")
        if len(workspace_ids) != 1:
            raise ValueError("Slack installation policy는 하나의 workspace에 결합해야 합니다.")
        workspace_id = next(iter(workspace_ids))
        expected_installation_ref = f"{workspace_id}:{self.api_app_id}"
        if self.provider_installation_ref != expected_installation_ref:
            raise ValueError("Slack installation은 workspace_id:api_app_id 형식이어야 합니다.")
        object.__setattr__(self, "workspace_ids", workspace_ids)
        object.__setattr__(self, "enterprise_ids", enterprise_ids)
        object.__setattr__(self, "action_ids", action_ids)


class SlackBlockActionAuthenticator:
    """Verify a Slack request from raw bytes, then normalize one message action."""

    def __init__(
        self,
        policy: SlackInstallationPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self._clock = clock or _system_now

    def verify(self, envelope: ProviderEnvelope) -> VerifiedProviderCommand:
        if envelope.provider is not ChannelProvider.SLACK:
            raise IngressError("SLACK_PROVIDER_MISMATCH")
        if envelope.provider_installation_ref != self.policy.provider_installation_ref:
            raise IngressError("SLACK_INSTALLATION_DENIED")
        if len(envelope.raw_body) > self.policy.max_raw_body_bytes:
            raise IngressError("SLACK_PAYLOAD_TOO_LARGE")
        headers = self._headers(envelope.headers)
        self._verify_signature(headers, envelope.raw_body)
        payload = self._payload(envelope.raw_body)
        return self._command(payload, envelope.raw_body)

    def _verify_signature(self, headers: Mapping[str, str], raw_body: bytes) -> int:
        timestamp_value = headers.get("x-slack-request-timestamp")
        signature = headers.get("x-slack-signature")
        if timestamp_value is None or signature is None:
            raise IngressError("SLACK_SIGNATURE_MISSING")
        if (
            not timestamp_value.isascii()
            or not timestamp_value.isdecimal()
            or not 1 <= len(timestamp_value) <= 12
        ):
            raise IngressError("SLACK_TIMESTAMP_INVALID")
        timestamp = int(timestamp_value)
        now = self._aware(self._clock())
        if abs(now.timestamp() - timestamp) > self.policy.max_clock_skew.total_seconds():
            raise IngressError("SLACK_TIMESTAMP_STALE")
        if _SIGNATURE_PATTERN.fullmatch(signature) is None:
            raise IngressError("SLACK_SIGNATURE_INVALID")
        basestring = b"v0:" + timestamp_value.encode("ascii") + b":" + raw_body
        expected = (
            "v0="
            + hmac.new(
                self.policy.signing_secret.get_secret_value().encode("utf-8"),
                basestring,
                hashlib.sha256,
            ).hexdigest()
        )
        if not hmac.compare_digest(expected, signature):
            raise IngressError("SLACK_SIGNATURE_INVALID")
        return timestamp

    def _command(
        self,
        payload: Mapping[str, object],
        raw_body: bytes,
    ) -> VerifiedProviderCommand:
        if payload.get("type") != "block_actions":
            raise IngressError("SLACK_PAYLOAD_UNSUPPORTED")
        self._text(payload.get("trigger_id"))
        api_app_id = self._text(payload.get("api_app_id"))
        if api_app_id != self.policy.api_app_id:
            raise IngressError("SLACK_APP_DENIED")
        user = self._mapping(payload.get("user"))
        team = self._optional_mapping(payload.get("team"))
        enterprise = self._optional_mapping(payload.get("enterprise"))
        actor_id = self._text(user.get("id"))
        workspace_id = self._text(team.get("id") if team is not None else user.get("team_id"))
        if team is None and enterprise is None:
            raise IngressError("SLACK_INSTALLATION_DENIED")
        if workspace_id not in self.policy.workspace_ids:
            raise IngressError("SLACK_INSTALLATION_DENIED")
        if enterprise is not None:
            enterprise_id = self._text(enterprise.get("id"))
            if enterprise_id not in self.policy.enterprise_ids:
                raise IngressError("SLACK_INSTALLATION_DENIED")

        actions = payload.get("actions")
        if not isinstance(actions, list) or len(actions) != 1:
            raise IngressError("SLACK_PAYLOAD_INVALID")
        action = self._mapping(actions[0])
        if action.get("type") != "button":
            raise IngressError("SLACK_PAYLOAD_UNSUPPORTED")
        action_id = self._text(action.get("action_id"))
        self._text(action.get("block_id"))
        decision = self.policy.action_ids.get(action_id)
        if decision is None:
            raise IngressError("SLACK_ACTION_UNSUPPORTED")
        action_ts = self._slack_timestamp(action.get("action_ts"))
        credential_id, raw_credential = self._credential(action.get("value"))

        container = self._mapping(payload.get("container"))
        if container.get("type") != "message":
            raise IngressError("SLACK_PAYLOAD_UNSUPPORTED")
        channel_id = self._text(container.get("channel_id"))
        message_ts = self._slack_timestamp(container.get("message_ts"))
        container_ref = f"message:{channel_id}:{message_ts}"
        raw_body_digest = f"sha256:{hashlib.sha256(raw_body).hexdigest()}"
        fingerprint = self._fingerprint(
            self.policy.provider_installation_ref,
            actor_id,
            container_ref,
            action_id,
            action_ts,
            raw_body_digest,
        )
        return VerifiedProviderCommand(
            external_event_id=f"SLK-{fingerprint.upper()}",
            external_actor_key=actor_id,
            channel_ref=ChannelRef(
                provider=ChannelProvider.SLACK,
                workspace_id=workspace_id,
                channel_id=channel_id,
                message_id=message_ts,
            ),
            credential_id=credential_id,
            raw_credential=SecretStr(raw_credential),
            action=decision,
        )

    @staticmethod
    def _headers(headers: Mapping[str, str]) -> Mapping[str, str]:
        normalized: dict[str, str] = {}
        for name, value in headers.items():
            key = name.strip().lower()
            if not key or key in normalized or "\r" in value or "\n" in value:
                raise IngressError("SLACK_HEADER_INVALID")
            normalized[key] = value.strip()
        return MappingProxyType(normalized)

    def _payload(self, raw_body: bytes) -> Mapping[str, object]:
        try:
            text = raw_body.decode("utf-8", errors="strict")
            fields = parse_qsl(
                text,
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
                max_num_fields=2,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise IngressError("SLACK_PAYLOAD_INVALID") from error
        if len(fields) != 1 or fields[0][0] != "payload" or not fields[0][1]:
            raise IngressError("SLACK_PAYLOAD_INVALID")
        try:
            payload = json.loads(
                fields[0][1],
                object_pairs_hook=self._unique_object,
                parse_constant=self._reject_json_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError) as error:
            raise IngressError("SLACK_PAYLOAD_INVALID") from error
        if not isinstance(payload, dict):
            raise IngressError("SLACK_PAYLOAD_INVALID")
        self._validate_json_budget(payload)
        return payload

    @staticmethod
    def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate Slack JSON key")
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(value: str) -> object:
        raise ValueError(f"non-finite Slack JSON value: {value}")

    def _validate_json_budget(self, payload: object) -> None:
        nodes = 0
        stack: list[tuple[object, int]] = [(payload, 1)]
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if nodes > self.policy.max_json_nodes or depth > self.policy.max_json_depth:
                raise IngressError("SLACK_PAYLOAD_INVALID")
            if isinstance(value, dict):
                stack.extend((key, depth + 1) for key in value)
                stack.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
            elif isinstance(value, str) and (
                len(value.encode("utf-8")) > self.policy.max_json_string_bytes
            ):
                raise IngressError("SLACK_PAYLOAD_INVALID")

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        if not isinstance(value, dict):
            raise IngressError("SLACK_PAYLOAD_INVALID")
        return value

    @classmethod
    def _optional_mapping(cls, value: object) -> Mapping[str, object] | None:
        return None if value is None else cls._mapping(value)

    @staticmethod
    def _text(value: object) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise IngressError("SLACK_PAYLOAD_INVALID")
        return value

    @classmethod
    def _slack_timestamp(cls, value: object) -> str:
        timestamp = cls._text(value)
        seconds, separator, fraction = timestamp.partition(".")
        if (
            separator != "."
            or not seconds.isdecimal()
            or not fraction.isdecimal()
            or not 1 <= len(fraction) <= 6
        ):
            raise IngressError("SLACK_PAYLOAD_INVALID")
        return timestamp

    @classmethod
    def _credential(cls, value: object) -> tuple[str, str]:
        credential = cls._text(value)
        credential_id, separator, raw_credential = credential.partition(".")
        if (
            separator != "."
            or _TOKEN_ID_PATTERN.fullmatch(credential_id) is None
            or not raw_credential
            or len(raw_credential.encode("utf-8")) > 64
            or raw_credential == credential_id
        ):
            raise IngressError("SLACK_CREDENTIAL_INVALID")
        return credential_id, raw_credential

    @staticmethod
    def _fingerprint(*components: str) -> str:
        payload = json.dumps(
            components,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp는 timezone-aware여야 합니다.")
        return value
