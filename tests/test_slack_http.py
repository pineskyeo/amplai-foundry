from __future__ import annotations

import ast
import contextlib
import hashlib
import inspect
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

# dispatcher 를 실물로 돌리는 fixture 는 test_governance_events 에 이미 있다. 복제하면 두
# 벌이 어긋난다. top-level import 는 pyproject 의 `pythonpath = ["tests"]` 가 받친다 (T009).
import test_governance_events as governance_fixtures
from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import (
    ActionTokenState,
    ActionTokenView,
    ActorRef,
    ActorType,
    DecisionAction,
    DecisionProjectionPayload,
    IssuedActionToken,
    PreparedReviewActionSet,
    ReviewActionSetService,
    ReviewActionSetView,
    ReviewProjectionPayload,
    slack_http,
)
from amplai_foundry.governance.events import (
    GovernanceEventService,
    OutboxConfig,
    OutboxDispatcher,
    OutboxEventView,
    OutboxState,
)
from amplai_foundry.governance.ingress import IngressCommandView
from amplai_foundry.governance.ingress_worker import SafeInteractionOutcome
from amplai_foundry.governance.models import ChannelProvider, ChannelRef, ProposalRef
from amplai_foundry.governance.slack_http import (
    PROBE_DESTINATION_REF,
    SLACK_API_BASE,
    SLACK_APP_ID_ENV,
    SLACK_BOT_TOKEN_ENV,
    SLACK_CHANNEL_ID_ENV,
    SLACK_SETTINGS_ENV_NAMES,
    SLACK_SIGNING_SECRET_ENV,
    CleanupFailureDetail,
    HttpSlackTransport,
    ReadbackDiagnosticData,
    ReadbackFailureCode,
    ReadbackLifecycleCause,
    ReadbackOutcome,
    ReadbackOutcomeStatus,
    SafeSlackProviderErrorCode,
    SlackCredentials,
    SlackInteractionFeedback,
    SlackSettings,
    build_probe_marker,
    load_slack_credentials,
    load_slack_settings,
    validate_call_budget,
    verify_marker_readback,
    worst_case_call_seconds,
)
from amplai_foundry.governance.slack_projection import (
    SLACK_MAX_HISTORY_PAGES,
    SlackFailureClass,
    SlackProjectionDestination,
    SlackTransport,
    SlackTransportError,
    build_slack_marker,
    classify_slack_failure,
    exhausted_cause_suffix,
    payload_digest,
)
from amplai_foundry.governance.store import GovernanceStore
from amplai_foundry.verification.runner import VerificationRunner

MODULE_PATH = Path(inspect.getfile(slack_http))
TOKEN = SecretStr("xoxb-test-token")
CHANNEL = "C0SLACK01"
# app ID 다. bot ID (B...) 와 다른 값이고, reconcile 이 이것으로 남의 message 를 배제한다.
APP_ID = "A0SLACKAPP"
MARKER: dict[str, object] = {
    "event_type": "amplai_proposal_card",
    "event_payload": {
        "event_id": "EVT-0000000000000001",
        "destination_ref": "provider:slack:" + "ab" * 32,
        "destination_sequence": 1,
        "payload_digest": "sha256:" + "cd" * 32,
    },
}
PAYLOAD: dict[str, object] = {"text": "제안 카드", "blocks": [{"type": "section"}]}


@dataclass(frozen=True)
class _FeedbackCommand:
    provider: ChannelProvider
    channel_ref: ChannelRef
    external_actor_key: str


# --------------------------------------------------------------------------------------
# Fake Slack. 실제 network 를 쓰지 않는다 — loopback 에 뜨는 http.server 하나다.
# 실제 Slack 은 T010 이고 그것은 marker 로 분리된다.
# --------------------------------------------------------------------------------------


@dataclass
class _Recorded:
    path: str
    content_type: str
    authorization: str
    body: bytes

    def json_body(self) -> Mapping[str, object]:
        decoded = json.loads(self.body.decode("utf-8"))
        assert isinstance(decoded, Mapping)
        return decoded

    def form_body(self) -> dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.body.decode("utf-8")).items()}


@dataclass
class _Reply:
    """한 응답. Slack 은 application error 를 HTTP 200 + ok:false 로 준다."""

    status: int = 200
    body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0
    # header 를 준 뒤 body 를 찔끔씩 흘린다. urlopen(timeout=) 은 recv 마다 timer 를
    # 되돌리므로 이 모양을 못 막는다 — deadline 을 보는 read loop 만 끊을 수 있다.
    dribble_seconds: float = 0.0
    dribble_chunks: int = 0

    @staticmethod
    def ok(payload: Mapping[str, object]) -> _Reply:
        return _Reply(body=json.dumps({"ok": True, **payload}).encode("utf-8"))

    @staticmethod
    def slack_error(code: str, *, status: int = 200) -> _Reply:
        return _Reply(status=status, body=json.dumps({"ok": False, "error": code}).encode("utf-8"))


class _FakeSlack:
    def __init__(self) -> None:
        self.requests: list[_Recorded] = []
        self.replies: dict[str, list[_Reply]] = {}
        self._server: ThreadingHTTPServer | None = None

    def queue(self, method: str, *replies: _Reply) -> None:
        self.replies.setdefault(method, []).extend(replies)

    def _next(self, method: str) -> _Reply:
        queued = self.replies.get(method)
        if not queued:
            return _Reply.ok({})
        return queued.pop(0) if len(queued) > 1 else queued[0]

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            # BaseHTTPRequestHandler 규약이라 이름을 바꿀 수 없다.
            #
            # GET 도 받는다. urllib 의 기본 redirect handler 는 301/302/303 을 만나면
            # POST 를 GET 으로 바꾸므로, GET 을 안 받으면 redirect 뒤의 요청이 기록되지
            # 않아 token 유출 test 가 아무것도 증명하지 못한다.
            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                method = self.path.rsplit("/", 1)[-1]
                outer.requests.append(
                    _Recorded(
                        path=self.path,
                        content_type=self.headers.get("Content-Type", ""),
                        authorization=self.headers.get("Authorization", ""),
                        body=body,
                    )
                )
                reply = outer._next(method)
                if reply.delay_seconds:
                    threading.Event().wait(reply.delay_seconds)
                if reply.dribble_chunks:
                    self.send_response(reply.status)
                    self.send_header("Content-Length", str(reply.dribble_chunks))
                    self.end_headers()
                    for _ in range(reply.dribble_chunks):
                        try:
                            self.wfile.write(b"a")
                            self.wfile.flush()
                        except OSError:
                            return
                        threading.Event().wait(reply.dribble_seconds)
                    return
                payload = reply.body if reply.body is not None else b'{"ok":true}'
                self.send_response(reply.status)
                for key, value in reply.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                """test 출력을 더럽히지 않는다."""

        class QuietServer(ThreadingHTTPServer):
            """client 가 먼저 끊는 것은 정상이다. traceback 으로 출력을 더럽히지 않는다."""

            def handle_error(self, request: object, client_address: object) -> None:
                return

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        # poll_interval 기본값 0.5초는 stop() 마다 그만큼 기다린다. test 83개면
        # 그것만 20초가 넘는다 (wave 5 regression lens A-3).
        threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.01) if self._server else None,
            daemon=True,
        ).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def slack() -> Iterator[_FakeSlack]:
    fake = _FakeSlack()
    fake.start()
    yield fake
    fake.stop()


def _transport(fake: _FakeSlack, *, timeout_seconds: float = 1.0) -> HttpSlackTransport:
    return HttpSlackTransport(
        bot_token=TOKEN,
        timeout_seconds=timeout_seconds,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        lease_seconds=_lease_for(timeout_seconds),
        base_url=fake.base_url,
    )


def _lease_for(timeout_seconds: float) -> int:
    """이 timeout 으로 transport 를 만들 수 있는 최소 lease. 예산 검증을 우회하지 않는다."""
    return int(worst_case_call_seconds(timeout_seconds, SLACK_MAX_HISTORY_PAGES)) + 1


def _resolved_signature(owner: type, name: str) -> inspect.Signature:
    return inspect.signature(getattr(owner, name))


# --------------------------------------------------------------------------------------
# MGC-012-T006 — Protocol 만족과 전송 형태
# --------------------------------------------------------------------------------------


# 계약은 001 C-1 이 권위다. annotation 으로 적으면 runtime 에 아무것도 증명되지 않고
# mypy 는 tests/ 를 안 본다 — 구성원과 signature 를 직접 대조한다.
def test_transport_matches_the_protocol_signature() -> None:
    required = {name for name in vars(SlackTransport) if not name.startswith("_")}
    assert required == {"post_message", "read_history"}
    for name in required:
        protocol = _resolved_signature(SlackTransport, name)
        actual = _resolved_signature(HttpSlackTransport, name)
        assert list(protocol.parameters) == list(actual.parameters)
        assert protocol.return_annotation == actual.return_annotation


# T006 AC-01
def test_post_message_returns_the_channel_and_ts(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply.ok({"channel": CHANNEL, "ts": "1700000000.000100"}))

    result = _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert result.channel == CHANNEL
    assert result.ts == "1700000000.000100"


# T006 AC-01 — marker 가 metadata 로 나가는 것이 계약의 본체다. 안 실리면 reconcile 이
# 영원히 못 찾아 매 재시도마다 Card 가 한 장씩 는다.
def test_post_message_sends_the_marker_as_metadata(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply.ok({"channel": CHANNEL, "ts": "1700000000.000100"}))

    _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    body = slack.requests[0].json_body()
    assert body["metadata"] == MARKER
    assert body["channel"] == CHANNEL
    assert body["text"] == PAYLOAD["text"]
    assert body["blocks"] == PAYLOAD["blocks"]


# T006 AC-01 — 복잡한 인자를 가진 method 는 JSON 으로 보낸다 (research P6). token 은
# Authorization header 로만 간다 — query string 에 넣지 않는다.
def test_post_message_uses_json_and_a_bearer_header(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply.ok({"channel": CHANNEL, "ts": "1700000000.000100"}))

    _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    recorded = slack.requests[0]
    assert recorded.content_type.startswith("application/json")
    assert recorded.authorization == f"Bearer {TOKEN.get_secret_value()}"
    assert "?" not in recorded.path


def test_http_failure_traceback_locals_contain_no_button_or_bot_credential(
    slack: _FakeSlack,
) -> None:
    raw_canary = "traceback-action-credential-canary"
    slack.queue("chat.postMessage", _Reply.slack_error("invalid_auth"))
    payload = {
        "text": "Review Card",
        "blocks": [
            {
                "type": "card",
                "actions": [
                    {
                        "type": "button",
                        "action_id": "approve",
                        "value": f"TOK-0123456789ABCDEF.{raw_canary}",
                    }
                ],
            }
        ],
    }

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=payload, marker=MARKER)

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
    diagnostics = "\n".join(rendered)
    assert raw_canary not in diagnostics
    assert TOKEN.get_secret_value() not in diagnostics


def test_http_interruption_traceback_contains_no_button_or_bot_credential(
    slack: _FakeSlack,
) -> None:
    raw_canary = "interrupt-action-credential-canary"
    payload = {
        "text": "Review Card",
        "blocks": [
            {
                "type": "card",
                "actions": [
                    {
                        "type": "button",
                        "action_id": "approve",
                        "value": f"TOK-0123456789ABCDEF.{raw_canary}",
                    }
                ],
            }
        ],
    }
    transport = _transport(slack)

    class InterruptingOpener:
        def open(self, request: object, *, timeout: float) -> object:
            del request, timeout
            raise KeyboardInterrupt("interrupted after serialization")

    transport._opener = InterruptingOpener()  # type: ignore[assignment]

    with pytest.raises(KeyboardInterrupt, match="Slack message delivery interrupted") as caught:
        transport.post_message(channel=CHANNEL, payload=payload, marker=MARKER)

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
    diagnostics = "\n".join(rendered)
    assert raw_canary not in diagnostics
    assert TOKEN.get_secret_value() not in diagnostics


def test_http_interruption_showlocals_child_is_secret_free() -> None:
    if os.environ.get("AMPLAI_HTTP_INTERRUPT_TRACEBACK_CHILD") != "1":
        return
    payload: Mapping[str, object] = {
        "text": "Review Card",
        "blocks": [
            {
                "type": "card",
                "actions": [
                    {
                        "type": "button",
                        "action_id": "approve",
                        "value": "TOK-0123456789ABCDEF." + "cafed00d" * 3 + "00000001",
                    }
                ],
            }
        ],
    }
    transport = HttpSlackTransport(
        bot_token=TOKEN,
        timeout_seconds=1.0,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        lease_seconds=_lease_for(1.0),
        base_url="https://example.invalid",
    )

    class InterruptingOpener:
        def open(self, request: object, *, timeout: float) -> object:
            del request, timeout
            raise KeyboardInterrupt("interrupted after serialization")

    transport._opener = InterruptingOpener()  # type: ignore[assignment]
    try:
        transport.post_message(channel=CHANNEL, payload=payload, marker=MARKER)
    except BaseException as error:
        payload = {}
        raise AssertionError("intentional sanitized HTTP interruption") from error
    raise AssertionError("HTTP interruption was expected")


def test_http_interruption_pytest_showlocals_contains_no_button_credential() -> None:
    environment = os.environ.copy()
    environment["AMPLAI_HTTP_INTERRUPT_TRACEBACK_CHILD"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__)),
            "-k",
            "http_interruption_showlocals_child_is_secret_free",
            "--showlocals",
            "-q",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert "cafed00d" * 3 not in completed.stdout + completed.stderr
    assert TOKEN.get_secret_value() not in completed.stdout + completed.stderr


def test_provider_controlled_error_text_cannot_echo_a_button_credential(
    slack: _FakeSlack,
) -> None:
    raw_canary = "provider-echo-action-credential-canary"
    slack.queue("chat.postMessage", _Reply.slack_error(raw_canary))
    payload = {
        "text": "Review Card",
        "blocks": [
            {
                "type": "card",
                "actions": [
                    {
                        "type": "button",
                        "action_id": "approve",
                        "value": f"TOK-0123456789ABCDEF.{raw_canary}",
                    }
                ],
            }
        ],
    }

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=payload, marker=MARKER)

    diagnostics = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            str(caught.value.raw_error_code),
            str(caught.value.transport_exception),
        )
    )
    assert raw_canary not in diagnostics
    assert caught.value.error_code is not None
    assert caught.value.error_code.startswith("unrecognized_")


