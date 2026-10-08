"""``tb2_tests`` grading: the task's own test entry in the task image (§10.5 step 6, §14 Q7).

Contract: ``specs/033-harness-taxonomy/interfaces.md`` §10.5 and the clarification "TB2 Test
Entry And Grading (2026-10-08)"; source facts: ``specs/033-harness-taxonomy/runs/tb2-q7.md``.

The TB2 entry (``harbor-framework/terminal-bench-2@2fd12b8``, run by Harbor):

- every one of the 89 tasks ships ``tests/test.sh``; Harbor uploads ``tests/`` to ``/tests`` and
  runs ``/tests/test.sh`` (``harbor/verifier/verifier.py:175-232``,
  ``harbor/models/trial/paths.py:44``) in the container's working directory
  (``harbor/environments/docker/docker.py:1362,1411-1412``);
- the verdict is the reward file, not the exit status: Harbor reads ``/logs/verifier/reward.json``,
  else ``/logs/verifier/reward.txt`` (``verifier.py:257-266``), and every TB2 ``test.sh`` ends
  with ``echo 1``/``echo 0 > /logs/verifier/reward.txt`` after its pytest run, so the script
  itself exits 0 either way.

``grade`` runs that entry once against a scratch copy of the workspace after the run, in a fresh
container of the task image with network ``none``, uid/gid 65534 and a read-only root; ``/tests``
is the task's ``tests/`` read-only and ``/logs/verifier`` a fresh empty directory, the only
writable paths besides ``/workspace``. The outcome is pass, fail or null: ``reward_file`` passes
on ``1`` and fails on ``0`` (the two values TB2 writes); ``exit_status`` passes on 0 and fails
otherwise; a missing entry, a timeout, a missing, empty or other reward, or a ``reward.json``
(whose pass rule TB2 2.0 does not define) is null. The container run is injected
(``GradeRunner``), so the logic is tested without docker; ``docker_runner`` is the operator runner.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..runtime.errors import Hold, RuntimeFault
from ..sandbox.container import ContainerProfile, ContainerSandbox
from . import tb2
from .local_corpus import Outcome

# Harbor's in-container paths (harbor/models/trial/paths.py:39-48), which TB2 test.sh files name.
TESTS_DIR = "/tests"
VERIFIER_DIR = "/logs/verifier"
REWARD_TXT = "reward.txt"
REWARD_JSON = "reward.json"
TEST_SCRIPT = "test.sh"
RESULTS = ("reward_file", "exit_status")
ENTRY_SCHEMA = "amplai.tb2-test-entry.v1"
# TB2 test.sh files write exactly these two values (89 of 89 at 2fd12b8, runs/tb2-q7.md).
PASS_REWARD, FAIL_REWARD = "1", "0"
WORKDIR_LINE = re.compile(r"^\s*WORKDIR\s+(\S+)", re.MULTILINE)

Result = Literal["reward_file", "exit_status"]


@dataclass(frozen=True)
class TestEntry:
    """How a task's tests run and how their verdict is read."""

    __test__ = False  # not a pytest class

    command: tuple[str, ...]
    result: Result
    timeout_sec: int | None
    # The task Dockerfile's last WORKDIR: informative only. Every run's working directory is
    # /workspace, which the per-task image links the task WORKDIR to (tb2.image_spec).
    dockerfile_workdir: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": ENTRY_SCHEMA,
            "command": list(self.command),
            "result": self.result,
            "timeout_sec": self.timeout_sec,
            "dockerfile_workdir": self.dockerfile_workdir,
            "tests_dir": TESTS_DIR,
            "verifier_dir": VERIFIER_DIR,
        }


def entry_from_json(value: Any) -> TestEntry | None:
    """The entry recorded in ``environment.json`` (``grading``); None when absent or malformed."""
    if not isinstance(value, dict) or value.get("schema") != ENTRY_SCHEMA:
        return None
    command, result, timeout = value.get("command"), value.get("result"), value.get("timeout_sec")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(a, str) and a and "\x00" not in a for a in command)
        or result not in RESULTS
        or not (timeout is None or (type(timeout) is int and timeout > 0))
    ):
        return None
    workdir = value.get("dockerfile_workdir")
    return TestEntry(tuple(command), result, timeout, workdir if isinstance(workdir, str) else None)


