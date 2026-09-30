"""Plain-text reports: aligned tables, rankings and the sales summary."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .orders import OrderBook
from .textutil import align, plural


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


def _cents(cents: int, symbol: str) -> str:
    sign = "-" if cents < 0 else ""
    units, rest = divmod(abs(cents), 100)
    return sign + symbol + f"{units:,}" + "." + str(rest).zfill(2)


def customer_totals(book: OrderBook) -> dict[str, int]:
    """Subtotal per customer over the orders that are not cancelled."""
    totals: dict[str, int] = {}
    for order in book:
        if order.is_active:
            totals[order.customer] = totals.get(order.customer, 0) + order.subtotal_cents
    return totals


def sales_summary(book: OrderBook, *, symbol: str = "$", top: int = 3) -> str:
    """Order count, revenue, revenue per month and the top customers."""
    active = [o for o in book if o.is_active]
    revenue = sum(o.subtotal_cents for o in active)
    lines = [
        f"{plural(len(active), 'order')}, {len(book) - len(active)} cancelled",
        f"Revenue: {_cents(revenue, symbol)}",
        "",
    ]
    months = book.revenue_by_month()
    lines.append(
        render_table(
            ["Month", "Revenue"],
            [[month, _cents(cents, symbol)] for month, cents in months.items()],
            align_right=[1],
        )
    )
    lines.append("")
    lines.append(
        render_table(
            ["Customer", "Total"],
            [[name, _cents(cents, symbol)] for name, cents in top_n(customer_totals(book), top)],
            align_right=[1],
        )
    )
    return "\n".join(lines)
