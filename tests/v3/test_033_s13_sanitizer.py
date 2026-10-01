"""Work 033 S13: the trace sanitizer ``trace-sanitizer-v1`` (interfaces.md §9.2, §14 Q14).

Pure unit tests over ``meta_harness/traces.py``: no store, no driver. The sanitizer is default-deny:

- Codex: ``item.completed`` items of type ``agent_message`` only; reasoning items and every unlisted
  type (command and file-change item names are 확인 필요, Q14) are dropped and counted.
- Claude Code: ``assistant`` blocks ``text`` and ``tool_use`` (a bare marker); the ``result``
  event's ``subtype``, ``is_error`` and ``num_turns``. ``tool_result``, ``thinking``,
  ``redacted_thinking``, unknown blocks and the ``system``, ``user``, ``stream_event``,
  ``rate_limit_event`` and ``tool_progress`` events are dropped (Q14: unverified names).
- Truncation: a message at most 4,000 characters, a tool text at most 2,000, a trace at most
  256 KiB (older tool outputs are cut first).
- The secret scan runs on each kept text before it is cut (the buffer records the pattern ids:
  a secret across the cut point is found), then on the serialized trace and on each item's text.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from amplai_foundry.meta_harness import traces
from amplai_foundry.meta_harness.traces import (
    CUT_TOOL_TEXT,
    MESSAGE_MAX,
    SANITIZER_VERSION,
    TOOL_MAX,
    TRACE_MAX_BYTES,
    TraceBuffer,
    combine,
    cut_body,
    cut_copy,
    cut_text,
    sanitize_event,
    secret_patterns,
    validate_sanitized,
)
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.evidence.cas import scan_secrets

# a value that matches SECRET_PATTERNS[1] (``password: <24+ chars>``) of runtime/evidence/cas.py
SECRET = "password: Zq9wXk3LmN8vBc2RtY6uHj4P"
SECRET_ANTHROPIC = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
# a 40-character key: 7 characters on one side of a cut, or 20 without the prefix on the other,
# are fewer than the 24 that pattern 3 needs
LONG_KEY = "sk-ant-api03-" + "Zq9wXk3LmN8vBc2RtY6uHj4PAa1Bb2Cc3Dd4Ee5F"


def fault(code: str, fn: Any, *args: Any, **kwargs: Any) -> RuntimeFault:
    with pytest.raises(RuntimeFault) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


# -- raw provider events -----------------------------------------------------------------------
def codex_message(text: str) -> dict[str, Any]:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def codex_item(item_type: str, **fields: Any) -> dict[str, Any]:
    return {"type": "item.completed", "item": {"type": item_type, **fields}}


def claude_assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def sanitized_of(provider: str, events: list[Any]) -> dict[str, Any]:
    buffer = TraceBuffer(provider)
    for event in events:
        buffer.add(event)
    return buffer.snapshot()


def make_item(kind: str, text: str, role: str = "assistant") -> dict[str, Any]:
    return {"type": kind, "role": role, "text": text, "tool": None, "exit_code": None}


def across_cut(side: str, length: int = 10_000) -> str:
    """A message of ``length`` characters whose ``LONG_KEY`` crosses the cut point of
    ``cut_text(text, MESSAGE_MAX)``: on the head side the prefix and 7 key characters stay, on the
    tail side the last 20 key characters stay without the prefix."""
    probe, _ = cut_text("." * length, MESSAGE_MAX)
    head = probe.index("\n[...")
    tail = len(probe) - probe.index("cut ...]\n") - len("cut ...]\n")
    start = head - 20 if side == "head" else length - tail - (len(LONG_KEY) - 20)
    text = "." * start + LONG_KEY + "." * (length - start - len(LONG_KEY))
    assert len(text) == length
    return text


def good_sanitized(items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A value of the ``combine`` shape that ``validate_sanitized`` accepts."""
    turn = {"turn": 0, "items": items if items is not None else [make_item("message", "hello")]}
    return combine("codex-cli", [(0, {**sanitized_of("codex", []), "items": turn["items"]})])


# ==================================================================================================
# Codex
# ==================================================================================================
def test_the_version_is_trace_sanitizer_v1() -> None:
    assert SANITIZER_VERSION == "trace-sanitizer-v1"