def dockerfile_workdir(source_dir: Path) -> str | None:
    """The last ``WORKDIR`` of ``environment/Dockerfile`` (the build's final working directory)."""
    path = Path(source_dir) / "environment" / "Dockerfile"
    if not path.is_file():
        return None
    found = WORKDIR_LINE.findall(path.read_text(errors="replace"))
    return found[-1] if found else None


def entry_for(task: tb2.Tb2Task) -> TestEntry | None:
    """The TB2 entry of a scanned task: ``bash /tests/test.sh``, verdict from the reward file.

    None when the task has no ``tests/test.sh`` (an unknown entry grades null). ``bash`` instead of
    Harbor's ``chmod +x`` then direct execution (``verifier.py:221-232``): ``/tests`` is mounted
    read-only, and 88 of 89 TB2 ``test.sh`` files start with ``#!/bin/bash`` while one has it on
    line 3 (runs/tb2-q7.md).
    """
    if task.source_dir is None or not (task.source_dir / "tests" / TEST_SCRIPT).is_file():
        return None
    return TestEntry(
        ("bash", f"{TESTS_DIR}/{TEST_SCRIPT}"),
        "reward_file",
        task.verifier_timeout_sec,
        dockerfile_workdir(task.source_dir),
    )


@dataclass(frozen=True)
class GradeRun:
    """One fresh grading container, as the injected runner receives it."""

    image: str
    argv: tuple[str, ...]
    workspace: Path  # host directory mounted read-write at /workspace (a scratch copy)
    tests: Path  # host directory mounted read-only at TESTS_DIR
    verifier: Path  # fresh empty host directory mounted read-write at VERIFIER_DIR
    timeout_sec: int | None
    uid: int = tb2.UID
    gid: int = tb2.GID
    network: str = tb2.NETWORK
    read_only_root: bool = True
    writable: tuple[str, ...] = field(default=(tb2.WORKSPACE, VERIFIER_DIR))


GradeRunner = Callable[[GradeRun], int | None]


@dataclass(frozen=True)
class Tb2Outcome(Outcome):
    """A ``tb2_tests`` grade. ``result`` is the §10.5 verdict: True, False or None (not graded).

    ``hidden_passed``/``visible_passed`` are both ``result is True``, so ``success`` (a bool for
    every ``Outcome``) never counts an ungraded run as a pass; a reader that must tell a failure
    from an ungraded run reads ``result``.
    """

    result: bool | None = None
    exit_status: int | None = None
    reward: str | None = None


def _outcome(result: bool | None, detail: str, **kw: Any) -> Tb2Outcome:
    passed = result is True
    return Tb2Outcome(passed, passed, f"tb2_tests: {detail}", result, **kw)


def check_profile(profile: dict[str, Any], image: str) -> None:
    """The grading container's profile: pinned image, uid/gid 65534, network none."""
    if not tb2.PINNED_IMAGE.fullmatch(image):
        raise Hold("TB2_GRADING", "the grading image needs an immutable digest")
    if profile.get("image") != image:
        raise Hold("TB2_GRADING", "the container profile names another image")
    if profile.get("uid") != tb2.UID or profile.get("gid") != tb2.GID:
        raise Hold("TB2_GRADING", f"grading runs as uid/gid {tb2.UID}")
    if profile.get("network") != tb2.NETWORK:
        raise Hold("TB2_GRADING", "grading runs with network none")


def _verdict(entry: TestEntry, code: int | None, verifier: Path) -> Tb2Outcome:
    if code is None:
        return _outcome(None, "the test entry timed out (not graded)")
    if entry.result == "exit_status":
        return _outcome(code == 0, f"exit status {code}", exit_status=code)
    if (verifier / REWARD_JSON).exists():
        return _outcome(
            None, f"{REWARD_JSON} written; its pass rule is not defined by TB2", exit_status=code
        )
    path = verifier / REWARD_TXT
    if not path.is_file() or path.is_symlink():
        return _outcome(None, f"no {REWARD_TXT} written (exit status {code})", exit_status=code)
    raw = path.read_bytes()[:64].decode("utf-8", errors="replace").strip()
    if raw == PASS_REWARD:
        return _outcome(True, f"reward {raw}", exit_status=code, reward=raw)
    if raw == FAIL_REWARD:
        return _outcome(False, f"reward {raw}", exit_status=code, reward=raw)
    return _outcome(None, f"reward {raw!r} is neither 1 nor 0", exit_status=code, reward=raw)


