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
from amplai_foundry.governance.models import ChannelProvider, ChannelRef, ProposalRef
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
from amplai_foundry.governance.store import GovernanceStore, governance_transaction

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


# T002 AC-03 / C-2.3 — receipt channel 은 생성자 값이지만 provider-confirmed channel과 먼저
# 같아야 한다. 다르면 구성된 history에서 marker를 찾을 수 없고, 특히 Review Card라면 raw
# action credential이 다른 channel에 보인다. receipt를 합성하지 않고 hold로 닫는다.
def test_response_channel_mismatch_fails_closed_before_receipt() -> None:
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
    with pytest.raises(OutboxReconcileError, match="SLACK_RESPONSE_CHANNEL_MISMATCH"):
        _destination(transport).send(_event())


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


def _hold_scopes(store: GovernanceStore) -> list[tuple[str, str]]:
    """Read which destination each operator hold actually landed on.

    `_terminal_rows` 는 `reason_code` 만 읽어 **어디에** 걸렸는지를 안 본다. hold 는
    되돌릴 수 없고 그 destination 의 이후 event 를 전부 멈추므로, 엉뚱한 destination 에
    걸리는 것은 원인 code 가 틀린 것보다 나쁘다. `scope_kind` 는 `CHECK` 제약이 구조로
    보장하지만 (`migrations.py:623`) `scope_ref` 는 아니다.
    """
    with store.connect() as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT scope_kind, scope_ref FROM governance_operator_holds"
            ).fetchall()
        ]


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


# T002 AC-08 — dispatcher/destination의 attempt budget이 갈리면 C-3.1 원인이 사라진다.
# Event를 claim하기 전에 배선을 거부해 그 상태 자체를 만들지 않는다.
def test_dispatcher_rejects_a_mismatched_attempt_budget_before_claim(tmp_path: Path) -> None:
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

    with pytest.raises(ValueError, match="max_attempts"):
        dispatcher.deliver_next("worker", destination)

    with store.connect() as connection:
        assert connection.execute(
            "SELECT state, attempts FROM governance_outbox_events "
            "WHERE destination_ref = ? AND supersession_key IS NOT NULL",
            (provider_ref,),
        ).fetchone() == ("pending", 0)
    assert _terminal_rows(store) == ([], [])


def test_constructor_rejects_a_transport_page_budget_mismatch() -> None:
    transport = FakeSlackTransport()
    transport.max_history_pages = 1
    transport.lease_seconds = 30

    with pytest.raises(ValueError, match="max_history_pages"):
        _destination(transport, max_history_pages=SLACK_MAX_HISTORY_PAGES)


def test_dispatcher_rejects_a_transport_lease_mismatch_before_claim(tmp_path: Path) -> None:
    store, clock, provider_ref = _dispatcher_fixture(tmp_path)
    transport = FakeSlackTransport()
    transport.max_history_pages = SLACK_MAX_HISTORY_PAGES
    transport.lease_seconds = 30
    destination = SlackProjectionDestination(
        transport,
        destination_ref=provider_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=OutboxConfig().max_attempts,
    )
    dispatcher = OutboxDispatcher(
        store,
        config=OutboxConfig(lease_seconds=5),
        clock=clock,
    )

    with pytest.raises(ValueError, match="lease_seconds"):
        dispatcher.deliver_next("worker", destination)

    with store.connect() as connection:
        assert connection.execute(
            "SELECT state, attempts FROM governance_outbox_events "
            "WHERE destination_ref = ? AND supersession_key IS NOT NULL",
            (provider_ref,),
        ).fetchone() == ("pending", 0)


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


# --------------------------------------------------------------------------------------
# MGC-012-T004 — 실제 OutboxDispatcher.deliver_next 와 물린 순서 보장
#
# 여기까지의 test 는 destination 하나를 직접 부른다. 순서·직렬화·supersession 은 destination
# 혼자서는 증명되지 않는다 — 그 계약은 dispatcher 의 claim 조건에 있고, destination 이
# 그것을 우회하면 (별도 전송 경로, 자체 retry) 사람이 낡은 Card 를 보고 결정한다.
# dispatcher 코드는 고치지 않는다. 통합이 깨지면 destination 쪽이 계약을 어긴 것이다.
# --------------------------------------------------------------------------------------


def _ordered_fixture(
    tmp_path: Path,
    *,
    count: int,
) -> tuple[GovernanceStore, governance_fixtures.MutableClock, tuple[OutboxEventView, ...]]:
    """같은 provider destination 에 pending event 를 `count` 개 쌓는다.

    `_dispatcher_fixture` 는 event 가 하나라 순서를 검사할 수 없다. decision 을 여러 번
    append 하면 같은 destination 에 `destination_sequence` 가 1 부터 이어 붙는다 —
    destination_ref 는 decision authority 의 channel 에서 나오므로 append 마다 같다.
    """
    store, _active, _draft = governance_fixtures._active_proposal(tmp_path)
    clock = governance_fixtures.MutableClock()
    events = GovernanceEventService(store, clock=clock)
    providers: list[OutboxEventView] = []
    for index in range(1, count + 1):
        _audit, outbox = governance_fixtures._append(
            events,
            store,
            command_id=f"command-{index}",
            state_revision=index + 1,
        )
        providers.append(next(event for event in outbox if event.supersession_key is not None))
    assert [event.destination_sequence for event in providers] == list(range(1, count + 1))
    assert len({event.destination_ref for event in providers}) == 1
    return store, clock, tuple(providers)


def _bound_destination(
    transport: FakeSlackTransport,
    destination_ref: str,
    *,
    max_attempts: int = MAX_ATTEMPTS,
) -> SlackProjectionDestination:
    return SlackProjectionDestination(
        transport,
        destination_ref=destination_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=max_attempts,
    )


