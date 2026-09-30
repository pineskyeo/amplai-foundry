from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
WARNING = "warning: 3 item(s) below reorder level\n"


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


def dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


ITEMS = ["items", "--file", "data/items.csv"]
STOCK = ["stock", "--items", "data/items.csv", "--stock", "data/stock.csv"]
SALES = ["sales", "--orders", "data/orders.csv"]

ALL_ITEMS = [
    {"sku": "MUG-001", "name": "Blue Mug", "price_cents": 1250, "tags": ["kitchen", "gift"], "reorder_level": 5},
    {"sku": "MUG-002", "name": "Red Mug", "price_cents": 1250, "tags": ["kitchen"], "reorder_level": 5},
    {"sku": "TEA-010", "name": "Green Tea 100g", "price_cents": 895, "tags": ["food", "tea"], "reorder_level": 10},
    {"sku": "TEA-011", "name": "Black Tea 100g", "price_cents": 740, "tags": ["food", "tea"], "reorder_level": 10},
    {"sku": "PEN-100", "name": "Fountain Pen", "price_cents": 3400, "tags": ["office", "gift"], "reorder_level": 2},
    {"sku": "NB-200", "name": "Notebook A5", "price_cents": 625, "tags": ["office"], "reorder_level": 20},
    {"sku": "LMP-030", "name": "Desk Lamp", "price_cents": 4999, "tags": ["office", "home"], "reorder_level": 1},
    {"sku": "CND-007", "name": "Beeswax Candle", "price_cents": 980, "tags": ["home", "gift"], "reorder_level": 8},
]


def test_items_json() -> None:
    done = run(*ITEMS, "--format", "json")
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout == dump(ALL_ITEMS)
    done = run(*ITEMS, "--tag", "TEA", "--format", "json")
    assert done.stdout == dump([ALL_ITEMS[2], ALL_ITEMS[3]])
    done = run(*ITEMS, "--format", "json", "--tag", "gift")
    assert done.stdout == dump([ALL_ITEMS[0], ALL_ITEMS[4], ALL_ITEMS[7]])
    done = run(*ITEMS, "--tag", "nothing", "--format", "json")
    assert done.returncode == 0 and done.stdout == "[]\n"
    # the explicit text format is the default output
    plain = run(*ITEMS)
    assert run(*ITEMS, "--format", "text").stdout == plain.stdout
    assert plain.stdout.splitlines()[0].split() == ["SKU", "Name", "Price"]


def test_items_json_reads_quoted_and_mixed_case_data(tmp_path: Path) -> None:
    path = tmp_path / "items.csv"
    path.write_text(
        'sku,name,price,tags,reorder_level\nBOX-001,Crème Brûlée Set,7.05,Kitchen; GIFT ;,3\nBOX-002,Plain,0.00,,0\n',
        encoding="utf-8",
    )
    done = run("items", "--file", str(path), "--format", "json")
    assert done.returncode == 0, done.stderr
    assert done.stdout == dump(
        [
            {"sku": "BOX-001", "name": "Crème Brûlée Set", "price_cents": 705, "tags": ["kitchen", "gift"], "reorder_level": 3},
            {"sku": "BOX-002", "name": "Plain", "price_cents": 0, "tags": [], "reorder_level": 0},
        ]
    )


LOW = [
    {"sku": "MUG-002", "name": "Red Mug", "available": 4, "reorder_level": 5},
    {"sku": "TEA-011", "name": "Black Tea 100g", "available": 9, "reorder_level": 10},
    {"sku": "PEN-100", "name": "Fountain Pen", "available": 1, "reorder_level": 2},
]


