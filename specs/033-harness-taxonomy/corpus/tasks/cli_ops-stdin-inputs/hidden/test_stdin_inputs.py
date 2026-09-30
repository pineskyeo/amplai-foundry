from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"


def run(
    *args: str,
    stdin: str | bytes = "",
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    base["PYTHONIOENCODING"] = "utf-8"
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd or ROOT,
        env={**base, **(env or {})},
        input=data,
        capture_output=True,
        timeout=50,
        check=False,
    )


def same(a: subprocess.CompletedProcess[bytes], b: subprocess.CompletedProcess[bytes]) -> None:
    assert (a.returncode, a.stdout, a.stderr) == (b.returncode, b.stdout, b.stderr)


def text(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def test_each_command_reads_its_input_from_standard_input() -> None:
    items, stock, orders = text("items.csv"), text("stock.csv"), text("orders.csv")
    file_items = run("items", "--file", str(DATA / "items.csv"))
    assert file_items.returncode == 0
    same(run("items", "--file", "-", stdin=items), file_items)
    same(
        run("items", "--file", "-", "--tag", "gift", stdin=items),
        run("items", "--file", str(DATA / "items.csv"), "--tag", "gift"),
    )
    file_stock = run("stock", "--items", str(DATA / "items.csv"), "--stock", str(DATA / "stock.csv"))
    assert file_stock.returncode == 0 and b"LOW MUG-002" in file_stock.stdout
    same(run("stock", "--items", "-", "--stock", str(DATA / "stock.csv"), stdin=items), file_stock)
    same(run("stock", "--items", str(DATA / "items.csv"), "--stock", "-", stdin=stock), file_stock)
    file_sales = run("sales", "--orders", str(DATA / "orders.csv"))
    assert file_sales.returncode == 0
    same(run("sales", "--orders", "-", stdin=orders), file_sales)
    same(
        run("sales", "--orders", "-", "--from", "2024-02-01", stdin=orders),
        run("sales", "--orders", str(DATA / "orders.csv"), "--from", "2024-02-01"),
    )
    price = ["--sku", "TEA-010", "--quantity", "4"]
    same(
        run("price", "--items", "-", *price, stdin=items),
        run("price", "--items", str(DATA / "items.csv"), *price),
    )
    # a data error in the text is reported the same way as for a file
    bad = run("price", "--items", "-", "--sku", "XYZ-999", "--quantity", "1", stdin=items)
    assert bad.returncode == 1 and bad.stdout == b""
    assert bad.stderr.startswith(b"stockroom: error: ")
    # the settings still apply
    same(
        run("items", "--file", "-", stdin=items, env={"STOCKROOM_CURRENCY_SYMBOL": "EUR"}),
        run("items", "--file", str(DATA / "items.csv"), env={"STOCKROOM_CURRENCY_SYMBOL": "EUR"}),
    )


def test_standard_input_is_decoded_as_utf8_text_mode(tmp_path: Path) -> None:
    content = (
        "sku,name,price,tags,reorder_level\n"
        "MUG-001,Crème Brûlée Set,12.50,kitchen,5\n"
        "TEA-010,Grüner Tee 日本,8.95,tea,10\n"
    )
    file = tmp_path / "items.csv"
    file.write_text(content, encoding="utf-8")
    expected = run("items", "--file", str(file))
    assert expected.returncode == 0 and "Crème Brûlée Set".encode() in expected.stdout
    same(run("items", "--file", "-", stdin=content), expected)
    crlf = content.replace("\n", "\r\n")
    same(run("items", "--file", "-", stdin=crlf), expected)
    crlf_file = tmp_path / "crlf.csv"
    crlf_file.write_bytes(crlf.encode("utf-8"))
    same(run("items", "--file", str(crlf_file)), expected)
    orders = text("orders.csv").replace("\n", "\r\n")
    same(
        run("sales", "--orders", "-", stdin=orders),
        run("sales", "--orders", str(DATA / "orders.csv")),
    )
    stock = text("stock.csv").replace("\n", "\r\n")
    same(
        run("stock", "--items", str(DATA / "items.csv"), "--stock", "-", stdin=stock),
        run("stock", "--items", str(DATA / "items.csv"), "--stock", str(DATA / "stock.csv")),
    )


def test_standard_input_twice_is_refused_before_any_output() -> None:
    for payload in (text("items.csv"), text("stock.csv"), ""):
        done = run("stock", "--items", "-", "--stock", "-", stdin=payload)
        assert done.returncode == 1, done.stderr
        assert done.stdout == b""
        lines = done.stderr.decode().splitlines()
        assert len(lines) == 1, lines
        assert lines[0].startswith("stockroom: error: ")
        assert "standard input" in lines[0]
        assert b"Traceback" not in done.stderr


def test_empty_standard_input_is_an_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    from_file = run("items", "--file", str(empty))
    assert from_file.returncode == 1 and b"stockroom: error: " in from_file.stderr
    same(run("items", "--file", "-", stdin=""), run("items", "--file", str(empty)))
    from_file = run("sales", "--orders", str(empty))
    assert from_file.returncode == 1
    same(run("sales", "--orders", "-", stdin=""), run("sales", "--orders", str(empty)))
    from_file = run("stock", "--items", str(DATA / "items.csv"), "--stock", str(empty))
    assert from_file.returncode == 1
    same(
        run("stock", "--items", str(DATA / "items.csv"), "--stock", "-", stdin=""),
        run("stock", "--items", str(DATA / "items.csv"), "--stock", str(empty)),
    )


def test_dot_slash_dash_is_an_ordinary_path(tmp_path: Path) -> None:
    (tmp_path / "-").write_text(text("items.csv"), encoding="utf-8")
    expected = run("items", "--file", str(DATA / "items.csv"))
    # standard input holds other data that must not be used
    done = run("items", "--file", "./-", stdin="sku,name\nbroken\n", cwd=tmp_path)
    same(done, expected)
    done = run(
        "stock",
        "--items",
        "./-",
        "--stock",
        str(DATA / "stock.csv"),
        stdin="not a catalogue",
        cwd=tmp_path,
    )
    same(
        done,
        run("stock", "--items", str(DATA / "items.csv"), "--stock", str(DATA / "stock.csv")),
    )
    # './-' and '-' together are two different inputs: no refusal
    done = run(
        "stock",
        "--items",
        "./-",
        "--stock",
        "-",
        stdin=text("stock.csv"),
        cwd=tmp_path,
    )
    same(
        done,
        run("stock", "--items", str(DATA / "items.csv"), "--stock", str(DATA / "stock.csv")),
    )


def test_config_dash_is_a_file_not_standard_input(tmp_path: Path) -> None:
    (tmp_path / "-").write_text("[stockroom]\ncurrency_symbol = EUR\n", encoding="utf-8")
    items = str(DATA / "items.csv")
    done = run(
        "--config",
        "-",
        "items",
        "--file",
        items,
        stdin="[stockroom]\ncurrency_symbol = GBP\n",
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode().splitlines()[2].endswith("EUR12.50")
    # with a settings file called '-' and '-' as the input, both work at once
    done = run("--config", "-", "items", "--file", "-", stdin=text("items.csv"), cwd=tmp_path)
    assert done.returncode == 0 and done.stdout.decode().splitlines()[2].endswith("EUR12.50")
    # no such file: the usual settings error, and standard input is not used as the settings
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    done = run(
        "--config",
        "-",
        "items",
        "--file",
        items,
        stdin="[stockroom]\ncurrency_symbol = GBP\n",
        cwd=empty_dir,
    )
    assert done.returncode == 1 and done.stdout == b""
    lines = done.stderr.decode().splitlines()
    assert len(lines) == 1 and lines[0].startswith("stockroom: error: ")


def test_ordinary_paths_still_work(tmp_path: Path) -> None:
    done = run("items", "--file", "data/items.csv", "--tag", "TEA")
    assert done.returncode == 0
    assert [ln.split()[0] for ln in done.stdout.decode().splitlines()[2:]] == ["TEA-010", "TEA-011"]
    done = run("sales", "--orders", "data/orders.csv")
    assert done.stdout.decode().splitlines()[:2] == ["5 orders, 1 cancelled", "Revenue: $149.65"]
    done = run("price", "--items", "data/items.csv", "--sku", "XYZ-999", "--quantity", "1")
    assert done.returncode == 1 and done.stdout == b""
    assert done.stderr.startswith(b"stockroom: error: ")
    assert run("items").returncode == 2
    assert run("items", "--file").returncode == 2
    # a file whose name merely contains a dash is a path
    odd = tmp_path / "-items.csv"
    odd.write_text(text("items.csv"), encoding="utf-8")
    same(run("items", "--file", str(odd)), run("items", "--file", str(DATA / "items.csv")))
