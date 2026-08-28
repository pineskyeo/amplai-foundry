#!/usr/bin/env python3
"""Install or update the AMPLAI Decision & Async Cross-App Runtime.

The installer is intentionally conservative:
- validates every package payload before touching the target;
- owns whole files only when their previous installed hash still matches;
- owns only marked sections inside existing skills/policies;
- merges Claude Code hooks without changing permissions;
- backs up and rolls back target files on write failure.

Python 3.6+; standard library only.
"""
from __future__ import print_function

import argparse
import copy
import datetime
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile

PACKAGE_ROOT = os.path.abspath(os.path.dirname(__file__))
MANIFEST_PATH = os.path.join(PACKAGE_ROOT, "manifest.json")
STATE_REL = ".ai-team/install/amplai-loop-kit.json"
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
    if os.path.isabs(rel) or rel.startswith("../") or "/../" in rel.replace("\\", "/"):
        raise InstallError("unsafe manifest path: %s" % rel)
    root = os.path.realpath(target)
    path = os.path.abspath(os.path.join(target, rel))
    parent = os.path.realpath(os.path.dirname(path))
    if parent != root and not parent.startswith(root + os.sep):
        raise InstallError("manifest path escapes target: %s" % rel)
    if os.path.lexists(path) and os.path.islink(path):
        raise InstallError("refusing to replace symlink: %s" % rel)
    return path


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
    def __init__(self, rel, data, mode=0o644, reason="update"):
        self.rel = rel
        self.data = data
        self.mode = mode
        self.reason = reason

    @property
    def new_hash(self):
        return sha256_bytes(self.data)


