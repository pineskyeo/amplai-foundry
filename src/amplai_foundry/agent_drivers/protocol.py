"""Version-aware provider event normalization and durable exact-session journal."""

from __future__ import annotations

import contextlib
import math
import os
import threading
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any, ClassVar

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
    # tool_progress: Claude Code 2.1.278 reports a foreground tool call still running after
    # about 30 s (measured in the app container 2026-09-28; the first real steer run died on it)
    CLAUDE = frozenset(
        {"system", "assistant", "user", "result", "stream_event", "rate_limit_event",
         "tool_progress"}
    )  # fmt: skip
    # Work 033 S4 (interfaces.md §4.1 row protocol.py, §8.6): rate-limit signals are kept as
    # "rate_limit" with allowlisted scalar fields only, never payload text. The field names of
    # Claude rate_limit_event payloads and the shape of Codex usage-limit errors are 확인 필요
    # (§14 Q5), so every allowlist is empty: the signal is counted, no field is kept.
    RATE_LIMIT_EVENTS: ClassVar[dict[str, frozenset[str]]] = {
        "claude": frozenset({"rate_limit_event"}),
        "codex": frozenset(),
    }
    RATE_LIMIT_FIELDS: ClassVar[dict[str, frozenset[str]]] = {
        "claude": frozenset(),
        "codex": frozenset(),
    }

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
        self.rate_limit_events = 0
        self.usage: dict[str, Any] = {
            "input_tokens": None,
            "output_tokens": None,
            "cost_microunits": None,
            "currency": "USD",
            "status": "unknown",
            "source_ref": None,
        }
        # D-094: the provider's own usage breakdown (cache and reasoning tokens) as it reported
        # it, with how its input count treats cache; the run record's usage stays the 3.0.0 shape
        self.usage_detail: dict[str, Any] | None = None

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
                "input_tokens": input_total(self.provider, usage),
                "output_tokens": usage["output_tokens"],
                "status": "measured",
            }
            self.usage_detail = usage_detail(self.provider, usage)
        total_cost_usd = event.get("total_cost_usd")
        if (
            self.provider == "claude"
            and kind == "result"
            and isinstance(total_cost_usd, (int, float))
            and not isinstance(total_cost_usd, bool)
            and (isinstance(total_cost_usd, int) or math.isfinite(total_cost_usd))
            and total_cost_usd >= 0
        ):
            scaled_cost = total_cost_usd * 1_000_000
            cost_microunits = (
                round(Fraction(*total_cost_usd.as_integer_ratio()) * 1_000_000)
                if isinstance(scaled_cost, float) and not math.isfinite(scaled_cost)
                else round(scaled_cost)
            )
            self.usage = {
                **self.usage,
                "cost_microunits": cost_microunits,
                "currency": "USD",
                "status": "estimated",
                "source_ref": {
                    "id": "claude-cli-total-cost-usd",
                    "revision": 1,
                    "digest": digest(event),
                },
            }
        digest_event = event
        if (
            self.provider == "claude"
            and kind == "result"
            and isinstance(total_cost_usd, float)
            and not math.isfinite(total_cost_usd)
        ):
            digest_event = {key: value for key, value in event.items() if key != "total_cost_usd"}
        # Do not retain private reasoning/tool payload text in the control-plane trace.
        normalized = {
            "provider": self.provider,
            "type": kind,
            "session_handle": self.session,
            "event_digest": digest(digest_event),
            "completed": self.completed,
            "failed": self.failed,
            "usage": self.usage.copy(),
            "call_mappings": self.calls.copy(),
        }
        if kind in self.RATE_LIMIT_EVENTS[self.provider]:
            self.rate_limit_events += 1
            normalized["rate_limit"] = self.rate_limit(event)
        return normalized

    def rate_limit(self, event: dict[str, Any]) -> dict[str, Any]:
        """Allowlisted top-level scalar fields (numbers, booleans, null): never text (§14 Q5)."""
        fields = self.RATE_LIMIT_FIELDS[self.provider]
        return {
            k: v
            for k, v in event.items()
            if k in fields
            and (v is None or isinstance(v, bool) or type(v) is int or (
                type(v) is float and math.isfinite(v)))
        }  # fmt: skip


