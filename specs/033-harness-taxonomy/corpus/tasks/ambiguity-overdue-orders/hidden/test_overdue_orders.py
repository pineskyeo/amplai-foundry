import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook

ROOT = Path(__file__).resolve().parents[3]
AS_OF = date(2024, 3, 20)
ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "V-1,Hana Kim,2024-03-13,open,MUG-001,1,12.50\n"
    "V-2,Omar Diaz,2024-03-12,paid,PEN-100,1,34.00\n"
    "V-3,Lena Berg,2024-03-01,shipped,LMP-030,1,49.99\n"
    "V-4,Ari Moss,2024-03-01,cancelled,NB-200,2,6.25\n"
    "V-10,Bo Ek,2024-03-05,open,TEA-010,2,8.95\n"
    "V-9,Kai Lund,2024-03-05,paid,MUG-002,1,12.50\n"
    "V-11,Zoe Park,2024-03-25,open,MUG-001,1,12.50\n"
    "V-12,Ivy Holm,2024-03-19,open,CND-007,1,9.80\n"
)


def make(oid: str, placed: date, status: str = "open") -> Order:
    return Order(oid, "Mia Chen", placed, (OrderLine("MUG-001", 1, 1250),), status)


def ids(orders: list[Order]) -> list[str]:
    return [o.order_id for o in orders]


def run(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "orders.csv").write_text(ORDERS, encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", "overdue", "--orders", "orders.csv", *args],
        cwd=tmp_path,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_overdue_starts_the_day_after_the_limit() -> None:
    book = OrderBook(
        [make("E-7", date(2024, 3, 13)), make("E-8", date(2024, 3, 12)), make("E-0", AS_OF)]
    )
    assert ids(book.awaiting(AS_OF, 7)) == ["E-8"]
    assert ids(book.awaiting(AS_OF, 6)) == ["E-8", "E-7"]
    assert ids(book.awaiting(AS_OF, 0)) == ["E-8", "E-7"]
    assert book.awaiting(AS_OF, 8) == []
    with pytest.raises(ValueError):
        book.awaiting(AS_OF, -1)
    with pytest.raises(ValueError):
        OrderBook().awaiting(AS_OF, -3)


def test_overdue_matches_the_stated_rule_on_random_books() -> None:
    rng = random.Random(7070)
    statuses = ["open", "paid", "shipped", "cancelled"]
    for _ in range(300):
        orders = [
            make(
                f"N-{rng.randrange(100)}-{i}",
                AS_OF + timedelta(rng.randrange(-30, 5)),
                rng.choice(statuses),
            )
            for i in range(rng.randrange(0, 12))
        ]
        days = rng.randrange(0, 20)
        expected = sorted(
            (o for o in orders if o.status in ("open", "paid") and (AS_OF - o.placed).days > days),
            key=lambda o: (o.placed, o.order_id),
        )
        assert OrderBook(orders).awaiting(AS_OF, days) == expected


def test_overdue_skips_shipped_cancelled_and_later_orders() -> None:
    book = OrderBook(
        [
            make("S-1", date(2024, 1, 1), "shipped"),
            make("S-2", date(2024, 1, 1), "cancelled"),
            make("S-3", date(2024, 1, 1), "paid"),
            make("S-4", date(2024, 4, 1), "open"),
        ]
    )
    assert ids(book.awaiting(AS_OF, 0)) == ["S-3"]
    assert OrderBook().awaiting(AS_OF, 7) == []


def test_overdue_lists_oldest_first_with_ties_by_order_id() -> None:
    book = OrderBook(
        [
            make("V-9", date(2024, 3, 5)),
            make("V-2", date(2024, 3, 12), "paid"),
            make("V-10", date(2024, 3, 5), "paid"),
            make("W-1", date(2024, 2, 29)),
        ]
    )
    assert ids(book.awaiting(AS_OF, 7)) == ["W-1", "V-10", "V-9", "V-2"]


def test_overdue_command_prints_orders_and_a_total(tmp_path: Path) -> None:
    done = run(tmp_path, "--as-of", "2024-03-20")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.splitlines() == [
        "V-10 Bo Ek: placed 2024-03-05, 15 days old, $17.90",
        "V-9 Kai Lund: placed 2024-03-05, 15 days old, $12.50",
        "V-2 Omar Diaz: placed 2024-03-12, 8 days old, $34.00",
        "3 overdue orders, $64.40 in total",
    ]
    done = run(
        tmp_path, "--as-of", "2024-03-20", "--days", "0", env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "}
    )
    assert done.stdout.splitlines() == [
        "V-10 Bo Ek: placed 2024-03-05, 15 days old, EUR 17.90",
        "V-9 Kai Lund: placed 2024-03-05, 15 days old, EUR 12.50",
        "V-2 Omar Diaz: placed 2024-03-12, 8 days old, EUR 34.00",
        "V-1 Hana Kim: placed 2024-03-13, 7 days old, EUR 12.50",
        "V-12 Ivy Holm: placed 2024-03-19, 1 day old, EUR 9.80",
        "5 overdue orders, EUR 86.70 in total",
    ]
    done = run(tmp_path, "--as-of", "2024-03-20", "--days", "14")
    assert done.stdout.splitlines()[-1] == "2 overdue orders, $30.40 in total"
    done = run(tmp_path, "--as-of", "2024-03-20", "--days", "15")
    assert done.stdout == "0 overdue orders, $0.00 in total\n"


def test_overdue_command_refuses_bad_options(tmp_path: Path) -> None:
    for args, message in (
        (("--as-of", "2024-03-20", "--days", "-1"), "--days must not be negative"),
        (("--as-of", "2024-02-30"), "not a date: '2024-02-30'"),
    ):
        done = run(tmp_path, *args)
        assert done.returncode == 1
        assert done.stdout == ""
        assert done.stderr == f"stockroom: error: {message}\n"
