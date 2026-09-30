from __future__ import annotations

import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from stockroom.config import load_config
from stockroom.csvio import read_items, read_orders, read_stock, write_items, write_orders
from stockroom.errors import ConfigError, ParseError
from stockroom.models import Item, Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]
BOM = "﻿"

ITEMS = "sku,name,price,tags,reorder_level\nMUG-001,Blue Mug,12.50,kitchen;gift,5\nNB-200,Notebook,6.25,,0\n"
STOCK = "sku,on_hand,reserved\nMUG-001,14,2\nNB-200,3,0\n"
ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "A-1,Hana Kim,2024-01-08,paid,MUG-001,2,12.50\n"
    "A-1,Hana Kim,2024-01-08,paid,NB-200,1,6.25\n"
    "A-2,Omar Diaz,2024-01-19,open,NB-200,4,6.25\n"
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


def test_readers_accept_a_leading_bom() -> None:
    assert read_items(BOM + ITEMS) == read_items(ITEMS)
    assert read_stock(BOM + STOCK) == read_stock(STOCK)
    assert read_orders(BOM + ORDERS) == read_orders(ORDERS)
    crlf = "\r\n"
    assert read_items(BOM + ITEMS.replace("\n", crlf)) == read_items(ITEMS)
    assert read_stock(BOM + STOCK.replace("\n", crlf)) == read_stock(STOCK)
    assert read_orders(BOM + ORDERS.replace("\n", crlf)) == read_orders(ORDERS)
    assert len(read_items(BOM + ITEMS)) == 2
    assert read_items(BOM + ITEMS)[0].name == "Blue Mug"
    assert read_orders(BOM + ORDERS)[0].customer == "Hana Kim"
    # the mark is removed before anything else is looked at: blank lines after it are ignored
    assert read_items(BOM + "\n\n" + ITEMS) == read_items(ITEMS)


@pytest.mark.parametrize("text", [BOM, BOM + "\n", BOM + "  \n\n", BOM * 2 + ITEMS])
def test_a_bom_alone_or_a_second_bom_is_refused_by_items(text: str) -> None:
    with pytest.raises(ParseError):
        read_items(text)


@pytest.mark.parametrize("text", [BOM, BOM + "\n", BOM + "\n\n", BOM * 2 + STOCK])
def test_a_bom_alone_or_a_second_bom_is_refused_by_stock(text: str) -> None:
    with pytest.raises(ParseError):
        read_stock(text)


@pytest.mark.parametrize("text", [BOM, BOM + "\n", BOM + "\n\n", BOM * 2 + ORDERS])
def test_a_bom_alone_or_a_second_bom_is_refused_by_orders(text: str) -> None:
    with pytest.raises(ParseError):
        read_orders(text)


def test_only_a_leading_bom_is_removed() -> None:
    orders = read_orders(BOM + ORDERS.replace("Omar Diaz", f"Omar{BOM}Diaz"))
    assert orders[1].customer == f"Omar{BOM}Diaz"
    items = read_items(BOM + ITEMS.replace("Notebook", f"Note{BOM}book"))
    assert items[1].name == f"Note{BOM}book"
    assert read_items(ITEMS.replace("Notebook", f"{BOM}Notebook"))[1].name == f"{BOM}Notebook"
    with pytest.raises(ParseError):
        read_items(ITEMS.replace("MUG-001,Blue", f"{BOM}MUG-001,Blue"))
    with pytest.raises(ParseError):
        read_stock(STOCK.replace("NB-200", f"{BOM}NB-200"))
    with pytest.raises(ParseError):
        read_items(ITEMS.replace("sku,name", f"sku,{BOM}name"))


def test_writers_can_start_with_a_bom() -> None:
    items = [Item("MUG-001", "Blue Mug", 1250, ("kitchen",), 5)]
    orders = [Order("A-1", "Hana Kim", date(2024, 1, 8), (OrderLine("MUG-001", 2, 1250),))]
    assert write_items(items, bom=True) == BOM + write_items(items)
    assert write_orders(orders, bom=True) == BOM + write_orders(orders)
    assert write_items(items, bom=False) == write_items(items)
    assert not write_items(items).startswith(BOM)
    assert not write_orders(orders).startswith(BOM)
    assert read_items(write_items(items, bom=True)) == items
    assert read_orders(write_orders(orders, bom=True)) == orders


