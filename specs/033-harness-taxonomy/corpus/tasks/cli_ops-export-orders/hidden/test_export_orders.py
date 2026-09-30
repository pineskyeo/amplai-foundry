from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]

HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"
ROWS = [
    # order, customer, placed, status, [(sku, quantity, price text, cents)]
    ("O-10", "Zed", "2024-03-05", "paid", [("MUG-001", 1, "12.50", 1250)]),
    ("O-2", "Amy", "2024-01-10", "open", [("TEA-010", 2, "8.95", 895), ("NB-200", 1, "6.25", 625)]),
    ("O-1", "amy", "2024-03-05", "shipped", [("PEN-100", 1, "34.00", 3400)]),
    ("O-3", "Zed", "2024-01-10", "cancelled", [("LMP-030", 1, "49.99", 4999)]),
    ("O-20", "Amy", "2024-02-01", "paid", [("CND-007", 3, "9.80", 980)]),
]


def orders_csv() -> str:
    lines = [HEADER.rstrip("\n")]
    for order_id, customer, placed, status, items in ROWS:
        for sku, quantity, price, _cents in items:
            lines.append(f"{order_id},{customer},{placed},{status},{sku},{quantity},{price}")
    return "\n".join(lines) + "\n"


def as_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    order_id, customer, placed, status, items = row
    return {
        "order_id": order_id,
        "customer": customer,
        "placed": placed,
        "status": status,
        "lines": [
            {"sku": sku, "quantity": quantity, "unit_price_cents": cents}
            for sku, quantity, _price, cents in items
        ],
    }


def expected(ids: list[str]) -> str:
    by_id = {row[0]: row for row in ROWS}
    return json.dumps([as_dict(by_id[i]) for i in ids], indent=2, sort_keys=True) + "\n"


FILE_ORDER = ["O-10", "O-2", "O-1", "O-3", "O-20"]


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd or ROOT,
        env=base,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def setup(tmp_path: Path) -> Path:
    path = tmp_path / "orders.csv"
    path.write_text(orders_csv(), encoding="utf-8")
    return path


