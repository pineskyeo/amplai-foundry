"""Canonical identities and Ed25519 attestations; exact immutable revision binding."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from amplai_foundry._vendor.rfc8785 import dumps

from ..errors import RuntimeFault

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return dumps(value)


def digest(value: object) -> str:
    return digest_bytes(canonical(unsigned(value) if isinstance(value, dict) else value))


def freeze(value: object) -> Any:
    return json.loads(canonical(value))


def reference(record: dict[str, Any], id_field: str, revision: int | None = None) -> dict[str, Any]:
    return {
        "id": record[id_field],
        "revision": revision or record.get("revision", 1),
        "digest": digest(record),
    }


def unsigned(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != "signature"}


def sign(record: dict[str, Any], key_id: str, key: Ed25519PrivateKey) -> dict[str, Any]:
    result: dict[str, Any] = freeze(unsigned(record))
    raw = canonical(result)
    result["signature"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "signed_digest": digest_bytes(raw),
        "signature_b64": base64.b64encode(key.sign(raw)).decode("ascii"),
    }
    return result


def verify_signature(record: dict[str, Any], trusted_keys: dict[str, Ed25519PublicKey]) -> None:
    sig = record.get("signature", {})
    if sig.get("algorithm") != "ed25519" or sig.get("key_id") not in trusted_keys:
        raise RuntimeFault("UNTRUSTED_SIGNATURE", "No trusted signing key for this object")
    raw = canonical(unsigned(record))
    if sig.get("signed_digest") != digest_bytes(raw):
        raise RuntimeFault("SIGNATURE_DIGEST", "Signed payload has changed")
    try:
        trusted_keys[sig["key_id"]].verify(
            base64.b64decode(sig["signature_b64"], validate=True), raw
        )
    except (ValueError, KeyError, InvalidSignature) as exc:
        raise RuntimeFault("BAD_SIGNATURE", "Ed25519 verification failed") from exc
