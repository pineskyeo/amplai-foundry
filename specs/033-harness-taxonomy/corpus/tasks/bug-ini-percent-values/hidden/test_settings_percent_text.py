import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from stockroom.config import load_config
from stockroom.errors import ConfigError

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


PRICE = ["price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "3"]


def ini(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "settings.ini"
    path.write_text("[stockroom]\n" + body, encoding="utf-8")
    return path


def test_a_percent_sign_as_currency_symbol_is_printed(tmp_path: Path) -> None:
    path = ini(tmp_path, "currency_symbol = %\n")
    done = run("--config", str(path), *PRICE)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "3 x MUG-001 Blue Mug: %37.50",
        "tax: %0.00",
        "total: %37.50",
    ]
    assert done.stderr == ""
    path = ini(tmp_path, "currency_symbol = %%\ntax_bp = 1000\n")
    done = run("--config", str(path), *PRICE)
    assert done.stdout.splitlines() == [
        "3 x MUG-001 Blue Mug: %%37.50",
        "tax: %%3.75",
        "total: %%41.25",
    ]


def test_percent_text_is_kept_literally(tmp_path: Path) -> None:
    path = ini(
        tmp_path,
        "currency_symbol = %(tax_bp)s\ntax_bp = 825\ndata_dir = /srv/100%/%%data%(x)s\n",
    )
    config = load_config(path)
    assert config["currency_symbol"] == "%(tax_bp)s"
    assert config["data_dir"] == "/srv/100%/%%data%(x)s"
    assert config["tax_bp"] == 825
    path = ini(tmp_path, "currency_symbol = 50% off \n")
    assert load_config(path)["currency_symbol"] == "50% off"
    path = ini(tmp_path, "data_dir = %(missing)s\n")
    assert load_config(path)["data_dir"] == "%(missing)s"


def test_percent_in_numbers_and_switches_is_a_settings_error(tmp_path: Path) -> None:
    for body in (
        "low_stock_warning = 100%\n",
        "low_stock_warning = %(x)s\n",
        "low_stock_warning = yes%\n",
        "tax_bp = 8%\n",
        "tax_bp = %(data_dir)s\n",
        "tax_bp = %%\n",
    ):
        path = ini(tmp_path, body)
        with pytest.raises(ConfigError):
            load_config(path)
        done = run("--config", str(path), *PRICE)
        assert done.returncode == 1, (body, done.stderr)
        assert done.stdout == ""
        assert "Traceback" not in done.stderr
        lines = done.stderr.splitlines()
        assert len(lines) == 1 and lines[0].startswith("stockroom: error: ")


def test_other_settings_behave_as_before(tmp_path: Path) -> None:
    path = ini(tmp_path, "currency_symbol = %\nlow_stock_warning = Off\ntax_bp = 10\n")
    config = load_config(path, {"STOCKROOM_CURRENCY_SYMBOL": "EUR"})
    assert config == {
        "currency_symbol": "EUR",
        "low_stock_warning": False,
        "tax_bp": 10,
        "data_dir": "data",
    }
    path = ini(tmp_path, "currency_symbol = %\n")
    assert load_config(path, {"STOCKROOM_DATA_DIR": "%(x)s%"})["data_dir"] == "%(x)s%"
    path = ini(tmp_path, "currency_symbol = %\ncolour = 5%\n")
    with pytest.raises(ConfigError):
        load_config(path)
    done = run(
        "--config",
        str(ini(tmp_path, "currency_symbol = %\n")),
        *PRICE,
        env={"STOCKROOM_CURRENCY_SYMBOL": "$"},
    )
    assert done.stdout.splitlines()[0] == "3 x MUG-001 Blue Mug: $37.50"


def test_generated_text_values_come_back_unchanged(tmp_path: Path) -> None:
    rng = random.Random(8080)
    alphabet = "%%%()sdx$€a ;#:=-/"
    for number in range(400):
        values = {}
        for key in ("currency_symbol", "data_dir"):
            inner = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 10)))
            values[key] = inner.strip()
        body = "".join(f"{key} = {value}\n" for key, value in values.items())
        path = tmp_path / f"s{number}.ini"
        path.write_text("[stockroom]\n" + body, encoding="utf-8")
        config = load_config(path)
        assert config["currency_symbol"] == values["currency_symbol"], body
        assert config["data_dir"] == values["data_dir"], body
