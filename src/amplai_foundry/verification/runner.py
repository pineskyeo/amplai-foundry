"""Run every Phase 0 completion check with one command."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VerificationStep:
    name: str
    command: tuple[str, ...]
    exit_code: int
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class VerificationReport:
    steps: tuple[VerificationStep, ...]

    @property
    def passed(self) -> bool:
        return all(step.passed for step in self.steps)


class VerificationRunner:
    def commands(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        python = sys.executable
        return (
            ("pytest", (python, "-m", "pytest")),
            ("ruff-check", (python, "-m", "ruff", "check", ".")),
            ("ruff-format", (python, "-m", "ruff", "format", "--check", ".")),
            ("mypy", (python, "-m", "mypy", "src")),
            (
                "schema",
                (python, "-m", "amplai_foundry.cli", "schema", "check"),
            ),
            (
                "vault-lint",
                (python, "-m", "amplai_foundry.cli", "lint", "vault/"),
            ),
            (
                "project-pack",
                (
                    python,
                    "-m",
                    "amplai_foundry.cli",
                    "project",
                    "validate",
                    "--workspace",
                    ".",
                ),
            ),
        )

    def run(self, root: Path) -> VerificationReport:
        environment = os.environ.copy()
        source_path = str((root / "src").resolve())
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path if not existing else f"{source_path}{os.pathsep}{existing}"
        )
        steps: list[VerificationStep] = []
        for name, command in self.commands():
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            steps.append(
                VerificationStep(
                    name=name,
                    command=command,
                    exit_code=completed.returncode,
                    output=completed.stdout,
                )
            )
            if completed.returncode != 0:
                break
        return VerificationReport(tuple(steps))
