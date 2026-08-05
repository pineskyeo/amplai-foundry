"""Injected Slack transport contract and fail-closed failure classification.

이 module 은 Slack 을 실제로 호출하지 않는다. `SlackProjectionDestination` 이 부를
transport 를 Protocol 로만 정의한다. 실제 HTTP 구현은 Package 4 가 맡는다
(`docs/workstreams/messenger-governance-closure-v3/DECISIONS.md` D-018 항목 2).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, NoReturn, Protocol

from amplai_foundry.governance.events import OutboxEventView, OutboxReconcileError

# Slack message metadata 의 event_type. 한 번 정하면 바꾸지 않는다 — 바꾸면 이전에 나간
# marker 를 reconcile 이 못 읽는다 (task manifest MGC-012-T001 invariants, OQ-002).
SLACK_PROJECTION_EVENT_TYPE: Final = "amplai_proposal_card"

# 재시도 가능한 Slack error code. allowlist 다 — 목록에 없는 code 는 terminal 로 떨어진다.
# 근거: research.md R-006, D-016 (event error 는 terminal 이 기본값).
RETRYABLE_SLACK_ERROR_CODES: Final = frozenset(
    {
        "fatal_error",
        "internal_error",
        "rate_limited",
        "ratelimited",
        "request_timeout",
        "service_unavailable",
    }
)

_TERMINAL_ERROR_CODE: Final = "SLACK_PROJECTION_TERMINAL_ERROR"
_RETRY_EXHAUSTED_ERROR_CODE: Final = "SLACK_PROJECTION_RETRY_EXHAUSTED"
_RATE_LIMIT_STATUS: Final = 429

# 저장될 접미사의 형식. **분류에는 쓰지 않는다.** terminal code 는 dead letter 와 operator
# hold 에 그대로 남고 두 table 은 append-only 라 지워지지도 고쳐지지도 않는다. 그래서
# 저장 직전에만 적용해 허용 밖 문자를 `_` 로 바꾸고 길이를 자른다. 값을 통째로 버리지
# 않는다 — 버리면 operator 가 복구 행동을 고를 근거를 잃는다 (D-020 항목 3).
_PERSISTED_CODE_DISALLOWED: Final = re.compile(r"[^a-z0-9_]")
_PERSISTED_CODE_MAX_LENGTH: Final = 64

# retry 예산 하한 상수는 두지 않는다. Slack 은 `Retry-After` 의 상한을 문서화하지 않고
# 문서화된 숫자는 30초 예시 하나뿐이다 (research.md S4·R-007). 상한을 모르면 "예산이
# 충분하다"를 판정할 수 없다. 대신 test 가 기본 config 의 재시도 schedule 을 그대로
# 고정한다 — Package 4 가 `OutboxConfig` 를 만들 때 그 schedule 을 입력으로 쓴다.


class SlackFailureClass(StrEnum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


class SlackTransportError(RuntimeError):
    """One failed Slack call.

    `error_code` 는 Slack 이 `ok: false` 와 함께 준 code 를 정규화한 값이다. transport 층에서
    실패해 Slack 응답 자체가 없으면 `None` 이다. 이 구분이 분류의 1차 기준이다.

    정규화는 앞뒤 공백 제거와 소문자화뿐이다. **형식이 이상해도 버리지 않는다.** 버리면
    allowlist 밖 code 가 code 없음이 되어 retryable 로 넘어가고, 그 경로는 원인을 남기지
    않는다. 형식 강제는 저장 직전에만 한다 (`persisted_code_suffix`).

    `raw_error_code` 는 transport 가 넘긴 원본이다. 진단용으로만 쓴다.

    `transport_exception` 은 `SlackTransportError` 가 아닌 예외를 destination 이 재감쌀 때
    (C-1 의무 위반에 대한 두 번째 방어선) 원인 예외의 class 이름을 넣는 자리다. **분류에는
    쓰지 않는다** — 쓰면 code 없는 실패가 terminal 로 떨어져 D-020 항목 1 이 뒤집힌다.
    C-3.1 이 attempt 소진 시점에 원인 문자열을 만들 때만 읽는다.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        transport_exception: str | None = None,
    ) -> None:
        # 빈 문자열과 대소문자 차이를 여기서 흡수한다. transport 가 응답 JSON 의 기본값으로
        # ""를 넘기면 `is None` 검사를 빠져나가 allowlist 를 못 만나고 terminal 로 떨어진다.
        #
        # str 이 아닌 값은 **버리지 않고 문자열로 만든다.** 생성자가 예외를 던지면 분류
        # 자체가 안 돌고, 버리면 code 없음이 되어 retryable 경로로 새면서 원인을 잃는다.
        # 문자열로 만들면 allowlist 밖이라 terminal 이 되고 원인이 접미사에 남는다.
        self.raw_error_code = error_code
        if error_code is None:
            normalized = ""
        elif isinstance(error_code, str):
            normalized = error_code.strip().lower()
        else:
            normalized = str(error_code).strip().lower()
        self.error_code = normalized or None
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.transport_exception = transport_exception
        super().__init__(message)