def test_codex_agent_message_is_kept_as_an_assistant_message() -> None:
    kept = sanitize_event("codex", codex_message("I will fix add()."))
    assert kept is not None
    assert kept["items"] == [
        {
            "type": "message",
            "role": "assistant",
            "text": "I will fix add().",
            "tool": None,
            "exit_code": None,
        }
    ]
    assert kept["result"] is None and kept["dropped"] == {} and kept["truncated"] == 0


def test_codex_reasoning_items_are_dropped() -> None:
    event = codex_item("reasoning", text="private chain of thought")
    assert sanitize_event("codex", event) is None
    buffer = TraceBuffer("codex")
    buffer.add(event)
    snap = buffer.snapshot()
    assert snap["items"] == [] and snap["dropped_event_types"] == {"item.completed/reasoning": 1}
    assert "private chain of thought" not in json.dumps(snap)


@pytest.mark.parametrize(
    "item_type", ["command_execution", "file_change", "mcp_tool_call", "web_search", "unknown_x"]
)
def test_codex_command_and_file_change_items_are_dropped_until_q14_names_them(
    item_type: str,
) -> None:
    """§14 Q14: the exact item type names are 확인 필요; every unlisted type is dropped."""
    event = codex_item(item_type, command="cat /etc/passwd", aggregated_output="root:x:0:0")
    assert sanitize_event("codex", event) is None
    buffer = TraceBuffer("codex")
    buffer.add(event)
    snap = buffer.snapshot()
    assert snap["dropped_event_types"] == {f"item.completed/{item_type}": 1}
    assert "passwd" not in json.dumps(snap) and "root:x" not in json.dumps(snap)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
        {"type": "error", "message": "boom"},
    ],
)
def test_codex_non_item_events_keep_nothing(event: dict[str, Any]) -> None:
    assert sanitize_event("codex", event) is None


def test_codex_agent_message_without_text_is_dropped_and_counted() -> None:
    event = {"type": "item.completed", "item": {"type": "agent_message"}}
    assert sanitize_event("codex", event) is None
    buffer = TraceBuffer("codex")
    buffer.add(event)
    assert buffer.snapshot()["dropped_event_types"] == {"item.completed/agent_message": 1}


def test_codex_item_started_of_a_listed_type_is_not_kept() -> None:
    """Only ``item.completed`` carries the final text; a started item holds nothing."""
    event = {"type": "item.started", "item": {"type": "agent_message", "text": "draft"}}
    assert sanitize_event("codex", event) is None


def test_codex_extra_fields_of_a_kept_item_are_not_copied() -> None:
    event = codex_message("ok")
    event["item"]["id"] = "item_7"
    event["item"]["extra"] = "api surface"
    kept = sanitize_event("codex", event)
    assert kept is not None
    assert set(kept["items"][0]) == {"type", "role", "text", "tool", "exit_code"}
    assert "api surface" not in json.dumps(kept)


# ==================================================================================================
# Claude Code (§14 Q14: only verified names pass)
# ==================================================================================================
def test_claude_text_block_is_kept() -> None:
    kept = sanitize_event("claude", claude_assistant({"type": "text", "text": "Done."}))
    assert kept is not None
    assert kept["items"] == [make_item("message", "Done.")]


def test_claude_tool_use_is_kept_only_as_a_bare_marker() -> None:
    """``name`` and ``input`` of ``tool_use`` are unverified (Q14): never copied."""
    block = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "Bash",
        "input": {"command": "echo " + SECRET},
    }
    kept = sanitize_event("claude", claude_assistant(block))
    assert kept is not None
    assert kept["items"] == [
        {"type": "tool_call", "role": "assistant", "text": "", "tool": None, "exit_code": None}
    ]
    flat = json.dumps(kept)
    assert "Bash" not in flat and "echo" not in flat and "toolu_1" not in flat
    assert secret_patterns({"turns": [{"items": kept["items"]}]}) == []


@pytest.mark.parametrize(
    "block_type",
    ["tool_result", "thinking", "redacted_thinking", "server_tool_use", "image", "unknown_block"],
)
def test_claude_unverified_block_names_are_dropped_and_counted(block_type: str) -> None:
    block = {"type": block_type, "text": "SECRET-BODY", "thinking": "SECRET-BODY", "content": "x"}
    event = claude_assistant({"type": "text", "text": "kept"}, block)
    buffer = TraceBuffer("claude")
    buffer.add(event)
    snap = buffer.snapshot()
    assert [i["text"] for i in snap["items"]] == ["kept"]
    assert snap["dropped_event_types"] == {f"assistant/{block_type}": 1}
    assert "SECRET-BODY" not in json.dumps(snap)