class Installer(object):
    def __init__(self, args):
        self.args = args
        self.target = os.path.abspath(os.path.expanduser(args.target))
        self.manifest = read_json(MANIFEST_PATH)
        self.state_path = os.path.join(self.target, STATE_REL)
        self.state = read_json(self.state_path) if os.path.isfile(self.state_path) else {}
        if self.state:
            verify_seal(self.state, "existing install record")
        self.actions = []
        self.conflicts = []
        self.notes = []
        self.backup_dir = None
        self.project_store_home = None
        self.project_store_created = False
        self.removals = []

    def validate_package(self):
        if self.manifest.get("runtime_protocol") != PROTOCOL:
            raise InstallError("unsupported package protocol")
        for item in self.manifest.get("owned_files", []):
            path = os.path.join(PACKAGE_ROOT, "payload", item["path"])
            if not os.path.isfile(path):
                raise InstallError("missing payload file: %s" % item["path"])
            if sha256_file(path) != item["sha256"]:
                raise InstallError("payload hash mismatch: %s" % item["path"])
        for item in self.manifest.get("markers", []):
            path = os.path.join(PACKAGE_ROOT, "fragments", item["fragment"])
            if not os.path.isfile(path):
                raise InstallError("missing fragment: %s" % item["fragment"])
            if sha256_file(path) != item["sha256"]:
                raise InstallError("fragment hash mismatch: %s" % item["fragment"])
        for item in self.manifest.get("json_merges", []):
            path = os.path.join(PACKAGE_ROOT, "fragments", item["fragment"])
            if not os.path.isfile(path):
                raise InstallError("missing JSON merge fragment: %s" % item["fragment"])
            if sha256_file(path) != item["sha256"]:
                raise InstallError("JSON merge fragment hash mismatch: %s" % item["fragment"])

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
            if not os.path.exists(os.path.join(self.target, rel)):
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
        if any(existing.rel == action.rel for existing in self.actions):
            raise InstallError("duplicate install action: %s" % action.rel)
        self.actions.append(action)

    def plan_owned_files(self):
        previous = self.state.get("owned_files") or {}
        for item in self.manifest.get("owned_files", []):
            rel = item["path"]
            src = os.path.join(PACKAGE_ROOT, "payload", rel)
            data = read_bytes(src)
            dest = safe_destination(self.target, rel)
            if os.path.exists(dest):
                current = sha256_file(dest)
                if current == item["sha256"]:
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

    def plan_markers(self):
        previous = self.state.get("marker_sections") or {}
        for item in self.manifest.get("markers", []):
            rel = item["path"]
            dest = safe_destination(self.target, rel)
            if not os.path.exists(dest) and item.get("required", False):
                raise InstallError("required marker target is missing: %s" % rel)
            if (not os.path.exists(dest) and not item.get("required", False)
                    and not item.get("create_if_missing", False)):
                continue
            current = (
                read_text(dest) if os.path.exists(dest)
                else item.get("create_header", "")
            )
            fragment = read_text(os.path.join(PACKAGE_ROOT, "fragments", item["fragment"]))
            merged, existing, desired = merge_section(
                current, fragment, item["begin"], item["end"], rel,
            )
            desired_hash = sha256_bytes(desired.encode("utf-8"))
            if existing is not None:
                current_hash = sha256_bytes(existing.encode("utf-8"))
                old_hash = previous.get(rel)
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
        settings = read_json(dest) if os.path.exists(dest) else {}
        merged = merge_hooks(settings, self.manifest.get("claude_hooks", []))
        if merged != settings:
            data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            mode = stat.S_IMODE(os.stat(dest).st_mode) if os.path.exists(dest) else 0o644
            self.add_action(Action(rel, data, mode, "additive Claude hook merge"))

    def plan_json_merges(self):
        previous = self.state.get("json_objects") or {}
        for item in self.manifest.get("json_merges", []):
            rel = item["path"]
            dest = safe_destination(self.target, rel)
            if not os.path.isfile(dest):
                if item.get("required", True):
                    raise InstallError("required JSON merge target is missing: %s" % rel)
                continue
            current = read_json(dest)
            extension = read_json(os.path.join(PACKAGE_ROOT, "fragments", item["fragment"]))
            merged = copy.deepcopy(current)
            rules = merged.setdefault("path_rules", [])
            previous_path = previous.get(rel) or {}
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
            if (existing.get("project_id"), existing.get("app_id")) != (self.args.project_id, self.args.app_id):
                if not self.args.force:
                    self.conflicts.append("existing .ai-team/app.json belongs to another project/app")
                    return
            if read_bytes(dest) == data:
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
        owned = {item["path"]: item["sha256"] for item in self.manifest.get("owned_files", [])}
        markers = {}
        for item in self.manifest.get("markers", []):
            fragment = read_text(os.path.join(PACKAGE_ROOT, "fragments", item["fragment"]))
            desired = section_from_text(fragment, item["begin"], item["end"], item["path"])
            markers[item["path"]] = sha256_bytes(desired.encode("utf-8"))
        json_objects = {}
        for item in self.manifest.get("json_merges", []):
            extension = read_json(os.path.join(PACKAGE_ROOT, "fragments", item["fragment"]))
            json_objects[item["path"]] = {
                rule["id"]: sha256_bytes(canonical_json(rule))
                for rule in extension.get("path_rules", [])
            }
        previous_installed_at = self.state.get("installed_at")
        state = {
            "schema_version": "1.0",
            "kind": "amplai_loop_kit_install",
            "package_version": self.manifest["version"],
            "runtime_protocol": PROTOCOL,
            "project_id": self.args.project_id,
            "app_id": self.args.app_id,
            "installed_at": previous_installed_at or utc_now(),
            "owned_files": owned,
            "marker_sections": markers,
            "json_objects": json_objects,
            "claude_hooks": self.manifest.get("claude_hooks", []),
        }
        data = (json.dumps(seal(state), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.actions = [action for action in self.actions if action.rel != STATE_REL]
        if not os.path.exists(self.state_path) or read_bytes(self.state_path) != data:
            self.add_action(Action(STATE_REL, data, 0o644, "install record"))

    def plan_uninstall(self):
        """Remove what this kit installed, leaving the central store alone."""
        if not self.state:
            raise InstallError("no install record at %s; nothing to uninstall" % STATE_REL)
        owned = self.state.get("owned_files") or {}
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
        for item in self.manifest.get("markers", []):
            rel = item["path"]
            dest = safe_destination(self.target, rel)
            if not os.path.exists(dest):
                continue
            current = read_text(dest)
            stripped, found = strip_section(current, item["begin"], item["end"])
            if not found or stripped == current:
                continue
            if stripped == "":
                removals.append(rel)
                continue
            mode = stat.S_IMODE(os.stat(dest).st_mode)
            self.add_action(Action(rel, stripped.encode("utf-8"), mode, "strip managed section"))
        for item in self.manifest.get("json_merges", []):
            rel = item["path"]
            dest = safe_destination(self.target, rel)
            if not os.path.isfile(dest):
                continue
            current = read_json(dest)
            extension = read_json(os.path.join(PACKAGE_ROOT, "fragments", item["fragment"]))
            merged = copy.deepcopy(current)
            owned_ids = set(
                rule.get("id") for rule in extension.get("path_rules", [])
            )
            merged["path_rules"] = [
                rule for rule in merged.get("path_rules", [])
                if rule.get("id") not in owned_ids
            ]
            owned_prohibitions = set(extension.get("forbidden_automatic_actions", []))
            merged["forbidden_automatic_actions"] = [
                value for value in merged.get("forbidden_automatic_actions", [])
                if value not in owned_prohibitions
            ]
            if merged != current:
                data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                self.add_action(Action(rel, data, stat.S_IMODE(os.stat(dest).st_mode),
                                       "remove managed JSON rules"))
        settings_rel = ".claude/settings.json"
        settings_dest = safe_destination(self.target, settings_rel)
        if os.path.exists(settings_dest):
            settings = read_json(settings_dest)
            merged = remove_hooks(settings, self.state.get("claude_hooks") or [])
            if merged != settings:
                data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                self.add_action(Action(settings_rel, data,
                                       stat.S_IMODE(os.stat(settings_dest).st_mode),
                                       "remove AMPLAI hooks"))
        for rel in (".ai-team/app.json", ".ai-team/local/project.json", STATE_REL):
            dest = safe_destination(self.target, rel)
            if os.path.exists(dest):
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
            "ok": True,
            "uninstall": True,
            "dry_run": bool(self.args.dry_run),
            "target": self.target,
            "rewrites": [{"path": a.rel, "reason": a.reason} for a in actions],
            "removals": removals,
            "notes": self.notes,
        }
        if self.args.dry_run:
            return report
        backup_dir = os.path.join(
            self.target, BACKUP_ROOT_REL, "%s-%s-uninstall" % (timestamp(), os.getpid())
        )
        for rel in [a.rel for a in actions] + removals:
            src = safe_destination(self.target, rel)
            if os.path.exists(src):
                dst = os.path.join(backup_dir, rel)
                ensure_dir(os.path.dirname(dst))
                shutil.copy2(src, dst)
        self.backup_dir = backup_dir
        for action in actions:
            atomic_write(safe_destination(self.target, action.rel), action.data, action.mode)
        for rel in removals:
            dest = safe_destination(self.target, rel)
            if os.path.exists(dest):
                os.unlink(dest)
        report["backup_dir"] = backup_dir
        return report

    def plan(self):
        self.validate_package()
        self.validate_target()
        self.plan_owned_files()
        self.plan_markers()
        self.plan_json_merges()
        self.plan_claude_settings()
        self.plan_identity()
        self.plan_local_binding()
        if self.conflicts:
            raise InstallError("preflight conflicts:\n- " + "\n- ".join(self.conflicts))
        self.plan_state()
        return self.actions

    def backup_and_apply(self):
        existing = []
        for action in self.actions:
            dest = safe_destination(self.target, action.rel)
            if os.path.exists(dest):
                existing.append(action.rel)
        if existing:
            self.backup_dir = os.path.join(
                self.target, BACKUP_ROOT_REL, "%s-%s" % (timestamp(), os.getpid())
            )
            for rel in existing:
                src = safe_destination(self.target, rel)
                dst = os.path.join(self.backup_dir, rel)
                ensure_dir(os.path.dirname(dst))
                shutil.copy2(src, dst)
        applied = []
        try:
            for action in self.actions:
                dest = safe_destination(self.target, action.rel)
                existed = os.path.exists(dest)
                old_mode = stat.S_IMODE(os.stat(dest).st_mode) if existed else None
                atomic_write(dest, action.data, action.mode)
                applied.append((action.rel, existed, old_mode))
        except Exception:
            for rel, existed, old_mode in reversed(applied):
                dest = safe_destination(self.target, rel)
                backup = os.path.join(self.backup_dir or "", rel)
                if existed and os.path.exists(backup):
                    ensure_dir(os.path.dirname(dest))
                    shutil.copy2(backup, dest)
                    if old_mode is not None:
                        os.chmod(dest, old_mode)
                elif not existed and os.path.exists(dest):
                    os.unlink(dest)
            raise
        return applied

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
        for rel, existed, old_mode in reversed(applied):
            dest = safe_destination(self.target, rel)
            backup = os.path.join(self.backup_dir or "", rel)
            if existed and os.path.exists(backup):
                ensure_dir(os.path.dirname(dest))
                shutil.copy2(backup, dest)
                if old_mode is not None:
                    os.chmod(dest, old_mode)
            elif not existed and os.path.exists(dest):
                os.unlink(dest)

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
        source_dir = os.path.join(PACKAGE_ROOT, "payload", "store", "supervisor")
        if not os.path.isdir(source_dir):
            return {"installed": False, "reason": "package has no store payload"}
        dest_dir = os.path.join(home, "supervisor")
        ensure_dir(dest_dir)
        written = []
        for name in sorted(os.listdir(source_dir)):
            src = os.path.join(source_dir, name)
            if not os.path.isfile(src):
                continue
            mode = 0o755 if name == "run" else 0o644
            atomic_write(os.path.join(dest_dir, name), read_bytes(src), mode)
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
            report["ok"] = False
            report["project_store_error"] = str(exc)
            raise
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
    parser.add_argument("--runner", choices=["claude-code", "command"], default="claude-code")
    parser.add_argument("--runner-command", default="claude")
    parser.add_argument("--runner-arg", action="append", default=[])
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--worker-timeout", type=int)
    parser.add_argument("--auto-start", action="store_true")
    parser.add_argument("--no-git", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove kit-owned files, managed sections and hooks")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.uninstall and not (args.app_id and args.project_id):
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
