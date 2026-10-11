"""Settings: defaults, then an INI file (section ``[stockroom]``), then ``STOCKROOM_*`` variables,
then ``--set KEY=VALUE`` overrides from the command line.

Booleans accept ``true/false``, ``yes/no``, ``on/off`` and ``1/0`` (any case).
"""

from __future__ import annotations

import configparser
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigError

SECTION = "stockroom"
ENV_PREFIX = "STOCKROOM_"
CONFIG_ENV = "STOCKROOM_CONFIG"
DEFAULTS: dict[str, Any] = {
    "currency_symbol": "$",
    "low_stock_warning": True,
    "tax_bp": 0,
    "data_dir": "data",
}
_BOOLEANS = {"true": True, "yes": True, "on": True, "1": True}
_BOOLEANS.update({"false": False, "no": False, "off": False, "0": False})


def _coerce(key: str, raw: str) -> Any:
    default = DEFAULTS[key]
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


def _override(key: str, raw: str) -> Any:
    """A ``--set`` value, converted like a value of the settings file (text kept exactly)."""
    if isinstance(DEFAULTS[key], bool):
        try:
            return _BOOLEANS[raw.lower()]
        except KeyError:
            raise ConfigError(f"{key} must be a boolean, not {raw!r}") from None
    return _coerce(key, raw)


def _from_file(path: Path) -> dict[str, Any]:
    parser = configparser.ConfigParser()
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not parser.has_section(SECTION):
        return {}
    values: dict[str, Any] = {}
    for key in parser[SECTION]:
        if key not in DEFAULTS:
            raise ConfigError(f"unknown setting {key!r} in {path}")
        if isinstance(DEFAULTS[key], bool):
            try:
                values[key] = parser.getboolean(SECTION, key)
            except ValueError as exc:
                raise ConfigError(f"{key} must be a boolean") from exc
        else:
            values[key] = _coerce(key, parser.get(SECTION, key))
    return values


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The settings: ``DEFAULTS``, overridden by ``path`` (when given), overridden by ``env``,
    overridden by ``overrides`` (``{key: raw text}`` from ``--set``).

    Only ``STOCKROOM_<KEY>`` variables for known keys are read; other variables are ignored, and
    an empty variable counts as not set. A variable is not read for a key that ``overrides`` sets.
    """
    config = dict(DEFAULTS)
    if path is not None:
        config.update(_from_file(path))
    overrides = overrides or {}
    for key in DEFAULTS:
        if key in overrides:
            config[key] = _override(key, overrides[key])
            continue
        raw = (env or {}).get(ENV_PREFIX + key.upper())
        if raw:
            config[key] = _coerce(key, raw)
    return config


def config_path(option: str | None, env: Mapping[str, str]) -> Path | None:
    """The settings file: ``--config`` when given (empty: none), else ``STOCKROOM_CONFIG``."""
    if option is not None:
        return Path(option) if option else None
    named = env.get(CONFIG_ENV, "")
    return Path(named) if named else None
