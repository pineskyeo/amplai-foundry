"""Real Slack Web API transport for `SlackTransport`, over the standard library.

`slack_projection.py` 는 Slack 을 호출하지 않는다 — 그 module 은 계약과 destination 만
갖는다. 실제 호출은 여기다. 두 파일을 나눈 이유는 방향이 아니라 **위험**이다. network
코드를 그쪽에 넣으면 gate PASS 한 188개 test 가 network 를 다루는 파일에 붙는다
(plan.md P-004).

runtime dependency 를 늘리지 않는다. `urllib.request` 로 충분하다 — 호출량이 작고
(`chat.postMessage` 는 channel 당 초당 1건), 필요한 것은 POST·header·timeout 뿐이며,
재시도는 이미 `OutboxDispatcher` 가 소유한다 (research R-013).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, NoReturn

from pydantic import SecretStr

from amplai_foundry.governance.slack_projection import (
    SlackHistoryMessage,
    SlackHistoryPage,
    SlackSendResult,
    SlackTransportError,
)

SLACK_API_BASE: Final = "https://slack.com/api"

_POST_MESSAGE: Final = "chat.postMessage"
_CONVERSATIONS_HISTORY: Final = "conversations.history"

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

# payload 가 이 key 를 갖고 있으면 우리가 세우는 값을 덮는다. `metadata` 가 덮이면
# marker 가 안 나가고, 그러면 reconcile 이 영원히 못 찾아 **매 재시도마다 Card 가 한 장씩
# 는다.** hold 가 아니라 조용한 중복이다. 그래서 전송 전에 막는다.
_RESERVED_PAYLOAD_KEYS: Final = frozenset({"channel", "metadata"})

# 값 하나에 변수 하나다 (contracts H-4.1). 합쳐 담으면 부분 구성이 조용히 통과한다.
# `AMPLAI_` 접두는 같은 환경에 있는 다른 Slack 도구와 섞이지 않게 한다.
SLACK_BOT_TOKEN_ENV: Final = "AMPLAI_SLACK_BOT_TOKEN"
SLACK_SIGNING_SECRET_ENV: Final = "AMPLAI_SLACK_SIGNING_SECRET"


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
    """
    source = os.environ if environ is None else environ
    bot_token = (source.get(SLACK_BOT_TOKEN_ENV) or "").strip()
    signing_secret = (source.get(SLACK_SIGNING_SECRET_ENV) or "").strip()
    if not bot_token and not signing_secret:
        return None
    missing = [
        name
        for name, value in (
            (SLACK_BOT_TOKEN_ENV, bot_token),
            (SLACK_SIGNING_SECRET_ENV, signing_secret),
        )
        if not value
    ]
    if missing:
        # 값을 message 에 넣지 않는다. 이름만 적는다.
        raise ValueError(f"Slack credential 구성이 불완전합니다. 빠진 변수: {missing}")
    return SlackCredentials(
        bot_token=SecretStr(bot_token),
        signing_secret=SecretStr(signing_secret),
    )


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

    한 번의 `deliver_next` 는 `read_history` 를 최대 `max_history_pages` 회 부른 뒤
    `post_message` 를 한 번 부른다. 그래서 최악은 `timeout x (pages + 1)` 이다.

    **호출 시점이 아니라 구성 시점에 막는다.** 호출 시점에는 이미 lease 를 쥐고 있어서
    거부해도 그 event 가 실패로 기록된다. 구성이 애초에 성립하지 않는 것은 그 전에 알아야
    한다. `SlackProjectionDestination.__init__` 이 `max_attempts` 를 검사하는 것과 같은
    자리다.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds는 0보다 커야 합니다.")
    if max_history_pages < 1:
        raise ValueError("max_history_pages는 1 이상이어야 합니다.")
    if lease_seconds < 1:
        raise ValueError("lease_seconds는 1 이상이어야 합니다.")
    worst_case = timeout_seconds * (max_history_pages + 1)
    if worst_case >= lease_seconds:
        raise ValueError(
            "Slack 호출 예산이 lease를 넘습니다: "
            f"timeout {timeout_seconds}s x (pages {max_history_pages} + 1) "
            f"= {worst_case}s >= lease {lease_seconds}s"
        )


