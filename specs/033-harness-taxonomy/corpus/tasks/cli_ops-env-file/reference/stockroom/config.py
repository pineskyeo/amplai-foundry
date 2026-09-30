"""Settings: defaults, then an INI file (section ``[stockroom]``), then ``STOCKROOM_*`` variables.

Booleans accept ``true/false``, ``yes/no``, ``on/off`` and ``1/0`` (any case).
"""

from __future__ import annotations

import configparser
import re
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


_ENV_LINE = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)", re.DOTALL)
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}


def _env_value(rest: str, where: str) -> str:
    text = rest.lstrip(" \t")
    if text[:1] in ('"', "'"):
        quote = text[0]
        out: list[str] = []
        i = 1
        while True:
            if i >= len(text):
                raise ConfigError(f"{where}: quote is never closed")
            ch = text[i]
            if quote == '"' and ch == "\\" and text[i + 1 : i + 2] in _ESCAPES and i + 1 < len(text):
                out.append(_ESCAPES[text[i + 1]])
                i += 2
                continue
            if ch == quote:
                break
            out.append(ch)
            i += 1
        tail = text[i + 1 :].lstrip(" \t")
        if tail and not tail.startswith("#"):
            raise ConfigError(f"{where}: text after the closing quote")
        return "".join(out)
    cut = re.search(r"[ \t]#", rest)
    return (rest[: cut.start()] if cut else rest).strip()


def parse_env_file(text: str, label: str) -> dict[str, str]:
    """``STOCKROOM_*`` assignments of a dotenv-style file; ``label`` names the file in errors."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        where = f"{label}: line {number}"
        match = _ENV_LINE.fullmatch(raw)
        if match is None:
            raise ConfigError(f"{where}: expected KEY=VALUE")
        key, rest = match.groups()
        value = _env_value(rest, where)
        if not key.startswith(ENV_PREFIX):
            continue
        if key[len(ENV_PREFIX) :].lower() not in DEFAULTS or key != key.upper():
            raise ConfigError(f"{where}: unknown setting {key!r}")
        values[key] = value
    return values


def load_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"cannot read {path}: not valid UTF-8 text") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    return parse_env_file(text, str(path))


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The settings: ``DEFAULTS``, overridden by ``path`` (when given), overridden by ``env``.

    Only ``STOCKROOM_<KEY>`` variables for known keys are read; other variables are ignored.
    """
    config = dict(DEFAULTS)
    if path is not None:
        config.update(_from_file(path))
    for key in DEFAULTS:
        raw = (env or {}).get(ENV_PREFIX + key.upper())
        if raw is not None:
            config[key] = _coerce(key, raw)
    return config
