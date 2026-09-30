from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PRICE = ["price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "3"]
STOCK = ["stock", "--items", "data/items.csv", "--stock", "data/stock.csv"]
WARNING = "warning: 3 item(s) below reorder level\n"

BASE_INI = """\
[stockroom]
currency_symbol = EUR
tax_bp = 825
low_stock_warning = yes

[stockroom:dev]
currency_symbol = GBP
low_stock_warning = no

[stockroom:prod-eu]
tax_bp = 1900
"""


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


def ini_file(tmp_path: Path, content: str = BASE_INI, name: str = "s.ini") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def price_lines(*args: str, env: dict[str, str] | None = None) -> list[str]:
    done = run(*args, *PRICE, env=env)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    return done.stdout.splitlines()


def amounts(symbol: str, tax: str, total: str) -> list[str]:
    return [f"3 x MUG-001 Blue Mug: {symbol}37.50", f"tax: {symbol}{tax}", f"total: {symbol}{total}"]


EUR_BASE = amounts("EUR", "3.09", "40.59")
GBP_DEV = amounts("GBP", "3.09", "40.59")
EUR_PROD = amounts("EUR", "7.13", "44.63")


def stock_stderr(*args: str, env: dict[str, str] | None = None) -> str:
    done = run(*args, *STOCK, env=env)
    assert done.returncode == 0, done.stderr
    return done.stderr


def assert_one_error(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert len(lines) == 1, done.stderr
    assert lines[0].startswith("stockroom: error: ") and "Traceback" not in done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def test_profile_overrides_the_base_section(tmp_path: Path) -> None:
    ini = str(ini_file(tmp_path))
    assert price_lines("--config", ini) == EUR_BASE
    assert price_lines("--config", ini, "--profile", "dev") == GBP_DEV
    assert price_lines("--config", ini, "--profile", "prod-eu") == EUR_PROD
    assert price_lines("--profile", "dev", "--config", ini) == GBP_DEV
    # the profile's boolean reaches the stock command: dev silences the warning
    assert stock_stderr("--config", ini) == WARNING
    assert stock_stderr("--config", ini, "--profile", "dev") == ""
    assert stock_stderr("--config", ini, "--profile", "prod-eu") == WARNING
    # a profile may be the only place that sets anything
    only = ini_file(
        tmp_path,
        "[stockroom:solo]\ncurrency_symbol = CHF\ntax_bp = 1000\n",
        "solo.ini",
    )
    assert price_lines("--config", str(only), "--profile", "solo") == amounts("CHF", "3.75", "41.25")
    assert price_lines("--config", str(only)) == amounts("$", "0.00", "37.50")
    # keys are case-insensitive as in the base section
    mixed = ini_file(tmp_path, "[stockroom:m]\nCurrency_Symbol = YEN\n", "mixed.ini")
    assert price_lines("--config", str(mixed), "--profile", "m")[0].startswith(
        "3 x MUG-001 Blue Mug: YEN37.50"
    )


def test_profile_from_the_environment_and_precedence(tmp_path: Path) -> None:
    ini = str(ini_file(tmp_path))
    assert price_lines("--config", ini, env={"STOCKROOM_PROFILE": "dev"}) == GBP_DEV
    assert price_lines("--config", ini, env={"STOCKROOM_PROFILE": "prod-eu"}) == EUR_PROD
    # the option wins over the variable
    env = {"STOCKROOM_PROFILE": "dev"}
    assert price_lines("--config", ini, "--profile", "prod-eu", env=env) == EUR_PROD
    # an empty option switches the variable's profile off; an empty variable means no profile
    assert price_lines("--config", ini, "--profile", "", env=env) == EUR_BASE
    assert price_lines("--config", ini, env={"STOCKROOM_PROFILE": ""}) == EUR_BASE
    assert price_lines("--config", ini, "--profile", "dev", env={"STOCKROOM_PROFILE": ""}) == GBP_DEV
    # an empty profile needs no settings file, and no profile is no error
    assert run("--profile", "", *PRICE).returncode == 0
    assert run(*PRICE, env={"STOCKROOM_PROFILE": ""}).returncode == 0
    assert stock_stderr("--config", ini, env={"STOCKROOM_PROFILE": "dev"}) == ""


def test_environment_settings_beat_the_profile(tmp_path: Path) -> None:
    ini = str(ini_file(tmp_path))
    env = {"STOCKROOM_CURRENCY_SYMBOL": "CHF", "STOCKROOM_TAX_BP": "500"}
    assert price_lines("--config", ini, "--profile", "dev", env=env) == amounts("CHF", "1.88", "39.38")
    env["STOCKROOM_PROFILE"] = "prod-eu"
    assert price_lines("--config", ini, env=env) == amounts("CHF", "1.88", "39.38")
    # STOCKROOM_PROFILE alone is not an unknown setting, without a profile section or file either
    done = run(*PRICE, env={"STOCKROOM_PROFILE": ""})
    assert done.returncode == 0 and done.stderr == ""
    plain = ini_file(tmp_path, "[stockroom]\ncurrency_symbol = EUR\n", "plain.ini")
    done = run("--config", str(plain), *PRICE, env={"STOCKROOM_PROFILE": ""})
    assert done.returncode == 0


def test_profiles_that_are_not_chosen_are_not_validated(tmp_path: Path) -> None:
    content = BASE_INI + (
        "\n[stockroom:broken]\ncolour = blue\ntax_bp = lots\nlow_stock_warning = maybe\n"
        "\n[stockroom:negative]\ntax_bp = -5\n"
    )
    ini = str(ini_file(tmp_path, content))
    assert price_lines("--config", ini) == EUR_BASE
    assert price_lines("--config", ini, "--profile", "dev") == GBP_DEV
    assert price_lines("--config", ini, env={"STOCKROOM_PROFILE": "prod-eu"}) == EUR_PROD
    assert price_lines("--config", ini, "--profile", "") == EUR_BASE
    # the base section is still validated
    bad = ini_file(tmp_path, "[stockroom]\ncolour = blue\n[stockroom:dev]\ntax_bp = 1\n", "bad.ini")
    assert_one_error(run("--config", str(bad), "--profile", "dev", *PRICE), "colour")
    assert_one_error(run("--config", str(bad), *PRICE), "colour")


def test_section_names_are_matched_exactly(tmp_path: Path) -> None:
    near = """\
[stockroom]
currency_symbol = EUR

[Stockroom:dev]
currency_symbol = NEAR1

[stockroom: dev]
currency_symbol = NEAR2

[stockroom-dev]
currency_symbol = NEAR3

[stockroom:dev ]
currency_symbol = NEAR4

[stockroom:DEV]
currency_symbol = NEAR5

[stockroomdev]
currency_symbol = NEAR6
"""
    path = ini_file(tmp_path, near)
    for name in ("dev", "Dev"):
        assert_one_error(run("--config", str(path), "--profile", name, *PRICE), name)
    assert price_lines("--config", str(path), "--profile", "DEV")[0].startswith("3 x MUG-001 Blue Mug: NEAR5")
    special = """\
[stockroom:prod-eu]
currency_symbol = P1
[stockroom:a.b]
currency_symbol = P2
[stockroom:x y]
currency_symbol = P3
[stockroom:a:b]
currency_symbol = P4
[stockroom:dev]
currency_symbol = GBP
[Stockroom:dev]
currency_symbol = NEAR
"""
    path = ini_file(tmp_path, special, "special.ini")
    for name, symbol in (("prod-eu", "P1"), ("a.b", "P2"), ("x y", "P3"), ("a:b", "P4"), ("dev", "GBP")):
        head = f"3 x MUG-001 Blue Mug: {symbol}37.50"
        assert price_lines("--config", str(path), "--profile", name)[0] == head


def test_default_section_is_not_copied_into_profiles(tmp_path: Path) -> None:
    content = """\
[DEFAULT]
tax_bp = 5

[stockroom]
currency_symbol = EUR
tax_bp = 1000

[stockroom:dev]
currency_symbol = GBP

[stockroom:rate]
tax_bp = 1900

[stockroom:same]
tax_bp = 5
"""
    ini = str(ini_file(tmp_path, content))
    assert price_lines("--config", ini) == amounts("EUR", "3.75", "41.25")
    # dev does not write tax_bp, so the value of [stockroom] stays (not the [DEFAULT] one)
    assert price_lines("--config", ini, "--profile", "dev") == amounts("GBP", "3.75", "41.25")
    assert price_lines("--config", ini, "--profile", "rate") == amounts("EUR", "7.13", "44.63")
    # a profile that writes the same value as [DEFAULT] does override [stockroom]
    assert price_lines("--config", ini, "--profile", "same") == amounts("EUR", "0.02", "37.52")
    # keys of [DEFAULT] still apply to [stockroom] when it does not set them itself
    content = """\
[DEFAULT]
tax_bp = 1000

[stockroom]
currency_symbol = EUR

[stockroom:dev]
currency_symbol = GBP
"""
    ini = str(ini_file(tmp_path, content, "d2.ini"))
    assert price_lines("--config", ini) == amounts("EUR", "3.75", "41.25")
    assert price_lines("--config", ini, "--profile", "dev") == amounts("GBP", "3.75", "41.25")
    # without a [stockroom] section [DEFAULT] is ignored, as before
    content = "[DEFAULT]\ntax_bp = 1000\n\n[stockroom:dev]\ncurrency_symbol = GBP\n"
    ini = str(ini_file(tmp_path, content, "d3.ini"))
    assert price_lines("--config", ini) == amounts("$", "0.00", "37.50")
    assert price_lines("--config", ini, "--profile", "dev") == amounts("GBP", "0.00", "37.50")


def test_errors_in_the_chosen_profile(tmp_path: Path) -> None:
    ini = ini_file(
        tmp_path,
        BASE_INI
        + "\n[stockroom:badkey]\ncolour = blue\n"
        + "\n[stockroom:badnum]\ntax_bp = lots\n"
        + "\n[stockroom:negative]\ntax_bp = -5\n"
        + "\n[stockroom:badbool]\nlow_stock_warning = maybe\n"
        + "\n[stockroom:unused space]\ntax_bp = 1\n",
    )
    path = str(ini)
    assert_one_error(run("--config", path, "--profile", "badkey", *PRICE), "colour", "[stockroom:badkey]")
    assert_one_error(run("--config", path, *PRICE, env={"STOCKROOM_PROFILE": "badkey"}), "colour", "[stockroom:badkey]")
    for name in ("badnum", "negative", "badbool"):
        assert_one_error(run("--config", path, "--profile", name, *PRICE))
        assert_one_error(run("--config", path, "--profile", name, "items", "--file", "data/items.csv"))
    assert_one_error(run("--config", path, "--profile", "nope", *PRICE), "nope")
    assert_one_error(run("--config", path, "--profile", "unused", *PRICE), "unused")
    assert_one_error(run("--profile", "dev", *PRICE), "dev")
    assert_one_error(run(*PRICE, env={"STOCKROOM_PROFILE": "prod-eu"}), "prod-eu")
    assert_one_error(run("--config", str(tmp_path / "missing.ini"), "--profile", "dev", *PRICE), "missing.ini")
    # the file that defines no profiles at all
    flat = ini_file(tmp_path, "[stockroom]\ncurrency_symbol = EUR\n", "flat.ini")
    assert_one_error(run("--config", str(flat), "--profile", "dev", *PRICE), "dev")
    # an empty file
    empty = ini_file(tmp_path, "", "empty.ini")
    assert_one_error(run("--config", str(empty), "--profile", "dev", *PRICE), "dev")


def test_nothing_changes_without_profiles(tmp_path: Path) -> None:
    ini = ini_file(tmp_path, "[stockroom]\ncurrency_symbol = EUR\ntax_bp = 825\n", "plain.ini")
    assert price_lines("--config", str(ini)) == EUR_BASE
    assert price_lines(env={"STOCKROOM_TAX_BP": "1000"}) == amounts("$", "3.75", "41.25")
    bad = ini_file(tmp_path, "[stockroom]\ncolour = blue\n", "bad.ini")
    assert_one_error(run("--config", str(bad), *PRICE), "colour")
    assert_one_error(run("--config", str(tmp_path / "gone.ini"), *PRICE), "gone.ini")
    assert run("--profile").returncode == 2
    assert run("--version").stdout == "stockroom 0.4.0\n"
    assert run(*PRICE[:1]).returncode == 2
