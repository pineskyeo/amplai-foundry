"""Real Slack Web API transport for `SlackTransport`, over the standard library.

`slack_projection.py` 는 Slack 을 호출하지 않는다 — 그 module 은 계약과 destination 만
갖는다. 실제 호출은 여기다. 두 파일을 나눈 이유는 방향이 아니라 **위험**이다. network
코드를 그쪽에 넣으면 gate PASS 한 188개 test 가 network 를 다루는 파일에 붙는다
(plan.md P-004).

runtime dependency 를 늘리지 않는다. `urllib.request` 로 충분하다 — 호출량이 작고
(`chat.postMessage` 는 channel 당 초당 1건), 필요한 것은 POST·header·deadline 뿐이며,
재시도는 이미 `OutboxDispatcher` 가 소유한다 (research R-013).
"""

from __future__ import annotations

import json
import math
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message as HTTPMessage
from enum import StrEnum
from typing import IO, Final, NoReturn, Protocol, cast

from pydantic import SecretStr, TypeAdapter, ValidationError

from amplai_foundry.governance.events import OutboxEventView
from amplai_foundry.governance.ingress import IngressCommandView
from amplai_foundry.governance.ingress_worker import SafeInteractionOutcome
from amplai_foundry.governance.models import ChannelProvider, Digest
from amplai_foundry.governance.slack_projection import (
    SLACK_PROJECTION_EVENT_TYPE,
    SlackHistoryMessage,
    SlackHistoryPage,
    SlackProjectionMetadataUnreadableError,
    SlackSendResult,
    SlackTransport,
    SlackTransportError,
    build_slack_marker,
    read_slack_marker,
    safe_slack_error_code,
    safe_transport_exception,
)

SLACK_API_BASE: Final = "https://slack.com/api"

_POST_MESSAGE: Final = "chat.postMessage"
_POST_EPHEMERAL: Final = "chat.postEphemeral"
_CONVERSATIONS_HISTORY: Final = "conversations.history"
# 자가검사가 남긴 probe 를 치우는 데만 쓴다 (H-3.2). 필요한 scope 는 `chat:write` 하나이고
# 이미 갖고 있다 — Slack 공식 문서가 bot token 에 대해 그렇게 적고, 같은 문서가
# "this method may delete only messages posted by that bot" 으로 대상을 우리 message 로
# 한정한다. 새 scope 를 요구하지 않으므로 설치 절차가 안 바뀐다.
_DELETE_MESSAGE: Final = "chat.delete"
_DIGEST_ADAPTER: Final = TypeAdapter(Digest)

# `chat.postMessage` 는 JSON 으로 보낸다. Slack 문서가 복잡한 인자를 가진 method 에 대해
# "these methods can be difficult to properly construct when using a
# application/x-www-form-urlencoded Content-type, so we strongly recommend using
# JSON-encoded bodies instead" 로 권장한다 (research P6). `metadata` 와 `blocks` 가 그
# 경우다.
#
# `conversations.history` 는 form-encoded 로 보낸다. Slack 문서는 JSON 을 "Most **write**
# methods allow arguments with application/json" 으로 한정하는데 이것은 read method 다.
# 확인되지 않은 지원에 기대지 않는다 — form-encoded 는 모든 method 가 받는다.
_JSON_CONTENT_TYPE: Final = "application/json; charset=utf-8"
_FORM_CONTENT_TYPE: Final = "application/x-www-form-urlencoded"

# 응답을 조각으로 읽으면서 deadline 을 본다. 조각 크기는 성능이 아니라 **deadline 확인
# 주기**를 정한다.
_READ_CHUNK_BYTES: Final = 64 * 1024

# 응답 크기 상한. `read()` 에 상한이 없으면 악의적이거나 고장난 상대가 memory 를 먹는다.
# Slack 의 정상 응답은 page 당 999 message 라 여유가 크다.
_MAX_RESPONSE_BYTES: Final = 16 * 1024 * 1024

# 한 호출의 최악 wall clock 은 `timeout` 이 아니라 **`2 x timeout`** 이다.
#
# `urlopen(timeout=)` 은 호출 전체가 아니라 **socket 연산 하나마다** 걸리는 값이다.
# 상대가 byte 를 찔끔씩 보내면 매 `recv` 가 timer 를 되돌려 한 호출이 무한히 늘어난다
# (wave 5 failure-recovery review P1-1 이 실측: timeout 1초에 20 byte body 가 10초).
#
# 아래 `_read_within` 이 조각마다 deadline 을 봐서 그 무한을 끊는다. 다만 deadline 을
# 넘긴 것을 **알아채려면 그 조각 read 가 먼저 돌아와야** 하고, 그 read 는 socket timeout
# 만큼 막힐 수 있다. 그래서 상한이 `timeout`(deadline 까지) + `timeout`(마지막 read) 다.
_WORST_CASE_MULTIPLIER: Final = 2


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. **The bot token must not follow one.**

    stdlib 의 기본 `HTTPRedirectHandler` 는 redirect 때 `content-length` 와
    `content-type` 만 떼고 나머지 header 를 그대로 옮긴다 (`urllib/request.py`
    `CONTENT_HEADERS`). host 대조도 없다. 즉 **302 를 준 쪽이 지정한 아무 host 로
    `Authorization: Bearer` 가 그대로 간다** (wave 5 failure-recovery review P1-2 가
    실측했다).

    부수 효과 둘이 더 있다. 301/302/303 은 POST 를 GET 으로 바꿔 body 를 버리고,
    기본 상한이 10 hop 이라 hop 마다 timeout 창이 새로 열려 예산을 무너뜨린다.

    `redirect_request` 가 `None` 을 돌려주면 urllib 이 redirect 를 따라가지 않고 3xx 를
    `HTTPError` 로 올린다. 그것은 Slack 봉투가 아니므로 code 없는 실패가 되어 retryable
    로 흐른다 — Slack API 가 redirect 를 쓸 이유가 없으니 그 응답 자체가 중간 장비의
    개입이고, transient 로 보는 것이 맞다.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


# 값 하나에 변수 하나다 (contracts H-4.1). 합쳐 담으면 부분 구성이 조용히 통과한다.
# `AMPLAI_` 접두는 같은 환경에 있는 다른 Slack 도구와 섞이지 않게 한다.
SLACK_BOT_TOKEN_ENV: Final = "AMPLAI_SLACK_BOT_TOKEN"
SLACK_SIGNING_SECRET_ENV: Final = "AMPLAI_SLACK_SIGNING_SECRET"


def _read_secret(source: Mapping[str, str], name: str) -> SecretStr:
    """Read one value and wrap it **before it becomes a named local**.

    평문을 이름 있는 local 에 담지 않는다. `pytest --showlocals` 나 예외 보고 도구가 frame
    local 을 찍으면 그 값이 그대로 나간다 (wave 6 failure-recovery review P1-5 가 실측).
    여기서는 값이 임시식으로만 존재하고 곧바로 `SecretStr` 이 된다.
    """
    return SecretStr((source.get(name) or "").strip())


