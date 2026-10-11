"""Split an orders file into one file per month: DIR/orders-YYYY-MM.csv.

    python scripts/split_orders.py --orders FILE --out DIR [--prune] [--dry-run]

Files whose content would not change are not written. Exit codes: 0 done, 1 an error,
2 a usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockroom.csvio import read_orders, write_orders
from stockroom.errors import StockroomError
from stockroom.orders import OrderBook

MONTH_FILE = re.compile(r"orders-[0-9]{4}-[0-9]{2}\.csv")


class Failure(Exception):
    pass


def replace(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with open(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def plan(orders_path: Path, out: Path, prune: bool) -> tuple[dict[str, bytes], list[str]]:
    try:
        text = orders_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Failure(f"cannot read {orders_path}: {exc}") from exc
    try:
        book = OrderBook(read_orders(text))
    except StockroomError as exc:
        raise Failure(f"{orders_path}: {exc}") from exc
    if out.exists() and not out.is_dir():
        raise Failure(f"{out} is not a directory")
    files = {
        f"orders-{month}.csv": write_orders(orders).encode("utf-8")
        for month, orders in book.by_month().items()
    }
    stale: list[str] = []
    if prune and out.is_dir():
        stale = [
            p.name
            for p in out.iterdir()
            if MONTH_FILE.fullmatch(p.name) and p.name not in files and p.is_file()
        ]
    source = os.path.realpath(orders_path)
    for name in [*files, *stale]:
        if os.path.realpath(out / name) == source:
            raise Failure(f"refusing to replace or remove the input file {orders_path}")
    return files, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Split an orders file by month.")
    parser.add_argument("--orders", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        files, stale = plan(args.orders, args.out, args.prune)
        events: list[tuple[str, str]] = []
        for name, data in files.items():
            path = args.out / name
            same = path.is_file() and path.read_bytes() == data
            events.append((name, "unchanged" if same else "write"))
        events += [(name, "remove") for name in stale]
        if not args.dry_run:
            args.out.mkdir(parents=True, exist_ok=True)
            for name, kind in events:
                if kind == "write":
                    replace(args.out / name, files[name])
                elif kind == "remove":
                    (args.out / name).unlink()
    except (Failure, OSError) as exc:
        print(f"split_orders: error: {exc}", file=sys.stderr)
        return 1
    words = {
        "write": "would write" if args.dry_run else "wrote",
        "unchanged": "unchanged",
        "remove": "would remove" if args.dry_run else "removed",
    }
    for name, kind in sorted(events):
        print(f"{words[kind]} {name}")
    count = {kind: sum(1 for _, k in events if k == kind) for kind in words}
    if args.dry_run:
        print(
            f"{count['write']} would be written, {count['unchanged']} unchanged, "
            f"{count['remove']} would be removed"
        )
    else:
        print(
            f"{count['write']} written, {count['unchanged']} unchanged, {count['remove']} removed"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
