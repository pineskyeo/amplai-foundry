import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from stockroom.csvio import read_items, read_orders, read_stock
from stockroom.errors import ParseError

ROOT = Path(__file__).resolve().parents[3]
ITEMS = "sku,name,price,tags,reorder_level"
STOCK = "sku,on_hand,reserved"
ORDERS = "order_id,customer,placed,status,sku,quantity,unit_price"


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def message(reader, text: str) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(ParseError) as caught:
        reader(text)
    return str(caught.value)


def test_catalogue_errors_count_blank_lines() -> None:
    text = f"{ITEMS}\n\nMUG-001,Mug,1.00,,0\n   \n\nNB-200,Book,2.00,,x\n"
    assert message(read_items, text) == "line 6: reorder_level is not a whole number: 'x'"
    text = f"\n\t\n{ITEMS}\nMUG-001,Mug,1.00,,0\n\nNB-200,Book,2.00,x\n"
    assert message(read_items, text) == "line 6: expected 5 fields"
    text = f"{ITEMS}\r\n\r\nMUG-001,Mug,1.00,,0\r\n\r\n\r\nNB-200,Book,2.00,,y\r\n"
    assert message(read_items, text) == "line 6: reorder_level is not a whole number: 'y'"
    text = f"{ITEMS}\nMUG-001,Mug,1.00,,z\n\n"
    assert message(read_items, text) == "line 2: reorder_level is not a whole number: 'z'"


def test_stock_errors_count_blank_lines_and_quoted_line_breaks() -> None:
    text = f"{STOCK}\n\nMUG-001,5,0\n  \n\nNB-200,x,0\n"
    assert message(read_stock, text) == "line 6: on_hand is not a whole number: 'x'"
    text = f"\n\n{STOCK}\nMUG-001,5,0\n , ,\nNB-200,4\n"
    assert message(read_stock, text) == "line 6: expected 3 fields"
    text = f'{STOCK}\nMUG-001,"5\n",0\nNB-200,4,q\n'
    assert message(read_stock, text) == "line 4: reserved is not a whole number: 'q'"
    text = f'{STOCK}\r\nMUG-001,"\r\n\r\n5",0\r\n\r\nNB-200,4,r\r\n'
    assert message(read_stock, text) == "line 6: reserved is not a whole number: 'r'"
    text = f'{STOCK}\nMUG-001,5,0\n\nNB-200,"x\ny",0\n'
    assert message(read_stock, text) == "line 4: on_hand is not a whole number: 'x\\ny'"
    assert read_stock(f'\n{STOCK}\n\nMUG-001,"5\n",0\n\n') == [("MUG-001", 5, 0)]


def test_order_errors_count_blank_lines_and_quoted_line_breaks() -> None:
    row = "2024-01-08,paid,MUG-001"
    text = f"{ORDERS}\n\nA-1,Hana,{row},1,1.00\n\n\nA-2,Omar,{row},x,1.00\n"
    assert message(read_orders, text) == "line 6: quantity is not a whole number: 'x'"
    text = (
        f'{ORDERS}\nA-1,"Hana\nKim",{row},1,1.00\n'
        f'A-2,"Omar\n\nDiaz",{row},2,1.00\nA-3,L,{row},-,1.00\n'
    )
    assert message(read_orders, text) == "line 7: quantity is not a whole number: '-'"
    text = f'{ORDERS}\r\nA-1,H,{row},1,1.00\r\n\r\nA-2,"Omar\r\nDiaz",{row},y,1.00\r\n'
    assert message(read_orders, text) == "line 4: quantity is not a whole number: 'y'"
    text = f"{ORDERS}\nA-1,H,{row},1.5,1.00\n"
    assert message(read_orders, text) == "line 2: quantity is not a whole number: '1.5'"