def _missing_names(source: Mapping[str, str], names: Sequence[str]) -> list[str]:
    """Return the **names** that are absent or blank. Never a value.

    부재와 빈 문자열을 같게 다룬다 (contracts H-4.1). 값은 어떤 local 에도 남기지 않는다.
    """
    return [name for name in names if not (source.get(name) or "").strip()]


@dataclass(frozen=True, slots=True)
class SlackCredentials:
    """The two secrets one process needs to talk to Slack. **Never durable.**

    어떤 table 에도 저장되지 않고 프로세스와 함께 사라진다. `SecretStr` + `repr=False` 로
    repr 유출을 막는다 — `slack.py` 의 `signing_secret` 이 이미 받는 처리와 같다.

    `signing_secret` 을 여기 함께 두는 이유는 **한 곳에서 읽기** 위해서다. 두 값이 서로
    다른 경로로 들어오면 부분 구성이 조용히 통과한다.
    """

    bot_token: SecretStr = field(repr=False)
    signing_secret: SecretStr = field(repr=False)

    def __post_init__(self) -> None:
        if not self.bot_token.get_secret_value().strip():
            raise ValueError("bot_token은 비어 있을 수 없습니다.")
        if not self.signing_secret.get_secret_value().strip():
            raise ValueError("signing_secret은 비어 있을 수 없습니다.")


def load_slack_credentials(
    environ: Mapping[str, str] | None = None,
) -> SlackCredentials | None:
    """Read the credentials at the one place that touches the environment (R-014).

    **이것이 유일한 읽기 지점이다.** core 는 계속 주입만 받는다. repo 전체에 Slack 설정을
    읽는 `os.environ` 이 없었고 그 구조가 test 가능성을 만들었으므로, 늘리지 않고 하나만
    연다. `environ` 인자는 test 를 위한 것이지 두 번째 읽기 지점이 아니다.

    반환은 셋이다.

    - 둘 다 없거나 비어 있으면 `None` — "구성 안 됨" 이다. E2E 가 그때 skip 한다
      (plan P-003). 부재와 빈 문자열을 같게 다룬다 (contracts H-4.1).
    - 둘 다 있으면 `SlackCredentials`.
    - **하나만 있으면 `ValueError`.** 부분 구성은 실수이지 미구성이 아니다. `None` 으로
      뭉뚱그리면 token 만 넣고 E2E 를 돌린 사람이 "skip" 만 보고 자기가 뭘 빠뜨렸는지
      모른다. 조용한 skip 은 조용한 pass 만큼 나쁘다.

    **collection 시점에 부르지 않는다.** `skipif` 표현식에서 부르면 부분 구성 환경의
    기본 suite 가 collection error 로 빨개진다 — P-003 이 피하려던 상태다. fixture 안에서
    부른다 (wave 5 contract review A-9).
    """
    source = os.environ if environ is None else environ
    names = (SLACK_BOT_TOKEN_ENV, SLACK_SIGNING_SECRET_ENV)
    missing = _missing_names(source, names)
    if len(missing) == len(names):
        return None
    if missing:
        # 값을 message 에 넣지 않는다. 이름만 적는다.
        raise ValueError(f"Slack credential 구성이 불완전합니다. 빠진 변수: {missing}")
    return SlackCredentials(
        bot_token=_read_secret(source, SLACK_BOT_TOKEN_ENV),
        signing_secret=_read_secret(source, SLACK_SIGNING_SECRET_ENV),
    )


# credential 이 **누구에게** 말하는지를 정하는 둘이다. secret 이 아니지만 같은 규칙을 받는다
# (contracts H-4.1) — 값 하나에 변수 하나, 부재와 빈 문자열은 같은 "구성 안 됨", 부분 구성은
# 오류다.
#
# `app_id` 를 환경에서 받는 이유는 `reconcile` 이 이 값으로 남의 message 를 배제하기
# 때문이다 (`slack_projection.py:495`). `auth.test` 가 주는 `bot_id` 는 **다른 값**이라
# 대체할 수 없다. 값이 틀린 경우는 형식 검사로 못 잡고 `verify_marker_readback` 이 기동
# 시점에 잡는다 (H-3) — 그래서 여기서 접두 문자를 검사하지 않는다. 검사하면 확인하지 않은
# 형식 가정을 계약으로 굳히게 된다.
SLACK_APP_ID_ENV: Final = "AMPLAI_SLACK_APP_ID"
SLACK_CHANNEL_ID_ENV: Final = "AMPLAI_SLACK_CHANNEL_ID"

# 한 구성에 필요한 전부다. **넷을 한 목록으로 두는 이유**는 빠진 이름을 한 번에 모으기
# 위해서다. credential 둘을 따로 판정하면 token 만 넣은 사람이 secret 이름만 받고, 그것을
# 고친 뒤에야 대상 둘이 빠진 것을 안다. 왕복이 두 번이 되고 H-4.1 이 약속한
# "빠진 이름을 전부" 가 거짓이 된다 (wave 6 review 에서 두 lens 가 독립으로 잡았다).
SLACK_SETTINGS_ENV_NAMES: Final = (
    SLACK_BOT_TOKEN_ENV,
    SLACK_SIGNING_SECRET_ENV,
    SLACK_APP_ID_ENV,
    SLACK_CHANNEL_ID_ENV,
)


@dataclass(frozen=True, slots=True)
class SlackSettings:
    """Everything one process needs to talk to one Slack channel.

    `SlackCredentials` 는 secret 만 담는다. 여기는 그 secret 이 가리키는 **대상**까지 담는
    한 단계 위다. 둘을 나눠 두면 secret 처리 규칙(`repr=False`)이 secret 에만 붙는다.
    """

    credentials: SlackCredentials
    app_id: str
    channel_id: str

    def __post_init__(self) -> None:
        if not self.app_id.strip():
            raise ValueError("app_id는 비어 있을 수 없습니다.")
        if not self.channel_id.strip():
            raise ValueError("channel_id는 비어 있을 수 없습니다.")


