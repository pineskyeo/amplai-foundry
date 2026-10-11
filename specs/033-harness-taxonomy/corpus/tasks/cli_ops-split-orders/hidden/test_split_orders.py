from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "split_orders.py"
HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"
ORDERS = HEADER + (
    "A-1,Hana Kim,2024-01-08,shipped,MUG-001,2,12.50\n"
    "B-1,Omar Diaz,2024-03-02,open,TEA-010,1,8.95\n"
    "A-2,Lena Berg,2024-01-31,cancelled,PEN-100,1,34.00\n"
    "A-1,Hana Kim,2024-01-08,shipped,NB-200,1,6.25\n"
    "Z-1,Ada Moss,2023-12-24,paid,LMP-030,1,49.99\n"
)
MONTHS = {
    "orders-2023-12.csv": HEADER + "Z-1,Ada Moss,2023-12-24,paid,LMP-030,1,49.99\n",
    "orders-2024-01.csv": HEADER
    + "A-1,Hana Kim,2024-01-08,shipped,MUG-001,2,12.50\n"
    + "A-1,Hana Kim,2024-01-08,shipped,NB-200,1,6.25\n"
    + "A-2,Lena Berg,2024-01-31,cancelled,PEN-100,1,34.00\n",
    "orders-2024-03.csv": HEADER + "B-1,Omar Diaz,2024-03-02,open,TEA-010,1,8.95\n",
}


def split(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def orders_file(tmp_path: Path, text: str = ORDERS, name: str = "orders.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def state(folder: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        p.name: (p.read_bytes() if p.is_file() else b"<dir>", p.stat().st_ino, p.stat().st_mtime_ns)
        for p in sorted(folder.iterdir())
    }


def contents(folder: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.iterdir()) if p.is_file()}


