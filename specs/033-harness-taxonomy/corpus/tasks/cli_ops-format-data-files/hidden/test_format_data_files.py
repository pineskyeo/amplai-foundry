from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "fmt_data.py"

ITEMS_MESSY = (
    "\ufeffsku, name ,price,tags,reorder_level\r\n"
    "MUG-001, Blue Mug ,$12.5, Kitchen ; GIFT ,5\r\n"
    "\r\n"
    "TEA-010,Green Tea,8.95,,0\r\n"
    "   \r\n"
    "LMP-030,Desk Lamp,999.99,office;home;,  1\r\n"
)
ITEMS_CANONICAL = (
    "sku,name,price,tags,reorder_level\n"
    "MUG-001,Blue Mug,12.50,kitchen;gift,5\n"
    "TEA-010,Green Tea,8.95,,0\n"
    "LMP-030,Desk Lamp,999.99,office;home,1\n"
)
STOCK_MESSY = "sku,on_hand,reserved\r\nMUG-001, 14 ,2\r\n\r\nTEA-010,030,5"
STOCK_CANONICAL = "sku,on_hand,reserved\nMUG-001,14,2\nTEA-010,30,5\n"
ORDERS_MESSY = (
    "order_id,customer,placed,status,sku,quantity,unit_price\r\n"
    'B-2,"Kim, Hana",2024-02-01,paid,MUG-001,1,12.5\r\n'
    "A-1,Omar Diaz, 2024-01-05 ,open,TEA-010, 2 ,$8.95\r\n"
    "\r\n"
    'B-2,"Kim, Hana",2024-02-01,paid,LMP-030,3,"1,234.5"\r\n'
)
ORDERS_CANONICAL = (
    "order_id,customer,placed,status,sku,quantity,unit_price\n"
    'B-2,"Kim, Hana",2024-02-01,paid,MUG-001,1,12.50\n'
    'B-2,"Kim, Hana",2024-02-01,paid,LMP-030,3,"1,234.50"\n'
    "A-1,Omar Diaz,2024-01-05,open,TEA-010,2,8.95\n"
)