class SlackProjectionTerminalError(OutboxReconcileError):
    """Terminal Slack failure, surfaced so the dispatcher dead-letters immediately.

    `OutboxDispatcher.deliver_next` 에서 `unreconcilable=True` 가 붙는 경로는
    `OutboxReconcileError` 하나뿐이다. 일반 예외로 던지면 회복 불가 오류를
    `max_attempts` 번 재시도한 뒤에야 DLQ 에 도달한다.

    `code` 에 Slack 원인을 붙인다 — `SLACK_PROJECTION_TERMINAL_ERROR:{code}`. dispatcher 가
    이 문자열을 dead letter 와 operator hold 에 그대로 적고, 예외 객체는 버린다. 원인이
    없으면 operator 가 token 회전, channel 재지정, code 수정 중 무엇을 해야 하는지 알 수
    없다.

    접미사는 `persisted_code_suffix` 가 만든다 — 허용 밖 문자를 `_` 로 바꾸고 64자로 자른다.
    저장 대상이 append-only table 이라 원격 문자열을 그대로 남기지 않는다. code 가 없으면
    `unknown` 이다. 이 생성자는 공개돼 있으므로 검증을 여기서 한다.
    """

    def __init__(self, slack_error_code: str | None) -> None:
        self.slack_error_code = slack_error_code
        super().__init__(f"{_TERMINAL_ERROR_CODE}:{persisted_code_suffix(slack_error_code)}")


class SlackProjectionRetryExhaustedError(OutboxReconcileError):
    """The last retryable attempt, raised so the cause reaches the dead letter.

    retryable 로 분류된 실패가 attempt 를 소진하면 `deliver_next` 의 generic handler 가
    예외를 버리고 `OUTBOX_DELIVERY_FAILED` 상수만 남긴다 (`events.py:2864`). 그 상수가
    dead letter 와 operator hold 에 그대로 적혀 연결 실패, timeout, `ratelimited`,
    `internal_error` 가 전부 같은 row 가 된다 (D-020 항목 6).

    **재분류가 아니라 소진 시점의 기록이다.** 마지막 attempt 에서만 만든다. 그 시점의
    `fail()` 은 `exhausted or unreconcilable` 을 같은 `_dead_letter` 로 보내므로
    (`events.py:2778`) state 전이는 바뀌지 않고 error_code 만 달라진다. 재시도 횟수를
    한 번도 줄이지 않는다.
    """

    def __init__(self, error: SlackTransportError) -> None:
        self.slack_error_code = error.error_code
        super().__init__(f"{_RETRY_EXHAUSTED_ERROR_CODE}:{exhausted_cause_suffix(error)}")


def exhausted_cause_suffix(error: SlackTransportError) -> str:
    """Name the cause of one exhausted retryable failure for an append-only column.

    Slack code 가 있으면 그것을 쓴다. transport 층 실패라 code 가 없으면 재감싼 원인
    예외의 class 이름을 쓴다 — `transport_connectionreseterror` 처럼 남아야 operator 가
    network 문제와 Slack 문제를 가른다. `persisted_code_suffix` 가 소문자로 내리므로
    저장된 문자열도 소문자다. 둘 다 없으면 `no_slack_code` 다. `unknown` 을 쓰지 않는다 —
    terminal 경로의 `unknown` 과 섞이면 두 사건이 구분되지 않는다.
    """
    if error.error_code:
        return persisted_code_suffix(error.error_code)
    if error.transport_exception:
        return persisted_code_suffix(f"transport_{error.transport_exception}")
    return "no_slack_code"