def test_claude_tool_result_in_a_user_event_is_dropped() -> None:
    event = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file body"}],
        },
    }
    assert sanitize_event("claude", event) is None
    buffer = TraceBuffer("claude")
    buffer.add(event)
    snap = buffer.snapshot()
    assert snap["dropped_event_types"] == {"user/tool_result": 1}
    assert "file body" not in json.dumps(snap)


@pytest.mark.parametrize(
    "event_type", ["system", "stream_event", "rate_limit_event", "tool_progress", "mystery"]
)
def test_claude_other_event_types_are_dropped_and_counted(event_type: str) -> None:
    event = {"type": event_type, "payload": "PAYLOAD"}
    assert sanitize_event("claude", event) is None
    buffer = TraceBuffer("claude")
    buffer.add(event)
    snap = buffer.snapshot()
    assert snap["dropped_event_types"] == {event_type: 1} and snap["events"] == 1
    assert "PAYLOAD" not in json.dumps(snap)


def test_claude_result_keeps_subtype_is_error_and_num_turns_only() -> None:
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "result": "FREE TEXT RESULT",
        "total_cost_usd": 1.5,
        "session_id": "s1",
    }
    kept = sanitize_event("claude", event)
    assert kept is not None and kept["items"] == []
    assert kept["result"] == {"subtype": "success", "is_error": False, "num_turns": 3}
    assert "FREE TEXT RESULT" not in json.dumps(kept)


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"subtype": "Bad Subtype!", "is_error": "yes", "num_turns": -1}, (None, None, None)),
        ({"subtype": 5, "is_error": 1, "num_turns": True}, (None, None, None)),
        (
            {"subtype": "error_max_turns", "is_error": True, "num_turns": 0},
            ("error_max_turns", True, 0),
        ),
    ],
)
def test_claude_result_fields_are_shape_checked(
    fields: dict[str, Any], expected: tuple[Any, Any, Any]
) -> None:
    kept = sanitize_event("claude", {"type": "result", **fields})
    assert kept is not None
    assert (
        kept["result"]["subtype"],
        kept["result"]["is_error"],
        kept["result"]["num_turns"],
    ) == expected


def test_claude_assistant_event_without_content_blocks_is_dropped() -> None:
    assert sanitize_event("claude", {"type": "assistant", "message": {"content": "text"}}) is None
    assert sanitize_event("claude", {"type": "assistant"}) is None


# ==================================================================================================
# robustness
# ==================================================================================================
@pytest.mark.parametrize("event", [None, 5, "text", ["item.completed"], {}])
def test_a_non_event_keeps_nothing(event: Any) -> None:
    assert sanitize_event("codex", event) is None
    assert sanitize_event("claude", event) is None


def test_an_unknown_provider_is_a_runtime_fault() -> None:
    fault("TRACE_PROVIDER", sanitize_event, "opencode", codex_message("x"))  # type: ignore[arg-type]
    fault("TRACE_PROVIDER", TraceBuffer, "opencode")


def test_dropped_keys_never_carry_free_text() -> None:
    """A provider-chosen type name becomes a key only in a safe shape, else ``unknown``."""
    buffer = TraceBuffer("claude")
    buffer.add({"type": "type with spaces and " + SECRET})
    snap = buffer.snapshot()
    assert snap["dropped_event_types"] == {"unknown": 1}
    assert "password" not in json.dumps(snap)


# ==================================================================================================
# truncation (§9.2)
# ==================================================================================================
def test_cut_text_keeps_head_and_tail_around_a_marker_within_the_limit() -> None:
    text = "H" * 3000 + "M" * 5000 + "T" * 3000
    cut, was_cut = cut_text(text, 2000)
    assert was_cut and len(cut) <= 2000
    assert cut.startswith("H") and cut.endswith("T")
    assert "characters cut" in cut
    same, was_cut = cut_text("short", 2000)
    assert (same, was_cut) == ("short", False)
    exact, was_cut = cut_text("x" * 2000, 2000)
    assert not was_cut and len(exact) == 2000


