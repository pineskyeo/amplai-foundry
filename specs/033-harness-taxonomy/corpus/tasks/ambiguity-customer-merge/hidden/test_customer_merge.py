import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from stockroom.csvio import read_orders, write_orders
from stockroom.customers import canonicalize, merge_customers, normalize_name
from stockroom.models import Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"


def _order(n: int, customer: str, placed: str = "2024-03-01", status: str = "paid") -> Order:
    return Order(
        f"C-{n}", customer, date.fromisoformat(placed), (OrderLine("MUG-001", 1, 1000 + n),), status
    )


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


def test_names_are_compared_without_dots_case_or_extra_whitespace() -> None:
    assert normalize_name("Hana Kim") == "hana kim"
    assert normalize_name("  HANA   kim ") == "hana kim"
    assert normalize_name("H. Kim") == "h kim"
    assert normalize_name("H . Kim") == "h kim"
    assert normalize_name("Hana  K.i.m.") == "hana kim"
    assert normalize_name("Kim, Hana") == "kim, hana"
    assert normalize_name("Hana\tKim\n") == "hana kim"
    assert normalize_name("Hana\u00a0Kim") == "hana kim"
    assert normalize_name("Stra\u00dfe") == "strasse"
    assert normalize_name("...") == ""
    assert normalize_name("") == ""
    assert normalize_name("Hana-Kim") == "hana-kim"
    assert normalize_name("O'Neil") == "o'neil"


def test_the_most_frequent_spelling_is_canonical_and_cancelled_orders_count() -> None:
    orders = [
        _order(1, "hana kim"),
        _order(2, "Hana Kim"),
        _order(3, "hana kim"),
        _order(4, "HANA KIM"),
        _order(5, "hana kim"),
        _order(6, "Omar Diaz"),
    ]
    assert merge_customers(orders) == {
        "hana kim": "hana kim",
        "Hana Kim": "hana kim",
        "HANA KIM": "hana kim",
        "Omar Diaz": "Omar Diaz",
    }
    orders = [
        _order(1, "Hana Kim", status="cancelled"),
        _order(2, "Hana Kim", status="cancelled"),
        _order(3, "Hana Kim", status="paid"),
        _order(4, "hana kim"),
        _order(5, "hana kim"),
    ]
    assert set(merge_customers(orders).values()) == {"Hana Kim"}


def test_equal_counts_go_to_the_earliest_order_then_to_the_smaller_string() -> None:
    orders = [
        _order(1, "Hana Kim", "2024-01-09"),
        _order(2, "hana kim", "2024-01-05"),
        _order(3, "Hana Kim", "2024-02-01"),
        _order(4, "hana kim", "2024-03-01"),
    ]
    assert set(merge_customers(orders).values()) == {"hana kim"}
    orders = [
        _order(1, "Hana Kim", "2024-01-05"),
        _order(2, "HANA KIM", "2024-01-05"),
        _order(3, "hana kim", "2024-01-05"),
        _order(4, "H. Kim", "2024-01-05"),
    ]
    assert merge_customers(orders) == {
        "Hana Kim": "HANA KIM",
        "HANA KIM": "HANA KIM",
        "hana kim": "HANA KIM",
        "H. Kim": "H. Kim",
    }
    same = [
        _order(1, "Hana Kim", "2024-01-05"),
        _order(2, "HANA KIM", "2024-01-05"),
        _order(3, "hana  kim", "2024-01-05"),
    ]
    assert set(merge_customers(same).values()) == {"HANA KIM"}
    earlier_counts_less = [
        _order(1, "Hana Kim", "2024-01-05"),
        _order(2, "hana kim", "2024-01-01"),
        _order(3, "hana kim", "2024-01-07"),
    ]
    assert set(merge_customers(earlier_counts_less).values()) == {"hana kim"}


def test_different_names_stay_apart_and_keys_follow_first_appearance() -> None:
    orders = [
        _order(1, "Kim, Hana"),
        _order(2, "Hana Kim"),
        _order(3, "hana kim Jr."),
        _order(4, "Hana kim"),
        _order(5, "Kim, Hana"),
        _order(6, "Hana-Kim"),
    ]
    mapping = merge_customers(orders)
    assert list(mapping) == ["Kim, Hana", "Hana Kim", "hana kim Jr.", "Hana kim", "Hana-Kim"]
    assert mapping["Kim, Hana"] == "Kim, Hana"
    assert mapping["hana kim Jr."] == "hana kim Jr."
    assert mapping["Hana-Kim"] == "Hana-Kim"
    assert mapping["Hana Kim"] == mapping["Hana kim"] == "Hana Kim"
    assert merge_customers([]) == {}
    assert merge_customers(iter([_order(1, "A")])) == {"A": "A"}


