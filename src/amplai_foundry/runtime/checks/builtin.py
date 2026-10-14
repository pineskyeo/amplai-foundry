"""The built-in checks' declarations (Work 034 OD-18; spec M-16, M-17, M-19).

The first list of M-16 that this slice builds: the module boundary lint, kind conformance, the
removal test (M-13), the secret exposure scan, the check policy (M-19), and the tools CI already
runs (ruff, ruff format, mypy, pytest) and the evaluator Q-suite, wrapped without changing them.
Not here yet: schema, dependency cycle and version conflict checks (they need pack dependency
data, M-14), ``doc_freshness`` (S2b), performance regression, ``scripts/site_env_check.sh`` (not on
main), and a permissions check.

``checks.lock.json`` at the repository root is the committed form of this list
(``amplai checks lock``). The check policy check fails a change that loosens a check against the
main-branch lock unless an approval record covers it (M-19).
"""

from __future__ import annotations

from .interface import CheckSpec

VERSION = "1"  # the built-in module version (``modules/builtin.py`` BUILTIN_VERSION)
CI = frozenset({"ci"})

# Base protection checks (M-19): module isolation, secrets, and the policy that guards the list.
# The runner refuses to run without them and never skips them. Changing this set is a core change.
BASE_PROTECTED: frozenset[str] = frozenset(
    {"check.module_boundary", "check.secret_exposure", "check.policy"}
)

# M-13 core test subset: the tests of the skeleton M-10 keeps in the core, plus the composition
# root and the execution loop through it. A test of a module's own feature is not here.
CORE_TESTS: tuple[str, ...] = (
    # storage and events
    "tests/runtime_storage/test_store_migration.py",
    "tests/v3/test_rc01_storage_uow.py",
    "tests/v3/test_034_s1f_event_cursor.py",
    # permissions and approval
    "tests/v3/test_rc06_operator_authority.py",
    "tests/v3/test_034_s1a_approve_digest.py",
    "tests/v3/test_034_s1a_intake_authority.py",
    # schedule (leases) and budget
    "tests/v3/test_rc02_runtime.py",
    "tests/v3/test_rc02_lease.py",
    # verification verdicts
    "tests/v3/test_rc06_patch_verifier.py",
    # module registry and loader
    "tests/v3/test_034_s0_modules.py",
    # the composition root, its configuration, and the loop and intake through it
    "tests/v3/test_rc06_local_product.py",
    "tests/v3/test_rc07_local_config.py",
    "tests/e2e/test_rc06_execution_e2e.py",
    "tests/v3/test_034_s1a_intake_route.py",
)

# Tracked files that carry a secret pattern on purpose: the tests of the scanners themselves.
# ``<path>::<pattern id>`` (``runtime/evidence/cas.py`` scan_secrets ids). Adding one loosens.
SECRET_ALLOW: tuple[str, ...] = (
    "tests/e2e/test_033_s9_strategies.py::secret-pattern-2",
    "tests/v3/test_033_answer_lookup.py::secret-pattern-2",
    "tests/v3/test_033_s13_aux_capture.py::secret-pattern-2",
    "tests/v3/test_033_s13_sanitizer.py::secret-pattern-1",
    "tests/v3/test_033_s13_sanitizer.py::secret-pattern-2",
    "tests/v3/test_033_s13_traces_acl.py::secret-pattern-1",
    "tests/v3/test_033_s13_traces_acl.py::secret-pattern-2",
    "tests/v3/test_033_s13_traces_acl.py::secret-pattern-3",
    "tests/v3/test_033_s4b_interpretation.py::secret-pattern-2",
    "tests/v3/test_033_s8_executor.py::secret-pattern-1",
)

_EVERYTHING = ("core", "kind:*")


def _tool(name: str, description: str) -> CheckSpec:
    return CheckSpec(f"check.tool_{name}", VERSION, _EVERYTHING, CI, True, False, description)


BUILTIN_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "check.module_boundary", VERSION, _EVERYTHING, CI, required=True, protected=True,
        description="modules import each other only through their kind's interface (M-12)",
        config={"composition_roots": ("amplai_foundry.runtime.local_deployment",)},
        loosens_when_added=frozenset({"composition_roots"}),
    ),
    CheckSpec(
        "check.kind_conformance", VERSION, ("kind:*",), frozenset({"ci", "enable"}),
        required=True, protected=False,
        description="every registered module passes its kind's conformance suite (M-2, M-13)",
    ),
    CheckSpec(
        "check.module_removal", VERSION, ("kind:git_host", "kind:notifier"), CI,
        required=True, protected=False,
        description="the core tests pass with each optional module off and unimportable (M-13)",
        config={"modules": ("git_host.github", "notifier.slack_webhook"),
                "core_tests": CORE_TESTS},
    ),
    CheckSpec(
        "check.secret_exposure", VERSION, _EVERYTHING, CI, required=True, protected=True,
        description="no tracked file carries a credential pattern (runtime/evidence/cas.py)",
        config={"allow": SECRET_ALLOW}, loosens_when_added=frozenset({"allow"}),
    ),
    CheckSpec(
        "check.policy", VERSION, ("core",), CI, required=True, protected=True,
        description="checks.lock.json matches the registry; a loosening has an approval (M-19)",
    ),
    _tool("ruff", "ruff check . (the CI lint)"),
    _tool("ruff_format", "ruff format --check . (the CI format check)"),
    _tool("mypy", "mypy src (the CI type check)"),
    _tool("pytest", "pytest -n auto --dist worksteal (the CI test run)"),
    CheckSpec(
        "check.evaluator_qsuite", VERSION, ("core",), frozenset({"upgrade"}),
        required=True, protected=False,
        description="the evaluator Q-suite (evaluation/quality.py QUALIFICATION_FILES, OD-7)",
    ),
)  # fmt: skip
