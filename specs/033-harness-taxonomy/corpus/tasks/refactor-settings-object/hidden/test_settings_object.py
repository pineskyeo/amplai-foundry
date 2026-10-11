from __future__ import annotations

import ast
import configparser
import dataclasses
import inspect
import os
import random
import subprocess
import sys
import types
import typing
import warnings
from pathlib import Path

import pytest
import stockroom
from stockroom import cli, config
from stockroom.errors import ConfigError

PACKAGE = Path(stockroom.__file__).resolve().parent
ROOT = PACKAGE.parent
NAMES = ("currency_symbol", "low_stock_warning", "tax_bp", "data_dir")
OLD_MESSAGE = "load_config() is deprecated; use load_settings()"


# -- the settings loader as it was before the change (the oracle) -------------------------------

_ORACLE_DEFAULTS = {
    "currency_symbol": "$",
    "low_stock_warning": True,
    "tax_bp": 0,
    "data_dir": "data",
}


def _oracle_coerce(key, raw):
    default = _ORACLE_DEFAULTS[key]
    if isinstance(default, bool):
        return bool(raw)
    if isinstance(default, int):
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a whole number, not {raw!r}") from exc
        if value < 0:
            raise ConfigError(f"{key} must not be negative")
        return value
    return raw


def _oracle_from_file(path):
    parser = configparser.ConfigParser()
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not parser.has_section("stockroom"):
        return {}
    values = {}
    for key in parser["stockroom"]:
        if key not in _ORACLE_DEFAULTS:
            raise ConfigError(f"unknown setting {key!r} in {path}")
        if isinstance(_ORACLE_DEFAULTS[key], bool):
            try:
                values[key] = parser.getboolean("stockroom", key)
            except ValueError as exc:
                raise ConfigError(f"{key} must be a boolean") from exc
        else:
            values[key] = _oracle_coerce(key, parser.get("stockroom", key))
    return values


def _oracle_load(path, env):
    result = dict(_ORACLE_DEFAULTS)
    if path is not None:
        result.update(_oracle_from_file(path))
    for key in _ORACLE_DEFAULTS:
        raw = (env or {}).get("STOCKROOM_" + key.upper())
        if raw is not None:
            result[key] = _oracle_coerce(key, raw)
    return result


def _typed(values: dict) -> list:
    return [(key, type(values[key]).__name__, values[key]) for key in sorted(values)]


def oracle_outcome(path, env):
    try:
        return ("ok", _typed(_oracle_load(path, env)))
    except ConfigError as exc:
        return ("error", str(exc))


def new_outcome(path, env):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            settings = config.load_settings(path, env)
        except ConfigError as exc:
            result = ("error", str(exc))
        else:
            assert type(settings) is config.Settings
            result = ("ok", _typed(dataclasses.asdict(settings)))
    assert [str(w.message) for w in caught] == []
    return result


# -- generated settings sources ------------------------------------------------------------------

INT_VALUES = ["0", "825", " 7", "+7", "1_000", "-0", "-5", "lots", "", "7.5", "٣", "12 ; note"]
BOOL_VALUES = ["on", "off", "yes", "No", "true", "FALSE", "1", "0", "maybe", ""]
TEXT_VALUES = ["EUR", "£", "", "GBP ", "a b", "x=y", "$"]
ENV_VALUES = {
    "STOCKROOM_CURRENCY_SYMBOL": ["EUR ", "", "¥", " $ "],
    "STOCKROOM_TAX_BP": ["0", "825", " 9 ", "+3", "2_5", "-0", "-1", "x", "", "١٢"],
    "STOCKROOM_DATA_DIR": ["/srv/data", "", "rel/dir"],
    "STOCKROOM_LOW_STOCK_WARNING": ["true", "yes", "on", "1", "TRUE", "On"],
    "STOCKROOM_COLOUR": ["blue"],
    "stockroom_tax_bp": ["x"],
    "HOME": ["/home/x"],
}


