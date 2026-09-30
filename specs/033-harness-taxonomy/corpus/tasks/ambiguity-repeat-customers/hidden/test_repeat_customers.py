import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from stockroom.csvio import read_orders, write_orders
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.report import repeat_customers

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"


def _orders(*rows: tuple[str, str]) -> list[Order]:
    return [
        Order(f"R-{n}", customer, date(2024, 4, 1 + n), (OrderLine("MUG-001", 1, 1250),), status)
        for n, (customer, status) in enumerate(rows)
    ]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=base,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_customers_with_several_orders_are_listed_once_and_sorted() -> None:
    book = OrderBook(
        _orders(
            ("Zed", "paid"),
            ("Ada", "open"),
            ("adam", "shipped"),
            ("Zed", "shipped"),
            ("Solo", "paid"),
            ("Ada", "paid"),
            ("adam", "paid"),
            ("Zed", "open"),
        )
    )
    assert repeat_customers(book) == ["Ada", "Zed", "adam"]
    assert repeat_customers(OrderBook()) == []


def test_customers_are_told_apart_by_their_exact_name() -> None:
    book = OrderBook(_orders(("Hana Kim", "paid"), ("hana kim", "paid"), ("Hana Kim ", "open")))
    assert repeat_customers(book) == []
    assert repeat_customers(OrderBook(_orders(("Hana Kim", "paid"), ("Hana Kim", "paid")))) == [
        "Hana Kim"
    ]


def test_cancelled_orders_do_not_hide_a_customer_with_enough_other_orders() -> None:
    book = OrderBook(
        _orders(("Ivo", "paid"), ("Ivo", "cancelled"), ("Ivo", "open"), ("Jun", "paid"))
    )
    assert repeat_customers(book) == ["Ivo"]


def test_the_sample_orders_list_hana_kim_and_omar_diaz() -> None:
    names = repeat_customers(OrderBook(read_orders((DATA / "orders.csv").read_text())))
    assert "Hana Kim" in names and "Omar Diaz" in names
    assert names == sorted(names)


def test_the_command_prints_one_name_per_line_or_says_there_are_none(tmp_path: Path) -> None:
    many = tmp_path / "many.csv"
    many.write_text(
        write_orders(_orders(("Zed", "paid"), ("Ada", "open"), ("Zed", "shipped"), ("Ada", "paid")))
    )
    done = run("repeat-customers", "--orders", str(many))
    assert done.returncode == 0, done.stderr
    assert done.stdout == "Ada\nZed\n" and done.stderr == ""
    none = tmp_path / "none.csv"
    none.write_text(write_orders(_orders(("Zed", "paid"), ("Ada", "open"))))
    done = run("repeat-customers", "--orders", str(none))
    assert done.returncode == 0 and done.stdout == "no repeat customers\n"


def test_every_order_counts_even_a_cancelled_one() -> None:
    assert repeat_customers(OrderBook(_orders(("Lena", "paid"), ("Lena", "cancelled")))) == ["Lena"]
    assert repeat_customers(OrderBook(_orders(("Lena", "cancelled"), ("Lena", "cancelled")))) == [
        "Lena"
    ]
    book = OrderBook(read_orders((DATA / "orders.csv").read_text()))
    assert repeat_customers(book) == ["Hana Kim", "Lena Berg", "Omar Diaz"]
    done = run("repeat-customers", "--orders", "data/orders.csv")
    assert done.stdout == "Hana Kim\nLena Berg\nOmar Diaz\n"
