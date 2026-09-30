from __future__ import annotations

import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from stockroom.dates import format_date, format_date_style, parse_date, parse_loose_date
from stockroom.errors import ParseError

ROOT = Path(__file__).resolve().parents[3]
STYLES = ("iso", "slash", "dotted", "compact", "short", "long", "us", "ordinal")


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


def test_numeric_forms() -> None:
    target = date(2024, 3, 9)
    for text in (
        "2024-03-09",
        "2024/3/9",
        "2024/03/09",
        "2024/3/09",
        "9.3.2024",
        "09.03.2024",
        "9.03.2024",
        "20240309",
        "  2024-03-09  ",
        "\t9.3.2024\n",
    ):
        assert parse_loose_date(text) == target, text
    assert parse_loose_date("2024/12/31") == date(2024, 12, 31)
    assert parse_loose_date("31.12.2023") == date(2023, 12, 31)
    assert parse_loose_date("0001-01-01") == date(1, 1, 1)
    assert parse_loose_date("1.2.0999") == date(999, 2, 1)
    assert parse_loose_date("99991231") == date(9999, 12, 31)
    assert parse_loose_date("2023-02-28") == date(2023, 2, 28)
    assert parse_date("2024-03-09") == target and format_date(target) == "2024-03-09"


def test_written_month_forms() -> None:
    target = date(2024, 3, 9)
    for text in (
        "9 Mar 2024",
        "09 march 2024",
        "9 MARCH  2024",
        "March 9, 2024",
        "mar 9,  2024",
        "MARCH 09, 2024",
        "9th March 2024",
        "9TH mar 2024",
        "09th Mar 2024",
        "Mar 9th, 2024",
    ):
        assert parse_loose_date(text) == target, text
    days = {
        "1st Jan 2024": date(2024, 1, 1),
        "2nd feb 2024": date(2024, 2, 2),
        "3rd Mar 2024": date(2024, 3, 3),
        "4th Apr 2024": date(2024, 4, 4),
        "11th Apr 2024": date(2024, 4, 11),
        "12th May 2024": date(2024, 5, 12),
        "13th Jun 2024": date(2024, 6, 13),
        "21st Jul 2024": date(2024, 7, 21),
        "22nd Aug 2024": date(2024, 8, 22),
        "23rd Sep 2024": date(2024, 9, 23),
        "30th Oct 2024": date(2024, 10, 30),
        "31st Dec 2024": date(2024, 12, 31),
        "Mar 1st, 2024": date(2024, 3, 1),
        "December 25th, 2023": date(2023, 12, 25),
        "May 2, 2024": date(2024, 5, 2),
        "2 May 2024": date(2024, 5, 2),
        "30 September 2024": date(2024, 9, 30),
        "sep 30, 2024": date(2024, 9, 30),
        "1 jan 0001": date(1, 1, 1),
    }
    for text, expected in days.items():
        assert parse_loose_date(text) == expected, text
    names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    for number, name in enumerate(names, start=1):
        assert parse_loose_date(f"5 {name} 2021") == date(2021, number, 5)
        assert parse_loose_date(f"5 {name[:3].lower()} 2021") == date(2021, number, 5)
        assert parse_loose_date(f"{name.upper()} 5, 2021") == date(2021, number, 5)


REFUSED = [
    "", " ", "03/04/2024", "3/4/2024", "2024-3-9", "2024-03-9", "2024-3-09", "24-03-09", "9.3.24",
    "2024.03.09", "9 Mar 24", "9 Sept 2024", "9 Marc 2024", "9 Ma 2024", "Mar 9 2024", "March 9,2024",
    "9th, March 2024", "1th Jan 2024", "2st Feb 2024", "3th Mar 2024", "4rd Apr 2024", "11st Apr 2024",
    "12nd May 2024", "13rd Jun 2024", "21th Jul 2024", "22st Aug 2024", "23th Sep 2024", "9 March, 2024",
    "31 Apr 2024", "30 Feb 2024", "31 Jun 2024", "2023-02-29", "29.2.2023", "0 Jan 2024", "32 Jan 2024",
    "2024-13-01", "2024-00-10", "2024-01-00", "20241301", "2024031", "202403099", "0000-01-01", "9-Mar-2024",
    "2024-03-09T10:00", "2024-03-09 10:00", "March 9th 2024", "mar. 9, 2024", "9 Mar. 2024", "9 Mar 2024 x",
    "x 9 Mar 2024", "March 9, 2024, 2025", "９.３.２０２４", "２０２４-03-09", "1 2 2024", "9 3 2024",
    "2024/3", "2024/3/9/1", "9.3.2024.", "+2024-03-09", "March", "9 March", "March 2024", "2024",
    "9\tMar 2024", "9thMar 2024", "9 Mar2024", "Mar9, 2024", "9 Märch 2024", "9 Mär 2024", "9 Mar 02024",
]  # fmt: skip


