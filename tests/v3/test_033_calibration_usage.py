"""Work 033 P8: the usage a calibration trial records (interfaces.md §5.3, §8.2-§8.3, D-094).

Two findings of calibration plan ``calplan-d6b91d293f9a69fc39678e74`` (2026-10-08):

1. Trial ``caltrial-441e1077d35d43d09312b43454ade75b`` (codex-cli.gpt-5.6-sol.high) stopped the
   calibration with unknown usage. Its driver journal holds 16 Codex events (thread.started,
   turn.started, then item.started/item.completed only), none with usage, and ends ``cancelled``
   with ``failure: "cancel"``, no ``exit_code``, ``row_version`` 39 (2 x 16 events + 4 + 3). A
   journal that completes has ``row_version`` 2 x cursor + 5; the extra update is ``_collect``'s
   stream-fault path writing ``failed`` with the fault's code, which the worker's ``_fail`` cancel
   then overwrote with ``cancelled``/``cancel``. The fault's code was lost; the next event's type
   was not recorded. The stand-in below quarantines an unqualified event type to reach the same
   path. The fault's code and the 17th event's type stay unknown (확인 필요). The run's usage
   stays unknown because the Codex ``exec --json`` stream reports usage only on
   ``turn.completed``. The native rollout file under the agent's writable home
   (``journal/native/dispatch-9abec031.../.codex/sessions/2026/10/08/rollout-...jsonl``) does hold
   a ``token_usage_record`` (02:43:58.773: input 141378, cached 124288, output 1599); operator
   decision (B) of 2026-10-08 never recovers usage from a file the agent can write.
2. Claude trials record input tokens of 4-54 while the run's ``usage-detail`` holds hundreds of
   thousands of cache tokens (``usage-detail-0f803a17b208ebbdd7f99eed843439e2``: input 20, cache
   read 324979, cache creation 43552, output 9363). Anthropic's ``input_tokens`` excludes cache,
   Codex's includes it, and budgets, trial tokens and quota windows count ``input + output``.

Real: store, runtime, worker coordinator, the Codex CLI port and driver, the journal, the
normalizer, pricing. Stand-in: the "container" is a host-process script (rc06 rig).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.agent_drivers.protocol import EventNormalizer, input_total
from amplai_foundry.evaluation import pricing
from amplai_foundry.meta_harness.quota import QuotaObserver
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.readonly_turn import _claude_usage
from amplai_foundry.runtime.storage.store import Scope, Store
from amplai_foundry.sandbox.git_workspace import PATCH_BINDING
from rc06_rig import rig_with_codex, submit

# The recorded journal's event types, in order (dispatch-9abec031c0a946fe9438a1d45ed3c6b2).
RECORDED_TYPES = ["thread.started", "turn.started", "item.completed"] + [
    "item.started", "item.completed",
] * 3 + ["item.completed"] + ["item.started", "item.completed"] * 3  # fmt: skip
STREAM = r"""
import json, sys, time
session, types = sys.argv[1], json.loads(sys.argv[2])
for i, kind in enumerate(types):
    event = {"type": kind}
    if kind == "thread.started":
        event["thread_id"] = session
    elif kind.startswith("item."):
        event["item"] = {"id": "item_%d" % i, "type": "command_execution"}
    sys.stdout.write(json.dumps(event) + "\n")