def test_post_ephemeral_uses_fixed_destination_fields_and_bearer_auth(
    slack: _FakeSlack,
) -> None:
    slack.queue("chat.postEphemeral", _Reply.ok({"message_ts": "1700000000.000200"}))

    _transport(slack).post_ephemeral(
        channel=CHANNEL,
        user="U0REVIEWER",
        text="This Review Card is no longer current.",
    )

    recorded = slack.requests[0]
    assert recorded.path.endswith("/chat.postEphemeral")
    assert recorded.content_type.startswith("application/json")
    assert recorded.authorization == f"Bearer {TOKEN.get_secret_value()}"
    assert recorded.json_body() == {
        "channel": CHANNEL,
        "user": "U0REVIEWER",
        "text": "This Review Card is no longer current.",
    }


def test_slack_feedback_maps_only_the_closed_safe_message(slack: _FakeSlack) -> None:
    slack.queue("chat.postEphemeral", _Reply.ok({"message_ts": "1700000000.000200"}))
    command = cast(
        IngressCommandView,
        _FeedbackCommand(
            provider=ChannelProvider.SLACK,
            channel_ref=ChannelRef(
                provider=ChannelProvider.SLACK,
                workspace_id="T0WORKSPACE",
                channel_id=CHANNEL,
                message_id="1700000000.000100",
            ),
            external_actor_key="U0REVIEWER",
        ),
    )

    SlackInteractionFeedback(_transport(slack)).send(
        command,
        SafeInteractionOutcome.EXPIRED,
    )

    assert slack.requests[0].json_body()["text"] == (
        "This Proposal action expired. Request a new Review Card."
    )


def test_slack_feedback_ignores_non_slack_commands(slack: _FakeSlack) -> None:
    command = cast(
        IngressCommandView,
        _FeedbackCommand(
            provider=ChannelProvider.TELEGRAM,
            channel_ref=ChannelRef(
                provider=ChannelProvider.TELEGRAM,
                chat_id="chat-1",
                message_id="message-1",
            ),
            external_actor_key="telegram-user",
        ),
    )

    SlackInteractionFeedback(_transport(slack)).send(
        command,
        SafeInteractionOutcome.DENIED,
    )

    assert slack.requests == []


# wave 5 review A-1 — dict literal 은 **뒤 key 가 이긴다.** payload 가 무엇을 담고 있든
# 우리 channel 과 marker 가 나간다. 이전 판은 반대로 알고 guard 를 넣었는데 그 hazard 는
# 이 구성에서 발생할 수 없었다.
@pytest.mark.parametrize("key", ["metadata", "channel"])
def test_our_fields_win_over_anything_the_payload_carries(
    slack: _FakeSlack,
    key: str,
) -> None:
    slack.queue("chat.postMessage", _Reply.ok({"channel": CHANNEL, "ts": "1700000000.000100"}))

    _transport(slack).post_message(
        channel=CHANNEL,
        payload={**PAYLOAD, key: "덮어쓰기"},
        marker=MARKER,
    )

    body = slack.requests[0].json_body()
    assert body["channel"] == CHANNEL
    assert body["metadata"] == MARKER


# T006 AC-05 — include_all_metadata 를 빼면 event_payload 가 안 와서 marker 를 하나도 못
# 읽는다. 인자로 열지 않고 고정한다.
def test_read_history_always_requests_all_metadata(slack: _FakeSlack) -> None:
    slack.queue("conversations.history", _Reply.ok({"messages": []}))

    _transport(slack).read_history(channel=CHANNEL, cursor=None, limit=999)

    form = slack.requests[0].form_body()
    assert form["include_all_metadata"] == "true"
    assert form["channel"] == CHANNEL
    assert form["limit"] == "999"
    assert "cursor" not in form


# T006 AC-05 — read method 는 form-encoded 다. Slack 문서가 JSON 을 write method 로
# 한정하므로 확인되지 않은 지원에 기대지 않는다.
def test_read_history_uses_form_encoding(slack: _FakeSlack) -> None:
    slack.queue("conversations.history", _Reply.ok({"messages": []}))

    _transport(slack).read_history(channel=CHANNEL, cursor="c1", limit=10)

    recorded = slack.requests[0]
    assert recorded.content_type == "application/x-www-form-urlencoded"
    assert recorded.form_body()["cursor"] == "c1"


# T006 AC-06
def test_read_history_maps_messages_and_cursor(slack: _FakeSlack) -> None:
    slack.queue(
        "conversations.history",
        _Reply.ok(
            {
                "messages": [
                    {"ts": "1700000000.000002", "metadata": MARKER, "app_id": "A123"},
                    {"ts": "1700000000.000001"},
                ],
                "response_metadata": {"next_cursor": "next-1"},
            }
        ),
    )

    page = _transport(slack).read_history(channel=CHANNEL, cursor=None, limit=999)

    assert [message.ts for message in page.messages] == [
        "1700000000.000002",
        "1700000000.000001",
    ]
    assert page.messages[0].metadata == MARKER
    assert page.messages[0].app_id == "A123"
    # 없는 key 를 만들어 채우지 않는다 (C-1.2). 채우면 read_slack_marker 가 조회 결함을
    # 못 알아본다.
    assert page.messages[1].metadata is None
    assert page.messages[1].app_id is None
    assert page.next_cursor == "next-1"


# T006 AC-06 — Slack 은 마지막 page 에서 next_cursor 를 빈 문자열로 준다. 그대로 넘기면
# SlackHistoryPage 가 ValueError 로 거부하고 그 예외는 계약 밖이라 분류를 건너뛴다.
@pytest.mark.parametrize(
    "response_metadata",
    [{"next_cursor": ""}, {"next_cursor": "   "}, {}, None],
)
def test_an_empty_cursor_reads_as_the_last_page(
    slack: _FakeSlack,
    response_metadata: dict[str, object] | None,
) -> None:
    payload: dict[str, object] = {"messages": []}
    if response_metadata is not None:
        payload["response_metadata"] = response_metadata
    slack.queue("conversations.history", _Reply.ok(payload))

    page = _transport(slack).read_history(channel=CHANNEL, cursor=None, limit=999)

    assert page.next_cursor is None


# T006 — ts 없는 message 하나가 조회 전체를 멈추면 안 된다. 뒤에 있는 진짜 marker 를 못
# 읽고 판정 불가로 떨어져 되돌릴 수 없는 hold 가 된다.
def test_a_malformed_message_is_skipped_not_raised(slack: _FakeSlack) -> None:
    slack.queue(
        "conversations.history",
        _Reply.ok(
            {
                "messages": [
                    "문자열 message",
                    {"no_ts": True},
                    {"ts": ""},
                    {"ts": "1700000000.000001", "metadata": MARKER, "app_id": "A123"},
                ]
            }
        ),
    )

    page = _transport(slack).read_history(channel=CHANNEL, cursor=None, limit=999)

    assert [message.ts for message in page.messages] == ["1700000000.000001"]


# --------------------------------------------------------------------------------------
# T006 AC-02·AC-03·AC-07 — 실패는 전부 SlackTransportError 로 나온다
# --------------------------------------------------------------------------------------


# T006 AC-02 — Slack 은 application error 를 HTTP 200 + ok:false 로 준다. status code 만
# 보고 성공을 판정하면 invalid_auth 가 성공으로 읽힌다.
def test_ok_false_on_http_200_becomes_a_classified_failure(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply.slack_error("invalid_auth"))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert caught.value.error_code == "invalid_auth"
    assert caught.value.status_code == 200
    assert classify_slack_failure(caught.value) is SlackFailureClass.TERMINAL


# T006 AC-02 — allowlist 안의 code 는 재시도로 흐른다.
def test_a_retryable_slack_code_stays_retryable(slack: _FakeSlack) -> None:
    slack.queue("conversations.history", _Reply.slack_error("ratelimited"))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).read_history(channel=CHANNEL, cursor=None, limit=999)

    assert caught.value.error_code == "ratelimited"
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# T006 AC-03 — code 없는 HTTP 실패는 전부 transport 층이라 재시도다 (D-020 항목 1).
@pytest.mark.parametrize("status", [500, 502, 503, 504, 403])
def test_an_http_error_without_a_slack_code_is_retryable(slack: _FakeSlack, status: int) -> None:
    slack.queue("chat.postMessage", _Reply(status=status, body=b"<html>proxy</html>"))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert caught.value.error_code is None
    assert caught.value.status_code == status
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# T006 AC-03 — HTTP error body 가 Slack code 를 갖고 있으면 그것을 싣는다. 원인이 남아야
# operator 가 복구 행동을 고른다.
def test_an_http_error_carrying_a_slack_code_keeps_it(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply.slack_error("channel_not_found", status=404))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert caught.value.error_code == "channel_not_found"
    assert caught.value.status_code == 404


# T006 AC-03 — 429 는 Retry-After 를 싣는다. backoff 에 주입하지는 않는다 (R-007).
def test_rate_limiting_carries_retry_after_without_consuming_it(slack: _FakeSlack) -> None:
    slack.queue(
        "conversations.history",
        _Reply(
            status=429,
            body=b'{"ok":false,"error":"ratelimited"}',
            headers={"Retry-After": "30"},
        ),
    )

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).read_history(channel=CHANNEL, cursor=None, limit=999)

    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds == 30
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# T006 AC-03 — proxy·WAF 가 HTML 을 줄 수 있다. JSON 이 아니면 code 없는 실패다.
def test_a_non_json_success_body_is_a_retryable_failure(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply(status=200, body=b"<html>not json</html>"))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert caught.value.error_code is None
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# T006 — ok:true 인데 필수 필드가 없다. receipt 를 만들 수 없으므로 실패다.
# **공백 값도 포함한다.** SlackSendResult 구성은 _call 의 try 밖이라
# (post_message), 공백이 통과하면 SlackSendResult.__post_init__ 의 raw ValueError 가
# 그대로 샌다 — C-1 의무 1 이 깨진다. _require_text 의 공백 검사가 그것을 막는 유일한
# 장치인데 wave 5 regression lens BP2-2 전까지 test 가 없었다.
@pytest.mark.parametrize(
    "payload",
    [
        {"ts": "1.0"},
        {"channel": CHANNEL},
        {},
        {"channel": "", "ts": "1.0"},
        {"channel": CHANNEL, "ts": "  "},
        {"channel": "   ", "ts": "   "},
    ],
)
def test_a_success_response_missing_identity_fields_fails(
    slack: _FakeSlack,
    payload: dict[str, object],
) -> None:
    slack.queue("chat.postMessage", _Reply.ok(payload))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert caught.value.transport_exception == "missing_success_field"


# T006 AC-04 — 응답이 안 오면 timeout 안에 돌아온다. lease 를 넘기면 terminal 판정이
# 통째로 버려진다 (C-1 의무 2).
def test_a_hanging_server_fails_within_the_timeout(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply(delay_seconds=2.0))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack, timeout_seconds=0.2).post_message(
            channel=CHANNEL, payload=PAYLOAD, marker=MARKER
        )

    assert caught.value.error_code is None
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# T006 AC-07 — 연결 자체가 안 되는 경우다. 이것도 SlackTransportError 로 나와야 한다.
def test_an_unreachable_host_is_wrapped_not_raised_raw() -> None:
    transport = HttpSlackTransport(
        bot_token=TOKEN,
        timeout_seconds=1.0,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        lease_seconds=_lease_for(1.0),
        # 예약된 discard port. 연결이 즉시 거부된다.
        base_url="http://127.0.0.1:9",
    )

    with pytest.raises(SlackTransportError) as caught:
        transport.read_history(channel=CHANNEL, cursor=None, limit=999)

    assert caught.value.error_code is None
    assert caught.value.transport_exception is not None
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# T006 AC-07 — 실패 message 에 credential 이 안 들어간다. token 이 dead letter 와 log 로
# 새는 유일한 경로가 그것이다.
def test_no_failure_ever_names_the_credential(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply.slack_error("invalid_auth"))
    secret = TOKEN.get_secret_value()

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


# --------------------------------------------------------------------------------------
# T006 — 구성 검증과 dependency 경계
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "timeout", "base_url"),
    [
        (SecretStr(""), 1.0, SLACK_API_BASE),
        (SecretStr("   "), 1.0, SLACK_API_BASE),
        (TOKEN, 0.0, SLACK_API_BASE),
        (TOKEN, -1.0, SLACK_API_BASE),
        (TOKEN, float("nan"), SLACK_API_BASE),
        (TOKEN, float("inf"), SLACK_API_BASE),
        (TOKEN, 1.0, "  "),
    ],
)
def test_the_constructor_rejects_unusable_configuration(
    token: SecretStr,
    timeout: float,
    base_url: str,
) -> None:
    with pytest.raises(ValueError):
        HttpSlackTransport(
            bot_token=token,
            timeout_seconds=timeout,
            max_history_pages=SLACK_MAX_HISTORY_PAGES,
            lease_seconds=60,
            base_url=base_url,
        )


# wave 5 review P1-3 — 예산 검증이 아무도 안 부르는 함수면 FR-016 은 안 닫힌다.
# 생성자가 부르므로 예산을 넘는 transport 는 **만들 수조차 없다**.
def test_the_constructor_enforces_the_call_budget() -> None:
    with pytest.raises(ValueError, match="lease"):
        HttpSlackTransport(
            bot_token=TOKEN,
            timeout_seconds=10.0,
            max_history_pages=SLACK_MAX_HISTORY_PAGES,
            lease_seconds=30,
        )


# C-1 의무 2 는 호출 하나가 아니라 **한 deliver_next 안의 합계**를 묶으라고 한다.
# 호출 시점이 아니라 구성 시점에 막는다 — 그때는 이미 lease 를 쥐고 있다.
def test_the_call_budget_must_fit_inside_the_lease() -> None:
    # 5 page + send 1 회 = 6 호출. 호출당 상한은 2 x timeout 이다 (아래 test 참조).
    # 2 x 2.5s x 6 = 30s 는 lease 30s 안에 안 들어간다.
    with pytest.raises(ValueError, match="lease"):
        validate_call_budget(timeout_seconds=2.5, max_history_pages=5, lease_seconds=30)
    validate_call_budget(timeout_seconds=2.0, max_history_pages=5, lease_seconds=30)