def grade(
    entry: TestEntry | None,
    workspace: Path,
    tests: Path,
    *,
    image: str,
    profile: dict[str, Any],
    run: GradeRunner | None = None,
    scratch: Path | None = None,
) -> Tb2Outcome:
    """Run ``entry`` once against ``workspace`` (a scratch copy of the result, never the agent's
    tree) with ``tests`` at ``/tests``. ``entry`` None grades null without running anything.
    ``run`` defaults to ``docker_runner(profile)`` (operator machines only). ``scratch`` holds the
    fresh verifier directory (default: a temporary directory; colima shares ``$HOME`` only)."""
    if entry is None:
        return _outcome(None, "the test entry is unknown (not graded)")
    check_profile(profile, image)
    workspace, tests = Path(workspace), Path(tests)
    if not workspace.is_dir() or not tests.is_dir():
        raise Hold("TB2_GRADING", "grading needs the workspace copy and the task tests directory")
    runner = run or docker_runner(profile)
    owned = scratch is None
    root = Path(tempfile.mkdtemp(prefix="amplai-tb2-grade-")) if scratch is None else Path(scratch)
    verifier = root.resolve() / f"verifier-{uuid.uuid4().hex[:12]}"
    verifier.mkdir(parents=True)
    # uid 65534 writes the reward into a directory the host created (Harbor opens it the same
    # way, harbor/models/trial/paths.py:161).
    verifier.chmod(0o777)
    try:
        code = runner(GradeRun(image, entry.command, workspace, tests, verifier, entry.timeout_sec))
        return _verdict(entry, code, verifier)
    finally:
        shutil.rmtree(verifier if not owned else root, ignore_errors=True)


def _mount_source(path: Path, what: str) -> str:
    original = Path(path).absolute()
    if original.resolve() != original or not original.is_dir() or "," in str(original):
        raise RuntimeFault("TB2_GRADING", f"{what} must be an existing non-symlink directory")
    return str(original)


def docker_runner(profile: dict[str, Any], *, engine: str = "docker") -> GradeRunner:
    """The operator runner: ``ContainerSandbox.command`` (``--read-only``, ``--network none``,
    ``--user 65534:65534``, ``--cap-drop=ALL``, the ``/workspace`` bind, tmpfs ``/tmp`` and
    ``/home/agent``) plus two grading mounts before the image: the task tests read-only at
    ``/tests`` and the fresh verifier directory read-write at ``/logs/verifier``.
    ``ContainerSandbox`` admits trusted mounts only under ``/amplai-input/`` for agent runs; the
    grading container runs no agent, and TB2 ``test.sh`` files name ``/tests`` and
    ``/logs/verifier`` literally (runs/tb2-q7.md)."""
    sandbox = ContainerSandbox(
        ContainerProfile(
            profile["image"],
            uid=profile["uid"],
            gid=profile["gid"],
            memory=profile.get("memory", "1g"),
            cpus=profile.get("cpus", 1.0),
            pids=profile.get("pids", 128),
            network=tb2.NETWORK,
        ),
        engine=engine,
    )

    def run(spec: GradeRun) -> int | None:
        if (
            spec.image != sandbox.profile.image
            or spec.network != tb2.NETWORK
            or (spec.uid, spec.gid) != (tb2.UID, tb2.GID)
        ):
            raise Hold("TB2_GRADING", "grading run outside the container profile")
        name = f"amplai-tb2-grade-{uuid.uuid4().hex[:12]}"
        argv = sandbox.command(list(spec.argv), spec.workspace, name)
        at = len(argv) - len(spec.argv) - 1  # the image position
        assert argv[at] == sandbox.profile.image
        argv[at:at] = [
            "--mount",
            f"type=bind,src={_mount_source(spec.tests, 'tests')},dst={TESTS_DIR},readonly",
            "--mount",
            f"type=bind,src={_mount_source(spec.verifier, 'verifier')},dst={VERIFIER_DIR}",
        ]
        code: int | None
        try:
            code = subprocess.run(
                argv, capture_output=True, timeout=spec.timeout_sec, check=False
            ).returncode
        except subprocess.TimeoutExpired:
            code = None
            sandbox.stop(name)
        sandbox.destroy(name)
        return code

    return run


# -- corpus v2 -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Grader:
    """What ``corpus_v2.grade`` needs for a ``tb2_tests`` task: the corpus root (the task folder
    is ``<root>/<task.tb2["dir"]>``), the container runner and a scratch directory."""

    corpus_root: Path
    run: GradeRunner | None = None
    scratch: Path | None = None


