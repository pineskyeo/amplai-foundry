from pathlib import Path

from stockroom.csvio import read_orders
from stockroom.orders import OrderBook
from stockroom.report import customer_totals, render_table, sales_summary, top_n

DATA = Path(__file__).resolve().parents[1] / "data"


def test_render_table() -> None:
    text = render_table(["A", "B"], [["alpha", 1], ["be", 22222]], align_right=[1])
    assert text.splitlines() == [
        "A          B",
        "-----  -----",
        "alpha      1",
        "be     22222",
    ]


def test_render_empty_table() -> None:
    assert render_table(["Name", "Qty"], []) == "Name  Qty\n----  ---"


def test_top_n() -> None:
    totals = {"b": 5, "a": 5, "c": 9, "d": 1}
    assert top_n(totals, 2) == [("c", 9), ("a", 5)]
    assert top_n(totals, 10) == [("c", 9), ("a", 5), ("b", 5), ("d", 1)]


def test_sales_summary() -> None:
    book = OrderBook(read_orders((DATA / "orders.csv").read_text()))
    assert customer_totals(book) == {"Hana Kim": 8835, "Omar Diaz": 4650, "Lena Berg": 1480}
    text = sales_summary(book)
    lines = text.splitlines()
    assert lines[0] == "5 orders, 1 cancelled"
    assert lines[1] == "Revenue: $149.65"
    for month, amount in (("2024-01", "$67.95"), ("2024-02", "$54.40"), ("2024-03", "$27.30")):
        assert any(month in line and amount in line for line in lines)
    names = [line.split("  ")[0] for line in lines if line.startswith(("Hana", "Omar", "Lena"))]
    assert names == ["Hana Kim", "Omar Diaz", "Lena Berg"]
