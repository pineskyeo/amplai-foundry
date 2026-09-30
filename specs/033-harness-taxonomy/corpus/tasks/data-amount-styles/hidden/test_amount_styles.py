from __future__ import annotations

import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stockroom.amounts import format_amount, parse_amount
from stockroom.config import load_config
from stockroom.errors import ConfigError, ParseError
from stockroom.money import format_money

ROOT = Path(__file__).resolve().parents[3]
STYLES = ("us", "eu", "ch", "in", "plain")
NEGATIVES = ("minus", "paren", "trailing")
POSITIONS = ("prefix", "suffix")


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def test_formats_of_each_style() -> None:
    expected = {
        "us": "1,234,567.89",
        "eu": "1.234.567,89",
        "ch": "1'234'567.89",
        "in": "12,34,567.89",
        "plain": "1234567.89",
    }
    for style, text in expected.items():
        assert format_amount(123456789, style=style) == text
    assert format_amount(123456789) == "1,234,567.89"
    table = {
        0: ("0.00", "0,00", "0.00", "0.00", "0.00"),
        5: ("0.05", "0,05", "0.05", "0.05", "0.05"),
        99999: ("999.99", "999,99", "999.99", "999.99", "999.99"),
        100000: ("1,000.00", "1.000,00", "1'000.00", "1,000.00", "1000.00"),
        1000000: ("10,000.00", "10.000,00", "10'000.00", "10,000.00", "10000.00"),
        10000000: ("100,000.00", "100.000,00", "100'000.00", "1,00,000.00", "100000.00"),
        123456789012: (
            "1,234,567,890.12",
            "1.234.567.890,12",
            "1'234'567'890.12",
            "1,23,45,67,890.12",
            "1234567890.12",
        ),
    }
    for cents, texts in table.items():
        assert tuple(format_amount(cents, style=s) for s in STYLES) == texts, cents
    assert format_amount(-5) == "-0.05"
    assert format_amount(-123456, style="in") == "-1,234.56"


def test_negative_forms_symbols_and_positions() -> None:
    assert format_amount(-123456) == "-1,234.56"
    assert format_amount(-123456, negative="minus") == "-1,234.56"
    assert format_amount(-123456, negative="paren") == "(1,234.56)"
    assert format_amount(-123456, negative="trailing") == "1,234.56-"
    assert format_amount(123456, negative="paren") == "1,234.56"
    assert format_amount(123456, symbol="€") == "€1,234.56"
    assert format_amount(-123456, symbol="€") == "-€1,234.56"
    assert format_amount(-123456, symbol="€", negative="paren") == "(€1,234.56)"
    assert format_amount(-123456, symbol="€", negative="trailing") == "€1,234.56-"
    assert format_amount(123456, style="eu", symbol="€", symbol_position="suffix") == "1.234,56 €"
    assert (
        format_amount(-123456, style="eu", symbol="€", symbol_position="suffix") == "-1.234,56 €"
    )
    assert (
        format_amount(-123456, style="eu", symbol="EUR", negative="paren", symbol_position="suffix")
        == "(1.234,56 EUR)"
    )
    assert (
        format_amount(-123456, style="ch", symbol="CHF", negative="trailing", symbol_position="suffix")
        == "1'234.56 CHF-"
    )
    assert format_amount(123456, symbol="", symbol_position="suffix") == "1,234.56"
    rng = random.Random(11)
    for _ in range(500):
        cents = rng.randrange(-10**9, 10**9)
        for symbol in ("", "$", "EUR "):
            assert format_amount(cents, symbol=symbol) == format_money(cents, symbol=symbol)
    with pytest.raises(ValueError):
        format_amount(1, style="de")
    with pytest.raises(ValueError):
        format_amount(1, negative="brackets")
    with pytest.raises(ValueError):
        format_amount(1, symbol_position="middle")
    with pytest.raises(ValueError):
        parse_amount("1", style="de")


def test_parses_well_formed_amounts() -> None:
    cases = [
        ("us", "1,234,567.89", 123456789),
        ("us", "1234567.89", 123456789),
        ("us", "0.5", 50),
        ("us", "12", 1200),
        ("us", "1,000", 100000),
        ("us", "  12.50  ", 1250),
        ("us", "0.05", 5),
        ("us", "(1,234.50)", -123450),
        ("us", "1,234.50-", -123450),
        ("us", "-1,234.50", -123450),
        ("us", "-0.00", 0),
        ("us", "(0.00)", 0),
        ("eu", "1.234.567,89", 123456789),
        ("eu", "1.234", 123400),
        ("eu", "12,5", 1250),
        ("eu", "1234,56", 123456),
        ("eu", "(1.234,50)", -123450),
        ("ch", "1'234'567.89", 123456789),
        ("ch", "1’234’567.89", 123456789),
        ("ch", "1'234’567", 123456700),
        ("ch", "1234.5", 123450),
        ("in", "12,34,567.89", 123456789),
        ("in", "1,23,456", 12345600),
        ("in", "1,234", 123400),
        ("in", "12,345.6", 1234560),
        ("in", "1,00,000.00", 10000000),
        ("in", "1234567", 123456700),
        ("plain", "1234567.89", 123456789),
        ("plain", "7", 700),
        ("plain", "1234.5-", -123450),
    ]
    for style, text, cents in cases:
        assert parse_amount(text, style=style) == cents, (style, text)
    assert parse_amount("12.50") == 1250