def test_a_long_message_is_cut_to_4000_and_counted() -> None:
    kept = sanitize_event("codex", codex_message("a" * 9000))
    assert kept is not None
    assert len(kept["items"][0]["text"]) <= MESSAGE_MAX == 4000
    assert kept["truncated"] == 1
    claude = sanitize_event("claude", claude_assistant({"type": "text", "text": "b" * 4001}))
    assert (
        claude is not None and len(claude["items"][0]["text"]) <= 4000 and claude["truncated"] == 1
    )


def test_a_message_of_exactly_4000_characters_is_not_cut() -> None:
    kept = sanitize_event("codex", codex_message("c" * 4000))
    assert kept is not None and kept["truncated"] == 0 and len(kept["items"][0]["text"]) == 4000


def test_tool_text_limit_is_2000_and_the_item_validator_enforces_it() -> None:
    assert TOOL_MAX == 2000
    ok = good_sanitized([make_item("tool_result", "o" * 2000, "tool")])
    validate_sanitized(ok)
    bad = good_sanitized([make_item("tool_result", "o" * 2001, "tool")])
    fault("TRACE_SANITIZED", validate_sanitized, bad)
    # a message may be up to 4000
    validate_sanitized(good_sanitized([make_item("message", "m" * 4000)]))
    fault("TRACE_SANITIZED", validate_sanitized, good_sanitized([make_item("message", "m" * 4001)]))


def test_cut_body_cuts_tool_outputs_first_and_keeps_messages() -> None:
    items = [make_item("message", "keep me")] + [
        make_item("tool_result", "t" * 2000, "tool") for _ in range(200)
    ]
    body = {"run_id": "r1", "turns": [{"turn": 0, "items": items, "result": None}]}
    assert len(traces.canonical(body)) > TRACE_MAX_BYTES
    fitted = cut_body(body, TRACE_MAX_BYTES)
    assert fitted is not None and len(traces.canonical(fitted)) <= TRACE_MAX_BYTES
    out = fitted["turns"][0]["items"]
    assert out[0]["text"] == "keep me"
    assert any(i["text"] == CUT_TOOL_TEXT for i in out)
    assert body["turns"][0]["items"][1]["text"] == "t" * 2000  # the input is not mutated


def test_cut_body_gives_up_when_messages_alone_exceed_the_limit() -> None:
    items = [make_item("message", "m" * 4000) for _ in range(80)]
    body = {"run_id": "r1", "turns": [{"turn": 0, "items": items, "result": None}]}
    assert cut_body(body, TRACE_MAX_BYTES) is None  # TraceService then writes trace-drop "size"


def test_cut_copy_always_fits_a_proposer_input_limit() -> None:
    items = [make_item("message", "m" * 4000) for _ in range(40)]
    body = {"run_id": "r1", "turns": [{"turn": 0, "items": items, "result": None}]}
    out = cut_copy(body, 32 * 1024)
    assert len(traces.canonical(out)) <= 32 * 1024
    assert out.get("cut") is True


# ==================================================================================================
# the buffer and the combined trace
# ==================================================================================================
def test_the_buffer_counts_every_event_and_snapshots_a_deep_copy() -> None:
    buffer = TraceBuffer("codex")
    buffer.add(codex_message("one"))
    buffer.add(codex_item("reasoning", text="r"))
    buffer.add({"type": "turn.completed"})
    snap = buffer.snapshot()
    assert snap["sanitizer_version"] == SANITIZER_VERSION and snap["provider"] == "codex"
    assert snap["events"] == 3 and len(snap["items"]) == 1
    assert snap["dropped_event_types"] == {"item.completed/reasoning": 1, "turn.completed": 1}
    assert snap["overflow"] is False and snap["errors"] == 0
    snap["items"][0]["text"] = "mutated"
    assert buffer.snapshot()["items"][0]["text"] == "one"


def test_the_buffer_never_raises_and_counts_a_sanitizer_error(monkeypatch: Any) -> None:
    def boom(provider: str, event: Any) -> Any:
        raise ValueError("bug")

    monkeypatch.setattr(traces, "_sanitize", boom)
    buffer = TraceBuffer("codex")
    buffer.add(codex_message("x"))  # must not raise: the collector never stops for a trace
    snap = buffer.snapshot()
    assert snap["errors"] == 1 and snap["items"] == []