def assert_error(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("split_orders: error: "), done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def test_one_file_per_month(tmp_path: Path) -> None:
    orders = orders_file(tmp_path)
    out = tmp_path / "by" / "month"
    done = split("--orders", str(orders), "--out", str(out))
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout == (
        "wrote orders-2023-12.csv\n"
        "wrote orders-2024-01.csv\n"
        "wrote orders-2024-03.csv\n"
        "3 written, 0 unchanged, 0 removed\n"
    )
    assert contents(out) == MONTHS
    # relative paths are relative to the current directory
    done = split("--orders", "orders.csv", "--out", "rel", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert contents(tmp_path / "rel") == MONTHS
    # an orders file without orders writes nothing
    empty = orders_file(tmp_path, HEADER, "empty.csv")
    done = split("--orders", str(empty), "--out", str(tmp_path / "none"))
    assert done.returncode == 0 and done.stdout == "0 written, 0 unchanged, 0 removed\n"
    assert (tmp_path / "none").is_dir() and contents(tmp_path / "none") == {}


def test_a_rerun_writes_only_what_changed(tmp_path: Path) -> None:
    orders = orders_file(tmp_path)
    out = tmp_path / "out"
    assert split("--orders", str(orders), "--out", str(out)).returncode == 0
    before = state(out)
    done = split("--orders", str(orders), "--out", str(out))
    assert done.returncode == 0
    assert done.stdout == (
        "unchanged orders-2023-12.csv\n"
        "unchanged orders-2024-01.csv\n"
        "unchanged orders-2024-03.csv\n"
        "0 written, 3 unchanged, 0 removed\n"
    )
    assert state(out) == before
    # one order changes, and one month file was edited by hand
    orders_file(tmp_path, ORDERS.replace("TEA-010,1,8.95", "TEA-010,3,8.95"))
    (out / "orders-2023-12.csv").write_text(MONTHS["orders-2023-12.csv"] + "\n", encoding="utf-8")
    keep = state(out)["orders-2024-01.csv"]
    done = split("--orders", str(orders), "--out", str(out))
    assert done.stdout == (
        "wrote orders-2023-12.csv\n"
        "unchanged orders-2024-01.csv\n"
        "wrote orders-2024-03.csv\n"
        "2 written, 1 unchanged, 0 removed\n"
    )
    assert state(out)["orders-2024-01.csv"] == keep
    assert (
        contents(out)["orders-2024-03.csv"]
        == HEADER + "B-1,Omar Diaz,2024-03-02,open,TEA-010,3,8.95\n"
    )
    assert contents(out)["orders-2023-12.csv"] == MONTHS["orders-2023-12.csv"]
    assert sorted(p.name for p in out.iterdir()) == sorted(MONTHS)


def _populate(out: Path) -> None:
    out.mkdir()
    for name in (
        "orders-2022-05.csv",
        "orders-2024-1.csv",
        "orders-2024-01.csv.bak",
        "Orders-2022-06.csv",
        "orders-2022-07.CSV",
        "orders-" + "".join(map(chr, (0xFF12, 0xFF10, 0xFF12, 0xFF12))) + "-08.csv",
        "notes.txt",
    ):
        (out / name).write_text("keep " + name, encoding="utf-8")
    (out / "orders-2020-01.csv").mkdir()


def test_prune_removes_only_stale_month_files(tmp_path: Path) -> None:
    orders = orders_file(tmp_path)
    out = tmp_path / "out"
    _populate(out)
    done = split("--orders", str(orders), "--out", str(out))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == "3 written, 0 unchanged, 0 removed"
    assert (out / "orders-2022-05.csv").exists()
    done = split("--orders", str(orders), "--out", str(out), "--prune")
    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        "removed orders-2022-05.csv\n"
        "unchanged orders-2023-12.csv\n"
        "unchanged orders-2024-01.csv\n"
        "unchanged orders-2024-03.csv\n"
        "0 written, 3 unchanged, 1 removed\n"
    )
    left = sorted(p.name for p in out.iterdir())
    assert "orders-2022-05.csv" not in left
    for name in (
        "orders-2024-1.csv",
        "orders-2024-01.csv.bak",
        "Orders-2022-06.csv",
        "orders-2022-07.CSV",
        "notes.txt",
        "orders-2020-01.csv",
    ):
        assert name in left, name
    assert (out / "orders-2020-01.csv").is_dir()
    assert len(left) == 3 + 7
    # an orders file without orders prunes every month file
    empty = orders_file(tmp_path, HEADER, "empty.csv")
    done = split("--prune", "--orders", str(empty), "--out", str(out))
    assert done.stdout == (
        "removed orders-2023-12.csv\n"
        "removed orders-2024-01.csv\n"
        "removed orders-2024-03.csv\n"
        "0 written, 0 unchanged, 3 removed\n"
    )
    assert len(list(out.iterdir())) == 7


def test_dry_run_and_prune_matrix(tmp_path: Path) -> None:
    orders = orders_file(tmp_path)
    n = 0
    for setup in ("missing", "current", "changed"):
        for prune in (False, True):
            for dry in (False, True):
                n += 1
                out = tmp_path / f"case{n}"
                if setup != "missing":
                    out.mkdir()
                    for name, text in MONTHS.items():
                        (out / name).write_text(text, encoding="utf-8")
                    (out / "orders-2021-02.csv").write_text("old", encoding="utf-8")
                    if setup == "changed":
                        (out / "orders-2024-03.csv").write_text("edited", encoding="utf-8")
                before = state(out) if out.exists() else None
                args = ["--orders", str(orders), "--out", str(out)]
                args += ["--prune"] if prune else []
                args += ["--dry-run"] if dry else []
                done = split(*args)
                assert done.returncode == 0, (setup, prune, dry, done.stderr)
                written = {
                    "missing": list(MONTHS),
                    "current": [],
                    "changed": ["orders-2024-03.csv"],
                }[setup]
                removed = ["orders-2021-02.csv"] if prune and setup != "missing" else []
                lines = []
                for name in sorted([*MONTHS, *removed]):
                    if name in removed:
                        lines.append(f"{'would remove' if dry else 'removed'} {name}")
                    elif name in written:
                        lines.append(f"{'would write' if dry else 'wrote'} {name}")
                    else:
                        lines.append(f"unchanged {name}")
                w, u, r = len(written), len(MONTHS) - len(written), len(removed)
                if dry:
                    lines.append(f"{w} would be written, {u} unchanged, {r} would be removed")
                else:
                    lines.append(f"{w} written, {u} unchanged, {r} removed")
                assert done.stdout.splitlines() == lines, (setup, prune, dry)
                if dry:
                    if before is None:
                        assert not out.exists(), (setup, prune, dry)
                    else:
                        assert state(out) == before, (setup, prune, dry)
                else:
                    files = contents(out)
                    for name, text in MONTHS.items():
                        assert files[name] == text, (setup, prune, dry, name)
                    assert ("orders-2021-02.csv" in files) == (setup != "missing" and not prune)


def test_split_failures_change_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    bad = orders_file(tmp_path, "id,who\n1,2\n", "bad.csv")
    assert_error(split("--orders", str(bad), "--out", str(out)), "bad.csv")
    assert not out.exists()
    missing = tmp_path / "missing.csv"
    assert_error(split("--orders", str(missing), "--out", str(out)), str(missing))
    assert_error(split("--orders", str(tmp_path), "--out", str(out)), str(tmp_path))
    assert not out.exists()
    orders = orders_file(tmp_path)
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    assert_error(split("--orders", str(orders), "--out", str(plain)), str(plain))
    assert plain.read_text(encoding="utf-8") == "x"
    # the input file is never replaced or removed by its own run
    out.mkdir()
    inside = out / "orders-2024-01.csv"
    shutil.copyfile(orders, inside)
    before = state(out)
    assert_error(split("--orders", str(inside), "--out", str(out)), "input")
    done = split("--orders", "out/orders-2024-01.csv", "--out", "./out", cwd=tmp_path)
    assert_error(done, "input")
    assert state(out) == before
    inside.unlink()
    stale = out / "orders-2022-05.csv"
    shutil.copyfile(orders, stale)
    before = state(out)
    assert_error(split("--orders", str(stale), "--out", str(out), "--prune"), "input")
    assert_error(split("--orders", str(stale), "--out", str(out), "--prune", "--dry-run"), "input")
    assert state(out) == before
    done = split("--orders", str(stale), "--out", str(out))
    assert done.returncode == 0, done.stderr
    assert stale.read_text(encoding="utf-8") == ORDERS


def test_by_month_groups_orders() -> None:
    sys.path.insert(0, str(ROOT))
    from stockroom.csvio import read_orders
    from stockroom.orders import OrderBook

    book = OrderBook(read_orders(ORDERS))
    months = book.by_month()
    assert list(months) == ["2023-12", "2024-01", "2024-03"]
    assert [o.order_id for o in months["2024-01"]] == ["A-1", "A-2"]
    assert months["2024-01"][1].status == "cancelled"
    assert [o.order_id for o in months["2023-12"]] == ["Z-1"]
    assert OrderBook().by_month() == {}
    text = HEADER + "".join(
        f"O-{i},C,2024-{m:02d}-{d:02d},paid,MUG-001,1,1.00\n"
        for i, (m, d) in enumerate([(5, 9), (2, 1), (5, 1), (11, 30), (2, 28)])
    )
    months = OrderBook(read_orders(text)).by_month()
    assert list(months) == ["2024-02", "2024-05", "2024-11"]
    assert [o.order_id for o in months["2024-05"]] == ["O-0", "O-2"]
    assert [o.order_id for o in months["2024-02"]] == ["O-1", "O-4"]


def test_split_usage_errors_write_nothing(tmp_path: Path) -> None:
    orders = orders_file(tmp_path)
    for args in ([], ["--orders", str(orders)], ["--out", str(tmp_path / "o")], ["--frob"]):
        done = split(*args)
        assert done.returncode == 2, args
        assert done.stdout == ""
    assert sorted(p.name for p in tmp_path.iterdir()) == ["orders.csv"]
