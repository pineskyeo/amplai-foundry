"""Version-aware provider event normalization and durable exact-session journal."""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.contracts.identity import ID, canonical, digest, new_id, now
from amplai_foundry.runtime.contracts.registry import strict_json_loads
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault


class JsonlDecoder:
    def __init__(
        self, *, max_line_bytes: int = 1024 * 1024, max_total_bytes: int = 64 * 1024 * 1024
    ):
        self.buffer = b""
        self.total = 0
        self.max_line = max_line_bytes
        self.max_total = max_total_bytes

    def feed(self, data: bytes, *, final: bool = False) -> list[dict[str, Any]]:
        self.total += len(data)
        if self.total > self.max_total:
            raise Hold("PROVIDER_STREAM_LIMIT", "Provider output exceeded its byte budget")
        self.buffer += data
        output: list[dict[str, Any]] = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if len(line) > self.max_line:
                raise Hold("PROVIDER_LINE_LIMIT", "Provider JSONL line is too large")
            if not line.strip():
                continue
            try:
                value = strict_json_loads(line)
            except (ValueError, UnicodeDecodeError, RuntimeFault) as exc:
                raise Hold("PROVIDER_JSONL", "Malformed provider JSONL record") from exc
            if not isinstance(value, dict):
                raise Hold("PROVIDER_EVENT_SHAPE", "Provider event must be a JSON object")
            output.append(value)
        if len(self.buffer) > self.max_line:
            raise Hold("PROVIDER_LINE_LIMIT", "Provider JSONL partial line is too large")
        if final and self.buffer.strip():
            try:
                value = strict_json_loads(self.buffer)
            except (ValueError, UnicodeDecodeError, RuntimeFault) as exc:
                raise Hold(
                    "PROVIDER_TRUNCATED", "Provider ended with a truncated JSON event"
                ) from exc
            if not isinstance(value, dict):
                raise Hold("PROVIDER_EVENT_SHAPE", "Provider event must be a JSON object")
            output.append(value)
            self.buffer = b""
        return output


class EventNormalizer:
    CODEX = frozenset(
        {
            "thread.started",
            "turn.started",
            "turn.completed",
            "turn.failed",
            "item.started",
            "item.updated",
            "item.completed",
            "error",
        }
    )
    CLAUDE = frozenset(
        {"system", "assistant", "user", "result", "stream_event", "rate_limit_event"}
    )

    def __init__(self, provider: str, *, expected_session: str | None = None):
        if provider not in {"codex", "claude"}:
            raise RuntimeFault("PROVIDER_UNSUPPORTED", "Unknown stream dialect")
        self.provider, self.session, self.completed, self.failed = (
            provider,
            expected_session,
            False,
            False,
        )
        self.calls: dict[str, str] = {}
        self.usage: dict[str, int | str | None] = {
            "input_tokens": None,
            "output_tokens": None,
            "cost_microunits": None,
            "currency": "USD",
            "status": "unknown",
            "source_ref": None,
        }

    def accept(self, event: dict[str, Any]) -> dict[str, Any]:
        kind = event.get("type")
        allowed = self.CODEX if self.provider == "codex" else self.CLAUDE
        if kind not in allowed:
            raise Hold("UNKNOWN_PROVIDER_EVENT", "Unqualified provider event type: " + str(kind))
        session = event.get("thread_id") if self.provider == "codex" else event.get("session_id")
        if session is not None and (
            not isinstance(session, str)
            or not session
            or session.startswith("-")
            or session in {"latest", "continue"}
        ):
            raise Hold("PROVIDER_SESSION_INVALID", "Session must be an exact opaque identifier")
        if session:
            if self.session and session != self.session:
                raise Hold("PROVIDER_SESSION_MISMATCH", "Provider changed the bound session")
            self.session = session
        if self.provider == "codex":
            if kind == "turn.completed":
                self.completed = True
            if kind in {"turn.failed", "error"}:
                self.failed = True
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("id"):
                native = item["id"]
                self.calls.setdefault(
                    native, "call-" + digest({"session": self.session, "native": native})[7:31]
                )
        else:
            if kind == "result":
                self.failed = bool(event.get("is_error")) or event.get("subtype") not in {
                    "success",
                    None,
                }
                self.completed = not self.failed
            message = event.get("message", {})
            if isinstance(message, dict):
                for block in message.get("content", []):
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("id")
                    ):
                        native = block["id"]
                        self.calls.setdefault(
                            native,
                            "call-" + digest({"session": self.session, "native": native})[7:31],
                        )
        usage = event.get("usage")
        if isinstance(usage, dict) and all(
            type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens")
        ):
            self.usage = {
                **self.usage,
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "status": "measured",
            }
        # Do not retain private reasoning/tool payload text in the control-plane trace.
        return {
            "provider": self.provider,
            "type": kind,
            "session_handle": self.session,
            "event_digest": digest(event),
            "completed": self.completed,
            "failed": self.failed,
            "usage": self.usage.copy(),
            "call_mappings": self.calls.copy(),
        }