class RecordingTransport(FakeSlackTransport):
    """`post_message` 호출을 **실패해도** 기록한다.

    `posted` 는 성공한 전송만 남는다. 순서 계약이 막는 것은 "보냈다" 가 아니라 "보내려
    했다" 다 — 실패한 시도도 Slack 에 도달했을 수 있고, 낮은 sequence 가 확정되기 전에
    높은 sequence 를 시도한 것 자체가 위반이다.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attempted: list[int] = []

    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        body = cast("Mapping[str, object]", marker["event_payload"])
        self.attempted.append(cast("int", body["destination_sequence"]))
        return super().post_message(channel=channel, payload=payload, marker=marker)


def _delivered_sequence(store: GovernanceStore, destination_ref: str) -> int:
    with store.connect() as connection:
        row = connection.execute(
            "SELECT delivered_sequence FROM governance_outbox_destinations "
            "WHERE destination_ref = ?",
            (destination_ref,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _states(dispatcher: OutboxDispatcher, events: Sequence[OutboxEventView]) -> list[OutboxState]:
    return [dispatcher.get(event.event_id).state for event in events]


# T004 AC-01 / SC-001 — sequence 1 이 확정되기 전에는 sequence 2 가 transport 에 닿지
# 않는다. 첫 event 를 재시도 상태로 붙잡아 두고 dispatcher 를 계속 돌린다.
def test_next_sequence_is_not_sent_until_the_previous_one_is_delivered(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=2)
    config = OutboxConfig(lease_seconds=5)
    dispatcher = OutboxDispatcher(store, config=config, clock=clock)
    transport = RecordingTransport()
    transport.failure = _transport_failure(error_code="internal_error")
    destination = _bound_destination(
        transport,
        providers[0].destination_ref,
        max_attempts=config.max_attempts,
    )

    dispatcher.deliver_next("worker", destination)

    # **시계를 밀기 전에 한 번 더 부른다.** 이 창이 앞 sequence gate 가 실제로 지키는
    # 구간이다 — sequence 1 은 retry_wait 인데 retry_at 이 아직 안 지났다. 여기서 dispatcher
    # 는 아무것도 안 준다. 시계를 밀고 나면 sequence 1 이 다시 claim 가능해져
    # `ORDER BY ... destination_sequence` 만으로도 1 이 먼저 나오므로, gate 를 지워도
    # 관측 순서가 안 바뀐다 (wave 4 regression lens F2).
    assert dispatcher.deliver_next("worker", destination) is None
    assert transport.attempted == [1]

    clock.advance(timedelta(seconds=120))
    dispatcher.deliver_next("worker", destination)
    clock.advance(timedelta(seconds=120))

    # 두 번 실패하는 동안 dispatcher 는 sequence 1 만 재시도한다. sequence 2 는 손도 안
    # 댄다 — claim 조건이 앞 sequence 의 확정을 요구하기 때문이다 (events.py:2648).
    assert transport.attempted == [1, 1]
    assert _states(dispatcher, providers) == [OutboxState.RETRY_WAIT, OutboxState.PENDING]

    transport.failure = None
    dispatcher.deliver_next("worker", destination)
    clock.advance(timedelta(seconds=120))
    dispatcher.deliver_next("worker", destination)

    assert transport.attempted == [1, 1, 1, 2]
    assert _states(dispatcher, providers) == [OutboxState.DELIVERED, OutboxState.DELIVERED]
    assert dispatcher.deliver_next("worker", destination) is None


# T004 AC-01 대조 — 한 번에 다 돌려도 순서가 뒤집히지 않는다. 위 test 는 실패를 끼워
# 넣어 만든 상태라, 아무 방해 없는 평상시 경로도 같은 순서인지 따로 고정한다.
def test_three_cards_leave_in_sequence_order(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=3)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = RecordingTransport()
    destination = _bound_destination(transport, providers[0].destination_ref)

    for _ in range(3):
        delivered = dispatcher.deliver_next("worker", destination)
        assert delivered is not None and delivered.state is OutboxState.DELIVERED

    assert transport.attempted == [1, 2, 3]
    # 각 Card 의 receipt 가 자기 순번의 전송에서 나왔는지 본다. `posted` 의 ts 가 오름차순
    # 인지 묻는 것은 무의미하다 — FakeSlackTransport 가 counter 로 만들어 순서가 뒤집혀도
    # 항상 참이다 (wave 4 contract lens A1).
    receipts = [dispatcher.get(event.event_id).remote_receipt for event in providers]
    assert receipts == [f"slack:{CHANNEL}:{message.ts}" for message in transport.posted]


# T004 AC-02 / SC-002 — 같은 destination 을 두 dispatcher 가 동시에 잡으면 한쪽만
# 전송한다. `deliver_next` 는 동기라 순차로 부르면 경쟁이 재현되지 않는다. 경쟁 dispatcher
# 를 **lease 를 쥔 send 안에서** 돌려 실제로 겹치는 창을 만든다.
def test_a_competing_dispatcher_gets_no_lease_while_the_first_is_sending(
    tmp_path: Path,
) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=2)
    config = OutboxConfig(lease_seconds=5)
    first = OutboxDispatcher(store, config=config, clock=clock)
    second = OutboxDispatcher(store, config=config, clock=clock)
    rival_transport = RecordingTransport()
    rival_destination = _bound_destination(rival_transport, providers[0].destination_ref)
    rival_results: list[OutboxEventView | None] = []

    class CompetingDestination(SlackProjectionDestination):
        """send 중에 경쟁 dispatcher 를 돌린다. lease 가 살아 있는 유일한 구간이다."""

        def send(self, event: OutboxEventView) -> str:
            rival_results.append(second.deliver_next("worker-b", rival_destination))
            return super().send(event)

    transport = RecordingTransport()
    destination = CompetingDestination(
        transport,
        destination_ref=providers[0].destination_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=config.max_attempts,
    )

    delivered = first.deliver_next("worker-a", destination)

    assert delivered is not None and delivered.state is OutboxState.DELIVERED
    # 경쟁 dispatcher 는 lease 를 얻지 못했다. sequence 2 도 못 가져간다 — 앞 event 가
    # 아직 leased 라 확정 전이기 때문이다.
    assert rival_results == [None]
    assert rival_transport.attempted == []
    assert transport.attempted == [1]
    assert _states(first, providers) == [OutboxState.DELIVERED, OutboxState.PENDING]

    # 대조군. 같은 dispatcher·destination 이 lease 밖에서는 sequence 2 를 가져간다.
    # 이것이 없으면 위의 None 이 lease 때문인지 fixture 가 틀린 것인지 못 가른다.
    after = second.deliver_next("worker-b", rival_destination)
    assert after is not None and after.destination_sequence == 2
    assert rival_transport.attempted == [2]


# T004 AC-03 / SC-003 — supersede 된 event 는 Slack 으로 나가지 않는다. 낡은 Card 를
# 사람에게 보이지 않는 것이 이 계약의 목적이다.
def test_a_superseded_event_never_reaches_the_transport(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=2)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = RecordingTransport()
    destination = _bound_destination(transport, providers[0].destination_ref)

    superseded = dispatcher.supersede_pending(
        providers[0].event_id,
        replacement_event_id=providers[1].event_id,
    )
    assert superseded.state is OutboxState.SUPERSEDED

    delivered = dispatcher.deliver_next("worker", destination)

    assert delivered is not None and delivered.event_id == providers[1].event_id
    assert transport.attempted == [2]
    assert _states(dispatcher, providers) == [OutboxState.SUPERSEDED, OutboxState.DELIVERED]
    assert dispatcher.deliver_next("worker", destination) is None


# T004 AC-03 경계 — supersede 는 pending event 만 대상이다 (events.py:2797). 이미 전달된
# Card 를 되돌리는 요구는 A11 에 없다.
#
# **이 test 가 지는 것은 dispatcher 의 guard 하나다** (events.py:2830). destination 을 어떤
# ProjectionDestination 으로 바꿔도 통과한다 — destination 은 이 경로에 관여하지 않는다.
# 경계를 못박는 용도이지 destination 계약의 증거가 아니다 (wave 4 contract lens A2).
def test_supersede_does_not_reach_a_card_already_delivered(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=2)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = RecordingTransport()
    destination = _bound_destination(transport, providers[0].destination_ref)

    assert dispatcher.deliver_next("worker", destination) is not None
    with pytest.raises(GovernanceEventError, match="OUTBOX_SUPERSEDE_INVALID"):
        dispatcher.supersede_pending(
            providers[0].event_id,
            replacement_event_id=providers[1].event_id,
        )

    assert dispatcher.get(providers[0].event_id).state is OutboxState.DELIVERED
    assert transport.attempted == [1]


# T004 AC-04 — destination row 의 delivered_sequence 가 마지막으로 전달한
# destination_sequence 와 같다. 이 값이 어긋나면 재기동 시 어디까지 나갔는지 모른다.
def test_delivered_sequence_tracks_the_last_delivered_card(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=3)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = RecordingTransport()
    destination_ref = providers[0].destination_ref
    destination = _bound_destination(transport, destination_ref)

    assert _delivered_sequence(store, destination_ref) == 0
    for expected in (1, 2, 3):
        delivered = dispatcher.deliver_next("worker", destination)
        assert delivered is not None and delivered.destination_sequence == expected
        assert _delivered_sequence(store, destination_ref) == expected

    assert _delivered_sequence(store, destination_ref) == providers[-1].destination_sequence


# T004 AC-04 — supersede 한 event 는 delivered_sequence 를 올리지 않는다. 건너뛴 sequence
# 가 값에 남으면 "1 까지 나갔다" 와 "1 은 취소됐다" 가 구분되지 않는다.
def test_delivered_sequence_skips_the_superseded_event(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=2)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = RecordingTransport()
    destination_ref = providers[0].destination_ref
    destination = _bound_destination(transport, destination_ref)

    dispatcher.supersede_pending(
        providers[0].event_id,
        replacement_event_id=providers[1].event_id,
    )
    assert dispatcher.deliver_next("worker", destination) is not None

    assert _delivered_sequence(store, destination_ref) == 2


# T004 FR-007 — retry, DLQ 와 operator hold 는 dispatcher 계약을 그대로 쓴다. destination
# 이 자기 재시도 loop 를 갖고 있으면 이 test 의 attempt 수가 어긋난다.
def test_the_destination_adds_no_retry_path_of_its_own(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=2)
    config = OutboxConfig(lease_seconds=5)
    dispatcher = OutboxDispatcher(store, config=config, clock=clock)
    transport = RecordingTransport()
    transport.failure = _transport_failure(error_code="internal_error")
    destination = _bound_destination(
        transport,
        providers[0].destination_ref,
        max_attempts=config.max_attempts,
    )

    for _ in range(config.max_attempts):
        dispatcher.deliver_next("worker", destination)
        clock.advance(timedelta(seconds=120))

    # deliver_next 한 번에 post_message 도 정확히 한 번이다.
    assert transport.attempted == [1] * config.max_attempts
    assert dispatcher.get(providers[0].event_id).state is OutboxState.DEAD_LETTER
    dead, holds = _terminal_rows(store)
    assert dead == ["SLACK_PROJECTION_RETRY_EXHAUSTED:internal_error"]
    assert holds == ["SLACK_PROJECTION_RETRY_EXHAUSTED:internal_error"]
    # operator hold 가 걸린 destination 은 다음 Card 를 내보내지 않는다 (events.py:2634).
    assert dispatcher.deliver_next("worker", destination) is None
    assert dispatcher.get(providers[1].event_id).state is OutboxState.PENDING


# --------------------------------------------------------------------------------------
# MGC-012-T005 — crash recovery, 판정 불가 hold, 실패 행렬
#
# T004 는 정상 순서를 고정한다. 여기서는 순서가 깨질 수 있는 지점을 본다 — send 는
# 성공했는데 mark 가 없는 구간, 판정 불가, terminal 실패, 그리고 hold 가 destination 을
# 멈추는 것. dispatcher 코드는 여기서도 안 고친다.
# --------------------------------------------------------------------------------------

# Telegram decision 하나로 다른 Provider destination 을 만든다. destination_ref 는 production
# 파생 규칙이 만든다 (`events.py:_decision_destinations`) — row 를 손으로 넣으면 실제로
# 생기지 않는 모양을 검사하게 된다.
TELEGRAM_CHANNEL = ChannelRef(
    provider=ChannelProvider.TELEGRAM,
    chat_id="-1001234567890",
    message_id="4242",
)

# crash 재현용 config. lease 와 backoff 를 짧게 잡아 clock 조작을 한눈에 보이게 한다.
_CRASH_CONFIG = OutboxConfig(lease_seconds=5, retry_base_seconds=1, retry_cap_seconds=1)


def _crashed_after_send(
    tmp_path: Path,
    *,
    count: int = 1,
    max_history_pages: int = SLACK_MAX_HISTORY_PAGES,
    config: OutboxConfig = _CRASH_CONFIG,
) -> tuple[
    GovernanceStore,
    governance_fixtures.MutableClock,
    OutboxDispatcher,
    RecordingTransport,
    SlackProjectionDestination,
    tuple[OutboxEventView, ...],
]:
    """post_message 는 성공했는데 mark_delivered 가 없는 상태를 만든다.

    예외를 던져 만들지 않는다 — 그건 실패 경로이고 `fail()` 이 상태를 정리한다. crash 는
    아무도 정리하지 않는 것이라서 위험하다. `claim_next` 는 자기 transaction 을 commit
    하므로 lease 를 쥔 채 죽은 row 가 durable 하게 남는다.

    `config` 를 여는 이유는 `max_attempts` 다. 재개 시점의 `attempts` 가 상한에 닿았는지가
    "재전송" 과 "영구 hold" 를 가른다 (`events.py:2844`-`2854`). 기본값으로는 그 경계에
    닿지 못한다.
    """
    store, clock, providers = _ordered_fixture(tmp_path, count=count)
    dispatcher = OutboxDispatcher(store, config=config, clock=clock)
    transport = RecordingTransport()
    destination = SlackProjectionDestination(
        transport,
        destination_ref=providers[0].destination_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=max_history_pages,
        max_attempts=config.max_attempts,
    )
    claimed = dispatcher.claim_next("crashed", destination_ref=destination.destination_ref)
    assert claimed is not None and claimed.destination_sequence == 1
    assert destination.send(claimed) == f"slack:{CHANNEL}:{transport.posted[0].ts}"
    assert transport.attempted == [1]
    return store, clock, dispatcher, transport, destination, providers


def _resume_after_the_lease_expires(
    dispatcher: OutboxDispatcher,
    destination: SlackProjectionDestination,
    clock: governance_fixtures.MutableClock,
) -> OutboxEventView | None:
    """Restart the dead worker's event.

    만료 처리와 재claim 은 한 호출에 같이 일어나지 않는다. 첫 호출은 lease 를 거둬
    `retry_wait` 로 내리면서 `retry_at` 을 미래로 잡고 (`events.py:2594`), 그 시각이 지나야
    두 번째 호출이 claim 한다.

    시간은 `dispatcher.config` 에서 읽는다. module 상수를 읽으면 호출자가 config 를 바꿨을
    때 조용히 어긋난다. `_backoff_seconds` 는 `min(base * 2^(n-1), cap)` 이라
    (events.py:2980) backoff 는 언제나 cap 이하다. **cap + 1 을 민다** — cap 만큼만 밀면
    `base == cap` 구성에서 `retry_at == now` 로 정확히 착지하고, 통과하는 이유가 query 의
    `<=` 하나가 된다 (wave 4 round 2 failure-recovery lens A2).
    """
    clock.advance(timedelta(seconds=dispatcher.config.lease_seconds + 1))
    assert dispatcher.deliver_next("recovery", destination) is None
    clock.advance(timedelta(seconds=dispatcher.config.retry_cap_seconds + 1))
    return dispatcher.deliver_next("recovery", destination)


# T005 AC-01 / SC-004 — send 뒤 mark 전에 죽어도 Card 는 한 장이다. reconcile 이 marker 를
# 찾아 확정한다. 이 test 가 깨지면 재기동마다 Card 가 한 장씩 는다.
def test_a_card_sent_before_the_crash_is_not_sent_twice(tmp_path: Path) -> None:
    _store, clock, dispatcher, transport, destination, providers = _crashed_after_send(tmp_path)
    sent_ts = transport.posted[0].ts

    recovered = _resume_after_the_lease_expires(dispatcher, destination, clock)

    assert recovered is not None and recovered.state is OutboxState.DELIVERED
    # 두 번째 post 가 없다. 시도조차 없어야 한다 — 실패한 시도도 Slack 에 닿을 수 있다.
    assert transport.attempted == [1]
    assert len(transport.posted) == 1
    # receipt 는 send 가 만든 것과 같은 문자열이어야 한다. 다르면 mark_delivered 가
    # OUTBOX_DELIVERY_RESULT_CONFLICT 를 던진다 (C-2.3).
    assert recovered.remote_receipt == f"slack:{CHANNEL}:{sent_ts}"
    assert dispatcher.get(providers[0].event_id).state is OutboxState.DELIVERED


# T005 AC-02 (D-023 으로 좁혀진 판) — 지워진 Card 가 hold 가 되는 것은 **조회 범위를 다 못
# 본 경우뿐**이다. `next_cursor` 가 남은 채 page 상한에 걸리면 "없다" 를 확정할 수 없다.
#
# manifest 의 AC-02 원문은 지워진 Card 를 무조건 hold 로 적었다. 그것은 D-023 이전 판이다
# (contracts C-2.2 표의 셋째 줄, spec SC-005). 아래 두 test 가 좁혀진 계약을 양쪽으로
# 고정한다.
def test_a_deleted_card_holds_when_the_search_range_was_not_exhausted(tmp_path: Path) -> None:
    store, clock, dispatcher, transport, destination, providers = _crashed_after_send(
        tmp_path,
        count=2,
        max_history_pages=1,
    )

    # 사람이 Card 를 지웠고 그 뒤로 채널에 다른 message 가 쌓였다. page 상한에 걸릴 때까지
    # 우리 marker 도 하위 sequence marker 도 안 나온다.
    transport.posted.clear()
    transport.page_size = 1
    _seed(transport, _noise("1700000000.000101"), _noise("1700000000.000102"))

    held = _resume_after_the_lease_expires(dispatcher, destination, clock)

    assert held is not None and held.state is OutboxState.DEAD_LETTER
    # attempts 를 소진하지 않았는데도 DLQ 다. 판정 불가는 재시도로 나아지지 않는다.
    assert held.attempts < _CRASH_CONFIG.max_attempts
    assert transport.attempted == [1]
    dead, holds = _terminal_rows(store)
    # 상한 부족은 다른 hold 원인과 구분되는 code 를 쓴다 (D-023 항목 4). 이 code 가 없으면
    # max_history_pages 가 작았다는 것을 사후에 알 방법이 없다.
    assert dead == ["SLACK_PROJECTION_RECONCILE_SEARCH_CAP_REACHED"]
    assert holds == ["SLACK_PROJECTION_RECONCILE_SEARCH_CAP_REACHED"]
    # AC-02 는 "outbox_destination scope 의" hold 를 요구한다. code 만 보면 어느
    # destination 이 멈췄는지 모른다.
    assert _hold_scopes(store) == [("outbox_destination", providers[0].destination_ref)]
    assert dispatcher.get(providers[1].event_id).state is OutboxState.PENDING


# T005 AC-02 반대쪽 / SC-005 — 채널이 작아 history 를 끝까지 훑을 수 있으면 없는 것이
# **확정**이라 재전송한다. Card 가 두 장이 될 수 없다. D-023 항목 3 이 이 갈래를 열었고,
# 열지 않으면 모든 destination 의 첫 Card 가 영구 hold 가 된다.
def test_a_deleted_card_is_resent_when_the_channel_history_is_exhausted(tmp_path: Path) -> None:
    store, clock, dispatcher, transport, destination, providers = _crashed_after_send(tmp_path)

    # 우리 Card 만 지운다. 채널을 통째로 비우면 "message 가 없다" 와 "우리 것만 없다" 가
    # 구분되지 않는다. AC-08 이 말하는 것은 후자다 — 사람들이 떠들고 있는 작은 채널.
    transport.posted.clear()
    _seed(transport, _noise("1700000000.000101"), _noise("1700000000.000102"))

    resent = _resume_after_the_lease_expires(dispatcher, destination, clock)

    assert resent is not None and resent.state is OutboxState.DELIVERED
    # 중복 부재를 지는 것은 이 한 줄이다. 두 번째 시도가 있고 그것이 전부다.
    assert transport.attempted == [1, 1]
    dead, holds = _terminal_rows(store)
    assert (dead, holds) == ([], [])
    assert dispatcher.get(providers[0].event_id).state is OutboxState.DELIVERED


# T005 AC-09 — 같은 "지워진 Card" 인데 여기서는 재전송하지 않는다. `deliver_next` 가
# `send()` 앞에서 가로채 `OUTBOX_POST_SEND_RECONCILE_REQUIRED` 로 떨어뜨린다
# (events.py:2844-2854). D-023 이 "마지막 안전망" 이라고 부른 branch 다.
#
# 이 갈래가 없으면 attempt 를 다 쓴 event 가 "없으니 보낸다" 를 반복한다. guard 가
# 지워져도 나머지 test 는 전부 초록이라 여기서 실물로 고정한다. wave 4 review 의 P1-1 이다.
#
# **경계는 두 조건의 AND 다.** 다른 한쪽은 아래 AC-10 test 가 진다.
def test_the_last_attempt_holds_instead_of_resending_a_deleted_card(tmp_path: Path) -> None:
    # max_attempts=1 이라 재claim 시점의 attempts 가 곧바로 상한이다. 이 구성은 D-023
    # 항목 2 가 `_never_attempted` 를 두 조건으로 만든 이유이기도 하다 — attempts 만 보면
    # 이 재claim 이 첫 시도로 보인다.
    config = OutboxConfig(
        lease_seconds=5,
        max_attempts=1,
        retry_base_seconds=1,
        retry_cap_seconds=1,
    )
    store, clock, dispatcher, transport, destination, providers = _crashed_after_send(
        tmp_path,
        config=config,
    )

    transport.posted.clear()
    _seed(transport, _noise("1700000000.000101"))

    held = _resume_after_the_lease_expires(dispatcher, destination, clock)

    assert held is not None and held.state is OutboxState.DEAD_LETTER
    # 재전송하지 않았다. 시도조차 없어야 한다.
    assert transport.attempted == [1]
    dead, holds = _terminal_rows(store)
    assert dead == ["OUTBOX_POST_SEND_RECONCILE_REQUIRED"]
    assert holds == ["OUTBOX_POST_SEND_RECONCILE_REQUIRED"]
    assert _hold_scopes(store) == [("outbox_destination", providers[0].destination_ref)]


# T005 AC-10 — 경계의 반대쪽. attempts 가 상한에 닿아도 **lease 만료 replay 가 아니면**
# guard 가 안 걸리고 재전송한다. guard 는 두 조건의 AND 다 (events.py:2844-2847).
#
# round 1 은 이 경계를 attempts 단독으로 적었고 그것은 거짓이다 (wave 4 round 2 contract
# lens NEW-1). 여기 없으면 다음 사람이 AC-09 를 읽고 attempts 하나로 이해한다.
def test_a_max_attempt_failure_that_is_not_a_lease_replay_still_resends(tmp_path: Path) -> None:
    config = OutboxConfig(
        lease_seconds=5,
        max_attempts=2,
        retry_base_seconds=1,
        retry_cap_seconds=1,
    )
    store, clock, providers = _ordered_fixture(tmp_path, count=1)
    dispatcher = OutboxDispatcher(store, config=config, clock=clock)
    transport = RecordingTransport()
    destination = _bound_destination(
        transport,
        providers[0].destination_ref,
        max_attempts=config.max_attempts,
    )

    # send 는 성공했는데 응답을 못 읽어 retryable 실패로 기록된 경로다. crash 와 달리
    # lease 를 반납하므로 last_error_code 가 OUTBOX_LEASE_EXPIRED 가 아니다.
    claimed = dispatcher.claim_next("worker", destination_ref=destination.destination_ref)
    assert claimed is not None
    destination.send(claimed)
    dispatcher.fail(
        claimed.event_id,
        dispatcher_id="worker",
        generation=claimed.claim_generation,
        error_code="OUTBOX_DELIVERY_FAILED",
    )
    # 사람이 Card 를 지운다.
    transport.posted.clear()
    clock.advance(timedelta(seconds=120))

    before = dispatcher.get(providers[0].event_id)
    assert before.last_error_code == "OUTBOX_DELIVERY_FAILED"

    resent = dispatcher.deliver_next("worker", destination)

    # attempts 가 상한에 닿았는데도 guard 가 안 걸린다. 조건 하나가 빠졌기 때문이다.
    assert resent is not None and resent.attempts == config.max_attempts
    assert resent.state is OutboxState.DELIVERED
    assert transport.attempted == [1, 1]
    assert _terminal_rows(store) == ([], [])


# T005 AC-03 — hold 가 걸린 destination 은 다음 event 를 claim 하지 않는다. deliver_next
# 가 아니라 claim_next 로 본다 — 멈추는 지점이 destination 이 아니라 claim 조건이라는 것이
# 계약이다.
#
# **이 test 는 두 gate 중 어느 쪽이 막았는지 가리지 않는다.** dead_letter 된 event 는
# `operator_hold = 0` (events.py:2634) 과 앞 sequence gate (events.py:2648) 양쪽에 걸린다.
# production 에서 hold 는 언제나 dead letter 와 함께 생기므로 (events.py:_dead_letter) 두
# 조건이 늘 같이 성립한다. hold gate 단독의 teeth 는 아래 synthetic test 가 준다
# (wave 4 regression lens F1).
def test_a_held_destination_stops_claiming_its_next_event(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=2)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = RecordingTransport()
    transport.failure = _transport_failure(error_code="channel_not_found")
    destination = _bound_destination(transport, providers[0].destination_ref)

    held = dispatcher.deliver_next("worker", destination)
    assert held is not None and held.state is OutboxState.DEAD_LETTER

    with store.connect() as connection:
        assert connection.execute(
            "SELECT operator_hold FROM governance_outbox_destinations WHERE destination_ref = ?",
            (providers[0].destination_ref,),
        ).fetchone() == (1,)
    assert dispatcher.claim_next("worker", destination_ref=providers[0].destination_ref) is None
    assert dispatcher.get(providers[1].event_id).state is OutboxState.PENDING
    # hold 를 자동으로 푸는 경로는 없다 (T005 non_goals). 시간이 지나도 그대로다.
    clock.advance(timedelta(seconds=3600))
    assert dispatcher.claim_next("worker", destination_ref=providers[0].destination_ref) is None


# T005 AC-03 — hold gate 단독의 teeth.
#
# **이 상태는 synthetic 이다.** production 에서 hold 는 `_dead_letter` 가 dead letter 와
# 함께 만들므로 (events.py:2903-2960) 앞 event 가 전부 delivered 인 채 hold 만 서 있는
# destination 은 지금 경로로는 안 생긴다. 그래서 SQL 로 세운다.
#
# 더 정확히는 repo 가 이 상태를 **능동적으로 거부한다** — `reconcile_connection` 이
# dead_letter/recovery_hold event 없는 `operator_hold = 1` 을 `OUTBOX_OPERATOR_HOLD_MISMATCH`
# 로 튕긴다 (events.py:2251-2261). 그 검사는 이 test 안에서 안 돌므로 충돌하지 않는다.
#
# 그래도 두는 이유는 `WHERE d.operator_hold = 0` (events.py:2634) 이 지금 **아무 test 도
# 죽이지 않는 gate** 라서다 — 그 조건을 `IN (0, 1)` 로 바꿔도 repo 전체 test 가 통과한다
# (wave 4 regression lens F1). 지금은 앞 sequence gate 에 가려 잉여지만, hold 해소나
# dead_letter event 의 supersession 이 생기면 그 순간 이 gate 가 유일한 방어선이 된다.
# 잉여인 채로 조용히 사라지는 것을 막는다.
def test_the_hold_gate_alone_stops_a_destination_whose_prior_events_are_delivered(
    tmp_path: Path,
) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=2)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    destination = _bound_destination(RecordingTransport(), providers[0].destination_ref)

    delivered = dispatcher.deliver_next("worker", destination)
    assert delivered is not None and delivered.state is OutboxState.DELIVERED
    # 앞 sequence gate 는 이제 통과한다. 남은 것은 hold gate 하나다.
    assert dispatcher.claim_next("probe", destination_ref=destination.destination_ref) is not None

    with store.connect() as connection, governance_transaction(connection):
        connection.execute(
            "UPDATE governance_outbox_events SET state = 'pending', lease_owner = NULL, "
            "lease_expires_at = NULL, attempts = 0, claim_generation = 0 WHERE event_id = ?",
            (providers[1].event_id,),
        )
        connection.execute(
            "UPDATE governance_outbox_destinations SET operator_hold = 1 WHERE destination_ref = ?",
            (destination.destination_ref,),
        )

    assert dispatcher.claim_next("worker", destination_ref=destination.destination_ref) is None
    assert dispatcher.deliver_next("worker", destination) is None
    assert dispatcher.get(providers[1].event_id).state is OutboxState.PENDING


# T005 AC-04 — ratelimited 는 allowlist 안이라 재시도다. 사람을 부르지 않는다.
def test_ratelimited_goes_to_retry_wait_and_raises_attempts(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=1)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = RecordingTransport()
    transport.failure = _transport_failure(error_code="ratelimited")
    destination = _bound_destination(transport, providers[0].destination_ref)

    failed = dispatcher.deliver_next("worker", destination)

    assert failed is not None and failed.state is OutboxState.RETRY_WAIT
    assert failed.attempts == 1
    assert failed.last_error_code == "OUTBOX_DELIVERY_FAILED"
    assert _terminal_rows(store) == ([], [])


# T005 AC-05 — channel_not_found 는 allowlist 밖이라 terminal 이다. attempts 소진을
# 기다리지 않는다. 잘못 지목한 channel 을 5번 더 두드려도 답은 안 바뀐다.
def test_channel_not_found_dead_letters_without_waiting_for_attempts(tmp_path: Path) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=1)
    config = OutboxConfig(lease_seconds=5)
    dispatcher = OutboxDispatcher(store, config=config, clock=clock)
    transport = RecordingTransport()
    transport.failure = _transport_failure(error_code="channel_not_found")
    destination = _bound_destination(transport, providers[0].destination_ref)

    failed = dispatcher.deliver_next("worker", destination)

    assert failed is not None and failed.state is OutboxState.DEAD_LETTER
    assert failed.attempts == 1 < config.max_attempts
    assert transport.attempted == [1]
    dead, holds = _terminal_rows(store)
    assert dead == ["SLACK_PROJECTION_TERMINAL_ERROR:channel_not_found"]
    assert holds == ["SLACK_PROJECTION_TERMINAL_ERROR:channel_not_found"]


# T005 AC-06 — Slack code 없는 HTTP 실패는 전부 transport 층이라 재시도다 (plan P-002,
# D-020 항목 1). 5xx 를 terminal 로 두면 일시 장애가 되돌릴 수 없는 hold 를 만든다.
@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_a_server_error_without_a_slack_code_goes_to_retry_wait(
    tmp_path: Path,
    status_code: int,
) -> None:
    store, clock, providers = _ordered_fixture(tmp_path, count=1)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = RecordingTransport()
    transport.failure = _transport_failure(status_code=status_code)
    destination = _bound_destination(transport, providers[0].destination_ref)

    failed = dispatcher.deliver_next("worker", destination)

    assert failed is not None and failed.state is OutboxState.RETRY_WAIT
    assert _terminal_rows(store) == ([], [])


def _destination_row(store: GovernanceStore, destination_ref: str) -> tuple[object, ...]:
    with store.connect() as connection:
        row = connection.execute(
            "SELECT next_sequence, delivered_sequence, operator_hold, updated_at "
            "FROM governance_outbox_destinations WHERE destination_ref = ?",
            (destination_ref,),
        ).fetchone()
    assert row is not None
    return tuple(row)


# T005 AC-07 / FR-011 / A14 — Slack 경로를 성공과 hold 양쪽으로 끝까지 돌려도 다른
# Provider 의 destination 은 그대로다.
#
# **읽는 대상을 좁혔다.** manifest 는 "다른 Provider 의 activation state" 라고 쓰지만 이
# repo 에 provider activation 을 담는 table 은 없다 (governance_* table 목록 확인). Package 3
# 범위에서 실재하고 관측 가능한 것은 provider 별 outbox destination row 와 그 event 다.
# 그것으로 좁혀 검사한다. activation rollout 자체는 Package 4 이고 CURRENT_ITEM 의
# Out Of Scope 다.
def test_the_slack_path_leaves_another_provider_destination_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _active, _draft = governance_fixtures._active_proposal(tmp_path)
    clock = governance_fixtures.MutableClock()
    events = GovernanceEventService(store, clock=clock)
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
    slack = next(event for event in slack_batches[0] if event.supersession_key is not None)
    assert telegram.destination_ref.startswith("provider:telegram:")
    assert slack.destination_ref.startswith("provider:slack:")
    before = _destination_row(store, telegram.destination_ref)

    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    transport = RecordingTransport()
    destination = _bound_destination(transport, slack.destination_ref)
    delivered = dispatcher.deliver_next("worker", destination)
    assert delivered is not None and delivered.state is OutboxState.DELIVERED

    # 성공만 보면 부족하다. hold 는 destination 전체를 멈추므로 번지면 피해가 크다.
    transport.failure = _transport_failure(error_code="invalid_auth")
    failed = dispatcher.deliver_next("worker", destination)
    assert failed is not None and failed.state is OutboxState.DEAD_LETTER

    assert _destination_row(store, telegram.destination_ref) == before
    assert dispatcher.get(telegram.event_id).state is OutboxState.PENDING
    dead, holds = _terminal_rows(store)
    assert dead == ["SLACK_PROJECTION_TERMINAL_ERROR:invalid_auth"]
    assert holds == ["SLACK_PROJECTION_TERMINAL_ERROR:invalid_auth"]
    # hold 가 Slack destination 에만 걸렸다. provider 가 둘인 유일한 fixture 라 여기서
    # scope 를 안 보면 hold 가 번지는 것을 code 문자열로만 판정하게 된다.
    assert _hold_scopes(store) == [("outbox_destination", slack.destination_ref)]
    # Telegram destination 은 여전히 자기 event 를 claim 할 수 있다.
    claimed = dispatcher.claim_next("telegram-worker", destination_ref=telegram.destination_ref)
    assert claimed is not None and claimed.event_id == telegram.event_id


# ---------------------------------------------------------------------------
# MGC-012-P5-T009 — 공유 interruption 분류기 (round 11 R-1, R-6)
# ---------------------------------------------------------------------------


class _RaisingTransport(FakeSlackTransport):
    """`post_message` 에서 임의의 BaseException 을 던지는 transport."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        del channel, payload, marker
        raise self._error