def load_slack_settings(environ: Mapping[str, str]) -> SlackSettings | None:
    """Assemble the four variables, or say which ones are missing (contracts H-4.1).

    **`environ` 을 요구한다.** 기본값을 주지 않는 이유는 `os.environ` 을 읽는 지점을 하나로
    유지하기 위해서다 — 이 module 에서 환경을 만지는 함수는 `load_slack_credentials` 뿐이고
    그 성질을 test 가 AST 로 지킨다. 부르는 쪽(composition root, E2E fixture)이 `os.environ`
    을 넘긴다. R-014 의 "entrypoint 한 곳에서만 읽는다" 와 같은 말이다.

    반환은 `load_slack_credentials` 와 같은 tri-state 다.

    - 넷 다 없거나 비어 있으면 `None` — E2E 가 그때 skip 한다 (plan P-003).
    - 넷 다 있으면 `SlackSettings`.
    - **하나라도 빠지면 `ValueError`.** 빠진 변수 **이름을 전부** 적는다. token 만 넣고
      E2E 를 돌린 사람이 "skip" 만 보면 자기가 뭘 빠뜨렸는지 모른다 (H-4.1).

    **판정을 `load_slack_credentials` 에 위임하지 않는다.** 위임하면 credential 이 부분일
    때 그 함수가 먼저 터져서 대상 둘의 누락이 message 에 안 실린다 — 고치고 다시 돌려야
    나머지를 본다. 넷을 한 번에 센다.
    """
    missing = _missing_names(environ, SLACK_SETTINGS_ENV_NAMES)
    if len(missing) == len(SLACK_SETTINGS_ENV_NAMES):
        return None
    if missing:
        raise ValueError(f"Slack 구성이 불완전합니다. 빠진 변수: {missing}")
    return SlackSettings(
        credentials=SlackCredentials(
            bot_token=_read_secret(environ, SLACK_BOT_TOKEN_ENV),
            signing_secret=_read_secret(environ, SLACK_SIGNING_SECRET_ENV),
        ),
        app_id=(environ.get(SLACK_APP_ID_ENV) or "").strip(),
        channel_id=(environ.get(SLACK_CHANNEL_ID_ENV) or "").strip(),
    )


def worst_case_call_seconds(timeout_seconds: float, max_history_pages: int) -> float:
    """Bound one `deliver_next` in wall clock.

    한 번의 `deliver_next` 는 `read_history` 를 최대 `max_history_pages` 회 부른 뒤
    `post_message` 를 한 번 부른다 (`events.py:2833`-`2875`, `slack_projection.py:656`).
    호출 하나의 상한은 `_WORST_CASE_MULTIPLIER` 가 설명하는 이유로 `2 x timeout` 이다.
    """
    return _WORST_CASE_MULTIPLIER * timeout_seconds * (max_history_pages + 1)


