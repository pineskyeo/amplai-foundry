#!/usr/bin/env python3
"""Install or update the AMPLAI Decision & Async Cross-App Runtime.

The installer is intentionally conservative:
- validates every package payload before touching the target;
- owns whole files only when their previous installed hash still matches;
- owns only marked sections inside existing skills/policies;
- merges Claude Code and Codex hooks without changing permissions;
- backs up and rolls back target files on write failure.

Python 3.6+; standard library only.
"""
from __future__ import print_function

import argparse
import base64
import copy
import datetime
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid

PACKAGE_ROOT = os.path.abspath(os.path.dirname(__file__))
MANIFEST_PATH = os.path.join(PACKAGE_ROOT, "manifest.json")
STATE_REL = ".ai-team/install/amplai-loop-kit.json"
JOURNAL_REL = ".ai-team/install/amplai-loop-kit.transaction.json"
BACKUP_ROOT_REL = ".ai-team/backups/amplai-loop-kit"
PROTOCOL = "amplai.async-cross-app.v1"


class InstallError(Exception):
    pass


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(
        tzinfo=None, microsecond=0
    ).isoformat() + "Z"


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal(value):
    result = copy.deepcopy(value)
    result.pop("content_hash", None)
    result["content_hash"] = "sha256:" + hashlib.sha256(canonical_json(result)).hexdigest()
    return result


def verify_seal(value, label):
    actual = value.get("content_hash") if isinstance(value, dict) else None
    expected = seal(value).get("content_hash") if isinstance(value, dict) else None
    if actual != expected:
        raise InstallError("%s content_hash mismatch" % label)


def read_bytes(path):
    with io.open(path, "rb") as handle:
        return handle.read()


def read_text(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_json(path):
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (IOError, ValueError) as exc:
        raise InstallError("cannot read JSON %s: %s" % (path, exc))


def sha256_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(read_bytes(path))


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)


