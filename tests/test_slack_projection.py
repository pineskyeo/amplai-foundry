from __future__ import annotations

import ast
import inspect
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

# dispatcher 를 실물로 돌리는 fixture 는 test_governance_events 에 이미 있다. 복제하면
# 두 벌이 어긋난다. tests/ 에 __init__.py 가 없어 pytest 가 그 디렉터리를 sys.path 에
# 넣으므로 top-level module 로 import 된다.
import test_governance_events as governance_fixtures
from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance import slack_projection
from amplai_foundry.governance.events import (
    GovernanceEventError,
    GovernanceEventService,
    OutboxConfig,
    OutboxDispatcher,
    OutboxEventView,
    OutboxReconcileError,
    OutboxState,
    ProjectionDestination,
)
from amplai_foundry.governance.models import ProposalRef
from amplai_foundry.governance.projections import YamlProjectionDestination
from amplai_foundry.governance.slack_projection import (
    RETRYABLE_SLACK_ERROR_CODES,
    SLACK_HISTORY_PAGE_LIMIT,
    SLACK_MAX_HISTORY_PAGES,
    SLACK_PROJECTION_EVENT_TYPE,
    SlackFailureClass,
    SlackHistoryMessage,
    SlackHistoryPage,
    SlackProjectionDestination,
    SlackProjectionMarkerDigestMismatchError,
    SlackProjectionMetadataUnreadableError,
    SlackProjectionRetryExhaustedError,
    SlackProjectionSearchCapError,
    SlackProjectionTerminalError,
    SlackSendResult,
    SlackTransport,
    SlackTransportError,
    build_slack_marker,
    classify_slack_failure,
    payload_digest,
    persisted_code_suffix,
    raise_for_slack_failure,
    read_slack_marker,
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


# --------------------------------------------------------------------------------------
# MGC-012-T002 — SlackProjectionDestination.send()
# --------------------------------------------------------------------------------------

DESTINATION_REF = "provider:slack:sha256:" + "ab" * 32
CHANNEL = "C0SLACK01"
# FakeSlackTransport 가 post 할 때 쓰는 값과 같아야 한다. reconcile 이 app_id 로 남의
# metadata 를 배제하므로 어긋나면 우리 marker 도 못 읽는다.
APP_ID = "A123"
MAX_ATTEMPTS = OutboxConfig().max_attempts
PROPOSAL = ProposalRef(
    project_ref=ProjectRef(project_id="amplai", namespace="org/default/project/amplai"),
    proposal_id="PROP-20260730-ABCDEF12",
)
PAYLOAD: dict[str, object] = {"text": "제안 카드", "blocks": [{"type": "section"}]}


def _event(
    *,
    payload: dict[str, object] | None = None,
    digest: str | None = None,
    destination_ref: str = DESTINATION_REF,
    destination_sequence: int = 1,
    attempts: int = 1,
) -> OutboxEventView:
    body = dict(PAYLOAD if payload is None else payload)
    return OutboxEventView(
        # 순번이 다르면 다른 event 다. 하나로 고정하면 "직전 카드" 와 "현재 카드" 가
        # 같은 event 가 되어 reconcile test 가 조용히 무의미해진다.
        event_id=f"EVT-{destination_sequence:016d}",
        proposal_ref=PROPOSAL,
        aggregate_sequence=destination_sequence,
        destination_ref=destination_ref,
        destination_sequence=destination_sequence,
        source_state_revision=1,
        payload_digest=digest or payload_digest(body),
        payload=body,
        state=OutboxState.LEASED,
        attempts=attempts,
        claim_generation=attempts,
        created_at=datetime(2026, 8, 5, tzinfo=UTC),
    )


def _destination(
    transport: FakeSlackTransport,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    max_history_pages: int = SLACK_MAX_HISTORY_PAGES,
) -> SlackProjectionDestination:
    return SlackProjectionDestination(
        transport,
        destination_ref=DESTINATION_REF,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=max_history_pages,
        max_attempts=max_attempts,
    )


class ExplodingTransport(FakeSlackTransport):
    """C-1 의 감싸기 의무를 어기는 구현. destination 의 두 번째 방어선을 검사한다."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        raise self.error


# T003 이 reconcile 을 붙여 이제 Protocol 을 실제로 만족한다. annotation 으로 적으면
# runtime 에 아무것도 증명되지 않고 mypy 는 tests/ 를 안 본다
# (pyproject.toml packages = ["amplai_foundry"]) — 구성원과 signature 를 직접 대조한다.
def test_destination_satisfies_the_projection_destination_protocol() -> None:
    required = {n for n in vars(ProjectionDestination) if not n.startswith("_")}
    assert required == {"reconcile", "send"}
    destination = _destination(FakeSlackTransport())
    assert destination.destination_ref == DESTINATION_REF
    for name in required:
        protocol = _resolved_signature(ProjectionDestination, name)
        actual = _resolved_signature(SlackProjectionDestination, name)
        assert list(protocol.parameters) == list(actual.parameters)
        assert protocol.return_annotation == actual.return_annotation


# T002 AC-01 — 검증이 transport 앞이라는 것이 계약이다. 뒤로 가면 손상된 payload 가 사람에게
# 보이는 Card 로 나가고 chat.update 로 되돌리는 것은 범위 밖이다 (research R-008).
def test_destination_mismatch_fails_before_the_transport_is_called() -> None:
    transport = FakeSlackTransport()
    with pytest.raises(OutboxReconcileError) as caught:
        _destination(transport).send(_event(destination_ref="provider:slack:other"))
    assert caught.value.code == "OUTBOX_DESTINATION_MISMATCH"
    assert transport.posted == []


# T002 AC-02
def test_payload_integrity_failure_fails_before_the_transport_is_called() -> None:
    transport = FakeSlackTransport()
    tampered = _event(digest=payload_digest({"text": "다른 내용"}))
    with pytest.raises(OutboxReconcileError) as caught:
        _destination(transport).send(tampered)
    assert caught.value.code == "OUTBOX_PAYLOAD_INTEGRITY_FAILURE"
    assert transport.posted == []


# T002 AC-02 — 검증 순서도 계약이다. 둘 다 어긋난 event 는 mismatch 로 먼저 걸린다
# (projections.py:57-61 과 같다).
def test_destination_mismatch_is_checked_before_payload_integrity() -> None:
    broken = _event(
        destination_ref="provider:slack:other",
        digest=payload_digest({"text": "다른 내용"}),
    )
    with pytest.raises(OutboxReconcileError) as caught:
        _destination(FakeSlackTransport()).send(broken)
    assert caught.value.code == "OUTBOX_DESTINATION_MISMATCH"


# T002 AC-03
def test_send_returns_a_slack_receipt() -> None:
    transport = FakeSlackTransport()
    receipt = _destination(transport).send(_event())
    assert receipt == f"slack:{CHANNEL}:{transport.posted[0].ts}"


# T002 AC-03 / C-2.3 — receipt 의 channel 은 생성자 값이다. reconcile 은 history message 에서
# channel 을 못 얻으므로 (ts·metadata·app_id 셋뿐) transport 가 다른 표현을 돌려줘도
# 두 경로가 같은 문자열을 만들어야 한다. 갈라지면 mark_delivered 가
# OUTBOX_DELIVERY_RESULT_CONFLICT 를 던진다 (events.py:2717).
def test_receipt_uses_the_configured_channel_not_the_response_channel() -> None:
    class RenamingTransport(FakeSlackTransport):
        def post_message(
            self,
            *,
            channel: str,
            payload: Mapping[str, object],
            marker: Mapping[str, object],
        ) -> SlackSendResult:
            result = super().post_message(channel=channel, payload=payload, marker=marker)
            return SlackSendResult(channel="C_RENAMED_BY_SLACK", ts=result.ts)

    transport = RenamingTransport()
    assert _destination(transport).send(_event()).startswith(f"slack:{CHANNEL}:")


# C-2.3 의 나머지 절반 — send 와 reconcile 이 같은 문자열을 만드는지 — 는 reconcile 이
# 없는 T002 에서 검사할 수 없다. T003 이 닫는다.


# T002 AC-04
def test_marker_carries_the_four_identity_fields() -> None:
    marker = build_slack_marker(_event(destination_sequence=7))
    assert marker["event_type"] == SLACK_PROJECTION_EVENT_TYPE
    assert marker["event_payload"] == {
        "event_id": "EVT-0000000000000007",
        "destination_ref": DESTINATION_REF,
        "destination_sequence": 7,
        "payload_digest": payload_digest(PAYLOAD),
    }


# T002 AC-04 — payload 본문을 넣으면 metadata_too_large 가 난다. 그 code 는 allowlist 밖이라
# terminal 이고 terminal 은 되돌릴 수 없는 hold 를 만든다 (research R-003 미확인).
def test_marker_never_carries_the_payload_body() -> None:
    transport = FakeSlackTransport()
    _destination(transport).send(_event())
    metadata = transport.posted[0].metadata
    assert metadata is not None
    serialized = repr(dict(metadata))
    assert "제안 카드" not in serialized
    assert "blocks" not in serialized


# T002 AC-04 — transport 가 실제로 받는 세 인자를 그대로 고정한다. marker 를 여기서 안 보면
# send() 가 marker 를 통째로 빼먹어도 test 가 전부 통과한다. marker 없이 나간 Card 는
# reconcile 이 못 읽어 재시도마다 사람이 보는 channel 에 중복 Card 를 만들고, chat.update
# 로 되돌리는 것은 범위 밖이다 (research R-008).
def test_send_passes_the_configured_channel_payload_and_marker() -> None:
    seen: dict[str, object] = {}

    class RecordingTransport(FakeSlackTransport):
        def post_message(
            self,
            *,
            channel: str,
            payload: Mapping[str, object],
            marker: Mapping[str, object],
        ) -> SlackSendResult:
            seen["channel"] = channel
            seen["payload"] = dict(payload)
            seen["marker"] = dict(marker)
            return super().post_message(channel=channel, payload=payload, marker=marker)

    event = _event()
    _destination(RecordingTransport()).send(event)
    assert seen == {
        "channel": CHANNEL,
        "payload": PAYLOAD,
        "marker": build_slack_marker(event),
    }


# T002 AC-05 — 두 destination 이 같은 event 를 다르게 판정하면 하나는 보내고 하나는 막는다.
def test_digest_rule_matches_the_yaml_destination(tmp_path: Path) -> None:
    yaml_destination = YamlProjectionDestination(
        tmp_path / "projection.yaml",
        destination_ref=DESTINATION_REF,
    )
    # digest 를 우리 규칙으로 계산한 event 를 YAML destination 이 무결성 실패 없이
    # 받아들이면 두 규칙이 같다. private helper 를 들여다보지 않고 행동으로 대조한다.
    # 규칙이 어긋나면 send 가 OUTBOX_PAYLOAD_INTEGRITY_FAILURE 로 터진다.
    event = _event()
    assert yaml_destination.send(event) == f"yaml:1:{event.payload_digest}"


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "한글"},
        {"b": 1, "a": 2},
        {"nested": {"z": [1, 2], "a": None}},
        {},
    ],
)
def test_digest_matches_the_yaml_destination_for_awkward_payloads(
    payload: dict[str, object],
    tmp_path: Path,
) -> None:
    yaml_destination = YamlProjectionDestination(
        tmp_path / "projection.yaml",
        destination_ref=DESTINATION_REF,
    )
    event = _event(payload=payload)
    assert yaml_destination.send(event) == f"yaml:1:{event.payload_digest}"


# T002 AC-06 — C-1 의무를 어긴 구현이 있어도 분류를 건너뛰지 않는다 (D-020 항목 4).
def test_unwrapped_transport_exception_is_rewrapped_and_classified() -> None:
    transport = ExplodingTransport(ConnectionResetError("peer closed"))
    with pytest.raises(SlackTransportError) as caught:
        _destination(transport).send(_event())
    assert not isinstance(caught.value, OutboxReconcileError)
    assert caught.value.error_code is None
    assert caught.value.transport_exception == "ConnectionResetError"
    assert isinstance(caught.value.__cause__, ConnectionResetError)


# T002 AC-06 — 재감쌀 때 error_code 를 채우면 allowlist 밖이라 terminal 이 되고, 구현 결함이
# 되돌릴 수 없는 hold 로 바뀐다. code 없음이 보수적인 쪽이다 (D-020 항목 1).
@pytest.mark.parametrize(
    "error",
    [ValueError("bad"), KeyError("ok"), RuntimeError("boom"), TimeoutError()],
)
def test_rewrapped_exceptions_stay_retryable(error: Exception) -> None:
    transport = ExplodingTransport(error)
    with pytest.raises(SlackTransportError) as caught:
        _destination(transport).send(_event())
    assert classify_slack_failure(caught.value) is SlackFailureClass.RETRYABLE


# T002 AC-06 — terminal 신호로 새어 나온 예외도 재감싸진다. 결과는 retryable 이라 attempt 를
# 소진한 뒤 같은 dead letter 에 도달한다.
def test_send_never_lets_an_unclassified_exception_escape() -> None:
    transport = ExplodingTransport(OutboxReconcileError("SOMETHING_ELSE"))
    with pytest.raises(SlackTransportError) as caught:
        _destination(transport).send(_event())
    assert caught.value.transport_exception == (
        "amplai_foundry_governance_events_OutboxReconcileError"
    )


# T002 AC-06 — 우리 자신의 사전 검증 실패는 재감싸지 않는다. transport 앞에서 나므로 애초에
# try 블록 밖이다.
def test_validation_errors_are_not_rewrapped_as_transport_failures() -> None:
    with pytest.raises(OutboxReconcileError) as caught:
        _destination(FakeSlackTransport()).send(_event(destination_ref="provider:slack:other"))
    assert not isinstance(caught.value, SlackTransportError)


# T002 AC-09 — D-022. 두 사전 검증 실패는 `OutboxReconcileError` 여야 한다. 부모인
# `GovernanceEventError` 로 던지면 deliver_next 의 unreconcilable 경로를 못 타
# (events.py:2856) 원인이 OUTBOX_DELIVERY_FAILED 로 덮인다. 부모 관계 때문에 타입 검사만
# 하면 통과하므로 여기서 방향을 명시한다.
@pytest.mark.parametrize(
    ("event", "code"),
    [
        (_event(destination_ref="provider:slack:other"), "OUTBOX_DESTINATION_MISMATCH"),
        (_event(digest=payload_digest({"text": "다른 내용"})), "OUTBOX_PAYLOAD_INTEGRITY_FAILURE"),
    ],
)
def test_pre_send_validation_failures_are_unreconcilable(
    event: OutboxEventView,
    code: str,
) -> None:
    with pytest.raises(OutboxReconcileError) as caught:
        _destination(FakeSlackTransport()).send(event)
    assert type(caught.value) is OutboxReconcileError
    assert caught.value.code == code


# T002 AC-02 (T001 계약 유지) — terminal Slack code 는 attempt 와 무관하게 즉시 DLQ 다.
@pytest.mark.parametrize("attempts", [1, MAX_ATTEMPTS])
def test_terminal_slack_error_dead_letters_regardless_of_attempts(attempts: int) -> None:
    transport = FakeSlackTransport()
    transport.failure = _transport_failure(error_code="channel_not_found")
    with pytest.raises(SlackProjectionTerminalError) as caught:
        _destination(transport).send(_event(attempts=attempts))
    assert caught.value.code == "SLACK_PROJECTION_TERMINAL_ERROR:channel_not_found"


# T002 AC-07 — 마지막 attempt 전에는 원래 예외를 그대로 올린다. dispatcher 의 retry_wait
# 경로를 타야 한다. 여기서 OutboxReconcileError 를 올리면 재시도를 통째로 없앤다.
@pytest.mark.parametrize("attempts", range(1, MAX_ATTEMPTS))
def test_retryable_failure_before_exhaustion_is_raised_as_itself(attempts: int) -> None:
    transport = FakeSlackTransport()
    transport.failure = _transport_failure(error_code="internal_error")
    with pytest.raises(SlackTransportError) as caught:
        _destination(transport).send(_event(attempts=attempts))
    assert caught.value is transport.failure
    assert not isinstance(caught.value, OutboxReconcileError)


# T002 AC-08 — D-020 항목 6. 마지막 attempt 는 어차피 dead letter 다 (events.py:2778
# `exhausted or unreconcilable`). 바뀌는 것은 error_code 뿐이고 재시도는 줄지 않는다.
# 이것이 없으면 연결 실패, timeout, ratelimited, internal_error 가 전부
# OUTBOX_DELIVERY_FAILED 한 줄이 된다 (events.py:2864).
def test_last_attempt_carries_the_slack_cause_into_the_dead_letter() -> None:
    transport = FakeSlackTransport()
    transport.failure = _transport_failure(error_code="ratelimited", status_code=429)
    with pytest.raises(SlackProjectionRetryExhaustedError) as caught:
        _destination(transport).send(_event(attempts=MAX_ATTEMPTS))
    assert isinstance(caught.value, OutboxReconcileError)
    assert caught.value.code == "SLACK_PROJECTION_RETRY_EXHAUSTED:ratelimited"
    assert caught.value.__cause__ is transport.failure


# T002 AC-08 — transport 층 실패는 Slack code 가 없다. 원인 예외 이름이 그 자리를 채운다.
# operator 가 network 문제와 Slack 문제를 가르는 유일한 근거다.
def test_last_attempt_names_the_transport_exception_when_slack_gave_no_code() -> None:
    transport = ExplodingTransport(ConnectionResetError("peer closed"))
    with pytest.raises(SlackProjectionRetryExhaustedError) as caught:
        _destination(transport).send(_event(attempts=MAX_ATTEMPTS))
    assert caught.value.code == "SLACK_PROJECTION_RETRY_EXHAUSTED:transport_connectionreseterror"


# T002 AC-08 — code 도 원인 예외도 없으면 no_slack_code 다. terminal 경로의 unknown 과 섞으면
# 두 사건이 같은 row 가 된다.
def test_last_attempt_without_any_cause_is_still_distinguishable() -> None:
    transport = FakeSlackTransport()
    transport.failure = _transport_failure(status_code=503)
    with pytest.raises(SlackProjectionRetryExhaustedError) as caught:
        _destination(transport).send(_event(attempts=MAX_ATTEMPTS))
    assert caught.value.code == "SLACK_PROJECTION_RETRY_EXHAUSTED:no_slack_code"
    assert "unknown" not in caught.value.code


# T002 AC-08 — 저장 대상이 append-only 라 원격 문자열을 그대로 남기지 않는다.
def test_exhausted_code_is_shaped_for_the_persisted_column() -> None:
    transport = FakeSlackTransport()
    # 이 code 는 allowlist 밖이라 terminal 이다. 소진 경로를 보려면 429 를 함께 준다.
    transport.failure = _transport_failure(
        error_code="missing_scope: chat:write",
        status_code=429,
    )
    with pytest.raises(SlackProjectionRetryExhaustedError) as caught:
        _destination(transport).send(_event(attempts=MAX_ATTEMPTS))
    suffix = caught.value.code.split(":", 1)[1]
    assert suffix.startswith("missing_scope__chat_write_")
    assert len(suffix) <= 64


# T002 AC-08 — max_attempts 를 넘긴 상태도 소진으로 본다. `claim_next` 가 counter 를 상한에서
# 멈추므로 (events.py:2672 의 CASE WHEN attempts < ?) 실제로는 도달하지 않는다. 방어적
# 검사다 — 경계를 == 로 쓰면 이 상태가 조용히 재시도로 새기 때문에 >= 를 고정한다.
def test_attempts_above_the_limit_are_also_treated_as_exhausted() -> None:
    transport = FakeSlackTransport()
    transport.failure = _transport_failure(error_code="internal_error")
    with pytest.raises(SlackProjectionRetryExhaustedError):
        _destination(transport).send(_event(attempts=MAX_ATTEMPTS + 3))


# T002 AC-08 — 경계가 dispatcher 의 것과 같아야 한다. fail() 은 attempts >= max_attempts 를
# 소진으로 본다 (events.py:2778). 한 칸 어긋나면 남은 attempt 를 버리거나 원인을 잃는다.
def test_exhaustion_boundary_matches_the_dispatcher_rule() -> None:
    transport = FakeSlackTransport()
    transport.failure = _transport_failure(error_code="internal_error")
    destination = _destination(transport, max_attempts=3)
    with pytest.raises(SlackTransportError) as retried:
        destination.send(_event(attempts=2))
    assert not isinstance(retried.value, OutboxReconcileError)
    with pytest.raises(SlackProjectionRetryExhaustedError):
        destination.send(_event(attempts=3))


def test_constructor_rejects_a_max_attempts_below_the_dispatcher_floor() -> None:
    # max_attempts=0 이면 첫 transient 실패가 곧바로 C-3.1 을 타 되돌릴 수 없는 hold 를
    # 만든다. dispatcher config 와의 일치는 검증 못 하지만 이 하한은 config 없이 검사된다
    # (OutboxConfig.max_attempts 는 Field(ge=1), events.py:2553).
    with pytest.raises(ValueError, match="max_attempts"):
        _destination(FakeSlackTransport(), max_attempts=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("destination_ref", " "),
        ("channel", " "),
        ("app_id", " "),
        ("max_history_pages", 0),
        ("max_attempts", -1),
    ],
)
def test_constructor_rejects_values_that_cannot_be_recovered_from(
    field: str,
    value: object,
) -> None:
    kwargs: dict[str, object] = {
        "destination_ref": DESTINATION_REF,
        "channel": CHANNEL,
        "app_id": APP_ID,
        "max_history_pages": 5,
        "max_attempts": MAX_ATTEMPTS,
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match=field):
        SlackProjectionDestination(FakeSlackTransport(), **kwargs)  # type: ignore[arg-type]


# T002 AC-10 — 검사만 하고 원본을 쓰면 env var 나 YAML scalar 에서 온 개행이 그대로 Slack
# 에 나가 channel_not_found 를 부른다. 그 code 는 terminal 이라 hold 가 되돌릴 수 없다.
# receipt 도 이 값을 쓰므로 정규화 안 하면 reconcile 과 갈린다 (C-2.3).
def test_constructor_strips_surrounding_whitespace() -> None:
    destination = SlackProjectionDestination(
        FakeSlackTransport(),
        destination_ref=f" {DESTINATION_REF}\n",
        channel=f"\t{CHANNEL} ",
        app_id=f" {APP_ID} ",
        max_history_pages=5,
        max_attempts=MAX_ATTEMPTS,
    )
    assert destination.destination_ref == DESTINATION_REF
    assert destination.channel == CHANNEL
    assert destination.app_id == APP_ID
    assert destination.send(_event()) == f"slack:{CHANNEL}:1700000000.000001"


# Package 4 가 HTTP client 를 넣으면 ConnectError 같은 흔한 이름이 여러 module 에서 나온다.
# module 을 안 붙이면 서로 다른 원인이 dead letter 에서 한 문자열로 뭉친다.
def test_non_builtin_exception_names_carry_their_module() -> None:
    transport = ExplodingTransport(GovernanceEventError("SOMETHING"))
    with pytest.raises(SlackProjectionRetryExhaustedError) as caught:
        _destination(transport).send(_event(attempts=MAX_ATTEMPTS))
    assert caught.value.code.endswith(
        ":transport_amplai_foundry_governance_events_governanceeventerror"
    )


# --------------------------------------------------------------------------------------
# 실제 dispatcher 를 통과시키는 검사. D-021 의 논거는 "마지막 attempt 는 어차피 dead
# letter 라 state 전이가 안 바뀐다" 인데, 그 주장은 exception 객체만 보는 test 로는 증명
# 되지 않는다. 틀렸을 때의 결과가 되돌릴 수 없는 hold 라 실물로 고정한다.
# --------------------------------------------------------------------------------------


def _dispatcher_fixture(
    tmp_path: Path,
) -> tuple[GovernanceStore, governance_fixtures.MutableClock, str]:
    store, _active, _draft = governance_fixtures._active_proposal(tmp_path)
    clock = governance_fixtures.MutableClock()
    events = GovernanceEventService(store, clock=clock)
    _audit, outbox = governance_fixtures._append(
        events,
        store,
        command_id="command-1",
        state_revision=2,
    )
    provider = next(event for event in outbox if event.supersession_key is not None)
    return store, clock, provider.destination_ref


def _drain(
    dispatcher: OutboxDispatcher,
    destination: ProjectionDestination,
    clock: governance_fixtures.MutableClock,
    *,
    rounds: int,
) -> list[int]:
    attempts: list[int] = []
    for _ in range(rounds):
        event = dispatcher.deliver_next("worker", destination)
        assert event is not None
        attempts.append(event.attempts)
        clock.advance(timedelta(seconds=120))
    return attempts


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


# T002 AC-08 — D-021 항목 2. 마지막 attempt 에서 OutboxReconcileError 를 올려도 state 전이는
# 그대로여야 한다. 재시도를 한 번이라도 잃으면 이 test 의 attempts 목록이 짧아진다.
def test_exhaustion_reaches_the_dead_letter_without_losing_a_retry(tmp_path: Path) -> None:
    store, clock, provider_ref = _dispatcher_fixture(tmp_path)
    config = OutboxConfig(lease_seconds=5)
    dispatcher = OutboxDispatcher(store, config=config, clock=clock)
    transport = FakeSlackTransport()
    transport.failure = _transport_failure(error_code="internal_error")
    destination = SlackProjectionDestination(
        transport,
        destination_ref=provider_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=config.max_attempts,
    )

    attempts = _drain(dispatcher, destination, clock, rounds=config.max_attempts)
    final = dispatcher.deliver_next("worker", destination)

    assert attempts == list(range(1, config.max_attempts + 1))
    assert final is None
    dead, holds = _terminal_rows(store)
    assert dead == ["SLACK_PROJECTION_RETRY_EXHAUSTED:internal_error"]
    assert holds == ["SLACK_PROJECTION_RETRY_EXHAUSTED:internal_error"]


# T002 AC-08 — 대조군. max_attempts 를 dispatcher 보다 크게 주면 C-3.1 이 안 돌고 원인이
# 사라진다. 두 test 의 차이가 error_code 하나뿐임을 보여 D-021 항목 2 를 고정한다.
def test_without_the_exhaustion_rule_the_cause_is_lost(tmp_path: Path) -> None:
    store, clock, provider_ref = _dispatcher_fixture(tmp_path)
    config = OutboxConfig(lease_seconds=5)
    dispatcher = OutboxDispatcher(store, config=config, clock=clock)
    transport = FakeSlackTransport()
    transport.failure = _transport_failure(error_code="internal_error")
    destination = SlackProjectionDestination(
        transport,
        destination_ref=provider_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=config.max_attempts + 1,
    )

    attempts = _drain(dispatcher, destination, clock, rounds=config.max_attempts)

    assert attempts == list(range(1, config.max_attempts + 1))
    dead, holds = _terminal_rows(store)
    assert dead == ["OUTBOX_DELIVERY_FAILED"]
    assert holds == ["OUTBOX_DELIVERY_FAILED"]


# T002 AC-02 — terminal 은 attempt 와 무관하게 첫 실패에서 DLQ 다. 실물로 확인한다.
def test_terminal_slack_error_dead_letters_on_the_first_attempt(tmp_path: Path) -> None:
    store, clock, provider_ref = _dispatcher_fixture(tmp_path)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = FakeSlackTransport()
    transport.failure = _transport_failure(error_code="invalid_auth")
    destination = SlackProjectionDestination(
        transport,
        destination_ref=provider_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=OutboxConfig().max_attempts,
    )

    failed = dispatcher.deliver_next("worker", destination)

    assert failed is not None and failed.state is OutboxState.DEAD_LETTER
    assert failed.attempts == 1
    dead, holds = _terminal_rows(store)
    assert dead == ["SLACK_PROJECTION_TERMINAL_ERROR:invalid_auth"]
    assert holds == ["SLACK_PROJECTION_TERMINAL_ERROR:invalid_auth"]


# T002 AC-09 — D-022 를 실물로 닫는다. 이전에는 이 event 가 5번 재시도된 뒤
# OUTBOX_DELIVERY_FAILED 로 남아 payload 손상과 평범한 전달 실패가 같은 row 가 됐다.
def test_payload_integrity_failure_dead_letters_immediately(tmp_path: Path) -> None:
    store, clock, provider_ref = _dispatcher_fixture(tmp_path)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = FakeSlackTransport()

    # payload 가 digest 와 어긋난 상태를 만든다. DB 의 payload column 은 불변이라
    # destination 이 보는 값을 바꿔 같은 조건을 만든다. destination_ref 불일치는
    # dispatcher 로는 재현이 안 된다 — claim 자체가 destination_ref 로 걸린다.
    class CorruptDigestDestination(SlackProjectionDestination):
        def send(self, event: OutboxEventView) -> str:
            return super().send(event.model_copy(update={"payload": {"text": "다른 내용"}}))

    destination = CorruptDigestDestination(
        transport,
        destination_ref=provider_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=OutboxConfig().max_attempts,
    )

    event = dispatcher.deliver_next("worker", destination)

    assert event is not None and event.state is OutboxState.DEAD_LETTER
    assert event.attempts == 1, "재시도가 확정적으로 무의미하므로 attempt 를 안 쓴다"
    assert transport.posted == []
    dead, holds = _terminal_rows(store)
    assert dead == ["OUTBOX_PAYLOAD_INTEGRITY_FAILURE"]
    assert holds == ["OUTBOX_PAYLOAD_INTEGRITY_FAILURE"]


# T002 AC-03 — 성공 경로도 실물로 닫는다. receipt 가 remote_receipt 에 그대로 저장된다.
def test_successful_send_records_the_slack_receipt(tmp_path: Path) -> None:
    store, clock, provider_ref = _dispatcher_fixture(tmp_path)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = FakeSlackTransport()
    destination = SlackProjectionDestination(
        transport,
        destination_ref=provider_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=OutboxConfig().max_attempts,
    )

    delivered = dispatcher.deliver_next("worker", destination)

    assert delivered is not None and delivered.state is OutboxState.DELIVERED
    assert delivered.remote_receipt == f"slack:{CHANNEL}:{transport.posted[0].ts}"
    # 실제 event payload 로도 digest 규칙이 통과한다. _event() 가 만든 payload 만
    # 통과하는 것이 아님을 여기서 확인한다 (AC-05). marker 는 send() 가 자기가 만든 것을
    # 그대로 넘겼는지만 본다 — 양변이 같은 함수를 쓰므로 marker **내용**은 여기서 안
    # 잡힌다. 내용을 고정하는 것은 test_marker_carries_the_four_identity_fields 하나뿐이니
    # 그것을 지우지 않는다.
    claimed = dispatcher.get(delivered.event_id)
    assert transport.posted[0].metadata == build_slack_marker(claimed)


# --------------------------------------------------------------------------------------
# MGC-012-T003 — SlackProjectionDestination.reconcile()
# --------------------------------------------------------------------------------------


def _marker_message(
    event: OutboxEventView,
    *,
    ts: str,
    app_id: str = APP_ID,
    digest: str | None = None,
) -> SlackHistoryMessage:
    marker = build_slack_marker(event)
    if digest is not None:
        body = dict(cast("Mapping[str, object]", marker["event_payload"]))
        body["payload_digest"] = digest
        marker = {"event_type": marker["event_type"], "event_payload": body}
    return SlackHistoryMessage(ts=ts, metadata=marker, app_id=app_id)


def _noise(ts: str) -> SlackHistoryMessage:
    """사람이 채널에서 떠든 message. marker 가 없다."""
    return SlackHistoryMessage(ts=ts, metadata=None, app_id="AHUMAN")


def _seed(transport: FakeSlackTransport, *messages: SlackHistoryMessage) -> None:
    """오래된 것부터 넣는다. read_history 가 뒤집어 최신부터 돌려준다."""
    transport.posted.extend(messages)


def _retried(event: OutboxEventView) -> OutboxEventView:
    """첫 시도 분기를 지나 실제로 조회하게 만든 event."""
    return event.model_copy(update={"attempts": 2})


# T003 AC-01
def test_reconcile_returns_the_receipt_when_the_marker_is_present() -> None:
    transport = FakeSlackTransport()
    event = _event(destination_sequence=2)
    _seed(transport, _noise("1700000000.000001"), _marker_message(event, ts="1700000000.000002"))

    assert (
        _destination(transport).reconcile(_retried(event)) == f"slack:{CHANNEL}:1700000000.000002"
    )


# T003 AC-02 — 하위 sequence marker 선발견이 "아직 안 보냈다" 의 증거다. claim_next 가
# 이전 sequence 미확정 시 다음 event 를 claim 하지 않으므로 (events.py:2648) 역순 조회에서
# N-1 을 N 보다 먼저 만나면 N 은 아직 없다.
def test_reconcile_returns_none_when_a_lower_sequence_marker_comes_first() -> None:
    # page 가 남아 있어야 이 규칙이 실제로 판정한다. history 를 소진시키면 "소진 → 미전송"
    # 규칙이 대신 None 을 만들어 이 test 가 규칙 2 를 지워도 통과한다.
    transport = FakeSlackTransport(page_size=1)
    previous = _event(destination_sequence=1)
    current = _event(destination_sequence=2)
    _seed(transport, _noise("1700000000.000001"), _marker_message(previous, ts="1700000000.000002"))

    assert _destination(transport, max_history_pages=1).reconcile(_retried(current)) is None
    assert len(transport.history_calls) == 1, "page 1 에서 판정해야 한다"


# T003 AC-02 — 다른 channel 의 marker 는 sequence counter 가 별개다. 그것을 하위 sequence
# 증거로 읽으면 아직 안 나간 Card 를 나갔다고 하거나 그 반대가 된다.
def test_foreign_destination_marker_is_not_evidence_of_anything() -> None:
    transport = FakeSlackTransport(page_size=1)
    foreign = _event(destination_ref="provider:slack:other", destination_sequence=1)
    ours = _event(destination_sequence=5)
    # 우리 marker 가 더 오래됐다. foreign 이 page 1, 우리 것이 page 2 다.
    _seed(transport, _marker_message(ours, ts="1700000000.000001"))
    _seed(transport, _marker_message(foreign, ts="1700000000.000002"))

    receipt = _destination(transport, max_history_pages=2).reconcile(_retried(ours))

    assert receipt == "slack:C0SLACK01:1700000000.000001"


# T003 AC-03 — 상한에 걸려 멈췄고 next_cursor 가 아직 남아 있다. 판정 불가라 fail-closed 다.
def test_reconcile_raises_when_the_page_cap_is_reached() -> None:
    transport = FakeSlackTransport(page_size=1)
    _seed(transport, *(_noise(f"1700000000.00000{n}") for n in range(1, 6)))

    with pytest.raises(SlackProjectionSearchCapError) as caught:
        _destination(transport, max_history_pages=2).reconcile(_retried(_event()))

    assert isinstance(caught.value, OutboxReconcileError)
    # 상한이 원인임이 code 로 드러나야 한다. 다른 hold 원인과 같은 문자열이면
    # max_history_pages 를 올려야 하는지 판단할 근거가 없다 (D-023 항목 4).
    assert caught.value.code == "SLACK_PROJECTION_RECONCILE_SEARCH_CAP_REACHED"
    assert len(transport.history_calls) == 2


# T003 AC-04 — 다른 app 이 심은 같은 모양의 metadata 를 우리 marker 로 읽으면 남의
# message 를 우리 Card 로 확정한다 (research S3).
def test_reconcile_ignores_markers_planted_by_another_app() -> None:
    transport = FakeSlackTransport()
    event = _event()
    _seed(transport, _marker_message(event, ts="1700000000.000001", app_id="AOTHER"))

    assert _destination(transport).reconcile(_retried(event)) is None


# T003 AC-05 (부분) — page 수가 상한을 넘지 않고 limit 은 999 로 나간다.
# `include_all_metadata` 는 이 계층에서 검사할 수 없다. C-1.2 가 transport **구현체**의
# 의무로 규정했고 Protocol signature 에 그 인자가 없다. Package 4 몫이다.
def test_reconcile_bounds_its_reads() -> None:
    transport = FakeSlackTransport(page_size=1)
    _seed(transport, *(_noise(f"1700000000.00000{n}") for n in range(1, 9)))

    with pytest.raises(SlackProjectionSearchCapError):
        _destination(transport, max_history_pages=3).reconcile(_retried(_event()))

    assert len(transport.history_calls) == 3
    assert {limit for _, limit in transport.history_calls} == {SLACK_HISTORY_PAGE_LIMIT}


# T003 AC-06 — C-2.3 의 나머지 절반. T002 에는 reconcile 이 없어 검사할 수 없었다.
# 두 경로가 다른 문자열을 만들면 mark_delivered 가 OUTBOX_DELIVERY_RESULT_CONFLICT 를
# 던진다 (events.py:2717).
def test_send_and_reconcile_agree_on_the_receipt() -> None:
    transport = FakeSlackTransport()
    destination = _destination(transport)
    event = _event()

    sent = destination.send(event)

    assert destination.reconcile(_retried(event)) == sent


# T003 AC-07 — read 경로도 C-1 의무 위반에 대한 방어선을 갖는다 (D-020 항목 4).
def test_reconcile_rewraps_an_unwrapped_read_exception() -> None:
    class ExplodingReader(FakeSlackTransport):
        def read_history(
            self,
            *,
            channel: str,
            cursor: str | None,
            limit: int,
        ) -> SlackHistoryPage:
            raise ConnectionResetError("peer closed")

    with pytest.raises(SlackTransportError) as caught:
        _destination(ExplodingReader()).reconcile(_retried(_event()))

    assert not isinstance(caught.value, OutboxReconcileError)
    assert caught.value.transport_exception == "ConnectionResetError"


# T003 AC-07 — 분류된 terminal read 실패는 즉시 DLQ 다.
def test_reconcile_classifies_a_terminal_read_failure() -> None:
    transport = FakeSlackTransport()
    transport.read_failure = _transport_failure(error_code="channel_not_found")

    with pytest.raises(SlackProjectionTerminalError) as caught:
        _destination(transport).reconcile(_retried(_event()))

    assert caught.value.code == "SLACK_PROJECTION_TERMINAL_ERROR:channel_not_found"


# T003 AC-08 — D-023 항목 4 가 확정한 값. 바꾸면 이 test 가 깨진다.
def test_search_bounds_are_pinned() -> None:
    assert SLACK_HISTORY_PAGE_LIMIT == 999
    assert SLACK_MAX_HISTORY_PAGES == 5
    # 999 는 conversations.history 의 문서화된 상한이다 (research S2). 넘으면 Slack 이 거부한다.
    assert SLACK_HISTORY_PAGE_LIMIT <= 999


# T003 AC-09 — 정상 상황이 상한 때문에 hold 로 떨어지지 않는다. OQ-001 의 한쪽 대가다.
def test_normal_gap_between_cards_stays_inside_the_bound() -> None:
    transport = FakeSlackTransport(page_size=10)
    previous = _event(destination_sequence=1)
    current = _event(destination_sequence=2)
    # 오래된 noise → 직전 Card → 그 뒤에 쌓인 무관한 message. 최신부터 읽으므로 marker 는
    # page 2 에 있고, 그 page 에는 next_cursor 가 남아 있다. 이렇게 두지 않으면 history
    # 소진이 대신 None 을 만들어 이 test 가 하위 sequence 규칙을 지워도 통과한다.
    _seed(transport, *(_noise(f"1700000000.{n:06d}") for n in range(1, 20)))
    _seed(transport, _marker_message(previous, ts="1700000000.000020"))
    _seed(transport, *(_noise(f"1700000000.{n:06d}") for n in range(21, 40)))

    assert _destination(transport).reconcile(_retried(current)) is None
    calls = len(transport.history_calls)
    assert calls == 2, "page 2 에서 판정해야 한다"
    assert calls < SLACK_MAX_HISTORY_PAGES, "정상 상황이 상한에 닿으면 안 된다"


# T003 AC-10 — 첫 시도면 조회 자체를 안 한다 (D-023 항목 2). 찾을 marker 가 정의상 없고
# conversations.history 는 Tier 2 다. 대부분의 Card 가 첫 시도에 성공하므로 평상시 조회가
# 0회가 된다.
def test_first_attempt_skips_the_read_entirely() -> None:
    transport = FakeSlackTransport()
    event = _event(attempts=1)
    assert event.last_error_code is None

    assert _destination(transport).reconcile(event) is None
    assert transport.history_calls == []


# T003 AC-11 — attempts 만 보면 안 되는 이유. max_attempts 가 1 인 구성에서는 증가가
# 상한에서 멈춰 (events.py:2672) 두 번째 claim 도 attempts == 1 로 보인다. 그 재claim 은
# last_error_code = OUTBOX_LEASE_EXPIRED 를 요구한다 (events.py:2644).
def test_lease_expired_replay_is_not_treated_as_a_first_attempt() -> None:
    transport = FakeSlackTransport()
    event = _event(attempts=1).model_copy(update={"last_error_code": "OUTBOX_LEASE_EXPIRED"})

    assert _destination(transport).reconcile(event) is None
    assert transport.history_calls != [], "조회를 건너뛰면 이미 나간 Card 를 다시 보낸다"


# T003 AC-12 — 이 test 가 없으면 모든 destination 의 첫 Card 가 일시 오류 한 번에 영구
# hold 가 된다. D-023 이 닫은 구멍의 회귀 방어다.
def test_first_card_survives_one_transient_failure() -> None:
    transport = FakeSlackTransport()
    first_card = _event(destination_sequence=1, attempts=2)

    assert _destination(transport).reconcile(first_card) is None


# T003 AC-13 — history 소진과 상한 도달의 차이는 next_cursor 하나다 (D-023 항목 3).
def test_exhausted_history_is_not_undecidable() -> None:
    transport = FakeSlackTransport(page_size=10)
    _seed(transport, *(_noise(f"1700000000.00000{n}") for n in range(1, 4)))

    assert _destination(transport).reconcile(_retried(_event())) is None
    assert transport.history_calls == [(None, SLACK_HISTORY_PAGE_LIMIT)]


# T003 — event_id 는 같은데 payload_digest 가 다른 marker 를 만나면 즉시 멈춘다. 지나가면
# 뒤에서 하위 sequence 를 만나거나 history 가 소진되어 미전송으로 판정하고, 이미 나간
# Card 옆에 한 장을 더 만든다 (wave 3 review). D-023 이 두 장은 없다고 약속했다.
def test_same_event_with_a_different_digest_fails_closed() -> None:
    transport = FakeSlackTransport(page_size=10)
    event = _event(destination_sequence=2)
    previous = _event(destination_sequence=1)
    _seed(transport, _marker_message(previous, ts="1700000000.000001"))
    _seed(
        transport,
        _marker_message(event, ts="1700000000.000002", digest=payload_digest({"other": True})),
    )

    with pytest.raises(SlackProjectionMarkerDigestMismatchError) as caught:
        _destination(transport).reconcile(_retried(event))

    assert isinstance(caught.value, OutboxReconcileError)
    # 원인이 상한 부족이나 지워진 Card 와 구분돼야 한다.
    assert caught.value.code == "SLACK_PROJECTION_MARKER_DIGEST_MISMATCH"


# T003 — 하위 sequence 규칙이 `<` 인 것을 고정한다. `<=` 로 바꾸면 같은 sequence 의
# 미판정 marker 가 미전송으로 읽혀 fail-closed 가 fail-open 이 된다.
def test_sequence_comparison_is_strict() -> None:
    transport = FakeSlackTransport(page_size=1)
    same_sequence_other_event = _event(destination_sequence=2).model_copy(
        update={"event_id": "EVT-0000000000009999"}
    )
    _seed(transport, _noise("1700000000.000001"))
    _seed(transport, _marker_message(same_sequence_other_event, ts="1700000000.000002"))

    with pytest.raises(SlackProjectionSearchCapError):
        _destination(transport, max_history_pages=1).reconcile(
            _retried(_event(destination_sequence=2))
        )


# T003 — marker 되읽기는 방어적이다. 한 message 의 모양이 이상하다고 예외를 던지면 그
# 뒤에 있는 진짜 marker 를 못 읽고 판정 불가로 떨어진다. 그 결과는 되돌릴 수 없는 hold 다.
@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"event_type": "someone_elses_type", "event_payload": {}},
        # event_type 하나만 어긋난 완전한 body. 이게 없으면 event_type 검사를 지워도
        # 아무 test 가 안 깨진다 — 남의 message 를 우리 Card 로 확정하는 변이다.
        {
            "event_type": "amplai_something_else",
            "event_payload": {
                "event_id": "EVT-0000000000000001",
                "destination_ref": DESTINATION_REF,
                "destination_sequence": 1,
                "payload_digest": payload_digest(PAYLOAD),
            },
        },
        {"event_type": SLACK_PROJECTION_EVENT_TYPE, "event_payload": "not-a-mapping"},
        {"event_type": SLACK_PROJECTION_EVENT_TYPE, "event_payload": {"event_id": 1}},
        {
            "event_type": SLACK_PROJECTION_EVENT_TYPE,
            "event_payload": {
                "event_id": "EVT-1",
                "destination_ref": DESTINATION_REF,
                "destination_sequence": True,
                "payload_digest": payload_digest({}),
            },
        },
    ],
)
def test_malformed_metadata_is_skipped_not_raised(metadata: dict[str, object] | None) -> None:
    message = SlackHistoryMessage(ts="1700000000.000001", metadata=metadata, app_id=APP_ID)
    assert read_slack_marker(message, app_id=APP_ID) is None


# T003 — 우리가 심은 marker 는 그대로 되읽힌다. send 와 reconcile 이 같은 형식을 쓴다는
# 것을 왕복으로 고정한다.
def test_marker_round_trips_through_history() -> None:
    event = _event(destination_sequence=7)
    recovered = read_slack_marker(_marker_message(event, ts="1.0"), app_id=APP_ID)

    assert recovered is not None
    assert recovered.event_id == event.event_id
    assert recovered.destination_ref == event.destination_ref
    assert recovered.destination_sequence == 7
    assert recovered.payload_digest == event.payload_digest


# T003 — page 안에서는 message 순서에 기대지 않는다. C-1.2 가 transport 에 최신-우선
# 순서를 요구하지만 signature 로 강제할 수 없다. 구현체가 오래된 것부터 돌려주면 하위
# sequence marker 를 우리 marker 보다 먼저 만나 "미전송" 으로 판정하고 이미 나간 Card 를
# 한 장 더 만든다. 그래서 우리 marker 가 page 안에서 이긴다.
@pytest.mark.parametrize("oldest_first", [False, True])
def test_our_marker_wins_inside_a_page_whatever_the_order(oldest_first: bool) -> None:
    previous = _event(destination_sequence=1)
    current = _event(destination_sequence=2)
    mine = _marker_message(current, ts="1700000000.000002")
    lower = _marker_message(previous, ts="1700000000.000001")

    transport = FakeSlackTransport(page_size=10)
    transport.posted.extend([mine, lower] if oldest_first else [lower, mine])

    assert (
        _destination(transport).reconcile(_retried(current)) == "slack:C0SLACK01:1700000000.000002"
    )


# T003 — 모양이 이상한 message 하나가 뒤에 있는 진짜 marker 를 가리면 안 된다. helper 만
# 부르는 test 는 "예외를 안 던진다" 만 증명하고 "계속 훑는다" 는 증명하지 못한다.
def test_reconcile_scans_past_a_malformed_marker_from_our_own_app() -> None:
    transport = FakeSlackTransport(page_size=1)
    event = _event(destination_sequence=3)
    _seed(transport, _marker_message(event, ts="1700000000.000001"))
    _seed(
        transport,
        SlackHistoryMessage(
            ts="1700000000.000002",
            metadata={"event_type": SLACK_PROJECTION_EVENT_TYPE, "event_payload": "broken"},
            app_id=APP_ID,
        ),
    )

    receipt = _destination(transport, max_history_pages=2).reconcile(_retried(event))

    assert receipt == "slack:C0SLACK01:1700000000.000001"


# T003 — 보이지 않는 문자는 strip 이 안 지운다. app_id 에 섞이면 우리 marker 를 하나도
# 못 알아보고 history 소진을 미전송으로 읽어 중복 Card 를 만든다 (wave 3 review).
@pytest.mark.parametrize("field", ["destination_ref", "channel", "app_id"])
@pytest.mark.parametrize("invisible", ["​", "﻿", "‎"])
def test_constructor_rejects_invisible_characters(field: str, invisible: str) -> None:
    kwargs: dict[str, object] = {
        "destination_ref": DESTINATION_REF,
        "channel": CHANNEL,
        "app_id": APP_ID,
        "max_attempts": MAX_ATTEMPTS,
    }
    kwargs[field] = f"{invisible}{kwargs[field]}"
    with pytest.raises(ValueError, match=field):
        SlackProjectionDestination(FakeSlackTransport(), **kwargs)  # type: ignore[arg-type]


# T003 — 그 값이 통과했을 때 실제로 벌어지는 일을 고정한다. 이 test 가 있어야 위 검사가
# 왜 필요한지가 회귀에서 드러난다.
def test_an_unrecognised_app_id_would_resend_the_card() -> None:
    transport = FakeSlackTransport()
    event = _event()
    _seed(transport, _marker_message(event, ts="1700000000.000001"))
    stranger = SlackProjectionDestination(
        transport,
        destination_ref=DESTINATION_REF,
        channel=CHANNEL,
        app_id="ADIFFERENT",
        max_attempts=MAX_ATTEMPTS,
    )

    # 우리 marker 가 채널에 있는데도 미전송으로 읽는다. 생성자 검사가 막는 것이 이것이다.
    assert stranger.reconcile(_retried(event)) is None


# T003 — max_history_pages 기본값이 D-023 확정값이다. 호출자가 안 주면 5 다.
def test_max_history_pages_defaults_to_the_decided_value() -> None:
    destination = SlackProjectionDestination(
        FakeSlackTransport(),
        destination_ref=DESTINATION_REF,
        channel=CHANNEL,
        app_id=APP_ID,
        max_attempts=MAX_ATTEMPTS,
    )
    assert destination.max_history_pages == SLACK_MAX_HISTORY_PAGES


# T003 — transport 가 include_all_metadata 를 빠뜨리면 event_type 만 오고 event_payload 가
# 안 온다 (research S3). 그냥 넘기면 모든 marker 가 안 읽혀 history 소진이 미전송으로
# 판정되고 **매 재시도마다 Card 가 한 장씩 는다.** hold 가 아니라 중복이라 조용하다.
def test_missing_event_payload_is_read_as_a_transport_defect() -> None:
    message = SlackHistoryMessage(
        ts="1700000000.000001",
        metadata={"event_type": SLACK_PROJECTION_EVENT_TYPE},
        app_id=APP_ID,
    )
    with pytest.raises(SlackProjectionMetadataUnreadableError) as caught:
        read_slack_marker(message, app_id=APP_ID)
    assert caught.value.code == "SLACK_PROJECTION_METADATA_UNREADABLE"


# T003 — 그 지문이 reconcile 을 통과해 dispatcher 까지 간다. 중복 Card 대신 hold 다.
def test_reconcile_fails_closed_when_metadata_comes_back_stripped() -> None:
    transport = FakeSlackTransport()
    _seed(
        transport,
        SlackHistoryMessage(
            ts="1700000000.000001",
            metadata={"event_type": SLACK_PROJECTION_EVENT_TYPE},
            app_id=APP_ID,
        ),
    )
    with pytest.raises(SlackProjectionMetadataUnreadableError) as caught:
        _destination(transport).reconcile(_retried(_event()))
    assert isinstance(caught.value, OutboxReconcileError)


# T003 — 오탐이 없어야 한다. 다른 app 이 보낸 같은 모양은 우리 문제가 아니다.
def test_stripped_metadata_from_another_app_is_not_our_defect() -> None:
    message = SlackHistoryMessage(
        ts="1700000000.000001",
        metadata={"event_type": SLACK_PROJECTION_EVENT_TYPE},
        app_id="AOTHER",
    )
    assert read_slack_marker(message, app_id=APP_ID) is None


# T003 — 이전 event 가 전부 superseded 라 우리 message 가 아예 없는 정상 흐름은 그대로
# 미전송이다. 위 지문 검사가 이 경우를 hold 로 만들면 안 된다.
def test_channel_with_no_message_from_us_is_still_not_sent() -> None:
    transport = FakeSlackTransport(page_size=10)
    _seed(transport, _noise("1700000000.000001"), _noise("1700000000.000002"))

    assert _destination(transport).reconcile(_retried(_event(destination_sequence=2))) is None


# T003 AC-14 — adapter 가 app_id 를 안 옮기면 marker 를 하나도 못 읽는다. Slack 은 app 이
# 보낸 message 에 app_id 를 서버에서 붙이므로 None 은 adapter 결함뿐이다. 그냥 넘기면
# hold 가 아니라 **중복 Card** 다 (wave 3 round 2 review).
def test_missing_app_id_on_our_own_message_is_a_transport_defect() -> None:
    event = _event()
    stripped = SlackHistoryMessage(
        ts="1700000000.000001",
        metadata=build_slack_marker(event),
        app_id=None,
    )
    with pytest.raises(SlackProjectionMetadataUnreadableError):
        read_slack_marker(stripped, app_id=APP_ID)


# T003 AC-14 — event_payload 가 None 으로 채워져 와도 같은 결함이다. adapter 가
# md.get("event_payload") 로 dataclass 를 만들면 없는 key 가 None 이 된다. key 부재만
# 보면 이 경로로 샌다.
def test_null_event_payload_is_read_as_a_transport_defect() -> None:
    message = SlackHistoryMessage(
        ts="1700000000.000001",
        metadata={"event_type": SLACK_PROJECTION_EVENT_TYPE, "event_payload": None},
        app_id=APP_ID,
    )
    with pytest.raises(SlackProjectionMetadataUnreadableError):
        read_slack_marker(message, app_id=APP_ID)


# T003 AC-15 — 지문은 우리 event_type 을 단 message 에만 발화한다. 사람이 쓴 글은
# app_id 가 없어도 우리 결함이 아니다.
def test_a_human_message_without_an_app_id_is_not_our_defect() -> None:
    human = SlackHistoryMessage(ts="1700000000.000001", metadata=None, app_id=None)
    assert read_slack_marker(human, app_id=APP_ID) is None
    other_app_type = SlackHistoryMessage(
        ts="1700000000.000002",
        metadata={"event_type": "someone_elses_type", "event_payload": {}},
        app_id=None,
    )
    assert read_slack_marker(other_app_type, app_id=APP_ID) is None