def validate_call_budget(
    *,
    timeout_seconds: float,
    max_history_pages: int,
    lease_seconds: int,
) -> None:
    """Reject a configuration whose worst-case call sum outlives the lease.

    C-1 의무 2 의 기준은 **한 `deliver_next` 안의 모든 호출 합계**다 (D-024 항목 3).
    호출 하나하나가 lease 안에 들어와도 합이 넘으면 `mark_delivered` 가 lease conflict 로
    터져 terminal 판정이 통째로 버려진다 — dead letter 도 hold 도 안 생긴다.

    **호출 시점이 아니라 구성 시점에 막는다.** 호출 시점에는 이미 lease 를 쥐고 있어서
    거부해도 그 event 가 실패로 기록된다. `HttpSlackTransport.__init__` 이 이것을 부르므로
    예산을 넘는 transport 는 **만들 수조차 없다** — 아무도 안 부르는 validator 로 두면
    FR-016 이 실제로는 안 닫힌다 (wave 5 review P1-3).
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds는 유한한 양수여야 합니다.")
    if max_history_pages < 1:
        raise ValueError("max_history_pages는 1 이상이어야 합니다.")
    if lease_seconds < 1:
        raise ValueError("lease_seconds는 1 이상이어야 합니다.")
    worst_case = worst_case_call_seconds(timeout_seconds, max_history_pages)
    if worst_case >= lease_seconds:
        raise ValueError(
            "Slack 호출 예산이 lease를 넘습니다: "
            f"{_WORST_CASE_MULTIPLIER} x timeout {timeout_seconds}s "
            f"x (pages {max_history_pages} + 1) = {worst_case}s >= lease {lease_seconds}s"
        )


class HttpSlackTransport:
    """`SlackTransport` (`slack_projection.py`) backed by the Slack Web API.

    `bot_token` 은 `SecretStr` 이다. **어떤 예외 message 에도 들어가지 않고 redirect 를
    따라가지도 않는다** — `add_unredirected_header` 와 `_NoRedirect` 가 후자를 막는다.

    `max_history_pages` 와 `lease_seconds` 를 **요구한다.** 그래야 C-1 의무 2 를 구성
    시점에 강제할 수 있다. 둘을 선택 인자로 두면 예산 검증이 아무도 안 부르는 함수가 되고
    FR-016 은 안 닫힌다.

    `base_url` 을 여는 이유는 test 다. 기본값은 실제 endpoint 이므로 잘못 쓰면 실물로 나간다.
    """

    def __init__(
        self,
        *,
        bot_token: SecretStr,
        timeout_seconds: float,
        max_history_pages: int,
        lease_seconds: int,
        base_url: str = SLACK_API_BASE,
    ) -> None:
        if not bot_token.get_secret_value().strip():
            raise ValueError("bot_token은 비어 있을 수 없습니다.")
        if not base_url.strip():
            raise ValueError("base_url은 비어 있을 수 없습니다.")
        validate_call_budget(
            timeout_seconds=timeout_seconds,
            max_history_pages=max_history_pages,
            lease_seconds=lease_seconds,
        )
        self._bot_token = bot_token
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(_NoRedirect())
        # **선언한 예산을 보관한다.** 검증에만 쓰고 버리면 그 숫자가 실제로 읽는 page 수와
        # 묶이지 않는다 — `SlackProjectionDestination` 은 자기 기본값을 쓰므로 `pages=1` 로
        # 통과시킨 transport 를 기본 destination 에 물리면 실제 최악이 lease 를 넘고,
        # `validate_call_budget` 자신이 적은 대로 dead letter 도 hold 도 안 남는다
        # (wave 6 failure-recovery review P1). 배선하는 쪽이 이 값을 읽어 대조한다.
        #
        # destination 이 이 값과 다르면 **거부**하게 만드는 것은 `slack_projection.py` 를
        # 열어야 해서 별도 item 이다 (index.yaml `transport-destination-page-binding`).
        self.max_history_pages = max_history_pages
        self.lease_seconds = lease_seconds

    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        """Post one Card and return the `channel`/`ts` that identify it (C-1.1).

        `channel` 과 `metadata` 를 payload **뒤에** 둔다. dict literal 은 뒤 key 가 이기므로
        payload 가 무엇을 담고 있든 우리 값이 나간다.
        """
        failure: SlackTransportError | None = None
        interruption_kind: str | None = None
        try:
            response = self._call(
                _POST_MESSAGE,
                lambda: json.dumps(
                    {
                        **_json_object(payload),
                        "channel": channel,
                        "metadata": _json_object(marker),
                    }
                ).encode("utf-8"),
                _JSON_CONTENT_TYPE,
            )
            # `ts` 를 먼저 뽑는다. `channel` 만 없는 응답에서도 `ts` 를 손에 쥐어야 자가검사가
            # 방금 보낸 probe 를 지울 수 있다 — `channel` 은 우리가 넘긴 값이라 이미 안다.
            ts = _require_text(response, "ts")
            response_channel = _require_text(response, "channel")
        except SlackTransportError as error:
            failure = _sanitized_transport_error(error)
        except BaseException as error:
            interruption_kind = _interruption_kind(error)
            _clear_exception_frames(error)
        if failure is not None:
            payload = {}
            marker = {}
            raise failure from None
        if interruption_kind is not None:
            payload = {}
            marker = {}
            _raise_sanitized_interruption(interruption_kind)
        return SlackSendResult(channel=response_channel, ts=ts)

    def post_ephemeral(self, *, channel: str, user: str, text: str) -> None:
        """Send one fixed, non-authoritative interaction outcome."""

        failure: SlackTransportError | None = None
        try:
            self._call(
                _POST_EPHEMERAL,
                lambda: json.dumps({"channel": channel, "user": user, "text": text}).encode(
                    "utf-8"
                ),
                _JSON_CONTENT_TYPE,
            )
        except SlackTransportError as error:
            failure = _sanitized_transport_error(error)
        if failure is not None:
            text = ""
            raise failure from None

    def delete_message(self, *, channel: str, ts: str) -> None:
        """Delete one message this bot posted (H-3.2).

        **자가검사가 남긴 probe 를 치우는 데만 쓴다.** Card 를 지우는 데 쓰지 않는다 —
        Card 의 수명은 Outbox 계약이 갖는다.

        scope 는 `chat:write` 하나이고 이미 갖고 있다. Slack 문서가 bot token 에 대해
        "this method may delete only messages posted by that bot" 으로 한정하므로 남의
        message 를 지울 수단이 되지 않는다.
        """
        self._call(
            _DELETE_MESSAGE,
            lambda: json.dumps({"channel": channel, "ts": ts}).encode("utf-8"),
            _JSON_CONTENT_TYPE,
        )

    def read_history(
        self,
        *,
        channel: str,
        cursor: str | None,
        limit: int,
    ) -> SlackHistoryPage:
        """Read one page newest-first, always with `include_all_metadata` (C-1.2).

        `include_all_metadata` 를 빼면 `event_type` 만 오고 `event_payload` 가 안 온다
        (research S3). 그러면 marker 를 하나도 못 읽고 history 소진이 미전송으로 판정되어
        **매 재시도마다 Card 가 한 장씩 는다.** 인자로 열지 않고 여기서 고정한다.
        """
        form: dict[str, str] = {
            "channel": channel,
            "limit": str(limit),
            "include_all_metadata": "true",
        }
        if cursor is not None:
            form["cursor"] = cursor
        response = self._call(
            _CONVERSATIONS_HISTORY,
            lambda: urllib.parse.urlencode(form).encode("utf-8"),
            _FORM_CONTENT_TYPE,
        )
        return SlackHistoryPage(
            messages=_read_messages(response.get("messages")),
            next_cursor=_read_next_cursor(response.get("response_metadata")),
        )

    def _call(
        self,
        method: str,
        build_body: Callable[[], bytes],
        content_type: str,
    ) -> Mapping[str, object]:
        """Make one call and turn every failure into `SlackTransportError` (C-1 의무 1).

        **body 를 만드는 것과 `Request` 를 세우는 것까지 try 안이다.** 둘 다 예외를 낼 수
        있다 — 직렬화 불가 payload 는 `TypeError`, scheme 없는 `base_url` 은 `ValueError`
        다. try 밖에 두면 raw 예외가 그대로 새고, 그러면 C-1 의무 1 이 깨진다
        (wave 5 contract review P1-1 이 실측했다). destination 의 `_rewrap` 이 두 번째
        방어선으로 있지만 C-1 은 그것을 "대체가 아니다" 로 못박았고, H-3 의 readback
        자가검사는 destination 을 거치지 않고 transport 를 직접 부른다.

        **`except Exception` 이 의도다.** 좁히면 빠뜨린 예외가 분류를 건너뛴다.
        """
        deadline = time.monotonic() + self._timeout_seconds
        try:
            body = build_body()
            request = urllib.request.Request(
                f"{self._base_url}/{method}",
                data=body,
                method="POST",
            )
            request.add_header("Content-Type", content_type)
            # **unredirected 다.** redirect 를 따라갈 때 옮겨지지 않는다. `_NoRedirect` 가
            # 이미 redirect 를 막지만 방어선을 둘 둔다 — 하나가 빠지면 token 이 network
            # 로 나간다.
            request.add_unredirected_header(
                "Authorization", f"Bearer {self._bot_token.get_secret_value()}"
            )
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw = _read_within(response, deadline)
                return _decode(raw, status_code=response.status)
        except SlackTransportError:
            # 이미 분류 가능한 형태다. 다시 감싸면 원인이 흐려진다.
            raise
        except urllib.error.HTTPError as error:
            _fail_http(error, deadline)
        except Exception as error:
            # 연결 실패, timeout, TLS, DNS, 직렬화 — 전부 여기다. code 를 비워 retryable 로
            # 흐르게 한다. 그쪽이 보수적이다 (D-020 항목 1).
            _fail(error)


class EphemeralSlackTransport(Protocol):
    def post_ephemeral(self, *, channel: str, user: str, text: str) -> None: ...


_SAFE_INTERACTION_MESSAGES: Final[Mapping[SafeInteractionOutcome, str]] = {
    SafeInteractionOutcome.ALREADY_COMPLETED: "This Proposal action was already completed.",
    SafeInteractionOutcome.EXPIRED: "This Proposal action expired. Request a new Review Card.",
    SafeInteractionOutcome.STALE: "This Review Card is no longer current.",
    SafeInteractionOutcome.DENIED: "This Proposal action is not available to you here.",
    SafeInteractionOutcome.UNAVAILABLE: "This Proposal action could not be completed safely.",
}


class SlackInteractionFeedback:
    """Map a closed outcome to one Slack ephemeral message without durable secrets."""

    def __init__(self, transport: EphemeralSlackTransport) -> None:
        self._transport = transport

    def send(
        self,
        command: IngressCommandView,
        outcome: SafeInteractionOutcome,
    ) -> None:
        if command.provider is not ChannelProvider.SLACK:
            return
        channel = command.channel_ref.channel_id
        if channel is None:
            raise ValueError("Slack feedback channel is unavailable.")
        self._transport.post_ephemeral(
            channel=channel,
            user=command.external_actor_key,
            text=_SAFE_INTERACTION_MESSAGES[outcome],
        )


def _sanitized_transport_error(error: SlackTransportError) -> SlackTransportError:
    safe = SlackTransportError(
        "Slack API request failed.",
        error_code=safe_slack_error_code(error.error_code),
        status_code=error.status_code,
        retry_after_seconds=error.retry_after_seconds,
        transport_exception=safe_transport_exception(error.transport_exception),
    )
    _clear_exception_frames(error)
    return safe


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


def _interruption_kind(error: BaseException) -> str:
    if isinstance(error, KeyboardInterrupt):
        return "keyboard_interrupt"
    if isinstance(error, SystemExit):
        return "system_exit"
    if isinstance(error, GeneratorExit):
        return "generator_exit"
    return "base_exception"


def _raise_sanitized_interruption(kind: str) -> NoReturn:
    if kind == "keyboard_interrupt":
        raise KeyboardInterrupt("Slack message delivery interrupted.") from None
    if kind == "system_exit":
        raise SystemExit("Slack message delivery interrupted.") from None
    if kind == "generator_exit":
        raise GeneratorExit from None
    raise BaseException("Slack message delivery interrupted.") from None


def _json_value(value: object) -> object:
    """Copy an immutable presentation tree into JSON-native containers."""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _json_object(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _json_value(item) for key, item in value.items()}


def _read_within(response: object, deadline: float) -> bytes:
    """Read one body under a wall-clock deadline and a size cap.

    `urlopen(timeout=)` 은 socket 연산 하나마다 걸리는 값이라 호출 전체를 묶지 않는다.
    조각마다 deadline 을 봐서 그 구멍을 막는다. 상한 계산은
    `_WORST_CASE_MULTIPLIER` 에 있다.
    """
    # **`read1` 을 먼저 쓴다.** `read(n)` 은 Content-Length 가 있으면 n byte 를 다 채울
    # 때까지 막혀서 (`http.client` 의 `_safe_read`) deadline 검사가 그 뒤에야 돈다 —
    # 상대가 찔끔씩 보내면 loop 가 한 바퀴도 못 돈다. `read1` 은 syscall 한 번 분량만
    # 돌려주므로 조각마다 deadline 을 볼 수 있다.
    read = getattr(response, "read1", None) or getattr(response, "read", None)
    if read is None:  # pragma: no cover — urlopen 응답은 항상 read 를 갖는다
        raise SlackTransportError(
            "Slack 응답을 읽을 수 없습니다.",
            transport_exception="unreadable_response",
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise SlackTransportError(
                "Slack 응답이 deadline 안에 끝나지 않았습니다.",
                transport_exception="deadline_exceeded",
            )
        chunk = read(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise SlackTransportError(
                "Slack 응답이 크기 상한을 넘었습니다.",
                transport_exception="response_too_large",
            )
        chunks.append(chunk)


def _decode(raw: bytes, *, status_code: int) -> Mapping[str, object]:
    """Read one Slack response. **`ok` 가 있어야 Slack 봉투다** (H-2.2).

    Slack 은 application error 를 HTTP 200 + `ok: false` 로 준다. status code 만 보고
    성공을 판정하면 `invalid_auth` 가 성공으로 읽힌다.

    **`error` key 만 보고 code 를 채우지 않는다.** proxy·WAF·gateway 가 `{"error": ...}`
    모양의 JSON 을 준다. 그것을 Slack code 로 실으면 allowlist 밖이라 **terminal** 이 되고,
    terminal 은 그 destination 의 이후 Card 를 전부 멈추는 되돌릴 수 없는 hold 를 만든다
    (wave 5 failure-recovery review P0-1 이 실측했다). Slack 의 error 응답은 언제나
    `ok: false` 를 달고 오고 중간 장비의 봉투는 안 단다 — 그 차이가 판별자다.
    """
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as error:
        # proxy·WAF 가 HTML 을 줄 수 있다. code 없는 실패라 retryable 이고 그것이 맞다.
        raise SlackTransportError(
            "Slack 응답이 JSON이 아닙니다.",
            status_code=status_code,
            transport_exception=type(error).__qualname__,
        ) from error
    if not isinstance(decoded, Mapping):
        raise SlackTransportError(
            "Slack 응답이 object가 아닙니다.",
            status_code=status_code,
            transport_exception="non_object_response",
        )
    if decoded.get("ok") is True:
        return decoded
    raise SlackTransportError(
        "Slack이 성공을 반환하지 않았습니다.",
        error_code=_slack_error_code(decoded),
        status_code=status_code,
        transport_exception=None if decoded.get("ok") is False else "not_a_slack_envelope",
    )


def _fail_http(error: urllib.error.HTTPError, deadline: float) -> NoReturn:
    """Turn one HTTP-level failure into `SlackTransportError`.

    body 를 읽어 Slack 봉투면 code 를 싣는다. **봉투가 아니면 code 를 비운다** — 위
    `_decode` 와 같은 이유다. 중간 장비의 5xx 를 terminal 로 만들면 일시 장애가 되돌릴 수
    없는 hold 가 된다.

    429 는 `Retry-After` 를 싣는다. **backoff 에 주입하지 않는다** — dispatcher 에 지연을
    넘길 인자가 없다 (research R-007). 진단으로만 남긴다.
    """
    body_error: str | None = None
    try:
        payload = json.loads(_read_within(error, deadline).decode("utf-8"))
        if isinstance(payload, Mapping):
            body_error = _slack_error_code(payload)
    except Exception:
        # body 를 못 읽어도 status 는 살린다.
        body_error = None
    raise SlackTransportError(
        f"Slack이 HTTP {error.code}를 반환했습니다.",
        error_code=body_error,
        status_code=error.code,
        retry_after_seconds=_read_retry_after(error),
        transport_exception=None if body_error else type(error).__qualname__,
    ) from error


def _slack_error_code(payload: Mapping[str, object]) -> str | None:
    """Read a Slack error code, or `None` if this is not a Slack envelope.

    `ok: false` 가 있어야 Slack 이 말한 것이다. 그 key 가 없으면 중간 장비의 JSON 이고,
    거기 담긴 `error` 는 Slack code 가 아니다.
    """
    if payload.get("ok") is not False:
        return None
    code = payload.get("error")
    return code if isinstance(code, str) else None


def _fail(error: BaseException) -> NoReturn:
    """Wrap one transport-layer failure. **Never name the credential.**

    message 에 요청 header 를 넣지 않는다. token 이 dead letter 와 log 로 새는 유일한
    경로가 그것이다.
    """
    origin = type(error)
    module = origin.__module__.replace(".", "_")
    label = origin.__qualname__ if module == "builtins" else f"{module}_{origin.__qualname__}"
    raise SlackTransportError(
        f"Slack 호출이 실패했습니다: {origin.__qualname__}",
        transport_exception=label,
    ) from error


def _read_retry_after(error: urllib.error.HTTPError) -> int | None:
    value = error.headers.get("Retry-After") if error.headers is not None else None
    if value is None:
        return None
    try:
        seconds = int(str(value).strip())
    except ValueError:
        # RFC 는 HTTP-date 형식도 허용한다. 진단용 값이라 못 읽으면 비운다.
        return None
    return seconds if seconds >= 0 else None


def _require_text(response: Mapping[str, object], key: str) -> str:
    value = response.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SlackTransportError(
            f"Slack 성공 응답에 {key}가 없습니다.",
            transport_exception="missing_success_field",
        )
    return value


def _read_messages(raw: object) -> tuple[SlackHistoryMessage, ...]:
    """Map Slack messages onto the view `reconcile()` reads.

    **`metadata` 를 그대로 옮긴다** (C-1.2). 없는 key 를 만들어 채우지 않는다 — 그렇게 하면
    `read_slack_marker` 가 "우리 event_type 인데 event_payload 가 없다" 를 조회 결함으로
    읽지 못하고 지나간다.

    **순서를 건드리지 않는다.** Slack 이 최신부터 준다 (research R-011). 정렬하거나
    뒤집으면 C-2.2 의 "먼저 만난 것이 이긴다" 판정이 뒤집혀 중복 Card 가 난다.

    `ts` 가 없는 message 는 **건너뛴다.** 예외를 던지면 그 뒤의 진짜 marker 를 못 읽고
    판정 불가로 떨어져 되돌릴 수 없는 hold 가 된다.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    messages: list[SlackHistoryMessage] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        ts = item.get("ts")
        if not isinstance(ts, str) or not ts.strip():
            continue
        metadata = item.get("metadata")
        app_id = item.get("app_id")
        messages.append(
            SlackHistoryMessage(
                ts=ts,
                metadata=metadata if isinstance(metadata, Mapping) else None,
                app_id=app_id if isinstance(app_id, str) else None,
            )
        )
    return tuple(messages)


