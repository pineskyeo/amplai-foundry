import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from stockroom.csvio import read_orders
from stockroom.dates import fiscal_week, week_bounds
from stockroom.errors import ParseError
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"


def _oracle() -> tuple[dict[date, str], list[str]]:
    """Every date of 1999-2036 with its label, built by walking the Saturdays of each year."""
    labels: dict[date, str] = {}
    order: list[str] = []
    for year in range(1999, 2037):
        saturday = date(year, 1, 1)
        while saturday.weekday() != 5:
            saturday += timedelta(days=1)
        week = 1
        while saturday.year == year:
            label = f"{year:04d}-W{week:02d}"
            order.append(label)
            for back in range(7):
                labels[saturday - timedelta(days=back)] = label
            saturday += timedelta(days=7)
            week += 1
    return labels, order


ORACLE, LABELS = _oracle()


def _order(order_id: str, placed: date, total: int, status: str = "paid") -> Order:
    return Order(order_id, "Hana Kim", placed, (OrderLine("MUG-001", 1, total),), status)


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


def test_fiscal_week_labels_of_the_stated_examples() -> None:
    assert fiscal_week(date(2024, 1, 1)) == "2024-W01"
    assert fiscal_week(date(2023, 12, 31)) == "2024-W01"
    assert fiscal_week(date(2024, 1, 6)) == "2024-W01"
    assert fiscal_week(date(2024, 1, 7)) == "2024-W02"
    assert fiscal_week(date(2022, 1, 1)) == "2022-W01"
    assert fiscal_week(date(2021, 12, 31)) == "2022-W01"
    assert fiscal_week(date(2021, 12, 26)) == "2022-W01"
    assert fiscal_week(date(2021, 12, 25)) == "2021-W52"
    assert fiscal_week(date(2024, 12, 28)) == "2024-W52"
    assert fiscal_week(date(2024, 12, 29)) == "2025-W01"
    assert fiscal_week(date(2022, 12, 31)) == "2022-W53"
    assert fiscal_week(date(2022, 12, 25)) == "2022-W53"
    assert fiscal_week(date(2023, 1, 1)) == "2023-W01"


def test_fiscal_week_follows_the_rule_for_every_day_from_2000_to_2035() -> None:
    day = date(2000, 1, 1)
    while day <= date(2035, 12, 31):
        assert fiscal_week(day) == ORACLE[day], day
        day += timedelta(days=1)


def test_week_bounds_gives_the_sunday_and_the_saturday_of_the_week() -> None:
    assert week_bounds("2024-W01") == (date(2023, 12, 31), date(2024, 1, 6))
    assert week_bounds("2022-W53") == (date(2022, 12, 25), date(2022, 12, 31))
    assert week_bounds("2025-W01") == (date(2024, 12, 29), date(2025, 1, 4))
    for label in LABELS:
        if not 2000 <= int(label[:4]) <= 2035:
            continue
        start, end = week_bounds(label)
        assert start.weekday() == 6 and end.weekday() == 5 and (end - start).days == 6
        assert end.year == int(label[:4])
        for back in range(7):
            assert fiscal_week(end - timedelta(days=back)) == label


def test_week_bounds_refuses_text_that_is_not_a_week() -> None:
    weeks_of_year = {y: sum(1 for label in LABELS if label.startswith(f"{y:04d}-")) for y in range(2000, 2036)}
    assert weeks_of_year[2022] == 53 and weeks_of_year[2023] == 52 and weeks_of_year[2024] == 52
    for year, count in weeks_of_year.items():
        week_bounds(f"{year:04d}-W{count:02d}")
        for bad in (f"{year:04d}-W{count + 1:02d}", f"{year:04d}-W00"):
            with pytest.raises(ParseError) as caught:
                week_bounds(bad)
            assert str(caught.value) == f"not a week: {bad!r}"
    for text in (
        "",
        "2024-W1",
        "2024-W001",
        "24-W01",
        "2024W01",
        "2024-w01",
        " 2024-W01",
        "2024-W01 ",
        "2024-W01\n",
        "2024-W5a",
        "2024-W99",
        "\uff12\uff10\uff12\uff14-W01",
    ):
        with pytest.raises(ParseError) as caught:
            week_bounds(text)
        assert str(caught.value) == f"not a week: {text!r}"


