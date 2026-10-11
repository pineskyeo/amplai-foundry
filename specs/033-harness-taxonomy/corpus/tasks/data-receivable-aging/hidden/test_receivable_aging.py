from __future__ import annotations

import os
import random
import subprocess
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import pytest
from stockroom.aging import AgingRow, Payment, age_receivables, customer_key, read_payments
from stockroom.errors import ParseError
from stockroom.models import Order, OrderLine

ROOT = Path(__file__).resolve().parents[3]
D = date.fromisoformat


def run(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=50,
        check=False,
    )


_ids = iter(range(1, 10**9))


def order(customer: str, placed: str, cents: int, status: str = "paid") -> Order:
    return Order(f"O-{next(_ids)}", customer, D(placed), (OrderLine("MUG-001", 1, cents),), status)


def pay(customer: str, day: str, cents: int) -> Payment:
    return Payment(customer, D(day), cents)


def test_read_payments() -> None:
    text = (
        "customer,paid,amount\r\n"
        " Hana Kim ,2024-03-01, 25.00\r\n"
        "\r\n"
        '"Moss, Ada",2024-03-02,7\n'
        "Cy,2024-03-03,0.05\n"
        ",,\n"
    )
    assert read_payments(text) == [
        Payment("Hana Kim", D("2024-03-01"), 2500),
        Payment("Moss, Ada", D("2024-03-02"), 700),
        Payment("Cy", D("2024-03-03"), 5),
    ]
    assert read_payments(" customer , paid , amount \n") == []
    for row in (
        ",2024-03-01,1.00",
        "Bo,20240301,1.00",
        "Bo,2024-3-1,1.00",
        "Bo,2024-02-30,1.00",
        "Bo,2024-03-01,0",
        "Bo,2024-03-01,0.00",
        "Bo,2024-03-01,-1.00",
        "Bo,2024-03-01,1.5",
        "Bo,2024-03-01,$1.00",
        'Bo,2024-03-01,"1,000.00"',
        "Bo,2024-03-01,\u0661.00",
        "Bo,2024-03-01,\u00b2",
        "Bo,2024-03-01,1_0.00",
        "Bo,2024-03-01",
        "Bo,2024-03-01,1.00,x",
    ):
        with pytest.raises(ParseError):
            read_payments("customer,paid,amount\n" + row + "\n")
    for header in ("", "customer,day,amount\n", "customer,paid\n"):
        with pytest.raises(ParseError):
            read_payments(header + "Bo,2024-03-01,1.00\n")


def test_customer_keys_are_normalised() -> None:
    assert customer_key("Hana Kim") == "hana kim"
    assert customer_key("  HANA\xa0\xa0KIM\t") == "hana kim"
    assert customer_key("\uff28\uff41\uff4e\uff41 Kim") == "hana kim"
    assert customer_key("Straße") == customer_key("STRASSE") == "strasse"
    assert customer_key("Jose\u0301") == customer_key("José")
    assert customer_key("a\u2003b\nc") == "a b c"
    assert customer_key("\ufb01ne") == "fine"
    rng = random.Random(77)
    pieces = ["a", "B", " ", "\xa0", "\t", "ß", "é", "e\u0301", "\uff21", "\u2003", "SS"]
    for _ in range(2000):
        name = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 8)))
        key = customer_key(name)
        assert customer_key(key) == key
        assert key == " ".join(unicodedata.normalize("NFKC", name).split()).casefold()
        assert customer_key(name.upper()) == key or "ß" in name or "\u0301" in name


def row(name, current=0, a=0, b=0, c=0, d=0, credit=0) -> AgingRow:
    return AgingRow(name, current, a, b, c, d, current + a + b + c + d, credit)


