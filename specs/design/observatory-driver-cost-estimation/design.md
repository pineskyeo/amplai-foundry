# Observatory Driver Cost Estimation

## Goal

Every run currently surfaces cost as **unknown** in Observatory, regardless of which
driver produced it. This design answers two questions:

1. Why does `RunRecord.usage.cost_microunits` come back `None` (and
   `usage.status == "unknown"`) for every run today, including runs where the
   Claude CLI driver actually reported a dollar cost on its `result` event?
2. How should the Claude CLI driver's self-reported `total_cost_usd` be parsed,
   converted to integer `cost_microunits`, and recorded into `RunRecord.usage`
   with `status="estimated"` — kept explicitly distinct from provider-settled
   `"measured"` cost — and how should Observatory's per-run cost field and its
   currency-aggregate sums (`cost_by_currency`, `total_cost_microunits`,
   `known_cost_sum_microunits`, `unknown_cost_run_count`) treat `"estimated"`
   vs `"measured"` vs `"unknown"` runs going forward?

This is a documentation-only design. No source, test, schema, or config file is
changed as part of it.

## Current State

### The usage object never carries a cost today, for any driver

`EventNormalizer` (the class that turns raw Claude/Codex stream-JSON events into
the normalized shape the rest of the system trusts) initializes usage with cost
fixed at `None` and status `"unknown"`:

```python
self.usage: dict[str, int | str | None] = {
    "input_tokens": None,
    "output_tokens": None,
    "cost_microunits": None,
    "currency": "USD",
    "status": "unknown",
    "source_ref": None,
}
```
(`src/amplai_foundry/agent_drivers/protocol.py:88-95`)

Its `accept()` method only ever updates `input_tokens`/`output_tokens` and flips
`status` to `"measured"` when both are non-negative integers; `cost_microunits`
is never assigned anywhere in `accept()`:

```python
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
```
(`src/amplai_foundry/agent_drivers/protocol.py:145-154`)

So even a "measured" run today is only token-measured; cost stays `None` forever
via this path. `CliDriver._collect` calls `normalizer.accept(event)` per stream
event and persists `normalizer.usage` into the durable journal on every
observation and again at terminal transition:

```python
def observe(event: dict[str, Any]) -> None:
    ...
    normalized = normalizer.accept(event)
    seq += 1
    self.journal.append(did, f"provider-{seq}", normalized)
    self.journal.update(did, session_handle=normalizer.session, usage=normalizer.usage)
```
(`src/amplai_foundry/agent_drivers/cli.py:315-320`, terminal write at
`src/amplai_foundry/agent_drivers/cli.py:341-350`)

`CliDriver.collect()` then hands that same journal-persisted usage dict back to
the worker unmodified:

```python
def collect(self, handle: str) -> dict[str, Any]:
    record = self.poll(handle)
    ...
    return {
        "provider_completed": True,
        "goal_verified": False,
        "session_handle": record["session_handle"],
        "usage": record.get("usage"),
        "process_stopped": True,
        "event_count": record.get("cursor", 0),
    }
```
(`src/amplai_foundry/agent_drivers/cli.py:475-486`)

The worker takes that usage (or an `UNKNOWN_USAGE` fallback if the receipt has
none) and forwards it to `output_ready` untouched:

```python
usage = receipt.get("usage") or UNKNOWN_USAGE.copy()
...
result = self.runtime.output_ready(
    worker, dispatch["run_id"], ..., usage=usage, process_stopped=True,
)
```
(`src/amplai_foundry/runtime/execution/worker.py:242-258`, `UNKNOWN_USAGE`
defined at `src/amplai_foundry/agent_drivers/ports.py:26-33`)

`output_ready` writes that same usage dict verbatim into the persisted
`RunRecord`:

```python
data = {
    **run["data"],
    "process_stopped": True,
    "record": {**run["data"]["record"], "usage": usage},
}
self._run_state(db, worker.scope, run_id, run, run_state, data)
...
self.budgets.settle(db, worker.scope, run_id, usage)
```
(`src/amplai_foundry/runtime/execution/service.py:872-887`)

