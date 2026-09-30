"""Write a seeded sample data set: items.csv, stock.csv and orders.csv.

    python scripts/make_sample_data.py --out DIR [--seed N] [--orders N]

The same seed and order count always give the same files.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockroom.csvio import write_items, write_orders, write_stock
from stockroom.models import Item, Order, OrderLine

NAMES = ("Mug", "Tea", "Pen", "Notebook", "Lamp", "Candle", "Bowl", "Scarf")
CUSTOMERS = ("Hana Kim", "Omar Diaz", "Lena Berg", "Ravi Nair", "Ada Moss")
STATUSES = ("open", "paid", "shipped", "cancelled")


def make_items(rng: random.Random) -> list[Item]:
    return [
        Item(
            sku=f"SMP-{100 + i:03d}",
            name=f"Sample {name}",
            price_cents=rng.randrange(199, 9999),
            tags=(rng.choice(("home", "office", "kitchen")),),
            reorder_level=rng.randrange(0, 12),
        )
        for i, name in enumerate(NAMES)
    ]


def make_stock(rng: random.Random, items: list[Item]) -> list[tuple[str, int, int]]:
    levels = []
    for item in items:
        on_hand = rng.randrange(0, 40)
        levels.append((item.sku, on_hand, rng.randrange(0, on_hand + 1)))
    return levels


def make_orders(rng: random.Random, items: list[Item], count: int) -> list[Order]:
    start = date(2024, 1, 1)
    book = []
    for n in range(count):
        picked = rng.sample(items, k=rng.randrange(1, 4))
        book.append(
            Order(
                order_id=f"S-{n + 1:04d}",
                customer=rng.choice(CUSTOMERS),
                placed=start + timedelta(days=rng.randrange(0, 180)),
                lines=tuple(
                    OrderLine(item.sku, rng.randrange(1, 6), item.price_cents) for item in picked
                ),
                status=rng.choice(STATUSES),
            )
        )
    return book


def build(seed: int, orders: int) -> dict[str, str]:
    rng = random.Random(seed)
    items = make_items(rng)
    stock = make_stock(rng, items)
    book = make_orders(rng, items, orders)
    return {
        "items.csv": write_items(items),
        "stock.csv": write_stock(stock),
        "orders.csv": write_orders(book),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a seeded sample data set.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--orders", type=int, default=20)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in build(args.seed, args.orders).items():
        (args.out / name).write_text(text, encoding="utf-8")
    print(f"wrote {len(NAMES)} items and {args.orders} orders to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
