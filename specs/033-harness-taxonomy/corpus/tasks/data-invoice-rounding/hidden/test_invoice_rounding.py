from __future__ import annotations

import math
import os
import random
import subprocess
import sys
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest
from stockroom.errors import ParseError
from stockroom.models import Order, OrderLine
from stockroom.rounding import (
    MODES,
    Invoice,
    InvoiceLine,
    format_rate,
    invoice,
    parse_rate,
    round_div,
)

ROOT = Path(__file__).resolve().parents[3]
ORDER_MODES = ("floor", "ceiling", "down", "up", "half_up", "half_down", "half_even")


def run(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=50,
        check=False,
    )


TABLE = {
    (5, 2): (2, 3, 2, 3, 3, 2, 2),
    (-5, 2): (-3, -2, -2, -3, -3, -2, -2),
    (3, 2): (1, 2, 1, 2, 2, 1, 2),
    (-3, 2): (-2, -1, -1, -2, -2, -1, -2),
    (12, 5): (2, 3, 2, 3, 2, 2, 2),
    (-12, 5): (-3, -2, -2, -3, -2, -2, -2),
    (13, 5): (2, 3, 2, 3, 3, 3, 3),
    (-13, 5): (-3, -2, -2, -3, -3, -3, -3),
    (4, 2): (2,) * 7,
    (-4, 2): (-2,) * 7,
    (0, 7): (0,) * 7,
    (5, -2): (-3, -2, -2, -3, -3, -2, -2),
    (-5, -2): (2, 3, 2, 3, 3, 2, 2),
    (1, 3): (0, 1, 0, 1, 0, 0, 0),
    (-1, 3): (-1, 0, 0, -1, 0, 0, 0),
    (1, 2): (0, 1, 0, 1, 1, 0, 0),
    (-1, 2): (-1, 0, 0, -1, -1, 0, 0),
    (7, 2): (3, 4, 3, 4, 4, 3, 4),
    (-7, 2): (-4, -3, -3, -4, -4, -3, -4),
    (10**30 + 1, 2): (
        5 * 10**29,
        5 * 10**29 + 1,
        5 * 10**29,
        5 * 10**29 + 1,
        5 * 10**29 + 1,
        5 * 10**29,
        5 * 10**29,
    ),
}


def test_round_div_examples() -> None:
    assert MODES == ORDER_MODES
    for (numerator, denominator), expected in TABLE.items():
        got = tuple(round_div(numerator, denominator, mode) for mode in MODES)
        assert got == expected, (numerator, denominator)
    with pytest.raises(ValueError):
        round_div(1, 2, "HALF_UP")
    with pytest.raises(ValueError):
        round_div(1, 2, "nearest")
    with pytest.raises(ZeroDivisionError):
        round_div(1, 0, "floor")


def exact(numerator: int, denominator: int, mode: str) -> int:
    q = Fraction(numerator, denominator)
    lo, hi = math.floor(q), math.ceil(q)
    if mode == "floor":
        return lo
    if mode == "ceiling":
        return hi
    if mode == "down":
        return lo if q >= 0 else hi
    if mode == "up":
        return hi if q >= 0 else lo
    if q - lo != hi - q or lo == hi:
        return lo if q - lo < hi - q else hi
    if mode == "half_up":
        return hi if q > 0 else lo
    if mode == "half_down":
        return lo if q > 0 else hi
    return lo if lo % 2 == 0 else hi


