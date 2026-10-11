from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "build_sdist.py"
TABLE = """
[tool.stockroom.sdist]
include = ["stockroom/**/*.py", "README.md", "scripts/*.py"]
exclude = ["__pycache__", "*.pyc"]
"""
PKG_INFO = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.2.3\n"


def build(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def project(
    tmp_path: Path, table: str = TABLE, *, version: str = "1.2.3", name: str = "proj"
) -> Path:
    root = tmp_path / name
    shutil.rmtree(root, ignore_errors=True)
    files = {
        "pyproject.toml": f'[project]\nname = "demo"\nversion = "1.2.3"\n{table}',
        "README.md": "# demo\n",
        "stockroom/__init__.py": f'"""demo."""\n\n__version__ = "{version}"\n',
        "stockroom/core.py": "def main():\n    return 1\n",
        "stockroom/sub/__init__.py": "",
        "stockroom/sub/deep.py": "X = 'deep'\n",
        "stockroom/__pycache__/core.cpython-311.pyc": "binary",
        "scripts/tool.py": "print('tool')\n",
        "scripts/notes.txt": "not python\n",
        "build/lib/stale.py": "old = True\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def names(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive) as z:
        return [info.filename for info in z.infolist()]


def assert_error(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("build_sdist: error: "), done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def test_the_repository_archive(tmp_path: Path) -> None:
    done = build("--out", str(tmp_path))
    assert done.returncode == 0, done.stderr
    archive = tmp_path / "stockroom-0.4.0.zip"
    expected = ["pyproject.toml", "README.md", "PKG-INFO"]
    expected += [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "stockroom").rglob("*.py")
        if "__pycache__" not in p.parts
    ]
    expected += [p.relative_to(ROOT).as_posix() for p in (ROOT / "scripts").glob("*.py")]
    assert "scripts/build_sdist.py" in expected and "stockroom/cli.py" in expected
    want = sorted(f"stockroom-0.4.0/{rel}" for rel in expected)
    assert names(archive) == want
    assert done.stdout == f"wrote {archive} ({len(want)} files)\n"
    assert done.stderr == ""
    with zipfile.ZipFile(archive) as z:
        assert (
            z.read("stockroom-0.4.0/stockroom/cli.py")
            == (ROOT / "stockroom" / "cli.py").read_bytes()
        )
        assert z.read("stockroom-0.4.0/PKG-INFO") == (
            b"Metadata-Version: 2.1\nName: stockroom\nVersion: 0.4.0\n"
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stockroom-0.4.0.zip"]
    # the default root does not depend on the current directory
    again = tmp_path / "again"
    again.mkdir()
    done = build("--out", "x", cwd=again)
    assert done.returncode == 0, done.stderr
    assert (again / "x" / "stockroom-0.4.0.zip").read_bytes() == archive.read_bytes()


def test_entries_are_normalised(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "scripts" / "tool.py").chmod(0o755)
    done = build("--root", str(root), "--out", str(tmp_path / "out"))
    assert done.returncode == 0, done.stderr
    archive = tmp_path / "out" / "demo-1.2.3.zip"
    assert done.stdout == f"wrote {archive} (7 files)\n"
    want = [
        "demo-1.2.3/PKG-INFO",
        "demo-1.2.3/README.md",
        "demo-1.2.3/scripts/tool.py",
        "demo-1.2.3/stockroom/__init__.py",
        "demo-1.2.3/stockroom/core.py",
        "demo-1.2.3/stockroom/sub/__init__.py",
        "demo-1.2.3/stockroom/sub/deep.py",
    ]
    assert names(archive) == want
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0), info.filename
            assert info.compress_type == zipfile.ZIP_DEFLATED, info.filename
            assert info.external_attr >> 16 == 0o100644, info.filename
            assert info.create_system == 3, info.filename
            assert not info.filename.endswith("/")
        assert z.read("demo-1.2.3/PKG-INFO") == PKG_INFO
        assert z.read("demo-1.2.3/stockroom/sub/__init__.py") == b""
        assert z.read("demo-1.2.3/scripts/tool.py") == b"print('tool')\n"


def test_a_rebuild_is_byte_identical(tmp_path: Path) -> None:
    root = project(tmp_path)
    assert build("--root", str(root), "--out", str(tmp_path / "a")).returncode == 0
    first = (tmp_path / "a" / "demo-1.2.3.zip").read_bytes()
    # new modification times, modes, creation order and caches do not matter
    for i, path in enumerate(sorted(root.rglob("*"))):
        os.utime(path, (1_000_000_000 + i * 977, 1_500_000_000 + i * 3_581))
    (root / "stockroom" / "core.py").chmod(0o700)
    text = (root / "stockroom" / "sub" / "deep.py").read_bytes()
    (root / "stockroom" / "sub" / "deep.py").unlink()
    (root / "stockroom" / "sub" / "zz.tmp").write_text("x", encoding="utf-8")
    (root / "stockroom" / "sub" / "deep.py").write_bytes(text)
    (root / "stockroom" / "sub" / "zz.tmp").unlink()
    (root / "stockroom" / "sub" / "__pycache__").mkdir()
    (root / "stockroom" / "sub" / "__pycache__" / "deep.cpython-311.pyc").write_text("c")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = build("--root", "../proj", "--out", "b", cwd=elsewhere)
    assert done.returncode == 0, done.stderr
    assert (elsewhere / "b" / "demo-1.2.3.zip").read_bytes() == first
    # and a rebuild into the same place replaces the file with the same bytes
    assert build("--root", str(root), "--out", str(tmp_path / "a")).returncode == 0
    assert (tmp_path / "a" / "demo-1.2.3.zip").read_bytes() == first
    assert sorted(p.name for p in (tmp_path / "a").iterdir()) == ["demo-1.2.3.zip"]


def test_include_and_exclude_rules(tmp_path: Path) -> None:
    table = """
[tool.stockroom.sdist]
include = ["stockroom/**/*.py", "stockroom/*.py", "*.md", "scripts/*", "stockroom/*"]
exclude = ["__pycache__", "*.pyc", "deep.py", "build"]
"""
    root = project(tmp_path, table)
    for rel in ("stockroom/build/x.py", "stockroom/rebuild/z.py", "stockroom/data.json"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("1\n", encoding="utf-8")
    done = build("--root", str(root), "--out", str(tmp_path / "out"))
    assert done.returncode == 0, done.stderr
    assert names(tmp_path / "out" / "demo-1.2.3.zip") == [
        "demo-1.2.3/PKG-INFO",
        "demo-1.2.3/README.md",
        "demo-1.2.3/scripts/notes.txt",
        "demo-1.2.3/scripts/tool.py",
        "demo-1.2.3/stockroom/__init__.py",
        "demo-1.2.3/stockroom/core.py",
        "demo-1.2.3/stockroom/data.json",
        "demo-1.2.3/stockroom/rebuild/z.py",
        "demo-1.2.3/stockroom/sub/__init__.py",
    ]
    assert done.stdout.endswith("(9 files)\n")
    # a pattern whose files are all excluded, or that matches only directories, is an error
    for pattern in ("stockroom/sub/deep.py", "build/**/*.py", "stockroom/sub", "nothing/*.py"):
        table = (
            f'\n[tool.stockroom.sdist]\ninclude = ["README.md", "{pattern}"]\n'
            'exclude = ["deep.py", "build"]\n'
        )
        root = project(tmp_path, table)
        assert_error(build("--root", str(root), "--out", str(tmp_path / "bad")), pattern)
        assert not (tmp_path / "bad").exists()


def test_output_directory_precedence(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for table_out in (None, "artifacts"):
        for option in (None, "o"):
            table = TABLE + (f'out = "{table_out}"\n' if table_out else "")
            root = project(tmp_path, table)
            shutil.rmtree(elsewhere)
            elsewhere.mkdir()
            args = ["--root", "../proj"] + (["--out", option] if option else [])
            done = build(*args, cwd=elsewhere)
            assert done.returncode == 0, (table_out, option, done.stderr)
            if option:
                shown, real = Path(option), elsewhere / option
            elif table_out:
                shown, real = Path("../proj") / table_out, root / table_out
            else:
                shown, real = Path("../proj") / "dist", root / "dist"
            assert done.stdout == f"wrote {shown / 'demo-1.2.3.zip'} (7 files)\n", (
                table_out,
                option,
            )
            assert (real / "demo-1.2.3.zip").is_file(), (table_out, option)
            for other in (elsewhere / "o", root / "artifacts", root / "dist"):
                if other != real:
                    assert not other.exists(), (table_out, option, other)


def test_check_mode_compares_archive_bytes(tmp_path: Path) -> None:
    root = project(tmp_path)
    out = tmp_path / "out"
    archive = out / "demo-1.2.3.zip"
    done = build("--root", str(root), "--out", str(out), "--check")
    assert done.returncode == 1 and done.stdout == f"missing {archive}\n" and done.stderr == ""
    assert not out.exists()
    assert build("--root", str(root), "--out", str(out)).returncode == 0
    stamp = archive.stat().st_mtime_ns, archive.stat().st_ino
    done = build("--root", str(root), "--out", str(out), "--check")
    assert done.returncode == 0 and done.stdout == f"up to date {archive}\n"
    (root / "stockroom" / "core.py").write_text("def main():\n    return 2\n", encoding="utf-8")
    done = build("--check", "--root", str(root), "--out", str(out))
    assert done.returncode == 1 and done.stdout == f"out of date {archive}\n"
    (root / "stockroom" / "core.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    os.utime(root / "stockroom" / "core.py", (2_000_000_000, 2_000_000_000))
    done = build("--check", "--root", str(root), "--out", str(out))
    assert done.returncode == 0 and done.stdout == f"up to date {archive}\n"
    assert (archive.stat().st_mtime_ns, archive.stat().st_ino) == stamp
    assert sorted(p.name for p in out.iterdir()) == ["demo-1.2.3.zip"]
    # errors in check mode are errors
    (root / "stockroom" / "__init__.py").write_text('__version__ = "2.0.0"\n', encoding="utf-8")
    assert_error(build("--check", "--root", str(root), "--out", str(out)), "1.2.3", "2.0.0")


def test_configuration_errors(tmp_path: Path) -> None:
    out = tmp_path / "out"
    cases = [
        ("", []),
        ('\n[tool.stockroom.sdist]\nexclude = ["*.pyc"]\n', ["include"]),
        ("\n[tool.stockroom.sdist]\ninclude = []\n", ["include"]),
        ('\n[tool.stockroom.sdist]\ninclude = "README.md"\n', ["include"]),
        ('\n[tool.stockroom.sdist]\ninclude = ["/etc/*.conf"]\n', ["/etc/*.conf"]),
        ('\n[tool.stockroom.sdist]\ninclude = ["../outside/*.py"]\n', ["../outside/*.py"]),
        (
            '\n[tool.stockroom.sdist]\ninclude = ["stockroom/../README.md"]\n',
            ["stockroom/../README.md"],
        ),
        ("\n[tool.stockroom.sdist]\ninclude = [\n", []),
    ]
    for table, needles in cases:
        root = project(tmp_path, table)
        assert_error(build("--root", str(root), "--out", str(out)), *needles)
        assert not out.exists(), table
    root = project(tmp_path, version="9.9.9")
    assert_error(build("--root", str(root), "--out", str(out)), "1.2.3", "9.9.9")
    root = project(tmp_path)
    (root / "pyproject.toml").unlink()
    assert_error(build("--root", str(root), "--out", str(out)), "pyproject.toml")
    root = project(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert_error(build("--root", str(root), "--out", str(blocker)), str(blocker))
    assert blocker.read_text(encoding="utf-8") == "x"
    assert not out.exists()
    done = build("--root", str(root), "--frobnicate")
    assert done.returncode == 2 and done.stdout == ""


def test_output_is_replaced_atomically(tmp_path: Path) -> None:
    root = project(tmp_path)
    out = tmp_path / "deep" / "er" / "out"
    done = build("--root", str(root), "--out", str(out))
    assert done.returncode == 0, done.stderr
    good = (out / "demo-1.2.3.zip").read_bytes()
    (out / "demo-1.2.3.zip").write_bytes(b"junk" * 10000)
    (out / "keep.txt").write_text("keep", encoding="utf-8")
    done = build("--root", str(root), "--out", str(out))
    assert done.returncode == 0, done.stderr
    assert (out / "demo-1.2.3.zip").read_bytes() == good
    assert sorted(p.name for p in out.iterdir()) == ["demo-1.2.3.zip", "keep.txt"]
    with zipfile.ZipFile(out / "demo-1.2.3.zip") as z:
        assert z.testzip() is None
