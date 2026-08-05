from __future__ import annotations

import ast
import inspect
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from amplai_foundry.governance import slack_projection
from amplai_foundry.governance.events import OutboxConfig, OutboxDispatcher, OutboxReconcileError
from amplai_foundry.governance.slack_projection import (
    RETRYABLE_SLACK_ERROR_CODES,
    SLACK_PROJECTION_EVENT_TYPE,
    SlackFailureClass,
    SlackHistoryMessage,
    SlackHistoryPage,
    SlackProjectionTerminalError,
    SlackSendResult,
    SlackTransport,
    SlackTransportError,
    classify_slack_failure,
    persisted_code_suffix,
    raise_for_slack_failure,
)
from amplai_foundry.governance.store import GovernanceStore

MODULE_PATH = Path(inspect.getfile(slack_projection))


class FakeSlackTransport:
    """In-memory stand-in for one Slack channel.

    실제 호출은 Package 4 몫이다. 여기서는 계약만 만족시킨다.
    """

    def __init__(self, *, page_size: int = 2) -> None:
        self.page_size = page_size
        self.posted: list[SlackHistoryMessage] = []
        self.history_calls: list[tuple[str | None, int]] = []
        self.failure: SlackTransportError | None = None
        self.read_failure: SlackTransportError | None = None
        self._counter = 0

    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        if self.failure is not None:
            raise self.failure
        self._counter += 1
        ts = f"1700000000.{self._counter:06d}"
        self.posted.append(SlackHistoryMessage(ts=ts, metadata=dict(marker), app_id="A123"))
        return SlackSendResult(channel=channel, ts=ts)

    def read_history(
        self,
        *,
        channel: str,
        cursor: str | None,
        limit: int,
    ) -> SlackHistoryPage:
        if self.read_failure is not None:
            raise self.read_failure
        self.history_calls.append((cursor, limit))
        newest_first = list(reversed(self.posted))
        start = int(cursor) if cursor is not None else 0
        window = newest_first[start : start + self.page_size]
        following = start + self.page_size
        next_cursor = str(following) if following < len(newest_first) else None
        return SlackHistoryPage(messages=window, next_cursor=next_cursor)


def _transport_failure(
    *,
    error_code: str | None = None,
    status_code: int | None = None,
) -> SlackTransportError:
    return SlackTransportError(
        "slack call failed",
        error_code=error_code,
        status_code=status_code,
    )


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# contracts/slack-transport.md C-1 이 고정한 signature. annotation 만으로는 runtime 에
# 아무것도 증명되지 않고 mypy 는 기본 설정에서 tests/ 를 보지 않으므로 여기서 직접
# 대조한다. 비교는 문자열이 아니라 resolve 된 객체로 한다 — alias 나 동명 class 가
# 문자열 비교를 통과하거나 실패시키는 것을 막는다.
PROTOCOL_SIGNATURES = {
    "post_message": (
        {"channel": str, "payload": Mapping[str, object], "marker": Mapping[str, object]},
        SlackSendResult,
    ),
    "read_history": (
        {"channel": str, "cursor": str | None, "limit": int},
        SlackHistoryPage,
    ),
}


def _resolved_signature(owner: type, name: str) -> inspect.Signature:
    return inspect.signature(getattr(owner, name), eval_str=True)


def test_protocol_exposes_exactly_the_contracted_methods() -> None:
    public = {n for n in vars(SlackTransport) if not n.startswith("_")}
    assert public == set(PROTOCOL_SIGNATURES)


@pytest.mark.parametrize("name", sorted(PROTOCOL_SIGNATURES))
def test_protocol_signature_matches_the_contract(name: str) -> None:
    expected_params, expected_return = PROTOCOL_SIGNATURES[name]
    signature = _resolved_signature(SlackTransport, name)
    parameters = [p for p in signature.parameters.values() if p.name != "self"]
    assert {p.name: p.annotation for p in parameters} == expected_params
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters)
    assert signature.return_annotation is expected_return


@pytest.mark.parametrize("name", sorted(PROTOCOL_SIGNATURES))
def test_fake_transport_matches_the_protocol_signature(name: str) -> None:
    protocol = _resolved_signature(SlackTransport, name)
    fake = _resolved_signature(FakeSlackTransport, name)
    assert list(protocol.parameters) == list(fake.parameters)
    assert [(p.kind, p.annotation) for p in protocol.parameters.values()] == [
        (p.kind, p.annotation) for p in fake.parameters.values()
    ]
    assert protocol.return_annotation is fake.return_annotation


