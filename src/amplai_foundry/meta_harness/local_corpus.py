"""The demo-app task corpus for the local meta-harness (Work 030 S4, D-089).

Layout under ``specs/030-meta-harness-live/corpus/``::

    manifest.json                  corpus_id, app_id, base_commit, task_ids
    base/                          the demo-app tree at ``base_commit`` (byte-identical)
    tasks/<task_id>/task.json      objective, acceptance, difficulty (what the agent is told)
    tasks/<task_id>/hidden/        test files the agent never sees (applied after its run)
    tasks/<task_id>/reference/     a known-good solution as files over ``base/``

A trial succeeds when the hidden tests pass on the agent's result and the visible tests still do.
``validate`` proves every task is fair: the hidden tests fail on ``base`` and pass on the
reference solution.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DIFFICULTIES = ("small", "medium", "large")
HIDDEN_DIR = Path("tests") / "hidden"
NO_TESTS_COLLECTED = 5  # pytest exit code: no visible test is not a regression
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", os.devnull]


class CorpusError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class CorpusTask:
    task_id: str
    difficulty: str
    objective: str
    acceptance: tuple[str, ...]
    hidden: dict[str, bytes]  # path relative to the workspace root -> bytes
    reference: dict[str, bytes]  # path relative to the workspace root -> bytes

    def contract_text(self) -> str:
        """What the agent is given: the objective and the acceptance statements."""
        lines = [self.objective.strip(), "", "Acceptance:"]
        lines += [f"- {statement}" for statement in self.acceptance]
        return "\n".join(lines)


@dataclass(frozen=True)
class Corpus:
    corpus_id: str
    app_id: str
    base_commit: str
    root: Path
    tasks: tuple[CorpusTask, ...]

    @property
    def base(self) -> Path:
        return self.root / "base"

    def task(self, task_id: str) -> CorpusTask:
        for item in self.tasks:
            if item.task_id == task_id:
                return item
        raise CorpusError("TASK_UNKNOWN", f"No task {task_id}")


@dataclass(frozen=True)
class Outcome:
    hidden_passed: bool
    visible_passed: bool
    detail: str

    @property
    def success(self) -> bool:
        return self.hidden_passed and self.visible_passed


def _files(root: Path, prefix: Path = Path()) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {
        str(prefix / p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def load(root: Path, task_ids: list[str] | None = None) -> Corpus:
    """The whole corpus from its manifest, or only ``task_ids`` while tasks are being written."""
    root = Path(root)
    manifest: dict[str, Any] = (
        json.loads((root / "manifest.json").read_text())
        if task_ids is None
        else {"corpus_id": "partial", "app_id": "", "base_commit": "", "task_ids": task_ids}
    )
    tasks = []
    for task_id in manifest["task_ids"]:
        folder = root / "tasks" / task_id
        meta = json.loads((folder / "task.json").read_text())
        if meta.get("task_id") != task_id or meta.get("difficulty") not in DIFFICULTIES:
            raise CorpusError("TASK_META", f"{task_id}: task_id or difficulty is wrong")
        acceptance = tuple(meta["acceptance"])
        if not meta["objective"].strip() or not acceptance:
            raise CorpusError("TASK_META", f"{task_id}: objective and acceptance are required")
        hidden = _files(folder / "hidden", HIDDEN_DIR / task_id)
        if not hidden:
            raise CorpusError("TASK_HIDDEN", f"{task_id}: no hidden tests")
        tasks.append(
            CorpusTask(
                task_id,
                meta["difficulty"],
                meta["objective"],
                acceptance,
                hidden,
                _files(folder / "reference"),
            )
        )
    if len({t.task_id for t in tasks}) != len(tasks):
        raise CorpusError("TASK_DUPLICATE", "Task ids must be unique")
    return Corpus(
        manifest["corpus_id"], manifest["app_id"], manifest["base_commit"], root, tuple(tasks)
    )


def write_files(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _run(args: Iterable[str], cwd: Path, timeout: int) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTEST_ADDOPTS", None)
    try:
        done = subprocess.run(
            [*args], cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return done.returncode, (done.stdout + done.stderr).decode(errors="replace")[-2000:]


def judge(task: CorpusTask, workspace: Path, *, timeout: int = 120) -> Outcome:
    """Apply the hidden tests to a copy-independent workspace and run both suites.

    The hidden tests are written into ``workspace`` (callers pass a scratch copy of the agent's
    result, never the agent's own tree) and removed before returning.
    """
    write_files(workspace, task.hidden)
    try:
        hidden_rc, hidden_out = _run([*PYTEST, str(HIDDEN_DIR / task.task_id)], workspace, timeout)
        visible_rc, _ = _run([*PYTEST, "--ignore", str(HIDDEN_DIR), "tests"], workspace, timeout)
    finally:
        shutil.rmtree(workspace / HIDDEN_DIR, ignore_errors=True)
    return Outcome(
        hidden_rc == 0,
        visible_rc in (0, NO_TESTS_COLLECTED),
        f"hidden rc={hidden_rc}: {hidden_out[-300:]!r}; visible rc={visible_rc}",
    )


def _validate_task(task: CorpusTask, base: Path, scratch: Path) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for label, files in (("base", {}), ("reference", task.reference)):
        work = scratch / task.task_id / label
        shutil.copytree(base, work, dirs_exist_ok=True)
        write_files(work, files)
        outcome = judge(task, work)
        row[label] = {
            "hidden_passed": outcome.hidden_passed,
            "visible_passed": outcome.visible_passed,
        }
        if label == "reference":
            row["detail"] = outcome.detail
    row["fair"] = (
        not row["base"]["hidden_passed"]
        and row["base"]["visible_passed"]
        and row["reference"]["hidden_passed"]
        and row["reference"]["visible_passed"]
    )
    return row


def validate(corpus: Corpus, scratch: Path, *, workers: int = 4) -> dict[str, dict[str, Any]]:
    """Per task: the hidden tests fail on base and pass on the reference; visible tests pass.

    Tasks are independent scratch copies, so they run in parallel (each is a pytest subprocess).
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda t: _validate_task(t, corpus.base, scratch), corpus.tasks))
    return {task.task_id: row for task, row in zip(corpus.tasks, rows, strict=True)}
