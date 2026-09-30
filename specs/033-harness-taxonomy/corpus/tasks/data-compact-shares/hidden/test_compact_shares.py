from __future__ import annotations

import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.errors import ParseError
from stockroom.numfmt import format_compact, parse_compact, percent_shares

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


def test_compact_format_examples() -> None:
    expected = {
        0: "0",
        1: "1",
        999: "999",
        1000: "1K",
        1049: "1K",
        1050: "1.1K",
        1099: "1.1K",
        1100: "1.1K",
        1500: "1.5K",
        1949: "1.9K",
        1950: "2K",
        999_949: "999.9K",
        999_950: "1M",
        999_999: "1M",
        1_000_000: "1M",
        1_049_999: "1M",
        1_050_000: "1.1M",
        999_949_999: "999.9M",
        999_950_000: "1B",
        10**12: "1T",
        999_950_000_000_000: "1000T",
        1_234_567_890_123_456: "1234.6T",
    }
    for number, text in expected.items():
        assert format_compact(number) == text, number
        if number:
            assert format_compact(-number) == ("-" + text if number >= 1000 else f"-{number}")


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1.5K", 1500),
        ("1.5k", 1500),
        ("2M", 2_000_000),
        ("2m", 2_000_000),
        ("3B", 3_000_000_000),
        ("4T", 4 * 10**12),
        ("1.25K", 1250),
        ("0.001K", 1),
        ("1.000000K", 1000),
        ("999.9K", 999_900),
        ("1234.6T", 1_234_600_000_000_000),
        ("12", 12),
        ("12.0", 12),
        ("007", 7),
        ("0", 0),
        ("-1.5K", -1500),
        ("-0", 0),
        ("  2K  ", 2000),
    ],
)
def test_parse_compact(text: str, value: int) -> None:
    assert parse_compact(text) == value


@pytest.mark.parametrize(
    "text",
    ["", " ", "K", "1.2345K", "1.5", "0.5", "1.0001K", "1 K", "1KK", "1x", "+1K", "--1K", "1,5K", ".5K", "5.K", "1e3", "١K", "1.5 M", "1.5Mi", "K1"],
)
def test_parse_compact_refusals(text: str) -> None:
    with pytest.raises(ParseError):
        parse_compact(text)


def test_compact_round_trip_properties() -> None:
    rng = random.Random(20261001)
    units = [1, 10**3, 10**6, 10**9, 10**12]
    numbers = []
    for _ in range(4000):
        numbers.append(rng.randrange(0, 10 ** rng.randrange(1, 16)))
    for unit in units[1:]:
        numbers += [unit - 51, unit - 50, unit - 49, unit - 1, unit, unit + 1, unit * 1000 - 51, unit * 1000 - 50]
    numbers = sorted(set(n for n in numbers if n >= 0))
    previous = None
    for n in numbers:
        for value in (n, -n):
            text = format_compact(value)
            assert re.fullmatch(r"-?[0-9]+(\.[1-9])?[KMBT]?", text), (value, text)
            back = parse_compact(text)
            # the error is at most half a tenth of the unit that was chosen
            suffix = text[-1] if text[-1] in "KMBT" else ""
            unit = {"": 1, "K": 10**3, "M": 10**6, "B": 10**9, "T": 10**12}[suffix]
            assert abs(back - value) * 20 <= unit or (not suffix and back == value), (value, text)
            assert format_compact(back) == text, (value, text)
        text = format_compact(n)
        back = parse_compact(text)
        if previous is not None:
            assert back >= previous, (n, text)
        previous = back
        if n < 1000:
            assert text == str(n)
        elif n < 10**15:
            # never a three-digit-plus number of the next unit down: 1000K is written 1M
            assert not text.startswith("1000") or text.endswith("T")


def test_percent_shares_examples() -> None:
    assert percent_shares([1, 1, 1]) == ["33.4%", "33.3%", "33.3%"]
    assert percent_shares([1, 1, 1], 0) == ["34%", "33%", "33%"]
    assert percent_shares([1, 1, 1], 2) == ["33.34%", "33.33%", "33.33%"]
    assert percent_shares([2, 1]) == ["66.7%", "33.3%"]
    assert percent_shares([1, 2]) == ["33.3%", "66.7%"]
    assert percent_shares([1, 1]) == ["50.0%", "50.0%"]
    assert percent_shares([5]) == ["100.0%"]
    assert percent_shares([0, 5]) == ["0.0%", "100.0%"]
    assert percent_shares([0, 0]) == ["0.0%", "0.0%"]
    assert percent_shares([0, 0], 0) == ["0%", "0%"]
    assert percent_shares([]) == []
    assert percent_shares([1] * 7) == ["14.3%"] * 6 + ["14.2%"]
    assert percent_shares([500000, 150000, 50050]) == ["71.4%", "21.4%", "7.2%"]
    assert percent_shares([500000, 150000, 50050], 0) == ["71%", "22%", "7%"]
    assert percent_shares([500000, 150000, 50050], 3) == ["71.423%", "21.427%", "7.150%"]
    assert percent_shares((3, 1)) == ["75.0%", "25.0%"]
    for bad in (-1, 5, 10):
        with pytest.raises(ValueError):
            percent_shares([1, 2], bad)
    with pytest.raises(ValueError):
        percent_shares([1, -1])
    with pytest.raises(ValueError):
        percent_shares([], -1)


