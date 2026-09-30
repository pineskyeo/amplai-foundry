from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from stockroom.analysis import abc_classes, sku_revenue
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook

ROOT = Path(__file__).resolve().parents[3]


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_sku_revenue() -> None:
    book = OrderBook(
        [
            Order(
                "AAA-001",
                "Hana Kim",
                date(2024, 1, 1),
                (OrderLine("MUG-001", 2, 1250), OrderLine("TEA-010", 1, 895)),
                "shipped",
            ),
            Order(
                "A-2",
                "Omar Diaz",
                date(2024, 1, 2),
                (OrderLine("MUG-001", 3, 1250), OrderLine("MUG-001", 1, 1200)),
                "open",
            ),
            Order("A-3", "Lena Berg", date(2024, 1, 3), (OrderLine("PEN-100", 5, 3400),), "cancelled"),
            Order("A-4", "Lena Berg", date(2024, 1, 4), (OrderLine("TEA-010", 2, 895),), "paid"),
        ]
    )
    assert sku_revenue(book) == {"MUG-001": 2500 + 3750 + 1200, "TEA-010": 895 + 1790}
    assert sku_revenue(OrderBook()) == {}


def test_sku_revenue_zero_price_and_cancelled_only() -> None:
    book = OrderBook(
        [
            Order("AAA-001", "Hana Kim", date(2024, 1, 1), (OrderLine("NB-200", 4, 0),), "paid"),
            Order("A-2", "Hana Kim", date(2024, 1, 2), (OrderLine("PEN-100", 1, 3400),), "cancelled"),
        ]
    )
    result = sku_revenue(book)
    assert result == {"NB-200": 0} and "PEN-100" not in result


def test_ranking_order_and_ties() -> None:
    result = abc_classes({"BBB-002": 10, "AAA-001": 10, "CCC-003": 50, "DDD-004": 10, "EEE-005": 0})
    assert list(result) == ["CCC-003", "AAA-001", "BBB-002", "DDD-004", "EEE-005"]
    assert isinstance(result, dict)
    assert list(abc_classes({"ZZZ-001": 7, "AAA-001": 7, "MMM-001": 7})) == ["AAA-001", "MMM-001", "ZZZ-001"]


def test_empty_revenue() -> None:
    assert abc_classes({}) == {}
    with pytest.raises(ValueError):
        abc_classes({}, a_pct=95, b_pct=80)


def test_class_boundaries() -> None:
    revenue = {"XXX-001": 50, "YYY-002": 30, "ZZZ-003": 15, "WWW-004": 5}
    assert abc_classes(revenue) == {"XXX-001": "A", "YYY-002": "A", "ZZZ-003": "B", "WWW-004": "C"}
    # Z starts exactly at 80 %: not below 80 -> B; W starts exactly at 95 %: not below -> C
    assert list(abc_classes(revenue).values()) == ["A", "A", "B", "C"]
    exact80 = {"XXX-001": 8000, "YYY-002": 1000, "ZZZ-003": 1000}
    assert list(abc_classes(exact80).values()) == ["A", "B", "B"]
    just_below80 = {"XXX-001": 7999, "YYY-002": 1001, "ZZZ-003": 1000}
    assert list(abc_classes(just_below80).values()) == ["A", "A", "B"]
    exact95 = {"XXX-001": 5000, "YYY-002": 4500, "ZZZ-003": 500}
    assert list(abc_classes(exact95).values()) == ["A", "A", "C"]
    just_below95 = {"XXX-001": 5000, "YYY-002": 4499, "ZZZ-003": 501}
    assert list(abc_classes(just_below95).values()) == ["A", "A", "B"]


