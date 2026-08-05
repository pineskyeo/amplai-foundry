"""Injected Slack transport contract and fail-closed failure classification.

이 module 은 Slack 을 실제로 호출하지 않는다. `SlackProjectionDestination` 이 부를
transport 를 Protocol 로만 정의한다. 실제 HTTP 구현은 Package 4 가 맡는다
(`docs/workstreams/messenger-governance-closure-v3/DECISIONS.md` D-018 항목 2).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, NoReturn, Protocol

from amplai_foundry.governance.events import OutboxReconcileError

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
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
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