def test_generated_quotients_follow_the_definitions() -> None:
    rng = random.Random(8080)
    for _ in range(4000):
        denominator = rng.choice([1, 2, 3, 4, 7, 10, 100, 10000, rng.randint(1, 10**6)])
        if rng.random() < 0.3:
            denominator = -denominator
        numerator = rng.choice(
            [
                rng.randint(-50, 50),
                rng.randint(-(10**12), 10**12),
                denominator * rng.randint(-1000, 1000) + denominator // 2,
                rng.randint(-(10**40), 10**40),
            ]
        )
        q = Fraction(numerator, denominator)
        results = {mode: round_div(numerator, denominator, mode) for mode in MODES}
        for mode, value in results.items():
            assert value == exact(numerator, denominator, mode), (numerator, denominator, mode)
        assert results["floor"] <= q <= results["ceiling"]
        for mode in ("half_up", "half_down", "half_even"):
            assert abs(results[mode] - q) <= Fraction(1, 2)
        assert round_div(-numerator, denominator, "floor") == -results["ceiling"]
        for mode in ("down", "up", "half_up", "half_down", "half_even"):
            assert round_div(-numerator, denominator, mode) == -results[mode]
        assert round_div(numerator, -denominator, "half_even") == -results["half_even"]


def test_parse_and_format_rate() -> None:
    cases = {
        "8.25%": 825,
        "8.5%": 850,
        "8%": 800,
        "0%": 0,
        "0.01%": 1,
        "100%": 10000,
        "100.00%": 10000,
        "007.5%": 750,
        "825bp": 825,
        "0bp": 0,
        "10000bp": 10000,
        "  8.25%\t": 825,
        "\xa012%\u2028": 1200,
    }
    for text, bp in cases.items():
        assert parse_rate(text) == bp, text
    bad = [
        "",
        " ",
        "8.25",
        "825",
        "8.255%",
        "8.%",
        ".5%",
        "8,25%",
        "8.25 %",
        "825 bp",
        "825BP",
        "825bps",
        "-1%",
        "+1%",
        "100.01%",
        "10001bp",
        "\u0668%",
        "8.2\u0665%",
        "\u00b2%",
        "1_0%",
        "1_0bp",
        "8.2\u00b2%",
        "%",
        "bp",
        "1e2%",
        "8.25%%",
    ]
    for text in bad:
        with pytest.raises(ParseError):
            parse_rate(text)
    assert format_rate(825) == "8.25%"
    assert format_rate(0) == "0.00%"
    assert format_rate(5) == "0.05%"
    assert format_rate(10000) == "100.00%"
    for bp in range(10001):
        assert parse_rate(format_rate(bp)) == bp
        assert parse_rate(f"{bp}bp") == bp
    for bp in (-1, 10001):
        with pytest.raises(ValueError):
            format_rate(bp)


def order(*lines: tuple[str, int, int]) -> Order:
    return Order("B-1", "Ada Moss", date(2024, 5, 1), tuple(OrderLine(*line) for line in lines))


ORDER = order(("MUG-001", 1, 5), ("TEA-010", 3, 15), ("PEN-100", 1, 1005))


def test_invoice_examples() -> None:
    bill = invoice(ORDER, tax_bp=1000, discount_bp=1000)
    assert bill == Invoice(
        lines=(
            InvoiceLine("MUG-001", 1, 5, 0, 5, 0),
            InvoiceLine("TEA-010", 3, 45, 4, 41, 4),
            InvoiceLine("PEN-100", 1, 1005, 100, 905, 90),
        ),
        amount=1055,
        discount=104,
        net=951,
        tax=94,
        total=1045,
        tax_on_total=95,
    )
    up = invoice(ORDER, tax_bp=1000, discount_bp=1000, mode="half_up")
    assert [(ln.discount, ln.net, ln.tax) for ln in up.lines] == [
        (1, 4, 0),
        (5, 40, 4),
        (101, 904, 90),
    ]
    assert (up.discount, up.net, up.tax, up.total, up.tax_on_total) == (107, 948, 94, 1042, 95)
    credit = invoice(ORDER, tax_bp=1000, discount_bp=1000, mode="half_up", credit=True)
    assert [(ln.amount, ln.discount, ln.net, ln.tax) for ln in credit.lines] == [
        (-5, -1, -4, 0),
        (-45, -5, -40, -4),
        (-1005, -101, -904, -90),
    ]
    assert (credit.amount, credit.total, credit.tax_on_total) == (-1055, -1042, -95)
    floor = invoice(ORDER, tax_bp=1000, discount_bp=1000, mode="floor", credit=True)
    assert [(ln.discount, ln.net, ln.tax) for ln in floor.lines] == [
        (-1, -4, -1),
        (-5, -40, -4),
        (-101, -904, -91),
    ]
    assert (floor.tax, floor.total, floor.tax_on_total) == (-96, -1044, -95)
    plain = invoice(ORDER, tax_bp=0)
    assert (plain.amount, plain.discount, plain.net, plain.tax, plain.total) == (
        1055,
        0,
        1055,
        0,
        1055,
    )
    empty = invoice(Order("E-1", "Bo", date(2024, 1, 1)), tax_bp=825)
    assert empty == Invoice((), 0, 0, 0, 0, 0, 0)
    for kwargs in (
        {"tax_bp": -1},
        {"tax_bp": 10001},
        {"tax_bp": 0, "discount_bp": 10001},
        {"tax_bp": 0, "discount_bp": -5},
        {"tax_bp": 0, "mode": "bankers"},
    ):
        with pytest.raises(ValueError):
            invoice(ORDER, **kwargs)