class SessionJournal:
    """Durable, process-safe journal. It is local state, not an authority source.

    One lock per dispatch is shared by independent journal instances/processes.
    External provider calls must never run under the journal lock. A persisted
    `starting` without a receipt requires reconciliation, not a second spawn.
    """

    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root:
            raise RuntimeFault("SESSION_ROOT", "Journal root must not traverse symlinks")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()

    def _path(self, handle: str) -> Path:
        if not isinstance(handle, str) or not ID.fullmatch(handle):
            raise RuntimeFault("SESSION_ID", "Journal session ID is malformed")
        path = self.root / (handle + ".json")
        if path.is_symlink():
            raise RuntimeFault("SESSION_SYMLINK", "Journal path cannot be a symlink")
        return path

    @contextlib.contextmanager
    def _guard(self, handle: str) -> Iterator[Path]:
        import fcntl

        path = self._path(handle)
        with self.lock:
            fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield path
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _read(self, path: Path) -> dict[str, Any]:
        from amplai_foundry.runtime.contracts.registry import strict_json_loads

        if not path.is_file():
            raise RuntimeFault("SESSION_NOT_FOUND", "Exact session journal not found")
        value: dict[str, Any] = strict_json_loads(path.read_bytes())
        return value

    def _write(self, path: Path, value: dict[str, Any]) -> None:
        temp = path.with_suffix(".tmp-" + new_id("write"))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(canonical(value))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temp.exists():
                temp.unlink()

    def create(self, dispatch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._guard(dispatch_id) as path:
            fp = digest(payload)
            if path.exists():
                value = self._read(path)
                if value["request_digest"] != fp:
                    raise Conflict("DISPATCH_PAYLOAD", "Existing dispatch has a different payload")
                return value
            value = {
                "dispatch_id": dispatch_id,
                "request_digest": fp,
                "state": "prepared",
                "created_at": now(),
                "updated_at": now(),
                "session_handle": None,
                "pid": None,
                "events": [],
                "failure": None,
                "row_version": 1,
                "cursor": 0,
            }
            self._write(path, value)
            return value

    def read(self, handle: str) -> dict[str, Any]:
        with self._guard(handle) as path:
            return self._read(path)

    def write(self, handle: str, value: dict[str, Any]) -> None:
        # Compatibility method for a trusted collector; mutation is still atomic.
        with self._guard(handle) as path:
            self._write(path, value)

    def update(self, handle: str, **changes: Any) -> dict[str, Any]:
        with self._guard(handle) as path:
            value = self._read(path)
            value.update(changes)
            value["updated_at"] = now()
            value["row_version"] = value.get("row_version", 0) + 1
            self._write(path, value)
            return value

    def transition(
        self,
        handle: str,
        states: set[str],
        new_state: str,
        *,
        expected_version: int | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        with self._guard(handle) as path:
            value = self._read(path)
            if value["state"] not in states or (
                expected_version is not None and value["row_version"] != expected_version
            ):
                raise Conflict(
                    "SESSION_STATE", "Session changed before this command; reconcile exact dispatch"
                )
            value.update(changes)
            value["state"] = new_state
            value["updated_at"] = now()
            value["row_version"] = value.get("row_version", 0) + 1
            self._write(path, value)
            return value

    def append(self, handle: str, event_id: str, event: dict[str, Any]) -> int:
        """Ordered sanitized observations with exact duplicate detection."""
        if len(canonical(event)) > 65536:
            raise Hold("SESSION_EVENT_LIMIT", "Normalized event exceeds limit")
        with self._guard(handle) as path:
            value = self._read(path)
            fp = digest(event)
            for old in value["events"]:
                if old.get("event_id") == event_id:
                    if old["digest"] != fp:
                        raise Conflict(
                            "SESSION_EVENT_CONFLICT", "Event ID binds another observation"
                        )
                    old_cursor: int = old["cursor"]
                    return old_cursor
            cursor: int = value.get("cursor", 0) + 1
            value["events"] = [
                *value["events"],
                {"event_id": event_id, "cursor": cursor, "digest": fp, "value": event},
            ][-4096:]
            value["cursor"] = cursor
            value["row_version"] = value.get("row_version", 0) + 1
            value["updated_at"] = now()
            self._write(path, value)
            return cursor

    def events_after(self, handle: str, cursor: int) -> list[dict[str, Any]]:
        value = self.read(handle)
        if type(cursor) is not int or cursor < 0 or cursor > value.get("cursor", 0):
            raise Hold("SESSION_CURSOR", "Cursor is not part of this session")
        if value["events"] and cursor < value["events"][0]["cursor"] - 1:
            raise Hold(
                "SESSION_CURSOR_EXPIRED", "Use a checkpoint; retained event window was exceeded"
            )
        return [e for e in value["events"] if e["cursor"] > cursor]
