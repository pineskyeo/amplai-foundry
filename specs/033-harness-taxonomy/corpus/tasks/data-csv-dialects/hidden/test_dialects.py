from __future__ import annotations

import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from stockroom.csvio import (
    ORDER_HEADER,
    STOCK_HEADER,
    detect_delimiter,
    read_orders,
    read_stock,
    write_orders,
)
from stockroom.errors import ParseError
from stockroom.models import Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]
DELIMS = (",", ";", "\t", "|")
ORDER = Order(
    "A-1", "Hana Kim", date(2024, 1, 8), (OrderLine("MUG-001", 2, 125000), OrderLine("TEA-010", 1, 895))
)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def order_file(delimiter: str, price: str, customer: str = "Hana Kim") -> str:
    header = delimiter.join(ORDER_HEADER)
    row = delimiter.join(["A-1", customer, "2024-01-08", "paid", "MUG-001", "2", price])
    return f"{header}\n{row}\n"


def test_delimiter_is_detected_from_the_header() -> None:
    for delimiter in DELIMS:
        assert detect_delimiter(order_file(delimiter, "1.00"), ORDER_HEADER) == delimiter
        stock = delimiter.join(STOCK_HEADER) + "\n"
        assert detect_delimiter(stock, STOCK_HEADER) == delimiter
        assert detect_delimiter(stock.replace("\n", "\r\n"), STOCK_HEADER) == delimiter
    quoted = ";".join(f'"{cell}"' for cell in ORDER_HEADER) + "\n"
    assert detect_delimiter(quoted, ORDER_HEADER) == ";"
    spaced = "; ".join(ORDER_HEADER) + "\n"
    assert detect_delimiter(spaced, ORDER_HEADER) == ";"
    for bad in ("", "\n", "a;b;c\n1;2;3\n", ";".join(ORDER_HEADER[:-1]) + "\n", "sku:on_hand:reserved\n"):
        with pytest.raises(ParseError):
            detect_delimiter(bad, ORDER_HEADER)
    with pytest.raises(ParseError):
        detect_delimiter("\n" + order_file(";", "1"), ORDER_HEADER)
    with pytest.raises(ParseError):
        detect_delimiter(order_file(";", "1"), STOCK_HEADER)


def test_readers_follow_the_delimiter() -> None:
    for delimiter in DELIMS:
        text = order_file(delimiter, "12.50")
        (order,) = read_orders(text)
        assert (order.order_id, order.customer, order.placed) == ("A-1", "Hana Kim", date(2024, 1, 8))
        assert order.lines[0].unit_price_cents == 1250 and order.lines[0].quantity == 2
        assert read_orders(text, delimiter=delimiter) == read_orders(text)
        stock = f"sku{delimiter}on_hand{delimiter}reserved\nMUG-001{delimiter}14{delimiter}2\n"
        assert read_stock(stock) == [("MUG-001", 14, 2)]
        assert read_stock(stock, delimiter=delimiter) == [("MUG-001", 14, 2)]
        assert read_stock(stock.replace("\n", "\r\n")) == [("MUG-001", 14, 2)]
    text = order_file(";", "12.50", customer="Kim; Hana")
    assert read_orders(text.replace("Kim; Hana", '"Kim; Hana"'))[0].customer == "Kim; Hana"
    with pytest.raises(ParseError):
        read_orders(order_file(";", "1.00"), delimiter=",")
    with pytest.raises(ParseError):
        read_orders(order_file(",", "1.00"), delimiter=";")
    with pytest.raises(ParseError):
        read_stock("sku;on_hand;reserved\nMUG-001;1;0\n", delimiter="|")
    for bad in (":", "ab", "", " ", "\n"):
        with pytest.raises(ValueError):
            read_orders(order_file(";", "1.00"), delimiter=bad)
        with pytest.raises(ValueError):
            read_stock("sku,on_hand,reserved\n", delimiter=bad)
    with pytest.raises(ParseError):
        read_orders("")
    with pytest.raises(ParseError):
        read_stock("")
    with pytest.raises(ParseError):
        read_orders("order_id;customer\n")


