from __future__ import annotations

import csv
import io
import json
import random
from datetime import date

import pytest
from stockroom import csvio, jsonio
from stockroom.models import Order, OrderLine

HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"


def oracle_dump(orders) -> str:
    """``jsonio.dump_orders`` as it was before the change."""
    return json.dumps([jsonio.order_to_dict(o) for o in orders], indent=2, sort_keys=True) + "\n"


def oracle_csv(orders) -> str:
    """``csvio.write_orders`` as it was before the change."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(("order_id", "customer", "placed", "status", "sku", "quantity", "unit_price"))
    for order in orders:
        for line in order.lines:
            units, rest = divmod(line.unit_price_cents, 100)
            writer.writerow(
                [
                    order.order_id,
                    order.customer,
                    order.placed.isoformat(),
                    order.status,
                    line.sku,
                    line.quantity,
                    f"{units:,}.{rest:02d}",
                ]
            )
    return out.getvalue()


# writer -> (the old whole-text function's oracle, the text without the trailing part that only
# the end of the input adds)
def json_done(orders) -> str:
    """The text of ``orders`` as the start of a longer JSON list: without the closing ``]``."""
    return oracle_dump(orders)[: -len("\n]\n")] if orders else ""


def csv_done(orders) -> str:
    return oracle_csv(orders) if orders else ""


WRITERS = {
    "json": ("write_orders_json", oracle_dump, json_done),
    "csv": ("write_orders_csv", oracle_csv, csv_done),
}


class Sink:
    """A stream that has nothing but ``write``."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text):
        assert type(text) is str, type(text)
        self.chunks.append(text)
        return len(text)

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def writer(kind: str):
    module = jsonio if kind == "json" else csvio
    return getattr(module, WRITERS[kind][0])


NAMES = [
    "Hana Kim",
    "José Álvarez",
    'Lena "LB" Berg',
    "Back\\slash",
    "Tab\tand\nnewline",
    "Comma, Inc.",
    "Café ☕",
    "Emoji \U0001f600",
    "",
]
SKUS = ["MUG-001", "TEA-010", "PEN-100", "NB-200", "LMP-03000"]
STATUSES = ["open", "paid", "shipped", "cancelled"]


def make_orders(rng: random.Random, low: int = 0, high: int = 7, min_lines: int = 0) -> list[Order]:
    orders = []
    for n in range(rng.randrange(low, high)):
        lines = tuple(
            OrderLine(rng.choice(SKUS), rng.randrange(1, 500), rng.randrange(0, 10**7))
            for _ in range(rng.randrange(min_lines, 4))
        )
        placed = date(rng.randrange(1990, 2031), rng.randrange(1, 13), rng.randrange(1, 29))
        orders.append(Order(f"J-{n:04d}", rng.choice(NAMES), placed, lines, rng.choice(STATUSES)))
    return orders


def test_json_stream_is_the_old_dump() -> None:
    rng = random.Random(9091)
    for _ in range(300):
        orders = make_orders(rng)
        expected = oracle_dump(orders)
        sink = Sink()
        assert jsonio.write_orders_json(iter(orders), sink) == len(orders)
        assert sink.text == expected
        assert jsonio.dump_orders(orders) == expected
        assert jsonio.dump_orders(o for o in orders) == expected


def test_csv_stream_is_the_old_file_text() -> None:
    rng = random.Random(6061)
    for _ in range(300):
        orders = make_orders(rng, min_lines=1)
        expected = oracle_csv(orders)
        sink = Sink()
        assert csvio.write_orders_csv(iter(orders), sink) == len(orders)
        assert sink.text == expected
        assert csvio.write_orders(orders) == expected
        assert csvio.write_orders(o for o in orders) == expected


class OnlyWrite:
    """No attribute but ``write``; ``write`` takes text only and returns nothing."""

    __slots__ = ("parts",)

    def __init__(self) -> None:
        self.parts: list[str] = []

    def write(self, text) -> None:
        if type(text) is not str:
            raise TypeError(f"text only, not {type(text).__name__}")
        self.parts.append(text)


@pytest.mark.parametrize("kind", sorted(WRITERS))
def test_the_stream_needs_only_a_write_method(kind: str) -> None:
    rng = random.Random(31)
    for _ in range(20):
        orders = make_orders(rng, min_lines=1)
        stream = OnlyWrite()
        assert writer(kind)(orders, stream) == len(orders)
        assert "".join(stream.parts) == WRITERS[kind][1](orders)