def _read_next_cursor(raw: object) -> str | None:
    """Read the pagination cursor. **Empty means last page, not a cursor.**

    Slack 은 더 볼 것이 없으면 `next_cursor` 를 빈 문자열로 준다. 그대로 넘기면
    `SlackHistoryPage` 가 `ValueError` 로 거부하고, 그 예외는 계약 밖이라 분류를 건너뛴다.
    `None` 으로 바꿔야 `reconcile` 이 "history 소진" 으로 읽는다 (D-023 항목 3).
    """
    if not isinstance(raw, Mapping):
        return None
    cursor = raw.get("next_cursor")
    if not isinstance(cursor, str) or not cursor.strip():
        return None
    return cursor


class ReadbackOutcomeStatus(StrEnum):
    """Closed startup decision returned by the readback layer (D-031)."""

    READY = "READY"
    DEGRADED_CLEANUP = "DEGRADED_CLEANUP"
    READBACK_FAILED = "READBACK_FAILED"
    HARD_BLOCKED_NO_POST = "HARD_BLOCKED_NO_POST"


class ReadbackFailureCode(StrEnum):
    """Closed readback failure taxonomy from FR-035."""

    PROBE_INPUT_INVALID = "PROBE_INPUT_INVALID"
    HISTORY_READ_FAILED = "HISTORY_READ_FAILED"
    PROBE_NOT_FOUND = "PROBE_NOT_FOUND"
    APP_ID_MISMATCH = "APP_ID_MISMATCH"
    MARKER_UNREADABLE = "MARKER_UNREADABLE"
    MARKER_MISMATCH = "MARKER_MISMATCH"
    RESPONSE_CHANNEL_MISMATCH = "RESPONSE_CHANNEL_MISMATCH"