def test_generated_invoices_follow_the_rules() -> None:
    rng = random.Random(2468)
    for _ in range(1500):
        lines = tuple(
            OrderLine(
                rng.choice(["MUG-001", "TEA-010", "PEN-100"]),
                rng.randint(1, 40),
                rng.randint(0, 250000),
            )
            for _ in range(rng.randint(1, 5))
        )
        the_order = Order("R-1", "Cy", date(2024, 2, 2), lines)
        tax_bp = rng.choice([0, 1, 500, 825, 1000, 1999, 2000, 10000, rng.randint(0, 10000)])
        discount_bp = rng.choice([0, 1, 250, 1000, 3333, 5000, 10000, rng.randint(0, 10000)])
        mode = rng.choice(MODES)
        credit = rng.random() < 0.5
        bill = invoice(the_order, tax_bp=tax_bp, discount_bp=discount_bp, mode=mode, credit=credit)
        sign = -1 if credit else 1
        assert len(bill.lines) == len(lines)
        for got, line in zip(bill.lines, lines, strict=True):
            amount = sign * line.quantity * line.unit_price_cents
            discount = exact(amount * discount_bp, 10000, mode)
            net = amount - discount
            assert got == InvoiceLine(
                line.sku, line.quantity, amount, discount, net, exact(net * tax_bp, 10000, mode)
            )
        assert bill.amount == sum(ln.amount for ln in bill.lines)
        assert bill.discount == sum(ln.discount for ln in bill.lines)
        assert bill.net == sum(ln.net for ln in bill.lines) == bill.amount - bill.discount
        assert bill.tax == sum(ln.tax for ln in bill.lines)
        assert bill.total == bill.net + bill.tax
        assert bill.tax_on_total == exact(bill.net * tax_bp, 10000, mode)
        mirrored = invoice(
            the_order, tax_bp=tax_bp, discount_bp=discount_bp, mode=mode, credit=not credit
        )
        if mode in ("down", "up", "half_up", "half_down", "half_even"):
            assert mirrored.total == -bill.total and mirrored.tax_on_total == -bill.tax_on_total
        else:
            other = "ceiling" if mode == "floor" else "floor"
            flipped = invoice(
                the_order, tax_bp=tax_bp, discount_bp=discount_bp, mode=other, credit=not credit
            )
            assert flipped.total == -bill.total and flipped.tax_on_total == -bill.tax_on_total


ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "B-1,Ada Moss,2024-05-01,paid,MUG-001,1,0.05\n"
    "B-1,Ada Moss,2024-05-01,paid,TEA-010,3,0.15\n"
    "B-1,Ada Moss,2024-05-01,paid,PEN-100,1,10.05\n"
    "B-2,Bo Lind,2024-05-02,open,NB-200,2,6.25\n"
)


