"""Injected Slack transport contract and fail-closed failure classification.

이 module 은 Slack 을 실제로 호출하지 않는다. `SlackProjectionDestination` 이 부를
transport 를 Protocol 로만 정의한다. 실제 HTTP 구현은 Package 4 가 맡는다
(`docs/workstreams/messenger-governance-closure-v3/DECISIONS.md` D-018 항목 2).
"""

from __future__ import annotations

import hashlib
import json
import re
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, NoReturn, Protocol

from amplai_foundry.governance.events import (
    OutboxConfig,
    OutboxEventView,
    OutboxReconcileError,
    OutboxRetryableError,
    ReviewProjectionPayload,
)
from amplai_foundry.governance.review_cards import (
    PreparedReviewActionSet,
    ReviewActionSetService,
    ReviewCardError,
)
from amplai_foundry.governance.slack_cards import (
    SlackCardRenderingError,
    SlackProposalCardRenderer,
    slack_presentation_payload,
)
from amplai_foundry.governance.store import is_transient_store_failure

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

# Provider/adapter text is untrusted. Review Card requests carry one-time action
# credentials, so a failed adapter must never be able to echo an arbitrary string
# into an exception, dead letter, or diagnostic. Only fixed Slack codes that the
# application understands remain readable; every other value is represented by an
# irreversible digest.
_SAFE_SLACK_ERROR_CODES: Final = RETRYABLE_SLACK_ERROR_CODES | frozenset(
    {
        "account_inactive",
        "cant_delete_message",
        "channel_not_found",
        "invalid_auth",
        "is_archived",
        "method_not_supported_for_channel_type",
        "missing_scope",
        "no_permission",
        "no_text",
        "not_authed",
        "not_in_channel",
        "restricted_action",
        "token_revoked",
    }
)
_SAFE_TRANSPORT_EXCEPTIONS: Final = frozenset(
    {
        "deadline_exceeded",
        "missing_success_field",
        "non_object_response",
        "not_a_slack_envelope",
        "response_too_large",
        "unreadable_response",
    }
)

_TERMINAL_ERROR_CODE: Final = "SLACK_PROJECTION_TERMINAL_ERROR"
_RETRY_EXHAUSTED_ERROR_CODE: Final = "SLACK_PROJECTION_RETRY_EXHAUSTED"
_RATE_LIMIT_STATUS: Final = 429

# reconcile 의 조회 범위. 둘 다 contracts C-2.2.1 이 확정했다 (D-023 항목 4).
#
# limit 은 `conversations.history` 의 문서화된 상한 그대로다 (research S2). Slack 이 세는
# 것은 message 수가 아니라 **호출 수**이므로 (S4) 한 번에 꽉 채우는 쪽이 손해가 없다.
#
# **page 수 5 는 판단이지 측정이 아니다.** 근거로 쓸 수 있는 fact 는 셋뿐이다 — page 당
# 999 상한 (S2), Tier 2 분당 20+ 요청 (S4), 그리고 이 조회가 재시도 때만 일어난다는 것
# (아래 `_never_attempted`). 정작 필요한 숫자인 "Card 한 장과 다음 Card 사이에 쌓이는
# message 수" 는 workspace 에 달렸고 **모른다.** 최악 5회 호출은 Tier 2 예산의 4분의 1이다.
# 상한에 걸려 생긴 hold 는 `_SEARCH_CAP_ERROR_CODE` 로 구분되므로 값이 작았다는 것을
# 사후에 알 수 있다. 실측은 Package 4 몫이다.
SLACK_HISTORY_PAGE_LIMIT: Final = 999
SLACK_MAX_HISTORY_PAGES: Final = 5

# 상한 도달로 생긴 판정 불가. 다른 원인의 hold 와 구분돼야 한다 (D-023 항목 4) — 구분이
# 없으면 SLACK_MAX_HISTORY_PAGES 가 작았다는 것을 알 방법이 없다.
_SEARCH_CAP_ERROR_CODE: Final = "SLACK_PROJECTION_RECONCILE_SEARCH_CAP_REACHED"
_MARKER_DIGEST_MISMATCH_ERROR_CODE: Final = "SLACK_PROJECTION_MARKER_DIGEST_MISMATCH"
_METADATA_UNREADABLE_ERROR_CODE: Final = "SLACK_PROJECTION_METADATA_UNREADABLE"

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


