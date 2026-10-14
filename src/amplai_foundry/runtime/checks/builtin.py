"""The built-in checks' declarations (Work 034 OD-18; spec M-16, M-17, M-19).

The first list of M-16 that this slice builds: the module boundary lint, kind conformance, the
removal test (M-13), the secret exposure scan, the check policy (M-19), and the tools CI already
runs (ruff, ruff format, mypy, pytest) and the evaluator Q-suite, wrapped without changing them.
Not here yet: schema, dependency cycle and version conflict checks (they need pack dependency
data, M-14), ``doc_freshness`` (S2b), performance regression, ``scripts/site_env_check.sh`` (not on
main), and a permissions check.

``checks.lock.json`` at the repository root is the committed form of this list and of
:data:`CI_EXTERNAL` (``amplai checks lock``). The check policy check fails a change that loosens a
check against the main-branch lock unless an approval record covers it (M-19).
"""

from __future__ import annotations

from .interface import CheckSpec

VERSION = "1"  # the built-in module version (``modules/builtin.py`` BUILTIN_VERSION)
CI = frozenset({"ci"})
# A base protection check runs at every moment it applies to. No caller runs the enable,
# pack_install or upgrade moments yet (M-17); at those moments these checks need a source tree
# under git as their root, like CI.
EVERY_MOMENT = frozenset({"ci", "enable", "pack_install", "upgrade"})

# Base protection checks (M-19): module isolation, secrets, and the policy that guards the list.
# The runner refuses to run without them, runs them whatever the targets, and never skips them.
# Changing this set is a core change.
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

# Single occurrences of a credential pattern that test fixtures carry on purpose: fake credentials
# fed to the code that must drop, quarantine or refuse them (trace sanitizer and ACL, aux capture,
# executor turn capture, answer lookup, interpretation, strategies). Form
# ``<path>::<pattern id>::<line hash>`` (``secret_exposure.py``); any other occurrence is a
# finding. Adding one loosens (M-19).
SECRET_ALLOW: tuple[str, ...] = (
    "tests/e2e/test_033_s9_strategies.py::secret-pattern-2::e097165ac6a1b510",
    "tests/v3/test_033_answer_lookup.py::secret-pattern-2::e8fc1396f593878c",
    "tests/v3/test_033_s13_aux_capture.py::secret-pattern-2::7e00226435e3c5a9",
    "tests/v3/test_033_s13_sanitizer.py::secret-pattern-1::b1f37570ec536443",
    "tests/v3/test_033_s13_sanitizer.py::secret-pattern-1::1810b50905100c9d",
    "tests/v3/test_033_s13_sanitizer.py::secret-pattern-2::7e00226435e3c5a9",
    "tests/v3/test_033_s13_sanitizer.py::secret-pattern-2::00c67f3a5f917fea",
    "tests/v3/test_033_s13_traces_acl.py::secret-pattern-1::48c67971e8b86100",
    "tests/v3/test_033_s13_traces_acl.py::secret-pattern-1::c9bfc8f8b9223d1d",
    "tests/v3/test_033_s13_traces_acl.py::secret-pattern-2::7e00226435e3c5a9",
    "tests/v3/test_033_s13_traces_acl.py::secret-pattern-2::8294745a1b39426d",
    "tests/v3/test_033_s13_traces_acl.py::secret-pattern-2::28c603715568eae0",
    "tests/v3/test_033_s13_traces_acl.py::secret-pattern-3::1a54d5ff2564b145",
    "tests/v3/test_033_s4b_interpretation.py::secret-pattern-2::e097165ac6a1b510",
    "tests/v3/test_033_s8_executor.py::secret-pattern-1::aa6b74ac35548bce",
)

# The checks another CI step runs (``.github/workflows/ci.yml`` runs pytest, ruff and mypy as their
# own steps). Only these may be skipped (``--skip``); the set is in the lock and adding one loosens
# (M-19).
CI_EXTERNAL: tuple[str, ...] = (
    "check.tool_mypy", "check.tool_pytest", "check.tool_ruff", "check.tool_ruff_format",
)  # fmt: skip

_EVERYTHING = ("core", "kind:*")


def _tool(name: str, description: str) -> CheckSpec:
    return CheckSpec(f"check.tool_{name}", VERSION, _EVERYTHING, CI, True, False, description)


BUILTIN_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "check.module_boundary", VERSION, _EVERYTHING, EVERY_MOMENT, required=True,
        protected=True,
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
        "check.module_removal", VERSION,
        ("kind:git_host", "kind:notifier", "kind:service_manager"), CI,
        required=True, protected=False,
        description="the core tests pass with each optional module off and unimportable (M-13)",
        config={"modules": ("git_host.github", "notifier.slack_webhook",
                            "service_manager.launchd", "service_manager.systemd"),
                "core_tests": CORE_TESTS},
    ),
    CheckSpec(
        "check.secret_exposure", VERSION, _EVERYTHING, EVERY_MOMENT, required=True,
        protected=True,
        description="no tracked file carries a credential pattern (evidence/cas.py and more)",
        config={"allow": SECRET_ALLOW}, loosens_when_added=frozenset({"allow"}),
    ),
    CheckSpec(
        "check.policy", VERSION, ("core",), EVERY_MOMENT, required=True, protected=True,
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