def atomic_write(path, data, mode):
    ensure_dir(os.path.dirname(path))
    fd, temp_path = tempfile.mkstemp(prefix=".amplai-install-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def safe_destination(target, rel):
    validate_relative(rel)
    root = os.path.realpath(target)
    path = os.path.abspath(os.path.join(target, rel))
    parent = os.path.realpath(os.path.dirname(path))
    if parent != root and not parent.startswith(root + os.sep):
        raise InstallError("manifest path escapes target: %s" % rel)
    if os.path.lexists(path) and os.path.islink(path):
        raise InstallError("refusing to replace symlink: %s" % rel)
    current = root
    for part in rel.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise InstallError("refusing symlink parent: %s" % rel)
    return path


def validate_relative(rel):
    if (not isinstance(rel, str) or "\\" in rel or "\x00" in rel
            or any(part in ("", ".", "..") for part in rel.split("/"))):
        raise InstallError("unsafe package/target path")
    return rel


class HeldTree(object):
    """Descriptor-contained reads and writes; no symlink ancestor traversal.

    Cooperating installers serialize on the held root inode. A non-cooperating
    editor is detected by snapshot comparison; changed content is not rolled back
    over. Equally privileged post-completion mutation is outside this guarantee.
    """
    def __init__(self, root):
        self.input = os.path.abspath(root)
        self.root = os.path.realpath(root)
        self.dirs = {"": os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)}
        self.bindings = []
        self.created_dirs = []
        self.temporary_names = {}

    def close(self):
        for fd in reversed(list(self.dirs.values())):
            os.close(fd)
        self.dirs = {}

    def lock(self):
        try:
            fcntl.flock(self.dirs[""], fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise InstallError("another installer holds this target; retry after it finishes")

    @staticmethod
    def identity(info):
        return info.st_dev, info.st_ino

    def check(self):
        if (os.path.realpath(self.input) != self.root
                or self.identity(os.stat(self.root, follow_symlinks=False))
                != self.identity(os.fstat(self.dirs[""]))):
            raise InstallError("target root changed during transaction")
        for parent, name, fd in self.bindings:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or self.identity(info) != self.identity(os.fstat(fd)):
                raise InstallError("target parent changed during transaction")

    def parent(self, rel, create=False):
        parts = validate_relative(rel).split("/")
        prefix = ""
        fd = self.dirs[""]
        self.check()
        for name in parts[:-1]:
            prefix = prefix + "/" + name if prefix else name
            if prefix not in self.dirs:
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        return None, parts[-1]
                    self.check()
                    os.mkdir(name, 0o700 if rel.startswith(BACKUP_ROOT_REL + "/") else 0o755, dir_fd=fd)
                    os.fsync(fd)
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    self.created_dirs.append(prefix)
                self.dirs[prefix] = child
                self.bindings.append((fd, name, child))
            fd = self.dirs[prefix]
        self.check()
        return fd, parts[-1]

    def snapshot(self, rel):
        fd, name = self.parent(rel)
        if fd is None:
            return {"kind": "absent"}
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"kind": "absent"}
        value = {"mode": stat.S_IMODE(info.st_mode), "identity": self.identity(info)}
        if stat.S_ISLNK(info.st_mode):
            value.update(kind="symlink", data=os.readlink(name, dir_fd=fd).encode("utf-8"))
        elif stat.S_ISREG(info.st_mode):
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(source, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if self.identity(opened) != self.identity(info):
                    raise InstallError("target file changed while opening: %s" % rel)
                data = handle.read(64 * 1024 * 1024 + 1)
                after = os.fstat(handle.fileno())
            fields = lambda s: (s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
            if len(data) > 64 * 1024 * 1024 or fields(opened) != fields(after):
                raise InstallError("target file changed or exceeds transaction budget: %s" % rel)
            value.update(kind="file", data=data)
        elif stat.S_ISDIR(info.st_mode):
            value.update(kind="directory")
        else:
            raise InstallError("unsupported target file type: %s" % rel)
        final = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if self.identity(final) != self.identity(info):
            raise InstallError("target leaf changed during read: %s" % rel)
        self.check()
        return value

    @staticmethod
    def matches(actual, expected, identity=False):
        keys = ["kind", "data"]
        if expected.get("kind") == "file":
            keys.append("mode")
        if identity and "identity" in expected:
            keys.append("identity")
        return all(actual.get(key) == expected.get(key) for key in keys)

    def write(self, rel, desired, expected):
        if not self.matches(self.snapshot(rel), expected, identity=True):
            raise InstallError("target changed since preflight: %s" % rel)
        fd, name = self.parent(rel, create=desired["kind"] != "absent")
        if fd is None:
            return
        temp = self.temporary_names.get(rel) or ".amplai-install-" + uuid.uuid4().hex
        allocated = False
        try:
            if desired["kind"] == "file":
                handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 desired["mode"], dir_fd=fd)
                allocated = True
                with os.fdopen(handle, "wb") as stream:
                    stream.write(desired["data"])
                    stream.flush()
                    os.fchmod(stream.fileno(), desired["mode"])
                    os.fsync(stream.fileno())
            elif desired["kind"] == "symlink":
                os.symlink(desired["data"].decode("utf-8"), temp, dir_fd=fd)
                allocated = True
            elif desired["kind"] != "absent":
                raise InstallError("unsupported transaction action")
            self.check()
            if not self.matches(self.snapshot(rel), expected, identity=True):
                raise InstallError("target changed at publication boundary: %s" % rel)
            if desired["kind"] == "absent":
                if expected["kind"] != "absent":
                    os.unlink(name, dir_fd=fd)
            elif expected["kind"] == "absent":
                os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                os.unlink(temp, dir_fd=fd)
                allocated = False
            else:
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
                allocated = False
            os.fsync(fd)
            self.check()
            if not self.matches(self.snapshot(rel), desired):
                raise InstallError("target failed final verification: %s" % rel)
        finally:
            if allocated:
                os.unlink(temp, dir_fd=fd)


def package_snapshot():
    """Read and bind every consumed byte before planning any target mutation.

    Checksums provide integrity, not a signature against an untrusted publisher.
    Subsequent consumers use only this immutable byte snapshot, including Store files.
    """
    files = {}
    tree = HeldTree(PACKAGE_ROOT)
    def visit(prefix, fd):
        for name in sorted(os.listdir(fd)):
            rel = validate_relative(prefix + name)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if name != "__pycache__":
                    child, _ = tree.parent(rel + "/.inventory")
                    visit(rel + "/", child)
            elif stat.S_ISREG(info.st_mode):
                member = tree.snapshot(rel)
                if member["kind"] != "file":
                    raise InstallError("package member changed type")
                files[rel] = member["data"]
            else:
                raise InstallError("package member is not a regular file/directory: %s" % rel)
    try:
        visit("", tree.dirs[""])
        tree.check()
    finally:
        tree.close()
    if "CHECKSUMS.sha256" not in files:
        raise InstallError("package lacks complete checksum inventory")
    declared = {}
    try:
        for line in files["CHECKSUMS.sha256"].decode("utf-8").splitlines():
            digest, rel = line.split("  ", 1)
            validate_relative(rel)
            if (not re.match(r"^[0-9a-f]{64}$", digest) or rel in declared
                    or rel == "CHECKSUMS.sha256"):
                raise ValueError("invalid or duplicate checksum")
            declared[rel] = digest
    except (ValueError, UnicodeError):
        raise InstallError("package checksum inventory is malformed")
    if set(declared) != set(files) - {"CHECKSUMS.sha256"}:
        raise InstallError("package checksum inventory has missing or unlisted members")
    for rel, digest in declared.items():
        if hashlib.sha256(files[rel]).hexdigest() != digest:
            raise InstallError("package checksum mismatch: %s" % rel)
    return files


def section_from_text(text, begin, end, label):
    begin_count = text.count(begin)
    end_count = text.count(end)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise InstallError("malformed managed markers in %s" % label)
    start = text.index(begin)
    finish = text.index(end, start) + len(end)
    return text[start:finish]


def merge_section(current, fragment, begin, end, label):
    desired = section_from_text(fragment, begin, end, "fragment %s" % label)
    if desired is None:
        raise InstallError("fragment lacks managed markers: %s" % label)
    existing = section_from_text(current, begin, end, label)
    if existing is None:
        separator = "" if not current else ("\n" if current.endswith("\n") else "\n\n")
        return current + separator + desired + "\n", None, desired
    start = current.index(begin)
    finish = current.index(end, start) + len(end)
    merged = current[:start] + desired + current[finish:]
    return merged, existing, desired


def strip_section(text, begin, end):
    """Remove one managed marker block, leaving the rest of the file intact."""
    if begin not in text or end not in text:
        return text, False
    start = text.index(begin)
    finish = text.index(end, start) + len(end)
    stripped = text[:start].rstrip() + "\n" + text[finish:].lstrip("\n")
    if not stripped.strip():
        return "", True
    return stripped, True


def remove_hooks(settings, hooks):
    result = copy.deepcopy(settings)
    root = result.get("hooks") or {}
    commands = set(item["command"] for item in hooks)
    for event in list(root):
        wrappers = []
        for wrapper in root[event]:
            kept = [
                hook for hook in wrapper.get("hooks", [])
                if not (hook.get("type") == "command" and hook.get("command") in commands)
            ]
            if kept:
                wrapper = dict(wrapper, hooks=kept)
                wrappers.append(wrapper)
            elif not wrapper.get("hooks"):
                wrappers.append(wrapper)
        if wrappers:
            root[event] = wrappers
        else:
            del root[event]
    if root:
        result["hooks"] = root
    else:
        result.pop("hooks", None)
    return result


def find_array_span(text, key):
    """Byte span of the array value for `key`, or None.

    Returns (open_index, close_index) where close_index points at the closing
    bracket.  String contents and escapes are respected so a bracket inside a
    value never ends the scan.
    """
    needle = '"%s"' % key
    start = text.find(needle)
    if start < 0:
        return None
    cursor = text.find("[", start + len(needle))
    if cursor < 0:
        return None
    between = text[start + len(needle):cursor]
    if between.strip() not in (":",):
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(cursor, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return (cursor, index)
    return None


def append_to_json_array(text, key, rendered_entries):
    """Append entries to a JSON array without reformatting the rest of the file.

    The AMPLAI policy fragment only ever adds rules, and the target files are
    hand-wrapped by their authors.  Re-dumping the whole document would rewrite
    every unrelated line, so the managed entries are spliced in textually.
    """
    if not rendered_entries:
        return text
    span = find_array_span(text, key)
    if span is None:
        return None
    open_index, close_index = span
    body = text[open_index + 1:close_index]
    close_line_start = text.rfind("\n", 0, close_index)
    closing_indent = text[close_line_start + 1:close_index]
    if closing_indent.strip():
        closing_indent = ""
    item_indent = closing_indent + "  "
    if body.strip():
        prefix = body.rstrip()
        if not prefix.endswith(","):
            prefix += ","
        joined = prefix + "\n" + "\n".join(
            indent_block(entry, item_indent) + ("," if position < len(rendered_entries) - 1 else "")
            for position, entry in enumerate(rendered_entries)
        ) + "\n" + closing_indent
    else:
        joined = "\n" + "\n".join(
            indent_block(entry, item_indent) + ("," if position < len(rendered_entries) - 1 else "")
            for position, entry in enumerate(rendered_entries)
        ) + "\n" + closing_indent
    return text[:open_index + 1] + joined + text[close_index:]


def indent_block(text, indent):
    lines = text.split("\n")
    return "\n".join(indent + line if line else line for line in lines)


def hook_present(settings, event, command):
    for wrapper in (settings.get("hooks") or {}).get(event, []):
        for hook in wrapper.get("hooks", []):
            if hook.get("type") == "command" and hook.get("command") == command:
                return True
    return False


def merge_hooks(settings, hooks):
    result = copy.deepcopy(settings)
    root = result.setdefault("hooks", {})
    for item in hooks:
        event = item["event"]
        command = item["command"]
        if hook_present(result, event, command):
            continue
        root.setdefault(event, []).append({
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": int(item.get("timeout", 5)),
            }]
        })
    return result


class Action(object):
    def __init__(self, rel, data, mode=0o644, reason="update", kind="file"):
        self.rel = rel
        self.data = data
        self.mode = mode
        self.reason = reason
        self.kind = kind

    @property
    def new_hash(self):
        prefix = b"symlink\x00" if self.kind == "symlink" else b""
        return sha256_bytes(prefix + self.data)