def _scaled(text: str, digits: int) -> int:
    match = re.fullmatch(r"([0-9]+)\.([0-9]{%d})%%" % digits if digits else r"([0-9]+)()%", text)
    assert match, text
    return int(match.group(1)) * 10**digits + (int(match.group(2)) if digits else 0)


def test_generated_shares_add_up_and_stay_close() -> None:
    rng = random.Random(77)
    for _ in range(3000):
        values = [
            rng.choice([0, 0, 1, 2, 3, rng.randrange(0, 50), rng.randrange(0, 10**9)])
            for _ in range(rng.randrange(1, 13))
        ]
        digits = rng.randrange(0, 5)
        shares = percent_shares(values, digits)
        assert len(shares) == len(values)
        total, scale = sum(values), 100 * 10**digits
        scaled = [_scaled(s, digits) for s in shares]
        if total == 0:
            assert scaled == [0] * len(values)
            continue
        assert sum(scaled) == scale, (values, digits, shares)
        for value, got in zip(values, scaled, strict=True):
            exact_floor = value * scale // total
            assert got in (exact_floor, exact_floor + 1), (values, digits, shares)
            if value * scale % total == 0:
                assert got == exact_floor
        for i, a in enumerate(values):
            for j, b in enumerate(values):
                if a > b:
                    assert scaled[i] >= scaled[j], (values, digits, shares)
                if a == b and i < j:
                    assert scaled[i] >= scaled[j], (values, digits, shares)
        # largest remainder: a value rounded up never has a smaller remainder than one rounded down
        ups = [i for i, v in enumerate(values) if scaled[i] > v * scale // total]
        downs = [i for i, v in enumerate(values) if scaled[i] == v * scale // total]
        for i in ups:
            for j in downs:
                ri, rj = values[i] * scale % total, values[j] * scale % total
                assert ri > rj or (ri == rj and i < j), (values, digits, shares)


def test_share_command(tmp_path: Path) -> None:
    orders = tmp_path / "orders.csv"
    orders.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "O-1,Ada Moss,2024-01-05,paid,MUG-001,1,1500.00\n"
        "O-2,Ben Ode,2024-01-06,shipped,MUG-001,2,2500.00\n"
        "O-3,Cy Ray,2024-01-07,open,MUG-002,1,500.50\n"
        "O-4,Dee Fox,2024-01-08,cancelled,MUG-001,1,9999.00\n"
        "O-5,Cy Ray,2024-01-09,cancelled,MUG-002,1,100.00\n"
    )
    done = run("share", "--orders", str(orders))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "Ben Ode: 71.4% (5K)",
        "Ada Moss: 21.4% (1.5K)",
        "Cy Ray: 7.2% (500)",
    ]
    done = run("share", "--orders", str(orders), "--digits", "0")
    assert done.stdout.splitlines() == ["Ben Ode: 71% (5K)", "Ada Moss: 22% (1.5K)", "Cy Ray: 7% (500)"]
    done = run("share", "--orders", str(orders), "--digits", "3")
    assert done.stdout.splitlines()[2] == "Cy Ray: 7.150% (500)"
    tie = tmp_path / "tie.csv"
    tie.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "O-1,Cy,2024-01-05,paid,MUG-001,1,100.00\n"
        "O-2,Ada,2024-01-06,paid,MUG-001,1,100.00\n"
        "O-3,Bo,2024-01-07,paid,MUG-001,1,100.00\n"
    )
    done = run("share", "--orders", str(tie))
    assert done.stdout.splitlines() == ["Ada: 33.4% (100)", "Bo: 33.3% (100)", "Cy: 33.3% (100)"]
    big = tmp_path / "big.csv"
    big.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "O-1,Ada,2024-01-05,paid,MUG-001,1000,1999.99\n"
        "O-2,Bo,2024-01-06,paid,MUG-001,1,0.99\n"
    )
    done = run("share", "--orders", str(big))
    assert done.stdout.splitlines() == ["Ada: 100.0% (2M)", "Bo: 0.0% (0)"]
    none = tmp_path / "none.csv"
    none.write_text(
        "order_id,customer,placed,status,sku,quantity,unit_price\n"
        "O-1,Ada,2024-01-05,cancelled,MUG-001,1,10.00\n"
    )
    done = run("share", "--orders", str(none))
    assert done.returncode == 0 and done.stdout == ""
    for bad in ("-1", "5"):
        done = run("share", "--orders", str(orders), "--digits", bad)
        assert done.returncode == 1 and done.stdout == ""
        assert done.stderr.startswith("stockroom: error:")