def test_payments_go_to_the_oldest_orders_and_credit_carries_over() -> None:
    orders = [
        order("Hana Kim", "2024-01-10", 3000),
        order("Hana Kim", "2024-01-05", 2000),
        order("Hana Kim", "2024-02-20", 1000),
    ]
    # 2500 pays the 01-05 order (2000) and 500 of the 01-10 one
    rows = age_receivables(orders, [pay("hana kim", "2024-02-01", 2500)], as_of=D("2024-03-01"))
    assert rows == [row("Hana Kim", current=1000, a=2500)]
    # a payment larger than everything open leaves a credit, used by the next order
    orders = [order("Bo", "2024-01-01", 1000), order("Bo", "2024-01-20", 3000)]
    payments = [pay("Bo", "2024-01-10", 1500)]
    assert age_receivables(orders, payments, as_of=D("2024-01-15")) == [row("Bo", credit=500)]
    assert age_receivables(orders, payments, as_of=D("2024-01-20")) == [row("Bo", current=2500)]
    # a payment before any order is a credit
    assert age_receivables(
        [order("Cy", "2024-03-05", 400)], [pay("Cy", "2024-03-01", 1000)], as_of=D("2024-03-10")
    ) == [row("Cy", credit=600)]
    # on the same day the order comes first, so the payment pays it
    same = age_receivables(
        [order("Di", "2024-03-05", 400)], [pay("Di", "2024-03-05", 400)], as_of=D("2024-03-05")
    )
    assert same == []
    # cancelled orders, orders and payments after the as-of day do not count
    orders = [
        order("Ed", "2024-01-01", 1000, "cancelled"),
        order("Ed", "2024-01-02", 700, "open"),
        order("Ed", "2024-04-01", 900),
    ]
    payments = [pay("Ed", "2024-01-03", 200), pay("Ed", "2024-03-02", 500)]
    assert age_receivables(orders, payments, as_of=D("2024-03-01")) == [row("Ed", a=500)]
    assert age_receivables(orders, payments, as_of=D("2024-03-02")) == []
    # keys join spellings; a payment of another customer does not help
    orders = [order("Fay\xa0Lo", "2024-02-01", 800), order("FAY LO", "2024-02-02", 800)]
    payments = [pay(" fay  lo ", "2024-02-03", 900), pay("Gus", "2024-02-03", 100)]
    assert age_receivables(orders, payments, as_of=D("2024-02-10")) == [
        row("Fay\xa0Lo", current=700),
        row("Gus", credit=100),
    ]
    assert age_receivables([], [], as_of=D("2024-01-01")) == []
    with pytest.raises(ValueError):
        age_receivables([], [], as_of=D("2024-01-01"), terms_days=-1)


def test_age_buckets_have_exact_boundaries() -> None:
    placed = D("2024-01-01")
    expected = {0: 0, 1: 1, 30: 1, 31: 2, 60: 2, 61: 3, 90: 3, 91: 4, 400: 4}
    for late, bucket in expected.items():
        as_of = placed + timedelta(days=30 + late)
        (got,) = age_receivables([order("Ho", "2024-01-01", 100)], [], as_of=as_of)
        values = [got.current, got.days_1_30, got.days_31_60, got.days_61_90, got.over_90]
        assert values == [100 if i == bucket else 0 for i in range(5)], late
    (got,) = age_receivables(
        [order("Ho", "2024-01-01", 100)], [], as_of=D("2024-01-01"), terms_days=0
    )
    assert got.current == 100
    (got,) = age_receivables(
        [order("Ho", "2024-01-01", 100)], [], as_of=D("2024-01-02"), terms_days=0
    )
    assert got.days_1_30 == 100
    (got,) = age_receivables(
        [order("Ho", "2024-01-01", 100)], [], as_of=D("2024-01-31"), terms_days=0
    )
    assert got.days_31_60 == 0 and got.days_1_30 == 100
    (got,) = age_receivables(
        [order("Ho", "2024-01-01", 100)], [], as_of=D("2024-04-15"), terms_days=7
    )
    assert got.over_90 == 100
    # a partly paid order ages with its remaining balance
    orders = [order("Ik", "2024-01-01", 1000), order("Ik", "2024-02-15", 1000)]
    (got,) = age_receivables(orders, [pay("Ik", "2024-01-05", 600)], as_of=D("2024-04-01"))
    assert (got.days_61_90, got.days_1_30, got.total) == (400, 1000, 1400)


