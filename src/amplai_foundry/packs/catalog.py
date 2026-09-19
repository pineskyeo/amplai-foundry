"""Repository pack catalog (design/13 §2): PACK.json + skills/context/verifiers/eval/templates.

The catalog reads unsigned pack sources from ``packs/<pack_id>/`` and seals them into
the signed envelope the registry accepts. Missing optional directories are not
corruption (T-093); a missing PACK.json, a symlink, or a file outside the layout is.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from amplai_foundry.distribution.packs import safe_path, seal_pack
from amplai_foundry.runtime.errors import Hold, RuntimeFault

MANIFEST = "PACK.json"
LAYOUT: tuple[str, ...] = ("skills", "context", "verifiers/definitions", "eval/cases", "templates")
MAX_FILE_BYTES = 4 * 1024 * 1024


class PackCatalog:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise RuntimeFault("PACK_CATALOG", "Pack catalog root must be an existing directory")

    def list_ids(self) -> list[str]:
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and not p.is_symlink() and (p / MANIFEST).is_file()
        )

    def _directory(self, pack_id: str) -> Path:
        safe_path(pack_id)
        if "/" in pack_id:
            raise RuntimeFault("PACK_ID_PATH", "Pack id must be a single path segment")
        directory = self.root / pack_id
        if directory.is_symlink() or not directory.is_dir():
            raise Hold("PACK_SOURCE_MISSING", "Pack directory is absent or a symlink")
        return directory

    def load(self, pack_id: str) -> tuple[dict[str, Any], dict[str, bytes]]:
        directory = self._directory(pack_id)
        manifest_path = directory / MANIFEST
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise Hold("PACK_MANIFEST_MISSING", "PACK.json is required for every pack")
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except ValueError as exc:
            raise RuntimeFault("PACK_MANIFEST", "PACK.json is not valid JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("pack_id") != pack_id:
            raise Hold("PACK_ID_MISMATCH", "PACK.json pack_id must equal its directory name")
        if "signature" in manifest:
            raise Hold("PACK_PRESIGNED", "Pack sources are unsigned; sealing signs them")
        files: dict[str, bytes] = {}
        for current, subdirs, names in os.walk(directory):
            subdirs[:] = sorted(d for d in subdirs if not (Path(current) / d).is_symlink())
            for name in sorted(names):
                path = Path(current) / name
                relative = path.relative_to(directory).as_posix()
                if relative == MANIFEST:
                    continue
                if path.is_symlink():
                    raise Hold("PACK_SYMLINK", "Pack sources may not contain symlinks")
                if not any(relative == area or relative.startswith(area + "/") for area in LAYOUT):
                    raise Hold(
                        "PACK_LAYOUT",
                        "Pack files must live under skills/, context/, verifiers/definitions/, "
                        "eval/cases/ or templates/",
                        details={"path": relative},
                    )
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise Hold("PACK_FILE_SIZE", "Pack file exceeds the bounded size")
                safe_path(relative)
                files[relative] = path.read_bytes()
        declared = manifest.get("owned_paths")
        if declared is None:
            manifest["owned_paths"] = sorted(files)
        elif sorted(declared) != sorted(files):
            raise Hold(
                "PACK_OWNERSHIP",
                "PACK.json owned_paths differ from the files actually present",
                details={
                    "missing": sorted(set(declared) - set(files)),
                    "undeclared": sorted(set(files) - set(declared)),
                },
            )
        return manifest, files

    def seal(self, pack_id: str, *, key_id: str, private_key: Ed25519PrivateKey) -> dict[str, Any]:
        manifest, files = self.load(pack_id)
        return seal_pack(manifest, files, key_id=key_id, private_key=private_key)

    @staticmethod
    def layout_of(files: dict[str, bytes]) -> dict[str, list[str]]:
        areas: dict[str, list[str]] = {area: [] for area in LAYOUT}
        for name in sorted(files):
            for area in LAYOUT:
                if name == area or name.startswith(area + "/"):
                    areas[area].append(name)
                    break
        return areas


def executable_like(name: str, data: bytes) -> bool:
    """Payload that a host could execute needs a release attestation (design/13 §2)."""
    suffix = PurePosixPath(name).suffix.lower()
    return data.startswith(b"#!") or suffix in {".sh", ".py", ".js", ".mjs", ".rb", ".pl", ".ps1"}
