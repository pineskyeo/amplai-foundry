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


def baseline_entries():
    """Finite optional baseline inventory; no implicit adoption of extra payloads."""
    base = os.path.join(ROOT, "baseline")
    path = os.path.join(base, "manifest.json")
    if not os.path.exists(path):
        return None, []
    with io.open(path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    entries = []
    declared = set()
    for item in manifest.get("files", []):
        rel = item["path"]
        if (not isinstance(rel, str) or rel.startswith("/") or "\\" in rel
                or any(part in ("", ".", "..") for part in rel.split("/")) or rel in declared):
            raise ValueError("unsafe or duplicate baseline path")
        declared.add(rel)
        target = os.path.join(base, "payload", rel)
        if not os.path.isfile(target) or os.path.islink(target):
            raise ValueError("baseline payload missing or symlink: " + rel)
        entries.append((item, target, "file:" + rel))
    actual = set()
    for directory, dirs, names in os.walk(os.path.join(base, "payload")):
        if any(os.path.islink(os.path.join(directory, name)) for name in dirs):
            raise ValueError("baseline payload contains directory symlink")
        for name in names:
            actual.add(os.path.relpath(os.path.join(directory, name), os.path.join(base, "payload")).replace(os.sep, "/"))
    if actual != declared:
        raise ValueError("baseline payload inventory differs from manifest")
    for item in manifest.get("markers", []):
        name = item["fragment"]
        if not isinstance(name, str) or os.path.basename(name) != name or name in (".", ".."):
            raise ValueError("unsafe baseline fragment")
        target = os.path.join(base, "fragments", name)
        if not os.path.isfile(target) or os.path.islink(target):
            raise ValueError("baseline fragment missing or symlink")
        entries.append((item, target, "marker:" + item["path"]))
    return manifest, entries


def reseal_baseline():
    manifest, entries = baseline_entries()
    changed = []
    for item, target, label in entries:
        digest = "sha256:" + sha256_file(target)
        if item.get("sha256") != digest:
            item["sha256"] = digest
            changed.append(label)
    if changed:
        with io.open(os.path.join(ROOT, "baseline", "manifest.json"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    return changed


def reseal_checksums():
    rows = ["%s  %s\n" % (sha256_file(os.path.join(ROOT, rel)), rel)
            for rel in package_files()]
    with io.open(os.path.join(ROOT, CHECKSUMS), "w", encoding="utf-8") as handle:
        handle.write("".join(rows))
    return len(rows)


def verify():
    """Report mismatches without writing.  Exit 1 when the seal is broken.

    The installer also verifies the complete inventory before any mutation.
    This read-only mode keeps source/payload integrity in the repository gate.
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

    baseline_stale = []
    try:
        _, entries = baseline_entries()
        baseline_stale = [label for item, target, label in entries
                          if item.get("sha256") != "sha256:" + sha256_file(target)]
    except (ValueError, OSError, KeyError, TypeError):
        baseline_stale.append("invalid baseline inventory")
    ok = not (mismatched or missing or unlisted or manifest_stale or baseline_stale)
    print(json.dumps({
        "ok": ok,
        "checksum_mismatched": mismatched,
        "checksum_missing_file": missing,
        "not_in_checksums": unlisted,
        "manifest_stale": sorted(manifest_stale),
        "baseline_stale": baseline_stale,
        "hint": None if ok else "run `python3 seal.py` after changing payload or fragments",
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 1


def main():
    if "--verify" in sys.argv[1:]:
        return verify()
    # Manifest first: it is itself a package file, so its hash must settle
    # before CHECKSUMS is written.
    changed = reseal_manifest()
    baseline_changed = reseal_baseline()
    count = reseal_checksums()
    print(json.dumps({
        "ok": True,
        "manifest_entries_updated": changed,
        "baseline_entries_updated": baseline_changed,
        "checksum_rows": count,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
