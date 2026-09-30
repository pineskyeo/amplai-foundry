"""Customers whose names were written in different ways."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .models import Order


def normalize_name(name: str) -> str:
    """The comparison form of a name: dots removed, case folded, whitespace runs made one space."""
    return " ".join(name.replace(".", "").casefold().split())


def merge_customers(orders: Iterable[Order]) -> dict[str, str]:
    """Map every spelling of a customer name found in ``orders`` to its canonical spelling."""
    stats: dict[str, list] = {}
    for order in orders:
        entry = stats.setdefault(order.customer, [0, order.placed])
        entry[0] += 1
        entry[1] = min(entry[1], order.placed)
    groups: dict[str, list[str]] = {}
    for spelling in stats:
        groups.setdefault(normalize_name(spelling), []).append(spelling)
    canonical: dict[str, str] = {}
    for spellings in groups.values():
        best = min(spellings, key=lambda s: (-stats[s][0], stats[s][1], s))
        for spelling in spellings:
            canonical[spelling] = best
    return {spelling: canonical[spelling] for spelling in stats}


def canonicalize(orders: Iterable[Order]) -> list[Order]:
    """The orders with every customer name replaced by its canonical spelling."""
    orders = list(orders)
    mapping = merge_customers(orders)
    return [replace(order, customer=mapping[order.customer]) for order in orders]