class HttpSlackTransport:
    """`SlackTransport` (`slack_projection.py`) backed by the Slack Web API.

    `bot_token` 은 `SecretStr` 이다. **어떤 예외 message 에도 들어가지 않는다** — 아래
    `_fail` 이 message 를 만들 때 요청 header 를 넣지 않는 것이 그 규칙이다.

    `base_url` 을 여는 이유는 test 다. 실제 Slack 을 부르지 않고 계약을 검사할 수 있어야
    한다. 기본값은 실제 endpoint 이므로 잘못 쓰면 실물로 나간다.
    """

    def __init__(
        self,
        *,
        bot_token: SecretStr,
        timeout_seconds: float,
        base_url: str = SLACK_API_BASE,
    ) -> None:
        if not bot_token.get_secret_value().strip():
            raise ValueError("bot_token은 비어 있을 수 없습니다.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds는 0보다 커야 합니다.")
        if not base_url.strip():
            raise ValueError("base_url은 비어 있을 수 없습니다.")
        self._bot_token = bot_token
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")

    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult:
        """Post one Card and return the `channel`/`ts` that identify it (C-1.1)."""
        collision = _RESERVED_PAYLOAD_KEYS.intersection(payload)
        if collision:
            # 전송하지 않는다. `metadata` 가 덮이면 marker 가 안 나가고 조용한 중복 Card 가
            # 된다. error_code 는 비운다 — 채우면 allowlist 밖이라 terminal 이 되어
            # 되돌릴 수 없는 hold 를 만든다 (D-020 항목 1).
            raise SlackTransportError(
                f"Slack payload가 예약 key를 덮어씁니다: {sorted(collision)}",
                transport_exception="reserved_payload_key",
            )
        body = {**dict(payload), "channel": channel, "metadata": dict(marker)}
        response = self._call_json(_POST_MESSAGE, body)
        return SlackSendResult(
            channel=_require_text(response, "channel"),
            ts=_require_text(response, "ts"),
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
        response = self._call_form(_CONVERSATIONS_HISTORY, form)
        return SlackHistoryPage(
            messages=_read_messages(response.get("messages")),
            next_cursor=_read_next_cursor(response.get("response_metadata")),
        )

    def _call_json(self, method: str, body: Mapping[str, object]) -> Mapping[str, object]:
        return self._call(method, json.dumps(body).encode("utf-8"), _JSON_CONTENT_TYPE)

    def _call_form(self, method: str, form: Mapping[str, str]) -> Mapping[str, object]:
        encoded = urllib.parse.urlencode(form).encode("utf-8")
        return self._call(method, encoded, _FORM_CONTENT_TYPE)

    def _call(self, method: str, body: bytes, content_type: str) -> Mapping[str, object]:
        """Make one call and turn every failure into `SlackTransportError` (C-1 의무 1).

        **`except Exception` 이 의도다.** 좁히면 빠뜨린 예외가 dispatcher 의 generic
        handler 로 새어 분류를 건너뛴다. terminal 이어야 할 `invalid_auth` 가 조용히
        재시도된다.
        """
        # base_url 은 우리가 만든 값이고 기본값은 https 고정이다. 사용자 입력이 아니다.
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._bot_token.get_secret_value()}",
                "Content-Type": content_type,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                return self._decode(response.read(), status_code=response.status)
        except SlackTransportError:
            # `_decode` 가 이미 분류 가능한 형태로 만든 것이다. 다시 감싸면 원인이 흐려진다.
            raise
        except urllib.error.HTTPError as error:
            self._fail_http(error)
        except Exception as error:
            # 연결 실패, timeout, TLS, DNS — 전부 여기다. code 를 비워 retryable 로
            # 흐르게 한다. 그쪽이 보수적이다 (D-020 항목 1).
            self._fail(error)

    def _decode(self, raw: bytes, *, status_code: int) -> Mapping[str, object]:
        """Read one Slack response. `ok: false` 가 1차 기준이다 (H-2.2).

        Slack 은 application error 를 **HTTP 200 + `ok: false`** 로 준다. status code 만
        보고 성공을 판정하면 `invalid_auth` 가 성공으로 읽힌다.
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
        raw_error = decoded.get("error")
        raise SlackTransportError(
            f"Slack이 ok:false를 반환했습니다: {raw_error!r}",
            error_code=raw_error if isinstance(raw_error, str) else None,
            status_code=status_code,
        )

    def _fail_http(self, error: urllib.error.HTTPError) -> NoReturn:
        """Turn one HTTP-level failure into `SlackTransportError`.

        429 는 `Retry-After` 를 싣는다. **backoff 에 주입하지 않는다** — dispatcher 에
        지연을 넘길 인자가 없다 (research R-007). 진단으로만 남긴다.
        """
        body_error: str | None = None
        try:
            payload = json.loads(error.read().decode("utf-8"))
            if isinstance(payload, Mapping) and isinstance(payload.get("error"), str):
                body_error = str(payload["error"])
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

    @staticmethod
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
        return int(str(value).strip())
    except ValueError:
        return None


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

    `ts` 가 없는 message 는 **건너뛴다.** 예외를 던지면 그 뒤의 진짜 marker 를 못 읽고
    판정 불가로 떨어져 되돌릴 수 없는 hold 가 된다 — `read_slack_marker` 가 이상한
    message 하나에 예외를 안 던지는 것과 같은 이유다.
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
