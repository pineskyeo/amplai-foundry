import os
from datetime import date
from pathlib import Path

import pytest

from stockroom.cli import main
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.report import render_table, sales_summary


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


DATA = Path(__file__).resolve().parents[3] / "data"


def test_a_header_wider_than_its_cells_widens_the_column() -> None:
    text = render_table(["Name", "Quantity"], [["a", "1"], ["bb", "22"]])
    assert text.splitlines() == [
        "Name  Quantity",
        "----  --------",
        "a     1",
        "bb    22",
    ]
    assert render_table(["Description"], [["a"], ["bc"]]).splitlines() == [
        "Description",
        "-----------",
        "a",
        "bc",
    ]


def test_right_aligned_columns_align_the_header_with_their_cells() -> None:
    text = render_table(["Item", "Total"], [["x", 5], ["yy", 10]], align_right=[1])
    assert text.splitlines() == [
        "Item  Total",
        "----  -----",
        "x         5",
        "yy       10",
    ]
    wide = render_table(["Left", "Right"], [["x", "1"]], align_right=[0, 1])
    assert wide.splitlines() == [
        "Left  Right",
        "----  -----",
        "   x      1",
    ]


def test_each_column_takes_the_wider_of_its_header_and_its_widest_cell() -> None:
    text = render_table(
        ["SKU", "Name", "Qty"], [["MUG-001", "Mug", 2], ["TEA-010", "Green Tea", 10]], align_right=[2]
    )
    assert text.splitlines() == [
        "SKU      Name       Qty",
        "-------  ---------  ---",
        "MUG-001  Mug          2",
        "TEA-010  Green Tea   10",
    ]
    text = render_table(["A", "Long header", "C"], [["wide cell", "x", "y"]])
    assert text.splitlines() == [
        "A          Long header  C",
        "---------  -----------  -",
        "wide cell  x            y",
    ]


def test_cells_are_converted_to_text_and_the_table_has_no_trailing_spaces() -> None:
    text = render_table(["Key", "Value"], [["a", None], ["b", 2.5], ["c", ""]])
    assert text.splitlines() == [
        "Key  Value",
        "---  -----",
        "a    None",
        "b    2.5",
        "c",
    ]
    assert all(line == line.rstrip() for line in text.splitlines())


def test_a_table_without_rows_is_as_wide_as_its_headers() -> None:
    assert render_table(["Name", "Qty"], []) == "Name  Qty\n----  ---"


def test_a_small_sales_summary_is_laid_out_by_its_headers() -> None:
    book = OrderBook(
        [Order("A-1", "Al", date(2024, 1, 5), (OrderLine("MUG-001", 1, 500),), "open")]
    )
    assert sales_summary(book).splitlines() == [
        "1 order, 0 cancelled",
        "Revenue: $5.00",
        "",
        "Month    Revenue",
        "-------  -------",
        "2024-01    $5.00",
        "",
        "Customer  Total",
        "--------  -----",
        "Al        $5.00",
    ]


def test_the_sales_command_lays_out_the_sample_data_by_its_headers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["sales", "--orders", str(DATA / "orders.csv")]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "5 orders, 1 cancelled",
        "Revenue: $149.65",
        "",
        "Month    Revenue",
        "-------  -------",
        "2024-01   $67.95",
        "2024-02   $54.40",
        "2024-03   $27.30",
        "",
        "Customer    Total",
        "---------  ------",
        "Hana Kim   $88.35",
        "Omar Diaz  $46.50",
        "Lena Berg  $14.80",
    ]
