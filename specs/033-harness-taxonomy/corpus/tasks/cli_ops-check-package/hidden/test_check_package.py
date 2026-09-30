from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_package.py"

PYPROJECT = """\
[project]
name = "demo"
version = "1.2.3"

[project.scripts]
demo = "stockroom.cli:main"

[tool.setuptools]
packages = ["stockroom"]
"""

README = (
    "| Module | What |\n|---|---|\n"
    "| `stockroom/cli.py` | the command |\n"
    "| `stockroom/money.py` | cents |\n"
    "| `scripts/tool.py` | a tool |\n"
    "| `data/` | sample data |\n"
)

GOOD: dict[str, str] = {
    "pyproject.toml": PYPROJECT,
    "stockroom/__init__.py": '__version__ = "1.2.3"\n',
    "stockroom/cli.py": "def main():\n    return 0\n",
    "stockroom/money.py": "",
    "scripts/tool.py": "",
    "README.md": README,
}


def project(tmp_path: Path, changes: dict[str, str | None] | None = None) -> Path:
    """A project tree made from GOOD with the given files added, replaced or (None) removed."""
    files: dict[str, str | None] = {**GOOD, **(changes or {})}
    root = tmp_path / "proj"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    for name, content in files.items():
        if content is None:
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def check(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *extra],
        cwd=root.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def lines_of(done: subprocess.CompletedProcess[str]) -> list[str]:
    assert done.stderr == "", done.stderr
    assert done.stdout == "" or done.stdout.endswith("\n")
    return done.stdout.splitlines()


def assert_ok(done: subprocess.CompletedProcess[str]) -> None:
    assert done.returncode == 0, done.stdout
    assert lines_of(done) == ["ok"]


def assert_problems(done: subprocess.CompletedProcess[str], *expected: tuple[str, ...]) -> None:
    """Each expected item is (rule, token, ...): one distinct line must match; no other lines."""
    assert done.returncode == 1, (done.returncode, done.stdout)
    lines = lines_of(done)
    assert lines == sorted(lines)
    assert len(lines) == len(expected), lines
    remaining = list(lines)
    for rule, *tokens in expected:
        for line in remaining:
            if line.startswith(f"{rule}: ") and all(t in line for t in tokens):
                remaining.remove(line)
                break
        else:
            raise AssertionError(f"no line for {(rule, *tokens)} in {lines}")
    assert not remaining


