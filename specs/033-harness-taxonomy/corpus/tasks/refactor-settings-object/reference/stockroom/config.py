"""Settings: defaults, then an INI file (section ``[stockroom]``), then ``STOCKROOM_*`` variables.

Booleans accept ``true/false``, ``yes/no``, ``on/off`` and ``1/0`` (any case).
"""

from __future__ import annotations

import configparser
import dataclasses
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigError

SECTION = "stockroom"
ENV_PREFIX = "STOCKROOM_"


@dataclasses.dataclass(frozen=True)
class Settings:
    """The settings of one run. The names of the settings are spelled out here and only here."""

    currency_symbol: str = "$"
    low_stock_warning: bool = True
    tax_bp: int = 0
    data_dir: str = "data"

    def __getitem__(self, name: str) -> Any:
        """Deprecated: ``settings[name]`` as on the old settings ``dict``."""
        if name not in _NAMES:
            raise KeyError(name)
        warnings.warn(
            f"settings[{name!r}] is deprecated; use settings.{name}",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(self, name)


_NAMES = tuple(field.name for field in dataclasses.fields(Settings))
DEFAULTS: dict[str, Any] = {field.name: field.default for field in dataclasses.fields(Settings)}


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


def load_settings(path: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    """The settings: the defaults, overridden by ``path`` (when given), overridden by ``env``.

    Only ``STOCKROOM_<KEY>`` entries of ``env`` for known keys are read; without ``env`` no
    environment variable is read at all.
    """
    values = dict(DEFAULTS)
    if path is not None:
        values.update(_from_file(path))
    for key in DEFAULTS:
        raw = (env or {}).get(ENV_PREFIX + key.upper())
        if raw is not None:
            values[key] = _coerce(key, raw)
    return Settings(**values)


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Deprecated: the settings of ``load_settings`` as a new ``dict``."""
    warnings.warn(
        "load_config() is deprecated; use load_settings()", DeprecationWarning, stacklevel=2
    )
    return dataclasses.asdict(load_settings(path, env))
