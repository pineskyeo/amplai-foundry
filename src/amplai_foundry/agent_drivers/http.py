"""Authenticated, bounded HTTP adapters. Provider acceptance never implies completion."""

from __future__ import annotations

import contextlib
import re
import string
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.contracts.registry import strict_json_loads
from amplai_foundry.runtime.errors import Hold, RuntimeFault

from .protocol import SessionJournal


def ascending_message_id(dispatch_id: str) -> str:
    """A user message ID in OpenCode's ascending form: 12 hex of ``ms * 0x1000`` + 14 chars.

    The server orders a session's messages by ID. An ID that sorts after the replies it mints
    leaves the user turn looking unanswered, and the server keeps generating replies (measured
    on 1.17.13, Work 017). The ID is persisted in the journal before the send, so a replayed
    dispatch reuses it instead of minting another. The ordering assumes the server clock is
    not behind this host; only a host-local server is qualified (a remote server is not).
    """
    ms = time.time_ns() // 1_000_000
    tail = digest({"dispatch": dispatch_id, "ns": str(time.time_ns())})[7:]
    alphabet = string.digits + string.ascii_letters
    value = int(tail, 16)
    chars = []
    for _ in range(14):
        value, index = divmod(value, len(alphabet))
        chars.append(alphabet[index])
    return "msg_" + format((ms * 0x1000) & (2**48 - 1), "012x") + "".join(chars)


def response_json(response: httpx.Response) -> Any:
    try:
        return strict_json_loads(response.content)
    except (ValueError, RuntimeFault) as exc:
        raise Hold("PROVIDER_JSON", "Provider response is not strict JSON") from exc


def native_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value)
        or value in {"latest", "continue"}
    ):
        raise Hold("NATIVE_ID", "Provider must return an exact path-safe native ID")
    return value


class BoundHttp:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        auth: httpx.Auth | None = None,
        transport: httpx.BaseTransport | None = None,
        allow_local: bool = False,
        timeout: float = 30,
    ) -> None:
        url = urlsplit(base_url)
        local = url.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            not url.hostname
            or url.query
            or url.username
            or url.password
            or url.fragment
            or (url.scheme != "https" and not (allow_local and local and url.scheme == "http"))
        ):
            raise RuntimeFault(
                "HTTP_ENDPOINT",
                "Use a trusted HTTPS endpoint, or explicitly configured loopback testing endpoint",
            )
        self.client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            auth=auth,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    @staticmethod
    def _check_path(path: str) -> None:
        if (
            "://" in path
            or path.startswith("//")
            or ".." in path.split("/")
            or any(x in path for x in ("\\", "%", "?", "#"))
        ):
            raise RuntimeFault(
                "HTTP_PATH", "HTTP adapter path must stay on its configured endpoint"
            )

    def sse(self, path: str, *, line_limit: int = 1024 * 1024) -> Iterator[str]:
        """The ``data`` of each server-sent event on ``path`` until the stream ends.

        A dropped stream ends the iteration; the caller reconciles what it may have missed.
        """
        self._check_path(path)
        try:
            with self.client.stream(
                "GET", path.lstrip("/"), headers={"Accept": "text/event-stream"}
            ) as stream:
                if stream.status_code >= 300:
                    raise Hold(
                        "PROVIDER_HTTP",
                        "Provider returned a non-success status",
                        details={"status_code": stream.status_code},
                    )
                data: list[str] = []
                for line in stream.iter_lines():
                    if len(line) > line_limit:
                        raise Hold("HTTP_BODY_LIMIT", "Server-sent event exceeded byte budget")
                    if line == "":
                        if data:
                            yield "\n".join(data)
                        data = []
                    elif line.startswith("data:"):
                        data.append(line[5:].removeprefix(" "))
        except httpx.HTTPError:
            return

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self._check_path(path)
        try:
            with self.client.stream(method, path.lstrip("/"), **kwargs) as stream:
                chunks = []
                size = 0
                for chunk in stream.iter_bytes():
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise Hold("HTTP_BODY_LIMIT", "Provider response exceeded byte budget")
                    chunks.append(chunk)
                # iter_bytes already decoded the body (the budget counts decoded bytes), so the
                # rebuilt response must not carry the encoding headers or it decodes twice.
                headers = [
                    (k, v)
                    for k, v in stream.headers.multi_items()
                    if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
                ]
                response = httpx.Response(
                    stream.status_code,
                    headers=headers,
                    content=b"".join(chunks),
                    request=stream.request,
                )
        except httpx.HTTPError as exc:
            raise Hold(
                "PROVIDER_UNREACHABLE", "Provider request outcome could not be established"
            ) from exc
        if response.status_code >= 300:
            raise Hold(
                "PROVIDER_HTTP",
                "Provider returned a non-success status",
                details={"status_code": response.status_code},
            )
        return response

    def close(self) -> None:
        self.client.close()