class SlackProjectionSearchCapError(OutboxReconcileError):
    """`reconcile()` hit the page cap without deciding. Fail-closed.

    "못 찾았다" 를 "안 보냈다" 로 읽으면 조회 범위 밖에 있던 message 를 중복 발행한다.
    그래서 여기서는 판정하지 않고 dispatcher 에 넘긴다 — `deliver_next` 가 이것을
    `fail(..., unreconcilable=True)` 로 보내 attempts 와 무관하게 DLQ 와 operator hold 를
    만든다 (`events.py:2856`-`2863`).

    **history 소진과 다르다.** `next_cursor` 가 없어 끝까지 훑었는데 없으면 그건 없는
    것이 확정이라 미전송으로 판정한다 (D-023 항목 3). 이 예외는 `next_cursor` 가 아직
    남았는데 page 상한에 걸려 멈춘 경우만이다.

    code 를 따로 두는 이유는 operator 가 "상한이 작았다" 를 알 수 있어야 하기 때문이다.
    다른 hold 원인과 같은 문자열이면 `SLACK_MAX_HISTORY_PAGES` 를 올려야 하는지 판단할
    근거가 없다 (D-023 항목 4).
    """

    def __init__(self) -> None:
        super().__init__(_SEARCH_CAP_ERROR_CODE)


class SlackProjectionMetadataUnreadableError(OutboxReconcileError):
    """The transport is not sending `include_all_metadata`. Fail-closed.

    C-1.2 는 `read_history` 구현체가 `include_all_metadata=true` 를 반드시 붙이도록
    요구한다. 안 붙이면 `event_type` 만 오고 `event_payload` 가 안 온다 (research S3).
    signature 로 강제할 수 없는 의무라 wave 3 review 가 P1 로 지적했다.

    **그냥 두면 hold 가 아니라 중복 Card 가 난다.** 모든 marker 가 안 읽히므로 history
    소진이 "미전송" 으로 판정되고 (D-023 항목 3) 매 재시도마다 Card 가 한 장씩 늘어난다.
    조용히 일어난다.

    그래서 지문으로 잡는다 — **우리 `event_type` 을 단 message 인데 `app_id` 가 없거나
    `event_payload` 가 없는 경우.** 우리는 `event_payload` 를 항상 채워 보내고
    (`build_slack_marker`), 사람이 Card 를 지웠거나 이전 event 가 전부 superseded 인
    경우와는 겹치지 않는다 — 그 경우엔 우리 `event_type` 을 단 message 자체가 없다.

    **오탐이 하나 있다.** 제3의 app 이 우리 `event_type` 을 쓰면서 `app_id` 없이 보내면
    여기 걸려 되돌릴 수 없는 hold 가 된다. Slack 이 `app_id` 를 항상 붙이는지 확인하지
    못했다 (research S2·S3). 수용한 이유는 방향이다 — 안 막으면 조용한 중복 Card, 막으면
    시끄러운 hold 다.

    **이 지문이 전부를 덮지는 않는다.** 형식은 맞지만 값이 틀린 `app_id`, `metadata` 를
    통째로 안 옮기는 adapter, `event_type` 개명은 여전히 조용히 중복 Card 를 만든다.
    그것들은 destination 안에서 판정할 수 없다 — C-1.2 의 readback 자가검사가 막는다
    (wave 3 round 2 review).
    """

    def __init__(self) -> None:
        super().__init__(_METADATA_UNREADABLE_ERROR_CODE)


class SlackProjectionMarkerDigestMismatchError(OutboxReconcileError):
    """Our own marker is in the channel with a different payload digest. Fail-closed.

    같은 `event_id` 의 Card 가 **다른 내용**으로 이미 나갔다는 뜻이다. `payload` 와
    `payload_digest` 는 event row 의 불변 column 이라 정상 경로에서 나올 수 없다.

    **계속 훑지 않는다.** 훑고 지나가면 그 뒤에서 하위 sequence marker 를 만나거나
    history 가 소진되어 "미전송" 으로 판정하고, 이미 나간 Card 옆에 한 장을 더 만든다
    (wave 3 review). D-023 이 "Card 가 두 장이 되는 일은 어느 쪽에서도 없다" 고 약속했으므로
    여기서 멈춘다. 판정 불가는 fail-closed 라는 T003 invariant 와도 같다.

    code 를 따로 두는 이유는 이 상태가 상한 부족이나 지워진 Card 와 원인이 전혀 다르기
    때문이다. operator 는 digest 가 갈린 경위를 먼저 봐야 한다.
    """

    def __init__(self) -> None:
        super().__init__(_MARKER_DIGEST_MISMATCH_ERROR_CODE)


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


def safe_slack_error_code(slack_error_code: str | None) -> str | None:
    """Return a classification-preserving, secret-safe provider diagnostic.

    Known fixed Slack codes remain useful to an operator. An arbitrary value is not
    copied: its digest keeps two unknown failures distinguishable without retaining
    provider-controlled text that could contain a Review Card credential.
    """
    if not slack_error_code:
        return None
    normalized = slack_error_code.strip().lower()
    if normalized in _SAFE_SLACK_ERROR_CODES:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"unrecognized_{digest}"


