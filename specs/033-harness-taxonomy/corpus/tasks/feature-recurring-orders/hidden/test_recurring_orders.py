from __future__ import annotations

import dataclasses
import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from stockroom.errors import ParseError, StockroomError
from stockroom.models import Order, OrderLine
from stockroom.orders import OrderBook
from stockroom.recurring import Template, generate, occurrences, read_templates

ROOT = Path(__file__).resolve().parents[3]
MUG = (OrderLine("MUG-001", 2, 1250),)
TEA_NB = (OrderLine("TEA-010", 1, 895), OrderLine("NB-200", 3, 625))
TEMPLATES = (
    "template_id,customer,start,end,every,weekdays,sku,quantity,unit_price\n"
    "SUB-1,Hana Kim,2024-03-01,,14,,MUG-001,2,12.50\n"
    "WK-1,Omar Diaz,2024-03-06,2024-03-21,,mon fri,TEA-010,1,8.95\n"
    "WK-1,Omar Diaz,2024-03-06,2024-03-21,,mon fri,NB-200,3,6.25\n"
    "SOLO,Ada Moss,2024-03-10,,30,,PEN-100,1,34.00\n"
)
ORDERS = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    "A-1,Lena Berg,2024-02-20,paid,PEN-100,1,34.00\n"
    "SUB-1-1,Hana Kim,2024-03-01,shipped,MUG-001,2,12.50\n"
)


def d(month: int, day: int) -> date:
    return date(2024, month, day)


def every(template_id: str, start: date, days: int, end: date | None = None) -> Template:
    return Template(template_id, "Hana Kim", start, end, days, (), MUG)