def _generated(seed: int) -> tuple[list[Item], list[Order]]:
    rng = random.Random(seed)
    alphabet = "abcdefghijklmnopqrstuvwxyzÉéüñ0123456789 -"
    items = []
    for n in range(rng.randrange(1, 8)):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 15))).strip()
        name = name or "x"
        tags = tuple(rng.sample(["home", "gift", "tea", "kitchen", "office"], rng.randrange(0, 3)))
        items.append(Item(f"GEN-{100 + n}", name, rng.randrange(0, 99999), tags, rng.randrange(0, 9)))
    orders = []
    for n in range(rng.randrange(1, 6)):
        customer = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 12))).strip() or "y"
        lines = tuple(
            OrderLine(rng.choice(items).sku, rng.randrange(1, 9), rng.randrange(0, 99999))
            for _ in range(rng.randrange(1, 4))
        )
        orders.append(
            Order(
                f"G-{n}",
                customer,
                date(2024, 1, 1) + timedelta(days=rng.randrange(0, 300)),
                lines,
                rng.choice(["open", "paid", "shipped", "cancelled"]),
            )
        )
    return items, orders


def test_generated_files_round_trip_with_and_without_a_bom() -> None:
    for seed in range(200):
        items, orders = _generated(seed)
        for bom in (False, True):
            for newline in ("\n", "\r\n"):
                text = write_items(items, bom=bom).replace("\n", newline)
                assert read_items(text) == items, (seed, bom, newline)
                text = write_orders(orders, bom=bom).replace("\n", newline)
                assert read_orders(text) == orders, (seed, bom, newline)


def test_settings_file_with_a_bom(tmp_path: Path) -> None:
    body = "[stockroom]\ncurrency_symbol = EUR \ntax_bp = 825\nlow_stock_warning = no\n"
    plain = tmp_path / "plain.ini"
    plain.write_bytes(body.encode("utf-8"))
    marked = tmp_path / "marked.ini"
    marked.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    assert load_config(marked) == load_config(plain)
    assert load_config(marked)["tax_bp"] == 825
    assert load_config(marked)["low_stock_warning"] is False
    assert load_config(marked, {"STOCKROOM_TAX_BP": "100"})["tax_bp"] == 100
    accented = tmp_path / "accented.ini"
    accented.write_bytes(b"\xef\xbb\xbf[stockroom]\ncurrency_symbol = \xc2\xa3\n")
    assert load_config(accented)["currency_symbol"] == "£"
    nosection = tmp_path / "nosection.ini"
    nosection.write_bytes(b"\xef\xbb\xbf; only a comment\n")
    assert load_config(nosection) == load_config()
    unknown = tmp_path / "unknown.ini"
    unknown.write_bytes(b"\xef\xbb\xbf[stockroom]\ncolour = blue\n")
    with pytest.raises(ConfigError):
        load_config(unknown)
    twice = tmp_path / "twice.ini"
    twice.write_bytes(b"\xef\xbb\xbf\xef\xbb\xbf" + body.encode("utf-8"))
    with pytest.raises(ConfigError):
        load_config(twice)


def test_command_line_reads_files_with_a_bom(tmp_path: Path) -> None:
    plain = {"items.csv": ITEMS, "stock.csv": STOCK, "orders.csv": ORDERS}
    for name, text in plain.items():
        (tmp_path / name).write_bytes(text.encode("utf-8"))
        (tmp_path / f"bom-{name}").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    (tmp_path / "bom.ini").write_bytes(b"\xef\xbb\xbf[stockroom]\ncurrency_symbol = EUR \n")
    (tmp_path / "plain.ini").write_bytes(b"[stockroom]\ncurrency_symbol = EUR \n")
    jobs = [
        ("items", "--file", "{p}items.csv"),
        ("stock", "--items", "{p}items.csv", "--stock", "{p}stock.csv"),
        ("sales", "--orders", "{p}orders.csv"),
    ]
    for job in jobs:
        expected = run(*(a.format(p=f"{tmp_path}/") for a in job))
        marked = run(*(a.format(p=f"{tmp_path}/bom-") for a in job))
        assert expected.returncode == 0, expected.stderr
        assert marked.returncode == 0, marked.stderr
        assert marked.stdout == expected.stdout and marked.stderr == expected.stderr
    done = run("--config", str(tmp_path / "bom.ini"), "items", "--file", f"{tmp_path}/bom-items.csv")
    same = run("--config", str(tmp_path / "plain.ini"), "items", "--file", f"{tmp_path}/items.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout == same.stdout and "EUR12.50" in done.stdout
    bad = tmp_path / "bad-items.csv"
    bad.write_bytes(b"\xef\xbb\xbfsku,name\nMUG-001,Mug\n")
    refused = run("items", "--file", str(bad))
    assert refused.returncode == 1 and refused.stderr.startswith("stockroom: error:")