def test_no_orders_give_an_empty_export() -> None:
    sink = Sink()
    assert jsonio.write_orders_json([], sink) == 0
    assert sink.text == "[]\n"
    assert jsonio.dump_orders([]) == "[]\n"
    assert jsonio.dump_orders(iter(())) == "[]\n"
    sink = Sink()
    assert csvio.write_orders_csv([], sink) == 0
    assert sink.text == HEADER
    assert csvio.write_orders([]) == HEADER
    assert csvio.write_orders(iter(())) == HEADER


@pytest.mark.parametrize("kind", sorted(WRITERS))
def test_each_order_is_written_before_the_next_is_taken(kind: str) -> None:
    _, oracle, done = WRITERS[kind]
    rng = random.Random(1213)
    for _ in range(150):
        orders = make_orders(rng, 1, 8, min_lines=1)
        full = oracle(orders)
        sink = Sink()
        taken: list[int] = []

        def source(orders=orders, sink=sink, full=full, taken=taken):
            for k, order in enumerate(orders):
                written = sink.text
                assert full.startswith(written), "text that is not part of the result"
                assert written.startswith(done(orders[:k])), (
                    f"order {k} not complete before order {k + 1} was taken"
                )
                taken.append(k)
                yield order

        assert writer(kind)(source(), sink) == len(orders)
        assert taken == list(range(len(orders)))
        assert sink.text == full


class Broken(Exception):
    pass


@pytest.mark.parametrize("kind", sorted(WRITERS))
def test_a_failing_source_leaves_the_orders_written_so_far(kind: str) -> None:
    _, oracle, done = WRITERS[kind]
    rng = random.Random(77)
    orders = make_orders(rng, 3, 4, min_lines=1)
    failure = Broken("source broke")

    def source():
        yield orders[0]
        yield orders[1]
        raise failure

    sink = Sink()
    with pytest.raises(Broken) as raised:
        writer(kind)(source(), sink)
    assert raised.value is failure
    taken = oracle(orders[:2])
    assert taken.startswith(sink.text)
    assert sink.text.startswith(done(orders[:2]))
    for good in (orders[:0], orders[:1], orders[:2]):
        sink = Sink()
        with pytest.raises(AttributeError) as raised:
            writer(kind)([*good, "not an order", orders[2]], sink)
        assert "'str' object has no attribute" in str(raised.value)
        taken = oracle(good)
        assert taken.startswith(sink.text), "a separator or part of the failed order was written"
        assert sink.text.startswith(done(good))


def test_whole_text_functions_write_through_the_streaming_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake(orders, stream) -> int:
        items = list(orders)
        calls.append(items)
        stream.write("<")
        stream.write(f"{len(items)} orders>")
        return len(items)

    order = Order("A-1", "Hana Kim", date(2024, 1, 8), (OrderLine("MUG-001", 2, 1250),))
    monkeypatch.setattr(jsonio, "write_orders_json", fake)
    assert jsonio.dump_orders([order, order]) == "<2 orders>"
    monkeypatch.setattr(csvio, "write_orders_csv", fake)
    assert csvio.write_orders(iter([order])) == "<1 orders>"
    assert calls == [[order, order], [order]]


def test_reading_and_converting_are_unchanged() -> None:
    order = Order("A-1", "Hana Kim", date(2024, 1, 8), (OrderLine("MUG-001", 2, 1250),))
    text = jsonio.dump_orders([order])
    assert text == (
        "[\n  {\n"
        '    "customer": "Hana Kim",\n'
        '    "lines": [\n      {\n        "quantity": 2,\n        "sku": "MUG-001",\n'
        '        "unit_price_cents": 1250\n      }\n    ],\n'
        '    "order_id": "A-1",\n    "placed": "2024-01-08",\n    "status": "open"\n  }\n]\n'
    )
    assert jsonio.load_orders(text) == [order]
    empty = Order("A-2", "Ann", date(2024, 2, 9))
    assert jsonio.load_orders(jsonio.dump_orders([order, empty])) == [order, empty]
    assert jsonio.order_to_dict(empty) == {
        "order_id": "A-2",
        "customer": "Ann",
        "placed": "2024-02-09",
        "status": "open",
        "lines": [],
    }
    paid = Order(
        "B-7",
        "Comma, Inc.",
        date(2024, 3, 9),
        (OrderLine("PEN-100", 3, 123456), OrderLine("TEA-010", 1, 5)),
        "paid",
    )
    text = csvio.write_orders([order, paid])
    assert text == (
        HEADER + "A-1,Hana Kim,2024-01-08,open,MUG-001,2,12.50\n"
        'B-7,"Comma, Inc.",2024-03-09,paid,PEN-100,3,"1,234.56"\n'
        'B-7,"Comma, Inc.",2024-03-09,paid,TEA-010,1,0.05\n'
    )
    assert csvio.read_orders(text) == [order, paid]