def test_rows_are_ordered_and_named() -> None:
    orders = [
        order("bo", "2024-01-01", 500),
        order("Ada", "2024-01-01", 500),
        order("Zed", "2024-01-01", 900),
        order("BO", "2024-01-02", 0 + 1),
        order("Cy", "2024-01-01", 100, "cancelled"),
    ]
    payments = [
        pay("Ada", "2024-01-03", 500),
        pay("Cy", "2024-01-03", 50),
        pay("Dee", "2024-01-03", 50),
    ]
    rows = age_receivables(orders, payments, as_of=D("2024-01-10"))
    assert rows == [
        row("Zed", current=900),
        row("bo", current=501),
        row("Cy", credit=50),
        row("Dee", credit=50),
    ]
    rows = age_receivables(iter(orders), iter(payments), as_of=D("2024-01-10"))
    assert [r.customer for r in rows] == ["Zed", "bo", "Cy", "Dee"]


def model(orders, payments, as_of, terms):
    names, events, seq = {}, [], 0
    for o in orders:
        k = " ".join(unicodedata.normalize("NFKC", o.customer).split()).casefold()
        names.setdefault(k, o.customer)
        if o.status != "cancelled" and o.placed <= as_of:
            events.append(
                (o.placed, 0, seq, k, sum(ln.quantity * ln.unit_price_cents for ln in o.lines))
            )
            seq += 1
    for p in payments:
        k = " ".join(unicodedata.normalize("NFKC", p.customer).split()).casefold()
        names.setdefault(k, p.customer)
        if p.day <= as_of:
            events.append((p.day, 1, seq, k, p.amount_cents))
            seq += 1
    events.sort(key=lambda e: e[:3])
    opened, credit = {}, {}
    for day, kind, _, k, amount in events:
        lst = opened.setdefault(k, [])
        if kind == 0:
            used = min(credit.get(k, 0), amount)
            credit[k] = credit.get(k, 0) - used
            lst.append([day, amount - used])
        else:
            for inv in lst:
                take = min(amount, inv[1])
                inv[1] -= take
                amount -= take
            credit[k] = credit.get(k, 0) + amount
    out = []
    for k in set(opened) | set(credit):
        b = [0] * 5
        for day, bal in opened.get(k, []):
            late = (as_of - day).days - terms
            b[
                0 if late <= 0 else 1 if late <= 30 else 2 if late <= 60 else 3 if late <= 90 else 4
            ] += bal
        if sum(b) or credit.get(k, 0):
            out.append((-sum(b), k, AgingRow(names[k], *b, sum(b), credit.get(k, 0))))
    return [r for *_, r in sorted(out, key=lambda t: t[:2])]