VALID_PRICES = [
    ("12,50", 1250),
    ("12,5", 1250),
    ("1.250,00", 125000),
    ("1.250,5", 125050),
    ("1.250", 125000),
    ("12.345", 1234500),
    ("100.500", 10050000),
    ("1.000", 100000),
    ("1.234.567,89", 123456789),
    ("1.25", 125),
    ("1.5", 150),
    ("1.50", 150),
    ("0.5", 50),
    ("0.05", 5),
    ("0,5", 50),
    ("0,05", 5),
    ("0", 0),
    ("7", 700),
    ("1250.00", 125000),
    ("1250,00", 125000),
    ("  12,50  ", 1250),
]
REFUSED_PRICES = [
    "1,250.00", "1,250", "12,345", "1,2,3", "1.2.3", "1.25,5,5", "-5", "-1,50", "$12,50", "12,", ",50",
    "1.250,505", "", "1.2500", "1.2345", "12.50,00", "1 250,00", "1e3", "12,50 EUR", "+1,50",
]  # fmt: skip


@pytest.mark.parametrize("delimiter", [";", "\t", "|"])
def test_prices_outside_comma_files(delimiter: str) -> None:
    for cell, cents in VALID_PRICES:
        (order,) = read_orders(order_file(delimiter, cell))
        assert order.lines[0].unit_price_cents == cents, (delimiter, cell)
    for cell in REFUSED_PRICES:
        with pytest.raises(ParseError):
            read_orders(order_file(delimiter, cell))
    (order,) = read_orders(order_file(delimiter, '"1.250,00"'))
    assert order.lines[0].unit_price_cents == 125000


def test_comma_files_are_unchanged() -> None:
    (order,) = read_orders(order_file(",", '"1,250.00"'))
    assert order.lines[0].unit_price_cents == 125000
    (order,) = read_orders(order_file(",", "12.50"))
    assert order.lines[0].unit_price_cents == 1250
    assert read_orders(order_file(",", "$3.10"))[0].lines[0].unit_price_cents == 310
    with pytest.raises(ParseError):
        read_orders(order_file(",", "abc"))
    old = (
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        'A-1,Hana Kim,2024-01-08,open,MUG-001,2,"1,250.00"\n'
        "A-1,Hana Kim,2024-01-08,open,TEA-010,1,8.95\n"
    )
    assert write_orders(read_orders(old)) == old
    assert write_orders([ORDER]) == (
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        'A-1,Hana Kim,2024-01-08,open,MUG-001,2,"1,250.00"\n'
        "A-1,Hana Kim,2024-01-08,open,TEA-010,1,8.95\n"
    )


def test_writer_dialects() -> None:
    header = "order_id;customer;placed;status;sku;quantity;unit_price\n"
    assert write_orders([ORDER], delimiter=";") == (
        header + "A-1;Hana Kim;2024-01-08;open;MUG-001;2;1250.00\n"
        "A-1;Hana Kim;2024-01-08;open;TEA-010;1;8.95\n"
    )
    assert write_orders([ORDER], delimiter=";", decimal=",") == (
        header + "A-1;Hana Kim;2024-01-08;open;MUG-001;2;1250,00\n"
        "A-1;Hana Kim;2024-01-08;open;TEA-010;1;8,95\n"
    )
    tabbed = write_orders([ORDER], delimiter="\t", decimal=",")
    assert tabbed.splitlines()[1] == "A-1\tHana Kim\t2024-01-08\topen\tMUG-001\t2\t1250,00"
    piped = write_orders([ORDER], delimiter="|")
    assert piped.splitlines()[0] == "order_id|customer|placed|status|sku|quantity|unit_price"
    assert piped.splitlines()[2] == "A-1|Hana Kim|2024-01-08|open|TEA-010|1|8.95"
    assert write_orders([ORDER], delimiter=",", decimal=".") == write_orders([ORDER])
    assert write_orders([], delimiter=";") == header
    cheap = Order("B-2", "Ada, Moss", date(2024, 2, 1), (OrderLine("PEN-100", 1, 5),))
    assert write_orders([cheap], delimiter=";", decimal=",").splitlines()[1] == (
        "B-2;Ada, Moss;2024-02-01;open;PEN-100;1;0,05"
    )
    assert write_orders([cheap]).splitlines()[1].startswith('B-2,"Ada, Moss",2024-02-01')
    with pytest.raises(ValueError):
        write_orders([ORDER], delimiter=",", decimal=",")
    for bad in (":", "ab", ""):
        with pytest.raises(ValueError):
            write_orders([ORDER], delimiter=bad)
    with pytest.raises(ValueError):
        write_orders([ORDER], delimiter=";", decimal=":")