class OpenCodeEvents:
    """The server's ``/event`` SSE stream folded into per-session turn state (design 11:36).

    Read from the pinned 1.17.13 binary (specs/031-opencode-driver/design.md, Event Stream):
    each event is ``{"id", "type", "properties"}`` in the SSE ``data`` field, the frame has no
    ``id:`` line and the server keeps no replay buffer, so a reconnect cannot resume from a
    cursor. Recovery is therefore: on every ``server.connected`` the tracked sessions are re-read
    over REST (``reconcile``) and later events fold on top. Event IDs are deduplicated and a
    completion is kept once per session, so a replayed or reconciled completion never doubles.
    """

    def __init__(
        self,
        http: BoundHttp,
        reconcile: Callable[[str, str | None], tuple[str, dict[str, Any] | None]],
        *,
        path: str = "event",
    ) -> None:
        self.http, self.reconcile, self.path = http, reconcile, path
        self._lock = threading.Lock()
        self._tracked: dict[str, str] = {}  # session -> request message id
        self._status: dict[str, str] = {}
        self._completed: dict[str, dict[str, Any]] = {}  # session -> assistant info
        self._seen: set[str] = set()
        self._order: deque[str] = deque()
        self._synced: set[str] = set()  # sessions reconciled on the current connection
        self.connections = 0
        self.connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def track(self, session: str, request_message_id: str) -> None:
        with self._lock:
            self._tracked[session] = request_message_id
            self._completed.pop(session, None)
            self._synced.discard(session)
            if self.connected:
                # tracked on a live connection before its prompt is sent: every event of the
                # turn arrives on this connection; idle only when the server says so
                self._status[session] = "busy"
                self._synced.add(session)

    def view(self, session: str) -> tuple[str, dict[str, Any] | None] | None:
        """(status type, completed assistant info) from events, or None if not yet known."""
        with self._lock:
            if not self.connected or session not in self._synced:
                return None  # dropped or not yet reconciled: the caller reads over REST
            return self._status.get(session, "idle"), self._completed.get(session)

    def _fresh(self, event_id: object) -> bool:
        if not isinstance(event_id, str) or not event_id:
            return True  # nothing to deduplicate on; folding is idempotent per session
        with self._lock:
            if event_id in self._seen:
                return False
            self._seen.add(event_id)
            self._order.append(event_id)
            if len(self._order) > 4096:
                self._seen.discard(self._order.popleft())
            return True

    def _connected(self) -> None:
        with self._lock:
            self._synced.clear()
            tracked = dict(self._tracked)
        for session, request in tracked.items():
            status, completed = self.reconcile(session, request)
            with self._lock:
                self._status[session] = status
                if completed is not None:
                    self._completed.setdefault(session, completed)
                self._synced.add(session)
        self.connections += 1
        self.connected = True

    def fold(self, event: dict[str, Any]) -> None:
        kind, props = event.get("type"), event.get("properties") or {}
        if not isinstance(props, dict) or not self._fresh(event.get("id")):
            return
        if kind == "server.connected":
            self._connected()
            return
        session = props.get("sessionID")
        if kind in {"session.status", "session.idle"}:
            status = "idle" if kind == "session.idle" else (props.get("status") or {}).get("type")
            with self._lock:
                if session in self._tracked and isinstance(status, str):
                    self._status[session] = status
            return
        if kind == "message.updated":
            info = props.get("info") or {}
            with self._lock:
                owner = [s for s, r in self._tracked.items() if r == info.get("parentID")]
            if not owner or info.get("role") != "assistant":
                return
            if info.get("sessionID") != owner[0]:
                raise Hold("SESSION_CORRELATION", "Cross-session response")
            if (info.get("time") or {}).get("completed"):
                with self._lock:
                    self._completed.setdefault(owner[0], info)

    def run_once(self) -> None:
        """Consume one connection until it ends; the next ``server.connected`` reconciles."""
        try:
            for data in self.http.sse(self.path):
                try:
                    event = strict_json_loads(data)
                except (ValueError, RuntimeFault) as exc:
                    raise Hold("PROVIDER_JSON", "Server-sent event is not strict JSON") from exc
                if isinstance(event, dict):
                    self.fold(event)
        finally:
            self.connected = False

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            delay = 0.5
            while not self._stop.is_set():
                before = self.connections
                # a broken stream is recovered by the next connection's reconcile
                with contextlib.suppress(Exception):
                    self.run_once()
                delay = 0.5 if self.connections > before else min(delay * 2, 5.0)
                self._stop.wait(delay)

        self._thread = threading.Thread(target=loop, name="opencode-events", daemon=True)
        self._thread.start()

    def wait_connected(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.connected

    def stop(self) -> None:
        self._stop.set()


class OpenCodeDriver:
    """One qualified isolated server workspace, explicit asynchronous turn correlation.

    HTTP acceptance/abort only acknowledge receipt. Neither establishes stopped
    processes or safe output collection; those require the server boundary probe.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        journal: SessionJournal,
        *,
        provider_id: str,
        model_id: str,
        qualified: bool = False,
        transport: httpx.BaseTransport | None = None,
        allow_local: bool = False,
        expected_version: str | None = None,
        boundary_probe: Callable[[str], bool] | None = None,
        subscribe: bool = False,
        event_thread: bool = True,
    ) -> None:
        if not password:
            raise Hold("OPENCODE_AUTH", "Server authentication is required")
        if not model_id or model_id in {"latest", "default", "auto"}:
            raise Hold("MODEL_UNPINNED", "Pinned model required")
        self.http = BoundHttp(
            base_url,
            auth=httpx.BasicAuth(username, password),
            transport=transport,
            allow_local=allow_local,
        )
        self.journal, self.provider_id, self.model_id, self.qualified = (
            journal,
            provider_id,
            model_id,
            qualified,
        )
        self.expected_version, self.boundary_probe = expected_version, boundary_probe
        # D1 (operator 2026-09-29): consume the server's SSE stream; REST reads are recovery
        self.events = OpenCodeEvents(self.http, self._rest_turn) if subscribe else None
        self.event_thread = event_thread

    def probe(self) -> dict[str, Any]:
        result = response_json(self.http.request("GET", "global/health"))
        if self.expected_version and result.get("version") != self.expected_version:
            raise Hold("OPENCODE_VERSION", "Server version differs from the qualified profile")
        return {
            "health": result,
            "qualified": self.qualified and bool(self.expected_version),
            "transport": "http_sse" if self.events is not None else "http_poll",
            "native_steering": False,
            "native_delegation": False,
        }

    def prepare(self, dispatch: dict[str, Any], prompt: str) -> dict[str, Any]:
        if not self.qualified or not self.expected_version:
            raise Hold(
                "DRIVER_UNQUALIFIED", "Exact server version and boundary qualification required"
            )
        if not isinstance(prompt, str) or len(prompt.encode()) > 1024 * 1024:
            raise Hold("PROMPT_LIMIT", "Prompt is too large")
        return self.journal.create(
            dispatch["dispatch_id"],
            {
                "dispatch_digest": digest(dispatch),
                "prompt_digest": digest(prompt),
                "provider": self.provider_id,
                "model": self.model_id,
                "server_version": self.expected_version,
            },
        )

    def start(self, dispatch: dict[str, Any], prompt: str) -> dict[str, Any]:
        record = self.prepare(dispatch, prompt)
        did = dispatch["dispatch_id"]
        if record["state"] != "prepared":
            return record  # Never replay an unknown creation or prompt.
        health = self.probe()["health"]
        if health.get("healthy") is not True:
            raise Hold("OPENCODE_HEALTH", "Server health is not established")
        self._subscribe()  # listen before the prompt so the turn's events are not missed
        self.journal.transition(
            did, {"prepared"}, "starting", expected_version=record["row_version"]
        )
        try:
            created = response_json(
                self.http.request("POST", "session", json={"title": "AMPLAI " + did})
            )
            session = native_id(created.get("id"))
            message = ascending_message_id(did)
            self.journal.update(
                did, session_handle=session, request_message_id=message, process_stopped=False
            )
            if self.events is not None:
                self.events.track(session, message)
            response = self.http.request(
                "POST",
                f"session/{session}/prompt_async",
                json={
                    "messageID": message,
                    "model": {"providerID": self.provider_id, "modelID": self.model_id},
                    "parts": [{"type": "text", "text": prompt}],
                },
            )
            if response.status_code not in {200, 202, 204}:
                raise Hold("OPENCODE_ACCEPTANCE", "Unexpected asynchronous response")
            self.journal.transition(did, {"starting"}, "running")
        except Exception as exc:
            self.journal.update(
                did, state="unknown", failure=getattr(exc, "code", type(exc).__name__)
            )
            raise
        return {
            "dispatch_id": did,
            "session_handle": session,
            "state": "running",
            "status": "accepted",
            "completed": False,
        }

    def _subscribe(self) -> None:
        if self.events is not None and self.event_thread:
            self.events.start()
            self.events.wait_connected(10)  # not connected yet: REST recovery covers the gap

    def _rest_turn(
        self, session: str, request_message_id: str | None
    ) -> tuple[str, dict[str, Any] | None]:
        """(status type, completed assistant reply to the exact user turn) read over REST."""
        statuses = response_json(self.http.request("GET", "session/status"))
        if not isinstance(statuses, dict):
            raise Hold("OPENCODE_STATUS", "Malformed session status")
        current = statuses.get(session, {"type": "idle"})
        status = current.get("type") if isinstance(current, dict) else None
        if not isinstance(status, str):
            raise Hold("OPENCODE_STATUS", "Malformed session status")
        if status != "idle":
            return status, None
        messages = response_json(self.http.request("GET", f"session/{session}/message"))
        if not isinstance(messages, list):
            raise Hold("OPENCODE_MESSAGES", "Expected bounded list of messages")
        assistants = []
        for message in messages:
            info = message.get("info", {})
            if info.get("sessionID") != session:
                raise Hold("SESSION_CORRELATION", "Cross-session response")
            if info.get("role") == "assistant" and info.get("parentID") == request_message_id:
                assistants.append(info)
        if not assistants or not assistants[-1].get("time", {}).get("completed"):
            return status, None
        return status, assistants[-1]

    def close(self) -> None:
        if self.events is not None:
            self.events.stop()

    def _boundary(self, session: str) -> bool:
        if self.boundary_probe is None:
            return False
        return self.boundary_probe(session) is True

    def poll(self, handle: str) -> dict[str, Any]:
        record = self.journal.read(handle)
        if (
            record["state"] in {"cancelled", "paused", "completed", "failed"}
            and record.get("process_stopped") is True
        ):
            return record
        session = native_id(record["session_handle"])
        seen = self.events.view(session) if self.events is not None else None
        # Until the stream has reconciled this session, the REST read is the recovery path.
        status, last = seen or self._rest_turn(session, record.get("request_message_id"))
        if status != "idle":
            return {
                **record,
                "state": "cancelling" if record["state"] == "cancelling" else "running",
            }
        if record["state"] == "cancelling":
            if not self._boundary(session):
                return {**record, "state": "cancelling", "process_stopped": False}
            return self.journal.transition(
                handle, {"cancelling"}, "cancelled", process_stopped=True
            )
        if last is None:
            return {
                **record,
                "state": "running",
                "reason": "No completed assistant response for the exact user turn",
            }
        stopped = self._boundary(session)
        if not stopped:
            return {
                **record,
                "state": "running",
                "provider_completed": True,
                "process_stopped": False,
            }
        return self.journal.update(
            handle,
            state="failed" if last.get("error") else "completed",
            process_stopped=True,
            last_message_id=native_id(last.get("id")),
            goal_verified=False,
        )

    def cancel(self, handle: str) -> dict[str, Any]:
        record = self.journal.read(handle)
        if record["state"] in {"cancelled", "paused"}:
            return record
        session = native_id(record["session_handle"])
        if record["state"] != "cancelling":
            self.journal.update(handle, state="cancelling", process_stopped=False)
            value = response_json(self.http.request("POST", f"session/{session}/abort"))
            if value is not True:
                raise Hold("CANCEL_UNCONFIRMED", "Server did not acknowledge abort")
            self.journal.update(handle, cancel_ack=True)
        # A true /abort response is NOT a stopped process receipt.
        return self.poll(handle)

    def pause(self, handle: str) -> dict[str, Any]:
        result = self.cancel(handle)
        if result["state"] == "cancelled":
            return self.journal.transition(handle, {"cancelled"}, "paused")
        return result

    def steer(self, handle: str, event: dict[str, Any]) -> dict[str, Any]:
        self.journal.read(handle)
        return {"status": "checkpoint_required", "native_applied": False}

    def checkpoint(self, handle: str) -> dict[str, Any]:
        state = self.poll(handle)
        if (
            state["state"] not in {"completed", "cancelled", "paused", "failed"}
            or state.get("process_stopped") is not True
        ):
            raise Hold(
                "CHECKPOINT_UNCONFIRMED", "Exact session has not reached a verified boundary"
            )
        return {
            "dispatch_id": handle,
            "session_handle": state["session_handle"],
            "journal_digest": digest(self.journal.read(handle)),
            "server_version": self.expected_version,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "request_message_id": state["request_message_id"],
        }

    def resume(
        self, dispatch: dict[str, Any], prompt: str, checkpoint: dict[str, Any]
    ) -> dict[str, Any]:
        prior = self.journal.read(checkpoint["dispatch_id"])
        if (
            digest(prior) != checkpoint["journal_digest"]
            or prior["session_handle"] != checkpoint["session_handle"]
        ):
            raise Hold("CHECKPOINT_RECEIPT", "Native checkpoint differs from durable journal")
        if (
            checkpoint["server_version"] != self.expected_version
            or checkpoint["model_id"] != self.model_id
            or checkpoint["provider_id"] != self.provider_id
        ):
            raise Hold("RESUME_PROFILE", "Native state cannot move to another profile")
        session = native_id(checkpoint["session_handle"])
        if not self._boundary(session):
            raise Hold("RESUME_UNCONFIRMED", "Old native process is not stopped")
        health = self.probe()["health"]
        if not self.qualified or health.get("healthy") is not True:
            raise Hold("DRIVER_UNQUALIFIED", "Server not qualified")
        did = dispatch["dispatch_id"]
        record = self.journal.create(
            did,
            {
                "dispatch_digest": digest(dispatch),
                "prompt_digest": digest(prompt),
                "checkpoint": checkpoint,
                "version": self.expected_version,
                "model": self.model_id,
            },
        )
        if record["state"] != "prepared":
            return record
        self.journal.transition(
            did, {"prepared"}, "starting", expected_version=record["row_version"]
        )
        message = ascending_message_id(did)
        self.journal.update(
            did, session_handle=session, request_message_id=message, process_stopped=False
        )
        self._subscribe()
        if self.events is not None:
            self.events.track(session, message)
        try:
            response = self.http.request(
                "POST",
                f"session/{session}/prompt_async",
                json={
                    "messageID": message,
                    "model": {"providerID": self.provider_id, "modelID": self.model_id},
                    "parts": [{"type": "text", "text": prompt}],
                },
            )
            if response.status_code not in {200, 202, 204}:
                raise Hold("RESUME_ACCEPTANCE", "Unexpected native resume response")
            return self.journal.transition(did, {"starting"}, "running")
        except Exception as exc:
            self.journal.update(
                did, state="unknown", failure=getattr(exc, "code", type(exc).__name__)
            )
            raise

    def collect(self, handle: str) -> dict[str, Any]:
        state = self.poll(handle)
        if state["state"] != "completed" or state.get("process_stopped") is not True:
            raise Hold("DRIVER_NOT_COMPLETE", "Not a confirmed completed turn")
        return {
            "session_handle": state["session_handle"],
            "process_stopped": True,
            "provider_completed": True,
            "goal_verified": False,
            "usage": None,
        }

    def destroy(self, handle: str) -> None:
        record = self.journal.read(handle)
        if record.get("process_stopped") is not True:
            raise Hold("DESTROY_RUNNING", "Stop and reconcile before destruction")
        # Retain server session history until a separately authorized retention job.
        self.close()
        self.http.close()


class ResponsesDriver:
    def __init__(
        self,
        api_key: str,
        journal: SessionJournal,
        *,
        model: str,
        base_url: str = "https://api.openai.com/v1/",
        qualified: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise Hold("RESPONSES_AUTH", "Server-side OpenAI credential is required")
        if not model or model in {"latest", "auto", "default"}:
            raise Hold("MODEL_UNPINNED", "Exact configured model profile required")
        self.http = BoundHttp(
            base_url, headers={"Authorization": "Bearer " + api_key}, transport=transport
        )
        self.journal, self.model, self.qualified = journal, model, qualified

    def start(
        self,
        dispatch: dict[str, Any],
        prompt: str,
        *,
        max_output_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.qualified:
            raise Hold("DRIVER_UNQUALIFIED", "Responses adapter requires deployment qualification")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 100000:
            raise RuntimeFault("OUTPUT_TOKEN_BUDGET", "Explicit bounded output tokens required")
        did = dispatch["dispatch_id"]
        record = self.journal.create(
            did,
            {
                "dispatch_digest": digest(dispatch),
                "prompt_digest": digest(prompt),
                "model": self.model,
                "max_output_tokens": max_output_tokens,
                "tools": tools or [],
            },
        )
        if record["state"] != "prepared":
            return record
        self.journal.transition(
            did, {"prepared"}, "running", expected_version=record["row_version"]
        )
        result = response_json(
            self.http.request(
                "POST",
                "responses",
                json={
                    "model": self.model,
                    "input": prompt,
                    "max_output_tokens": max_output_tokens,
                    "store": False,
                    "parallel_tool_calls": False,
                    "tools": tools or [],
                },
            )
        )
        response_id = result.get("id")
        if not response_id:
            raise Hold("RESPONSE_CORRELATION", "Provider did not return a response identifier")
        response_id = native_id(response_id)
        pending: list[dict[str, Any]] = []
        text: list[str] = []
        call_ids: set[str] = set()
        for item in result.get("output", []):
            if item.get("type") == "function_call":
                if not item.get("call_id") or item["call_id"] in call_ids:
                    raise Hold("CALL_CORRELATION", "Tool call lacks a unique call_id")
                call_ids.add(native_id(item["call_id"]))
                pending.append(
                    {
                        "provider_call_id": item["call_id"],
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                    }
                )
            if item.get("type") == "message":
                text.extend(
                    p["text"]
                    for p in item.get("content", [])
                    if p.get("type") == "output_text" and isinstance(p.get("text"), str)
                )
        state = (
            "awaiting_tools"
            if pending
            else "completed"
            if result.get("status") == "completed"
            else "failed"
        )
        self.journal.update(
            did,
            state=state,
            session_handle=response_id,
            pending_calls=pending,
            usage=result.get("usage"),
            output_text="\n".join(text),
        )
        return {
            "dispatch_id": did,
            "session_handle": response_id,
            "state": state,
            "pending_calls": pending,
            "goal_verified": False,
        }

    def poll(self, handle: str) -> dict[str, Any]:
        return self.journal.read(handle)

    def tool_outputs(self, handle: str, outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        record = self.journal.read(handle)
        expected = {c["provider_call_id"] for c in record.get("pending_calls", [])}
        supplied = [o["provider_call_id"] for o in outputs]
        if len(supplied) != len(set(supplied)) or set(supplied) != expected:
            raise Hold(
                "TOOL_CALL_MAPPING", "Tool outputs must match each native call_id exactly once"
            )
        # Returned input must be sent as a new budget-admitted turn with the complete
        # explicitly retained conversation because store=false is the privacy default.
        return [
            {
                "type": "function_call_output",
                "call_id": o["provider_call_id"],
                "output": o["output"],
            }
            for o in outputs
        ]

    def structured_plan(
        self, prompt: str, schema: dict[str, Any], *, max_output_tokens: int = 8192
    ) -> dict[str, Any]:
        if not self.qualified:
            raise Hold("PLANNER_UNQUALIFIED", "Planning model is not qualified")
        result = response_json(
            self.http.request(
                "POST",
                "responses",
                json={
                    "model": self.model,
                    "input": prompt,
                    "max_output_tokens": max_output_tokens,
                    "store": False,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "amplai_plan",
                            "strict": True,
                            "schema": schema,
                        }
                    },
                },
            )
        )
        if result.get("status") != "completed":
            raise Hold("PLANNER_INCOMPLETE", "Provider did not complete the structured plan")
        texts = [
            part["text"]
            for item in result.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ]
        try:
            value: dict[str, Any] = strict_json_loads("".join(texts))
        except ValueError as exc:
            raise Hold("PLANNER_JSON", "Planning response is not valid structured JSON") from exc
        from jsonschema import Draft202012Validator

        if list(Draft202012Validator(schema).iter_errors(value)):
            raise Hold("PLANNER_SCHEMA", "Planning output does not match its contract")
        return value


class ExperimentalDriver:
    """Managed API / Codex app-server extension boundary: disabled until qualified.

    A transport implementation can be injected by an installed signed capability pack.
    No guessed vendor endpoint or fake successful implementation is provided.
    """

    def __init__(
        self, name: str, implementation: Any = None, qualification_ref: dict[str, Any] | None = None
    ) -> None:
        self.name, self.implementation, self.qualification_ref = (
            name,
            implementation,
            qualification_ref,
        )

    def probe(self) -> dict[str, Any]:
        return {
            "driver_id": self.name,
            "maturity": "experimental",
            "enabled": self.implementation is not None and self.qualification_ref is not None,
        }

    def start(self, *args: Any, **kwargs: Any) -> Any:
        if not self.implementation or not self.qualification_ref:
            raise Hold(
                "EXPERIMENTAL_DISABLED",
                "This optional transport has not been implemented and qualified "
                "by an installed adapter pack",
            )
        return self.implementation.start(*args, **kwargs)
