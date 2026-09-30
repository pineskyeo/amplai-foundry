"""D-094 — usage detail, dated price tables and the API-equivalent cost.

The provider breakdown (cache, reasoning) is kept beside the run's 3.0.0 usage; tokens are split
into mutually exclusive buckets per provider before pricing; a price table is chosen by date and
named in the estimate; nothing is priced silently with another model's price.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.agent_drivers.protocol import EventNormalizer, usage_detail
from amplai_foundry.evaluation import pricing
from amplai_foundry.meta_harness.trial_metrics import diff_stats

REPO_TABLES = pricing.DEFAULT_ROOT


def table(root: Path, table_id: str, effective: str, **models: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{table_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "price-table-1",
                "price_table_id": table_id,
                "effective_from": effective,
                "currency": "USD",
                "models": models,
            }
        )
    )
    return path


OPENAI = {"provider": "openai", "input": 4_000_000, "cache_read": 400_000,
          "cache_write": 5_000_000, "output": 20_000_000, "sources": ["https://x"]}  # fmt: skip
ANTHROPIC = {"provider": "anthropic", "input": 2_000_000, "cache_read": 200_000,
             "cache_write_5m": 2_500_000, "cache_write_1h": 4_000_000, "output": 10_000_000,
             "sources": ["https://y"]}  # fmt: skip


# -- the shipped table -------------------------------------------------------------------------


def test_the_shipped_table_prices_both_configured_models() -> None:
    tables = pricing.load_tables(REPO_TABLES)
    current = pricing.table_for(tables, "2026-09-30")
    assert current is not None
    assert {"gpt-5.6-sol", "claude-sonnet-5"} <= set(current.models)
    sol = current.models["gpt-5.6-sol"]
    assert (sol["input"], sol["cache_read"], sol["output"]) == (4_000_000, 400_000, 20_000_000)
    assert all(row["sources"] for row in current.models.values())


# -- normalizer --------------------------------------------------------------------------------


def test_codex_usage_keeps_cache_and_reasoning_and_says_input_includes_cache() -> None:
    n = EventNormalizer("codex")
    n.accept({"type": "thread.started", "thread_id": "t1"})
    n.accept(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 72047,
                "cached_input_tokens": 64128,
                "cache_write_input_tokens": 0,
                "output_tokens": 1138,
                "reasoning_output_tokens": 186,
            },
        }
    )
    assert (n.usage["input_tokens"], n.usage["output_tokens"]) == (72047, 1138)
    assert n.usage_detail == {
        "provider": "codex",
        "input_includes_cache": True,
        "fields": {
            "input_tokens": 72047,
            "cached_input_tokens": 64128,
            "cache_write_input_tokens": 0,
            "output_tokens": 1138,
            "reasoning_output_tokens": 186,
        },
    }


def test_claude_usage_keeps_cache_writes_by_duration_and_says_input_excludes_cache() -> None:
    detail = usage_detail(
        "claude",
        {
            "input_tokens": 12,
            "cache_read_input_tokens": 9000,
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 100},
            "output_tokens": 1500,
            "junk": "ignored",
            "negative": -1,
        },
    )
    assert detail["input_includes_cache"] is False
    assert detail["fields"] == {
        "input_tokens": 12,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 300,
        "output_tokens": 1500,
        "ephemeral_5m_input_tokens": 200,
        "ephemeral_1h_input_tokens": 100,
    }


# -- pricing -----------------------------------------------------------------------------------


def test_codex_cache_is_carved_out_of_input_never_counted_twice(tmp_path: Path) -> None:
    table(tmp_path, "t1", "2026-09-30", **{"gpt-5.6-sol": OPENAI})
    tables = pricing.load_tables(tmp_path)
    detail = usage_detail("codex", {"input_tokens": 72047, "cached_input_tokens": 64128,
                                    "cache_write_input_tokens": 0, "output_tokens": 1138,
                                    "reasoning_output_tokens": 186})  # fmt: skip
    got = pricing.estimate("gpt-5.6-sol", "2026-09-30", detail=detail, usage=None, tables=tables)
    assert got["tokens"] == {"input": 7919, "cache_read": 64128, "cache_write": 0, "output": 1138}
    # 7919*4 + 64128*0.4 + 1138*20 dollars per 1M = 0.080087 USD; reasoning is inside output
    assert got["cost_microunits"] == 80087 and got["upper_bound"] is False


def test_claude_cache_is_added_to_input_by_bucket(tmp_path: Path) -> None:
    table(tmp_path, "t1", "2026-09-30", **{"claude-sonnet-5": ANTHROPIC})
    tables = pricing.load_tables(tmp_path)
    detail = usage_detail("claude", {"input_tokens": 1000, "cache_read_input_tokens": 10000,
                                     "cache_creation_input_tokens": 2000,
                                     "cache_creation": {"ephemeral_5m_input_tokens": 1000,
                                                        "ephemeral_1h_input_tokens": 1000},
                                     "output_tokens": 500})  # fmt: skip
    got = pricing.estimate(
        "claude-sonnet-5", "2026-10-01", detail=detail, usage=None, tables=tables
    )
    # 1000*2 + 10000*0.2 + 1000*2.5 + 1000*4 + 500*10 = 15500 per 1M -> 0.0155 USD
    assert got["cost_microunits"] == 15500 and got["notes"] == []


def test_a_claude_write_without_its_duration_is_priced_as_five_minutes_and_says_so(
    tmp_path: Path,
) -> None:
    table(tmp_path, "t1", "2026-09-30", **{"claude-sonnet-5": ANTHROPIC})
    detail = usage_detail("claude", {"input_tokens": 0, "cache_creation_input_tokens": 1000,
                                     "output_tokens": 0})  # fmt: skip
    got = pricing.estimate(
        "claude-sonnet-5", "2026-10-01", detail=detail, usage=None,
        tables=pricing.load_tables(tmp_path),
    )  # fmt: skip
    assert got["cost_microunits"] == 2500
    assert got["notes"] == ["cache_write_duration_not_reported_priced_as_5m"]


def test_totals_without_a_breakdown_are_an_upper_bound(tmp_path: Path) -> None:
    table(tmp_path, "t1", "2026-09-30", **{"gpt-5.6-sol": OPENAI})
    got = pricing.estimate(
        "gpt-5.6-sol", "2026-09-30", detail=None,
        usage={"input_tokens": 72047, "output_tokens": 1138},
        tables=pricing.load_tables(tmp_path),
    )  # fmt: skip
    assert got["upper_bound"] is True and got["notes"] == ["no_cache_breakdown"]
    assert got["cost_microunits"] == round((72047 * 4_000_000 + 1138 * 20_000_000) / 1e6)


def test_a_model_without_a_price_is_not_priced_with_another(tmp_path: Path) -> None:
    table(tmp_path, "t1", "2026-09-30", **{"gpt-5.6-sol": OPENAI})
    got = pricing.estimate(
        "gpt-9-unknown", "2026-09-30", detail=None,
        usage={"input_tokens": 1, "output_tokens": 1}, tables=pricing.load_tables(tmp_path),
    )  # fmt: skip
    assert got == {"status": "no_price", "model": "gpt-9-unknown", "price_table_id": "t1"}
    before = pricing.estimate(
        "gpt-5.6-sol", "2026-01-01", detail=None,
        usage={"input_tokens": 1, "output_tokens": 1}, tables=pricing.load_tables(tmp_path),
    )  # fmt: skip
    assert before["status"] == "no_table"


def test_a_run_is_priced_with_the_table_in_effect_on_its_day(tmp_path: Path) -> None:
    table(tmp_path, "t1", "2026-09-30", **{"gpt-5.6-sol": OPENAI})
    table(tmp_path, "t2", "2026-11-22", **{"gpt-5.6-sol": {**OPENAI, "input": 5_000_000}})
    tables = pricing.load_tables(tmp_path)
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    old = pricing.estimate("gpt-5.6-sol", "2026-10-15", detail=None, usage=usage, tables=tables)
    new = pricing.estimate("gpt-5.6-sol", "2026-11-22", detail=None, usage=usage, tables=tables)
    assert (old["price_table_id"], old["cost_microunits"]) == ("t1", 4_000_000)
    assert (new["price_table_id"], new["cost_microunits"]) == ("t2", 5_000_000)


@pytest.mark.parametrize(
    "broken",
    [
        {"provider": "openai", "input": 1.5, "cache_read": 0, "cache_write": 0, "output": 0,
         "sources": ["u"]},
        {"provider": "openai", "input": 1, "cache_read": 0, "output": 0, "sources": ["u"]},
        {"provider": "mystery", "input": 1, "sources": ["u"]},
        {**OPENAI, "sources": []},
    ],
)  # fmt: skip
def test_a_malformed_table_stops_loading(tmp_path: Path, broken: dict[str, Any]) -> None:
    table(tmp_path, "t1", "2026-09-30", **{"m": broken})
    with pytest.raises(pricing.PriceTableError):
        pricing.load_tables(tmp_path)


def test_codex_cache_larger_than_input_is_refused(tmp_path: Path) -> None:
    table(tmp_path, "t1", "2026-09-30", **{"gpt-5.6-sol": OPENAI})
    detail = usage_detail("codex", {"input_tokens": 10, "cached_input_tokens": 11,
                                    "output_tokens": 1})  # fmt: skip
    with pytest.raises(pricing.PriceTableError):
        pricing.estimate(
            "gpt-5.6-sol", "2026-09-30", detail=detail, usage=None,
            tables=pricing.load_tables(tmp_path),
        )  # fmt: skip


# -- diff metrics ------------------------------------------------------------------------------

PATCH = b"""diff --git a/demo_app/semver.py b/demo_app/semver.py
new file mode 100644
--- /dev/null
+++ b/demo_app/semver.py
@@ -0,0 +1,2 @@
+def f():
+    return 1
diff --git a/tests/test_semver.py b/tests/test_semver.py
new file mode 100644
--- /dev/null
+++ b/tests/test_semver.py
@@ -0,0 +1 @@
+def test_f(): pass
diff --git a/tests/test_version_report.py b/tests/test_version_report.py
--- a/tests/test_version_report.py
+++ b/tests/test_version_report.py
@@ -1,2 +1,1 @@
-    assert summarize(info) == "x"
-    pass
+    pass
diff --git a/tests/old.py b/tests/old.py
deleted file mode 100644
--- a/tests/old.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""


def test_a_new_test_is_not_a_changed_test() -> None:
    stats = diff_stats(PATCH)
    assert stats["files"] == 4
    assert stats["tests_added"] == ["tests/test_semver.py"]
    assert stats["tests_changed"] == ["tests/old.py", "tests/test_version_report.py"]
    assert (stats["lines_added"], stats["lines_removed"]) == (4, 3)
