#!/usr/bin/env python3
"""Portable document primitives consumed by loopctl/loopv2, not a controller.

Authored metadata has one owner. Source/review digests are derived externally.
All public current consumers use select_documents before deriving display text.
There is deliberately no persistent search cache or independently edited index.
"""

import bisect
import contextlib
import copy
import errno
import fcntl
import fnmatch
import hashlib
import html
import json
import os
import re
import stat
import subprocess
import tempfile
import uuid
from urllib.parse import unquote, urlsplit

GIT_READ_PREFIX = ("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull)

RETIREMENT_REFERENCE_KINDS = (
    "reverse",
    "build",
    "test",
    "packaging",
    "runtime_config",
    "dynamic_entry",
    "external",
)


def preservation_sections(data):
    """Partition every byte, including preamble/fences, rather than summarize claims."""
    try:
        lines = data.decode("utf-8").splitlines(keepends=True)
    except UnicodeError:
        raise DocumentError("UNSUPPORTED_PARSER") from None
    starts = [0] if lines else []
    fence = None
    for n, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(\x60{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None and n and re.match(r"^ {0,3}#{1,6}\s+", line):
            starts.append(n)
    return [
        (start + 1, end, "".join(lines[start:end]).encode("utf-8"))
        for start, end in zip(starts, [*starts[1:], len(lines)])  # noqa: B905 -- Python 3.6
    ]


def preservation_evidence(tree, paths):
    if not isinstance(paths, list) or not paths or len(paths) > 100:
        raise DocumentError("INVALID_PRESERVATION")
    if any(not isinstance(p, str) for p in paths) or len(paths) != len(set(paths)):
        raise DocumentError("INVALID_PRESERVATION")
    return [{"path": relative_path(path), "sha256": digest(tree.read(path))} for path in paths]


def preservation_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise DocumentError("INVALID_PRESERVATION")
    return value


def original_document(tree, source):
    if not isinstance(source, dict) or set(source) != {"repository_id", "commit", "path", "doc_id"}:
        raise DocumentError("INVALID_PRESERVATION")
    preservation_text(source["repository_id"])
    _qualified(source["doc_id"])
    config, _ = configuration(tree)
    if source["repository_id"] != config["repository_id"] or not source["doc_id"].startswith(
        source["repository_id"] + ":"
    ):
        raise DocumentError("PRESERVATION_OWNER_MISMATCH")
    path = relative_path(source["path"])
    revision = source["commit"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise DocumentError("INVALID_BASELINE")
    location = strict_git(tree.root, ["rev-parse", "--show-toplevel"])
    if os.path.realpath(os.fsdecode(location).strip()) != tree.root:
        raise DocumentError("GIT_ROOT_MISMATCH")
    locator = revision + ":" + path
    size = strict_git(tree.root, ["cat-file", "-s", locator])
    if not size.strip().isdigit() or int(size) > tree.max_bytes:
        raise DocumentError("SCOPE_INCOMPLETE")
    return strict_git(tree.root, ["cat-file", "blob", locator])


def preservation_ledger(root, sources, mappings=None):
    """Read-only exact section coverage; a retained Git revision alone is insufficient."""
    if not isinstance(sources, list) or not sources or len(sources) > 100:
        raise DocumentError("INVALID_PRESERVATION")
    mappings = [] if mappings is None else mappings
    if not isinstance(mappings, list) or len(mappings) > 10000:
        raise DocumentError("INVALID_PRESERVATION")
    mapped = {}
    for entry in mappings:
        if (
            not isinstance(entry, dict)
            or set(entry)
            != {"section_id", "action", "destination", "reason", "reviewer", "method", "evidence"}
            or entry["action"] != "copied"
        ):
            raise DocumentError("INVALID_PRESERVATION")
        key = preservation_text(entry["section_id"])
        if key in mapped:
            raise DocumentError("INVALID_PRESERVATION")
        mapped[key] = entry
    sections, originals, seen, total = [], [], set(), 0
    observed, destinations, evidence_hashes = {}, {}, {}
    with SourceTree(root) as tree:
        for source in sources:
            data = original_document(tree, source)
            identity = object_digest(source)
            if identity in seen:
                raise DocumentError("INVALID_PRESERVATION")
            seen.add(identity)
            total += len(data)
            if total > 67108864:
                raise DocumentError("SCOPE_INCOMPLETE")
            originals.append({"source": source, "sha256": digest(data)})
            current = tree.read(source["path"], missing_ok=True)
            if source["path"] in observed and observed[source["path"]] != current:
                raise DocumentError("SOURCE_DRIFT")
            observed[source["path"]] = current
            current_lines = current.splitlines(keepends=True) if current is not None else []
            for start, end, body in preservation_sections(data):
                section_id = object_digest(
                    {"source": source, "start": start, "end": end, "sha256": digest(body)}
                )
                row = {
                    "section_id": section_id,
                    "source": source,
                    "line_start": start,
                    "line_end": end,
                    "sha256": digest(body),
                    "claims_sha256": digest(body),
                    "action": "hold",
                    "reason": "Original section not yet preserved.",
                    "destination": None,
                    "review": None,
                    "approval_required": True,
                }
                entry = mapped.pop(section_id, None)
                if entry is None:
                    if b"".join(current_lines[start - 1 : end]) == body:
                        row.update(
                            action="retained",
                            reason="Exact original section retained.",
                            approval_required=False,
                        )
                else:
                    for field in ("reason", "reviewer", "method"):
                        preservation_text(entry[field])
                    dest = entry["destination"]
                    if not isinstance(dest, dict) or set(dest) != {
                        "path",
                        "doc_id",
                        "line_start",
                        "line_end",
                    }:
                        raise DocumentError("INVALID_PRESERVATION")
                    _qualified(dest["doc_id"])
                    prior = destinations.setdefault(dest["doc_id"], dest["path"])
                    if prior != dest["path"]:
                        raise DocumentError("IDENTITY_COLLISION")
                    first, last = dest["line_start"], dest["line_end"]
                    if type(first) is not int or type(last) is not int or first < 1 or last < first:
                        raise DocumentError("INVALID_PRESERVATION")
                    target = tree.read(relative_path(dest["path"]), missing_ok=True)
                    if dest["path"] in observed and observed[dest["path"]] != target:
                        raise DocumentError("SOURCE_DRIFT")
                    observed[dest["path"]] = target
                    row["destination"] = dest
                    row["review"] = {
                        "reviewer": entry["reviewer"],
                        "method": entry["method"],
                        "evidence": preservation_evidence(tree, entry["evidence"]),
                    }
                    for proof in row["review"]["evidence"]:
                        if (
                            proof["path"] in evidence_hashes
                            and evidence_hashes[proof["path"]] != proof["sha256"]
                        ):
                            raise DocumentError("SOURCE_DRIFT")
                        evidence_hashes[proof["path"]] = proof["sha256"]
                    if (
                        target is not None
                        and last <= len(target.splitlines())
                        and b"".join(target.splitlines(keepends=True)[first - 1 : last]) == body
                    ):
                        row.update(action="copied", reason=entry["reason"])
                sections.append(row)
                if len(sections) > 10000:
                    raise DocumentError("SCOPE_INCOMPLETE")
        if mapped:
            raise DocumentError("INVALID_PRESERVATION")
        for path, data in observed.items():
            if tree.read(path, missing_ok=True) != data:
                raise DocumentError("SOURCE_DRIFT")
        for path, expected in evidence_hashes.items():
            if digest(tree.read(path)) != expected:
                raise DocumentError("SOURCE_DRIFT")
    unprocessed = [row["section_id"] for row in sections if row["action"] == "hold"]
    result = {
        "schema_version": "1.0",
        "sources": copy.deepcopy(sources),
        "originals": originals,
        "mappings": copy.deepcopy(mappings),
        "sections": sections,
        "current_files": [
            {"path": p, "sha256": digest(data) if data is not None else None}
            for p, data in sorted(observed.items())
        ],
        "complete": not unprocessed,
        "unprocessed_sections": unprocessed,
        "coverage": (len(sections) - len(unprocessed)) / len(sections) if sections else 1.0,
        "rule": "Exact source bytes only. No canonical promotion or semantic-summary equivalence.",
    }
    result["sha256"] = object_digest(result)
    return result


def retirement_snapshot(tree):
    """All tracked, untracked and ignored bytes bind the finite reference assessment."""
    location = strict_git(tree.root, ["rev-parse", "--show-toplevel"])
    if os.path.realpath(os.fsdecode(location).strip()) != tree.root:
        raise DocumentError("GIT_ROOT_MISMATCH")
    head = strict_git(tree.root, ["rev-parse", "--verify", "HEAD"]).decode("ascii").strip()
    index = tree.read(".git/index")
    paths = set(git_paths(tree.root, ["ls-files", "-z"]))
    paths.update(git_paths(tree.root, ["ls-files", "--others", "--exclude-standard", "-z"]))
    paths.update(
        git_paths(tree.root, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"])
    )
    if len(paths) > 10000:
        raise DocumentError("SCOPE_INCOMPLETE")
    rows, total = [], 0
    for path in sorted(paths):
        data = tree.read(path, missing_ok=True)
        info = tree.info(path, missing_ok=True)
        total += len(data) if data is not None else 0
        if total > 67108864:
            raise DocumentError("SCOPE_INCOMPLETE")
        rows.append(
            {
                "path": path,
                "sha256": digest(data) if data is not None else None,
                "mode": stat.S_IMODE(info.st_mode) if info is not None else None,
            }
        )
    return {"head": head, "index_sha256": digest(index), "files": rows, "complete": True}


def retirement_plan(root, request):
    """No destructive CLI: evidence/retention/approval are distinct from this proposal."""
    if (
        not isinstance(request, dict)
        or set(request)
        != {"schema_version", "work_id", "targets", "preservation", "retention", "references"}
        or request["schema_version"] != "1.0"
    ):
        raise DocumentError("INVALID_RETIREMENT")
    preservation_text(request["work_id"])
    targets = request["targets"]
    if not isinstance(targets, list) or not targets or len(targets) > 100:
        raise DocumentError("INVALID_RETIREMENT")
    if any(not isinstance(p, str) for p in targets) or len(set(targets)) != len(targets):
        raise DocumentError("INVALID_RETIREMENT")
    for path in targets:
        relative_path(path)
        if path.split("/")[0] in (".git", "vault", ".ai-team", ".specify") or not path.endswith(
            ".md"
        ):
            raise DocumentError("HUMAN_GATED_SOURCE")
    ledger = request["preservation"]
    if not isinstance(ledger, dict):
        raise DocumentError("INVALID_PRESERVATION")
    current_ledger = preservation_ledger(root, ledger.get("sources"), ledger.get("mappings"))
    if ledger != current_ledger:
        raise DocumentError("PRESERVATION_DRIFT")
    holds, evidence, inbound = [], [], []
    with SourceTree(root) as tree:
        before = retirement_snapshot(tree)
        tracked = set(git_paths(tree.root, ["ls-files", "-z"]))
        for path in targets:
            if path not in tracked or tree.read(path) is None:
                raise DocumentError("TRACKED_SOURCE_REQUIRED")
            rows = [s for s in ledger["sections"] if s["source"]["path"] == path]
            if not rows or any(
                s["action"] != "copied" or s["destination"]["path"] in targets for s in rows
            ):
                holds.append("PRESERVATION_INCOMPLETE")
            if any(s["source"]["commit"] != before["head"] for s in rows):
                holds.append("SOURCE_REVISION_MISMATCH")
            if rows and digest(original_document(tree, rows[0]["source"])) != digest(
                tree.read(path)
            ):
                holds.append("TARGET_NOT_CLEAN")
        retention = request["retention"]
        if not isinstance(retention, list) or len(retention) != len(targets):
            raise DocumentError("INVALID_RETENTION")
        seen = set()
        for entry in retention:
            if not isinstance(entry, dict) or set(entry) != {"path", "hold", "reason", "evidence"}:
                raise DocumentError("INVALID_RETENTION")
            if (
                entry["path"] not in targets
                or entry["path"] in seen
                or type(entry["hold"]) is not bool
            ):
                raise DocumentError("INVALID_RETENTION")
            seen.add(entry["path"])
            preservation_text(entry["reason"])
            evidence.extend(preservation_evidence(tree, entry["evidence"]))
            if entry["hold"]:
                holds.append("RETENTION_HOLD")
        references = request["references"]
        if not isinstance(references, list):
            raise DocumentError("INVALID_REFERENCE_ASSESSMENT")
        kinds = set()
        for entry in references:
            if not isinstance(entry, dict) or set(entry) != {
                "kind",
                "status",
                "reviewer",
                "reason",
                "evidence",
            }:
                raise DocumentError("INVALID_REFERENCE_ASSESSMENT")
            if entry["kind"] not in RETIREMENT_REFERENCE_KINDS or entry["kind"] in kinds:
                raise DocumentError("INVALID_REFERENCE_ASSESSMENT")
            kinds.add(entry["kind"])
            if entry["status"] not in ("CLEAR", "REFERENCED", "UNKNOWN"):
                raise DocumentError("INVALID_REFERENCE_ASSESSMENT")
            for key in ("reason", "reviewer"):
                preservation_text(entry[key])
            evidence.extend(preservation_evidence(tree, entry["evidence"]))
            if entry["status"] != "CLEAR":
                holds.append(
                    "EXTERNAL_REFERENCE_UNAVAILABLE"
                    if entry["kind"] == "external"
                    else "REFERENCE_REVIEW_REQUIRED"
                )
        if kinds != set(RETIREMENT_REFERENCE_KINDS):
            holds.append("REFERENCE_REVIEW_REQUIRED")
        for item in before["files"]:
            if item["path"] in targets or item["sha256"] is None:
                continue
            data = tree.read(item["path"])
            if b"\0" in data:
                continue
            for target in targets:
                if (
                    target.encode("utf-8") in data
                    or os.path.basename(target).encode("utf-8") in data
                ):
                    inbound.append({"source": item["path"], "target": target})
        if inbound:
            holds.append("REVERSE_REFERENCE_PRESENT")
        if retirement_snapshot(tree) != before:
            raise DocumentError("SOURCE_DRIFT")
        after_index = _prepared_retirement_index(tree.root, tree.read(".git/index"), targets)
    result = {
        "schema_version": "1.0",
        "request": copy.deepcopy(request),
        "snapshot": before,
        "evidence": evidence,
        "inbound_references": inbound,
        "hold_reasons": sorted(set(holds)),
        "status": "HOLD" if holds else "APPROVAL_REQUIRED",
        "report_only": True,
        "index_after_sha256": digest(after_index),
    }
    result["sha256"] = object_digest(result)
    return result


def retirement_authority_request(root, plan, backup, operation):
    """Exact request to an existing trusted authority adapter, not self-signed permission."""
    root, backup = os.path.abspath(str(root)), os.path.abspath(str(backup))
    if root != os.path.realpath(root) or backup != os.path.realpath(backup):
        raise DocumentError("UNSAFE_PATH")
    if os.path.commonpath([root, backup]) == root or os.path.dirname(backup) == backup:
        raise DocumentError("UNSAFE_BACKUP")
    if operation not in ("retire", "recover"):
        raise DocumentError("INVALID_RETIREMENT")
    info = os.stat(root, follow_symlinks=False)
    return {
        "operation": operation,
        "root": root,
        "root_identity": list(_identity(info)),
        "backup": backup,
        "plan_sha256": plan["sha256"],
        "targets": plan["request"]["targets"],
    }


def _exclusive_bytes(tree, path, data, mode=0o600):
    parent, name, chain = tree._parents(path)
    try:
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent
        )
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            # Restore exact approved permission bits, independent of caller umask.
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        tree._check(chain)
        os.fsync(parent)
    finally:
        for _, _, child in reversed(chain):
            os.close(child)


def _retirement_index(tree, expected, replacement):
    """Compare and replace the exact index under Git's existing exclusive index lock."""
    parent, name, chain = tree._parents(".git/index")
    created = False
    try:
        try:
            fd = os.open(
                "index.lock",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        except FileExistsError:
            raise DocumentError("INDEX_BUSY") from None
        created = True
        with os.fdopen(fd, "wb") as out:
            out.write(replacement)
            out.flush()
            os.fsync(out.fileno())
        if tree.read(".git/index") != expected:
            raise DocumentError("INDEX_DRIFT")
        tree._check(chain)
        os.replace("index.lock", name, src_dir_fd=parent, dst_dir_fd=parent)
        created = False
        os.fsync(parent)
    finally:
        if created:
            os.unlink("index.lock", dir_fd=parent)
        for _, _, child in reversed(chain):
            os.close(child)


def _prepared_retirement_index(root, original, targets):
    # Git edits only this owned temporary index. Neither source nor real index is written.
    directory = tempfile.TemporaryDirectory(prefix="amplai-retirement-index-")
    with directory as temporary, SourceTree(temporary) as tree:
        _exclusive_bytes(tree, "index", original)
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["GIT_INDEX_FILE"] = os.path.join(tree.root, "index")
        result = subprocess.run(  # noqa: UP022 -- Python 3.6
            [*GIT_READ_PREFIX, "update-index", "--force-remove", "--", *targets],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        if result.returncode:
            raise DocumentError("INDEX_PREPARATION_FAILED")
        return tree.read("index")


def _retirement_expect(tree, plan, removed, staged=False):
    expected = copy.deepcopy(plan["snapshot"])
    if staged:
        expected["index_sha256"] = plan["index_after_sha256"]
        expected["files"] = [row for row in expected["files"] if row["path"] not in removed]
    else:
        for row in expected["files"]:
            if row["path"] in removed:
                row.update(sha256=None, mode=None)
    if retirement_snapshot(tree) != expected:
        raise DocumentError("SOURCE_DRIFT")


def _retirement_verify(root, verify):
    if not callable(verify):
        raise DocumentError("VERIFICATION_REQUIRED")
    proof = verify(root)
    if not isinstance(proof, dict) or set(proof) != {"exit_code", "check_id", "evidence_sha256"}:
        raise DocumentError("VERIFICATION_FAILED")
    if type(proof["exit_code"]) is not int or proof["exit_code"] != 0:
        raise DocumentError("VERIFICATION_FAILED")
    preservation_text(proof["check_id"])
    hash_value(proof["evidence_sha256"])
    return proof


def _retirement_payloads(plan):
    return {
        path: "source-" + digest(path.encode("utf-8")) + ".bin"
        for path in plan["request"]["targets"]
    }


def _retirement_journal(backup, value):
    expected = backup.read("journal.json", missing_ok=True)
    atomic_json(backup, "journal.json", value, expected)


def _retire_approved(root, plan, backup, authorize=None, verify=None, fault=None):
    """Internal adapter seam, no CLI caller. Exercised only in disposable fixtures.

    The caller must provide its existing trusted authority/verification adapters.
    A plan JSON or a synthetic boolean in a file is never consumed as permission.
    """
    if not callable(authorize) or not callable(verify):
        raise DocumentError("APPROVAL_REQUIRED")
    authority = retirement_authority_request(root, plan, backup, "retire")
    if plan != retirement_plan(root, plan["request"]) or plan["status"] != "APPROVAL_REQUIRED":
        raise DocumentError("APPROVAL_MISMATCH")
    if authorize(copy.deepcopy(authority)) is not True:
        raise DocumentError("APPROVAL_REQUIRED")
    fault = fault or (lambda _: None)
    with SourceTree(root) as tree, review_lock(tree):
        if plan != retirement_plan(root, plan["request"]):
            raise DocumentError("SOURCE_DRIFT")
        backup = os.path.abspath(str(backup))
        with SourceTree(os.path.dirname(backup)) as parent:
            os.mkdir(os.path.basename(backup), 0o700, dir_fd=parent.fd)
            parent._check([])
        with view_tree(backup) as recovery:
            original_index = tree.read(".git/index")
            _exclusive_bytes(recovery, "index.bin", original_index)
            staged_index = _prepared_retirement_index(
                tree.root, original_index, plan["request"]["targets"]
            )
            if digest(staged_index) != plan["index_after_sha256"]:
                raise DocumentError("INDEX_DRIFT")
            _exclusive_bytes(recovery, "candidate.index", staged_index)
            files = _retirement_payloads(plan)
            for path, name in files.items():
                _exclusive_bytes(recovery, name, tree.read(path))
            _exclusive_bytes(recovery, "plan.json", json_bytes(plan))
            _exclusive_bytes(recovery, "authority-request.json", json_bytes(authority))
            journal = {
                "schema_version": "1.0",
                "plan_sha256": plan["sha256"],
                "root_identity": authority["root_identity"],
                "status": "PREPARED",
                "index_before_sha256": digest(original_index),
                "index_after_sha256": digest(staged_index),
                "files": files,
                "verification": None,
            }
            _retirement_journal(recovery, journal)
            try:
                if retirement_snapshot(tree) != plan["snapshot"]:
                    raise DocumentError("SOURCE_DRIFT")
                fault("backup_ready")
                removed = []
                for path, name in files.items():
                    if authorize(copy.deepcopy(authority)) is not True:
                        raise DocumentError("APPROVAL_REQUIRED")
                    _retirement_expect(tree, plan, removed)
                    parent_fd, leaf, chain = tree._parents(path)
                    try:
                        if tree.read(path) != recovery.read(name):
                            raise DocumentError("SOURCE_DRIFT")
                        tree._check(chain)
                        os.unlink(leaf, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                        removed.append(path)
                    finally:
                        for _, _, child in reversed(chain):
                            os.close(child)
                    fault("file_retired")
                _retirement_expect(tree, plan, removed)
                _retirement_index(tree, original_index, staged_index)
                fault("index_staged")
                proof = _retirement_verify(tree.root, verify)
                fault("verified")
                if authorize(copy.deepcopy(authority)) is not True:
                    raise DocumentError("SOURCE_DRIFT")
                _retirement_expect(tree, plan, removed, staged=True)
                journal.update(status="RETIRED", verification=proof)
                _retirement_journal(recovery, journal)
                tombstone = {
                    "schema_version": "1.0",
                    "plan_sha256": plan["sha256"],
                    "replacements": [
                        {
                            "source": s["source"]["doc_id"],
                            "section_id": s["section_id"],
                            "destination": s["destination"],
                        }
                        for s in plan["request"]["preservation"]["sections"]
                    ],
                    "verification": proof,
                    "status": "RETIRED",
                }
                _exclusive_bytes(recovery, "tombstone.json", json_bytes(tombstone))
                return journal
            except Exception:
                with contextlib.suppress(OSError, ValueError):
                    _recover_retirement(root, plan, backup, verify=verify, _locked_tree=tree)
                raise


def _recover_retirement(root, plan, backup, verify=None, _locked_tree=None):
    """Restore exact journaled bytes only; retain recovery evidence and reject drift."""
    if _locked_tree is None:
        with SourceTree(root) as tree, review_lock(tree):
            return _recover_retirement(root, plan, backup, verify, _locked_tree=tree)
    tree = _locked_tree
    expected_authority = retirement_authority_request(root, plan, backup, "retire")
    with view_tree(backup) as recovery:
        if (
            strict_json(recovery.read("plan.json")) != plan
            or strict_json(recovery.read("authority-request.json")) != expected_authority
        ):
            raise DocumentError("RECOVERY_MISMATCH")
        core = {key: value for key, value in plan.items() if key != "sha256"}
        if object_digest(core) != plan["sha256"]:
            raise DocumentError("RECOVERY_MISMATCH")
        journal = strict_json(recovery.read("journal.json"))
        files = _retirement_payloads(plan)
        if journal.get("plan_sha256") != plan["sha256"] or journal.get("files") != files:
            raise DocumentError("RECOVERY_MISMATCH")
        before, after = recovery.read("index.bin"), recovery.read("candidate.index")
        if (
            digest(before) != plan["snapshot"]["index_sha256"]
            or digest(after) != plan["index_after_sha256"]
        ):
            raise DocumentError("RECOVERY_MISMATCH")
        if (
            strict_git(tree.root, ["rev-parse", "HEAD"]).decode("ascii").strip()
            != plan["snapshot"]["head"]
        ):
            raise DocumentError("RECOVERY_HEAD_DRIFT")
        current_index = tree.read(".git/index")
        if current_index not in (before, after):
            raise DocumentError("INDEX_DRIFT")
        rows = {item["path"]: item for item in plan["snapshot"]["files"]}
        for path, name in files.items():
            data = recovery.read(name)
            current = tree.read(path, missing_ok=True)
            if digest(data) != rows[path]["sha256"] or (current is not None and current != data):
                raise DocumentError("RECOVERY_CONTENT_DRIFT")
        for path, name in files.items():
            if tree.read(path, missing_ok=True) is None:
                _exclusive_bytes(tree, path, recovery.read(name), rows[path]["mode"])
        if current_index != before:
            _retirement_index(tree, current_index, before)
        proof = _retirement_verify(tree.root, verify)
        journal.update(status="RECOVERED", verification=proof)
        _retirement_journal(recovery, journal)
        tombstone = recovery.read("tombstone.json", missing_ok=True)
        if tombstone is not None:
            value = strict_json(tombstone)
            value["status"] = "RECOVERED"
            atomic_json(recovery, "tombstone.json", value, tombstone)
        return journal


POLICY = ".ai-team/policy/documentation.json"
PROFILE = ".ai-team/runtime/repository-profile.json"
SIDECAR = ".amplai.json"
CURRENT_CONSUMERS = (
    "inventory",
    "query",
    "index",
    "view",
    "loop-context",
    "loop-claim",
    "loop-decision",
)
SECURITY = ("PUBLIC", "EXTERNAL_APPROVED", "INTERNAL", "RESTRICTED")
LIFECYCLES = ("draft", "active", "deprecated", "superseded", "archived")
AUTHORITIES = ("canonical", "supporting", "historical")
LEGACY = {
    "ACTIVE": ("active", "unknown"),
    "STALE": ("active", "stale"),
    "CANDIDATE": ("draft", "unknown"),
    "DEPRECATED": ("deprecated", "unknown"),
    "SUPERSEDED": ("superseded", "unknown"),
    "ARCHIVED": ("archived", "unknown"),
}
DEFAULT_CONFIG = {
    "schema_version": "1.0",
    "repository_id": "local",
    "roots": ["docs"],
    "optional_roots": [],
    "review_store": ".ai-team/knowledge/document-reviews.json",
    "default_security": "INTERNAL",
    "maximum_security": "INTERNAL",
    "current_release_id": None,
    "max_files": 10000,
    "max_file_bytes": 8388608,
    "max_total_bytes": 67108864,
    "context_dependencies": [],
    "governing_inputs": [],
}


class DocumentError(ValueError):
    """Safe public error: never include rejected source names, titles or bytes."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def object_digest(value):
    return digest(json_bytes(value))


def strict_git(root, args, empty_codes=()):
    """A failed repository read is never an empty change set."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        value = subprocess.run(  # noqa: UP022 -- Python 3.6 compatibility
            [*GIT_READ_PREFIX, *args],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DocumentError("GIT_UNAVAILABLE") from None
    if value.returncode in empty_codes:
        return None
    if value.returncode != 0:
        raise DocumentError("GIT_READ_FAILED")
    return value.stdout


def git_paths(root, args):
    value = strict_git(root, args)
    try:
        paths = [part.decode("utf-8") for part in value.split(b"\0") if part]
    except UnicodeError:
        raise DocumentError("UNSUPPORTED_PATH") from None
    for path in paths:
        relative_path(path)
    return paths


def review_output_path(path, config):
    target = config["review_store"]
    return path == target or path.startswith(target + ".pending-")


def candidate_exclusion(path, config):
    """Known generated/runtime roles, not a blanket exemption for unknown code."""
    if path in (
        ".specify/eval/last.json",
        ".specify/eval/history.jsonl",
        ".specify/eval/verifier-last.json",
    ):
        return "canonical evaluator-generated evidence: " + path.rsplit("/", 1)[-1]
    if review_output_path(path, config):
        return "canonical document review result/transaction; each review separately verified"
    for prefix, reason in (
        ("specs/", "per-Work contract/plan/evidence; active contract independently bound"),
        (".ai-team/local/", "local runtime coordination, not product source"),
        (".ai-team/install/", "installation receipt, not product source"),
        (".ai-team/backups/", "retained installation recovery bytes"),
    ):
        if path.startswith(prefix):
            return reason
    return None


def changed_paths(root, base=None):
    paths = set(
        git_paths(root, ["diff", "--name-only", "--no-renames", "--no-ext-diff", "-z", "--"])
    )
    paths.update(
        git_paths(
            root, ["diff", "--cached", "--name-only", "--no-renames", "--no-ext-diff", "-z", "--"]
        )
    )
    paths.update(git_paths(root, ["ls-files", "--others", "--exclude-standard", "-z"]))
    if base:
        if not re.fullmatch(r"[0-9a-f]{40,64}", base):
            raise DocumentError("INVALID_BASELINE")
        paths.update(
            git_paths(
                root,
                ["diff", "--name-only", "--no-renames", "--no-ext-diff", "-z", base, "HEAD", "--"],
            )
        )
    return sorted(paths)


def source_record(tree, path, config=None, depth=0):
    chain = []
    try:
        parent, name, chain = tree._parents(path)
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(name, dir_fd=parent)
            record = dependency_path_record(tree.root, path)
            if os.readlink(name, dir_fd=parent) != target:
                raise DocumentError("SOURCE_DRIFT")
            tree._check(chain)
            return {
                "path": path,
                "state": "SYMLINK",
                "target_sha256": digest(os.fsencode(target)),
                "dependency": record,
            }
    except (FileNotFoundError, DocumentError) as exc:
        if isinstance(exc, DocumentError) and exc.code != "MISSING_SOURCE":
            raise
    finally:
        for _, _, fd in reversed(chain):
            os.close(fd)
    info = tree.info(path, missing_ok=True)
    if info is None:
        return {"path": path, "state": "MISSING", "sha256": None}
    if stat.S_ISDIR(info.st_mode):
        if depth >= 8:
            raise DocumentError("SCOPE_INCOMPLETE")
        rows = strict_git(tree.root, ["ls-files", "--stage", "-z", "--", path]).split(b"\0")
        prefix = b"160000 "
        suffix = b" 0\t" + path.encode("utf-8")
        if not any(row.startswith(prefix) and row.endswith(suffix) for row in rows):
            raise DocumentError("UNSUPPORTED_SOURCE")
        child = os.path.join(tree.root, path)
        location = strict_git(child, ["rev-parse", "--show-toplevel"])
        if os.path.realpath(os.fsdecode(location).strip()) != child:
            raise DocumentError("EXTERNAL_REFERENCE_UNAVAILABLE")
        with SourceTree(child, tree.max_bytes) as subtree:
            snapshot = candidate_snapshot(subtree, config or DEFAULT_CONFIG, depth + 1)
        return {
            "path": path,
            "state": "GITLINK",
            "checkout_sha256": object_digest(snapshot),
            "checkout_commit": snapshot["commit"],
        }
    if not stat.S_ISREG(info.st_mode):
        raise DocumentError("UNSUPPORTED_SOURCE")
    return {
        "path": path,
        "state": "PRESENT",
        "mode": stat.S_IMODE(info.st_mode),
        "sha256": digest(tree.read(path)),
    }


def candidate_snapshot(tree, config, depth=0):
    location = strict_git(tree.root, ["rev-parse", "--show-toplevel"], empty_codes=(128,))
    if location is None:
        if os.path.lexists(os.path.join(tree.root, ".git")):
            raise DocumentError("GIT_READ_FAILED")
        return {"mode": "standalone", "commit": None, "index": [], "changes": [], "complete": True}
    if os.path.realpath(os.fsdecode(location).strip()) != tree.root:
        raise DocumentError("GIT_ROOT_MISMATCH")
    head = strict_git(tree.root, ["rev-parse", "--verify", "HEAD"], empty_codes=(128,))
    if head is None:
        if strict_git(tree.root, ["show-ref", "--head"], empty_codes=(1,)) is not None:
            raise DocumentError("GIT_READ_FAILED")
        commit = None
    else:
        commit = head.decode("ascii").strip()
    index = []
    for row in strict_git(tree.root, ["ls-files", "--stage", "-z"]).split(b"\0"):
        if not row:
            continue
        try:
            fields, name = row.split(b"\t", 1)
            mode, oid, stage = fields.decode("ascii").split(" ")
            path = name.decode("utf-8")
        except (ValueError, UnicodeError):
            raise DocumentError("INVALID_INDEX") from None
        relative_path(path)
        if stage != "0":
            raise DocumentError("UNMERGED_CANDIDATE")
        if not candidate_exclusion(path, config):
            index.append({"path": path, "mode": mode, "oid": oid})
            if len(index) > config["max_files"]:
                raise DocumentError("SCOPE_INCOMPLETE")
    paths = [p for p in changed_paths(tree.root) if not candidate_exclusion(p, config)]
    if len(paths) > config["max_files"]:
        raise DocumentError("SCOPE_INCOMPLETE")
    changes = []
    total = 0
    for path in paths:
        changes.append(source_record(tree, path, config, depth))
        if changes[-1]["state"] == "PRESENT":
            total += tree.info(path).st_size
            if total > config["max_total_bytes"]:
                raise DocumentError("SCOPE_INCOMPLETE")
    return {
        "mode": "git",
        "commit": commit,
        "index": sorted(index, key=lambda x: x["path"]),
        "changes": changes,
        "complete": True,
    }


def strict_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise DocumentError("UNSUPPORTED_PARSER")
            result[key] = value
        return result

    def constant(_):
        raise DocumentError("UNSUPPORTED_PARSER")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DocumentError("UNSUPPORTED_PARSER") from None


def relative_path(value):
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise DocumentError("UNSAFE_PATH")
    return value


def _identity(info):
    return info.st_dev, info.st_ino


class SourceTree:
    """Bounded no-follow reads rooted at an explicit local directory descriptor."""

    def __init__(self, root, max_bytes=8388608):
        self.root = os.path.realpath(str(root))
        self.max_bytes = max_bytes
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def _parents(self, path):
        parts = relative_path(path).split("/")
        chain = []
        parent = self.fd
        try:
            for part in parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                chain.append((parent, part, fd))
                parent = fd
            return parent, parts[-1], chain
        except OSError as exc:
            for _, _, fd in reversed(chain):
                os.close(fd)
            raise DocumentError(
                "MISSING_SOURCE" if exc.errno == errno.ENOENT else "UNSAFE_PATH"
            ) from None

    def _check(self, chain):
        try:
            if _identity(os.stat(self.root, follow_symlinks=False)) != _identity(os.fstat(self.fd)):
                raise DocumentError("SOURCE_DRIFT")
            for parent, name, fd in chain:
                if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(
                    os.fstat(fd)
                ):
                    raise DocumentError("SOURCE_DRIFT")
        except OSError:
            raise DocumentError("SOURCE_DRIFT") from None

    def info(self, path, missing_ok=False):
        chain = []
        try:
            parent, name, chain = self._parents(path)
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            self._check(chain)
            if stat.S_ISLNK(info.st_mode):
                raise DocumentError("UNSAFE_PATH")
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise DocumentError("UNSAFE_PATH")
            return info
        except (FileNotFoundError, DocumentError) as exc:
            if missing_ok and (isinstance(exc, FileNotFoundError) or exc.code == "MISSING_SOURCE"):
                return None
            if isinstance(exc, DocumentError):
                raise
            raise DocumentError("MISSING_SOURCE") from None
        except OSError:
            raise DocumentError("UNSAFE_PATH") from None
        finally:
            for _, _, fd in reversed(chain):
                os.close(fd)

    def read(self, path, missing_ok=False, limit=None):
        chain = []
        fd = None
        try:
            parent, name, chain = self._parents(path)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise DocumentError("UNSAFE_PATH")
            bound = self.max_bytes if limit is None else limit
            if before.st_size > bound:
                raise DocumentError("SCOPE_INCOMPLETE")
            chunks = []
            total = 0
            while True:
                chunk = os.read(fd, min(65536, bound + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > bound:
                    raise DocumentError("SCOPE_INCOMPLETE")
            after = os.fstat(fd)

            def fields(info):
                return _identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns

            if fields(before) != fields(after):
                raise DocumentError("SOURCE_DRIFT")
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if fields(current) != fields(after):
                raise DocumentError("SOURCE_DRIFT")
            self._check(chain)
            return b"".join(chunks)
        except (FileNotFoundError, DocumentError) as exc:
            if missing_ok and (isinstance(exc, FileNotFoundError) or exc.code == "MISSING_SOURCE"):
                return None
            if isinstance(exc, DocumentError):
                raise
            raise DocumentError("MISSING_SOURCE") from None
        except OSError:
            raise DocumentError("UNSAFE_PATH") from None
        finally:
            if fd is not None:
                os.close(fd)
            for _, _, fd in reversed(chain):
                os.close(fd)

    def paths(self, roots, maximum, optional_roots=()):
        """Enumerate all declared roots; a resource cap is an error, not truncation."""
        found = []
        seen = set()
        seen_objects = {}
        entries = [0]

        def visit(path, optional=False):
            info = self.info(path, missing_ok=optional)
            if info is None:
                return
            folded = path.casefold()
            if folded in seen:
                raise DocumentError("IDENTITY_COLLISION")
            seen.add(folded)
            entries[0] += 1
            if entries[0] > maximum:
                raise DocumentError("SCOPE_INCOMPLETE")
            key = _identity(info)
            if key in seen_objects:
                raise DocumentError("IDENTITY_COLLISION")
            seen_objects[key] = path
            if stat.S_ISDIR(info.st_mode):
                parent, name, chain = self._parents(path)
                fd = None
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    if _identity(os.fstat(fd)) != key:
                        raise DocumentError("SOURCE_DRIFT")
                    names = sorted(os.listdir(fd))
                    self._check([*chain, (parent, name, fd)])
                except OSError:
                    raise DocumentError("SOURCE_DRIFT") from None
                finally:
                    if fd is not None:
                        os.close(fd)
                    for _, _, handle in reversed(chain):
                        os.close(handle)
                for child in names:
                    visit(path + "/" + child)
            elif path.lower().endswith(".md") or path.endswith(SIDECAR):
                found.append(path)

        for path in roots:
            visit(path, optional=path in optional_roots)
        return sorted(found)


def configuration(tree):
    policy_bytes = tree.read(POLICY, limit=8388608)
    policy = strict_json(policy_bytes)
    if not isinstance(policy, dict):
        raise DocumentError("INVALID_POLICY")
    value = copy.deepcopy(policy.get("portable_documents", DEFAULT_CONFIG))
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise DocumentError("INVALID_POLICY")
    if set(value) - set(DEFAULT_CONFIG):
        raise DocumentError("INVALID_POLICY")
    for key, default in DEFAULT_CONFIG.items():
        value.setdefault(key, copy.deepcopy(default))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(value.get("repository_id") or "")):
        raise DocumentError("INVALID_POLICY")
    roots = value.get("roots")
    if not isinstance(roots, list) or any(not isinstance(x, str) for x in roots):
        raise DocumentError("INVALID_POLICY")
    for path in roots:
        relative_path(path)
    optional = value["optional_roots"]
    if (
        not isinstance(optional, list)
        or any(x not in roots for x in optional)
        or len(optional) != len(set(optional))
    ):
        raise DocumentError("INVALID_POLICY")
    folded = [x.casefold() for x in roots]
    if len(folded) != len(set(folded)) or any(
        a != b and a.startswith(b.rstrip("/") + "/") for a in folded for b in folded
    ):
        raise DocumentError("IDENTITY_COLLISION")
    relative_path(value["review_store"])
    if value["default_security"] not in SECURITY or value["maximum_security"] not in SECURITY:
        raise DocumentError("INVALID_POLICY")
    if SECURITY.index(value["default_security"]) > SECURITY.index(value["maximum_security"]):
        raise DocumentError("INVALID_POLICY")
    for key, upper in (
        ("max_files", 100000),
        ("max_file_bytes", 67108864),
        ("max_total_bytes", 268435456),
    ):
        if type(value[key]) is not int or not 1 <= value[key] <= upper:
            raise DocumentError("INVALID_POLICY")
    current = value["current_release_id"]
    if current is not None:
        exact_release(current)
    governing = value["governing_inputs"]
    if not isinstance(governing, list):
        raise DocumentError("INVALID_POLICY")
    seen = set()
    for item in governing:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "role", "security"}
            or item.get("role") not in ("instruction", "decision_ledger")
            or item.get("security") not in SECURITY
        ):
            raise DocumentError("INVALID_POLICY")
        path = relative_path(item["path"])
        if path.casefold() in seen:
            raise DocumentError("INVALID_POLICY")
        seen.add(path.casefold())
    tree.max_bytes = value["max_file_bytes"]
    if not isinstance(value["context_dependencies"], list):
        raise DocumentError("INVALID_POLICY")
    context_dependencies = dependency_snapshot(
        tree, {"dependencies": value["context_dependencies"]}, value
    )
    profile = tree.read(PROFILE, limit=8388608)
    strict_json(profile)
    return value, {
        "policy_sha256": digest(policy_bytes),
        "profile_sha256": digest(profile),
        "context_dependencies_sha256": object_digest(context_dependencies),
        "engine_version": "document-lifecycle-1",
    }


def exact_release(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,159}", value)
        or value.lower() in ("latest", "head", "current", "*")
    ):
        raise DocumentError("EXACT_RELEASE_REQUIRED")
    return value


def _strings(values, required=False):
    if (
        not isinstance(values, list)
        or (required and not values)
        or any(not isinstance(x, str) or not x.strip() for x in values)
        or len(values) != len(set(values))
    ):
        raise DocumentError("INVALID_METADATA")
    return values


def _qualified(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._:-]*", value
    ):
        raise DocumentError("INVALID_METADATA")
    return value


def authored_metadata(tree, path, config):
    source = tree.read(path)
    sidecar = tree.read(path + SIDECAR, missing_ok=True)
    try:
        text = source.decode("utf-8")
    except UnicodeError:
        raise DocumentError("UNSUPPORTED_PARSER") from None
    head = None
    frontmatter_owned = False
    body = text
    if text.startswith("---\n") or text.startswith("---\r\n"):
        lines = text.splitlines(keepends=True)
        end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
        if end is None:
            raise DocumentError("UNSUPPORTED_PARSER")
        raw = "".join(lines[1:end]).strip()
        if raw.startswith("{"):
            frontmatter = strict_json(raw)
            if not isinstance(frontmatter, dict):
                raise DocumentError("UNSUPPORTED_PARSER")
            if "amplai_document" in frontmatter:
                frontmatter_owned = True
                if set(frontmatter) != {"amplai_document"}:
                    raise DocumentError("INVALID_METADATA")
                head = frontmatter["amplai_document"]
                body = "".join(lines[end + 1 :])
        elif re.search(r"(?m)^\s*amplai_document\s*:", raw):
            raise DocumentError("UNSUPPORTED_PARSER")
        if head is None and sidecar is None:
            raise DocumentError("UNCLASSIFIED_DOCUMENT")
    if frontmatter_owned and sidecar is not None:
        raise DocumentError("DUPLICATE_AUTHORITY")
    owner = "frontmatter"
    if sidecar is not None:
        wrapper = strict_json(sidecar)
        if not isinstance(wrapper, dict) or set(wrapper) != {"amplai_document"}:
            raise DocumentError("INVALID_METADATA")
        head = wrapper["amplai_document"]
        owner = "sidecar"
    if head is None:
        raise DocumentError("UNCLASSIFIED_DOCUMENT")
    if not isinstance(head, dict):
        raise DocumentError("INVALID_METADATA")
    allowed = {
        "schema_version",
        "doc_id",
        "topic_id",
        "source",
        "title",
        "owner",
        "kind",
        "authority",
        "lifecycle",
        "security",
        "release_ids",
        "audiences",
        "dependencies",
        "retention",
        "replacement",
        "historical_disposition",
        "example",
        "aliases",
        "legacy_status",
        "sections",
    }
    if set(head) - allowed:
        raise DocumentError("INVALID_METADATA")
    value = copy.deepcopy(head)
    if value.get("schema_version") != "1.0":
        raise DocumentError("INVALID_METADATA")
    for key in ("doc_id", "topic_id"):
        _qualified(value.get(key))
    for key in ("title", "owner", "kind"):
        if not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 4096:
            raise DocumentError("INVALID_METADATA")
    if value.get("source") != {"repository": config["repository_id"], "path": path}:
        raise DocumentError("SOURCE_LOCATOR_MISMATCH")
    if value.get("authority") not in AUTHORITIES:
        raise DocumentError("INVALID_METADATA")
    legacy = value.get("legacy_status")
    initial_freshness = "unknown"
    if legacy is not None:
        if legacy not in LEGACY:
            raise DocumentError("INVALID_METADATA")
        state, initial_freshness = LEGACY[legacy]
        if "lifecycle" in value and value["lifecycle"] != state:
            raise DocumentError("INVALID_METADATA")
        value["lifecycle"] = state
    if value.get("lifecycle") not in LIFECYCLES:
        raise DocumentError("INVALID_METADATA")
    value.setdefault("security", "INTERNAL")
    if value["security"] not in SECURITY:
        raise DocumentError("INVALID_METADATA")
    for release in _strings(value.get("release_ids"), required=True):
        exact_release(release)
    _strings(value.get("audiences"), required=True)
    for alias in _strings(value.get("aliases", [])):
        _qualified(alias)
    if value["lifecycle"] == "superseded":
        if not value.get("replacement") and not value.get("historical_disposition"):
            raise DocumentError("INVALID_METADATA")
        if value.get("replacement"):
            _qualified(value["replacement"])
            if value["replacement"] == value["doc_id"]:
                raise DocumentError("INVALID_METADATA")
    retention = value.get("retention")
    if (
        not isinstance(retention, dict)
        or not isinstance(retention.get("policy"), str)
        or type(retention.get("hold")) is not bool
    ):
        raise DocumentError("INVALID_METADATA")
    if not isinstance(value.get("dependencies"), list):
        raise DocumentError("INVALID_METADATA")
    if value.get("example", False) not in (True, False):
        raise DocumentError("INVALID_METADATA")
    return value, owner, source, sidecar, body, initial_freshness


def dependency_snapshot(tree, metadata, config):
    result = []
    seen = set()
    for item in metadata["dependencies"]:
        if not isinstance(item, dict) or set(item) != {"repository", "path", "kind"}:
            raise DocumentError("INVALID_METADATA")
        if item["repository"] != config["repository_id"]:
            raise DocumentError("EXTERNAL_REFERENCE_UNAVAILABLE")
        if item["kind"] not in ("code", "contract", "ontology", "binding", "document", "config"):
            raise DocumentError("INVALID_METADATA")
        path = relative_path(item["path"])
        if path.casefold() in seen:
            raise DocumentError("IDENTITY_COLLISION")
        seen.add(path.casefold())
        result.append({**item, "sha256": digest(tree.read(path))})
    return sorted(result, key=lambda x: (x["repository"], x["path"], x["kind"]))


def candidate_content_digest(candidate):
    """Digest of the candidate CONTENT (index entries and working changes).

    The commit id is recorded separately as ``candidate_revision``; keeping it out of the
    content digest means committing already-reviewed content does not change the digest.
    """
    return object_digest({k: v for k, v in candidate.items() if k != "commit"})


def candidate_revision(root):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(  # noqa: UP022 -- capture_output requires Python 3.7; Kit supports 3.6
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DocumentError("CANDIDATE_UNAVAILABLE") from None
    if result.returncode == 0:
        value = result.stdout.decode("ascii", "strict").strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise DocumentError("CANDIDATE_UNAVAILABLE")
        return value
    # Source digests are still mandatory for standalone/unborn local document fixtures.
    if result.returncode == 128:
        return "NO_COMMIT"
    raise DocumentError("CANDIDATE_UNAVAILABLE")


def review_store(tree, config):
    data = tree.read(config["review_store"], missing_ok=True, limit=8388608)
    if data is None:
        return {
            "schema_version": "1.0",
            "reviews": [],
            "history": [],
            "reference_reviews": [],
            "reference_history": [],
        }
    value = strict_json(data)
    if (
        not isinstance(value, dict)
        or set(value)
        - {"schema_version", "reviews", "history", "reference_reviews", "reference_history"}
        or value["schema_version"] != "1.0"
        or not isinstance(value["reviews"], list)
    ):
        raise DocumentError("INVALID_REVIEW_STORE")
    result = value["reviews"]
    if any(not isinstance(x, dict) or not isinstance(x.get("doc_id"), str) for x in result):
        raise DocumentError("INVALID_REVIEW_STORE")
    if len({x["doc_id"] for x in result}) != len(result):
        raise DocumentError("INVALID_REVIEW_STORE")
    value.setdefault("history", [])
    if not isinstance(value["history"], list) or any(
        not isinstance(x, dict) for x in value["history"]
    ):
        raise DocumentError("INVALID_REVIEW_STORE")
    for field in ("reference_reviews", "reference_history"):
        value.setdefault(field, [])
        if not isinstance(value[field], list) or any(not isinstance(x, dict) for x in value[field]):
            raise DocumentError("INVALID_REVIEW_STORE")
    keys = [(x.get("doc_id"), x.get("reference_hash")) for x in value["reference_reviews"]]
    if any(not isinstance(a, str) or not isinstance(b, str) for a, b in keys) or len(keys) != len(
        set(keys)
    ):
        raise DocumentError("INVALID_REVIEW_STORE")
    return value


def reviews(tree, config):
    return review_store(tree, config)["reviews"]


# A review stays bound to the repository content it saw: source, metadata, declared inputs
# and the whole candidate index, so a new tool, a staged edit or an unknown file still
# invalidates it. candidate_revision is only the commit id. Recording a review and then
# committing it must not invalidate the review it just recorded, so the commit id is kept
# as provenance and left out of the match.
REVIEW_PROVENANCE_ONLY_KEYS = ("candidate_revision",)


def review_snapshot_match_view(snapshot):
    if not isinstance(snapshot, dict):
        return None
    return {k: v for k, v in snapshot.items() if k not in REVIEW_PROVENANCE_ONLY_KEYS}


def release_snapshot_digest(record):
    """Digest of a document's snapshot as a release manifest and a view builder must see it.

    Uses the review match view, so the commit id (provenance) never makes a shipped
    release manifest drift from the tree it was built on.
    """
    return object_digest(review_snapshot_match_view(record["snapshot"]))


def review_matches(tree, record, review, config):
    if review_snapshot_match_view(review.get("snapshot")) != review_snapshot_match_view(
        record["snapshot"]
    ) or review.get("outcome") not in (
        "updated",
        "reviewed_unchanged",
    ):
        return False
    for key in ("reason", "reviewer", "method"):
        if not isinstance(review.get(key), str) or not review[key].strip():
            return False
    contract = review.get("contract")
    if contract is not None:
        if not isinstance(contract, dict) or set(contract) != {"path", "sha256"}:
            return False
        try:
            if digest(tree.read(contract["path"])) != contract["sha256"]:
                return False
        except DocumentError:
            return False
    evidence = review.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    excluded = {record["path"], record["path"] + SIDECAR, config["review_store"]}
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            return False
        if item["path"] in excluded or not isinstance(item["sha256"], str):
            return False
        try:
            if digest(tree.read(item["path"])) != item["sha256"]:
                return False
        except DocumentError:
            return False
    return True


def document_source_routes(tree):
    """Explicit non-guide input roles; neither metadata promotion nor deletion permission."""
    policy = strict_json(tree.read(POLICY, limit=8388608))
    routes = policy.get("document_source_routes", [])
    if not isinstance(routes, list) or len(routes) > 100:
        raise DocumentError("INVALID_POLICY")
    seen = set()
    for row in routes:
        if not isinstance(row, dict) or set(row) - {"path", "kind", "owner", "security", "reason"}:
            raise DocumentError("INVALID_POLICY")
        path = relative_path(row.get("path"))
        if path in seen or row.get("kind") not in ("historical", "source_asset"):
            raise DocumentError("INVALID_POLICY")
        seen.add(path)
        if row.get("security") not in SECURITY:
            raise DocumentError("INVALID_POLICY")
        preservation_text(row.get("reason"))
        if row["kind"] == "source_asset":
            relative_path(row.get("owner"))
        elif "owner" in row:
            raise DocumentError("INVALID_POLICY")
    return routes


def owned_source_assets(tree, owner, config):
    result, seen = [], set()
    total_bytes = 0
    for row in document_source_routes(tree):
        if row["kind"] != "source_asset" or row["owner"] != owner:
            continue
        if not security_allowed(
            row["security"], config["maximum_security"], config["maximum_security"]
        ):
            raise DocumentError("DISCLOSURE_DENIED")
        for path in tree.paths([row["path"]], config["max_files"]):
            if not path.endswith(".md") or path == owner or path in seen:
                continue
            total_bytes += tree.info(path).st_size
            if total_bytes > config["max_total_bytes"]:
                raise DocumentError("SCOPE_INCOMPLETE")
            seen.add(path)
            result.append(source_record(tree, path, config))
    if len(result) > config["max_files"]:
        raise DocumentError("SCOPE_INCOMPLETE")
    return sorted(result, key=lambda x: x["path"])


def route_document_sources(tree, discovered, by_path, config, base):
    routes = document_source_routes(tree)
    routed = []
    for path in sorted(list(discovered)):
        if path in by_path:
            continue
        item = discovered[path]
        if "impact_rules" in item["axes"]:
            raise DocumentError("DOCUMENT_METADATA_REQUIRED")
        matches = [r for r in routes if path == r["path"] or path.startswith(r["path"] + "/")]
        if not matches:
            raise DocumentError("DOCUMENT_METADATA_REQUIRED")
        row = max(matches, key=lambda x: len(x["path"]))
        if not security_allowed(
            row["security"], config["maximum_security"], config["maximum_security"]
        ):
            raise DocumentError("DISCLOSURE_DENIED")
        source = source_record(tree, path, config)
        if source["state"] != "PRESENT":
            raise DocumentError("UNSUPPORTED_SOURCE")
        entry = {
            "path": path,
            "kind": row["kind"],
            "security": row["security"],
            "reason": row["reason"],
            "source": source,
            "current_guide": False,
        }
        if row["kind"] == "historical":
            size = strict_git(tree.root, ["cat-file", "-s", base + ":" + path])
            if int(size) > config["max_file_bytes"]:
                raise DocumentError("SCOPE_INCOMPLETE")
            original = strict_git(tree.root, ["cat-file", "blob", base + ":" + path])
            if digest(original) != source["sha256"]:
                raise DocumentError("HISTORICAL_SOURCE_CHANGED")
            entry["retained_baseline"] = {"commit": base, "sha256": digest(original)}
        else:
            owner = row["owner"]
            record = by_path.get(owner)
            if (
                not record
                or record["metadata"]["authority"] != "canonical"
                or record["metadata"]["lifecycle"] != "active"
            ):
                raise DocumentError("DOCUMENT_METADATA_REQUIRED")
            if not security_allowed(
                record["metadata"]["security"],
                config["maximum_security"],
                config["maximum_security"],
            ):
                raise DocumentError("DISCLOSURE_DENIED")
            if source not in record["source_assets"]:
                raise DocumentError("SOURCE_DRIFT")
            target = discovered.setdefault(owner, {"axes": [], "reasons": [], "evidence": []})
            target["axes"].append("source_asset_owner")
            target["reasons"].append("Current owner review includes exact development source bytes")
            target["evidence"].append(path)
            entry["owner"] = owner
        del discovered[path]
        routed.append(entry)
    return routed


def _document(tree, path, config, rules, review_records, revision, candidate_hash=None):
    metadata, owner, source, sidecar, body, freshness = authored_metadata(tree, path, config)
    dependencies = dependency_snapshot(tree, metadata, config)
    declared = declared_reference_snapshot(tree, path, body, config)
    source_assets = owned_source_assets(tree, path, config)
    snapshot = {
        "source_sha256": digest(source),
        "metadata_sha256": object_digest(metadata),
        "metadata_owner": owner,
        "sidecar_sha256": digest(sidecar) if sidecar is not None else None,
        "dependency_snapshot_sha256": object_digest(dependencies),
        "declared_reference_sha256": object_digest(declared),
        "source_assets_sha256": object_digest(source_assets),
        "candidate_revision": revision,
        "candidate_sha256": candidate_hash
        or candidate_content_digest(candidate_snapshot(tree, config)),
        "rules": rules,
    }
    record = {
        "path": path,
        "metadata": metadata,
        "metadata_owner": owner,
        "source_sha256": digest(source),
        "snapshot": snapshot,
        "dependencies": dependencies,
        "declared_dependencies": declared,
        "source_assets": source_assets,
        "freshness": freshness,
        "body": body,
    }
    matched = next((r for r in review_records if r["doc_id"] == metadata["doc_id"]), None)
    if matched is not None:
        record["freshness"] = (
            "verified"
            if freshness != "stale" and review_matches(tree, record, matched, config)
            else "stale"
        )
    if record["freshness"] == "verified":
        findings = scan_broken_references(tree.root, strict_json(tree.read(POLICY)), [path])
        dispositions = review_store(tree, config)["reference_reviews"]
        if any(
            not any(
                reference_review_matches(tree, record, finding, entry, config)
                for entry in dispositions
            )
            for finding in findings
        ):
            record["freshness"] = "stale"
    return record


def read_document(root, path):
    """Internal authoring/review input, never returned directly to an audience."""
    with SourceTree(root) as tree:
        config, rules = configuration(tree)
        relative_path(path)
        if not any(
            path == base or path.startswith(base.rstrip("/") + "/") for base in config["roots"]
        ):
            raise DocumentError("SCOPE_INCOMPLETE")
        return _document(
            tree,
            relative_path(path),
            config,
            rules,
            reviews(tree, config),
            candidate_revision(tree.root),
        )


def _catalog(tree, config, rules):
    paths = tree.paths(config["roots"], config["max_files"], config["optional_roots"])
    sources = [p for p in paths if p.lower().endswith(".md")]
    if any(p.endswith(SIDECAR) and p[: -len(SIDECAR)] not in sources for p in paths):
        raise DocumentError("MISSING_SOURCE")
    review_records = reviews(tree, config)
    revision = candidate_revision(tree.root)
    candidate = candidate_snapshot(tree, config)
    candidate_hash = candidate_content_digest(candidate)
    records = []
    ids = set()
    total_bytes = 0
    for path in sources:
        total_bytes += tree.info(path).st_size
        sidecar_info = tree.info(path + SIDECAR, missing_ok=True)
        total_bytes += sidecar_info.st_size if sidecar_info is not None else 0
        if total_bytes > config["max_total_bytes"]:
            raise DocumentError("SCOPE_INCOMPLETE")
        record = _document(tree, path, config, rules, review_records, revision, candidate_hash)
        metadata = record["metadata"]
        for identity in [metadata["doc_id"], *metadata.get("aliases", [])]:
            if identity in ids:
                raise DocumentError("IDENTITY_COLLISION")
            ids.add(identity)
        records.append(record)
    # Re-read membership and all exact inputs before exposing a selected snapshot.
    if paths != tree.paths(config["roots"], config["max_files"], config["optional_roots"]):
        raise DocumentError("SOURCE_DRIFT")
    for record in records:
        path = record["path"]
        if digest(tree.read(path)) != record["source_sha256"]:
            raise DocumentError("SOURCE_DRIFT")
        sidecar = tree.read(path + SIDECAR, missing_ok=True)
        if (digest(sidecar) if sidecar is not None else None) != record["snapshot"][
            "sidecar_sha256"
        ]:
            raise DocumentError("SOURCE_DRIFT")
        if dependency_snapshot(tree, record["metadata"], config) != record["dependencies"]:
            raise DocumentError("SOURCE_DRIFT")
        if owned_source_assets(tree, path, config) != record["source_assets"]:
            raise DocumentError("SOURCE_DRIFT")
        if (
            declared_reference_snapshot(tree, path, record["body"], config)
            != record["declared_dependencies"]
        ):
            raise DocumentError("SOURCE_DRIFT")
    if (
        configuration(tree) != (config, rules)
        or reviews(tree, config) != review_records
        or candidate_revision(tree.root) != revision
        or candidate_snapshot(tree, config) != candidate
    ):
        raise DocumentError("SOURCE_DRIFT")
    return records


def security_allowed(classification, requested, maximum):
    return (
        classification in SECURITY
        and requested in SECURITY
        and maximum in SECURITY
        and SECURITY.index(classification) <= SECURITY.index(requested) <= SECURITY.index(maximum)
    )


def reference_uri(value, syntax="markdown"):
    """Decode source syntax once, before interpreting URI separators."""
    if syntax == "markdown":
        value = value[1:-1] if value.startswith("<") and value.endswith(">") else value
        # Simultaneous substitution matters: \&amp; means literal &amp;, not &.
        value = MARKDOWN_URI_ESCAPE.sub(
            lambda match: (
                match.group(1) if match.group(1) is not None else html.unescape(match.group(0))
            ),
            value,
        )
    elif syntax == "html":
        value = html.unescape(value)
    else:
        raise DocumentError("UNSUPPORTED_PARSER")
    # urlsplit strips some raw whitespace on newer Python versions. Reject that
    # ambiguity before parsing; percent-encoded filename spaces remain exact.
    if value != value.strip(" ") or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DocumentError("UNSUPPORTED_PARSER")
    return value


def local_markdown_target(value, syntax="markdown"):
    """Exact URI path; prose punctuation/editor locators never process this."""
    value = reference_uri(value, syntax)
    try:
        parsed = urlsplit(value)
        if parsed.scheme:
            # A scheme is not proof that a reference is non-local. file: names
            # local files; data/javascript may hide another reference language.
            if parsed.scheme not in ("http", "https", "mailto") or (
                parsed.scheme in ("http", "https") and not parsed.netloc
            ):
                raise DocumentError("UNSUPPORTED_PARSER")
            return None
        if parsed.netloc or not parsed.path:
            return None
        path = unquote(parsed.path, errors="strict")
    except (ValueError, UnicodeError):
        raise DocumentError("UNSUPPORTED_PARSER") from None
    if (
        "\\" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path)
    ):
        raise DocumentError("UNSUPPORTED_PARSER")
    return path


def reference_visibility(tree, config, records, source_path, text, security, allowed_paths=None):
    """Check both URL syntax and typed local references before deriving output."""
    by_path = {row["path"]: row["metadata"] for row in records}
    controls = {row["path"]: row for row in config["governing_inputs"]}
    routes = document_source_routes(tree)
    error = (
        "CROSS_BOUNDARY_VIEW_REFERENCE" if allowed_paths is not None else "CROSS_BOUNDARY_REFERENCE"
    )
    checked, expanded_targets = {}, {}
    spend = reference_walk_budget(config["max_files"])

    def check_classification(path, directory=False):
        if path not in checked:
            classifications = []
            resolved = rel(tree.root, safe_path(tree.root, path))
            # An alias is not a new security authority. Combine every known
            # logical-path label with the actual target's metadata and routes.
            for name in sorted({os.path.normpath(path).replace(os.sep, "/"), resolved}):
                if name in by_path:
                    classifications.append(by_path[name]["security"])
                elif name == resolved and not directory and name.lower().endswith(".md"):
                    try:
                        metadata = authored_metadata(tree, name, config)[0]
                    except DocumentError as exc:
                        if exc.code != "UNCLASSIFIED_DOCUMENT":
                            raise DocumentError(error) from None
                    else:
                        classifications.append(metadata["security"])
                if name in controls:
                    classifications.append(controls[name]["security"])
                classifications.extend(
                    row["security"]
                    for row in routes
                    if name == row["path"] or name.startswith(row["path"].rstrip("/") + "/")
                )
            if any(
                not security_allowed(level, security, config["maximum_security"])
                for level in classifications
            ):
                raise DocumentError(error)
            if not classifications and not directory:
                raise DocumentError(error)
            checked[path] = bool(classifications)
        return checked[path]

    # Literal/code URL examples still carry identifiers. Typed dependency
    # extraction intentionally excludes some of those examples: keep this union.
    for token in document_reference_tokens(text):
        target = local_markdown_target(token["destination"], token["syntax"])
        if target is None:
            continue
        candidates = reference_candidates(
            tree.root, os.path.dirname(source_path), target, "markdown_link"
        )
        if len(candidates) != 1:
            raise DocumentError(error)
        path = candidates[0]
        spend()
        check_classification(path)
        if allowed_paths is not None and path.lower().endswith(".md") and path not in allowed_paths:
            raise DocumentError(error)

    budget = ReferenceBudget(text)
    local_roots = reference_local_roots(tree.root)
    try:
        for raw in extract_typed_references(text):
            reference = classify_document_reference(raw, local_roots, budget=budget)
            if reference["kind"] != "repository_path" or raw["source_kind"] == "markdown_link":
                continue
            candidates = reference_candidates(
                tree.root,
                os.path.dirname(source_path),
                reference["reference"],
                raw["source_kind"],
                reject_unsafe=True,
            )
            if not candidates:
                raise DocumentError(error)
            found = False
            for candidate in candidates:
                if candidate not in expanded_targets:
                    expanded_targets[candidate] = expand_repo_target(
                        tree.root, candidate, include_directories=True, spend=spend
                    )
                for path in expanded_targets[candidate]:
                    check_reference_candidate(tree.root, path, spend=spend)
                    full = safe_path(tree.root, path)
                    if not os.path.exists(full):
                        continue
                    found = True
                    if os.path.isdir(full):
                        members = list(declared_directory_paths(tree.root, path, spend=spend))
                        covered = set()
                        # A structural directory inherits only its finite,
                        # completely classified members. An unknown empty
                        # directory is not implicitly PUBLIC; explicit labels
                        # on any directory/member still constrain the whole set.
                        for member in reversed(members):
                            directory = os.path.isdir(safe_path(tree.root, member))
                            classified = check_classification(member, directory=directory)
                            if not classified and member not in covered:
                                raise DocumentError(error)
                            covered.add(os.path.dirname(member))
                    elif os.path.isfile(full):
                        check_classification(path)
                    else:
                        raise DocumentError(error)
            # Missing optional examples keep their existing review semantics.
            # Ignore/known-absent hints never exempt an existing local target.
            if not found and reference["required"]:
                raise DocumentError(error)
    except DocumentError as exc:
        if exc.code == "SCOPE_INCOMPLETE":
            raise
        raise DocumentError(error) from None
    except (OSError, RuntimeError):
        raise DocumentError(error) from None


def select_documents(
    root, release_id, security="INTERNAL", history=False, query="", consumer="inventory"
):
    """Only filtered records can produce identities, titles, snippets or ranking."""
    exact_release(release_id)
    if consumer not in CURRENT_CONSUMERS or type(history) is not bool or not isinstance(query, str):
        raise DocumentError("INVALID_SELECTION")
    with SourceTree(root) as tree:
        config, rules = configuration(tree)
        if not security_allowed(security, security, config["maximum_security"]):
            raise DocumentError("DISCLOSURE_DENIED")
        records = _catalog(tree, config, rules)
        candidates = []
        canonical = set()
        for record in records:
            meta = record["metadata"]
            if (
                not security_allowed(meta["security"], security, config["maximum_security"])
                or release_id not in meta["release_ids"]
            ):
                continue
            if meta["authority"] == "canonical" and meta["lifecycle"] == "active":
                key = (meta["topic_id"], release_id, meta["security"])
                if key in canonical:
                    raise DocumentError("CANONICAL_CONFLICT")
                canonical.add(key)
            if not history and (meta["lifecycle"] != "active" or record["freshness"] != "verified"):
                continue
            candidates.append(record)
        for record in candidates:
            reference_visibility(
                tree,
                config,
                records,
                record["path"],
                record["metadata"]["title"] + "\n" + record["body"],
                security,
            )
        tokens = set(re.findall(r"\w+", query.casefold()))
        documents = []
        for record in candidates:
            meta = record["metadata"]
            # The first display/ranking derivation occurs after the complete filter.
            haystack = (meta["title"] + "\n" + record["body"]).casefold()
            score = sum(1 for token in tokens if token in haystack)
            if tokens and score == 0:
                continue
            snippet = re.sub(r"\s+", " ", record["body"]).strip()[:240]
            documents.append(
                {
                    "doc_id": meta["doc_id"],
                    "topic_id": meta["topic_id"],
                    "path": record["path"],
                    "title": meta["title"],
                    "snippet": snippet,
                    "authority": meta["authority"],
                    "lifecycle": meta["lifecycle"],
                    "freshness": record["freshness"],
                    "security": meta["security"],
                    "release_id": release_id,
                    "source_sha256": record["source_sha256"],
                    "snapshot_sha256": release_snapshot_digest(record),
                    "score": score,
                }
            )
        documents.sort(key=lambda x: (-x["score"], x["doc_id"]))
        result = {
            "schema_version": "1.0",
            "consumer": consumer,
            "mode": "history" if history else "current",
            "release_id": release_id,
            "security": security,
            "query_sha256": digest(query.encode("utf-8")),
            "documents": documents,
            "count": len(documents),
            "complete": True,
        }
        # This is a digest of the filtered view only, never a private path inventory.
        result["content_hash"] = object_digest(result)
        return result


def validate_index(root, value, query=""):
    try:
        if not isinstance(value, dict) or value.get("consumer") not in CURRENT_CONSUMERS:
            raise DocumentError("INVALID_INDEX")
        current = select_documents(
            root,
            value.get("release_id"),
            value.get("security"),
            value.get("mode") == "history",
            query,
            value.get("consumer"),
        )
        if current != value:
            raise DocumentError("SOURCE_DRIFT")
        return {"valid": True, "code": "MATCH"}
    except DocumentError as exc:
        return {"valid": False, "code": exc.code}


def scope_applies(contract, item):
    scope = (contract.get("scope") or {}).get("include") or []
    item_scope = item.get("scope") or []
    return (
        not scope
        or not item_scope
        or any(a.startswith(b) or b.startswith(a) for a in scope for b in item_scope)
    )


def indexed_decisions(tree, contract, security):
    value = strict_json(tree.read(".ai-team/knowledge/decisions.index.json", limit=8388608))
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise DocumentError("INVALID_DECISION_INDEX")
    selected = {}
    seen = set()
    for item in value["entries"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise DocumentError("INVALID_DECISION_INDEX")
        if item["id"] in seen:
            raise DocumentError("INVALID_DECISION_INDEX")
        seen.add(item["id"])
        if (
            item.get("status") == "active"
            and scope_applies(contract, item)
            and security_allowed(item.get("security", "INTERNAL"), security, security)
        ):
            relative_path(item.get("path"))
            selected[(item["id"], item["path"])] = item
    return selected


def governing_source(tree, item, config, security):
    try:
        source = tree.read(item["path"])
        metadata_hash = None
        if item["path"].lower().endswith(".md"):
            try:
                meta, _, _, sidecar, _, _ = authored_metadata(tree, item["path"], config)
            except DocumentError as exc:
                if exc.code != "UNCLASSIFIED_DOCUMENT":
                    raise
            else:
                if not security_allowed(meta["security"], security, config["maximum_security"]):
                    raise DocumentError("DISCLOSURE_DENIED")
                if item["role"] == "instruction" and meta["lifecycle"] != "active":
                    raise DocumentError("INACTIVE_GOVERNING_INPUT")
                metadata_hash = digest(sidecar) if sidecar is not None else digest(source)
        return {"source_sha256": digest(source), "metadata_sha256": metadata_hash}
    except DocumentError:
        raise DocumentError("GOVERNING_INPUT_UNAVAILABLE") from None


def governing_snapshot(tree, config, contract, security):
    """Release-independent, explicit authority only; not a guide/search inventory."""
    declared = config["governing_inputs"]
    decisions = indexed_decisions(tree, contract, security) if declared else {}
    decision_paths = {item["path"] for item in decisions.values()}
    inputs = []
    for item in declared:
        if not security_allowed(item["security"], security, config["maximum_security"]):
            continue
        if item["role"] == "decision_ledger" and item["path"] not in decision_paths:
            continue
        inputs.append({**item, **governing_source(tree, item, config, security)})
    return sorted(inputs, key=lambda item: item["path"])


def context_selection(root, contract):
    with SourceTree(root) as tree:
        config, rules = configuration(tree)
    requested = contract.get("document_selection") or {}
    if not isinstance(requested, dict) or set(requested) - {"release_id", "security"}:
        raise DocumentError("INVALID_SELECTION")
    release_id = requested.get("release_id", config["current_release_id"])
    security = requested.get("security", config["default_security"])
    if not security_allowed(security, security, config["maximum_security"]):
        raise DocumentError("DISCLOSURE_DENIED")
    if release_id is None:
        # No implicit 'latest'. Legacy Markdown remains on disk, not verified guidance.
        value = {
            "documents": [],
            "release_id": None,
            "security": security,
            "rules": rules,
            "document_guidance": "NOT_SELECTED_WITHOUT_EXACT_RELEASE",
        }
    else:
        value = select_documents(root, release_id, security, consumer="loop-context")
    with SourceTree(root) as tree:
        if configuration(tree) != (config, rules):
            raise DocumentError("SOURCE_DRIFT")
        value["governing_inputs"] = governing_snapshot(tree, config, contract, security)
    value["rules"] = rules
    return value


def knowledge_filter(root, contract):
    """One predicate shared by source, claim and decision selection before output."""
    selection = context_selection(root, contract)
    paths = {x["path"] for x in selection["documents"]}
    security = selection["security"]
    with SourceTree(root) as tree:
        config, _ = configuration(tree)
        decisions = indexed_decisions(tree, contract, security)
    governing = {x["path"]: x for x in config["governing_inputs"]}

    def accepted(item, evidence_paths=None, kind="source"):
        if item.get("status") != "active":
            return False
        if not security_allowed(item.get("security", "INTERNAL"), security, security):
            return False
        values = evidence_paths if evidence_paths is not None else [item.get("path")]
        if not values:
            return False
        with SourceTree(root) as tree:
            for reference in values:
                path = reference.get("path") if isinstance(reference, dict) else reference
                if not isinstance(path, str) or not path:
                    return False
                try:
                    relative_path(path)
                    control = governing.get(path)
                    if control:
                        if not security_allowed(control["security"], security, security):
                            if kind == "decision":
                                raise DocumentError("GOVERNING_INPUT_UNAVAILABLE")
                            return False
                        if control["role"] == "decision_ledger":
                            identity = (
                                item.get("id")
                                if kind == "decision"
                                else (
                                    reference.get("locator")
                                    if kind == "claim"
                                    and isinstance(reference, dict)
                                    and reference.get("type") == "decision"
                                    else None
                                )
                            )
                            if (identity, path) not in decisions:
                                return False
                        governing_source(tree, control, config, security)
                        continue
                    if path.lower().endswith(".md"):
                        if path not in paths:
                            if kind == "decision":
                                # A classified inactive guide stays filtered. An unknown
                                # governing ledger must not silently erase an active ADR.
                                read_document(root, path)
                            return False
                    else:
                        info = tree.info(path, missing_ok=True)
                        if info is None or not stat.S_ISREG(info.st_mode):
                            return False
                except DocumentError:
                    if kind == "decision":
                        raise DocumentError("GOVERNING_INPUT_UNAVAILABLE") from None
                    return False
        return True

    return accepted, selection


def atomic_json(tree, path, value, expected, before_publish=None, max_bytes=8388608):
    """Replace one generated record; callers serialize cooperating writers."""

    def record_state(info):
        if info is None:
            return None
        return (
            _identity(info),
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    # The exact UTF-8/pretty-print bytes must fit their reader before any stage
    # exists. Oversized accumulated history must not replace a readable owner.
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(body) > max_bytes:
        raise DocumentError("SCOPE_INCOMPLETE")
    parent, name, chain = tree._parents(path)
    temporary = name + ".pending-" + uuid.uuid4().hex
    created = False
    try:
        mode = 0o600
        info = tree.info(path, missing_ok=True)
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                raise DocumentError("UNSAFE_PATH")
            mode = stat.S_IMODE(info.st_mode)
        original = record_state(info)
        if (info is None) != (expected is None):
            raise DocumentError("SOURCE_DRIFT")
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        created = True
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            staged = os.fstat(handle.fileno())
            if info is not None and (staged.st_uid, staged.st_gid) != (info.st_uid, info.st_gid):
                # Never silently change ownership/group while preserving mode.
                # If this caller cannot retain them, fail before publication.
                os.fchown(handle.fileno(), info.st_uid, info.st_gid)
            # open's mode is filtered by umask. The validated intended mode must
            # be set on the actual descriptor, including the private new default.
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        if before_publish:
            before_publish()
        if (
            record_state(tree.info(path, missing_ok=True)) != original
            or tree.read(path, missing_ok=True, limit=max_bytes) != expected
            or record_state(tree.info(path, missing_ok=True)) != original
        ):
            raise DocumentError("SOURCE_DRIFT")
        tree._check(chain)
        if expected is None:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent)
        else:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        created = False
        os.fsync(parent)
        tree._check(chain)
    finally:
        if created:
            os.unlink(temporary, dir_fd=parent)
        for _, _, fd in reversed(chain):
            os.close(fd)


@contextlib.contextmanager
def review_lock(tree):
    try:
        fcntl.flock(tree.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise DocumentError("REVIEW_BUSY") from None
    try:
        yield
    finally:
        fcntl.flock(tree.fd, fcntl.LOCK_UN)


def impact(root, feature, adapter, changed=None, write=True, acknowledged=None):
    """Adapt existing discovery to exact documents and one external review owner."""
    with SourceTree(root) as tree:
        config, rules = configuration(tree)
        feature_rel = relative_path(os.path.relpath(feature, tree.root).replace(os.sep, "/"))
        contract_path = feature_rel + "/work-contract.json"
        contract_bytes = tree.read(contract_path)
        contract = strict_json(contract_bytes)
        policy = strict_json(tree.read(POLICY))
        target = feature_rel + "/doc-impact.json"
        previous_bytes = tree.read(target, missing_ok=True)
        if previous_bytes is not None:
            previous = strict_json(previous_bytes)
            if not isinstance(previous, dict) or previous.get("content_hash") != adapter[
                "object_sha"
            ](previous):
                raise DocumentError("INVALID_IMPACT_REPORT")
        base = adapter["documentation_base_commit"](str(root), str(feature), contract)
        if not base:
            base = candidate_revision(tree.root)
        if base == "NO_COMMIT":
            raise DocumentError("VERIFIABLE_BASELINE_REQUIRED")
        all_changed = changed_paths(tree.root, base)
        # The view being generated and its own temporary file are not inputs to
        # that same view. Other role exclusions remain explicit in the ledger.
        all_changed = [
            p for p in all_changed if p != target and not p.startswith(target + ".pending-")
        ]
        if changed is not None and set(changed) != set(all_changed):
            raise DocumentError("SCOPE_INCOMPLETE")
        excluded = {
            p: candidate_exclusion(p, config) for p in all_changed if candidate_exclusion(p, config)
        }
        sources = [p for p in all_changed if p not in excluded]
        if len(all_changed) > config["max_files"]:
            raise DocumentError("SCOPE_INCOMPLETE")
        semantic, other = adapter["split_semantic_changes"](sources, policy)
        records = _catalog(tree, config, rules)
        by_path = {r["path"]: r for r in records}
        canonical = set()
        for record in records:
            meta = record["metadata"]
            if not security_allowed(
                meta["security"], config["maximum_security"], config["maximum_security"]
            ):
                continue
            if meta["authority"] == "canonical" and meta["lifecycle"] == "active":
                for release_id in meta["release_ids"]:
                    key = (meta["topic_id"], release_id, meta["security"])
                    if key in canonical:
                        raise DocumentError("CANONICAL_CONFLICT")
                    canonical.add(key)
        discovered = (
            adapter["discover_impacted_documents"](str(root), policy, semantic, contract)
            if semantic
            else {}
        )
        for record in records:
            path = record["path"]
            dependencies = {
                d["path"] for d in record["dependencies"] + record["declared_dependencies"]
            }
            if (
                path in sources
                or path + SIDECAR in sources
                or any(
                    d == p or d.startswith(p.rstrip("/") + "/")
                    for d in dependencies
                    for p in sources
                )
            ):
                discovered.setdefault(path, {"axes": [], "reasons": [], "evidence": []})
                discovered[path]["axes"].append("declared_document_dependency")
                discovered[path]["reasons"].append(
                    "Owner source/metadata or exact declared dependency changed"
                )
                discovered[path]["evidence"].append(path)
        discovered = {
            p: value
            for p, value in discovered.items()
            if not adapter["work_scoped_artifact"](policy, p)
        }
        routed_sources = route_document_sources(tree, discovered, by_path, config, base)
        configured_scan = set((policy.get("reference_scan") or {}).get("documents") or [])
        for record in records:
            if (
                record["path"] in discovered
                or record["path"] in sources
                or record["path"] in configured_scan
            ) and not security_allowed(
                record["metadata"]["security"],
                config["maximum_security"],
                config["maximum_security"],
            ):
                raise DocumentError("DISCLOSURE_DENIED")
        candidate = candidate_snapshot(tree, config)
        snapshot = {
            "base_commit": base,
            "source_role_records": routed_sources,
            "contract_sha256": digest(contract_bytes),
            "rules": rules,
            # Content only: the commit id is provenance (candidate_revision on each record),
            # so committing reviewed content does not move this hash.
            "candidate": {
                "mode": candidate["mode"],
                "sha256": candidate_content_digest(candidate),
                "complete": candidate["complete"],
            },
            "sources": [source_record(tree, p, config) for p in sources],
            "documents": [
                {"path": p, "snapshot": review_snapshot_match_view(by_path[p]["snapshot"])}
                for p in sorted(discovered)
            ],
        }
        snapshot_hash = object_digest(snapshot)
        documents = []
        review_data = review_store(tree, config)
        review_records = [r for r in review_data["reviews"] if r.get("path") in discovered]
        reference_reviews = [
            r for r in review_data["reference_reviews"] if r.get("path") in discovered
        ]
        for path in sorted(discovered):
            record = by_path[path]
            meta = record["metadata"]
            review = next((r for r in review_records if r["doc_id"] == meta["doc_id"]), None)
            handled = bool(
                review
                and record["freshness"] == "verified"
                and review.get("impact_snapshot_sha256") == snapshot_hash
            )
            item = discovered[path]
            documents.append(
                {
                    "path": path,
                    "doc_id": meta["doc_id"],
                    "category": meta["kind"],
                    "reason": "; ".join(item["reasons"]),
                    "action": "acknowledge"
                    if handled and review["outcome"] == "reviewed_unchanged"
                    else "update",
                    "state": "ACTIVE" if handled else "STALE",
                    "handled": handled,
                    "discovered_by": sorted(set(item["axes"])),
                    "evidence": sorted(set(item["evidence"])),
                    "document_sha256": record["source_sha256"],
                    "record_snapshot": record["snapshot"],
                    "review_hash": object_digest(review) if handled else None,
                }
            )
        scan_docs = sorted(
            set((policy.get("reference_scan") or {}).get("documents") or []) | set(discovered)
        )
        broken = adapter["scan_broken_references"](str(root), policy, scan_docs)
        for finding in broken:
            record = by_path.get(finding["document"])
            if record and not security_allowed(
                record["metadata"]["security"],
                config["maximum_security"],
                config["maximum_security"],
            ):
                raise DocumentError("DISCLOSURE_DENIED")
            matched = next(
                (
                    entry
                    for entry in reference_reviews
                    if record
                    and entry.get("impact_snapshot_sha256") == snapshot_hash
                    and reference_review_matches(tree, record, finding, entry, config)
                ),
                None,
            )
            finding["review_disposition_valid"] = matched is not None
            finding["disposition"] = matched["disposition"] if matched else None
            finding["disposition_eligible"] = reference_disposition_allowed(finding)
        unresolved_broken = [x for x in broken if not x["review_disposition_valid"]]
        for row in routed_sources:
            if source_record(tree, row["path"], config) != row["source"]:
                raise DocumentError("SOURCE_DRIFT")
        status = (
            "STALE"
            if unresolved_broken or any(not r["handled"] for r in documents)
            else ("RESOLVED" if documents else "NOT_APPLICABLE")
        )
        value = {
            "schema_version": "1.0",
            "work_id": contract["id"],
            "status": status,
            "reason": "Exact document/dependency review required"
            if status == "STALE"
            else "Current complete snapshot evaluated",
            "semantic_change": bool(semantic),
            "changed_sources": semantic,
            "non_semantic_sources": other,
            "impacted_documents": documents,
            "source_role_records": routed_sources,
            "reviews": review_records,
            "reference_reviews": reference_reviews,
            "review_authority": config["review_store"],
            "acknowledged": sorted(acknowledged or []),
            "legacy_acknowledgements_accepted": False,
            "dependency_snapshot": snapshot,
            "dependency_snapshot_hash": snapshot_hash,
            "scan_complete": True,
            "completeness": {
                "expected": all_changed,
                "processed": sources,
                "excluded": excluded,
                "unprocessed": [],
                "rule_sha256": rules["policy_sha256"],
            },
            "projection_exclusion": {
                "path": target,
                "reason": "this derived view and its atomic publication stage",
            },
            "broken_references": broken,
            "evidence": [
                "complete_git_scope",
                "declared_dependencies",
                "existing_discovery",
                "external_exact_reviews",
            ],
            "unresolved_broken_references": unresolved_broken,
            "generated_from": {
                "commit": candidate["commit"],
                "policy_sha256": rules["policy_sha256"],
                "contract_sha256": digest(contract_bytes),
            },
        }
        value["content_hash"] = adapter["object_sha"](value)
        if write:

            def recheck_projection():
                current = impact(
                    root, feature, adapter, changed=changed, write=False, acknowledged=acknowledged
                )
                if current != value:
                    raise DocumentError("SOURCE_DRIFT")

            atomic_json(
                tree,
                target,
                value,
                previous_bytes,
                before_publish=recheck_projection,
                max_bytes=config["max_file_bytes"],
            )
        return value


def declared_reference_snapshot(tree, path, body, config):
    """Bind required prose, directory/glob membership and ignored local inputs."""
    policy = strict_json(tree.read(POLICY, limit=8388608))
    scan = policy.get("reference_scan") or {}
    local_roots = reference_local_roots(tree.root, policy)
    dependencies = set()
    classification_budget = ReferenceBudget(body)
    spend = reference_walk_budget(config["max_files"])
    for raw in extract_typed_references(body):
        reference = classify_document_reference(raw, local_roots, budget=classification_budget)
        if reference["kind"] == "external_dependency":
            raise DocumentError("EXTERNAL_REFERENCE_UNAVAILABLE")
        if reference["kind"] != "repository_path":
            continue
        name = reference["reference"]
        if not reference["required"] and matches_any(
            name, scan.get("known_absent", []) + scan.get("ignore_patterns", [])
        ):
            continue
        candidates = reference_candidates(
            tree.root, os.path.dirname(path), name, reference["source_kind"]
        )
        if not candidates and reference["required"]:
            raise DocumentError("EXTERNAL_REFERENCE_UNAVAILABLE")
        found = False
        missing = []
        for candidate in candidates:
            for expanded in expand_repo_target(
                tree.root,
                candidate,
                include_directories=True,
                literal=reference["source_kind"] == "markdown_link",
                spend=spend,
            ):
                full = safe_path(tree.root, expanded)
                if not os.path.exists(full):
                    missing.append(expanded)
                    continue
                found = True
                if os.path.isdir(full):
                    for child in declared_directory_paths(tree.root, expanded, spend=spend):
                        # A structural directory cannot consume the review that
                        # this operation is publishing. Only the configured
                        # result and its transaction have this output role;
                        # explicit file references below remain content-bound.
                        if child != expanded.rstrip("/") and review_output_path(child, config):
                            continue
                        # A non-required folder hint is not a dependency on the
                        # Work's own reports/transactions. Keep the directory
                        # identity and all non-generated members; explicit file
                        # and required-directory references remain fully bound.
                        if (
                            child != expanded.rstrip("/")
                            and not reference["required"]
                            and candidate_exclusion(child, config)
                        ):
                            continue
                        dependencies.add(child)
                        if len(dependencies) > config["max_files"]:
                            raise DocumentError("SCOPE_INCOMPLETE")
                else:
                    dependencies.add(expanded)
        if not found:
            if reference["required"]:
                raise DocumentError("MISSING_SOURCE")
            dependencies.update(missing)
    if len(dependencies) > config["max_files"]:
        raise DocumentError("SCOPE_INCOMPLETE")
    total_bytes = 0
    records = []
    for dependency in sorted(dependencies):
        full = safe_path(tree.root, dependency)
        if os.path.isfile(full):
            size = os.stat(full).st_size
            if size > config["max_file_bytes"]:
                raise DocumentError("SCOPE_INCOMPLETE")
            total_bytes += size
            if total_bytes > config["max_total_bytes"]:
                raise DocumentError("SCOPE_INCOMPLETE")
        records.append(dependency_path_record(tree.root, dependency))
    return records


def reference_disposition_allowed(finding):
    return (
        finding.get("source_kind") == "inline_code"
        and finding.get("kind") == "repository_path"
        and not finding.get("required")
        and finding.get("line", 0) > 0
    )


def reference_finding_hash(finding):
    return object_digest(
        {
            key: finding.get(key)
            for key in (
                "document",
                "reference",
                "raw_reference",
                "line",
                "source_kind",
                "kind",
                "required",
            )
        }
    )


def reference_review_matches(tree, record, finding, review, config):
    return (
        reference_disposition_allowed(finding)
        and review.get("doc_id") == record["metadata"]["doc_id"]
        and review.get("reference_hash") == reference_finding_hash(finding)
        and review.get("disposition") in ("historical", "external", "example")
        and review_matches(tree, record, {**review, "outcome": "reviewed_unchanged"}, config)
    )


def review_batch(root, feature, request, adapter):
    """One locked canonical submission; doc-impact.json is a derived projection."""
    if (
        not isinstance(request, dict)
        or set(request) != {"snapshot", "reviews", "reference_reviews"}
        or not isinstance(request["snapshot"], str)
        or not isinstance(request["reviews"], list)
        or not isinstance(request["reference_reviews"], list)
        or not (request["reviews"] or request["reference_reviews"])
    ):
        raise DocumentError("INVALID_REVIEW_REQUEST")
    with SourceTree(root) as tree, review_lock(tree):
        config, _ = configuration(tree)
        current = impact(root, feature, adapter, write=False)
        if current["dependency_snapshot_hash"] != request["snapshot"]:
            raise DocumentError("SOURCE_DRIFT")
        store_bytes = tree.read(config["review_store"], missing_ok=True, limit=8388608)
        store = review_store(tree, config)
        documents = {x["path"]: x for x in current["impacted_documents"]}
        pending = {}
        pending_references = {}
        feature_rel = relative_path(os.path.relpath(feature, tree.root).replace(os.sep, "/"))
        for entry, is_reference in [(x, False) for x in request["reviews"]] + [
            (x, True) for x in request["reference_reviews"]
        ]:
            required = {"document", "outcome", "reason", "reviewer", "method", "evidence"}
            if is_reference:
                required = required - {"outcome"} | {"reference", "line", "disposition"}
            if not isinstance(entry, dict) or set(entry) != required:
                raise DocumentError("INVALID_REVIEW_REQUEST")
            path = relative_path(entry["document"])
            if path not in documents or (not is_reference and path in pending):
                raise DocumentError("INVALID_REVIEW_TARGET")
            if not is_reference and entry["outcome"] not in (
                "update",
                "updated",
                "reviewed_unchanged",
            ):
                raise DocumentError("INVALID_REVIEW_OUTCOME")
            finding = None
            if is_reference:
                finding = next(
                    (
                        x
                        for x in current["broken_references"]
                        if x["document"] == path
                        and x["reference"] == entry["reference"]
                        and x["line"] == entry["line"]
                    ),
                    None,
                )
                if (
                    type(entry["line"]) is not int
                    or not finding
                    or not reference_disposition_allowed(finding)
                    or entry["disposition"] not in ("historical", "external", "example")
                ):
                    raise DocumentError("NONWAIVABLE_REFERENCE")
            if any(
                not isinstance(entry.get(key), str) or not entry[key].strip()
                for key in ("reason", "reviewer", "method")
            ):
                raise DocumentError("REVIEW_ATTRIBUTION_REQUIRED")
            if not isinstance(entry["evidence"], list) or not entry["evidence"]:
                raise DocumentError("REVIEW_EVIDENCE_REQUIRED")
            evidence = []
            prohibited = {
                path,
                path + SIDECAR,
                config["review_store"],
                feature_rel + "/doc-impact.json",
            }
            for locator in entry["evidence"]:
                relative_path(locator)
                if locator in prohibited:
                    raise DocumentError("SELF_ATTESTATION_REJECTED")
                evidence.append({"path": locator, "sha256": digest(tree.read(locator))})
            record = read_document(root, path)
            outcome = (
                "reviewed_unchanged"
                if is_reference
                else ("updated" if entry["outcome"] in ("update", "updated") else entry["outcome"])
            )
            if outcome == "updated":
                if record["metadata"]["authority"] == "historical" or record["metadata"][
                    "lifecycle"
                ] in ("archived", "superseded"):
                    raise DocumentError("HISTORY_REWRITE_REJECTED")
                base = current["dependency_snapshot"]["base_commit"]
                tracked = git_paths(
                    tree.root, ["ls-tree", "-r", "--name-only", "-z", base, "--", path]
                )
                before = (
                    strict_git(tree.root, ["show", base + ":" + path]) if path in tracked else b""
                )
                after = tree.read(path)
                meaningful = adapter["meaningful_document_content"]
                if meaningful(before, path) == meaningful(after, path):
                    raise DocumentError("SEMANTIC_UPDATE_REQUIRED")
            review = {
                "doc_id": record["metadata"]["doc_id"],
                "path": path,
                "snapshot": record["snapshot"],
                "impact_snapshot_sha256": request["snapshot"],
                "outcome": outcome,
                "reason": entry["reason"].strip(),
                "reviewer": entry["reviewer"].strip(),
                "method": entry["method"].strip(),
                "evidence": evidence,
                "contract": {
                    "path": feature_rel + "/work-contract.json",
                    "sha256": current["dependency_snapshot"]["contract_sha256"],
                },
                "authority": "attributed_semantic_review_not_human_gate",
            }
            if is_reference:
                review.update(
                    reference=entry["reference"],
                    line=entry["line"],
                    disposition=entry["disposition"],
                    reference_hash=reference_finding_hash(finding),
                )
                key = (review["doc_id"], review["reference_hash"])
                if key in pending_references:
                    raise DocumentError("INVALID_REVIEW_TARGET")
                pending_references[key] = review
            else:
                pending[path] = review
        replacement_ids = {r["doc_id"] for r in pending.values()}
        replaced = [r for r in store["reviews"] if r["doc_id"] in replacement_ids]
        store["history"].extend(
            r for r in replaced if r not in store["history"] and r not in pending.values()
        )
        store["reviews"] = sorted(
            [r for r in store["reviews"] if r["doc_id"] not in replacement_ids]
            + list(pending.values()),
            key=lambda r: r["doc_id"],
        )
        replaced_references = [
            r
            for r in store["reference_reviews"]
            if (r["doc_id"], r["reference_hash"]) in pending_references
        ]
        store["reference_history"].extend(
            r
            for r in replaced_references
            if r not in store["reference_history"] and r not in pending_references.values()
        )
        store["reference_reviews"] = sorted(
            [r for r in store["reference_reviews"] if r not in replaced_references]
            + list(pending_references.values()),
            key=lambda r: (r["doc_id"], r["reference_hash"]),
        )

        def recheck():
            value = impact(root, feature, adapter, write=False)
            if value["dependency_snapshot_hash"] != request["snapshot"]:
                raise DocumentError("SOURCE_DRIFT")
            for review in list(pending.values()) + list(pending_references.values()):
                record = read_document(root, review["path"])
                if not review_matches(tree, record, review, config):
                    raise DocumentError("SOURCE_DRIFT")

        recheck()
        atomic_json(tree, config["review_store"], store, store_bytes, before_publish=recheck)
        # Canonical evidence is committed. A later projection failure cannot lose it;
        # a repeated docs impact regenerates this derived view from the same owner.
        return impact(root, feature, adapter, write=True)


# Shared typed reference parser, adapted from the existing Synapse Loop contract.
PATH_IN_CODE = re.compile(r"`([^`\n]+)`")
MARKDOWN_URI_ESCAPE = re.compile(
    r"""\\([!"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])|&(?:\#[xX][0-9a-fA-F]{1,6}|\#[0-9]{1,7}|[A-Za-z][A-Za-z0-9]{1,31});"""
)
HTML_TAG_START = re.compile(r"<[A-Za-z][A-Za-z0-9:._-]*(?=[\s/>])")
# This is a finite reference language, not a browser parser. A newly encountered
# value-bearing attribute cannot silently become an unchecked destination.
HTML_SINGLE_URL_ATTRIBUTES = frozenset(
    (
        "href",
        "src",
        "poster",
        "data",
        "action",
        "formaction",
        "cite",
        "longdesc",
        "background",
        "manifest",
        "codebase",
        "classid",
        "usemap",
        "itemid",
        "xlink:href",
        "xmlns",
    )
)
HTML_INERT_ATTRIBUTES = frozenset(
    (
        "id",
        "class",
        "title",
        "alt",
        "role",
        "lang",
        "dir",
        "width",
        "height",
        "align",
        "valign",
        "border",
        "cellpadding",
        "cellspacing",
        "colspan",
        "rowspan",
        "scope",
        "headers",
        "abbr",
        "span",
        "start",
        "reversed",
        "type",
        "name",
        "value",
        "method",
        "enctype",
        "target",
        "rel",
        "download",
        "hreflang",
        "media",
        "sizes",
        "charset",
        "content",
        "datetime",
        "open",
        "controls",
        "autoplay",
        "loop",
        "muted",
        "preload",
        "loading",
        "decoding",
        "crossorigin",
        "referrerpolicy",
        "integrity",
        "sandbox",
        "allow",
        "allowfullscreen",
        "frameborder",
        "tabindex",
        "hidden",
        "disabled",
        "checked",
        "selected",
        "multiple",
        "required",
        "readonly",
        "placeholder",
        "for",
        "form",
        "accept",
        "accept-charset",
        "autocomplete",
        "min",
        "max",
        "step",
        "minlength",
        "maxlength",
        "size",
        "wrap",
        "rows",
        "cols",
        "label",
        "kind",
        "srclang",
        "default",
        "summary",
        "coords",
        "shape",
        "ismap",
        "viewbox",
        "preserveaspectratio",
        "x",
        "y",
        "x1",
        "x2",
        "y1",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "points",
        "d",
        "version",
        "xml:lang",
    )
)
HTML_EMBEDDED_LANGUAGE_TAGS = frozenset(
    (
        "script",
        "style",
        "animate",
        "animatemotion",
        "animatetransform",
        "set",
        "handler",
        "foreignobject",
        "template",
        "param",
        "base",
    )
)
REFERENCE_DEFINITION_PREFIX = re.compile(
    r"^(?:[ \t]*>[ \t]*)*(?:[ \t]*(?:[-+*]|\d{1,9}[.)])[ \t]+)?[ \t]*$"
)
TREE_ROOT = re.compile(r"^([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/)$")
TREE_ITEM = re.compile(r"^([\s│|]*)[├└]──[ \t]+(.+?)\s*$")
GLOB_CHARS = "*?[]"
# These bare literals name public workflow commands or the standard temporary
# root discussed by the guides, not source inputs. No prefix/URI/tree exemption.
PUBLIC_NOTATION_LITERALS = frozenset(("/work", "/design", "/hooks", "/tmp"))


class ReferenceBudget:
    """Bound scan work and retained strings before allocating their copies."""

    def __init__(self, text):
        self.remaining = 20 * len(text) + 1024

    def spend(self, amount=1):
        self.remaining -= amount
        if self.remaining < 0:
            raise DocumentError("SCOPE_INCOMPLETE")


def document_reference_tokens(text, retain_context=True):
    """One bounded full-document lexer for visibility, dependencies and links.

    Deliberately scans literal examples too; callers, not the security lexer,
    exclude dependency-only code/fence contexts. No complete Markdown AST claim.
    Overlapping/nested references are retained, never hidden by an outer span.
    Lean candidate scans omit declaration prefixes, not reference offsets or
    grammar checks. Exact dependency and source validation retain full context.
    """
    tokens, brackets = [], []
    starts = [0] + [m.end() for m in re.finditer(r"\r\n?|\n", text)]
    spend = ReferenceBudget(text).spend
    size = len(text)

    def token(start, end, destination, syntax, kind, label=""):
        if len(tokens) >= 10000:
            raise DocumentError("SCOPE_INCOMPLETE")
        line = bisect.bisect_right(starts, start)
        # Prefixes may overlap across thousands of tokens on one line. Their
        # aggregate size, not just token count, must fit before any copy occurs.
        column = start - starts[line - 1]
        spend((column if retain_context else 0) + len(destination) + len(label))
        tokens.append(
            {
                "start": start,
                "end": end,
                "line": line,
                "column": column,
                "prefix": text[starts[line - 1] : start] if retain_context else "",
                "destination": destination,
                "syntax": syntax,
                "kind": kind,
                "label": label,
            }
        )

    def whitespace(pos, quotes=0):
        while pos < size:
            spend()
            if text[pos] in " \t":
                pos += 1
            elif text[pos] in "\r\n":
                pos += 2 if text[pos : pos + 2] == "\r\n" else 1
                for _ in range(quotes):
                    while pos < size and text[pos] in " \t":
                        spend()
                        pos += 1
                    if pos < size and text[pos] == ">":
                        pos += 1
            else:
                break
        return pos

    def destination(pos):
        start = pos
        angle = pos < size and text[pos] == "<"
        if angle:
            pos += 1
        depth = 0
        while pos < size:
            spend()
            char = text[pos]
            if (
                char == "\\"
                and pos + 1 < size
                and text[pos + 1] in r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~"""
            ):
                pos += 2
                continue
            if angle:
                if char == ">":
                    return text[start : pos + 1], pos + 1
                if char in "\r\n<":
                    raise DocumentError("UNSUPPORTED_PARSER")
            else:
                if char in " \t\r\n" or (char == ")" and not depth):
                    break
                if char == "<" or ord(char) < 32:
                    raise DocumentError("UNSUPPORTED_PARSER")
                if char == "(":
                    depth += 1
                    if depth > 128:
                        raise DocumentError("SCOPE_INCOMPLETE")
                elif char == ")":
                    depth -= 1
            pos += 1
        if angle or depth:
            raise DocumentError("UNSUPPORTED_PARSER")
        return text[start:pos], pos

    def inline_end(pos):
        spaced = whitespace(pos)
        if spaced < size and text[spaced] in "\"'(" and spaced > pos:
            close = ")" if text[spaced] == "(" else text[spaced]
            pos = spaced + 1
            while pos < size:
                spend()
                if text[pos] == "\\" and pos + 1 < size:
                    pos += 2
                    continue
                if text[pos] == close:
                    break
                pos += 1
            if pos == size:
                raise DocumentError("UNSUPPORTED_PARSER")
            spaced = whitespace(pos + 1)
        if spaced >= size or text[spaced] != ")":
            raise DocumentError("UNSUPPORTED_PARSER")
        return spaced + 1

    # Quote-aware attributes, including newlines, duplicate names and extensionless
    # destinations. Attribute entities are decoded once by reference_uri, not here.
    html_ends = {}
    spend(size)
    if re.search(r"<\?|<!(?!--)", text):
        raise DocumentError("UNSUPPORTED_PARSER")
    for match in HTML_TAG_START.finditer(text):
        if match.group()[1:].lower().split(":")[-1] in HTML_EMBEDDED_LANGUAGE_TAGS:
            raise DocumentError("UNSUPPORTED_PARSER")
        pos, references = match.end(), []
        while pos < size:
            spend()
            if text[pos] in " \t\r\n\f":
                pos += 1
                continue
            if text[pos] == ">" or text[pos : pos + 2] == "/>":
                end = pos + (2 if text[pos] == "/" else 1)
                html_ends[match.start()] = end
                spend(end - match.start())
                if text.find("](", match.start(), end) >= 0:
                    # Nested Markdown-looking text inside an HTML attribute is
                    # ambiguous literal output, never an unchecked hiding place.
                    raise DocumentError("UNSUPPORTED_PARSER")
                for raw in references:
                    token(match.start(), end, raw, "html", "html")
                break
            attr_start = pos
            while pos < size and text[pos] not in " \t\r\n\f/>=\"'":
                spend()
                pos += 1
            if pos == attr_start:
                # Literal placeholders such as <file / symbol / role> are not
                # references. Keep scanning: a later href/src must still bind.
                pos += 1
                continue
            name = text[attr_start:pos].lower()
            while pos < size and text[pos] in " \t\r\n\f":
                spend()
                pos += 1
            if pos == size or text[pos] != "=":
                continue
            pos += 1
            while pos < size and text[pos] in " \t\r\n\f":
                spend()
                pos += 1
            quote = text[pos] if pos < size and text[pos] in "\"'" else None
            pos += 1 if quote else 0
            value_start = pos
            while pos < size and (text[pos] != quote if quote else text[pos] not in " \t\r\n\f>"):
                spend()
                pos += 1
            if pos == size:
                raise DocumentError("UNSUPPORTED_PARSER")
            raw = text[value_start:pos]
            pos += 1 if quote else 0
            if name in HTML_SINGLE_URL_ATTRIBUTES or name.startswith("xmlns:"):
                references.append(raw)
            elif name not in HTML_INERT_ATTRIBUTES and not name.startswith(("aria-", "data-")):
                # Includes multi-URL attributes, CSS/SVG functional URLs, srcdoc,
                # refresh and event handlers. Reject, never guess embedded syntax.
                raise DocumentError("UNSUPPORTED_PARSER")
        else:
            raise DocumentError("UNSUPPORTED_PARSER")
    escaped, resume = False, 0
    for pos, char in enumerate(text):
        spend()
        if pos < resume:
            continue
        if pos in html_ends:
            resume, escaped = html_ends[pos], False
            continue
        if char == "[":
            brackets.append(pos)
            if len(brackets) > 1024:
                raise DocumentError("SCOPE_INCOMPLETE")
        elif char == "]" and brackets and not escaped:
            start = brackets.pop()
            following = text[pos + 1 : pos + 2]
            if following == "(":
                raw, end = destination(whitespace(pos + 2))
                end = inline_end(end)
                spend(end - pos - 2 + pos - start - 1)
                if text.find("](", pos + 2, end) >= 0:
                    raise DocumentError("UNSUPPORTED_PARSER")
                token(start, end, raw, "markdown", "inline", text[start + 1 : pos])
                # URI/title bytes are opaque to label balancing. An image URI
                # containing ']' must not consume the enclosing link's '['.
                resume = end
            elif following == ":":
                line_start = starts[bisect.bisect_right(starts, start) - 1]
                spend(start - line_start)
                prefix = text[line_start:start]
                if REFERENCE_DEFINITION_PREFIX.fullmatch(prefix):
                    raw, end = destination(whitespace(pos + 2, prefix.count(">")))
                    if "](" in raw:
                        raise DocumentError("UNSUPPORTED_PARSER")
                    if raw:
                        token(start, end, raw, "markdown", "definition")
                        resume = end
        escaped = char == "\\" and not escaped
    return sorted(tokens, key=lambda item: (item["start"], item["end"]))


def norm_rel(path):
    value = path or ""
    while value.startswith("./"):
        value = value[2:]
    return value


def rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def safe_path(root, path):
    root_real = os.path.realpath(root)
    target = os.path.realpath(path if os.path.isabs(path) else os.path.join(root_real, path))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise DocumentError("UNSAFE_PATH")
    return target


def file_sha(path):
    if not os.path.isfile(path):
        return ""
    value = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 67108864:
                raise DocumentError("SCOPE_INCOMPLETE")
            value.update(chunk)
    return value.hexdigest()


def path_matches(path, pattern):
    path, pattern = norm_rel(path), norm_rel(pattern)
    if not path or not pattern:
        return False
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/"):
        tail = pattern[3:]
        if fnmatch.fnmatchcase(path, tail) or fnmatch.fnmatchcase(os.path.basename(path), tail):
            return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def matches_any(path, patterns):
    return any(path_matches(path, pattern) for pattern in patterns or [])


def reference_walk_budget(limit):
    """Bound aggregate target attempts and retained traversal entries."""
    remaining = limit

    def spend():
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise DocumentError("SCOPE_INCOMPLETE")

    return spend


def reference_directory_entries(directory, spend=None):
    """Charge directory entries before retaining or sorting their names."""
    entries = []
    with os.scandir(directory) as stream:
        for entry in stream:
            if spend is not None:
                spend()
            entries.append(entry)
    return sorted(entries, key=lambda entry: entry.name)


def expand_repo_target(root, target, include_directories=False, literal=False, spend=None):
    """Expand dependencies inside the repo; retain unmatched targets as missing."""
    if spend is not None:
        spend()
    safe_path(root, target)
    # Keep logical aliases: the same target bytes can have different dependency
    # names or mappings. Containment and cycle checks still use resolved paths.
    root_real = os.path.realpath(root)
    full = os.path.join(root_real, target)
    if not literal and any(ch in target for ch in GLOB_CHARS):
        parts = rel(root_real, full).split("/")
        paths = []

        def visit(directory, remaining, ancestors):
            if spend is not None:
                spend()
            if not remaining:
                try:
                    info = os.stat(directory)
                except (FileNotFoundError, NotADirectoryError):
                    if os.path.lexists(directory):
                        raise RuntimeError(
                            "required dependency glob contains an unresolved entry"
                        ) from None
                    return
                if stat.S_ISREG(info.st_mode) or (
                    include_directories and stat.S_ISDIR(info.st_mode)
                ):
                    safe_path(root, directory)
                    paths.append(rel(root_real, directory))
                elif not stat.S_ISDIR(info.st_mode):
                    raise RuntimeError("required dependency glob contains a special file")
                return
            part = remaining[0]
            if not any(ch in part for ch in GLOB_CHARS):
                visit(os.path.join(directory, part), remaining[1:], ancestors)
                return
            try:
                entries = reference_directory_entries(directory, spend)
            except (FileNotFoundError, NotADirectoryError):
                return
            # PermissionError and other traversal errors deliberately propagate.
            if part == "**":
                visit(directory, remaining[1:], ancestors)
            for entry in entries:
                if entry.name.startswith(".") and not part.startswith("."):
                    continue
                if part != "**" and not fnmatch.fnmatchcase(entry.name, part):
                    continue
                child = safe_path(root, entry.path)
                if entry.is_symlink() and not os.path.exists(entry.path):
                    raise RuntimeError("required dependency glob contains an unresolved symlink")
                if part == "**" and entry.is_dir():
                    if child in ancestors:
                        raise RuntimeError("required dependency glob has a directory cycle")
                    visit(entry.path, remaining, ancestors | {child})
                elif part != "**" or len(remaining) == 1:
                    visit(entry.path, remaining[1:], ancestors)

        visit(os.path.realpath(root), parts, {os.path.realpath(root)})
        paths = sorted(set(paths))
        return paths or [target]
    return [target]


def declared_directory_paths(root, directory, spend=None):
    """Bind logical members, including empty directories and symlink aliases."""
    root_real = os.path.realpath(root)

    def visit(current, ancestors):
        if spend is not None:
            spend()
        resolved = safe_path(root, current)
        if resolved in ancestors:
            raise RuntimeError("required dependency has a directory cycle")
        yield rel(root_real, current)
        entries = reference_directory_entries(current, spend)
        for entry in entries:
            if entry.name in (".git", "__pycache__"):
                continue
            child = safe_path(root, entry.path)
            info = os.stat(child)  # Broken links and unreadable inputs must fail.
            if stat.S_ISDIR(info.st_mode):
                yield from visit(entry.path, ancestors | {resolved})
            elif stat.S_ISREG(info.st_mode):
                yield rel(root_real, entry.path)
            else:
                raise RuntimeError("required dependency is not a regular file or directory")

    return visit(os.path.join(root_real, directory), set())


def dependency_path_record(root, path):
    """Bind logical identity, all resolution hops and final target content."""
    full = safe_path(root, path)
    root_real = os.path.realpath(root)
    current = os.path.sep if os.path.isabs(path) else root_real
    pending = path.split(os.sep)
    links = []
    observed = {}
    states = set()
    # Expand link targets instead of letting lstat follow an unrecorded parent
    # chain. In particular, do not normpath away '..' before resolving a link.
    while pending:
        part, pending = pending[0], pending[1:]
        if part in ("", "."):
            continue
        if part == "..":
            current = os.path.dirname(current)
            continue
        candidate = os.path.join(current, part)
        try:
            info = os.lstat(candidate)
        except (FileNotFoundError, NotADirectoryError):
            break
        if stat.S_ISLNK(info.st_mode):
            state = (candidate, tuple(pending))
            if state in states or len(links) >= 256:
                raise RuntimeError(
                    "required dependency has a symlink cycle or excessive resolution depth"
                )
            states.add(state)
            target = os.readlink(candidate)
            digest = hashlib.sha256(os.fsencode(target)).hexdigest()
            if candidate in observed and observed[candidate] != digest:
                raise RuntimeError("required dependency changed during symlink resolution")
            observed[candidate] = digest
            name = rel(root_real, candidate)
            if name == ".." or name.startswith("../"):
                # Absolute targets may traverse an OS root alias (e.g. /tmp).
                # Bind its identity without persisting any out-of-repo locator.
                name = "outside:" + hashlib.sha256(os.fsencode(candidate)).hexdigest()
            links.append({"path": name, "target_sha256": digest})
            if os.path.isabs(target):
                current = os.path.sep
            pending = target.split(os.sep) + pending
        else:
            current = candidate
    try:
        info = os.stat(full)
    except (FileNotFoundError, NotADirectoryError):
        kind = "missing"
    else:
        if stat.S_ISREG(info.st_mode):
            kind = "file"
        elif stat.S_ISDIR(info.st_mode):
            kind = "directory"
        else:
            raise RuntimeError("required dependency is not a regular file or directory")
    digest = file_sha(full)
    if safe_path(root, path) != full or any(
        hashlib.sha256(os.fsencode(os.readlink(link))).hexdigest() != target_hash
        for link, target_hash in observed.items()
    ):
        raise RuntimeError("required dependency changed during snapshot capture")
    return {
        "path": path,
        "state": "MISSING" if kind == "missing" else "PRESENT",
        "kind": kind,
        "sha256": digest,
        "symlinks": links,
        "resolved_path_sha256": hashlib.sha256(os.fsencode(rel(root_real, full))).hexdigest(),
    }


def extract_typed_references(text, retain_context=True):
    """Keep syntax/location; full context is mandatory for exact dependencies.

    Lean mode is only a candidate-discovery input, not dependency validation.
    """
    results = []
    tree_root = None
    tree_stack = []
    fence = None
    # Match the shared lexer's physical line offsets. str.splitlines also splits
    # Unicode/control separators and can assign a real link to another line's
    # code span or fence, silently omitting its dependency.
    lines = re.split(r"\r\n?|\n", text)
    spend = ReferenceBudget(text).spend
    by_line = {}
    for item in document_reference_tokens(text, retain_context=retain_context):
        by_line.setdefault(item["line"], []).append(item)

    def add(value, line_no, kind, context, prefix="", syntax="markdown"):
        if len(results) >= 10000:
            raise DocumentError("SCOPE_INCOMPLETE")
        spend(len(value))
        opaque_origin = False
        if kind == "markdown_link" and not retain_context:
            try:
                parsed = urlsplit(reference_uri(value, syntax))
            except (ValueError, UnicodeError):
                raise DocumentError("UNSUPPORTED_PARSER") from None
            # An opaque origin is only an unavailable candidate, never a local
            # target, fetched resource or visibility approval. Exact validation
            # below and all visibility consumers retain the strict scheme list.
            opaque_origin = bool(
                parsed.scheme
                and parsed.netloc
                and parsed.scheme
                not in ("file", "data", "javascript", "vbscript", "http", "https", "mailto")
            )
        reference = (
            local_markdown_target(value, syntax)
            if kind == "markdown_link" and not opaque_origin
            else None
        )
        results.append(
            {
                "reference": (reference if reference is not None else reference_uri(value, syntax))
                if kind == "markdown_link"
                else clean_declared_path(value),
                "raw_reference": value,
                "line": line_no,
                "source_kind": kind,
                "context": context if retain_context else "",
                "prefix": prefix,
                "uri_local": kind == "markdown_link" and reference is not None,
            }
        )

    for line_no, line in enumerate(lines, start=1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            run, suffix = marker.groups()
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not suffix.strip():
                fence = None
            tree_root = None
            tree_stack = []
            continue
        item = TREE_ITEM.match(line)
        if item:
            indentation = len(item.group(1).expandtabs(4))
            # Aligned comment/status separators are annotations; single spaces and
            # literal POSIX backslashes belong to the filename.
            name = re.split(r"[ \t]{2,}(?:#|\[|←|→)", item.group(2), maxsplit=1)[0].rstrip()
            while tree_stack and tree_stack[-1][0] >= indentation:
                tree_stack.pop()
            parent = tree_stack[-1][1] if tree_stack else tree_root
            spend((len(parent) + 1 if parent else 0) + len(name))
            path = parent.rstrip("/") + "/" + name if parent else name
            add(path, line_no, "tree", line)
            tree_stack.append((indentation, path))
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("```", "~~~")):
            tree_root = None
            tree_stack = []
            continue
        elif (
            TREE_ROOT.match(stripped)
            or (
                stripped.endswith("/")
                and " " not in stripped
                and any(ch in stripped for ch in "<>{}$")
            )
            or (stripped.endswith("/") and line_no < len(lines) and TREE_ITEM.match(lines[line_no]))
        ):
            tree_root = stripped
            tree_stack = []
            continue
        tree_root = None
        tree_stack = []
        # Fenced commands are literal examples, not Markdown. Tree entries above
        # remain explicit structural dependencies even inside a text fence.
        if fence is not None:
            continue
        code_spans = []
        for match in PATH_IN_CODE.finditer(line):
            spend((match.start() if retain_context else 0) + len(match.group(1)))
            code_spans.append(match)
            add(
                match.group(1),
                line_no,
                "inline_code",
                line,
                line[: match.start()] if retain_context else "",
            )
        for item in by_line.get(line_no, []):
            offset = item["column"]
            # Each membership comparison is work even though it makes no copy.
            spend(len(code_spans))
            if not any(code.start() <= offset < code.end() for code in code_spans):
                add(
                    item["destination"],
                    line_no,
                    "markdown_link",
                    line,
                    item["prefix"],
                    item["syntax"],
                )
    return results


def reference_local_roots(root, policy=None):
    with os.scandir(root) as entries:
        roots = {entry.name for entry in entries}
    roots.update(
        (item.get("path") or "").split("/", 1)[0]
        for item in (policy or {}).get("canonical_roots") or []
    )
    return roots


def classify_document_reference(item, local_roots, budget=None):
    # Charge all retained input before copying or scanning it. Callers processing
    # a document share one budget, so repeated prefixes cannot hide aggregate work.
    fields = ("prefix", "context", "reference", "raw_reference")
    if budget is None:
        budget = ReferenceBudget(max((item.get(key) or "" for key in fields), key=len))
    budget.spend(1 + sum(len(item.get(key) or "") for key in fields))
    value = item["reference"]
    source_kind = item["source_kind"]
    prefix = PATH_IN_CODE.sub("", item.get("prefix") or "")
    # Only a dependency declaration directly introducing this value (or its list)
    # makes it mandatory. Incidental prose such as "Dependencies. `Eval:`" or
    # "deployment requires a future `approval_name`" does not declare a file.
    declaration = None
    for candidate in re.finditer(
        r"(?:\brequires?\s+(?:(?:the|a|an|both|all)\s+)?"
        r"(?:(?:files?|directories|directory|folder|path|artifact|document)\s+)?|"
        r"\bdepends?\s+on\s+|\bdependenc(?:y|ies)\s*:\s*|"
        r"(?:필수\s*참조|의존\s*(?:경로|파일))\s*:\s*)",
        prefix,
        re.IGNORECASE,
    ):
        declaration = candidate
    # A declaration header cannot occur in the separator-only suffix. Only the
    # last header can apply. Match its suffix separately with disjoint, bounded
    # alternatives: nested whitespace repeats backtrack exponentially on prose.
    if declaration and not re.fullmatch(
        r"(?:[\s,*]|\band\b|\bor\b|및|와|과)*",
        prefix[declaration.end() :],
        re.IGNORECASE,
    ):
        declaration = None
    # Negation immediately preceding this declaration is not a requirement.
    # Do not let an earlier negative clause cancel a later positive declaration.
    explicit = bool(
        declaration
        and not re.search(
            r"(?:\bnot|\bnever|n't)\s+$", prefix[: declaration.start()], re.IGNORECASE
        )
    )
    result = {key: item[key] for key in ("reference", "raw_reference", "line", "source_kind")}
    result.update({"explicit_dependency": explicit, "required": explicit})
    if not value or (value.startswith("#") and not item.get("uri_local")):
        kind, reason = "anchor_or_empty", "document anchor is not a filesystem dependency"
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) or value.startswith(("mailto:", "//")):
        kind = "external_dependency" if explicit else "external_link"
        reason = (
            "explicit external dependency"
            if explicit
            else "external hyperlink, not a repository path"
        )
    elif re.match(r"^[A-Za-z][A-Za-z0-9_-]*:[^\s:]+/", value):
        kind, reason = (
            "external_dependency",
            "repository-qualified reference is not a local repository path",
        )
        result["required"] = explicit or source_kind != "inline_code"
    elif source_kind == "markdown_link" and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        # URI syntax is not a local filename. Rendering separately allowlists
        # navigable schemes; javascript/data/etc remain inert literal text.
        kind = "external_dependency" if explicit else "external_link"
        reason = "explicit external dependency" if explicit else "non-filesystem URI"
    elif (
        source_kind == "tree"
        and not explicit
        and (
            re.search(r"<[A-Za-z][A-Za-z0-9_-]*>", value)
            or re.search(
                r"(?:^|/)\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})(?:/|$)", value
            )
        )
    ):
        kind, reason = "template_path", "tree contains an explicit uninstantiated placeholder"
    elif explicit or source_kind in ("markdown_link", "tree"):
        kind, reason = "repository_path", "explicit dependency or structural local reference"
        result["required"] = True
    elif (
        source_kind == "inline_code"
        and value in PUBLIC_NOTATION_LITERALS
        and item["raw_reference"] == value
    ):
        kind, reason = "code_token", "known bare workflow/system notation, not a source dependency"
    elif not looks_like_repo_path(value):
        kind, reason = "code_token", "inline code lacks local reference syntax"
    else:
        head = value.split("/", 1)[0]
        repository_id = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value))
        source_context = bool(
            re.search(
                r"github|\bofficial\b|\brepositor(?:y|ies)\b|공식|출처",
                item.get("context") or "",
                re.IGNORECASE,
            )
        )
        if head in local_roots or value.startswith(("./", "../", "/", "~")):
            kind, reason = (
                "repository_path",
                "repository root or explicit relative/absolute path syntax",
            )
        elif repository_id and source_context:
            kind, reason = (
                "repository_identifier",
                "owner/repository identifier in source-repository context",
            )
        elif (
            value.endswith("/")
            or any(ch in value for ch in GLOB_CHARS)
            or re.search(r"\.[A-Za-z][A-Za-z0-9_.-]*$", os.path.basename(value))
        ):
            kind, reason = "repository_path", "directory, glob or filename-extension syntax"
        else:
            kind, reason = (
                "code_token",
                "slash-separated code identifier without filesystem dependency evidence",
            )
    result.update({"kind": kind, "classification_reason": reason})
    return result


def clean_declared_path(text):
    """문서에 적힌 경로 표기에서 locator/장식만 걷어낸다.

    leading '.' 을 지우면 '.ai-team/' 이 'ai-team/' 이 되어 없는 경로처럼 보인다.
    그래서 왼쪽은 건드리지 않는다.
    """
    value = (text or "").strip()
    value = value.rstrip(",;")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return value
    value = value.split("#", 1)[0] if not value.startswith("#") else value
    # Single, range and repeated line locators are not part of the filename.
    match = re.match(r"^(.+?):[0-9]+(?:-[0-9]+)?(?:[/,][0-9]+(?:-[0-9]+)?)*$", value)
    if match:
        value = match.group(1)
    return value.strip()


def looks_like_repo_path(text):
    """문서 안의 조각이 repository 경로 선언처럼 보이는지 본다.

    파일명만 적은 언급이나 'scope.include/exclude' 같은 JSON 필드 경로를
    경로 선언으로 오해하면 broken reference 신호가 잡음에 묻힌다.
    """
    text = (text or "").strip()
    if not text or " " in text:
        return False
    if text.startswith("-") or text.startswith("+"):
        return False
    if "(" in text or ")" in text or "=" in text or "::" in text or "@" in text:
        return False
    # repository 경로 선언은 최소한 하나의 '/' 를 가진다.
    if "/" not in text:
        return False
    head = text.split("/", 1)[0]
    # '.ai-team' 처럼 dot 으로 시작하는 top-level 은 정상이다.
    # 반대로 'scope.include/exclude' 는 필드 경로지 파일 경로가 아니다.
    return not ("." in head and not head.startswith("."))


def extract_declared_paths(text):
    """Compatibility view; consumers requiring semantics use typed references."""
    return [(item["reference"], item["line"]) for item in extract_typed_references(text)]


def check_reference_candidate(root, candidate, spend=None):
    """Check exact expanded paths, including unmatched-pattern literal fallback."""
    current = os.path.realpath(root)
    for part in candidate.split("/"):
        if spend is not None:
            spend()
        current = os.path.join(current, part)
        safe_path(root, current)
        try:
            info = os.stat(current)
        except FileNotFoundError:
            if os.path.lexists(current):
                raise DocumentError("UNSAFE_PATH") from None
            return  # An ordinary missing prefix makes the optional path absent.
        except OSError:
            # Includes cycles, access failure and a non-directory prefix.
            # os.path.exists would silently turn these into absence.
            raise DocumentError("UNSAFE_PATH") from None
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise DocumentError("UNSAFE_PATH")


def reference_candidates(root, doc_dir, value, source_kind="inline_code", reject_unsafe=False):
    """참조가 가리킬 수 있는 repository-relative 경로 후보를 만든다.

    Markdown link는 문서 기준으로만 해석한다. 코드 안의 locator는 기존
    repository-root 후보도 유지한다. 외부 경로는 빈 후보를 반환하므로
    호출자가 UNAVAILABLE로 보고할 수 있다.
    """
    raw = []
    if source_kind != "markdown_link" and not value.startswith(".."):
        raw.append(value)
    if doc_dir:
        raw.append(os.path.normpath(os.path.join(doc_dir, value)))
    else:
        raw.append(os.path.normpath(value))
    inside = []
    for candidate in raw:
        candidate = norm_rel(candidate)
        if not candidate or candidate.startswith("..") or os.path.isabs(candidate):
            continue
        try:
            safe_path(root, candidate)
        except (RuntimeError, DocumentError):
            if reject_unsafe:
                raise
            continue
        if candidate not in inside:
            inside.append(candidate)
    return inside


def reference_exists(root, doc_dir, value, source_kind="inline_code"):
    for candidate in reference_candidates(root, doc_dir, value, source_kind):
        for expanded in expand_repo_target(
            root, candidate, include_directories=True, literal=source_kind == "markdown_link"
        ):
            if os.path.exists(safe_path(root, expanded)):
                return True
    return False


def scan_broken_references(root, policy, documents=None, classifications=None):
    """문서가 존재하지 않는 repository 경로를 가리키는지 본다.

    문서가 실제와 어긋났음을 기계적으로 판정할 수 있는 가장 확실한 신호다.
    """
    scan = policy.get("reference_scan") or {}
    targets = documents if documents is not None else (scan.get("documents") or [])
    ignore = scan.get("ignore_patterns") or []
    known_absent = scan.get("known_absent") or []
    # historical decision log 는 과거 시점의 사실을 적은 기록이다. 그때 존재하던
    # 경로가 지금 없는 것은 정상이며, 고치면 오히려 이력을 덮어쓰게 된다.
    historical = {
        norm_rel(item.get("path") or "") for item in policy.get("historical_documents") or []
    }
    findings = []
    local_roots = reference_local_roots(root, policy)
    expanded_targets = []
    for target in targets:
        expanded_targets.extend(expand_repo_target(root, target))
    for doc in sorted(set(expanded_targets)):
        doc = norm_rel(doc)
        if doc in historical or document_category(policy, doc)[1] == "historical":
            continue
        full = safe_path(root, doc)
        if not os.path.isfile(full):
            findings.append(
                {
                    "document": doc,
                    "reference": doc,
                    "line": 0,
                    "reason": "required document is missing",
                    "required": True,
                    "source_kind": "configured_target",
                    "kind": "required_document",
                }
            )
            continue
        if not doc.endswith(".md"):
            # Glob impact rules may also include images and machine-readable
            # artifacts. Hash/review them, but do not parse binary bytes as Markdown.
            continue
        with SourceTree(root) as tree:
            text = tree.read(doc).decode("utf-8")
        doc_dir = os.path.dirname(doc)
        seen = set()
        classification_budget = ReferenceBudget(text)
        for raw in extract_typed_references(text):
            typed = classify_document_reference(raw, local_roots, budget=classification_budget)
            typed["document"] = doc
            if classifications is not None:
                # Non-file code may contain configuration values. Persist only
                # its location/type/hash, not an unnecessary copy of that value.
                classified = {
                    key: typed[key]
                    for key in (
                        "document",
                        "line",
                        "kind",
                        "source_kind",
                        "classification_reason",
                        "required",
                    )
                }
                classified["reference_sha256"] = hashlib.sha256(
                    typed["raw_reference"].encode("utf-8")
                ).hexdigest()
                classifications.append(classified)
            if typed["kind"] not in ("repository_path", "external_dependency"):
                continue
            value, line_no = typed["reference"], typed["line"]
            if typed["kind"] == "external_dependency":
                findings.append(dict(typed, reason="explicit external dependency is UNAVAILABLE"))
                continue
            if not typed["required"] and (
                matches_any(value, ignore) or matches_any(value, known_absent)
            ):
                continue
            candidates = reference_candidates(root, doc_dir, value, typed["source_kind"])
            if not candidates:
                findings.append(
                    dict(typed, reason="external dependency is UNAVAILABLE in this repository")
                )
                continue
            if not typed["required"] and any(
                matches_any(candidate, known_absent) for candidate in candidates
            ):
                continue
            if reference_exists(root, doc_dir, value, typed["source_kind"]):
                continue
            key = (doc, value, line_no)
            if key in seen:
                continue
            seen.add(key)
            findings.append(dict(typed, reason="선언된 경로가 repository 에 없다"))
    return findings


def docs_referencing(root, paths, limit=None, include_document=None):
    """Inspect every tracked/untracked document and every changed dependency.

    The legacy limit argument remains accepted, but never truncates required work.
    Git's NUL records preserve literal names; missing tracked documents are handled
    by required target validation, not read as if they still existed. Impact-only
    callers may select their existing document roles before parsing; the default
    remains a strict scan of every document, with no implicit path exclusions.
    """

    def matches_candidate(path, pattern, literal):
        if path == pattern:
            return True
        if literal:
            return False
        try:
            return path_matches(path, pattern)
        except re.error:
            # Older fnmatch versions raise for malformed ranges in ordinary
            # inline notation. Such a glob has no matches; its literal identity
            # was checked above. This candidate-only fallback is not dependency
            # validation or permission to ignore a required target.
            return False

    hits = {}
    local_roots = reference_local_roots(root)
    paths = sorted(set(paths))
    documents = git_paths(root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    for doc in sorted(set(documents)):
        if not doc.endswith(".md"):
            continue
        if include_document is not None and not include_document(doc):
            continue
        full = safe_path(root, doc)
        if not os.path.isfile(full):
            continue
        with SourceTree(root) as tree:
            text = tree.read(doc).decode("utf-8")
        declarations = []
        classification_budget = ReferenceBudget(text)
        for item in extract_typed_references(text, retain_context=False):
            kind = classify_document_reference(item, local_roots, budget=classification_budget)[
                "kind"
            ]
            # Candidate discovery may over-select; it must not miss a relative
            # extensionless name just because its declaration context is omitted.
            # Required-dependency proof still uses the full-context default.
            if kind == "repository_path" or (
                item["source_kind"] == "inline_code"
                and kind not in ("external_link", "external_dependency", "anchor_or_empty")
            ):
                declarations.append(item)
        patterns = [
            (candidate, item["source_kind"] == "markdown_link")
            for item in declarations
            for candidate in reference_candidates(
                root, os.path.dirname(doc), item["reference"], item["source_kind"]
            )
        ]
        # Remove complete link spans, retaining unrelated prose and code paths.
        pieces, end = [], 0
        for item in document_reference_tokens(text, retain_context=False):
            if item["start"] >= end:
                pieces.append(text[end : item["start"]])
            end = max(end, item["end"])
        prose = " ".join([*pieces, text[end:]])
        refs = [
            path
            for path in paths
            if path != doc
            and (
                path in prose
                or any(matches_candidate(path, pattern, literal) for pattern, literal in patterns)
            )
        ]
        if refs:
            hits[doc] = refs
    return hits


def document_category(policy, path):
    path = norm_rel(path)
    for item in policy.get("historical_document_patterns") or []:
        if path_matches(path, item.get("pattern") or ""):
            return item.get("category") or "historical_record", "historical"
    best = None
    for item in policy.get("canonical_roots") or []:
        prefix = norm_rel(item.get("path") or "")
        if not prefix:
            continue
        if (path == prefix or path.startswith(prefix.rstrip("/") + "/")) and (
            best is None or len(prefix) > len(best[0])
        ):
            best = (prefix, item.get("category") or "other", item.get("kind") or "canonical")
    if best:
        return best[1], best[2]
    return "other", "canonical"


# Deterministic document views: no network, model, execution or publication API.
VIEW_COMPONENTS = ("ui", "backend", "contracts", "ontology", "kit")
VIEW_AUDIENCES = ("user", "developer", "operator", "executive")
VIEW_FORBIDDEN_KINDS = ("agent_trace", "agent-trace", "work_memory", "handoff", "raw_evidence")


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise DocumentError("INVALID_DIGEST")
    return value


def bound_file(tree, reference):
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise DocumentError("INVALID_VERIFICATION_EVIDENCE")
    data = tree.read(relative_path(reference["path"]))
    if digest(data) != hash_value(reference["sha256"]):
        raise DocumentError("SOURCE_DRIFT")
    return data


def verified_release(tree, path):
    value = strict_json(tree.read(relative_path(path)))
    expected = {
        "schema_version",
        "release_id",
        "status",
        "security",
        "audiences",
        "components",
        "documents",
        "verification",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value["schema_version"] != "1.0"
        or value["status"] != "verified"
        or value["security"] not in SECURITY
    ):
        raise DocumentError("VERIFIED_RELEASE_REQUIRED")
    exact_release(value["release_id"])
    if any(x not in VIEW_AUDIENCES for x in _strings(value["audiences"], required=True)):
        raise DocumentError("INVALID_AUDIENCE")
    if not isinstance(value["components"], dict) or set(value["components"]) != set(
        VIEW_COMPONENTS
    ):
        raise DocumentError("INCOMPLETE_RELEASE_SET")
    for component in value["components"].values():
        if not isinstance(component, dict) or set(component) != {"revision", "path", "sha256"}:
            raise DocumentError("INCOMPLETE_RELEASE_SET")
        exact_release(component["revision"])
        bound_file(tree, {key: component[key] for key in ("path", "sha256")})
    document_ids = []
    if (
        not isinstance(value["documents"], list)
        or not value["documents"]
        or len(value["documents"]) > 1000
    ):
        raise DocumentError("INCOMPLETE_RELEASE_SET")
    for item in value["documents"]:
        if not isinstance(item, dict) or set(item) != {
            "doc_id",
            "source_sha256",
            "snapshot_sha256",
            "review_sha256",
        }:
            raise DocumentError("INCOMPLETE_RELEASE_SET")
        document_ids.append(_qualified(item["doc_id"]))
        for key in ("source_sha256", "snapshot_sha256", "review_sha256"):
            hash_value(item[key])
    if len(document_ids) != len(set(document_ids)):
        raise DocumentError("IDENTITY_COLLISION")
    proof = strict_json(bound_file(tree, value["verification"]))
    core = {key: item for key, item in value.items() if key != "verification"}
    if (
        not isinstance(proof, dict)
        or set(proof) != {"schema_version", "verdict", "input_sha256", "checks"}
        or proof["schema_version"] != "1.0"
        or proof["verdict"] != "PASS"
        or proof["input_sha256"] != object_digest(core)
        or not isinstance(proof["checks"], list)
        or not proof["checks"]
    ):
        raise DocumentError("VERIFIED_RELEASE_REQUIRED")
    check_ids = set()
    for check in proof["checks"]:
        if (
            not isinstance(check, dict)
            or set(check) != {"id", "exit_code", "evidence"}
            or not isinstance(check["id"], str)
            or not check["id"].strip()
            or check["id"] in check_ids
            or type(check["exit_code"]) is not int
            or check["exit_code"] != 0
        ):
            raise DocumentError("VERIFIED_RELEASE_REQUIRED")
        check_ids.add(check["id"])
        bound_file(tree, check["evidence"])
    return value


def resolve_release(root, index_path, requested):
    with SourceTree(root) as tree:
        value = strict_json(tree.read(relative_path(index_path)))
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "latest", "releases"}
            or value["schema_version"] != "1.0"
            or not isinstance(value["releases"], list)
        ):
            raise DocumentError("INVALID_RELEASE_INDEX")
        release_id = exact_release(value["latest"] if requested == "latest" else requested)
        matches = []
        ids = set()
        for entry in value["releases"]:
            if not isinstance(entry, dict) or set(entry) != {"release_id", "path", "sha256"}:
                raise DocumentError("INVALID_RELEASE_INDEX")
            exact_release(entry["release_id"])
            if entry["release_id"] in ids:
                raise DocumentError("IDENTITY_COLLISION")
            ids.add(entry["release_id"])
            if entry["release_id"] == release_id:
                matches.append(entry)
        if len(matches) != 1:
            raise DocumentError("VERIFIED_RELEASE_REQUIRED")
        selected = matches[0]
        bound_file(tree, {key: selected[key] for key in ("path", "sha256")})
        if verified_release(tree, selected["path"])["release_id"] != release_id:
            raise DocumentError("SOURCE_DRIFT")
        return selected["path"]


def audience_body(record, audience):
    selectors = record["metadata"].get("sections")
    if selectors is None:
        return record["body"]
    audiences = record["metadata"]["audiences"]
    if not isinstance(selectors, dict) or set(selectors) != set(audiences):
        raise DocumentError("INVALID_AUDIENCE_SECTIONS")
    for entries in selectors.values():
        _strings(entries, required=True)
    headings = []
    fence = None
    lines = record["body"].splitlines(keepends=True)
    for number, line in enumerate(lines):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match and fence is None:
            headings.append((number, len(match.group(1)), match.group(2)))
    selected_lines = set()
    for title in selectors[audience]:
        matches = [item for item in headings if item[2] == title]
        if len(matches) != 1:
            raise DocumentError("INVALID_AUDIENCE_SECTIONS")
        start, level, _ = matches[0]
        end = next(
            (index for index, other_level, _ in headings if index > start and other_level <= level),
            len(lines),
        )
        selected_lines.update(range(start, end))
    return "".join(line for number, line in enumerate(lines) if number in selected_lines)


def prepare_view_input(root, release_path, audience, security="INTERNAL", history=False):
    """Internal resolver. This result is a candidate, not external approval."""
    if audience not in VIEW_AUDIENCES or type(history) is not bool:
        raise DocumentError("INVALID_AUDIENCE")
    with SourceTree(root) as tree:
        config, rules = configuration(tree)
        if not security_allowed(security, security, config["maximum_security"]):
            raise DocumentError("DISCLOSURE_DENIED")
        release = verified_release(tree, release_path)
        if not security_allowed(release["security"], security, config["maximum_security"]):
            raise DocumentError("DISCLOSURE_DENIED")
        if audience not in release["audiences"]:
            raise DocumentError("INVALID_AUDIENCE")
        records = _catalog(tree, config, rules)
        by_id = {record["metadata"]["doc_id"]: record for record in records}
        review_data = reviews(tree, config)
        documents = []
        canonical = set()
        for record in records:
            meta = record["metadata"]
            if (
                meta["authority"] == "canonical"
                and meta["lifecycle"] == "active"
                and release["release_id"] in meta["release_ids"]
                and security_allowed(meta["security"], security, config["maximum_security"])
            ):
                key = (meta["topic_id"], release["release_id"], meta["security"])
                if key in canonical:
                    raise DocumentError("CANONICAL_CONFLICT")
                canonical.add(key)
        for reference in release["documents"]:
            record = by_id.get(reference["doc_id"])
            if record is None:
                raise DocumentError("MISSING_SOURCE")
            meta = record["metadata"]
            if not security_allowed(meta["security"], security, config["maximum_security"]):
                raise DocumentError("DISCLOSURE_DENIED")
            review = next((x for x in review_data if x["doc_id"] == meta["doc_id"]), None)
            current = {
                "doc_id": meta["doc_id"],
                "source_sha256": record["source_sha256"],
                "snapshot_sha256": release_snapshot_digest(record),
                "review_sha256": object_digest(review),
            }
            if current != reference or record["freshness"] != "verified":
                raise DocumentError("SOURCE_DRIFT")
            if release["release_id"] not in meta["release_ids"] or (
                not history and meta["lifecycle"] != "active"
            ):
                raise DocumentError("CURRENT_RELEASE_DOCUMENT_REQUIRED")
            if audience not in meta["audiences"]:
                continue
            if meta["kind"] in VIEW_FORBIDDEN_KINDS:
                raise DocumentError("INTERNAL_TRACE_NOT_A_DOCUMENT")
            body = audience_body(record, audience)
            documents.append(
                {
                    **current,
                    "title": meta["title"],
                    "owner": meta["owner"],
                    "kind": meta["kind"],
                    "source_path": record["path"],
                    "security": meta["security"],
                    "lifecycle": meta["lifecycle"],
                    "body": body,
                }
            )
        if not documents:
            raise DocumentError("EMPTY_VIEW_SELECTION")
        # Reject explicit cross-boundary references, rather than leaking their names.
        allowed_paths = {x["source_path"] for x in documents}
        for document in documents:
            reference_visibility(
                tree,
                config,
                records,
                document["source_path"],
                document["title"] + "\n" + document["body"],
                security,
                allowed_paths,
            )
        if verified_release(tree, release_path) != release:
            raise DocumentError("SOURCE_DRIFT")
        value = {
            "schema_version": "1.0",
            "renderer": "amplai-offline-view-1",
            "release_id": release["release_id"],
            "audience": audience,
            "security": security,
            "mode": "history" if history else "current",
            "components": {
                key: {"revision": item["revision"], "sha256": item["sha256"]}
                for key, item in release["components"].items()
            },
            "verification_sha256": release["verification"]["sha256"],
            "documents": sorted(documents, key=lambda x: x["doc_id"]),
        }
        validate_view_input(value)
        return value


def validate_view_input(value):
    expected = {
        "schema_version",
        "renderer",
        "release_id",
        "audience",
        "security",
        "mode",
        "components",
        "verification_sha256",
        "documents",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value["schema_version"] != "1.0"
        or value["renderer"] != "amplai-offline-view-1"
        or value["security"] not in SECURITY
        or value["audience"] not in VIEW_AUDIENCES
        or value["mode"] not in ("current", "history")
    ):
        raise DocumentError("INVALID_VIEW_INPUT")
    exact_release(value["release_id"])
    hash_value(value["verification_sha256"])
    if not isinstance(value["components"], dict) or set(value["components"]) != set(
        VIEW_COMPONENTS
    ):
        raise DocumentError("INVALID_VIEW_INPUT")
    for component in value["components"].values():
        if not isinstance(component, dict) or set(component) != {"revision", "sha256"}:
            raise DocumentError("INVALID_VIEW_INPUT")
        exact_release(component["revision"])
        hash_value(component["sha256"])
    if not isinstance(value["documents"], list) or not 1 <= len(value["documents"]) <= 1000:
        raise DocumentError("INVALID_VIEW_INPUT")
    ids, paths = set(), set()
    for document in value["documents"]:
        if not isinstance(document, dict) or set(document) != {
            "doc_id",
            "source_sha256",
            "snapshot_sha256",
            "review_sha256",
            "title",
            "owner",
            "kind",
            "source_path",
            "security",
            "lifecycle",
            "body",
        }:
            raise DocumentError("INVALID_VIEW_INPUT")
        _qualified(document["doc_id"])
        relative_path(document["source_path"])
        if document["doc_id"] in ids or document["source_path"].casefold() in paths:
            raise DocumentError("IDENTITY_COLLISION")
        ids.add(document["doc_id"])
        paths.add(document["source_path"].casefold())
        if (
            not security_allowed(document["security"], value["security"], value["security"])
            or document["kind"] in VIEW_FORBIDDEN_KINDS
        ):
            raise DocumentError("DISCLOSURE_DENIED")
        if document["lifecycle"] not in LIFECYCLES or (
            value["mode"] == "current" and document["lifecycle"] != "active"
        ):
            raise DocumentError("CURRENT_RELEASE_DOCUMENT_REQUIRED")
        for key in ("title", "owner", "kind", "body"):
            if (
                not isinstance(document[key], str)
                or not document[key].strip()
                or len(document[key].encode("utf-8")) > 8388608
            ):
                raise DocumentError("INVALID_VIEW_INPUT")
        for key in ("source_sha256", "snapshot_sha256", "review_sha256"):
            hash_value(document[key])
    if len(json_bytes(value)) > 67108864:
        raise DocumentError("SCOPE_INCOMPLETE")


VIEW_CSS = """
:root{color-scheme:light;--ink:#203047;--muted:#526176;--paper:#fff;--edge:#d9e2ec}
*{box-sizing:border-box}html{scroll-behavior:auto}body{margin:0;background:#f5f7fa;
color:var(--ink);font:17px/1.7 system-ui,-apple-system,sans-serif;overflow-wrap:anywhere}
a{color:#075b86;text-underline-offset:.2em}a:focus-visible,summary:focus-visible{
outline:3px solid #8f3b00;outline-offset:4px}.skip{position:absolute;top:-100px;
left:1rem;background:white;padding:.6rem 1rem;z-index:3}.skip:focus{top:1rem}
header{background:#142b43;color:#fff;padding:2rem max(1rem,calc((100vw - 76rem)/2))}
header a{color:#c0edff}.eyebrow{font-size:.8rem;letter-spacing:.1em;text-transform:uppercase}
h1{font-size:clamp(1.7rem,5vw,2.8rem);line-height:1.2;margin:.6rem 0}
h2{font-size:1.55rem;margin-top:2.2rem}h3{font-size:1.2rem;margin-top:1.8rem}
.layout{max-width:78rem;margin:auto;padding:2rem;display:grid;
grid-template-columns:minmax(12rem,17rem) minmax(0,1fr);gap:2rem}
nav{align-self:start}nav ul{padding-left:1.2rem}nav li{margin:.4rem 0}
main{background:var(--paper);padding:clamp(1rem,3vw,2.5rem);border:1px solid var(--edge);
border-radius:.7rem;min-width:0}p{margin:.8rem 0}code{font-family:ui-monospace,monospace;
font-size:.9em;background:#edf2f7;padding:.15rem .3rem;border-radius:.2rem}
pre{background:#edf2f7;padding:1rem;overflow:auto;border-left:3px solid #18707f}
pre code{padding:0;white-space:pre-wrap;word-break:break-word}
blockquote,.notice{border-left:4px solid #8f6500;background:#fff6dc;padding:.75rem 1rem;
margin:1rem 0}.meta,footer{color:var(--muted);font-size:.85rem}
.table-scroll{max-width:100%;overflow:auto}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.5rem;border-bottom:1px solid var(--edge);vertical-align:top}
th{font-weight:650}footer{max-width:78rem;margin:auto;padding:1rem 2rem 3rem}
.release-table{table-layout:fixed;min-width:32rem}
.release-table th:first-child{width:22%}.release-table th:nth-child(2){width:20%}
@media(max-width:700px){.layout{display:block;padding:1rem}nav{margin-bottom:1rem}
main{padding:1rem}header{padding:1.5rem 1rem}footer{padding:1rem}}
@media print{body{background:white;font-size:11pt;color:#000}.layout{display:block;padding:0}
header{background:white;color:#000;padding:0;border-bottom:2px solid #000}nav,.skip{display:none}
main{border:0;padding:0}pre,blockquote,table{break-inside:avoid}a{color:#000}
h1,h2,h3{break-after:avoid}footer{padding:1rem 0}.table-scroll{overflow:visible}}
""".strip()


def view_page_name(document):
    return "document-" + digest(document["doc_id"].encode("utf-8")) + ".html"


def view_heading_ids(body):
    anchors, used = [], set()
    fence = None
    for line in body.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            run, suffix = marker.groups()
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not suffix.strip():
                fence = None
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading and fence is None:
            base = (
                re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", heading.group(2)).strip().casefold())
                or "section"
            )
            candidate, number = base, 0
            while candidate in used:
                number += 1
                candidate = base + "-" + str(number)
            used.add(candidate)
            anchors.append(candidate)
    return anchors


def view_inline(text, document, pages):
    tokens = [
        item
        for item in document_reference_tokens(text)
        if item["kind"] == "inline" and text[item["start"] - 1 : item["start"]] != "!"
    ]
    tokens.extend(
        {"start": match.start(), "end": match.end(), "kind": "code", "label": match.group(1)}
        for match in PATH_IN_CODE.finditer(text)
    )
    pieces, end = [], 0
    for item in sorted(tokens, key=lambda row: (row["start"], -row["end"])):
        if item["start"] < end:
            continue
        pieces.append(html.escape(text[end : item["start"]]))
        if item["kind"] == "code":
            pieces.append("<code>" + html.escape(item["label"]) + "</code>")
        else:
            label = item["label"]
            target = reference_uri(item["destination"])
            parsed = urlsplit(target)
            href = None
            if not any(ord(c) < 32 or ord(c) == 127 for c in target):
                if parsed.scheme in ("https", "http", "mailto"):
                    href = target
                elif not parsed.scheme and not parsed.netloc:
                    path = (
                        os.path.normpath(
                            os.path.join(
                                os.path.dirname(document["source_path"]),
                                local_markdown_target(item["destination"]),
                            )
                        )
                        if parsed.path
                        else document["source_path"]
                    )
                    selected = pages.get(path)
                    if selected:
                        anchor = unquote(parsed.fragment)
                        if anchor and anchor not in selected["anchors"]:
                            raise DocumentError("MISSING_VIEW_ANCHOR")
                        href = (selected["file"] if parsed.path else "") + (
                            "#section-" + anchor if anchor else ""
                        )
            if href:
                pieces.append(
                    '<a href="'
                    + html.escape(href, quote=True)
                    + '" rel="noreferrer noopener">'
                    + html.escape(label)
                    + "</a>"
                )
            else:
                pieces.append(html.escape(text[item["start"] : item["end"]]))
        end = item["end"]
    pieces.append(html.escape(text[end:]))
    return "".join(pieces)


def view_markdown(document, pages):
    lines = document["body"].splitlines()
    output, paragraph, code, headings = [], [], [], []
    fence = None
    list_kind = None
    anchors = iter(view_heading_ids(document["body"]))
    consumed = set()

    def flush():
        if paragraph:
            output.append("<p>" + view_inline("\n".join(paragraph), document, pages) + "</p>")
            paragraph[:] = []

    def close_list():
        nonlocal list_kind
        if list_kind:
            output.append("</" + list_kind + ">")
            list_kind = None

    for number, line in enumerate(lines):
        if number in consumed:
            continue
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                marker
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= fence[1]
                and not marker.group(2).strip()
            ):
                output.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code, fence = [], None
            else:
                code.append(line)
            continue
        if marker:
            flush()
            close_list()
            fence = (marker.group(1)[0], len(marker.group(1)))
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        item = re.match(r"^\s*(?:([-+*])|([0-9]+)[.)])\s+(.+)$", line)

        def cells(row):
            return [
                x.strip().replace("\\|", "|")
                for x in re.split(r"(?<!\\)\|", row.strip().strip("|"))
            ]

        table = (
            number + 1 < len(lines)
            and "|" in line
            and "|" in lines[number + 1]
            and all(re.fullmatch(r":?-{3,}:?", x) for x in cells(lines[number + 1]))
            and len(cells(line)) == len(cells(lines[number + 1]))
        )
        if table:
            flush()
            close_list()
            headers = cells(line)
            output.append(
                '<div class="table-scroll" tabindex="0" role="region" aria-label="Document table">'
                "<table><thead><tr>"
                + "".join(
                    '<th scope="col">' + view_inline(x, document, pages) + "</th>" for x in headers
                )
                + "</tr></thead><tbody>"
            )
            consumed.add(number + 1)
            cursor = number + 2
            while (
                cursor < len(lines)
                and "|" in lines[cursor]
                and len(cells(lines[cursor])) == len(headers)
            ):
                output.append(
                    "<tr>"
                    + "".join(
                        "<td>" + view_inline(x, document, pages) + "</td>"
                        for x in cells(lines[cursor])
                    )
                    + "</tr>"
                )
                consumed.add(cursor)
                cursor += 1
            output.append("</tbody></table></div>")
        elif heading:
            flush()
            close_list()
            level = min(len(heading.group(1)) + 1, 6)
            # Namespace source headings apart from the document shell's IDs.
            anchor = "section-" + next(anchors)
            title = heading.group(2)
            headings.append((anchor, title))
            output.append(
                "<h"
                + str(level)
                + ' id="'
                + anchor
                + '">'
                + view_inline(title, document, pages)
                + "</h"
                + str(level)
                + ">"
            )
        elif item:
            flush()
            kind = "ul" if item.group(1) else "ol"
            if list_kind != kind:
                close_list()
                list_kind = kind
                output.append("<" + kind + ">")
            output.append("<li>" + view_inline(item.group(3), document, pages) + "</li>")
        elif not line.strip():
            flush()
            close_list()
        elif line.startswith("> "):
            flush()
            close_list()
            output.append("<blockquote>" + view_inline(line[2:], document, pages) + "</blockquote>")
        else:
            close_list()
            paragraph.append(line)
    flush()
    close_list()
    if fence:
        output.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    return "\n".join(output), headings


def view_html(title, content, navigation, value, provenance=""):
    escape = html.escape
    notice = (
        '<p class="notice">Historical reference. This is not current operating guidance.</p>'
        if value["mode"] == "history"
        else ""
    )
    return (
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; '
        'style-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">'
        "<title>" + escape(title) + "</title><style>" + VIEW_CSS + "</style></head><body>"
        '<a class="skip" href="#content">Skip to content / 본문으로</a><header>'
        '<div class="eyebrow">Verified documentation · ' + escape(value["audience"]) + "</div>"
        "<h1>"
        + escape(title)
        + "</h1><p>"
        + escape(value["release_id"])
        + " · "
        + escape(value["security"])
        + "</p>"
        '</header><div class="layout"><nav aria-label="Document navigation">'
        + navigation
        + "</nav>"
        '<main id="content" tabindex="-1">' + notice + content + "</main></div>"
        "<footer>"
        + provenance
        + "<p>Offline document · Source-derived, not a hand-maintained copy.</p>"
        "</footer></body></html>\n"
    ).encode("utf-8")


def render_view_files(value):
    validate_view_input(value)
    pages = {
        x["source_path"]: {"file": view_page_name(x), "anchors": view_heading_ids(x["body"])}
        for x in value["documents"]
    }
    links = (
        "<ul>"
        + "".join(
            '<li><a href="' + view_page_name(x) + '">' + html.escape(x["title"]) + "</a></li>"
            for x in value["documents"]
        )
        + "</ul>"
    )
    files = {}
    entries = []
    for document in value["documents"]:
        body, headings = view_markdown(document, pages)
        toc = (
            "<ul>"
            + "".join(
                '<li><a href="#' + anchor + '">' + html.escape(title) + "</a></li>"
                for anchor, title in headings
            )
            + "</ul>"
        )
        navigation = '<p><a href="index.html">All documents / 문서 목록</a></p>' + toc
        source = (
            "<p>Owner: "
            + html.escape(document["owner"])
            + " · Source SHA256: <code>"
            + document["source_sha256"]
            + "</code></p>"
        )
        files[view_page_name(document)] = view_html(
            document["title"], body, navigation, value, source
        )
        entries.append(
            {
                key: document[key]
                for key in (
                    "doc_id",
                    "title",
                    "owner",
                    "kind",
                    "source_sha256",
                    "snapshot_sha256",
                    "review_sha256",
                )
            }
        )
        entries[-1]["page"] = view_page_name(document)
    rows = "".join(
        '<tr><th scope="row">'
        + name
        + "</th><td>"
        + html.escape(value["components"][name]["revision"])
        + "</td><td><code>"
        + value["components"][name]["sha256"]
        + "</code></td></tr>"
        for name in VIEW_COMPONENTS
    )
    index = (
        "<h2>Documents / 문서</h2>"
        + links
        + "<h2>Verified release set / 검증 버전</h2>"
        + (
            '<div class="table-scroll" tabindex="0" role="region" aria-label="Release components">'
            '<table class="release-table">'
            "<caption>Component versions bound to this document set</caption>"
            '<thead><tr><th scope="col">Component</th><th scope="col">Revision</th>'
            '<th scope="col">SHA256</th></tr></thead>'
            "<tbody>" + rows + "</tbody></table></div>"
        )
    )
    files["index.html"] = view_html("Documentation / 문서 안내", index, links, value)
    manifest = {
        "schema_version": "1.0",
        "release_id": value["release_id"],
        "audience": value["audience"],
        "security": value["security"],
        "mode": value["mode"],
        "complete": True,
        "input_sha256": object_digest(value),
        "verification_sha256": value["verification_sha256"],
        "documents": entries,
        "files": [
            {"path": path, "sha256": digest(data), "bytes": len(data)}
            for path, data in sorted(files.items())
        ],
        "manifest_identity": "recomputed from exact approved input; no circular self-digest",
    }
    files["manifest.json"] = json_bytes(manifest) + b"\n"
    return files


@contextlib.contextmanager
def view_tree(directory):
    """Open the final input/output directory through its parent, without aliases."""
    parent_path, name = os.path.split(os.path.abspath(str(directory)))
    relative_path(name)
    with SourceTree(parent_path) as parent:
        # Preserve SourceTree's read/check implementation while binding its root
        # by an already-open descriptor, not realpath of an untrusted final link.
        tree = SourceTree.__new__(SourceTree)
        tree.root = os.path.join(parent.root, name)
        tree.max_bytes = 67108864
        try:
            tree.fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.fd)
        except OSError:
            raise DocumentError("UNSAFE_VIEW_DIRECTORY") from None
        with tree:
            parent._check([])
            tree._check([])
            yield tree
            tree._check([])
            parent._check([])


def verify_view_files(output, files):
    with view_tree(output) as tree:
        names = os.listdir(tree.fd)
        if set(names) != set(files):
            raise DocumentError("GENERATED_VIEW_DRIFT")
        for path, content in files.items():
            if tree.read(path) != content:
                raise DocumentError("GENERATED_VIEW_DRIFT")
        if set(os.listdir(tree.fd)) != set(names):
            raise DocumentError("SOURCE_DRIFT")
        tree._check([])


def publish_view(output, value, recheck):
    """Reserve a new private directory exclusively; never replace an existing one.

    A directory without its complete expected inventory is not a valid bundle.
    Normal failures remove only unchanged files created by this call; interruption
    leaves an unverified partial directory, never an automatic latest publication.
    """
    files = render_view_files(value)
    output = os.path.abspath(str(output))
    parent_path, name = os.path.split(output)
    relative_path(name)
    with SourceTree(parent_path) as parent:
        existing = parent.info(name, missing_ok=True)
        if existing is not None:
            verify_view_files(output, files)
            recheck()
        else:
            recheck()
            os.mkdir(name, 0o700, dir_fd=parent.fd)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.fd)
            created = []
            try:
                for path in sorted(files, key=lambda p: (p == "manifest.json", p)):
                    file_fd = os.open(
                        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
                    )
                    created.append(path)
                    with os.fdopen(file_fd, "wb") as handle:
                        handle.write(files[path])
                        handle.flush()
                        os.fsync(handle.fileno())
                os.fsync(fd)
                parent._check([])
                if _identity(os.stat(name, dir_fd=parent.fd, follow_symlinks=False)) != _identity(
                    os.fstat(fd)
                ):
                    raise DocumentError("SOURCE_DRIFT")
                recheck()
                verify_view_files(output, files)
                os.fsync(parent.fd)
            except Exception:
                # Do not remove changed/user-added content during failure cleanup.
                try:
                    parent._check([])
                    if _identity(
                        os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
                    ) == _identity(os.fstat(fd)):
                        with SourceTree(output, 67108864) as target:
                            for path in created:
                                if target.read(path, missing_ok=True) == files[path]:
                                    os.unlink(path, dir_fd=fd)
                        if not os.listdir(fd):
                            os.rmdir(name, dir_fd=parent.fd)
                except (OSError, DocumentError):
                    pass
                raise
            finally:
                os.close(fd)
    return {
        "schema_version": "1.0",
        "complete": True,
        "release_id": value["release_id"],
        "input_sha256": object_digest(value),
        "files": [
            {"path": p, "sha256": digest(b), "bytes": len(b)} for p, b in sorted(files.items())
        ],
    }


def build_view(root, release_path, output, audience, security="INTERNAL", history=False):
    if security in ("PUBLIC", "EXTERNAL_APPROVED"):
        raise DocumentError("APPROVED_EXTERNAL_INPUT_REQUIRED")
    value = prepare_view_input(root, release_path, audience, security, history)

    def recheck():
        if prepare_view_input(root, release_path, audience, security, history) != value:
            raise DocumentError("SOURCE_DRIFT")

    return publish_view(output, value, recheck)


def validate_view(root, release_path, output, audience, security="INTERNAL", history=False):
    try:
        value = prepare_view_input(root, release_path, audience, security, history)
        verify_view_files(output, render_view_files(value))
        if prepare_view_input(root, release_path, audience, security, history) != value:
            raise DocumentError("SOURCE_DRIFT")
        return {"valid": True, "code": "MATCH", "input_sha256": object_digest(value)}
    except (OSError, ValueError):
        return {"valid": False, "code": "VIEW_OR_SOURCE_DRIFT"}


def load_approved_view_input(bundle, approved_sha256):
    """Approval digest is a separate trusted caller input, never a file's own label.

    This checks an already approved finite input, not actor authentication or a
    grant to export/publish. The external builder cannot inspect an internal checkout.
    """
    hash_value(approved_sha256)
    with view_tree(bundle) as tree:
        if os.listdir(tree.fd) != ["approved-input.json"]:
            raise DocumentError("UNAPPROVED_INPUT")
        value = strict_json(tree.read("approved-input.json"))
        validate_view_input(value)
        if object_digest(value) != approved_sha256 or value["security"] not in (
            "PUBLIC",
            "EXTERNAL_APPROVED",
        ):
            raise DocumentError("APPROVAL_MISMATCH")
        if os.listdir(tree.fd) != ["approved-input.json"]:
            raise DocumentError("UNAPPROVED_INPUT")
        tree._check([])
        return value


def build_approved_view(bundle, output, approved_sha256):
    value = load_approved_view_input(bundle, approved_sha256)

    def recheck():
        if load_approved_view_input(bundle, approved_sha256) != value:
            raise DocumentError("SOURCE_DRIFT")

    return publish_view(output, value, recheck)


def configured_views(root, build=False):
    """Existing work docs gate, not a separate scheduler or publication service."""
    with SourceTree(root) as tree:
        policy_bytes = tree.read(POLICY)
        policy = strict_json(policy_bytes)
        config = policy.get("offline_views", {"schema_version": "1.0", "entries": []})
        if (
            not isinstance(config, dict)
            or set(config) != {"schema_version", "entries"}
            or config["schema_version"] != "1.0"
            or not isinstance(config["entries"], list)
            or len(config["entries"]) > 100
        ):
            raise DocumentError("INVALID_VIEW_CONFIGURATION")
        outputs = set()
        for entry in config["entries"]:
            if not isinstance(entry, dict) or set(entry) != {
                "release_manifest",
                "output",
                "audience",
                "security",
                "history",
            }:
                raise DocumentError("INVALID_VIEW_CONFIGURATION")
            relative_path(entry["release_manifest"])
            relative_path(entry["output"])
            output = entry["output"]
            if (
                entry["audience"] not in VIEW_AUDIENCES
                or type(entry["history"]) is not bool
                or entry["security"] not in ("INTERNAL", "RESTRICTED")
                or not output.startswith(("specs/", ".ai-team/local/document-views/"))
                or any(
                    output == p or output.startswith(p + "/") or p.startswith(output + "/")
                    for p in outputs
                )
            ):
                raise DocumentError("INVALID_VIEW_CONFIGURATION")
            outputs.add(output)
        # Preflight the entire set before creating anything. Individual bundles
        # remain independently complete; no all-release activation is performed.
        values = [
            prepare_view_input(
                root, x["release_manifest"], x["audience"], x["security"], x["history"]
            )
            for x in config["entries"]
        ]
        results = []
        for number, entry in enumerate(config["entries"]):
            value = values[number]
            _, _, chain = tree._parents(entry["output"])
            try:
                tree._check(chain)
                output = os.path.join(tree.root, entry["output"])

                def recheck(chain=chain, entry=entry, value=value):
                    tree._check(chain)
                    if (
                        tree.read(POLICY) != policy_bytes
                        or prepare_view_input(
                            root,
                            entry["release_manifest"],
                            entry["audience"],
                            entry["security"],
                            entry["history"],
                        )
                        != value
                    ):
                        raise DocumentError("SOURCE_DRIFT")

                if build:
                    publish_view(output, value, recheck)
                result = validate_view(
                    root,
                    entry["release_manifest"],
                    output,
                    entry["audience"],
                    entry["security"],
                    entry["history"],
                )
                recheck()
                results.append(result)
            finally:
                for _, _, fd in reversed(chain):
                    os.close(fd)
        if tree.read(POLICY) != policy_bytes:
            raise DocumentError("SOURCE_DRIFT")
        return {
            "valid": all(x["valid"] for x in results),
            "checked": len(results),
            "views": results,
            "status": "NOT_CONFIGURED"
            if not results
            else "MATCH"
            if all(x["valid"] for x in results)
            else "STALE",
        }