def test_symbols_before_or_after_the_number() -> None:
    assert parse_amount("€1,234.56", symbol="€") == 123456
    assert parse_amount("€ 1,234.56", symbol="€") == 123456
    assert parse_amount("1,234.56€", symbol="€") == 123456
    assert parse_amount("1,234.56   €", symbol="€") == 123456
    assert parse_amount("1,234.56", symbol="€") == 123456
    assert parse_amount("-€1,234.56", symbol="€") == -123456
    assert parse_amount("(€ 1,234.56)", symbol="€") == -123456
    assert parse_amount("€1,234.56-", symbol="€") == -123456
    assert parse_amount("-1.234,56 €", style="eu", symbol="€") == -123456
    assert parse_amount("CHF 1'234.50", style="ch", symbol="CHF") == 123450
    assert parse_amount("(1.234,56 EUR)", style="eu", symbol="EUR") == -123456
    for bad, kwargs in [
        ("$1.00", {}),
        ("1.00 USD", {}),
        ("€1.00", {"symbol": "$"}),
        ("€1.00€", {"symbol": "€"}),
        ("$-1.00", {"symbol": "$"}),
        ("$ -1.00", {"symbol": "$"}),
        ("-", {"symbol": "$"}),
        ("$", {"symbol": "$"}),
        ("€ €1.00", {"symbol": "€"}),
    ]:
        with pytest.raises(ParseError):
            parse_amount(bad, **kwargs)  # type: ignore[arg-type]


REFUSED = {
    "us": [
        "", " ", "abc", "1.234", "1,23", "1,2345", ",123", "1,,234", "1,234,56", "12.", ".5",
        "1.2.3", "1,234.5.6", "--1.00", "(1.00", "1.00)", "-(1.00)", "(-1.00)", "-1.00-",
        "- 1.00", "( 1.00)", "1 234.00", "1e3", "١٢٣", "１２３", "1,23４", "()", "-", "1,000,00",
        "1.000,00", "1'000.00", "+1.00", "1_000", "0x10", "12,34,567", "1,2,3", "NaN", "--",
    ],
    "eu": ["1,234,567", "1.23", "1.2345", "12.50,00", "1.234,567", "1,", ",5", "1.,50", "1,2.3"],
    "ch": ["1,234.50", "1.234'567.50", "1'23.00", "1'2345", "1''234", "'234", "1'234,50"],
    "in": ["1,234,567.89", "123,456", "1,23,45", "12,34,5678", ",123", "1,23,", "1,2,345"],
    "plain": ["1,234", "1'234", "1 234", "1.234,5", "1234.", "1234.567"],
}


@pytest.mark.parametrize("style", STYLES)
def test_malformed_amounts_are_refused(style: str) -> None:
    for text in REFUSED[style]:
        with pytest.raises(ParseError):
            parse_amount(text, style=style)
    if style != "us":
        for text in REFUSED["us"]:
            if text in {"", " ", "abc", "()", "-", "--", "NaN", "0x10", "1e3", "١٢٣", "１２３", "-(1.00)"}:
                with pytest.raises(ParseError):
                    parse_amount(text, style=style)


def test_round_trip_over_every_form() -> None:
    rng = random.Random(20261001)
    samples = [0, 1, 5, 99, 100, 101, 99999, 100000, 100001, 1234567, 99999999, 10**9]
    while len(samples) < 400:
        magnitude = rng.choice([3, 4, 5, 6, 7, 8, 9, 10, 11])
        samples.append(rng.randrange(0, 10**magnitude))
    for cents in samples + [-c for c in samples if c >= 100]:
        for style in STYLES:
            for negative in NEGATIVES:
                for symbol in ("", "€", "CHF", "$"):
                    for position in POSITIONS:
                        text = format_amount(
                            cents,
                            style=style,
                            symbol=symbol,
                            negative=negative,
                            symbol_position=position,
                        )
                        back = parse_amount(text, style=style, symbol=symbol)
                        assert back == cents, (cents, style, negative, symbol, position, text)


