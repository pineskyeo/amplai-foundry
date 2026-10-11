"""Rewrite stockroom data files (items, stock, orders) in their canonical form.

    python scripts/fmt_data.py [--check] FILE [FILE ...]

Exit codes: 0 done (or, with --check, nothing to change), 1 --check found a file to change,
2 a usage error, 3 at least one file could not be read or parsed.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockroom.csvio import (
    ITEM_HEADER,
    ORDER_HEADER,
    STOCK_HEADER,
    read_items,
    read_orders,
    read_stock,
    write_items,
    write_orders,
    write_stock,
)
from stockroom.errors import StockroomError

KINDS: dict[tuple[str, ...], Callable[[str], str]] = {
    ITEM_HEADER: lambda text: write_items(read_items(text)),
    STOCK_HEADER: lambda text: write_stock(read_stock(text)),
    ORDER_HEADER: lambda text: write_orders(read_orders(text)),
}


class FileProblem(Exception):
    pass


def canonical(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileProblem(f"not UTF-8 text: {exc}") from exc
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines()
    if not lines:
        raise FileProblem("empty file")
    header = tuple(cell.strip() for cell in lines[0].split(","))
    if header not in KINDS:
        raise FileProblem(f"unknown header {lines[0]!r}")
    try:
        return KINDS[header](text)
    except StockroomError as exc:
        raise FileProblem(str(exc)) from exc


def replace(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rewrite data files in canonical form.")
    parser.add_argument("--check", action="store_true", help="only report, write nothing")
    parser.add_argument("files", nargs="+", metavar="FILE")
    args = parser.parse_args(argv)
    seen: set[str] = set()
    changed = unchanged = failed = 0
    for name in args.files:
        key = os.path.realpath(name)
        if key in seen:
            continue
        seen.add(key)
        path = Path(name)
        try:
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise FileProblem(f"cannot read: {exc.strerror}") from exc
            new = canonical(data).encode("utf-8")
            if new == data:
                unchanged += 1
                continue
            if not args.check:
                try:
                    replace(path, new)
                except OSError as exc:
                    raise FileProblem(f"cannot write: {exc.strerror}") from exc
        except FileProblem as exc:
            failed += 1
            print(f"fmt_data: error: {name}: {exc}", file=sys.stderr)
            continue
        changed += 1
        print(f"would reformat {name}" if args.check else f"reformatted {name}")
    verb = "would be reformatted" if args.check else "reformatted"
    print(f"{changed} {verb}, {unchanged} unchanged, {failed} failed")
    if failed:
        return 3
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