**Root cause**: at no point in this chain — `EventNormalizer.accept`,
`CliDriver._collect`/`collect`, `worker.py` receipt handling, or
`execution/service.py:output_ready` — does any code read a cost figure out of
the provider stream and put it into `cost_microunits`. The field is `None` by
construction from `src/amplai_foundry/agent_drivers/protocol.py:88-95` onward and nothing downstream ever
overwrites it. This is true for **every** driver, not just Claude CLI, because
none of them populate `cost_microunits` either (see cross-driver comparison
below) — the gap is upstream of Observatory, not in Observatory's math.

### Claude CLI already reports a cost figure that is silently discarded

The Claude CLI's terminal `result` stream-JSON event carries a top-level
`total_cost_usd` float, separate from the nested `usage.input_tokens` /
`usage.output_tokens` token counts (which themselves carry no cost):

```json
{"duration_api_ms":0,"stop_reason":"stop_sequence","session_id":"...","total_cost_usd":0,
 "usage":{...,"input_tokens":0,"output_tokens":0,...},"type":"result", ...}
```
(`specs/015-external-qualification/artifacts/claude-stream.jsonl:3`)

`EventNormalizer.CLAUDE` accepts `"result"` as a known event type
(`src/amplai_foundry/agent_drivers/protocol.py:74-76`) and does use it to
determine `completed`/`failed` (`src/amplai_foundry/agent_drivers/protocol.py:126-131`), but `accept()` never
reads `event.get("total_cost_usd")`. The figure is parsed by the JSONL decoder,
handed to `accept()`, and dropped on the floor.

By contrast, Codex CLI's stream has no equivalent field at all — its `result`-
shaped completion events (`turn.completed`, `item.completed`, etc., in
`specs/015-external-qualification/artifacts/codex-stream.jsonl`) carry no cost
figure of any kind. This is why the fix under discussion is inherently
Claude-CLI-specific: there is nothing analogous to parse for Codex CLI, and
`status="unknown"` is the correct terminal answer there today.

### The schema already models exactly the distinction needed

`common.schema.json`'s `usage` definition already has the three-way status
enum and a provenance ref, unchanged and sufficient for this design:

```json
"status": {
  "enum": ["measured", "estimated", "unknown"]
},
"source_ref": {
  "anyOf": [
    {"$ref": "https://schemas.amplai.invalid/v3/common.schema.json#/$defs/ref"},
    {"type": "null"}
  ]
}
```
(`src/amplai_foundry/runtime/contracts/data/schemas/common.schema.json:211-227`,
full `usage` object at lines 168-238)

No schema change is required to record a Claude-CLI-derived estimate: `status`
already permits `"estimated"` alongside `"measured"`/`"unknown"`, and
`source_ref` (a `$ref`-shaped `{id, revision, digest}` object, defined at
`src/amplai_foundry/runtime/contracts/data/schemas/common.schema.json:39-54`) already exists to record *where* an estimate came
from, e.g. a versioned reference to the CLI's internal price table or to the
`result` event's digest.

### Observatory already treats "estimated" distinctly from "unknown" — correctly

`Observatory.summary()` builds a per-currency aggregate. A run only lands in
`unknown_runs` if its status is not `measured`/`estimated`, or its
`cost_microunits` is not a valid non-negative int:

```python
status, cost = usage["status"], usage["cost_microunits"]
if status in {"measured", "estimated"} and type(cost) is int and cost >= 0:
    item[status + "_microunits"] += cost
    item[status + "_runs"] += 1
else:
    item["unknown_runs"] += 1
```
(`src/amplai_foundry/evaluation/observatory.py:163-168`)

But the same per-currency block deliberately keeps `total_cost_microunits`
`None` whenever *any* run in that currency is estimated or unknown — it only
collapses to a hard number when the currency's cost is fully settled:

```python
for item in currencies.values():
    item["total_cost_microunits"] = (
        item["measured_microunits"]
        if item["unknown_runs"] == 0 and item["estimated_runs"] == 0
        else None
    )
```
(`src/amplai_foundry/evaluation/observatory.py:169-174`)

