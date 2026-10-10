"""Extension modules from a pinned manifest only (Work 034 S0, spec M-3, AC-M3).

The manifest pins every distribution an extension needs: the package and its whole dependency
closure, each by name, version and the sha256 of its wheel ``RECORD`` bytes. Verification is
fail-closed and all-or-nothing; any finding rejects the whole manifest:

- every extension root is a dedicated directory filled by verbatim wheel extraction, so the
  installed ``RECORD`` is the wheel ``RECORD`` (an installer that adds ``INSTALLER``/``REQUESTED``
  or compiled files would change the digest);
- no ``.pth`` file anywhere under a root, listed or not, and no symlink;
- every distribution under a root is pinned, every pinned one is installed once at its version;
- ``RECORD`` bytes match the pinned digest, every listed file matches its sha256 and size, and no
  file under a root is unlisted (``__pycache__`` included: loading from a verified read-only copy,
  plan.md §3.1, is a later PR);
- every ``Requires-Dist`` of a pinned distribution names a pinned distribution. Requirements gated
  on an ``extra`` are not part of the closure; every other marker counts as true (fail-closed);
- a module's code location comes from the manifest ``entry`` and must be a file of its package.
  Entry points declared by installed metadata are never read.

Manifest shape (signature and trust root, M-4, are not checked in this PR)::

    {
      "manifest_version": 1,
      "packages": [{"name": str, "version": str, "record_digest": "sha256:<64 hex>"}],
      "modules": [{"kind": str, "module_id": str, "version": str, "interface_version": int,
                   "conformance_suite": str, "permissions": [str], "placement": str,
                   "package": str, "entry": "pkg.module:attribute"}]
    }
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib.metadata import PathDistribution
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from ..contracts.identity import digest_bytes
from ..errors import RuntimeFault
from .spec import ModuleSpec, Placement

MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["manifest_version", "packages", "modules"],
    "properties": {
        "manifest_version": {"const": 1},
        "packages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "version", "record_digest"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "version": {"type": "string", "minLength": 1},
                    "record_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                },
            },
        },
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind", "module_id", "version", "interface_version", "conformance_suite",
                    "permissions", "placement", "package", "entry",
                ],
                "properties": {
                    "kind": {"type": "string"},
                    "module_id": {"type": "string"},
                    "version": {"type": "string"},
                    "interface_version": {"type": "integer"},
                    "conformance_suite": {"type": "string"},
                    "permissions": {"type": "array", "items": {"type": "string"}},
                    "placement": {"type": "string"},
                    "package": {"type": "string", "minLength": 1},
                    "entry": {"type": "string"},
                },
            },
        },
    },
}  # fmt: skip
_ENTRY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):[A-Za-z_][A-Za-z0-9_]*")
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_EXTRA_MARKER = re.compile(r"\bextra\s*==")


def normalize(name: str) -> str:
    """PEP 503 name normalization; the manifest and installed metadata compare on this."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _fault(code: str, message: str, **details: object) -> RuntimeFault:
    return RuntimeFault(code, message, details=details or None)


@dataclass(frozen=True)
class PinnedPackage:
    name: str  # normalized
    version: str
    record_digest: str


@dataclass(frozen=True)
class PinnedModule:
    spec: ModuleSpec
    package: str  # normalized
    entry: str


@dataclass(frozen=True)
class PinnedManifest:
    packages: tuple[PinnedPackage, ...]
    modules: tuple[PinnedModule, ...]

    @classmethod
    def from_dict(cls, value: object) -> PinnedManifest:
        errors = sorted(
            Draft202012Validator(MANIFEST_SCHEMA).iter_errors(value), key=lambda e: list(e.path)
        )
        if errors:
            raise _fault(
                "MODULE_MANIFEST",
                "invalid module manifest",
                errors=[f"{list(e.path)}: {e.message}" for e in errors[:10]],
            )
        data = cast(dict[str, Any], value)
        packages: dict[str, PinnedPackage] = {}
        for item in data["packages"]:
            name = normalize(item["name"])
            if name in packages:
                raise _fault("MODULE_MANIFEST", "package pinned twice", package=name)
            packages[name] = PinnedPackage(name, item["version"], item["record_digest"])
        modules = tuple(
            PinnedModule(
                spec=ModuleSpec(
                    kind=item["kind"],
                    module_id=item["module_id"],
                    version=item["version"],
                    interface_version=item["interface_version"],
                    conformance_suite=item["conformance_suite"],
                    permissions=frozenset(item["permissions"]),
                    placement=cast(Placement, item["placement"]),
                ),
                package=normalize(item["package"]),
                entry=item["entry"],
            )
            for item in data["modules"]
        )
        return cls(tuple(packages.values()), modules)

    def package(self, name: str) -> PinnedPackage | None:
        wanted = normalize(name)
        return next((p for p in self.packages if p.name == wanted), None)


@dataclass(frozen=True)
class _Installed:
    root: Path
    dist_info: Path
    dist: PathDistribution
    name: str  # normalized
    version: str


def _scan_roots(roots: Sequence[Path]) -> None:
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise _fault("MODULE_ROOT", "extension root is not a directory", root=str(root))
        for path in root.rglob("*"):
            if path.is_symlink():
                raise _fault("MODULE_SYMLINK", "symlink under an extension root", path=str(path))
            if path.suffix == ".pth":
                raise _fault("MODULE_PTH", ".pth file under an extension root", path=str(path))


