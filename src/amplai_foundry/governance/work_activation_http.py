"""Runnable signed-Slack HTTP boundary for Work Activation Cards."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from amplai_foundry.governance.ingress import IngressError, ProviderEnvelope
from amplai_foundry.governance.models import ChannelProvider
from amplai_foundry.governance.slack_work_activation import SlackWorkActivationIngress
from amplai_foundry.governance.work_activation import (
    IssuedWorkActivationToken,
    WorkActivationActionType,
    WorkActivationResult,
)

StartResponse = Callable[[str, list[tuple[str, str]]], Any]
_MAX_BODY_BYTES = 1 << 20


def render_work_activation_card(
    *, work_id: str, tokens: tuple[IssuedWorkActivationToken, ...]
) -> dict[str, object]:
    """Render exactly the token-bound buttons for one DRAFT Work."""
    actions = {item.record.action: item for item in tokens}
    required = set(WorkActivationActionType)
    if set(actions) != required or any(item.record.work_id != work_id for item in tokens):
        raise ValueError("WORK_ACTIVATION_CARD_TOKENS_INVALID")
    labels = {
        WorkActivationActionType.APPROVE: ("amplai_work_approve", "Approve", "primary"),
        WorkActivationActionType.REJECT: ("amplai_work_reject", "Reject", "danger"),
        WorkActivationActionType.REQUEST_CHANGES: (
            "amplai_work_request_changes",
            "Request changes",
            None,
        ),
    }
    elements: list[dict[str, object]] = []
    for action in WorkActivationActionType:
        action_id, text, style = labels[action]
        issued = actions[action]
        button: dict[str, object] = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": text},
            "value": f"{issued.record.token_id}.{issued.raw_token}",
        }
        if style is not None:
            button["style"] = style
        elements.append(button)
    return {
        "text": f"AMPLAI Work activation: {work_id}",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Activate Work* `{work_id}`"},
            },
            {"type": "actions", "block_id": "work-actions", "elements": elements},
        ],
    }


@dataclass(slots=True)
class SlackWorkActivationWSGIApp:
    """Accept one signed AMPLAI Slack Work action at a narrow public route."""

    ingress: SlackWorkActivationIngress
    provider_installation_ref: str

    def __call__(
        self, environ: Mapping[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        try:
            if (
                environ.get("REQUEST_METHOD") != "POST"
                or environ.get("PATH_INFO") != "/v1/slack/work-activation"
            ):
                return self._respond(start_response, 404, {"error": "ROUTE_NOT_FOUND"})
            body = self._body(environ)
            result = self.ingress.apply(
                ProviderEnvelope(
                    provider=ChannelProvider.SLACK,
                    provider_installation_ref=self.provider_installation_ref,
                    raw_body=body,
                    headers={
                        "X-Slack-Request-Timestamp": str(
                            environ.get("HTTP_X_SLACK_REQUEST_TIMESTAMP") or ""
                        ),
                        "X-Slack-Signature": str(environ.get("HTTP_X_SLACK_SIGNATURE") or ""),
                    },
                )
            )
            return self._respond(start_response, 200, self._result(result))
        except (IngressError, ValueError, RuntimeError):
            return self._respond(start_response, 400, {"error": "WORK_ACTIVATION_DENIED"})

    @staticmethod
    def _body(environ: Mapping[str, Any]) -> bytes:
        declared = str(environ.get("CONTENT_LENGTH") or "").strip()
        try:
            length = int(declared) if declared else 0
        except ValueError:
            raise ValueError("CONTENT_LENGTH_INVALID") from None
        if not 0 <= length <= _MAX_BODY_BYTES:
            raise ValueError("PAYLOAD_TOO_LARGE")
        stream = environ.get("wsgi.input")
        body = stream.read(length) if stream is not None and length else b""
        if len(body) != length:
            raise ValueError("REQUEST_BODY_TRUNCATED")
        return body

    @staticmethod
    def _result(result: WorkActivationResult) -> dict[str, object]:
        return {"work_id": result.work_id, "state": result.state, "replayed": result.replayed}

    @staticmethod
    def _respond(
        start_response: StartResponse, status: int, payload: dict[str, object]
    ) -> Iterable[bytes]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        start_response(
            f"{status} {HTTPStatus(status).phrase}",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]
