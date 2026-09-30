from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BAD_BYTES = b"sku,name\n\xff\xfe\x80 not utf-8 \xc3\x28\n"

# (command, option that names an input file, other arguments)
INPUTS = [
    ("items", "--file", ["items"]),
    ("stock", "--items", ["stock", "--stock", "data/stock.csv"]),
    ("stock", "--stock", ["stock", "--items", "data/items.csv"]),
    ("sales", "--orders", ["sales"]),
    ("price", "--items", ["price", "--sku", "MUG-001", "--quantity", "1"]),
]


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


def command_with(option: str, fixed: list[str], path: str) -> list[str]:
    return [*fixed, option, path]


def assert_one_error_line(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stderr)
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    lines = done.stderr.splitlines()
    assert len(lines) == 1, done.stderr
    assert done.stderr.endswith("\n")
    assert lines[0].startswith("stockroom: error: ") and len(lines[0]) > len("stockroom: error: ")
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def test_missing_input_file_for_every_command(tmp_path: Path) -> None:
    for _name, option, fixed in INPUTS:
        missing = str(tmp_path / f"no-such-{option.strip('-')}.csv")
        done = run(*command_with(option, fixed, missing))
        assert_one_error_line(done, missing)
    # a relative path is reported as it was typed
    done = run("items", "--file", "data/nope.csv")
    assert_one_error_line(done, "data/nope.csv")


def test_directory_as_input_file_for_every_input_option(tmp_path: Path) -> None:
    folder = tmp_path / "a-directory"
    folder.mkdir()
    for _name, option, fixed in INPUTS:
        done = run(*command_with(option, fixed, str(folder)))
        assert_one_error_line(done, str(folder))


def test_file_that_is_not_utf8_for_every_input_option(tmp_path: Path) -> None:
    bad = tmp_path / "binary.csv"
    bad.write_bytes(BAD_BYTES)
    for _name, option, fixed in INPUTS:
        done = run(*command_with(option, fixed, str(bad)))
        assert_one_error_line(done, str(bad))


def test_config_file_that_is_not_utf8(tmp_path: Path) -> None:
    bad = tmp_path / "settings.ini"
    bad.write_bytes(b"[stockroom]\ncurrency_symbol = \xff\xfe\n")
    done = run("--config", str(bad), "items", "--file", "data/items.csv")
    assert_one_error_line(done, str(bad))
    # the config problems that were already reported keep working
    done = run("--config", str(tmp_path / "missing.ini"), "items", "--file", "data/items.csv")
    assert_one_error_line(done, str(tmp_path / "missing.ini"))
    done = run("--config", str(tmp_path), "items", "--file", "data/items.csv")
    assert_one_error_line(done, str(tmp_path))


@pytest.mark.parametrize(
    "value", ["150", "100.01", "101", "-5", "-0.01", "nan", "inf", "-inf", "1e9", "NaN"]
)
def test_discount_outside_zero_to_hundred_or_not_finite(value: str) -> None:
    done = run(
        "price",
        "--items",
        "data/items.csv",
        "--sku",
        "MUG-001",
        "--quantity",
        "3",
        f"--discount={value}",
    )
    assert_one_error_line(done, "discount")


def price(discount: str | None, quantity: str = "3") -> subprocess.CompletedProcess[str]:
    args = ["price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", quantity]
    if discount is not None:
        args += [f"--discount={discount}"]
    return run(*args)


def test_discount_bounds_and_ordinary_values_work() -> None:
    done = price("100")
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout.splitlines() == [
        "3 x MUG-001 Blue Mug: $0.00",
        "tax: $0.00",
        "total: $0.00",
    ]
    done = price("100.0")
    assert done.returncode == 0 and done.stdout.splitlines()[0].endswith("$0.00")
    done = price("0")
    assert done.returncode == 0 and done.stdout.splitlines()[0].endswith("$37.50")
    done = price("10", quantity="2")
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout.splitlines() == [
        "2 x MUG-001 Blue Mug: $22.50",
        "tax: $0.00",
        "total: $22.50",
    ]
    done = price("50", quantity="2")
    assert done.stdout.splitlines()[0].endswith("$12.50")
    done = run(
        "price",
        "--items",
        "data/items.csv",
        "--sku",
        "MUG-001",
        "--quantity",
        "2",
        "--discount",
        "50",
        env={"STOCKROOM_TAX_BP": "1000"},
    )
    assert done.stdout.splitlines() == [
        "2 x MUG-001 Blue Mug: $12.50",
        "tax: $1.25",
        "total: $13.75",
    ]


def test_successful_and_other_failing_runs_keep_their_behaviour(tmp_path: Path) -> None:
    done = run("items", "--file", "data/items.csv", "--tag", "tea")
    assert done.returncode == 0 and done.stderr == ""
    assert [line.split()[0] for line in done.stdout.splitlines()[2:]] == ["TEA-010", "TEA-011"]
    done = run("stock", "--items", "data/items.csv", "--stock", "data/stock.csv")
    assert done.returncode == 0
    assert [line.split()[1] for line in done.stdout.splitlines()] == [
        "MUG-002",
        "TEA-011",
        "PEN-100",
    ]
    assert done.stderr == "warning: 3 item(s) below reorder level\n"
    done = run("sales", "--orders", "data/orders.csv", "--from", "2024-02-01")
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout.splitlines()[:2] == ["3 orders, 1 cancelled", "Revenue: $81.70"]
    # data errors: an unknown SKU, a bad header, a zero quantity
    done = run("price", "--items", "data/items.csv", "--sku", "XYZ-999", "--quantity", "1")
    assert_one_error_line(done, "XYZ-999")
    odd = tmp_path / "odd.csv"
    odd.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    done = run("items", "--file", str(odd))
    assert_one_error_line(done, "header")
    done = run("price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "0")
    assert_one_error_line(done, "quantity")
    # usage errors stay exit code 2
    assert run("items").returncode == 2
    assert run("price", "--items", "data/items.csv", "--sku", "MUG-001").returncode == 2
    done = run("price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "x")
    assert done.returncode == 2
    # a good settings file with a good input still works
    ini = tmp_path / "s.ini"
    ini.write_text("[stockroom]\ncurrency_symbol = EUR\n", encoding="utf-8")
    done = run("--config", str(ini), "items", "--file", "data/items.csv")
    assert done.returncode == 0 and done.stdout.splitlines()[2].endswith("EUR12.50")