# wave 5 review P1-1 — urlopen(timeout=) 은 socket 연산 하나마다 걸리는 값이라 호출
# 전체를 묶지 않는다. 상한이 timeout 이 아니라 2 x timeout 인 이유가 그것이다. 공식이
# 1배로 돌아가면 예산이 거짓이 된다.
def test_the_worst_case_accounts_for_a_stall_after_the_deadline() -> None:
    # 호출 6회 x (2 x 3s) = 36s
    assert worst_case_call_seconds(3.0, 5) == 36.0
    assert worst_case_call_seconds(1.0, 1) == 4.0


@pytest.mark.parametrize(
    ("timeout", "pages", "lease"),
    [
        (0.0, 5, 30),
        (-1.0, 5, 30),
        (float("nan"), 5, 30),
        (float("inf"), 5, 30),
        (5.0, 0, 30),
        (5.0, 5, 0),
    ],
)
def test_the_call_budget_rejects_meaningless_inputs(
    timeout: float,
    pages: int,
    lease: int,
) -> None:
    with pytest.raises(ValueError):
        validate_call_budget(
            timeout_seconds=timeout,
            max_history_pages=pages,
            lease_seconds=lease,
        )


# T006 AC-08 — dependency 셋을 유지한다 (R-013). HTTP client 를 추가하지 않는다.
def test_the_module_adds_no_third_party_dependency() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    # stdlib 목록을 손으로 세지 않는다. 그러면 stdlib import 를 더할 때마다 이 test 를
    # 고치게 되고, 고치는 동안 진짜 third-party 가 섞여 들어와도 안 보인다.
    third_party = roots - sys.stdlib_module_names - {"amplai_foundry"}
    assert third_party == {"pydantic"}


# 기본 base_url 이 실제 Slack 이어야 한다. 잘못 두면 test 는 통과하고 운영이 조용히
# 아무 데도 안 보낸다.
def test_the_default_base_url_is_the_real_slack_api() -> None:
    assert SLACK_API_BASE == "https://slack.com/api"


# --------------------------------------------------------------------------------------
# MGC-012-T007 — credential 은 한 곳에서만 들어오고 어디에도 남지 않는다
# --------------------------------------------------------------------------------------

_BOTH: dict[str, str] = {
    SLACK_BOT_TOKEN_ENV: "xoxb-real-token",
    SLACK_SIGNING_SECRET_ENV: "0123456789abcdef",
}


# T007 AC-01
def test_both_variables_load_as_secrets() -> None:
    loaded = load_slack_credentials(_BOTH)

    assert loaded is not None
    assert loaded.bot_token.get_secret_value() == "xoxb-real-token"
    assert loaded.signing_secret.get_secret_value() == "0123456789abcdef"


# T007 AC-02 — 부재와 빈 문자열이 같은 결과다. 다르면 빈 변수를 export 한 환경이
# "구성됨" 으로 읽혀 엉뚱한 실패를 낸다.
@pytest.mark.parametrize(
    "environ",
    [
        {},
        {SLACK_BOT_TOKEN_ENV: "", SLACK_SIGNING_SECRET_ENV: ""},
        {SLACK_BOT_TOKEN_ENV: "   ", SLACK_SIGNING_SECRET_ENV: "\n"},
    ],
)
def test_absent_and_blank_are_the_same_unconfigured_result(environ: dict[str, str]) -> None:
    assert load_slack_credentials(environ) is None


# T007 AC-02 경계 — 부분 구성은 미구성이 아니라 실수다. None 으로 뭉뚱그리면 token 만
# 넣고 E2E 를 돌린 사람이 skip 만 보고 자기가 뭘 빠뜨렸는지 모른다.
@pytest.mark.parametrize(
    "environ",
    [
        {SLACK_BOT_TOKEN_ENV: "xoxb-real-token"},
        {SLACK_SIGNING_SECRET_ENV: "0123456789abcdef"},
        {SLACK_BOT_TOKEN_ENV: "xoxb-real-token", SLACK_SIGNING_SECRET_ENV: "  "},
    ],
)
def test_partial_configuration_is_loud(environ: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="불완전"):
        load_slack_credentials(environ)


# T007 AC-04 — 실수 message 에 값이 들어가면 안 된다. 이름만 적는다.
def test_the_partial_configuration_error_names_variables_not_values() -> None:
    with pytest.raises(ValueError) as caught:
        load_slack_credentials({SLACK_BOT_TOKEN_ENV: "xoxb-real-token"})

    assert "xoxb-real-token" not in str(caught.value)
    assert SLACK_SIGNING_SECRET_ENV in str(caught.value)


# T007 AC-03 — repr 로 secret 이 새지 않는다. dataclass 의 기본 repr 이 값을 찍는 것이
# 가장 흔한 유출 경로다.
def test_credentials_never_render_their_secrets() -> None:
    loaded = load_slack_credentials(_BOTH)
    assert loaded is not None

    for rendered in (repr(loaded), str(loaded), f"{loaded}"):
        assert "xoxb-real-token" not in rendered
        assert "0123456789abcdef" not in rendered


# T007 — 빈 값으로 직접 만들 수도 없어야 한다. loader 를 우회하는 경로를 막는다.
@pytest.mark.parametrize(
    ("bot_token", "signing_secret"),
    [("", "s"), ("   ", "s"), ("t", ""), ("t", "  ")],
)
def test_credentials_reject_blank_values(bot_token: str, signing_secret: str) -> None:
    with pytest.raises(ValueError):
        SlackCredentials(
            bot_token=SecretStr(bot_token),
            signing_secret=SecretStr(signing_secret),
        )


# T007 AC-01 — 인자를 안 주면 실제 환경을 읽는다. 그 경로가 실제로 도는지 확인한다.
def test_the_default_source_is_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SLACK_BOT_TOKEN_ENV, "xoxb-from-env")
    monkeypatch.setenv(SLACK_SIGNING_SECRET_ENV, "sig-from-env")

    loaded = load_slack_credentials()

    assert loaded is not None
    assert loaded.bot_token.get_secret_value() == "xoxb-from-env"


def test_an_unconfigured_environment_reads_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SLACK_BOT_TOKEN_ENV, raising=False)
    monkeypatch.delenv(SLACK_SIGNING_SECRET_ENV, raising=False)

    assert load_slack_credentials() is None


# T007 AC-05 — 읽는 지점이 하나다. core 경로가 os.environ 을 직접 읽으면 주입이 무의미해지고
# test 가 실제 환경에 의존하게 된다.
def test_the_environment_is_read_in_exactly_one_place() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ]
    assert len(reads) == 1

    enclosing = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(inner, ast.Attribute)
            and inner.attr == "environ"
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "os"
            for inner in ast.walk(node)
        )
    ]
    assert enclosing == ["load_slack_credentials"]


# T007 — transport 가 credential 을 직접 읽지 않는다. 주입만 받는다.
def test_the_transport_does_not_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SLACK_BOT_TOKEN_ENV, "xoxb-should-not-be-used")

    with pytest.raises(ValueError):
        HttpSlackTransport(
            bot_token=SecretStr(""),
            timeout_seconds=1.0,
            max_history_pages=SLACK_MAX_HISTORY_PAGES,
            lease_seconds=60,
        )


# --------------------------------------------------------------------------------------
# wave 5 review 가 낸 gap 들. 전부 통과하는 suite 안에서 재현됐던 것이라 여기 고정한다.
# --------------------------------------------------------------------------------------


# wave 5 review P0-1 — proxy·WAF·gateway 가 {"error": ...} 모양 JSON 을 준다. 그것을
# Slack code 로 실으면 allowlist 밖이라 terminal 이 되고, terminal 은 그 destination 의
# 이후 Card 를 전부 멈추는 **되돌릴 수 없는 hold** 다. Slack 의 error 는 언제나 ok:false
# 를 달고 오고 중간 장비의 봉투는 안 단다 — 그 차이가 판별자다.
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, b'{"error": "upstream connect error or disconnect/reset before headers"}'),
        (502, b'{"error": "Bad Gateway"}'),
        (500, b'{"error": "internal server error"}'),
        (200, b'{"error": "authentication required"}'),
        (200, b'{"error": "quota exceeded, retry later"}'),
    ],
)
def test_a_non_slack_error_envelope_never_becomes_terminal(
    slack: _FakeSlack,
    status: int,
    body: bytes,
) -> None:
    slack.queue("chat.postMessage", _Reply(status=status, body=body))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert caught.value.error_code is None
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# 반대쪽 — 진짜 Slack 봉투는 code 를 그대로 싣는다. 위 test 만 있으면 code 를 통째로
# 버리는 구현도 통과한다.
@pytest.mark.parametrize("status", [200, 429, 503])
def test_a_real_slack_envelope_keeps_its_code(slack: _FakeSlack, status: int) -> None:
    slack.queue(
        "conversations.history",
        _Reply(status=status, body=b'{"ok":false,"error":"service_unavailable"}'),
    )

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).read_history(channel=CHANNEL, cursor=None, limit=999)

    assert caught.value.error_code == "service_unavailable"
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# wave 5 review A-5 — ok:false 인데 error key 가 없는 응답. code 없이 retryable 로
# 흘러야 한다.
def test_a_slack_failure_without_a_code_stays_retryable(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply(body=b'{"ok":false}'))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert caught.value.error_code is None
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# wave 5 review P1-2 (contract) — 이전 판의 AC-04 test 는 timeout 과 무관한 이유로
# 통과했다. delay 응답이 body 를 안 줘서 missing_success_field 로 떨어졌고, timeout=
# 인자를 통째로 지워도 초록이었다. **경과 시간을 잰다.**
def test_a_stalled_response_fails_within_the_deadline(slack: _FakeSlack) -> None:
    slack.queue(
        "chat.postMessage",
        _Reply(
            body=json.dumps({"ok": True, "channel": CHANNEL, "ts": "1.0"}).encode("utf-8"),
            delay_seconds=5.0,
        ),
    )

    started = time.monotonic()
    with pytest.raises(SlackTransportError):
        _transport(slack, timeout_seconds=0.3).post_message(
            channel=CHANNEL, payload=PAYLOAD, marker=MARKER
        )
    elapsed = time.monotonic() - started

    # timeout 이 없으면 5초를 기다린 뒤 **성공**한다. 여기서 실패하고 빨리 돌아오는 것이
    # 두 사실을 동시에 고정한다.
    assert elapsed < 3.0


# wave 5 review P1-1 (failure-recovery) — header 를 준 뒤 body 를 찔끔씩 보내는 상대.
# urlopen(timeout=) 은 recv 마다 timer 를 되돌리므로 이 경우를 못 막는다. 조각마다
# deadline 을 보는 read loop 가 있어야 끊긴다.
def test_a_dribbling_body_is_cut_off_by_the_deadline(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply(dribble_seconds=0.2, dribble_chunks=30))

    started = time.monotonic()
    with pytest.raises(SlackTransportError) as caught:
        _transport(slack, timeout_seconds=0.5).post_message(
            channel=CHANNEL, payload=PAYLOAD, marker=MARKER
        )
    elapsed = time.monotonic() - started

    # 상대는 6초어치를 흘리려 한다. deadline 이 없으면 끝까지 받는다.
    assert elapsed < 3.0
    assert caught.value.transport_exception == "deadline_exceeded"
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# wave 5 review P1-1 (contract) — body 를 만드는 것과 Request 를 세우는 것이 try 밖이면
# raw 예외가 샌다. C-1 의무 1 이 깨지고, H-3 의 readback 자가검사는 destination 의
# 두 번째 방어선을 안 거치므로 거기서 raw 예외로 죽는다.
def test_an_unserializable_payload_is_wrapped_not_leaked(slack: _FakeSlack) -> None:
    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(
            channel=CHANNEL,
            payload={"text": datetime(2026, 8, 7, tzinfo=UTC)},
            marker=MARKER,
        )

    assert caught.value.error_code is None
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


def test_an_unserializable_marker_is_wrapped_not_leaked(slack: _FakeSlack) -> None:
    with pytest.raises(SlackTransportError):
        _transport(slack).post_message(
            channel=CHANNEL,
            payload=PAYLOAD,
            marker={"event_type": "x", "event_payload": {"bad": {1, 2}}},
        )


def test_a_base_url_without_a_scheme_is_wrapped_not_leaked() -> None:
    transport = HttpSlackTransport(
        bot_token=TOKEN,
        timeout_seconds=1.0,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        lease_seconds=_lease_for(1.0),
        base_url="slack.com/api",
    )

    with pytest.raises(SlackTransportError) as caught:
        transport.read_history(channel=CHANNEL, cursor=None, limit=999)

    assert caught.value.error_code is None


# wave 5 review P1-2 (failure-recovery) — stdlib 의 기본 redirect handler 는
# Authorization 을 안 떼고 host 도 안 본다. 302 를 준 쪽이 지정한 아무 host 로 token 이
# 간다. redirect 를 아예 따라가지 않고, header 도 unredirected 로 단다.
def test_a_redirect_is_refused_and_never_carries_the_token(slack: _FakeSlack) -> None:
    victim = _FakeSlack()
    victim.start()
    try:
        slack.queue(
            "chat.postMessage",
            _Reply(
                status=302,
                body=b"",
                headers={"Location": f"{victim.base_url}/api/chat.postMessage"},
            ),
        )

        with pytest.raises(SlackTransportError) as caught:
            _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

        # redirect 를 안 따라갔다. 상대 server 는 요청을 한 번도 못 받는다.
        assert victim.requests == []
        assert caught.value.status_code == 302
        assert caught.value.error_code is None
        assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE
    finally:
        victim.stop()


