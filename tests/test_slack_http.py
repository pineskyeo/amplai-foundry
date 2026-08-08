from __future__ import annotations

import ast
import inspect
import json
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import SecretStr

from amplai_foundry.governance import slack_http
from amplai_foundry.governance.slack_http import (
    SLACK_API_BASE,
    SLACK_BOT_TOKEN_ENV,
    SLACK_SIGNING_SECRET_ENV,
    HttpSlackTransport,
    SlackCredentials,
    load_slack_credentials,
    validate_call_budget,
    worst_case_call_seconds,
)
from amplai_foundry.governance.slack_projection import (
    SLACK_MAX_HISTORY_PAGES,
    SlackFailureClass,
    SlackTransport,
    SlackTransportError,
    classify_slack_failure,
)

MODULE_PATH = Path(inspect.getfile(slack_http))
TOKEN = SecretStr("xoxb-test-token")
CHANNEL = "C0SLACK01"
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
            def do_POST(self) -> None:
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
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

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
@pytest.mark.parametrize("payload", [{"ts": "1.0"}, {"channel": CHANNEL}, {}])
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
    [(0.0, 5, 30), (-1.0, 5, 30), (5.0, 0, 30), (5.0, 5, 0)],
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
    for path in Path("src/amplai_foundry").rglob("*.py"):
        if path.name == "slack_http.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "SLACK" in text and ("os.environ" in text or "from os import environ" in text):
            offenders.append(str(path))
    assert offenders == []
