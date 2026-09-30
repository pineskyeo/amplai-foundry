"""Catalogue search: match items by word prefixes and rank them."""

from __future__ import annotations

from collections.abc import Iterable

from .models import Item
from .textutil import words


def _name_words(item: Item) -> list[str]:
    return words(item.name)


def _all_words(item: Item) -> list[str]:
    found = _name_words(item) + words(item.sku)
    for tag in item.tags:
        found += words(tag)
    return found


def search(items: Iterable[Item], query: str) -> list[Item]:
    """The items matching every word of ``query``, best first.

    A query word matches an item when it is a prefix of a word of the item's name, SKU or tags.
    Best first means: most query words that equal an item word exactly, then most query words that
    are a prefix of a name word, then SKU in ascending order. A query without words returns all
    items in their given order.
    """
    tokens = words(query)
    catalogue = list(items)
    if not tokens:
        return catalogue
    ranked: list[tuple[int, int, str, Item]] = []
    for item in catalogue:
        everywhere = _all_words(item)
        in_name = _name_words(item)
        if not all(any(w.startswith(t) for w in everywhere) for t in tokens):
            continue
        exact = sum(1 for t in tokens if t in everywhere)
        named = sum(1 for t in tokens if any(w.startswith(t) for w in in_name))
        ranked.append((-exact, -named, item.sku, item))
    ranked.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in ranked]