# wave 5 review A-2 — read() 에 상한이 없으면 고장난 상대가 memory 를 먹는다.
def test_an_oversized_response_is_cut_off(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply(body=b'{"ok":true,"x":"' + b"a" * (17 * 1024 * 1024)))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack, timeout_seconds=10.0).post_message(
            channel=CHANNEL, payload=PAYLOAD, marker=MARKER
        )

    assert caught.value.transport_exception == "response_too_large"


# wave 5 review A-4 — 이전 판은 ok:false 경로 하나만 봤다. 실패 경로 전부에서 token 이
# 안 나오는 것을 본다. __cause__ 체인까지 훑는다.
@pytest.mark.parametrize(
    "reply",
    [
        _Reply.slack_error("invalid_auth"),
        _Reply(status=500, body=b"<html>proxy</html>"),
        _Reply(status=502, body=b'{"error":"Bad Gateway"}'),
        _Reply(body=b"not json"),
        _Reply.ok({}),
    ],
)
def test_no_failure_path_ever_names_the_credential(slack: _FakeSlack, reply: _Reply) -> None:
    slack.queue("chat.postMessage", reply)
    secret = TOKEN.get_secret_value()

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    rendered = [str(caught.value), repr(caught.value), str(caught.value.transport_exception)]
    cause = caught.value.__cause__
    while cause is not None:
        rendered.extend([str(cause), repr(cause)])
        cause = cause.__cause__
    for text in rendered:
        assert secret not in text


# wave 5 review A-3 — T007 AC-05 의 "core 경로에는 없다" 절을 어떤 test 도 안 지켰다.
# slack_http.py 한 파일만 훑으면 다음 commit 이 다른 파일에서 읽어도 안 보인다.
def test_no_other_module_reads_slack_configuration_from_the_environment() -> None:
    offenders: list[str] = []
    # **절대 경로다.** 상대 경로면 다른 CWD 에서 0개를 훑고 조용히 통과한다
    # (wave 5 regression lens A-2). MODULE_PATH 는 inspect 가 준 절대 경로다.
    package_root = MODULE_PATH.parents[1]
    for path in package_root.rglob("*.py"):
        if path.name == "slack_http.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "SLACK" in text and ("os.environ" in text or "from os import environ" in text):
            offenders.append(str(path))
    assert offenders == []


# wave 5 regression lens BP2-1 — commit ec3b8fa 는 "방어선을 둘 둔다" 고 적었는데
# 첫째 줄(_NoRedirect)만 고정돼 있었다. 둘째 줄을 add_header 로 되돌려도 전부 초록이었다.
#
# **둘째 줄은 실제로 값이 있다.** redirect handler 를 기본값으로 되돌린 상태에서
# add_unredirected_header 면 token 이 안 가고 add_header 면 간다 — lens 가 실측했다.
# 그래서 _NoRedirect 를 일부러 뺀 opener 로 그 줄만 검사한다.
def test_the_authorization_header_does_not_survive_a_redirect(slack: _FakeSlack) -> None:
    victim = _FakeSlack()
    victim.start()
    try:
        slack.queue(
            "chat.postMessage",
            _Reply(
                status=302,
                body=b"",
                headers={"Location": f"{victim.base_url}/api/chat.postMessage"},
            ),
        )
        victim.queue("chat.postMessage", _Reply.ok({"channel": CHANNEL, "ts": "1.0"}))
        transport = _transport(slack)
        # redirect 를 막는 첫째 방어선을 뺀다. 남는 것은 unredirected header 하나다.
        transport._opener = urllib.request.build_opener()

        with contextlib.suppress(SlackTransportError):
            transport.post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

        # redirect 는 따라갔지만 token 은 안 따라갔다.
        assert victim.requests, "redirect 를 안 따라갔으면 이 test 는 아무것도 증명하지 않는다"
        assert all(recorded.authorization == "" for recorded in victim.requests)
    finally:
        victim.stop()


# wave 5 regression lens A-1 — 이 표지는 진단 문자열이 아니라 append-only column 에
# 저장되는 code 다. exhausted_cause_suffix 가 transport_{...} 로 만든다. 빠지면
# "Slack 이 code 없이 거절" 과 "애초에 Slack 이 아님" 이 같은 row 로 뭉개진다.
def test_a_non_slack_envelope_is_labelled_for_the_dead_letter(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply(body=b'{"error":"authentication required"}'))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert caught.value.transport_exception == "not_a_slack_envelope"
    assert exhausted_cause_suffix(caught.value) == "transport_not_a_slack_envelope"


# 반대쪽 — 진짜 Slack 이 code 없이 거절한 것은 그 표지를 안 단다. 위 test 만 있으면
# 모든 실패에 표지를 다는 구현도 통과한다.
def test_a_slack_envelope_without_a_code_is_not_labelled(slack: _FakeSlack) -> None:
    slack.queue("chat.postMessage", _Reply(body=b'{"ok":false}'))

    with pytest.raises(SlackTransportError) as caught:
        _transport(slack).post_message(channel=CHANNEL, payload=PAYLOAD, marker=MARKER)

    assert caught.value.transport_exception is None
    assert exhausted_cause_suffix(caught.value) == "no_slack_code"


# --------------------------------------------------------------------------------------
# MGC-012-T008 — readback 자가검사
#
# 이 검사가 잡는 넷은 destination 안에서 판정할 수 없다. 못 잡으면 결과가 hold 가 아니라
# **조용한 중복 Card** 다 — marker 를 하나도 못 읽으면 history 소진이 미전송으로 판정되고
# 매 재시도마다 Card 가 한 장씩 는다.
# --------------------------------------------------------------------------------------

PROBE_TS = "1700000000.009000"

# probe 는 진짜 marker 와 **destination_ref 만** 다르다. 나머지는 실제로 나가는 것과 같은
# 모양이어야 자가검사가 진짜 경로를 검증한다. 그 하나로 reconcile 의 시야에서 빠진다.
PROBE_MARKER: dict[str, object] = {
    "event_type": MARKER["event_type"],
    "event_payload": {
        **cast("dict[str, object]", MARKER["event_payload"]),
        "destination_ref": PROBE_DESTINATION_REF,
    },
}


def _probe_reply() -> _Reply:
    return _Reply.ok({"channel": CHANNEL, "ts": PROBE_TS})


def _history_with(*messages: dict[str, object]) -> _Reply:
    return _Reply.ok({"messages": list(messages)})


# 자가검사는 방금 Slack이 확인한 `ts`만 되읽는다. 이전 probe history scan은 금지다.
# 요청 순서가 곧 계약이므로 index를 이름으로 고정한다.
PROBE_POST = 0
PROBE_READ = 1
PROBE_DELETE = 2


def _queue_probe_flow(slack: _FakeSlack, history: _Reply) -> None:
    """Queue the post and exact confirmed-ts readback replies."""
    slack.queue("conversations.history", history)
    slack.queue("chat.postMessage", _probe_reply())


def _probe_event() -> OutboxEventView:
    """A real event view, so `build_probe_marker` is exercised on a real input."""
    payload: dict[str, object] = {"text": "제안 카드"}
    return OutboxEventView(
        event_id="EVT-0000000000000042",
        proposal_ref=ProposalRef(
            project_ref=ProjectRef(project_id="amplai", namespace="org/default/project/amplai"),
            proposal_id="PROP-20260730-ABCDEF12",
        ),
        aggregate_sequence=1,
        destination_ref="provider:slack:" + "ab" * 32,
        destination_sequence=1,
        source_state_revision=1,
        payload_digest="sha256:" + "cd" * 32,
        payload=payload,
        state=OutboxState.LEASED,
        attempts=1,
        claim_generation=1,
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


def _check(
    slack: _FakeSlack,
    *,
    probe_marker: Mapping[str, object] | None = None,
    lifecycle_cause: ReadbackLifecycleCause | None = None,
) -> ReadbackOutcome:
    return verify_marker_readback(
        _transport(slack),
        channel=CHANNEL,
        app_id=APP_ID,
        probe_marker=PROBE_MARKER if probe_marker is None else probe_marker,
        lifecycle_cause=lifecycle_cause,
    )


# T008 AC-01
def test_a_marker_that_round_trips_lets_startup_proceed(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": APP_ID}),
    )

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.READY
    assert outcome.allows_activation
    assert outcome.primary_failure_code is None
    assert outcome.secondary_cleanup_failure is None

    # probe 가 실제로 나갔고 marker 를 달고 나갔다.
    body = slack.requests[PROBE_POST].json_body()
    assert body["metadata"] == PROBE_MARKER
    # 사람이 채널에서 보고 무엇인지 알아야 한다. 빈 message 는 정체불명의 흔적만 남긴다.
    assert isinstance(body["text"], str)
    assert "AMPLAI" in body["text"]


# T008 AC-02 — include_all_metadata 를 안 붙이는 구현이면 event_type 만 오고
# event_payload 가 안 온다. 그 상태로 기동하면 조용한 중복 Card 다.
def test_stripped_metadata_refuses_startup(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with(
            {
                "ts": PROBE_TS,
                "metadata": {"event_type": PROBE_MARKER["event_type"]},
                "app_id": APP_ID,
            }
        ),
    )

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.READBACK_FAILED
    assert outcome.primary_failure_code is ReadbackFailureCode.MARKER_UNREADABLE
    assert not outcome.allows_activation


# T008 AC-03 — app_id 가 안 오면 reconcile 이 우리 marker 를 하나도 우리 것으로
# 인정하지 않는다.
def test_a_missing_app_id_refuses_startup(slack: _FakeSlack) -> None:
    _queue_probe_flow(slack, _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER}))

    outcome = _check(slack)

    assert outcome.primary_failure_code is ReadbackFailureCode.APP_ID_MISMATCH
    assert not outcome.allows_activation


# T008 AC-03 — 값이 틀린 app_id 도 같다. 구성 오류가 조용한 중복 Card 로 이어진다.
def test_a_mismatched_app_id_refuses_startup(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": "A_SOMEONE_ELSE"}),
    )

    outcome = _check(slack)

    assert outcome.primary_failure_code is ReadbackFailureCode.APP_ID_MISMATCH
    assert not outcome.allows_activation


# T008 AC-04 — event_payload 의 필드 하나가 왕복에서 바뀌면 reconcile 판정이 틀린다.
@pytest.mark.parametrize(
    "field",
    ["event_id", "destination_ref", "destination_sequence", "payload_digest"],
)
def test_a_field_changed_in_transit_refuses_startup(slack: _FakeSlack, field: str) -> None:
    body = dict(cast("Mapping[str, object]", PROBE_MARKER["event_payload"]))
    body[field] = 999 if field == "destination_sequence" else "바뀐 값"
    _queue_probe_flow(
        slack,
        _history_with(
            {
                "ts": PROBE_TS,
                "metadata": {"event_type": PROBE_MARKER["event_type"], "event_payload": body},
                "app_id": APP_ID,
            }
        ),
    )

    outcome = _check(slack)

    assert outcome.primary_failure_code is ReadbackFailureCode.MARKER_MISMATCH
    assert not outcome.allows_activation


# T008 — event_type 을 개명하면 marker 를 아예 못 알아본다.
def test_a_renamed_event_type_refuses_startup(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with(
            {
                "ts": PROBE_TS,
                "metadata": {
                    "event_type": "renamed",
                    "event_payload": PROBE_MARKER["event_payload"],
                },
                "app_id": APP_ID,
            }
        ),
    )

    outcome = _check(slack)

    assert outcome.primary_failure_code is ReadbackFailureCode.MARKER_MISMATCH
    assert not outcome.allows_activation


def test_a_missing_event_type_is_structurally_unreadable(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with(
            {
                "ts": PROBE_TS,
                "metadata": {"event_payload": PROBE_MARKER["event_payload"]},
                "app_id": APP_ID,
            }
        ),
    )

    outcome = _check(slack)

    assert outcome.primary_failure_code is ReadbackFailureCode.MARKER_UNREADABLE
    assert not outcome.allows_activation


# T008 — probe 를 아예 못 찾는 경우. 조회 범위나 scope 문제다.
def test_a_probe_that_cannot_be_found_refuses_startup(slack: _FakeSlack) -> None:
    _queue_probe_flow(slack, _Reply.ok({"messages": []}))

    outcome = _check(slack)

    assert outcome.primary_failure_code is ReadbackFailureCode.PROBE_NOT_FOUND
    assert not outcome.allows_activation


# T008 AC-05 — scope 가 모자라면 read 에서 missing_scope 가 난다. H-1.1 이 경고한
# 상황이고, 이 검사가 그것을 **첫 재시도가 아니라 기동 시점**으로 앞당긴다.
def test_a_missing_scope_surfaces_at_startup(slack: _FakeSlack) -> None:
    _queue_probe_flow(slack, _Reply.slack_error("missing_scope"))

    outcome = _check(slack)

    assert outcome.primary_failure_code is ReadbackFailureCode.HISTORY_READ_FAILED
    assert outcome.diagnostic_data.provider_error_code is SafeSlackProviderErrorCode.MISSING_SCOPE
    assert not outcome.allows_activation


# T008 — 자가검사가 실제 계약대로 조회하는지. include_all_metadata 를 빼면 이 검사
# 자체가 무의미해진다.
def test_the_self_check_reads_with_all_metadata(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": APP_ID}),
    )

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.READY

    form = slack.requests[PROBE_READ].form_body()
    assert form["include_all_metadata"] == "true"
    assert form["channel"] == CHANNEL
    # 조회 범위도 고정한다. 이 값이 작아지면 조용한 채널을 전제하게 되고, 남이 방금 떠든
    # 채널에서 probe 를 놓쳐 기동이 엉뚱한 이유로 거부된다.
    assert form["limit"] == "100"


# T008 round 2 — **probe 는 진짜 destination 의 marker 를 달면 안 된다.**
#
# 달면 reconcile 이 probe 를 Card 로 오인한다. 결과는 조용한 미전송(DELIVERED 로 기록되는데
# Card 는 안 나감) 이거나 중복 Card 다. wave 6 failure-recovery review 의 P0 다.
def test_a_probe_marked_for_a_real_destination_is_refused_before_sending(
    slack: _FakeSlack,
) -> None:
    real = {
        "event_type": MARKER["event_type"],
        "event_payload": dict(cast("Mapping[str, object]", MARKER["event_payload"])),
    }

    outcome = _check(slack, probe_marker=real)

    # **보내기 전에** 막는다. 보낸 뒤에 알면 이미 채널에 남아 reconcile 이 그것을 본다.
    assert outcome.primary_failure_code is ReadbackFailureCode.PROBE_INPUT_INVALID
    assert not outcome.allows_activation
    assert slack.requests == []


