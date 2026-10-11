"""Plain-text reports: aligned tables, rankings and the sales summary."""

from __future__ import annotations

import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .dates import month_key
from .models import Order
from .money import Currency
from .orders import OrderBook
from .pricing import order_subtotal
from .textutil import align, plural

_UNSET: object = object()


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    align_right: Iterable[int] = (),
) -> str:
    """A table with a header line, a ``-`` rule and one line per row.

    Each column is as wide as its widest cell or header; columns are separated by two spaces;
    columns whose index is in ``align_right`` are right-aligned. Trailing spaces are removed.
    """
    right = set(align_right)
    cells = [[str(value) for value in row] for row in rows]
    if cells:
        widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    else:
        widths = [len(h) for h in headers]

    def line(values: Sequence[str]) -> str:
        parts = [
            align(value, widths[i], "right" if i in right else "left")
            for i, value in enumerate(values)
        ]
        return "  ".join(parts).rstrip()

    out = [line(list(headers)), "  ".join("-" * w for w in widths)]
    out += [line(row) for row in cells]
    return "\n".join(out)


def top_n(totals: dict[str, int], n: int) -> list[tuple[str, int]]:
    """The ``n`` largest ``(name, total)`` pairs, largest first, ties by name."""
    ranked = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[: n or None]


def customer_totals(book: OrderBook) -> dict[str, int]:
    """Subtotal per customer over the orders that are not cancelled."""
    totals: dict[str, int] = {}
    for order in book:
        if order.status != "cancelled":
            totals[order.customer] = totals.get(order.customer, 0) + order_subtotal(order)
    return totals


@dataclass(frozen=True)
class SalesSummary:
    """The figures of the sales summary, without any layout."""

    order_count: int
    cancelled_count: int
    revenue_cents: int
    months: tuple[tuple[str, int], ...]
    top_customers: tuple[tuple[str, int], ...]


def summarize(orders: Iterable[Order], top: int = 3) -> SalesSummary:
    """The figures of ``orders`` (any iterable, read once): counts, revenue, months, customers."""
    active = cancelled = revenue = 0
    months: dict[str, int] = {}
    customers: dict[str, int] = {}
    for order in orders:
        if order.status == "cancelled":
            cancelled += 1
            continue
        subtotal = order_subtotal(order)
        active += 1
        revenue += subtotal
        key = month_key(order.placed)
        months[key] = months.get(key, 0) + subtotal
        customers[order.customer] = customers.get(order.customer, 0) + subtotal
    return SalesSummary(
        order_count=active,
        cancelled_count=cancelled,
        revenue_cents=revenue,
        months=tuple(sorted(months.items())),
        top_customers=tuple(top_n(customers, top)),
    )


def render_summary(summary: SalesSummary, currency: Currency | None = None) -> str:
    """The text of ``summary``; every amount is written by ``currency.format`` (default ``$``)."""
    money = (currency if currency is not None else Currency()).format
    lines = [
        f"{plural(summary.order_count, 'order')}, {summary.cancelled_count} cancelled",
        f"Revenue: {money(summary.revenue_cents)}",
        "",
        render_table(
            ["Month", "Revenue"],
            [[month, money(cents)] for month, cents in summary.months],
            align_right=[1],
        ),
        "",
        render_table(
            ["Customer", "Total"],
            [[name, money(cents)] for name, cents in summary.top_customers],
            align_right=[1],
        ),
    ]
    return "\n".join(lines)


def sales_summary(
    book: OrderBook,
    *,
    currency: Currency | None = None,
    top: int = 3,
    symbol: str | object = _UNSET,
) -> str:
    """Order count, revenue, revenue per month and the top customers.

    ``symbol`` is the deprecated way to choose the currency: pass ``currency=Currency(symbol)``.
    """
    if symbol is not _UNSET:
        if currency is not None:
            raise TypeError("sales_summary() takes currency or symbol, not both")
        warnings.warn(
            "the symbol argument of sales_summary() is deprecated; pass currency=Currency(symbol)",
            DeprecationWarning,
            stacklevel=2,
        )
        currency = Currency(str(symbol))
    return render_summary(summarize(book, top=top), currency)