def test_canonicalize_renames_customers_and_keeps_everything_else() -> None:
    orders = [_order(1, "hana kim", "2024-02-01"), _order(2, "Hana Kim", "2024-01-01", "open"), _order(3, "hana kim")]
    done = canonicalize(orders)
    assert [o.customer for o in done] == ["hana kim", "hana kim", "hana kim"]
    assert [(o.order_id, o.placed, o.lines, o.status) for o in done] == [
        (o.order_id, o.placed, o.lines, o.status) for o in orders
    ]
    assert [o.customer for o in orders] == ["hana kim", "Hana Kim", "hana kim"]
    assert canonicalize([]) == []
    assert canonicalize(iter(orders))[1].customer == "hana kim"


def test_random_orders_match_the_definition_whatever_their_order() -> None:
    rng = random.Random(41)
    bases = ["hana kim", "omar diaz", "lena.berg", "ada moss", "ravi nair"]

    def variant(name: str) -> str:
        out = []
        for ch in name:
            roll = rng.random()
            out.append(ch.upper() if roll < 0.25 else ch)
            if ch == " " and rng.random() < 0.3:
                out.append("  ")
            if rng.random() < 0.05:
                out.append(".")
        return ("  " if rng.random() < 0.1 else "") + "".join(out)

    for _ in range(200):
        orders = [
            _order(n, variant(rng.choice(bases)), str(date(2024, 1, 1) + timedelta(days=rng.randrange(0, 40))), rng.choice(["paid", "cancelled", "open"]))
            for n in range(rng.randrange(0, 15))
        ]
        expected: dict[str, str] = {}
        counts: dict[str, int] = {}
        first: dict[str, date] = {}
        for o in orders:
            counts[o.customer] = counts.get(o.customer, 0) + 1
            first[o.customer] = min(first.get(o.customer, o.placed), o.placed)
        by_key: dict[str, list[str]] = {}
        for spelling in counts:
            key = " ".join(spelling.replace(".", "").casefold().split())
            by_key.setdefault(key, []).append(spelling)
        for spellings in by_key.values():
            winner = sorted(spellings, key=lambda s: (-counts[s], first[s], s))[0]
            for s in spellings:
                expected[s] = winner
        mapping = merge_customers(orders)
        assert mapping == expected
        assert list(mapping) == list(counts)
        shuffled = orders[:]
        rng.shuffle(shuffled)
        assert merge_customers(shuffled) == expected
        assert [o.customer for o in canonicalize(orders)] == [expected[o.customer] for o in orders]


def test_the_command_lists_customers_with_their_orders_and_revenue() -> None:
    done = run("customers", "--orders", "data/orders.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "Hana Kim: 2 orders, $88.35",
        "Lena Berg: 2 orders, $14.80",
        "Omar Diaz: 2 orders, $46.50",
    ]
    assert done.stderr == ""


def test_the_command_merges_spellings_and_names_the_other_ones(tmp_path: Path) -> None:
    orders = [
        _order(1, "hana kim", "2024-03-01"),
        _order(2, "Hana Kim", "2024-03-02"),
        _order(3, "hana kim", "2024-03-03", "cancelled"),
        _order(4, "H. Kim", "2024-03-04"),
        _order(5, "HANA  KIM", "2024-03-05", "shipped"),
        _order(6, "Zoe Ng", "2024-03-06"),
        _order(7, "Zoe Ng", "2024-03-07", "cancelled"),
        _order(8, "ZOE NG", "2024-03-08", "open"),
        _order(9, "Kim, Hana", "2024-03-09"),
    ]
    path = tmp_path / "orders.csv"
    path.write_text(write_orders(orders))
    done = run("customers", "--orders", str(path), env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "H. Kim: 1 order, EUR 10.04",
        "Kim, Hana: 1 order, EUR 10.09",
        "Zoe Ng: 3 orders, EUR 20.14",
        "  also written: ZOE NG",
        "hana kim: 4 orders, EUR 30.08",
        "  also written: HANA  KIM, Hana Kim",
    ]
    assert read_orders(path.read_text())[4].customer == "HANA  KIM"
    none = tmp_path / "none.csv"
    none.write_text("order_id,customer,placed,status,sku,quantity,unit_price\n")
    done = run("customers", "--orders", str(none))
    assert done.returncode == 0 and done.stdout == ""
