from __future__ import annotations

import itertools
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.errors import ParseError
from stockroom.skuspec import compress_skus, expand_skus

ROOT = Path(__file__).resolve().parents[3]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_expand_examples() -> None:
    assert expand_skus("MUG-001") == ["MUG-001"]
    assert expand_skus("MUG-001..MUG-003") == ["MUG-001", "MUG-002", "MUG-003"]
    assert expand_skus("MUG-001..003") == ["MUG-001", "MUG-002", "MUG-003"]
    assert expand_skus("MUG-005..MUG-005") == ["MUG-005"]
    assert expand_skus("TEA-010, MUG-001..MUG-002, TEA-010") == ["MUG-001", "MUG-002", "TEA-010"]
    assert expand_skus("  MUG-001 ,\tMUG-002  ") == ["MUG-001", "MUG-002"]
    assert expand_skus("MUG-0998..MUG-1001") == ["MUG-0998", "MUG-0999", "MUG-1000", "MUG-1001"]
    assert expand_skus("MUG-998..999,MUG-0999,MUG-1000") == ["MUG-998", "MUG-999", "MUG-0999", "MUG-1000"]
    assert expand_skus("MUG-999,MUG-1000,MUG-0999") == ["MUG-999", "MUG-0999", "MUG-1000"]
    assert expand_skus("AB-100,ABC-100,AB-200") == ["AB-100", "AB-200", "ABC-100"]
    assert expand_skus("MUG-098..100") == ["MUG-098", "MUG-099", "MUG-100"]
    assert expand_skus("NB-09998..09999") == ["NB-09998", "NB-09999"]
    assert expand_skus("MUG-001..MUG-003, MUG-002..MUG-004") == [f"MUG-00{n}" for n in (1, 2, 3, 4)]
    assert expand_skus("") == []
    assert expand_skus("   ") == []
    assert len(expand_skus("NB-10000..NB-10999")) == 1000
    assert expand_skus("NB-10000..NB-10999")[-1] == "NB-10999"


@pytest.mark.parametrize(
    "spec",
    [
        "MUG-003..MUG-001",
        "MUG-001..TEA-003",
        "MUG-001..MUG-0003",
        "MUG-001..5",
        "MUG-001..0003",
        "mug-001",
        "MUG-01",
        "MUG-123456",
        "MUGGY-001",
        "M-001",
        "MUG001",
        "MUG-001..",
        "..MUG-003",
        "MUG-001...MUG-003",
        "MUG-001 .. MUG-003",
        "MUG-001.. MUG-003",
        "MUG-001,,TEA-010",
        "MUG-001,",
        ",MUG-001",
        ", ",
        "MUG-001;TEA-010",
        "MUG-001..MUG-003..MUG-005",
        "MUG-001..M",
        "MUG-001..abc",
        "MUG-001 MUG-002",
        "MUG-001-MUG-003",
    ],
)
def test_expand_refusals(spec: str) -> None:
    with pytest.raises(ParseError):
        expand_skus(spec)


def test_compress_examples() -> None:
    assert compress_skus([]) == ""
    assert compress_skus(["MUG-001"]) == "MUG-001"
    assert compress_skus(["MUG-001", "MUG-002"]) == "MUG-001, MUG-002"
    assert compress_skus(["MUG-001", "MUG-002", "MUG-003"]) == "MUG-001..MUG-003"
    assert compress_skus(["MUG-001", "MUG-002", "MUG-004"]) == "MUG-001, MUG-002, MUG-004"
    everything = [f"MUG-{n:03d}" for n in (1, 2, 3, 4, 5, 7, 9, 10, 11)]
    assert compress_skus(everything) == "MUG-001..MUG-005, MUG-007, MUG-009..MUG-011"
    assert compress_skus(["MUG-998", "MUG-999", "MUG-1000"]) == "MUG-998, MUG-999, MUG-1000"
    assert compress_skus(["MUG-0998", "MUG-0999", "MUG-1000", "MUG-1001"]) == "MUG-0998..MUG-1001"
    assert compress_skus(["AB-100", "ABC-100", "AB-101", "AB-102"]) == "AB-100..AB-102, ABC-100"
    assert compress_skus(["TEA-011", "MUG-001", "TEA-010", "TEA-012", "TEA-010"]) == "MUG-001, TEA-010..TEA-012"
    assert compress_skus(sku for sku in ["NB-001", "NB-002", "NB-003"]) == "NB-001..NB-003"
    assert compress_skus(["MUG-099", "MUG-100", "MUG-101"]) == "MUG-099..MUG-101"
    assert compress_skus(["MUG-000", "MUG-001", "MUG-002"]) == "MUG-000..MUG-002"
    assert compress_skus(["MUG-099", "MUG-0100", "MUG-0101"]) == "MUG-099, MUG-0100, MUG-0101"
    assert compress_skus(["MUG-99999", "MUG-99998", "MUG-99997"]) == "MUG-99997..MUG-99999"
    for bad in ("mug-1", "MUG-01", "", "MUG-001..MUG-003"):
        with pytest.raises(ParseError):
            compress_skus(["MUG-001", bad])