def test_stock_json(tmp_path: Path) -> None:
    done = run(*STOCK, "--format", "json")
    assert done.returncode == 0
    assert done.stdout == dump({"low": LOW})
    assert done.stderr == WARNING
    # the warning follows the setting as before
    quiet = tmp_path / "quiet.ini"
    quiet.write_text("[stockroom]\nlow_stock_warning = off\n", encoding="utf-8")
    done = run("--config", str(quiet), *STOCK, "--format", "json")
    assert done.returncode == 0 and done.stderr == "" and done.stdout == dump({"low": LOW})
    # nothing is low
    ok_stock = tmp_path / "stock.csv"
    ok_stock.write_text("sku,on_hand,reserved\n" + "\n".join(
        f"{i['sku']},100,0" for i in ALL_ITEMS) + "\n", encoding="utf-8")
    done = run("stock", "--items", "data/items.csv", "--stock", str(ok_stock), "--format", "json")
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout == dump({"low": []})
    assert done.stdout == '{\n  "low": []\n}\n'
    done = run("stock", "--items", "data/items.csv", "--stock", str(ok_stock))
    assert done.returncode == 0 and done.stdout == "stock ok\n"
    # items missing from the stock file have nothing available
    partial = tmp_path / "partial.csv"
    partial.write_text("sku,on_hand,reserved\nMUG-001,100,0\nLMP-030,5,4\n", encoding="utf-8")
    done = run("stock", "--items", "data/items.csv", "--stock", str(partial), "--format", "json")
    data = json.loads(done.stdout)
    skus = [row["sku"] for row in data["low"]]
    assert skus == ["MUG-002", "TEA-010", "TEA-011", "PEN-100", "NB-200", "CND-007"]
    assert {r["sku"]: r["available"] for r in data["low"]}["MUG-002"] == 0
    assert done.stdout == dump(data)
    # reserved stock is not available
    reserved = tmp_path / "reserved.csv"
    reserved.write_text("sku,on_hand,reserved\nMUG-001,10,6\n", encoding="utf-8")
    done = run("stock", "--items", "data/items.csv", "--stock", str(reserved), "--format", "json")
    first = json.loads(done.stdout)["low"][0]
    assert first == {"sku": "MUG-001", "name": "Blue Mug", "available": 4, "reorder_level": 5}


SALES_ALL = {
    "orders": 5,
    "cancelled": 1,
    "revenue_cents": 14965,
    "months": {"2024-01": 6795, "2024-02": 5440, "2024-03": 2730},
    "top_customers": [
        {"customer": "Hana Kim", "total_cents": 8835},
        {"customer": "Omar Diaz", "total_cents": 4650},
        {"customer": "Lena Berg", "total_cents": 1480},
    ],
}


def test_sales_json() -> None:
    done = run(*SALES, "--format", "json")
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout == dump(SALES_ALL)
    done = run(*SALES, "--from", "2024-02-01", "--format", "json")
    assert done.stdout == dump(
        {
            "orders": 3,
            "cancelled": 1,
            "revenue_cents": 8170,
            "months": {"2024-02": 5440, "2024-03": 2730},
            "top_customers": [
                {"customer": "Hana Kim", "total_cents": 5440},
                {"customer": "Lena Berg", "total_cents": 1480},
                {"customer": "Omar Diaz", "total_cents": 1250},
            ],
        }
    )
    done = run(*SALES, "--format", "json", "--from", "2024-03-01")
    assert done.stdout == dump(
        {
            "orders": 2,
            "cancelled": 0,
            "revenue_cents": 2730,
            "months": {"2024-03": 2730},
            "top_customers": [
                {"customer": "Lena Berg", "total_cents": 1480},
                {"customer": "Omar Diaz", "total_cents": 1250},
            ],
        }
    )
    done = run(*SALES, "--from", "2025-01-01", "--format", "json")
    assert done.returncode == 0
    assert done.stdout == dump(
        {"orders": 0, "cancelled": 0, "revenue_cents": 0, "months": {}, "top_customers": []}
    )
    # the figures of the text format are the same numbers in dollars
    text = run(*SALES).stdout.splitlines()
    assert text[:2] == ["5 orders, 1 cancelled", "Revenue: $149.65"]


def test_sales_json_ranks_customers_and_ignores_cancelled_orders(tmp_path: Path) -> None:
    header = "order_id,customer,placed,status,sku,quantity,unit_price\n"
    rows = [
        "1,Bo,2024-05-03,paid,MUG-001,1,10.00",
        "2,Al,2024-05-20,shipped,MUG-001,1,10.00",
        "3,Cy,2024-06-01,open,MUG-001,2,10.00",
        "4,Di,2024-06-02,paid,MUG-001,1,5.00",
        "5,Ed,2024-06-03,cancelled,MUG-001,9,111.00",
        "6,Al,2024-07-04,cancelled,MUG-001,3,10.00",
        "7,Bo,2024-05-31,cancelled,MUG-001,1,10.00",
    ]
    path = tmp_path / "orders.csv"
    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
    done = run("sales", "--orders", str(path), "--format", "json")
    assert done.returncode == 0, done.stderr
    assert done.stdout == dump(
        {
            "orders": 4,
            "cancelled": 3,
            "revenue_cents": 4500,
            "months": {"2024-05": 2000, "2024-06": 2500},
            "top_customers": [
                {"customer": "Cy", "total_cents": 2000},
                {"customer": "Al", "total_cents": 1000},
                {"customer": "Bo", "total_cents": 1000},
            ],
        }
    )
    # only cancelled orders: a month with no revenue order does not appear
    done = run("sales", "--orders", str(path), "--from", "2024-07-01", "--format", "json")
    assert done.stdout == dump(
        {"orders": 0, "cancelled": 1, "revenue_cents": 0, "months": {}, "top_customers": []}
    )
    done = run("sales", "--orders", str(path), "--from", "2024-06-01", "--format", "json")
    assert json.loads(done.stdout)["top_customers"] == [
        {"customer": "Cy", "total_cents": 2000},
        {"customer": "Di", "total_cents": 500},
    ]


