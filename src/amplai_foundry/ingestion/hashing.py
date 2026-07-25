"""Byte-preserving source hashing helpers."""

from hashlib import sha256


def content_sha256(content: bytes) -> str:
    """Hash the exact UTF-8 input bytes."""
    return sha256(content).hexdigest()


def normalize_content(content: bytes) -> bytes:
    """Normalize CRLF and insignificant final newlines for duplicate detection."""
    return content.replace(b"\r\n", b"\n").rstrip(b"\n")


def normalized_sha256(content: bytes) -> str:
    """Hash normalized input bytes."""
    return sha256(normalize_content(content)).hexdigest()
