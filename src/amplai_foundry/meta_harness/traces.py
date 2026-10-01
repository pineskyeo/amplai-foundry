"""Traces of meta-harness trials (Work 033 S13, D-100, interfaces.md §2.11, §3.12, §9.1-§9.3).

Only trial goals of the corpus capture: a dispatch captures when its
``DispatchOptions.capture_trace`` is true, which the loop sets from the plan's ``TrialContext``
(a real goal never captures). The CLI
driver feeds every raw provider event to a bounded ``TraceBuffer`` before ``EventNormalizer``
(``agent_drivers/cli.py`` ``_collect``); the worker hands the turns of one run to a trace sink after
collect, and ``TraceService.admit`` stores what passes as a ``harness-trace`` record with a
``restricted`` CAS artifact, or writes a ``trace-drop`` record and stores nothing.

Sanitizer ``trace-sanitizer-v1`` is default-deny (§9.2):

- Codex: ``item.completed`` items of type ``agent_message`` (text). Reasoning items and every other
  event or item type are dropped and counted; the command and file-change item names are 확인 필요
  (§14 Q14), so they are dropped too.
- Claude Code: ``assistant`` content blocks ``text`` (text) and ``tool_use`` (kept as a bare marker:
  only ``type``/``id`` are read in code, ``protocol.py:140-147``); the ``result`` event's
  ``subtype``, ``is_error`` and ``num_turns``. Every other event type (``system``, ``user``,
  ``stream_event``, ``rate_limit_event``, ``tool_progress``) and block (``tool_result``,
  ``thinking``, ``redacted_thinking``, unknown) is dropped and counted (§14 Q14).
- A message is at most 4,000 characters and a tool text at most 2,000 (head and tail around a
  marker); a stored trace is at most 256 KiB (older tool outputs are cut first, then ``size``).
- Any ``scan_secrets`` hit (``runtime/evidence/cas.py``) is ``trace-drop`` ``secret_pattern`` with
  the pattern ids; nothing is stored. The scan runs on every kept text **before** it is cut (the
  buffer records the ids as ``secret_patterns``: a secret across the cut point would otherwise be
  stored in part, unscanned), then again on the serialized trace and on each item's text.

Not in the §2.11 body shape and added here (reported): each turn carries ``"result"`` (the Claude
``result`` fields above, else null), and a turn of a read-only turn may be named ``"planner"``,
``"reviewer"`` or ``"investigator-<k>"`` (§9.1) where the body shape says ``"turn": int``. The
product's planner turn runs before the goal has a run, so ``TraceService.admit_turns`` stores it
under the carrier ``<goal_id>.planner`` (record ``trace-<goal_id>.planner``), linked to the trial
goal by ``goal_id`` and admitted exactly as a run's trace.

ACL (§9.3): an actor with ``harness.propose`` reads development traces only (``Hold TRACE_ACL``),
as the corpus ACL does for cases; any other reader needs ``corpus.read`` (the operator), and a
holdout trace also ``corpus.holdout.evaluate``. Traces are never exported: the telemetry export
projects events without payload (``evaluation/telemetry.py``), and nothing here emits trace text.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

from ..runtime.contracts.authority import Actor
from ..runtime.contracts.identity import canonical, now
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.evidence.cas import ArtifactStore, scan_secrets
from ..runtime.storage.store import Scope, Store

if TYPE_CHECKING:
    from ..runtime.execution.product import TrialContext

Ref = dict[str, Any]

SANITIZER_VERSION = "trace-sanitizer-v1"
TRACE_KIND, DROP_KIND = "harness-trace", "trace-drop"
TRACE_SCHEMA, DROP_SCHEMA = "amplai.harness-trace.v1", "amplai.trace-drop.v1"
SPLITS = ("development", "validation", "holdout")
DROP_REASONS = ("secret_pattern", "size", "not_trial", "sanitizer_error")
ITEM_TYPES = ("message", "tool_call", "tool_result")
ROLES = ("assistant", "user", "tool")
MESSAGE_MAX = 4000  # characters of one message (§9.2)
TOOL_MAX = 2000  # characters of one tool input or output (§9.2)
TRACE_MAX_BYTES = 256 * 1024  # one stored trace (§9.2)
# one dispatch's buffer: a run that kept more cannot fit a stored trace either, so it stops
# collecting (``overflow``) and its admission is trace-drop ``size``
BUFFER_MAX_BYTES = 1024 * 1024
CUT_MARKER = "\n[... {n} characters cut ...]\n"
CUT_TOOL_TEXT = "[tool output cut to fit the trace size]"
# provider names become dropped_event_types keys only in this shape (never free text)
DROP_KEY = re.compile(r"[A-Za-z0-9._/-]{1,64}")
SUBTYPE = re.compile(r"[a-z_]{1,64}")
AUX_TURN = re.compile(r"(planner|reviewer|investigator-[0-9]{1,2})")
CODEX_ITEMS = frozenset({"agent_message"})  # §9.2; command/file-change names: §14 Q14
CLAUDE_BLOCKS = frozenset({"text", "tool_use"})  # §9.2; tool_result, thinking, ...: §14 Q14
PROVIDERS = ("codex", "claude")
SECRET_ID = re.compile(r"secret-pattern-[0-9]{1,3}")  # the ids ``scan_secrets`` returns
# IC-28 (provisional): a turn's own credential literals (a credential file's string values of at
# least this many characters, or an injected token) are checked in the turn's events
CREDENTIAL_MIN = 16
CREDENTIAL_READ_MAX = 1 << 20


# -- a turn's own credential literals (IC-28; read-only turns and executor turns) --
def strings_of(value: Any, *, keys: bool = True) -> Iterator[str]:
    """Every string in ``value`` (dict values, and keys unless ``keys`` is false; list items;
    nested)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if keys:
                yield from strings_of(key)
            yield from strings_of(item, keys=keys)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from strings_of(item, keys=keys)