def test_the_buffer_stops_collecting_at_its_byte_bound_and_marks_overflow() -> None:
    buffer = TraceBuffer("codex", max_bytes=500)
    for i in range(20):
        buffer.add(codex_message(f"{i}:" + "x" * 100))
    snap = buffer.snapshot()
    assert snap["overflow"] is True and 0 < len(snap["items"]) < 20
    kept = [i["text"] for i in snap["items"]]
    assert kept == [f"{n}:" + "x" * 100 for n in range(len(kept))]  # a prefix, never a gap


def test_the_result_event_of_a_stream_is_kept_in_the_snapshot() -> None:
    buffer = TraceBuffer("claude")
    buffer.add(claude_assistant({"type": "text", "text": "hi"}))
    buffer.add({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1})
    snap = buffer.snapshot()
    assert snap["result"] == {"subtype": "success", "is_error": False, "num_turns": 1}


def test_combine_merges_turns_and_sums_counters() -> None:
    first = sanitized_of("codex", [codex_message("a"), codex_item("reasoning")])
    second = sanitized_of("codex", [codex_message("b"), {"type": "turn.completed"}])
    out = combine("codex-cli", [(0, first), (1, second), ("planner", first)])
    validate_sanitized(out)
    assert out["driver_id"] == "codex-cli" and out["sanitizer_version"] == SANITIZER_VERSION
    assert [t["turn"] for t in out["turns"]] == [0, 1, "planner"]
    assert out["events"] == 2 + 2 + 2
    assert out["dropped_event_types"] == {"item.completed/reasoning": 2, "turn.completed": 1}
    assert out["overflow"] is False and out["errors"] == 0


def test_combine_counts_a_foreign_snapshot_as_an_error() -> None:
    out = combine("codex-cli", [(0, {"sanitizer_version": "other"}), (1, "not a dict")])  # type: ignore[list-item]
    assert out["turns"] == [] and out["errors"] == 2


@pytest.mark.parametrize("turn", ["planner", "reviewer", "investigator-1", "investigator-12", 0, 3])
def test_named_read_only_turns_are_valid_turn_names(turn: Any) -> None:
    snap = sanitized_of("codex", [codex_message("x")])
    validate_sanitized(combine("codex-cli", [(turn, snap)]))


@pytest.mark.parametrize(
    "turn", ["judge", "investigator-", "investigator-123", "-1", -1, 1.5, None]
)
def test_other_turn_names_are_refused(turn: Any) -> None:
    value = good_sanitized()
    value["turns"][0]["turn"] = turn
    fault("TRACE_SANITIZED", validate_sanitized, value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(sanitizer_version="trace-sanitizer-v0"),
        lambda v: v.update(extra=1),
        lambda v: v.pop("errors"),
        lambda v: v.update(driver_id=7),
        lambda v: v["turns"][0]["items"][0].update(type="reasoning"),
        lambda v: v["turns"][0]["items"][0].update(role="system"),
        lambda v: v["turns"][0]["items"][0].update(extra="x"),
        lambda v: v["turns"][0]["items"][0].update(exit_code="0"),
        lambda v: v["turns"][0]["items"][0].update(tool="t" * 65),
        lambda v: v["turns"][0].update(result={"subtype": "ok"}),
        lambda v: v.update(dropped_event_types={"has space": 1}),
        lambda v: v.update(dropped_event_types={"k": -1}),
        lambda v: v.update(overflow="no"),
        lambda v: v.update(events=-1),
        lambda v: v.update(truncated_outputs=True),
        lambda v: v.pop("secret_patterns"),
        lambda v: v.update(secret_patterns="secret-pattern-1"),
        lambda v: v.update(secret_patterns=["password: x"]),
        lambda v: v.update(secret_patterns=[3]),
    ],
)
def test_validate_sanitized_refuses_a_malformed_trace(mutate: Any) -> None:
    value = good_sanitized()
    validate_sanitized(value)
    mutate(value)
    fault("TRACE_SANITIZED", validate_sanitized, value)


def test_validate_sanitized_refuses_a_non_dict() -> None:
    fault("TRACE_SANITIZED", validate_sanitized, None)
    fault("TRACE_SANITIZED", validate_sanitized, [])


# ==================================================================================================
# secret scan (§9.2)
# ==================================================================================================
def test_secret_patterns_find_a_secret_in_an_items_text() -> None:
    body = {"run_id": "r", "turns": [{"turn": 0, "items": [make_item("message", SECRET)]}]}
    assert secret_patterns(body) == ["secret-pattern-2"]
    body = {
        "run_id": "r",
        "turns": [{"turn": 0, "items": [make_item("message", "key " + SECRET_ANTHROPIC)]}],
    }
    assert secret_patterns(body) == ["secret-pattern-3"]
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE"
    body = {"run_id": "r", "turns": [{"turn": 0, "items": [make_item("message", pem)]}]}
    assert secret_patterns(body) == ["secret-pattern-1"]