def persisted_code_suffix(slack_error_code: str | None) -> str:
    """Shape one Slack code for a column that can never be edited or deleted.

    값을 버리지 않고 다듬는다. operator 는 이 접미사만 보고 token 회전, channel 재지정,
    code 수정 중 무엇을 할지 고른다.

    다듬는 과정에서 정보가 없어지면 (치환이 일어났거나 길이가 잘렸으면) 원본의 digest
    8자를 붙인다. `###` 와 `%%%` 가 둘 다 `___` 가 되어 같은 row 를 남기는 것을 막고,
    64자 접두가 같은 두 code 도 구분된다.
    """
    if not slack_error_code:
        return "unknown"
    normalized = slack_error_code.strip().lower()
    sanitized = _PERSISTED_CODE_DISALLOWED.sub("_", normalized)
    if sanitized == normalized and len(sanitized) <= _PERSISTED_CODE_MAX_LENGTH:
        return sanitized or "unknown"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    head = sanitized[: _PERSISTED_CODE_MAX_LENGTH - len(digest) - 1]
    return f"{head}_{digest}"


@dataclass(frozen=True, slots=True)
class SlackSendResult:
    """`chat.postMessage` 성공 응답에서 message 를 지목하는 두 필드."""

    channel: str
    ts: str

    def __post_init__(self) -> None:
        if not self.channel.strip() or not self.ts.strip():
            raise ValueError("Slack send 결과의 channel과 ts는 비어 있을 수 없습니다.")


@dataclass(frozen=True, slots=True)
class SlackHistoryMessage:
    """`conversations.history` 가 돌려준 message 중 marker 판정에 쓰는 부분.

    `metadata` 를 담으면 instance 가 hashable 하지 않다 — `MappingProxyType` 때문이다.
    `set` 이나 `dict` key 로 쓰지 않는다. 비교는 `__eq__` 로 한다.
    """

    ts: str
    metadata: Mapping[str, object] | None = None
    app_id: str | None = None

    def __post_init__(self) -> None:
        if not self.ts.strip():
            raise ValueError("Slack history message의 ts는 비어 있을 수 없습니다.")
        if self.metadata is not None:
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class SlackHistoryPage:
    """한 page 의 message 와 다음 cursor. `next_cursor` 가 없으면 마지막 page 다."""

    messages: Sequence[SlackHistoryMessage] = field(default_factory=tuple)
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        if self.next_cursor is not None and not self.next_cursor.strip():
            raise ValueError("Slack history cursor는 비어 있을 수 없습니다.")


class SlackTransport(Protocol):
    """Injected Slack calls. send 와 read 를 모두 갖는다.

    read 가 필요한 이유는 reconcile 이 message marker read-back 으로 판정하기 때문이다
    (D-018 항목 3). 구현체는 `read_history` 를 반드시 `include_all_metadata=true` 로
    호출한다 — 그러지 않으면 `event_payload` 가 오지 않아 판정 자체가 불가능하다.

    구현체가 지켜야 할 의무 둘. 둘 다 signature 로 강제할 수 없어 여기 적는다.

    1. **모든 실패를 `SlackTransportError` 로 감싼다.** 다른 예외가 새어 나가면 분류가
       아예 돌지 않고 dispatcher 의 일반 실패 경로가 무조건 재시도로 만든다. terminal
       이어야 할 `invalid_auth` 가 조용히 재시도된다.
    2. **호출 시간을 `OutboxConfig.lease_seconds` 보다 확실히 짧은 deadline 으로 묶는다.**
       lease 가 만료된 뒤 실패하면 dispatcher 의 `fail()` 이 lease conflict 로 터져
       terminal 판정이 통째로 버려진다. dead letter 도 operator hold 도 안 생긴다.
    """

    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult: ...

    def read_history(
        self,
        *,
        channel: str,
        cursor: str | None,
        limit: int,
    ) -> SlackHistoryPage: ...


