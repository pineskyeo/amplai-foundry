import os
from pathlib import Path

import pytest

from stockroom.cli import main
from stockroom.config import load_config
from stockroom.errors import ConfigError


@pytest.fixture(autouse=True)
def _clean_stockroom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("STOCKROOM_"):
            monkeypatch.delenv(name)


DATA = Path(__file__).resolve().parents[3] / "data"
KEY = "STOCKROOM_LOW_STOCK_WARNING"
FALSE_WORDS = ["false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF", "0"]
TRUE_WORDS = ["true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON", "1"]
BAD_WORDS = ["maybe", "2", "y", "t", "n", "f", "", "  ", "enabled", "nope", "-1", "truee", "yes please"]


def _ini(tmp_path: Path, value: str, name: str = "stockroom.ini") -> Path:
    path = tmp_path / name
    path.write_text(f"[stockroom]\nlow_stock_warning = {value}\n")
    return path


def test_environment_words_for_false_turn_the_setting_off() -> None:
    for raw in FALSE_WORDS:
        assert load_config(env={KEY: raw})["low_stock_warning"] is False, raw


def test_environment_words_for_true_keep_the_setting_on() -> None:
    for raw in TRUE_WORDS:
        assert load_config(env={KEY: raw})["low_stock_warning"] is True, raw


def test_the_environment_overrides_the_settings_file_in_both_directions(tmp_path: Path) -> None:
    on, off = _ini(tmp_path, "yes", "on.ini"), _ini(tmp_path, "no", "off.ini")
    assert load_config(on, {KEY: "false"})["low_stock_warning"] is False
    assert load_config(off, {KEY: "true"})["low_stock_warning"] is True
    assert load_config(off, {KEY: "off"})["low_stock_warning"] is False
    assert load_config(on, {})["low_stock_warning"] is True
    assert load_config(off, {})["low_stock_warning"] is False


def test_words_that_are_not_booleans_are_refused() -> None:
    for raw in BAD_WORDS:
        with pytest.raises(ConfigError):
            load_config(env={KEY: raw})


def test_surrounding_whitespace_is_ignored() -> None:
    assert load_config(env={KEY: " false "})["low_stock_warning"] is False
    assert load_config(env={KEY: "\tno\n"})["low_stock_warning"] is False
    assert load_config(env={KEY: " Yes"})["low_stock_warning"] is True
    assert load_config(env={KEY: "1 "})["low_stock_warning"] is True


def test_the_environment_accepts_exactly_the_words_the_settings_file_accepts(
    tmp_path: Path,
) -> None:
    for raw in [*FALSE_WORDS, *TRUE_WORDS, *BAD_WORDS]:
        stripped = raw.strip()
        try:
            from_file = load_config(_ini(tmp_path, stripped))["low_stock_warning"]
        except ConfigError:
            with pytest.raises(ConfigError):
                load_config(env={KEY: raw})
        else:
            assert load_config(env={KEY: raw})["low_stock_warning"] is from_file, raw


def test_other_settings_and_unrelated_variables_are_unaffected() -> None:
    config = load_config(env={KEY: "no", "STOCKROOM_TAX_BP": "825", "STOCKROOM_CURRENCY_SYMBOL": "EUR "})
    assert config["low_stock_warning"] is False
    assert config["tax_bp"] == 825 and config["currency_symbol"] == "EUR "
    assert load_config(env={"STOCKROOM_LOW_STOCK": "false", "OTHER": "no"})["low_stock_warning"]
    with pytest.raises(ConfigError):
        load_config(env={"STOCKROOM_TAX_BP": "lots"})


def test_the_stock_command_follows_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["stock", "--items", str(DATA / "items.csv"), "--stock", str(DATA / "stock.csv")]
    monkeypatch.setenv(KEY, "false")
    assert main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "LOW MUG-002" in captured.out
    monkeypatch.setenv(KEY, "OFF")
    assert main(args) == 0
    assert capsys.readouterr().err == ""
    monkeypatch.setenv(KEY, "yes")
    assert main(args) == 0
    assert "3 item(s) below reorder level" in capsys.readouterr().err
    monkeypatch.setenv(KEY, "sometimes")
    assert main(args) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("stockroom: error: ") and captured.out == ""
