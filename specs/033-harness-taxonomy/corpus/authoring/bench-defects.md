# Bench Base Defect Catalogue (authoring only)

For corpus task authors and hidden-test reviewers only. This file is outside `bases/bench/`, so it is never part
of a trial workspace (the workspace is materialized from the bench base commit alone, `sandbox/git_workspace.py`),
and read-only meta turns never run on a copy of this repository (interfaces §0 IC-14).

The S6-base author planted these defects on purpose (interfaces §10.3: a bug task's base holds a subtle defect, and
task authors may not edit the base). Each contradicts its own docstring; the visible tests avoid every trigger.
Confirmed intentional by the orchestrator (2026-10-01). Base commit: `bases/bench.commit`.

## Bug Targets (one defect per `bug-*` task)

| ID | Location | Defect | Correct behaviour (docstring) |
|---|---|---|---|
| D1 | `stockroom/money.py` `parse_money` | loses the sign for amounts between -1 and 0 ('-0.50' → 50) | keep the sign |
| D2 | `stockroom/money.py` `parse_money` | accepts misplaced commas ('1,2,3') | refuse malformed grouping |
| D3 | `stockroom/textutil.py` `truncate` | returns width + len(ellipsis) characters | at most `width` characters |
| D4 | `stockroom/dates.py` `days_in_month` | ignores leap years; `add_months(2024-01-31, 1)` → 2024-02-28 | leap years counted (2024-02-29) |
| D5 | `stockroom/csvio.py` `read_items` | naive split; no CSV quoting, so names with commas and prices ≥ 1,000.00 fail the round trip | read what `write_items` writes |
| D6 | `stockroom/jsonio.py` `order_from_dict` | reads key 'state'; status lost on the JSON round trip | read 'status' |
| D7 | `stockroom/inventory.py` `reserve` | checks on_hand instead of available; repeated reservations over-commit | check available |
| D8 | `stockroom/pricing.py` `unit_price` | '>' instead of '>='; tier not applied at exactly min_quantity | "from min_quantity on" |
| D9 | `stockroom/pricing.py` `apply_discount` | truncates (999 at 15% → 850) | round half up (→ 849) |
| D10 | `stockroom/orders.py` `OrderBook.between`, CLI `sales --to` | end date excluded | both dates included |
| D11 | `stockroom/report.py` `render_table` | header widths ignored when rows exist | columns fit headers and rows |
| D12 | `stockroom/report.py` `top_n` | `n=0` returns everything | empty |
| D13 | `stockroom/config.py` env booleans | `bool(raw)`: `STOCKROOM_LOW_STOCK_WARNING=false` → True (INI path correct) | parse booleans like the INI path |

## Other Domains

- `cli_ops`: D14 — a missing input file or an out-of-range `--discount` gives a traceback instead of
  `stockroom: error: …`. Hidden tests run `python -m stockroom` or `scripts/*.py` with `PYTHONPATH`, never
  `pip install` (no network in trials).
- `data`: D15 — `read_items` / `read_orders` reject a UTF-8 BOM header. Also available: `slugify` drops accented
  letters ('Café' → 'caf').
- `refactor` candidates: `report._cents` duplicates `money.format_money`; `cli._cmd_items` hand-rolls a table
  instead of `report.render_table`; the `COMMANDS` dispatch.

## Must Not Depend On

A task outside `bug` must not require fixing any of D1–D13 (its hidden tests must not touch those triggers), or the
reference would fix an unstated requirement (§10.3 rule 2). Two bug tasks never target the same defect.