# T008 round 2 — event_payload 가 아예 없는 probe_marker 도 보내기 전에 막는다. 이 분기는
# round 1 까지 어떤 test 도 안 밟아서 mutation 이 살아남았다 (regression lens M13).
def test_a_probe_marker_without_a_payload_is_refused_before_sending(slack: _FakeSlack) -> None:
    outcome = _check(slack, probe_marker={"event_type": MARKER["event_type"]})

    assert outcome.primary_failure_code is ReadbackFailureCode.PROBE_INPUT_INVALID
    assert not outcome.allows_activation
    assert slack.requests == []


# T008 AC-15 — local input의 전체 shape와 type을 post 전에 검사한다. destination_ref 하나만
# 맞는 malformed marker가 remote side effect를 만들면 PROBE_INPUT_INVALID 계약이 거짓이다.
@pytest.mark.parametrize(
    "mutate",
    [
        lambda marker: marker.update(event_type="renamed"),
        lambda marker: marker.update(extra="not-approved"),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).pop("event_id"),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(event_id=1),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(event_id=" "),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(
            destination_sequence=True
        ),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(
            destination_sequence=0
        ),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(
            payload_digest=None
        ),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(
            payload_digest="not-a-digest"
        ),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(
            payload_digest="sha256:" + "A" * 64
        ),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(
            payload_digest="sha256:" + "a" * 63
        ),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(
            payload_digest="sha256:" + "g" * 64
        ),
        lambda marker: cast("dict[str, object]", marker["event_payload"]).update(extra="nope"),
    ],
)
def test_every_malformed_probe_input_is_rejected_without_network(
    slack: _FakeSlack,
    mutate: Callable[[dict[str, object]], object],
) -> None:
    marker = {
        "event_type": PROBE_MARKER["event_type"],
        "event_payload": dict(cast("Mapping[str, object]", PROBE_MARKER["event_payload"])),
    }
    mutate(marker)

    outcome = _check(slack, probe_marker=marker)

    assert outcome.status is ReadbackOutcomeStatus.READBACK_FAILED
    assert outcome.primary_failure_code is ReadbackFailureCode.PROBE_INPUT_INVALID
    assert not outcome.allows_activation
    assert slack.requests == []


# T008 round 2 — 채널이 조용하다고 가정하지 않는다. probe 가 첫 message 가 아니어도
# **ts 로** 찾아야 한다. 첫 message 를 집으면 남의 message 를 검사하고 엉뚱하게 거부한다.
def test_the_probe_is_found_by_timestamp_not_by_position(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with(
            {"ts": "1700000000.009900", "text": "남이 방금 떠들었다", "app_id": "A_SOMEONE_ELSE"},
            {"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": APP_ID},
            {"ts": "1700000000.008000", "text": "그 전에도 떠들었다", "app_id": "A_SOMEONE_ELSE"},
        ),
    )

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.READY


# T008 round 2 — **끝나면 probe 를 지운다.** 판정에서 빠지는 것만으로 부족하다. probe 는
# conversations.history 의 조회 예산을 그대로 먹고, 재시작 loop 이 그 예산을 채우면 진짜
# Card 가 probe 아래 묻혀 reconcile 이 판정 불가로 떨어진다. 그 결과는 되돌릴 수 없는
# hold 다 (wave 6 review round 2 가 실측, D-030).
def test_the_probe_is_deleted_after_a_successful_check(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": APP_ID}),
    )

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.READY

    assert [record.path for record in slack.requests] == [
        "/chat.postMessage",
        "/conversations.history",
        "/chat.delete",
    ]
    assert slack.requests[PROBE_DELETE].json_body() == {"channel": CHANNEL, "ts": PROBE_TS}


# T008 round 2 — 검사가 실패해도 지운다. 실패하는 상태가 바로 재시작 loop 이 도는 상태이고,
# 거기서 안 지우면 누적이 가장 빨리 쌓인다.
def test_the_probe_is_deleted_even_when_the_check_fails(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": "A_SOMEONE_ELSE"}),
    )

    outcome = _check(slack)

    assert outcome.primary_failure_code is ReadbackFailureCode.APP_ID_MISMATCH
    assert not outcome.allows_activation
    assert slack.requests[PROBE_DELETE].path == "/chat.delete"


# Slack success response의 channel+ts가 exact remote identity다. Configured target과 모순되면
# READY로 진행하지 않고, cleanup은 confirmed channel을 사용한다.
def test_a_mismatched_response_channel_fails_closed_and_cleans_the_confirmed_identity(
    slack: _FakeSlack,
) -> None:
    confirmed_channel = "C_CONFIRMED_OTHER"
    slack.queue(
        "chat.postMessage",
        _Reply.ok({"channel": confirmed_channel, "ts": PROBE_TS}),
    )

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.READBACK_FAILED
    assert outcome.primary_failure_code is ReadbackFailureCode.RESPONSE_CHANNEL_MISMATCH
    assert not outcome.allows_activation
    assert [request.path for request in slack.requests] == [
        "/chat.postMessage",
        "/chat.delete",
    ]
    assert slack.requests[1].json_body() == {
        "channel": confirmed_channel,
        "ts": PROBE_TS,
    }


# T008 AC-10 — readback 성공 뒤 cleanup만 실패하면 degraded로 진행한다.
def test_a_probe_that_cannot_be_deleted_returns_degraded(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": APP_ID}),
    )
    slack.queue("chat.delete", _Reply.slack_error("cant_delete_message"))

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.DEGRADED_CLEANUP
    assert outcome.allows_activation
    assert outcome.primary_failure_code is None
    assert outcome.secondary_cleanup_failure is None
    assert outcome.diagnostic_data.cleanup_failure_count == 1
    assert (
        outcome.diagnostic_data.provider_error_code
        is SafeSlackProviderErrorCode.CANT_DELETE_MESSAGE
    )


# T008 round 2 — 검사도 실패하고 지우기도 실패하면 **원래 원인이 이긴다.** 삭제 실패가
# 원인을 가리면 operator 가 엉뚱한 곳을 고친다.
def test_a_failed_delete_does_not_mask_the_readback_failure(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": "A_SOMEONE_ELSE"}),
    )
    slack.queue("chat.delete", _Reply.slack_error("cant_delete_message"))

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.READBACK_FAILED
    assert outcome.primary_failure_code is ReadbackFailureCode.APP_ID_MISMATCH
    assert outcome.secondary_cleanup_failure == CleanupFailureDetail(
        cleanup_failure_count=1,
        provider_error_code=SafeSlackProviderErrorCode.CANT_DELETE_MESSAGE,
    )
    assert not outcome.allows_activation


# T008 AC-12 — supplied durable cause는 post보다 먼저 판정한다. Outcome을 cause state로
# 재사용하지 않으며 실제 persistence/recovery는 T013 소유다.
@pytest.mark.parametrize("cause", list(ReadbackLifecycleCause))
def test_a_supplied_lifecycle_cause_hard_blocks_without_network(
    slack: _FakeSlack,
    cause: ReadbackLifecycleCause,
) -> None:
    outcome = _check(slack, lifecycle_cause=cause)

    assert outcome.status is ReadbackOutcomeStatus.HARD_BLOCKED_NO_POST
    assert outcome.lifecycle_cause is cause
    assert not outcome.allows_activation
    assert slack.requests == []
    assert "HARD_BLOCKED_NO_POST" not in {item.value for item in ReadbackLifecycleCause}


@pytest.mark.parametrize("cause", list(ReadbackLifecycleCause))
def test_a_lifecycle_short_circuit_never_exposes_unvalidated_marker_data(
    slack: _FakeSlack,
    cause: ReadbackLifecycleCause,
) -> None:
    canary = "xoxb-secret-canary-must-not-survive"
    marker = {
        "event_type": "not-approved",
        "event_payload": {
            **cast("Mapping[str, object]", PROBE_MARKER["event_payload"]),
            "event_id": canary,
        },
    }

    outcome = _check(slack, probe_marker=marker, lifecycle_cause=cause)

    assert outcome.status is ReadbackOutcomeStatus.HARD_BLOCKED_NO_POST
    assert outcome.diagnostic_data.probe_id is None
    assert canary not in repr(outcome)
    assert slack.requests == []


@pytest.mark.parametrize(
    "outcome",
    [
        ReadbackOutcome(
            status=ReadbackOutcomeStatus.READY,
            primary_failure_code=None,
            secondary_cleanup_failure=None,
            diagnostic_data=ReadbackDiagnosticData("READY", CHANNEL, APP_ID),
        ),
    ],
)
def test_a_valid_readback_outcome_constructs(outcome: ReadbackOutcome) -> None:
    assert outcome.allows_activation


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "status": ReadbackOutcomeStatus.READY,
            "primary_failure_code": ReadbackFailureCode.HISTORY_READ_FAILED,
            "secondary_cleanup_failure": None,
            "lifecycle_cause": None,
        },
        {
            "status": ReadbackOutcomeStatus.DEGRADED_CLEANUP,
            "primary_failure_code": None,
            "secondary_cleanup_failure": CleanupFailureDetail(1, None),
            "lifecycle_cause": None,
        },
        {
            "status": ReadbackOutcomeStatus.READBACK_FAILED,
            "primary_failure_code": None,
            "secondary_cleanup_failure": None,
            "lifecycle_cause": None,
        },
        {
            "status": ReadbackOutcomeStatus.READBACK_FAILED,
            "primary_failure_code": ReadbackFailureCode.PROBE_NOT_FOUND,
            "secondary_cleanup_failure": None,
            "lifecycle_cause": ReadbackLifecycleCause.AMBIGUOUS_POST,
        },
        {
            "status": ReadbackOutcomeStatus.HARD_BLOCKED_NO_POST,
            "primary_failure_code": None,
            "secondary_cleanup_failure": None,
            "lifecycle_cause": None,
        },
        {
            "status": ReadbackOutcomeStatus.HARD_BLOCKED_NO_POST,
            "primary_failure_code": ReadbackFailureCode.HISTORY_READ_FAILED,
            "secondary_cleanup_failure": None,
            "lifecycle_cause": ReadbackLifecycleCause.AMBIGUOUS_POST,
        },
    ],
)
def test_contradictory_readback_outcomes_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ReadbackOutcome(
            **kwargs,  # type: ignore[arg-type]
            diagnostic_data=ReadbackDiagnosticData("test", CHANNEL, APP_ID),
        )


# T008 AC-13 — 이전 probe를 찾는 history scan은 없다. Fresh check의 첫 network side effect는
# post이며, 이후 read는 Slack이 확인한 ts만 exact match한다.
def test_the_self_check_does_not_guess_previous_probes_from_history(slack: _FakeSlack) -> None:
    _queue_probe_flow(
        slack,
        _history_with(
            {"ts": "old", "metadata": PROBE_MARKER, "app_id": APP_ID},
            {"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": APP_ID},
        ),
    )

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.READY
    assert slack.requests[0].path == "/chat.postMessage"
    assert not hasattr(slack_http, "_reclaim_probes")
    assert [request.path for request in slack.requests] == [
        "/chat.postMessage",
        "/conversations.history",
        "/chat.delete",
    ]


def _readback_failure_reply(code: ReadbackFailureCode) -> _Reply:
    if code is ReadbackFailureCode.HISTORY_READ_FAILED:
        return _Reply.slack_error("missing_scope")
    if code is ReadbackFailureCode.PROBE_NOT_FOUND:
        return _history_with()
    if code is ReadbackFailureCode.APP_ID_MISMATCH:
        return _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": "A_SOMEONE_ELSE"})
    if code is ReadbackFailureCode.MARKER_UNREADABLE:
        return _history_with(
            {
                "ts": PROBE_TS,
                "metadata": {"event_type": PROBE_MARKER["event_type"]},
                "app_id": APP_ID,
            }
        )
    if code is ReadbackFailureCode.MARKER_MISMATCH:
        payload = dict(cast("Mapping[str, object]", PROBE_MARKER["event_payload"]))
        payload["payload_digest"] = "changed"
        return _history_with(
            {
                "ts": PROBE_TS,
                "metadata": {
                    "event_type": PROBE_MARKER["event_type"],
                    "event_payload": payload,
                },
                "app_id": APP_ID,
            }
        )
    raise AssertionError(f"post-readback failure가 아닙니다: {code}")


POST_READBACK_FAILURE_CODES = (
    ReadbackFailureCode.HISTORY_READ_FAILED,
    ReadbackFailureCode.PROBE_NOT_FOUND,
    ReadbackFailureCode.APP_ID_MISMATCH,
    ReadbackFailureCode.MARKER_UNREADABLE,
    ReadbackFailureCode.MARKER_MISMATCH,
    ReadbackFailureCode.RESPONSE_CHANNEL_MISMATCH,
)


def test_readback_failure_taxonomy_is_exact_and_the_matrix_is_exhaustive() -> None:
    expected = {
        ReadbackFailureCode.PROBE_INPUT_INVALID,
        ReadbackFailureCode.HISTORY_READ_FAILED,
        ReadbackFailureCode.PROBE_NOT_FOUND,
        ReadbackFailureCode.APP_ID_MISMATCH,
        ReadbackFailureCode.MARKER_UNREADABLE,
        ReadbackFailureCode.MARKER_MISMATCH,
        ReadbackFailureCode.RESPONSE_CHANNEL_MISMATCH,
    }

    assert set(ReadbackFailureCode) == expected
    assert set(POST_READBACK_FAILURE_CODES) == expected - {ReadbackFailureCode.PROBE_INPUT_INVALID}