@pytest.mark.parametrize("text", REFUSED)
def test_other_texts_are_refused(text: str) -> None:
    with pytest.raises(ParseError):
        parse_loose_date(text)


def test_format_date_style() -> None:
    value = date(2024, 3, 9)
    assert [format_date_style(value, s) for s in STYLES] == [
        "2024-03-09",
        "2024/3/9",
        "9.3.2024",
        "20240309",
        "9 Mar 2024",
        "9 March 2024",
        "March 9, 2024",
        "9th March 2024",
    ]
    assert format_date_style(date(1, 2, 3), "iso") == "0001-02-03"
    assert format_date_style(date(1, 2, 3), "slash") == "0001/2/3"
    assert format_date_style(date(999, 12, 31), "dotted") == "31.12.0999"
    assert format_date_style(date(999, 12, 31), "long") == "31 December 0999"
    assert format_date_style(date(2024, 1, 1), "ordinal") == "1st January 2024"
    assert format_date_style(date(2024, 1, 2), "ordinal") == "2nd January 2024"
    assert format_date_style(date(2024, 1, 3), "ordinal") == "3rd January 2024"
    assert format_date_style(date(2024, 1, 11), "ordinal") == "11th January 2024"
    assert format_date_style(date(2024, 1, 12), "ordinal") == "12th January 2024"
    assert format_date_style(date(2024, 1, 13), "ordinal") == "13th January 2024"
    assert format_date_style(date(2024, 1, 21), "ordinal") == "21st January 2024"
    assert format_date_style(date(2024, 1, 22), "ordinal") == "22nd January 2024"
    assert format_date_style(date(2024, 1, 23), "ordinal") == "23rd January 2024"
    assert format_date_style(date(2024, 1, 31), "ordinal") == "31st January 2024"
    assert format_date_style(date(2024, 5, 5), "short") == "5 May 2024"
    assert format_date_style(date(2024, 9, 30), "us") == "September 30, 2024"
    with pytest.raises(ValueError):
        format_date_style(value, "german")
    with pytest.raises(ValueError):
        format_date_style(value, "ISO")


def test_every_style_round_trips() -> None:
    rng = random.Random(20261001)
    checked = 0
    while checked < 3000:
        value = date.fromordinal(rng.randrange(1, date.max.toordinal() + 1))
        if value.month == 2 and value.day == 29:
            continue
        for style in STYLES:
            text = format_date_style(value, style)
            assert parse_loose_date(text) == value, (value, style, text)
            assert parse_loose_date(f" {text}\n") == value
        checked += 1
    every_day = date(2023, 1, 1)
    for _ in range(365):
        for style in STYLES:
            assert parse_loose_date(format_date_style(every_day, style)) == every_day
        every_day += timedelta(days=1)
    for day in range(1, 32):
        value = date(2024, 12, day)
        assert parse_loose_date(format_date_style(value, "ordinal")) == value


def test_sales_dates_on_the_command_line() -> None:
    orders = str(ROOT / "data" / "orders.csv")
    base = run("sales", "--orders", orders, "--from", "2024-02-01", "--to", "2030-12-31")
    assert base.returncode == 0, base.stderr
    assert base.stdout != run("sales", "--orders", orders).stdout
    for start in ("February 1, 2024", "1 Feb 2024", "1st February 2024", "1.2.2024", "2024/2/1", "20240201", "2024-02-01"):
        for end in ("31.12.2030", "December 31st, 2030", "31 dec 2030", "2030/12/31", "20301231", "2030-12-31"):
            done = run("sales", "--orders", orders, "--from", start, "--to", end)
            assert done.returncode == 0, (start, end, done.stderr)
            assert done.stdout == base.stdout, (start, end)
    only_from = run("sales", "--orders", orders, "--from", "14 Feb 2024")
    assert only_from.stdout == run("sales", "--orders", orders, "--from", "2024-02-14").stdout
    assert only_from.stdout != base.stdout
    for bad in ("03/04/2024", "9 Sept 2024", "2024-3-9", "yesterday"):
        done = run("sales", "--orders", orders, "--from", bad)
        assert done.returncode == 1 and done.stdout == "", bad
        assert done.stderr.startswith("stockroom: error:")
        done = run("sales", "--orders", orders, "--to", bad)
        assert done.returncode == 1 and done.stdout == "", bad
        assert done.stderr.startswith("stockroom: error:")
