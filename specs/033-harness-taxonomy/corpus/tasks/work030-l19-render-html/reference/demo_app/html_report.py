"""Render version infos as a small HTML page."""

from __future__ import annotations

from typing import Any

_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#x27;",
}

_HEAD = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    '<head><meta charset="utf-8"><title>AMPLAI versions</title></head>\n'
    "<body>\n"
    "<table>\n"
    "<thead><tr><th>version</th><th>stage</th></tr></thead>\n"
    "<tbody>\n"
)
_TAIL = "</tbody>\n</table>\n</body>\n</html>\n"


def escape_html(text: str) -> str:
    return "".join(_ESCAPES.get(char, char) for char in text)


def _cells(info: dict[str, Any]) -> tuple[str, str]:
    version = info.get("package_version")
    if not isinstance(version, str):
        raise ValueError("package_version must be a str")
    stage = info.get("stage", "")
    if not isinstance(stage, str):
        raise ValueError("stage must be a str when present")
    return escape_html(version), escape_html(stage)


def render_html(infos: list[dict[str, Any]]) -> str:
    rows = [_cells(info) for info in infos]
    body = "".join(f"<tr><td>{v}</td><td>{s}</td></tr>\n" for v, s in rows)
    return _HEAD + body + _TAIL