def safe_transport_exception(transport_exception: str | None) -> str | None:
    """Irreversibly label adapter-controlled exception metadata."""
    if not transport_exception:
        return None
    if transport_exception in _SAFE_TRANSPORT_EXCEPTIONS:
        return transport_exception
    digest = hashlib.sha256(transport_exception.encode("utf-8")).hexdigest()[:12]
    return f"adapter_{digest}"


_REVIEW_INTERRUPTION_MESSAGE: Final = "Slack Review Card delivery interrupted."


def classify_interruption(error: BaseException) -> str:
    """Name what stopped a delivery so the caller can re-raise it in kind.

    `except BaseException` 은 process 를 멈추는 것과 평범한 Exception 을 함께 잡는다. 둘은
    결말이 달라야 한다. process interruption 은 그대로 전파돼야 하고, 평범한 Exception 은
    dispatcher 가 잡아 durable 실패로 닫아야 한다. 이 함수가 그 구분을 만든다.

    **정의는 저장소에 하나만 둔다.** 이 함수와 `raise_sanitized_interruption` 은 원래 이
    module 과 `slack_http` 에 사본으로 있었고 한쪽만 고쳐졌다. 그 결과 `slack_http` 사본은
    평범한 Exception 을 `"base_exception"` 으로 부르고 bare `BaseException` 으로 다시 올려
    `deliver_next` 의 두 handler 를 모두 통과했다 (round 11 `R-1`). 사본을 만들지 않는다.
    """
    if isinstance(error, KeyboardInterrupt):
        return "keyboard_interrupt"
    if isinstance(error, SystemExit):
        return "system_exit"
    if isinstance(error, GeneratorExit):
        return "generator_exit"
    return "exception"


def raise_sanitized_interruption(kind: str, message: str) -> NoReturn:
    """Re-raise one classified interruption without its original context.

    원본 예외를 그대로 다시 올리지 않는 이유는 그 traceback 이 credential 을 든 frame 을
    붙잡고 있을 수 있어서다. 같은 종류의 새 예외를 `from None` 으로 올린다.

    `"exception"` 은 `RuntimeError` 가 된다. bare `BaseException` 으로 올리면 `deliver_next`
    의 `except OutboxReconcileError` 와 `except Exception` 을 모두 통과해 조용히 새고, event 가
    `leased` 로 남아 worker 가 매번 죽는다. `except Exception` 이 잡을 수 있는 형으로 올려
    fail-closed 를 유지한다.
    """
    if kind == "keyboard_interrupt":
        raise KeyboardInterrupt(message) from None
    if kind == "system_exit":
        raise SystemExit(message) from None
    if kind == "generator_exit":
        raise GeneratorExit from None
    raise RuntimeError(message) from None


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


@dataclass(frozen=True, slots=True)
class SlackMarkerView:
    """One marker recovered from `conversations.history`.

    `build_slack_marker` 가 심은 네 필드를 되읽은 것이다. 되읽기는 **방어적이다** —
    metadata 는 원격에서 온 값이고 다른 app 이 아무 모양이나 넣을 수 있다.
    """

    event_id: str
    destination_ref: str
    destination_sequence: int
    payload_digest: str


