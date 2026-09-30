import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_version() -> None:
    done = run("--version")
    assert done.returncode == 0 and done.stdout.strip() == "stockroom 0.4.0"


def test_items() -> None:
    done = run("items", "--file", "data/items.csv")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0].split() == ["SKU", "Name", "Price"]
    assert len(lines) == 2 + 8
    assert "MUG-001" in lines[2] and lines[2].endswith("$12.50")


def test_items_by_tag() -> None:
    done = run("items", "--file", "data/items.csv", "--tag", "TEA")
    skus = [line.split()[0] for line in done.stdout.splitlines()[2:]]
    assert skus == ["TEA-010", "TEA-011"]


def test_stock() -> None:
    done = run("stock", "--items", "data/items.csv", "--stock", "data/stock.csv")
    assert done.returncode == 0, done.stderr
    assert [line.split()[1] for line in done.stdout.splitlines()] == [
        "MUG-002",
        "TEA-011",
        "PEN-100",
    ]
    assert "3 item(s) below reorder level" in done.stderr


def test_stock_warning_off_by_file(tmp_path: Path) -> None:
    config = tmp_path / "s.ini"
    config.write_text("[stockroom]\nlow_stock_warning = off\n")
    done = run(
        "--config", str(config), "stock", "--items", "data/items.csv", "--stock", "data/stock.csv"
    )
    assert done.returncode == 0 and done.stderr == ""


def test_sales() -> None:
    done = run("sales", "--orders", "data/orders.csv")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[:2] == ["5 orders, 1 cancelled", "Revenue: $149.65"]


def test_sales_from() -> None:
    done = run("sales", "--orders", "data/orders.csv", "--from", "2024-02-01")
    assert done.stdout.splitlines()[:2] == ["3 orders, 1 cancelled", "Revenue: $81.70"]


def test_price() -> None:
    done = run("price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "3")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "3 x MUG-001 Blue Mug: $37.50",
        "tax: $0.00",
        "total: $37.50",
    ]


def test_price_with_tax_from_env() -> None:
    done = run(
        "price",
        "--items",
        "data/items.csv",
        "--sku",
        "MUG-001",
        "--quantity",
        "3",
        env={"STOCKROOM_TAX_BP": "1000"},
    )
    assert done.stdout.splitlines()[1:] == ["tax: $3.75", "total: $41.25"]


def test_data_error_exit_code() -> None:
    done = run("price", "--items", "data/items.csv", "--sku", "XYZ-999", "--quantity", "1")
    assert done.returncode == 1
    assert done.stderr.startswith("stockroom: error: ") and done.stdout == ""


def test_usage_error_exit_code() -> None:
    assert run().returncode == 2
    assert run("items").returncode == 2
