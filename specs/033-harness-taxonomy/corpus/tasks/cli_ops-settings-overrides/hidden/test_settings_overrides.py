from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ITEMS = str(ROOT / "data" / "items.csv")
STOCK = str(ROOT / "data" / "stock.csv")
PRICE = ("price", "--items", ITEMS, "--sku", "MUG-001", "--quantity", "3")
STOCK_CMD = ("stock", "--items", ITEMS, "--stock", STOCK)


def run(
    *args: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd or ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def money(cents: int, symbol: str = "$") -> str:
    return f"{symbol}{cents // 100:,}.{cents % 100:02d}"


def price_lines(bp: int, symbol: str = "$") -> list[str]:
    tax = (3750 * bp + 5000) // 10000
    return [
        f"3 x MUG-001 Blue Mug: {money(3750, symbol)}",
        f"tax: {money(tax, symbol)}",
        f"total: {money(3750 + tax, symbol)}",
    ]


def ok(done: subprocess.CompletedProcess[str]) -> list[str]:
    assert done.returncode == 0, (done.returncode, done.stderr)
    return done.stdout.splitlines()


def assert_error(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("stockroom: error: "), done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def assert_usage(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    for needle in needles:
        assert needle in done.stderr, (needle, done.stderr)


def ini(path: Path, **values: str) -> Path:
    body = "".join(f"{key} = {value}\n" for key, value in values.items())
    path.write_text("[stockroom]\n" + body, encoding="utf-8")
    return path


def test_set_overrides_settings_of_every_command(tmp_path: Path) -> None:
    assert ok(run("--set", "tax_bp=1000", *PRICE)) == price_lines(1000)
    assert ok(run("--set", "currency_symbol=EUR", "--set", "tax_bp=825", *PRICE)) == price_lines(
        825, "EUR"
    )
    # the option beats the settings file and the environment
    settings = ini(tmp_path / "s.ini", tax_bp="200", currency_symbol="GBP")
    done = run(
        "--config", str(settings), "--set", "tax_bp=10", *PRICE, env={"STOCKROOM_TAX_BP": "300"}
    )
    assert ok(done) == price_lines(10, "GBP")
    # booleans are read like the settings file reads them
    for word in ("off", "OFF", "No", "false", "0"):
        done = run("--set", f"low_stock_warning={word}", *STOCK_CMD)
        assert done.returncode == 0 and done.stderr == "", (word, done.stderr)
    quiet = ini(tmp_path / "quiet.ini", low_stock_warning="off")
    for word in ("on", "Yes", "TRUE", "1"):
        done = run("--config", str(quiet), "--set", f"low_stock_warning={word}", *STOCK_CMD)
        assert done.returncode == 0
        assert "3 item(s) below reorder level" in done.stderr, word
    # data_dir is a setting too
    assert ok(run("--set", "data_dir=/srv/x", *PRICE)) == price_lines(0)
    done = run(
        "--set",
        "low_stock_warning=no",
        "--set",
        "currency_symbol=€",
        "sales",
        "--orders",
        str(ROOT / "data" / "orders.csv"),
    )
    assert ok(done)[:2] == ["5 orders, 1 cancelled", "Revenue: €149.65"]


def test_last_set_wins_and_an_empty_value_cancels(tmp_path: Path) -> None:
    assert ok(run("--set", "tax_bp=100", "--set", "tax_bp=1000", *PRICE)) == price_lines(1000)
    assert ok(run("--set", "tax_bp=1000", "--set", "tax_bp=100", *PRICE)) == price_lines(100)
    # an empty value cancels the earlier --set options of the key only
    assert ok(run("--set", "tax_bp=1000", "--set", "tax_bp=", *PRICE)) == price_lines(0)
    done = run("--set", "tax_bp=1000", "--set", "currency_symbol=EUR", "--set", "tax_bp=", *PRICE)
    assert ok(done) == price_lines(0, "EUR")
    assert ok(run("--set", "tax_bp=1", "--set", "tax_bp=", "--set", "tax_bp=500", *PRICE)) == (
        price_lines(500)
    )
    # after the cancel the weaker sources decide again
    settings = ini(tmp_path / "s.ini", tax_bp="200", currency_symbol="GBP")
    done = run("--config", str(settings), "--set", "currency_symbol=", *PRICE)
    assert ok(done) == price_lines(200, "GBP")
    done = run("--set", "tax_bp=900", "--set", "tax_bp=", *PRICE, env={"STOCKROOM_TAX_BP": "300"})
    assert ok(done) == price_lines(300)
    # an empty currency symbol therefore cannot come from --set
    assert ok(run("--set", "currency_symbol=", *PRICE)) == price_lines(0)


def test_set_text_values_are_taken_exactly() -> None:
    assert ok(run("--set", "currency_symbol=a=b", *PRICE)) == price_lines(0, "a=b")
    assert ok(run("--set", "currency_symbol= € ", *PRICE)) == price_lines(0, " € ")
    assert ok(run("--set", "currency_symbol=EUR ", *PRICE)) == price_lines(0, "EUR ")
    assert ok(run("--set=currency_symbol=kr", *PRICE)) == price_lines(0, "kr")
    assert ok(run("--set", "currency_symbol==", *PRICE)) == price_lines(0, "=")


def test_malformed_set_options_are_usage_errors() -> None:
    for bad in ("tax_bp", "TAX_BP=5", "colour=blue", "=5", "tax-bp=5", " tax_bp=5", "Tax_bp="):
        assert_usage(run("--set", bad, *PRICE), bad)
    assert_usage(run(*PRICE, "--set", "tax_bp=5"))
    assert_usage(run("--set"))
    assert_usage(run("--set", "tax_bp=5"))


def test_bad_set_values_are_data_errors_only_when_used() -> None:
    assert_error(run("--set", "tax_bp=abc", *PRICE), "tax_bp")
    assert_error(run("--set", "tax_bp=-3", *PRICE), "tax_bp")
    assert_error(run("--set", "tax_bp=1.5", *PRICE), "tax_bp")
    assert_error(run("--set", "low_stock_warning=maybe", *STOCK_CMD), "low_stock_warning")
    assert_error(
        run("--set", "low_stock_warning=", "--set", "low_stock_warning=2", *STOCK_CMD),
        "low_stock_warning",
    )
    assert_error(run("--set", "tax_bp=100", "--set", "tax_bp=abc", *PRICE), "tax_bp")
    # only the value that is finally used is converted
    assert ok(run("--set", "tax_bp=abc", "--set", "tax_bp=100", *PRICE)) == price_lines(100)
    assert ok(run("--set", "tax_bp=abc", "--set", "tax_bp=", *PRICE)) == price_lines(0)
    done = run("--set", "low_stock_warning=maybe", "--set", "low_stock_warning=off", *STOCK_CMD)
    assert done.returncode == 0 and done.stderr == ""
    # an environment value is not read for a key that --set decides
    bad_env = {"STOCKROOM_TAX_BP": "lots"}
    assert ok(run("--set", "tax_bp=100", *PRICE, env=bad_env)) == price_lines(100)
    assert_error(run(*PRICE, env=bad_env), "tax_bp")
    assert_error(run("--set", "tax_bp=100", "--set", "tax_bp=", *PRICE, env=bad_env), "tax_bp")


def test_empty_environment_variables_count_as_unset(tmp_path: Path) -> None:
    assert ok(run(*PRICE, env={"STOCKROOM_TAX_BP": ""})) == price_lines(0)
    assert ok(run(*PRICE, env={"STOCKROOM_CURRENCY_SYMBOL": ""})) == price_lines(0)
    settings = ini(tmp_path / "s.ini", tax_bp="200", currency_symbol="EUR")
    env = {"STOCKROOM_TAX_BP": "", "STOCKROOM_CURRENCY_SYMBOL": ""}
    assert ok(run("--config", str(settings), *PRICE, env=env)) == price_lines(200, "EUR")
    assert ok(run(*PRICE, env={"STOCKROOM_TAX_BP": "300"})) == price_lines(300)


def test_settings_file_from_the_environment(tmp_path: Path) -> None:
    a = ini(tmp_path / "a.ini", tax_bp="200")
    b = ini(tmp_path / "b.ini", tax_bp="250", currency_symbol="CHF")
    assert ok(run(*PRICE, env={"STOCKROOM_CONFIG": str(b)})) == price_lines(250, "CHF")
    # --config wins over the variable and the two files are not merged
    assert ok(run("--config", str(a), *PRICE, env={"STOCKROOM_CONFIG": str(b)})) == (
        price_lines(200)
    )
    # an empty --config means no settings file, also over the variable
    assert ok(run("--config", "", *PRICE, env={"STOCKROOM_CONFIG": str(b)})) == price_lines(0)
    assert ok(run("--config", "", *PRICE)) == price_lines(0)
    assert ok(run(*PRICE, env={"STOCKROOM_CONFIG": ""})) == price_lines(0)
    # a relative path is relative to the current directory
    assert ok(run(*PRICE, env={"STOCKROOM_CONFIG": "b.ini"}, cwd=tmp_path)) == (
        price_lines(250, "CHF")
    )
    # the file named by the variable is read and checked like a --config file
    assert_error(run(*PRICE, env={"STOCKROOM_CONFIG": "missing.ini"}, cwd=tmp_path), "missing.ini")
    bad = tmp_path / "bad.ini"
    bad.write_text("[stockroom]\ncolour = blue\n", encoding="utf-8")
    assert_error(run(*PRICE, env={"STOCKROOM_CONFIG": str(bad)}), "colour")
    assert ok(run("--config", str(a), *PRICE, env={"STOCKROOM_CONFIG": str(bad)})) == (
        price_lines(200)
    )
    done = run("--config", str(a), *STOCK_CMD, env={"STOCKROOM_CONFIG": str(bad)})
    assert done.returncode == 0


def test_settings_source_precedence_matrix(tmp_path: Path) -> None:
    a = ini(tmp_path / "a.ini", tax_bp="200")
    b = ini(tmp_path / "b.ini", tax_bp="250")
    config_cases = {
        "none": ([], {}, 0),
        "option": (["--config", str(a)], {}, 200),
        "variable": ([], {"STOCKROOM_CONFIG": str(b)}, 250),
        "both": (["--config", str(a)], {"STOCKROOM_CONFIG": str(b)}, 200),
        "empty-option": (["--config", ""], {"STOCKROOM_CONFIG": str(b)}, 0),
    }
    env_cases = {
        "absent": ({}, None),
        "empty": ({"STOCKROOM_TAX_BP": ""}, None),
        "set": ({"STOCKROOM_TAX_BP": "300"}, 300),
    }
    set_cases = {
        "none": ([], None),
        "set": (["--set", "tax_bp=400"], 400),
        "cancelled": (["--set", "tax_bp=400", "--set", "tax_bp="], None),
        "twice": (["--set", "tax_bp=450", "--set", "tax_bp=400"], 400),
    }
    for config_name, (config_args, config_env, from_file) in config_cases.items():
        for env_name, (env, from_env) in env_cases.items():
            for set_name, (set_args, from_set) in set_cases.items():
                if from_set is not None:
                    want = from_set
                elif from_env is not None:
                    want = from_env
                else:
                    want = from_file
                done = run(*config_args, *set_args, *PRICE, env={**config_env, **env})
                assert done.returncode == 0, (config_name, env_name, set_name, done.stderr)
                assert done.stdout.splitlines() == price_lines(want), (
                    config_name,
                    env_name,
                    set_name,
                    done.stdout,
                )


def test_runs_without_the_new_options_are_unchanged(tmp_path: Path) -> None:
    assert ok(run(*PRICE)) == price_lines(0)
    settings = ini(tmp_path / "s.ini", tax_bp="825", currency_symbol="EUR")
    assert ok(run("--config", str(settings), *PRICE)) == price_lines(825, "EUR")
    assert ok(run("--config", str(settings), *PRICE, env={"STOCKROOM_TAX_BP": "1000"})) == (
        price_lines(1000, "EUR")
    )
    assert_error(run("--config", str(tmp_path / "nope.ini"), *PRICE), "nope.ini")
    assert run("--version").stdout == "stockroom 0.4.0\n"
    assert run("items").returncode == 2
    done = run(*STOCK_CMD)
    assert done.returncode == 0 and "3 item(s) below reorder level" in done.stderr
