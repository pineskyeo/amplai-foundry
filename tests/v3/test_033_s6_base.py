"""Work 033 S6-base (D-098) - the bench base app and its reproducible base repository.

Contract: specs/033-harness-taxonomy/interfaces.md §10.1 (layout, ``bases/bench.commit``, fixed
author/committer/date), §10.3 (visible tests pass on the base), §13 row S6-base, §14 Q17 (the
demo base commit). The script under test is ``scripts/corpus_base_repo.py``; git runs locally in
``tmp_path``, no network, no docker.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from amplai_foundry.meta_harness import corpus_v2, local_corpus

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "corpus_base_repo.py"
V2_ROOT = REPO / "specs" / "033-harness-taxonomy" / "corpus"
BENCH = V2_ROOT / "bases" / "bench"
DEMO = V2_ROOT / "bases" / "demo"
W030_BASE = REPO / "specs" / "030-meta-harness-live" / "corpus" / "base"
DEMO_COMMIT = "9e574bcd0270ad8ef6e27aa6f6b757d7c032c06b"  # Work 030 manifest, §10.1
SHA1 = re.compile(r"[0-9a-f]{40}")


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("corpus_base_repo", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base_repo = _load_script()


def _script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=60
    ).stdout.decode()


def _bench_commit() -> str:
    return (V2_ROOT / "bases" / "bench.commit").read_text().strip()


# -- the bench app: visible tests ----------------------------------------------------------------


def _visible(workspace: Path) -> subprocess.CompletedProcess[bytes]:
    """The visible run exactly as ``local_corpus.judge`` makes it (§8.3)."""
    env = {**os.environ, "PYTHONPATH": str(workspace), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [*local_corpus.PYTEST, "--ignore", str(local_corpus.HIDDEN_DIR), "tests"],
        cwd=workspace,
        env=env,
        capture_output=True,
        timeout=300,
        check=False,
    )


def test_bench_visible_tests_pass_on_the_base(tmp_path: Path) -> None:
    work = tmp_path / "bench"
    shutil.copytree(BENCH, work)
    done = _visible(work)
    out = (done.stdout + done.stderr).decode(errors="replace")
    assert done.returncode == 0, out[-3000:]
    match = re.search(r"(\d+) passed", out)
    assert match and int(match.group(1)) >= 60, out[-500:]
    assert " failed" not in out and " error" not in out


def test_bench_is_a_multi_module_app_with_an_entry_point() -> None:
    modules = {p.stem for p in (BENCH / "stockroom").glob("*.py")} - {"__init__", "__main__"}
    assert len(modules) >= 10  # several modules per task domain (§10.3: feature touches >= 2)
    assert (BENCH / "stockroom" / "__main__.py").is_file()  # cli_ops: subprocess entry point
    assert 'stockroom = "stockroom.cli:main"' in (BENCH / "pyproject.toml").read_text()
    tested = {p.stem.removeprefix("test_") for p in (BENCH / "tests").glob("test_*.py")}
    assert modules - {"errors"} <= tested  # every module has visible tests
    assert not (BENCH / "tests" / "hidden").exists()  # the judge's hidden directory is free


def test_judge_runs_on_the_bench_base(tmp_path: Path) -> None:
    """The grading path S6 authors use: hidden tests applied to a bench copy (§10.3 rule 1)."""
    passing = local_corpus.CorpusTask(
        "probe-pass",
        "small",
        "probe",
        ("probe",),
        {
            "tests/hidden/probe-pass/test_probe.py": (
                b"from stockroom.money import format_money\n\n\n"
                b"def test_probe():\n    assert format_money(1234) == '$12.34'\n"
            )
        },
        {},
    )
    failing = replace(
        passing,
        task_id="probe-fail",
        hidden={
            "tests/hidden/probe-fail/test_probe.py": (
                b"from stockroom.textutil import shout\n\n\n"
                b"def test_probe():\n    assert shout('a') == 'A!'\n"
            )
        },
    )
    for task, hidden_passed in ((passing, True), (failing, False)):
        work = tmp_path / task.task_id
        shutil.copytree(BENCH, work)
        outcome = local_corpus.judge(task, work)
        assert outcome.visible_passed, outcome.detail
        assert outcome.hidden_passed is hidden_passed, outcome.detail
        assert not (work / "tests" / "hidden").exists()


def test_a_task_over_the_bench_base_is_fair(tmp_path: Path) -> None:
    """corpus_v2.validate on a synthetic own task whose base is the real bench tree."""
    root = tmp_path / "corpus"
    shutil.copytree(BENCH, root / "bases" / "bench")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "corpus_id": "s6-probe",
                "version": "0.0.1",
                "split_seed": None,
                "bases": {"bench": {"dir": "bases/bench", "commit_file": "bases/bench.commit"}},
            }
        )
    )
    (root / "bases" / "bench.commit").write_text(_bench_commit() + "\n")
    task_dir = root / "tasks" / "feature-00-probe"
    (task_dir / "hidden").mkdir(parents=True)
    (task_dir / "reference" / "stockroom").mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "feature-00-probe",
                "version": 2,
                "domain": "feature",
                "subdomain": None,
                "set": "main",
                "split": None,
                "source": {
                    "kind": "own",
                    "ref": "s6-probe",
                    "author": "t",
                    "created": "2026-10-01",
                },
                "license": "LicenseRef-amplai-internal",
                "base": "bench",
                "environment": "app",
                "grading": "pytest_hidden",
                "objective": "Add shout(text) to stockroom.textutil.",
                "acceptance": ["shout('a') returns 'A!'"],
                "hidden_map": {"test_shout": 0},
                "ambiguity": None,
                "difficulty_declared": None,
            }
        )
    )
    (task_dir / "hidden" / "test_shout.py").write_text(
        "from stockroom.textutil import shout\n\n\n"
        "def test_shout():\n    assert shout('a') == 'A!'\n"
    )
    text = (BENCH / "stockroom" / "textutil.py").read_text()
    (task_dir / "reference" / "stockroom" / "textutil.py").write_text(
        text + "\n\ndef shout(text: str) -> str:\n    return text.upper() + '!'\n"
    )
    corpus = corpus_v2.load(root)
    row = corpus_v2.validate(corpus, tmp_path / "scratch", repeats=2)["feature-00-probe"]
    assert row["fair"] is True, row
    assert corpus_v2.base_commit(corpus, corpus.task("feature-00-probe")) == _bench_commit()


# -- the base repository: determinism ------------------------------------------------------------


def test_bench_commit_file_is_the_commit_of_a_fresh_build() -> None:
    done = _script("--base", "bench", "--verify")
    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout)
    assert SHA1.fullmatch(report["commit"]) and report["commit"] == _bench_commit()
    assert report["reproduced"] is True and report["written"] is None
    assert (V2_ROOT / "bases" / "bench.commit").read_text() == report["commit"] + "\n"


def test_same_tree_same_commit_whatever_the_copy(tmp_path: Path) -> None:
    """File modes, mtimes, __pycache__ and the caller's git environment do not change the hash."""
    first = base_repo.build_repo(BENCH, tmp_path / "r1", base_repo.message_for("bench"))
    copy = tmp_path / "copy"
    shutil.copytree(BENCH, copy)
    (copy / "scripts" / "make_sample_data.py").chmod(0o755)
    os.utime(copy / "README.md", (1_000_000_000, 1_000_000_000))
    (copy / "stockroom" / "__pycache__").mkdir()
    (copy / "stockroom" / "__pycache__" / "money.cpython-311.pyc").write_bytes(b"junk")
    env = {
        "GIT_AUTHOR_NAME": "Someone Else",
        "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "hostile.gitconfig"),
    }
    (tmp_path / "hostile.gitconfig").write_text(
        "[commit]\n\tgpgSign = true\n[init]\n\tdefaultObjectFormat = sha256\n"
    )
    with pytest.MonkeyPatch.context() as mp:
        for key, value in env.items():
            mp.setenv(key, value)
        second = base_repo.build_repo(copy, tmp_path / "r2", base_repo.message_for("bench"))
    assert first == second
    assert first["commit"] == _bench_commit()