def test_the_weekly_revenue_lists_every_week_between_the_first_and_the_last_sale() -> None:
    weeks = OrderBook(read_orders((DATA / "orders.csv").read_text())).revenue_by_fiscal_week()
    assert list(weeks) == [f"2024-W{n:02d}" for n in range(2, 13)]
    assert list(weeks.values()) == [3395, 3400, 0, 0, 0, 5440, 0, 1480, 0, 0, 1250]
    assert sum(weeks.values()) == 14965


def test_the_weekly_revenue_crosses_a_year_boundary_and_skips_cancelled_orders() -> None:
    book = OrderBook(
        [
            _order("Y-1", date(2023, 12, 20), 700),
            _order("Y-2", date(2023, 12, 22), 300, "shipped"),
            _order("Y-3", date(2024, 1, 10), 1000, "open"),
            _order("Y-4", date(2024, 1, 20), 5000, "cancelled"),
            _order("Y-5", date(2024, 1, 1), 50, "cancelled"),
        ]
    )
    assert book.revenue_by_fiscal_week() == {
        "2023-W51": 1000,
        "2023-W52": 0,
        "2024-W01": 0,
        "2024-W02": 1000,
    }
    lone = OrderBook([_order("L-1", date(2024, 5, 9), 250)])
    assert lone.revenue_by_fiscal_week() == {"2024-W19": 250}
    only_cancelled = OrderBook([_order("C-1", date(2024, 5, 9), 250, "cancelled")])
    assert only_cancelled.revenue_by_fiscal_week() == {}
    assert OrderBook().revenue_by_fiscal_week() == {}
    wide = OrderBook([_order("W-1", date(2021, 12, 29), 100), _order("W-2", date(2022, 1, 12), 200)])
    assert wide.revenue_by_fiscal_week() == {"2022-W01": 100, "2022-W02": 0, "2022-W03": 200}


def test_the_weekly_revenue_labels_follow_the_rule_for_random_orders() -> None:
    import random

    rng = random.Random(3)
    for _ in range(50):
        orders = [
            _order(f"R-{n}", date(2019, 1, 1) + timedelta(days=rng.randrange(0, 2500)), rng.randrange(1, 900), rng.choice(["paid", "cancelled", "open"]))
            for n in range(rng.randrange(0, 12))
        ]
        weeks = OrderBook(orders).revenue_by_fiscal_week()
        live = [o for o in orders if o.status != "cancelled"]
        if not live:
            assert weeks == {}
            continue
        first = LABELS.index(ORACLE[min(o.placed for o in live)])
        last = LABELS.index(ORACLE[max(o.placed for o in live)])
        assert list(weeks) == LABELS[first : last + 1]
        for label in weeks:
            expected = sum(o.lines[0].unit_price_cents for o in live if ORACLE[o.placed] == label)
            assert weeks[label] == expected


def test_sales_weekly_prints_the_summary_then_one_line_per_week() -> None:
    plain = run("sales", "--orders", "data/orders.csv")
    weekly = run("sales", "--orders", "data/orders.csv", "--weekly")
    assert plain.returncode == 0 and weekly.returncode == 0, weekly.stderr
    assert "Weekly revenue" not in plain.stdout
    assert weekly.stdout.startswith(plain.stdout + "\nWeekly revenue\n")
    assert weekly.stdout[len(plain.stdout) :].splitlines() == [
        "",
        "Weekly revenue",
        "2024-W02  $33.95",
        "2024-W03  $34.00",
        "2024-W04  $0.00",
        "2024-W05  $0.00",
        "2024-W06  $0.00",
        "2024-W07  $54.40",
        "2024-W08  $0.00",
        "2024-W09  $14.80",
        "2024-W10  $0.00",
        "2024-W11  $0.00",
        "2024-W12  $12.50",
    ]
    assert weekly.stderr == ""


def test_sales_weekly_follows_from_and_the_currency_setting() -> None:
    narrowed = run("sales", "--orders", "data/orders.csv", "--from", "2024-03-01", "--weekly")
    assert narrowed.returncode == 0, narrowed.stderr
    assert narrowed.stdout.split("Weekly revenue\n", 1)[1].splitlines() == [
        "2024-W09  $14.80",
        "2024-W10  $0.00",
        "2024-W11  $0.00",
        "2024-W12  $12.50",
    ]
    euro = run("sales", "--orders", "data/orders.csv", "--weekly", env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert euro.stdout.split("Weekly revenue\n", 1)[1].splitlines()[0] == "2024-W02  EUR 33.95"
