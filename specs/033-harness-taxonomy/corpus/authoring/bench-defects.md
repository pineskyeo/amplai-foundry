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
| D16 | `stockroom/dates.py` `parse_date` (+ `cli._cmd_sales` truthiness) | `date.fromisoformat` on Python 3.11 also accepts `20240309`, `2024-W10-6`, `2024W106`; `sales --from ""` / `--to ""` is ignored instead of refused | "`"2024-03-09"` -> `date(2024, 3, 9)`; anything else is a `ParseError`" (dates.py:13); README:32 `YYYY-MM-DD` |
| D17 | `stockroom/csvio.py` `read_orders` | a row with fewer cells than the header, or a whitespace-only line, gives `None` cells and an `AttributeError` traceback; extra cells are silently dropped | data errors end in exit 1 with `stockroom: error: …` (cli.py:3, README:37-38, errors.py:1,5) |
| D18 | `stockroom/csvio.py` `read_items` / `read_stock` / `read_orders` | `line N` in messages counts kept rows, not physical lines (blank lines, quoted line breaks, DictReader skipping) | the messages name a line (`line {number}`, csvio.py:27,47,88) |
| D19 | `stockroom/jsonio.py` `order_from_dict` | non-string `placed`/`order_id` raise `AttributeError` (not `ParseError`); non-string customer, float/bool quantity or unit price, `"lines": ""`/`{}` are accepted | bad input raises `StockroomError`/`ParseError` (errors.py:5,9; jsonio.py:43-44 wraps bad objects) |
| D20 | `stockroom/config.py` `_from_file` | `ConfigParser` interpolation: a `%` in an INI value raises `InterpolationSyntaxError` (traceback); `%%` → `%`, `%(key)s` is substituted | settings errors are `ConfigError` → exit 1 (errors.py:17, cli.py:3); values are plain settings text |
| D21 | `stockroom/dates.py` `months_between` | `add_months` past 9999-12 → `ValueError: year 10000 is out of range` for any range ending in December 9999 | "The month keys from `start`'s month to `end`'s month, both included" (dates.py:50) |
| D22 | `stockroom/csvio.py` `read_orders` | only the first row of an order is used: later rows' customer/placed/status are never compared, a later `placed` is never parsed | "rows of one order share `order_id`, `customer`, `placed` and `status`" (csvio.py:95-96); bad dates are `ParseError` (errors.py:9) |
| D23 | `stockroom/csvio.py` `write_orders` / `read_orders` | an `Order` without lines (the model default) writes no row and vanishes on the CSV round trip | "The order file for `orders` (`read_orders` reads it back)" (csvio.py:127) |

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

The same holds for D16–D23 (added 2026-10-11 by the hard-wave-1 bug author): a task outside `bug` must not require
fixing them, so its hidden tests avoid their triggers — dates other than `YYYY-MM-DD` (e.g. `20240309`, week dates)
or an empty `--from`/`--to`; orders rows with too few or too many cells, or whitespace-only lines in an orders file;
expected `line N` numbers in files with blank lines or quoted line breaks; non-string or float/bool values in JSON
orders; `%` in INI values; `months_between` ranges reaching December 9999; rows of one order that disagree on
customer, `placed` or status; orders without lines through `write_orders`/`read_orders`.