GROUPED = {
    "us": r"[0-9]{1,3}(,[0-9]{3})*\.[0-9]{2}",
    "eu": r"[0-9]{1,3}(\.[0-9]{3})*,[0-9]{2}",
    "ch": r"[0-9]{1,3}('[0-9]{3})*\.[0-9]{2}",
    "in": r"([0-9]{1,2}(,[0-9]{2})*,)?[0-9]{3}\.[0-9]{2}|[0-9]{1,3}\.[0-9]{2}",
    "plain": r"[0-9]+\.[0-9]{2}",
}


def test_written_numbers_have_the_shape_of_their_style() -> None:
    rng = random.Random(77)
    for _ in range(1500):
        cents = rng.randrange(0, 10 ** rng.randrange(1, 14))
        for style in STYLES:
            text = format_amount(cents, style=style)
            assert re.fullmatch(GROUPED[style], text), (cents, style, text)
            digits = re.sub(r"[^0-9]", "", text)
            assert int(digits) == cents
            assert len(digits) >= 3
            if style == "in" and cents >= 100000:
                groups = re.split(r"[,.]", text)
                assert len(groups[-2]) == 3 and all(len(g) == 2 for g in groups[1:-2])


def test_amount_style_setting(tmp_path: Path) -> None:
    assert load_config()["amount_style"] == "us"
    for style in STYLES:
        assert load_config(env={"STOCKROOM_AMOUNT_STYLE": style})["amount_style"] == style
        ini = tmp_path / f"{style}.ini"
        ini.write_text(f"[stockroom]\namount_style = {style}\n")
        assert load_config(ini)["amount_style"] == style
    ini = tmp_path / "eu.ini"
    assert load_config(ini, {"STOCKROOM_AMOUNT_STYLE": "ch"})["amount_style"] == "ch"
    for bad in ("EU", "de", "", "us,eu"):
        with pytest.raises(ConfigError):
            load_config(env={"STOCKROOM_AMOUNT_STYLE": bad})
    wrong = tmp_path / "wrong.ini"
    wrong.write_text("[stockroom]\namount_style = german\n")
    with pytest.raises(ConfigError):
        load_config(wrong)


def test_items_and_price_commands_use_the_style(tmp_path: Path) -> None:
    items = tmp_path / "items.csv"
    items.write_text(
        "sku,name,price,tags,reorder_level\n"
        "BIG-001,Grand Piano,1234567.89,,0\n"
        "MUG-001,Blue Mug,12.50,,0\n"
    )
    shown = {
        "us": ("$1,234,567.89", "$12.50"),
        "eu": ("$1.234.567,89", "$12,50"),
        "ch": ("$1'234'567.89", "$12.50"),
        "in": ("$12,34,567.89", "$12.50"),
        "plain": ("$1234567.89", "$12.50"),
    }
    for style, (big, mug) in shown.items():
        done = run("items", "--file", str(items), env={"STOCKROOM_AMOUNT_STYLE": style})
        assert done.returncode == 0, done.stderr
        rows = done.stdout.splitlines()
        assert rows[2].split() == ["BIG-001", "Grand", "Piano", big]
        assert rows[3].split() == ["MUG-001", "Blue", "Mug", mug]
        assert rows[2].endswith(big) and rows[3].endswith(mug)
        assert len(rows[2]) == len(rows[3])
    default = run("items", "--file", str(items))
    assert default.stdout.splitlines()[2].endswith("$1,234,567.89")
    totals = {
        "us": "$3,703,703.67",
        "eu": "$3.703.703,67",
        "ch": "$3'703'703.67",
        "in": "$37,03,703.67",
        "plain": "$3703703.67",
    }
    zero = {"us": "$0.00", "eu": "$0,00", "ch": "$0.00", "in": "$0.00", "plain": "$0.00"}
    for style, total in totals.items():
        done = run(
            "price", "--items", str(items), "--sku", "BIG-001", "--quantity", "3",
            env={"STOCKROOM_AMOUNT_STYLE": style},
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        assert done.stdout.splitlines() == [
            f"3 x BIG-001 Grand Piano: {total}",
            f"tax: {zero[style]}",
            f"total: {total}",
        ]
    ini = tmp_path / "eu.ini"
    ini.write_text("[stockroom]\namount_style = eu\ncurrency_symbol = EUR \n")
    done = run("--config", str(ini), "items", "--file", str(items))
    assert done.stdout.splitlines()[3].endswith("EUR12,50")
    refused = run("items", "--file", str(items), env={"STOCKROOM_AMOUNT_STYLE": "german"})
    assert refused.returncode == 1 and refused.stderr.startswith("stockroom: error:")