def test_fake_transport_is_usable_through_the_protocol() -> None:
    transport: SlackTransport = FakeSlackTransport()
    result = transport.post_message(channel="C1", payload={"text": "hi"}, marker={"a": 1})
    assert result.channel == "C1"
    assert transport.read_history(channel="C1", cursor=None, limit=10).messages


def test_fake_transport_can_fail_reads_for_later_waves() -> None:
    transport = FakeSlackTransport()
    transport.read_failure = _transport_failure(error_code="missing_scope")
    with pytest.raises(SlackTransportError):
        transport.read_history(channel="C1", cursor=None, limit=10)


# AC-01 — research.md R-006 이 고정한 여섯 code. 이 집합이 곧 계약이다.
def test_retryable_allowlist_is_exactly_the_researched_set() -> None:
    researched = frozenset(
        {
            "fatal_error",
            "internal_error",
            "rate_limited",
            "ratelimited",
            "request_timeout",
            "service_unavailable",
        }
    )
    assert researched == RETRYABLE_SLACK_ERROR_CODES


# AC-01
@pytest.mark.parametrize("error_code", sorted(RETRYABLE_SLACK_ERROR_CODES))
def test_every_allowlisted_code_is_retryable(error_code: str) -> None:
    assert (
        classify_slack_failure(_transport_failure(error_code=error_code))
        is SlackFailureClass.RETRYABLE
    )


# AC-04
def test_rate_limit_status_is_retryable_without_an_error_code() -> None:
    assert (
        classify_slack_failure(_transport_failure(status_code=429)) is SlackFailureClass.RETRYABLE
    )


# AC-08 — 규칙 순서가 우선순위다. 429 검사가 code 검사보다 앞이라 allowlist 밖 code 를
# 달고 온 429 도 재시도한다. 영구 실패면 attempt 를 소진하고 같은 dead letter 로 간다.
def test_rate_limit_status_wins_over_a_terminal_error_code() -> None:
    failure = _transport_failure(error_code="invalid_auth", status_code=429)
    assert classify_slack_failure(failure) is SlackFailureClass.RETRYABLE


# AC-02
def test_channel_not_found_is_terminal() -> None:
    assert (
        classify_slack_failure(_transport_failure(error_code="channel_not_found"))
        is SlackFailureClass.TERMINAL
    )


# AC-02
def test_terminal_failure_raises_a_reconcile_error_the_dispatcher_dead_letters() -> None:
    failure = _transport_failure(error_code="channel_not_found")
    with pytest.raises(SlackProjectionTerminalError) as caught:
        raise_for_slack_failure(failure)
    assert isinstance(caught.value, OutboxReconcileError)
    # dispatcher 는 예외 객체를 버리고 이 문자열만 dead letter 와 hold 에 적는다.
    # 원인이 문자열에 없으면 operator 가 복구 행동을 고를 수 없다.
    assert caught.value.code == "SLACK_PROJECTION_TERMINAL_ERROR:channel_not_found"
    assert caught.value.slack_error_code == "channel_not_found"
    assert caught.value.__cause__ is failure


def test_terminal_error_without_a_slack_code_is_still_labelled() -> None:
    assert SlackProjectionTerminalError(None).code == "SLACK_PROJECTION_TERMINAL_ERROR:unknown"


# AC-01
def test_retryable_failure_is_re_raised_as_itself() -> None:
    failure = _transport_failure(error_code="internal_error")
    with pytest.raises(SlackTransportError) as caught:
        raise_for_slack_failure(failure)
    assert caught.value is failure
    assert not isinstance(caught.value, OutboxReconcileError)


# AC-03
@pytest.mark.parametrize(
    "error_code",
    ["not_in_channel", "invalid_blocks", "metadata_too_large", "unheard_of_future_code"],
)
def test_error_codes_outside_the_allowlist_are_terminal(error_code: str) -> None:
    assert error_code not in RETRYABLE_SLACK_ERROR_CODES
    assert (
        classify_slack_failure(_transport_failure(error_code=error_code))
        is SlackFailureClass.TERMINAL
    )