class ReadbackLifecycleCause(StrEnum):
    """Supplied durable cause that requires a no-post startup outcome.

    This is deliberately not an outcome enum.  In particular,
    `HARD_BLOCKED_NO_POST` cannot be persisted as a lifecycle cause (FR-026).
    T013 will own the repository that supplies these values.
    """

    POST_INTENT_RECORDED = "POST_INTENT_RECORDED"
    AMBIGUOUS_POST = "AMBIGUOUS_POST"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    CLAIM_FAILED = "CLAIM_FAILED"
    RESOLUTION_COMMIT_FAILED = "RESOLUTION_COMMIT_FAILED"


class SafeSlackProviderErrorCode(StrEnum):
    """Provider codes approved for the typed diagnostic boundary.

    Unknown provider text is discarded instead of being copied into logs, metrics, exceptions,
    or persisted diagnostics.  This is a closed contract, not a pattern-based sanitizer.
    """

    CANT_DELETE_MESSAGE = "cant_delete_message"
    CHANNEL_NOT_FOUND = "channel_not_found"
    MESSAGE_NOT_FOUND = "message_not_found"
    MISSING_SCOPE = "missing_scope"
    NOT_AUTHED = "not_authed"
    INVALID_AUTH = "invalid_auth"
    TOKEN_REVOKED = "token_revoked"
    ACCOUNT_INACTIVE = "account_inactive"
    NO_PERMISSION = "no_permission"
    RATELIMITED = "ratelimited"
    RATE_LIMITED = "rate_limited"
    REQUEST_TIMEOUT = "request_timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"
    FATAL_ERROR = "fatal_error"


@dataclass(frozen=True, slots=True)
class CleanupFailureDetail:
    """One immutable, safe cleanup failure detail (FR-034)."""

    cleanup_failure_count: int
    provider_error_code: SafeSlackProviderErrorCode | None

    def __post_init__(self) -> None:
        if self.cleanup_failure_count < 1:
            raise ValueError("cleanup_failure_count는 1 이상이어야 합니다.")


@dataclass(frozen=True, slots=True)
class ReadbackDiagnosticData:
    """Closed safe-field surface shared with the future composition root (FR-030)."""

    diagnostic_code: str
    channel_id: str
    app_id: str
    probe_id: str | None = None
    message_ts: str | None = None
    cleanup_failure_count: int | None = None
    provider_error_code: SafeSlackProviderErrorCode | None = None
    operator_action: str | None = None


@dataclass(frozen=True, slots=True)
class ReadbackOutcome:
    """Immutable startup decision; this layer never logs or activates a worker."""

    status: ReadbackOutcomeStatus
    primary_failure_code: ReadbackFailureCode | None
    secondary_cleanup_failure: CleanupFailureDetail | None
    diagnostic_data: ReadbackDiagnosticData
    lifecycle_cause: ReadbackLifecycleCause | None = None

    def __post_init__(self) -> None:
        if self.status in {
            ReadbackOutcomeStatus.READY,
            ReadbackOutcomeStatus.DEGRADED_CLEANUP,
        }:
            if self.primary_failure_code is not None or self.lifecycle_cause is not None:
                raise ValueError("activation 허용 outcome은 failure code/cause를 가질 수 없습니다.")
            if self.secondary_cleanup_failure is not None:
                raise ValueError("activation 허용 outcome은 secondary failure를 가질 수 없습니다.")
            return
        if self.status is ReadbackOutcomeStatus.READBACK_FAILED:
            if self.primary_failure_code is None:
                raise ValueError("READBACK_FAILED는 primary_failure_code가 필요합니다.")
            if self.lifecycle_cause is not None:
                raise ValueError("READBACK_FAILED는 lifecycle cause를 가질 수 없습니다.")
            return
        if self.primary_failure_code is not None or self.secondary_cleanup_failure is not None:
            raise ValueError("HARD_BLOCKED_NO_POST는 readback failure를 가질 수 없습니다.")
        if self.lifecycle_cause is None:
            raise ValueError("HARD_BLOCKED_NO_POST는 lifecycle cause가 필요합니다.")

    @property
    def allows_activation(self) -> bool:
        return self.status in {
            ReadbackOutcomeStatus.READY,
            ReadbackOutcomeStatus.DEGRADED_CLEANUP,
        }


