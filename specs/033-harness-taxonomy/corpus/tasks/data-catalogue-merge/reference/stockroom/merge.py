"""Merge catalogues: one item per SKU, by a stated priority, renamed SKUs followed, name changes
reported."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .errors import ParseError
from .models import Item

RENAME_HEADER = ("old_sku", "new_sku")
_SKU = re.compile(r"[A-Z]{2,4}-[0-9]{3,5}")


@dataclass(frozen=True)
class Conflict:
    sku: str
    before: str
    after: str


@dataclass(frozen=True)
class MergeResult:
    items: tuple[Item, ...]
    conflicts: tuple[Conflict, ...]


def name_key(name: str) -> str:
    """NFKC, whitespace runs joined by one space and trimmed, then case-folded."""
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def read_renames(text: str) -> dict[str, str]:
    """``{old_sku: new_sku}`` from a CSV text with the header ``old_sku,new_sku``."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows or tuple(c.strip() for c in rows[0]) != RENAME_HEADER:
        raise ParseError(f"rename header must be {','.join(RENAME_HEADER)}")
    renames: dict[str, str] = {}
    for row in rows[1:]:
        if len(row) != 2:
            raise ParseError(f"expected 2 fields: {row!r}")
        old, new = (c.strip() for c in row)
        for sku in (old, new):
            if not _SKU.fullmatch(sku):
                raise ParseError(f"bad sku: {sku!r}")
        if old in renames:
            raise ParseError(f"{old} is renamed twice")
        renames[old] = new
    return renames


def _resolver(renames: Mapping[str, str]) -> dict[str, str]:
    final: dict[str, str] = {}
    for start in renames:
        chain = [start]
        sku = start
        while sku in renames:
            sku = renames[sku]
            if sku in chain:
                raise ValueError("rename cycle: " + " -> ".join([*chain, sku]))
            chain.append(sku)
        final[start] = sku
    return final


def _add_tags(tags: list[str], more: Iterable[str]) -> None:
    for tag in more:
        if all(tag.casefold() != seen.casefold() for seen in tags):
            tags.append(tag)


def merge_catalogues(
    catalogues: Iterable[Iterable[Item]],
    *,
    prefer: str = "last",
    renames: Mapping[str, str] | None = None,
) -> MergeResult:
    if prefer not in ("first", "last"):
        raise ValueError(f"prefer must be 'first' or 'last', not {prefer!r}")
    final = _resolver(renames or {})
    merged: dict[str, Item] = {}
    conflicts: list[Conflict] = []
    for catalogue in catalogues:
        for item in catalogue:
            sku = final.get(item.sku, item.sku)
            old = merged.get(sku)
            if old is None:
                tags: list[str] = []
                _add_tags(tags, item.tags)
                merged[sku] = Item(
                    sku, item.name, item.price_cents, tuple(tags), item.reorder_level
                )
                continue
            if name_key(old.name) != name_key(item.name):
                conflicts.append(Conflict(sku, old.name, item.name))
            tags = list(old.tags)
            _add_tags(tags, item.tags)
            if prefer == "last":
                name = item.name
                price = item.price_cents if item.price_cents else old.price_cents
            else:
                name = old.name
                price = old.price_cents if old.price_cents else item.price_cents
            merged[sku] = Item(
                sku, name, price, tuple(tags), max(old.reorder_level, item.reorder_level)
            )
    return MergeResult(tuple(merged.values()), tuple(conflicts))