def read_slack_marker(message: SlackHistoryMessage, *, app_id: str) -> SlackMarkerView | None:
    """Recover our marker from one history message, or `None` if it is not ours.

    `None` 을 반환하는 경우는 전부 "이 message 는 우리 marker 가 아니다" 다. 배제 순서에
    이유가 있다.

    1. `event_type` 이 다르면 배제한다. 우리 marker 가 아니다.
    2. **`app_id` 가 다르면 배제한다.** 다른 app 이 같은 모양의 metadata 를 심을 수 있고
       (research S3), 그것을 우리 marker 로 읽으면 남의 message 를 우리 Card 로 확정한다.
    3. 네 필드 중 하나라도 없거나 형이 다르면 배제한다. **예외를 던지지 않는다** — 한
       message 의 모양이 이상하다고 조회 전체를 멈추면 그 뒤에 있는 진짜 marker 를 못
       읽고 판정 불가로 떨어진다. 그 결과는 되돌릴 수 없는 hold 다.

    예외가 둘 있다. **우리 `event_type` 을 단 message 인데** `app_id` 가 없거나
    `event_payload` 가 없으면 `SlackProjectionMetadataUnreadableError` 를 던진다. 둘 다 한
    message 의 문제가 아니라 조회 방식의 문제이고, 넘기면 모든 marker 가 안 읽혀 중복
    Card 로 이어진다. 자세한 근거는 그 class 에 있다.
    """
    metadata = message.metadata
    if metadata is None:
        return None
    if metadata.get("event_type") != SLACK_PROJECTION_EVENT_TYPE:
        return None
    # 여기까지 왔으면 **우리 event_type 을 단 message** 다. 아래 둘은 그 조건에서만 보므로
    # 사람이 쓴 글이나 다른 app 의 message 를 오탐하지 않는다.
    if message.app_id is None:
        # 우리 `event_type` 을 단 message 인데 보낸 app 을 모른다. 그러면 아래 대조가
        # 전부 실패해 marker 를 하나도 못 읽고, 결과는 hold 가 아니라 중복 Card 다
        # (wave 3 round 2 review). 그래서 fail-closed 로 멈춘다.
        #
        # **오탐이 하나 있다.** 제3의 app 이 우리 `event_type` 을 쓰면서 `app_id` 없이
        # 보내면 여기 걸린다. Slack 이 app message 에 `app_id` 를 **항상** 붙이는지는
        # research S2·S3 에서 확인하지 못했다 — 확인된 것은 "각 message 는 app_id 를 갖고
        # 있어 어느 app 이 보냈는지 구분할 수 있다" 까지다. 그 오탐은 되돌릴 수 없는
        # hold 를 만든다. 수용한 이유는 방향이다 — 막지 않으면 조용한 중복 Card 이고,
        # 막으면 시끄러운 hold 다. 확정은 C-1.2 의 readback 자가검사와 함께 Package 4 다.
        raise SlackProjectionMetadataUnreadableError
    if message.app_id != app_id:
        return None
    if metadata.get("event_payload") is None:
        # 우리 app 이 우리 event_type 으로 보낸 message 인데 body 가 없다. 우리는 그렇게
        # 보낸 적이 없으므로 (`build_slack_marker`) 이것은 조회 쪽 결함이다 —
        # `include_all_metadata` 를 안 붙이면 정확히 이 모양이 온다 (research S3).
        # **key 부재와 `None` 을 함께 본다.** adapter 가 `md.get("event_payload")` 로
        # dataclass 를 만들면 없는 key 가 `None` 으로 채워져 key 검사만으로는 샌다.
        # 그냥 넘기면 모든 marker 가 안 읽혀 history 소진이 미전송으로 판정되고 중복
        # Card 가 난다.
        raise SlackProjectionMetadataUnreadableError
    body = metadata.get("event_payload")
    if not isinstance(body, Mapping):
        return None
    event_id = body.get("event_id")
    destination_ref = body.get("destination_ref")
    sequence = body.get("destination_sequence")
    digest = body.get("payload_digest")
    if not isinstance(event_id, str) or not isinstance(destination_ref, str):
        return None
    if not isinstance(digest, str):
        return None
    # bool 은 int 의 subclass 다. sequence 자리에 True 가 오면 1 로 읽혀 하위 sequence
    # 판정을 잘못 통과시킨다.
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None
    return SlackMarkerView(
        event_id=event_id,
        destination_ref=destination_ref,
        destination_sequence=sequence,
        payload_digest=digest,
    )