def test_generated_ledgers_follow_the_rules() -> None:
    rng = random.Random(6060)
    spellings = [
        ["Hana Kim", "hana  kim", "HANA\xa0KIM"],
        ["Straße", "STRASSE"],
        ["Bo"],
        ["\uff21da", "ada"],
    ]
    for _ in range(800):
        orders, payments = [], []
        start = D("2024-01-01")
        for _ in range(rng.randint(0, 8)):
            who = rng.choice(spellings)
            status = rng.choice(["open", "paid", "shipped", "cancelled"])
            orders.append(
                order(
                    rng.choice(who),
                    (start + timedelta(days=rng.randint(0, 200))).isoformat(),
                    rng.randint(1, 50000),
                    status,
                )
            )
        for _ in range(rng.randint(0, 6)):
            who = rng.choice(spellings)
            payments.append(
                Payment(
                    rng.choice(who),
                    start + timedelta(days=rng.randint(0, 220)),
                    rng.randint(1, 60000),
                )
            )
        as_of = start + timedelta(days=rng.randint(0, 260))
        terms = rng.choice([0, 7, 30, 45])
        rows = age_receivables(orders, payments, as_of=as_of, terms_days=terms)
        assert rows == model(orders, payments, as_of, terms)
        for r in rows:
            assert r.total == r.current + r.days_1_30 + r.days_31_60 + r.days_61_90 + r.over_90
            assert r.total == 0 or r.credit == 0
            k = customer_key(r.customer)
            billed = sum(
                o.lines[0].unit_price_cents
                for o in orders
                if customer_key(o.customer) == k and o.status != "cancelled" and o.placed <= as_of
            )
            paid = sum(
                p.amount_cents for p in payments if customer_key(p.customer) == k and p.day <= as_of
            )
            assert billed - paid == r.total - r.credit
        totals = [r.total for r in rows]
        assert totals == sorted(totals, reverse=True)


ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "A-1,Hana Kim,2024-01-05,paid,MUG-001,2,10.00\n"
    "A-2,Omar Diaz,2024-02-10,open,PEN-100,1,34.00\n"
    "A-3,hana  kim,2024-03-01,shipped,TEA-010,1,8.95\n"
    "A-4,Lena Berg,2024-03-02,cancelled,LMP-030,1,49.99\n"
    "A-5,Omar Diaz,2024-04-20,open,NB-200,1,6.25\n"
)
PAYMENTS = (
    "customer,paid,amount\n"
    "HANA KIM,2024-01-20,5.00\n"
    "Lena Berg,2024-03-05,10\n"
    "Omar Diaz,2024-05-01,40.25\n"
)


def test_aging_command(tmp_path: Path) -> None:
    (tmp_path / "orders.csv").write_text(ORDERS, encoding="utf-8")
    (tmp_path / "payments.csv").write_text(PAYMENTS, encoding="utf-8")
    base = ("aging", "--orders", "orders.csv", "--payments", "payments.csv")
    done = run(*base, "--as-of", "2024-04-15", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "Omar Diaz: current $0.00, 1-30 $0.00, 31-60 $34.00, 61-90 $0.00, 90+ $0.00,"
        " total $34.00, credit $0.00",
        "Hana Kim: current $0.00, 1-30 $8.95, 31-60 $0.00, 61-90 $15.00, 90+ $0.00,"
        " total $23.95, credit $0.00",
        "Lena Berg: current $0.00, 1-30 $0.00, 31-60 $0.00, 61-90 $0.00, 90+ $0.00,"
        " total $0.00, credit $10.00",
    ]
    done = run(
        *base,
        "--as-of",
        "2024-06-01",
        "--terms",
        "10",
        cwd=tmp_path,
        env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == (
        "Hana Kim: current EUR 0.00, 1-30 EUR 0.00, 31-60 EUR 0.00, 61-90 EUR 8.95, 90+ EUR 15.00,"
        " total EUR 23.95, credit EUR 0.00"
    )
    assert done.stdout.splitlines()[1].startswith("Lena Berg: ")
    assert len(done.stdout.splitlines()) == 2
    done = run(*base, "--as-of", "2024-01-01", cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == "nothing outstanding\n"
    (tmp_path / "bad.csv").write_text("customer,paid,amount\nBo,2024-03-01,1.5\n", encoding="utf-8")
    for args in (
        ("aging", "--orders", "orders.csv", "--payments", "bad.csv", "--as-of", "2024-04-15"),
        (*base, "--as-of", "2024-02-30"),
        (*base, "--as-of", "2024-04-15", "--terms=-1"),
    ):
        done = run(*args, cwd=tmp_path)
        assert done.returncode == 1 and done.stdout == "", args
        assert done.stderr.startswith("stockroom: error:")
