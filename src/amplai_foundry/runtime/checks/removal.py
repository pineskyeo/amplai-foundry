"""``check.module_removal``: the removal test (Work 034 M-13, M-16).

Each optional module in config ``modules`` (``git_host.github``, ``notifier.slack_webhook``):
the check runs the core test subset (config ``core_tests``, :data:`.builtin.CORE_TESTS`) in a
pytest subprocess where the module is gone: its files (:data:`BUILTIN_CODE` implementation and
conformance subject) are unimportable and its ``local.json`` key (``config_key``) defaults to
``null`` (``removal_plugin.py``). A failing or erroring core test means another feature depends on
the module: ``MODULE_REMOVAL_BROKE_CORE`` with the failed test ids (M-13 "격리 위반").

The core test subset is the M-10 skeleton (storage and events, permissions and approval, schedule
and budget, verification verdicts, module registry and loader) plus the composition root and the
loop through it; a module's own feature tests are not in it (they need the module).
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from ..modules.builtin import BUILTIN_CODE, BUILTIN_MODULES
from .interface import BuiltinCheck, CheckContext, CheckResult, Finding, covers, result
from .removal_plugin import ENV_FILES, ENV_MODULE, ENV_OFF_KEY

PLUGIN = "amplai_foundry.runtime.checks.removal_plugin"
TIMEOUT_SECONDS = 1800
MAX_FAILED = 20


def failed_cases(junit: Path) -> list[str]:
    """``classname::name`` of every failed or erroring case of a pytest JUnit file."""
    root = ET.parse(junit).getroot()  # the file this check's own pytest run just wrote
    out = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            out.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return out


def pytest_command(junit: Path, tests: tuple[str, ...], workers: str | None) -> list[str]:
    xdist = workers is not None and importlib.util.find_spec("xdist") is not None
    parallel = ["-n", workers] if xdist and workers is not None else []
    return [
        sys.executable, "-m", "pytest", "-p", PLUGIN, "-p", "no:cacheprovider",
        "-o", "addopts=", "-m", "not visual and not container", "-q",
        f"--junitxml={junit}", *parallel, *tests,
    ]  # fmt: skip


def clean_env() -> dict[str, str]:
    """This process's environment without ``PYTEST_*`` (``PYTEST_ADDOPTS``, ``PYTEST_PLUGINS``,
    ``PYTEST_DISABLE_PLUGIN_AUTOLOAD``, an outer xdist worker's variables): they would change
    which tests the removal run selects or how it loads."""
    return {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}


def run_group(
    command: list[str], cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str] | None:
    """Run ``command`` in its own process group; on timeout kill the whole group (pytest-xdist
    workers included) and return ``None``."""
    pipe = subprocess.PIPE
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=pipe, stderr=pipe, text=True,
                               start_new_session=True)  # fmt: skip
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class ModuleRemovalCheck(BuiltinCheck):
    workers: str | None = "auto"  # pytest-xdist workers of each run (None: one process)
    timeout: float = TIMEOUT_SECONDS

    def run(self, context: CheckContext) -> CheckResult:
        tests = tuple(self.check.config.get("core_tests", ()))
        kinds = {s.module_id: s.kind for s in BUILTIN_MODULES}
        findings: list[Finding] = []
        missing = [t for t in tests if not (context.root / t).is_file()]
        if missing:
            findings.append(Finding("REMOVAL_TEST_MISSING", "a core test file is missing",
                                    details={"missing": missing}))  # fmt: skip
        tests = tuple(t for t in tests if t not in missing)
        if not tests:  # pytest without paths would run every test of the checkout
            findings.append(Finding("REMOVAL_NO_CORE_TESTS", "there is no core test to run"))
            return result(findings, core_tests=[], runs={})
        wanted = tuple(t for t in context.targets if t.startswith("kind:"))
        ran: dict[str, dict[str, object]] = {}
        for module_id in self.check.config.get("modules", ()):
            kind = kinds.get(module_id)
            code = BUILTIN_CODE.get(module_id)
            if kind is None or code is None or code.config_key is None:
                findings.append(Finding("REMOVAL_NOT_OPTIONAL",
                                        f"{module_id} is not a registered module with an off key",
                                        details={"module": module_id}))  # fmt: skip
                continue
            if wanted and not any(t == "kind:*" or covers((f"kind:{kind}",), t) for t in wanted):
                continue
            junit = context.workdir / f"{module_id}.xml"
            env = {**clean_env(), ENV_MODULE: module_id, ENV_FILES: ",".join(code.files()),
                   ENV_OFF_KEY: code.config_key}  # fmt: skip
            command = pytest_command(junit, tests, self.workers)
            done = run_group(command, context.root, env, self.timeout)
            if done is None:
                findings.append(Finding(
                    "MODULE_REMOVAL_TIMEOUT",
                    f"the core tests without {module_id} did not finish in {self.timeout}s; "
                    "the run's process group was killed",
                    details={"module": module_id, "timeout_seconds": self.timeout},
                ))  # fmt: skip
                ran[module_id] = {"exit_code": None, "summary": "timeout"}
                continue
            failed = failed_cases(junit) if junit.is_file() else []
            summary = (done.stdout.strip().splitlines() or [""])[-1]
            ran[module_id] = {
                "exit_code": done.returncode,
                "summary": summary,
                "hidden": list(code.files()),
                "off_key": code.config_key,
            }
            if done.returncode != 0:
                findings.append(Finding(
                    "MODULE_REMOVAL_BROKE_CORE",
                    f"the core tests do not pass without {module_id}",
                    details={"module": module_id, "exit_code": done.returncode,
                             "failed": failed[:MAX_FAILED], "failed_total": len(failed),
                             "summary": summary, "stderr_tail": done.stderr[-2000:]},
                ))  # fmt: skip
        return result(findings, core_tests=list(tests), runs=ran)
