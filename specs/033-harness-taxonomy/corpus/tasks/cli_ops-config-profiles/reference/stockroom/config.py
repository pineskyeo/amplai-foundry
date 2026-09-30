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


def _section_values(
    parser: configparser.ConfigParser, section: str, path: Path, *, own: bool
) -> dict[str, Any]:
    """The validated values of ``section``; ``own`` keeps out keys inherited from ``[DEFAULT]``."""
    keys = list(parser._sections[section]) if own else list(parser[section])  # type: ignore[attr-defined]
    values: dict[str, Any] = {}
    for key in keys:
        if key not in DEFAULTS:
            raise ConfigError(f"unknown setting {key!r} in [{section}] of {path}")
        if isinstance(DEFAULTS[key], bool):
            try:
                values[key] = parser.getboolean(section, key)
            except ValueError as exc:
                raise ConfigError(f"{key} must be a boolean") from exc
        else:
            values[key] = _coerce(key, parser.get(section, key))
    return values


def _from_file(path: Path, profile: str | None = None) -> dict[str, Any]:
    parser = configparser.ConfigParser()
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    values: dict[str, Any] = {}
    if parser.has_section(SECTION):
        values.update(_section_values(parser, SECTION, path, own=False))
    if profile:
        name = f"{SECTION}:{profile}"
        if not parser.has_section(name):
            raise ConfigError(f"no profile {profile!r} in {path}")
        values.update(_section_values(parser, name, path, own=True))
    return values


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """The settings: ``DEFAULTS``, overridden by ``path`` (when given), overridden by ``env``.

    Only ``STOCKROOM_<KEY>`` variables for known keys are read; other variables are ignored.
    """
    config = dict(DEFAULTS)
    if profile and path is None:
        raise ConfigError(f"profile {profile!r} needs a settings file (--config)")
    if path is not None:
        config.update(_from_file(path, profile))
    for key in DEFAULTS:
        raw = (env or {}).get(ENV_PREFIX + key.upper())
        if raw is not None:
            config[key] = _coerce(key, raw)
    return config
