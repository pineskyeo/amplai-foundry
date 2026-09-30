import os
from pathlib import Path

import pytest

from stockroom.cli import main
from stockroom.csvio import read_orders
from stockroom.errors import ParseError
from stockroom.money import parse_money


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


ORDER_HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("1,234", 123400),
        ("1,234.50", 123450),
        ("12,345.6", 1234560),
        ("123,456", 12345600),
        ("1,234,567.89", 123456789),
        ("999,999,999.99", 99999999999),
        ("$1,250.00", 125000),
        ("-1,234.50", -123450),
        ("  $12,345  ", 1234500),
        ("1234", 123400),
        ("1234567.89", 123456789),
        ("0.50", 50),
        ("$1,000", 100000),
        ("100", 10000),
    ],
)
def test_well_formed_amounts_are_read(text: str, cents: int) -> None:
    assert parse_money(text) == cents


@pytest.mark.parametrize(
    "text",
    [
        "1,2,3",
        "12,34",
        "1,23",
        "1,2345",
        "1234,567",
        ",123",
        "1,",
        "1,234,",
        "1,,234",
        "1,234.5,6",
        "1.234,5",
        "$,123",
        "-,123",
        "1, 234",
        "1 ,234",
        "1,$234",
        "3,50",
        "0,5",
        "1,23,456",
        "1,234,56",
        "12,3456",
        "$1,2,3.00",
        "-1,23.45",
        "1,234.567",
    ],
)
def test_malformed_grouping_is_refused(text: str) -> None:
    with pytest.raises(ParseError):
        parse_money(text)


def test_commas_are_never_allowed_after_the_decimal_point() -> None:
    for text in ("1.2,3", "1.,50", "1,234.5,0", "0.5,", "12.3,4"):
        with pytest.raises(ParseError):
            parse_money(text)


def test_order_file_unit_prices_follow_the_same_rule() -> None:
    good = ORDER_HEADER + 'A-1,Hana Kim,2024-01-08,open,MUG-001,1,"1,250.00"\n'
    assert read_orders(good)[0].lines[0].unit_price_cents == 125000
    for bad in ("1,2,3", "12,50", "3,50", "1,23.00"):
        text = ORDER_HEADER + f'A-1,Hana Kim,2024-01-08,open,MUG-001,1,"{bad}"\n'
        with pytest.raises(ParseError):
            read_orders(text)


def test_cli_refuses_a_misgrouped_amount(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "orders.csv"
    path.write_text(ORDER_HEADER + 'A-1,Hana Kim,2024-01-08,open,MUG-001,1,"3,50"\n')
    assert main(["sales", "--orders", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("stockroom: error: ")
    assert captured.out == ""
    path.write_text(ORDER_HEADER + 'A-1,Hana Kim,2024-01-08,open,MUG-001,1,"1,350.00"\n')
    assert main(["sales", "--orders", str(path)]) == 0
    assert "Revenue: $1,350.00" in capsys.readouterr().out
