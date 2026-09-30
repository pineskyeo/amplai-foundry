"""Looking things up in a list of catalogue items."""

from __future__ import annotations

from collections.abc import Sequence

from .errors import ParseError
from .models import Item


def find_item(items: Sequence[Item], sku: str) -> Item:
    """The first item with this SKU; ``ParseError`` when there is none."""
    for item in items:
        if item.sku == sku:
            return item
    raise ParseError(f"no item {sku}")


def with_tag(items: Sequence[Item], tag: str) -> list[Item]:
    """The items carrying ``tag`` (any case), in the given order; an empty tag keeps them all."""
    if not tag:
        return list(items)
    return [item for item in items if tag.lower() in item.tags]
