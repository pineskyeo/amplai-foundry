"""API-equivalent cost of a run from its token usage and a dated price table (D-094).

What a run would have cost at the provider's published API prices. It is an estimate beside the
run, never the run's ``usage.cost_microunits``: a subscription run is not billed per token (D-085).

Price tables live in ``deployment/prices/<id>.json``. A table is never edited after it is added; a
new price is a new table with a later ``effective_from``. A run is priced with the latest table in
effect on its date, and the estimate names that table, so an old number can always be explained.

Tokens are split into mutually exclusive buckets before pricing, because providers count cache
differently (the double-counting mistake is common):

- OpenAI/Codex: ``input_tokens`` already contains the cached reads and the cache writes; the
  uncached part is ``input - cached - cache_write``. Reasoning tokens are part of ``output``.
- Anthropic: ``input_tokens`` excludes cache; reads and writes are added as their own buckets.

A model with no price, or usage without a breakdown, is reported as such. Nothing falls back
silently to another price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PER = 1_000_000
REQUIRED = {
    "openai": ("input", "cache_read", "cache_write", "output"),
    "anthropic": ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output"),
}
DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "deployment" / "prices"


class PriceTableError(ValueError):
    pass


@dataclass(frozen=True)
class PriceTable:
    table_id: str
    effective_from: str
    models: dict[str, dict[str, Any]]
    path: str


def load_tables(root: Path = DEFAULT_ROOT) -> list[PriceTable]:
    """Every table under ``root``, oldest first. A malformed table stops loading."""
    tables = []
    for path in sorted(Path(root).glob("*.json")):
        value = json.loads(path.read_text())
        if value.get("schema_version") != "price-table-1" or value.get("currency") != "USD":
            raise PriceTableError(f"{path.name}: unknown table version or currency")
        if value.get("price_table_id") != path.stem:
            raise PriceTableError(f"{path.name}: price_table_id must equal the file name")
        for model, row in value["models"].items():
            fields = REQUIRED.get(row.get("provider", ""))
            if fields is None:
                raise PriceTableError(f"{path.name}: {model} has an unknown provider")
            if any(type(row.get(k)) is not int or row[k] < 0 for k in fields):
                raise PriceTableError(f"{path.name}: {model} needs integer prices {fields}")
            if not row.get("sources"):
                raise PriceTableError(f"{path.name}: {model} needs its source URLs")
        tables.append(PriceTable(value["price_table_id"], value["effective_from"],
                                 value["models"], str(path)))  # fmt: skip
    if len({t.effective_from for t in tables}) != len(tables):
        raise PriceTableError("Two tables take effect on the same date")
    return sorted(tables, key=lambda t: t.effective_from)


def table_for(tables: list[PriceTable], day: str) -> PriceTable | None:
    """The latest table in effect on ``day`` (YYYY-MM-DD)."""
    usable = [t for t in tables if t.effective_from <= day]
    return usable[-1] if usable else None


def buckets(detail: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
    """Mutually exclusive token buckets from a usage-detail record, and what was assumed."""
    fields, notes = detail.get("fields") or {}, []
    if detail.get("provider") == "codex":
        total = fields.get("input_tokens")
        if total is None or fields.get("output_tokens") is None:
            raise PriceTableError("The breakdown has no input or output count")
        cached = fields.get("cached_input_tokens")
        if cached is None:
            cached = 0
            notes.append("no_cache_breakdown")  # priced as all uncached: an upper bound
        written = fields.get("cache_write_input_tokens", 0)
        uncached = total - cached - written
        if uncached < 0:
            raise PriceTableError("Cached and written tokens exceed the input count")
        return {"input": uncached, "cache_read": cached, "cache_write": written,
                "output": fields["output_tokens"]}, notes  # fmt: skip
    if detail.get("provider") == "claude":
        if fields.get("input_tokens") is None or fields.get("output_tokens") is None:
            raise PriceTableError("The breakdown has no input or output count")
        created = fields.get("cache_creation_input_tokens", 0)
        five = fields.get("ephemeral_5m_input_tokens")
        hour = fields.get("ephemeral_1h_input_tokens")
        if five is None and hour is None:
            five, hour = created, 0
            if created:
                notes.append("cache_write_duration_not_reported_priced_as_5m")
        return {"input": fields["input_tokens"],
                "cache_read": fields.get("cache_read_input_tokens", 0),
                "cache_write_5m": five or 0, "cache_write_1h": hour or 0,
                "output": fields["output_tokens"]}, notes  # fmt: skip
    raise PriceTableError("Unknown provider in the usage detail")


def estimate(
    model: str,
    day: str,
    *,
    detail: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    tables: list[PriceTable],
) -> dict[str, Any]:
    """The API-equivalent cost of one run, or why there is none."""
    table = table_for(tables, day)
    if table is None:
        return {"status": "no_table", "day": day}
    row = table.models.get(model)
    if row is None:
        return {"status": "no_price", "model": model, "price_table_id": table.table_id}
    notes: list[str] = []
    if detail is not None:
        tokens, notes = buckets(detail)
    elif usage and usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
        # an older run: only the totals; every input token at the uncached rate (upper bound)
        tokens = {"input": usage["input_tokens"], "output": usage["output_tokens"]}
        notes = ["no_cache_breakdown"]
    else:
        return {"status": "no_usage", "model": model, "price_table_id": table.table_id}
    cost = sum(count * row[bucket] for bucket, count in tokens.items() if count)
    return {
        "status": "estimated",
        "cost_microunits": round(cost / PER),
        "currency": "USD",
        "model": model,
        "price_table_id": table.table_id,
        "tokens": tokens,
        "notes": notes,
        "upper_bound": "no_cache_breakdown" in notes,
    }
