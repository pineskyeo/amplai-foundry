"""Write a seeded sample data set: items.csv, stock.csv and orders.csv.

    python scripts/make_sample_data.py --out DIR [--seed N] [--orders N]
        [--start YYYY-MM-DD] [--days N] [--force] [--dry-run]

The same seed and order count always give the same files.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockroom.csvio import write_items, write_orders
from stockroom.models import Item, Order, OrderLine

NAMES = ("Mug", "Tea", "Pen", "Notebook", "Lamp", "Candle", "Bowl", "Scarf")
CUSTOMERS = ("Hana Kim", "Omar Diaz", "Lena Berg", "Ravi Nair", "Ada Moss")
STATUSES = ("open", "paid", "shipped", "cancelled")


def build(
    seed: int, orders: int, start: date = date(2024, 1, 1), days: int = 180
) -> dict[str, str]:
    rng = random.Random(seed)
    items = [
        Item(
            sku=f"SMP-{100 + i:03d}",
            name=f"Sample {name}",
            price_cents=rng.randrange(199, 9999),
            tags=(rng.choice(("home", "office", "kitchen")),),
            reorder_level=rng.randrange(0, 12),
        )
        for i, name in enumerate(NAMES)
    ]
    stock = ["sku,on_hand,reserved"]
    for item in items:
        on_hand = rng.randrange(0, 40)
        stock.append(f"{item.sku},{on_hand},{rng.randrange(0, on_hand + 1)}")
    book = []
    for n in range(orders):
        picked = rng.sample(items, k=rng.randrange(1, 4))
        book.append(
            Order(
                order_id=f"S-{n + 1:04d}",
                customer=rng.choice(CUSTOMERS),
                placed=start + timedelta(days=rng.randrange(0, days)),
                lines=tuple(
                    OrderLine(item.sku, rng.randrange(1, 6), item.price_cents) for item in picked
                ),
                status=rng.choice(STATUSES),
            )
        )
    return {
        "items.csv": write_items(items),
        "stock.csv": "\n".join(stock) + "\n",
        "orders.csv": write_orders(book),
    }


FILES = ("items.csv", "stock.csv", "orders.csv")
LAST_DAY = date(9999, 12, 31)


def _non_negative(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def _positive(text: str) -> int:
    value = _non_negative(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _iso_date(text: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise argparse.ArgumentTypeError(f"not a date written as YYYY-MM-DD: {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a calendar date: {text!r}") from None


def _fail(message: str) -> int:
    print(f"make_sample_data: error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a seeded sample data set.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--orders", type=_non_negative, default=20)
    parser.add_argument("--start", type=_iso_date, default=date(2024, 1, 1))
    parser.add_argument("--days", type=_positive, default=180)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if (LAST_DAY - args.start).days < args.days - 1:
        parser.error("argument --days: the last day would pass 9999-12-31")
    if args.out.exists() and not args.out.is_dir():
        return _fail(f"{args.out} exists and is not a directory")
    existing = [n for n in FILES if (args.out / n).exists()]
    if existing and not args.force:
        return _fail(f"{', '.join(existing)} already exist(s) in {args.out}; use --force")
    files = build(args.seed, args.orders, args.start, args.days)
    if args.dry_run:
        for name in FILES:
            print(f"would write {args.out / name}")
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        (args.out / name).write_text(files[name], encoding="utf-8")
    print(f"wrote {len(NAMES)} items and {args.orders} orders to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
