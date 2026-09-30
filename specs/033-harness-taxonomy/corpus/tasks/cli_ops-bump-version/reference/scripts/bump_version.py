"""Raise the project version in ``stockroom/__init__.py`` and ``pyproject.toml``.

    python scripts/bump_version.py NEW [--root DIR] [--dry-run]

``NEW`` is ``major``, ``minor``, ``patch`` or an explicit ``X.Y.Z`` greater than the current version.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
INIT_LINE = re.compile(
    r"""^(__version__[ \t]*=[ \t]*(["']))([^"']*)(\2[ \t]*(#.*)?)$""",
)
PY_LINE = re.compile(r"""^([ \t]*version[ \t]*=[ \t]*(["']))([^"']*)(\2[ \t]*(#.*)?)$""")
HEADER = re.compile(r"^[ \t]*\[\[?[^\[\]]+\]\]?[ \t]*(#.*)?$")
PROJECT_HEADER = re.compile(r"^[ \t]*\[project\][ \t]*(#.*)?$")


class BumpError(Exception):
    pass


def _new(text: str) -> str:
    if text in ("major", "minor", "patch") or VERSION.fullmatch(text):
        return text
    raise argparse.ArgumentTypeError(f"not major, minor, patch or X.Y.Z: {text!r}")


def _tuple(text: str) -> tuple[int, int, int]:
    a, b, c = VERSION.fullmatch(text).groups()  # type: ignore[union-attr]
    return int(a), int(b), int(c)


def _find(lines: list[str], *, in_project: bool, pattern: re.Pattern[str]) -> int | None:
    inside = not in_project
    for index, line in enumerate(lines):
        body = line.rstrip("\r")
        if in_project:
            if PROJECT_HEADER.match(body):
                inside = True
                continue
            if HEADER.match(body):
                inside = False
                continue
        if inside and pattern.match(body):
            return index
    return None


def _read(root: Path, name: str, *, in_project: bool, pattern: re.Pattern[str]):
    path = root / name
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise BumpError(f"cannot read {name}: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise BumpError(f"{name} is not valid UTF-8 text") from None
    lines = text.split("\n")
    index = _find(lines, in_project=in_project, pattern=pattern)
    if index is None:
        raise BumpError(f"{name} has no version line")
    match = pattern.match(lines[index].rstrip("\r"))
    assert match is not None
    if not VERSION.fullmatch(match.group(3)):
        raise BumpError(f"{name}: the version {match.group(3)!r} is not X.Y.Z")
    return path, lines, index, match


def _rewrite(lines: list[str], index: int, match: re.Match[str], new: str) -> str:
    tail = lines[index][len(match.group(0)) :]  # the "\r" of a CRLF line
    lines = list(lines)
    lines[index] = match.group(1) + new + match.group(4) + tail
    return "\n".join(lines)


def run(root: Path, new_arg: str, dry_run: bool) -> str:
    init = _read(root, "stockroom/__init__.py", in_project=False, pattern=INIT_LINE)
    proj = _read(root, "pyproject.toml", in_project=True, pattern=PY_LINE)
    a, b = init[3].group(3), proj[3].group(3)
    if a != b:
        raise BumpError(f"versions differ: pyproject.toml has {b}, stockroom/__init__.py has {a}")
    current = _tuple(a)
    if new_arg == "major":
        new = f"{current[0] + 1}.0.0"
    elif new_arg == "minor":
        new = f"{current[0]}.{current[1] + 1}.0"
    elif new_arg == "patch":
        new = f"{current[0]}.{current[1]}.{current[2] + 1}"
    else:
        new = new_arg
        if _tuple(new) <= current:
            raise BumpError(f"{new} is not greater than the current version {a}")
    if not dry_run:
        for path, lines, index, match in (init, proj):
            path.write_bytes(_rewrite(lines, index, match, new).encode("utf-8"))
    return f"{a} -> {new}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Raise the project version.")
    parser.add_argument("new", type=_new, metavar="NEW", help="major, minor, patch or X.Y.Z")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(run(args.root, args.new, args.dry_run))
    except BumpError as exc:
        print(f"bump_version: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
