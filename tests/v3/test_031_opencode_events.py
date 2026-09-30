"""OpenCodeDriver consumes the server's SSE stream (specs/031-opencode-driver D1), fixtures only.

Event shapes follow the pinned 1.17.13 server: ``{"id", "type", "properties"}`` in ``data``, no
SSE ``id:`` line, no replay after a reconnect (design.md Event Stream).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from amplai_foundry.agent_drivers.http import BoundHttp, OpenCodeDriver
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.errors import Hold

SESSION = "ses_exact"


def frame(event: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(event).encode() + b"\n\n"


class Server:
    def __init__(self) -> None:
        self.busy = True
        self.completed = False
        self.message: str | None = None
        self.streams: list[bytes] = []
        self.calls: list[str] = []

    def reply(self, *, completed: bool = True, session: str = SESSION) -> dict[str, Any]:
        done = {"completed": 1} if completed else {}
        return {"id": "msg_reply", "sessionID": session, "role": "assistant",
                "parentID": self.message, "time": done}  # fmt: skip

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/event":
            body = self.streams.pop(0) if self.streams else b""
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        if path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "1.17.13"})
        if path == "/session" and request.method == "POST":
            return httpx.Response(200, json={"id": SESSION})
        if path.endswith("/prompt_async"):
            self.message = json.loads(request.content)["messageID"]
            return httpx.Response(204)
        if path == "/session/status":
            return httpx.Response(200, json={SESSION: {"type": "busy" if self.busy else "idle"}})
        if path.endswith("/message"):
            infos = [self.reply()] if self.completed else []
            return httpx.Response(200, json=[{"info": i, "parts": []} for i in infos])
        if path.endswith("/abort"):
            return httpx.Response(200, json=True)
        raise AssertionError(path)


def driver(tmp_path: Path) -> tuple[OpenCodeDriver, Server, dict[str, bool]]:
    server = Server()
    stopped = {"value": False}
    d = OpenCodeDriver(
        "http://127.0.0.1:4096",
        "opencode",
        "fixture-password",
        SessionJournal(tmp_path / "journal"),
        provider_id="opencode-go",
        model_id="glm-5.3-flash",
        qualified=True,
        transport=httpx.MockTransport(server),
        allow_local=True,
        expected_version="1.17.13",
        boundary_probe=lambda _: stopped["value"],
        subscribe=True,
        event_thread=False,  # the test drives the stream
    )
    return d, server, stopped


def rest_reads(server: Server) -> int:
    return sum(1 for c in server.calls if c in {"/session/status", f"/session/{SESSION}/message"})


def test_204_is_acceptance_and_completion_comes_from_the_correlated_event(tmp_path: Path) -> None:
    d, server, stopped = driver(tmp_path)
    assert d.probe()["transport"] == "http_sse"
    result = d.start({"dispatch_id": "dispatch-one"}, "task")
    assert result["status"] == "accepted" and result["completed"] is False
    assert d.poll("dispatch-one")["state"] == "running"  # no stream yet: REST recovery
    d.events.fold({"id": "evt_1", "type": "server.connected", "properties": {}})
    reads = rest_reads(server)
    other = {**server.reply(), "parentID": "msg_someone_else"}
    d.events.fold({"id": "evt_2", "type": "message.updated", "properties": {"info": other}})
    d.events.fold({"id": "evt_3", "type": "message.updated",
                   "properties": {"info": server.reply(completed=False)}})  # fmt: skip
    assert d.poll("dispatch-one")["state"] == "running"
    reply = {"info": server.reply()}
    d.events.fold({"id": "evt_4", "type": "message.updated", "properties": reply})
    assert d.poll("dispatch-one")["state"] == "running"  # server still busy
    d.events.fold({"id": "evt_5", "type": "session.status",
                   "properties": {"sessionID": SESSION, "status": {"type": "idle"}}})  # fmt: skip
    polled = d.poll("dispatch-one")
    assert polled["state"] == "running" and polled["provider_completed"] is True
    stopped["value"] = True
    assert d.poll("dispatch-one")["state"] == "completed"
    assert rest_reads(server) == reads  # the connected stream answered; no REST polling
    assert d.collect("dispatch-one")["goal_verified"] is False


def test_a_reply_to_this_turn_from_another_session_holds(tmp_path: Path) -> None:
    d, server, _ = driver(tmp_path)
    d.start({"dispatch_id": "dispatch-one"}, "task")
    d.events.fold({"id": "evt_1", "type": "server.connected", "properties": {}})
    with pytest.raises(Hold) as held:
        d.events.fold({"id": "evt_2", "type": "message.updated",
                       "properties": {"info": server.reply(session="ses_other")}})  # fmt: skip
    assert held.value.code == "SESSION_CORRELATION"


def test_a_dropped_stream_is_reconciled_without_losing_or_duplicating(tmp_path: Path) -> None:
    d, server, stopped = driver(tmp_path)
    d.start({"dispatch_id": "dispatch-one"}, "task")
    busy = {"id": "evt_busy", "type": "session.status",
            "properties": {"sessionID": SESSION, "status": {"type": "busy"}}}  # fmt: skip
    server.streams.append(
        frame({"id": "evt_c1", "type": "server.connected", "properties": {}}) + frame(busy)
    )
    d.events.run_once()  # the stream drops after two events
    assert d.events.connected is False and d.events.connections == 1
    assert d.poll("dispatch-one")["state"] == "running"  # REST fallback during the gap
    # The turn finishes while nobody listens: these events are gone for good on this server.
    server.busy, server.completed = False, True
    reads = rest_reads(server)
    d.events.fold({"id": "evt_c2", "type": "server.connected", "properties": {}})
    assert rest_reads(server) > reads  # reconnect re-read the tracked session
    assert d.events.view(SESSION) == ("idle", server.reply())
    d.events.fold(busy)  # a replayed event is dropped by its ID
    d.events.fold({"id": "evt_late", "type": "message.updated",
                   "properties": {"info": {**server.reply(), "id": "msg_second"}}})  # fmt: skip
    assert d.events.view(SESSION) == ("idle", server.reply())  # one completion, the first
    stopped["value"] = True
    done = d.poll("dispatch-one")
    assert done["state"] == "completed" and done["last_message_id"] == "msg_reply"


def test_cancel_waits_for_the_idle_event_and_the_boundary(tmp_path: Path) -> None:
    d, _, stopped = driver(tmp_path)
    d.start({"dispatch_id": "dispatch-one"}, "task")
    d.events.fold({"id": "evt_1", "type": "server.connected", "properties": {}})
    assert d.cancel("dispatch-one")["state"] == "cancelling"  # abort ack is not a stop
    d.events.fold({"id": "evt_2", "type": "session.idle", "properties": {"sessionID": SESSION}})
    assert d.poll("dispatch-one")["state"] == "cancelling"
    stopped["value"] = True
    assert d.poll("dispatch-one")["state"] == "cancelled"


def sse_http(body: bytes) -> BoundHttp:
    return BoundHttp(
        "http://127.0.0.1:4096",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)),
        allow_local=True,
    )


def test_sse_frames_are_split_on_blank_lines_and_bounded() -> None:
    body = b': comment\ndata: {"a":\ndata: 1}\n\nevent: message\ndata: {"b": 2}\n\n'
    assert list(sse_http(body).sse("event")) == ['{"a":\n1}', '{"b": 2}']
    with pytest.raises(Hold) as held:
        list(sse_http(b"data: " + b"x" * 64 + b"\n\n").sse("event", line_limit=32))
    assert held.value.code == "HTTP_BODY_LIMIT"


def test_a_malformed_event_holds_and_marks_the_stream_down(tmp_path: Path) -> None:
    d, server, _ = driver(tmp_path)
    server.streams.append(b"data: {not json\n\n")
    with pytest.raises(Hold) as held:
        d.events.run_once()
    assert held.value.code == "PROVIDER_JSON" and d.events.connected is False
