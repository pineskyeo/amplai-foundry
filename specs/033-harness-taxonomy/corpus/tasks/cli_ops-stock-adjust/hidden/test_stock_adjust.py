from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ITEMS = str(ROOT / "data" / "items.csv")
LEVELS = [
    ("MUG-001", 14, 2),
    ("MUG-002", 4, 0),
    ("TEA-010", 30, 5),
    ("TEA-011", 9, 0),
    ("PEN-100", 3, 2),
    ("NB-200", 40, 4),
    ("LMP-030", 2, 0),
    ("CND-007", 12, 1),
]


def canonical(levels: list[tuple[str, int, int]]) -> str:
    return "sku,on_hand,reserved\n" + "".join(f"{s},{o},{r}\n" for s, o, r in levels)


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


def setup(tmp_path: Path, text: str | None = None, name: str = "stock.csv") -> Path:
    folder = tmp_path / "data"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes((canonical(LEVELS) if text is None else text).encode("utf-8"))
    return path


def adjust(stock: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run("adjust", "--stock", str(stock), *args)


def ok(done: subprocess.CompletedProcess[str]) -> list[str]:
    assert done.returncode == 0, (done.returncode, done.stderr)
    assert done.stderr == ""
    return done.stdout.splitlines()


def snapshot(folder: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in sorted(folder.iterdir())
    }


def assert_error(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("stockroom: error: "), done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def with_levels(changes: dict[str, int], extra: list[tuple[str, int, int]] = ()) -> str:
    rows = [(s, changes.get(s, o), r) for s, o, r in LEVELS]
    return canonical(rows + list(extra))


def test_changes_update_the_file_and_report_in_file_order(tmp_path: Path) -> None:
    stock = setup(tmp_path)
    done = adjust(stock, "PEN-100=10", "MUG-001+6", "TEA-011-4")
    assert ok(done) == ["MUG-001: 14 -> 20", "TEA-011: 9 -> 5", "PEN-100: 3 -> 10"]
    assert stock.read_text(encoding="utf-8") == with_levels(
        {"MUG-001": 20, "TEA-011": 5, "PEN-100": 10}
    )
    stock = setup(tmp_path)
    done = adjust(stock, "XYZ-123+5", "MUG-002=0", "AB-0001=0", "XYZ-123+1")
    assert ok(done) == ["MUG-002: 4 -> 0", "XYZ-123: new -> 6", "AB-0001: new -> 0"]
    assert stock.read_text(encoding="utf-8") == with_levels(
        {"MUG-002": 0}, [("XYZ-123", 6, 0), ("AB-0001", 0, 0)]
    )
    # the stock command reads the rewritten file
    done = run("stock", "--items", ITEMS, "--stock", str(stock))
    assert done.returncode == 0, done.stderr
    # relative paths are relative to the current directory
    stock = setup(tmp_path)
    done = run("adjust", "--stock", "data/stock.csv", "LMP-030+1", cwd=tmp_path)
    assert ok(done) == ["LMP-030: 2 -> 3"]
    assert stock.read_text(encoding="utf-8") == with_levels({"LMP-030": 3})


def test_changes_apply_in_order_and_accumulate(tmp_path: Path) -> None:
    cases = [
        (["MUG-001+1", "MUG-001+1"], ["MUG-001: 14 -> 16"], {"MUG-001": 16}),
        (["MUG-001=3", "MUG-001+5"], ["MUG-001: 14 -> 8"], {"MUG-001": 8}),
        (["MUG-001+5", "MUG-001=3"], ["MUG-001: 14 -> 3"], {"MUG-001": 3}),
        (["PEN-100-1", "PEN-100+5"], ["PEN-100: 3 -> 7"], {"PEN-100": 7}),
        (["TEA-010-25", "TEA-010+1"], ["TEA-010: 30 -> 6"], {"TEA-010": 6}),
        (["NB-200=8", "NB-200-0004", "NB-200+1"], ["NB-200: 40 -> 5"], {"NB-200": 5}),
    ]
    for changes, lines, values in cases:
        stock = setup(tmp_path)
        assert ok(adjust(stock, *changes)) == lines, changes
        assert stock.read_text(encoding="utf-8") == with_levels(values), changes
    # a SKU added by an earlier change exists for the later ones
    stock = setup(tmp_path)
    assert ok(adjust(stock, "NEW-100+2", "NEW-100-2")) == ["NEW-100: new -> 0"]
    assert stock.read_text(encoding="utf-8") == with_levels({}, [("NEW-100", 0, 0)])
    # every single change must keep on hand at or above reserved, even if the net is fine
    stock = setup(tmp_path)
    before = stock.read_bytes()
    assert_error(adjust(stock, "PEN-100-2", "PEN-100+2"), "PEN-100")
    assert_error(adjust(stock, "MUG-001=1", "MUG-001+10"), "MUG-001")
    assert_error(adjust(stock, "NEW-100-1", "NEW-100+2"), "NEW-100")
    assert stock.read_bytes() == before


def test_no_change_leaves_the_file_untouched(tmp_path: Path) -> None:
    messy = "sku,on_hand,reserved\r\n MUG-001 , 14 ,2\r\n\r\nMUG-002,4,0\r\n"
    stock = setup(tmp_path, messy)
    before = snapshot(stock.parent)
    for changes in (["MUG-001=14"], ["MUG-002-4", "MUG-002+4"], ["MUG-001+3", "MUG-001-3"]):
        assert ok(adjust(stock, *changes)) == ["no changes"], changes
        assert ok(adjust(stock, *changes, "--backup")) == ["no changes"], changes
        assert ok(adjust(stock, "--dry-run", *changes)) == ["no changes"], changes
        assert snapshot(stock.parent) == before, changes
    # a real change rewrites the whole file in the canonical form
    assert ok(adjust(stock, "MUG-002+1")) == ["MUG-002: 4 -> 5"]
    assert stock.read_bytes() == b"sku,on_hand,reserved\nMUG-001,14,2\nMUG-002,5,0\n"


def test_malformed_changes_are_usage_errors(tmp_path: Path) -> None:
    stock = setup(tmp_path)
    before = snapshot(stock.parent)
    bad = [
        "MUG-001",
        "MUG-001+0",
        "MUG-001-0",
        "MUG-001-00",
        "mug-001+1",
        "MUG-001+1.5",
        "MUG-001+" + chr(0x0663),
        "MUG-001=" + chr(0xFF15),
        "MUG-001=-1",
        "MUG-001+ 1",
        "MUG-1+1",
        "M-001+1",
        "MUG-001*2",
        "MUG-001++1",
        "MUG-001+1x",
        " MUG-001+1",
    ]
    for text in bad:
        done = adjust(stock, "MUG-002+1", text)
        assert done.returncode == 2, (text, done.returncode, done.stderr)
        assert done.stdout == "" and (text in done.stderr or not text.isascii()), text
    done = adjust(stock)
    assert done.returncode == 2 and done.stdout == ""
    assert run("adjust", "MUG-001+1").returncode == 2
    assert snapshot(stock.parent) == before


def test_failed_adjustments_change_nothing(tmp_path: Path) -> None:
    stock = setup(tmp_path)
    (stock.parent / "stock.csv.bak").write_text("old backup", encoding="utf-8")
    before = snapshot(stock.parent)
    for flags in ([], ["--backup"], ["--dry-run"]):
        assert_error(adjust(stock, *flags, "MUG-001+1", "PEN-100-2"), "PEN-100")
        assert_error(adjust(stock, *flags, "XYZ-999-1"), "XYZ-999")
        # the first failing change is the one reported
        assert_error(adjust(stock, *flags, "MUG-001+1", "XYZ-999-1", "PEN-100-5"), "XYZ-999")
        done = adjust(stock, *flags, "--items", ITEMS, "MUG-001+1", "ZZZ-001+1", "QQQ-002+1")
        assert_error(done, "ZZZ-001")
        assert "QQQ-002" not in done.stderr
        assert snapshot(stock.parent) == before, flags
    # the catalogue check passes for known SKUs
    assert ok(adjust(stock, "--items", ITEMS, "CND-007+1")) == ["CND-007: 12 -> 13"]
    for text, needle in [
        ("sku,count\nMUG-001,1\n", ""),
        ("sku,on_hand,reserved\nMUG-001,1,2\n", "MUG-001"),
        ("sku,on_hand,reserved\nMUG-001,1,0\nTEA-010,2,0\nMUG-001,3,0\n", "MUG-001"),
        ("sku,on_hand,reserved\nMUG-001,x,0\n", ""),
    ]:
        bad = setup(tmp_path, text, "bad.csv")
        snap = snapshot(bad.parent)
        assert_error(adjust(bad, "--backup", "TEA-010+1"), needle)
        assert snapshot(bad.parent) == snap
    missing = tmp_path / "data" / "missing.csv"
    assert_error(adjust(missing, "MUG-001+1"), str(missing))
    assert not missing.exists()


def test_rewrite_is_atomic_and_backup_keeps_the_old_bytes(tmp_path: Path) -> None:
    messy = "sku,on_hand,reserved\r\n MUG-001 , 14 ,2\r\n\r\nMUG-002,4,0\r\n"
    stock = setup(tmp_path, messy)
    old_ino = stock.stat().st_ino
    assert ok(adjust(stock, "--backup", "MUG-001-4")) == ["MUG-001: 14 -> 10"]
    assert stock.read_bytes() == b"sku,on_hand,reserved\nMUG-001,10,2\nMUG-002,4,0\n"
    assert stock.stat().st_ino != old_ino
    assert (stock.parent / "stock.csv.bak").read_bytes() == messy.encode("utf-8")
    assert sorted(p.name for p in stock.parent.iterdir()) == ["stock.csv", "stock.csv.bak"]
    # a later backup replaces the earlier one
    assert ok(adjust(stock, "MUG-002+1", "--backup")) == ["MUG-002: 4 -> 5"]
    assert (stock.parent / "stock.csv.bak").read_bytes() == (
        b"sku,on_hand,reserved\nMUG-001,10,2\nMUG-002,4,0\n"
    )
    # without --backup no backup is written or changed
    assert ok(adjust(stock, "MUG-002+1")) == ["MUG-002: 5 -> 6"]
    assert (stock.parent / "stock.csv.bak").read_bytes() == (
        b"sku,on_hand,reserved\nMUG-001,10,2\nMUG-002,4,0\n"
    )
    assert sorted(p.name for p in stock.parent.iterdir()) == ["stock.csv", "stock.csv.bak"]
    # the old file is replaced as a whole, also when the new text is shorter
    stock = setup(tmp_path, canonical([("MUG-001", 123456789, 0)]), "big.csv")
    assert ok(adjust(stock, "MUG-001=1")) == ["MUG-001: 123456789 -> 1"]
    assert stock.read_text(encoding="utf-8") == canonical([("MUG-001", 1, 0)])
    # the csvio counterpart of read_stock writes the same format
    sys.path.insert(0, str(ROOT))
    from stockroom.csvio import read_stock, write_stock

    assert write_stock(LEVELS) == canonical(LEVELS)
    assert write_stock([]) == "sku,on_hand,reserved\n"
    assert read_stock(write_stock(LEVELS)) == LEVELS


def test_dry_run_prints_the_changes_and_writes_nothing(tmp_path: Path) -> None:
    stock = setup(tmp_path)
    before = snapshot(stock.parent)
    done = adjust(stock, "--dry-run", "--backup", "TEA-010-5", "ZZ-999=3")
    assert ok(done) == ["TEA-010: 30 -> 25", "ZZ-999: new -> 3", "dry run: nothing written"]
    assert snapshot(stock.parent) == before
    assert ok(adjust(stock, "TEA-010-5", "ZZ-999=3", "--dry-run")) == [
        "TEA-010: 30 -> 25",
        "ZZ-999: new -> 3",
        "dry run: nothing written",
    ]
    assert snapshot(stock.parent) == before


def _model(levels: list[tuple[str, int, int]], changes: list[tuple[str, str, int]]):
    on_hand = {s: o for s, o, _ in levels}
    reserved = {s: r for s, _, r in levels}
    order = [s for s, _, _ in levels]
    for sku, op, number in changes:
        if sku not in on_hand:
            if op == "-":
                return sku, None, None
            order.append(sku)
            on_hand[sku], reserved[sku] = 0, 0
        new = {"+": on_hand[sku] + number, "-": on_hand[sku] - number, "=": number}[op]
        if new < reserved[sku]:
            return sku, None, None
        on_hand[sku] = new
    before = {s: o for s, o, _ in levels}
    lines = []
    for sku in order:
        if sku not in before:
            lines.append(f"{sku}: new -> {on_hand[sku]}")
        elif on_hand[sku] != before[sku]:
            lines.append(f"{sku}: {before[sku]} -> {on_hand[sku]}")
    rows = [(s, on_hand[s], reserved[s]) for s in order]
    return None, lines, rows


def test_seeded_sequences_match_the_model(tmp_path: Path) -> None:
    rng = random.Random(20261011)
    levels = [("AA-100", 5, 2), ("BB-200", 0, 0), ("CC-300", 7, 7)]
    for trial in range(40):
        changes = []
        for _ in range(rng.randrange(1, 6)):
            sku = rng.choice(("AA-100", "BB-200", "CC-300", "DD-400"))
            op = rng.choice("+-=")
            number = rng.randrange(0, 10) if op == "=" else rng.randrange(1, 10)
            changes.append((sku, op, number))
        stock = setup(tmp_path, canonical(levels), f"t{trial}.csv")
        done = adjust(stock, *(f"{s}{op}{n}" for s, op, n in changes))
        failing, lines, rows = _model(levels, changes)
        if failing is not None:
            assert_error(done, failing)
            assert stock.read_text(encoding="utf-8") == canonical(levels), changes
        elif not lines:
            assert ok(done) == ["no changes"], changes
            assert stock.read_text(encoding="utf-8") == canonical(levels), changes
        else:
            assert ok(done) == lines, changes
            assert stock.read_text(encoding="utf-8") == canonical(rows), changes
