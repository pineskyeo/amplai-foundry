"""Work 033 OD-15 wave 1: ``scripts/tb2_oracle_wave.py`` pure helpers (screen verdict, oracle
image Dockerfile, admission profile, screen argv, record reuse, admission outcome, report rows)
and the fail-closed paths of the docker steps against a fake ``_sh``. No docker: the docker
steps are operator-run (``runs/tb2-oracle-wave1.md``)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness import tb2, tb2_grading
from amplai_foundry.meta_harness.tb2 import Tb2Task
from amplai_foundry.runtime.errors import RuntimeFault

REPO = Path(__file__).resolve().parents[2]
PINNED = "alexgshaw/fake-task@sha256:" + "c" * 64


def _wave() -> Any:
    path = REPO / "scripts" / "tb2_oracle_wave.py"
    spec = importlib.util.spec_from_file_location("tb2_oracle_wave", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wave = _wave()


def _task(name: str = "fake-task") -> Tb2Task:
    return Tb2Task(name, "alexgshaw/fake-task:20251031", True, 0, "games", "hard", 900, 900)


# -- screen verdict ------------------------------------------------------------------------------


def test_screen_passes_when_the_pytest_session_starts() -> None:
    out = "Collecting pytest\n============ test session starts ============\n1 failed\n"
    verdict = wave.classify_screen(out, 0, "0")
    assert verdict["status"] == "runner_started"
    assert verdict["reward"] == "0" and verdict["exit_status"] == 0


def test_screen_without_a_session_is_test_entry_offline_with_the_tail() -> None:
    out = "\n".join(
        [
            "E: Unable to acquire the dpkg frontend lock",
            "WARNING: The requested image's platform (linux/amd64) does not match the detected "
            "host platform (linux/arm64/v8) and no specific platform was requested",
            "/tests/test.sh: line 8: curl: command not found",
            "/tests/test.sh: line 20: uvx: command not found",
        ]
    )
    verdict = wave.classify_screen(out, 0, "0")
    assert verdict["status"] == "test_entry_offline"
    # docker's emulation notice is not task output
    assert all("host platform" not in line for line in verdict["tail"])
    assert verdict["tail"][-1].endswith("uvx: command not found")


def test_screen_tail_is_bounded() -> None:
    out = "\n".join(f"line {i}" for i in range(100))
    assert len(wave.classify_screen(out, 1, None)["tail"]) == wave.TAIL_LINES


def test_screen_timeout_without_a_session_is_undecided() -> None:
    assert wave.classify_screen("installing...\n", None, None)["status"] == "screen_timeout"
    started = wave.classify_screen("== test session starts ==\n", None, None)
    assert started["status"] == "runner_started"


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ("docker: Error response from daemon: invalid mount config for type bind\n", 125),
        ("", 125),  # docker itself failed, no message captured
        (
            "docker: Error response from daemon: failed to create task for container: "
            'OCI runtime create failed: exec: "bash": executable file not found\n',
            127,
        ),
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n", 1),
        ("error while creating mount source path '/x': mkdir /x: read-only file system\n", 126),
    ],
)
def test_screen_launch_error_is_never_test_entry_offline(output: str, code: int) -> None:
    verdict = wave.classify_screen(output, code, None)
    assert verdict["status"] == "screen_launch_error"


def test_container_command_exit_127_without_a_docker_message_is_the_task() -> None:
    # bash returns the script's last status: `uvx: command not found` exits 127
    out = "/tests/test.sh: line 19: uvx: command not found\n"
    assert wave.classify_screen(out, 127, "0")["status"] == "test_entry_offline"


def test_a_launch_error_screen_is_screened_again() -> None:
    assert not wave.screen_current(_screen(status="screen_launch_error"), _task(), COMMIT)
    assert wave.screen_current(_screen(status="test_entry_offline"), _task(), COMMIT)


# -- oracle image --------------------------------------------------------------------------------


def test_oracle_dockerfile_is_the_image_spec_without_the_driver_layer() -> None:
    text = wave.oracle_dockerfile(_task(), PINNED, "/app")
    lines = text.splitlines()
    assert f"FROM {PINNED}" in lines
    assert not any(line.startswith("COPY") for line in lines)
    assert "RUN mkdir -p /workspace && rm -rf /app && ln -s /workspace /app" in lines
    assert lines[-3:] == [f"USER {tb2.UID}:{tb2.GID}", "WORKDIR /workspace", "ENTRYPOINT []"]
    assert sum(line.startswith("FROM ") for line in lines) == 1


def test_oracle_dockerfile_for_a_workspace_workdir_needs_no_link() -> None:
    text = wave.oracle_dockerfile(_task(), PINNED, "/workspace")
    assert "RUN mkdir -p /workspace" in text.splitlines()
    assert "ln -s" not in text


@pytest.mark.parametrize("image", ["alexgshaw/fake-task:20251031", "fake@sha256:abc"])
def test_oracle_dockerfile_needs_a_pinned_task_image(image: str) -> None:
    with pytest.raises(RuntimeFault):
        wave.oracle_dockerfile(_task(), image, "/app")


# -- profile and argv ----------------------------------------------------------------------------


def test_profile_is_the_v3_containment_capped_at_the_local_vm() -> None:
    big = wave.container_profile(PINNED, {"environment": {"memory_mb": 8192, "cpus": 4}})
    assert big["memory"] == "2g" and big["cpus"] == 2.0
    assert (big["uid"], big["gid"], big["network"]) == (65534, 65534, "none")
    assert big["image"] == PINNED
    small = wave.container_profile(PINNED, {"environment": {"memory_mb": 1024, "cpus": 1}})
    assert small["memory"] == "1024m" and small["cpus"] == 1.0
    assert wave.container_profile(PINNED, {})["memory"] == "2g"


def test_screen_argv_runs_the_entry_in_the_admission_layout(tmp_path: Path) -> None:
    oracle = "sha256:" + "d" * 64  # the locally built oracle image
    profile = wave.container_profile(oracle, {})
    argv = wave.screen_argv(
        oracle, tmp_path / "ws", tmp_path / "tests", tmp_path / "verifier", "amplai-x", profile
    )
    assert argv[:2] == ["docker", "run"]
    # containment is unchanged
    for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert flag in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "65534:65534"
    # the admission layout: a writable workspace copy at /workspace, the working directory there
    assert f"type=bind,src={tmp_path / 'ws'},dst=/workspace" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace"
    assert f"type=bind,src={tmp_path / 'tests'},dst=/tests,readonly" in argv
    assert f"type=bind,src={tmp_path / 'verifier'},dst=/logs/verifier" in argv
    writable = [a for a in argv if a.startswith("type=bind") and not a.endswith(",readonly")]
    assert len(writable) == 2  # /workspace and /logs/verifier only
    assert argv[-3:] == [oracle, "bash", f"{tb2_grading.TESTS_DIR}/test.sh"]


# -- record reuse --------------------------------------------------------------------------------

COMMIT = "2fd12b8" + "0" * 33


def _screen(**kw: Any) -> dict[str, Any]:
    record = {
        "schema": wave.SCREEN_SCHEMA,
        "task": "fake-task",
        "image": "alexgshaw/fake-task:20251031",
        "source_commit": COMMIT,
        "pinned": PINNED,
        "workdir": "/app",
        "platform": "linux/amd64",
        "status": "runner_started",
    }
    record.update(kw)
    return record


def test_screen_record_is_reused_only_for_the_same_commit_image_and_digest() -> None:
    assert wave.screen_current(_screen(), _task(), COMMIT)
    for stale in (
        _screen(schema="amplai.tb2-oracle-screen.v1"),  # the earlier layout
        _screen(source_commit="f" * 40),
        _screen(image="alexgshaw/fake-task:20260101"),
        _screen(pinned=None),  # no digest: never reused
        _screen(pinned="alexgshaw/other@sha256:" + "c" * 64),
        _screen(task="other"),
    ):
        assert not wave.screen_current(stale, _task(), COMMIT)
    assert not wave.screen_current(_screen(), _task(), "e" * 40)


def test_admit_record_belongs_to_the_current_screen() -> None:
    record = {
        "task": "fake-task",
        "source_commit": COMMIT,
        "pinned": PINNED,
        "screen_schema": wave.SCREEN_SCHEMA,
    }
    assert wave.admit_current(record, _screen(), COMMIT)
    assert not wave.admit_current(record, _screen(pinned="x@sha256:" + "e" * 64), COMMIT)
    assert not wave.admit_current({**record, "source_commit": "f" * 40}, _screen(), COMMIT)
    assert not wave.admit_current({"task": "fake-task"}, _screen(), COMMIT)


def _settings(**kw: Any) -> dict[str, Any]:
    args = argparse.Namespace(repeats=3, dest=Path("/corpus/tb2"), registry="localhost:5000")
    for key, value in kw.items():
        setattr(args, key, value)
    return dict(wave.admit_settings(args))


def test_admit_record_is_reused_only_for_the_same_repeats_dest_and_registry() -> None:
    record = {"task": "fake-task", "source_commit": COMMIT, "pinned": PINNED, **_settings()}
    assert wave.admit_current(record, _screen(), COMMIT, _settings())
    for later in (
        _settings(repeats=1),
        _settings(dest=None),
        _settings(dest=Path("/other/tb2")),
        _settings(registry="registry.local:5000"),
    ):
        assert not wave.admit_current(record, _screen(), COMMIT, later)
    # a one-repeat run without --dest is not reused for a three-repeat run with --dest
    quick = {**record, **_settings(repeats=1, dest=None)}
    assert not wave.admit_current(quick, _screen(), COMMIT, _settings())
    # a record of the earlier format (no settings) is admitted again
    bare = {"task": "fake-task", "source_commit": COMMIT, "pinned": PINNED}
    assert not wave.admit_current(bare, _screen(), COMMIT, _settings(dest=None))


def test_base_commit_is_reused_only_for_the_same_image_digest() -> None:
    sha = "1" * 40
    base = {"task": "fake-task", "pinned": PINNED, "commit": sha}
    assert wave.base_current({"fake-task": sha}, base, "fake-task", PINNED)
    other = "alexgshaw/fake-task@sha256:" + "e" * 64
    assert not wave.base_current({"fake-task": sha}, base, "fake-task", other)
    assert not wave.base_current({"fake-task": "2" * 40}, base, "fake-task", PINNED)
    assert not wave.base_current({"fake-task": sha}, None, "fake-task", PINNED)  # no digest record
    assert not wave.base_current({"fake-task": sha}, {}, "fake-task", PINNED)
    assert not wave.base_current({}, base, "fake-task", PINNED)


ORACLE = "localhost:5000/amplai-tb2-oracle-fake-task@sha256:" + "a" * 64


def _result(**kw: Any) -> dict[str, Any]:
    result = {"task": "fake-task", "image": ORACLE, "admitted": True, "reason": "admitted"}
    result.update(kw)
    return result


@pytest.mark.parametrize(
    ("code", "result", "expected"),
    [
        (0, _result(), (True, "admitted")),
        (1, _result(admitted=False, reason="timeout"), (False, "timeout")),
        # the return code disagrees with the result
        (1, _result(), (False, "admission_error")),
        (2, _result(), (False, "admission_error")),
        # a result of another task or image (a stale file)
        (0, _result(task="other"), (False, "admission_error")),
        (0, _result(image=PINNED), (False, "admission_error")),
        (0, None, (False, "admission_error")),
    ],
)
def test_admission_outcome_needs_an_agreeing_return_code(
    code: int, result: dict[str, Any] | None, expected: tuple[bool, str]
) -> None:
    assert wave.admission_outcome(code, result, "fake-task", ORACLE) == expected


# -- docker steps with a fake docker -------------------------------------------------------------


def _done(code: int = 0, out: str = "") -> Any:
    return subprocess.CompletedProcess([], code, out, "")


def test_screen_without_a_digest_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def sh(argv: list[str], timeout: int | None = None) -> Any:
        calls.append(argv)
        return _done()

    monkeypatch.setattr(wave, "_sh", sh)
    monkeypatch.setattr(wave, "_compressed_bytes", lambda image: 1000)
    monkeypatch.setattr(wave, "_repo_digest", lambda image: None)
    record = wave.screen_one(_task(), tmp_path, 3.0, COMMIT)
    assert record["status"] == "no_digest" and record["source_commit"] == COMMIT
    assert "pinned" not in record
    # the mutable tag is pulled and removed, never run
    assert not any(argv[:2] == ["docker", "run"] for argv in calls)
    assert ["docker", "image", "rm", "alexgshaw/fake-task:20251031"] in calls


def _admit_setup(tmp_path: Path, base_pinned: str) -> tuple[Tb2Task, argparse.Namespace]:
    """A task with a recorded base commit made from ``base_pinned``."""
    source = tmp_path / "fake-task"
    (source / "tests").mkdir(parents=True)
    (source / "task.toml").write_text("[environment]\n")
    # the runner the oracle image bakes (TB2 option A, tb2_test_deps)
    (source / "tests" / "test.sh").write_text(
        "uvx -p 3.13 -w pytest==8.4.1 -w pytest-json-ctrf==0.3.5 pytest /tests/test_outputs.py\n"
    )
    task = Tb2Task(
        "fake-task", "alexgshaw/fake-task:20251031", True, 0, "games", "hard", 900, 900, source
    )
    out = tmp_path / "out"
    out.mkdir()
    commits = tmp_path / "tb2.commits.json"
    commits.write_text(json.dumps({"fake-task": "1" * 40}))
    base = {"task": "fake-task", "pinned": base_pinned, "commit": "1" * 40}
    (out / "fake-task.base.json").write_text(json.dumps(base))
    args = argparse.Namespace(
        out=out,
        source=tmp_path,
        commit=COMMIT,
        base_repo=tmp_path / "base",
        commits_file=commits,
        registry="localhost:5000",
        repeats=3,
        dest=tmp_path / "corpus",
    )
    return task, args


def test_admit_redoes_the_base_commit_for_a_changed_image_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = "alexgshaw/fake-task@sha256:" + "e" * 64
    task, args = _admit_setup(tmp_path, base_pinned=old)
    monkeypatch.setattr(wave, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(wave, "_sh", lambda argv, timeout=None: _done())
    adapter_calls: list[tuple[str, ...]] = []

    def adapter(*argv: str) -> int:
        adapter_calls.append(argv)
        return 2  # commit_base refuses to replace the recorded base of another tree

    monkeypatch.setattr(wave, "_adapter", adapter)
    record = wave.admit_one(task, _screen(), args)
    assert [c[0] for c in adapter_calls] == ["base-commit"]  # not skipped, never graded
    assert (record["stage"], record["reason"]) == ("base-commit", "base_commit")
    assert not (args.out / "fake-task.base.json").exists()  # the old digest record is gone


def test_admit_records_the_digest_of_a_new_base_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, args = _admit_setup(tmp_path, base_pinned=PINNED)
    (args.out / "fake-task.base.json").unlink()  # a commit made before digests were recorded
    monkeypatch.setattr(wave, "SCRATCH", tmp_path / "scratch")
    # stop at the oracle image build
    monkeypatch.setattr(wave, "_sh", lambda argv, timeout=None: _done(int(argv[1] == "build")))
    monkeypatch.setattr(wave, "_adapter", lambda *argv: 0)
    record = wave.admit_one(task, _screen(), args)
    assert record["reason"] == "build_failed"
    base = json.loads((args.out / "fake-task.base.json").read_text())
    assert base == {"task": "fake-task", "pinned": PINNED, "commit": "1" * 40}
    assert (record["repeats"], record["dest"], record["registry"]) == (
        3,
        str(tmp_path / "corpus"),
        "localhost:5000",
    )


def test_admit_ignores_a_stale_result_and_checks_the_adapter_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, args = _admit_setup(tmp_path, base_pinned=PINNED)
    out = args.out
    stale = out / "fake-task.admission.json"
    stale.write_text(json.dumps(_result()))  # an earlier run's admitted result
    monkeypatch.setattr(wave, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(wave, "_sh", lambda argv, timeout=None: _done())
    monkeypatch.setattr(wave, "_repo_digest", lambda image: ORACLE)
    adapter_calls: list[tuple[str, ...]] = []

    def adapter(*argv: str) -> int:
        adapter_calls.append(argv)
        return 2  # a fault: no result written

    monkeypatch.setattr(wave, "_adapter", adapter)
    record = wave.admit_one(task, _screen(), args)
    assert record["admitted"] is False and record["reason"] == "admission_error"
    assert record["adapter_exit"] == 2
    assert (record["source_commit"], record["pinned"]) == (COMMIT, PINNED)
    assert not stale.exists()  # removed before the run, never read
    assert [c[0] for c in adapter_calls] == ["admit"]  # nothing written to the corpus


def test_report_rows_list_every_task_sorted_and_escape_pipes() -> None:
    rows = wave.report_rows(
        [
            {"task": "b", "stage": "screen", "reason": "test_entry_offline", "evidence": "a|b"},
            {"task": "a", "stage": "admit", "admitted": True, "reason": "admitted"},
        ]
    )
    assert rows[0].startswith("| `a` |") and "| admitted | `admitted` |" in rows[0]
    assert rows[1].startswith("| `b` |") and "a\\|b" in rows[1] and "| failed |" in rows[1]


def test_admit_does_not_build_a_task_whose_runner_cannot_be_baked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, args = _admit_setup(tmp_path, base_pinned=PINNED)
    assert task.source_dir is not None
    (task.source_dir / "tests" / "test.sh").write_text("uv venv -p 3.12 .tb\nuv run pytest\n")
    calls: list[list[str]] = []

    def sh(argv: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _done()

    monkeypatch.setattr(wave, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(wave, "_sh", sh)
    monkeypatch.setattr(wave, "_adapter", lambda *argv: 0)
    record = wave.admit_one(task, _screen(), args)
    assert (record["stage"], record["reason"]) == ("build", "test_deps_unsupported")
    assert record["admitted"] is False
    assert not any(argv[:2] == ["docker", "build"] for argv in calls)


def test_admit_result_of_another_oracle_layout_is_not_reused() -> None:
    screen = _screen()
    record = {"task": "fake-task", "source_commit": COMMIT, "pinned": PINNED}
    assert not wave.admit_current(record, screen, COMMIT)  # pre-v3: no screen_schema
    assert wave.admit_current({**record, "screen_schema": wave.SCREEN_SCHEMA}, screen, COMMIT)
