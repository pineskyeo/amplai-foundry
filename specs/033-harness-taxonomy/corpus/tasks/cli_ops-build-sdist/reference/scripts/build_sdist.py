"""Build a reproducible source archive: OUT/<name>-<version>.zip.

    python scripts/build_sdist.py [--root DIR] [--out DIR] [--check]

The files come from the [tool.stockroom.sdist] table of pyproject.toml. The same files always
give the same bytes. Exit codes: 0 done (or up to date), 1 --check found the archive missing or
out of date, 2 a usage or configuration error.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import os
import re
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
VERSION_LINE = re.compile(r"""__version__\s*=\s*(["'])([^"']*)\1""")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


class BuildError(Exception):
    pass


def _strings(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BuildError(f"{what} must be a list of strings")
    return value


def _config(root: Path) -> tuple[str, str, list[str], list[str], str | None]:
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BuildError(f"cannot read pyproject.toml: {exc}") from exc
    project = data.get("project", {})
    name, version = project.get("name"), project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise BuildError("pyproject.toml needs [project] name and version strings")
    table = data.get("tool", {}).get("stockroom", {}).get("sdist")
    if not isinstance(table, dict):
        raise BuildError("pyproject.toml has no [tool.stockroom.sdist] table")
    include = _strings(table.get("include"), "include")
    if not include:
        raise BuildError("include must not be empty")
    exclude = _strings(table.get("exclude", []), "exclude")
    out = table.get("out")
    if out is not None and not isinstance(out, str):
        raise BuildError("out must be a string")
    try:
        init = (root / "stockroom" / "__init__.py").read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read stockroom/__init__.py: {exc}") from exc
    for line in init.splitlines():
        match = VERSION_LINE.match(line)
        if match:
            if match[2] != version:
                raise BuildError(f"version {version} differs from __version__ {match[2]}")
            break
    else:
        raise BuildError("stockroom/__init__.py has no __version__")
    return name, version, include, exclude, out


def _files(root: Path, include: list[str], exclude: list[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for pattern in include:
        if not pattern or PurePosixPath(pattern).is_absolute() or ".." in pattern.split("/"):
            raise BuildError(f"include pattern {pattern!r} must be a relative path without ..")
        matched = 0
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatchcase(part, ex) for part in rel.split("/") for ex in exclude):
                continue
            matched += 1
            found[rel] = path
        if not matched:
            raise BuildError(f"include pattern {pattern!r} matches no file")
    return found


def build(root: Path) -> tuple[str, bytes, int, str | None]:
    name, version, include, exclude, out = _config(root)
    base = f"{name}-{version}"
    entries = {
        f"{base}/{rel}": path.read_bytes() for rel, path in _files(root, include, exclude).items()
    }
    entries[f"{base}/PKG-INFO"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for entry in sorted(entries):
            info = zipfile.ZipInfo(entry, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[entry])
    return f"{base}.zip", buffer.getvalue(), len(entries), out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a reproducible source archive.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        filename, data, count, table_out = build(args.root)
        if args.out is not None:
            out_dir = args.out
        elif table_out is not None:
            out_dir = args.root / table_out
        else:
            out_dir = args.root / "dist"
        target = out_dir / filename
        if out_dir.exists() and not out_dir.is_dir():
            raise BuildError(f"{out_dir} is not a directory")
        if args.check:
            if not target.is_file():
                print(f"missing {target}")
                return 1
            if target.read_bytes() != data:
                print(f"out of date {target}")
                return 1
            print(f"up to date {target}")
            return 0
        out_dir.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=out_dir, prefix=f".{filename}.")
        try:
            with open(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, target)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
    except (BuildError, OSError) as exc:
        print(f"build_sdist: error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {target} ({count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
