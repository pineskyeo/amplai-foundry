"""Work 034 S0 (spec M-2/M-3/M-5, AC-M3, AC-M6): the module registry core.

Real: ``ModuleSpec``, ``ConformanceLedger``, ``PinnedManifest``/``verify_extensions`` and
``ModuleRegistry`` over fake distributions written to ``tmp_path`` (dist-info with METADATA and a
RECORD carrying real sha256 file hashes), the way a verbatim wheel extraction lays them out. Nothing
is installed or imported from those roots.
"""

from __future__ import annotations

import base64
import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.contracts.identity import digest_bytes
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.policies import STRATEGIES
from amplai_foundry.runtime.execution.product import ROUTER_ORDER
from amplai_foundry.runtime.modules import (
    BUILTIN_MODULES,
    INTERFACE_VERSIONS,
    KINDS,
    OUT_OF_PROCESS_KINDS,
    ConformanceLedger,
    ConformanceSuite,
    Module,
    ModuleRegistry,
    ModuleSpec,
    PinnedManifest,
    suite_id,
)

PKG = "acme-modules"
DEP = "acme-dep"


def write_dist(
    root: Path,
    name: str,
    version: str,
    files: dict[str, bytes],
    *,
    requires: tuple[str, ...] = (),
    unhashed: tuple[str, ...] = (),
) -> str:
    """Lay out one extracted wheel under ``root``; return the sha256 digest of its RECORD."""
    info = f"{name.replace('-', '_')}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(
        f"Requires-Dist: {r}\n" for r in requires
    )
    wheel = b"Wheel-Version: 1.0\n"
    every = {**files, f"{info}/METADATA": metadata.encode(), f"{info}/WHEEL": wheel}
    lines = []
    for rel, data in every.items():
        target = root / rel
        if ".." not in Path(rel).parts:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if rel in unhashed:
            lines.append(f"{rel},,")
            continue
        b64 = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        lines.append(f"{rel},sha256={b64},{len(data)}")
    lines.append(f"{info}/RECORD,,")
    record = ("\n".join(lines) + "\n").encode()
    (root / info / "RECORD").write_bytes(record)
    return digest_bytes(record)


def module_entry(**overrides: Any) -> dict[str, Any]:
    return {
        "kind": "judge",
        "module_id": "acme.judge",
        "version": "0.1.0",
        "interface_version": INTERFACE_VERSIONS["judge"],
        "conformance_suite": suite_id("judge"),
        "permissions": ["meta.read"],
        "placement": "in_process",
        "package": PKG,
        "entry": "acme_modules.judge:factory",
        **overrides,
    }


