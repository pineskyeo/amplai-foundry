import random
from pathlib import Path

from stockroom.csvio import read_orders
from stockroom.orders import OrderBook
from stockroom.report import sales_summary, top_n

DATA = Path(__file__).resolve().parents[3] / "data"


def _book() -> OrderBook:
    return OrderBook(read_orders((DATA / "orders.csv").read_text()))


def _customer_rows(text: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Customer"))
    return lines[start + 2 :]


def test_asking_for_none_returns_an_empty_ranking() -> None:
    assert top_n({"a": 1, "b": 2}, 0) == []
    assert top_n({}, 0) == []
    assert top_n({"b": 5, "a": 5}, 0) == []


def test_other_counts_still_rank_largest_first_with_ties_by_name() -> None:
    totals = {"b": 5, "a": 5, "c": 9, "d": 1}
    assert top_n(totals, 1) == [("c", 9)]
    assert top_n(totals, 2) == [("c", 9), ("a", 5)]
    assert top_n(totals, 3) == [("c", 9), ("a", 5), ("b", 5)]
    assert top_n(totals, 4) == [("c", 9), ("a", 5), ("b", 5), ("d", 1)]
    assert top_n(totals, 99) == [("c", 9), ("a", 5), ("b", 5), ("d", 1)]
    assert top_n({}, 3) == []


def test_random_totals_match_the_definition_for_every_count() -> None:
    rng = random.Random(11)
    for _ in range(200):
        totals = {f"n{rng.randint(0, 30)}": rng.randint(0, 6) for _ in range(rng.randint(0, 15))}
        ranked = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
        for n in range(0, len(totals) + 3):
            assert top_n(totals, n) == ranked[:n], (totals, n)


def test_a_sales_summary_with_no_top_customers_lists_none() -> None:
    text = sales_summary(_book(), top=0)
    lines = text.splitlines()
    assert lines[0] == "5 orders, 1 cancelled"
    assert lines[1] == "Revenue: $149.65"
    assert _customer_rows(text) == []
    assert not any(name in text for name in ("Hana Kim", "Omar Diaz", "Lena Berg"))
    assert any("2024-02" in line and "$54.40" in line for line in lines)


def test_a_sales_summary_lists_as_many_customers_as_asked() -> None:
    book = _book()
    one = _customer_rows(sales_summary(book, top=1))
    assert len(one) == 1 and one[0].startswith("Hana Kim")
    two = _customer_rows(sales_summary(book, top=2))
    assert [row.split("  ")[0] for row in two] == ["Hana Kim", "Omar Diaz"]
    default = _customer_rows(sales_summary(book))
    assert [row.split("  ")[0] for row in default] == ["Hana Kim", "Omar Diaz", "Lena Berg"]
    many = _customer_rows(sales_summary(book, top=50))
    assert len(many) == 3
