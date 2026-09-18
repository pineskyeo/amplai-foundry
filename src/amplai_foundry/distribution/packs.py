"""Signed capability packs bind manifest AND exact payload bytes.

A pack's requested permissions are not execution grants. Dependencies and publisher
trust are resolved before any file mutation. ZIP/symlink extraction is never used.
"""

from __future__ import annotations

import base64
from collections.abc import Collection, Iterable
from pathlib import PurePosixPath
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..runtime.contracts.identity import digest, digest_bytes, sign, verify_signature
from ..runtime.contracts.semantics import check_refs
from ..runtime.errors import Hold, RuntimeFault
from ..runtime.storage.store import Store

MAX_PACK_BYTES = 32 * 1024 * 1024
PROTECTED_PARTS = {".git", ".env", "node_modules", ".venv", "__pycache__"}
EFFECT_RANK = {
    "pure_read": 0,
    "sandbox_write": 1,
    "external_idempotent_write": 2,
    "external_nonidempotent_write": 3,
    "production_control": 4,
}
# Without an explicit ceiling a pack may only ask for reads and sandbox writes.
DEFAULT_CEILING_RANK = EFFECT_RANK["sandbox_write"]

Ref = dict[str, Any]
Capability = dict[str, str]


def safe_path(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise RuntimeFault("PACK_PATH", "Invalid path")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or any(p in PROTECTED_PARTS for p in path.parts)
    ):
        raise RuntimeFault("PACK_PATH", "Pack path crosses a protected boundary")
    if path.as_posix() != name:
        raise RuntimeFault("PACK_PATH", "Pack paths must be normalized POSIX paths")
    return path