def _key(rng: random.Random) -> str:
    key = rng.choice((*NAMES, "colour", "currency", "taxbp"))
    return rng.choice([key, key, key.upper(), key.title()])


def _value(rng: random.Random, key: str) -> str:
    lowered = key.lower()
    if lowered == "tax_bp":
        return rng.choice(INT_VALUES)
    if lowered == "low_stock_warning":
        return rng.choice(BOOL_VALUES)
    return rng.choice(TEXT_VALUES)


def make_ini(rng: random.Random) -> str:
    lines: list[str] = []
    if rng.random() < 0.04:
        lines.append("tax_bp = 1")
    sections = rng.sample(
        ["stockroom", "stockroom", "DEFAULT", "STOCKROOM", "other"], rng.randrange(1, 4)
    )
    for section in sections:
        lines.append(f"[{section}]")
        for _ in range(rng.randrange(0, 5)):
            roll = rng.random()
            if roll < 0.08:
                lines.append(rng.choice(["# note", "; note", ""]))
                continue
            key = _key(rng)
            separator = rng.choice([" = ", "=", ": ", " : "])
            lines.append(f"{key}{separator}{_value(rng, key)}")
            if roll > 0.95:
                lines.append(rng.choice(["  7", "    more"]))
    return "\n".join(lines) + rng.choice(["\n", ""])


def make_env(rng: random.Random):
    if rng.random() < 0.2:
        return None
    env = {}
    for name, values in ENV_VALUES.items():
        if rng.random() < 0.35:
            env[name] = rng.choice(values)
    return types.MappingProxyType(env) if rng.random() < 0.2 else env


def test_generated_settings_sources_load_as_before(tmp_path: Path) -> None:
    rng = random.Random(20261011)
    path = tmp_path / "generated.ini"
    seen = {"ok": 0, "error": 0}
    for _ in range(1500):
        use_file = rng.random() < 0.85
        if use_file:
            path.write_text(make_ini(rng), encoding="utf-8")
        env = make_env(rng)
        expected = oracle_outcome(path if use_file else None, env)
        assert new_outcome(path if use_file else None, env) == expected, (
            path.read_text(encoding="utf-8") if use_file else None,
            env,
        )
        seen[expected[0]] += 1
    assert seen["ok"] > 300 and seen["error"] > 300


EDGE_CASES = {
    "nothing": (None, None),
    "empty-env": (None, {}),
    "key-case-in-file": ("[stockroom]\nTax_BP = 5\nCURRENCY_SYMBOL = EUR\n", None),
    "default-section-keys": ("[DEFAULT]\ndata_dir = /x\n[stockroom]\ntax_bp = 3\n", None),
    "default-section-unknown-key-after-bad-value": (
        "[DEFAULT]\ncolour = blue\n[stockroom]\ntax_bp = lots\n",
        None,
    ),
    "default-section-without-own-section": ("[DEFAULT]\ncolour = blue\n", None),
    "file-key-order-bad-value-first": ("[stockroom]\ntax_bp = lots\ncolour = blue\n", None),
    "file-key-order-unknown-first": ("[stockroom]\ncolour = blue\ntax_bp = lots\n", None),
    "other-section-only": ("[other]\ncolour = blue\n", None),
    "upper-section-name": ("[STOCKROOM]\ncolour = blue\n", None),
    "no-section-header": ("tax_bp = 4\n", None),
    "duplicate-key": ("[stockroom]\ntax_bp = 1\nTAX_BP = 2\n", None),
    "empty-values": ("[stockroom]\ncurrency_symbol =\ndata_dir =\n", None),
    "empty-tax": ("[stockroom]\ntax_bp =\n", None),
    "spaces-around-values": ("[stockroom]\ncurrency_symbol =   EUR  \ntax_bp =  12  \n", None),
    "continuation-line": ("[stockroom]\ntax_bp =\n  7\n", None),
    "int-spellings-file": ("[stockroom]\ntax_bp = 1_000\n", None),
    "minus-zero": ("[stockroom]\ntax_bp = -0\n", None),
    "negative-file": ("[stockroom]\ntax_bp = -3\n", None),
    "booleans-in-file": ("[stockroom]\nlow_stock_warning = OFF\n", None),
    "bad-boolean-in-file": ("[stockroom]\nlow_stock_warning = maybe\n", None),
    "file-error-before-env-error": ("[stockroom]\ncolour = blue\n", {"STOCKROOM_TAX_BP": "lots"}),
    "env-over-file": (
        "[stockroom]\ntax_bp = 825\ncurrency_symbol = GBP\n",
        {"STOCKROOM_TAX_BP": "1000"},
    ),
    "env-empty-text": (None, {"STOCKROOM_CURRENCY_SYMBOL": "", "STOCKROOM_DATA_DIR": ""}),
    "env-empty-number": (None, {"STOCKROOM_TAX_BP": ""}),
    "env-int-spellings": (None, {"STOCKROOM_TAX_BP": " +1_0 "}),
    "env-unicode-digits": (None, {"STOCKROOM_TAX_BP": "١٢"}),
    "env-negative": (None, {"STOCKROOM_TAX_BP": "-1"}),
    "env-lower-case-name-ignored": (None, {"stockroom_tax_bp": "x", "STOCKROOM_UNKNOWN": "1"}),
    "env-mapping-proxy": (None, types.MappingProxyType({"STOCKROOM_DATA_DIR": "/srv"})),
}


