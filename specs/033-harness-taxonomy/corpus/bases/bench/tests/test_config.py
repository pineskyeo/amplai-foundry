from pathlib import Path

import pytest

from stockroom.config import DEFAULTS, load_config
from stockroom.errors import ConfigError


def test_defaults() -> None:
    assert load_config() == DEFAULTS
    assert load_config(env={"HOME": "/x", "STOCKROOM_UNKNOWN": "1"}) == DEFAULTS


def test_file_values(tmp_path: Path) -> None:
    path = tmp_path / "stockroom.ini"
    path.write_text("[stockroom]\ncurrency_symbol = EUR \ntax_bp = 825\nlow_stock_warning = no\n")
    config = load_config(path)
    assert config["currency_symbol"] == "EUR"
    assert config["tax_bp"] == 825
    assert config["low_stock_warning"] is False
    assert config["data_dir"] == "data"


def test_env_overrides_file(tmp_path: Path) -> None:
    path = tmp_path / "stockroom.ini"
    path.write_text("[stockroom]\ntax_bp = 825\n")
    config = load_config(path, {"STOCKROOM_TAX_BP": "1000", "STOCKROOM_DATA_DIR": "/srv/data"})
    assert config["tax_bp"] == 1000 and config["data_dir"] == "/srv/data"


def test_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.ini")
    bad = tmp_path / "bad.ini"
    bad.write_text("[stockroom]\ncolour = blue\n")
    with pytest.raises(ConfigError):
        load_config(bad)
    bad.write_text("[stockroom]\ntax_bp = lots\n")
    with pytest.raises(ConfigError):
        load_config(bad)
    with pytest.raises(ConfigError):
        load_config(env={"STOCKROOM_TAX_BP": "-5"})