def _raising_destination(
    provider_ref: str, error: BaseException
) -> tuple[SlackProjectionDestination, _RaisingTransport]:
    transport = _RaisingTransport(error)
    destination = SlackProjectionDestination(
        transport,
        destination_ref=provider_ref,
        channel=CHANNEL,
        app_id=APP_ID,
        max_history_pages=SLACK_MAX_HISTORY_PAGES,
        max_attempts=OutboxConfig().max_attempts,
    )
    return destination, transport


def _event_states(store: GovernanceStore, destination_ref: str) -> list[tuple[str, int]]:
    with store.connect() as connection:
        return [
            (str(row[0]), int(row[1]))
            for row in connection.execute(
                "SELECT state, attempts FROM governance_outbox_events WHERE destination_ref = ?",
                (destination_ref,),
            ).fetchall()
        ]


# T009 AC-01 — non-review event 의 평범한 Exception 이 dispatcher 밖으로 새지 않는다.
#
# round 11 `R-1` 이 잡은 결함이 정확히 이 경로다. transport 가 평범한 Exception 을 bare
# `BaseException` 으로 바꿔 올리면 `slack_projection` 의 `if not is_review: raise` 가 그대로
# 통과시키고, `deliver_next` 의 `except OutboxReconcileError` 와 `except Exception` 이 둘 다
# 놓쳐 event 가 `leased` 로 남는다. 그래서 이 test 는 예외가 안 나오는 것만 보지 않고
# **event 가 leased 로 방치되지 않았다는 것**까지 본다.
def test_a_plain_exception_from_a_non_review_send_stays_inside_the_dispatcher(
    tmp_path: Path,
) -> None:
    store, clock, provider_ref = _dispatcher_fixture(tmp_path)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    destination, _ = _raising_destination(provider_ref, RuntimeError("transport 가 죽었다"))

    event = dispatcher.deliver_next("worker", destination)

    assert event is not None
    assert event.state is not OutboxState.LEASED, "leased 로 남으면 아무도 이 event 를 못 잡는다"
    assert [state for state, _ in _event_states(store, provider_ref)] != ["leased"]