@pytest.mark.parametrize("name", sorted(EDGE_CASES))
def test_settings_edge_cases_load_as_before(name: str, tmp_path: Path) -> None:
    text, env = EDGE_CASES[name]
    path = None
    if text is not None:
        path = tmp_path / "s.ini"
        path.write_text(text, encoding="utf-8")
    assert new_outcome(path, env) == oracle_outcome(path, env)
    missing = tmp_path / "missing.ini"
    assert new_outcome(missing, env) == oracle_outcome(missing, env)


def test_settings_record_has_four_typed_fields() -> None:
    fields = dataclasses.fields(config.Settings)
    assert [(f.name, f.default) for f in fields] == [
        ("currency_symbol", "$"),
        ("low_stock_warning", True),
        ("tax_bp", 0),
        ("data_dir", "data"),
    ]
    hints = typing.get_type_hints(config.Settings)
    assert [hints[f.name] for f in fields] == [str, bool, int, str]
    default = config.Settings()
    assert default == config.Settings("$", True, 0, "data")
    assert hash(default) == hash(config.Settings())
    assert default != config.Settings(tax_bp=825)
    with pytest.raises(dataclasses.FrozenInstanceError):
        default.tax_bp = 5  # type: ignore[misc]
    assert config.Settings(currency_symbol="EUR", tax_bp=7).tax_bp == 7