def test_the_repository_holds_exactly_the_base_tree(tmp_path: Path) -> None:
    dest = tmp_path / "bench-repo"
    done = _script("--base", "bench", "--dest", str(dest))
    assert done.returncode == 0, done.stderr
    commit = json.loads(done.stdout)["commit"]
    assert _git(dest, "rev-parse", "refs/heads/main").strip() == commit
    assert _git(dest, "rev-list", "--parents", "-n", "1", commit).split() == [commit]  # root
    meta = _git(dest, "log", "-1", "--format=%an|%ae|%ad|%cn|%ce|%cd|%s", "--date=raw", commit)
    assert meta.strip() == (
        "AMPLAI Corpus|corpus@amplai.invalid|1790812800 +0000|"
        "AMPLAI Corpus|corpus@amplai.invalid|1790812800 +0000|amplai corpus base bench"
    )
    files = base_repo.base_files(BENCH)
    listed = _git(dest, "ls-tree", "-r", commit).splitlines()
    assert {line.split("\t")[1] for line in listed} == set(files)
    assert {line.split()[0] for line in listed} == {"100644"}
    for name, data in files.items():
        blob = subprocess.run(
            ["git", "-C", str(dest), "cat-file", "blob", f"{commit}:{name}"],
            capture_output=True,
            check=True,
        ).stdout
        assert blob == data, name
    assert _git(dest, "status", "--porcelain") == ""  # the working tree is checked out


