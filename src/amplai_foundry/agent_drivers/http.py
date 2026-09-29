"""Authenticated, bounded HTTP adapters. Provider acceptance never implies completion."""

from __future__ import annotations

import re
import string
import time
from collections.abc import Callable
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

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if (
            "://" in path
            or path.startswith("//")
            or ".." in path.split("/")
            or any(x in path for x in ("\\", "%", "?", "#"))
        ):
            raise RuntimeFault(
                "HTTP_PATH", "HTTP adapter path must stay on its configured endpoint"
            )
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
        loopback_exec: bool = False,
    ) -> None:
        # loopback_exec (D-087): the server listens only on its container's loopback and is
        # reached through an exec transport. A password would only be an env value the agent's
        # own tools inherit (the secret_isolation failure), so none is set there. Anywhere else
        # the server must authenticate.
        if not password and not loopback_exec:
            raise Hold("OPENCODE_AUTH", "Server authentication is required")
        if loopback_exec and transport is None:
            raise Hold(
                "OPENCODE_TRANSPORT",
                "A loopback-only server is reached only through its exec transport",
            )
        if not model_id or model_id in {"latest", "default", "auto"}:
            raise Hold("MODEL_UNPINNED", "Pinned model required")
        self.http = BoundHttp(
            base_url,
            auth=httpx.BasicAuth(username, password) if password else None,
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

    def probe(self) -> dict[str, Any]:
        result = response_json(self.http.request("GET", "global/health"))
        if self.expected_version and result.get("version") != self.expected_version:
            raise Hold("OPENCODE_VERSION", "Server version differs from the qualified profile")
        return {
            "health": result,
            "qualified": self.qualified and bool(self.expected_version),
            "transport": "http_poll",
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
        statuses = response_json(self.http.request("GET", "session/status"))
        if not isinstance(statuses, dict):
            raise Hold("OPENCODE_STATUS", "Malformed session status")
        current = statuses.get(session, {"type": "idle"})
        if current.get("type") != "idle":
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
        messages = response_json(self.http.request("GET", f"session/{session}/message"))
        if not isinstance(messages, list):
            raise Hold("OPENCODE_MESSAGES", "Expected bounded list of messages")
        assistants = []
        for message in messages:
            info = message.get("info", {})
            if info.get("sessionID") != session:
                raise Hold("SESSION_CORRELATION", "Cross-session response")
            if info.get("role") == "assistant" and info.get("parentID") == record.get(
                "request_message_id"
            ):
                assistants.append(info)
        if not assistants or not assistants[-1].get("time", {}).get("completed"):
            return {
                **record,
                "state": "running",
                "reason": "No completed assistant response for the exact user turn",
            }
        last = assistants[-1]
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