def test_invoice_command(tmp_path: Path) -> None:
    (tmp_path / "orders.csv").write_text(ORDERS, encoding="utf-8")
    base = ("invoice", "--orders", "orders.csv", "--order", "B-1")
    done = run(*base, "--tax", "10%", "--discount", "1000bp", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "invoice B-1 Ada Moss 2024-05-01",
        "MUG-001 1 x $0.05: amount $0.05 discount $0.00 net $0.05 tax $0.00",
        "TEA-010 3 x $0.15: amount $0.45 discount $0.04 net $0.41 tax $0.04",
        "PEN-100 1 x $10.05: amount $10.05 discount $1.00 net $9.05 tax $0.90",
        "amount: $10.55",
        "discount: $1.04",
        "net: $9.51",
        "tax: $0.94",
        "total: $10.45",
        "tax on total: $0.95 (difference $0.01)",
    ]
    done = run(
        *base, "--tax", "10%", "--discount", "10%", "--rounding", "floor", "--credit", cwd=tmp_path
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "credit note B-1 Ada Moss 2024-05-01"
    assert lines[1] == "MUG-001 1 x $0.05: amount -$0.05 discount -$0.01 net -$0.04 tax -$0.01"
    assert lines[3] == "PEN-100 1 x $10.05: amount -$10.05 discount -$1.01 net -$9.04 tax -$0.91"
    assert lines[-3:] == [
        "tax: -$0.96",
        "total: -$10.44",
        "tax on total: -$0.95 (difference $0.01)",
    ]
    # without --tax the configured tax rate applies; the configured symbol is used
    done = run(
        "invoice",
        "--orders",
        "orders.csv",
        "--order",
        "B-2",
        cwd=tmp_path,
        env={"STOCKROOM_TAX_BP": "825", "STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[1:] == [
        "NB-200 2 x EUR 6.25: amount EUR 12.50 discount EUR 0.00 net EUR 12.50 tax EUR 1.03",
        "amount: EUR 12.50",
        "discount: EUR 0.00",
        "net: EUR 12.50",
        "tax: EUR 1.03",
        "total: EUR 13.53",
        "tax on total: EUR 1.03 (difference EUR 0.00)",
    ]
    done = run(
        "invoice",
        "--orders",
        "orders.csv",
        "--order",
        "B-2",
        "--rounding",
        "half_up",
        cwd=tmp_path,
        env={"STOCKROOM_TAX_BP": "825"},
    )
    assert "tax: $1.03" in done.stdout.splitlines()
    done = run(
        "invoice",
        "--orders",
        "orders.csv",
        "--order",
        "B-2",
        "--tax",
        "0.04%",
        "--rounding",
        "half_up",
        cwd=tmp_path,
    )
    assert done.stdout.splitlines()[-2:] == [
        "total: $12.51",
        "tax on total: $0.01 (difference $0.00)",
    ]
    done = run(
        "invoice", "--orders", "orders.csv", "--order", "B-2", "--tax", "0.04%", cwd=tmp_path
    )
    assert done.stdout.splitlines()[-2:] == [
        "total: $12.50",
        "tax on total: $0.00 (difference $0.00)",
    ]
    done = run(
        "invoice", "--orders", "orders.csv", "--order", "B-2", "--tax", "0.12%", cwd=tmp_path
    )
    assert done.stdout.splitlines()[-2:] == [
        "total: $12.52",
        "tax on total: $0.02 (difference $0.00)",
    ]
    for args in (
        ("--order", "B-9", "--tax", "1%"),
        ("--order", "B-1", "--tax", "8.255%"),
        ("--order", "B-1", "--tax", "101%"),
        ("--order", "B-1", "--tax", "1%", "--discount=-5%"),
    ):
        done = run("invoice", "--orders", "orders.csv", *args, cwd=tmp_path)
        assert done.returncode == 1 and done.stdout == "", args
        assert done.stderr.startswith("stockroom: error:")
    done = run(*base, "--tax", "1%", "--rounding", "bankers", cwd=tmp_path)
    assert done.returncode == 2 and done.stdout == ""
