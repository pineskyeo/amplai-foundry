from __future__ import annotations

import csv
import hashlib
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "make_sample_data.py"
NAMES = ("items.csv", "stock.csv", "orders.csv")

# SHA-256 of the files the unchanged script writes (checked with Python 3.11)
DIGESTS = {
    (7, 20): (
        "1eb8b9a547ef855651a80c8ccff7d44ec14cf47e0a82acde9407b0eeda104be6",
        "792768bfa9e664aa9c28e412b612f70d38fd864e8f575a88bdd0cdb953f6ad74",
        "6d44cad1d322b12d90a388670123d89a22820eca017f2b109856852f95615f7e",
    ),
    (1, 5): (
        "a1d8b8d58e97c2fc8d3c3bff7b5aed09b9804d1d7c960bbed072eaa4af2cfad0",
        "55b82bbf06e87488dea3fb36749715121ca9e4b1bf02801537d824925b07450d",
        "cd8fac5c68558e966fefa32ebc0db6d2d0b7e5a685b46ff7364fcb38c6c4b1d0",
    ),
    (42, 0): (
        "6e1654d7b2d10b474c8294172dbba0d5c25de709b1a388e2e5c6712150a09306",
        "7bcfeb157a25434311337f54f6e8e139bdc5b4bc4f64bcf5105339328d399f13",
        "8390daebc9454b09c2e2ae99196c6cd5faec144527896b4249699532ad957232",
    ),
    (9, 60): (
        "1b2c88225a9aaf9f46d8935b955b02a5b756cccac4d7d8d69bab42ad2926a8bb",
        "77c5f5bff76b8ed349ee934316bd0417b142670926f51fd77feff073a0ebfecc",
        "61c0e9f6fb7442f4cf123cd9457112295c7ee6523b7f94c7422233084cd01050",
    ),
}


def make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def order_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][:3] == ["order_id", "customer", "placed"]
    return rows[1:]


def snapshot(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())}


def assert_error(done: subprocess.CompletedProcess[str], code: int, *needles: str) -> None:
    assert done.returncode == code, (done.returncode, done.stderr)
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    for needle in needles:
        assert needle in done.stderr, (needle, done.stderr)


def test_defaults_are_byte_identical_to_the_current_script(tmp_path: Path) -> None:
    for (seed, orders), expected in DIGESTS.items():
        out = tmp_path / f"d{seed}_{orders}"
        done = make("--out", str(out), "--seed", str(seed), "--orders", str(orders))
        assert done.returncode == 0, done.stderr
        assert done.stdout == f"wrote 8 items and {orders} orders to {out}\n"
        assert done.stderr == ""
        assert tuple(digest(out / n) for n in NAMES) == expected, (seed, orders)
        explicit = tmp_path / f"e{seed}_{orders}"
        done = make(
            "--out", str(explicit), "--seed", str(seed), "--orders", str(orders),
            "--start", "2024-01-01", "--days", "180",
        )
        assert done.returncode == 0, done.stderr
        assert tuple(digest(explicit / n) for n in NAMES) == expected
    done = make("--out", str(tmp_path / "plain"))
    assert done.returncode == 0
    assert tuple(digest(tmp_path / "plain" / n) for n in NAMES) == DIGESTS[(7, 20)]
    assert order_rows(tmp_path / "d42_0" / "orders.csv") == []


def test_start_shifts_dates_and_nothing_else(tmp_path: Path) -> None:
    base = tmp_path / "base"
    assert make("--out", str(base), "--seed", "9", "--orders", "60").returncode == 0
    base_rows = order_rows(base / "orders.csv")
    for start in ("2025-03-01", "2023-12-25", "2024-02-29"):
        out = tmp_path / f"s{start}"
        done = make("--out", str(out), "--seed", "9", "--orders", "60", "--start", start)
        assert done.returncode == 0, done.stderr
        shift = date.fromisoformat(start) - date(2024, 1, 1)
        rows = order_rows(out / "orders.csv")
        assert len(rows) == len(base_rows)
        for new, old in zip(rows, base_rows, strict=True):
            assert new[2] == (date.fromisoformat(old[2]) + shift).isoformat()
            assert new[:2] == old[:2] and new[3:] == old[3:]
        assert digest(out / "items.csv") == DIGESTS[(9, 60)][0]
        assert digest(out / "stock.csv") == DIGESTS[(9, 60)][1]
    # items and stock do not depend on start, days or the order count
    other = tmp_path / "other"
    assert make(
        "--out", str(other), "--seed", "9", "--orders", "3", "--start", "2030-05-05", "--days", "7"
    ).returncode == 0
    assert digest(other / "items.csv") == DIGESTS[(9, 60)][0]
    assert digest(other / "stock.csv") == DIGESTS[(9, 60)][1]


def test_days_bound_the_order_dates(tmp_path: Path) -> None:
    start = date(2024, 2, 28)
    one = tmp_path / "one"
    assert make("--out", str(one), "--seed", "9", "--orders", "60", "--start", "2024-02-28",
                "--days", "1").returncode == 0
    assert {r[2] for r in order_rows(one / "orders.csv")} == {"2024-02-28"}
    two = tmp_path / "two"
    assert make("--out", str(two), "--seed", "9", "--orders", "60", "--start", "2024-02-28",
                "--days", "2").returncode == 0
    assert {r[2] for r in order_rows(two / "orders.csv")} == {"2024-02-28", "2024-02-29"}
    wide = tmp_path / "wide"
    assert make("--out", str(wide), "--seed", "9", "--orders", "60", "--start", "2024-02-28",
                "--days", "400").returncode == 0
    dates = [date.fromisoformat(r[2]) for r in order_rows(wide / "orders.csv")]
    assert all(start <= d <= start + timedelta(days=399) for d in dates)
    assert max(dates) > start + timedelta(days=180)
    for name in NAMES[:2]:
        assert digest(wide / name) == digest(one / name)
    # the range may end on the last supported day
    edge = tmp_path / "edge"
    done = make("--out", str(edge), "--seed", "9", "--orders", "60", "--start", "9999-12-30",
                "--days", "2")
    assert done.returncode == 0, done.stderr
    assert {r[2] for r in order_rows(edge / "orders.csv")} == {"9999-12-30", "9999-12-31"}