def credential_literals(data: bytes) -> set[str]:
    """The literals of a credential file: every JSON string value of at least ``CREDENTIAL_MIN``
    characters (the token values; key names are not relied on), or the whole stripped text when
    it is not JSON."""
    text = data.decode(errors="replace")
    try:
        value: Any = json.loads(text)
    except ValueError:
        value = text.strip()
    return {s for s in strings_of(value, keys=False) if len(s) >= CREDENTIAL_MIN}


def read_credential_file(path: Path) -> set[str]:
    """``credential_literals`` of a run home's credential file; never follows a link the agent
    may have put there (the home is a writable bind mount). Empty when unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return set()
    with os.fdopen(fd, "rb") as handle:
        return credential_literals(handle.read(CREDENTIAL_READ_MAX))


def holds_credential(events: Iterable[Any], literals: set[str] | frozenset[str]) -> bool:
    """Whether any string of the raw decoded events contains one of ``literals``."""
    return any(lit in text for event in events for text in strings_of(event) for lit in literals)


def withhold(snapshot: dict[str, Any]) -> dict[str, Any]:
    """A snapshot whose turn held a credential literal (IC-28): no item and no result kept, one
    sanitizer error counted, so admission writes ``trace-drop`` ``sanitizer_error`` and stores
    nothing (``TraceService.admit``)."""
    snapshot["items"], snapshot["result"] = [], None
    snapshot["errors"] = int(snapshot.get("errors") or 0) + 1
    return snapshot


# -- sanitizer --
def _key(*parts: Any) -> str:
    """A ``dropped_event_types`` key from provider type names, or ``unknown``."""
    text = "/".join(p if isinstance(p, str) else "?" for p in parts)
    return text if DROP_KEY.fullmatch(text) else "unknown"


def cut_text(text: str, limit: int) -> tuple[str, bool]:
    """``text`` within ``limit`` characters: its head and tail around a marker when longer."""
    if len(text) <= limit:
        return text, False
    marker = CUT_MARKER.format(n=len(text) - limit)
    keep = max(0, limit - len(marker))
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else ""), True


def _item(
    kind: str, role: str, text: str, limit: int, secrets: set[str]
) -> tuple[dict[str, Any], bool]:
    """The kept item of ``text`` cut to ``limit``; the secret scan runs on the whole raw text
    first and adds its pattern ids to ``secrets`` (D-100: store only after the secret scan)."""
    secrets.update(scan_secrets(text.encode()))
    kept, truncated = cut_text(text, limit)
    return {"type": kind, "role": role, "text": kept, "tool": None, "exit_code": None}, truncated


_Kept = tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, int], int, set[str]]


def _sanitize(provider: str, event: Any) -> _Kept:
    """(kept items, kept result fields, dropped counts, truncated items, secret pattern ids found
    in the kept texts before they were cut) of one raw event."""
    if provider not in PROVIDERS:
        raise RuntimeFault("TRACE_PROVIDER", "The sanitizer knows the codex and claude streams")
    items: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    truncated = 0
    secrets: set[str] = set()

    def drop(*parts: Any) -> None:
        key = _key(*parts)
        dropped[key] = dropped.get(key, 0) + 1

    if not isinstance(event, dict):
        drop("unknown")
        return items, None, dropped, truncated, secrets
    kind = event.get("type")
    if provider == "codex":
        item = event.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        if kind == "item.completed" and item_type in CODEX_ITEMS:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str):
                kept, cut = _item("message", "assistant", text, MESSAGE_MAX, secrets)
                items.append(kept)
                truncated += int(cut)
            else:
                drop(kind, item_type)
        elif isinstance(kind, str) and kind.startswith("item."):
            drop(kind, item_type)
        else:
            drop(kind)
        return items, None, dropped, truncated, secrets
    if kind == "assistant":
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            drop(kind)
            return items, None, dropped, truncated, secrets
        for block in blocks:
            block_type = block.get("type") if isinstance(block, dict) else None
            if (
                block_type == "text"
                and isinstance(block, dict)
                and isinstance(block.get("text"), str)
            ):
                kept, cut = _item("message", "assistant", block["text"], MESSAGE_MAX, secrets)
                items.append(kept)
                truncated += int(cut)
            elif block_type == "tool_use":
                # a bare marker: name and input are not verified field names (§14 Q14)
                items.append(
                    {
                        "type": "tool_call",
                        "role": "assistant",
                        "text": "",
                        "tool": None,
                        "exit_code": None,
                    }
                )
            else:
                drop(kind, block_type)
        return items, None, dropped, truncated, secrets
    if kind == "result":
        subtype, is_error, turns = (
            event.get("subtype"),
            event.get("is_error"),
            event.get("num_turns"),
        )
        result = {
            "subtype": subtype if isinstance(subtype, str) and SUBTYPE.fullmatch(subtype) else None,
            "is_error": is_error if isinstance(is_error, bool) else None,
            "num_turns": turns if type(turns) is int and turns >= 0 else None,
        }
        return items, result, dropped, truncated, secrets
    if kind == "user":
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if isinstance(blocks, list) and blocks:
            for block in blocks:
                drop(kind, block.get("type") if isinstance(block, dict) else None)
        else:
            drop(kind)
        return items, None, dropped, truncated, secrets
    drop(kind)
    return items, None, dropped, truncated, secrets


def sanitize_event(
    provider: Literal["codex", "claude"], event: dict[str, Any]
) -> dict[str, Any] | None:
    """Default-deny (§9.2): ``{"items", "result", "dropped", "truncated", "secret_patterns"}`` of
    what one raw event keeps, or None when it keeps nothing (dropped). ``secret_patterns`` are the
    ids found in the kept texts before they were cut."""
    items, result, dropped, truncated, secrets = _sanitize(provider, event)
    if not items and result is None:
        return None
    return {
        "items": items,
        "result": result,
        "dropped": dropped,
        "truncated": truncated,
        "secret_patterns": sorted(secrets),
    }


class TraceBuffer:
    """The sanitized trace of one dispatch (or one read-only turn), bounded by
    ``BUFFER_MAX_BYTES``. ``add`` never raises: a sanitizer failure is counted in ``errors``
    (admission then writes ``trace-drop`` ``sanitizer_error``). ``secrets`` holds the pattern ids
    found in kept texts before they were cut, also after an overflow (admission then writes
    ``trace-drop`` ``secret_pattern``)."""

    def __init__(self, provider: str, *, max_bytes: int = BUFFER_MAX_BYTES) -> None:
        if provider not in PROVIDERS:
            raise RuntimeFault("TRACE_PROVIDER", "The sanitizer knows the codex and claude streams")
        self.provider, self.max_bytes = provider, max_bytes
        self.items: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None
        self.events = 0
        self.dropped: dict[str, int] = {}
        self.truncated = 0
        self.size = 0
        self.overflow = False
        self.errors = 0
        self.secrets: set[str] = set()
        self._lock = threading.Lock()

    def add(self, event: Any) -> None:
        with self._lock:
            self.events += 1
            try:
                items, result, dropped, truncated, secrets = _sanitize(self.provider, event)
            except Exception:  # never stop the collector; counted, admission drops the trace
                self.errors += 1
                return
            self.secrets |= secrets
            for key, count in dropped.items():
                self.dropped[key] = self.dropped.get(key, 0) + count
            self.truncated += truncated
            if result is not None:
                self.result = result
            for item in items:
                size = len(item["text"]) + 64
                if self.overflow or self.size + size > self.max_bytes:
                    self.overflow = True
                    continue
                self.size += size
                self.items.append(item)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "provider": self.provider,
                "sanitizer_version": SANITIZER_VERSION,
                "items": copy.deepcopy(self.items),
                "result": copy.deepcopy(self.result),
                "events": self.events,
                "dropped_event_types": dict(self.dropped),
                "truncated_outputs": self.truncated,
                "overflow": self.overflow,
                "errors": self.errors,
                "secret_patterns": sorted(self.secrets),
            }


def _secret_ids(value: Any) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(
        isinstance(p, str) and SECRET_ID.fullmatch(p) is not None for p in value
    )


def combine(driver_id: str, turns: list[tuple[int | str, dict[str, Any]]]) -> dict[str, Any]:
    """The run's sanitized trace from its turns' buffer snapshots (turn 0, follow-ups 1..2, or a
    named read-only turn): what the worker hands to the trace sink. A snapshot without a valid
    ``secret_patterns`` list counts as an error (admission then drops the trace)."""
    out: dict[str, Any] = {
        "sanitizer_version": SANITIZER_VERSION,
        "driver_id": driver_id,
        "turns": [],
        "events": 0,
        "dropped_event_types": {},
        "truncated_outputs": 0,
        "overflow": False,
        "errors": 0,
        "secret_patterns": [],
    }
    secrets: set[str] = set()
    for turn, snap in turns:
        if not isinstance(snap, dict) or snap.get("sanitizer_version") != SANITIZER_VERSION:
            out["errors"] += 1
            continue
        out["turns"].append(
            {"turn": turn, "items": list(snap.get("items") or []), "result": snap.get("result")}
        )
        out["events"] += int(snap.get("events") or 0)
        for key, count in (snap.get("dropped_event_types") or {}).items():
            out["dropped_event_types"][key] = out["dropped_event_types"].get(key, 0) + int(count)
        out["truncated_outputs"] += int(snap.get("truncated_outputs") or 0)
        out["overflow"] = out["overflow"] or snap.get("overflow") is True
        out["errors"] += int(snap.get("errors") or 0)
        found = snap.get("secret_patterns")
        if _secret_ids(found):
            secrets.update(found)
        else:
            out["errors"] += 1
    out["secret_patterns"] = sorted(secrets)
    return out


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _valid_turn(turn: Any) -> bool:
    return _count(turn) or (isinstance(turn, str) and AUX_TURN.fullmatch(turn) is not None)


def _valid_item(item: Any) -> bool:
    if not isinstance(item, dict) or set(item) != {"type", "role", "text", "tool", "exit_code"}:
        return False
    limit = MESSAGE_MAX if item["type"] == "message" else TOOL_MAX
    return (
        item["type"] in ITEM_TYPES
        and item["role"] in ROLES
        and isinstance(item["text"], str)
        and len(item["text"]) <= limit
        and (item["tool"] is None or (isinstance(item["tool"], str) and len(item["tool"]) <= 64))
        and (item["exit_code"] is None or type(item["exit_code"]) is int)
    )


def _valid_result(result: Any) -> bool:
    return result is None or (
        isinstance(result, dict)
        and set(result) == {"subtype", "is_error", "num_turns"}
        and (
            result["subtype"] is None
            or (
                isinstance(result["subtype"], str)
                and SUBTYPE.fullmatch(result["subtype"]) is not None
            )
        )
        and (result["is_error"] is None or isinstance(result["is_error"], bool))
        and (result["num_turns"] is None or _count(result["num_turns"]))
    )


def validate_sanitized(value: Any) -> None:
    """The shape ``combine`` returns; RuntimeFault TRACE_SANITIZED otherwise."""
    keys = {
        "sanitizer_version",
        "driver_id",
        "turns",
        "events",
        "dropped_event_types",
        "truncated_outputs",
        "overflow",
        "errors",
        "secret_patterns",
    }
    ok = (
        isinstance(value, dict)
        and set(value) == keys
        and value["sanitizer_version"] == SANITIZER_VERSION
        and isinstance(value["driver_id"], str)
        and isinstance(value["turns"], list)
        and all(
            isinstance(t, dict)
            and set(t) == {"turn", "items", "result"}
            and _valid_turn(t["turn"])
            and isinstance(t["items"], list)
            and all(_valid_item(i) for i in t["items"])
            and _valid_result(t["result"])
            for t in value["turns"]
        )
        and _count(value["events"])
        and isinstance(value["dropped_event_types"], dict)
        and all(
            isinstance(k, str) and DROP_KEY.fullmatch(k) and _count(v)
            for k, v in value["dropped_event_types"].items()
        )
        and _count(value["truncated_outputs"])
        and isinstance(value["overflow"], bool)
        and _count(value["errors"])
        and _secret_ids(value["secret_patterns"])
    )
    if not ok:
        raise RuntimeFault("TRACE_SANITIZED", "The sanitized trace does not have its shape")


def secret_patterns(body: dict[str, Any]) -> list[str]:
    """``scan_secrets`` over the serialized trace and over each item's own text (JSON escaping
    can hide a quoted secret from the patterns in the serialized form)."""
    found = set(scan_secrets(canonical(body)))
    for turn in body.get("turns") or []:
        for item in turn.get("items") or []:
            found |= set(scan_secrets(str(item.get("text") or "").encode()))
    return sorted(found)


def cut_body(body: dict[str, Any], limit: int) -> dict[str, Any] | None:
    """``body`` within ``limit`` serialized bytes for storage: older tool outputs are cut first
    (§9.2). None when that cannot make it fit (trace-drop ``size``)."""
    out = copy.deepcopy(body)
    if len(canonical(out)) <= limit:
        return out
    for turn in out["turns"]:
        for item in turn["items"]:
            if item["type"] == "tool_result" and item["text"] != CUT_TOOL_TEXT:
                item["text"] = CUT_TOOL_TEXT
                if len(canonical(out)) <= limit:
                    return out
    return None


def cut_copy(body: dict[str, Any], limit: int) -> dict[str, Any]:
    """A copy of ``body`` within ``limit`` bytes for a proposer input (§9.4: each trace cut to
    32 KiB): older tool outputs cut first, then the oldest items left out (``"cut": true``)."""
    fitted = cut_body(body, limit)
    if fitted is not None:
        return fitted
    out = copy.deepcopy(body)
    out["cut"] = True
    for turn in out["turns"]:
        for item in turn["items"]:
            if item["type"] == "tool_result":
                item["text"] = CUT_TOOL_TEXT
    for turn in out["turns"]:
        while turn["items"] and len(canonical(out)) > limit:
            turn["items"].pop(0)
    while len(out["turns"]) > 1 and len(canonical(out)) > limit:
        out["turns"].pop(0)
    return out


# -- records --
def _is_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "revision", "digest"}
        and isinstance(value["id"], str)
        and type(value["revision"]) is int
        and isinstance(value["digest"], str)
    )


def validate_trace_record(value: Any) -> None:
    """The §2.11 ``harness-trace`` shape (validated before ``put``); RuntimeFault TRACE_RECORD."""
    keys = {
        "schema",
        "scope",
        "run_id",
        "goal_id",
        "trial_ref",
        "experiment_id",
        "task_id",
        "split",
        "arm",
        "cell_id",
        "driver_id",
        "artifact",
        "events",
        "dropped_event_types",
        "truncated_outputs",
        "sanitizer_version",
        "captured_at",
    }
    artifact = value.get("artifact") if isinstance(value, dict) else None
    ok = (
        isinstance(value, dict)
        and set(value) == keys
        and value["schema"] == TRACE_SCHEMA
        and isinstance(value["scope"], dict)
        and all(isinstance(value[k], str) and value[k] for k in ("run_id", "task_id", "cell_id"))
        and isinstance(value["goal_id"], str)
        and isinstance(value["driver_id"], str)
        and (value["trial_ref"] is None or _is_ref(value["trial_ref"]))
        and (value["experiment_id"] is None or isinstance(value["experiment_id"], str))
        and value["split"] in SPLITS
        and (value["arm"] is None or isinstance(value["arm"], str))
        and isinstance(artifact, dict)
        and set(artifact) == {"id", "digest", "media_type", "size_bytes"}
        and artifact["media_type"] == "application/json"
        and _count(value["events"])
        and isinstance(value["dropped_event_types"], dict)
        and _count(value["truncated_outputs"])
        and value["sanitizer_version"] == SANITIZER_VERSION
        and isinstance(value["captured_at"], str)
    )
    if not ok:
        raise RuntimeFault("TRACE_RECORD", "Invalid harness-trace record (§2.11)")


def validate_drop_record(value: Any) -> None:
    ok = (
        isinstance(value, dict)
        and set(value) == {"schema", "scope", "run_id", "reason", "patterns", "at"}
        and value["schema"] == DROP_SCHEMA
        and isinstance(value["run_id"], str)
        and value["reason"] in DROP_REASONS
        and isinstance(value["patterns"], list)
        and all(isinstance(p, str) and p.startswith("secret-pattern-") for p in value["patterns"])
        and isinstance(value["at"], str)
    )
    if not ok:
        raise RuntimeFault("TRACE_RECORD", "Invalid trace-drop record (§2.11)")


def _trial_wire(trial: Any) -> dict[str, Any] | None:
    """The trial context as its wire dict, checked by ``TrialContext`` (None when absent or
    invalid)."""
    from ..runtime.execution.product import TrialContext

    if trial is None:
        return None
    try:
        context = trial if isinstance(trial, TrialContext) else TrialContext(**dict(trial))
    except (RuntimeFault, TypeError, ValueError):
        return None
    return context.wire()


class TraceService:
    """Stores, lists and reads trial traces (§9.3); counts for the dashboard."""

    def __init__(self, store: Store, scope: Scope, artifacts: ArtifactStore) -> None:
        self.store, self.scope, self.artifacts = store, scope, artifacts

    # -- admission --
    def _latest(self, kind: str, object_id: str) -> tuple[Ref, dict[str, Any]] | None:
        rows = [
            (r, v) for r, v in self.store.list_objects(self.scope, kind) if r["id"] == object_id
        ]
        return max(rows, key=lambda row: row[0]["revision"]) if rows else None

    def _put_once(self, kind: str, object_id: str, value: dict[str, Any]) -> Ref:
        """Revision 1 of ``object_id``, or the one already there (a replayed admission)."""
        with self.store.tx() as db:
            row = db.execute(
                "SELECT digest FROM objects WHERE tenant=? AND project=? AND kind=? AND id=? "
                "AND revision=1",
                (*self.scope.keys(), kind, object_id),
            ).fetchone()
            if row is not None:
                return {"id": object_id, "revision": 1, "digest": row[0]}
            ref: Ref = self.store.put(db, self.scope, kind, object_id, 1, value)
            return ref

    def _drop(self, run_id: str, reason: str, patterns: list[str] | None = None) -> None:
        value = {
            "schema": DROP_SCHEMA,
            "scope": self.scope.wire(),
            "run_id": run_id,
            "reason": reason,
            "patterns": sorted(patterns or []),
            "at": now(),
        }
        validate_drop_record(value)
        self._put_once(DROP_KIND, "tracedrop-" + run_id, value)

    def _task_of(self, subject: dict[str, Any]) -> str | None:
        """The task of the dispatching trial head the subject names (``eval-trial`` or
        ``calibration-trial``, written before the executor is called)."""
        trial_id = subject.get("trial_id")
        kind = "eval-trial" if "experiment_id" in subject else "calibration-trial"
        if not isinstance(trial_id, str) or not trial_id:
            return None
        try:
            task = self.store.head(self.scope, kind, trial_id)["data"].get("task_id")
        except RuntimeFault:
            return None
        return task if isinstance(task, str) and task else None

    def admit(
        self,
        *,
        run_id: str,
        goal_id: str,
        trial: TrialContext | dict[str, Any] | None,
        driver_id: str,
        sanitized: dict[str, Any],
    ) -> Ref | None:
        """The ``harness-trace`` ref, or None after writing ``trace-drop``: ``not_trial`` (no
        trial context with ``capture_trace``, or one not bound to a trial record and a split),
        ``sanitizer_error``, ``secret_pattern`` (nothing stored: a hit the buffer found in a kept
        text before it was cut, or one in the trace as cut), ``size``. Idempotent per run."""
        found = self._latest(TRACE_KIND, "trace-" + run_id)
        if found is not None:
            return found[0]
        if self._latest(DROP_KIND, "tracedrop-" + run_id) is not None:
            return None
        context = _trial_wire(trial)
        subject = (context or {}).get("subject") or {}
        task_id = self._task_of(subject) if context else None
        if (
            context is None
            or context.get("capture_trace") is not True
            or context.get("split") not in SPLITS
            or task_id is None
        ):
            self._drop(run_id, "not_trial")
            return None
        try:
            validate_sanitized(sanitized)
        except RuntimeFault:
            self._drop(run_id, "sanitizer_error")
            return None
        if sanitized["errors"]:
            self._drop(run_id, "sanitizer_error")
            return None
        body = {"run_id": run_id, "turns": copy.deepcopy(sanitized["turns"])}
        patterns = sorted(set(sanitized["secret_patterns"]) | set(secret_patterns(body)))
        if patterns:
            self._drop(run_id, "secret_pattern", patterns)
            return None
        fitted = None if sanitized["overflow"] else cut_body(body, TRACE_MAX_BYTES)
        if fitted is None:
            self._drop(run_id, "size")
            return None
        data = canonical(fitted)
        try:
            artifact = self.artifacts.admit(
                self.scope, data, "application/json", classification="restricted", trust="worker"
            )
        except Hold as exc:
            if exc.code != "SECRET_DETECTED":
                raise
            details = exc.details if isinstance(exc.details, list) else []
            self._drop(run_id, "secret_pattern", [str(p) for p in details])
            return None
        record = {
            "schema": TRACE_SCHEMA,
            "scope": self.scope.wire(),
            "run_id": run_id,
            "goal_id": goal_id,
            "trial_ref": None,  # the trial record is written after the executor returns
            "experiment_id": subject.get("experiment_id"),
            "task_id": task_id,
            "split": context["split"],
            "arm": context.get("arm"),
            "cell_id": context["cell_id"],
            "driver_id": driver_id,
            "artifact": {k: artifact[k] for k in ("id", "digest", "media_type", "size_bytes")},
            "events": sanitized["events"],
            "dropped_event_types": dict(sanitized["dropped_event_types"]),
            "truncated_outputs": sanitized["truncated_outputs"],
            "sanitizer_version": SANITIZER_VERSION,
            "captured_at": now(),
        }
        validate_trace_record(record)
        return self._put_once(TRACE_KIND, "trace-" + run_id, record)

    def admit_turns(
        self,
        *,
        goal_id: str,
        trial: TrialContext | dict[str, Any] | None,
        driver_id: str,
        turn: str,
        snapshots: list[dict[str, Any]],
    ) -> Ref | None:
        """A trial goal's read-only turn(s) of one kind (§9.1: ``planner``, ``reviewer``,
        ``investigator-<k>``) as the named turns of a trace of that goal: ``combine`` of the
        buffer snapshots, admitted exactly as a run's trace (the same trial-context check,
        sanitizer shape, secret scan, size rule, ``restricted`` artifact and proposer-only ACL).

        The carrier: a read-only turn runs before (planner) or beside the goal's runs and has no
        run of its own, so its trace is stored under ``<goal_id>.<turn>`` (record
        ``trace-<goal_id>.<turn>``, its ``run_id`` field holding that carrier id) with the trial
        goal's ``goal_id``; a run's trace stays ``trace-<run_id>``. Idempotent per goal and
        turn kind: the first admission wins."""
        if not isinstance(turn, str) or AUX_TURN.fullmatch(turn) is None:
            raise RuntimeFault("TRACE_TURN", "A read-only turn is planner, reviewer or "
                               "investigator-<k>", details=turn)  # fmt: skip
        carrier = f"{goal_id}.{turn}"
        return self.admit(
            run_id=carrier,
            goal_id=goal_id,
            trial=trial,
            driver_id=driver_id,
            sanitized=combine(driver_id, [(turn, snap) for snap in snapshots]),
        )

    def of_run(self, run_id: str) -> Ref | None:
        """The ``harness-trace`` ref admitted for ``run_id`` (or a read-only turn carrier), or
        None when none was (never captured, or ``trace-drop``). Host code only: no ACL, no
        text."""
        found = self._latest(TRACE_KIND, "trace-" + run_id)
        return dict(found[0]) if found is not None else None

    # -- reading (ACL) --
    def _acl(self, actor: Actor, split: str) -> None:
        if actor.scope != self.scope:
            raise RuntimeFault("FORBIDDEN", "Actor acts in another scope")
        if split not in SPLITS:
            raise RuntimeFault("CORPUS_SPLIT", "Unknown corpus split")
        if "harness.propose" in actor.permissions:
            if split != "development":
                raise Hold("TRACE_ACL", "A proposer reads development traces only")
            return
        actor.require("corpus.read")
        if split == "holdout":
            actor.require("corpus.holdout.evaluate")

    def records(self) -> list[tuple[Ref, dict[str, Any]]]:
        """The latest revision of every stored trace record (host code only: no ACL)."""
        latest: dict[str, tuple[Ref, dict[str, Any]]] = {}
        for ref, value in self.store.list_objects(self.scope, TRACE_KIND):
            if ref["id"] not in latest or ref["revision"] > latest[ref["id"]][0]["revision"]:
                latest[ref["id"]] = (ref, value)
        return list(latest.values())

    def list(self, actor: Actor, *, cell_id: str, split: str = "development") -> list[Ref]:
        """Trace refs of ``cell_id`` in ``split``, newest first (Hold TRACE_ACL for a proposer
        asking for another split)."""
        self._acl(actor, split)
        rows = [
            (value["captured_at"], ref)
            for ref, value in self.records()
            if value.get("cell_id") == cell_id and value.get("split") == split
        ]
        return [
            ref for _, ref in sorted(rows, key=lambda row: (row[0], row[1]["id"]), reverse=True)
        ]

    def read(self, actor: Actor, trace_ref: Ref) -> dict[str, Any]:
        """``{"trace_ref", "record", "body"}``; a proposer reads development traces only."""
        if actor.scope != self.scope:
            raise RuntimeFault("FORBIDDEN", "Actor acts in another scope")
        record = self.store.get(self.scope, TRACE_KIND, trace_ref)
        self._acl(actor, str(record.get("split")))
        body = json.loads(self.artifacts.read(self.scope, record["artifact"]))
        return {"trace_ref": dict(trace_ref), "record": record, "body": body}

    def outcomes(self, records: Sequence[dict[str, Any]]) -> dict[str, bool | None]:
        """Trace run id -> the trial result (host code): the ``success`` of the eval or
        calibration trial of the trace's task whose receipt names the trace's goal; None when no
        such trial is recorded yet (the trial record is written after the executor returns)."""
        tasks = {r.get("task_id") for r in records if r.get("task_id")}
        by_goal: dict[str, bool | None] = {}
        for kind in ("eval-trial", "calibration-trial"):
            for _ref, trial in self.store.list_objects(self.scope, kind):
                refs = trial.get("artifact_refs") or []
                if trial.get("task_id") not in tasks or not refs:
                    continue
                try:
                    receipt = json.loads(self.artifacts.read(self.scope, refs[0], trusted=True))
                except (RuntimeFault, ValueError, TypeError, KeyError):
                    continue
                goal = receipt.get("goal_id") if isinstance(receipt, dict) else None
                if isinstance(goal, str) and goal:
                    success = trial.get("success")
                    by_goal[goal] = success if isinstance(success, bool) else None
        return {str(r.get("run_id")): by_goal.get(str(r.get("goal_id") or "")) for r in records}

    def counts(self) -> dict[str, Any]:
        """Counts only, never text (the dashboard rule, §9.3): traces per split, drops per
        reason."""
        traces: dict[str, int] = {}
        for _ref, value in self.records():
            split = str(value.get("split"))
            traces[split] = traces.get(split, 0) + 1
        drops: dict[str, int] = {}
        seen: set[str] = set()
        for ref, value in self.store.list_objects(self.scope, DROP_KIND):
            if ref["id"] in seen:
                continue
            seen.add(ref["id"])
            reason = str(value.get("reason"))
            drops[reason] = drops.get(reason, 0) + 1
        return {"traces": traces, "drops": drops}


def trace_sink(
    store: Store,
    scope: Scope,
    artifacts: ArtifactStore,
    plan_of: Callable[[str], dict[str, Any]],
) -> Callable[[str, dict[str, Any]], None]:
    """The ``WorkCoordinator`` trace sink of a deployment: the run's goal and the trial context of
    its plan record (``plan["trial"]``) decide admission (``TraceService.admit``)."""
    service = TraceService(store, scope, artifacts)

    def sink(run_id: str, sanitized: dict[str, Any]) -> None:
        goal_id, trial = "", None
        try:
            run = store.head(scope, "run", run_id)
            goal_id = str(run["data"]["record"]["root_goal_id"])
            trial = (plan_of(goal_id) or {}).get("trial")
        except (RuntimeFault, KeyError, TypeError):
            trial = None
        driver = sanitized.get("driver_id") if isinstance(sanitized, dict) else None
        service.admit(
            run_id=run_id,
            goal_id=goal_id,
            trial=trial,
            driver_id=driver if isinstance(driver, str) else "",
            sanitized=sanitized,
        )

    return sink