class Installer(object):
    def __init__(self, args):
        self.args = args
        self.target = os.path.abspath(os.path.expanduser(args.target))
        self.package = package_snapshot()
        self.manifest = json.loads(self.package["manifest.json"].decode("utf-8"))
        self.state_path = os.path.join(self.target, STATE_REL)
        self.tree = HeldTree(self.target)
        self.before = {STATE_REL: self.tree.snapshot(STATE_REL)}
        self.before[JOURNAL_REL] = self.tree.snapshot(JOURNAL_REL)
        self.journal = None
        initial_state = self.before[STATE_REL]
        if initial_state["kind"] not in ("absent", "file"):
            raise InstallError("install record must be a regular file")
        self.state = (json.loads(initial_state["data"].decode("utf-8"))
                      if initial_state["kind"] == "file" else {})
        if self.state:
            verify_seal(self.state, "existing install record")
        self.bootstrap = bool(getattr(args, "bootstrap_baseline", False) or self.state.get("baseline"))
        if getattr(args, "repo_profile", None) and not self.bootstrap:
            raise InstallError("--repo-profile requires --bootstrap-baseline or an installed baseline")
        self.baseline = (json.loads(self.package["baseline/manifest.json"].decode("utf-8"))
                         if self.bootstrap else {})
        self.baseline_receipt = copy.deepcopy(self.state.get("baseline") or {
            "profile": "generic", "files": {}, "mirrors": {},
        })
        self.baseline_paths = set(item["path"] for item in self.baseline.get("files", []))
        self.baseline_paths.update(self.baseline_receipt.get("files", {}))
        self.actions = []
        self.conflicts = []
        self.notes = []
        self.backup_dir = None
        self.project_store_home = None
        self.project_store_created = False
        self.removals = []
        # Paths this run brings into existence, for a symmetric uninstall.
        self.created_paths = set()
        self.borrowed_owned = set(self.state.get("borrowed_owned_files") or [])
        self.borrowed_markers = set(self.state.get("borrowed_marker_sections") or [])
        self.borrowed_json = copy.deepcopy(self.state.get("borrowed_json") or {})
        paths = {item["path"] for key in ("owned_files", "markers", "json_merges")
                 for item in self.manifest.get(key, [])}
        paths.update(item["path"] for key in ("files", "markers", "mirrors")
                     for item in self.baseline.get(key, []))
        paths.update([".claude/settings.json", ".codex/hooks.json", ".ai-team/app.json",
                      ".ai-team/local/project.json"])
        paths.update(self.state.get("owned_files") or {})
        paths.update(self.baseline_receipt.get("files", {}))
        paths.update(self.baseline_receipt.get("mirrors", {}))
        for rel in sorted(paths):
            self.before[rel] = self.tree.snapshot(rel)

    def __del__(self):
        tree = getattr(self, "tree", None)
        if tree and tree.dirs:
            tree.close()

    def package_bytes(self, rel):
        validate_relative(rel)
        if rel not in self.package:
            raise InstallError("package member not in sealed snapshot: %s" % rel)
        return self.package[rel]

    def marker_items(self):
        return self.manifest.get("markers", []) + self.baseline.get("markers", [])

    def fragment_bytes(self, item):
        prefix = "baseline/" if item in self.baseline.get("markers", []) else ""
        return self.package_bytes(prefix + "fragments/" + item["fragment"])

    def validate_package(self):
        if self.manifest.get("runtime_protocol") != PROTOCOL:
            raise InstallError("unsupported package protocol")
        for item in self.manifest.get("owned_files", []):
            if sha256_bytes(self.package_bytes("payload/" + item["path"])) != item["sha256"]:
                raise InstallError("payload hash mismatch: %s" % item["path"])
        for item in self.marker_items():
            validate_relative(item["path"])
            if sha256_bytes(self.fragment_bytes(item)) != item["sha256"]:
                raise InstallError("fragment hash mismatch: %s" % item["fragment"])
        for item in self.manifest.get("json_merges", []):
            if sha256_bytes(self.fragment_bytes(item)) != item["sha256"]:
                raise InstallError("JSON merge fragment hash mismatch: %s" % item["fragment"])
        if self.bootstrap:
            if self.baseline.get("profile") != "generic" or self.baseline_receipt.get("profile") != "generic":
                raise InstallError("unsupported baseline repository profile")
            declared = [validate_relative(item["path"]) for item in self.baseline["files"]]
            actual = {rel[len("baseline/payload/"):] for rel in self.package
                      if rel.startswith("baseline/payload/")}
            if len(set(declared)) != len(declared) or set(declared) != actual:
                raise InstallError("package baseline inventory is incomplete or duplicated")
            for item in self.baseline["files"]:
                if sha256_bytes(self.package_bytes("baseline/payload/" + item["path"])) != item["sha256"]:
                    raise InstallError("package baseline hash mismatch")
            skills = self.baseline.get("required_skills", [])
            mirrors = self.baseline.get("mirrors", [])
            expected = {".claude/skills/" + name: "../../.agents/skills/" + name for name in skills}
            if (len(skills) != 12 or len(set(skills)) != 12 or len(mirrors) != 12
                    or {item["path"]: item["target"] for item in mirrors} != expected):
                raise InstallError("package baseline mirror inventory is not exact")

    @staticmethod
    def _version_key(value):
        parts = []
        for chunk in str(value or "0").split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            parts.append(int(digits or 0))
        return tuple(parts)

    def validate_target(self):
        if not os.path.isdir(self.target):
            raise InstallError("target is not a directory: %s" % self.target)
        installed = self.state.get("package_version")
        if installed and not self.args.force:
            if self._version_key(installed) > self._version_key(self.manifest["version"]):
                raise InstallError(
                    "target already has kit %s, which is newer than this package %s; "
                    "use --force to downgrade" % (installed, self.manifest["version"])
                )
        for rel in self.manifest.get("required_paths", []):
            if rel not in self.baseline_paths and not os.path.exists(os.path.join(self.target, rel)):
                raise InstallError("target is not an AMPLAI Loop V2 app; missing %s" % rel)
        if self.args.project_home:
            home = os.path.abspath(os.path.expanduser(self.args.project_home))
            project_path = os.path.join(home, "project.json")
            if os.path.exists(project_path):
                project = read_json(project_path)
                verify_seal(project, "existing Project Store")
                if project.get("project_id") != self.args.project_id:
                    raise InstallError(
                        "Project Store id is %s, not %s" %
                        (project.get("project_id"), self.args.project_id)
                    )

    def add_action(self, action):
        validate_relative(action.rel)
        if any(existing.rel == action.rel for existing in self.actions):
            raise InstallError("duplicate install action: %s" % action.rel)
        self.actions.append(action)

    def plan_owned_files(self):
        previous = self.state.get("owned_files") or {}
        for item in self.manifest.get("owned_files", []):
            rel = item["path"]
            data = self.package_bytes("payload/" + rel)
            dest = safe_destination(self.target, rel)
            if os.path.exists(dest):
                current = sha256_file(dest)
                if current == item["sha256"]:
                    if rel not in previous:
                        self.borrowed_owned.add(rel)
                    continue
                old_installed = previous.get(rel)
                if old_installed and current == old_installed:
                    self.add_action(Action(rel, data, int(item.get("mode", "644"), 8), "package update"))
                elif self.args.force:
                    self.add_action(Action(rel, data, int(item.get("mode", "644"), 8), "forced replacement"))
                else:
                    self.conflicts.append(
                        "%s has local modifications (current %s, expected previous %s)" %
                        (rel, current, old_installed or "unmanaged")
                    )
            else:
                self.add_action(Action(rel, data, int(item.get("mode", "644"), 8), "new package file"))

    def composed_baseline(self, item):
        """Compose whole-file baseline and async extension before scheduling it."""
        rel = item["path"]
        original = self.package_bytes("baseline/payload/" + rel)
        data = original
        for marker in self.manifest.get("markers", []):
            if marker["path"] == rel:
                data = merge_section(data.decode("utf-8"), self.fragment_bytes(marker).decode("utf-8"),
                                     marker["begin"], marker["end"], rel)[0].encode("utf-8")
        for merge in self.manifest.get("json_merges", []):
            if merge["path"] != rel:
                continue
            current = json.loads(data.decode("utf-8"))
            extension = json.loads(self.fragment_bytes(merge).decode("utf-8"))
            for key in ("path_rules", "forbidden_automatic_actions"):
                for value in extension.get(key, []):
                    if value not in current.setdefault(key, []):
                        current[key].append(value)
            data = (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return original, data

    def plan_baseline(self):
        if not self.bootstrap:
            return
        for item in self.baseline["files"]:
            rel = item["path"]
            original, data = self.composed_baseline(item)
            dest = safe_destination(self.target, rel)
            previous = self.baseline_receipt["files"].get(rel)
            mode = int(item.get("mode", "644"), 8)
            desired_hash = sha256_bytes(data)
            if os.path.exists(dest):
                if not os.path.isfile(dest):
                    raise InstallError("baseline target is not a regular file: %s" % rel)
                current = read_bytes(dest)
                old_mode = stat.S_IMODE(os.stat(dest).st_mode)
                if previous:
                    if (sha256_bytes(current) != previous["installed_hash"]
                            or old_mode != previous["mode"]):
                        if not self.args.force:
                            self.conflicts.append("baseline file has local modifications: %s" % rel)
                            continue
                    if not previous["created"] and current != data:
                        self.conflicts.append("borrowed baseline file requires explicit reconciliation: %s" % rel)
                        continue
                    receipt = copy.deepcopy(previous)
                    # Borrowed permissions belong to the local owner, including
                    # mode drift accepted through the existing explicit force path.
                    # Only installer-created files adopt package mode updates.
                    if not previous["created"]:
                        mode = old_mode
                elif current == data:
                    # Identical user files are usable but are never adopted for deletion.
                    receipt = {"created": False, "restore": None}
                    mode = old_mode
                elif current == original:
                    # Only known public package bytes are stored for restoring the extension.
                    receipt = {"created": False, "restore": base64.b64encode(original).decode("ascii")}
                    mode = old_mode
                else:
                    self.conflicts.append("unmanaged baseline file has local content: %s" % rel)
                    continue
                if current != data or old_mode != mode:
                    self.add_action(Action(rel, data, mode, "composed baseline update"))
            else:
                receipt = {"created": True, "restore": None}
                self.add_action(Action(rel, data, mode, "composed baseline file"))
            receipt.update(installed_hash=desired_hash, mode=mode)
            self.baseline_receipt["files"][rel] = receipt
        for item in self.baseline["mirrors"]:
            rel, link = item["path"], item["target"]
            # The exact target is validated above; parent symlinks remain forbidden.
            dest = os.path.join(self.target, validate_relative(rel))
            previous = self.baseline_receipt["mirrors"].get(rel)
            if os.path.lexists(dest):
                if not os.path.islink(dest) or os.readlink(dest) != link:
                    self.conflicts.append("baseline mirror conflicts with local path: %s" % rel)
                    continue
                receipt = previous or {"created": False, "target": link}
            else:
                receipt = {"created": True, "target": link}
                self.add_action(Action(rel, link.encode("utf-8"), reason="exact baseline mirror", kind="symlink"))
            self.baseline_receipt["mirrors"][rel] = receipt

    def plan_markers(self):
        previous = self.state.get("marker_sections") or {}
        for item in self.marker_items():
            rel = item["path"]
            if rel in self.baseline_paths:
                continue
            dest = safe_destination(self.target, rel)
            if not os.path.exists(dest) and item.get("required", False):
                raise InstallError("required marker target is missing: %s" % rel)
            if (not os.path.exists(dest) and not item.get("required", False)
                    and not item.get("create_if_missing", False)):
                continue
            if not os.path.exists(dest):
                self.created_paths.add(rel)
            current = (
                read_text(dest) if os.path.exists(dest)
                else item.get("create_header", "")
            )
            fragment = self.fragment_bytes(item).decode("utf-8")
            merged, existing, desired = merge_section(
                current, fragment, item["begin"], item["end"], rel,
            )
            desired_hash = sha256_bytes(desired.encode("utf-8"))
            if existing is not None:
                current_hash = sha256_bytes(existing.encode("utf-8"))
                old_hash = previous.get(rel)
                if not old_hash and current_hash == desired_hash:
                    self.borrowed_markers.add(rel)
                if current_hash != desired_hash and old_hash and current_hash != old_hash and not self.args.force:
                    self.conflicts.append("managed section was locally modified: %s" % rel)
                    continue
                if current_hash != desired_hash and not old_hash and not self.args.force:
                    self.conflicts.append("unmanaged AMPLAI marker differs from package: %s" % rel)
                    continue
            if merged != current:
                mode = stat.S_IMODE(os.stat(dest).st_mode) if os.path.exists(dest) else 0o644
                self.add_action(Action(rel, merged.encode("utf-8"), mode, "managed section merge"))

    def plan_claude_settings(self):
        rel = ".claude/settings.json"
        dest = safe_destination(self.target, rel)
        if not os.path.exists(dest):
            self.created_paths.add(rel)
        settings = read_json(dest) if os.path.exists(dest) else {}
        merged = merge_hooks(settings, self.manifest.get("claude_hooks", []))
        if merged != settings:
            data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            mode = stat.S_IMODE(os.stat(dest).st_mode) if os.path.exists(dest) else 0o644
            self.add_action(Action(rel, data, mode, "additive Claude hook merge"))

    def plan_codex_hooks(self):
        rel = ".codex/hooks.json"
        dest = safe_destination(self.target, rel)
        if not os.path.exists(dest):
            self.created_paths.add(rel)
        settings = read_json(dest) if os.path.exists(dest) else {}
        merged = merge_hooks(settings, self.manifest.get("codex_hooks", []))
        if merged != settings:
            data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            mode = stat.S_IMODE(os.stat(dest).st_mode) if os.path.exists(dest) else 0o644
            self.add_action(Action(rel, data, mode, "additive Codex hook merge"))
        if self.manifest.get("codex_hooks"):
            note = (
                "Codex project-local hooks require review/trust with `/hooks` before "
                "they run; the installer never bypasses hook trust"
            )
            if note not in self.notes:
                self.notes.append(note)

    def plan_json_merges(self):
        previous = self.state.get("json_objects") or {}
        for item in self.manifest.get("json_merges", []):
            rel = item["path"]
            if rel in self.baseline_paths:
                continue
            dest = safe_destination(self.target, rel)
            if not os.path.isfile(dest):
                if item.get("required", True):
                    raise InstallError("required JSON merge target is missing: %s" % rel)
                continue
            current = read_json(dest)
            extension = json.loads(self.fragment_bytes(item).decode("utf-8"))
            merged = copy.deepcopy(current)
            rules = merged.setdefault("path_rules", [])
            previous_path = previous.get(rel) or {}
            borrowed = self.borrowed_json.setdefault(rel, {"rules": [], "prohibitions": []})
            for desired in extension.get("path_rules", []):
                rule_id = desired.get("id")
                if not rule_id:
                    raise InstallError("policy extension path rule lacks id")
                index = next((i for i, rule in enumerate(rules) if rule.get("id") == rule_id), None)
                desired_hash = sha256_bytes(canonical_json(desired))
                if index is None:
                    rules.append(copy.deepcopy(desired))
                    continue
                existing = rules[index]
                existing_hash = sha256_bytes(canonical_json(existing))
                old_hash = previous_path.get(rule_id)
                if not old_hash and existing_hash == desired_hash and rule_id not in borrowed["rules"]:
                    borrowed["rules"].append(rule_id)
                if existing_hash != desired_hash:
                    if old_hash and existing_hash != old_hash and not self.args.force:
                        self.conflicts.append("managed JSON rule was locally modified: %s#%s" % (rel, rule_id))
                        continue
                    if not old_hash and not self.args.force:
                        self.conflicts.append("unmanaged JSON rule differs from package: %s#%s" % (rel, rule_id))
                        continue
                    rules[index] = copy.deepcopy(desired)
            forbidden = merged.setdefault("forbidden_automatic_actions", [])
            for value in extension.get("forbidden_automatic_actions", []):
                if (value in forbidden and rel not in (self.state.get("json_objects") or {})
                        and value not in borrowed["prohibitions"]):
                    borrowed["prohibitions"].append(value)
                if value not in forbidden:
                    forbidden.append(value)
            if merged == current:
                continue
            mode = stat.S_IMODE(os.stat(dest).st_mode)
            text = read_text(dest)
            spliced = self._splice_json_additions(text, current, merged)
            if spliced is not None:
                self.add_action(Action(rel, spliced.encode("utf-8"), mode,
                                       "append managed JSON rules"))
            else:
                data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                self.notes.append(
                    "%s was rewritten with standard JSON formatting because the "
                    "managed section changed in place" % rel
                )
                self.add_action(Action(rel, data, mode, "structured JSON policy merge"))

    @staticmethod
    def _splice_json_additions(text, current, merged):
        """Textual append when the merge only added entries; else None."""
        current_rules = current.get("path_rules") or []
        merged_rules = merged.get("path_rules") or []
        if merged_rules[:len(current_rules)] != current_rules:
            return None
        new_rules = merged_rules[len(current_rules):]
        current_forbidden = current.get("forbidden_automatic_actions") or []
        merged_forbidden = merged.get("forbidden_automatic_actions") or []
        if merged_forbidden[:len(current_forbidden)] != current_forbidden:
            return None
        new_forbidden = merged_forbidden[len(current_forbidden):]
        rest_current = dict(
            (key, value) for key, value in current.items()
            if key not in ("path_rules", "forbidden_automatic_actions")
        )
        rest_merged = dict(
            (key, value) for key, value in merged.items()
            if key not in ("path_rules", "forbidden_automatic_actions")
        )
        if rest_current != rest_merged:
            return None
        result = text
        if new_rules:
            result = append_to_json_array(result, "path_rules", [
                json.dumps(rule, ensure_ascii=False, indent=2, sort_keys=True)
                for rule in new_rules
            ])
            if result is None:
                return None
        if new_forbidden:
            result = append_to_json_array(result, "forbidden_automatic_actions", [
                json.dumps(value, ensure_ascii=False) for value in new_forbidden
            ])
            if result is None:
                return None
        try:
            if json.loads(result) != merged:
                return None
        except ValueError:
            return None
        return result

    def plan_identity(self):
        rel = ".ai-team/app.json"
        dest = safe_destination(self.target, rel)
        value = seal({
            "schema_version": "1.0",
            "kind": "app_identity",
            "runtime_protocol": PROTOCOL,
            "project_id": self.args.project_id,
            "app_id": self.args.app_id,
        })
        data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if os.path.exists(dest):
            existing = read_json(dest)
            verify_seal(existing, "existing app identity")
            if (existing.get("project_id"), existing.get("app_id")) != (self.args.project_id, self.args.app_id):
                if not self.args.force:
                    self.conflicts.append("existing .ai-team/app.json belongs to another project/app")
                    return
            else:
                # App-owned identity extension fields and layout are not installer content.
                return
        self.add_action(Action(rel, data, 0o644, "app identity"))

    def plan_local_binding(self):
        if not self.args.project_home:
            return
        rel = ".ai-team/local/project.json"
        dest = safe_destination(self.target, rel)
        existing = read_json(dest) if os.path.exists(dest) else None
        if existing:
            try:
                verify_seal(existing, "existing local Project Store binding")
            except InstallError:
                if not self.args.force:
                    raise
                existing = None
        desired_fields = {
            "schema_version": "1.0",
            "kind": "local_project_binding",
            "runtime_protocol": PROTOCOL,
            "project_id": self.args.project_id,
            "app_id": self.args.app_id,
            "project_home": os.path.abspath(os.path.expanduser(self.args.project_home)),
        }
        if existing and all(existing.get(key) == value for key, value in desired_fields.items()):
            return
        desired_fields["updated_at"] = utc_now()
        value = seal(desired_fields)
        data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.add_action(Action(rel, data, 0o600, "host-local Project Store binding"))

    def final_file_hash(self, rel):
        for action in self.actions:
            if action.rel == rel:
                return action.new_hash
        path = os.path.join(self.target, rel)
        return sha256_file(path) if os.path.exists(path) else None

    def plan_state(self):
        owned = dict(self.state.get("owned_files") or {})
        owned.update({item["path"]: item["sha256"] for item in self.manifest.get("owned_files", [])
                      if item["path"] not in self.borrowed_owned})
        markers = {}
        for item in self.marker_items():
            if item["path"] in self.borrowed_markers:
                continue
            fragment = self.fragment_bytes(item).decode("utf-8")
            desired = section_from_text(fragment, item["begin"], item["end"], item["path"])
            markers[item["path"]] = sha256_bytes(desired.encode("utf-8"))
        json_objects = {}
        for item in self.manifest.get("json_merges", []):
            extension = json.loads(self.fragment_bytes(item).decode("utf-8"))
            json_objects[item["path"]] = {
                rule["id"]: sha256_bytes(canonical_json(rule))
                for rule in extension.get("path_rules", [])
                if rule["id"] not in self.borrowed_json.get(item["path"], {}).get("rules", [])
            }
        previous_installed_at = self.state.get("installed_at")
        # Remember which paths this kit brought into existence.  Without it an
        # uninstall cannot tell "the app had this before" from "we created it",
        # and has to leave both behind.
        created = sorted(set(self.state.get("created_paths") or []) | self.created_paths)
        directories = set(self.state.get("created_directories") or [])
        for action in self.actions + [Action(STATE_REL, b"")]:
            parts = action.rel.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                directory = "/".join(parts[:index])
                if directory not in self.tree.dirs:
                    directories.add(directory)
        identities = dict(self.state.get("identity_ownership") or {})
        for rel in (".ai-team/app.json", ".ai-team/local/project.json"):
            if rel not in identities:
                identities[rel] = self.before[rel]["kind"] == "absent"
        hook_ownership = {}
        for key, rel in (("claude_hooks", ".claude/settings.json"), ("codex_hooks", ".codex/hooks.json")):
            before = self.before[rel]
            settings = json.loads(before["data"].decode("utf-8")) if before["kind"] == "file" else {}
            previous_hooks = self.state.get(key) or []
            hook_ownership[key] = [item for item in self.manifest.get(key, [])
                                   if item in previous_hooks or not hook_present(settings, item["event"], item["command"])]
        state = {
            "schema_version": "1.0",
            "kind": "amplai_loop_kit_install",
            "package_version": self.manifest["version"],
            "package_sha256": sha256_bytes(self.package["CHECKSUMS.sha256"]),
            "runtime_protocol": PROTOCOL,
            "project_id": self.args.project_id,
            "app_id": self.args.app_id,
            "installed_at": previous_installed_at or utc_now(),
            "owned_files": owned,
            "marker_sections": markers,
            "json_objects": json_objects,
            "claude_hooks": hook_ownership["claude_hooks"],
            "codex_hooks": hook_ownership["codex_hooks"],
            "created_paths": created,
            "created_directories": sorted(directories),
            "identity_ownership": identities,
            "borrowed_owned_files": sorted(self.borrowed_owned),
            "borrowed_marker_sections": sorted(self.borrowed_markers),
            "borrowed_json": self.borrowed_json,
        }
        if self.bootstrap:
            state["baseline"] = self.baseline_receipt
        data = (json.dumps(seal(state), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.actions = [action for action in self.actions if action.rel != STATE_REL]
        if not os.path.exists(self.state_path) or read_bytes(self.state_path) != data:
            self.add_action(Action(STATE_REL, data, 0o644, "install record"))

    def plan_uninstall(self):
        """Remove what this kit installed, leaving the central store alone."""
        if not self.state:
            raise InstallError("no install record at %s; nothing to uninstall" % STATE_REL)
        owned = self.state.get("owned_files") or {}
        created = set(self.state.get("created_paths") or [])
        removals = []
        for rel, installed_hash in sorted(owned.items()):
            dest = safe_destination(self.target, rel)
            if not os.path.exists(dest):
                continue
            if sha256_file(dest) != installed_hash and not self.args.force:
                self.conflicts.append(
                    "%s changed since install; use --force to remove it anyway" % rel
                )
                continue
            removals.append(rel)
        for item in self.marker_items():
            rel = item["path"]
            if rel in self.baseline_paths or rel in self.borrowed_markers:
                continue
            dest = safe_destination(self.target, rel)
            if not os.path.exists(dest):
                continue
            current = read_text(dest)
            section = section_from_text(current, item["begin"], item["end"], rel)
            installed = (self.state.get("marker_sections") or {}).get(rel)
            if section is not None and (not installed or sha256_bytes(section.encode("utf-8")) != installed):
                if not self.args.force:
                    self.conflicts.append("managed section changed since install: %s" % rel)
                    continue
            stripped, found = strip_section(current, item["begin"], item["end"])
            if not found or stripped == current:
                continue
            if stripped == "":
                removals.append(rel)
                continue
            if rel in created and not self._only_kit_header(rel, stripped, item):
                # We created the file, but the application has written into it
                # since.  Deleting it would take their content with ours, so
                # strip our section and leave the rest.
                self.notes.append(
                    "%s was created by this kit but now has content outside the "
                    "managed section; the section was removed and the file kept" % rel
                )
            elif rel in created:
                # The kit brought this file into existence and nothing but our
                # own header remains, so leaving it would make an uninstall
                # look incomplete.
                removals.append(rel)
                continue
            mode = stat.S_IMODE(os.stat(dest).st_mode)
            self.add_action(Action(rel, stripped.encode("utf-8"), mode, "strip managed section"))
        for item in self.manifest.get("json_merges", []):
            rel = item["path"]
            if rel in self.baseline_paths:
                continue
            dest = safe_destination(self.target, rel)
            if not os.path.isfile(dest):
                continue
            current = read_json(dest)
            extension = json.loads(self.fragment_bytes(item).decode("utf-8"))
            merged = copy.deepcopy(current)
            recorded = (self.state.get("json_objects") or {}).get(rel, {})
            owned_ids = set(recorded)
            for rule in current.get("path_rules", []):
                if rule.get("id") in recorded and sha256_bytes(canonical_json(rule)) != recorded[rule["id"]]:
                    if not self.args.force:
                        self.conflicts.append("managed JSON rule changed since install: %s" % rel)
            merged["path_rules"] = [
                rule for rule in merged.get("path_rules", [])
                if rule.get("id") not in owned_ids
            ]
            owned_prohibitions = set(extension.get("forbidden_automatic_actions", [])) - set(
                self.borrowed_json.get(rel, {}).get("prohibitions", []))
            merged["forbidden_automatic_actions"] = [
                value for value in merged.get("forbidden_automatic_actions", [])
                if value not in owned_prohibitions
            ]
            if merged != current:
                # Install splices additions in textually to keep the rest of
                # the file byte-identical; removal has no such counterpart yet,
                # so the file comes back with standard formatting.  The content
                # is exactly the pre-install content -- only the layout differs.
                data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                self.notes.append(
                    "%s was rewritten with standard JSON formatting; the managed rules "
                    "are gone and the content matches the pre-install state, but the "
                    "original line layout is not restored" % rel
                )
                self.add_action(Action(rel, data, stat.S_IMODE(os.stat(dest).st_mode),
                                       "remove managed JSON rules"))
        settings_rel = ".claude/settings.json"
        settings_dest = safe_destination(self.target, settings_rel)
        if os.path.exists(settings_dest):
            settings = read_json(settings_dest)
            merged = remove_hooks(settings, self.state.get("claude_hooks") or [])
            if settings_rel in created and not merged:
                # We created this file and removing our hooks empties it.
                removals.append(settings_rel)
            elif merged != settings:
                data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                self.add_action(Action(settings_rel, data,
                                       stat.S_IMODE(os.stat(settings_dest).st_mode),
                                       "remove AMPLAI hooks"))
        codex_rel = ".codex/hooks.json"
        codex_dest = safe_destination(self.target, codex_rel)
        if os.path.exists(codex_dest):
            settings = read_json(codex_dest)
            merged = remove_hooks(settings, self.state.get("codex_hooks") or [])
            if codex_rel in created and not merged:
                removals.append(codex_rel)
            elif merged != settings:
                data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                self.add_action(Action(codex_rel, data,
                                       stat.S_IMODE(os.stat(codex_dest).st_mode),
                                       "remove AMPLAI Codex hooks"))
        for rel in (".ai-team/app.json", ".ai-team/local/project.json", STATE_REL):
            dest = safe_destination(self.target, rel)
            if rel != STATE_REL and not self.state.get("identity_ownership", {}).get(rel, True):
                continue
            if os.path.exists(dest):
                removals.append(rel)
        if self.bootstrap:
            for rel, item in self.baseline_receipt["files"].items():
                dest = safe_destination(self.target, rel)
                if not os.path.exists(dest):
                    continue
                if (sha256_file(dest) != item["installed_hash"]
                        or stat.S_IMODE(os.stat(dest).st_mode) != item["mode"]):
                    self.conflicts.append("baseline file changed since install: %s" % rel)
                elif item["created"]:
                    removals.append(rel)
                elif item.get("restore"):
                    self.add_action(Action(rel, base64.b64decode(item["restore"]), item["mode"], "restore borrowed baseline"))
            for rel, item in self.baseline_receipt["mirrors"].items():
                dest = os.path.join(self.target, validate_relative(rel))
                if not os.path.lexists(dest):
                    continue
                if not os.path.islink(dest) or os.readlink(dest) != item["target"]:
                    self.conflicts.append("baseline mirror changed since install: %s" % rel)
                elif item["created"]:
                    removals.append(rel)
        if self.conflicts:
            raise InstallError("uninstall conflicts:\n- " + "\n- ".join(self.conflicts))
        self.removals = sorted(set(removals))
        self.notes.append(
            "the central Project Store is not touched; remove it yourself if it is no longer used"
        )
        return self.actions, self.removals

    def execute_uninstall(self):
        self.validate_package()
        actions, removals = self.plan_uninstall()
        report = {
            "ok": True, "uninstall": True, "dry_run": bool(self.args.dry_run),
            "target": self.target,
            "rewrites": [{"path": a.rel, "reason": a.reason} for a in actions],
            "removals": removals, "notes": self.notes,
        }
        if self.args.dry_run:
            return report
        self.backup_and_apply(removals)
        self.finish_transaction()
        report["removed_directories"] = self._prune_empty_dirs(
            set(os.path.dirname(os.path.join(self.target, rel)) for rel in removals))
        report["backup_dir"] = self.backup_dir
        return report

    @staticmethod
    def _only_kit_header(rel, stripped, item):
        """True when what survives stripping is just the header we wrote.

        `create_header` is what the installer puts at the top of a file it had
        to create.  Anything beyond it belongs to the application.
        """
        header = (item.get("create_header") or "").strip()
        remainder = (stripped or "").strip()
        if not remainder:
            return True
        if not header:
            return False
        return remainder == header.strip()

    def _prune_empty_dirs(self, candidates):
        """Prune only owned empty directories, never traverse substituted parents."""
        removed = []
        owned = set(self.state.get("created_directories") or []) | set(self.tree.created_dirs)
        for start in sorted(candidates, key=len, reverse=True):
            current = os.path.abspath(start)
            while current != self.target:
                rel = os.path.relpath(current, self.target).replace(os.sep, "/")
                if (rel.startswith("../") or rel == BACKUP_ROOT_REL
                        or rel.startswith(BACKUP_ROOT_REL + "/")):
                    break
                if "created_directories" in self.state and rel not in owned:
                    break
                try:
                    parent, name = self.tree.parent(rel)
                    if parent is None:
                        break
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        break
                    held = self.tree.dirs.get(rel)
                    if held is not None and self.tree.identity(info) != self.tree.identity(os.fstat(held)):
                        break
                    os.rmdir(name, dir_fd=parent)
                    if held is not None:
                        self.tree.bindings = [entry for entry in self.tree.bindings if entry[2] != held]
                        del self.tree.dirs[rel]
                        os.close(held)
                except (OSError, InstallError):
                    break
                removed.append(rel)
                current = os.path.dirname(current)
        return sorted(set(removed))

    def plan(self):
        self.validate_package()
        self.validate_target()
        self.plan_baseline()
        self.plan_owned_files()
        self.plan_markers()
        self.plan_json_merges()
        self.plan_claude_settings()
        self.plan_codex_hooks()
        self.plan_identity()
        self.plan_local_binding()
        if self.conflicts:
            raise InstallError("preflight conflicts:\n- " + "\n- ".join(self.conflicts))
        self.plan_state()
        return self.actions

    def check_before(self):
        for rel, previous in self.before.items():
            if not self.tree.matches(self.tree.snapshot(rel), previous, identity=True):
                raise InstallError("target changed since preflight: %s" % rel)

    def backup_and_apply(self, removals=()):
        self.check_before()
        desired = [(a.rel, {"kind": a.kind, "data": a.data, "mode": a.mode})
                   for a in self.actions]
        desired.extend((rel, {"kind": "absent"}) for rel in removals)
        if len({rel for rel, _ in desired}) != len(desired):
            raise InstallError("transaction has competing actions for one path")
        if not desired:
            return []
        existing = [rel for rel, _ in desired if self.before[rel]["kind"] != "absent"]
        backup_rel = None
        if existing:
            backup_rel = BACKUP_ROOT_REL + "/" + timestamp() + "-" + uuid.uuid4().hex
            self.backup_dir = os.path.join(self.target, backup_rel)
            for rel in existing:
                self.tree.write(backup_rel + "/" + rel, self.before[rel], {"kind": "absent"})
        for rel, _ in desired:
            name = ".amplai-install-" + uuid.uuid4().hex
            temp_rel = (rel.rsplit("/", 1)[0] + "/" if "/" in rel else "") + name
            if self.tree.snapshot(temp_rel)["kind"] != "absent":
                raise InstallError("transaction temporary path collision")
            self.tree.temporary_names[rel] = name
        journal = seal({
            "schema_version": "1.0", "kind": "amplai_loop_kit_transaction",
            "package_sha256": sha256_bytes(self.package["CHECKSUMS.sha256"]),
            "created_at": utc_now(), "backup_root": backup_rel,
            "project_store_requested": bool(self.args.project_home),
            "entries": [{"path": rel, "before": self.snapshot_receipt(self.before[rel]),
                         "desired": self.snapshot_receipt(value),
                         "temporary": self.tree.temporary_names[rel]} for rel, value in desired],
        })
        self.journal = {"kind": "file", "mode": 0o600,
                        "data": (json.dumps(journal, sort_keys=True, indent=2) + "\n").encode("utf-8")}
        self.tree.write(JOURNAL_REL, self.journal, {"kind": "absent"})
        applied = []
        try:
            for rel, value in desired:
                before = self.before[rel]
                applied.append((rel, before, value))
                self.tree.write(rel, value, before)
            for rel, _, value in applied:
                if not self.tree.matches(self.tree.snapshot(rel), value):
                    raise InstallError("transaction final inventory changed: %s" % rel)
        except BaseException:
            self.rollback_applied(applied)
            self.finish_transaction()
            raise
        return applied

    @staticmethod
    def snapshot_receipt(snapshot):
        value = {"kind": snapshot["kind"]}
        if snapshot["kind"] != "absent":
            value["sha256"] = sha256_bytes(
                (b"symlink\x00" if snapshot["kind"] == "symlink" else b"") + snapshot["data"])
        if snapshot["kind"] == "file":
            value["mode"] = snapshot["mode"]
        return value

    def finish_transaction(self):
        if self.journal is None:
            return
        current = self.tree.snapshot(JOURNAL_REL)
        if not self.tree.matches(current, self.journal):
            raise InstallError("transaction journal changed; preserved for explicit recovery")
        self.tree.write(JOURNAL_REL, {"kind": "absent"}, current)
        self.journal = None
        self.tree.temporary_names = {}

    def execute_recovery(self):
        """Restore only hash-bound target paths; never alter the central Store."""
        current_journal = self.tree.snapshot(JOURNAL_REL)
        if current_journal["kind"] != "file":
            raise InstallError("no recoverable transaction journal")
        journal = json.loads(current_journal["data"].decode("utf-8"))
        verify_seal(journal, "transaction journal")
        if journal.get("kind") != "amplai_loop_kit_transaction" or journal.get("schema_version") != "1.0":
            raise InstallError("unsupported transaction journal")
        entries = journal.get("entries")
        if not isinstance(entries, list) or not entries or len(entries) > 10000:
            raise InstallError("invalid recovery inventory")
        all_baseline = json.loads(self.package_bytes("baseline/manifest.json").decode("utf-8"))
        allowed = set(self.before) - {JOURNAL_REL}
        allowed.update(item["path"] for key in ("files", "markers", "mirrors")
                       for item in all_baseline.get(key, []))
        backup_root = journal.get("backup_root")
        if backup_root is not None:
            validate_relative(backup_root)
            if not backup_root.startswith(BACKUP_ROOT_REL + "/") or "/" in backup_root[len(BACKUP_ROOT_REL) + 1:]:
                raise InstallError("invalid recovery backup location")
        planned = []
        temporaries = []
        seen = set()
        for entry in entries:
            rel = validate_relative(entry["path"])
            if rel not in allowed or rel in seen:
                raise InstallError("recovery path is outside the declared inventory or duplicated")
            seen.add(rel)
            name = entry.get("temporary")
            if not isinstance(name, str) or not re.match(r"^\.amplai-install-[0-9a-f]{32}$", name):
                raise InstallError("recovery temporary name is not exact")
            temp_rel = (rel.rsplit("/", 1)[0] + "/" if "/" in rel else "") + name
            temporary = self.tree.snapshot(temp_rel)
            if temporary["kind"] not in ("absent", "file", "symlink"):
                raise InstallError("recovery preserves unexpected temporary path type")
            if temporary["kind"] != "absent":
                temporaries.append((temp_rel, temporary))
            self.tree.temporary_names[rel] = name
            current = self.tree.snapshot(rel)
            before, desired = entry["before"], entry["desired"]
            if self.snapshot_receipt(current) == before:
                continue
            if self.snapshot_receipt(current) != desired:
                raise InstallError("recovery preserves a concurrently changed path: %s" % rel)
            if before["kind"] == "absent":
                restore = {"kind": "absent"}
            else:
                if not backup_root:
                    raise InstallError("recovery lacks required original backup")
                restore = self.tree.snapshot(backup_root + "/" + rel)
                if self.snapshot_receipt(restore) != before:
                    raise InstallError("recovery backup does not match the recorded original: %s" % rel)
            planned.append((rel, restore, current))
        report = {"ok": True, "recovery": True, "dry_run": bool(self.args.dry_run),
                  "restores": [rel for rel, _, _ in planned], "central_store_modified": False,
                  "staged_temporaries_preserved": len(temporaries)}
        if journal.get("project_store_requested"):
            report["note"] = "Target recovery only; inspect the separately requested central Store before retrying registration."
        if self.args.dry_run:
            return report
        # A stopped write may contain only part of its bytes. Keep that exact
        # inode content in a recoverable backup rather than deleting by a glob.
        if temporaries:
            quarantine = BACKUP_ROOT_REL + "/recovery-" + uuid.uuid4().hex
            for index, (rel, previous) in enumerate(temporaries):
                self.tree.write(quarantine + "/staged-" + str(index), previous, {"kind": "absent"})
            for rel, previous in temporaries:
                self.tree.write(rel, {"kind": "absent"}, previous)
            report["staged_backup"] = quarantine
        for rel, restore, previous in reversed(planned):
            self.tree.write(rel, restore, previous)
        for entry in entries:
            if self.snapshot_receipt(self.tree.snapshot(entry["path"])) != entry["before"]:
                raise InstallError("recovery final inventory changed; journal preserved")
        self.journal = current_journal
        self.finish_transaction()
        return report

    def rollback_project_store(self):
        if not (self.project_store_created and self.project_store_home):
            return
        if not os.path.exists(os.path.join(self.project_store_home, "project.json")):
            return
        shutil.rmtree(self.project_store_home, ignore_errors=True)
        self.notes.append(
            "removed the Project Store this run created: %s" % self.project_store_home
        )

    def rollback_applied(self, applied):
        conflicts = []
        for rel, before, desired in reversed(applied):
            try:
                current = self.tree.snapshot(rel)
                if self.tree.matches(current, before):
                    continue
                if not self.tree.matches(current, desired):
                    conflicts.append(rel)
                    continue
                self.tree.write(rel, before, current)
            except (OSError, InstallError):
                conflicts.append(rel)
        if conflicts:
            raise InstallError("recovery kept concurrently changed paths; inspect preserved backup: %s" %
                               ", ".join(conflicts))
        self._prune_empty_dirs(
            set(os.path.join(self.target, rel) for rel in self.tree.created_dirs))

    def configure_project_store(self):
        if not self.args.project_home:
            return None
        scripts = os.path.join(self.target, "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from amplai_runtime import ProjectStore
        home = os.path.abspath(os.path.expanduser(self.args.project_home))
        desired_policy = read_json(
            os.path.join(self.target, ".ai-team", "runtime", "async-policy.json")
        )
        self.project_store_home = home
        # Only a store this run created from nothing may be removed on failure.
        self.project_store_created = not os.path.isdir(home)
        store = ProjectStore.initialize(
            home, self.args.project_id, name=self.args.project_name or self.args.project_id,
            git_init=not self.args.no_git,
            policy=desired_policy,
        )
        store.register_app(
            self.args.app_id,
            repo_path=self.target,
            display_name=self.args.app_name,
            max_concurrency=self.args.max_concurrency,
            runner_type=self.args.runner,
            command=self.args.runner_command,
            runner_args=self.args.runner_arg,
            auto_start=self.args.auto_start,
            timeout_seconds=self.args.worker_timeout,
            actor="installer",
        )
        # ProjectStore.initialize keeps an existing central policy untouched, so
        # a kit update can leave the store on an older policy.  Report the drift
        # instead of silently overwriting an operator's customisation.
        drift = store.policy_drift(desired_policy)
        if drift:
            self.notes.append(
                "central policy.json differs from this kit's async-policy.json in %s; "
                "run `amplai.py project set-policy --from .ai-team/runtime/async-policy.json` "
                "to adopt it" % ", ".join(drift)
            )
        supervisor = self.install_store_supervisor(home)
        return {
            "status": store.status_summary(),
            "policy_drift": drift,
            "supervisor": supervisor,
        }

    def install_store_supervisor(self, home):
        """Place the Store's supervisor entry point.

        The Store owns the right to run a supervisor, so its entry point lives
        here rather than in any application repository.  Only the entry point
        and a version marker are copied -- the supervisor itself runs from an
        application's installed copy, which keeps a single implementation.
        """
        prefix = "payload/store/supervisor/"
        sources = {rel[len(prefix):]: data for rel, data in self.package.items() if rel.startswith(prefix)}
        if not sources:
            return {"installed": False, "reason": "package has no store payload"}
        dest_dir = os.path.join(home, "supervisor")
        ensure_dir(dest_dir)
        written = []
        for name, data in sorted(sources.items()):
            if "/" in name:
                raise InstallError("package Store supervisor inventory has unsupported nested member")
            mode = 0o755 if name == "run" else 0o644
            atomic_write(os.path.join(dest_dir, name), data, mode)
            written.append(name)
        atomic_write(
            os.path.join(dest_dir, "VERSION"),
            (self.manifest["version"] + "\n").encode("utf-8"), 0o644,
        )
        written.append("VERSION")
        # Record which application copy this entry point should execute.  The
        # most recent install wins; `run --source-app <id>` overrides it.
        atomic_write(
            os.path.join(dest_dir, "source.json"),
            canonical_json({
                "schema_version": "1.0",
                "app_id": self.args.app_id,
                "kit_version": self.manifest["version"],
            }),
            0o644,
        )
        written.append("source.json")
        return {"installed": True, "path": dest_dir, "files": sorted(set(written))}

    def execute(self):
        try:
            self.tree.lock()
            self.check_before()
            if getattr(self.args, "recover", False):
                return self.execute_recovery()
            if self.before[JOURNAL_REL]["kind"] != "absent":
                raise InstallError("unfinished install transaction; inspect --recover --dry-run, then --recover")
            return self.execute_plan()
        finally:
            self.tree.close()

    def execute_plan(self):
        if self.args.uninstall:
            return self.execute_uninstall()
        actions = self.plan()
        report = {
            "ok": True,
            "dry_run": bool(self.args.dry_run),
            "target": self.target,
            "version": self.manifest["version"],
            "actions": [{"path": a.rel, "reason": a.reason, "sha256": a.new_hash} for a in actions],
        }
        if self.args.dry_run:
            report["notes"] = self.notes
            return report
        applied = self.backup_and_apply()
        try:
            report["project_store"] = self.configure_project_store()
        except Exception as exc:
            self.rollback_applied(applied)
            self.rollback_project_store()
            self.finish_transaction()
            report["ok"] = False
            report["project_store_error"] = str(exc)
            raise
        self.finish_transaction()
        report["backup_dir"] = self.backup_dir
        report["notes"] = self.notes
        return report


def build_parser():
    parser = argparse.ArgumentParser(description="Install/update AMPLAI Loop Kit")
    parser.add_argument("--target", required=True)
    parser.add_argument("--app-id", required=False)
    parser.add_argument("--project-id", required=False)
    parser.add_argument("--project-name")
    parser.add_argument("--app-name")
    parser.add_argument("--project-home")
    parser.add_argument(
        "--runner", choices=["claude-code", "codex", "command"],
        default="claude-code",
    )
    parser.add_argument(
        "--runner-command",
        help="worker executable; defaults to claude or codex for native runners",
    )
    parser.add_argument("--runner-arg", action="append", default=[])
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--worker-timeout", type=int)
    parser.add_argument("--auto-start", action="store_true")
    parser.add_argument("--no-git", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--bootstrap-baseline", action="store_true",
                        help="opt in to the complete generic work/design baseline")
    parser.add_argument("--repo-profile", choices=["generic"],
                        help="explicit baseline profile; existing baseline choice survives omission")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove kit-owned files, managed sections and hooks")
    parser.add_argument("--recover", action="store_true",
                        help="explicitly roll back a stopped, hash-bound target transaction")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.recover and args.uninstall:
        print("INSTALL_ERROR: --recover and --uninstall are separate operations", file=sys.stderr)
        return 2
    if not (args.uninstall or args.recover) and not (args.app_id and args.project_id):
        print("INSTALL_ERROR: --app-id and --project-id are required to install",
              file=sys.stderr)
        return 2
    try:
        report = Installer(args).execute()
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (InstallError, OSError, ValueError) as exc:
        print("INSTALL_ERROR: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:
        print("INSTALL_ERROR: %s: %s" % (exc.__class__.__name__, exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