# 자가검사가 보내는 probe message 의 본문. 사람이 보고 무엇인지 알아야 한다.
_PROBE_TEXT: Final = "AMPLAI marker readback self-check. 이 message 는 지워도 된다."

# **probe 는 어느 destination 의 것도 아니어야 한다.**
#
# probe 는 진짜 채널에 우리 `event_type` 과 `app_id` 를 달고 남는다. 그 상태로 진짜
# marker 와 같은 `destination_ref` 를 달면 `reconcile` 이 둘을 구분하지 못한다. 결과가 둘
# 다 나쁘다 (wave 6 failure-recovery review P0 가 실측).
#
# - probe 를 event N 의 marker 로 만들면 N 의 재시도에서 `reconcile` 이 probe 를 N 의
#   Card 로 읽는다. Card 는 한 장도 안 나갔는데 DELIVERED 로 기록된다.
# - probe 의 sequence 가 더 낮으면 "하위 sequence 를 먼저 만났다 → 미전송" 판정이 돈다.
#   probe 는 재시작마다 새로 올라가므로 **오래된 sequence 를 가진 가장 새로운 message**
#   이고, 이것이 `reconcile` 이 기대는 순서 불변을 정확히 깬다. 결과는 중복 Card 다.
#
# `_our_marker` 가 `destination_ref` 불일치 marker 를 두 loop 모두에서 배제하므로
# (`slack_projection.py:708`) 이 값 하나로 probe 가 `reconcile` 에 안 보이게 된다.
#
# **충돌할 수 없다.** 진짜 값은 `provider:{provider}:{sha256 hexdigest}` 이고
# (`events.py:1442`) hexdigest 는 소문자 hex 64자다. 아래 접미는 `z` 64자라 hex 가 아니다.
#
# **길이를 진짜와 맞춘다.** 짧게 두면 probe 의 metadata 가 진짜보다 작아지고, metadata 크기
# 상한이 두 값 사이에 있으면 자가검사는 통과하는데 첫 진짜 Card 가 `metadata_too_large` 로
# terminal 이 된다. 그 상한은 아직 모른다 (OQ-003) — 모르는 값을 사이에 두지 않는다.
PROBE_DESTINATION_REF: Final = "provider:slack:" + "z" * 64

# 방금 게시해 Slack이 `ts`를 확인한 probe를 되읽는 한 page의 크기다. 위치나 최근 message
# 개수로 identity를 추정하지 않고, 오직 confirmed `ts`로 찾는다 (FR-026).
_PROBE_HISTORY_LIMIT: Final = 100


def build_probe_marker(event: OutboxEventView) -> dict[str, object]:
    """Build the probe marker the self-check requires (H-3.1).

    `build_slack_marker` 는 event 의 진짜 `destination_ref` 를 심으므로 그대로 쓰면
    `verify_marker_readback` 이 거부한다. 여기서 그 한 필드만 바꿔 준다.

    **손으로 조립하게 두지 않는 이유**는 round 1 의 P0 가 정확히 그 안내에서 나왔기
    때문이다. 안내문이 아니라 함수로 준다. 거부 guard 는 그대로 둔다 — 두 겹이다.
    """
    marker = build_slack_marker(event)
    payload = dict(cast("Mapping[str, object]", marker["event_payload"]))
    payload["destination_ref"] = PROBE_DESTINATION_REF
    return {**marker, "event_payload": payload}


class ProbeTransport(SlackTransport, Protocol):
    """`SlackTransport` plus the one call the self-check needs to clean up after itself.

    `SlackTransport` 를 안 늘린다 — 그 Protocol 은 Package 3 에서 gate PASS 했고 H-6 이
    변경을 금지한다. 자가검사만 쓰는 능력이므로 여기서 좁게 더한다.
    """

    def delete_message(self, *, channel: str, ts: str) -> None: ...


def verify_marker_readback(
    transport: ProbeTransport,
    *,
    channel: str,
    app_id: str,
    probe_marker: Mapping[str, object],
    lifecycle_cause: ReadbackLifecycleCause | None = None,
) -> ReadbackOutcome:
    """Return a typed startup outcome without operator output (H-3, FR-018).

    `probe_marker` 는 호출자가 `build_probe_marker` 로 만든다. 여기서 새로 만들지 않는
    이유는 **실제로 나가는 것과 같은 것**을 검사해야 하기 때문이다. 자가검사 전용 모양을
    만들면 그 모양만 검증된다.

    **단 `destination_ref` 만은 `PROBE_DESTINATION_REF` 여야 한다.** 그 하나로 probe 가
    `reconcile` 의 판정에서 빠진다. 이유는 그 상수에 적었다. 호출자가 진짜 destination 의
    값을 넣으면 **보내기 전에** 거부한다 — 보낸 뒤에 알면 이미 채널에 남는다.

    History scan으로 이전 probe를 추정하지 않는다. T013의 durable lifecycle이 supplied cause를
    제공하면 network 호출 전에 `HARD_BLOCKED_NO_POST`를 반환한다. Cause가 없을 때만 fresh
    unit self-check를 실행한다. Cleanup-only 실패는 `DEGRADED_CLEANUP`; readback 실패는 closed
    primary code와 optional safe cleanup detail을 가진 `READBACK_FAILED`다 (D-031).
    """
    expected = probe_marker.get("event_payload")
    if lifecycle_cause is not None:
        return _readback_outcome(
            status=ReadbackOutcomeStatus.HARD_BLOCKED_NO_POST,
            channel=channel,
            app_id=app_id,
            # T008 has no separately validated durable ProbeIdentity.  Marker input is not a
            # trusted substitute, so no marker value crosses this diagnostic short-circuit.
            probe_id=None,
            lifecycle_cause=lifecycle_cause,
        )
    safe_probe_id = _probe_id(expected)
    if not _valid_probe_marker(probe_marker):
        return _readback_failure(
            ReadbackFailureCode.PROBE_INPUT_INVALID,
            channel=channel,
            app_id=app_id,
            probe_id=safe_probe_id,
        )
    expected_payload = cast("Mapping[str, object]", expected)
    if not callable(getattr(transport, "delete_message", None)):
        return _readback_failure(
            ReadbackFailureCode.PROBE_INPUT_INVALID,
            channel=channel,
            app_id=app_id,
            probe_id=safe_probe_id,
        )
    result = transport.post_message(
        channel=channel,
        payload={"text": _PROBE_TEXT},
        marker=probe_marker,
    )
    primary_failure: ReadbackFailureCode | None = None
    primary_provider_code: SafeSlackProviderErrorCode | None = None
    if result.channel != channel:
        # A confirmed response identity contradicts the configured target.  Do not claim a
        # history absence that was never observed; fail with the dedicated FR-035 code and clean
        # only Slack's confirmed identity.
        primary_failure = ReadbackFailureCode.RESPONSE_CHANNEL_MISMATCH
    else:
        try:
            page = transport.read_history(
                channel=result.channel,
                cursor=None,
                limit=_PROBE_HISTORY_LIMIT,
            )
        except Exception as error:
            primary_failure = ReadbackFailureCode.HISTORY_READ_FAILED
            primary_provider_code = _safe_provider_error_code(error)
        else:
            primary_failure = _inspect_probe(
                page,
                result,
                app_id=app_id,
                expected=expected_payload,
            )
    cleanup_failure: CleanupFailureDetail | None = None
    try:
        transport.delete_message(channel=result.channel, ts=result.ts)
    except Exception as error:
        cleanup_failure = CleanupFailureDetail(
            cleanup_failure_count=1,
            provider_error_code=_safe_provider_error_code(error),
        )
    if primary_failure is not None:
        return _readback_failure(
            primary_failure,
            channel=channel,
            app_id=app_id,
            probe_id=safe_probe_id,
            message_ts=result.ts,
            cleanup_failure=cleanup_failure,
            provider_error_code=primary_provider_code,
        )
    if cleanup_failure is not None:
        return _readback_outcome(
            status=ReadbackOutcomeStatus.DEGRADED_CLEANUP,
            channel=channel,
            app_id=app_id,
            probe_id=safe_probe_id,
            message_ts=result.ts,
            cleanup_failure=cleanup_failure,
        )
    return _readback_outcome(
        status=ReadbackOutcomeStatus.READY,
        channel=channel,
        app_id=app_id,
        probe_id=safe_probe_id,
        message_ts=result.ts,
    )


