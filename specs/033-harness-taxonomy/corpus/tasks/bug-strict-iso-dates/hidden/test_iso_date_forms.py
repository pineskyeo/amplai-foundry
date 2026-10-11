import json
import os
import random
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from stockroom.csvio import read_orders
from stockroom.dates import parse_date
from stockroom.errors import ParseError
from stockroom.jsonio import load_orders

ROOT = Path(__file__).resolve().parents[3]
HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def assert_one_error_line(done: subprocess.CompletedProcess[str]) -> None:
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    lines = done.stderr.splitlines()
    assert len(lines) == 1, done.stderr
    assert lines[0].startswith("stockroom: error: ")


def test_only_dashed_calendar_dates_are_read() -> None:
    assert parse_date("2024-03-09") == date(2024, 3, 9)
    assert parse_date(" 2024-03-09 ") == date(2024, 3, 9)
    assert parse_date("\t2024-03-09\n") == date(2024, 3, 9)
    assert parse_date("0001-01-01") == date(1, 1, 1)
    assert parse_date("9999-12-31") == date(9999, 12, 31)
    assert parse_date("2024-02-29") == date(2024, 2, 29)
    assert parse_date("2000-02-29") == date(2000, 2, 29)
    for compact in ("20240309", "2024-W10-6", "2024W106"):
        with pytest.raises(ParseError):
            parse_date(compact)


def test_other_written_forms_are_refused() -> None:
    bad = [
        "",
        "   ",
        "20240309",
        " 20240309 ",
        "2024-W10-6",
        "2024W106",
        "2024-W10",
        "2024W10",
        "2024-069",
        "2024069",
        "2024-3-9",
        "2024-03-9",
        "2024-3-09",
        "24-03-09",
        "02024-03-09",
        "+2024-03-09",
        "-2024-03-09",
        "2024/03/09",
        "2024.03.09",
        "2024-03",
        "2024",
        "2024-03-09T10:00",
        "2024-03-09 10:00",
        "2024-03-09T00:00:00",
        "2024 -03-09",
        "2024- 03-09",
        "2024-03-09x",
        "x2024-03-09",
        "".join(chr(0xFF10 + int(c)) if c.isdigit() else c for c in "2024-03-09"),
        "".join(chr(0x0660 + int(c)) if c.isdigit() else c for c in "2024-03-09"),
        "2024-" + chr(0x0660) + chr(0x0663) + "-09",
        "0000-01-01",
        "2023-02-29",
        "2024-02-30",
        "2024-04-31",
        "2024-13-01",
        "2024-00-10",
        "2024-01-00",
        "2024-01-32",
    ]
    for text in bad:
        with pytest.raises(ParseError):
            parse_date(text)


ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def model(text: str) -> date | None:
    core = text.strip()
    if not ISO.fullmatch(core):
        return None
    try:
        return date(int(core[:4]), int(core[5:7]), int(core[8:10]))
    except ValueError:
        return None


def test_generated_texts_follow_the_date_rule() -> None:
    rng = random.Random(3031)
    texts = []
    for _ in range(1500):
        day = date(1, 1, 1) + timedelta(days=rng.randrange(0, 3652058))
        year, week, weekday = day.isocalendar()
        texts.append(day.isoformat())
        texts.append(f"{day.year:04d}{day.month:02d}{day.day:02d}")
        texts.append(f"{year:04d}-W{week:02d}-{weekday}")
        texts.append(f"{year:04d}W{week:02d}{weekday}")
        noise = "".join(rng.choice("0123456789-W") for _ in range(rng.randint(6, 11)))
        texts.append(noise)
        texts.append(
            f"{rng.randint(0, 9999):04d}-{rng.randint(0, 19):02d}-{rng.randint(0, 39):02d}"
        )
    pads = ["", " ", "\t", "\n", "  "]
    for text in texts:
        padded = rng.choice(pads) + text + rng.choice(pads)
        expected = model(padded)
        if expected is None:
            with pytest.raises(ParseError):
                parse_date(padded)
        else:
            assert parse_date(padded) == expected, padded


def test_order_files_with_other_date_forms_are_refused(tmp_path: Path) -> None:
    for placed in ("20240309", "2024-W10-6"):
        text = (
            HEADER
            + "A-1,Hana Kim,2024-01-08,paid,MUG-001,1,5.00\n"
            + f"A-2,Omar Diaz,{placed},paid,MUG-001,2,5.00\n"
        )
        with pytest.raises(ParseError):
            read_orders(text)
        path = tmp_path / "orders.csv"
        path.write_text(text, encoding="utf-8")
        assert_one_error_line(run("sales", "--orders", str(path)))
    good = HEADER + "A-1,Hana Kim, 2024-03-09 ,paid,MUG-001,1,5.00\n"
    assert read_orders(good)[0].placed == date(2024, 3, 9)


def test_json_orders_with_other_date_forms_are_refused() -> None:
    def obj(placed: str) -> str:
        return json.dumps(
            [
                {
                    "order_id": "A-1",
                    "customer": "Hana Kim",
                    "placed": placed,
                    "lines": [{"sku": "MUG-001", "quantity": 1, "unit_price_cents": 500}],
                }
            ]
        )

    assert load_orders(obj("2024-03-09"))[0].placed == date(2024, 3, 9)
    for placed in ("20240309", "2024-W10-6", "2024W106"):
        with pytest.raises(ParseError):
            load_orders(obj(placed))


def test_sales_range_options_take_only_dashed_dates() -> None:
    orders = "data/orders.csv"
    done = run("sales", "--orders", orders, "--from", "2024-02-01")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[:2] == ["3 orders, 1 cancelled", "Revenue: $81.70"]
    done = run("sales", "--orders", orders, "--from", " 2024-02-01 ")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[:2] == ["3 orders, 1 cancelled", "Revenue: $81.70"]
    for option in ("--from", "--to"):
        for value in ("20240201", "2024-W05-4", "2024W054", "", " ", "2024-2-1"):
            assert_one_error_line(run("sales", "--orders", orders, option, value))
    assert_one_error_line(run("sales", "--orders", orders, "--from", "2024-02-01", "--to", ""))
    assert_one_error_line(run("sales", "--orders", orders, "--from", "", "--to", "2024-12-30"))
