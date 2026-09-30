import subprocess
import sys
from pathlib import Path

from stockroom.csvio import read_items, read_orders, read_stock

ROOT = Path(__file__).resolve().parents[1]


def make(out: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_sample_data.py"), "--out", str(out), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_same_seed_same_files(tmp_path: Path) -> None:
    assert make(tmp_path / "a").returncode == 0
    assert make(tmp_path / "b").returncode == 0
    for name in ("items.csv", "stock.csv", "orders.csv"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_other_seed_other_orders(tmp_path: Path) -> None:
    make(tmp_path / "a", "--seed", "1")
    make(tmp_path / "b", "--seed", "2")
    a = (tmp_path / "a" / "orders.csv").read_text()
    assert a != (tmp_path / "b" / "orders.csv").read_text()


def test_files_are_readable(tmp_path: Path) -> None:
    done = make(tmp_path, "--orders", "12")
    assert done.returncode == 0, done.stderr
    assert len(read_items((tmp_path / "items.csv").read_text())) == 8
    assert len(read_stock((tmp_path / "stock.csv").read_text())) == 8
    assert len(read_orders((tmp_path / "orders.csv").read_text())) == 12