# T009 AC-03 — 세 process interruption 은 여전히 같은 종류로 전파된다.
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (KeyboardInterrupt("stop"), KeyboardInterrupt),
        (SystemExit("stop"), SystemExit),
        (GeneratorExit(), GeneratorExit),
    ],
)
def test_process_interruptions_still_propagate_from_a_send(
    tmp_path: Path,
    raised: BaseException,
    expected: type[BaseException],
) -> None:
    store, clock, provider_ref = _dispatcher_fixture(tmp_path)
    dispatcher = OutboxDispatcher(store, config=OutboxConfig(lease_seconds=5), clock=clock)
    destination, _ = _raising_destination(provider_ref, raised)

    with pytest.raises(expected):
        dispatcher.deliver_next("worker", destination)


# T009 AC-04 — 분류기 정의가 저장소에 하나다.
#
# `slack_http` 가 자기 사본을 갖고 있던 것이 `R-1` 의 원인이다. 이름만 같은 두 함수를
# 비교하면 다시 갈라져도 안 잡히므로 **같은 함수 객체인지** 본다.
def test_the_interruption_classifier_has_exactly_one_definition() -> None:
    from amplai_foundry.governance import slack_http

    assert slack_http.classify_interruption is slack_projection.classify_interruption
    assert slack_http.raise_sanitized_interruption is slack_projection.raise_sanitized_interruption
    assert not hasattr(slack_http, "_interruption_kind")
    assert not hasattr(slack_http, "_raise_sanitized_interruption")


