"""Work 033 S7a - Terminal-Bench 2.0 adapter tooling: scan, selection, image spec, admission.

Contract: specs/033-harness-taxonomy/interfaces.md §10.5, §3.11 (tb2), §13 S7a, §14 Q7.
Every fixture is a synthetic TB2-shaped clone in ``tmp_path``; admission containers are a fake
runner that simulates the task in Python. No docker, no network: ``subprocess.run`` is replaced
by a stub that refuses every program except local ``git`` (the tb2 base repository and the
workspace materialization, both in ``tmp_path``).
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness import corpus_v2, tb2
from amplai_foundry.meta_harness.tb2 import ContainerRun, Tb2Task
from amplai_foundry.runtime.errors import Hold, RuntimeFault

REPO = Path(__file__).resolve().parents[2]
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
IMAGE = "localhost:5000/amplai-tb2-demo@sha256:" + "a" * 64
DRIVER_LAYER = "localhost:5000/amplai-worker@sha256:" + "b" * 64
APACHE = """
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION
"""
MIT = "MIT License\n\nPermission is hereby granted, free of charge, to any person...\n"
TEST_COMMAND = ["bash", f"{tb2.TESTS_MOUNT}/test.sh"]
SOLUTION_COMMAND = ["bash", f"{tb2.SOLUTION_MOUNT}/solve.sh"]


_REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def _no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    def local_git_only(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list | tuple) and argv and argv[0] == "git":
            return _REAL_RUN(argv, *args, **kwargs)
        raise AssertionError(f"only local git runs in S7a tests: {argv!r}")

    monkeypatch.setattr(subprocess, "run", local_git_only)
    monkeypatch.setattr(tb2, "git_head", lambda source: SHA)


def _script_module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _commit(extracted: Path, repo: Path, name: str) -> dict[str, Any]:
    fixed = _script_module("corpus_base_repo")
    return tb2.commit_base(
        extracted,
        repo,
        name,
        identity=fixed.IDENTITY,
        message=fixed.message_for(f"tb2/{name}"),
    )


def _git_out(repo: Path, *args: str) -> str:
    done = _REAL_RUN(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout


def _toml(
    *,
    category: str = "software-engineering",
    difficulty: str = "medium",
    image: str = "ghcr.io/example/tb2-task:1",
    gpus: int | None = 0,
    allow_internet: str = "true",
    agent_timeout: int | None = 900,
    verifier_timeout: int | None = 180,
    extra: str = "",
) -> str:
    lines = [
        "[metadata]",
        f'difficulty = "{difficulty}"',
        f'category = "{category}"',
        'tags = ["python", "async"]',
        "expert_time_estimate_min = 20",
        "junior_time_estimate_min = 60",
        "",
        "[verifier]",
    ]
    if verifier_timeout is not None:
        lines.append(f"timeout_sec = {verifier_timeout}.0")
    lines += ["", "[agent]"]
    if agent_timeout is not None:
        lines.append(f"timeout_sec = {agent_timeout}.0")
    lines += [
        "",
        "[environment]",
        f'docker_image = "{image}"',
        "cpus = 1",
        "memory_mb = 2048",
        "storage_mb = 10240",
    ]
    if gpus is not None:
        lines.append(f"gpus = {gpus}")
    lines.append(f"allow_internet = {allow_internet}")
    return "\n".join(lines) + "\n" + extra


def _task_dir(root: Path, name: str, toml: str | None = None, *, tests: bool = True) -> Path:
    folder = root / name
    (folder / "environment").mkdir(parents=True)
    (folder / "environment" / "Dockerfile").write_text("FROM python:3.11-slim\nWORKDIR /app\n")
    (folder / "instruction.md").write_text(f"Write the answer to answer.txt ({name}).\n")
    (folder / "task.toml").write_text(toml if toml is not None else _toml())
    (folder / "tests").mkdir()
    if tests:
        (folder / "tests" / "test.sh").write_text("#!/bin/bash\npytest /tests/test_outputs.py\n")
        (folder / "tests" / "test_outputs.py").write_text(
            "def test_answer():\n    assert open('/app/answer.txt').read() == '42'\n"
        )
    (folder / "solution").mkdir()
    (folder / "solution" / "solve.sh").write_text("#!/bin/bash\necho -n 42 > answer.txt\n")
    return folder


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    root = tmp_path / "terminal-bench-2"
    root.mkdir()
    (root / "LICENSE").write_text(APACHE)
    (root / "NOTICE").write_text("Terminal-Bench upstream notice.\n")
    (root / ".github").mkdir()  # not a task directory: no task.toml
    (root / "README.md").write_text("tasks\n")
    _task_dir(root, "cancel-async-tasks")
    _task_dir(root, "fix-permissions", _toml(category="system-administration", difficulty="easy"))
    _task_dir(root, "train-on-gpu", _toml(category="model-training", gpus=1))
    _task_dir(root, "no-timeouts", _toml(agent_timeout=None, verifier_timeout=None))
    return root


def _task(clone: Path, name: str = "cancel-async-tasks") -> Tb2Task:
    return next(t for t in tb2.scan(clone, commit=SHA) if t.name == name)


# -- scan ----------------------------------------------------------------------------------------


def test_scan_parses_task_toml_and_skips_gpu_tasks(clone: Path) -> None:
    tasks, skipped = tb2.scan_all(clone, commit=SHA)
    assert [t.name for t in tasks] == ["cancel-async-tasks", "fix-permissions", "no-timeouts"]
    assert skipped == [{"name": "train-on-gpu", "reason": "gpus", "gpus": 1}]
    task = tasks[0]
    assert (
        task.docker_image,
        task.allow_internet,
        task.gpus,
        task.category,
        task.difficulty,
        task.agent_timeout_sec,
        task.verifier_timeout_sec,
    ) == ("ghcr.io/example/tb2-task:1", True, 0, "software-engineering", "medium", 900, 180)
    assert task.commit == SHA and task.source_dir == clone / "cancel-async-tasks"
    assert task.tags == ("python", "async")
    assert tasks[2].agent_timeout_sec is None and tasks[2].verifier_timeout_sec is None
    assert tb2.scan(clone, commit=SHA) == tasks


def test_scan_checks_the_pinned_commit(clone: Path) -> None:
    with pytest.raises(Hold) as exc:
        tb2.scan(clone, commit=OTHER_SHA)
    assert exc.value.code == "TB2_SOURCE"
    with pytest.raises(Hold) as exc:
        tb2.scan(clone, commit="main")
    assert exc.value.code == "TB2_SOURCE"
    seen: list[Path] = []
    tasks = tb2.scan(clone, commit=OTHER_SHA, head=lambda p: seen.append(p) or OTHER_SHA)
    assert seen == [clone] and {t.commit for t in tasks} == {OTHER_SHA}
    # without a pin nothing is compared and nothing is recorded
    assert {t.commit for t in tb2.scan(clone)} == {None}


@pytest.mark.parametrize("text", [None, MIT, "Apache License\nVersion 1.1\n", ""])
def test_scan_holds_unless_the_license_is_apache_2(clone: Path, text: str | None) -> None:
    if text is None:
        (clone / "LICENSE").unlink()
    else:
        (clone / "LICENSE").write_text(text)
    with pytest.raises(Hold) as exc:
        tb2.scan(clone, commit=SHA)
    assert exc.value.code == "TB2_LICENSE"


def test_license_check_ignores_layout_whitespace(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text(
        "Apache License\nVersion 2.0,   January 2004\nhttps://www.apache.org/licenses/\n"
    )
    assert tb2.check_license(tmp_path) == "Apache-2.0"


@pytest.mark.parametrize(
    "toml",
    [
        "not = [valid",
        _toml(gpus=None),
        _toml(allow_internet='"yes"'),
        _toml(image=""),
        _toml().replace('category = "software-engineering"\n', ""),
        _toml(extra="[agent]\n"),  # duplicate table: invalid TOML
        _toml().replace("timeout_sec = 900.0", "timeout_sec = -1"),
        _toml().replace("timeout_sec = 900.0", "timeout_sec = 1.5"),
        _toml().replace('tags = ["python", "async"]', 'tags = "python"'),
    ],
)
def test_scan_refuses_malformed_task_toml(clone: Path, toml: str) -> None:
    (clone / "cancel-async-tasks" / "task.toml").write_text(toml)
    with pytest.raises(Hold) as exc:
        tb2.scan(clone, commit=SHA)
    assert exc.value.code == "TB2_SOURCE"


def test_scan_refuses_incomplete_task_directories(clone: Path) -> None:
    _task_dir(clone, "empty-tests", tests=False)
    with pytest.raises(Hold, match="tests/ has no files"):
        tb2.scan(clone, commit=SHA)
    (clone / "empty-tests" / "tests" / "test.sh").write_text("exit 1\n")
    (clone / "empty-tests" / "instruction.md").unlink()
    with pytest.raises(Hold, match=r"instruction\.md"):
        tb2.scan(clone, commit=SHA)


def test_scan_refuses_a_directory_without_tasks(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text(APACHE)
    with pytest.raises(Hold) as exc:
        tb2.scan(tmp_path)
    assert exc.value.code == "TB2_SOURCE"
    with pytest.raises(Hold):
        tb2.scan(tmp_path / "missing")


# -- selection -----------------------------------------------------------------------------------

# The category counts of the 89 tasks (research-terminal-bench-2.md, scan of 2026-09-30).
RESEARCH_CATEGORIES = {
    "software-engineering": 26,
    "system-administration": 9,
    "scientific-computing": 8,
    "security": 8,
    "data-science": 8,
    "debugging": 5,
    "file-operations": 5,
    "model-training": 4,
    "mathematics": 4,
    "data-processing": 4,
    "machine-learning": 3,
    "games": 1,
    "personal-assistant": 1,
    "optimization": 1,
    "data-querying": 1,
    "video-processing": 1,
}


def _synthetic(counts: dict[str, int]) -> list[Tb2Task]:
    return [
        Tb2Task(f"{category}-{i:02d}", "img", True, 0, category, "medium", None, None)
        for category, n in counts.items()
        for i in range(n)
    ]


def test_selection_spreads_over_categories() -> None:
    tasks = _synthetic({"a": 5, "b": 1, "c": 3})
    picked = tb2.select_candidates(tasks, limit=4)
    assert [t.name for t in picked] == ["a-00", "b-00", "c-00", "a-01"]
    picked = tb2.select_candidates(list(reversed(tasks)), limit=7)
    assert [t.name for t in picked] == ["a-00", "b-00", "c-00", "a-01", "c-01", "a-02", "c-02"]
    assert tb2.select_candidates(tasks, limit=0) == []
    assert len(tb2.select_candidates(tasks, limit=100)) == 9
    with pytest.raises(RuntimeFault):
        tb2.select_candidates(tasks, limit=-1)


def test_selection_of_the_research_distribution_keeps_every_category() -> None:
    tasks = _synthetic(RESEARCH_CATEGORIES)
    assert len(tasks) == 89
    picked = tb2.select_candidates(tasks)
    assert len(picked) == tb2.MAX_CANDIDATES == 60
    counts = tb2.category_counts(picked)
    assert set(counts) == set(RESEARCH_CATEGORIES)
    # round robin: no category exceeds another by more than one unless the other ran out
    for category, n in counts.items():
        assert n == RESEARCH_CATEGORIES[category] or n >= max(counts.values()) - 1
    assert counts["software-engineering"] < RESEARCH_CATEGORIES["software-engineering"]
    assert tb2.select_candidates(list(reversed(tasks))) == picked


# -- image spec ----------------------------------------------------------------------------------


def test_image_spec_builds_from_the_task_image_with_the_driver_layer(clone: Path) -> None:
    task = _task(clone)
    paths = ["/usr/local/bin/node", "/usr/local/lib/node_modules"]
    spec = tb2.image_spec(task, driver_layer_image=DRIVER_LAYER, driver_paths=paths, workdir="/app")
    assert spec["ready"] is True and spec["unknowns"] == []
    assert (spec["uid"], spec["gid"], spec["workspace"]) == (65534, 65534, "/workspace")
    assert spec["from_image"] == task.docker_image and spec["from_pinned"] is False
    lines = [line for line in spec["dockerfile"].splitlines() if not line.startswith("#")]
    assert lines == [
        f"FROM {task.docker_image}",
        f"COPY --from={DRIVER_LAYER} /usr/local/bin/node /usr/local/bin/node",
        f"COPY --from={DRIVER_LAYER} /usr/local/lib/node_modules /usr/local/lib/node_modules",
        "USER 0:0",
        "RUN mkdir -p /workspace && rm -rf /app && ln -s /workspace /app",
        "USER 65534:65534",
        "WORKDIR /workspace",
        "ENTRYPOINT []",
    ]
    assert SHA in spec["dockerfile"]
    # the driver-layer-on-base-distribution question stays open whatever the inputs
    assert any("확인 필요" in note for note in spec["notes"])
    again = tb2.image_spec(
        task, driver_layer_image=DRIVER_LAYER, driver_paths=paths, workdir="/app"
    )
    assert again["digest"] == spec["digest"]
    other = tb2.image_spec(
        task, driver_layer_image=DRIVER_LAYER, driver_paths=paths, workdir="/src"
    )
    assert other["digest"] != spec["digest"]


def test_image_spec_workspace_workdir_needs_no_link(clone: Path) -> None:
    spec = tb2.image_spec(
        _task(clone), driver_layer_image=DRIVER_LAYER, driver_paths=["/opt/d"], workdir="/workspace"
    )
    assert "RUN mkdir -p /workspace\n" in spec["dockerfile"]
    assert "ln -s" not in spec["dockerfile"]


def test_image_spec_marks_unknowns_instead_of_guessing(clone: Path) -> None:
    task = _task(clone)
    spec = tb2.image_spec(task, driver_layer_image=DRIVER_LAYER)
    assert spec["ready"] is False and spec["dockerfile"] is None
    assert [u.split(":")[0] for u in spec["unknowns"]] == ["workdir", "driver_paths"]
    assert all("확인 필요" in u for u in spec["unknowns"])
    spec = tb2.image_spec(task, driver_layer_image=DRIVER_LAYER, workdir="/app")
    assert spec["ready"] is False and [u.split(":")[0] for u in spec["unknowns"]] == [
        "driver_paths"
    ]


def test_image_spec_needs_a_pinned_driver_layer(clone: Path) -> None:
    with pytest.raises(Hold) as exc:
        tb2.image_spec(_task(clone), driver_layer_image="amplai-worker:latest", workdir="/app")
    assert exc.value.code == "IMAGE_NOT_PINNED"


@pytest.mark.parametrize(
    ("workdir", "paths"),
    [
        ("/", ["/opt/d"]),
        ("/workspace/app", ["/opt/d"]),
        ("app", ["/opt/d"]),
        ("/a/../etc", ["/opt/d"]),
        ("/app; rm -rf /", ["/opt/d"]),
        ("/app\nRUN id", ["/opt/d"]),
        ("/app/", ["/opt/d"]),
        ("/app", ["/"]),
        ("/app", ["relative/path"]),
        ("/app", ["/opt/d /etc/passwd"]),
    ],
)
def test_image_spec_refuses_unsafe_paths(clone: Path, workdir: str, paths: list[str]) -> None:
    with pytest.raises(RuntimeFault) as exc:
        tb2.image_spec(
            _task(clone), driver_layer_image=DRIVER_LAYER, driver_paths=paths, workdir=workdir
        )
    assert exc.value.code == "TB2_SPEC"


def test_operator_commands_are_built_not_run(tmp_path: Path) -> None:
    assert tb2.workdir_command(IMAGE) == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Config.WorkingDir}}",
        IMAGE,
    ]
    create, copy, remove = tb2.extract_commands("ghcr.io/x:1", "/app", tmp_path / "out")
    name = create[3]
    assert create == ["docker", "create", "--name", name, "ghcr.io/x:1"]
    assert copy == ["docker", "cp", f"{name}:/app/.", str(tmp_path / "out")]
    assert remove == ["docker", "rm", name]
    with pytest.raises(RuntimeFault):
        tb2.extract_commands("ghcr.io/x:1", "../app", tmp_path)


def test_record_base_commit_merges_sorted(tmp_path: Path) -> None:
    path = tmp_path / "bases" / "tb2.commits.json"
    tb2.record_base_commit(path, "zeta", SHA)
    assert tb2.record_base_commit(path, "alpha", OTHER_SHA) == {"alpha": OTHER_SHA, "zeta": SHA}
    assert list(json.loads(path.read_text())) == ["alpha", "zeta"]
    with pytest.raises(RuntimeFault):
        tb2.record_base_commit(path, "alpha", "abc")
    path.write_text("[]")
    with pytest.raises(RuntimeFault):
        tb2.record_base_commit(path, "alpha", SHA)


def _tree(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hello')\n")
    (root / "run.sh").write_text("#!/bin/sh\n")
    (root / "run.sh").chmod(0o755)
    return root


def test_commit_base_writes_every_task_into_one_repository(tmp_path: Path) -> None:
    repo = tmp_path / "amplai-tb2"
    first = _commit(_tree(tmp_path / "a"), repo, "task-a")
    other = _tree(tmp_path / "b")
    (other / "data.txt").write_text("b\n")
    (other / "empty" / "nested").mkdir(parents=True)
    (other / "latest.py").symlink_to("src/main.py")
    second = _commit(other, repo, "task-b")
    assert first["commit"] != second["commit"]
    refs = _git_out(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    assert refs == [
        f"refs/heads/tb2/task-a {first['commit']}",
        f"refs/heads/tb2/task-b {second['commit']}",
    ]
    for built in (first, second):  # root commits with the fixed identity
        assert _git_out(repo, "rev-list", "--parents", "-n1", built["commit"]).split() == [
            built["commit"]
        ]
        assert _git_out(repo, "log", "-1", "--format=%an|%ae|%at|%cn|%ce|%ct", built["commit"]) == (
            "AMPLAI Corpus|corpus@amplai.invalid|1790812800|"
            "AMPLAI Corpus|corpus@amplai.invalid|1790812800\n"
        )
    listing = _git_out(repo, "ls-tree", "-r", second["commit"]).splitlines()
    modes = {line.split("\t")[1]: line.split()[0] for line in listing}
    assert modes == {
        "data.txt": "100644",
        "latest.py": "120000",
        "run.sh": "100755",  # the execute bit is kept
        "src/main.py": "100644",
    }
    assert _git_out(repo, "cat-file", "-p", f"{second['commit']}:latest.py") == "src/main.py"
    assert (second["executables"], second["symlinks"], second["files"]) == (1, 1, 4)
    assert second["dropped_empty_dirs"] == ["empty", "empty/nested"]
    # the checkout itself is untouched: no index, no working tree files, main unborn
    assert sorted(p.name for p in repo.iterdir()) == [".git"]
    assert not (repo / ".git" / "index").exists()


def test_commit_base_is_reproducible_and_never_replaced(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "t")
    first = _commit(tree, tmp_path / "r1", "task-a")
    assert _commit(tree, tmp_path / "r1", "task-a")["commit"] == first["commit"]  # idempotent
    assert _commit(tree, tmp_path / "r2", "task-a")["commit"] == first["commit"]  # reproducible
    (tree / "src" / "main.py").write_text("print('changed')\n")
    with pytest.raises(RuntimeFault) as exc:
        _commit(tree, tmp_path / "r1", "task-a")
    assert exc.value.code == "TB2_SPEC" and "never replaced" in exc.value.message


def test_commit_base_matches_the_corpus_base_builder_for_plain_trees(tmp_path: Path) -> None:
    """Same identity, message and 100644 tree as scripts/corpus_base_repo.py: same hash."""
    tree = tmp_path / "plain"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "main.py").write_text("print('hello')\n")
    (tree / "README.md").write_text("plain\n")
    fixed = _script_module("corpus_base_repo")
    built = fixed.build_repo(tree, tmp_path / "ref", fixed.message_for("tb2/plain"))
    assert _commit(tree, tmp_path / "tb2", "plain")["commit"] == built["commit"]


def test_commit_base_refuses_trees_git_cannot_hold_faithfully(tmp_path: Path) -> None:
    cases: dict[str, Path] = {}
    dotgit = _tree(tmp_path / "dotgit")
    (dotgit / "vendor" / ".git").mkdir(parents=True)
    (dotgit / "vendor" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    cases["dotgit"] = dotgit
    absolute = _tree(tmp_path / "absolute")
    (absolute / "passwd").symlink_to("/etc/passwd")
    cases["absolute"] = absolute
    escaping = _tree(tmp_path / "escaping")
    (escaping / "src" / "up").symlink_to("../../outside")
    cases["escaping"] = escaping
    fifo = _tree(tmp_path / "fifo")
    os.mkfifo(fifo / "pipe")
    cases["fifo"] = fifo
    empty = tmp_path / "empty"
    (empty / "dir").mkdir(parents=True)
    cases["empty"] = empty
    for label, tree in cases.items():
        with pytest.raises(RuntimeFault) as exc:
            _commit(tree, tmp_path / f"repo-{label}", "task-a")
        assert exc.value.code == "TB2_SPEC", label
    with pytest.raises(RuntimeFault):
        _commit(_tree(tmp_path / "ok"), tmp_path / "repo-name", "bad:name")
    not_git = tmp_path / "not-git"
    not_git.mkdir()
    (not_git / "file").write_text("x")
    with pytest.raises(RuntimeFault):
        _commit(_tree(tmp_path / "ok2"), not_git, "task-a")
    with pytest.raises(RuntimeFault):
        tb2.commit_base(
            _tree(tmp_path / "ok3"), tmp_path / "r", "task-a", identity={}, message="m\n"
        )


# -- admission -----------------------------------------------------------------------------------


class FakeContainers:
    """Simulates one task: the tests pass iff ``answer.txt`` holds ``42``; the solution writes
    ``self.answer``. ``script`` overrides exit codes per (purpose, repeat)."""

    def __init__(self, answer: str | None = "42", solution_exit: int = 0) -> None:
        self.answer, self.solution_exit = answer, solution_exit
        self.script: dict[tuple[str, int], int | None] = {}
        self.calls: list[ContainerRun] = []
        self.seen_files: list[dict[str, bytes]] = []
        self.seen_modes: list[dict[str, int]] = []  # permission bits, .git excluded

    def __call__(self, run: ContainerRun) -> int | None:
        self.calls.append(run)
        self.seen_files.append(tb2.snapshot(run.workspace))
        self.seen_modes.append(
            {
                p.relative_to(run.workspace).as_posix(): stat.S_IMODE(p.lstat().st_mode)
                for p in [run.workspace, *run.workspace.rglob("*")]
                if ".git" not in p.relative_to(run.workspace).parts and not p.is_symlink()
            }
        )
        if (run.purpose, run.repeat) in self.script:
            return self.script[(run.purpose, run.repeat)]
        answer = run.workspace / "answer.txt"
        if run.purpose == "solution":
            if self.answer is not None:
                answer.write_text(self.answer)
            (run.workspace / "scratch.log").unlink(missing_ok=True)
            return self.solution_exit
        return 0 if answer.is_file() and answer.read_text() == "42" else 1


def _profile(tmp_path: Path, name: str = "profile.json", **changes: Any) -> Path:
    value = {
        "schema_version": "1.0",
        "engine": "docker",
        "image": IMAGE,
        "uid": 65534,
        "gid": 65534,
        "memory": "2g",
        "cpus": 2.0,
        "pids": 256,
        "network": "none",
        **changes,
    }
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def initial(tmp_path: Path) -> Path:
    root = tmp_path / "initial"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hello')\n")
    (root / "scratch.log").write_text("old\n")
    return root


_REPOS = itertools.count()


def _admit(clone: Path, tmp_path: Path, initial: Path, fake: FakeContainers, **kw: Any) -> dict:
    """Admission of ``initial`` committed into a new tb2 base repository (§10.5 step 3)."""
    repo = tmp_path / f"tb2-repo-{next(_REPOS)}"
    built = _commit(initial, repo, "cancel-async-tasks")
    args: dict[str, Any] = {
        "image": IMAGE,
        "container_profile": _profile(tmp_path),
        "test_command": TEST_COMMAND,
        "solution_command": SOLUTION_COMMAND,
        "base_repo": repo,
        "base_commit": built["commit"],
        "run": fake,
        "scratch": tmp_path / "scratch",
    }
    args.update(kw)
    return tb2.admit(_task(clone), **args)


def test_admission_admits_a_fair_task_under_containment(
    clone: Path, tmp_path: Path, initial: Path
) -> None:
    fake = FakeContainers()
    before = tb2.snapshot(initial)
    result = _admit(clone, tmp_path, initial, fake)
    assert result["admitted"] is True and result["reason"] == "admitted"
    assert result["repeats"] == 3 and len(result["runs"]) == 3
    assert result["patch_stable"] is True
    assert result["constraints"] == {
        "network": "none",
        "uid": 65534,
        "gid": 65534,
        "read_only_root": True,
        "writable": ["/workspace"],
    }
    assert [(c.purpose, c.repeat) for c in fake.calls] == [
        (purpose, r)
        for r in range(3)
        for purpose in ("tests_unmodified", "solution", "tests_reference")
    ]
    task = _task(clone)
    for call in fake.calls:
        assert (call.image, call.network, call.uid, call.gid) == (IMAGE, "none", 65534, 65534)
        assert call.read_only_root is True and call.writable == ("/workspace",)
        if call.purpose == "solution":
            assert call.argv == tuple(SOLUTION_COMMAND) and call.timeout_sec == 900
            assert call.inputs == {tb2.SOLUTION_MOUNT: task.source_dir / "solution"}
        else:
            assert call.argv == tuple(TEST_COMMAND) and call.timeout_sec == 180
            assert call.inputs == {tb2.TESTS_MOUNT: task.source_dir / "tests"}
    # every run gets its own fresh copy; the extracted workspace is never touched
    assert len({c.workspace for c in fake.calls}) == 9
    assert tb2.snapshot(initial) == before
    unmodified, solution, reference = fake.seen_files[:3]
    assert unmodified == before and solution == before
    assert reference["answer.txt"] == b"42" and "scratch.log" not in reference
    row = result["runs"][0]
    assert (row["tests_unmodified_exit"], row["solution_exit"], row["tests_reference_exit"]) == (
        1,
        0,
        0,
    )
    assert row["patch_files"] == 2  # answer.txt written, scratch.log deleted
    assert tb2.SHA1.fullmatch(result["base_commit"]) and tb2.SHA1.fullmatch(result["base_tree"])


def test_admission_workspaces_are_the_trial_workspace(
    clone: Path, tmp_path: Path, initial: Path
) -> None:
    """Every fresh copy is the recorded commit as a trial materializes it: the execute bit and
    in-tree links survive, a local .git holds the base, and the tree is opened for uid 65534."""
    helper = initial / "bin" / "run.sh"
    helper.parent.mkdir()
    helper.write_text("#!/bin/sh\necho ok\n")
    helper.chmod(0o755)
    (initial / "link.py").symlink_to("src/main.py")
    (initial / "src" / "main.py").chmod(0o600)  # not readable by others on the host
    fake = FakeContainers()
    result = _admit(clone, tmp_path, initial, fake)
    assert result["admitted"] is True
    for call, modes in zip(fake.calls, fake.seen_modes, strict=True):
        ws = call.workspace
        assert modes["."] == 0o777 and modes["src"] == 0o777 and modes["bin"] == 0o777
        assert modes["bin/run.sh"] == 0o777  # 0755 kept from the commit, then | 0666
        assert modes["src/main.py"] == 0o666  # 100644 in the commit, opened
        assert os.readlink(ws / "link.py") == "src/main.py"
        assert (ws / ".git").is_dir()
        head = _git_out(ws, "log", "--format=%s", "-1").strip()
        assert head == "amplai base " + result["base_commit"]
    # the patch-applied reference copy is opened as well
    pairs = zip(fake.calls, fake.seen_modes, strict=True)
    reference = [m for c, m in pairs if c.purpose == "tests_reference"]
    assert all(m["answer.txt"] & 0o666 == 0o666 for m in reference)


def test_admission_uses_the_commit_not_a_directory(
    clone: Path, tmp_path: Path, initial: Path
) -> None:
    """Changing the extracted directory after the commit changes nothing the runs see."""
    repo = tmp_path / "tb2-repo"
    built = _commit(initial, repo, "cancel-async-tasks")
    (initial / "answer.txt").write_text("42")  # would make the tests pass unmodified
    fake = FakeContainers()
    result = _admit(clone, tmp_path, initial, fake, base_repo=repo, base_commit=built["commit"])
    assert result["admitted"] is True
    assert "answer.txt" not in fake.seen_files[0]


def test_admission_reasons(clone: Path, tmp_path: Path, initial: Path) -> None:
    cases: list[tuple[FakeContainers, str]] = []
    solved = FakeContainers()
    (initial / "answer.txt").write_text("42")
    result = _admit(clone, tmp_path, initial, solved, scratch=tmp_path / "s0")
    assert (result["admitted"], result["reason"]) == (False, "tests_pass_unmodified")
    (initial / "answer.txt").unlink()
    cases.append((FakeContainers(solution_exit=1), "solution_failed"))
    noop = FakeContainers(answer=None)
    noop.script = {("solution", r): 0 for r in range(3)}  # succeeds, changes nothing
    cases.append((noop, "empty_patch"))
    cases.append((FakeContainers(answer="41"), "reference_fails"))
    flaky = FakeContainers()
    flaky.script = {("tests_reference", 1): 1}
    cases.append((flaky, "repeats_disagree"))
    timeout = FakeContainers()
    timeout.script = {("tests_unmodified", r): None for r in range(3)}
    cases.append((timeout, "timeout"))
    for index, (fake, reason) in enumerate(cases, start=1):
        result = _admit(clone, tmp_path, initial, fake, scratch=tmp_path / f"s{index}")
        assert (result["admitted"], result["reason"]) == (False, reason), reason


def test_admission_repeat_count_and_owned_scratch(
    clone: Path, tmp_path: Path, initial: Path
) -> None:
    fake = FakeContainers()
    result = _admit(clone, tmp_path, initial, fake, repeats=1, scratch=None)
    assert result["admitted"] is True and len(fake.calls) == 3
    assert not any(c.workspace.exists() for c in fake.calls)  # temporary scratch removed


@pytest.mark.parametrize(
    "change",
    [
        {"test_command": None},
        {"solution_command": []},
        {"base_repo": None},
        {"base_commit": None},
        {"base_commit": "abc"},
        {"base_commit": OTHER_SHA},  # not in the repository
        {"base_repo": Path("/nonexistent/tb2")},
        {"repeats": 0},
        {"image": "localhost:5000/amplai-tb2-demo:latest"},
        {"profile": {"network": "bridge"}},
        {"profile": {"uid": 0}},
        {"profile": {"gid": 1000}},
        {"profile": {"image": "localhost:5000/other@sha256:" + "c" * 64}},
    ],
)
def test_admission_holds_without_its_inputs_or_containment(
    clone: Path, tmp_path: Path, initial: Path, change: dict[str, Any]
) -> None:
    fake = FakeContainers()
    kwargs = dict(change)
    if "profile" in kwargs:
        kwargs["container_profile"] = _profile(tmp_path, "changed.json", **kwargs.pop("profile"))
    with pytest.raises(Hold) as exc:
        _admit(clone, tmp_path, initial, fake, **kwargs)
    assert exc.value.code == "TB2_ADMISSION"
    assert fake.calls == []


def test_admission_holds_on_an_unreadable_profile(
    clone: Path, tmp_path: Path, initial: Path
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(Hold) as exc:
        _admit(clone, tmp_path, initial, FakeContainers(), container_profile=broken)
    assert exc.value.code == "TB2_ADMISSION"


def test_patch_helpers(tmp_path: Path) -> None:
    before = {"a.txt": b"1", "b.txt": b"2"}
    patch = tb2.workspace_patch(before, {"a.txt": b"1", "c/d.txt": b"3"})
    assert patch == {"changed": {"c/d.txt": b"3"}, "deleted": ["b.txt"]}
    root = tmp_path / "ws"
    root.mkdir()
    (root / "b.txt").write_text("2")
    tb2.apply_patch(root, patch)
    assert tb2.snapshot(root) == {"c/d.txt": b"3"}
    with pytest.raises(RuntimeFault):
        tb2.apply_patch(root, {"changed": {"../escape.txt": b"x"}, "deleted": []})
    with pytest.raises(RuntimeFault):
        tb2.apply_patch(root, {"changed": {}, "deleted": ["../../etc/hosts"]})
    assert not (tmp_path / "escape.txt").exists()


# -- output --------------------------------------------------------------------------------------


def _corpus_root(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    (root / "tb2").mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "corpus_id": "amplai-bench-v2",
                "version": "2.0.0",
                "split_seed": None,
                "bases": {
                    "tb2": {
                        "app_id": "amplai-tb2",
                        "dir": "bases/tb2",
                        "commits_file": "bases/tb2.commits.json",
                    }
                },
            }
        )
    )
    return root


def test_write_task_output_loads_as_a_corpus_v2_task(
    clone: Path, tmp_path: Path, initial: Path
) -> None:
    task = _task(clone)
    admitted = _admit(clone, tmp_path, initial, FakeContainers())
    root = _corpus_root(tmp_path)
    folder = tb2.write_task(task, admitted, root / "tb2", created="2026-10-01")
    assert folder == root / "tb2" / task.name
    assert sorted(p.name for p in folder.iterdir()) == [
        "NOTICE",
        "environment.json",
        "solution",
        "task.json",
        "tests",
    ]
    tb2.record_base_commit(root / "bases" / "tb2.commits.json", task.name, OTHER_SHA)
    corpus = corpus_v2.load(root)
    loaded = corpus.task(task.name)
    assert (loaded.domain, loaded.grading, loaded.environment_id, loaded.base_id) == (
        "terminal",
        "tb2_tests",
        "tb2-cancel-async-tasks",
        "tb2",
    )
    assert loaded.source["kind"] == "tb2" and loaded.license == "Apache-2.0"
    assert loaded.source["ref"] == f"harbor-framework/terminal-bench-2@{SHA}:{task.name}"
    assert set(loaded.hidden) == {"tests/test.sh", "tests/test_outputs.py"}
    assert set(loaded.reference) == {"solve.sh"}
    assert loaded.objective == (task.source_dir / "instruction.md").read_text()
    assert loaded.acceptance == (tb2.TB2_ACCEPTANCE,)
    meta = json.loads((folder / "task.json").read_text())
    assert meta["subdomain"] == "software-engineering" and meta["split"] is None
    assert corpus_v2.case_payload(corpus, loaded)["base_commit"] == OTHER_SHA
    environment = json.loads((folder / "environment.json").read_text())
    assert environment["environment_id"] == "tb2-cancel-async-tasks"
    assert environment["image"] == IMAGE
    assert environment["container_profile"]["network"] == "none"
    assert environment["admission"]["test_command"] == TEST_COMMAND
    notice = (folder / "NOTICE").read_text()
    assert SHA in notice and "Apache License" in notice
    assert "Terminal-Bench upstream notice." in notice
    # tests and solution are copied byte for byte
    for sub in ("tests", "solution"):
        assert tb2.snapshot(folder / sub) == tb2.snapshot(task.source_dir / sub)


def test_write_task_refuses_unadmitted_or_unpinned(
    clone: Path, tmp_path: Path, initial: Path
) -> None:
    task = _task(clone)
    dest = tmp_path / "tb2"
    admitted = _admit(clone, tmp_path, initial, FakeContainers())
    with pytest.raises(Hold) as exc:
        tb2.write_task(task, {**admitted, "admitted": False}, dest)
    assert exc.value.code == "TB2_ADMISSION"
    with pytest.raises(Hold) as exc:
        tb2.write_task(_task(clone, "fix-permissions"), admitted, dest)
    assert exc.value.code == "TB2_ADMISSION"
    unpinned = next(t for t in tb2.scan(clone) if t.name == task.name)
    with pytest.raises(Hold) as exc:
        tb2.write_task(unpinned, admitted, dest)
    assert exc.value.code == "TB2_SOURCE"
    tb2.write_task(task, admitted, dest, created="2026-10-01")
    with pytest.raises(RuntimeFault):
        tb2.write_task(task, admitted, dest, created="2026-10-01")
    assert not (dest / "fix-permissions").exists()


# -- script --------------------------------------------------------------------------------------


def _script() -> Any:
    return _script_module("tb2_adapter")


def test_script_scan_select_and_spec(
    clone: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script()
    assert script.main(["scan", "--source", str(clone), "--commit", SHA]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tasks"] == 3 and report["skipped"][0]["name"] == "train-on-gpu"
    assert report["categories"] == {"software-engineering": 2, "system-administration": 1}
    assert script.main(["select", "--source", str(clone), "--commit", SHA, "--limit", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["names"] == [
        "cancel-async-tasks",
        "fix-permissions",
    ]
    base = ["spec", "--source", str(clone), "--commit", SHA, "--task", "cancel-async-tasks"]
    assert script.main([*base, "--driver-layer-image", DRIVER_LAYER]) == 1  # unknowns open
    assert json.loads(capsys.readouterr().out)["ready"] is False
    out = tmp_path / "spec-out"
    argv = [*base, "--driver-layer-image", DRIVER_LAYER, "--driver-path", "/opt/d"]
    assert script.main([*argv, "--workdir", "/app", "--out", str(out)]) == 0
    capsys.readouterr()
    assert (out / "Dockerfile").read_text().startswith("# AMPLAI Work 033 S7a")
    assert script.main(["scan", "--source", str(clone), "--commit", OTHER_SHA]) == 2
    assert "TB2_SOURCE" in capsys.readouterr().err


def test_script_prints_operator_commands_without_running_them(
    clone: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script()
    base = ["--source", str(clone), "--commit", SHA, "--task", "cancel-async-tasks"]
    assert script.main(["workdir", *base]) == 0
    assert "{{.Config.WorkingDir}}" in capsys.readouterr().out
    dest = tmp_path / "extracted"
    assert script.main(["extract", *base, "--workdir", "/app", "--dest", str(dest)]) == 0
    assert capsys.readouterr().out.count("docker ") == 3


def test_script_admit_requires_the_unknown_entry_commands(clone: Path) -> None:
    script = _script()
    argv = [
        "admit",
        "--source",
        str(clone),
        "--commit",
        SHA,
        "--task",
        "cancel-async-tasks",
        "--image",
        IMAGE,
        "--container-profile",
        "p.json",
        "--base-repo",
        "repo",
        "--commits-file",
        "tb2.commits.json",
    ]
    with pytest.raises(SystemExit) as exc:
        script.main(argv)
    assert exc.value.code == 2


def test_script_base_commit_then_admit_from_the_recorded_commit(
    clone: Path,
    tmp_path: Path,
    initial: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _script()
    repo, commits = tmp_path / "amplai-tb2", tmp_path / "corpus" / "bases" / "tb2.commits.json"
    base = ["--commits-file", str(commits)]
    for name in ("cancel-async-tasks", "fix-permissions"):
        argv = ["base-commit", "--task", name, "--extracted", str(initial), "--repo", str(repo)]
        assert script.main([*argv, *base]) == 0
    built = json.loads(capsys.readouterr().out.split("\n}\n")[0] + "\n}")
    recorded = json.loads(commits.read_text())
    assert list(recorded) == ["cancel-async-tasks", "fix-permissions"]
    assert recorded["cancel-async-tasks"] == built["commit"]
    assert built["ref"] == "refs/heads/tb2/cancel-async-tasks"
    fake = FakeContainers()
    monkeypatch.setattr(tb2, "docker_runner", lambda profile: fake)
    argv = [
        "admit",
        "--source",
        str(clone),
        "--commit",
        SHA,
        "--task",
        "cancel-async-tasks",
        "--image",
        IMAGE,
        "--container-profile",
        str(_profile(tmp_path)),
        "--base-repo",
        str(repo),
        "--test-command",
        " ".join(TEST_COMMAND),
        "--solution-command",
        " ".join(SOLUTION_COMMAND),
        "--scratch",
        str(tmp_path / "scratch"),
        *base,
    ]
    assert script.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["admitted"] is True and result["base_commit"] == built["commit"]
    assert len(fake.calls) == 9
    commits.write_text("{}")
    argv[argv.index(str(tmp_path / "scratch"))] = str(tmp_path / "scratch2")
    assert script.main(argv) == 2
    assert "TB2_ADMISSION" in capsys.readouterr().err