def export(orders: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run("export", "--orders", str(orders), *args)


def ok(done: subprocess.CompletedProcess[str]) -> str:
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    return done.stdout


def assert_usage(done: subprocess.CompletedProcess[str]) -> None:
    assert done.returncode == 2, (done.returncode, done.stderr)
    assert done.stdout == ""
    assert done.stderr != ""


def assert_error(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("stockroom: error: "), done.stderr
    assert "Traceback" not in done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def test_export_prints_all_orders_in_file_order(tmp_path: Path) -> None:
    orders = setup(tmp_path)
    assert ok(export(orders)) == expected(FILE_ORDER)
    assert ok(export(orders, "--sort", "file")) == expected(FILE_ORDER)
    # the sample data of the repository: same text as dump_orders for its orders
    text = ok(run("export", "--orders", "data/orders.csv"))
    data = json.loads(text)
    assert [o["order_id"] for o in data] == [f"A-100{i}" for i in range(1, 7)]
    assert data[0] == {
        "customer": "Hana Kim",
        "lines": [
            {"quantity": 2, "sku": "MUG-001", "unit_price_cents": 1250},
            {"quantity": 1, "sku": "TEA-010", "unit_price_cents": 895},
        ],
        "order_id": "A-1001",
        "placed": "2024-01-08",
        "status": "shipped",
    }
    assert data[2]["status"] == "cancelled" and data[5]["status"] == "open"
    assert text == json.dumps(data, indent=2, sort_keys=True) + "\n"


def test_empty_selection_prints_an_empty_list(tmp_path: Path) -> None:
    orders = setup(tmp_path)
    assert ok(export(orders, "--customer", "Nobody")) == "[]\n"
    assert ok(export(orders, "--status", "paid", "--customer", "Zed", "--sort", "id")) == expected(
        ["O-10"]
    )
    assert ok(export(orders, "--status", "open", "--customer", "Zed")) == "[]\n"
    header_only = tmp_path / "header-only.csv"
    header_only.write_text(HEADER, encoding="utf-8")
    assert ok(export(header_only)) == "[]\n"


def test_status_and_customer_selection(tmp_path: Path) -> None:
    orders = setup(tmp_path)
    assert ok(export(orders, "--status", "paid")) == expected(["O-10", "O-20"])
    assert ok(export(orders, "--status", "paid", "--status", "open")) == expected(
        ["O-10", "O-2", "O-20"]
    )
    assert ok(export(orders, "--status", "paid", "--status", "paid")) == expected(["O-10", "O-20"])
    everything = ["--status", "open", "--status", "paid", "--status", "shipped", "--status", "cancelled"]
    assert ok(export(orders, *everything)) == expected(FILE_ORDER)
    assert ok(export(orders, "--status", "cancelled")) == expected(["O-3"])
    assert ok(export(orders, "--customer", "Amy")) == expected(["O-2", "O-20"])
    assert ok(export(orders, "--customer", "amy")) == expected(["O-1"])
    assert ok(export(orders, "--customer", "AMY")) == "[]\n"
    assert ok(export(orders, "--customer", "Zed")) == expected(["O-10", "O-3"])
    assert ok(export(orders, "--customer", "Amy", "--status", "paid")) == expected(["O-20"])
    assert ok(export(orders, "--status", "shipped", "--customer", "amy")) == expected(["O-1"])


def test_sorting(tmp_path: Path) -> None:
    orders = setup(tmp_path)
    assert ok(export(orders, "--sort", "placed")) == expected(["O-2", "O-3", "O-20", "O-1", "O-10"])
    assert ok(export(orders, "--sort", "id")) == expected(["O-1", "O-10", "O-2", "O-20", "O-3"])
    assert ok(export(orders, "--sort", "placed", "--status", "paid", "--status", "shipped")) == (
        expected(["O-20", "O-1", "O-10"])
    )
    assert ok(export(orders, "--customer", "Zed", "--sort", "placed")) == expected(["O-3", "O-10"])
    assert ok(export(orders, "--sort", "id", "--customer", "Zed")) == expected(["O-10", "O-3"])
    # the file is not modified by reading it
    assert orders.read_text(encoding="utf-8") == orders_csv()


def test_limit(tmp_path: Path) -> None:
    orders = setup(tmp_path)
    assert ok(export(orders, "--limit", "0")) == "[]\n"
    assert ok(export(orders, "--limit", "1")) == expected(["O-10"])
    assert ok(export(orders, "--limit", "2", "--sort", "placed")) == expected(["O-2", "O-3"])
    assert ok(export(orders, "--limit", "3", "--sort", "id")) == expected(["O-1", "O-10", "O-2"])
    assert ok(export(orders, "--limit", "99")) == expected(FILE_ORDER)
    assert ok(export(orders, "--limit", "5")) == expected(FILE_ORDER)
    # selection and sorting come before the limit
    assert ok(export(orders, "--status", "paid", "--sort", "placed", "--limit", "1")) == expected(
        ["O-20"]
    )
    assert ok(export(orders, "--customer", "Zed", "--limit", "1", "--sort", "placed")) == expected(
        ["O-3"]
    )
    assert ok(export(orders, "--status", "open", "--limit", "0")) == "[]\n"
    assert ok(export(orders, "--limit", "007")) == expected(FILE_ORDER)


def test_usage_errors(tmp_path: Path) -> None:
    orders = setup(tmp_path)
    out = tmp_path / "result.json"
    bad = [
        ["--status", "pending"],
        ["--status", "PAID"],
        ["--status", ""],
        ["--sort", "customer"],
        ["--sort", "Placed"],
        ["--limit", "-1"],
        ["--limit", "x"],
        ["--limit", "1.5"],
        ["--limit", ""],
    ]
    for extra in bad:
        assert_usage(export(orders, *extra, "--out", str(out)))
        assert not out.exists(), extra
    assert_usage(run("export"))
    assert_usage(run("export", "--orders"))
    assert listing(tmp_path) == ["orders.csv"]


def test_out_replaces_the_file_atomically(tmp_path: Path) -> None:
    orders = setup(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "orders.json"
    done = export(orders, "--out", str(out))
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    assert out.read_text(encoding="utf-8") == expected(FILE_ORDER)
    assert listing(out_dir) == ["orders.json"]
    # replaced, not appended, also when the old content is longer
    out.write_text("x" * 100000, encoding="utf-8")
    done = export(orders, "--limit", "1", "--out", str(out))
    assert done.returncode == 0 and done.stdout == ""
    assert out.read_text(encoding="utf-8") == expected(["O-10"])
    assert listing(out_dir) == ["orders.json"]
    done = export(orders, "--status", "open", "--customer", "Nobody", "--out", str(out))
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    assert out.read_text(encoding="utf-8") == "[]\n"
    assert listing(out_dir) == ["orders.json"]
    # a relative path, resolved against the current directory
    done = run("export", "--orders", str(orders), "--out", "rel.json", "--sort", "id", cwd=out_dir)
    assert done.returncode == 0 and done.stdout == ""
    assert (out_dir / "rel.json").read_text(encoding="utf-8") == expected(
        ["O-1", "O-10", "O-2", "O-20", "O-3"]
    )
    assert listing(out_dir) == ["orders.json", "rel.json"]
    # the output may be the input file: the orders are read before anything is replaced
    copy = tmp_path / "copy.csv"
    copy.write_text(orders_csv(), encoding="utf-8")
    done = export(copy, "--limit", "2", "--out", str(copy))
    assert done.returncode == 0, done.stderr
    assert copy.read_text(encoding="utf-8") == expected(["O-10", "O-2"])
    assert "copy.csv" in listing(tmp_path) and len(listing(tmp_path)) == 3
    # non-ASCII names survive, written as JSON text (escaped, as dump_orders does)
    text = orders_csv().replace("Zed", "Zoë Ünal")
    uni = tmp_path / "uni.csv"
    uni.write_text(text, encoding="utf-8")
    done = export(uni, "--customer", "Zoë Ünal", "--out", str(out_dir / "uni.json"))
    assert done.returncode == 0, done.stderr
    data = json.loads((out_dir / "uni.json").read_text(encoding="utf-8"))
    assert [o["order_id"] for o in data] == ["O-10", "O-3"] and data[0]["customer"] == "Zoë Ünal"


def test_failures_write_nothing(tmp_path: Path) -> None:
    orders = setup(tmp_path)
    bad = tmp_path / "bad.csv"
    bad.write_text("id,who\n1,2\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # an invalid orders file, with and without an existing output
    done = export(bad, "--out", str(out_dir / "new.json"))
    assert_error(done)
    assert listing(out_dir) == []
    (out_dir / "keep.json").write_text("previous content", encoding="utf-8")
    done = export(bad, "--out", str(out_dir / "keep.json"))
    assert_error(done)
    assert (out_dir / "keep.json").read_text(encoding="utf-8") == "previous content"
    assert listing(out_dir) == ["keep.json"]
    assert_error(export(bad))
    wrong_row = tmp_path / "wrong.csv"
    wrong_row.write_text(HEADER + "A-1,Amy,2024-13-40,open,MUG-001,1,1.00\n", encoding="utf-8")
    done = export(wrong_row, "--out", str(out_dir / "keep.json"))
    assert_error(done)
    assert (out_dir / "keep.json").read_text(encoding="utf-8") == "previous content"
    assert listing(out_dir) == ["keep.json"]
    # an output directory that does not exist
    missing = out_dir / "no" / "such" / "orders.json"
    done = export(orders, "--out", str(missing))
    assert_error(done, str(missing))
    assert listing(out_dir) == ["keep.json"]
    # an output path that is a directory
    target = out_dir / "a-directory"
    target.mkdir()
    (target / "inner.txt").write_text("inside", encoding="utf-8")
    before = listing(out_dir)
    done = export(orders, "--out", str(target))
    assert_error(done, str(target))
    assert listing(out_dir) == before
    assert target.is_dir() and listing(target) == ["inner.txt"]
    assert (target / "inner.txt").read_text(encoding="utf-8") == "inside"
    assert (out_dir / "keep.json").read_text(encoding="utf-8") == "previous content"
    # the same through the current directory
    done = run("export", "--orders", str(orders), "--out", "a-directory", cwd=out_dir)
    assert_error(done, "a-directory")
    assert listing(out_dir) == before


def test_other_commands_are_unchanged() -> None:
    done = run("sales", "--orders", "data/orders.csv")
    assert done.returncode == 0
    assert done.stdout.splitlines()[:2] == ["5 orders, 1 cancelled", "Revenue: $149.65"]
    done = run("items", "--file", "data/items.csv", "--tag", "tea")
    assert [ln.split()[0] for ln in done.stdout.splitlines()[2:]] == ["TEA-010", "TEA-011"]
    assert run("--version").stdout == "stockroom 0.4.0\n"
    assert run("frobnicate").returncode == 2