# T008 AC-15·16 — post 뒤 여섯 failure x cleanup 성공/실패 12개 조합이다.
@pytest.mark.parametrize("primary_code", POST_READBACK_FAILURE_CODES)
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_post_readback_failure_matrix_preserves_the_primary_code(
    slack: _FakeSlack,
    primary_code: ReadbackFailureCode,
    cleanup_fails: bool,
) -> None:
    confirmed_channel = "C_CONFIRMED_OTHER"
    if primary_code is ReadbackFailureCode.RESPONSE_CHANNEL_MISMATCH:
        slack.queue(
            "chat.postMessage",
            _Reply.ok({"channel": confirmed_channel, "ts": PROBE_TS}),
        )
    else:
        _queue_probe_flow(slack, _readback_failure_reply(primary_code))
    if cleanup_fails:
        slack.queue("chat.delete", _Reply.slack_error("cant_delete_message"))

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.READBACK_FAILED
    assert outcome.primary_failure_code is primary_code
    assert not outcome.allows_activation
    assert (outcome.secondary_cleanup_failure is not None) is cleanup_fails
    if cleanup_fails:
        assert outcome.secondary_cleanup_failure == CleanupFailureDetail(
            cleanup_failure_count=1,
            provider_error_code=SafeSlackProviderErrorCode.CANT_DELETE_MESSAGE,
        )
    if primary_code is ReadbackFailureCode.RESPONSE_CHANNEL_MISMATCH:
        assert [request.path for request in slack.requests] == [
            "/chat.postMessage",
            "/chat.delete",
        ]
        assert slack.requests[1].json_body() == {
            "channel": confirmed_channel,
            "ts": PROBE_TS,
        }
    else:
        assert [request.path for request in slack.requests] == [
            "/chat.postMessage",
            "/conversations.history",
            "/chat.delete",
        ]


