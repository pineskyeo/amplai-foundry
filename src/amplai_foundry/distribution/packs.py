"""Signed capability packs bind manifest AND exact payload bytes.

A pack's requested permissions are not execution grants. Dependencies and publisher
trust are resolved before any file mutation. ZIP/symlink extraction is never used.
"""

from __future__ import annotations

import base64
from pathlib import PurePosixPath

from ..runtime.contracts.identity import digest, digest_bytes, sign, verify_signature
from ..runtime.contracts.semantics import check_refs
from ..runtime.errors import Hold, RuntimeFault

MAX_PACK_BYTES = 32 * 1024 * 1024
PROTECTED_PARTS = {".git", ".env", "node_modules", ".venv", "__pycache__"}


def safe_path(name):
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


def seal_pack(manifest, files, *, key_id, private_key):
    normalized = {}
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


class PackRegistry:
    def __init__(self, store, contracts, trusted_publishers):
        self.store, self.contracts, self.publishers = store, contracts, trusted_publishers

    def inspect(self, bundle):
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
        content = {}
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
        return manifest, content

    def register(self, actor, bundle):
        actor.require("pack.install")
        manifest, content = self.inspect(bundle)
        check_refs(self.store, actor.scope, manifest)
        # Dependencies resolve to complete registered pack manifests, never names/latest.
        visiting = set()
        seen = set()

        def walk(ref):
            key = digest(ref)
            if key in visiting:
                raise Hold("PACK_DEPENDENCY_CYCLE", "Pack dependency cycle")
            if key in seen:
                return
            visiting.add(key)
            value = self.store.get(actor.scope, "capability-pack", ref)
            self.contracts.validate("capability-pack", value)
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