The top-level fields returned by `summary()` mirror this:
`cost_by_currency` exposes the full per-status breakdown,
`known_cost_sum_microunits` sums measured+estimated across currencies (but only
when there is a single currency), `unknown_cost_run_count` counts only the
truly-unknown runs, and `total_cost_microunits` is the single-currency "fully
settled" total:

```python
"cost_by_currency": currencies,
"known_cost_sum_microunits": known_cost if len(currencies) <= 1 else None,
"unknown_cost_run_count": sum(x["unknown_runs"] for x in cost_values),
"total_cost_microunits": cost_values[0]["total_cost_microunits"]
if len(cost_values) == 1 else None,
```
(`src/amplai_foundry/evaluation/observatory.py:247-252`)

This behavior is already covered by tests and passes today for a hand-crafted
`usage` dict with `status="estimated"` and a real `cost_microunits`:

```python
@pytest.mark.parametrize("status,cost", [("unknown", None), ("estimated", 70), ("measured", None)])
def test_dev03_unknown_or_estimated_cost_never_becomes_actual_total(...):
    ...
    assert v["total_cost_microunits"] is None
    assert v["cost_by_currency"]["USD"]["total_cost_microunits"] is None
    assert v["unknown_cost_run_count"] == (0 if status == "estimated" else 1)
```
(`tests/v3/test_dev03_observatory.py:57-74`)

In other words: **Observatory's aggregation logic is already correct and needs
no change** to support estimated CLI cost. It has simply never received a run
whose `usage.status` is `"estimated"` with a non-null `cost_microunits`,
because no driver code path produces one — confirmed directly by
`tests/v3/test_dev02_drivers.py:93`, which asserts
`record["usage"]["cost_microunits"] is None` even for a token-measured CLI run.

This split — a well-modeled schema/aggregation layer waiting on unpopulated
upstream data — is also how the DEV-03 delivery describes the intended
semantics: "cost is separated into measured/estimated/unknown per currency;
missing reports are not folded into zero, and different currencies are not
summed" (`docs/v3/DEV03_OBSERVATORY_META.ko.md:34`), and the original design
note for Observatory metrics defines the `cost` row as "provider-settled usage
or bounded estimate/unknown; unreported usage is not treated as zero cost"
(`design-reference/design/15_EVAL_OBSERVATORY.md:28`). Both predate any actual
producer of `estimated` cost.

### Budget settlement has a matching inconsistency, but it's out of scope to fix here

`BudgetService.settle()` only trusts `status == "measured"` to move a
reservation's `cost` away from its pre-reserved ceiling value:

```python
known = (
    usage.get("status") == "measured"
    and usage.get("input_tokens") is not None
    and usage.get("output_tokens") is not None
)
...
cost = (
    usage.get("cost_microunits")
    if known and usage.get("cost_microunits") is not None
    else row["cost"]
)
...
status = "settled" if known and usage.get("cost_microunits") is not None else "unknown"
```
(`src/amplai_foundry/runtime/budgets/service.py:94-107`)

Because `known` requires `status == "measured"`, an `"estimated"` cost is
treated exactly like an `"unknown"` one for settlement purposes: the
reservation keeps its original pre-flight ceiling `cost`/`tokens` and its
status stays `"unknown"` rather than `"settled"`. This is intentional caution
(estimated cost should not silently authorize spend), but it means adding
`estimated` cost to `RunRecord.usage` will **not** by itself improve budget
accounting — that's a distinct, deliberately out-of-scope change addressed
under Risks/Implementation Plan.

### Cross-driver comparison (context, not objects of this design)

- `RecipePort`/`LocalDriver` (pure local recipes) report a real, honest
  zero-cost **measured** usage because there is no external provider call:
  `ports.py` defines `ZERO_USAGE` as `UNKNOWN_USAGE` with all-zero
  tokens/cost and `status="measured"` (`src/amplai_foundry/agent_drivers/ports.py:26-40`),
  and `local.py` constructs that same shape inline before calling
  `output_ready` (`src/amplai_foundry/agent_drivers/local.py:60-67`).
