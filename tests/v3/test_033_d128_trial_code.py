"""D-128 review P1-1: the trial code digest of the calibration reuse key.

``local_executor.trial_code_digest`` hashes every file of the installed package except an
explicit allowlist (``TRIAL_CODE_EXCLUDED``). The allowlist may only name modules a trial cannot
reach: this test walks the static import closure (every ``import`` and ``from ... import``, inside
functions too) of the trial roots and checks each allowlisted module is outside it, except the
evaluator's own files, which the evaluator version binds instead.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from amplai_foundry.evaluation import versions
from amplai_foundry.meta_harness import local_executor
from amplai_foundry.meta_harness.local_executor import (
    TRIAL_CODE_EXCLUDED,
    trial_code_digest,
    trial_code_excluded,
)

PACKAGE = Path(local_executor.__file__).resolve().parents[1]
PKG = "amplai_foundry"
# the code a trial runs: the executor, every driver module, the execution entry points
ROOTS = (
    "meta_harness.local_executor",
    "runtime.execution.loop",
    "runtime.execution.worker",
    "runtime.execution.product",
    "runtime.execution.service",
    "runtime.execution.codex",
)
# bound by the evaluator version (evaluator_version_ref, trial_compatible_with), not by the digest
EVALUATOR_FILES = {
    "evaluation/analysis.py",
    "evaluation/sequential.py",
    "evaluation/versions.py",
    "evaluation/calibration.py",
    "evaluation/service.py",
}


def module_name(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


MODULES = {module_name(p): p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts}


def imported(path: Path) -> set[str]:
    name = module_name(path)
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith(PKG)}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                target = ".".join([*base, *([node.module] if node.module else [])])
            else:
                target = node.module or ""
            if target.startswith(PKG):
                found.add(target)
                found |= {target + "." + a.name for a in node.names}
    out = set()
    for full in found:  # every package on the way is imported too
        parts = full.split(".")
        out |= {".".join(parts[:i]) for i in range(1, len(parts) + 1)} & set(MODULES)
    return out


def closure() -> set[str]:
    roots = [f"{PKG}.{r}" for r in ROOTS]
    roots += [m for m in MODULES if m.startswith(f"{PKG}.agent_drivers")]
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen or name not in MODULES:
            continue
        seen.add(name)
        stack.extend(imported(MODULES[name]) - seen)
    return seen


def relpath(name: str) -> str:
    return MODULES[name].relative_to(PACKAGE).as_posix()


def test_every_allowlisted_module_is_outside_the_trial_import_closure() -> None:
    reachable = {relpath(m) for m in closure()}
    assert "meta_harness/local_executor.py" in reachable and "agent_drivers/cli.py" in reachable
    excluded = sorted(
        p.relative_to(PACKAGE).as_posix()
        for p in PACKAGE.rglob("*.py")
        if "__pycache__" not in p.parts and trial_code_excluded(p.relative_to(PACKAGE).as_posix())
    )
    assert excluded, "the allowlist names existing files"
    leaked = [p for p in excluded if p in reachable and p not in EVALUATOR_FILES]
    assert leaked == [], f"allowlisted but reachable from a trial: {leaked}"
    # each allowlist entry names at least one file
    for pattern in TRIAL_CODE_EXCLUDED:
        assert any(trial_code_excluded(p) for p in excluded if p.startswith(pattern.split("*")[0]))


def test_the_evaluator_files_are_bound_by_the_evaluator_version_digest() -> None:
    digested = {f"evaluation/{n}" for n in (*versions.ANALYSIS_FILES, *versions.SERVICE_FILES)}
    assert digested <= EVALUATOR_FILES
    assert all(trial_code_excluded(p) for p in EVALUATOR_FILES)


def test_reachable_modules_that_might_not_matter_stay_in_the_digest() -> None:
    # safe by default: these are left in (a change to them makes earlier trials non-reusable)
    for kept in ("meta_harness/deciders.py", "runtime/execution/meta_ops.py",
                 "evaluation/pricing.py", "evaluation/observatory.py", "evaluation/telemetry.py",
                 "meta_harness/archive.py", "meta_harness/proposer.py", "meta_harness/stages.py",
                 "runtime/meta_commands/__init__.py", "runtime/local_deployment.py",
                 "agent_drivers/cli.py", "runtime/execution/codex.py"):  # fmt: skip
        assert (PACKAGE / kept).is_file() and not trial_code_excluded(kept), kept


@pytest.fixture
def package_copy(tmp_path: Path) -> Path:
    root = tmp_path / "amplai_foundry"
    shutil.copytree(PACKAGE, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def test_editing_a_trial_file_changes_the_digest_and_an_allowlisted_one_does_not(
    package_copy: Path,
) -> None:
    before = trial_code_digest(package_copy)
    assert before.startswith("sha256:") and len(before) == 71
    # an allowlisted file (and a compiled cache) leaves the digest alone
    for name in ("meta_harness/dashboard.py", "runtime/cli.py", "evaluation/calibration.py"):
        with (package_copy / name).open("a") as out:
            out.write("\n# edited\n")
    cache = package_copy / "agent_drivers" / "__pycache__"
    cache.mkdir()
    (cache / "cli.cpython-311.pyc").write_bytes(b"\0")
    local_executor._TRIAL_CODE.pop(package_copy, None)
    assert trial_code_digest(package_copy) == before
    # a file a trial runs, or a data file, changes it
    for name in ("agent_drivers/cli.py", "runtime/meta_commands/__init__.py"):
        local_executor._TRIAL_CODE.pop(package_copy, None)
        with (package_copy / name).open("a") as out:
            out.write("\n# edited\n")
        after = trial_code_digest(package_copy)
        assert after != before
        before = after
    data = next(p for p in package_copy.rglob("*.json") if not trial_code_excluded(
        p.relative_to(package_copy).as_posix()))  # fmt: skip
    local_executor._TRIAL_CODE.pop(package_copy, None)
    data.write_text(data.read_text() + "\n")
    assert trial_code_digest(package_copy) != before


def test_the_installed_digest_is_read_once_per_process() -> None:
    assert trial_code_digest() == trial_code_digest()