def test_secret_patterns_see_a_quoted_secret_that_json_escaping_hides() -> None:
    """The serialized form escapes quotes; the scan also runs on each item's own text."""
    text = 'password = "Zq9wXk3LmN8vBc2RtY6uHj4P"'
    body = {"run_id": "r", "turns": [{"turn": 0, "items": [make_item("message", text)]}]}
    assert secret_patterns(body) == ["secret-pattern-2"]


def test_secret_patterns_are_empty_for_clean_text() -> None:
    body = {
        "run_id": "r",
        "turns": [{"turn": 0, "items": [make_item("message", "The password is set elsewhere.")]}],
    }
    assert secret_patterns(body) == []


# ==================================================================================================
# a secret across the cut point (D-100: the secret scan runs before the cut)
# ==================================================================================================
@pytest.mark.parametrize("side", ["head", "tail"])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_a_secret_across_the_cut_point_is_found_before_the_cut(provider: str, side: str) -> None:
    text = across_cut(side)
    assert scan_secrets(text.encode()) == ["secret-pattern-3"]
    cut, was_cut = cut_text(text, MESSAGE_MAX)
    assert was_cut and scan_secrets(cut.encode()) == []  # the cut alone hides the key
    event = (
        codex_message(text)
        if provider == "codex"
        else claude_assistant({"type": "text", "text": text})
    )
    kept = sanitize_event(provider, event)  # type: ignore[arg-type]
    assert kept is not None and kept["truncated"] == 1
    assert kept["secret_patterns"] == ["secret-pattern-3"]
    assert secret_patterns({"turns": [{"items": kept["items"]}]}) == []  # the scan after the cut
    snap = sanitized_of(provider, [event])
    assert snap["secret_patterns"] == ["secret-pattern-3"]
    out = combine("codex-cli", [(0, snap)])
    validate_sanitized(out)
    assert out["secret_patterns"] == ["secret-pattern-3"] and out["errors"] == 0


def test_clean_kept_text_records_no_secret_pattern() -> None:
    kept = sanitize_event("codex", codex_message("." * 9000))
    assert kept is not None and kept["secret_patterns"] == []
    assert sanitized_of("codex", [codex_message("fine")])["secret_patterns"] == []


def test_the_buffer_records_a_secret_it_saw_after_it_overflowed() -> None:
    buffer = TraceBuffer("codex", max_bytes=100)
    buffer.add(codex_message("x" * 200))
    buffer.add(codex_message("key " + SECRET_ANTHROPIC))
    snap = buffer.snapshot()
    assert snap["overflow"] is True and snap["items"] == []
    assert snap["secret_patterns"] == ["secret-pattern-3"]


def test_material_the_sanitizer_drops_is_neither_kept_nor_scanned() -> None:
    """The scan covers what a trace keeps (§9.2): dropped events and blocks are never stored."""
    snap = sanitized_of(
        "codex", [codex_item("reasoning", text=SECRET), codex_item("command_execution", cmd=SECRET)]
    )
    assert snap["secret_patterns"] == [] and "Zq9wXk3L" not in json.dumps(snap)


def test_combine_merges_the_secret_ids_of_every_turn() -> None:
    first = sanitized_of("codex", [codex_message("pem -----BEGIN PRIVATE KEY-----")])
    second = sanitized_of("codex", [codex_message(across_cut("tail"))])
    out = combine("codex-cli", [(0, first), (1, second), (2, sanitized_of("codex", []))])
    validate_sanitized(out)
    assert out["secret_patterns"] == ["secret-pattern-1", "secret-pattern-3"]


@pytest.mark.parametrize("value", ["missing", "secret-pattern-1", ["password"], [1], None])
def test_combine_counts_a_snapshot_without_valid_secret_ids_as_an_error(value: Any) -> None:
    snap = sanitized_of("codex", [codex_message("x")])
    if value == "missing":
        snap.pop("secret_patterns")
    else:
        snap["secret_patterns"] = value
    out = combine("codex-cli", [(0, snap)])
    assert out["errors"] == 1 and out["secret_patterns"] == []
