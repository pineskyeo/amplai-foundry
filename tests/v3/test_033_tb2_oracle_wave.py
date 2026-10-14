"""Work 033 OD-15 wave 1: ``scripts/tb2_oracle_wave.py`` pure helpers (screen verdict, oracle
image Dockerfile, admission profile, screen argv, report rows). No docker: the docker steps are
operator-run (``runs/tb2-oracle-wave1.md``)."""

from __future__ import annotations

import importlib.util
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


def test_screen_argv_runs_the_entry_under_containment(tmp_path: Path) -> None:
    profile = wave.container_profile(PINNED, {})
    argv = wave.screen_argv(
        PINNED, "/app", tmp_path / "tests", tmp_path / "verifier", "amplai-x", profile
    )
    assert argv[:2] == ["docker", "run"]
    for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert flag in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "65534:65534"
    assert argv[argv.index("--workdir") + 1] == "/app"
    assert f"type=bind,src={tmp_path / 'tests'},dst=/tests,readonly" in argv
    assert f"type=bind,src={tmp_path / 'verifier'},dst=/logs/verifier" in argv
    assert argv[-3:] == [PINNED, "bash", f"{tb2_grading.TESTS_DIR}/test.sh"]


def test_report_rows_list_every_task_sorted_and_escape_pipes() -> None:
    rows = wave.report_rows(
        [
            {"task": "b", "stage": "screen", "reason": "test_entry_offline", "evidence": "a|b"},
            {"task": "a", "stage": "admit", "admitted": True, "reason": "admitted"},
        ]
    )
    assert rows[0].startswith("| `a` |") and "| admitted | `admitted` |" in rows[0]
    assert rows[1].startswith("| `b` |") and "a\\|b" in rows[1] and "| failed |" in rows[1]