# AC-04
@pytest.mark.parametrize("status_code", [None, 500, 502, 503])
def test_transport_layer_failures_are_retryable(status_code: int | None) -> None:
    assert (
        classify_slack_failure(_transport_failure(status_code=status_code))
        is SlackFailureClass.RETRYABLE
    )


# AC-04 — Slack code 가 없으면 status 로 가르지 않는다. code 없는 HTTP status 는 거의
# 전부 중간 장비가 낸 것이고 그건 transient 다. 영구 실패라도 attempt 소진 후 같은
# dead letter 에 도달하므로 손해는 몇십 초다. 반대 방향은 되돌릴 수 없는 hold 다.
@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 407, 408, 425, 499])
def test_client_status_without_a_slack_code_is_retryable(status_code: int) -> None:
    assert (
        classify_slack_failure(_transport_failure(status_code=status_code))
        is SlackFailureClass.RETRYABLE
    )


# AC-06 — 빈 문자열은 `is None` 검사를 빠져나가 전체 분류를 뒤집는다. 생성자에서 흡수한다.
@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_error_code_is_treated_as_absent(raw: str) -> None:
    failure = _transport_failure(error_code=raw)
    assert failure.error_code is None
    assert classify_slack_failure(failure) is SlackFailureClass.RETRYABLE


# AC-06
@pytest.mark.parametrize("raw", ["Ratelimited", " ratelimited ", "RATELIMITED"])
def test_error_code_matching_is_case_and_whitespace_insensitive(raw: str) -> None:
    failure = _transport_failure(error_code=raw)
    assert failure.error_code == "ratelimited"
    assert classify_slack_failure(failure) is SlackFailureClass.RETRYABLE


# AC-03 — 형식이 이상해도 분류에서 버리지 않는다. 버리면 allowlist 밖 code 가 code 없음이
# 되어 retryable 로 넘어가고, 그 경로는 원인을 안 남긴다.
@pytest.mark.parametrize(
    "raw",
    [
        "x" * 65,
        "missing_scope: chat:write",
        "channel not found",
        "channel\nnot_found",
        "<b>oops</b>",
    ],
)
def test_malformed_error_code_still_classifies_as_terminal(raw: str) -> None:
    failure = _transport_failure(error_code=raw)
    assert failure.error_code is not None
    assert classify_slack_failure(failure) is SlackFailureClass.TERMINAL


# AC-06 — 원본은 진단용으로 남는다.
def test_raw_error_code_survives_normalization() -> None:
    failure = _transport_failure(error_code="  Missing_Scope  ")
    assert failure.raw_error_code == "  Missing_Scope  "
    assert failure.error_code == "missing_scope"


# AC-06 — 생성자가 예외를 던지면 분류 자체가 안 돈다. non-str 도 버리지 않고 문자열로
# 만든다. 버리면 code 없음이 되어 retryable 경로로 새면서 원인을 잃는다.
def test_non_string_error_code_is_stringified_not_discarded() -> None:
    failure = SlackTransportError("bad payload", error_code=cast("str", b"invalid_auth"))
    assert failure.error_code == "b'invalid_auth'"
    assert cast("object", failure.raw_error_code) == b"invalid_auth"
    assert classify_slack_failure(failure) is SlackFailureClass.TERMINAL
    assert "invalid_auth" in SlackProjectionTerminalError(failure.error_code).code


# AC-07 — 저장되는 접미사만 형식을 강제한다. 버리지 않고 다듬는다.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("missing_scope", "missing_scope"),
        ("  Missing_Scope  ", "missing_scope"),
        (None, "unknown"),
        ("", "unknown"),
        ("   ", "unknown"),
    ],
)
def test_persisted_suffix_keeps_well_formed_codes_verbatim(raw: str | None, expected: str) -> None:
    assert persisted_code_suffix(raw) == expected


# AC-07 — 다듬느라 정보가 사라지면 원본 digest 를 붙인다. 서로 다른 원본이 같은 row 를
# 남기면 operator 가 두 사건을 구분할 수 없다.
@pytest.mark.parametrize(
    ("raw", "head"),
    [
        ("missing_scope: chat:write", "missing_scope__chat_write"),
        ("channel not found", "channel_not_found"),
        ("###", "___"),
        ("인증 실패", "_____"),
        ("x" * 100, "x" * 55),
    ],
)
def test_persisted_suffix_marks_lossy_shaping_with_a_digest(raw: str, head: str) -> None:
    suffix = persisted_code_suffix(raw)
    assert suffix.startswith(f"{head}_")
    assert len(suffix) <= 64