def test_load_settings_reads_only_the_given_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in [k for k in os.environ if k.startswith("STOCKROOM_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("STOCKROOM_TAX_BP", "999")
    monkeypatch.setenv("STOCKROOM_CURRENCY_SYMBOL", "ZZZ")
    assert config.load_settings() == config.Settings()
    assert config.load_settings(None, None) == config.Settings()
    path = tmp_path / "s.ini"
    path.write_text("[stockroom]\ntax_bp = 825\nlow_stock_warning = no\n", encoding="utf-8")
    assert config.load_settings(path) == config.Settings(low_stock_warning=False, tax_bp=825)
    assert config.load_settings(path, {"STOCKROOM_TAX_BP": "5"}) == config.Settings(
        low_stock_warning=False, tax_bp=5
    )
    assert config.load_settings(env=os.environ) == config.Settings(
        currency_symbol="ZZZ", tax_bp=999
    )


def test_load_config_is_a_deprecated_wrapper(tmp_path: Path) -> None:
    path = tmp_path / "s.ini"
    path.write_text("[stockroom]\ntax_bp = 825\n", encoding="utf-8")
    env = {"STOCKROOM_CURRENCY_SYMBOL": "EUR"}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = config.load_config(path, env)
        second = config.load_config(path, env=env)
        plain = config.load_config()
    assert [(w.category, str(w.message), w.filename) for w in caught] == [
        (DeprecationWarning, OLD_MESSAGE, __file__)
    ] * 3
    assert type(first) is dict
    assert first == {
        "currency_symbol": "EUR",
        "low_stock_warning": True,
        "tax_bp": 825,
        "data_dir": "data",
    }
    assert list(first) == list(NAMES)
    assert first == second and first is not second and plain is not config.DEFAULTS
    first["tax_bp"] = 1
    assert second["tax_bp"] == 825 and config.DEFAULTS["tax_bp"] == 0
    assert plain == config.DEFAULTS


def test_load_config_warns_before_it_fails(tmp_path: Path) -> None:
    for kwargs, message in (
        ({"env": {"STOCKROOM_TAX_BP": "-1"}}, "tax_bp must not be negative"),
        ({"path": tmp_path / "missing.ini"}, "cannot read "),
    ):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(ConfigError) as raised:
                config.load_config(**kwargs)
        assert str(raised.value).startswith(message)
        assert [(w.category, str(w.message), w.filename) for w in caught] == [
            (DeprecationWarning, OLD_MESSAGE, __file__)
        ]


def test_settings_items_stay_readable_with_a_warning() -> None:
    settings = config.Settings(currency_symbol="EUR", tax_bp=825)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        values = [settings[name] for name in NAMES]
    assert values == ["EUR", True, 825, "data"]
    assert [(w.category, str(w.message), w.filename) for w in caught] == [
        (DeprecationWarning, f"settings[{name!r}] is deprecated; use settings.{name}", __file__)
        for name in NAMES
    ]
    for key in ("TAX_BP", "colour", "__class__", "__dataclass_fields__", "load_settings", ""):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(KeyError) as raised:
                settings[key]
        assert raised.value.args == (key,)
        assert caught == []


def test_defaults_mapping_is_unchanged() -> None:
    assert type(config.DEFAULTS) is dict
    assert list(config.DEFAULTS.items()) == [
        ("currency_symbol", "$"),
        ("low_stock_warning", True),
        ("tax_bp", 0),
        ("data_dir", "data"),
    ]
    assert dataclasses.asdict(config.Settings()) == config.DEFAULTS


def test_each_setting_name_is_written_once() -> None:
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in NAMES:
                offenders.append((path.name, node.lineno, node.value))
    assert offenders == []
    source = Path(inspect.getsourcefile(config.Settings))
    tree = ast.parse(source.read_text(encoding="utf-8"))
    settings = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Settings"]
    assert len(settings) == 1
    declared = [
        stmt.target.id
        for stmt in settings[0].body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]
    assert [name for name in declared if name in NAMES] == list(NAMES)


def test_command_line_reads_the_settings_record_without_deprecated_calls(tmp_path: Path) -> None:
    tree = ast.parse((PACKAGE / "cli.py").read_text(encoding="utf-8"))
    names = {
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute, ast.alias))
    }
    assert "load_config" not in names and "load_settings" in names
    (tmp_path / "items.csv").write_text(ITEMS_CSV, encoding="utf-8")
    (tmp_path / "stock.csv").write_text(STOCK_CSV, encoding="utf-8")
    (tmp_path / "s.ini").write_text(
        "[stockroom]\ntax_bp = 825\nlow_stock_warning = on\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env.update(PYTHONPATH=str(ROOT), STOCKROOM_CURRENCY_SYMBOL="EUR ")
    for argv in (
        [
            "--config",
            "s.ini",
            "price",
            "--items",
            "items.csv",
            "--sku",
            "PEN-100",
            "--quantity",
            "5",
        ],
        ["--config", "s.ini", "stock", "--items", "items.csv", "--stock", "stock.csv"],
    ):
        done = subprocess.run(
            [sys.executable, "-W", "error::DeprecationWarning", "-m", "stockroom", *argv],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert "Deprecation" not in done.stderr
    assert done.stderr == "warning: 3 item(s) below reorder level\n"


# -- the command line before and after ----------------------------------------------------------

ITEMS_CSV = """sku,name,price,tags,reorder_level
MUG-001,Blue Mug,12.50,kitchen;gift,5
MUG-002,Red Mug,12.50,kitchen,5
TEA-010,Green Tea 100g,8.95,food;tea,10
TEA-011,Black Tea 100g,7.40,food;tea,10
PEN-100,Fountain Pen,34.00,office;gift,2
NB-200,Notebook A5,6.25,office,20
LMP-030,Desk Lamp,49.99,office;home,1
CND-007,Beeswax Candle,9.80,home;gift,8
"""
STOCK_CSV = """sku,on_hand,reserved
MUG-001,14,2
MUG-002,4,0
TEA-010,30,5
TEA-011,9,0
PEN-100,3,2
NB-200,40,4
LMP-030,2,0
CND-007,12,1
"""
ORDERS_CSV = """order_id,customer,placed,status,sku,quantity,unit_price
Z-1,Hana Kimura-Lee,2024-01-08,shipped,MUG-001,20,12.50
Z-1,Hana Kimura-Lee,2024-01-08,shipped,TEA-010,10,8.95
Z-2,Omar Diaz-Fuentes,2024-01-19,paid,PEN-100,5,34.00
Z-3,Lena Berg-Holmgren,2024-02-03,cancelled,LMP-030,1,49.99
Z-4,Hana Kimura-Lee,2024-02-14,paid,CND-007,30,9.80
Z-5,Lena Berg-Holmgren,2024-03-02,open,TEA-011,20,7.40
"""

PRICE = ["price", "--items", "{I}", "--sku", "PEN-100", "--quantity", "5", "--discount", "10"]
STOCK = ["stock", "--items", "{I}", "--stock", "{S}"]
# name -> (argv, environment variables, settings file text or None)
CLI_CASES = {
    "items": (["items", "--file", "{I}"], {}, None),
    "items-symbol-env": (
        ["items", "--file", "{I}", "--tag", "tea"],
        {"STOCKROOM_CURRENCY_SYMBOL": "EUR "},
        None,
    ),
    "items-symbol-file": (
        ["--config", "{C}", "items", "--file", "{I}"],
        {},
        "[stockroom]\ncurrency_symbol = GBP\n",
    ),
    "items-env-over-file": (
        ["--config", "{C}", "items", "--file", "{I}", "--tag", "gift"],
        {"STOCKROOM_CURRENCY_SYMBOL": "CHF "},
        "[stockroom]\ncurrency_symbol = GBP\n",
    ),
    "stock": (STOCK, {}, None),
    "stock-quiet-file": (["--config", "{C}", *STOCK], {}, "[stockroom]\nlow_stock_warning = off\n"),
    "stock-env-over-quiet-file": (
        ["--config", "{C}", *STOCK],
        {"STOCKROOM_LOW_STOCK_WARNING": "yes"},
        "[stockroom]\nlow_stock_warning = no\n",
    ),
    "sales": (["sales", "--orders", "{O}"], {}, None),
    "sales-symbol-file": (
        ["--config", "{C}", "sales", "--orders", "{O}", "--from", "2024-02-01"],
        {},
        "[stockroom]\ncurrency_symbol = EUR \n",
    ),
    "price": (PRICE, {}, None),
    "price-tax-file": (["--config", "{C}", *PRICE], {}, "[stockroom]\ntax_bp = 825\n"),
    "price-tax-env-over-file": (
        ["--config", "{C}", *PRICE],
        {"STOCKROOM_TAX_BP": "1000"},
        "[stockroom]\ntax_bp = 825\n",
    ),
    "price-tax-env-spelling": (
        PRICE,
        {"STOCKROOM_TAX_BP": " +1_000 ", "STOCKROOM_CURRENCY_SYMBOL": ""},
        None,
    ),
    "bad-key": (["--config", "{C}", "items", "--file", "{I}"], {}, "[stockroom]\ncolour = blue\n"),
    "bad-tax-env": (PRICE, {"STOCKROOM_TAX_BP": "lots"}, None),
    "negative-tax-file": (["--config", "{C}", *PRICE], {}, "[stockroom]\ntax_bp = -5\n"),
    "bad-boolean-file": (
        ["--config", "{C}", *STOCK],
        {},
        "[stockroom]\nlow_stock_warning = maybe\n",
    ),
    "missing-settings-file": (["--config", "{D}/missing.ini", "items", "--file", "{I}"], {}, None),
    "file-error-wins": (
        ["--config", "{C}", *PRICE],
        {"STOCKROOM_TAX_BP": "lots"},
        "[stockroom]\ncolour = blue\n",
    ),
    "foreign-section": (["--config", "{C}", *PRICE], {}, "[other]\ncolour = blue\n"),
}

# (exit code, stdout, stderr) recorded from the command before the change.
CLI_GOLD: dict[str, tuple[int, str, str]] = {
    "bad-boolean-file": (
        1,
        "",
        "stockroom: error: low_stock_warning must be a boolean\n",
    ),
    "bad-key": (
        1,
        "",
        "stockroom: error: unknown setting 'colour' in <D>/s.ini\n",
    ),
    "bad-tax-env": (
        1,
        "",
        "stockroom: error: tax_bp must be a whole number, not 'lots'\n",
    ),
    "file-error-wins": (
        1,
        "",
        "stockroom: error: unknown setting 'colour' in <D>/s.ini\n",
    ),
    "foreign-section": (
        0,
        "5 x PEN-100 Fountain Pen: $153.00\ntax: $0.00\ntotal: $153.00\n",
        "",
    ),
    "items": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  ------\n"
        "MUG-001  Blue Mug        $12.50\n"
        "MUG-002  Red Mug         $12.50\n"
        "TEA-010  Green Tea 100g   $8.95\n"
        "TEA-011  Black Tea 100g   $7.40\n"
        "PEN-100  Fountain Pen    $34.00\n"
        "NB-200   Notebook A5      $6.25\n"
        "LMP-030  Desk Lamp       $49.99\n"
        "CND-007  Beeswax Candle   $9.80\n",
        "",
    ),
    "items-env-over-file": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  ---------\n"
        "MUG-001  Blue Mug        CHF 12.50\n"
        "PEN-100  Fountain Pen    CHF 34.00\n"
        "CND-007  Beeswax Candle   CHF 9.80\n",
        "",
    ),
    "items-symbol-env": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  --------\n"
        "TEA-010  Green Tea 100g  EUR 8.95\n"
        "TEA-011  Black Tea 100g  EUR 7.40\n",
        "",
    ),
    "items-symbol-file": (
        0,
        "SKU      Name            Price\n"
        "-------  --------------  --------\n"
        "MUG-001  Blue Mug        GBP12.50\n"
        "MUG-002  Red Mug         GBP12.50\n"
        "TEA-010  Green Tea 100g   GBP8.95\n"
        "TEA-011  Black Tea 100g   GBP7.40\n"
        "PEN-100  Fountain Pen    GBP34.00\n"
        "NB-200   Notebook A5      GBP6.25\n"
        "LMP-030  Desk Lamp       GBP49.99\n"
        "CND-007  Beeswax Candle   GBP9.80\n",
        "",
    ),
    "missing-settings-file": (
        1,
        "",
        "stockroom: error: cannot read <D>/missing.ini: [Errno 2] No such fil"
        "e or directory: '<D>/missing.ini'\n",
    ),
    "negative-tax-file": (
        1,
        "",
        "stockroom: error: tax_bp must not be negative\n",
    ),
    "price": (
        0,
        "5 x PEN-100 Fountain Pen: $153.00\ntax: $0.00\ntotal: $153.00\n",
        "",
    ),
    "price-tax-env-over-file": (
        0,
        "5 x PEN-100 Fountain Pen: $153.00\ntax: $15.30\ntotal: $168.30\n",
        "",
    ),
    "price-tax-env-spelling": (
        0,
        "5 x PEN-100 Fountain Pen: 153.00\ntax: 15.30\ntotal: 168.30\n",
        "",
    ),
    "price-tax-file": (
        0,
        "5 x PEN-100 Fountain Pen: $153.00\ntax: $12.62\ntotal: $165.62\n",
        "",
    ),
    "sales": (
        0,
        "4 orders, 1 cancelled\n"
        "Revenue: $951.50\n"
        "\n"
        "Month    Revenue\n"
        "-------  -------\n"
        "2024-01  $509.50\n"
        "2024-02  $294.00\n"
        "2024-03  $148.00\n"
        "\n"
        "Customer              Total\n"
        "------------------  -------\n"
        "Hana Kimura-Lee     $633.50\n"
        "Omar Diaz-Fuentes   $170.00\n"
        "Lena Berg-Holmgren  $148.00\n",
        "",
    ),
    "sales-symbol-file": (
        0,
        "2 orders, 1 cancelled\n"
        "Revenue: EUR442.00\n"
        "\n"
        "Month      Revenue\n"
        "-------  ---------\n"
        "2024-02  EUR294.00\n"
        "2024-03  EUR148.00\n"
        "\n"
        "Customer                Total\n"
        "------------------  ---------\n"
        "Hana Kimura-Lee     EUR294.00\n"
        "Lena Berg-Holmgren  EUR148.00\n",
        "",
    ),
    "stock": (
        0,
        "LOW MUG-002 Red Mug: 4/5\n"
        "LOW TEA-011 Black Tea 100g: 9/10\n"
        "LOW PEN-100 Fountain Pen: 1/2\n",
        "warning: 3 item(s) below reorder level\n",
    ),
    "stock-env-over-quiet-file": (
        0,
        "LOW MUG-002 Red Mug: 4/5\n"
        "LOW TEA-011 Black Tea 100g: 9/10\n"
        "LOW PEN-100 Fountain Pen: 1/2\n",
        "warning: 3 item(s) below reorder level\n",
    ),
    "stock-quiet-file": (
        0,
        "LOW MUG-002 Red Mug: 4/5\n"
        "LOW TEA-011 Black Tea 100g: 9/10\n"
        "LOW PEN-100 Fountain Pen: 1/2\n",
        "",
    ),
}


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    for key in [k for k in os.environ if k.startswith("STOCKROOM_")]:
        monkeypatch.delenv(key)
    (tmp_path / "items.csv").write_text(ITEMS_CSV, encoding="utf-8")
    (tmp_path / "stock.csv").write_text(STOCK_CSV, encoding="utf-8")
    (tmp_path / "orders.csv").write_text(ORDERS_CSV, encoding="utf-8")
    files = {
        "{I}": str(tmp_path / "items.csv"),
        "{S}": str(tmp_path / "stock.csv"),
        "{O}": str(tmp_path / "orders.csv"),
        "{C}": str(tmp_path / "s.ini"),
    }

    def call(name: str) -> tuple[int, str, str]:
        argv, env, text = CLI_CASES[name]
        if text is not None:
            (tmp_path / "s.ini").write_text(text, encoding="utf-8")
        with monkeypatch.context() as patch:
            for key, value in env.items():
                patch.setenv(key, value)
            real = [files.get(a, a).replace("{D}", str(tmp_path)) for a in argv]
            code = cli.main(real)
        captured = capsys.readouterr()
        return code, captured.out, captured.err.replace(str(tmp_path), "<D>")

    return call


@pytest.mark.parametrize("name", sorted(CLI_CASES))
def test_command_line_output_is_unchanged(name: str, run) -> None:
    assert run(name) == CLI_GOLD[name]
