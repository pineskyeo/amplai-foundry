from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "bump_version.py"

PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]

[tool.before]
version = "{v}"

[project]
name = "demo"
version = "{v}"
description = "A demo"

[project.urls]
version = "{v}"

[tool.after]
version = "{v}"
"""
INIT = '"""demo."""\n\n__version__ = "{v}"\n'


def make(tmp_path: Path, current: str = "0.4.0", *, py: str | None = None, init: str | None = None,
         name: str = "proj") -> Path:
    root = tmp_path / name
    shutil.rmtree(root, ignore_errors=True)
    (root / "stockroom").mkdir(parents=True)
    (root / "pyproject.toml").write_bytes((py or PYPROJECT).replace("{v}", current).encode())
    (root / "stockroom" / "__init__.py").write_bytes((init or INIT).replace("{v}", current).encode())
    return root


def files(root: Path) -> dict[str, bytes]:
    return {
        "pyproject.toml": (root / "pyproject.toml").read_bytes(),
        "stockroom/__init__.py": (root / "stockroom" / "__init__.py").read_bytes(),
    }


def bump(root: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--root", str(root)],
        cwd=cwd or root.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def assert_done(done: subprocess.CompletedProcess[str], old: str, new: str) -> None:
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"{old} -> {new}\n"
    assert done.stderr == ""


def assert_error(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert len(lines) == 1, done.stderr
    assert lines[0].startswith("bump_version: error: ") and "Traceback" not in done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def test_keywords_raise_the_right_part(tmp_path: Path) -> None:
    cases = [
        ("patch", "0.4.0", "0.4.1"),
        ("minor", "0.4.7", "0.5.0"),
        ("major", "0.4.7", "1.0.0"),
        ("patch", "0.9.9", "0.9.10"),
        ("minor", "0.9.9", "0.10.0"),
        ("patch", "1.2.99", "1.2.100"),
        ("major", "9.9.9", "10.0.0"),
        ("minor", "3.0.0", "3.1.0"),
    ]
    for keyword, current, new in cases:
        root = make(tmp_path, current)
        done = bump(root, keyword)
        assert_done(done, current, new)
        assert files(root) == {
            "pyproject.toml": PYPROJECT.replace("{v}", current)
            .replace(f'[project]\nname = "demo"\nversion = "{current}"', f'[project]\nname = "demo"\nversion = "{new}"')
            .encode(),
            "stockroom/__init__.py": INIT.replace("{v}", new).encode(),
        }, (keyword, current)
    # two bumps in a row
    root = make(tmp_path, "0.4.0", name="twice")
    assert_done(bump(root, "patch"), "0.4.0", "0.4.1")
    assert_done(bump(root, "minor"), "0.4.1", "0.5.0")
    assert_done(bump(root, "0.5.1"), "0.5.0", "0.5.1")
    assert b'__version__ = "0.5.1"' in files(root)["stockroom/__init__.py"]


def test_explicit_versions_must_be_greater(tmp_path: Path) -> None:
    root = make(tmp_path, "0.9.9")
    assert_done(bump(root, "0.10.0"), "0.9.9", "0.10.0")
    assert b'__version__ = "0.10.0"' in files(root)["stockroom/__init__.py"]
    root = make(tmp_path, "0.4.0", name="big")
    assert_done(bump(root, "12.0.3"), "0.4.0", "12.0.3")
    root = make(tmp_path, "0.4.0", name="zero")
    assert_done(bump(root, "0.4.1"), "0.4.0", "0.4.1")
    for current, given in (
        ("0.10.0", "0.9.10"),
        ("0.4.0", "0.4.0"),
        ("1.0.0", "0.99.99"),
        ("2.0.0", "1.9.9"),
        ("0.4.10", "0.4.9"),
    ):
        root = make(tmp_path, current, name="low")
        before = files(root)
        done = bump(root, given)
        assert_error(done, current, given)
        assert files(root) == before
    root = make(tmp_path, "0.9.10", name="numeric")
    assert_done(bump(root, "0.10.0"), "0.9.10", "0.10.0")


def test_only_the_version_text_changes(tmp_path: Path) -> None:
    pyproject = (
        "# packaging\r\n"
        "[[tool.items]]\r\n"
        "version = \"0.4.0\"\r\n"
        "\r\n"
        "[tool.before]\r\n"
        "version = '0.4.0'\r\n"
        "\r\n"
        "[project]   # the project\r\n"
        "name = 'demo'\r\n"
        "classifiers = [\r\n"
        "    \"Programming Language :: Python\",\r\n"
        "]\r\n"
        "version_note = \"version = 0.4.0\"\r\n"
        "  version   =   '0.4.0'   # keep this comment\r\n"
        "description = \"version = 0.4.0\"\r\n"
        "\r\n"
        "[project.scripts]\r\n"
        "version = \"demo.cli:main\"\r\n"
        "\r\n"
        "[tool.after]\r\n"
        "version = \"0.4.0\""
    )
    init = (
        "import os\n\n__version__='0.4.0'  # keep me\nversion_info = (0, 4, 0)\n"
        '__version__ = "0.4.0"\n'
    )
    root = make(tmp_path, py=pyproject, init=init)
    before = files(root)
    assert_done(bump(root, "minor"), "0.4.0", "0.5.0")
    after = files(root)
    expected_py = pyproject.replace(
        "  version   =   '0.4.0'   # keep this comment", "  version   =   '0.5.0'   # keep this comment"
    )
    assert after["pyproject.toml"] == expected_py.encode()
    assert after["pyproject.toml"].count(b"\r\n") == pyproject.count("\r\n")
    assert not after["pyproject.toml"].endswith(b"\n")
    expected_init = init.replace("__version__='0.4.0'  # keep me", "__version__='0.5.0'  # keep me")
    assert after["stockroom/__init__.py"] == expected_init.encode()
    assert after["stockroom/__init__.py"].endswith(b'__version__ = "0.4.0"\n')
    assert before != after

    # the [project] table is the last table of the file, with LF line ends and no other tables
    tiny_py = '[project]\nname = "x"\nversion = "1.0.0"'
    tiny_init = "__version__ = \"1.0.0\""
    root = make(tmp_path, py=tiny_py, init=tiny_init, name="tiny")
    assert_done(bump(root, "patch"), "1.0.0", "1.0.1")
    assert files(root) == {
        "pyproject.toml": b'[project]\nname = "x"\nversion = "1.0.1"',
        "stockroom/__init__.py": b'__version__ = "1.0.1"',
    }
    # a version key before the [project] header with the very same text is not touched
    root = make(tmp_path, "0.4.0", name="same")
    assert_done(bump(root, "major"), "0.4.0", "1.0.0")
    text = files(root)["pyproject.toml"].decode()
    assert text.count('version = "0.4.0"') == 3 and text.count('version = "1.0.0"') == 1
    assert text.index('version = "1.0.0"') > text.index("[project]\n")
    assert text.index('version = "1.0.0"') < text.index("[project.urls]")


def test_errors_change_nothing(tmp_path: Path) -> None:
    root = make(tmp_path, "0.4.0", init=INIT.replace("{v}", "0.3.0"), name="differ")
    before = files(root)
    assert_error(bump(root, "patch"), "0.4.0", "0.3.0")
    assert files(root) == before
    assert_error(bump(root, "9.9.9"), "0.4.0", "0.3.0")
    assert files(root) == before

    root = make(tmp_path, name="nopy")
    (root / "pyproject.toml").unlink()
    init_before = files_init(root)
    assert_error(bump(root, "patch"), "pyproject.toml")
    assert files_init(root) == init_before

    root = make(tmp_path, name="noinit")
    (root / "stockroom" / "__init__.py").unlink()
    assert_error(bump(root, "patch"), "__init__.py")
    assert b'version = "0.4.0"' in (root / "pyproject.toml").read_bytes()

    dynamic = '[project]\nname = "x"\ndynamic = ["version"]\n\n[tool.other]\nversion = "0.4.0"\n'
    root = make(tmp_path, py=dynamic, name="dynamic")
    before = files(root)
    assert_error(bump(root, "patch"), "pyproject.toml")
    assert files(root) == before

    for bad in ("0.4", "0.4.0rc1", "v0.4.0", "0.4.0.1", "x"):
        py = f'[project]\nname = "x"\nversion = "{bad}"\n'
        root = make(tmp_path, py=py, name="badpy")
        before = files(root)
        assert_error(bump(root, "patch"), "pyproject.toml")
        assert files(root) == before
    for bad in ("0.4", "0.4.0rc1"):
        root = make(tmp_path, init=f'__version__ = "{bad}"\n', name="badinit")
        before = files(root)
        assert_error(bump(root, "patch"), "__init__.py")
        assert files(root) == before

    no_line = make(tmp_path, init="VERSION = '0.4.0'\n", name="noline")
    before = files(no_line)
    assert_error(bump(no_line, "minor"), "__init__.py")
    assert files(no_line) == before

    # a version key outside [project] does not count as the project version
    outside = '[tool.x]\nversion = "0.4.0"\n\n[project]\nname = "x"\n'
    root = make(tmp_path, py=outside, name="outside")
    before = files(root)
    assert_error(bump(root, "patch"), "pyproject.toml")
    assert files(root) == before
    # the error for a bad explicit version never reaches the files either
    root = make(tmp_path, name="same-version")
    assert_error(bump(root, "0.4.0"), "0.4.0")


def files_init(root: Path) -> bytes:
    return (root / "stockroom" / "__init__.py").read_bytes()


def test_invalid_new_values_are_usage_errors(tmp_path: Path) -> None:
    root = make(tmp_path)
    before = files(root)
    for bad in ("1.2", "v1.2.3", "01.2.3", "1.02.3", "1.2.03", "1.2.3.4", "1.2.x", "", "MAJOR",
                "Patch", "1.2.-3", "1.2.3 ", " 1.2.3", "1,2,3", "1.2.3-rc1", "+1.2.3", "１.２.３"):
        done = bump(root, bad)
        assert done.returncode == 2, (bad, done.returncode, done.stderr)
        assert done.stdout == ""
        assert done.stderr != ""
        assert files(root) == before, bad
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root)], env=env,
                          capture_output=True, text=True, timeout=50, check=False)
    assert done.returncode == 2 and files(root) == before
    done = subprocess.run([sys.executable, str(SCRIPT), "patch", "minor", "--root", str(root)],
                          env=env, capture_output=True, text=True, timeout=50, check=False)
    assert done.returncode == 2 and files(root) == before
    assert bump(root, "patch", "--bogus").returncode == 2
    assert files(root) == before
    # valid spellings that look unusual
    assert_done(bump(root, "0.4.1"), "0.4.0", "0.4.1")
    assert_done(bump(root, "10.20.30"), "0.4.1", "10.20.30")
    assert_done(bump(root, "10.20.300"), "10.20.30", "10.20.300")


def test_dry_run_and_the_default_root(tmp_path: Path) -> None:
    root = make(tmp_path, "0.9.9")
    before = files(root)
    assert_done(bump(root, "minor", "--dry-run"), "0.9.9", "0.10.0")
    assert_done(bump(root, "--dry-run", "0.9.10"), "0.9.9", "0.9.10")
    assert files(root) == before
    # the same checks apply to a dry run
    assert_error(bump(root, "0.9.9", "--dry-run"), "0.9.9")
    differ = make(tmp_path, "0.4.0", init=INIT.replace("{v}", "0.4.1"), name="differ")
    assert_error(bump(differ, "patch", "--dry-run"), "0.4.0", "0.4.1")
    assert bump(root, "1.x", "--dry-run").returncode == 2
    assert files(root) == before
    # the repository itself, from another directory, without --root
    repo_before = {
        "pyproject.toml": (ROOT / "pyproject.toml").read_bytes(),
        "stockroom/__init__.py": (ROOT / "stockroom" / "__init__.py").read_bytes(),
    }
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "patch", "--dry-run"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    assert_done(done, "0.4.0", "0.4.1")
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "major", "--dry-run", "--root", str(ROOT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    assert_done(done, "0.4.0", "1.0.0")
    assert repo_before == {
        "pyproject.toml": (ROOT / "pyproject.toml").read_bytes(),
        "stockroom/__init__.py": (ROOT / "stockroom" / "__init__.py").read_bytes(),
    }
    # the default root is the directory above the script's own directory
    copy = make(tmp_path, "2.3.4", name="copy")
    (copy / "scripts").mkdir()
    (copy / "scripts" / "bump_version.py").write_bytes(SCRIPT.read_bytes())
    done = subprocess.run(
        [sys.executable, str(copy / "scripts" / "bump_version.py"), "patch"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    assert_done(done, "2.3.4", "2.3.5")
    assert b'__version__ = "2.3.5"' in files(copy)["stockroom/__init__.py"]
