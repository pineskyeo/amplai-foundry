import os
import subprocess
import sys
from pathlib import Path

from stockroom.csvio import read_items
from stockroom.lint import lint_orders

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
HEADER = "order_id,customer,placed,status,sku,quantity,unit_price"
ITEMS = read_items((DATA / "items.csv").read_text())


def _file(*rows: str, header: str = HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=base,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_a_clean_file_has_no_problems() -> None:
    text = (DATA / "orders.csv").read_text()
    assert lint_orders(text) == []
    assert lint_orders(text, ITEMS) == []
    assert lint_orders(_file()) == []
    assert lint_orders(_file("A-1,\"Kim, Hana\",2024-01-08,paid,MUG-001,2,12.50")) == []
    assert lint_orders(_file("A-1, Hana ,2024-01-08 , paid ,MUG-001 , 2 , 12.5 ", "A-1,Hana,2024-01-08,paid,TEA-010,1,8")) == []
    assert lint_orders("order_id,customer,placed,status,sku,quantity,unit_price\r\nA-1,Hana,2024-01-08,paid,MUG-001,1,1.00\r\n") == []


def test_an_empty_file_and_a_bad_header_stop_the_check() -> None:
    for text in ("", "   \n \n\t\n", "\n"):
        assert lint_orders(text) == ["line 1: empty file"]
    assert lint_orders(HEADER) == []
    assert lint_orders(" order_id , customer ,placed,status,sku,quantity, unit_price \n") == []
    bad_headers = [
        "order_id,customer,placed,status,sku,quantity",
        "order_id,customer,placed,status,sku,quantity,unit_price,extra",
        "customer,order_id,placed,status,sku,quantity,unit_price",
        "Order_id,customer,placed,status,sku,quantity,unit_price",
        "order_id,customer,placed,status,sku,quantity,price",
        "",
    ]
    for header in bad_headers:
        text = header + "\nA-1,Hana,2024-01-08,nonsense,mug,0,x\nA-2,Hana\n"
        assert lint_orders(text) == ["line 1: bad header"], header
    assert lint_orders("\n" + HEADER + "\n") == ["line 1: bad header"]


def test_a_row_with_the_wrong_number_of_fields_is_reported_alone() -> None:
    text = _file(
        "A-1,Hana,2024-01-08,paid,MUG-001,2",
        "",
        "   ",
        "A-2,Hana,2024-01-08,paid,MUG-001,2,12.50,extra",
        "A-3,\"Kim, Hana\",2024-01-08,paid,MUG-001,2,12.50",
        "A-4,Kim, Hana,2024-01-08,paid,MUG-001,2,12.50",
        "A-5",
    )
    assert lint_orders(text) == [
        "line 2: expected 7 fields, found 6",
        "line 5: expected 7 fields, found 8",
        "line 7: expected 7 fields, found 8",
        "line 8: expected 7 fields, found 1",
    ]


def test_each_field_is_checked_and_problems_of_a_row_come_in_field_order() -> None:
    text = _file(
        "A-1,Hana,2024-01-08,Paid,MUG-001,2,12.50",
        "A-2,Hana,2024-02-30,paid,MUG-001,2,12.50",
        "A-3,Hana,2024-2-3,paid,MUG-001,2,12.50",
        "A-4,Hana,20240203,paid,MUG-001,2,12.50",
        "A-5,Hana,2024-W05-1,paid,MUG-001,2,12.50",
        "A-6,Hana,2024-01-08,paid,mug-001,2,12.50",
        "A-7,Hana,2024-01-08,paid,MUG-1,2,12.50",
        "A-8,Hana,2024-01-08,paid,MUG-001,0,12.50",
        "A-9,Hana,2024-01-08,paid,MUG-001,-1,12.50",
        "B-1,Hana,2024-01-08,paid,MUG-001,1.5,12.50",
        "B-2,Hana,2024-01-08,paid,MUG-001,+2,12.50",
        "B-3,Hana,2024-01-08,paid,MUG-001,,12.50",
        "B-4,Hana,2024-01-08,paid,MUG-001,2,-1.00",
        "B-5,Hana,2024-01-08,paid,MUG-001,2,$5.00",
        'B-6,Hana,2024-01-08,paid,MUG-001,2,"1,250.00"',
        "B-7,Hana,2024-01-08,paid,MUG-001,2,5.",
        "B-8,Hana,2024-01-08,paid,MUG-001,2,.5",
        "B-9,Hana,2024-01-08,paid,MUG-001,2,5.005",
        "C-1,Hana,2024-01-08,paid,MUG-001,2,",
        "C-2,Hana,2024-01-08,paid,MUG-001,2,0",
        "C-3,Hana,2024-01-08,paid,MUG-001,2,7.5",
        "C-4,Hana,2024-01-08,,MUG-001,2,12.50",
        "C-5,Hana,0000-01-01,paid,MUG-001,2,12.50",
    )
    assert lint_orders(text) == [
        "line 2: unknown status 'Paid'",
        "line 3: bad date '2024-02-30'",
        "line 4: bad date '2024-2-3'",
        "line 5: bad date '20240203'",
        "line 6: bad date '2024-W05-1'",
        "line 7: bad sku 'mug-001'",
        "line 8: bad sku 'MUG-1'",
        "line 9: bad quantity '0'",
        "line 10: bad quantity '-1'",
        "line 11: bad quantity '1.5'",
        "line 12: bad quantity '+2'",
        "line 13: bad quantity ''",
        "line 14: bad price '-1.00'",
        "line 15: bad price '$5.00'",
        "line 16: bad price '1,250.00'",
        "line 17: bad price '5.'",
        "line 18: bad price '.5'",
        "line 19: bad price '5.005'",
        "line 20: bad price ''",
        "line 23: unknown status ''",
        "line 24: bad date '0000-01-01'",
    ]
    several = _file("A-1,Hana,2024-13-01,shipped?,mug,x,y")
    assert lint_orders(several) == [
        "line 2: unknown status 'shipped?'",
        "line 2: bad date '2024-13-01'",
        "line 2: bad sku 'mug'",
        "line 2: bad quantity 'x'",
        "line 2: bad price 'y'",
    ]


def test_rows_of_one_order_must_agree_with_its_first_row() -> None:
    text = _file(
        "A-1,Hana,2024-01-08,paid,MUG-001,1,12.50",
        "A-2,Omar,2024-01-09,open,MUG-001,1,12.50",
        "A-1,hana,2024-01-08,paid,TEA-010,1,8.95",
        "A-1,Hana,2024-01-09,shipped,PEN-100,1,34.00",
        "A-1,Omar,2024-01-10,cancelled,NB-200,1,6.25",
        "A-2,Omar,2024-01-09,open,TEA-010,1,8.95",
        "A-1,Hana,2024-01-08",
        "A-1,Hana,2024-01-08,paid,LMP-030,1,49.99",
    )
    assert lint_orders(text) == [
        "line 4: customer differs from line 2 for order 'A-1'",
        "line 5: placed differs from line 2 for order 'A-1'",
        "line 5: status differs from line 2 for order 'A-1'",
        "line 6: customer differs from line 2 for order 'A-1'",
        "line 6: placed differs from line 2 for order 'A-1'",
        "line 6: status differs from line 2 for order 'A-1'",
        "line 8: expected 7 fields, found 3",
    ]


def test_a_first_row_with_problems_still_defines_its_order() -> None:
    text = _file(
        "A-1,Hana,2024-01-08,paid!,mug,1,x",
        "A-1,Hana,2024-01-08,paid!,TEA-010,1,8.95",
        "A-1,Hana,2024-01-08,paid,TEA-011,1,7.40",
    )
    assert lint_orders(text) == [
        "line 2: unknown status 'paid!'",
        "line 2: bad sku 'mug'",
        "line 2: bad price 'x'",
        "line 3: unknown status 'paid!'",
        "line 4: status differs from line 2 for order 'A-1'",
    ]


def test_an_order_may_not_repeat_a_sku() -> None:
    text = _file(
        "A-1,Hana,2024-01-08,paid,MUG-001,1,12.50",
        "A-1,Hana,2024-01-08,paid,TEA-010,1,8.95",
        "A-2,Hana,2024-01-08,paid,MUG-001,1,12.50",
        "A-1,Hana,2024-01-08,paid,MUG-001,3,12.50",
        "A-1,Omar,2024-01-08,paid,TEA-010 ,1,8.95",
        "A-3,Hana,2024-01-08,paid,mug,1,12.50",
        "A-3,Hana,2024-01-08,paid,mug,1,12.50",
    )
    assert lint_orders(text) == [
        "line 5: duplicate line for order 'A-1' and sku 'MUG-001'",
        "line 6: customer differs from line 2 for order 'A-1'",
        "line 6: duplicate line for order 'A-1' and sku 'TEA-010'",
        "line 7: bad sku 'mug'",
        "line 8: bad sku 'mug'",
        "line 8: duplicate line for order 'A-3' and sku 'mug'",
    ]


def test_with_a_catalogue_skus_and_prices_are_compared() -> None:
    text = _file(
        "A-1,Hana,2024-01-08,paid,MUG-001,1,12.50",
        "A-1,Hana,2024-01-08,paid,MUG-002,1,12.00",
        "A-1,Hana,2024-01-08,paid,TEA-010,1,8.9",
        "A-1,Hana,2024-01-08,paid,XYZ-999,1,1.00",
        "A-1,Hana,2024-01-08,paid,PEN-100,1,34",
        "A-1,Hana,2024-01-08,paid,NB-200,1,6.250",
        "A-1,Hana,2024-01-08,paid,lmp-030,1,49.99",
        "A-1,Hana,2024-01-08,paid,CND-007,1,9.8x",
        "A-2,Hana,2024-01-08,paid,XYZ-999,0,x",
    )
    assert lint_orders(text, ITEMS) == [
        "line 3: price 12.00 differs from catalogue 12.50",
        "line 4: price 8.90 differs from catalogue 8.95",
        "line 5: unknown item 'XYZ-999'",
        "line 7: bad price '6.250'",
        "line 8: bad sku 'lmp-030'",
        "line 9: bad price '9.8x'",
        "line 10: bad quantity '0'",
        "line 10: bad price 'x'",
        "line 10: unknown item 'XYZ-999'",
    ]
    assert lint_orders(text.replace("TEA-010,1,8.9\n", "TEA-010,1,8.95\n"), [])[:1] == [
        "line 2: unknown item 'MUG-001'"
    ]
    plain = lint_orders(text)
    assert not any("catalogue" in p or "unknown item" in p for p in plain)


def test_the_command_says_ok_or_lists_the_problems_and_exits_with_one(tmp_path: Path) -> None:
    ok = run("lint-orders", "--orders", "data/orders.csv")
    assert ok.returncode == 0 and ok.stdout == "orders ok\n" and ok.stderr == ""
    ok = run("lint-orders", "--orders", "data/orders.csv", "--items", "data/items.csv")
    assert ok.returncode == 0 and ok.stdout == "orders ok\n"
    bad = tmp_path / "orders.csv"
    bad.write_text(
        _file(
            "A-1,Hana,2024-01-08,paid,MUG-001,1,12.00",
            "A-1,Hana,2024-01-09,paid,MUG-001,1,12.50",
            "A-2,Hana,2024-01-08,paid,XYZ-999,2",
        )
    )
    plain = run("lint-orders", "--orders", str(bad))
    assert plain.returncode == 1 and plain.stderr == ""
    assert plain.stdout.splitlines() == [
        "line 3: placed differs from line 2 for order 'A-1'",
        "line 3: duplicate line for order 'A-1' and sku 'MUG-001'",
        "line 4: expected 7 fields, found 6",
        "3 problem(s)",
    ]
    full = run("lint-orders", "--orders", str(bad), "--items", "data/items.csv")
    assert full.returncode == 1
    assert full.stdout.splitlines() == [
        "line 2: price 12.00 differs from catalogue 12.50",
        "line 3: placed differs from line 2 for order 'A-1'",
        "line 3: duplicate line for order 'A-1' and sku 'MUG-001'",
        "line 4: expected 7 fields, found 6",
        "4 problem(s)",
    ]
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    done = run("lint-orders", "--orders", str(empty))
    assert done.returncode == 1 and done.stdout == "line 1: empty file\n1 problem(s)\n"