def test_write_commit_writes_the_commit_file(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    shutil.copytree(BENCH, root / "bases" / "bench")
    shutil.copytree(DEMO, root / "bases" / "demo")
    shutil.copy(V2_ROOT / "manifest.json", root / "manifest.json")
    assert _script("--corpus", str(root), "--base", "bench", "--verify").returncode == 1
    done = _script("--corpus", str(root), "--base", "bench", "--write-commit")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["written"] == "bases/bench.commit"
    assert (root / "bases" / "bench.commit").read_text() == _bench_commit() + "\n"
    assert _script("--corpus", str(root), "--base", "bench", "--verify").returncode == 0
    (root / "bases" / "bench" / "README.md").write_text("changed\n")
    changed = _script("--corpus", str(root), "--base", "bench", "--verify")
    assert changed.returncode == 1 and json.loads(changed.stdout)["reproduced"] is False


def test_refusals(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("a\n")
    message = base_repo.message_for("x")

    def code(base: Path, dest: Path) -> str:
        with pytest.raises(base_repo.BaseRepoError) as e:
            base_repo.build_repo(base, dest, message)
        return str(e.value.code)

    assert code(tmp_path / "missing", tmp_path / "d0") == "BASE_DIR"
    (tmp_path / "empty").mkdir()
    assert code(tmp_path / "empty", tmp_path / "d1") == "BASE_DIR"
    assert code(tree, tree / "inside") == "BASE_DEST"
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    assert code(tree, busy) == "BASE_DEST"
    (tree / "link").symlink_to(tree / "a.txt")
    assert code(tree, tmp_path / "d2") == "BASE_TREE"
    (tree / "link").unlink()
    (tree / ".pytest_cache").mkdir()
    (tree / ".pytest_cache" / "v").write_text("x")
    assert code(tree, tmp_path / "d3") == "BASE_TREE"
    shutil.rmtree(tree / ".pytest_cache")
    (tree / ".git").mkdir()
    assert code(tree, tmp_path / "d4") == "BASE_TREE"

    pinned = _script("--base", "demo", "--write-commit")
    assert pinned.returncode == 2 and "BASE_PINNED" in pinned.stderr
    unknown = _script("--base", "nope")
    assert unknown.returncode == 2 and "BASE_UNKNOWN" in unknown.stderr


# -- §14 Q17: the Work 030 demo base commit ------------------------------------------------------


def test_demo_base_commit_is_not_reproduced_q17() -> None:
    """Q17 answer: the script does not reproduce 9e574bc from bases/demo.

    The Work 030 manifest records only the hash (§10.1), so the meta deployment installs the
    existing amplai-demo-app repository at that commit. The tree hash is the value to compare with
    ``git rev-parse 9e574bc^{tree}`` in that repository (확인 필요: needs that repository).
    """
    done = _script("--base", "demo", "--verify")
    assert done.returncode == 1, done.stderr
    report = json.loads(done.stdout)
    assert report["recorded"] == DEMO_COMMIT
    assert report["reproduced"] is False and report["commit"] != DEMO_COMMIT
    assert report["tree"] == "e3209d6796b2cc81d41bd782ff8a627d776e42cb"
    assert report["files"] == 6


def test_demo_base_is_the_work030_base_tree(tmp_path: Path) -> None:
    """bases/demo is byte-identical to the Work 030 base/ (same tree hash)."""
    message = base_repo.message_for("demo")
    ours = base_repo.build_repo(DEMO, tmp_path / "a", message)
    w030 = base_repo.build_repo(W030_BASE, tmp_path / "b", message)
    assert ours == w030


def test_real_corpus_resolves_the_bench_commit() -> None:
    corpus = corpus_v2.load(V2_ROOT)
    task = replace(corpus.tasks[0], base_id="bench")
    assert corpus_v2.base_commit(corpus, task) == _bench_commit()
    assert corpus_v2.base_dir(corpus, task) == BENCH