# T008 AC-14·16 — typed boundary 자체가 닫혀 있고 판정 계층은 아무것도 출력하지 않는다.
def test_diagnostic_data_has_only_the_approved_safe_fields_and_no_output(
    slack: _FakeSlack,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _queue_probe_flow(
        slack,
        _history_with({"ts": PROBE_TS, "metadata": PROBE_MARKER, "app_id": APP_ID}),
    )
    slack.queue("chat.delete", _Reply.slack_error("raw-secret=must-not-survive"))

    outcome = _check(slack)

    assert outcome.status is ReadbackOutcomeStatus.DEGRADED_CLEANUP
    assert {item.name for item in fields(ReadbackDiagnosticData)} == {
        "diagnostic_code",
        "channel_id",
        "app_id",
        "probe_id",
        "message_ts",
        "cleanup_failure_count",
        "provider_error_code",
        "operator_action",
    }
    assert {item.name for item in fields(CleanupFailureDetail)} == {
        "cleanup_failure_count",
        "provider_error_code",
    }
    assert outcome.diagnostic_data.provider_error_code is None
    assert "raw-secret" not in repr(outcome)
    assert caplog.records == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# T008 round 2 — probe marker 를 손으로 조립하지 않는다. round 1 의 P0 가 정확히 그 안내에서
# 나왔다. 안내문이 아니라 함수로 준다.
def test_the_probe_marker_helper_produces_the_isolated_ref() -> None:
    event = _probe_event()

    marker = build_probe_marker(event)

    payload = cast("Mapping[str, object]", marker["event_payload"])
    assert payload["destination_ref"] == PROBE_DESTINATION_REF
    # 나머지는 진짜와 같은 모양이다. 그래야 실제로 나가는 것을 검사한 것이 된다.
    assert payload["event_id"] == event.event_id
    assert payload["destination_sequence"] == event.destination_sequence
    assert payload["payload_digest"] == event.payload_digest
    assert marker["event_type"] == MARKER["event_type"]


# T008 round 2 — sentinel 은 진짜 destination_ref 와 **충돌할 수 없고 길이가 같다.**
#
# 충돌하면 격리가 무너진다. 길이가 다르면 probe 의 metadata 가 진짜보다 작아지고, metadata
# 크기 상한이 두 값 사이에 있으면 자가검사는 통과하는데 첫 진짜 Card 가 terminal 이 된다.
# 그 상한은 아직 모른다 (OQ-003).
def test_the_probe_ref_cannot_collide_with_a_real_one() -> None:
    real = _probe_event().destination_ref
    prefix, _, probe_suffix = PROBE_DESTINATION_REF.rpartition(":")
    _, _, real_suffix = real.rpartition(":")

    assert prefix == "provider:slack"
    # 진짜 접미는 sha256 hexdigest 다 (events.py:1442). hex 가 아니면 절대 안 겹친다.
    assert len(real_suffix) == 64
    assert all(char in "0123456789abcdef" for char in real_suffix)
    assert len(probe_suffix) == len(real_suffix)
    assert not any(char in "0123456789abcdef" for char in probe_suffix)


# --------------------------------------------------------------------------------------
# MGC-012-T009 — E2E 는 기본 실행에서 빠지고, credential 이 없으면 시끄럽게 skip 한다
# --------------------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]

# 이 이름을 subprocess 실행의 stdout 에서 찾아 선택·비선택을 확인한다. 이름을 바꾸면
# 아래 두 test 가 함께 깨진다 — 그것이 의도다. 조용히 안 도는 것보다 낫다.
E2E_TEST_NAME = "test_the_harness_hands_over_a_configured_target"

# 구성 이름은 production 이 소유한다. 여기서 다시 세면 두 벌이 어긋난다.
E2E_ENV_NAMES = SLACK_SETTINGS_ENV_NAMES

_ALL_FOUR: dict[str, str] = {
    SLACK_BOT_TOKEN_ENV: "xoxb-real-token",
    SLACK_SIGNING_SECRET_ENV: "0123456789abcdef",
    SLACK_APP_ID_ENV: APP_ID,
    SLACK_CHANNEL_ID_ENV: CHANNEL,
}


def _pytest_ini() -> Mapping[str, object]:
    parsed = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return cast(Mapping[str, object], parsed["tool"]["pytest"]["ini_options"])


def _child_environment(extra: Mapping[str, str] = MappingProxyType({})) -> dict[str, str]:
    """Build the child's environment. **Real Slack credentials never go in.**

    자식에게 진짜 credential 을 주지 않는 것이 유일하게 확실한 방법이다. `env=` 로 넘긴
    mapping 은 `subprocess.run` **자신의 local** (`kwargs`) 에 담기므로, 그것이
    `TimeoutExpired` 나 fork 실패를 던지면 `--showlocals` 가 그 안의 token 을 찍는다
    (wave 6 review round 2 P1, 실측). 부르는 쪽에서 local 을 없애는 것만으로는 안 닫힌다.

    그래서 네 변수를 **항상** 뺀다. 값이 필요한 test 는 `extra` 로 자기가 만든 가짜 값을
    넣는다 — 그러면 유출되더라도 진짜가 아니다.
    """
    environment = dict(os.environ)
    for name in E2E_ENV_NAMES:
        environment.pop(name, None)
    environment.update(extra)
    return environment


def _run_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    """Run pytest in a child process so the **real** ini options apply.

    marker 등록과 deselect 는 `pyproject.toml` 이 하는 일이라 in-process 로는 확인할 수
    없다. 실제로 그 설정이 도는지 보려면 그 설정을 읽는 pytest 를 한 번 더 띄워야 한다.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=REPO_ROOT,
        env=_child_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


@pytest.fixture
def slack_e2e_settings() -> SlackSettings:
    """Hand the E2E its configuration, or skip **with the reason printed**.

    **collection 이 아니라 setup 에서 부른다.** `skipif` 표현식에서 부르면 부분 구성 환경의
    기본 suite 가 collection error 로 빨개진다 (wave 5 contract review A-9).

    부분 구성은 skip 이 아니라 **실패**다 — 무엇을 빠뜨렸는지 모른 채 지나가는 것이 조용한
    pass 만큼 나쁘다 (contracts H-4.1).

    **그 실패를 `pytest.fail` 로 바꾸고 원래 traceback 을 버린다.** `ValueError` 를 그대로
    올리면 traceback 에 `os.environ` 을 인자로 받은 frame 이 남고, `--showlocals` 를 붙인
    실행이 그 frame 의 local 을 찍으면서 **환경 전체를 출력한다** — bot token 이 거기
    들어 있다 (wave 6 failure-recovery review P1). `pytest.fail` 을 `except` **밖에서**
    부르는 이유도 같다. 안에서 부르면 원래 예외가 context 로 함께 출력된다.
    """
    reason: str | None = None
    settings: SlackSettings | None = None
    try:
        settings = load_slack_settings(os.environ)
    except ValueError as error:
        # message 에는 변수 **이름**만 들어 있다. 값은 안 들어간다 (H-4.1).
        reason = str(error)
    if reason is not None:
        pytest.fail(reason)
    if settings is None:
        pytest.skip(
            "Slack E2E 미구성 — 다음 환경변수를 설정하십시오 (quickstart A-6): "
            + ", ".join(E2E_ENV_NAMES)
        )
    return settings


# T009 — 실제 호출은 T010 이 여기에 붙인다. 지금 확인하는 것은 harness 배선이다.
# 비어 있는 test 를 두지 않는 이유는 credential 이 **있는** 환경에서 통과가 아무것도
# 뜻하지 않게 되기 때문이다.
@pytest.mark.slack_e2e
def test_the_harness_hands_over_a_configured_target(slack_e2e_settings: SlackSettings) -> None:
    transport = HttpSlackTransport(
        bot_token=slack_e2e_settings.credentials.bot_token,
        timeout_seconds=1.0,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        lease_seconds=60,
    )

    # Protocol 적합성은 mypy 가 이 대입에서 본다. `SlackTransport` 는 runtime_checkable 이
    # 아니라 isinstance 로는 못 본다.
    live: SlackTransport = transport

    assert live is transport
    assert slack_e2e_settings.app_id
    assert slack_e2e_settings.channel_id


# --------------------------------------------------------------------------------------
# MGC-012-T010 — live Slack transport, readback, ordering, and human-delete recovery
# --------------------------------------------------------------------------------------

_LIVE_SLACK_TIMEOUT_SECONDS = 4.0
_LIVE_SLACK_LEASE_SECONDS = 60
_LIVE_SLACK_MAX_ATTEMPTS = 3
# Slack의 channel별 chat.postMessage 제한은 대략 초당 한 건이다. self-check와 두 Card를
# 연달아 보내는 test가 provider limit 자체를 검사하는 test로 변질되지 않게 간격을 둔다.
_LIVE_SLACK_POST_INTERVAL_SECONDS = 1.1


def _live_slack_transport(settings: SlackSettings) -> HttpSlackTransport:
    return HttpSlackTransport(
        bot_token=settings.credentials.bot_token,
        timeout_seconds=_LIVE_SLACK_TIMEOUT_SECONDS,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        lease_seconds=_LIVE_SLACK_LEASE_SECONDS,
    )


def _live_destination(
    settings: SlackSettings,
    transport: HttpSlackTransport,
    *,
    destination_ref: str,
) -> SlackProjectionDestination:
    # transport의 budget 검사에 쓴 page 수와 destination의 실제 scan page 수를 같은 값으로
    # 명시한다. 둘이 갈라지면 lease 안에서 끝난다는 보장이 사라진다.
    assert transport.max_history_pages == SLACK_MAX_HISTORY_PAGES
    return SlackProjectionDestination(
        transport,
        destination_ref=destination_ref,
        channel=settings.channel_id,
        app_id=settings.app_id,
        max_attempts=_LIVE_SLACK_MAX_ATTEMPTS,
        max_history_pages=transport.max_history_pages,
    )


def _live_destination_ref(run_id: str) -> str:
    # run마다 다른 destination을 쓴다. 이전 test run의 lower-sequence marker가 이번
    # human-delete reconcile의 결론에 영향을 주지 않게 하는 격리 경계다.
    suffix = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return f"provider:slack:{suffix}"


def _live_event(
    *,
    run_id: str,
    destination_ref: str,
    destination_sequence: int,
) -> OutboxEventView:
    payload = DecisionProjectionPayload(
        action="approve",
        active_definition_digest="sha256:" + "1" * 64,
        aggregate_ref=ProposalRef(
            project_ref=ProjectRef(
                project_id="amplai",
                namespace="org/default/project/amplai",
            ),
            proposal_id=f"PROP-20260812-{run_id[:8].upper()}",
        ),
        content_revision=destination_sequence,
        decision_epoch=1,
        proposal_status="approved",
        state_revision=destination_sequence + 1,
    ).model_dump(mode="json")
    return OutboxEventView(
        event_id=f"EVT-{run_id.upper()}-{destination_sequence}",
        proposal_ref=ProposalRef(
            project_ref=ProjectRef(project_id="amplai", namespace="org/default/project/amplai"),
            proposal_id="PROP-20260730-ABCDEF12",
        ),
        aggregate_sequence=destination_sequence,
        destination_ref=destination_ref,
        destination_sequence=destination_sequence,
        source_state_revision=destination_sequence,
        payload_digest=payload_digest(payload),
        payload=payload,
        state=OutboxState.LEASED,
        attempts=1,
        claim_generation=1,
        created_at=datetime.now(UTC),
    )


class _LiveReviewActionSets:
    def __init__(self, prepared: PreparedReviewActionSet) -> None:
        self.prepared = prepared

    def prepare(self, event: OutboxEventView) -> PreparedReviewActionSet:
        return self.prepared

    def abandon(self, event_id: str, generation: int) -> None:
        return None


def _live_review_event(
    *,
    settings: SlackSettings,
    run_id: str,
    destination_ref: str,
) -> tuple[OutboxEventView, PreparedReviewActionSet]:
    now = datetime.now(UTC)
    expires_at = now.replace(microsecond=0) + timedelta(hours=24)
    proposal = ProposalRef(
        project_ref=ProjectRef(
            project_id="amplai",
            namespace="org/default/project/amplai",
        ),
        proposal_id=f"PROP-20260812-{run_id[:8].upper()}",
    )
    channel = ChannelRef(
        provider=ChannelProvider.SLACK,
        workspace_id="AMPLAI-LIVE-E2E",
        channel_id=settings.channel_id,
        message_id=f"e2e-request-{run_id[:8]}",
    )
    payload_model = ReviewProjectionPayload(
        aggregate_ref=proposal,
        active_definition_digest="sha256:" + "2" * 64,
        content_revision=1,
        state_revision=2,
        decision_epoch=1,
        reviewer_actor_id="ACT-LIVE-E2E-REVIEWER",
        reviewer_external_key=settings.app_id,
        bound_channel_ref=channel,
        expires_at=expires_at,
        operation_count=4,
        operation_counts={"CREATE": 2, "IGNORE": 2},
        operation_titles=("Create Proposal Card UI", "Keep governed decision", "Add E2E"),
        remaining_operation_count=1,
    )
    actor = ActorRef(actor_id=payload_model.reviewer_actor_id, actor_type=ActorType.HUMAN)
    token_specs = (
        (DecisionAction.APPROVE, "A", "a" * 32),
        (DecisionAction.REQUEST_CHANGES, "B", "b" * 32),
        (DecisionAction.REJECT, "C", "c" * 32),
    )
    issued = tuple(
        IssuedActionToken(
            record=ActionTokenView(
                token_id=f"TOK-{suffix * 16}",
                proposal_ref=proposal,
                active_definition_digest=payload_model.active_definition_digest,
                content_revision=payload_model.content_revision,
                state_revision=payload_model.state_revision,
                decision_epoch=payload_model.decision_epoch,
                allowed_action=action,
                allowed_actor_ref=actor,
                bound_channel_ref=channel,
                issued_at=now,
                expires_at=expires_at,
                state=ActionTokenState.ISSUED,
            ),
            raw_token=raw,
        )
        for action, suffix, raw in token_specs
    )
    event_id = f"EVT-{run_id.upper()}-REVIEW"
    prepared = PreparedReviewActionSet(
        view=ReviewActionSetView(
            event_id=event_id,
            generation=1,
            approve_token_id=issued[0].record.token_id,
            request_changes_token_id=issued[1].record.token_id,
            reject_token_id=issued[2].record.token_id,
            state="issued",
            issued_at=now,
            expires_at=expires_at,
        ),
        issued=issued,
    )
    payload = payload_model.model_dump(mode="json")
    event = OutboxEventView(
        event_id=event_id,
        proposal_ref=proposal,
        aggregate_sequence=1,
        destination_ref=destination_ref,
        destination_sequence=1,
        source_state_revision=2,
        payload_digest=payload_digest(payload),
        payload=payload,
        state=OutboxState.LEASED,
        attempts=1,
        claim_generation=1,
        created_at=now,
    )
    return event, prepared


def _receipt_ts(receipt: str, *, channel: str) -> str:
    prefix = f"slack:{channel}:"
    assert receipt.startswith(prefix)
    timestamp = receipt.removeprefix(prefix)
    assert timestamp
    return timestamp


def _wait_for_next_slack_post() -> None:
    time.sleep(_LIVE_SLACK_POST_INTERVAL_SECONDS)


@pytest.mark.slack_e2e
def test_live_result_card_send_reconcile_metadata_and_newest_first(
    slack_e2e_settings: SlackSettings,
) -> None:
    """Exercise the configured Slack app without printing any credential value."""
    run_id = uuid4().hex
    destination_ref = _live_destination_ref(run_id)
    first = _live_event(
        run_id=run_id,
        destination_ref=destination_ref,
        destination_sequence=1,
    )
    second = _live_event(
        run_id=run_id,
        destination_ref=destination_ref,
        destination_sequence=2,
    )
    transport = _live_slack_transport(slack_e2e_settings)
    destination = _live_destination(
        slack_e2e_settings,
        transport,
        destination_ref=destination_ref,
    )

    # 실제 Card와 같은 marker 모양이 app_id를 포함해 되읽히는지 먼저 확인한다. probe는
    # transport가 지우며 Card cleanup에는 이 API를 사용하지 않는다.
    readback = verify_marker_readback(
        transport,
        channel=slack_e2e_settings.channel_id,
        app_id=slack_e2e_settings.app_id,
        probe_marker=build_probe_marker(first),
    )
    assert readback.status is ReadbackOutcomeStatus.READY
    _wait_for_next_slack_post()

    first_receipt = destination.send(first)
    first_ts = _receipt_ts(first_receipt, channel=slack_e2e_settings.channel_id)

    # 재시도 event만 history를 읽는다. 같은 logical event가 같은 remote receipt로 복원되어
    # 두 번째 Card가 생기지 않는 것이 핵심 계약이다.
    retried_first = first.model_copy(update={"attempts": 2})
    assert destination.reconcile(retried_first) == first_receipt

    first_page = transport.read_history(
        channel=slack_e2e_settings.channel_id,
        cursor=None,
        limit=999,
    )
    first_message = next(message for message in first_page.messages if message.ts == first_ts)
    assert first_message.app_id == slack_e2e_settings.app_id
    assert first_message.metadata is not None
    assert dict(first_message.metadata) == build_slack_marker(first)

    _wait_for_next_slack_post()
    second_receipt = destination.send(second)
    second_ts = _receipt_ts(second_receipt, channel=slack_e2e_settings.channel_id)

    newest_page = transport.read_history(
        channel=slack_e2e_settings.channel_id,
        cursor=None,
        limit=999,
    )
    timestamps = [message.ts for message in newest_page.messages]
    assert timestamps.index(second_ts) < timestamps.index(first_ts)


@pytest.mark.slack_e2e
def test_live_review_card_send_and_reconcile_marker(
    slack_e2e_settings: SlackSettings,
) -> None:
    run_id = uuid4().hex
    destination_ref = _live_destination_ref(run_id)
    event, prepared = _live_review_event(
        settings=slack_e2e_settings,
        run_id=run_id,
        destination_ref=destination_ref,
    )
    transport = _live_slack_transport(slack_e2e_settings)
    destination = SlackProjectionDestination(
        transport,
        destination_ref=destination_ref,
        channel=slack_e2e_settings.channel_id,
        app_id=slack_e2e_settings.app_id,
        max_attempts=_LIVE_SLACK_MAX_ATTEMPTS,
        max_history_pages=transport.max_history_pages,
        review_action_sets=cast(
            ReviewActionSetService,
            _LiveReviewActionSets(prepared),
        ),
    )

    _wait_for_next_slack_post()
    receipt = destination.send(event)
    timestamp = _receipt_ts(receipt, channel=slack_e2e_settings.channel_id)
    retried = event.model_copy(update={"attempts": 2})
    assert destination.reconcile(retried) == receipt

    page = transport.read_history(
        channel=slack_e2e_settings.channel_id,
        cursor=None,
        limit=999,
    )
    message = next(candidate for candidate in page.messages if candidate.ts == timestamp)
    assert message.app_id == slack_e2e_settings.app_id
    assert message.metadata is not None
    assert dict(message.metadata) == build_slack_marker(event)


@pytest.mark.slack_e2e
def test_a_human_deleted_card_reconciles_as_unsent(
    slack_e2e_settings: SlackSettings,
) -> None:
    """Interactive AC-05; run with ``pytest -s`` and delete the named Card in Slack."""
    if not sys.stdin.isatty():
        pytest.skip(
            "수동 Slack 삭제 검증 — -s와 이 test 이름으로 실행하고 안내된 Card를 삭제해야 합니다."
        )

    run_id = uuid4().hex
    destination_ref = _live_destination_ref(run_id)
    event = _live_event(
        run_id=run_id,
        destination_ref=destination_ref,
        destination_sequence=1,
    )
    transport = _live_slack_transport(slack_e2e_settings)
    destination = _live_destination(
        slack_e2e_settings,
        transport,
        destination_ref=destination_ref,
    )

    original_receipt = destination.send(event)
    original_ts = _receipt_ts(original_receipt, channel=slack_e2e_settings.channel_id)
    input(
        "Slack에서 방금 전송한 Proposal approved Result Card "
        f"({run_id[:8]})를 직접 삭제한 뒤 Enter를 누르십시오: "
    )

    retried = event.model_copy(update={"attempts": 2})
    assert destination.reconcile(retried) is None
    _wait_for_next_slack_post()

    replacement_receipt = destination.send(retried)
    replacement_ts = _receipt_ts(replacement_receipt, channel=slack_e2e_settings.channel_id)
    assert replacement_ts != original_ts
    assert destination.reconcile(retried) == replacement_receipt

    page = transport.read_history(
        channel=slack_e2e_settings.channel_id,
        cursor=None,
        limit=999,
    )
    expected_marker = build_slack_marker(event)
    remaining = [
        message
        for message in page.messages
        if message.app_id == slack_e2e_settings.app_id
        and message.metadata is not None
        and dict(message.metadata) == expected_marker
    ]
    assert [message.ts for message in remaining] == [replacement_ts]


# T009 AC-04 — marker 가 등록돼 있어야 unknown mark 경고가 안 나고, 오타 난 marker 가
# 조용히 아무것도 선택하지 않는 상태를 막는다.
def test_the_e2e_marker_is_registered() -> None:
    markers = _pytest_ini()["markers"]

    assert isinstance(markers, list)
    assert any(entry.startswith("slack_e2e:") for entry in markers)


# T009 AC-02 — 기본 실행이 E2E 를 고르지 않는다는 것을 ini 설정으로 고정한다.
#
# **정확 일치로 본다.** 부분 문자열로 보면 `not slack_e2ee` 같은 오타가 이 검사를 통과한다
# (regression lens M21). 그 오타는 아무것도 deselect 하지 않는다.
def test_the_default_options_deselect_the_marker() -> None:
    assert '-m "not slack_e2e"' in str(_pytest_ini()["addopts"])


# W2-pytest-import-mode — tests/ 를 sys.path 에 넣는 것을 import mode 의 부수효과가 아니라
# 명시 설정으로 만든다. test_slack_projection.py 가 test_governance_events 를 top-level 로
# import 하고, 그 의존이 조용히 깨지면 collection error 로만 보인다.
def test_the_tests_directory_is_on_the_configured_path() -> None:
    assert _pytest_ini()["pythonpath"] == ["tests"]


# T009 AC-02·AC-04 — 설정이 아니라 **실제 실행**으로 확인한다. 인자 없이 돌린 pytest 가
# E2E 를 고르지 않고, unknown mark 경고도 내지 않는다.
def test_the_default_run_does_not_collect_the_e2e_test() -> None:
    # `-q` 를 더 주지 않는다. addopts 에 이미 있어서 `-qq` 가 되면 test id 가 아니라 개수만
    # 나오고, 그러면 이 test 가 아무것도 확인하지 못한다.
    completed = _run_pytest("tests/test_slack_http.py", "--collect-only")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert E2E_TEST_NAME not in completed.stdout
    assert "deselected" in completed.stdout
    assert "PytestUnknownMarkWarning" not in completed.stdout + completed.stderr


# T009 — 명령줄의 `-m` 이 addopts 를 이긴다. 이기지 않으면 E2E 를 돌릴 방법이 없다.
def test_selecting_the_marker_collects_the_e2e_test() -> None:
    completed = _run_pytest("tests/test_slack_http.py", "--collect-only", "-m", "slack_e2e")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert E2E_TEST_NAME in completed.stdout


# T009 AC-01 — credential 이 없으면 **skip 이고 pass 가 아니다.** 조용한 통과는
# Constitution III 위반이고 gate 기록을 거짓으로 만든다 (contracts H-5.2).
def test_missing_credentials_report_as_a_skip_with_a_reason() -> None:
    completed = _run_pytest(
        "tests/test_slack_http.py",
        "-m",
        "slack_e2e",
        "-rs",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "skipped" in completed.stdout
    assert "passed" not in completed.stdout
    assert SLACK_BOT_TOKEN_ENV in completed.stdout
    assert SLACK_CHANNEL_ID_ENV in completed.stdout


# T009 round 2 — **자식은 진짜 credential 을 물려받지 않는다.**
#
# `env=` 로 넘긴 mapping 은 `subprocess.run` 자신의 local 에 담기므로 그것이 던지면
# `--showlocals` 가 그 안의 token 을 찍는다. 부르는 쪽 local 만 없애서는 안 닫힌다.
# 자식에게 진짜 값을 안 주는 것이 유일하게 확실한 방법이다 (wave 6 review round 2 P1).
def test_the_child_process_never_inherits_real_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "canary-must-not-reach-the-child"
    for name in E2E_ENV_NAMES:
        monkeypatch.setenv(name, canary)

    completed = _run_pytest("tests/test_slack_http.py", "-m", "slack_e2e", "-rs")

    # 물려받았다면 자식이 구성됨으로 보고 E2E 를 **돌린다** — skip 이 아니라 pass 다.
    assert "skipped" in completed.stdout
    assert "passed" not in completed.stdout
    assert canary not in completed.stdout + completed.stderr


# T009 round 2 — **부분 구성 실행이 token 을 출력하지 않는다.**
#
# fixture 가 `os.environ` 을 loader 에 넘기는 repo 유일 경로다. 그 경로에서 예외가 그대로
# 올라가면 `--showlocals` 가 환경 전체를 찍고 token 이 CI log 로 나간다 (wave 6
# failure-recovery review P1). `-l` 을 **일부러 붙여서** 본다.
def test_a_partial_configuration_fails_without_printing_the_token() -> None:
    canary = "xoxb-LEAK-CANARY-must-not-appear"

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_slack_http.py", "-m", "slack_e2e", "-l"],
        cwd=REPO_ROOT,
        env=_child_environment({SLACK_BOT_TOKEN_ENV: canary, SLACK_CHANNEL_ID_ENV: CHANNEL}),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    output = completed.stdout + completed.stderr
    # 실패는 해야 한다. 조용히 skip 되면 뭘 빠뜨렸는지 모른다.
    assert completed.returncode != 0, output
    assert canary not in output
    # 대신 빠진 이름은 전부 보인다.
    assert SLACK_SIGNING_SECRET_ENV in output
    assert SLACK_APP_ID_ENV in output


# T009 AC-03 / FR-020 — verify 는 7 stage 이고 network 를 부르는 stage 가 없다. E2E 를
# 여기 넣으면 clean clone·offline 계약이 깨진다 (SC-014).
def test_verify_has_no_network_stage() -> None:
    commands = VerificationRunner().commands()
    names = [name for name, _ in commands]

    assert len(names) == 7
    assert not [name for name in names if "slack" in name or "e2e" in name]
    pytest_command = next(command for name, command in commands if name == "pytest")
    # `-m pytest` 뒤에 marker 선택이 붙지 않는다. 붙으면 verify 가 network 를 요구하게 된다.
    assert list(pytest_command[3:]) == []


# T009 — 넷이 다 있으면 하나로 모인다. secret 은 계속 SecretStr 이다.
def test_all_four_variables_assemble_into_settings() -> None:
    settings = load_slack_settings(_ALL_FOUR)

    assert settings is not None
    assert settings.credentials.bot_token.get_secret_value() == "xoxb-real-token"
    assert settings.app_id == APP_ID
    assert settings.channel_id == CHANNEL


# T009 — 부재와 빈 문자열이 같은 "구성 안 됨" 이다. 다르면 빈 변수를 export 한 환경이
# skip 이 아니라 오류로 떨어진다.
@pytest.mark.parametrize(
    "environ",
    [
        {},
        dict.fromkeys(E2E_ENV_NAMES, ""),
        dict.fromkeys(E2E_ENV_NAMES, "  \n"),
    ],
)
def test_an_unconfigured_environment_has_no_settings(environ: dict[str, str]) -> None:
    assert load_slack_settings(environ) is None


# T009 — 부분 구성은 미구성이 아니라 실수다. 빠진 **이름을 전부** 알려준다.
#
# round 2 에서 아래 셋을 더했다. round 1 의 다섯은 credential 이 둘 다 있거나 둘 다 없는
# 조합뿐이라, credential 이 **부분**일 때 대상 누락이 message 에서 빠지는 결함을 못 잡았다
# (wave 6 contract·failure lens 가 독립으로 잡았다).
@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        (
            {name: value for name, value in _ALL_FOUR.items() if name != SLACK_APP_ID_ENV},
            (SLACK_APP_ID_ENV,),
        ),
        (
            {name: value for name, value in _ALL_FOUR.items() if name != SLACK_CHANNEL_ID_ENV},
            (SLACK_CHANNEL_ID_ENV,),
        ),
        (
            {SLACK_APP_ID_ENV: APP_ID, SLACK_CHANNEL_ID_ENV: CHANNEL},
            (SLACK_BOT_TOKEN_ENV, SLACK_SIGNING_SECRET_ENV),
        ),
        (
            {SLACK_CHANNEL_ID_ENV: CHANNEL},
            (SLACK_BOT_TOKEN_ENV, SLACK_SIGNING_SECRET_ENV, SLACK_APP_ID_ENV),
        ),
        (
            {**_ALL_FOUR, SLACK_APP_ID_ENV: "   "},
            (SLACK_APP_ID_ENV,),
        ),
        # round 2 — token 만 넣은 사람. 빠진 셋을 한 번에 봐야 한다.
        (
            {SLACK_BOT_TOKEN_ENV: "xoxb-real-token"},
            (SLACK_SIGNING_SECRET_ENV, SLACK_APP_ID_ENV, SLACK_CHANNEL_ID_ENV),
        ),
        # round 2 — token 과 channel 만. credential 이 부분이면서 대상도 부분인 경우다.
        (
            {SLACK_BOT_TOKEN_ENV: "xoxb-real-token", SLACK_CHANNEL_ID_ENV: CHANNEL},
            (SLACK_SIGNING_SECRET_ENV, SLACK_APP_ID_ENV),
        ),
        # round 2 — secret 만.
        (
            {SLACK_SIGNING_SECRET_ENV: "0123456789abcdef"},
            (SLACK_BOT_TOKEN_ENV, SLACK_APP_ID_ENV, SLACK_CHANNEL_ID_ENV),
        ),
    ],
)
def test_partial_settings_name_every_missing_variable(
    environ: dict[str, str], expected: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match="불완전") as caught:
        load_slack_settings(environ)

    for name in expected:
        assert name in str(caught.value)


# T009 — 실수 message 에 값이 들어가면 안 된다. 이름만 적는다.
#
# round 2 에서 입력을 넓혔다. round 1 은 `{token}` 하나만 줬는데 그 입력은
# `load_slack_credentials` 에서 먼저 터져서 **T009 가 새로 만든 raise 를 한 번도 안 탔다.**
# 그 자리에 token 을 흘리는 mutation 두 종이 살아남았다 (regression lens M30·M41).
@pytest.mark.parametrize(
    "environ",
    [
        {SLACK_BOT_TOKEN_ENV: "xoxb-real-token"},
        {**_ALL_FOUR, SLACK_APP_ID_ENV: ""},
        {**_ALL_FOUR, SLACK_SIGNING_SECRET_ENV: ""},
        {SLACK_BOT_TOKEN_ENV: "xoxb-real-token", SLACK_CHANNEL_ID_ENV: CHANNEL},
    ],
)
def test_the_settings_error_names_variables_not_values(environ: dict[str, str]) -> None:
    with pytest.raises(ValueError) as caught:
        load_slack_settings(environ)

    assert "xoxb-real-token" not in str(caught.value)
    assert "0123456789abcdef" not in str(caught.value)


# T009 — settings 도 secret 을 찍지 않는다. 한 겹 감싸면 repr 이 되살아나는 것이 흔하다.
def test_settings_never_render_their_secrets() -> None:
    settings = load_slack_settings(_ALL_FOUR)
    assert settings is not None

    for rendered in (repr(settings), str(settings), f"{settings}"):
        assert "xoxb-real-token" not in rendered
        assert "0123456789abcdef" not in rendered


# T009 — loader 를 우회해 빈 대상으로 만들 수도 없어야 한다.
@pytest.mark.parametrize(
    ("app_id", "channel_id"), [("", "C1"), ("  ", "C1"), ("A1", ""), ("A1", " ")]
)
def test_settings_reject_blank_targets(app_id: str, channel_id: str) -> None:
    with pytest.raises(ValueError):
        SlackSettings(
            credentials=SlackCredentials(bot_token=TOKEN, signing_secret=SecretStr("s")),
            app_id=app_id,
            channel_id=channel_id,
        )


# --------------------------------------------------------------------------------------
# MGC-012-T011 — 실제 transport 로 돌아도 다른 Provider 는 그대로다 (A14, FR-021)
# --------------------------------------------------------------------------------------

TELEGRAM_CHANNEL = ChannelRef(
    provider=ChannelProvider.TELEGRAM,
    chat_id="-1001234567890",
    message_id="4242",
)

# dispatcher 와 transport 가 **같은 lease 숫자**를 본다. 다르면 예산 검증이 실제로 쥐는
# lease 가 아닌 값을 검사해 아무것도 막지 못한다 (contracts C-1 의무 2).
_LEASE_SECONDS = 5

# 2 x timeout x (pages + 1) 이 lease 안에 들어와야 transport 를 만들 수 있다
# (`validate_call_budget`). 5초 lease 에 6회분이면 timeout 은 0.4초 미만이어야 한다.
_E2E_TIMEOUT_SECONDS = 0.2


def _destination_row(store: GovernanceStore, destination_ref: str) -> tuple[object, ...]:
    with store.connect() as connection:
        row = connection.execute(
            "SELECT next_sequence, delivered_sequence, operator_hold, updated_at "
            "FROM governance_outbox_destinations WHERE destination_ref = ?",
            (destination_ref,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _terminal_rows(store: GovernanceStore) -> tuple[list[str], list[str]]:
    with store.connect() as connection:
        dead = [
            str(row[0])
            for row in connection.execute(
                "SELECT error_code FROM governance_outbox_dead_letters"
            ).fetchall()
        ]
        holds = [
            str(row[0])
            for row in connection.execute(
                "SELECT reason_code FROM governance_operator_holds"
            ).fetchall()
        ]
    return dead, holds


def _hold_scopes(store: GovernanceStore) -> list[tuple[str, str]]:
    """Read **which destination** each hold landed on, not just why.

    hold 는 되돌릴 수 없고 그 destination 의 이후 event 를 전부 멈춘다. 원인 code 만 보면
    엉뚱한 destination 이 멈춘 것을 못 잡는다.
    """
    with store.connect() as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT scope_kind, scope_ref FROM governance_operator_holds"
            ).fetchall()
        ]


# T011 AC-01·AC-02·AC-03 / FR-021 / A14 — Package 3 의 T005 AC-07 이 같은 격리를 fake
# transport 로 확인했다. 여기서 다른 것은 **실패가 만들어지는 경로**다.
#
# T005 는 `SlackTransportError` 를 손으로 만들어 주입한다. 그러면 응답 판별과 code 추출
# (`slack_http.py` 의 `_slack_error_code`·`_decode`) 을 안 탄다. 여기서는 fake server 가
# 돌려준 `ok:false` JSON 에서 그 예외가 **생성**되고, 그 뒤 분류·DLQ·hold 까지 간다.
# 전송 계층이 실물로 바뀌어도 hold 가 Slack destination 밖으로 안 번지는지가 질문이다.
def test_the_real_transport_leaves_another_provider_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slack: _FakeSlack,
) -> None:
    store, _active, _view = governance_fixtures._active_proposal(tmp_path)
    clock = governance_fixtures.MutableClock()
    events = GovernanceEventService(store, clock=clock)
    # Telegram destination 을 production 파생 규칙이 만들게 한다. row 를 손으로 넣으면
    # 실제로는 생기지 않는 모양을 검사하게 된다 (T005 선례).
    monkeypatch.setattr(governance_fixtures, "CHANNEL", TELEGRAM_CHANNEL)
    _telegram_audit, telegram_outbox = governance_fixtures._append(
        events,
        store,
        command_id="command-telegram",
        state_revision=2,
    )
    monkeypatch.undo()
    # Slack 쪽은 둘이다. 하나로는 성공 뒤에 claim 할 event 가 없어 실패 경로를 못 돈다.
    slack_batches = [
        governance_fixtures._append(
            events,
            store,
            command_id=f"command-slack-{index}",
            state_revision=index + 2,
        )[1]
        for index in (1, 2)
    ]
    telegram = next(event for event in telegram_outbox if event.supersession_key is not None)
    slack_event = next(event for event in slack_batches[0] if event.supersession_key is not None)
    assert telegram.destination_ref.startswith("provider:telegram:")
    assert slack_event.destination_ref.startswith("provider:slack:")
    before = _destination_row(store, telegram.destination_ref)

    # 첫 전송은 성공, 두 번째는 wire 에서 온 terminal 오류다. Slack 은 application error 를
    # HTTP 200 + ok:false 로 준다.
    slack.queue(
        "chat.postMessage",
        _Reply.ok({"channel": CHANNEL, "ts": "1700000000.000100"}),
        _Reply.slack_error("invalid_auth"),
    )
    transport = HttpSlackTransport(
        bot_token=TOKEN,
        timeout_seconds=_E2E_TIMEOUT_SECONDS,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        lease_seconds=_LEASE_SECONDS,
        base_url=slack.base_url,
    )
    destination = SlackProjectionDestination(
        transport,
        destination_ref=slack_event.destination_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=OutboxConfig().max_attempts,
    )
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(lease_seconds=_LEASE_SECONDS),
        clock=clock,
    )
    # 배선이 선언한 예산과 실제로 읽는 page 수가 같다. 다르면 예산 검증이 실제로 안 도는
    # 숫자를 검사하고, 넘겨도 dead letter 도 hold 도 안 남는다 (wave 6 failure lens P1).
    assert transport.max_history_pages == destination.max_history_pages
    assert transport.lease_seconds == _LEASE_SECONDS

    delivered = dispatcher.deliver_next("worker", destination)
    assert delivered is not None and delivered.state is OutboxState.DELIVERED

    # 성공만 보면 부족하다. hold 는 destination 전체를 멈추므로 번지면 피해가 크다.
    failed = dispatcher.deliver_next("worker", destination)
    assert failed is not None and failed.state is OutboxState.DEAD_LETTER

    # 두 호출 다 실제 HTTP 로 나갔다. 이것이 없으면 fake transport 로도 통과하는 test 다.
    assert [record.path for record in slack.requests] == [
        "/chat.postMessage",
        "/chat.postMessage",
    ]
    dead, holds = _terminal_rows(store)
    # code 에 wire 에서 온 `invalid_auth` 가 실려 있다. 손으로 만든 예외가 아니라 응답
    # 판별을 거쳐 나온 것이라는 증거다.
    assert dead == ["SLACK_PROJECTION_TERMINAL_ERROR:invalid_auth"]
    assert holds == ["SLACK_PROJECTION_TERMINAL_ERROR:invalid_auth"]

    # AC-01 — Telegram destination row 가 통째로 그대로다.
    assert _destination_row(store, telegram.destination_ref) == before
    assert dispatcher.get(telegram.event_id).state is OutboxState.PENDING
    # AC-02 — hold 가 Slack destination 에만 걸렸다.
    assert _hold_scopes(store) == [("outbox_destination", slack_event.destination_ref)]
    # AC-03 — Telegram 은 계속 자기 event 를 가져간다.
    claimed = dispatcher.claim_next("telegram-worker", destination_ref=telegram.destination_ref)
    assert claimed is not None and claimed.event_id == telegram.event_id


# T011 round 2 — transport 가 **선언한 예산을 보관한다.** 검증에만 쓰고 버리면 그 숫자가
# 실제로 읽는 page 수와 묶이지 않는다. `pages=1` 로 통과시킨 transport 를 기본 destination
# (5 page) 에 물리면 실제 최악이 lease 를 넘고, 그 결과는 `validate_call_budget` 자신이 적은
# 대로 dead letter 도 hold 도 안 남는다 (wave 6 failure-recovery review P1).
#
# destination constructor와 dispatcher pre-claim hook이 이 값을 실제 runtime 구성과 대조한다.
# 여기서는 transport가 대조 가능한 선언을 보존하는 절반을 고정한다.
def test_the_transport_keeps_the_budget_it_declared() -> None:
    transport = HttpSlackTransport(
        bot_token=TOKEN,
        timeout_seconds=0.2,
        max_history_pages=3,
        lease_seconds=_LEASE_SECONDS,
    )

    assert transport.max_history_pages == 3
    assert transport.lease_seconds == _LEASE_SECONDS
    # 보관한 값으로 최악 예산을 다시 셀 수 있다. 셀 수 없으면 대조가 불가능하다.
    assert worst_case_call_seconds(0.2, transport.max_history_pages) < _LEASE_SECONDS
