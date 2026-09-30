import random
import re
import subprocess
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from stockroom.csvio import read_orders
from stockroom.dates import week_start
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"


def _book() -> OrderBook:
    return OrderBook(read_orders((DATA / "orders.csv").read_text()))


def _order(order_id: str, placed: date, quantity: int, price: int, status: str = "paid") -> Order:
    return Order(order_id, "Hana Kim", placed, (OrderLine("MUG-001", quantity, price),), status)


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


def test_week_start_is_the_first_day_of_a_seven_day_week_alt() -> None:
    rng = random.Random(5)
    for _ in range(300):
        day = date(2020, 1, 1) + timedelta(days=rng.randrange(0, 2500))
        start = week_start(day)
        assert start <= day and (day - start).days < 7
        assert week_start(start) == start
        for k in range(7):
            assert week_start(start + timedelta(days=k)) == start
        assert week_start(start + timedelta(days=7)) == start + timedelta(days=7)


def test_revenue_by_week_groups_by_the_week_of_the_placement_date_alt() -> None:
    orders = [
        _order("W-1", date(2024, 3, 6), 2, 500),
        _order("W-2", date(2024, 3, 7), 1, 250),
        _order("W-3", date(2024, 3, 20), 1, 900),
        _order("W-4", date(2024, 3, 21), 3, 100, "cancelled"),
        _order("W-5", date(2024, 4, 10), 1, 100, "cancelled"),
        _order("W-6", date(2024, 1, 15), 1, 40, "open"),
        _order("W-7", date(2023, 12, 28), 4, 25, "shipped"),
    ]
    weeks = OrderBook(orders).revenue_by_week()
    expected: dict[str, int] = {}
    for order in orders:
        if order.status != "cancelled":
            key = week_start(order.placed).isoformat()
            expected[key] = expected.get(key, 0) + order.lines[0].quantity * order.lines[0].unit_price_cents
    assert weeks == expected
    assert list(weeks) == sorted(weeks)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", key) for key in weeks)
    assert weeks[week_start(date(2024, 3, 6)).isoformat()] == 1250
    assert week_start(date(2024, 4, 10)).isoformat() not in weeks
    assert weeks[week_start(date(2024, 3, 20)).isoformat()] == 900
    assert OrderBook().revenue_by_week() == {}


def test_weekly_breakdown_of_the_sample_orders_adds_up_to_the_revenue_alt() -> None:
    weeks = _book().revenue_by_week()
    assert len(weeks) == 5
    assert sum(weeks.values()) == 14965
    assert list(weeks) == sorted(weeks)
    assert list(weeks.values()) == [3395, 3400, 5440, 1480, 1250]
    for key in weeks:
        assert week_start(date.fromisoformat(key)) == date.fromisoformat(key)


def test_sales_weekly_prints_the_summary_then_the_weekly_lines_alt() -> None:
    plain = run("sales", "--orders", "data/orders.csv")
    weekly = run("sales", "--orders", "data/orders.csv", "--weekly")
    assert plain.returncode == 0 and weekly.returncode == 0, weekly.stderr
    assert "Weekly revenue" not in plain.stdout
    assert weekly.stdout.startswith(plain.stdout + "\nWeekly revenue\n")
    rest = weekly.stdout[len(plain.stdout) + len("\nWeekly revenue\n") :].splitlines()
    assert len(rest) == 5
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}  \$[\d,]+\.\d{2}", line) for line in rest)
    assert [line.split("  ")[1] for line in rest] == ["$33.95", "$34.00", "$54.40", "$14.80", "$12.50"]
    assert [line.split("  ")[0] for line in rest] == sorted(line.split("  ")[0] for line in rest)
    assert weekly.stdout.endswith("\n") and not weekly.stdout.endswith("\n\n")


def test_sales_weekly_follows_from_and_the_currency_setting_alt() -> None:
    narrowed = run("sales", "--orders", "data/orders.csv", "--from", "2024-02-01", "--weekly")
    assert narrowed.returncode == 0, narrowed.stderr
    tail = narrowed.stdout.split("Weekly revenue\n", 1)[1].splitlines()
    assert [line.split("  ")[1] for line in tail] == ["$54.40", "$14.80", "$12.50"]
    euro = run(
        "sales",
        "--orders",
        "data/orders.csv",
        "--weekly",
        env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    )
    tail = euro.stdout.split("Weekly revenue\n", 1)[1].splitlines()
    assert [line.split("  ")[1] for line in tail][0] == "EUR 33.95"


def test_weeks_start_on_sunday() -> None:
    assert week_start(date(2024, 2, 3)) == date(2024, 1, 28)
    assert week_start(date(2024, 2, 4)) == date(2024, 2, 4)
    assert week_start(date(2024, 2, 5)) == date(2024, 2, 4)
    assert week_start(date(2024, 1, 8)) == date(2024, 1, 7)
    assert week_start(date(2024, 2, 4)).weekday() == 6


def test_sample_orders_fall_in_sunday_weeks() -> None:
    assert _book().revenue_by_week() == {
        "2024-01-07": 3395,
        "2024-01-14": 3400,
        "2024-02-11": 5440,
        "2024-02-25": 1480,
        "2024-03-17": 1250,
    }
    done = run("sales", "--orders", "data/orders.csv", "--weekly")
    assert done.stdout.split("Weekly revenue\n", 1)[1].splitlines()[:2] == [
        "2024-01-07  $33.95",
        "2024-01-14  $34.00",
    ]