def fmt(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def put(folder: Path, name: str, text: str | bytes) -> Path:
    path = folder / name
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


def snapshot(folder: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        p.name: (p.read_bytes() if p.is_file() else b"<dir>", p.stat().st_ino, p.stat().st_mtime_ns)
        for p in sorted(folder.iterdir())
    }


def summary(done: subprocess.CompletedProcess[str]) -> str:
    return done.stdout.splitlines()[-1]


def test_each_kind_is_rewritten_in_canonical_form(tmp_path: Path) -> None:
    put(tmp_path, "items.csv", ITEMS_MESSY)
    put(tmp_path, "stock.csv", STOCK_MESSY)
    put(tmp_path, "orders.csv", ORDERS_MESSY)
    done = fmt("orders.csv", "items.csv", "stock.csv", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout == (
        "reformatted orders.csv\n"
        "reformatted items.csv\n"
        "reformatted stock.csv\n"
        "3 reformatted, 0 unchanged, 0 failed\n"
    )
    assert (tmp_path / "items.csv").read_bytes() == ITEMS_CANONICAL.encode("utf-8")
    assert (tmp_path / "stock.csv").read_bytes() == STOCK_CANONICAL.encode("utf-8")
    assert (tmp_path / "orders.csv").read_bytes() == ORDERS_CANONICAL.encode("utf-8")
    # a file that differs from its canonical form only by a BOM or line ends is rewritten
    put(tmp_path, "bom.csv", "\ufeff" + STOCK_CANONICAL)
    put(tmp_path, "crlf.csv", STOCK_CANONICAL.replace("\n", "\r\n"))
    put(tmp_path, "noeol.csv", STOCK_CANONICAL.rstrip("\n"))
    done = fmt("bom.csv", "crlf.csv", "noeol.csv", cwd=tmp_path)
    assert done.returncode == 0 and summary(done) == "3 reformatted, 0 unchanged, 0 failed"
    for name in ("bom.csv", "crlf.csv", "noeol.csv"):
        assert (tmp_path / name).read_bytes() == STOCK_CANONICAL.encode("utf-8"), name
    # absolute paths are printed as given
    path = put(tmp_path, "abs.csv", STOCK_MESSY)
    done = fmt(str(path), cwd=ROOT)
    assert done.stdout == f"reformatted {path}\n1 reformatted, 0 unchanged, 0 failed\n"


def test_canonical_files_are_not_written_and_a_rerun_changes_nothing(tmp_path: Path) -> None:
    put(tmp_path, "items.csv", ITEMS_MESSY)
    put(tmp_path, "stock.csv", STOCK_CANONICAL)
    put(tmp_path, "orders.csv", ORDERS_MESSY)
    keep = snapshot(tmp_path)["stock.csv"]
    done = fmt("items.csv", "stock.csv", "orders.csv", cwd=tmp_path)
    assert done.stdout == (
        "reformatted items.csv\nreformatted orders.csv\n2 reformatted, 1 unchanged, 0 failed\n"
    )
    assert snapshot(tmp_path)["stock.csv"] == keep
    before = snapshot(tmp_path)
    done = fmt("items.csv", "stock.csv", "orders.csv", cwd=tmp_path)
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout == "0 reformatted, 3 unchanged, 0 failed\n"
    assert snapshot(tmp_path) == before
    # the repository's own sample data is canonical
    data = [str(ROOT / "data" / n) for n in ("items.csv", "stock.csv", "orders.csv")]
    done = fmt("--check", *data, cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == "0 would be reformatted, 3 unchanged, 0 failed\n"


def test_check_mode_reports_and_writes_nothing(tmp_path: Path) -> None:
    put(tmp_path, "items.csv", ITEMS_MESSY)
    put(tmp_path, "stock.csv", STOCK_CANONICAL)
    put(tmp_path, "orders.csv", ORDERS_MESSY)
    before = snapshot(tmp_path)
    done = fmt("--check", "items.csv", "stock.csv", "orders.csv", cwd=tmp_path)
    assert done.returncode == 1
    assert done.stderr == ""
    assert done.stdout == (
        "would reformat items.csv\n"
        "would reformat orders.csv\n"
        "2 would be reformatted, 1 unchanged, 0 failed\n"
    )
    assert snapshot(tmp_path) == before
    done = fmt("stock.csv", "--check", cwd=tmp_path)
    assert done.returncode == 0
    assert done.stdout == "0 would be reformatted, 1 unchanged, 0 failed\n"


def test_failures_are_reported_per_file_and_the_rest_is_processed(tmp_path: Path) -> None:
    put(tmp_path, "good.csv", STOCK_MESSY)
    (tmp_path / "folder").mkdir()
    put(tmp_path, "latin.csv", b"sku,on_hand,reserved\nMUG-001,1,0 \xff\n")
    put(tmp_path, "unknown.csv", "id,name\n1,x\n")
    put(tmp_path, "badrow.csv", "sku,on_hand,reserved\nMUG-001,x,0\n")
    put(tmp_path, "empty.csv", "")
    put(tmp_path, "header.csv", "sku,price\nMUG-001,1.00\n")
    names = [
        "nope.csv",
        "good.csv",
        "folder",
        "latin.csv",
        "unknown.csv",
        "badrow.csv",
        "empty.csv",
        "header.csv",
    ]
    failing = [n for n in names if n != "good.csv"]
    keep = {n: v for n, v in snapshot(tmp_path).items() if n != "good.csv"}
    done = fmt(*names, cwd=tmp_path)
    assert done.returncode == 3
    assert done.stdout == "reformatted good.csv\n1 reformatted, 0 unchanged, 7 failed\n"
    lines = done.stderr.splitlines()
    assert len(lines) == len(failing), done.stderr
    for line, name in zip(lines, failing, strict=True):
        assert line.startswith(f"fmt_data: error: {name}: "), (line, name)
    assert "Traceback" not in done.stderr
    assert (tmp_path / "good.csv").read_bytes() == STOCK_CANONICAL.encode("utf-8")
    assert {n: v for n, v in snapshot(tmp_path).items() if n != "good.csv"} == keep
    assert not (tmp_path / "nope.csv").exists()


def test_exit_code_matrix(tmp_path: Path) -> None:
    def case(name: str, *, change: bool, fail: bool) -> list[str]:
        folder = tmp_path / name
        folder.mkdir()
        put(folder, "ok.csv", STOCK_CANONICAL)
        args = ["ok.csv"]
        if change:
            put(folder, "messy.csv", STOCK_MESSY)
            args.append("messy.csv")
        if fail:
            args.append("missing.csv")
        return args

    for check in (False, True):
        for change in (False, True):
            for fail in (False, True):
                name = f"c{int(check)}{int(change)}{int(fail)}"
                args = case(name, change=change, fail=fail)
                done = fmt(*(["--check"] if check else []), *args, cwd=tmp_path / name)
                want = 3 if fail else 1 if (check and change) else 0
                assert done.returncode == want, (name, done.returncode, done.stdout, done.stderr)
                verb = "would be reformatted" if check else "reformatted"
                assert summary(done) == (
                    f"{int(change)} {verb}, 1 unchanged, {int(fail)} failed"
                ), name
                assert len(done.stderr.splitlines()) == int(fail), name
                messy = tmp_path / name / "messy.csv"
                if change:
                    want_text = STOCK_MESSY if check else STOCK_CANONICAL
                    assert messy.read_bytes() == want_text.encode("utf-8"), name


def test_each_file_is_processed_once(tmp_path: Path) -> None:
    put(tmp_path, "a.csv", STOCK_MESSY)
    put(tmp_path, "b.csv", ITEMS_MESSY)
    (tmp_path / "sub").mkdir()
    (tmp_path / "link.csv").symlink_to(tmp_path / "a.csv")
    args = ["a.csv", "./a.csv", str(tmp_path / "a.csv"), "sub/../a.csv", "b.csv", "link.csv"]
    done = fmt(*args, "a.csv", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert (
        done.stdout
        == "reformatted a.csv\nreformatted b.csv\n2 reformatted, 0 unchanged, 0 failed\n"
    )
    assert (tmp_path / "link.csv").is_symlink()
    done = fmt("nope.csv", "./nope.csv", "--check", cwd=tmp_path)
    assert done.returncode == 3
    assert done.stdout == "0 would be reformatted, 0 unchanged, 1 failed\n"
    assert len(done.stderr.splitlines()) == 1
    assert done.stderr.startswith("fmt_data: error: nope.csv: ")


def test_usage_and_names_that_start_with_a_dash(tmp_path: Path) -> None:
    put(tmp_path, "-x.csv", STOCK_MESSY)
    done = fmt("--", "-x.csv", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "reformatted -x.csv\n1 reformatted, 0 unchanged, 0 failed\n"
    assert (tmp_path / "-x.csv").read_bytes() == STOCK_CANONICAL.encode("utf-8")
    for args in ([], ["--check"], ["--frobnicate", "-x.csv"], ["-x.csv"]):
        done = fmt(*args, cwd=tmp_path)
        assert done.returncode == 2, (args, done.returncode, done.stderr)
        assert done.stdout == ""
    assert sorted(p.name for p in tmp_path.iterdir()) == ["-x.csv"]


def _dataset(rng: random.Random) -> dict[str, tuple[list[list[str]], str]]:
    names = ["Blue Mug", "Café Ünal", "Desk Lamp", "Green Tea", "Pen"]
    items, stock, orders = [], [], []
    for i in range(rng.randrange(1, 5)):
        sku = f"{rng.choice(['MUG', 'TEA', 'AB', 'LAMP'])}-{100 + i:03d}"
        cents = rng.randrange(0, 99999)
        tags = rng.sample(["home", "gift", "office", "tea"], k=rng.randrange(0, 3))
        items.append(
            [
                sku,
                rng.choice(names),
                f"{cents // 100}.{cents % 100:02d}",
                ";".join(tags),
                str(rng.randrange(0, 30)),
            ]
        )
        on_hand = rng.randrange(0, 50)
        stock.append([sku, str(on_hand), str(rng.randrange(0, on_hand + 1))])
    for n in range(rng.randrange(1, 4)):
        head = [
            f"O-{n}",
            rng.choice(["Hana Kim", "Omar Diaz", "Zoë"]),
            f"2024-{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d}",
            rng.choice(["open", "paid", "shipped", "cancelled"]),
        ]
        for j in range(rng.randrange(1, 3)):
            cents = rng.randrange(1, 99999)
            price = f"{cents // 100}.{cents % 100:02d}"
            orders.append([*head, f"SKU-{j + 100}", str(rng.randrange(1, 9)), price])
    return {
        "items.csv": (items, "sku,name,price,tags,reorder_level"),
        "stock.csv": (stock, "sku,on_hand,reserved"),
        "orders.csv": (orders, "order_id,customer,placed,status,sku,quantity,unit_price"),
    }


def _messy(rng: random.Random, name: str, rows: list[list[str]], header: str) -> str:
    def pad(cell: str) -> str:
        return " " * rng.randrange(0, 2) + cell + " " * rng.randrange(0, 2)

    lines = [header if name == "orders.csv" else ",".join(pad(c) for c in header.split(","))]
    for row in rows:
        cells = [pad(c) for c in row]
        if name == "items.csv":
            if row[2].endswith("0") and rng.random() < 0.5:
                cells[2] = row[2][:-1]
            if rng.random() < 0.3:
                cells[2] = "$" + cells[2].strip()
            tags = [t.upper() if rng.random() < 0.5 else t for t in row[3].split(";")]
            cells[3] = " ; ".join(tags) + (";" if rng.random() < 0.3 else "")
        if name == "stock.csv" and rng.random() < 0.3:
            cells[1] = "0" + row[1]
        lines.append(",".join(cells))
        if rng.random() < 0.3:
            lines.append("" if name == "orders.csv" else " " * rng.randrange(0, 3))
    eol = "\r\n" if rng.random() < 0.5 else "\n"
    text = eol.join(lines) + (eol if rng.random() < 0.7 else "")
    return ("\ufeff" if rng.random() < 0.3 else "") + text


def test_seeded_messy_files_become_canonical(tmp_path: Path) -> None:
    rng = random.Random(4711)
    for trial in range(20):
        folder = tmp_path / f"t{trial}"
        folder.mkdir()
        wanted = {}
        for name, (rows, header) in _dataset(rng).items():
            put(folder, name, _messy(rng, name, rows, header))
            if name == "items.csv":
                rows = [[*r[:3], ";".join(t for t in r[3].split(";") if t), *r[4:]] for r in rows]
            wanted[name] = "".join(",".join(r) + "\n" for r in [header.split(","), *rows])
        done = fmt("items.csv", "stock.csv", "orders.csv", cwd=folder)
        assert done.returncode == 0, (trial, done.stderr)
        for name, text in wanted.items():
            assert (folder / name).read_bytes() == text.encode("utf-8"), (trial, name)
        done = fmt("--check", "items.csv", "stock.csv", "orders.csv", cwd=folder)
        assert done.returncode == 0, (trial, done.stdout)
        assert done.stdout == "0 would be reformatted, 3 unchanged, 0 failed\n"
        assert sorted(p.name for p in folder.iterdir()) == ["items.csv", "orders.csv", "stock.csv"]
