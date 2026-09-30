from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from stockroom.config import DEFAULTS, load_config

ROOT = Path(__file__).resolve().parents[3]

DEFAULT_LINES = [
    "currency_symbol = $",
    "data_dir = data",
    "low_stock_warning = true",
    "tax_bp = 0",
]


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


def settings_file(tmp_path: Path) -> Path:
    path = tmp_path / "s.ini"
    path.write_text(
        "[stockroom]\n"
        "currency_symbol = EUR\n"
        "tax_bp = 825\n"
        "low_stock_warning = no\n"
        "data_dir = data\n",
        encoding="utf-8",
    )
    return path


def lines(done: subprocess.CompletedProcess[str]) -> list[str]:
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.endswith("\n")
    return done.stdout.splitlines()


def test_defaults_are_listed_sorted(tmp_path: Path) -> None:
    assert lines(run("config")) == DEFAULT_LINES
    assert lines(run("config", env={"HOME": "/x", "STOCKROOM_UNKNOWN": "1", "OTHER_TAX_BP": "9"})) == (
        DEFAULT_LINES
    )
    empty = tmp_path / "empty.ini"
    empty.write_text("", encoding="utf-8")
    assert lines(run("--config", str(empty), "config")) == DEFAULT_LINES
    only_other = tmp_path / "other.ini"
    only_other.write_text("[other]\ntax_bp = 5\n", encoding="utf-8")
    assert lines(run("--config", str(only_other), "config")) == DEFAULT_LINES


def test_file_and_environment_values_win_in_order(tmp_path: Path) -> None:
    ini = settings_file(tmp_path)
    assert lines(run("--config", str(ini), "config")) == [
        "currency_symbol = EUR",
        "data_dir = data",
        "low_stock_warning = false",
        "tax_bp = 825",
    ]
    env = {"STOCKROOM_TAX_BP": "1000", "STOCKROOM_DATA_DIR": "/srv/data", "STOCKROOM_UNKNOWN": "x"}
    assert lines(run("--config", str(ini), "config", env=env)) == [
        "currency_symbol = EUR",
        "data_dir = /srv/data",
        "low_stock_warning = false",
        "tax_bp = 1000",
    ]
    assert lines(run("config", env={"STOCKROOM_LOW_STOCK_WARNING": "yes", "STOCKROOM_TAX_BP": "7"})) == [
        "currency_symbol = $",
        "data_dir = data",
        "low_stock_warning = true",
        "tax_bp = 7",
    ]
    # the option may also come after the command name's own options
    assert lines(run("--config", str(ini), "config", "--origin"))[0].startswith("currency_symbol = EUR")


def test_origin_of_each_setting(tmp_path: Path) -> None:
    assert lines(run("config", "--origin")) == [f"{line} (default)" for line in DEFAULT_LINES]
    ini = settings_file(tmp_path)
    assert lines(run("--config", str(ini), "config", "--origin")) == [
        "currency_symbol = EUR (file)",
        "data_dir = data (file)",
        "low_stock_warning = false (file)",
        "tax_bp = 825 (file)",
    ]
    env = {
        "STOCKROOM_TAX_BP": "1000",
        "STOCKROOM_DATA_DIR": "/srv/data",
        "STOCKROOM_CURRENCY_SYMBOL": "EUR",
        "STOCKROOM_OTHER": "1",
        "TAX_BP": "3",
    }
    assert lines(run("--config", str(ini), "config", "--origin", env=env)) == [
        "currency_symbol = EUR (env)",
        "data_dir = /srv/data (env)",
        "low_stock_warning = false (file)",
        "tax_bp = 1000 (env)",
    ]
    # an environment value equal to the default is still 'env'; no file: the rest is 'default'
    env = {"STOCKROOM_TAX_BP": "0", "STOCKROOM_CURRENCY_SYMBOL": "$"}
    assert lines(run("config", "--origin", env=env)) == [
        "currency_symbol = $ (env)",
        "data_dir = data (default)",
        "low_stock_warning = true (default)",
        "tax_bp = 0 (env)",
    ]
    # a settings file that sets only some keys
    part = tmp_path / "part.ini"
    part.write_text("[stockroom]\ntax_bp = 0\n", encoding="utf-8")
    assert lines(run("--config", str(part), "config", "--origin")) == [
        "currency_symbol = $ (default)",
        "data_dir = data (default)",
        "low_stock_warning = true (default)",
        "tax_bp = 0 (file)",
    ]