def _require_visible(name: str, value: str) -> None:
    """Reject a config string that would silently break a remote comparison.

    공백만 있는 값은 물론이고 **보이지 않는 문자**도 막는다. `\u200b`(zero width space),
    `\ufeff`(BOM), `\u200e`(LRM) 은 `str.strip()` 이 지우지 않으면서 눈에도 안 보인다.
    UTF-8-BOM YAML 이나 Slack UI 복사로 섞여 들어온다.

    `channel` 에 섞이면 `channel_not_found` 가 나고 그 code 는 allowlist 밖이라 terminal
    이다. `app_id` 에 섞이면 더 나쁘다 — `reconcile` 이 우리 marker 를 하나도 못 알아보고
    history 소진을 미전송으로 읽어 **중복 Card** 를 만든다 (D-023 항목 3). 둘 다 조용히
    일어나므로 생성 시점에 막는다.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name}은(는) 비어 있을 수 없습니다.")
    if not stripped.isprintable():
        raise ValueError(f"{name}에 보이지 않는 문자가 있습니다: {stripped!r}")


class _Undecided:
    """Sentinel — this message decides nothing, keep scanning.

    `None` 은 이미 "미전송" 이라는 판정에 쓰이므로 "아직 판정 안 됨" 을 그것으로 표현할 수
    없다. 둘을 섞으면 무관한 message 하나가 곧바로 재전송을 부른다.
    """

    __slots__ = ()


_UNDECIDED: Final = _Undecided()


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

    `validate_dispatcher_config()`가 실제 claim 전에 dispatcher의 lease/attempt 설정을
    대조한다. HTTP transport가 선언한 page/lease 예산도 constructor와 같은 hook에서
    묶는다. 각각 유효하지만 서로 다른 구성이 한 worker에 연결되는 것을 허용하지 않는다.
    """

    def __init__(
        self,
        transport: SlackTransport,
        *,
        destination_ref: str,
        channel: str,
        app_id: str,
        max_attempts: int,
        max_history_pages: int = SLACK_MAX_HISTORY_PAGES,
        card_renderer: SlackProposalCardRenderer | None = None,
        review_action_sets: ReviewActionSetService | None = None,
    ) -> None:
        # dispatcher config 와의 일치는 검증하지 못하지만 그 자체로 말이 안 되는 값은
        # 여기서 막는다. `max_attempts < 1` 이면 첫 transient 실패가 곧바로 C-3.1 을 타
        # 되돌릴 수 없는 hold 를 만들고, `max_history_pages < 1` 이면 reconcile 이 아무것도
        # 훑지 않아 첫 전달부터 판정불가로 떨어진다. 둘 다 `OutboxConfig` 의 `ge=1` 과 같은
        # 하한이라 config 를 안 읽고도 검사된다.
        _require_visible("destination_ref", destination_ref)
        _require_visible("channel", channel)
        # app_id 가 어긋나면 reconcile 이 아무 marker 도 우리 것으로 인정하지 않는다. 그
        # 결과는 hold 가 아니라 **중복 Card** 다 — history 를 소진하면 미전송으로 판정하기
        # 때문이다 (D-023 항목 3). T003 에서 app_id 가 판정의 열쇠가 되면서 이 검사가
        # 무게를 갖게 됐다.
        _require_visible("app_id", app_id)
        if max_history_pages < 1:
            raise ValueError("max_history_pages는 1 이상이어야 합니다.")
        if max_attempts < 1:
            raise ValueError("max_attempts는 1 이상이어야 합니다.")
        transport_pages = getattr(transport, "max_history_pages", None)
        if transport_pages is not None and transport_pages != max_history_pages:
            raise ValueError("Slack transport와 destination의 max_history_pages가 다릅니다.")
        transport_lease = getattr(transport, "lease_seconds", None)
        if transport_lease is not None and (
            isinstance(transport_lease, bool) or not isinstance(transport_lease, int)
        ):
            raise ValueError("Slack transport lease_seconds가 올바르지 않습니다.")
        # 앞뒤 공백을 지우고 저장한다. 검사만 하고 원본을 쓰면 env var 나 YAML scalar 에서
        # 온 개행 하나가 그대로 Slack 에 나가 `channel_not_found` 를 부른다. 그 code 는
        # allowlist 밖이라 terminal 이고 hold 는 되돌릴 수 없다. `_receipt` 도 이 값을
        # 쓰므로 정규화 안 하면 reconcile 과 receipt 가 갈라진다 (C-2.3).
        self.destination_ref = destination_ref.strip()
        self.channel = channel.strip()
        self.app_id = app_id.strip()
        self.max_history_pages = max_history_pages
        self.max_attempts = max_attempts
        self._transport_lease_seconds = transport_lease
        self._transport = transport
        self._card_renderer = card_renderer or SlackProposalCardRenderer()
        self._review_action_sets = review_action_sets

    def validate_dispatcher_config(self, config: OutboxConfig) -> None:
        """Reject split delivery budgets before the dispatcher claims an event."""
        if config.max_attempts != self.max_attempts:
            raise ValueError("Slack destination과 dispatcher의 max_attempts가 다릅니다.")
        if (
            self._transport_lease_seconds is not None
            and config.lease_seconds != self._transport_lease_seconds
        ):
            raise ValueError("Slack transport와 dispatcher의 lease_seconds가 다릅니다.")

    def reconcile(self, event: OutboxEventView) -> str | None:
        """Decide whether this event's Card is already in the channel (C-2.2).

        반환은 셋이다. receipt 는 전달 완료, `None` 은 미전송, `OutboxReconcileError` 는
        판정 불가다. 판정 불가만 dispatcher 가 DLQ + operator hold 로 보낸다.

        **첫 시도는 조회하지 않는다** (`_never_attempted`, D-023 항목 2). 찾을 marker 가
        정의상 존재하지 않고 `conversations.history` 는 Tier 2 다 (research S4). 대부분의
        Card 가 첫 시도에 성공하므로 평상시 조회가 0회가 된다.

        그 밖에는 최신부터 역순으로 훑는다. 먼저 만나는 것이 이긴다.

        - 이 event 의 marker → 전달 완료
        - 같은 destination 의 **더 낮은** sequence marker → 미전송. `claim_next` 가 이전
          sequence 미확정 시 다음 event 를 claim 하지 않으므로 (`events.py:2648`) 역순
          조회에서 N-1 을 N 보다 먼저 만났다면 N 은 아직 없다
        - history 소진 → 미전송. 없는 것이 확정이다 (D-023 항목 3)
        - page 상한 도달 → 판정 불가

        **`event_id` 는 같은데 `payload_digest` 가 다른 marker 를 만나면 즉시 멈춘다**
        (`SlackProjectionMarkerDigestMismatchError`). 같은 event 의 다른 내용이 이미
        나갔다는 뜻이라 정상 경로에서 나올 수 없고, 지나가면 뒤에서 미전송으로 판정해
        중복 Card 를 만든다.
        """
        if self._never_attempted(event):
            return None
        cursor: str | None = None
        for _ in range(self.max_history_pages):
            page = self._read_page(cursor, event)
            verdict = self._page_verdict(page, event)
            if not isinstance(verdict, _Undecided):
                return verdict
            if page.next_cursor is None:
                # history 를 끝까지 봤다. 없는 것이 확정이라 미전송이다.
                return None
            cursor = page.next_cursor
        raise SlackProjectionSearchCapError

    def _page_verdict(
        self,
        page: SlackHistoryPage,
        event: OutboxEventView,
    ) -> str | None | _Undecided:
        """Judge one page. **Our own marker wins inside the page, whatever the order.**

        page 안에서는 message 순서에 기대지 않는다. C-1.2 는 transport 에 정렬 순서를
        요구하지만 signature 로 강제할 수 없다. 구현체가 오래된 것부터 돌려주면 하위
        sequence marker 를 우리 marker 보다 먼저 만나 "미전송" 으로 판정하고, 이미 나간
        Card 를 한 장 더 만든다. 그래서 page 를 두 번 훑는다 — 먼저 우리 marker 를
        찾고, 없을 때만 하위 sequence 를 본다.

        page **사이**의 순서는 여전히 계약에 의존한다. 그건 cursor 를 우리가 만들지
        않아 여기서 막을 수 없다.
        """
        for message in page.messages:
            marker = self._our_marker(message)
            if marker is None or marker.event_id != event.event_id:
                continue
            if marker.payload_digest == event.payload_digest:
                return self._receipt(message.ts)
            # 같은 event 가 다른 내용으로 이미 나갔다. 지나가면 뒤에서 미전송으로 판정해
            # 중복 Card 를 만든다.
            raise SlackProjectionMarkerDigestMismatchError
        for message in page.messages:
            marker = self._our_marker(message)
            if marker is None:
                continue
            if marker.destination_sequence < event.destination_sequence:
                return None
        return _UNDECIDED

    def _our_marker(self, message: SlackHistoryMessage) -> SlackMarkerView | None:
        """Read one message's marker, or `None` if it is not this destination's.

        `destination_ref` 대조가 여기 있다. 다른 channel 로 나간 marker 는 sequence
        counter 가 별개라 하위 sequence 판정에 쓰면 안 된다 — 남의 counter 를 우리 것의
        증거로 읽으면 아직 안 나간 Card 를 나갔다고 하거나 그 반대가 된다.
        """
        marker = read_slack_marker(message, app_id=self.app_id)
        if marker is None or marker.destination_ref != self.destination_ref:
            return None
        return marker

    @staticmethod
    def _never_attempted(event: OutboxEventView) -> bool:
        """Answer whether Slack can never have been called for this event.

        outbox row 는 언제나 `attempts=0`·`last_error_code=NULL` 로 생성된다
        (`events.py:2358`-`2359`). 그 값을 되돌리는 곳은 `mark_delivered` 하나뿐인데
        (`events.py:2729`) 그 row 는 `delivered` 라 `claim_next` 가 다시 claim 하지
        않는다. `claim_next` 는 자기 transaction 을 commit 하므로 `attempts=1` 은
        `post_message` 보다 **먼저** durable 하다.

        **두 조건을 함께 봐야 한다.** `attempts` 증가는
        `CASE WHEN attempts < max_attempts` 라 상한에서 멈춘다 (`events.py:2672`).
        `max_attempts == 1` 구성에서는 두 번째 claim 도 `attempts == 1` 로 보인다. 그
        재claim 은 `last_error_code = 'OUTBOX_LEASE_EXPIRED'` 를 요구하므로
        (`events.py:2644`) 두 번째 조건이 그것을 막는다. `attempts` 만 보면 이미 나간
        Card 를 한 번 더 보낸다.
        """
        return event.attempts <= 1 and event.last_error_code is None

    def _read_page(self, cursor: str | None, event: OutboxEventView) -> SlackHistoryPage:
        """Read one page, routing every failure through C-3 classification.

        transport 가 C-1 의 감싸기 의무를 어겨도 분류를 건너뛰지 않는다 (D-020 항목 4).
        send 쪽과 같은 방어선이다.
        """
        try:
            return self._transport.read_history(
                channel=self.channel,
                cursor=cursor,
                limit=SLACK_HISTORY_PAGE_LIMIT,
            )
        except SlackTransportError as error:
            self._raise_for_failure(error, event)
        except Exception as error:
            self._raise_for_failure(self._rewrap(error), event)

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
        is_review = "reviewer_actor_id" in event.payload
        prepared_review = False
        prepared_generation: int | None = None
        action_set: PreparedReviewActionSet | None = None
        presentation: Mapping[str, object] = {}
        if is_review:
            if self._review_action_sets is None:
                raise OutboxReconcileError("REVIEW_ACTION_SET_SERVICE_REQUIRED")
            render_failure_code: str | None = None
            interruption_kind: str | None = None
            # 분류는 `except` 블록 안에서 끝낸다. Python 이 블록을 벗어나면서 예외 변수를
            # 지우므로 뒤에서 다시 볼 수 없다. 예외 객체를 밖으로 들고 나가면 credential 을
            # 든 frame 을 살려두게 되므로 boolean 하나만 남긴다.
            prepare_is_transient = False
            try:
                review_payload = ReviewProjectionPayload.model_validate(event.payload)
                if review_payload.bound_channel_ref.channel_id != self.channel:
                    raise ReviewCardError("REVIEW_CARD_CHANNEL_MISMATCH")
                action_set = self._review_action_sets.prepare(event)
                prepared_review = True
                prepared_generation = action_set.view.generation
                presentation = self._card_renderer.render_review(review_payload, action_set)
            except (ReviewCardError, SlackCardRenderingError, ValueError) as error:
                render_failure_code = str(getattr(error, "code", "SLACK_CARD_RENDER_FAILED"))
                self._clear_exception_frames(error)
            except BaseException as error:
                interruption_kind = self._interruption_kind(error)
                prepare_is_transient = is_transient_store_failure(error)
                self._clear_exception_frames(error)
            if render_failure_code is not None or interruption_kind is not None:
                # Drop every memory-only credential before cleanup. `abandon()` may
                # itself fail and its exception may be rendered with `--showlocals`.
                action_set = None
                presentation = {}
                self._abandon_review_closed(event.event_id, prepared_generation)
                if interruption_kind is not None:
                    # `_abandon_review_closed` 와 같은 기준이다. 위의 `except BaseException`
                    # 은 `sqlite3.OperationalError` 같은 평범한 Exception 도 잡는다. 그것을
                    # interruption 으로 올리면 `deliver_next` 의 두 handler 가 둘 다 놓쳐
                    # dead letter 도 operator hold 도 없이 event 가 `leased` 로 남는다.
                    if interruption_kind != "exception":
                        self._raise_sanitized_interruption(interruption_kind)
                    # **일시적 store 실패는 예산을 쓴다.** 전부 terminal 로 닫으면 sqlite
                    # lock 한 번에 destination 전체가 즉시 멈추고 그 hold 는 되돌릴 수 없다
                    # (round 11 `R-2`). 같은 저장소의 `ingress_worker` 는 같은 예외를
                    # 재시도로 분류한다. 한 저장소 한 정책으로 맞춘다 — 판별은
                    # `store.is_transient_store_failure` 하나가 한다.
                    if prepare_is_transient:
                        raise OutboxRetryableError("REVIEW_CARD_PREPARE_UNAVAILABLE") from None
                    raise OutboxReconcileError("REVIEW_CARD_PREPARE_FAILED") from None
                assert render_failure_code is not None
                raise OutboxReconcileError(render_failure_code) from None
        else:
            try:
                presentation = slack_presentation_payload(
                    event.payload,
                    renderer=self._card_renderer,
                )
            except SlackCardRenderingError as error:
                raise OutboxReconcileError(error.code) from error
        # Marker 구성부터 post 반환까지 한 interruption 경계로 묶는다. Review presentation이
        # raw action credential을 가진 뒤에는 이 사이 어느 bytecode에서 중단돼도 아래
        # `finally`가 credential-bearing local을 비워야 한다. Marker 자체의 결함은 별도
        # code로 남겨 transport 구현자 탓으로 분류하지 않는다.
        transport_failure: SlackTransportError | None = None
        marker_failure = False
        interruption_kind = None
        post_started = False
        result: SlackSendResult | None = None
        try:
            marker = build_slack_marker(event)
            post_started = True
            result = self._transport.post_message(
                channel=self.channel,
                payload=presentation,
                marker=marker,
            )
        except SlackTransportError as error:
            if post_started:
                transport_failure = self._sanitized_transport_error(error) if is_review else error
            else:
                marker_failure = True
                self._clear_exception_frames(error)
        except Exception as error:
            if post_started:
                # C-1 의무 위반에 대한 두 번째 방어선이다. 넓게 잡는 것이 의도다 — 좁히면
                # 새어 나온 예외가 dispatcher 의 generic handler 로 가서 원인 없이 재시도된다.
                transport_failure = self._rewrap(error, preserve_cause=not is_review)
            else:
                marker_failure = True
                self._clear_exception_frames(error)
        except BaseException as error:
            if not is_review:
                raise
            interruption_kind = self._interruption_kind(error)
            self._clear_exception_frames(error)
        finally:
            if is_review:
                # This executes for success, classified failures, and process-level
                # interruptions. The transport traceback is scrubbed above; this
                # frame must also stop retaining the presentation and raw tokens.
                action_set = None
                presentation = {}
        if marker_failure:
            if prepared_review:
                self._abandon_review_closed(event.event_id, prepared_generation)
            raise OutboxReconcileError("SLACK_MARKER_BUILD_FAILED") from None
        if interruption_kind is not None:
            if prepared_review and not post_started:
                # Marker 단계에서는 remote post가 시작되지 않았으므로 exact generation을
                # 안전하게 닫는다. Transport 호출 뒤 interruption은 remote acceptance가
                # 모호하므로 issued 상태를 남겨 reconcile이 판단하게 한다.
                self._abandon_review_closed(event.event_id, prepared_generation)
            self._raise_sanitized_interruption(interruption_kind)
        if transport_failure is not None:
            if (
                prepared_review
                and classify_slack_failure(transport_failure) is SlackFailureClass.TERMINAL
                and self._review_action_sets is not None
            ):
                self._abandon_review_closed(event.event_id, prepared_generation)
            self._raise_for_failure(transport_failure, event)
        assert result is not None
        if result.channel != self.channel:
            if prepared_review:
                self._abandon_review_closed(event.event_id, prepared_generation)
            raise OutboxReconcileError("SLACK_RESPONSE_CHANNEL_MISMATCH")
        return self._receipt(result.ts)

    def _abandon_review_closed(self, event_id: str, generation: int | None) -> None:
        """Revoke one undelivered action set without exposing cleanup internals."""
        if generation is None:
            return
        if self._review_action_sets is None:
            raise OutboxReconcileError("REVIEW_ACTION_SET_SERVICE_REQUIRED")
        cleanup_failure_kind: str | None = None
        try:
            self._review_action_sets.abandon(event_id, generation)
        except BaseException as error:
            cleanup_failure_kind = self._interruption_kind(error)
            self._clear_exception_frames(error)
        if cleanup_failure_kind is not None:
            if cleanup_failure_kind != "exception":
                self._raise_sanitized_interruption(cleanup_failure_kind)
            raise OutboxReconcileError("REVIEW_ACTION_SET_CLEANUP_FAILED") from None

    @staticmethod
    def _interruption_kind(error: BaseException) -> str:
        """Delegate to the single shared classifier. 사본을 만들지 않는다."""
        return classify_interruption(error)

    @staticmethod
    def _raise_sanitized_interruption(kind: str) -> NoReturn:
        """Delegate to the single shared re-raiser with this adapter's message."""
        raise_sanitized_interruption(kind, _REVIEW_INTERRUPTION_MESSAGE)

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
    def _rewrap(
        error: BaseException,
        *,
        preserve_cause: bool = True,
    ) -> SlackTransportError:
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
        if preserve_cause:
            wrapped.__cause__ = error
        else:
            SlackProjectionDestination._clear_exception_frames(error)
        return wrapped

    @staticmethod
    def _sanitized_transport_error(error: SlackTransportError) -> SlackTransportError:
        safe = SlackTransportError(
            "Slack Review Card delivery failed.",
            error_code=safe_slack_error_code(error.error_code),
            status_code=error.status_code,
            retry_after_seconds=error.retry_after_seconds,
            transport_exception=safe_transport_exception(error.transport_exception),
        )
        SlackProjectionDestination._clear_exception_frames(error)
        return safe

    @staticmethod
    def _clear_exception_frames(error: BaseException) -> None:
        seen: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if current.__traceback__ is not None:
                traceback.clear_frames(current.__traceback__)
                current.__traceback__ = None
            next_error = current.__cause__ or current.__context__
            current.__cause__ = None
            current.__context__ = None
            current = next_error

    def _receipt(self, ts: str) -> str:
        """Build the receipt both `send()` and `reconcile()` must agree on (C-2.3).

        `{channel}` 은 생성자가 받은 값이다. `SlackSendResult.channel` 이 아니다 —
        `reconcile()` 은 `conversations.history` message 에서 channel 을 못 얻어
        (`ts`·`metadata`·`app_id` 셋뿐) 생성자 값밖에 쓸 수 없다. 두 경로가 다른 문자열을
        만들면 `mark_delivered` 가 `OUTBOX_DELIVERY_RESULT_CONFLICT` 를 던진다
        (`events.py:2717`).
        """
        return f"slack:{self.channel}:{ts}"