def classify_slack_failure(error: SlackTransportError) -> SlackFailureClass:
    """Classify one transport failure. Unknown Slack codes are terminal.

    규칙 순서가 곧 우선순위다.

    **위에서부터 먼저 맞는 규칙이 이긴다.** 그래서 HTTP 429 는 allowlist 밖 Slack code 를
    달고 와도 재시도로 분류된다 — 429 는 Slack 이 "지금 말고 나중에" 라고 답한 것이므로
    함께 온 code 를 확정된 판정으로 읽지 않는다. 결말은 같다: 영구 실패면 attempt 를
    소진하고 같은 dead letter 에 도달한다.

    1. HTTP 429 는 Slack code 유무와 무관하게 재시도한다 (`Retry-After` 를 주는 응답).
    2. **Slack code 가 없으면 전부 transport 층 실패이고 재시도한다.** status code 로
       가르지 않는다. Slack 은 application error 를 HTTP 200 + `ok: false` 로 주므로
       code 없는 HTTP status 는 거의 전부 proxy·WAF·load balancer 가 낸 것이다. 그건
       transient 다.
    3. Slack code 가 allowlist 에 있으면 재시도, 나머지는 전부 terminal 이다.

    retryable 이 보수적인 쪽이다. 영구 실패를 retryable 로 잘못 분류해도 attempt 를
    소진하면 같은 dead letter 와 operator hold 에 도달한다 — 몇십 초 손해다. 반대로
    transient 를 terminal 로 분류하면 destination 전체가 즉시 멈추고, 그 hold 는
    `resolved_at IS NULL` 제약 때문에 되돌릴 수 없다.
    """
    # status_code 도 transport 가 채우는 값이라 형이 어긋날 수 있다. `"429"` 가 == 비교를
    # 빠져나가면 규칙 1 을 건너뛰고 terminal 로 떨어져 되돌릴 수 없는 hold 를 만든다.
    # error_code 오염은 retryable 쪽으로 흐르는데 이쪽은 반대라 별도로 좁힌다.
    if _as_status_code(error.status_code) == _RATE_LIMIT_STATUS:
        return SlackFailureClass.RETRYABLE
    if error.error_code is None:
        return SlackFailureClass.RETRYABLE
    if error.error_code in RETRYABLE_SLACK_ERROR_CODES:
        return SlackFailureClass.RETRYABLE
    return SlackFailureClass.TERMINAL


def _as_status_code(status_code: object) -> int | None:
    """Read one HTTP status defensively. `\"429\"` must not slip past rule 1."""
    if isinstance(status_code, bool):
        return None
    if isinstance(status_code, int):
        return status_code
    if isinstance(status_code, str):
        try:
            return int(status_code.strip())
        except ValueError:
            return None
    return None


def raise_for_slack_failure(error: SlackTransportError) -> NoReturn:
    """Re-raise one transport failure as the exception the dispatcher expects.

    terminal 은 `SlackProjectionTerminalError` 로 올려 즉시 DLQ 와 operator hold 를
    만든다. retryable 은 원래 예외를 그대로 올려 dispatcher 의 일반 실패 경로
    (`OUTBOX_DELIVERY_FAILED` → `retry_wait`)를 타게 한다.

    retryable 이 중복 Card 를 만들지 않는 근거는 재시도가 언제나 reconcile 을 먼저
    돌기 때문이다. send 를 reconcile 없이 부르는 경로가 생기면 이 분류는 그 순간부터
    안전하지 않다.

    `retry_after_seconds` 는 여기서 쓰지 않는다. dispatcher 의 backoff 에 지연을 주입할
    인자가 없다. 기본값이면 재시도 예산이 약 75초라 30~60초짜리 `Retry-After` 창 안에서
    소진될 수 있다. 수용한 위험이고 근거는 research.md R-007 에 있다.
    """
    if classify_slack_failure(error) is SlackFailureClass.TERMINAL:
        raise SlackProjectionTerminalError(error.error_code) from error
    raise error


def build_slack_marker(event: OutboxEventView) -> dict[str, object]:
    """Build the Slack `metadata` that `reconcile()` reads back.

    Slack 쪽 표현은 `event_type` + `event_payload` 다 (research S3). `event_payload` 에
    담는 것은 넷뿐이다 — `event_id`, `destination_ref`, `destination_sequence`,
    `payload_digest`.

    **payload 본문은 안 담는다.** metadata 크기 상한을 공식 문서에서 확인하지 못했고
    (research.md R-003 미확인) `metadata_too_large` error code 는 실재한다. 그 code 는
    allowlist 밖이라 terminal 이고, terminal 은 되돌릴 수 없는 hold 를 만든다.
    """
    return {
        "event_type": SLACK_PROJECTION_EVENT_TYPE,
        "event_payload": {
            "event_id": event.event_id,
            "destination_ref": event.destination_ref,
            "destination_sequence": event.destination_sequence,
            "payload_digest": event.payload_digest,
        },
    }