def test_fail_on_low(tmp_path: Path) -> None:
    plain = run(*STOCK)
    for extra in ([], ["--format", "text"]):
        done = run(*STOCK, "--fail-on-low", *extra)
        assert done.returncode == 3
        assert done.stdout == plain.stdout and done.stderr == plain.stderr == WARNING
    plain_json = run(*STOCK, "--format", "json")
    done = run(*STOCK, "--fail-on-low", "--format", "json")
    assert done.returncode == 3
    assert done.stdout == plain_json.stdout == dump({"low": LOW})
    assert done.stderr == WARNING
    quiet = tmp_path / "quiet.ini"
    quiet.write_text("[stockroom]\nlow_stock_warning = no\n", encoding="utf-8")
    done = run("--config", str(quiet), *STOCK, "--fail-on-low")
    assert done.returncode == 3 and done.stderr == ""
    # nothing is low: exit 0 with the flag
    ok_stock = tmp_path / "stock.csv"
    ok_stock.write_text("sku,on_hand,reserved\n" + "\n".join(
        f"{i['sku']},100,0" for i in ALL_ITEMS) + "\n", encoding="utf-8")
    for extra in ([], ["--format", "json"]):
        done = run("stock", "--items", "data/items.csv", "--stock", str(ok_stock), "--fail-on-low", *extra)
        assert done.returncode == 0 and done.stderr == ""
    assert run("stock", "--items", "data/items.csv", "--stock", str(ok_stock), "--fail-on-low").stdout == "stock ok\n"
    # without the flag the exit code stays 0 when items are low
    assert plain.returncode == 0 and plain_json.returncode == 0


def test_errors_and_unchanged_text(tmp_path: Path) -> None:
    for command in (ITEMS, STOCK, SALES):
        for value in ("xml", "JSON", ""):
            done = run(*command, "--format", value)
            assert done.returncode == 2 and done.stdout == ""
    assert run(*ITEMS, "--format").returncode == 2
    assert run("price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "1", "--format", "json").returncode == 2
    # data errors keep exit code 1, also with the new options
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n", encoding="utf-8")
    for args in (
        ["items", "--file", str(bad), "--format", "json"],
        ["stock", "--items", str(bad), "--stock", "data/stock.csv", "--fail-on-low"],
        ["stock", "--items", "data/items.csv", "--stock", str(bad), "--format", "json", "--fail-on-low"],
        ["sales", "--orders", str(bad), "--format", "json"],
    ):
        done = run(*args)
        assert done.returncode == 1 and done.stdout == ""
        assert done.stderr.startswith("stockroom: error: ") and len(done.stderr.splitlines()) == 1
    assert run("sales", "--orders", "data/orders.csv", "--from", "nonsense", "--format", "json").returncode == 1
    # text output is unchanged
    text = run(*ITEMS).stdout.splitlines()
    assert len(text) == 10 and text[2].startswith("MUG-001") and text[2].endswith("$12.50")
    done = run(*STOCK)
    assert [ln.split()[1] for ln in done.stdout.splitlines()] == ["MUG-002", "TEA-011", "PEN-100"]
    assert done.stdout.splitlines()[0] == "LOW MUG-002 Red Mug: 4/5"
    sales = run(*SALES, "--from", "2024-02-01").stdout.splitlines()
    assert sales[:2] == ["3 orders, 1 cancelled", "Revenue: $81.70"]
    done = run("price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "3")
    assert done.stdout.splitlines()[0] == "3 x MUG-001 Blue Mug: $37.50"