def weekly(
    template_id: str, start: date, days: tuple[int, ...], end: date | None = None
) -> Template:
    return Template(template_id, "Omar Diaz", start, end, None, days, TEA_NB)


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_template_record_and_checks() -> None:
    assert [f.name for f in dataclasses.fields(Template)] == [
        "template_id",
        "customer",
        "start",
        "end",
        "every_days",
        "weekdays",
        "lines",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        every("SUB-1", d(3, 1), 7).every_days = 1  # type: ignore[misc]
    good = Template("SUB-1", "", d(3, 1), d(3, 2), None, (0, 6), MUG)
    assert good.weekdays == (0, 6)
    for args in (
        (" ", "Hana Kim", d(3, 1), None, 7, (), MUG),
        ("SUB-1", "Hana Kim", d(3, 1), None, 7, (1,), MUG),
        ("SUB-1", "Hana Kim", d(3, 1), None, None, (), MUG),
        ("SUB-1", "Hana Kim", d(3, 1), None, 0, (), MUG),
        ("SUB-1", "Hana Kim", d(3, 1), None, None, (7,), MUG),
        ("SUB-1", "Hana Kim", d(3, 1), None, None, (-1,), MUG),
        ("SUB-1", "Hana Kim", d(3, 1), None, None, (3, 1), MUG),
        ("SUB-1", "Hana Kim", d(3, 1), None, None, (1, 1), MUG),
        ("SUB-1", "Hana Kim", d(3, 1), None, 7, (), ()),
        ("SUB-1", "Hana Kim", d(3, 1), d(3, 1), 7, (), MUG),
        ("SUB-1", "Hana Kim", d(3, 1), d(2, 1), 7, (), MUG),
    ):
        with pytest.raises(ParseError):
            Template(*args)  # type: ignore[arg-type]


def test_interval_occurrences() -> None:
    template = every("SUB-1", d(3, 1), 14)
    assert occurrences(template, d(3, 29)) == [(1, d(3, 1)), (2, d(3, 15)), (3, d(3, 29))]
    assert occurrences(template, d(3, 28)) == [(1, d(3, 1)), (2, d(3, 15))]
    assert occurrences(template, d(3, 1)) == [(1, d(3, 1))]
    assert occurrences(template, d(2, 29)) == []
    assert occurrences(every("SUB-1", d(3, 1), 14, d(3, 29)), d(4, 30)) == [
        (1, d(3, 1)),
        (2, d(3, 15)),
    ]
    assert occurrences(every("SUB-1", d(3, 1), 14, d(3, 30)), d(4, 30))[-1] == (3, d(3, 29))
    assert occurrences(every("SUB-1", d(3, 1), 1), d(3, 3)) == [
        (1, d(3, 1)),
        (2, d(3, 2)),
        (3, d(3, 3)),
    ]


def test_weekday_occurrences() -> None:
    # 2024-03-06 is a Wednesday; 0 is Monday and 6 is Sunday
    template = weekly("WK-1", d(3, 6), (0, 3))
    assert occurrences(template, d(3, 18)) == [
        (1, d(3, 7)),
        (2, d(3, 11)),
        (3, d(3, 14)),
        (4, d(3, 18)),
    ]
    assert occurrences(weekly("WK-1", d(3, 7), (0, 3)), d(3, 11)) == [(1, d(3, 7)), (2, d(3, 11))]
    assert occurrences(weekly("WK-1", d(3, 6), (0, 3), d(3, 14)), d(3, 31)) == [
        (1, d(3, 7)),
        (2, d(3, 11)),
    ]
    assert occurrences(weekly("WK-2", d(3, 1), (6,)), d(3, 17)) == [
        (1, d(3, 3)),
        (2, d(3, 10)),
        (3, d(3, 17)),
    ]
    assert occurrences(weekly("WK-3", d(3, 6), (2,)), d(3, 6)) == [(1, d(3, 6))]


def test_generate_numbers_skips_and_order() -> None:
    first = Template("SUB-1", "Hana Kim", d(3, 1), None, 7, (), MUG)
    second = Template("WK-1", "Omar Diaz", d(3, 6), d(3, 21), None, (0, 4), TEA_NB)
    existing = Order("X-1", "Lena Berg", d(2, 1), (OrderLine("PEN-100", 1, 3400),), "paid")
    book = OrderBook([existing])
    new = generate([first, second], book, d(3, 15), skip={d(3, 8)})
    assert new == [
        Order("SUB-1-1", "Hana Kim", d(3, 1), MUG, "open"),
        Order("WK-1-2", "Omar Diaz", d(3, 11), TEA_NB, "open"),
        Order("SUB-1-3", "Hana Kim", d(3, 15), MUG, "open"),
        Order("WK-1-3", "Omar Diaz", d(3, 15), TEA_NB, "open"),
    ]
    assert [o.order_id for o in book] == ["X-1", "SUB-1-1", "WK-1-2", "SUB-1-3", "WK-1-3"]
    swapped = generate([second, first], OrderBook(), d(3, 15))
    assert [o.order_id for o in swapped] == [
        "SUB-1-1",
        "WK-1-1",
        "SUB-1-2",
        "WK-1-2",
        "WK-1-3",
        "SUB-1-3",
    ]


def test_generate_is_idempotent() -> None:
    first = Template("SUB-1", "Hana Kim", d(3, 1), None, 7, (), MUG)
    second = Template("WK-1", "Omar Diaz", d(3, 6), d(3, 21), None, (0, 4), TEA_NB)
    book = OrderBook()
    generate([first, second], book, d(3, 15), skip=[d(3, 8)])
    assert generate([first, second], book, d(3, 15), skip=[d(3, 8)]) == []
    assert len(book) == 4
    again = generate([first, second], book, d(3, 15))
    assert [(o.order_id, o.placed) for o in again] == [("SUB-1-2", d(3, 8)), ("WK-1-1", d(3, 8))]
    later = generate([first, second], book, d(3, 29))
    assert [o.order_id for o in later] == ["WK-1-4", "SUB-1-4", "SUB-1-5"]
    assert len(book) == 9
    other = Order("SUB-1-1", "Somebody", d(1, 1), (OrderLine("PEN-100", 1, 1),), "cancelled")
    book = OrderBook([other])
    assert [o.order_id for o in generate([first], book, d(3, 8))] == ["SUB-1-2"]
    assert book.get("SUB-1-1") is other
    empty = OrderBook()
    twin = Template("SUB-1", "Ada Moss", d(3, 2), None, 3, (), MUG)
    with pytest.raises(StockroomError):
        generate([first, second, twin], empty, d(3, 31))
    assert len(empty) == 0
    assert generate([], empty, d(3, 31)) == [] and generate([first], empty, d(2, 1)) == []


def test_read_templates() -> None:
    text = (
        "template_id,customer,start,end,every,weekdays,sku,quantity,unit_price\n"
        "SUB-1,Hana Kim,2024-03-01,,7,,MUG-001,2,12.50\n"
        "WK-1, Omar Diaz ,2024-03-06,2024-03-21,, fri  mon ,TEA-010,1,8.95\n"
        ",,,,,,,,\n"
        "WK-1,Omar Diaz,2024-03-06,2024-03-21,,mon fri,NB-200,3,$6.25\n"
    )
    assert read_templates(text) == [
        Template("SUB-1", "Hana Kim", d(3, 1), None, 7, (), MUG),
        Template("WK-1", "Omar Diaz", d(3, 6), d(3, 21), None, (0, 4), TEA_NB),
    ]
    header = "template_id,customer,start,end,every,weekdays,sku,quantity,unit_price\n"
    row = "SUB-1,Hana Kim,2024-03-01,,7,,MUG-001,2,12.50\n"
    for bad in (
        "",
        header.replace("every", "days") + row,
        header + "SUB-1,Hana Kim,2024-03-01,,7,,MUG-001,2\n",
        header + row + "SUB-1,Hana  Kim,2024-03-01,,7,,TEA-010,1,8.95\n",
        header + row + "SUB-1,Hana Kim,2024-03-01,,14,,TEA-010,1,8.95\n",
        header + row + "SUB-1,Hana Kim,2024-03-02,,7,,TEA-010,1,8.95\n",
        header + "WK-1,Omar Diaz,2024-03-06,,,Mon,TEA-010,1,8.95\n",
        header + "WK-1,Omar Diaz,2024-03-06,,,mon mon,TEA-010,1,8.95\n",
        header + "WK-1,Omar Diaz,2024-03-06,,,monday,TEA-010,1,8.95\n",
        header + "SUB-1,Hana Kim,2024-03-01,,7,mon,MUG-001,2,12.50\n",
        header + "SUB-1,Hana Kim,2024-03-01,,,,MUG-001,2,12.50\n",
        header + "SUB-1,Hana Kim,2024-03-01,,0,,MUG-001,2,12.50\n",
        header + "SUB-1,Hana Kim,2024-03-01,,7.0,,MUG-001,2,12.50\n",
        header + "SUB-1,Hana Kim,2024-03-01,,-7,,MUG-001,2,12.50\n",
        header + "SUB-1,Hana Kim,2024-03-01,2024-02-01,7,,MUG-001,2,12.50\n",
        header + "SUB-1,Hana Kim,2024-02-30,,7,,MUG-001,2,12.50\n",
        header + "SUB-1,Hana Kim,2024-03-01,,7,,MUG-001,two,12.50\n",
        header + "SUB-1,Hana Kim,2024-03-01,,7,,MUG-001,0,12.50\n",
        header + "SUB-1,Hana Kim,2024-03-01,,7,,MUG-001,2,cheap\n",
        header + "SUB-1,Hana Kim,2024-03-01,,7,,mug-1,2,12.50\n",
    ):
        with pytest.raises(ParseError):
            read_templates(bad)
    assert read_templates(header) == []


def _model_occurrences(template: Template, until: date) -> list[tuple[int, date]]:
    out, number, day = [], 0, template.start
    while day <= until:
        if template.every_days is not None:
            hit = (day - template.start).days % template.every_days == 0
        else:
            hit = day.weekday() in template.weekdays
        if hit:
            number += 1
            if template.end is None or day < template.end:
                out.append((number, day))
        day += timedelta(days=1)
    return out


def test_random_schedules_match_the_model() -> None:
    rng = random.Random(8128)
    for _ in range(150):
        templates = []
        for k, template_id in enumerate(
            rng.sample(["Z-9", "A-1", "M-5", "B-2"], rng.randrange(1, 4))
        ):
            start = d(3, 1) + timedelta(days=rng.randrange(0, 10))
            end = None if rng.random() < 0.5 else start + timedelta(days=rng.randrange(1, 30))
            if rng.random() < 0.5:
                days, weekdays = rng.randrange(1, 9), ()
            else:
                days, weekdays = None, tuple(sorted(rng.sample(range(7), rng.randrange(1, 4))))
            templates.append(Template(template_id, f"C{k}", start, end, days, weekdays, MUG))
        until = d(3, 1) + timedelta(days=rng.randrange(-3, 40))
        for template in templates:
            assert occurrences(template, until) == _model_occurrences(template, until)
        skip = {d(3, 1) + timedelta(days=rng.randrange(0, 40)) for _ in range(rng.randrange(0, 4))}
        candidates = [
            (day, position, f"{t.template_id}-{n}", t)
            for position, t in enumerate(templates)
            for n, day in _model_occurrences(t, until)
        ]
        taken = {c[2] for c in candidates if rng.random() < 0.2}
        book = OrderBook(Order(i, "X", d(1, 1), MUG, "paid") for i in sorted(taken))
        expected = sorted(
            (c for c in candidates if c[0] not in skip and c[2] not in taken),
            key=lambda c: (c[0], c[1]),
        )
        new = generate(templates, book, until, skip=skip)
        assert new == [Order(c[2], c[3].customer, c[0], MUG, "open") for c in expected]
        assert [o.order_id for o in book] == sorted(taken) + [c[2] for c in expected]


def test_cli_prints_new_orders(tmp_path: Path) -> None:
    (tmp_path / "templates.csv").write_text(TEMPLATES)
    orders = tmp_path / "orders.csv"
    orders.write_text(ORDERS)
    args = ("recur", "--templates", "templates.csv", "--orders", "orders.csv")
    done = run(*args, "--until", "2024-03-15", "--skip", "2024-03-11", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "WK-1-1  Omar Diaz  2024-03-08  4 units  $27.70",
        "SOLO-1  Ada Moss  2024-03-10  1 unit  $34.00",
        "SUB-1-2  Hana Kim  2024-03-15  2 units  $25.00",
        "WK-1-3  Omar Diaz  2024-03-15  4 units  $27.70",
        "4 new orders",
    ]
    assert orders.read_text() == ORDERS
    done = run(*args, "--until", "2024-03-07", cwd=tmp_path)
    assert done.stdout.splitlines() == ["0 new orders"]
    done = run(*args, "--until", "2024-03-08", "--skip", "2024-03-01", cwd=tmp_path)
    assert done.stdout.splitlines() == [
        "WK-1-1  Omar Diaz  2024-03-08  4 units  $27.70",
        "1 new order",
    ]


def test_cli_write_and_rerun(tmp_path: Path) -> None:
    (tmp_path / "templates.csv").write_text(TEMPLATES)
    orders = tmp_path / "orders.csv"
    orders.write_text(ORDERS)
    args = ("recur", "--templates", "templates.csv", "--orders", "orders.csv", "--write")
    done = run(*args, "--until", "2024-03-15", "--skip", "2024-03-11", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-2:] == ["4 new orders", "wrote 6 orders"]
    written = (
        ORDERS
        + "WK-1-1,Omar Diaz,2024-03-08,open,TEA-010,1,8.95\n"
        + "WK-1-1,Omar Diaz,2024-03-08,open,NB-200,3,6.25\n"
        + "SOLO-1,Ada Moss,2024-03-10,open,PEN-100,1,34.00\n"
        + "SUB-1-2,Hana Kim,2024-03-15,open,MUG-001,2,12.50\n"
        + "WK-1-3,Omar Diaz,2024-03-15,open,TEA-010,1,8.95\n"
        + "WK-1-3,Omar Diaz,2024-03-15,open,NB-200,3,6.25\n"
    )
    assert orders.read_text() == written
    done = run(*args, "--until", "2024-03-15", "--skip", "2024-03-11", cwd=tmp_path)
    assert done.stdout.splitlines() == ["0 new orders", "wrote 6 orders"]
    assert orders.read_text() == written
    done = run(*args, "--until", "2024-03-15", cwd=tmp_path)
    assert done.stdout.splitlines() == [
        "WK-1-2  Omar Diaz  2024-03-11  4 units  $27.70",
        "1 new order",
        "wrote 7 orders",
    ]
    assert orders.read_text() == written + (
        "WK-1-2,Omar Diaz,2024-03-11,open,TEA-010,1,8.95\n"
        "WK-1-2,Omar Diaz,2024-03-11,open,NB-200,3,6.25\n"
    )
    for extra in (("--until", "2024-13-01"), ("--until", "2024-03-15", "--skip", "soon")):
        done = run(*args, *extra, cwd=tmp_path)
        assert done.returncode == 1 and done.stdout == ""
        assert done.stderr.startswith("stockroom: error: ")
    (tmp_path / "templates.csv").write_text(
        TEMPLATES + "SOLO,Ada Moss,2024-03-11,,30,,TEA-010,1,8.95\n"
    )
    before = orders.read_text()
    done = run(*args, "--until", "2024-04-30", cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert orders.read_text() == before
