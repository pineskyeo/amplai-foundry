import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from stockroom.errors import ParseError
from stockroom.fx import Invoice, convert, make_invoice, parse_rate
from stockroom.models import Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]

ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "A-7,Mia Chen,2024-05-02,paid,MUG-001,2,12.50\n"
    "A-7,Mia Chen,2024-05-02,paid,TEA-010,1,8.95\n"
    "A-8,Mia Chen,2024-05-03,cancelled,PEN-100,1,34.00\n"
    "A-9,Ravi Rao,2024-05-04,open,NB-200,700,6.25\n"
)


def invoice(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "orders.csv").write_text(ORDERS, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", "invoice", "--orders", "orders.csv", *extra],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def order(*lines: tuple[str, int, int], status: str = "paid") -> Order:
    return Order(
        "Q-1",
        "Mia Chen",
        date(2024, 5, 2),
        tuple(OrderLine(sku, quantity, price) for sku, quantity, price in lines),
        status,
    )


def test_fx_rates_are_parsed_as_whole_millionths() -> None:
    assert parse_rate("0.9215") == 921500
    assert parse_rate("2") == 2000000
    assert parse_rate(" 1.5 ") == 1500000
    assert parse_rate("0.000001") == 1
    assert parse_rate("12.345678") == 12345678
    for bad in (
        "0",
        "0.000000",
        "-1",
        "+1",
        "1e3",
        ".5",
        "1.",
        "0.0000001",
        "",
        "1,5",
        "\u0661.\u0665",
    ):
        with pytest.raises(ParseError) as caught:
            parse_rate(bad)
        assert str(caught.value) == f"not a rate: {bad!r}"


def test_fx_convert_rounds_half_up_with_integer_arithmetic() -> None:
    assert convert(2500, 921500) == 2304
    assert convert(5, 500000) == 3
    assert convert(15, 500000) == 8
    assert convert(0, 921500) == 0
    assert convert(10**15 + 1, 1) == 1000000000
    with pytest.raises(ValueError):
        convert(-1, 921500)
    with pytest.raises(ValueError):
        convert(100, 0)
    rng = random.Random(3303)
    for _ in range(3000):
        cents = rng.randrange(0, 10**9)
        rate = rng.randrange(1, 5 * 10**6)
        expected = cents * rate // 10**6
        if (cents * rate) % 10**6 * 2 >= 10**6:
            expected += 1
        assert convert(cents, rate) == expected


def test_fx_invoice_lines_convert_each_line_total() -> None:
    made = make_invoice(order(("MUG-001", 2, 1250), ("TEA-010", 1, 895)), 921500)
    assert isinstance(made, Invoice)
    assert (made.order_id, made.customer) == ("Q-1", "Mia Chen")
    assert made.lines == (("MUG-001", 2, 2304), ("TEA-010", 1, 825))
    assert make_invoice(order(("PEN-100", 3, 5)), 500000).lines == (("PEN-100", 3, 8),)
    with pytest.raises(ParseError) as caught:
        make_invoice(order(("PEN-100", 1, 3400), status="cancelled"), 921500)
    assert str(caught.value) == "order Q-1 is cancelled"
    rng = random.Random(77)
    for _ in range(200):
        lines = [("NB-200", rng.randrange(1, 50), rng.randrange(0, 20000)) for _ in range(4)]
        rate = rng.randrange(1, 3 * 10**6)
        made = make_invoice(order(*lines), rate)
        assert made.lines == tuple((s, q, convert(q * p, rate)) for s, q, p in lines)


def test_fx_single_line_total_matches_its_line() -> None:
    made = make_invoice(order(("NB-200", 700, 625)), 921500)
    assert made.lines == (("NB-200", 700, 403156),)
    assert made.total == 403156


def test_fx_invoice_command_prints_header_lines_and_total(tmp_path: Path) -> None:
    done = invoice(tmp_path, "--order-id", "A-9", "--rate", "0.9215")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.splitlines() == [
        "invoice A-9 for Ravi Rao, placed 2024-05-04",
        "NB-200 x700: 4,031.56",
        "total: 4,031.56",
    ]
    done = invoice(tmp_path, "--order-id", "A-9", "--rate", "1.5", "--symbol", "EUR ")
    assert done.stdout.splitlines() == [
        "invoice A-9 for Ravi Rao, placed 2024-05-04",
        "NB-200 x700: EUR 6,562.50",
        "total: EUR 6,562.50",
    ]
    done = invoice(tmp_path, "--order-id", "A-7", "--rate", "0.9215")
    assert done.stdout.splitlines()[:3] == [
        "invoice A-7 for Mia Chen, placed 2024-05-02",
        "MUG-001 x2: 23.04",
        "TEA-010 x1: 8.25",
    ]


def test_fx_invoice_command_refuses_bad_input(tmp_path: Path) -> None:
    cases = [
        (("--order-id", "A-7", "--rate", "0"), "not a rate: '0'"),
        (("--order-id", "A-7", "--rate", "1e3"), "not a rate: '1e3'"),
        (("--order-id", "A-99", "--rate", "1"), "no order A-99"),
        (("--order-id", "A-8", "--rate", "1"), "order A-8 is cancelled"),
    ]
    for args, message in cases:
        done = invoice(tmp_path, *args)
        assert done.returncode == 1
        assert done.stdout == ""
        assert done.stderr == f"stockroom: error: {message}\n"


def test_fx_total_converts_the_order_subtotal_once(tmp_path: Path) -> None:
    assert make_invoice(order(("MUG-001", 2, 1250), ("TEA-010", 1, 895)), 921500).total == 3128
    three = order(("PEN-100", 1, 5), ("NB-200", 1, 5), ("CND-007", 1, 5))
    assert make_invoice(three, 500000).total == 8
    rng = random.Random(4242)
    for _ in range(200):
        lines = [("NB-200", rng.randrange(1, 50), rng.randrange(0, 20000)) for _ in range(3)]
        rate = rng.randrange(1, 3 * 10**6)
        subtotal = sum(q * p for _, q, p in lines)
        assert make_invoice(order(*lines), rate).total == convert(subtotal, rate)
    done = invoice(tmp_path, "--order-id", "A-7", "--rate", "0.9215")
    assert done.stdout.splitlines()[-1] == "total: 31.28"