def seal_pack(
    manifest: dict[str, Any],
    files: dict[str, bytes],
    *,
    key_id: str,
    private_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    normalized: dict[str, dict[str, Any]] = {}
    for path, data in files.items():
        safe_path(path)
        if not isinstance(data, bytes):
            raise TypeError("Pack payloads are bytes")
        normalized[path] = {
            "digest": digest_bytes(data),
            "size_bytes": len(data),
            "content_base64": base64.b64encode(data).decode(),
        }
    signed_manifest = sign(manifest, key_id, private_key)
    envelope = {"schema_version": "3.0.0", "manifest": signed_manifest, "files": normalized}
    return sign(envelope, key_id, private_key)


def _resource_matches(ceiling_resource: str, requested: str) -> bool:
    if ceiling_resource.endswith("*"):
        return requested.startswith(ceiling_resource[:-1])
    return ceiling_resource == requested


def check_permissions(
    requested: Iterable[Capability], ceiling: Iterable[Capability] | None
) -> None:
    """Requested permissions must fit inside the ceiling; they are never grants (T-077)."""
    ceiling_list = list(ceiling) if ceiling is not None else None
    for cap in requested:
        rank = EFFECT_RANK.get(cap["effect_class"])
        if rank is None:
            raise RuntimeFault("PACK_EFFECT_CLASS", "Unknown effect class in pack permissions")
        if ceiling_list is None:
            allowed = rank <= DEFAULT_CEILING_RANK
        else:
            allowed = any(
                c["action"] == cap["action"]
                and _resource_matches(c["resource"], cap["resource"])
                and rank <= EFFECT_RANK[c["effect_class"]]
                for c in ceiling_list
            )
        if not allowed:
            raise Hold(
                "PACK_PERMISSION_ELEVATED",
                "Pack requests a permission above the installation ceiling; refused before install",
                details={"capability": cap},
            )


class PackRegistry:
    def __init__(
        self,
        store: Store,
        contracts: Any,
        trusted_publishers: dict[str, dict[str, Ed25519PublicKey]],
        *,
        tool_adapters: Collection[str] = (),
        attested_digests: Collection[str] = (),
    ) -> None:
        self.store, self.contracts, self.publishers = store, contracts, trusted_publishers
        # Entry capabilities must name adapters that already exist in the policy registry;
        # a pack cannot register an executable by path string alone (design/13 §2).
        self.tool_adapters = frozenset(tool_adapters)
        # Executable payload is admitted only with a release attestation of its digest.
        self.attested_digests = frozenset(attested_digests)

    def inspect(self, bundle: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes]]:
        if (
            set(bundle) != {"schema_version", "manifest", "files", "signature"}
            or bundle["schema_version"] != "3.0.0"
        ):
            raise RuntimeFault("PACK_ENVELOPE", "Unknown pack envelope fields/version")
        manifest = bundle["manifest"]
        self.contracts.validate("capability-pack", manifest)
        keys = self.publishers.get(manifest["publisher"])
        if not keys:
            raise Hold("PACK_PUBLISHER", "Publisher is not explicitly trusted")
        verify_signature(bundle, keys)
        verify_signature(manifest, keys)
        if set(bundle["files"]) != set(manifest["owned_paths"]) or len(
            manifest["owned_paths"]
        ) != len(set(manifest["owned_paths"])):
            raise Hold("PACK_OWNERSHIP", "Manifest owned paths differ from the signed payload")
        content: dict[str, bytes] = {}
        size = 0
        for name, item in bundle["files"].items():
            safe_path(name)
            if set(item) != {"digest", "size_bytes", "content_base64"}:
                raise RuntimeFault("PACK_FILE_FIELDS", "Unknown payload metadata")
            try:
                data = base64.b64decode(item["content_base64"], validate=True)
            except Exception as exc:
                raise RuntimeFault("PACK_ENCODING", "Invalid base64 payload") from exc
            size += len(data)
            if (
                size > MAX_PACK_BYTES
                or digest_bytes(data) != item["digest"]
                or len(data) != item["size_bytes"]
            ):
                raise Hold(
                    "PACK_INTEGRITY", "Pack payload exceeds bounds or differs from signed content"
                )
            content[name] = data
        from ..packs.catalog import executable_like
        from ..runtime.evidence.cas import scan_secrets

        for name, data in content.items():
            if scan_secrets(data):
                raise Hold(
                    "PACK_SECRET",
                    "Pack payload contains a credential pattern; raw secret is not logged",
                    details={"path": name},
                )
            if executable_like(name, data) and digest_bytes(data) not in self.attested_digests:
                raise Hold(
                    "PACK_EXECUTABLE_UNATTESTED",
                    "Executable pack payload needs a release attestation of its digest",
                    details={"path": name},
                )
        return manifest, content

    def register(
        self,
        actor: Any,
        bundle: dict[str, Any],
        *,
        permission_ceiling: Iterable[Capability] | None = None,
    ) -> Ref:
        actor.require("pack.install")
        manifest, _content = self.inspect(bundle)
        check_refs(self.store, actor.scope, manifest)
        check_permissions(manifest["permissions"], permission_ceiling)
        unknown_tools = sorted(set(manifest["entry_capabilities"]) - self.tool_adapters)
        if unknown_tools:
            raise Hold(
                "PACK_TOOL_UNREGISTERED",
                "Entry capabilities must map to adapters in the policy registry",
                details={"entry_capabilities": unknown_tools},
            )
        # Dependencies resolve to complete registered pack manifests, never names/latest.
        visiting: set[str] = set()
        seen: set[str] = set()
        closure: dict[str, str] = {manifest["pack_id"]: manifest["version"]}

        def walk(ref: Ref) -> None:
            key = digest(ref)
            if key in visiting:
                raise Hold("PACK_DEPENDENCY_CYCLE", "Pack dependency cycle")
            if key in seen:
                return
            visiting.add(key)
            value = self.store.get(actor.scope, "capability-pack", ref)
            self.contracts.validate("capability-pack", value)
            known = closure.get(value["pack_id"])
            if known is not None and known != value["version"]:
                raise Hold(
                    "PACK_VERSION_CONFLICT",
                    "The dependency closure needs two versions of one pack",
                    details={
                        "pack_id": value["pack_id"],
                        "versions": sorted({known, value["version"]}),
                    },
                )
            closure[value["pack_id"]] = value["version"]
            for child in value["dependencies"]:
                walk(child)
            visiting.remove(key)
            seen.add(key)

        for dep in manifest["dependencies"]:
            walk(dep)
        with self.store.tx() as db:
            versions = self.store.list_objects(actor.scope, "capability-pack")
            matching = [
                (ref, value) for ref, value in versions if value["pack_id"] == manifest["pack_id"]
            ]
            for ref, value in matching:
                if value["version"] == manifest["version"]:
                    if value != manifest:
                        raise Hold(
                            "PACK_VERSION_MUTATION",
                            "The same semantic pack version has different signed contents",
                        )
                    return ref
            revision = max([ref["revision"] for ref, _ in matching] + [0]) + 1
            ref = self.store.put(
                db, actor.scope, "capability-pack", manifest["pack_id"], revision, manifest
            )
            self.store.put(db, actor.scope, "pack-envelope", manifest["pack_id"], revision, bundle)
            self.store.event(
                db,
                actor.scope,
                "release",
                manifest["pack_id"],
                "pack.registered",
                {"pack_ref": ref, "requested_permissions_are_grants": False},
            )
            return ref