- `OpenCodeDriver.collect()` always returns `"usage": None`
  (`src/amplai_foundry/agent_drivers/http.py:402`), which the worker falls back
  to `UNKNOWN_USAGE` for — correctly `"unknown"`, not a design gap.
- `ResponsesDriver` and `CodexAppServerDriver` store **raw, non-normalized**
  provider usage payloads (`result.get("usage")` at
  `src/amplai_foundry/agent_drivers/http.py:512`, and
  `params.get("tokenUsage", {})` at
  `src/amplai_foundry/agent_drivers/codex_app_server.py:294`, forwarded again at
  `src/amplai_foundry/agent_drivers/codex_app_server.py:462`). These payloads do
  not conform to the `usage` schema's fixed key set
  (`input_tokens`/`output_tokens`/`cost_microunits`/`currency`/`status`/`source_ref`)
  and would fail `run-record` schema validation if routed through the same
  `output_ready` path with `additionalProperties: false` in effect
  (`src/amplai_foundry/runtime/contracts/data/schemas/common.schema.json:229-238`). Fixing that is a separate, driver-specific
  problem and is out of scope here.
- Codex CLI's stream fixture has no `total_cost_usd`-equivalent field
  (`specs/015-external-qualification/artifacts/codex-stream.jsonl`), so Codex
  CLI runs will legitimately remain `status="unknown"` after this design is
  implemented.

## Options

Both options assume the schema is unchanged (`measured`/`estimated`/`unknown`
stays as-is) and both target only the Claude CLI path.

### Option A — Convert in `EventNormalizer.accept()` (protocol layer)