def test_the_repository_passes_its_own_check(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == "ok\n" and done.stderr == ""
    assert_ok(check(ROOT))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "stockroom/errors.py" in readme and "scripts/check_package.py" in readme


def test_clean_projects_pass(tmp_path: Path) -> None:
    assert_ok(check(project(tmp_path)))
    # the script imports nothing of the project
    root = project(
        tmp_path,
        {
            "stockroom/__init__.py": 'raise SystemExit("do not import")\n__version__ = "1.2.3"\n',
            "stockroom/cli.py": 'raise RuntimeError("do not import")\ndef main():\n    pass\n',
        },
    )
    assert_ok(check(root))
    # files that need nothing
    root = project(
        tmp_path,
        {
            "stockroom/_private.py": "",
            "stockroom/__main__.py": "",
            "stockroom/py.typed": "",
            "tests/test_x.py": "",
            "docs/notes.md": "",
        },
    )
    assert_ok(check(root))


def test_pyproject_problems_stop_the_other_rules(tmp_path: Path) -> None:
    elsewhere: dict[str, str | None] = {
        "stockroom/__init__.py": '__version__ = "9.9.9"\n',
        "README.md": None,
    }
    cases = {
        "missing": None,
        "invalid": "[project\nname = 'x'\n",
        "no project": '[tool.setuptools]\npackages = ["stockroom"]\n',
        "no name": '[project]\nversion = "1.2.3"\n',
        "name not text": '[project]\nname = 5\nversion = "1.2.3"\n',
        "empty": "",
    }
    for label, content in cases.items():
        root = project(tmp_path, {**elsewhere, "pyproject.toml": content})
        done = check(root)
        assert done.returncode == 1, label
        assert len(lines_of(done)) == 1 and lines_of(done)[0].startswith("pyproject: "), label
    assert_problems(check(tmp_path / "does-not-exist"), ("pyproject",))
    empty = tmp_path / "empty-dir"
    empty.mkdir()
    assert_problems(check(empty), ("pyproject",))


def test_version_rule(tmp_path: Path) -> None:
    root = project(tmp_path, {"stockroom/__init__.py": '__version__ = "2.0.0"\n'})
    assert_problems(check(root), ("version",))
    # quote styles, spacing, a trailing comment, and only the first assignment counts
    for init in (
        "__version__='1.2.3'\n",
        '__version__   =   "1.2.3"   # current release\n',
        '"""doc"""\n\nimport os\n__version__ = "1.2.3"\nother = 1\n',
        '__version__ = "1.2.3"\n__version__ = "9.9.9"\n',
    ):
        assert_ok(check(project(tmp_path, {"stockroom/__init__.py": init})))
    swapped = '__version__ = "9.9.9"\n__version__ = "1.2.3"\n'
    assert_problems(check(project(tmp_path, {"stockroom/__init__.py": swapped})), ("version",))
    # a missing __version__, or a missing file
    for init in ("", "version = '1.2.3'\n", "x = 1\n", None):
        done = check(project(tmp_path, {"stockroom/__init__.py": init}))
        assert_problems(done, ("version", "__version__"))

    dynamic = """\
[project]
name = "demo"
dynamic = ["version", "readme"]

[project.scripts]
demo = "stockroom.cli:main"

[tool.setuptools]
packages = ["stockroom"]

[tool.setuptools.dynamic]
version = {attr = "stockroom.__version__"}
"""
    assert_ok(check(project(tmp_path, {"pyproject.toml": dynamic})))
    wrong = dynamic.replace("stockroom.__version__", "stockroom.VERSION")
    assert_problems(check(project(tmp_path, {"pyproject.toml": wrong})), ("version",))
    no_table = dynamic.split("[tool.setuptools.dynamic]")[0]
    assert_problems(check(project(tmp_path, {"pyproject.toml": no_table})), ("version",))
    other_key = dynamic.replace('dynamic = ["version", "readme"]', 'dynamic = ["readme"]')
    assert_problems(check(project(tmp_path, {"pyproject.toml": other_key})), ("version",))
    neither = PYPROJECT.replace('version = "1.2.3"\n', "")
    assert_problems(check(project(tmp_path, {"pyproject.toml": neither})), ("version",))
    pre = PYPROJECT.replace("1.2.3", "1.2.3rc1")
    same = {"pyproject.toml": pre, "stockroom/__init__.py": '__version__ = "1.2.3rc1"\n'}
    assert_ok(check(project(tmp_path, same)))
    assert_problems(check(project(tmp_path, {"pyproject.toml": pre})), ("version",))


def test_script_rule(tmp_path: Path) -> None:
    def with_scripts(entries: str) -> str:
        text = PYPROJECT.replace('demo = "stockroom.cli:main"\n', entries)
        return text.replace('packages = ["stockroom"]', 'packages = ["stockroom", "stockroom.sub"]')

    tree: dict[str, str | None] = {
        "stockroom/sub/__init__.py": "def run():\n    pass\n",
        "stockroom/cli2.py": "class A:\n    def run(self):\n        pass\n",
        "README.md": README + "| `stockroom/cli2.py` | second |\n",
    }
    good = 'demo = "stockroom.cli:main"\npkg = "stockroom.sub:run"\n'
    root = project(tmp_path, {**tree, "pyproject.toml": with_scripts(good)})
    assert_ok(check(root))

    bad = (
        'demo = "stockroom.cli:main"\n'
        'nocolon = "stockroom.cli"\n'
        'nomodule = "stockroom.nomodule:main"\n'
        'nofunc = "stockroom.cli:missing"\n'
        'dotted = "stockroom.cli:Model.method"\n'
        'digit = "1bad.mod:x"\n'
        'empty = ""\n'
        'method = "stockroom.cli2:run"\n'
        'pkg = "stockroom.sub:run"\n'
    )
    root = project(tmp_path, {**tree, "pyproject.toml": with_scripts(bad)})
    done = check(root)
    assert done.returncode == 1, done.stdout
    lines = lines_of(done)
    assert lines == sorted(lines)
    assert len(lines) == 7, lines
    for name in ("nocolon", "nomodule", "nofunc", "dotted", "digit", "empty", "method"):
        found = [ln for ln in lines if ln.startswith(f"script: {name}")]
        assert len(found) == 1, (name, lines)
    # a module that is a plain file and one that is a package directory both count
    only_ok = 'demo = "stockroom.cli:main"\npkg = "stockroom.sub:run"\nmoney = "stockroom.money:go"\n'
    both = {**tree, "pyproject.toml": with_scripts(only_ok), "stockroom/money.py": "def go(): ...\n"}
    assert_ok(check(project(tmp_path, both)))
    none = {**tree, "pyproject.toml": with_scripts("")}
    assert_ok(check(project(tmp_path, none)))


def test_packages_rule(tmp_path: Path) -> None:
    def listing(names: list[str]) -> str:
        quoted = ", ".join(f'"{n}"' for n in names)
        return PYPROJECT.replace('packages = ["stockroom"]', f"packages = [{quoted}]")

    tree: dict[str, str | None] = {
        "stockroom/sub/__init__.py": "",
        "stockroom/sub/deep/__init__.py": "",
        "stockroom/sub/deep/leaf.py": "",
        "stockroom/assets/data.txt": "x",
        "stockroom/loose/inner/__init__.py": "",
        "stockroom/sub/plain/below/__init__.py": "",
    }

    def build(names: list[str]) -> Path:
        return project(tmp_path, {**tree, "pyproject.toml": listing(names)})

    assert_problems(
        check(build(["stockroom"])),
        ("packages", "stockroom.sub"),
        ("packages", "stockroom.sub.deep"),
    )
    both = ["stockroom", "stockroom.sub", "stockroom.sub.deep"]
    assert_ok(check(build(both)))
    done = check(build(["stockroom", "stockroom.sub"]))
    assert_problems(done, ("packages", "stockroom.sub.deep"))
    for ghost in ("stockroom.ghost", "stockroom.assets", "stockroom.loose", "other"):
        assert_problems(check(build([*both, ghost])), ("packages", ghost))
    done = check(build(["stockroom.sub", "stockroom.sub.deep"]))
    assert_problems(done, ("packages", "stockroom"))
    find = PYPROJECT.replace(
        'packages = ["stockroom"]', '\n[tool.setuptools.packages.find]\ninclude = ["stockroom*"]'
    )
    assert_ok(check(project(tmp_path, {**tree, "pyproject.toml": find})))
    bare = PYPROJECT.split("[tool.setuptools]")[0]
    assert_ok(check(project(tmp_path, {**tree, "pyproject.toml": bare})))


def test_readme_rule(tmp_path: Path) -> None:
    root = project(tmp_path, {"stockroom/extra.py": "", "stockroom/_hidden.py": ""})
    assert_problems(check(root), ("readme", "stockroom/extra.py"))
    root = project(tmp_path, {"scripts/other.py": "", "scripts/_helper.py": ""})
    assert_problems(check(root), ("readme", "scripts/other.py"), ("readme", "scripts/_helper.py"))
    stale = README + "| `stockroom/ghost.py` | gone |\n| `scripts/ghost.py` | gone |\n"
    assert_problems(
        check(project(tmp_path, {"README.md": stale})),
        ("readme", "stockroom/ghost.py"),
        ("readme", "scripts/ghost.py"),
    )
    ignored = (
        "|`stockroom/cli.py`|the command\n"
        "| `stockroom/money.py` | cents |\n"
        "|   `scripts/tool.py`   | a tool |\n"
        "| `scripts/tool.py` | again |\n"
        "| `tests/test_x.py` | ignored |\n"
        "| `stockroom/sub/inner.py` | ignored |\n"
        "| `stockroom/README` | ignored |\n"
        "| `data/` | ignored |\n"
        "text with `stockroom/ghost.py` inside a sentence\n"
        " | `stockroom/ghost2.py` | an indented line is not a row |\n"
    )
    assert_ok(check(project(tmp_path, {"README.md": ignored})))
    not_rows = README.replace("| `stockroom/money.py` | cents |", "see `stockroom/money.py` for cents")
    done = check(project(tmp_path, {"README.md": not_rows}))
    assert_problems(done, ("readme", "stockroom/money.py"))
    done = check(project(tmp_path, {"README.md": None, "stockroom/extra.py": ""}))
    assert_problems(done, ("readme", "README.md"))
    assert lines_of(done) == ["readme: README.md is missing"]
    root = project(
        tmp_path,
        {
            "stockroom/sub/__init__.py": "",
            "stockroom/sub/inner.py": "",
            "stockroom/notes.txt": "",
            "pyproject.toml": PYPROJECT.replace('["stockroom"]', '["stockroom", "stockroom.sub"]'),
        },
    )
    assert_ok(check(root))


def test_output_format_order_and_exit_codes(tmp_path: Path) -> None:
    broken = PYPROJECT.replace('demo = "stockroom.cli:main"', 'demo = "stockroom.cli:nope"')
    root = project(
        tmp_path,
        {
            "stockroom/__init__.py": '__version__ = "0.0.1"\n',
            "stockroom/extra.py": "",
            "scripts/zzz.py": "",
            "stockroom/sub/__init__.py": "",
            "pyproject.toml": broken,
        },
    )
    done = check(root)
    assert_problems(
        done,
        ("version",),
        ("script", "demo"),
        ("packages", "stockroom.sub"),
        ("readme", "stockroom/extra.py"),
        ("readme", "scripts/zzz.py"),
    )
    lines = lines_of(done)
    assert all(": " in ln for ln in lines)
    assert check(root, "--bogus").returncode == 2
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--root"],
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    assert done.returncode == 2 and done.stdout == ""
    # the default root is the directory above the directory of the script itself
    copy = project(tmp_path, {"README.md": README + "| `scripts/check_package.py` | this check |\n"})
    shutil.copy(SCRIPT, copy / "scripts" / "check_package.py")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    def run_copy() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(copy / "scripts" / "check_package.py")],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=50,
            check=False,
        )

    done = run_copy()
    assert done.returncode == 0 and done.stdout == "ok\n" and done.stderr == ""
    (copy / "stockroom" / "__init__.py").write_text('__version__ = "0.0.9"\n', encoding="utf-8")
    done = run_copy()
    assert done.returncode == 1 and done.stdout.startswith("version: ")