# the event after the 16th was not recorded; an unqualified type takes the same fault path
sys.stdout.write(json.dumps({"type": "not.a.qualified.codex.event"}) + "\n")
sys.stdout.flush()
time.sleep(30)  # still running when the worker reads the fault: it is stopped, never awaited
"""


def recorded_stream(container: Any) -> None:
    def command(argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        return [sys.executable, "-c", STREAM, "thread_" + run_name, json.dumps(RECORDED_TYPES)]

    container.command = command


def test_a_codex_turn_that_faults_mid_stream_keeps_why_it_ended(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "right")
    recorded_stream(container)
    goal = submit(rig, "make value return 2")
    plan = rig.service.plan(goal)
    rig.service.approve(rig.operator, goal)
    dispatch = rig.d.runtime.claim(rig.actors.worker, goal_id=goal)
    assert dispatch is not None
    did = dispatch["dispatch_id"]
    with pytest.raises(Hold) as held:
        loop.coordinator.execute(
            rig.actors.worker, dispatch, prompt="Make value() return 2.",
            base_snapshot=plan["base"], output_paths={"change": PATCH_BINDING},
        )  # fmt: skip
    assert held.value.code == "DRIVER_BOUNDARY"
    assert held.value.details == {"state": "failed", "failure": "UNKNOWN_PROVIDER_EVENT"}
    journal = container.driver.journal.read(did)
    # the worker's cancel after the fact no longer rewrites the fault as a cancellation
    assert (journal["state"], journal["failure"]) == ("failed", "UNKNOWN_PROVIDER_EVENT")
    assert journal["process_stopped"] is True and journal["cursor"] == len(RECORDED_TYPES)
    # what the trial saw: no event of the turn carried usage (Codex reports it on turn.completed)
    assert all(e["value"]["usage"]["status"] == "unknown" for e in journal["events"])
    assert journal["usage"]["status"] == "unknown" and journal["usage_detail"] is None
    head = rig.d.store.head(rig.d.scope, "worker-execution", did)
    assert head["state"] == "held" and head["data"]["hold_code"] == "DRIVER_BOUNDARY"
    assert head["data"]["hold_details"] == {"state": "failed", "failure": "UNKNOWN_PROVIDER_EVENT"}
    assert head["data"]["process_stopped"] is True


# -- Claude: cache tokens count as input, as Codex's do -----------------------------------------
# The result event's usage of run-730c167378a14c84b059c83c2cdd11e3 (its usage-detail record and
# the run's cost_microunits 600752), in Claude Code's stream-json shape.
CLAUDE_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "session-claude-1",
    "total_cost_usd": 0.600752,
    "usage": {
        "input_tokens": 20,
        "cache_read_input_tokens": 324979,
        "cache_creation_input_tokens": 43552,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 43552},
        "output_tokens": 9363,
    },
}
CLAUDE_INPUT = 20 + 324979 + 43552


def test_a_claude_run_counts_its_cache_reads_and_writes_as_input() -> None:
    n = EventNormalizer("claude")
    n.accept({"type": "system", "subtype": "init", "session_id": "session-claude-1"})
    n.accept(CLAUDE_RESULT)
    assert (n.usage["input_tokens"], n.usage["output_tokens"]) == (CLAUDE_INPUT, 9363)
    # the provider's own counts stay as reported, with how its input treats cache (D-094)
    assert n.usage_detail is not None and n.usage_detail["input_includes_cache"] is False
    assert n.usage_detail["fields"]["input_tokens"] == 20
    assert n.usage_detail["fields"]["cache_read_input_tokens"] == 324979
    assert n.usage["status"] == "estimated" and n.usage["cost_microunits"] == 600752


def test_codex_input_is_unchanged_because_it_already_contains_its_cache() -> None:
    usage = {"input_tokens": 473463, "cached_input_tokens": 441472, "output_tokens": 10812}
    assert input_total("codex", usage) == 473463


def test_a_claude_result_without_cache_fields_counts_its_input_as_reported() -> None:
    assert input_total("claude", {"input_tokens": 7, "output_tokens": 11}) == 7


def test_pricing_still_buckets_claude_cache_once(tmp_path: Path) -> None:
    root = tmp_path / "prices"
    root.mkdir()
    (root / "t1.json").write_text(json.dumps({
        "schema_version": "price-table-1", "price_table_id": "t1",
        "effective_from": "2026-09-30", "currency": "USD",
        "models": {"claude-opus-5-5": {
            "provider": "anthropic", "input": 2_000_000, "cache_read": 200_000,
            "cache_write_5m": 2_500_000, "cache_write_1h": 4_000_000, "output": 10_000_000,
            "sources": ["https://y"]}},
    }))  # fmt: skip
    n = EventNormalizer("claude")
    n.accept(CLAUDE_RESULT)
    got = pricing.estimate(
        "claude-opus-5-5", "2026-10-08", detail=n.usage_detail, usage=n.usage,
        tables=pricing.load_tables(root),
    )  # fmt: skip
    assert got["tokens"] == {"input": 20, "cache_read": 324979, "cache_write_5m": 0,
                             "cache_write_1h": 43552, "output": 9363}  # fmt: skip
    assert sum(got["tokens"].values()) == CLAUDE_INPUT + 9363  # nothing counted twice
    assert got["upper_bound"] is False


def test_a_claude_read_only_turn_reports_its_input_with_cache_like_a_codex_turn() -> None:
    assert _claude_usage(CLAUDE_RESULT["usage"]) == {
        "input_tokens": CLAUDE_INPUT,
        "output_tokens": 9363,
        "cache_read_input_tokens": 324979,
        "cache_creation_input_tokens": 43552,
        # the cache-write lifetime split Anthropic reports under ``cache_creation``
        "ephemeral_5m_input_tokens": 0,
        "ephemeral_1h_input_tokens": 43552,
    }
    assert _claude_usage(None) == {"input_tokens": None, "output_tokens": None}


def test_quota_reads_cached_input_from_a_stored_usage_detail_record(tmp_path: Path) -> None:
    scope = Scope("local", "app")
    with Store(tmp_path / "runtime", clock=lambda: 1_791_430_000.0) as store:
        with store.tx() as db:
            driver = store.put(db, scope, "driver-capabilities", "claude-drv", 1,
                               {"driver_id": "claude-cli"})  # fmt: skip
            # the shape worker._usage writes (usage-detail-0f803a17b208ebbdd7f99eed843439e2)
            detail = store.put(db, scope, "usage-detail", "usage-detail-1", 1, {
                "usage_detail_id": "usage-detail-1", "run_id": "run-1", "provider": "claude",
                "input_includes_cache": False, "cost_source_ref": None,
                "fields": {"input_tokens": 20, "cache_read_input_tokens": 324979,
                           "cache_creation_input_tokens": 43552, "output_tokens": 9363,
                           "ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 43552},
            })  # fmt: skip
            store.put(db, scope, "run-record", "run-1", 1, {
                "driver_profile_ref": driver, "finished_at": "2026-10-08T02:33:28Z",
                "usage": {"input_tokens": CLAUDE_INPUT, "output_tokens": 9363,
                          "source_ref": detail},
            })  # fmt: skip
        windows = QuotaObserver(store, scope).observe(until="2026-10-08T03:00:00Z")
    (one,) = [w for w in windows if w.window == "1h"]
    assert (one.input_tokens, one.cached_input_tokens) == (CLAUDE_INPUT, 324979)