def _generated(rng: random.Random) -> list[Order]:
    pieces = list("abcXYZ 019-éü;,|\t\"'\n.")
    orders = []
    for n in range(rng.randrange(1, 7)):
        customer = "".join(rng.choice(pieces) for _ in range(rng.randrange(1, 12))).strip()
        customer = customer or "Zed"
        lines = tuple(
            OrderLine(
                rng.choice(["MUG-001", "TEA-010", "PEN-100", "NB-200"]),
                rng.randrange(1, 50),
                rng.choice([0, 5, 99, 100, 101, 99999, 100000, rng.randrange(0, 10**8)]),
            )
            for _ in range(rng.randrange(1, 4))
        )
        orders.append(
            Order(
                f"G-{n + 1:03d}",
                customer,
                date(2024, 1, 1) + timedelta(days=rng.randrange(0, 365)),
                lines,
                rng.choice(["open", "paid", "shipped", "cancelled"]),
            )
        )
    return orders


def test_every_dialect_round_trips() -> None:
    rng = random.Random(20261001)
    for _ in range(150):
        orders = _generated(rng)
        for delimiter in DELIMS:
            for decimal in (".", ","):
                if delimiter == "," and decimal == ",":
                    continue
                text = write_orders(orders, delimiter=delimiter, decimal=decimal)
                assert detect_delimiter(text, ORDER_HEADER) == delimiter
                assert read_orders(text) == orders, (delimiter, decimal, text)
                assert read_orders(text, delimiter=delimiter) == orders
    text = write_orders([], delimiter=";")
    assert read_orders(text) == []


def test_command_line_reads_any_dialect(tmp_path: Path) -> None:
    original = (ROOT / "data" / "orders.csv").read_text()
    orders = read_orders(original)
    base = run("sales", "--orders", str(ROOT / "data" / "orders.csv"))
    assert base.returncode == 0, base.stderr
    for name, delimiter, decimal in (("semi.csv", ";", ","), ("tab.csv", "\t", "."), ("pipe.csv", "|", ",")):
        path = tmp_path / name
        path.write_text(write_orders(orders, delimiter=delimiter, decimal=decimal))
        done = run("sales", "--orders", str(path))
        assert done.returncode == 0, done.stderr
        assert done.stdout == base.stdout
    stock = (ROOT / "data" / "stock.csv").read_text()
    expected = run("stock", "--items", "data/items.csv", "--stock", "data/stock.csv")
    for delimiter in (";", "\t", "|"):
        path = tmp_path / f"stock-{ord(delimiter)}.csv"
        path.write_text(stock.replace(",", delimiter))
        done = run("stock", "--items", "data/items.csv", "--stock", str(path))
        assert done.returncode == 0, done.stderr
        assert done.stdout == expected.stdout and done.stderr == expected.stderr


def test_convert_command(tmp_path: Path) -> None:
    source = ROOT / "data" / "orders.csv"
    orders = read_orders(source.read_text())
    out = tmp_path / "semi.csv"
    done = run("convert", "--orders", str(source), "--out", str(out), "--delimiter", ";", "--decimal", ",")
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"converted {len(orders)} orders\n"
    assert out.read_text() == write_orders(orders, delimiter=";", decimal=",")
    back = tmp_path / "back.csv"
    done = run("convert", "--orders", str(out), "--out", str(back))
    assert done.returncode == 0, done.stderr
    assert back.read_text() == write_orders(orders)
    tabbed = tmp_path / "tab.csv"
    done = run("convert", "--orders", str(out), "--out", str(tabbed), "--delimiter", "\t")
    assert done.returncode == 0, done.stderr
    assert tabbed.read_text() == write_orders(orders, delimiter="\t")
    single = tmp_path / "one.csv"
    single.write_text(write_orders([ORDER], delimiter="|"))
    done = run("convert", "--orders", str(single), "--out", str(tmp_path / "one-out.csv"))
    assert done.stdout == "converted 1 order\n"
    clash = tmp_path / "clash.csv"
    done = run("convert", "--orders", str(source), "--out", str(clash), "--decimal", ",")
    assert done.returncode == 1 and done.stderr.startswith("stockroom: error:")
    assert not clash.exists() and done.stdout == ""
    bad = tmp_path / "bad.csv"
    bad.write_text("sku;on_hand;reserved\nMUG-001;1;0\n")
    done = run("convert", "--orders", str(bad), "--out", str(clash))
    assert done.returncode == 1 and done.stderr.startswith("stockroom: error:")
    assert not clash.exists()
    done = run("convert", "--orders", str(source), "--out", str(clash), "--delimiter", ":")
    assert done.returncode == 2