def _probe_id(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    event_id = payload.get("event_id")
    return event_id if isinstance(event_id, str) and event_id.strip() else None


def _valid_probe_marker(marker: Mapping[str, object]) -> bool:
    """Validate the complete locally-produced marker before any remote side effect."""
    if set(marker) != {"event_type", "event_payload"}:
        return False
    if marker.get("event_type") != SLACK_PROJECTION_EVENT_TYPE:
        return False
    payload = marker.get("event_payload")
    if not isinstance(payload, Mapping):
        return False
    if set(payload) != {
        "event_id",
        "destination_ref",
        "destination_sequence",
        "payload_digest",
    }:
        return False
    event_id = payload.get("event_id")
    sequence = payload.get("destination_sequence")
    digest = payload.get("payload_digest")
    return (
        isinstance(event_id, str)
        and bool(event_id.strip())
        and payload.get("destination_ref") == PROBE_DESTINATION_REF
        and isinstance(sequence, int)
        and not isinstance(sequence, bool)
        and sequence >= 1
        and _valid_digest(digest)
    )


def _valid_digest(value: object) -> bool:
    """Reuse the canonical governance Digest contract instead of duplicating its pattern."""
    if not isinstance(value, str):
        return False
    try:
        _DIGEST_ADAPTER.validate_python(value)
    except ValidationError:
        return False
    return True


def _safe_provider_error_code(error: Exception) -> SafeSlackProviderErrorCode | None:
    if not isinstance(error, SlackTransportError) or error.error_code is None:
        return None
    try:
        return SafeSlackProviderErrorCode(error.error_code)
    except ValueError:
        return None


def _readback_failure(
    code: ReadbackFailureCode,
    *,
    channel: str,
    app_id: str,
    probe_id: str | None,
    message_ts: str | None = None,
    cleanup_failure: CleanupFailureDetail | None = None,
    provider_error_code: SafeSlackProviderErrorCode | None = None,
) -> ReadbackOutcome:
    return _readback_outcome(
        status=ReadbackOutcomeStatus.READBACK_FAILED,
        channel=channel,
        app_id=app_id,
        probe_id=probe_id,
        message_ts=message_ts,
        primary_failure_code=code,
        cleanup_failure=cleanup_failure,
        provider_error_code=provider_error_code,
    )


def _readback_outcome(
    *,
    status: ReadbackOutcomeStatus,
    channel: str,
    app_id: str,
    probe_id: str | None,
    message_ts: str | None = None,
    primary_failure_code: ReadbackFailureCode | None = None,
    cleanup_failure: CleanupFailureDetail | None = None,
    provider_error_code: SafeSlackProviderErrorCode | None = None,
    lifecycle_cause: ReadbackLifecycleCause | None = None,
) -> ReadbackOutcome:
    diagnostic_code = (
        primary_failure_code.value
        if primary_failure_code is not None
        else "SLACK_PROBE_CLEANUP_DEGRADED"
        if status is ReadbackOutcomeStatus.DEGRADED_CLEANUP
        else status.value
    )
    diagnostic_provider_code = provider_error_code
    if cleanup_failure is not None and diagnostic_provider_code is None:
        diagnostic_provider_code = cleanup_failure.provider_error_code
    return ReadbackOutcome(
        status=status,
        primary_failure_code=primary_failure_code,
        secondary_cleanup_failure=cleanup_failure if primary_failure_code is not None else None,
        diagnostic_data=ReadbackDiagnosticData(
            diagnostic_code=diagnostic_code,
            channel_id=channel,
            app_id=app_id,
            probe_id=probe_id,
            message_ts=message_ts,
            cleanup_failure_count=(
                cleanup_failure.cleanup_failure_count if cleanup_failure is not None else None
            ),
            provider_error_code=diagnostic_provider_code,
        ),
        lifecycle_cause=lifecycle_cause,
    )


def _inspect_probe(
    page: SlackHistoryPage,
    result: SlackSendResult,
    *,
    app_id: str,
    expected: Mapping[str, object],
) -> ReadbackFailureCode | None:
    """Compare one confirmed probe without raising or retaining raw provider data."""
    probe = next((message for message in page.messages if message.ts == result.ts), None)
    if probe is None:
        return ReadbackFailureCode.PROBE_NOT_FOUND
    if probe.app_id != app_id:
        return ReadbackFailureCode.APP_ID_MISMATCH
    metadata = probe.metadata
    if not isinstance(metadata, Mapping):
        return ReadbackFailureCode.MARKER_UNREADABLE
    event_type = metadata.get("event_type")
    if not isinstance(event_type, str):
        return ReadbackFailureCode.MARKER_UNREADABLE
    if event_type != SLACK_PROJECTION_EVENT_TYPE:
        return ReadbackFailureCode.MARKER_MISMATCH
    if not isinstance(metadata.get("event_payload"), Mapping):
        return ReadbackFailureCode.MARKER_UNREADABLE
    try:
        recovered = read_slack_marker(probe, app_id=app_id)
    except SlackProjectionMetadataUnreadableError:
        return ReadbackFailureCode.MARKER_UNREADABLE
    if recovered is None:
        return ReadbackFailureCode.MARKER_UNREADABLE
    mismatched = [
        name
        for name, actual in (
            ("event_id", recovered.event_id),
            ("destination_ref", recovered.destination_ref),
            ("destination_sequence", recovered.destination_sequence),
            ("payload_digest", recovered.payload_digest),
        )
        if expected.get(name) != actual
    ]
    if mismatched:
        return ReadbackFailureCode.MARKER_MISMATCH
    return None