@dataclass
class World:
    root: Path
    manifest: dict[str, Any]

    def registry(self, ledger: ConformanceLedger | None = None) -> ModuleRegistry:
        return ModuleRegistry(
            ledger or ConformanceLedger(),
            manifest=PinnedManifest.from_dict(self.manifest),
            extension_roots=(self.root,),
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    root = tmp_path / "ext"
    root.mkdir()
    pkg_digest = write_dist(
        root,
        PKG,
        "0.1.0",
        {
            "acme_modules/__init__.py": b"",
            "acme_modules/judge.py": b"def factory():\n    return None\n",
            "acme_modules/gateway/__init__.py": b"def factory():\n    return None\n",
        },
        requires=(f"{DEP}>=1.0", 'pytest>=8; extra == "test"'),
    )
    dep_digest = write_dist(root, DEP, "1.2.0", {"acme_dep/__init__.py": b"VALUE = 1\n"})
    manifest = {
        "manifest_version": 1,
        "packages": [
            {"name": PKG, "version": "0.1.0", "record_digest": pkg_digest},
            {"name": DEP, "version": "1.2.0", "record_digest": dep_digest},
        ],
        "modules": [module_entry()],
    }
    return World(root, manifest)


def code_of(excinfo: pytest.ExceptionInfo[RuntimeFault]) -> str:
    return excinfo.value.code


# -- specs and the kind table ----------------------------------------------------------------------
def test_kinds_are_the_closed_first_set_and_two_run_out_of_process() -> None:
    assert set(KINDS) == {
        "driver", "strategy", "decider", "judge", "knowledge", "doc_freshness", "intake_adapter",
        "model_gateway",
    }  # fmt: skip
    assert set(INTERFACE_VERSIONS) == set(KINDS)
    assert set(OUT_OF_PROCESS_KINDS) == {"intake_adapter", "model_gateway"}


@pytest.mark.parametrize("kind", sorted(OUT_OF_PROCESS_KINDS))
def test_outside_input_kinds_cannot_declare_in_process(kind: str) -> None:
    with pytest.raises(RuntimeFault) as excinfo:
        ModuleSpec(kind, f"x.{kind}", "1", 1, suite_id(kind), frozenset(), "in_process")
    assert code_of(excinfo) == "MODULE_PLACEMENT"


def test_spec_rejects_unknown_kind_and_wrong_interface_version() -> None:
    with pytest.raises(RuntimeFault) as excinfo:
        ModuleSpec("planner", "x.p", "1", 1, "conformance.planner.v1", frozenset(), "in_process")
    assert code_of(excinfo) == "MODULE_SPEC"
    with pytest.raises(RuntimeFault) as excinfo:
        ModuleSpec("judge", "x.j", "1", 2, suite_id("judge"), frozenset(), "in_process")
    assert code_of(excinfo) == "MODULE_INTERFACE"


# -- built-ins -------------------------------------------------------------------------------------
def test_builtins_come_from_the_static_list() -> None:
    registry = ModuleRegistry(ConformanceLedger())
    assert registry.modules() == BUILTIN_MODULES
    assert [s.module_id for s in registry.modules("driver")] == [
        f"driver.{d}" for d in ROUTER_ORDER
    ]
    assert [s.module_id for s in registry.modules("strategy")] == [
        f"strategy.{s}" for s in STRATEGIES
    ]
    for spec in BUILTIN_MODULES:
        assert spec.placement == "in_process"
        assert spec.interface_version == INTERFACE_VERSIONS[spec.kind]
        assert registry.is_builtin(*spec.key)
        assert registry.extension(*spec.key) is None


def test_extension_roots_without_a_manifest_are_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeFault) as excinfo:
        ModuleRegistry(ConformanceLedger(), extension_roots=(tmp_path,))
    assert code_of(excinfo) == "MODULE_MANIFEST"


# -- pinned extensions (AC-M3) ---------------------------------------------------------------------
def test_extension_with_matching_pinned_record_digest_is_listed(world: World) -> None:
    registry = world.registry()
    spec = registry.get("acme.judge", "0.1.0")
    assert spec.kind == "judge" and spec.permissions == frozenset({"meta.read"})
    assert registry.modules("judge") == (spec,)
    assert not registry.is_builtin(*spec.key)
    pinned = registry.extension(*spec.key)
    assert pinned is not None
    assert (pinned.package, pinned.entry) == (PKG, "acme_modules.judge:factory")


def test_record_digest_mismatch_is_rejected(world: World) -> None:
    world.manifest["packages"][0]["record_digest"] = "sha256:" + "0" * 64
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_RECORD_DIGEST"


def test_installed_file_that_differs_from_record_is_rejected(world: World) -> None:
    (world.root / "acme_modules/judge.py").write_bytes(b"import os\nos.system('x')\n")
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_FILE_MISMATCH"


def test_record_entry_naming_a_missing_file_is_rejected(world: World) -> None:
    # Python 3.12+ ``Distribution.files`` drops such entries; RECORD is parsed directly instead.
    (world.root / "acme_dep/__init__.py").unlink()
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_FILE_MISSING"
    assert excinfo.value.details == {"package": DEP, "path": "acme_dep/__init__.py"}


def test_malformed_record_row_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "ext"
    root.mkdir()
    write_dist(root, DEP, "1.2.0", {"acme_dep/__init__.py": b""})
    record = next(root.glob("*.dist-info")) / "RECORD"
    data = record.read_bytes() + b"acme_dep/extra.py,sha256=x\n"
    record.write_bytes(data)
    manifest = {
        "manifest_version": 1,
        "packages": [{"name": DEP, "version": "1.2.0", "record_digest": digest_bytes(data)}],
        "modules": [],
    }
    with pytest.raises(RuntimeFault) as excinfo:
        World(root, manifest).registry()
    assert code_of(excinfo) == "MODULE_RECORD_FORMAT"


def test_file_not_listed_by_any_record_is_rejected(world: World) -> None:
    (world.root / "acme_modules/extra.py").write_bytes(b"")
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_FILE_UNLISTED"


