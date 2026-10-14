"""The existing tools as checks (Work 034 M-16): the CI commands, unchanged.

Each wraps one step of ``.github/workflows/ci.yml`` with the same arguments, run by the current
interpreter from the repository root: ``ruff check .``, ``ruff format --check .``, ``mypy src``,
``pytest -n auto --dist worksteal``. The evaluator Q-suite runs the files and runner of
``evaluation/quality.py`` (``QUALIFICATION_FILES``, ``run_qualification_suite``) at ``upgrade``
(OD-7). A non-zero exit is a fail with the output's tail; the tool's version is recorded (M-20).
"""

from __future__ import annotations

import subprocess
import sys
from importlib import metadata

from .interface import BuiltinCheck, CheckContext, CheckResult, Finding, result

TIMEOUT_SECONDS = 3600
TAIL = 4000


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


class ToolCheck(BuiltinCheck):
    distribution = ""
    arguments: tuple[str, ...] = ()

    def run(self, context: CheckContext) -> CheckResult:
        command = [sys.executable, "-m", *self.arguments]
        done = subprocess.run(command, cwd=context.root, capture_output=True, text=True,
                              timeout=TIMEOUT_SECONDS, check=False)  # fmt: skip
        findings = []
        if done.returncode != 0:
            findings.append(Finding(
                "TOOL_FAILED", f"{' '.join(self.arguments)} exited {done.returncode}",
                details={"exit_code": done.returncode, "stdout_tail": done.stdout[-TAIL:],
                         "stderr_tail": done.stderr[-TAIL:]},
            ))  # fmt: skip
        last = (done.stdout.strip().splitlines() or [""])[-1]
        return result(findings, command=self.arguments, tool_version=_version(self.distribution),
                      summary=last)  # fmt: skip


class RuffCheck(ToolCheck):
    distribution = "ruff"
    arguments = ("ruff", "check", ".")


class RuffFormatCheck(ToolCheck):
    distribution = "ruff"
    arguments = ("ruff", "format", "--check", ".")


class MypyCheck(ToolCheck):
    distribution = "mypy"
    arguments = ("mypy", "src")


class PytestCheck(ToolCheck):
    distribution = "pytest"
    arguments = ("pytest", "-n", "auto", "--dist", "worksteal")


class EvaluatorQSuiteCheck(BuiltinCheck):
    def run(self, context: CheckContext) -> CheckResult:
        from ...evaluation.quality import (
            QUALIFICATION_FILES,
            parse_junit,
            run_qualification_suite,
        )

        junit = context.workdir / "q-suite.xml"
        code = run_qualification_suite(QUALIFICATION_FILES, junit, root=context.root)
        counts = parse_junit(junit, QUALIFICATION_FILES) if junit.is_file() else None
        findings = []
        if code != 0:
            findings.append(Finding("QSUITE_FAILED", f"the Q-suite exited {code}",
                                    details={"exit_code": code, "counts": counts}))  # fmt: skip
        return result(findings, files=list(QUALIFICATION_FILES), counts=counts)