def test_persisted_suffix_separates_inputs_that_sanitize_alike() -> None:
    assert persisted_code_suffix("###") != persisted_code_suffix("%%%")
    assert persisted_code_suffix("a" * 64 + "b") != persisted_code_suffix("a" * 64 + "c")


# AC-07
def test_terminal_code_stays_within_the_persisted_identifier_shape() -> None:
    assert (
        SlackProjectionTerminalError("channel_not_found").code
        == "SLACK_PROJECTION_TERMINAL_ERROR:channel_not_found"
    )
    assert len(SlackProjectionTerminalError("x" * 100).code) <= 96


# AC-09 — Slack 은 Retry-After 상한을 문서화하지 않는다 (research.md R-007). 그래서 예산이
# 충분한지 판정하지 않고 기본 schedule 을 사실로만 고정한다. Package 4 가 OutboxConfig 를
# 만들 때 이 schedule 을 입력으로 쓴다.
def test_default_retry_schedule_is_pinned() -> None:
    config = OutboxConfig()
    # store 는 backoff 계산에 관여하지 않는다. 실제 dispatcher 의 계산식
    # (events.py:2980 `_backoff_seconds`) 을 그대로 쓰기 위해 instance 를 만든다.
    dispatcher = OutboxDispatcher(cast("GovernanceStore", None), config=config)
    waits = [dispatcher._backoff_seconds(attempt) for attempt in range(1, config.max_attempts)]
    assert waits == [5, 10, 20, 40]
    assert sum(waits) == 75


def test_retry_after_is_carried_but_not_consumed() -> None:
    failure = SlackTransportError("slow down", error_code="ratelimited", retry_after_seconds=17)
    assert failure.retry_after_seconds == 17
    assert classify_slack_failure(failure) is SlackFailureClass.RETRYABLE


def test_event_type_is_a_stable_non_empty_identifier() -> None:
    assert SLACK_PROJECTION_EVENT_TYPE == "amplai_proposal_card"


def test_send_result_rejects_blank_identity() -> None:
    with pytest.raises(ValueError):
        SlackSendResult(channel=" ", ts="1700000000.000100")
    with pytest.raises(ValueError):
        SlackSendResult(channel="C1", ts=" ")


def test_history_message_metadata_is_read_only() -> None:
    message = SlackHistoryMessage(ts="1700000000.000100", metadata={"event_type": "x"})
    assert message.metadata is not None
    with pytest.raises(TypeError):
        message.metadata["event_type"] = "y"  # type: ignore[index]


def test_history_page_normalizes_messages_to_a_tuple() -> None:
    messages: Sequence[SlackHistoryMessage] = [SlackHistoryMessage(ts="1700000000.000100")]
    page = SlackHistoryPage(messages=messages)
    assert isinstance(page.messages, tuple)


# AC-05
def test_module_imports_no_third_party_dependency_beyond_pydantic() -> None:
    allowed = set(sys.stdlib_module_names) | {"amplai_foundry", "pydantic"}
    assert _imported_roots(MODULE_PATH) <= allowed


# AC-08 — status_code 오염은 되돌릴 수 없는 방향으로 흐른다. "429" 가 규칙 1 을 빠져나가면
# transient rate limit 이 즉시 hold 를 만든다.
@pytest.mark.parametrize("status_code", ["429", " 429 "])
def test_rate_limit_status_is_read_defensively(status_code: str) -> None:
    failure = SlackTransportError(
        "rate limited",
        error_code="invalid_auth",
        status_code=cast("int", status_code),
    )
    assert classify_slack_failure(failure) is SlackFailureClass.RETRYABLE


@pytest.mark.parametrize("status_code", [True, "not-a-number", object()])
def test_unreadable_status_code_does_not_trigger_the_rate_limit_rule(status_code: object) -> None:
    failure = SlackTransportError(
        "odd status",
        error_code="channel_not_found",
        status_code=cast("int", status_code),
    )
    assert classify_slack_failure(failure) is SlackFailureClass.TERMINAL