# T009 AC-03 보조 — 분류기 자체의 네 분기를 직접 고정한다. `generator_exit` 분기는
# round 11 `R-6` 이 두 파일 모두 test 0건이라고 지목한 자리다.
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (KeyboardInterrupt(), "keyboard_interrupt"),
        (SystemExit(), "system_exit"),
        (GeneratorExit(), "generator_exit"),
        (RuntimeError(), "exception"),
        (ValueError(), "exception"),
    ],
)
def test_classify_interruption_names_each_kind(error: BaseException, kind: str) -> None:
    assert slack_projection.classify_interruption(error) == kind


# T009 AC-02 — `"exception"` 은 `except Exception` 이 잡을 수 있는 형으로 올라간다.
# 이것이 `R-1` 을 되돌리는 mutation 을 죽이는 검사다.
def test_a_classified_exception_is_re_raised_as_something_except_exception_catches() -> None:
    with pytest.raises(BaseException) as caught:
        slack_projection.raise_sanitized_interruption("exception", "메시지")

    assert isinstance(caught.value, Exception), (
        "bare BaseException 으로 올리면 deliver_next 의 두 handler 를 모두 통과해 조용히 샌다"
    )
    assert str(caught.value) == "메시지"
    assert caught.value.__cause__ is None and caught.value.__context__ is None


# T009 AC-03 보조 — 재던지기가 종류와 메시지를 보존한다. `GeneratorExit` 은 메시지를 안 받는다.
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("keyboard_interrupt", KeyboardInterrupt),
        ("system_exit", SystemExit),
        ("generator_exit", GeneratorExit),
    ],
)
def test_raise_sanitized_interruption_preserves_the_kind(
    kind: str, expected: type[BaseException]
) -> None:
    with pytest.raises(expected) as caught:
        slack_projection.raise_sanitized_interruption(kind, "메시지")

    if expected is not GeneratorExit:
        assert str(caught.value) == "메시지"