def grade_task(task: Any, workspace: Path, grader: Grader) -> Tb2Outcome:
    """``tb2_tests`` grade of a corpus v2 task from its ``environment.json`` (``image``,
    ``container_profile`` and the ``grading`` entry written by ``write_task``) and its ``tests/``.
    A task without a recorded entry grades null."""
    folder = Path(grader.corpus_root) / str((task.tb2 or {}).get("dir", ""))
    try:
        environment = json.loads((folder / "environment.json").read_text())
    except (OSError, ValueError):
        return _outcome(None, f"{task.task_id}: no readable environment.json (not graded)")
    if not isinstance(environment, dict):
        return _outcome(None, f"{task.task_id}: environment.json is not an object (not graded)")
    entry = entry_from_json(environment.get("grading"))
    profile = environment.get("container_profile")
    if entry is None or not isinstance(profile, dict):
        return _outcome(None, f"{task.task_id}: the test entry is unknown (not graded)")
    return grade(
        entry,
        workspace,
        folder / "tests",
        image=str(environment.get("image")),
        profile=profile,
        run=grader.run,
        scratch=grader.scratch,
    )


# -- adapter wiring (scripts/tb2_adapter.py) -----------------------------------------------------


@dataclass
class AdmissionGrades:
    """The test steps of an admission graded by ``grade``; ``tb2.admit`` sees 0 (pass), 1 (fail)
    or None (not graded, which admission reports as ``timeout``) and ``grades`` keeps the detail."""

    entry: TestEntry
    profile: dict[str, Any]
    grade_run: GradeRunner | None = None
    solution_run: tb2.Runner | None = None
    scratch: Path | None = None
    grades: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, spec: tb2.ContainerRun) -> int | None:
        if spec.purpose == "solution":
            runner = self.solution_run or tb2.docker_runner(self.profile)
            return runner(spec)
        if tuple(spec.argv) != self.entry.command:
            raise Hold("TB2_ADMISSION", "the admission test command is not the grading entry")
        outcome = grade(
            self.entry,
            spec.workspace,
            spec.inputs[tb2.TESTS_MOUNT],
            image=spec.image,
            profile=self.profile,
            run=self.grade_run or docker_runner(self.profile),
            scratch=self.scratch,
        )
        self.grades.append(
            {
                "purpose": spec.purpose,
                "repeat": spec.repeat,
                "result": outcome.result,
                "exit_status": outcome.exit_status,
                "reward": outcome.reward,
                "detail": outcome.detail,
            }
        )
        return None if outcome.result is None else (0 if outcome.result else 1)


def admit(
    task: tb2.Tb2Task,
    *,
    entry: TestEntry,
    image: str,
    container_profile: Path,
    solution_command: Sequence[str],
    base_repo: Path,
    base_commit: str,
    repeats: int = tb2.ADMISSION_REPEATS,
    scratch: Path | None = None,
    grade_run: GradeRunner | None = None,
    solution_run: tb2.Runner | None = None,
) -> dict[str, Any]:
    """``tb2.admit`` with its test steps graded by ``entry``; the result also records the entry
    (``grading``) and every test step's grade (``test_grades``) for ``write_task``."""
    profile = tb2.load_container_profile(container_profile)
    graded = AdmissionGrades(entry, profile, grade_run, solution_run, scratch)
    result = tb2.admit(
        task,
        image=image,
        container_profile=container_profile,
        repeats=repeats,
        test_command=list(entry.command),
        solution_command=solution_command,
        base_repo=base_repo,
        base_commit=base_commit,
        run=graded,
        scratch=scratch,
    )
    return {**result, "grading": entry.to_json(), "test_grades": graded.grades}


def write_task(
    task: tb2.Tb2Task, admitted: dict[str, Any], dest: Path, *, created: str | None = None
) -> Path:
    """``tb2.write_task`` plus the admission's ``grading`` entry in ``environment.json``, which
    ``grade_task`` reads. A result without a valid entry is written without one (grades null)."""
    folder = tb2.write_task(task, admitted, dest, created=created)
    entry = entry_from_json(admitted.get("grading"))
    if entry is not None:
        path = folder / "environment.json"
        try:
            environment = json.loads(path.read_text())
            environment["grading"] = entry.to_json()
            path.write_text(json.dumps(environment, indent=2) + "\n")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
    return folder