def test_single_key_and_unknown_key(tmp_path: Path) -> None:
    ini = settings_file(tmp_path)
    assert lines(run("--config", str(ini), "config", "tax_bp")) == ["tax_bp = 825"]
    assert lines(run("config", "tax_bp", "--origin", env={"STOCKROOM_TAX_BP": "5"})) == [
        "tax_bp = 5 (env)"
    ]
    assert lines(run("--config", str(ini), "config", "--origin", "low_stock_warning")) == [
        "low_stock_warning = false (file)"
    ]
    assert lines(run("config", "currency_symbol")) == ["currency_symbol = $"]
    for key in ("colour", "TAX_BP", "tax-bp", "tax_bp ", "stockroom"):
        done = run("config", key)
        assert done.returncode == 1, (key, done.stderr)
        assert done.stdout == ""
        err = done.stderr.splitlines()
        assert len(err) == 1 and err[0].startswith("stockroom: error: ")
        assert key in err[0]
        assert "Traceback" not in done.stderr
    done = run("config", "--json", "nope", "--origin")
    assert done.returncode == 1 and done.stdout == ""


def dump(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def test_json_output(tmp_path: Path) -> None:
    ini = settings_file(tmp_path)
    env = {"STOCKROOM_TAX_BP": "1000"}
    done = run("--config", str(ini), "config", "--json", env=env)
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout == dump(
        {"currency_symbol": "EUR", "data_dir": "data", "low_stock_warning": False, "tax_bp": 1000}
    )
    done = run("--config", str(ini), "config", "--json", "--origin", env=env)
    assert done.stdout == dump(
        {
            "currency_symbol": {"value": "EUR", "origin": "file"},
            "data_dir": {"value": "data", "origin": "file"},
            "low_stock_warning": {"value": False, "origin": "file"},
            "tax_bp": {"value": 1000, "origin": "env"},
        }
    )
    assert run("config", "--json").stdout == dump(DEFAULTS)
    assert run("config", "tax_bp", "--json", env=env).stdout == "1000\n"
    assert run("config", "currency_symbol", "--json").stdout == '"$"\n'
    assert run("--config", str(ini), "config", "low_stock_warning", "--json").stdout == "false\n"
    assert run("config", "low_stock_warning", "--json").stdout == "true\n"
    done = run("config", "tax_bp", "--json", "--origin", env=env)
    assert done.stdout == dump({"value": 1000, "origin": "env"})
    done = run("--config", str(ini), "config", "data_dir", "--origin", "--json")
    assert done.stdout == dump({"value": "data", "origin": "file"})
    # text is still plain without --json, and tax_bp is a number, not a string
    assert json.loads(run("config", "--json", env=env).stdout)["tax_bp"] == 1000


def test_bad_settings_end_the_command(tmp_path: Path) -> None:
    bad_key = tmp_path / "bad_key.ini"
    bad_key.write_text("[stockroom]\ncolour = blue\n", encoding="utf-8")
    bad_value = tmp_path / "bad_value.ini"
    bad_value.write_text("[stockroom]\ntax_bp = lots\n", encoding="utf-8")
    bad_bool = tmp_path / "bad_bool.ini"
    bad_bool.write_text("[stockroom]\nlow_stock_warning = maybe\n", encoding="utf-8")
    cases = [
        (["--config", str(tmp_path / "missing.ini"), "config"], {}),
        (["--config", str(bad_key), "config"], {}),
        (["--config", str(bad_value), "config", "--json"], {}),
        (["--config", str(bad_bool), "config", "tax_bp"], {}),
        (["config", "--origin"], {"STOCKROOM_TAX_BP": "-5"}),
        (["config"], {"STOCKROOM_TAX_BP": "many"}),
    ]
    for argv, env in cases:
        done = run(*argv, env=env)
        assert done.returncode == 1, (argv, done.stderr)
        assert done.stdout == ""
        err = done.stderr.splitlines()
        assert len(err) == 1 and err[0].startswith("stockroom: error: "), done.stderr


def test_other_commands_and_the_loader_are_unchanged(tmp_path: Path) -> None:
    ini = settings_file(tmp_path)
    assert load_config(ini, {"STOCKROOM_TAX_BP": "1000"}) == {
        "currency_symbol": "EUR",
        "low_stock_warning": False,
        "tax_bp": 1000,
        "data_dir": "data",
    }
    assert load_config() == DEFAULTS
    done = run("--config", str(ini), "price", "--items", "data/items.csv", "--sku", "MUG-001",
               "--quantity", "3")
    assert done.stdout.splitlines() == [
        "3 x MUG-001 Blue Mug: EUR37.50",
        "tax: EUR3.09",
        "total: EUR40.59",
    ]
    done = run("--config", str(ini), "stock", "--items", "data/items.csv", "--stock",
               "data/stock.csv")
    assert done.returncode == 0 and done.stderr == ""
    done = run("items", "--file", "data/items.csv", "--tag", "tea")
    assert [ln.split()[0] for ln in done.stdout.splitlines()[2:]] == ["TEA-010", "TEA-011"]
    assert run("--version").stdout == "stockroom 0.4.0\n"
    assert run("config", "--bogus").returncode == 2
    assert run("config", "a", "b").returncode == 2