def test_record_entry_without_hash_is_rejected(tmp_path: Path, world: World) -> None:
    root = tmp_path / "unhashed"
    root.mkdir()
    files = {"acme_dep/__init__.py": b"VALUE = 1\n"}
    digest = write_dist(root, DEP, "1.2.0", files, unhashed=("acme_dep/__init__.py",))
    manifest = {
        "manifest_version": 1,
        "packages": [{"name": DEP, "version": "1.2.0", "record_digest": digest}],
        "modules": [],
    }
    with pytest.raises(RuntimeFault) as excinfo:
        ModuleRegistry(
            ConformanceLedger(),
            manifest=PinnedManifest.from_dict(manifest),
            extension_roots=(root,),
        )
    assert code_of(excinfo) == "MODULE_FILE_HASH"


def test_dependency_outside_the_pinned_closure_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "ext"
    root.mkdir()
    digest = write_dist(
        root,
        PKG,
        "0.1.0",
        {"acme_modules/__init__.py": b"", "acme_modules/judge.py": b""},
        requires=(f"{DEP}>=1.0",),
    )
    manifest = {
        "manifest_version": 1,
        "packages": [{"name": PKG, "version": "0.1.0", "record_digest": digest}],
        "modules": [module_entry()],
    }
    with pytest.raises(RuntimeFault) as excinfo:
        World(root, manifest).registry()
    assert code_of(excinfo) == "MODULE_CLOSURE"
    assert excinfo.value.details == {"package": PKG, "requires": DEP}


def test_listed_pth_file_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "ext"
    root.mkdir()
    digest = write_dist(root, DEP, "1.2.0", {"acme_dep.pth": b"import acme_dep\n"})
    manifest = {
        "manifest_version": 1,
        "packages": [{"name": DEP, "version": "1.2.0", "record_digest": digest}],
        "modules": [],
    }
    with pytest.raises(RuntimeFault) as excinfo:
        World(root, manifest).registry()
    assert code_of(excinfo) == "MODULE_PTH"


def test_planted_pth_file_is_rejected(world: World) -> None:
    (world.root / "zz-planted.pth").write_bytes(b"import os\n")
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_PTH"


def test_module_from_an_unpinned_package_is_rejected(world: World) -> None:
    world.manifest["modules"] = [module_entry(package="other-package")]
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_UNPINNED"


def test_installed_distribution_that_is_not_pinned_is_rejected(world: World) -> None:
    write_dist(world.root, "stray", "1.0", {"stray/__init__.py": b""})
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_UNPINNED"


def test_pinned_version_must_match_the_installed_one(world: World) -> None:
    world.manifest["packages"][1]["version"] = "1.3.0"
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_VERSION"


def test_record_path_leaving_the_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "ext"
    root.mkdir()
    digest = write_dist(root, DEP, "1.2.0", {"../outside.py": b""})
    manifest = {
        "manifest_version": 1,
        "packages": [{"name": DEP, "version": "1.2.0", "record_digest": digest}],
        "modules": [],
    }
    with pytest.raises(RuntimeFault) as excinfo:
        World(root, manifest).registry()
    assert code_of(excinfo) == "MODULE_RECORD_PATH"


def test_installed_entry_points_are_never_scanned(tmp_path: Path) -> None:
    root = tmp_path / "ext"
    root.mkdir()
    info = f"{PKG.replace('-', '_')}-0.1.0.dist-info"
    entry_points = b"[amplai.modules]\nsneaky = acme_modules.judge:factory\n"
    pkg_digest = write_dist(
        root,
        PKG,
        "0.1.0",
        {
            "acme_modules/__init__.py": b"",
            "acme_modules/judge.py": b"",
            f"{info}/entry_points.txt": entry_points,
        },
    )
    manifest = {
        "manifest_version": 1,
        "packages": [{"name": PKG, "version": "0.1.0", "record_digest": pkg_digest}],
        "modules": [],
    }
    registry = World(root, manifest).registry()
    assert registry.modules() == BUILTIN_MODULES


def test_entry_must_be_a_file_of_its_package(world: World) -> None:
    world.manifest["modules"] = [module_entry(entry="acme_dep:VALUE")]
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_ENTRY"


def test_extension_may_not_reuse_a_builtin_module_id(world: World) -> None:
    world.manifest["modules"] = [
        module_entry(
            kind="driver",
            module_id="driver.codex-cli",
            conformance_suite=suite_id("driver"),
        )
    ]
    with pytest.raises(RuntimeFault) as excinfo:
        world.registry()
    assert code_of(excinfo) == "MODULE_DUPLICATE"


def test_manifest_shape_is_strict(world: World) -> None:
    bad = copy.deepcopy(world.manifest)
    bad["modules"][0]["entry_point_group"] = "amplai.modules"
    with pytest.raises(RuntimeFault) as excinfo:
        PinnedManifest.from_dict(bad)
    assert code_of(excinfo) == "MODULE_MANIFEST"