Read `event.get("total_cost_usd")` inside `accept()` when `provider == "claude"`
and `kind == "result"`, convert USD float to integer microunits (e.g.
`round(total_cost_usd * 1_000_000)`), and set
`status="estimated"`/`source_ref` pointing at the event digest — but only when
`cost_microunits` is not already `"measured"`-quality (it never is for Claude
CLI, since Claude's `usage` block carries no cost).

- **Pro**: Single choke point; `CliDriver`, worker, and `output_ready` need no
  changes; the journal and every consumer of `normalizer.usage` gets the
  estimate "for free," identically to how `status="measured"` already flows
  today for tokens.
- **Pro**: Keeps the parsing logic colocated with the only code that already
  understands the Claude vs. Codex stream dialect split
  (`src/amplai_foundry/agent_drivers/protocol.py:74-76`, `src/amplai_foundry/agent_drivers/protocol.py:99`), so provider-specific quirks (float precision,
  `total_cost_usd` sometimes `0` for a failed/auth-rejected turn as in the
  fixture) are handled in one place.
- **Con**: `EventNormalizer` currently only *merges* usage fields
  (`src/amplai_foundry/agent_drivers/protocol.py:149-154`); introducing a second independent trigger
  (`total_cost_usd` vs. `usage.{input,output}_tokens`) means the two halves of
  `status` (tokens measured, cost estimated) can now disagree in one dict —
  e.g. `status="measured"` from tokens but cost was actually estimated. The
  current schema only has one `status` field for the whole usage object, so
  this option must decide a priority rule (see Decision).
- **Budget interaction**: unchanged from today's `settle()` behavior — since
  `status` would need to stay `"estimated"` (not `"measured"`) for the cost
  component, `BudgetService.settle()`'s `known` check
  (`src/amplai_foundry/runtime/budgets/service.py:94-98`) still treats it as unsettled. No settlement
  logic needs to move, but the inconsistency (estimated cost known to
  Observatory but invisible to budget enforcement) persists.

### Option B — Convert in `CliDriver._collect`/`collect()` (driver layer)

Leave `EventNormalizer` token-only, and instead have `CliDriver._collect`
inspect the raw `event` dict (or keep a running `last_total_cost_usd` local to
the collector thread) when `self.provider == "claude"` and the event is a
`result` event, then merge a converted `cost_microunits`/`status="estimated"`
into `normalizer.usage` before it's journaled at
`src/amplai_foundry/agent_drivers/cli.py:320`/`src/amplai_foundry/agent_drivers/cli.py:341-350`.

- **Pro**: Keeps `EventNormalizer` provider-agnostic in its *output shape* (it
  still only knows about tokens); the USD→microunits conversion and Claude-only
  special-casing live in `CliDriver`, which already has an explicit
  `self.provider` branch for CLI argv construction (`src/amplai_foundry/agent_drivers/cli.py:107-145`) — so this
  keeps a pattern already established in that file rather than adding a new
  branch inside the shared normalizer used by both providers.
  argument-building logic already lives.
- **Con**: `_collect`'s `observe()` closure (`src/amplai_foundry/agent_drivers/cli.py:315-320`) currently just
  forwards whatever `normalizer.accept()` returns; this option requires it (or
  a wrapper) to also read the *original* `event` dict for `total_cost_usd`,
  duplicating some of the event-shape knowledge (`kind == "result"`) that
  `EventNormalizer` already encapsulates — two places now know what a Claude
  `result` event looks like instead of one.
- **Con**: `CliDriver` is shared by both `ClaudeCodeDriver` and
  `CodexCliDriver` (`src/amplai_foundry/agent_drivers/cli.py:501-513`); this option pushes a Claude-only branch
  into a class that's supposed to be provider-parametrized, whereas Option A's
  branch point (`EventNormalizer`) already *is* provider-specific by
  construction (`self.provider` stored at construction, `src/amplai_foundry/agent_drivers/protocol.py:78-86`).
- **Budget interaction**: identical to Option A — `settle()` is unaffected
  either way, since the fix is entirely about where `cost_microunits`/`status`
  get set on the `usage` dict before it ever reaches `budgets/service.py`.

Both options leave `src/amplai_foundry/runtime/budgets/service.py:94-107` as today: `estimated` remains
"not `known`" for settlement. Whether that's the right long-term budget
behavior is a separate question (see Risks / Implementation Plan) — it is not
part of choosing between A and B, since either option produces the same
`usage` dict shape by the time it reaches `output_ready`.

## Decision

**Recommend Option A**: convert `total_cost_usd` inside
`EventNormalizer.accept()`, scoped to `provider == "claude"` and
`kind == "result"`.

Concretely (described at the level the Implementation Plan below expands on,
no code written here):

- When a Claude `result` event carries a numeric `total_cost_usd`, compute
  `cost_microunits = round(total_cost_usd * 1_000_000)` and set
  `usage["cost_microunits"]` to that integer, `usage["currency"] = "USD"`
  (Claude CLI's cost figure is always USD; there's no currency field on the
  event to trust otherwise), and `usage["source_ref"]` to a `ref`-shaped
  pointer identifying this normalization (e.g. digest of the raw `result`
  event, or a versioned constant identifying "claude-cli self-reported
  price"), per the existing `ref` shape at `src/amplai_foundry/runtime/contracts/data/schemas/common.schema.json:39-54`.
- **Status priority rule**: cost-derived `"estimated"` must never overwrite or
  be overwritten by token-derived `"measured"` in a way that mislabels the
  cost figure as settled. Since Claude CLI never supplies a provider-settled
  cost (only a self-computed one from an internal, unauthenticated price
  table), the combined status for any Claude CLI run that has a
  `total_cost_usd`-derived cost must be `"estimated"`, never `"measured"` —
  even though the *token* counts in the same event may independently satisfy
  the existing `"measured"` token check at `src/amplai_foundry/agent_drivers/protocol.py:146-148`. This is a
  deliberate, explicit divergence from today's single combined `status`
  field's implicit assumption that token-measured and cost-known always
  co-occur at the same trust level. The design does not propose splitting
  `status` into separate token/cost statuses (that would be a schema change);
  instead, the recommendation is that the *whole* `usage.status` for a Claude
  CLI run with a parsed `total_cost_usd` should read `"estimated"`, on the
  reasoning that the operator's primary use of `status` is "can I trust
  `cost_microunits`," and cost is the field this design is about. Token counts
  remain exact/measured in fact; `status="estimated"` here describes the
  record's cost trustworthiness, matching the explicit instruction that a
  CLI-self-reported figure must never be labeled `"measured"`.
- No change to `common.schema.json` — `status` enum and `source_ref` already
  support exactly this (`src/amplai_foundry/runtime/contracts/data/schemas/common.schema.json:211-227`).

**Observatory changes: none required.** `cost_by_currency`,
`known_cost_sum_microunits`, `unknown_cost_run_count`, and per-currency
`total_cost_microunits` (`src/amplai_foundry/evaluation/observatory.py:150-252`) already implement the
correct semantics today and are already covered by
`tests/v3/test_dev03_observatory.py:57-74`:

- `cost_by_currency["USD"].estimated_microunits`/`estimated_runs` will start
  being populated once Claude CLI runs carry `status="estimated"` with a real
  `cost_microunits` — no code change, this falls directly out of the existing
  `if status in {"measured", "estimated"} and type(cost) is int...` branch
  (`src/amplai_foundry/evaluation/observatory.py:163-166`).
- `unknown_cost_run_count` will correctly stop counting Claude CLI runs once
  they're estimated — again no code change, just newly-true input data.
- **`total_cost_microunits` must stay `None`** for any currency/scope that
  contains estimated (or unknown) runs, exactly as today
  (`src/amplai_foundry/evaluation/observatory.py:169-174`). This design explicitly does **not** propose
  promoting estimated cost into that single confirmed-total number — an
  estimate from an internal, unauthenticated CLI price table is not the same
  epistemic guarantee as `"measured"`, and the whole point of keeping
  `"estimated"` distinct from `"measured"` is that Observatory callers who read
  `total_cost_microunits` as "the confirmed bill" must not be shown a number
  that silently includes guesses. This existing aggregate behavior is kept
  as-is, by design.
- `known_cost_sum_microunits` already sums measured+estimated
  (`src/amplai_foundry/evaluation/observatory.py:176`, `src/amplai_foundry/evaluation/observatory.py:247`) — this is the correct place for Claude CLI's
  estimated spend to become visible in aggregate, and it requires no change.

**Budget settlement (`budgets/service.py`)**: left as `"estimated" == not
known` for this design's scope, per the explicit instruction that this design
addresses the inconsistency only as a risk/option, not as an implemented or
recommended code change. See Risks for the reasoning on why that gap should
not be silently closed as a side effect of this work.

## Risks

- **Price-table staleness/drift**: `total_cost_usd` is Claude CLI's own
  internal, unauthenticated estimate of spend (per-model, per-token pricing
  baked into the CLI binary at `self.version`, `src/amplai_foundry/agent_drivers/cli.py:59`). If Anthropic
  changes pricing and the pinned CLI `version` is not updated, the emitted
  `total_cost_usd` — and therefore the derived `cost_microunits` — silently
  drifts from real billing with no signal in the record itself. `source_ref`
  should record enough (e.g. the CLI `version` string already tracked at
  `src/amplai_foundry/agent_drivers/cli.py:59`) that a later audit can at least identify which price
  table an estimate came from.
- **Currency assumption**: this design assumes Claude CLI's `total_cost_usd`
  is always USD (the field name implies it, and the fixture shows no currency
  field to contradict it). If Anthropic ever emits cost in another currency
  under the same field name, hardcoding `currency="USD"` would silently
  mislabel it — Observatory's per-currency split (`src/amplai_foundry/evaluation/observatory.py:150-176`)
  would then aggregate wrongly-labeled figures without any integrity finding,
  since currency mismatches aren't currently detected as anomalies.
- **Budget-enforcement inconsistency (carried over, not fixed)**: once
  `RunRecord.usage.status="estimated"` starts appearing with real
  `cost_microunits` for Claude CLI runs, an operator who only skims
  `known_cost_sum_microunits` may believe spend is being tracked precisely
  enough to enforce — but `BudgetService.settle()` (`src/amplai_foundry/runtime/budgets/service.py:94-107`)
  still won't move a reservation's `cost`/`status` off its pre-flight ceiling
  for those runs, because `known` requires `status == "measured"`. This design
  intentionally leaves that gap in place (closing it is a separate, larger
  decision about whether estimated cost should ever be trusted to *release*
  reserved budget, which risks under-reserving future spend on price-table
  drift). Any future change to `settle()` to also honor `"estimated"` must
  weigh that risk explicitly; it should not be a silent side effect of this
  design's rollout.
- **Cross-driver inconsistency remains**: after this change, only Claude CLI
  runs gain estimated cost. Codex CLI (no cost field in its stream at all),
  `OpenCodeDriver` (`usage=None` always, `src/amplai_foundry/agent_drivers/http.py:402`), and
  `ResponsesDriver`/`CodexAppServerDriver` (raw non-normalized usage,
  `src/amplai_foundry/agent_drivers/http.py:512`, `src/amplai_foundry/agent_drivers/codex_app_server.py:294`/`src/amplai_foundry/agent_drivers/codex_app_server.py:462`) all remain `"unknown"` or
  schema-nonconforming. Dashboards or reports that compare "cost per driver"
  across a fleet will show Claude CLI as the only driver with any non-`unknown`
  cost, which could be misread as "Claude CLI is more expensive" rather than
  "Claude CLI is the only one currently instrumented." This should be called
  out wherever Observatory's per-driver cost slice (`slices["driver"]`,
  `src/amplai_foundry/evaluation/observatory.py:214-215`, combined with `cost_by_currency`) is surfaced to
  operators.
- **`round()` boundary behavior**: `total_cost_usd` is a float; converting to
  integer microunits via multiply-and-round is lossy at the sub-microunit
  level (negligible in practice, since 1 microunit = 1e-6 currency unit, but
  worth noting since the schema enforces `cost_microunits` as a plain
  non-negative integer with no fractional remainder tracked,
  `src/amplai_foundry/runtime/contracts/data/schemas/common.schema.json:195-206`).

## Implementation Plan

For a future implementation (not performed here):

1. **`src/amplai_foundry/agent_drivers/protocol.py`** — extend
   `EventNormalizer.accept()` (the same branch region as
   `src/amplai_foundry/agent_drivers/protocol.py:145-154`) to also inspect `event.get("total_cost_usd")` when
   `self.provider == "claude"` and `kind == "result"`; compute
   `cost_microunits`, set `currency="USD"`, `status="estimated"`, and populate
   `source_ref`. Decide and implement the status-priority rule from the
   Decision section (cost-estimated must not be reported as `"measured"`).
2. **`src/amplai_foundry/agent_drivers/cli.py`** — no change expected under
   Option A; confirm `_collect`'s `observe()` (`src/amplai_foundry/agent_drivers/cli.py:315-320`) and terminal
   `journal.transition` (`src/amplai_foundry/agent_drivers/cli.py:341-350`) continue to pass `normalizer.usage`
   through unchanged, and that `collect()` (`src/amplai_foundry/agent_drivers/cli.py:475-486`) still returns it
   verbatim.
3. **`tests/v3/test_dev02_drivers.py`** — update/extend the existing assertion
   at `tests/v3/test_dev02_drivers.py:93` (currently asserts
   `cost_microunits is None`) to cover a Claude `result` event carrying
   `total_cost_usd`, asserting the new `estimated` status and converted
   microunits; add a case confirming Codex CLI (no cost field) still yields
   `status="unknown"`.
4. **`tests/v3/test_dev03_observatory.py`** — add a case exercising a
   real (non-synthetic) Claude-CLI-shaped run to confirm the existing
   aggregation in `src/amplai_foundry/evaluation/observatory.py:150-252` behaves as this design predicts
   end-to-end, complementing the already-passing synthetic-usage test at
   `tests/v3/test_dev03_observatory.py:57-74`.
5. **`src/amplai_foundry/runtime/budgets/service.py`** — as a *separate*,
   explicitly-scoped follow-up (not bundled into the above), revisit `settle()`
   (`src/amplai_foundry/runtime/budgets/service.py:80-116`) to decide whether/how `"estimated"` cost
   should affect reservation settlement, per the Risks section. This design
   recommends that follow-up be its own decision, not an automatic
   consequence of populating `cost_microunits` for Claude CLI.
6. **Documentation** — once implemented, update
   `docs/v3/DEV03_OBSERVATORY_META.ko.md:34` and any adjacent delivery notes to
   state that Claude CLI now produces `estimated` cost, and that Codex
   CLI/OpenCode/Responses/CodexAppServer remain `unknown` pending separate work.

## Sources

- `src/amplai_foundry/agent_drivers/protocol.py:88-95` — `EventNormalizer`'s
  default usage dict (`cost_microunits: None`, `status: "unknown"`).
- `src/amplai_foundry/agent_drivers/protocol.py:145-154` — `accept()`'s only
  usage mutation, token-only, never touches `cost_microunits`.
- `src/amplai_foundry/agent_drivers/cli.py:315-320` — `_collect`'s `observe()`
  journaling `normalizer.usage` per event.
- `src/amplai_foundry/agent_drivers/cli.py:341-350` — terminal journal
  transition also carrying `usage=normalizer.usage`.
- `src/amplai_foundry/agent_drivers/cli.py:475-486` — `CliDriver.collect()`
  returning the journaled usage unchanged to the worker.
- `src/amplai_foundry/runtime/execution/worker.py:242` — worker falling back to
  `UNKNOWN_USAGE` and forwarding usage to `output_ready`.
- `src/amplai_foundry/runtime/execution/service.py:872-887` — `output_ready`
  persisting `usage` into `RunRecord` and calling `budgets.settle`.
- `src/amplai_foundry/runtime/contracts/data/schemas/common.schema.json:168-238` —
  full `usage` schema object.
- `src/amplai_foundry/runtime/contracts/data/schemas/common.schema.json:211-227` —
  `status` enum (`measured`/`estimated`/`unknown`) and `source_ref`.
- `specs/015-external-qualification/artifacts/claude-stream.jsonl:3` — Claude
  CLI `result` event with top-level `total_cost_usd` and nested
  cost-free `usage.input_tokens`/`output_tokens`.
- `specs/015-external-qualification/artifacts/codex-stream.jsonl` — Codex CLI
  stream with no cost-equivalent field, contrasted with Claude's.
- `src/amplai_foundry/evaluation/observatory.py:150-176` — per-currency cost
  aggregation loop and per-currency `total_cost_microunits` collapse rule.
- `src/amplai_foundry/evaluation/observatory.py:247-252` — `summary()`'s
  exposed cost fields (`cost_by_currency`, `known_cost_sum_microunits`,
  `unknown_cost_run_count`, `total_cost_microunits`).
- `src/amplai_foundry/control_plane/api_v3/server.py:725-750` — `/api/v3/metrics`
  endpoint exposing `Observatory.summary()` verbatim.
- `src/amplai_foundry/runtime/budgets/service.py:94-107` — `settle()`'s
  `known`/`status` logic, which only trusts `status=="measured"`.
- `src/amplai_foundry/agent_drivers/ports.py:26-40` — `UNKNOWN_USAGE`/`ZERO_USAGE`
  constants shared across drivers.
- `src/amplai_foundry/agent_drivers/local.py:60-67` — `RecipePort`/local driver
  reporting genuine zero-cost `measured` usage.
- `src/amplai_foundry/agent_drivers/http.py:402` — `OpenCodeDriver.collect()`
  always returning `usage: None`.
- `src/amplai_foundry/agent_drivers/http.py:512` — `ResponsesDriver` storing raw
  provider usage.
- `src/amplai_foundry/agent_drivers/codex_app_server.py:294` and `src/amplai_foundry/agent_drivers/codex_app_server.py:462` —
  `CodexAppServerDriver` storing/forwarding raw `tokenUsage`.
- `tests/v3/test_dev03_observatory.py:57-74` — existing test proving Observatory
  already treats `estimated`-with-cost as distinct from `unknown`.
- `tests/v3/test_dev02_drivers.py:93` — existing test proving no driver path
  today ever produces a non-`None` `cost_microunits`.
- `docs/v3/DEV03_OBSERVATORY_META.ko.md:34` — delivery note describing the
  intended measured/estimated/unknown cost split.
- `design-reference/design/15_EVAL_OBSERVATORY.md:28` — original design intent
  for the `cost` metric row.
