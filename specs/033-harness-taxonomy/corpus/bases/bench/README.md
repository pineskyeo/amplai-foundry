# stockroom

A small inventory and order ledger for a shop: an item catalogue, stock levels, orders, pricing
and text reports, with a command line.

Standard library only; Python 3.11 or newer.

## Layout

| Module | What it does |
|---|---|
| `stockroom/money.py` | amounts as integer cents: parse, format, split evenly |
| `stockroom/textutil.py` | slugs, truncation, alignment, plurals |
| `stockroom/dates.py` | ISO dates, month keys, month arithmetic |
| `stockroom/models.py` | `Item`, `OrderLine`, `Order` records and their validation |
| `stockroom/csvio.py` | read and write items, stock levels and orders as CSV |
| `stockroom/jsonio.py` | orders as JSON |
| `stockroom/inventory.py` | on-hand, reserved and available stock |
| `stockroom/pricing.py` | quantity tiers, discounts, tax, order totals |
| `stockroom/orders.py` | an order book: lookup, date ranges, revenue per month |
| `stockroom/report.py` | text tables, rankings, the sales summary |
| `stockroom/config.py` | settings from an INI file and `STOCKROOM_*` environment variables |
| `stockroom/cli.py` | the `stockroom` command |
| `scripts/make_sample_data.py` | writes a seeded sample data set |
| `data/` | a small sample data set |

## Command Line

```bash
python -m stockroom items --file data/items.csv [--tag TAG]
python -m stockroom stock --items data/items.csv --stock data/stock.csv
python -m stockroom sales --orders data/orders.csv [--from YYYY-MM-DD] [--to YYYY-MM-DD]
python -m stockroom price --items data/items.csv --sku SKU --quantity N [--discount PERCENT]
python -m stockroom --version
```

Global option `--config PATH` reads an INI file (section `[stockroom]`). Exit codes: `0` success,
`1` a data or configuration error (message on stderr), `2` a usage error.

## Settings

| Key | Default | Environment variable |
|---|---|---|
| `currency_symbol` | `$` | `STOCKROOM_CURRENCY_SYMBOL` |
| `low_stock_warning` | `true` | `STOCKROOM_LOW_STOCK_WARNING` |
| `tax_bp` | `0` (basis points, 825 = 8.25 %) | `STOCKROOM_TAX_BP` |
| `data_dir` | `data` | `STOCKROOM_DATA_DIR` |

Environment variables override the file.

## Tests

```bash
python -m pytest -q tests
```
