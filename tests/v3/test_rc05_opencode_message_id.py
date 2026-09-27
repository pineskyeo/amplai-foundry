"""OpenCode orders messages by ID; the driver's user message must sort like a fresh one.

Measured on opencode 1.17.13 (scripts/opencode_qualify.py, Work 017): a client ``messageID``
that sorts after the server's time-ordered IDs (``msg_`` + 12 hex of ``ms * 0x1000 + counter``)
makes the server treat the user turn as still unanswered, so it generates assistant replies
without end (31 replies in 60 s before abort) or none at all. An ID in the server's ascending
form ends the turn with exactly one reply.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from amplai_foundry.agent_drivers.http import OpenCodeDriver
from amplai_foundry.agent_drivers.protocol import SessionJournal

ASCENDING = re.compile(r"msg_([0-9a-f]{12})[0-9A-Za-z]{14}")


def server_style_id(ms: int, counter: int = 0) -> str:
    return "msg_" + format((ms * 0x1000 + counter) & (2**48 - 1), "012x") + "Z" * 14


class Fixture:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "fixture-1"})
        if request.url.path == "/session" and request.method == "POST":
            return httpx.Response(200, json={"id": "ses_exact"})
        if request.url.path.endswith("/prompt_async"):
            self.sent.append(json.loads(request.content)["messageID"])
            return httpx.Response(204)
        raise AssertionError(str(request.url))


def driver(tmp_path: Path, fixture: Fixture) -> OpenCodeDriver:
    return OpenCodeDriver(
        "http://127.0.0.1:4096",
        "opencode",
        "test-only",
        SessionJournal(tmp_path / "s"),
        provider_id="local",
        model_id="pinned-model",
        qualified=True,
        transport=httpx.MockTransport(fixture),
        allow_local=True,
        expected_version="fixture-1",
        boundary_probe=lambda _: False,
    )


def test_start_sends_a_message_id_in_the_server_ascending_form(tmp_path: Path) -> None:
    # given: a server that orders messages by ID
    f = Fixture()
    d = driver(tmp_path, f)
    before = int(time.time() * 1000)
    # when: the driver starts a turn
    d.start({"dispatch_id": "dispatch-one"}, "task")
    after = int(time.time() * 1000)
    # expected: the ID sorts between server IDs minted just before and just after the send
    (sent,) = f.sent
    assert ASCENDING.fullmatch(sent), sent
    assert server_style_id(before) <= sent <= server_style_id(after, 0xFFF)
    assert d.journal.read("dispatch-one")["request_message_id"] == sent


def test_start_is_idempotent_on_the_persisted_message_id(tmp_path: Path) -> None:
    # given: a dispatch already started once
    f = Fixture()
    d = driver(tmp_path, f)
    d.start({"dispatch_id": "dispatch-one"}, "task")
    # when: the same dispatch is started again
    again = d.start({"dispatch_id": "dispatch-one"}, "task")
    # expected: no second prompt and the journal keeps the first ID
    assert len(f.sent) == 1
    assert again["request_message_id"] == f.sent[0]


def test_gzip_encoded_provider_response_is_decoded_once() -> None:
    """opencode 1.17.13 gzips larger JSON bodies (measured, Work 017)."""
    import gzip

    from amplai_foundry.agent_drivers.http import BoundHttp, response_json

    body = json.dumps([{"info": {"id": "msg_x"}, "parts": []}] * 50).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(body),
            headers={"content-encoding": "gzip", "content-type": "application/json"},
        )

    http = BoundHttp(
        "http://127.0.0.1:4096", transport=httpx.MockTransport(handler), allow_local=True
    )
    # when/expected: the bounded read decodes once and the JSON is intact
    assert len(response_json(http.request("GET", "session/ses_exact/message"))) == 50


def test_distinct_dispatches_get_distinct_ids(tmp_path: Path) -> None:
    f = Fixture()
    d = driver(tmp_path, f)
    d.start({"dispatch_id": "dispatch-one"}, "task")
    d.start({"dispatch_id": "dispatch-two"}, "task")
    assert len(set(f.sent)) == 2
