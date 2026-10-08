"""Work 033 ``tb2_tests`` grading (interfaces §10.5 step 6, §14 Q7, clarification "TB2 Test Entry
And Grading (2026-10-08)"): the task's own test entry in the task image, verdict from the reward
file, network none and uid 65534. No docker: a fake task and a fake runner."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness import corpus_v2, tb2, tb2_grading
from amplai_foundry.meta_harness.local_corpus import CorpusError, Outcome
from amplai_foundry.meta_harness.tb2 import ContainerRun, Tb2Task
from amplai_foundry.meta_harness.tb2_grading import GradeRun, TestEntry
from amplai_foundry.runtime.errors import Hold

REPO = Path(__file__).resolve().parents[2]
SHA = "0123456789abcdef0123456789abcdef01234567"
IMAGE = "localhost:5000/amplai-tb2-demo@sha256:" + "a" * 64
APACHE = "Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/\n"
# The TB2 test.sh shape (runs/tb2-q7.md): the script exits 0 either way, the reward is the verdict.
TEST_SH = """#!/bin/bash
pytest /tests/test_outputs.py -rA
if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
"""
SOLUTION_COMMAND = ["bash", f"{tb2.SOLUTION_MOUNT}/solve.sh"]
_REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def _no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    def local_git_only(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list | tuple) and argv and argv[0] == "git":
            return _REAL_RUN(argv, *args, **kwargs)
        raise AssertionError(f"no container runs in grading tests: {argv!r}")

    monkeypatch.setattr(subprocess, "run", local_git_only)
    monkeypatch.setattr(tb2, "git_head", lambda source: SHA)


def _script_module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _toml(verifier_timeout: int = 180) -> str:
    return (
        '[metadata]\ndifficulty = "medium"\ncategory = "software-engineering"\ntags = []\n\n'
        f"[verifier]\ntimeout_sec = {verifier_timeout}.0\n\n[agent]\ntimeout_sec = 900.0\n\n"
        '[environment]\ndocker_image = "alexgshaw/fake-task:20251031"\ngpus = 0\n'
        "allow_internet = true\n"
    )


def _fake_task(root: Path, name: str, *, test_sh: bool = True) -> Path:
    folder = root / name
    (folder / "environment").mkdir(parents=True)
    (folder / "environment" / "Dockerfile").write_text(
        "FROM python:3.13-slim-bookworm\nWORKDIR /build\nRUN true\nWORKDIR /app\n"
    )
    (folder / "instruction.md").write_text("Write 42 to answer.txt.\n")
    (folder / "task.toml").write_text(_toml())
    (folder / "tests").mkdir()
    if test_sh:
        (folder / "tests" / "test.sh").write_text(TEST_SH)
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
    _fake_task(root, "fake-task")
    _fake_task(root, "no-entry", test_sh=False)
    return root


def _task(clone: Path, name: str = "fake-task") -> Tb2Task:
    return next(t for t in tb2.scan(clone, commit=SHA) if t.name == name)


def _profile(**changes: Any) -> dict[str, Any]:
    return {"image": IMAGE, "uid": 65534, "gid": 65534, "network": "none", **changes}


class FakeGrader:
    """Plays the task's test.sh: reward 1 iff ``answer.txt`` holds ``42``; always exit 0.
    ``reward`` / ``code`` / ``extra`` override what the run writes and returns."""

    def __init__(
        self,
        reward: str | None = "auto",
        code: int | None = 0,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.reward, self.code, self.extra = reward, code, extra or {}
        self.calls: list[GradeRun] = []
        self.verifier_listing: list[list[str]] = []

    def __call__(self, run: GradeRun) -> int | None:
        self.calls.append(run)
        self.verifier_listing.append(sorted(p.name for p in run.verifier.iterdir()))
        assert run.tests.is_dir() and run.workspace.is_dir()
        reward = self.reward
        if reward == "auto":
            answer = run.workspace / "answer.txt"
            reward = "1" if answer.is_file() and answer.read_text() == "42" else "0"
        if reward is not None:
            (run.verifier / "reward.txt").write_text(reward + "\n")
        for name, text in self.extra.items():
            (run.verifier / name).write_text(text)
        return self.code


def _workspace(tmp_path: Path, answer: str | None) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    if answer is not None:
        (ws / "answer.txt").write_text(answer)
    return ws


def _grade(clone: Path, tmp_path: Path, fake: Any, answer: str | None = "42", **kw: Any) -> Any:
    task = _task(clone)
    args: dict[str, Any] = {
        "image": IMAGE,
        "profile": _profile(),
        "run": fake,
        "scratch": tmp_path / "scratch",
    }
    args.update(kw)
    return tb2_grading.grade(
        tb2_grading.entry_for(task),
        _workspace(tmp_path, answer),
        task.source_dir / "tests",  # type: ignore[operator]
        **args,
    )


# -- the entry -----------------------------------------------------------------------------------


def test_entry_is_the_tasks_own_test_sh_graded_by_reward(clone: Path) -> None:
    entry = tb2_grading.entry_for(_task(clone))
    assert entry == TestEntry(("bash", "/tests/test.sh"), "reward_file", 180, "/app")
    assert tb2_grading.entry_from_json(entry.to_json()) == entry
    assert entry.to_json()["verifier_dir"] == "/logs/verifier"


def test_missing_entry_is_none_and_grades_null_without_running(clone: Path, tmp_path: Path) -> None:
    task = _task(clone, "no-entry")
    assert tb2_grading.entry_for(task) is None
    fake = FakeGrader()
    outcome = tb2_grading.grade(
        None, _workspace(tmp_path, "42"), task.source_dir / "tests",  # type: ignore[operator]
        image=IMAGE, profile=_profile(), run=fake,
    )  # fmt: skip
    assert outcome.result is None and outcome.success is False and fake.calls == []
    assert "unknown" in outcome.detail


@pytest.mark.parametrize(
    "value",
    [None, {}, {"schema": "other"}, {"schema": tb2_grading.ENTRY_SCHEMA, "command": []}],
)
def test_malformed_recorded_entry_is_unknown(value: Any) -> None:
    assert tb2_grading.entry_from_json(value) is None


# -- verdicts ------------------------------------------------------------------------------------


def test_pass_by_reward_one(clone: Path, tmp_path: Path) -> None:
    fake = FakeGrader()
    outcome = _grade(clone, tmp_path, fake, "42")
    assert outcome.result is True and outcome.success is True
    assert (outcome.reward, outcome.exit_status) == ("1", 0)
    assert isinstance(outcome, Outcome)
    # the fresh verifier directory was empty and is removed afterwards
    assert fake.verifier_listing == [[]] and not fake.calls[0].verifier.exists()


def test_fail_by_reward_zero_although_the_script_exits_zero(clone: Path, tmp_path: Path) -> None:
    outcome = _grade(clone, tmp_path, FakeGrader(), "41")
    assert outcome.result is False and outcome.success is False
    assert (outcome.reward, outcome.exit_status) == ("0", 0)


@pytest.mark.parametrize(
    ("fake", "fragment"),
    [
        (FakeGrader(reward=None, code=127), "no reward.txt"),
        (FakeGrader(reward=""), "neither 1 nor 0"),
        (FakeGrader(reward="0.5"), "neither 1 nor 0"),
        (FakeGrader(code=None), "timed out"),
        (FakeGrader(extra={"reward.json": '{"reward": 1}'}), "reward.json"),
    ],
)
def test_unreadable_verdicts_are_null(
    clone: Path, tmp_path: Path, fake: FakeGrader, fragment: str
) -> None:
    outcome = _grade(clone, tmp_path, fake)
    assert outcome.result is None and outcome.success is False
    assert fragment in outcome.detail


def test_exit_status_entry(clone: Path, tmp_path: Path) -> None:
    task = _task(clone)
    entry = TestEntry(("bash", "/tests/run.sh"), "exit_status", 60)
    for code, expected in ((0, True), (3, False), (None, None)):
        outcome = tb2_grading.grade(
            entry, _workspace(tmp_path, None), task.source_dir / "tests",  # type: ignore[operator]
            image=IMAGE, profile=_profile(), run=FakeGrader(reward=None, code=code),
        )  # fmt: skip
        assert outcome.result is expected


# -- containment ---------------------------------------------------------------------------------


def test_every_run_has_network_none_uid_65534_and_a_read_only_root(
    clone: Path, tmp_path: Path
) -> None:
    fake = FakeGrader()
    _grade(clone, tmp_path, fake)
    (run,) = fake.calls
    assert (run.image, run.network, run.uid, run.gid) == (IMAGE, "none", 65534, 65534)
    assert run.read_only_root is True and run.writable == ("/workspace", "/logs/verifier")
    assert run.argv == ("bash", "/tests/test.sh") and run.timeout_sec == 180
    assert run.tests == _task(clone).source_dir / "tests"  # type: ignore[operator]


@pytest.mark.parametrize(
    "profile",
    [
        _profile(network="bridge"),
        _profile(uid=0),
        _profile(gid=0),
        _profile(image="localhost:5000/other@sha256:" + "c" * 64),
    ],
)
def test_grading_holds_outside_the_containment(
    clone: Path, tmp_path: Path, profile: dict[str, Any]
) -> None:
    fake = FakeGrader()
    with pytest.raises(Hold) as exc:
        _grade(clone, tmp_path, fake, profile=profile)
    assert exc.value.code == "TB2_GRADING" and fake.calls == []


def test_grading_holds_on_an_unpinned_image(clone: Path, tmp_path: Path) -> None:
    with pytest.raises(Hold) as exc:
        _grade(
            clone,
            tmp_path,
            FakeGrader(),
            image="alexgshaw/fake-task:20251031",
            profile=_profile(image="alexgshaw/fake-task:20251031"),
        )
    assert exc.value.code == "TB2_GRADING"


def test_docker_runner_argv(clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        seen.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, '[{"State": {"Running": false}}]', "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    task = _task(clone)
    tests = task.source_dir / "tests"  # type: ignore[operator]
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    runner = tb2_grading.docker_runner(_profile())
    ws = _workspace(tmp_path, "42")
    code = runner(GradeRun(IMAGE, ("bash", "/tests/test.sh"), ws, tests, verifier, 30))
    assert code == 0
    argv = seen[0]
    assert argv[:2] == ["docker", "run"] and argv[-3:] == [IMAGE, "bash", "/tests/test.sh"]
    joined = " ".join(argv)
    for flag in ("--read-only", "--cap-drop=ALL", "--network none", "--user 65534:65534"):
        assert flag in joined
    assert f"type=bind,src={tests},dst=/tests,readonly" in argv
    assert f"type=bind,src={verifier},dst=/logs/verifier" in argv
    assert f"type=bind,src={ws},dst=/workspace" in argv
    with pytest.raises(Hold):
        runner(GradeRun(IMAGE, ("x",), ws, tests, verifier, 30, network="bridge"))
    with pytest.raises(Hold):
        runner(GradeRun(IMAGE, ("x",), ws, tests, verifier, 30, uid=0))


# -- admission and corpus wiring -----------------------------------------------------------------


class FakeSolution:
    def __init__(self) -> None:
        self.calls: list[ContainerRun] = []

    def __call__(self, run: ContainerRun) -> int | None:
        self.calls.append(run)
        assert run.purpose == "solution"
        (run.workspace / "answer.txt").write_text("42")
        return 0


def _base(clone: Path, tmp_path: Path) -> tuple[Path, str]:
    initial = tmp_path / "initial"
    (initial / "src").mkdir(parents=True)
    (initial / "src" / "main.py").write_text("print('hello')\n")
    fixed = _script_module("corpus_base_repo")
    repo = tmp_path / "tb2-repo"
    built = tb2.commit_base(
        initial,
        repo,
        "fake-task",
        identity=fixed.IDENTITY,
        message=fixed.message_for("tb2/fake-task"),
    )
    return repo, built["commit"]


def _profile_file(tmp_path: Path) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({**_profile(), "memory": "2g", "cpus": 2.0, "pids": 256}))
    return path


def _admit(clone: Path, tmp_path: Path, fake: FakeGrader) -> dict[str, Any]:
    repo, commit = _base(clone, tmp_path)
    task = _task(clone)
    entry = tb2_grading.entry_for(task)
    assert entry is not None
    return tb2_grading.admit(
        task,
        entry=entry,
        image=IMAGE,
        container_profile=_profile_file(tmp_path),
        solution_command=SOLUTION_COMMAND,
        base_repo=repo,
        base_commit=commit,
        scratch=tmp_path / "scratch",
        grade_run=fake,
        solution_run=FakeSolution(),
    )


def test_admission_grades_its_test_steps_by_the_reward(clone: Path, tmp_path: Path) -> None:
    fake = FakeGrader()
    result = _admit(clone, tmp_path, fake)
    assert result["admitted"] is True and result["reason"] == "admitted"
    assert result["test_command"] == ["bash", "/tests/test.sh"]
    assert result["grading"]["result"] == "reward_file"
    grades = result["test_grades"]
    assert [(g["purpose"], g["result"], g["exit_status"]) for g in grades[:2]] == [
        ("tests_unmodified", False, 0),
        ("tests_reference", True, 0),
    ]
    assert len(fake.calls) == 6 and all(c.network == "none" for c in fake.calls)


def test_admission_reports_ungraded_test_steps(clone: Path, tmp_path: Path) -> None:
    result = _admit(clone, tmp_path, FakeGrader(reward=None))
    assert result["admitted"] is False and result["reason"] == "timeout"
    assert all(g["result"] is None for g in result["test_grades"])


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    (root / "tb2").mkdir(parents=True)
    bases = {"tb2": {"app_id": "amplai-tb2", "dir": "bases/tb2", "commits_file": "c.json"}}
    manifest = {"corpus_id": "amplai-bench-v2", "version": "2.0.0", "bases": bases}
    (root / "manifest.json").write_text(json.dumps({**manifest, "split_seed": None}))
    return root


def test_written_task_is_graded_by_corpus_v2(clone: Path, tmp_path: Path) -> None:
    admitted = _admit(clone, tmp_path, FakeGrader())
    root = _corpus(tmp_path)
    folder = tb2_grading.write_task(_task(clone), admitted, root / "tb2", created="2026-10-08")
    environment = json.loads((folder / "environment.json").read_text())
    assert environment["grading"] == admitted["grading"]
    task = corpus_v2.load(root).task("fake-task")
    fake = FakeGrader()
    grader = tb2_grading.Grader(root, run=fake, scratch=tmp_path / "grade-scratch")
    passed = corpus_v2.grade(task, _workspace(tmp_path / "a", "42"), tb2_grader=grader)
    failed = corpus_v2.grade(task, _workspace(tmp_path / "b", "7"), tb2_grader=grader)
    assert isinstance(passed, tb2_grading.Tb2Outcome) and passed.result is True
    assert isinstance(failed, tb2_grading.Tb2Outcome) and failed.result is False
    assert fake.calls[0].tests == folder / "tests"
    with pytest.raises(CorpusError) as exc:
        corpus_v2.grade(task, _workspace(tmp_path / "c", "42"))
    assert exc.value.code == "TASK_GRADING"


def test_corpus_task_without_a_recorded_entry_grades_null(clone: Path, tmp_path: Path) -> None:
    admitted = _admit(clone, tmp_path, FakeGrader())
    root = _corpus(tmp_path)
    tb2.write_task(_task(clone), admitted, root / "tb2", created="2026-10-08")  # no entry
    task = corpus_v2.load(root).task("fake-task")
    fake = FakeGrader()
    outcome = corpus_v2.grade(
        task, _workspace(tmp_path, "42"), tb2_grader=tb2_grading.Grader(root, run=fake)
    )
    assert isinstance(outcome, tb2_grading.Tb2Outcome)
    assert outcome.result is None and fake.calls == []


def test_script_admit_defaults_to_the_task_entry(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    repo, commit = _base(clone, tmp_path)
    commits = tmp_path / "tb2.commits.json"
    commits.write_text(json.dumps({"fake-task": commit}))
    fake = FakeGrader()
    monkeypatch.setattr(tb2_grading, "docker_runner", lambda profile: fake)
    monkeypatch.setattr(tb2, "docker_runner", lambda profile: FakeSolution())
    script = _script_module("tb2_adapter")
    out = tmp_path / "admission.json"
    common = ["--source", str(clone), "--commit", SHA]
    argv = [
        "admit", *common, "--task", "fake-task", "--image", IMAGE,
        "--container-profile", str(_profile_file(tmp_path)), "--base-repo", str(repo),
        "--commits-file", str(commits), "--solution-command", " ".join(SOLUTION_COMMAND),
        "--scratch", str(tmp_path / "scratch"), "--out", str(out),
    ]  # fmt: skip
    assert script.main(argv) == 0
    result = json.loads(out.read_text())
    assert result["admitted"] is True and result["grading"]["command"] == ["bash", "/tests/test.sh"]
    assert len(fake.calls) == 6
    root = _corpus(tmp_path)
    write = ["write", *common, "--task", "fake-task", "--admission", str(out)]
    assert script.main([*write, "--dest", str(root / "tb2"), "--created", "2026-10-08"]) == 0
    environment = json.loads((root / "tb2" / "fake-task" / "environment.json").read_text())
    assert environment["grading"]["result"] == "reward_file"
    capsys.readouterr()
    no_entry = [a if a != "fake-task" else "no-entry" for a in argv]
    no_entry[no_entry.index(str(tmp_path / "scratch"))] = str(tmp_path / "scratch2")
    commits.write_text(json.dumps({"fake-task": commit, "no-entry": commit}))
    assert script.main(no_entry) == 2
    assert "TB2_ADMISSION" in capsys.readouterr().err