def test_command_line_errors_name_the_physical_line(tmp_path: Path) -> None:
    stock = tmp_path / "stock.csv"
    stock.write_text(f"{STOCK}\n\nMUG-001,14,2\n\nMUG-002,four,0\n", encoding="utf-8")
    done = run("stock", "--items", "data/items.csv", "--stock", str(stock))
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == "stockroom: error: line 5: on_hand is not a whole number: 'four'\n"
    items = tmp_path / "items.csv"
    items.write_text(
        f"{ITEMS}\n\n\nMUG-001,Mug,1.00,,0\nMUG-002,Mug 2,1.00,,many\n", encoding="utf-8"
    )
    done = run("items", "--file", str(items))
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == "stockroom: error: line 5: reorder_level is not a whole number: 'many'\n"
    orders = tmp_path / "orders.csv"
    orders.write_text(
        f'{ORDERS}\nA-1,"Hana\nKim",2024-01-08,paid,MUG-001,1,1.00\n\nA-2,Omar,2024-01-09,paid,MUG-001,two,1.00\n',
        encoding="utf-8",
    )
    done = run("sales", "--orders", str(orders))
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == "stockroom: error: line 5: quantity is not a whole number: 'two'\n"


class Builder:
    """Text built line by line; ``line`` is the number of the line being written."""

    def __init__(self, end: str) -> None:
        self.parts: list[str] = []
        self.line = 1
        self.end = end

    def add(self, text: str) -> int:
        start = self.line
        self.parts.append(text + self.end)
        self.line += text.count("\n") + 1
        return start

    def text(self) -> str:
        return "".join(self.parts)


def test_generated_files_report_the_line_where_the_bad_row_starts() -> None:
    rng = random.Random(4242)
    for _ in range(200):
        end = rng.choice(["\n", "\r\n"])
        # stock
        b = Builder(end)
        for _ in range(rng.randint(0, 2)):
            b.add(rng.choice(["", "  ", "\t"]))
        b.add(STOCK)
        count = rng.randint(1, 8)
        bad = rng.randrange(count)
        expected = ""
        for k in range(count):
            for _ in range(rng.choice([0, 0, 1, 2])):
                b.add(rng.choice(["", "  ", "\t", " , ,", ",,"]))
            sku = f"MUG-{k:03d}"
            on_hand = rng.choice(["5", f'"5{end}"', f'"{end}7"', " 9 "])
            if k == bad:
                kind = rng.randrange(3)
                if kind == 0:
                    n = b.add(f"{sku},{on_hand},x")
                    expected = f"line {n}: reserved is not a whole number: 'x'"
                elif kind == 1:
                    n = b.add(f"{sku},{on_hand}")
                    expected = f"line {n}: expected 3 fields"
                else:
                    n = b.add(f'{sku},"1{end}2",0')
                    expected = f"line {n}: on_hand is not a whole number: {'1' + end + '2'!r}"
            else:
                b.add(f"{sku},{on_hand},0")
        assert message(read_stock, b.text()) == expected
        # catalogue
        b = Builder(end)
        for _ in range(rng.randint(0, 2)):
            b.add(rng.choice(["", "  ", "\t"]))
        b.add(ITEMS)
        count = rng.randint(1, 8)
        bad = rng.randrange(count)
        for k in range(count):
            for _ in range(rng.choice([0, 0, 1, 2])):
                b.add(rng.choice(["", "  ", "\t"]))
            if k == bad:
                if rng.random() < 0.5:
                    n = b.add(f"NB-{k:03d},Book {k},1.00,,lots")
                    expected = f"line {n}: reorder_level is not a whole number: 'lots'"
                else:
                    n = b.add(f"NB-{k:03d},Book {k},1.00,a;b")
                    expected = f"line {n}: expected 5 fields"
            else:
                b.add(f"NB-{k:03d},Book {k},1.00,a;b,{k}")
        assert message(read_items, b.text()) == expected
        # orders
        b = Builder(end)
        b.add(ORDERS)
        count = rng.randint(1, 8)
        bad = rng.randrange(count)
        for k in range(count):
            for _ in range(rng.choice([0, 0, 1, 2])):
                b.add("")
            customer = rng.choice(["Hana", f'"Hana{end}Kim"', f'"A{end}{end}B"', '"Kim, Hana"'])
            quantity = "nine" if k == bad else str(k + 1)
            n = b.add(f"Q-{k},{customer},2024-01-08,paid,MUG-001,{quantity},1.00")
            if k == bad:
                expected = f"line {n}: quantity is not a whole number: 'nine'"
        assert message(read_orders, b.text()) == expected
