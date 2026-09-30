"""Invoices: an order priced line by line with per-tag tax rates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .errors import ParseError
from .models import Item, Order
from .money import format_money
from .pricing import tax


@dataclass(frozen=True)
class InvoiceLine:
    sku: str
    name: str
    quantity: int
    unit_price_cents: int
    net_cents: int
    tax_bp: int
    tax_cents: int


@dataclass(frozen=True)
class Invoice:
    order_id: str
    customer: str
    lines: tuple[InvoiceLine, ...]
    subtotal_cents: int
    tax_cents: int
    total_cents: int


def build_invoice(
    order: Order,
    items: Iterable[Item],
    *,
    default_tax_bp: int = 0,
    tag_tax_bp: Mapping[str, int] | None = None,
) -> Invoice:
    """The invoice of ``order``: per line, the tax at the rate of the item's highest-rated tag that
    is in ``tag_tax_bp`` (else ``default_tax_bp``), rounded half up per line."""
    rates = dict(tag_tax_bp or {})
    if default_tax_bp < 0 or any(rate < 0 for rate in rates.values()):
        raise ValueError("tax rates must not be negative")
    catalogue = {item.sku: item for item in items}
    lines: list[InvoiceLine] = []
    for line in order.lines:
        item = catalogue.get(line.sku)
        if item is None:
            raise ParseError(f"no item {line.sku}")
        mapped = [rates[tag] for tag in item.tags if tag in rates]
        rate = max(mapped) if mapped else default_tax_bp
        net = line.quantity * line.unit_price_cents
        lines.append(
            InvoiceLine(line.sku, item.name, line.quantity, line.unit_price_cents, net, rate,
                        tax(net, rate))
        )
    subtotal = sum(line.net_cents for line in lines)
    tax_total = sum(line.tax_cents for line in lines)
    return Invoice(
        order.order_id, order.customer, tuple(lines), subtotal, tax_total, subtotal + tax_total
    )


def _percent(bp: int) -> str:
    return f"{bp // 100}.{bp % 100:02d}%"


def render_invoice(invoice: Invoice, symbol: str = "$") -> str:
    """The invoice as text lines joined by newlines (no trailing newline)."""
    out = [f"Invoice {invoice.order_id} for {invoice.customer}"]
    for line in invoice.lines:
        out.append(
            f"{line.sku}  {line.name}  {line.quantity} x "
            f"{format_money(line.unit_price_cents, symbol)} = {format_money(line.net_cents, symbol)}"
            f"  tax {format_money(line.tax_cents, symbol)} ({_percent(line.tax_bp)})"
        )
    out.append(f"Subtotal: {format_money(invoice.subtotal_cents, symbol)}")
    out.append(f"Tax: {format_money(invoice.tax_cents, symbol)}")
    out.append(f"Total: {format_money(invoice.total_cents, symbol)}")
    return "\n".join(out)