def test_crossing_sku_keeps_its_class() -> None:
    assert abc_classes({"AAA-001": 90, "BBB-002": 6, "CCC-003": 4}) == {"AAA-001": "A", "BBB-002": "B", "CCC-003": "C"}
    assert abc_classes({"AAA-001": 100}) == {"AAA-001": "A"}
    assert abc_classes({"AAA-001": 97, "BBB-002": 3}) == {"AAA-001": "A", "BBB-002": "C"}
    assert abc_classes({"AAA-001": 1000, "BBB-002": 1}) == {"AAA-001": "A", "BBB-002": "C"}
    assert abc_classes({"AAA-001": 85, "BBB-002": 9, "CCC-003": 6}) == {"AAA-001": "A", "BBB-002": "B", "CCC-003": "B"}
    big = {"AAA-001": 10**12, "BBB-002": 10**12 - 1, "CCC-003": 1}
    assert abc_classes(big) == {"AAA-001": "A", "BBB-002": "A", "CCC-003": "C"}


def test_zero_total_is_all_c() -> None:
    assert abc_classes({"AAA-001": 0, "BBB-002": 0}) == {"AAA-001": "C", "BBB-002": "C"}


def test_custom_thresholds() -> None:
    revenue = {"XXX-001": 50, "YYY-002": 30, "ZZZ-003": 15, "WWW-004": 5}
    assert list(abc_classes(revenue, a_pct=50, b_pct=80).values()) == ["A", "B", "C", "C"]
    assert list(abc_classes(revenue, a_pct=51, b_pct=80).values()) == ["A", "A", "C", "C"]
    assert list(abc_classes(revenue, a_pct=10, b_pct=100).values()) == ["A", "B", "B", "B"]
    assert list(abc_classes(revenue, a_pct=99, b_pct=100).values()) == ["A", "A", "A", "A"]
    assert list(abc_classes(revenue, a_pct=1, b_pct=2).values()) == ["A", "C", "C", "C"]


@pytest.mark.parametrize(
    ("a_pct", "b_pct"), [(0, 95), (80, 80), (90, 80), (80, 101), (-1, 50), (100, 100), (0, 0)]
)
def test_invalid_thresholds(a_pct: int, b_pct: int) -> None:
    with pytest.raises(ValueError):
        abc_classes({"AAA-001": 10, "BBB-002": 5}, a_pct=a_pct, b_pct=b_pct)


def test_negative_revenue_and_no_mutation() -> None:
    with pytest.raises(ValueError):
        abc_classes({"AAA-001": 10, "BBB-002": -1})
    revenue = {"BBB-002": 5, "AAA-001": 10}
    abc_classes(revenue)
    assert revenue == {"BBB-002": 5, "AAA-001": 10} and list(revenue) == ["BBB-002", "AAA-001"]


def test_cli_defaults_on_sample_data() -> None:
    done = run("abc", "--orders", "data/orders.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "A  PEN-100  $34.00",
        "A  CND-007  $29.40",
        "A  MUG-001  $25.00",
        "A  NB-200  $25.00",
        "A  TEA-011  $14.80",
        "B  MUG-002  $12.50",
        "B  TEA-010  $8.95",
    ]


def test_cli_custom_thresholds() -> None:
    done = run("abc", "--orders", "data/orders.csv", "--a", "50", "--b", "80")
    assert done.returncode == 0, done.stderr
    assert [line.split("  ")[0] for line in done.stdout.splitlines()] == list("AAABBCC")
    done = run(
        "abc",
        "--orders",
        "data/orders.csv",
        "--b",
        "90",
        env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "A  PEN-100  EUR 34.00"
    assert [line.split("  ")[0] for line in done.stdout.splitlines()] == list("AAAAABC")


def test_cli_no_sales_and_errors(tmp_path: Path) -> None:
    orders = tmp_path / "orders.csv"
    orders.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "A-1,Hana Kim,2024-01-05,cancelled,MUG-001,1,12.50\n"
    )
    done = run("abc", "--orders", str(orders))
    assert done.returncode == 0 and done.stdout.splitlines() == ["no sales"]
    for a, b in (("90", "80"), ("0", "95"), ("80", "101"), ("80", "80")):
        done = run("abc", "--orders", "data/orders.csv", "--a", a, "--b", b)
        assert done.returncode == 1 and done.stdout == ""
        assert done.stderr.startswith("stockroom: error: ")
    assert run("abc", "--orders", "data/orders.csv", "--a", "lots").returncode == 2
    assert run("abc", "--orders", str(orders), "--a", "90", "--b", "80").returncode == 1
