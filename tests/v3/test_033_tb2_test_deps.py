"""Work 033 TB2 option A: ``tb2_test_deps`` (the test runner baked into the oracle image at build
time so that grading stays egress none, D-136) and its use in ``scripts/tb2_oracle_wave.py``.
The shim is run with ``sh`` against a fake venv; no docker (the image build is operator-run,
``runs/tb2-oracle-wave1.md``)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness import tb2_test_deps as deps_mod
from amplai_foundry.meta_harness.tb2 import Tb2Task
from amplai_foundry.meta_harness.tb2_test_deps import RunnerDeps, Unsupported

REPO = Path(__file__).resolve().parents[2]
PINNED = "alexgshaw/fake-task@sha256:" + "c" * 64

UVX_SCRIPT = """#!/bin/bash

# Install curl
apt-get update
apt-get install -y curl

# Install uv
curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh

source $HOME/.local/bin/env

uvx \\
  -p 3.13 \\
  -w pytest==8.4.1 \\
  -w numpy==2.3.1 \\
  -w pytest-json-ctrf==0.3.5 \\
  pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
fi
"""

PIP_SCRIPT = """#!/bin/bash

# Install test dependencies
pip install pytest==8.4.1 psutil==7.0.0 pytest-json-ctrf==0.3.5 --break-system-packages

pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
"""


def _wave() -> Any:
    path = REPO / "scripts" / "tb2_oracle_wave.py"
    spec = importlib.util.spec_from_file_location("tb2_oracle_wave_deps", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wave = _wave()


# -- parse ---------------------------------------------------------------------------------------


def test_uvx_runner_is_parsed_across_line_continuations() -> None:
    deps = deps_mod.parse_test_entry(UVX_SCRIPT)
    assert deps == RunnerDeps(
        "uvx", ("pytest==8.4.1", "numpy==2.3.1", "pytest-json-ctrf==0.3.5"), "3.13"
    )


def test_uvx_extra_index_is_kept() -> None:
    script = (
        "uvx --index https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match "
        "-p 3.13 -w torch==2.7.1 -w pytest==8.4.1 pytest /tests/test_outputs.py\n"
    )
    deps = deps_mod.parse_test_entry(script)
    assert deps.index == "https://download.pytorch.org/whl/cpu"
    assert deps.index_strategy == "unsafe-best-match"


def test_pip_runner_drops_break_system_packages() -> None:
    deps = deps_mod.parse_test_entry(PIP_SCRIPT)
    assert deps == RunnerDeps("pip", ("pytest==8.4.1", "psutil==7.0.0", "pytest-json-ctrf==0.3.5"))


@pytest.mark.parametrize(
    "script",
    [
        "uvx -p 3.13 -w pytest==8.4.1 -w git+https://github.com/x/y.git@${COMMIT} pytest t.py\n",
        "uvx -p 3.13 -w pytest pytest t.py\n",  # unpinned
        "uvx -p 3.13 -w pytest==8.4.1 python t.py\n",  # not pytest
        "uvx -w pytest==8.4.1 pytest t.py\n",  # no python
        "uvx -p 3.13 --from foo -w pytest==8.4.1 pytest t.py\n",  # unknown option
        "uv venv -p 3.12 .tb\nuv pip install pytest==8.4.1\nuv run pytest t.py\n",
        "pip install pytest==8.4.1\nuvx -p 3.13 -w pytest==8.4.1 pytest t.py\n",  # two runners
        "pip install -r requirements.txt\n",
        "pip install requests==2.32.4\npytest t.py\n",  # pip without pytest
        "echo no runner\n",
    ],
)
def test_runners_that_cannot_be_pinned_are_unsupported(script: str) -> None:
    with pytest.raises(Unsupported):
        deps_mod.parse_test_entry(script)


def test_commented_runner_lines_are_ignored() -> None:
    script = "# uvx -p 3.12 -w pytest==1 pytest x\n" + UVX_SCRIPT
    assert deps_mod.parse_test_entry(script).python == "3.13"


# -- Dockerfile lines ----------------------------------------------------------------------------


def test_bake_lines_pin_uv_by_digest_and_put_the_shim_first_on_path() -> None:
    lines = deps_mod.bake_dockerfile_lines(deps_mod.parse_test_entry(UVX_SCRIPT), "linux/amd64")
    text = "\n".join(lines)
    assert f"COPY --from={deps_mod.UV_IMAGES['linux/amd64']} " in text
    assert all("@sha256:" in image for image in deps_mod.UV_IMAGES.values())
    assert f"ENV PATH={deps_mod.SHIM}:" in text
    assert "--managed-python --python 3.13" in text
    assert "pytest==8.4.1 numpy==2.3.1 pytest-json-ctrf==0.3.5" in text
    assert "rm -rf /tmp/amplai-uv-cache" in text and "chmod -R a+rX" in text


def test_bake_lines_for_pip_install_into_the_image_interpreter() -> None:
    deps = deps_mod.parse_test_entry(PIP_SCRIPT)
    text = "\n".join(deps_mod.bake_dockerfile_lines(deps, "linux/arm64"))
    assert "uv pip install --system --break-system-packages" in text
    assert "command -v python3 || command -v python" in text  # `python -m pytest` sees it too
    assert "ENV PATH" not in text and deps_mod.shim_files(deps) == {}


def test_python_m_pytest_entry_is_a_pip_runner() -> None:
    script = (
        "pip install pytest==8.4.1 pytest-json-ctrf==0.3.5 --break-system-packages\n"
        "python -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\n"
    )
    assert deps_mod.parse_test_entry(script).kind == "pip"


def test_heredoc_bodies_are_not_tokenized() -> None:
    script = "cat > /tmp/x.py <<'EOF'\nprint(\"it's fine\")\nEOF\n" + UVX_SCRIPT
    assert deps_mod.parse_test_entry(script).kind == "uvx"


def test_an_untokenizable_runner_line_is_unsupported_not_a_crash() -> None:
    with pytest.raises(Unsupported):
        deps_mod.parse_test_entry("uvx -p 3.13 -w 'pytest==8.4.1 pytest t.py\n")


def test_oracle_dockerfile_bakes_as_root_before_dropping_to_65534() -> None:
    deps = deps_mod.parse_test_entry(UVX_SCRIPT)
    task = Tb2Task("fake-task", "alexgshaw/fake-task:20251031", True, 0, "games", "hard", 9, 9)
    lines = wave.oracle_dockerfile(task, PINNED, "/app", deps).splitlines()
    root, bake, user = (
        lines.index("USER 0:0"),
        next(i for i, line in enumerate(lines) if "uv pip install" in line),
        lines.index("USER 65534:65534"),
    )
    assert root < bake < user
    assert lines[2] == f"FROM {PINNED}"


def test_oracle_build_context_reads_the_unmodified_test_sh(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test.sh").write_text(UVX_SCRIPT)
    task = Tb2Task(
        "fake-task", "alexgshaw/fake-task:20251031", True, 0, "games", "hard", 9, 9, tmp_path
    )
    files = wave.oracle_build_context(task, PINNED, "/app", "linux/amd64")
    assert set(files) == {"Dockerfile", f"{deps_mod.SHIM_CONTEXT}/uvx"}
    wave.write_build_context(tmp_path / "build", files)
    assert (tmp_path / "build" / deps_mod.SHIM_CONTEXT / "uvx").is_file()
    assert (tmp_path / "tests" / "test.sh").read_text() == UVX_SCRIPT


def test_oracle_build_context_fails_closed_for_an_unsupported_runner(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test.sh").write_text("uv venv -p 3.12 .tb\n")
    task = Tb2Task("t", "alexgshaw/t:1", True, 0, "games", "hard", 9, 9, tmp_path)
    with pytest.raises(Unsupported):
        wave.oracle_build_context(task, PINNED, "/app", "linux/amd64")


def test_screen_schema_changed_with_the_baked_layout() -> None:
    assert wave.SCREEN_SCHEMA == "amplai.tb2-oracle-screen.v3"


# -- the uvx shim (run with sh against a fake venv) ----------------------------------------------


def _shim(tmp_path: Path, deps: RunnerDeps) -> Path:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    fake = venv / "bin" / "pytest"
    fake.write_text('#!/bin/sh\necho "baked pytest $*"\n')
    fake.chmod(0o755)
    text = deps_mod.shim_files(deps)[f"{deps_mod.SHIM_CONTEXT}/uvx"]
    shim = tmp_path / "uvx"
    shim.write_text(text.replace(deps_mod.VENV, str(venv)))
    shim.chmod(0o755)
    return shim


def _run(shim: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run([str(shim), *args], capture_output=True, text=True, env=env)


def test_uvx_shim_runs_the_baked_pytest_for_the_exact_request(tmp_path: Path) -> None:
    shim = _shim(tmp_path, deps_mod.parse_test_entry(UVX_SCRIPT))
    # -w order differs from the bake: the set is compared
    done = _run(
        shim, "-p", "3.13", "-w", "pytest-json-ctrf==0.3.5", "-w", "pytest==8.4.1",
        "-w", "numpy==2.3.1", "pytest", "--ctrf", "/logs/verifier/ctrf.json", "-rA",
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "baked pytest --ctrf /logs/verifier/ctrf.json -rA"


@pytest.mark.parametrize(
    "args",
    [
        (
            "-p",
            "3.12",
            "-w",
            "pytest==8.4.1",
            "-w",
            "numpy==2.3.1",
            "-w",
            "pytest-json-ctrf==0.3.5",
        ),
        ("-p", "3.13", "-w", "pytest==8.4.1", "-w", "pytest-json-ctrf==0.3.5"),
        (
            "-p",
            "3.13",
            "-w",
            "pytest==8.4.1",
            "-w",
            "numpy==2.3.2",
            "-w",
            "pytest-json-ctrf==0.3.5",
        ),
        ("--from", "x", "-p", "3.13"),
        # one -w value carrying two requirements must not match the baked set
        ("-p", "3.13", "-w", "pytest==8.4.1\nnumpy==2.3.1", "-w", "pytest-json-ctrf==0.3.5"),
    ],
)
def test_uvx_shim_refuses_any_other_request(tmp_path: Path, args: tuple[str, ...]) -> None:
    shim = _shim(tmp_path, deps_mod.parse_test_entry(UVX_SCRIPT))
    done = _run(shim, *args, "pytest", "t.py")
    assert done.returncode == 127
    assert "amplai-tb2" in done.stderr and "baked pytest" not in done.stdout


def test_bake_refuses_a_platform_without_a_pinned_uv() -> None:
    with pytest.raises(Unsupported):
        deps_mod.bake_dockerfile_lines(deps_mod.parse_test_entry(UVX_SCRIPT), "linux/s390x")