def _installed(roots: Sequence[Path]) -> dict[str, _Installed]:
    found: dict[str, _Installed] = {}
    for root in roots:
        for info in sorted(root.glob("*.dist-info")):
            dist = PathDistribution(info)
            name, version = dist.metadata.get_all("Name"), dist.metadata.get_all("Version")
            if not name or not version or len(name) != 1 or len(version) != 1:
                raise _fault(
                    "MODULE_DIST", "dist-info without one Name and Version", path=str(info)
                )
            key = normalize(name[0])
            if key in found:
                raise _fault("MODULE_DIST_DUPLICATE", "distribution installed twice", package=key)
            found[key] = _Installed(root, info, dist, key, version[0])
    return found


def _file_digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _verify_files(installed: _Installed) -> set[Path]:
    """Check every ``RECORD`` entry; return the absolute paths the distribution owns."""
    files = installed.dist.files
    if files is None:
        raise _fault("MODULE_RECORD_MISSING", "distribution has no RECORD", package=installed.name)
    record = f"{installed.dist_info.name}/RECORD"
    owned: set[Path] = set()
    for entry in files:
        rel = entry.as_posix()
        if entry.is_absolute() or ".." in entry.parts or rel.startswith("/"):
            raise _fault(
                "MODULE_RECORD_PATH",
                "RECORD entry leaves the root",
                package=installed.name,
                path=rel,
            )
        path = installed.root / rel
        if path.suffix == ".pth":
            raise _fault("MODULE_PTH", "RECORD lists a .pth file", package=installed.name, path=rel)
        owned.add(path)
        if rel == record:
            continue
        if entry.hash is None or entry.hash.mode != "sha256":
            raise _fault(
                "MODULE_FILE_HASH", "RECORD entry without sha256", package=installed.name, path=rel
            )
        if not path.is_file():
            raise _fault(
                "MODULE_FILE_MISMATCH", "listed file is missing", package=installed.name, path=rel
            )
        data = path.read_bytes()
        size_ok = entry.size is None or entry.size == len(data)
        if not size_ok or _file_digest(data) != entry.hash.value:
            raise _fault(
                "MODULE_FILE_MISMATCH",
                "installed file differs from RECORD",
                package=installed.name,
                path=rel,
            )
    return owned


def _requirements(dist: PathDistribution) -> Iterable[str]:
    for requirement in dist.requires or ():
        spec, _, marker = requirement.partition(";")
        if marker and _EXTRA_MARKER.search(marker):
            continue
        match = _REQUIREMENT_NAME.match(spec)
        if match is None:
            raise _fault("MODULE_CLOSURE", "unreadable Requires-Dist", requirement=requirement)
        yield normalize(match.group(1))


def verify_extensions(manifest: PinnedManifest, roots: Sequence[Path]) -> tuple[PinnedModule, ...]:
    """Verify ``roots`` against ``manifest`` (module docstring); return its modules or raise."""
    if not roots:
        if manifest.packages or manifest.modules:
            raise _fault("MODULE_ROOT", "a manifest with packages needs an extension root")
        return ()
    _scan_roots(roots)
    installed = _installed(roots)
    pinned = {p.name: p for p in manifest.packages}
    unpinned = sorted(set(installed) - set(pinned))
    if unpinned:
        raise _fault("MODULE_UNPINNED", "installed distribution is not pinned", packages=unpinned)
    owned: dict[str, set[Path]] = {}
    for name, pin in sorted(pinned.items()):
        dist = installed.get(name)
        if dist is None:
            raise _fault(
                "MODULE_DIST_MISSING", "pinned distribution is not installed", package=name
            )
        if dist.version != pin.version:
            raise _fault(
                "MODULE_VERSION",
                "installed version differs from the pin",
                package=name,
                pinned=pin.version,
                installed=dist.version,
            )
        record = dist.dist_info / "RECORD"
        if not record.is_file():
            raise _fault("MODULE_RECORD_MISSING", "distribution has no RECORD", package=name)
        actual = digest_bytes(record.read_bytes())
        if actual != pin.record_digest:
            raise _fault(
                "MODULE_RECORD_DIGEST",
                "RECORD digest differs from the pin",
                package=name,
                pinned=pin.record_digest,
                installed=actual,
            )
        owned[name] = _verify_files(dist)
    every: set[Path] = set()
    for paths in owned.values():
        every |= paths
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path not in every:
                raise _fault(
                    "MODULE_FILE_UNLISTED", "file not listed by any RECORD", path=str(path)
                )
    for name in sorted(pinned):
        for requirement in _requirements(installed[name].dist):
            if requirement not in pinned:
                raise _fault(
                    "MODULE_CLOSURE",
                    "dependency outside the pinned closure",
                    package=name,
                    requires=requirement,
                )
    for module in manifest.modules:
        if module.package not in pinned:
            raise _fault(
                "MODULE_UNPINNED",
                "module package is not pinned",
                module_id=module.spec.module_id,
                package=module.package,
            )
        match = _ENTRY.fullmatch(module.entry)
        if match is None:
            raise _fault("MODULE_ENTRY", "entry is not module:attribute", entry=module.entry)
        dotted = match.group(1).replace(".", "/")
        dist = installed[module.package]
        candidates = {dist.root / f"{dotted}.py", dist.root / dotted / "__init__.py"}
        if not candidates & owned[module.package]:
            raise _fault(
                "MODULE_ENTRY",
                "entry module is not a file of its package",
                module_id=module.spec.module_id,
                entry=module.entry,
            )
    return manifest.modules