def _random_skus(rng: random.Random) -> list[str]:
    skus = []
    for _ in range(rng.randrange(0, 5)):
        prefix = rng.choice(["MUG-", "TEA-", "AB-", "ABC-", "ZZZZ-"])
        width = rng.choice([3, 3, 4, 5])
        limit = 10**width
        center = rng.choice([0, 1, 98, 99, 100, 998, 999, 1000, 9998, 9999, limit - 1, rng.randrange(limit)])
        for _ in range(rng.randrange(1, 12)):
            number = center + rng.randrange(-5, 6)
            if 0 <= number < limit:
                skus.append(f"{prefix}{number:0{width}d}")
    rng.shuffle(skus)
    return skus


def _key(sku: str) -> tuple[str, int, int]:
    prefix, digits = sku.rsplit("-", 1)
    return prefix + "-", len(digits), int(digits)


def _model(skus: list[str]) -> str:
    keys = sorted({_key(s) for s in skus})
    out = []
    for _, group in itertools.groupby(enumerate(keys), lambda pair: (pair[1][0], pair[1][1], pair[1][2] - pair[0])):
        run = [key for _, key in group]
        names = [f"{p}{n:0{w}d}" for p, w, n in run]
        out += [f"{names[0]}..{names[-1]}"] if len(run) >= 3 else names
    return ", ".join(out)


def test_generated_lists_round_trip_and_are_shortest() -> None:
    rng = random.Random(20261001)
    for _ in range(1500):
        skus = _random_skus(rng)
        text = compress_skus(skus)
        expected = sorted(set(skus), key=_key)
        assert expand_skus(text) == expected, (skus, text)
        assert compress_skus(expand_skus(text)) == text
        assert compress_skus(reversed(skus)) == text
        assert compress_skus(skus + skus) == text
        pieces = [p for p in text.split(", ") if p]
        assert ", ".join(pieces) == text
        assert text == _model(skus), (skus, text)
        for piece in pieces:
            members = expand_skus(piece)
            if ".." in piece:
                assert len(members) >= 3, (skus, text)
                assert re.fullmatch(r"[A-Z]+-[0-9]+\.\.[A-Z]+-[0-9]+", piece)
            else:
                assert len(members) == 1
        # a run of one or two is never written as a range
        for piece in pieces:
            if ".." in piece:
                first, last = piece.split("..")
                assert _key(last)[2] - _key(first)[2] >= 2
    lone = compress_skus(["MUG-001", "MUG-002"])
    assert ".." not in lone


def test_command_line_with_sku_specs(tmp_path: Path) -> None:
    path = tmp_path / "items.csv"
    rows = ["MUG-001", "MUG-002", "TEA-010", "TEA-011", "TEA-012", "PEN-100", "MUG-0100"]
    names = ["Blue Mug", "Red Mug", "Green Tea", "Black Tea", "White Tea", "Pen", "Big Mug"]
    tags = ["kitchen", "kitchen", "tea", "tea", "tea;kitchen", "", "kitchen"]
    lines = ["sku,name,price,tags,reorder_level"]
    lines += [f"{s},{n},1.00,{t},0" for s, n, t in zip(rows, names, tags, strict=True)]
    path.write_text("\n".join(lines) + "\n")

    def listed(*extra: str) -> list[str]:
        done = run("items", "--file", str(path), *extra)
        assert done.returncode == 0, done.stderr
        return [line.split()[0] for line in done.stdout.splitlines()[2:]]

    assert listed() == rows
    assert listed("--skus", "MUG-001..MUG-002, TEA-011..012") == ["MUG-001", "MUG-002", "TEA-011", "TEA-012"]
    assert listed("--skus", "TEA-012,MUG-002") == ["MUG-002", "TEA-012"]
    assert listed("--skus", "MUG-001..003, XYZ-999, PEN-100") == ["MUG-001", "MUG-002", "PEN-100"]
    assert listed("--skus", "MUG-0100") == ["MUG-0100"]
    assert listed("--skus", "MUG-100") == []
    assert listed("--skus", "MUG-0099..0101") == ["MUG-0100"]
    assert listed("--skus", "") == []
    assert listed("--skus", "TEA-010..TEA-012", "--tag", "kitchen") == ["TEA-012"]
    done = run("skus", "--file", str(path))
    assert done.returncode == 0, done.stderr
    assert done.stdout == "MUG-001, MUG-002, MUG-0100, PEN-100, TEA-010..TEA-012\n"
    empty = tmp_path / "empty.csv"
    empty.write_text("sku,name,price,tags,reorder_level\n")
    done = run("skus", "--file", str(empty))
    assert done.returncode == 0 and done.stdout == ""
    for spec in ("MUG-003..MUG-001", "MUG-001,,MUG-002", "mug-001", "MUG-001.."):
        done = run("items", "--file", str(path), "--skus", spec)
        assert done.returncode == 1 and done.stdout == "", spec
        assert done.stderr.startswith("stockroom: error:")
