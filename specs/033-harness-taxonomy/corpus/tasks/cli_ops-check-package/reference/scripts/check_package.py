"""Find packaging and documentation drift before a release.

    python scripts/check_package.py [--root DIR]

Prints one ``<rule>: <message>`` line per problem (sorted) and exits 1, or prints ``ok`` and exits
0. The project is only read, never imported.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

VERSION_LINE = re.compile(r"""^__version__\s*=\s*(["'])(.*?)\1\s*(#.*)?$""", re.MULTILINE)
TARGET = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")
ROW = re.compile(r"^\|\s*`((?:stockroom|scripts)/[^`/]+\.py)`\s*\|", re.MULTILINE)


def _load(root: Path) -> tuple[dict[str, Any] | None, str]:
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"cannot read pyproject.toml: {exc.strerror or exc}"
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return None, f"pyproject.toml is not valid TOML: {exc}"
    project = data.get("project")
    if not isinstance(project, dict):
        return None, "pyproject.toml has no [project] table"
    if not isinstance(project.get("name"), str):
        return None, "[project] has no name"
    return data, ""


def _version(root: Path, data: dict[str, Any]) -> list[str]:
    init = root / "stockroom" / "__init__.py"
    try:
        match = VERSION_LINE.search(init.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        match = None
    if match is None:
        return ["version: stockroom/__init__.py does not define __version__"]
    actual = match.group(2)
    project = data["project"]
    declared = project.get("version")
    if isinstance(declared, str):
        if declared != actual:
            return [f"version: pyproject says {declared} but stockroom/__init__.py says {actual}"]
        return []
    if isinstance(project.get("dynamic"), list) and "version" in project["dynamic"]:
        dynamic = data.get("tool", {}).get("setuptools", {}).get("dynamic", {})
        entry = dynamic.get("version") if isinstance(dynamic, dict) else None
        if isinstance(entry, dict) and entry.get("attr") == "stockroom.__version__":
            return []
        return ["version: dynamic version must read stockroom.__version__"]
    return ["version: pyproject has no version"]


def _scripts(root: Path, data: dict[str, Any]) -> list[str]:
    problems = []
    scripts = data["project"].get("scripts", {})
    for name, target in sorted(scripts.items() if isinstance(scripts, dict) else []):
        if not isinstance(target, str) or not TARGET.fullmatch(target):
            problems.append(f"script: {name}: malformed target {target!r}")
            continue
        module, function = target.split(":")
        base = root / Path(*module.split("."))
        candidates = [base.with_suffix(".py"), base / "__init__.py"]
        found = next((c for c in candidates if c.is_file()), None)
        if found is None:
            problems.append(f"script: {name}: module {module} not found")
            continue
        text = found.read_text(encoding="utf-8", errors="replace")
        if not re.search(rf"^def\s+{re.escape(function)}\s*\(", text, re.MULTILINE):
            problems.append(f"script: {name}: {module} has no function {function}")
    return problems


def _subpackages(folder: Path, prefix: str) -> list[str]:
    names = []
    for child in sorted(folder.iterdir()):
        if child.is_dir() and (child / "__init__.py").is_file():
            dotted = f"{prefix}.{child.name}"
            names.append(dotted)
            names += _subpackages(child, dotted)
    return names


def _packages(root: Path, data: dict[str, Any]) -> list[str]:
    listed = data.get("tool", {}).get("setuptools", {}).get("packages")
    if not isinstance(listed, list):
        return []
    problems = []
    top = root / "stockroom"
    wanted = ["stockroom"] + (_subpackages(top, "stockroom") if top.is_dir() else [])
    for name in wanted:
        if name not in listed:
            problems.append(f"packages: {name} is not listed")
    for name in listed:
        if name not in wanted and not (root / Path(*str(name).split(".")) / "__init__.py").is_file():
            problems.append(f"packages: {name} does not exist")
    return problems


def _readme(root: Path) -> list[str]:
    try:
        text = (root / "README.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["readme: README.md is missing"]
    documented = set(ROW.findall(text))
    existing = set()
    for folder, skip_private in (("stockroom", True), ("scripts", False)):
        base = root / folder
        if not base.is_dir():
            continue
        for path in base.glob("*.py"):
            if skip_private and path.name.startswith("_"):
                continue
            existing.add(f"{folder}/{path.name}")
    problems = [f"readme: {p} is not described" for p in sorted(existing - documented)]
    for path in sorted(documented):
        if not (root / path).is_file():
            problems.append(f"readme: {path} does not exist")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check packaging and documentation drift.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    data, reason = _load(args.root)
    if data is None:
        problems = [f"pyproject: {reason}"]
    else:
        problems = (
            _version(args.root, data)
            + _scripts(args.root, data)
            + _packages(args.root, data)
            + _readme(args.root)
        )
    if not problems:
        print("ok")
        return 0
    print("\n".join(sorted(problems)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
