#!/usr/bin/env python3
"""Recompute the two integrity records this package carries.

There are two, and editing one without the other is the mistake this script
exists to prevent:

* ``manifest.json`` stores a hash per payload file and per fragment, which is
  what ``install.py`` checks when it decides whether a target's file was
  modified locally.
* ``CHECKSUMS.sha256`` stores a hash per package file, which is what verifies
  the package itself was delivered intact.

Run this after changing anything under ``payload/`` or ``fragments/``, then run
``selftest.py``.  Never edit either record by hand.
"""
from __future__ import print_function

import hashlib
import io
import json
import os
import sys

ROOT = os.path.abspath(os.path.dirname(__file__))
CHECKSUMS = "CHECKSUMS.sha256"


def sha256_file(path):
    digest = hashlib.sha256()
    with io.open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_files():
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            rel = os.path.relpath(os.path.join(base, name), ROOT).replace(os.sep, "/")
            if rel != CHECKSUMS:
                yield rel


def reseal_manifest():
    path = os.path.join(ROOT, "manifest.json")
    with io.open(path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    changed = []
    for item in manifest.get("owned_files", []):
        target = os.path.join(ROOT, "payload", item["path"])
        if not os.path.isfile(target):
            raise SystemExit("owned file missing from payload: %s" % item["path"])
        digest = "sha256:" + sha256_file(target)
        if item.get("sha256") != digest:
            changed.append("owned_files:" + item["path"])
            item["sha256"] = digest

    for key in ("markers", "json_merges"):
        for item in manifest.get(key, []):
            target = os.path.join(ROOT, "fragments", item["fragment"])
            if not os.path.isfile(target):
                raise SystemExit("fragment missing: %s" % item["fragment"])
            digest = "sha256:" + sha256_file(target)
            if item.get("sha256") != digest:
                changed.append("%s:%s" % (key, item["fragment"]))
                item["sha256"] = digest

    if changed:
        body = json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
    return changed


def reseal_checksums():
    rows = ["%s  %s\n" % (sha256_file(os.path.join(ROOT, rel)), rel)
            for rel in package_files()]
    with io.open(os.path.join(ROOT, CHECKSUMS), "w", encoding="utf-8") as handle:
        handle.write("".join(rows))
    return len(rows)


def verify():
    """Report mismatches without writing.  Exit 1 when the seal is broken.

    Nothing else reads CHECKSUMS.sha256, so a stale seal survived two commits
    unnoticed.  This mode exists to be wired into a verifier.
    """
    mismatched = []
    missing = []
    listed = set()
    path = os.path.join(ROOT, CHECKSUMS)
    with io.open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            digest, rel = line.split("  ", 1)
            listed.add(rel)
            target = os.path.join(ROOT, rel)
            if not os.path.isfile(target):
                missing.append(rel)
            elif sha256_file(target) != digest:
                mismatched.append(rel)
    unlisted = sorted(set(package_files()) - listed)

    manifest_stale = []
    with io.open(os.path.join(ROOT, "manifest.json"), "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    for item in manifest.get("owned_files", []):
        target = os.path.join(ROOT, "payload", item["path"])
        if os.path.isfile(target) and item.get("sha256") != "sha256:" + sha256_file(target):
            manifest_stale.append("owned_files:" + item["path"])
    for key in ("markers", "json_merges"):
        for item in manifest.get(key, []):
            target = os.path.join(ROOT, "fragments", item["fragment"])
            if os.path.isfile(target) and item.get("sha256") != "sha256:" + sha256_file(target):
                manifest_stale.append("%s:%s" % (key, item["fragment"]))

    ok = not (mismatched or missing or unlisted or manifest_stale)
    print(json.dumps({
        "ok": ok,
        "checksum_mismatched": mismatched,
        "checksum_missing_file": missing,
        "not_in_checksums": unlisted,
        "manifest_stale": sorted(manifest_stale),
        "hint": None if ok else "run `python3 seal.py` after changing payload or fragments",
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 1


def main():
    if "--verify" in sys.argv[1:]:
        return verify()
    # Manifest first: it is itself a package file, so its hash must settle
    # before CHECKSUMS is written.
    changed = reseal_manifest()
    count = reseal_checksums()
    print(json.dumps({
        "ok": True,
        "manifest_entries_updated": changed,
        "checksum_rows": count,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