def payload_digest(payload: Mapping[str, object]) -> str:
    """Digest one payload the way `YamlProjectionDestination` does.

    규칙이 `projections.py:169` 와 같아야 한다 — `ensure_ascii=False`,
    `separators=(",", ":")`, `sort_keys=True`, `sha256:` prefix. 두 destination 이 같은
    event 를 다르게 판정하면 하나는 보내고 하나는 무결성 실패로 막는다. 규칙 일치는
    test 가 두 값을 직접 대조해 고정한다.
    """
    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class SlackProjectionDestination:
    """`ProjectionDestination` (`events.py:236`) backed by an injected Slack transport.

    `destination_ref` 는 `provider:slack:{channel_digest}` 를 그대로 받는다. 앞뒤 공백만
    지우고 형식은 만들지도 해석하지도 않는다 (D-018 항목 1). `channel` 을 따로 받는 이유는
    `channel_digest` 가 digest 라 역산이 안 되기 때문이다.

    `max_attempts` 는 이 destination 을 도는 `OutboxDispatcher` 의
    `OutboxConfig.max_attempts` 와 **같은 값이어야 한다** (contracts C-2). destination 은
    dispatcher config 를 읽을 경로가 없어 검증하지 못한다. 더 크면 C-3.1 이 안 돌아
    retryable 원인이 사라지고, 더 작으면 남은 attempt 를 두고 되돌릴 수 없는 hold 를
    만든다.
    """

    def __init__(
        self,
        transport: SlackTransport,
        *,
        destination_ref: str,
        channel: str,
        max_history_pages: int,
        max_attempts: int,
    ) -> None:
        # dispatcher config 와의 일치는 검증하지 못하지만 그 자체로 말이 안 되는 값은
        # 여기서 막는다. `max_attempts < 1` 이면 첫 transient 실패가 곧바로 C-3.1 을 타
        # 되돌릴 수 없는 hold 를 만들고, `max_history_pages < 1` 이면 reconcile 이 아무것도
        # 훑지 않아 첫 전달부터 판정불가로 떨어진다. 둘 다 `OutboxConfig` 의 `ge=1` 과 같은
        # 하한이라 config 를 안 읽고도 검사된다.
        if not destination_ref.strip():
            raise ValueError("destination_ref는 비어 있을 수 없습니다.")
        if not channel.strip():
            raise ValueError("channel은 비어 있을 수 없습니다.")
        if max_history_pages < 1:
            raise ValueError("max_history_pages는 1 이상이어야 합니다.")
        if max_attempts < 1:
            raise ValueError("max_attempts는 1 이상이어야 합니다.")
        # 앞뒤 공백을 지우고 저장한다. 검사만 하고 원본을 쓰면 env var 나 YAML scalar 에서
        # 온 개행 하나가 그대로 Slack 에 나가 `channel_not_found` 를 부른다. 그 code 는
        # allowlist 밖이라 terminal 이고 hold 는 되돌릴 수 없다. `_receipt` 도 이 값을
        # 쓰므로 정규화 안 하면 reconcile 과 receipt 가 갈라진다 (C-2.3).
        self.destination_ref = destination_ref.strip()
        self.channel = channel.strip()
        self.max_history_pages = max_history_pages
        self.max_attempts = max_attempts
        self._transport = transport

    def send(self, event: OutboxEventView) -> str:
        """Post one Card and return `slack:{channel}:{ts}`.

        검증 둘을 먼저 통과해야 transport 를 부른다. 순서는
        `YamlProjectionDestination.send` 와 같다 (`projections.py:57`-`61`). 사전 검증이
        없으면 손상된 payload 가 사람에게 보이는 Card 로 나가고, `chat.update` 로 되돌리는
        것은 범위 밖이다 (research R-008).
        """
        # `OutboxReconcileError` 다. 부모인 `GovernanceEventError` 로 던지면
        # `deliver_next` 의 `except OutboxReconcileError` (`events.py:2856`)가 못 잡고
        # generic handler 로 떨어져 원인이 `OUTBOX_DELIVERY_FAILED` 로 덮인다. 두 조건은
        # event row 의 불변 column 에서 나와 재시도가 확정적으로 무의미하다 (D-022).
        if event.destination_ref != self.destination_ref:
            raise OutboxReconcileError("OUTBOX_DESTINATION_MISMATCH")
        if payload_digest(event.payload) != event.payload_digest:
            raise OutboxReconcileError("OUTBOX_PAYLOAD_INTEGRITY_FAILURE")
        # try 밖에서 만든다. 안에서 만들면 marker 구성 버그가 `transport_...` 로 기록되어
        # 우리 결함이 transport 구현자 탓으로 남는다.
        marker = build_slack_marker(event)
        try:
            result = self._transport.post_message(
                channel=self.channel,
                payload=event.payload,
                marker=marker,
            )
        except SlackTransportError as error:
            self._raise_for_failure(error, event)
        except Exception as error:
            # C-1 의무 위반에 대한 두 번째 방어선이다. 넓게 잡는 것이 의도다 — 좁히면
            # 새어 나온 예외가 dispatcher 의 generic handler 로 가서 원인 없이 재시도된다.
            self._raise_for_failure(self._rewrap(error), event)
        return self._receipt(result.ts)

    def _raise_for_failure(self, error: SlackTransportError, event: OutboxEventView) -> NoReturn:
        """Turn one classified failure into the exception the dispatcher expects.

        terminal 은 즉시 DLQ + hold 다. retryable 은 원래 예외를 그대로 올려
        `OUTBOX_DELIVERY_FAILED` → `retry_wait` 경로를 탄다. 단 **마지막 attempt 는**
        원인을 담은 `SlackProjectionRetryExhaustedError` 로 올린다 (C-3.1, D-020 항목 6).
        그 시점의 state 전이는 어차피 dead letter 라 바뀌는 것은 error_code 뿐이다.
        """
        if event.attempts >= self.max_attempts and (
            classify_slack_failure(error) is SlackFailureClass.RETRYABLE
        ):
            raise SlackProjectionRetryExhaustedError(error) from error
        # terminal 판정과 retryable 재던지기는 T001 의 `raise_for_slack_failure` 하나만
        # 쓴다. 여기서 다시 쓰면 분류 규칙이 두 벌이 되고 T001 test 가 도는 쪽은 죽은
        # copy 가 된다.
        raise_for_slack_failure(error)

    @staticmethod
    def _rewrap(error: BaseException) -> SlackTransportError:
        """Wrap one out-of-contract exception so classification still runs.

        C-1 은 transport 가 모든 실패를 `SlackTransportError` 로 감싸도록 요구한다. 그
        의무를 어긴 구현이 있어도 분류를 건너뛰지 않는다 (D-020 항목 4).

        `error_code` 는 비운다. 채우면 allowlist 밖이라 terminal 이 되고, 그것은 구현
        결함을 되돌릴 수 없는 hold 로 바꾼다. code 없음은 규칙 2 로 retryable 이고 그쪽이
        보수적이다 (D-020 항목 1). 원인은 `transport_exception` 에 남아 attempt 소진 시
        dead letter 까지 간다.

        이름은 builtin 이 아니면 module 을 붙인다. Package 4 가 HTTP client 를 넣으면
        `ConnectError` 같은 흔한 이름이 여러 module 에서 나와 한 문자열로 뭉친다.
        """
        origin = type(error)
        module = origin.__module__.replace(".", "_")
        label = origin.__qualname__ if module == "builtins" else f"{module}_{origin.__qualname__}"
        wrapped = SlackTransportError(
            f"Slack transport raised an unwrapped {origin.__qualname__}",
            transport_exception=label,
        )
        # 재시도 경로에서 이 예외가 그대로 올라간다. `__cause__` 를 손으로 붙이지 않으면
        # 원인 예외가 traceback 에서만 보이고 (`__context__`), 그것도 dispatcher 가
        # 예외 객체를 버리는 순간 사라진다.
        wrapped.__cause__ = error
        return wrapped

    def _receipt(self, ts: str) -> str:
        """Build the receipt both `send()` and `reconcile()` must agree on (C-2.3).

        `{channel}` 은 생성자가 받은 값이다. `SlackSendResult.channel` 이 아니다 —
        `reconcile()` 은 `conversations.history` message 에서 channel 을 못 얻어
        (`ts`·`metadata`·`app_id` 셋뿐) 생성자 값밖에 쓸 수 없다. 두 경로가 다른 문자열을
        만들면 `mark_delivered` 가 `OUTBOX_DELIVERY_RESULT_CONFLICT` 를 던진다
        (`events.py:2717`).
        """
        return f"slack:{self.channel}:{ts}"