def test_bad_values_are_usage_errors_and_write_nothing(tmp_path: Path) -> None:
    cases = [
        (["--orders", "-1"], "--orders"),
        (["--orders", "-20"], "--orders"),
        (["--days", "0"], "--days"),
        (["--days", "-3"], "--days"),
        (["--start", "2024-02-30"], "--start"),
        (["--start", "2024-2-3"], "--start"),
        (["--start", "20240101"], "--start"),
        (["--start", "2024-13-01"], "--start"),
        (["--start", "tomorrow"], "--start"),
        (["--start", ""], "--start"),
        (["--start", "2024-01-01T00:00"], "--start"),
        (["--start", "9999-12-30", "--days", "5"], "--days"),
        (["--start", "9999-12-31", "--days", "2"], "--days"),
    ]
    for extra, option in cases:
        out = tmp_path / "never"
        done = make("--out", str(out), *extra)
        assert_error(done, 2, option)
        assert not out.exists(), extra
    # an existing directory stays as it was
    folder = tmp_path / "keep"
    folder.mkdir()
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    assert_error(make("--out", str(folder), "--orders", "-1"), 2, "--orders")
    assert snapshot(folder) == {"notes.txt": b"x"}
    # the boundaries themselves are fine
    ok = make("--out", str(tmp_path / "ok"), "--orders", "0", "--days", "1", "--start", "0001-01-01")
    assert ok.returncode == 0, ok.stderr


def test_existing_files_are_protected(tmp_path: Path) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "stock.csv").write_text("mine", encoding="utf-8")
    (folder / "notes.txt").write_text("keep", encoding="utf-8")
    before = snapshot(folder)
    done = make("--out", str(folder))
    assert_error(done, 1, "stock.csv")
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("make_sample_data: error: ")
    assert "items.csv" not in lines[0] and "orders.csv" not in lines[0]
    assert snapshot(folder) == before
    (folder / "orders.csv").write_text("also mine", encoding="utf-8")
    before = snapshot(folder)
    done = make("--out", str(folder), "--seed", "3")
    assert_error(done, 1, "stock.csv", "orders.csv")
    assert "items.csv" not in done.stderr
    assert len(done.stderr.splitlines()) == 1
    assert snapshot(folder) == before
    # a bad value is still a usage error first
    assert_error(make("--out", str(folder), "--days", "0"), 2, "--days")
    # an --out that is a regular file
    target = tmp_path / "file.txt"
    target.write_text("plain", encoding="utf-8")
    done = make("--out", str(target))
    assert_error(done, 1, str(target))
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("make_sample_data: error: ")
    assert target.read_text(encoding="utf-8") == "plain"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data", "file.txt"]


def test_force_overwrites_only_the_three_files(tmp_path: Path) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    for name in NAMES:
        (folder / name).write_text("old", encoding="utf-8")
    (folder / "notes.txt").write_text("keep", encoding="utf-8")
    done = make("--out", str(folder), "--force", "--seed", "1", "--orders", "5")
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"wrote 8 items and 5 orders to {folder}\n"
    assert tuple(digest(folder / n) for n in NAMES) == DIGESTS[(1, 5)]
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "keep"
    # --force on a fresh directory, and with a missing parent, just works
    done = make("--out", str(tmp_path / "a" / "b"), "--force", "--seed", "1", "--orders", "5")
    assert done.returncode == 0
    assert tuple(digest(tmp_path / "a" / "b" / n) for n in NAMES) == DIGESTS[(1, 5)]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "fresh" / "deeper"
    done = make("--out", str(out), "--dry-run")
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.splitlines() == [f"would write {out / n}" for n in NAMES]
    assert done.stdout.endswith("\n")
    assert not (tmp_path / "fresh").exists()
    # an existing directory stays unchanged
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "notes.txt").write_text("keep", encoding="utf-8")
    done = make("--out", str(folder), "--dry-run", "--orders", "4")
    assert done.returncode == 0
    assert done.stdout.splitlines() == [f"would write {folder / n}" for n in NAMES]
    assert snapshot(folder) == {"notes.txt": b"keep"}
    # conflicts and usage errors still apply
    (folder / "items.csv").write_text("mine", encoding="utf-8")
    done = make("--out", str(folder), "--dry-run")
    assert_error(done, 1, "items.csv")
    assert "stock.csv" not in done.stderr and "orders.csv" not in done.stderr
    done = make("--out", str(folder), "--dry-run", "--force")
    assert done.returncode == 0
    assert done.stdout.splitlines() == [f"would write {folder / n}" for n in NAMES]
    assert (folder / "items.csv").read_text(encoding="utf-8") == "mine"
    assert_error(make("--out", str(tmp_path / "x"), "--dry-run", "--days", "0"), 2, "--days")
    assert not (tmp_path / "x").exists()
