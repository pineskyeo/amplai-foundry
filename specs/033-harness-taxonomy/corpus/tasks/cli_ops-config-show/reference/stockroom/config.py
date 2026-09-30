"""Settings: defaults, then an INI file (section ``[stockroom]``), then ``STOCKROOM_*`` variables.

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
DEFAULTS: dict[str, Any] = {
    "currency_symbol": "$",
    "low_stock_warning": True,
    "tax_bp": 0,
    "data_dir": "data",
}


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


def load_with_origins(
    path: Path | None = None, env: Mapping[str, str] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """The settings as ``load_config`` returns them, and per key where the value came from:
    ``"default"``, ``"file"`` or ``"env"``."""
    config = dict(DEFAULTS)
    origins = {key: "default" for key in DEFAULTS}
    if path is not None:
        for key, value in _from_file(path).items():
            config[key] = value
            origins[key] = "file"
    for key in DEFAULTS:
        raw = (env or {}).get(ENV_PREFIX + key.upper())
        if raw is not None:
            config[key] = _coerce(key, raw)
            origins[key] = "env"
    return config, origins


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The settings: ``DEFAULTS``, overridden by ``path`` (when given), overridden by ``env``.

    Only ``STOCKROOM_<KEY>`` variables for known keys are read; other variables are ignored.
    """
    return load_with_origins(path, env)[0]
