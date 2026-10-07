"""Owner-only credential files and Ed25519 trust keys for the local operator.

A key file is read only when it is a regular, non-symlinked, owner-only (0600) file that
belongs to the service identity. Generating a key never overwrites an existing one and
never grants execution authority by itself.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .errors import Hold


def private_bytes(path: Path, *, maximum: int = 4 * 1024 * 1024) -> bytes:
    path = path.expanduser().absolute()
    if path.resolve(strict=True) != path or path.is_symlink():
        raise Hold("SECRET_PATH", "Credential paths and their ancestors must not be symlinks")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise Hold("SECRET_PERMISSIONS", "Credential files require owner-only permissions (0600)")
    if info.st_uid != os.getuid():
        raise Hold("SECRET_OWNER", "Credential files must belong to the service identity")
    if info.st_size > maximum:
        raise Hold("SECRET_SIZE", "Credential file exceeds its configured bound")
    return path.read_bytes()


def read_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(private_bytes(path), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise Hold("SIGNING_ALGORITHM", "An Ed25519 signing key is required")
    return key


def generate_key(path: Path) -> dict[str, Any]:
    path = path.expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise Hold("KEY_EXISTS", "An existing trust key will not be overwritten")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.resolve() != path.parent:
        raise Hold("SECRET_PATH", "Key parent path may not contain symlinks")
    key = Ed25519PrivateKey.generate()
    payload = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )
    return {"key_file": str(path), "public_key_hex": public, "authority_granted": False}