# D-094: the breakdown fields each provider reports, and whether its input count includes cache.
# Codex (OpenAI): cached_input_tokens is a subset of input_tokens; turn.completed is the thread's
# cumulative total, so the last value is the turn's total, never a sum. Anthropic: input_tokens
# excludes cache reads and cache writes.
USAGE_DETAIL_FIELDS = {
    "codex": (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ),
    "claude": (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
    ),
}
INPUT_INCLUDES_CACHE = {"codex": True, "claude": False}
# The cache classes Anthropic reports beside (not inside) ``input_tokens``.
CLAUDE_CACHE_INPUT = ("cache_read_input_tokens", "cache_creation_input_tokens")


def input_total(provider: str, usage: dict[str, Any]) -> int:
    """Every input token the turn sent, cache included: the run's 3.0.0 ``usage.input_tokens``.

    Budgets, trial tokens and quota windows count ``input_tokens + output_tokens``, so the input
    count means the same for every provider. Codex's ``input_tokens`` already contains its cached
    reads and cache writes; Anthropic's excludes them, so its cache reads and cache writes are
    added (a field it did not report counts 0). The provider's own fields stay in the usage
    detail (D-094), which pricing splits into exclusive buckets, so nothing is counted twice.
    Operator decision IC-34 (A), 2026-10-08 (interfaces.md).
    """
    total: int = usage["input_tokens"]
    if provider == "claude":
        total += sum(
            usage[k] for k in CLAUDE_CACHE_INPUT if type(usage.get(k)) is int and usage[k] >= 0
        )
    return total


# Anthropic splits ``cache_creation_input_tokens`` by cache lifetime under ``cache_creation``.
CLAUDE_CACHE_WRITE_SPLIT = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


def turn_detail(provider: str, usage: dict[str, Any]) -> dict[str, Any] | None:
    """The D-094 usage detail of a read-only turn (planner, auxiliary, judge) from the counts
    it reports (``readonly_turn.TurnResult.usage``), so pricing splits it into exclusive buckets
    like a run's detail. A Claude turn reports ``input_tokens`` as the total (IC-34 (A)) and its
    cache classes beside it, so the provider's own uncached ``input_tokens`` is the total minus
    both cache classes. None when the turn did not report its cache breakdown (a Claude turn
    recorded before IC-34 kept input and output only): it is then priced as an upper bound."""
    counts = (usage.get("input_tokens"), usage.get("output_tokens"))
    if any(type(c) is not int or c < 0 for c in counts):
        return None
    if provider == "codex":
        if type(usage.get("cached_input_tokens")) is not int:
            return None
        return usage_detail("codex", usage)
    if provider != "claude":
        return None
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in CLAUDE_CACHE_INPUT):
        return None
    fields: dict[str, int] = {k: usage[k] for k in CLAUDE_CACHE_INPUT}
    fields["input_tokens"] = usage["input_tokens"] - sum(fields.values())
    if fields["input_tokens"] < 0:
        return None
    fields["output_tokens"] = usage["output_tokens"]
    for k in CLAUDE_CACHE_WRITE_SPLIT:
        if type(usage.get(k)) is int and usage[k] >= 0:
            fields[k] = usage[k]
    return {"provider": "claude", "input_includes_cache": False, "fields": fields}


def usage_detail(provider: str, usage: dict[str, Any]) -> dict[str, Any]:
    """The reported breakdown: nonnegative integer fields only, absent ones left out."""
    fields = {
        k: usage[k]
        for k in USAGE_DETAIL_FIELDS[provider]
        if type(usage.get(k)) is int and usage[k] >= 0
    }
    creation = usage.get("cache_creation")
    if provider == "claude" and isinstance(creation, dict):
        for k in CLAUDE_CACHE_WRITE_SPLIT:
            if type(creation.get(k)) is int and creation[k] >= 0:
                fields[k] = creation[k]
    return {
        "provider": provider,
        "input_includes_cache": INPUT_INCLUDES_CACHE[provider],
        "fields": fields,
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