# -- placement (AC-M6) and conformance (M-2) -------------------------------------------------------
@dataclass(frozen=True)
class Impl:
    spec: ModuleSpec
    healthy: bool = True


def judge_suite(kind: str = "judge") -> ConformanceSuite:
    def check(module: Module) -> None:
        if not getattr(module, "healthy", False):
            raise RuntimeFault("JUDGE_CONFORMANCE", "judge is not healthy")

    return ConformanceSuite(suite_id(kind), kind, INTERFACE_VERSIONS[kind], check)


def test_out_of_process_kind_is_refused_in_process_even_when_qualified(world: World) -> None:
    world.manifest["modules"] = [
        module_entry(
            kind="model_gateway",
            module_id="acme.gateway",
            conformance_suite=suite_id("model_gateway"),
            placement="out_of_process",
            entry="acme_modules.gateway:factory",
        )
    ]
    ledger = ConformanceLedger()
    ledger.register_suite(judge_suite("model_gateway"))
    registry = world.registry(ledger)
    spec = registry.get("acme.gateway", "0.1.0")
    assert ledger.run(spec, Impl(spec)).passed
    assert registry.enable("acme.gateway", "0.1.0") == spec
    with pytest.raises(RuntimeFault) as excinfo:
        registry.admit_in_process("acme.gateway", "0.1.0")
    assert code_of(excinfo) == "MODULE_PLACEMENT"


def test_enabling_needs_a_recorded_conformance_pass(world: World) -> None:
    ledger = ConformanceLedger()
    ledger.register_suite(judge_suite())
    registry = world.registry(ledger)
    spec = registry.get("acme.judge", "0.1.0")

    with pytest.raises(Hold) as held:
        registry.enable(*spec.key)
    assert held.value.code == "MODULE_UNQUALIFIED" and held.value.outcome == "hold"
    with pytest.raises(Hold):
        registry.admit_in_process(*spec.key)

    failed = ledger.run(spec, Impl(spec, healthy=False))
    assert not failed.passed and failed.failure == {
        "code": "JUDGE_CONFORMANCE",
        "message": "judge is not healthy",
    }
    with pytest.raises(Hold):
        registry.enable(*spec.key)

    assert ledger.run(spec, Impl(spec)).passed
    assert registry.enable(*spec.key) == spec
    assert registry.admit_in_process(*spec.key) == spec

    # the latest result decides: a later failure takes the qualification away
    ledger.run(spec, Impl(spec, healthy=False))
    with pytest.raises(Hold):
        registry.enable(*spec.key)


def test_a_pass_is_bound_to_the_exact_spec(world: World) -> None:
    ledger = ConformanceLedger()
    ledger.register_suite(judge_suite())
    registry = world.registry(ledger)
    spec = registry.get("acme.judge", "0.1.0")
    other = ModuleSpec(
        "judge", "acme.judge", "0.2.0", 1, suite_id("judge"), frozenset(), "in_process"
    )
    result = ledger.run(spec, Impl(other))
    assert not result.passed and result.failure is not None
    assert result.failure["code"] == "CONFORMANCE_SPEC"
    with pytest.raises(Hold):
        registry.enable(*spec.key)


def test_builtins_also_need_a_conformance_pass() -> None:
    ledger = ConformanceLedger()
    registry = ModuleRegistry(ledger)
    spec = registry.get("strategy.repair_loop", "1")
    with pytest.raises(Hold) as held:
        registry.enable(*spec.key)
    assert held.value.code == "MODULE_UNQUALIFIED"
    ledger.register_suite(
        ConformanceSuite(suite_id("strategy"), "strategy", 1, lambda module: None)
    )
    assert ledger.run(spec, Impl(spec)).passed
    assert registry.admit_in_process(*spec.key) == spec


def test_suite_registration_checks_kind_version_and_uniqueness() -> None:
    ledger = ConformanceLedger()
    with pytest.raises(RuntimeFault) as excinfo:
        ledger.register_suite(ConformanceSuite("conformance.judge.v2", "judge", 2, lambda m: None))
    assert code_of(excinfo) == "MODULE_INTERFACE"
    ledger.register_suite(judge_suite())
    with pytest.raises(RuntimeFault) as excinfo:
        ledger.register_suite(judge_suite())
    assert code_of(excinfo) == "CONFORMANCE_SUITE_DUPLICATE"
