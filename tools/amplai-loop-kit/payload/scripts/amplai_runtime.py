#!/usr/bin/env python3
"""Durable decision and asynchronous cross-app runtime for AMPLAI Loop V2.

The module intentionally separates two planes:

* project state (Git-friendly): CR, Work, Contract, Question, Decision, Evidence,
  and an append-only event chain;
* host-local state (gitignored): repository paths, worker commands, leases,
  sessions, locks, and run logs.

The local supervisor is deterministic.  It may claim and launch READY work, but
it never invents goals, chooses architecture, edits code, merges, deploys, or
releases.  Reasoning remains inside each app's /design and /work loop.

Python 3.6+; standard library only.
"""
from __future__ import print_function

import copy
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid


SCHEMA_VERSION = "1.0"
RUNTIME_PROTOCOL = "amplai.async-cross-app.v1"
HASH_FIELD = "content_hash"

WORK_STATUSES = (
    "DRAFT", "WAITING", "READY", "CLAIMED", "RUNNING", "BLOCKED",
    "HUMAN_REQUIRED", "FAILED", "DONE", "CANCELLED",
)
ACTIVE_WORK_STATUSES = ("CLAIMED", "RUNNING")
TERMINAL_WORK_STATUSES = ("DONE", "CANCELLED")
QUESTION_STATUSES = ("OPEN", "RESOLVED", "DEFERRED", "CANCELLED")
DECISION_AUTHORITIES = ("AUTO", "CHALLENGE", "HUMAN")
AUTHORITY_RANK = {"AUTO": 0, "CHALLENGE": 1, "HUMAN": 2}
EVIDENCE_MODES = ("DIRECT", "LOCAL", "PARALLEL")
EVIDENCE_TYPES = (
    "code", "test", "runtime", "documentation", "git", "web",
    "benchmark", "contract", "review", "human_approval", "artifact", "other",
)
DECISION_CLASSES = (
    "engineering", "architecture", "compatibility", "persistence",
    "performance_architecture", "cross_app_contract", "public_contract",
    "domain", "product", "production", "safety", "security", "privacy",
    "legal", "destructive", "unknown",
)
REVERSIBILITY = ("high", "medium", "low")
BLAST_RADIUS = ("low", "medium", "high")
# Prohibitions the Runtime itself can refuse.  Everything else in
# policy.forbidden_automatic_actions is worker-side prompt guidance and is NOT
# machine-enforced; `project verify` reports which is which.
ENFORCED_PROHIBITIONS = {
    "lower AUTO/CHALLENGE/HUMAN authority below policy minimum":
        "create_question/record_decision authority floor",
    "resolve domain/product/production/safety/security/privacy/legal decisions without human evidence":
        "record_decision human_approval evidence check",
    "launch work in DRAFT, BLOCKED, HUMAN_REQUIRED, FAILED, DONE, or CANCELLED":
        "claim_work READY-only guard",
}
UNATTENDED_BYPASS_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
)
RESEALABLE_KINDS = (
    "project", "policy", "app", "contract", "change", "work",
    "question", "decision", "evidence",
)
CHILD_ID_MARKERS = ("W", "Q", "D", "E")
# A change_id that already looks like a derived child id (CR-0001-W001)
# would make its own children unresolvable.
CHILD_SUFFIX_RE = re.compile(r"-[WQDE][0-9]+$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
CONTRACT_REF_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,95})@([A-Za-z0-9][A-Za-z0-9._-]{0,63})$")


DEFAULT_POLICY = {
    "schema_version": SCHEMA_VERSION,
    "runtime_protocol": RUNTIME_PROTOCOL,
    "decision": {
        "human_classes": [
            "domain", "product", "production", "safety", "security",
            "privacy", "legal", "destructive", "public_contract",
        ],
        "challenge_classes": [
            "architecture", "compatibility", "persistence",
            "performance_architecture", "cross_app_contract",
        ],
        "auto_requires_evidence": True,
        "challenge_requires_independent_review": True,
        "human_requires_approval_evidence": True,
    },
    "evidence": {
        "max_parallel_lanes": 3,
        "max_subagent_depth": 1,
        "parallelizable_only": True,
        "parent_owns_decision": True,
    },
    "work": {
        "default_max_attempts": 3,
        "default_app_concurrency": 1,
        "done_requires_evidence": True,
    },
    "supervisor": {
        "poll_seconds": 5,
        "lease_seconds": 300,
        "heartbeat_seconds": 30,
        "lock_wait_seconds": 10,
        "lock_stale_seconds": 120,
        "worker_timeout_seconds": 7200,
        "auto_retry_worker_failures": True,
        "retry_backoff_seconds": 30,
        "retry_backoff_max_seconds": 900,
    },
    "forbidden_automatic_actions": [
        "invent cross-app goals or affected apps",
        "lower AUTO/CHALLENGE/HUMAN authority below policy minimum",
        "resolve domain/product/production/safety/security/privacy/legal decisions without human evidence",
        "merge, push, deploy, release, or perform destructive production operations",
        "treat rendered handoff text as the source of truth",
        "launch work in DRAFT, BLOCKED, HUMAN_REQUIRED, FAILED, DONE, or CANCELLED",
    ],
}


class AmplaiError(Exception):
    """Base runtime error."""


class ValidationError(AmplaiError):
    """Artifact or request violates the protocol."""


class NotFoundError(AmplaiError):
    """Requested artifact does not exist."""


class ConflictError(AmplaiError):
    """Current durable state conflicts with the requested transition."""


class LockError(AmplaiError):
    """Project mutation lock could not be acquired."""


def utc_now():
    return utc_naive_now().replace(microsecond=0).isoformat() + "Z"


def utc_naive_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def parse_time(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError("UTC timestamp must end in Z: %r" % value)
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValidationError("invalid UTC timestamp: %r" % value)


def utc_after(seconds):
    dt = utc_naive_now() + datetime.timedelta(seconds=int(seconds))
    return dt.replace(microsecond=0).isoformat() + "Z"


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def hash_object(value, omit_fields=None):
    omit_fields = set(omit_fields or [HASH_FIELD])
    body = copy.deepcopy(value)
    if isinstance(body, dict):
        for field in omit_fields:
            body.pop(field, None)
    return "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()


def seal(value):
    result = copy.deepcopy(value)
    result[HASH_FIELD] = hash_object(result)
    return result


def verify_seal(value, label="object"):
    if not isinstance(value, dict):
        raise ValidationError("%s must be a JSON object" % label)
    actual = value.get(HASH_FIELD)
    expected = hash_object(value)
    if actual != expected:
        raise ValidationError(
            "%s content_hash mismatch: expected %s, got %s" %
            (label, expected, actual)
        )
    return True


def sha256_file(path):
    digest = hashlib.sha256()
    with io.open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def read_json(path):
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except IOError as exc:
        raise NotFoundError("cannot read JSON %s: %s" % (path, exc))
    except ValueError as exc:
        raise ValidationError("invalid JSON %s: %s" % (path, exc))


def write_json_atomic(path, value, mode=0o644):
    ensure_dir(os.path.dirname(path))
    fd, temp_path = tempfile.mkstemp(prefix=".amplai-write-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as handle:
            text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def append_jsonl(path, value):
    ensure_dir(os.path.dirname(path))
    with io.open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path):
    result = []
    if not os.path.exists(path):
        return result
    with io.open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                result.append(json.loads(line))
            except ValueError as exc:
                raise ValidationError("invalid JSONL %s:%d: %s" % (path, lineno, exc))
    return result


def validate_id(value, field="id"):
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValidationError(
            "%s must match %s (got %r)" % (field, ID_RE.pattern, value)
        )
    return value


def validate_nonempty(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a non-empty string" % field)
    return value.strip()


def unique_strings(values, field, allow_empty=True):
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValidationError("%s must be an array" % field)
    result = []
    seen = set()
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError("%s entries must be non-empty strings" % field)
        item = item.strip()
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    if not allow_empty and not result:
        raise ValidationError("%s must contain at least one entry" % field)
    return result


def safe_relative(path):
    path = path.replace("\\", "/")
    if path.startswith("/") or path.startswith("../") or "/../" in path:
        raise ValidationError("path must be repository-relative: %s" % path)
    return path


def write_managed_gitignore(path):
    start = "# >>> AMPLAI ASYNC LOCAL STATE >>>"
    end = "# <<< AMPLAI ASYNC LOCAL STATE <<<"
    block = "\n".join([
        start,
        ".amplai/local/",
        ".amplai/locks/",
        end,
    ])
    existing = ""
    if os.path.exists(path):
        with io.open(path, "r", encoding="utf-8") as handle:
            existing = handle.read()
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if pattern.search(existing):
        updated = pattern.sub(block, existing)
    else:
        updated = existing.rstrip() + ("\n\n" if existing.strip() else "") + block + "\n"
    if updated != existing:
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(updated)


class AtomicDirectoryLock(object):
    """Portable best-effort host-local lock based on atomic mkdir.

    Ownership is proven by the token stored inside the lock directory.  Two
    invariants keep mutual exclusion intact:

    * a lock directory is only ever removed by the holder that still owns it;
    * a stale lock is reclaimed by moving it aside with a single atomic
      rename, so exactly one contender can win the reclaim race.
    """

    def __init__(self, lock_dir, wait_seconds=10, stale_seconds=120):
        self.lock_dir = lock_dir
        self.wait_seconds = float(wait_seconds)
        self.stale_seconds = float(stale_seconds)
        self.token = str(uuid.uuid4())
        self.acquired = False
        self.created_at = None

    @property
    def owner_path(self):
        return os.path.join(self.lock_dir, "owner.json")

    def _owner_token(self):
        try:
            return read_json(self.owner_path).get("token")
        except Exception:
            return None

    def _stale(self):
        try:
            owner = read_json(self.owner_path)
            # A long-lived holder refreshes heartbeat_at; a short-lived one only
            # ever writes created_at.  Prefer the newer field when present so a
            # supervisor that is still running is not reclaimed underneath it.
            marker = owner.get("heartbeat_at") or owner.get("created_at")
            created = parse_time(marker)
            age = (utc_naive_now() - created).total_seconds()
            return age > self.stale_seconds
        except Exception:
            # A lock directory without a readable owner file is either being
            # created right now or was left behind by a crash.  Fall back to
            # the directory mtime so a half-written lock is not stolen.
            try:
                age = time.time() - os.path.getmtime(self.lock_dir)
                return age > self.stale_seconds
            except OSError:
                return False

    def _reclaim_stale(self):
        aside = "%s.stale-%s" % (self.lock_dir, uuid.uuid4().hex)
        try:
            os.rename(self.lock_dir, aside)
        except OSError:
            return False
        shutil.rmtree(aside, ignore_errors=True)
        return True

    def acquire(self):
        ensure_dir(os.path.dirname(self.lock_dir))
        deadline = time.time() + self.wait_seconds
        while True:
            try:
                os.mkdir(self.lock_dir)
            except OSError:
                if os.path.isdir(self.lock_dir) and self._stale():
                    if self._reclaim_stale():
                        continue
                if time.time() >= deadline:
                    raise LockError("timed out acquiring project lock: %s" % self.lock_dir)
                time.sleep(0.05)
                continue
            self.created_at = utc_now()
            write_json_atomic(self.owner_path, {
                "token": self.token,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "created_at": self.created_at,
                "heartbeat_at": self.created_at,
            })
            self.acquired = True
            return self

    def describe_owner(self):
        """Who holds this lock, for a refusal message.  Never raises."""
        try:
            owner = read_json(self.owner_path)
        except Exception:
            return None
        return {
            "pid": owner.get("pid"),
            "host": owner.get("host"),
            "created_at": owner.get("created_at"),
            "heartbeat_at": owner.get("heartbeat_at"),
        }

    def heartbeat(self):
        """Refresh the liveness marker of a lock this process still owns.

        A holder that runs for longer than ``stale_seconds`` must call this or
        another contender will reclaim the lock as stale.  Refusing to write
        when the token no longer matches keeps a reclaimed lock from being
        resurrected by its former owner.
        """
        if not self.acquired:
            return False
        if self._owner_token() != self.token:
            return False
        write_json_atomic(self.owner_path, {
            "token": self.token,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created_at": self.created_at,
            "heartbeat_at": utc_now(),
        })
        return True

    def release(self):
        if not self.acquired:
            return
        self.acquired = False
        if self._owner_token() != self.token:
            # Our lock was reclaimed as stale and now belongs to someone else.
            # Deleting it here would destroy the new owner's mutual exclusion.
            raise LockError("project lock ownership changed: %s" % self.lock_dir)
        shutil.rmtree(self.lock_dir, ignore_errors=True)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.release()
        except LockError:
            if exc_type is None:
                raise
        return False



class ProjectStore(object):
    """Read and mutate one durable AMPLAI project store."""

    def __init__(self, home):
        self.home = os.path.abspath(os.path.expanduser(home))
        self.project_path = os.path.join(self.home, "project.json")
        self.policy_path = os.path.join(self.home, "policy.json")
        if not os.path.isfile(self.project_path):
            raise NotFoundError("not an AMPLAI project store: %s" % self.home)
        self.project = self._read_sealed(self.project_path, "project")
        self.policy = self._read_sealed(self.policy_path, "policy")
        if self.project.get("runtime_protocol") != RUNTIME_PROTOCOL:
            raise ValidationError("unsupported runtime protocol: %r" % self.project.get("runtime_protocol"))
        self._ensure_layout()

    @classmethod
    def initialize(cls, home, project_id, name=None, git_init=True, policy=None):
        home = os.path.abspath(os.path.expanduser(home))
        validate_id(project_id, "project_id")
        if os.path.exists(os.path.join(home, "project.json")):
            store = cls(home)
            if store.project.get("project_id") != project_id:
                raise ConflictError(
                    "existing project_id is %s, not %s" %
                    (store.project.get("project_id"), project_id)
                )
            return store
        ensure_dir(home)
        for rel in (
            "apps", "contracts", "changes", ".amplai/local/apps",
            ".amplai/local/leases", ".amplai/local/sessions",
            ".amplai/local/runs", ".amplai/locks",
        ):
            ensure_dir(os.path.join(home, rel))
        project = seal({
            "schema_version": SCHEMA_VERSION,
            "kind": "project",
            "runtime_protocol": RUNTIME_PROTOCOL,
            "project_id": project_id,
            "name": name or project_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        })
        effective_policy = copy.deepcopy(policy or DEFAULT_POLICY)
        effective_policy["kind"] = "policy"
        effective_policy["schema_version"] = SCHEMA_VERSION
        effective_policy["runtime_protocol"] = RUNTIME_PROTOCOL
        effective_policy = seal(effective_policy)
        write_json_atomic(os.path.join(home, "project.json"), project)
        write_json_atomic(os.path.join(home, "policy.json"), effective_policy)
        events = os.path.join(home, ".amplai", "events.jsonl")
        ensure_dir(os.path.dirname(events))
        if not os.path.exists(events):
            with io.open(events, "w", encoding="utf-8"):
                pass
        write_managed_gitignore(os.path.join(home, ".gitignore"))
        if git_init and not os.path.isdir(os.path.join(home, ".git")):
            try:
                subprocess.check_call(
                    ["git", "init"], cwd=home,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.CalledProcessError):
                raise ValidationError("git init failed for project store: %s" % home)
        store = cls(home)
        with store.lock():
            store._append_event_unlocked(
                "project.initialized", "project", project_id, "system",
                {"project_id": project_id},
            )
        return store

    def _ensure_layout(self):
        for rel in (
            "apps", "contracts", "changes", ".amplai/local/apps",
            ".amplai/local/leases", ".amplai/local/sessions",
            ".amplai/local/runs", ".amplai/locks",
        ):
            ensure_dir(os.path.join(self.home, rel))

    def lock(self):
        supervisor = self.policy.get("supervisor") or {}
        return AtomicDirectoryLock(
            os.path.join(self.home, ".amplai", "locks", "project.lock"),
            supervisor.get("lock_wait_seconds", 10),
            supervisor.get("lock_stale_seconds", 120),
        )

    def supervisor_lock(self):
        """The Store's single-supervisor lock.

        The Store owns the right to run a supervisor, so this lock lives beside
        the project lock rather than in any application repository.  Whichever
        copy of ``amplai_supervisor.py`` starts, it contends for this one
        directory, which is what makes "only one supervisor" enforceable
        instead of merely conventional.

        It does not wait: a second supervisor is refused immediately rather
        than queued.  The holder must call ``heartbeat()`` because a supervisor
        outlives the stale window by design.
        """
        supervisor = self.policy.get("supervisor") or {}
        return AtomicDirectoryLock(
            os.path.join(self.home, ".amplai", "locks", "supervisor.lock"),
            0,
            supervisor.get("supervisor_lock_stale_seconds", 300),
        )

    def _read_sealed(self, path, label=None):
        value = read_json(path)
        verify_seal(value, label or path)
        return value

    def _write_sealed(self, path, value):
        sealed = seal(value)
        write_json_atomic(path, sealed)
        return sealed

    @property
    def events_path(self):
        return os.path.join(self.home, ".amplai", "events.jsonl")

    def _last_event(self):
        """Read the newest event without parsing the whole chain."""
        path = self.events_path
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        if size == 0:
            return None
        window = 8192
        with io.open(path, "rb") as handle:
            while True:
                start = max(0, size - window)
                handle.seek(start)
                lines = [
                    line for line in handle.read(size - start).split(b"\n")
                    if line.strip()
                ]
                # With start > 0 a second line proves the last one is complete.
                if lines and (start == 0 or len(lines) > 1):
                    try:
                        return json.loads(lines[-1].decode("utf-8"))
                    except ValueError as exc:
                        raise ValidationError(
                            "invalid trailing JSONL %s: %s" % (path, exc)
                        )
                if start == 0:
                    return None
                window *= 2

    def _append_event_unlocked(self, event_type, entity_type, entity_id, actor, data=None):
        last = self._last_event()
        previous = last.get("event_hash") if last else None
        event = {
            "schema_version": SCHEMA_VERSION,
            "sequence": (int(last.get("sequence", 0)) + 1) if last else 1,
            "timestamp": utc_now(),
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor or "unknown",
            "data": copy.deepcopy(data or {}),
            "previous_event_hash": previous,
        }
        event["event_hash"] = hash_object(event, omit_fields=["event_hash"])
        append_jsonl(self.events_path, event)
        return event

    def verify_event_chain(self):
        previous = None
        for index, event in enumerate(read_jsonl(self.events_path), start=1):
            if event.get("sequence") != index:
                raise ValidationError("event sequence mismatch at %d" % index)
            if event.get("previous_event_hash") != previous:
                raise ValidationError("event previous hash mismatch at %d" % index)
            expected = hash_object(event, omit_fields=["event_hash"])
            if event.get("event_hash") != expected:
                raise ValidationError("event hash mismatch at %d" % index)
            previous = event.get("event_hash")
        return True

    def app_path(self, app_id):
        validate_id(app_id, "app_id")
        return os.path.join(self.home, "apps", app_id + ".json")

    def local_app_path(self, app_id):
        validate_id(app_id, "app_id")
        return os.path.join(self.home, ".amplai", "local", "apps", app_id + ".json")

    def register_app(self, app_id, repo_path=None, display_name=None,
                     max_concurrency=None, runner_type="claude-code",
                     command="claude", runner_args=None, auto_start=False,
                     timeout_seconds=None, actor="human"):
        validate_id(app_id, "app_id")
        work_policy = self.policy.get("work") or {}
        supervisor_policy = self.policy.get("supervisor") or {}
        if max_concurrency is None:
            max_concurrency = work_policy.get("default_app_concurrency", 1)
        # `or` would treat an explicit 0 as "unset" and silently install 1.
        max_concurrency = int(max_concurrency)
        if max_concurrency < 1:
            raise ValidationError("max_concurrency must be >= 1")
        if runner_type not in ("claude-code", "command"):
            raise ValidationError("unsupported runner type: %s" % runner_type)
        if not isinstance(command, str) or not command.strip():
            raise ValidationError("runner command must be a non-empty string")
        validated_repo_path = None
        if repo_path:
            validated_repo_path = os.path.abspath(os.path.expanduser(repo_path))
            if not os.path.isdir(validated_repo_path):
                raise ValidationError("app repo_path does not exist: %s" % validated_repo_path)
        now = utc_now()
        with self.lock():
            path = self.app_path(app_id)
            existing = None
            if os.path.exists(path):
                existing = self._read_sealed(path, "app %s" % app_id)
            logical = {
                "schema_version": SCHEMA_VERSION,
                "kind": "app",
                "app_id": app_id,
                "display_name": display_name or (existing or {}).get("display_name") or app_id,
                "status": "ACTIVE",
                "capabilities": ["amplai.app-runtime.v1"],
                "max_concurrency": max_concurrency,
                "created_at": (existing or {}).get("created_at") or now,
                "updated_at": now,
            }
            logical = self._write_sealed(path, logical)
            if validated_repo_path:
                local = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "local_app_binding",
                    "app_id": app_id,
                    "repo_path": validated_repo_path,
                    "auto_start": bool(auto_start),
                    "runner": {
                        "type": runner_type,
                        "command": command,
                        "args": list(runner_args or []),
                        "timeout_seconds": int(
                            timeout_seconds or supervisor_policy.get("worker_timeout_seconds", 7200)
                        ),
                    },
                    "updated_at": now,
                }
                write_json_atomic(self.local_app_path(app_id), seal(local), mode=0o600)
            self._append_event_unlocked(
                "app.registered", "app", app_id, actor,
                {"max_concurrency": max_concurrency, "local_binding": bool(validated_repo_path)},
            )
        return logical

    def get_app(self, app_id):
        return self._read_sealed(self.app_path(app_id), "app %s" % app_id)

    def get_local_app(self, app_id, required=True):
        path = self.local_app_path(app_id)
        if not os.path.exists(path):
            if required:
                raise NotFoundError("no host-local binding for app %s" % app_id)
            return None
        return self._read_sealed(path, "local app %s" % app_id)

    def list_apps(self):
        result = []
        root = os.path.join(self.home, "apps")
        for name in sorted(os.listdir(root)):
            if name.endswith(".json"):
                result.append(self._read_sealed(os.path.join(root, name), "app"))
        return result

    def contract_path(self, contract_ref):
        match = CONTRACT_REF_RE.match(contract_ref or "")
        if not match:
            raise ValidationError("contract ref must be id@version: %r" % contract_ref)
        contract_id, version = match.groups()
        return os.path.join(self.home, "contracts", contract_id, version + ".json")

    def register_contract(self, contract_id, version, kind, producer_apps,
                          consumer_apps, source_ref, compatibility="BACKWARD",
                          status="ACTIVE", verification=None, actor="human"):
        validate_id(contract_id, "contract_id")
        validate_id(version, "version")
        producer_apps = unique_strings(producer_apps, "producer_apps", False)
        consumer_apps = unique_strings(consumer_apps, "consumer_apps", False)
        for app_id in producer_apps + consumer_apps:
            self.get_app(app_id)
        if status not in ("DRAFT", "ACTIVE", "DEPRECATED", "SUPERSEDED"):
            raise ValidationError("invalid contract status: %s" % status)
        if compatibility not in ("BACKWARD", "FORWARD", "FULL", "BREAKING", "UNKNOWN"):
            raise ValidationError("invalid compatibility: %s" % compatibility)
        ref = "%s@%s" % (contract_id, version)
        now = utc_now()
        with self.lock():
            path = self.contract_path(ref)
            existing = self._read_sealed(path, "contract %s" % ref) if os.path.exists(path) else None
            value = {
                "schema_version": SCHEMA_VERSION,
                "kind": "contract",
                "contract_ref": ref,
                "contract_id": contract_id,
                "version": version,
                "contract_kind": validate_nonempty(kind, "kind"),
                "producer_apps": producer_apps,
                "consumer_apps": consumer_apps,
                "source_ref": validate_nonempty(source_ref, "source_ref"),
                "compatibility": compatibility,
                "status": status,
                "verification": list(verification or []),
                "created_at": (existing or {}).get("created_at") or now,
                "updated_at": now,
            }
            value = self._write_sealed(path, value)
            self._append_event_unlocked(
                "contract.registered", "contract", ref, actor,
                {"status": status, "compatibility": compatibility},
            )
        return value

    def get_contract(self, contract_ref):
        return self._read_sealed(self.contract_path(contract_ref), "contract %s" % contract_ref)

    def _next_change_id_unlocked(self):
        highest = 0
        for name in os.listdir(os.path.join(self.home, "changes")):
            match = re.match(r"^CR-(\d+)$", name)
            if match:
                highest = max(highest, int(match.group(1)))
        return "CR-%04d" % (highest + 1)

    def change_dir(self, cr_id):
        validate_id(cr_id, "cr_id")
        return os.path.join(self.home, "changes", cr_id)

    def change_path(self, cr_id):
        return os.path.join(self.change_dir(cr_id), "change.json")

    def _child_path(self, cr_id, category, object_id):
        validate_id(cr_id, "cr_id")
        validate_id(object_id, category[:-1] + "_id")
        return os.path.join(self.change_dir(cr_id), category, object_id + ".json")

    def work_path(self, cr_id, work_id):
        return self._child_path(cr_id, "work", work_id)

    def question_path(self, cr_id, question_id):
        return self._child_path(cr_id, "questions", question_id)

    def decision_path(self, cr_id, decision_id):
        return self._child_path(cr_id, "decisions", decision_id)

    def evidence_path(self, cr_id, evidence_id):
        return self._child_path(cr_id, "evidence", evidence_id)

    def _next_child_id_unlocked(self, cr_id, category, tag):
        root = os.path.join(self.change_dir(cr_id), category)
        ensure_dir(root)
        highest = 0
        pattern = re.compile(r"^%s-%s(\d+)\.json$" % (re.escape(cr_id), tag))
        for name in os.listdir(root):
            match = pattern.match(name)
            if match:
                highest = max(highest, int(match.group(1)))
        return "%s-%s%03d" % (cr_id, tag, highest + 1)

    def create_change(self, title, goal, source_app, affected_apps=None,
                      contract_refs=None, change_id=None, actor="human"):
        self.get_app(source_app)
        affected = unique_strings(affected_apps or [source_app], "affected_apps", False)
        if source_app not in affected:
            affected.insert(0, source_app)
        for app_id in affected:
            self.get_app(app_id)
        contracts = unique_strings(contract_refs or [], "contract_refs")
        for ref in contracts:
            self.get_contract(ref)
        with self.lock():
            cr_id = change_id or self._next_change_id_unlocked()
            validate_id(cr_id, "change_id")
            if CHILD_SUFFIX_RE.search(cr_id):
                raise ValidationError(
                    "change_id must not end like a derived child id (-W001/-Q001/"
                    "-D001/-E001): %s" % cr_id
                )
            path = self.change_path(cr_id)
            if os.path.exists(path):
                raise ConflictError("change already exists: %s" % cr_id)
            for category in ("work", "questions", "decisions", "evidence"):
                ensure_dir(os.path.join(self.change_dir(cr_id), category))
            now = utc_now()
            change = {
                "schema_version": SCHEMA_VERSION,
                "kind": "change",
                "change_id": cr_id,
                "title": validate_nonempty(title, "title"),
                "goal": validate_nonempty(goal, "goal"),
                "status": "DRAFT",
                "source_app": source_app,
                "affected_apps": affected,
                "contract_refs": contracts,
                "work_refs": [],
                "question_refs": [],
                "decision_refs": [],
                "evidence_refs": [],
                "created_at": now,
                "updated_at": now,
            }
            change = self._write_sealed(path, change)
            self._append_event_unlocked(
                "change.created", "change", cr_id, actor,
                {"source_app": source_app, "affected_apps": affected},
            )
        return change

    def get_change(self, cr_id):
        return self._read_sealed(self.change_path(cr_id), "change %s" % cr_id)

    def activate_change(self, cr_id, actor="human"):
        with self.lock():
            change = self.get_change(cr_id)
            if change.get("status") not in ("DRAFT", "BLOCKED", "WAITING", "HUMAN_REQUIRED"):
                if change.get("status") == "ACTIVE":
                    return change
                raise ConflictError("cannot activate change from %s" % change.get("status"))
            change["status"] = "ACTIVE"
            change["updated_at"] = utc_now()
            change = self._write_sealed(self.change_path(cr_id), change)
            self._append_event_unlocked("change.activated", "change", cr_id, actor)
        return change

    def _parse_cr_from_child(self, object_id, marker):
        token = "-" + marker
        if token not in object_id:
            raise ValidationError("%s id does not contain %s: %s" % (marker, token, object_id))
        # A change_id may itself contain "-W"/"-Q"/"-D"/"-E".  Splitting on the
        # first separator would silently address the wrong change directory, so
        # try every split point (longest prefix first) and accept the one that
        # names a change that actually exists.
        candidates = []
        index = object_id.rfind(token)
        while index > 0:
            candidates.append(object_id[:index])
            index = object_id.rfind(token, 0, index)
        for candidate in candidates:
            probe = os.path.join(self.home, "changes", candidate, "change.json")
            if os.path.isfile(probe):
                return candidate
        return candidates[0] if candidates else object_id.split(token, 1)[0]

    def get_work(self, work_id):
        cr_id = self._parse_cr_from_child(work_id, "W")
        return self._read_sealed(self.work_path(cr_id, work_id), "work %s" % work_id)

    def get_question(self, question_id):
        cr_id = self._parse_cr_from_child(question_id, "Q")
        return self._read_sealed(self.question_path(cr_id, question_id), "question %s" % question_id)

    def get_decision(self, decision_id):
        cr_id = self._parse_cr_from_child(decision_id, "D")
        return self._read_sealed(self.decision_path(cr_id, decision_id), "decision %s" % decision_id)

    def get_evidence(self, evidence_id):
        cr_id = self._parse_cr_from_child(evidence_id, "E")
        return self._read_sealed(self.evidence_path(cr_id, evidence_id), "evidence %s" % evidence_id)

    def _list_objects(self, cr_id, category):
        root = os.path.join(self.change_dir(cr_id), category)
        if not os.path.isdir(root):
            return []
        result = []
        for name in sorted(os.listdir(root)):
            if name.endswith(".json"):
                result.append(self._read_sealed(os.path.join(root, name), "%s object" % category))
        return result

    def list_changes(self):
        result = []
        root = os.path.join(self.home, "changes")
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name, "change.json")
            if os.path.isfile(path):
                result.append(self._read_sealed(path, "change %s" % name))
        return result

    def list_work(self, cr_id=None, target_app=None, statuses=None):
        statuses = set(statuses or [])
        changes = [self.get_change(cr_id)] if cr_id else self.list_changes()
        result = []
        for change in changes:
            for work in self._list_objects(change["change_id"], "work"):
                if target_app and work.get("target_app") != target_app:
                    continue
                if statuses and work.get("status") not in statuses:
                    continue
                result.append(work)
        result.sort(key=lambda item: (-int(item.get("priority", 0)), item.get("created_at", ""), item.get("work_id", "")))
        return result

    def _assert_refs(self, refs, getter, field):
        refs = unique_strings(refs or [], field)
        for ref in refs:
            getter(ref)
        return refs

    def _retry_delay_seconds(self, attempts):
        supervisor = self.policy.get("supervisor") or {}
        base = int(supervisor.get("retry_backoff_seconds", 30))
        cap = int(supervisor.get("retry_backoff_max_seconds", 900))
        if base <= 0:
            return 0
        # Bound the shift so a corrupt attempt counter cannot overflow.
        delay = base * (2 ** min(max(0, int(attempts) - 1), 16))
        return min(delay, cap)

    def retry_ready(self, work):
        stamp = work.get("retry_not_before")
        if not stamp:
            return True
        try:
            return parse_time(stamp) <= utc_naive_now()
        except ValidationError:
            return True

    def _all_dependencies_done(self, work):
        for dependency in work.get("depends_on") or []:
            if self.get_work(dependency).get("status") != "DONE":
                return False
        return True

    def _assert_dependency_scope(self, cr_id, work_id, dependencies):
        dependencies = unique_strings(dependencies or [], "depends_on")
        for dependency in dependencies:
            if dependency == work_id:
                raise ValidationError("work cannot depend on itself: %s" % work_id)
            dep = self.get_work(dependency)
            if dep.get("change_id") != cr_id:
                raise ValidationError("cross-change dependency is not supported: %s" % dependency)
        return dependencies

    def _assert_acyclic_unlocked(self, cr_id, proposed=None):
        graph = {}
        for work in self._list_objects(cr_id, "work"):
            graph[work["work_id"]] = list(work.get("depends_on") or [])
        if proposed:
            graph[proposed[0]] = list(proposed[1])
        visiting = set()
        visited = set()

        def visit(node):
            if node in visiting:
                raise ValidationError("work dependency cycle detected at %s" % node)
            if node in visited:
                return
            visiting.add(node)
            for dep in graph.get(node, []):
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        for node in sorted(graph):
            visit(node)
        return True

    def create_work(self, cr_id, target_app, goal, source_app=None,
                    depends_on=None, acceptance=None, contract_refs=None,
                    decision_refs=None, input_evidence_refs=None,
                    priority=50, max_attempts=None, work_type="implementation",
                    actor="agent"):
        change = self.get_change(cr_id)
        self.get_app(target_app)
        source_app = source_app or change.get("source_app")
        self.get_app(source_app)
        contracts = unique_strings(contract_refs or [], "contract_refs")
        for ref in contracts:
            self.get_contract(ref)
        decisions = self._validate_decision_refs(decision_refs or [], cr_id)
        evidence = self._validate_evidence_refs(input_evidence_refs or [], cr_id)
        acceptance = unique_strings(acceptance or [], "acceptance", False)
        priority = int(priority)
        if priority < 0 or priority > 100:
            raise ValidationError("priority must be 0..100")
        max_attempts = int(max_attempts or (self.policy.get("work") or {}).get("default_max_attempts", 3))
        if max_attempts < 1 or max_attempts > 20:
            raise ValidationError("max_attempts must be 1..20")
        with self.lock():
            work_id = self._next_child_id_unlocked(cr_id, "work", "W")
            dependencies = self._assert_dependency_scope(cr_id, work_id, depends_on or [])
            self._assert_acyclic_unlocked(cr_id, (work_id, dependencies))
            now = utc_now()
            work = {
                "schema_version": SCHEMA_VERSION,
                "kind": "work",
                "work_id": work_id,
                "change_id": cr_id,
                "work_type": validate_nonempty(work_type, "work_type"),
                "source_app": source_app,
                "target_app": target_app,
                "goal": validate_nonempty(goal, "goal"),
                "status": "DRAFT",
                "priority": priority,
                "depends_on": dependencies,
                "acceptance": acceptance,
                "contract_refs": contracts,
                "decision_refs": decisions,
                "question_refs": [],
                "input_evidence_refs": evidence,
                "evidence_refs": list(evidence),
                "attempts": 0,
                "max_attempts": max_attempts,
                "claimed_by": None,
                "claimed_host": None,
                "lease_id": None,
                "retry_not_before": None,
                "blocked_reason": None,
                "human_gate": None,
                "last_error": None,
                "result": None,
                "created_at": now,
                "updated_at": now,
            }
            work = self._write_sealed(self.work_path(cr_id, work_id), work)
            change = self.get_change(cr_id)
            if work_id not in change["work_refs"]:
                change["work_refs"].append(work_id)
            if target_app not in change["affected_apps"]:
                change["affected_apps"].append(target_app)
            change["updated_at"] = now
            self._write_sealed(self.change_path(cr_id), change)
            self._append_event_unlocked(
                "work.created", "work", work_id, actor,
                {"target_app": target_app, "status": "DRAFT", "depends_on": dependencies},
            )
        return work

    def _write_work_unlocked(self, work, event_type, actor, data=None):
        work["updated_at"] = utc_now()
        value = self._write_sealed(
            self.work_path(work["change_id"], work["work_id"]), work
        )
        self._append_event_unlocked(event_type, "work", work["work_id"], actor, data)
        return value

    def activate_work(self, work_id, actor="human"):
        with self.lock():
            work = self.get_work(work_id)
            if work.get("status") == "READY" or work.get("status") == "WAITING":
                return work
            if work.get("status") not in ("DRAFT", "BLOCKED", "HUMAN_REQUIRED", "FAILED"):
                raise ConflictError("cannot activate work from %s" % work.get("status"))
            if work.get("status") == "HUMAN_REQUIRED":
                unresolved = []
                for ref in work.get("question_refs") or []:
                    question = self.get_question(ref)
                    if question.get("required_authority") == "HUMAN" and question.get("status") != "RESOLVED":
                        unresolved.append(ref)
                if unresolved:
                    raise ConflictError(
                        "cannot reactivate HUMAN_REQUIRED work before HUMAN questions are resolved: %s" %
                        ", ".join(unresolved)
                    )
            if work.get("attempts", 0) >= work.get("max_attempts", 3) and work.get("status") == "FAILED":
                raise ConflictError("work exhausted max_attempts: %s" % work_id)
            work["status"] = "READY" if self._all_dependencies_done(work) else "WAITING"
            work["blocked_reason"] = None
            work["human_gate"] = None
            work["last_error"] = None
            work = self._write_work_unlocked(
                work, "work.activated", actor, {"status": work["status"]}
            )
            self._reconcile_change_unlocked(work["change_id"])
        return work

    def _lease_path(self, work_id):
        return os.path.join(self.home, ".amplai", "local", "leases", work_id + ".json")

    def _read_lease(self, work_id, required=True):
        path = self._lease_path(work_id)
        if not os.path.exists(path):
            if required:
                raise ConflictError("no active lease for %s" % work_id)
            return None
        return self._read_sealed(path, "lease %s" % work_id)

    def _write_lease(self, lease):
        write_json_atomic(self._lease_path(lease["work_id"]), seal(lease), mode=0o600)

    def _clear_lease(self, work_id):
        path = self._lease_path(work_id)
        if os.path.exists(path):
            os.unlink(path)

    def _validate_lease(self, work_id, token):
        lease = self._read_lease(work_id)
        if lease.get("token") != token:
            raise ConflictError("lease token mismatch for %s" % work_id)
        if parse_time(lease["expires_at"]) <= utc_naive_now():
            raise ConflictError("lease expired for %s" % work_id)
        return lease

    def active_work_count(self, app_id):
        return len(self.list_work(target_app=app_id, statuses=ACTIVE_WORK_STATUSES))

    def claim_work(self, work_id, worker_id, lease_seconds=None, actor="supervisor"):
        validate_nonempty(worker_id, "worker_id")
        lease_seconds = int(lease_seconds or (self.policy.get("supervisor") or {}).get("lease_seconds", 300))
        if lease_seconds < 10:
            raise ValidationError("lease_seconds must be >= 10")
        with self.lock():
            self._reconcile_all_unlocked()
            work = self.get_work(work_id)
            if work.get("status") != "READY":
                raise ConflictError("work is not READY: %s (%s)" % (work_id, work.get("status")))
            app = self.get_app(work["target_app"])
            if self.active_work_count(work["target_app"]) >= int(app.get("max_concurrency", 1)):
                raise ConflictError("target app is busy: %s" % work["target_app"])
            if work.get("attempts", 0) >= work.get("max_attempts", 3):
                raise ConflictError("work exhausted max_attempts: %s" % work_id)
            if not self.retry_ready(work):
                raise ConflictError(
                    "work is in retry backoff until %s: %s" %
                    (work.get("retry_not_before"), work_id)
                )
            token = uuid.uuid4().hex
            lease_id = uuid.uuid4().hex
            now = utc_now()
            lease = {
                "schema_version": SCHEMA_VERSION,
                "kind": "lease",
                "lease_id": lease_id,
                "work_id": work_id,
                "token": token,
                "worker_id": worker_id,
                "host": socket.gethostname(),
                "claimed_at": now,
                "heartbeat_at": now,
                "expires_at": utc_after(lease_seconds),
                "lease_seconds": lease_seconds,
            }
            self._write_lease(lease)
            work["status"] = "CLAIMED"
            work["attempts"] = int(work.get("attempts", 0)) + 1
            work["claimed_by"] = worker_id
            work["claimed_host"] = socket.gethostname()
            work["lease_id"] = lease_id
            work["retry_not_before"] = None
            work = self._write_work_unlocked(
                work, "work.claimed", actor,
                {"worker_id": worker_id, "lease_id": lease_id, "attempt": work["attempts"]},
            )
            self._reconcile_change_unlocked(work["change_id"])
        return work, token

    def start_work(self, work_id, token, actor="worker"):
        with self.lock():
            self._validate_lease(work_id, token)
            work = self.get_work(work_id)
            if work.get("status") != "CLAIMED":
                if work.get("status") == "RUNNING":
                    return work
                raise ConflictError("cannot start work from %s" % work.get("status"))
            work["status"] = "RUNNING"
            work = self._write_work_unlocked(work, "work.started", actor)
            self._reconcile_change_unlocked(work["change_id"])
        return work

    def heartbeat(self, work_id, token, actor="supervisor"):
        lease_seconds = int((self.policy.get("supervisor") or {}).get("lease_seconds", 300))
        with self.lock():
            lease = self._validate_lease(work_id, token)
            work = self.get_work(work_id)
            if work.get("status") not in ACTIVE_WORK_STATUSES:
                raise ConflictError("cannot heartbeat work in %s" % work.get("status"))
            lease["heartbeat_at"] = utc_now()
            lease["expires_at"] = utc_after(lease.get("lease_seconds", lease_seconds))
            self._write_lease(lease)
        return lease

    def _validate_evidence_refs(self, refs, cr_id=None, required=False):
        refs = unique_strings(refs or [], "evidence_refs", not required)
        for ref in refs:
            value = self.get_evidence(ref)
            if cr_id and value.get("change_id") != cr_id:
                raise ValidationError("evidence belongs to another change: %s" % ref)
        return refs

    def _validate_decision_refs(self, refs, cr_id=None):
        refs = unique_strings(refs or [], "decision_refs")
        for ref in refs:
            value = self.get_decision(ref)
            if cr_id and value.get("change_id") != cr_id:
                raise ValidationError("decision belongs to another change: %s" % ref)
            if value.get("status") != "ACTIVE":
                raise ValidationError("decision is not ACTIVE: %s" % ref)
        return refs

    def complete_work(self, work_id, token, summary, evidence_refs,
                      decision_refs=None, outputs=None, actor="worker"):
        with self.lock():
            self._validate_lease(work_id, token)
            work = self.get_work(work_id)
            if work.get("status") != "RUNNING":
                raise ConflictError("cannot complete work from %s" % work.get("status"))
            unresolved = []
            for ref in work.get("question_refs") or []:
                question = self.get_question(ref)
                if question.get("status") == "OPEN":
                    unresolved.append(ref)
            if unresolved:
                raise ConflictError(
                    "cannot complete work with OPEN questions: %s" % ", ".join(unresolved)
                )
            required = bool((self.policy.get("work") or {}).get("done_requires_evidence", True))
            evidence_refs = self._validate_evidence_refs(evidence_refs, work["change_id"], required)
            decision_refs = self._validate_decision_refs(decision_refs or [], work["change_id"])
            work["status"] = "DONE"
            work["result"] = {
                "summary": validate_nonempty(summary, "summary"),
                "evidence_refs": evidence_refs,
                "decision_refs": decision_refs,
                "outputs": unique_strings(outputs or [], "outputs"),
                "completed_at": utc_now(),
            }
            for ref in evidence_refs:
                if ref not in work["evidence_refs"]:
                    work["evidence_refs"].append(ref)
            for ref in decision_refs:
                if ref not in work["decision_refs"]:
                    work["decision_refs"].append(ref)
            work["claimed_by"] = None
            work["claimed_host"] = None
            work["lease_id"] = None
            work = self._write_work_unlocked(
                work, "work.completed", actor,
                {"evidence_refs": evidence_refs, "decision_refs": decision_refs},
            )
            self._clear_lease(work_id)
            self._reconcile_all_unlocked()
        return work

    def wait_work(self, work_id, token, depends_on, reason, actor="worker"):
        with self.lock():
            self._validate_lease(work_id, token)
            work = self.get_work(work_id)
            if work.get("status") != "RUNNING":
                raise ConflictError("cannot wait work from %s" % work.get("status"))
            dependencies = self._assert_dependency_scope(
                work["change_id"], work_id,
                list(work.get("depends_on") or []) + list(depends_on or []),
            )
            self._assert_acyclic_unlocked(work["change_id"], (work_id, dependencies))
            work["depends_on"] = dependencies
            work["status"] = "READY" if self._all_dependencies_done(work) else "WAITING"
            work["blocked_reason"] = validate_nonempty(reason, "reason")
            work["claimed_by"] = None
            work["claimed_host"] = None
            work["lease_id"] = None
            work = self._write_work_unlocked(
                work, "work.waiting", actor,
                {"status": work["status"], "depends_on": dependencies, "reason": reason},
            )
            self._clear_lease(work_id)
            self._reconcile_change_unlocked(work["change_id"])
        return work

    def block_work(self, work_id, token, reason, actor="worker"):
        return self._stop_active_work(
            work_id, token, "BLOCKED", "work.blocked", actor,
            blocked_reason=validate_nonempty(reason, "reason"),
        )

    def require_human(self, work_id, token, gate, reason, question_refs=None, actor="worker"):
        question_refs = unique_strings(question_refs or [], "question_refs")
        if not question_refs:
            raise ValidationError("HUMAN_REQUIRED requires at least one HUMAN question")
        for ref in question_refs:
            question = self.get_question(ref)
            if question.get("work_id") != work_id:
                raise ValidationError("question does not belong to work: %s" % ref)
            if question.get("required_authority") != "HUMAN":
                raise ValidationError("HUMAN_REQUIRED question must require HUMAN: %s" % ref)
        return self._stop_active_work(
            work_id, token, "HUMAN_REQUIRED", "work.human_required", actor,
            blocked_reason=validate_nonempty(reason, "reason"),
            human_gate=validate_nonempty(gate, "gate"),
            question_refs=question_refs,
        )

    def fail_work(self, work_id, token, error, retryable=False, actor="worker"):
        with self.lock():
            self._validate_lease(work_id, token)
            work = self.get_work(work_id)
            if work.get("status") not in ACTIVE_WORK_STATUSES:
                raise ConflictError("cannot fail work from %s" % work.get("status"))
            error = validate_nonempty(error, "error")
            can_retry = bool(retryable) and int(work.get("attempts", 0)) < int(work.get("max_attempts", 3))
            if can_retry:
                work["status"] = "READY" if self._all_dependencies_done(work) else "WAITING"
                work["retry_not_before"] = utc_after(
                    self._retry_delay_seconds(work.get("attempts", 0))
                )
            else:
                work["status"] = "FAILED"
                work["retry_not_before"] = None
            work["last_error"] = error
            work["claimed_by"] = None
            work["claimed_host"] = None
            work["lease_id"] = None
            work = self._write_work_unlocked(
                work, "work.failed", actor,
                {"error": error, "retryable": can_retry, "status": work["status"]},
            )
            self._clear_lease(work_id)
            self._reconcile_change_unlocked(work["change_id"])
        return work

    def _stop_active_work(self, work_id, token, new_status, event_type, actor,
                          blocked_reason=None, human_gate=None, question_refs=None):
        with self.lock():
            self._validate_lease(work_id, token)
            work = self.get_work(work_id)
            if work.get("status") not in ACTIVE_WORK_STATUSES:
                raise ConflictError("cannot transition work from %s" % work.get("status"))
            work["status"] = new_status
            work["blocked_reason"] = blocked_reason
            work["human_gate"] = human_gate
            for ref in question_refs or []:
                if ref not in work["question_refs"]:
                    work["question_refs"].append(ref)
            work["claimed_by"] = None
            work["claimed_host"] = None
            work["lease_id"] = None
            work = self._write_work_unlocked(
                work, event_type, actor,
                {"reason": blocked_reason, "gate": human_gate, "question_refs": question_refs or []},
            )
            self._clear_lease(work_id)
            self._reconcile_change_unlocked(work["change_id"])
        return work

    def _dependents_unlocked(self, work_id, change_id):
        result = []
        for other in self._list_objects(change_id, "work"):
            if other["work_id"] == work_id:
                continue
            if work_id not in (other.get("depends_on") or []):
                continue
            if other.get("status") in TERMINAL_WORK_STATUSES:
                continue
            result.append(other["work_id"])
        return result

    def _cancel_work_unlocked(self, work_id, reason, cascade, actor, seen):
        if work_id in seen:
            return []
        seen.add(work_id)
        work = self.get_work(work_id)
        status = work.get("status")
        if status == "CANCELLED":
            return []
        if status in TERMINAL_WORK_STATUSES:
            raise ConflictError("cannot cancel work in %s: %s" % (status, work_id))
        cancelled = []
        dependents = self._dependents_unlocked(work_id, work["change_id"])
        if dependents:
            if not cascade:
                raise ConflictError(
                    "work %s still blocks %s; retarget them or cancel with cascade" %
                    (work_id, ", ".join(dependents))
                )
            for dependent in dependents:
                cancelled.extend(
                    self._cancel_work_unlocked(dependent, reason, True, actor, seen)
                )
        work = self.get_work(work_id)
        previous = work.get("status")
        work["status"] = "CANCELLED"
        work["blocked_reason"] = reason
        work["human_gate"] = None
        work["claimed_by"] = None
        work["claimed_host"] = None
        work["lease_id"] = None
        work["retry_not_before"] = None
        self._write_work_unlocked(
            work, "work.cancelled", actor, {"from": previous, "reason": reason},
        )
        self._clear_lease(work_id)
        cancelled.append(work_id)
        return cancelled

    def cancel_work(self, work_id, reason, cascade=False, actor="human"):
        """Abandon a Work so its change can move on.

        A Work that other Work still depends on is only cancelled together with
        those dependents (``cascade``), because ``_all_dependencies_done`` only
        ever accepts ``DONE`` and a dangling dependency would deadlock them.
        """
        reason = validate_nonempty(reason, "reason")
        with self.lock():
            cancelled = self._cancel_work_unlocked(work_id, reason, cascade, actor, set())
            self._reconcile_all_unlocked()
        return {"cancelled": cancelled}

    def cancel_change(self, cr_id, reason, actor="human"):
        reason = validate_nonempty(reason, "reason")
        with self.lock():
            change = self.get_change(cr_id)
            if change.get("status") == "CANCELLED":
                return change
            seen = set()
            cancelled = []
            for work in self._list_objects(cr_id, "work"):
                if work.get("status") not in TERMINAL_WORK_STATUSES:
                    cancelled.extend(
                        self._cancel_work_unlocked(work["work_id"], reason, True, actor, seen)
                    )
            change = self.get_change(cr_id)
            change["status"] = "CANCELLED"
            change["updated_at"] = utc_now()
            change = self._write_sealed(self.change_path(cr_id), change)
            self._append_event_unlocked(
                "change.cancelled", "change", cr_id, actor,
                {"reason": reason, "cancelled_work": cancelled},
            )
        return change

    def retarget_work(self, work_id, depends_on, actor="human"):
        """Replace a Work's dependency set so a dead upstream can be dropped."""
        with self.lock():
            work = self.get_work(work_id)
            status = work.get("status")
            if status in ACTIVE_WORK_STATUSES:
                raise ConflictError("cannot retarget work while it is %s" % status)
            if status in TERMINAL_WORK_STATUSES:
                raise ConflictError("cannot retarget work in %s" % status)
            dependencies = self._assert_dependency_scope(
                work["change_id"], work_id, depends_on or [],
            )
            self._assert_acyclic_unlocked(work["change_id"], (work_id, dependencies))
            previous = list(work.get("depends_on") or [])
            work["depends_on"] = dependencies
            if status in ("WAITING", "READY"):
                work["status"] = "READY" if self._all_dependencies_done(work) else "WAITING"
            work = self._write_work_unlocked(
                work, "work.retargeted", actor,
                {"from": previous, "to": dependencies, "status": work["status"]},
            )
            self._reconcile_change_unlocked(work["change_id"])
        return work

    def reset_work_attempts(self, work_id, max_attempts=None, actor="human"):
        """Give an exhausted Work a fresh attempt budget."""
        with self.lock():
            work = self.get_work(work_id)
            status = work.get("status")
            if status in ACTIVE_WORK_STATUSES:
                raise ConflictError("cannot reset attempts while work is %s" % status)
            if status in TERMINAL_WORK_STATUSES:
                raise ConflictError("cannot reset attempts for work in %s" % status)
            previous = int(work.get("attempts", 0))
            work["attempts"] = 0
            work["retry_not_before"] = None
            if max_attempts is not None:
                max_attempts = int(max_attempts)
                if max_attempts < 1 or max_attempts > 20:
                    raise ValidationError("max_attempts must be 1..20")
                work["max_attempts"] = max_attempts
            work = self._write_work_unlocked(
                work, "work.attempts_reset", actor,
                {"from": previous, "max_attempts": work["max_attempts"]},
            )
        return work

    def _reconcile_work_unlocked(self, work):
        original = work.get("status")
        if original == "WAITING" and self._all_dependencies_done(work):
            work["status"] = "READY"
            work["blocked_reason"] = None
        elif original == "READY" and not self._all_dependencies_done(work):
            work["status"] = "WAITING"
        if work.get("status") != original:
            return self._write_work_unlocked(
                work, "work.reconciled", "supervisor",
                {"from": original, "to": work["status"]},
            )
        return work

    def _reconcile_change_unlocked(self, cr_id):
        change = self.get_change(cr_id)
        works = self._list_objects(cr_id, "work")
        if change.get("status") == "CANCELLED":
            return change
        statuses = [work.get("status") for work in works]
        old = change.get("status")
        if not works:
            derived = old
        elif all(status in TERMINAL_WORK_STATUSES for status in statuses):
            derived = "DONE"
        elif "HUMAN_REQUIRED" in statuses:
            derived = "HUMAN_REQUIRED"
        elif "BLOCKED" in statuses or "FAILED" in statuses:
            derived = "BLOCKED"
        elif any(status in ("READY", "CLAIMED", "RUNNING") for status in statuses):
            derived = "ACTIVE"
        elif any(status == "WAITING" for status in statuses):
            derived = "WAITING"
        elif all(status == "DRAFT" for status in statuses):
            derived = "DRAFT"
        else:
            derived = old
        if derived != old:
            change["status"] = derived
            change["updated_at"] = utc_now()
            change = self._write_sealed(self.change_path(cr_id), change)
            self._append_event_unlocked(
                "change.reconciled", "change", cr_id, "supervisor",
                {"from": old, "to": derived},
            )
        return change

    def _recover_expired_unlocked(self):
        recovered = []
        now = utc_naive_now()
        auto_retry = bool((self.policy.get("supervisor") or {}).get("auto_retry_worker_failures", True))
        this_host = socket.gethostname()
        for work in self.list_work(statuses=ACTIVE_WORK_STATUSES):
            lease = self._read_lease(work["work_id"], required=False)
            if lease is None and work.get("claimed_host") not in (None, this_host):
                # Leases are host-local.  A missing lease file on this host says
                # nothing about a worker still running on the host that claimed
                # the Work, so recovering it here would double-run it.
                continue
            expired = lease is None
            if lease is not None:
                try:
                    expired = parse_time(lease.get("expires_at")) <= now
                except ValidationError:
                    expired = True
            if not expired:
                continue
            old = work["status"]
            can_retry = auto_retry and int(work.get("attempts", 0)) < int(work.get("max_attempts", 3))
            if can_retry:
                work["status"] = "READY" if self._all_dependencies_done(work) else "WAITING"
                work["retry_not_before"] = utc_after(
                    self._retry_delay_seconds(work.get("attempts", 0))
                )
            else:
                work["status"] = "FAILED"
                work["retry_not_before"] = None
            work["last_error"] = "lease expired while %s" % old
            work["claimed_by"] = None
            work["claimed_host"] = None
            work["lease_id"] = None
            work = self._write_work_unlocked(
                work, "work.lease_expired", "supervisor",
                {"from": old, "to": work["status"], "retryable": can_retry},
            )
            self._clear_lease(work["work_id"])
            recovered.append(work["work_id"])
        return recovered

    def _reconcile_all_unlocked(self):
        self._recover_expired_unlocked()
        changes = self.list_changes()
        for change in changes:
            cr_id = change["change_id"]
            for work in self._list_objects(cr_id, "work"):
                self._reconcile_work_unlocked(work)
            self._reconcile_change_unlocked(cr_id)

    def reconcile(self):
        with self.lock():
            self._reconcile_all_unlocked()
        return self.status_summary()

    def next_ready(self, app_id=None):
        for work in self.list_work(target_app=app_id, statuses=["READY"]):
            if self.retry_ready(work):
                return work
        return None

    def add_evidence(self, cr_id, evidence_type, summary, source_kind,
                     source_locator, work_id=None, app_id=None, facts=None,
                     metadata=None, actor="agent"):
        change = self.get_change(cr_id)
        if evidence_type not in EVIDENCE_TYPES:
            raise ValidationError("invalid evidence_type: %s" % evidence_type)
        if work_id:
            work = self.get_work(work_id)
            if work.get("change_id") != cr_id:
                raise ValidationError("work belongs to another change: %s" % work_id)
        if app_id:
            self.get_app(app_id)
        with self.lock():
            evidence_id = self._next_child_id_unlocked(cr_id, "evidence", "E")
            now = utc_now()
            evidence = {
                "schema_version": SCHEMA_VERSION,
                "kind": "evidence",
                "evidence_id": evidence_id,
                "change_id": cr_id,
                "work_id": work_id,
                "evidence_type": evidence_type,
                "summary": validate_nonempty(summary, "summary"),
                "source": {
                    "kind": validate_nonempty(source_kind, "source_kind"),
                    "locator": validate_nonempty(source_locator, "source_locator"),
                    "app_id": app_id,
                    "observed_at": now,
                },
                "facts": unique_strings(facts or [], "facts"),
                "metadata": copy.deepcopy(metadata or {}),
                "created_by": actor,
                "created_at": now,
            }
            evidence = self._write_sealed(self.evidence_path(cr_id, evidence_id), evidence)
            change = self.get_change(cr_id)
            if evidence_id not in change["evidence_refs"]:
                change["evidence_refs"].append(evidence_id)
                change["updated_at"] = now
                self._write_sealed(self.change_path(cr_id), change)
            if work_id:
                work = self.get_work(work_id)
                if evidence_id not in work["evidence_refs"]:
                    work["evidence_refs"].append(evidence_id)
                    self._write_work_unlocked(
                        work, "work.evidence_linked", actor,
                        {"evidence_id": evidence_id},
                    )
            self._append_event_unlocked(
                "evidence.added", "evidence", evidence_id, actor,
                {"evidence_type": evidence_type, "work_id": work_id},
            )
        return evidence

    def _minimum_authority(self, decision_class, reversibility, blast_radius):
        policy = self.policy.get("decision") or {}
        if decision_class in (policy.get("human_classes") or []):
            return "HUMAN"
        if decision_class in (policy.get("challenge_classes") or []):
            return "CHALLENGE"
        if blast_radius == "high" or reversibility == "low":
            return "CHALLENGE"
        return "AUTO"

    def create_question(self, work_id, question, decision_class="engineering",
                        reversibility="high", blast_radius="low",
                        requested_authority=None, evidence_mode=None,
                        evidence_refs=None, evidence_lanes=None,
                        alternatives=None, actor="agent"):
        work = self.get_work(work_id)
        cr_id = work["change_id"]
        if decision_class not in DECISION_CLASSES:
            raise ValidationError("invalid decision_class: %s" % decision_class)
        if reversibility not in REVERSIBILITY:
            raise ValidationError("invalid reversibility: %s" % reversibility)
        if blast_radius not in BLAST_RADIUS:
            raise ValidationError("invalid blast_radius: %s" % blast_radius)
        minimum = self._minimum_authority(decision_class, reversibility, blast_radius)
        authority = requested_authority or minimum
        if authority not in DECISION_AUTHORITIES:
            raise ValidationError("invalid authority: %s" % authority)
        if AUTHORITY_RANK[authority] < AUTHORITY_RANK[minimum]:
            raise ValidationError(
                "authority %s is below policy minimum %s" % (authority, minimum)
            )
        evidence_refs = self._validate_evidence_refs(evidence_refs or [], cr_id)
        lanes = list(evidence_lanes or [])
        max_lanes = int((self.policy.get("evidence") or {}).get("max_parallel_lanes", 3))
        if len(lanes) > max_lanes:
            raise ValidationError("evidence lanes exceed max_parallel_lanes=%d" % max_lanes)
        for index, lane in enumerate(lanes):
            if not isinstance(lane, dict):
                raise ValidationError("evidence_lanes[%d] must be an object" % index)
            validate_nonempty(lane.get("name"), "evidence_lanes[%d].name" % index)
            validate_nonempty(lane.get("goal"), "evidence_lanes[%d].goal" % index)
        if evidence_mode is None:
            if evidence_refs:
                evidence_mode = "DIRECT"
            elif len(lanes) >= 2:
                evidence_mode = "PARALLEL"
            else:
                evidence_mode = "LOCAL"
        if evidence_mode not in EVIDENCE_MODES:
            raise ValidationError("invalid evidence_mode: %s" % evidence_mode)
        if evidence_mode == "PARALLEL" and len(lanes) < 2:
            raise ValidationError("PARALLEL requires at least two independent evidence lanes")
        with self.lock():
            question_id = self._next_child_id_unlocked(cr_id, "questions", "Q")
            now = utc_now()
            value = {
                "schema_version": SCHEMA_VERSION,
                "kind": "question",
                "question_id": question_id,
                "change_id": cr_id,
                "work_id": work_id,
                "question": validate_nonempty(question, "question"),
                "decision_class": decision_class,
                "reversibility": reversibility,
                "blast_radius": blast_radius,
                "required_authority": authority,
                "policy_minimum_authority": minimum,
                "evidence_plan": {
                    "mode": evidence_mode,
                    "lanes": lanes,
                },
                "evidence_refs": evidence_refs,
                "alternatives": unique_strings(alternatives or [], "alternatives"),
                "status": "OPEN",
                "resolution_decision_ref": None,
                "review_trigger": None,
                "created_by": actor,
                "created_at": now,
                "updated_at": now,
            }
            value = self._write_sealed(self.question_path(cr_id, question_id), value)
            work = self.get_work(work_id)
            if question_id not in work["question_refs"]:
                work["question_refs"].append(question_id)
                self._write_work_unlocked(
                    work, "work.question_linked", actor,
                    {"question_id": question_id},
                )
            change = self.get_change(cr_id)
            if question_id not in change["question_refs"]:
                change["question_refs"].append(question_id)
                change["updated_at"] = now
                self._write_sealed(self.change_path(cr_id), change)
            self._append_event_unlocked(
                "question.created", "question", question_id, actor,
                {"authority": authority, "evidence_mode": evidence_mode},
            )
        return value

    def defer_question(self, question_id, review_trigger, actor="agent"):
        with self.lock():
            question = self.get_question(question_id)
            if question.get("status") != "OPEN":
                raise ConflictError("only OPEN question can be deferred")
            if question.get("required_authority") == "HUMAN":
                raise ConflictError("HUMAN question cannot be deferred; resolve or cancel it explicitly")
            question["status"] = "DEFERRED"
            question["review_trigger"] = validate_nonempty(review_trigger, "review_trigger")
            question["updated_at"] = utc_now()
            question = self._write_sealed(
                self.question_path(question["change_id"], question_id), question
            )
            self._append_event_unlocked(
                "question.deferred", "question", question_id, actor,
                {"review_trigger": review_trigger},
            )
        return question

    def _review_evidence_valid(self, refs):
        accepted = 0
        for ref in refs:
            evidence = self.get_evidence(ref)
            if evidence.get("evidence_type") != "review":
                raise ValidationError("challenge review ref is not review evidence: %s" % ref)
            metadata = evidence.get("metadata") or {}
            if metadata.get("independent") is not True:
                raise ValidationError("challenge review is not marked independent: %s" % ref)
            verdict = str(metadata.get("verdict", "")).upper()
            if verdict == "ESCALATE":
                raise ValidationError("challenge review escalated the decision: %s" % ref)
            if verdict == "ACCEPT":
                accepted += 1
        if accepted < 1:
            raise ValidationError("CHALLENGE requires at least one independent ACCEPT review")

    def _human_approval_valid(self, refs):
        for ref in refs:
            evidence = self.get_evidence(ref)
            if evidence.get("evidence_type") == "human_approval":
                metadata = evidence.get("metadata") or {}
                if metadata.get("approved_by"):
                    return True
        raise ValidationError("HUMAN decision requires human_approval evidence with approved_by")

    def record_decision(self, question_id, statement, rationale, evidence_refs,
                        authority=None, alternatives=None, review_evidence_refs=None,
                        approval_evidence_refs=None, review_trigger=None,
                        supersedes=None, actor="agent"):
        # Every check below runs under the project lock: validating first and
        # locking afterwards would let two runs resolve the same OPEN question.
        with self.lock():
            return self._record_decision_unlocked(
                question_id, statement, rationale, evidence_refs, authority,
                alternatives, review_evidence_refs, approval_evidence_refs,
                review_trigger, supersedes, actor,
            )

    def _record_decision_unlocked(self, question_id, statement, rationale,
                                  evidence_refs, authority, alternatives,
                                  review_evidence_refs, approval_evidence_refs,
                                  review_trigger, supersedes, actor):
        question = self.get_question(question_id)
        if question.get("status") != "OPEN":
            raise ConflictError("question is not OPEN: %s" % question_id)
        cr_id = question["change_id"]
        work_id = question["work_id"]
        authority = authority or question.get("required_authority")
        if authority not in DECISION_AUTHORITIES:
            raise ValidationError("invalid authority: %s" % authority)
        required = question.get("required_authority")
        if AUTHORITY_RANK[authority] < AUTHORITY_RANK[required]:
            raise ValidationError("decision authority %s is below required %s" % (authority, required))
        evidence_refs = self._validate_evidence_refs(evidence_refs, cr_id, required=True)
        review_refs = self._validate_evidence_refs(review_evidence_refs or [], cr_id)
        approval_refs = self._validate_evidence_refs(approval_evidence_refs or [], cr_id)
        if AUTHORITY_RANK[authority] >= AUTHORITY_RANK["CHALLENGE"] and authority != "HUMAN":
            if (self.policy.get("decision") or {}).get("challenge_requires_independent_review", True):
                self._review_evidence_valid(review_refs)
        if authority == "HUMAN" and (self.policy.get("decision") or {}).get("human_requires_approval_evidence", True):
            self._human_approval_valid(approval_refs)
        if supersedes:
            old = self.get_decision(supersedes)
            if old.get("change_id") != cr_id:
                raise ValidationError("cannot supersede decision from another change")
            if old.get("status") != "ACTIVE":
                raise ValidationError("can only supersede ACTIVE decision: %s" % supersedes)
        decision_id = self._next_child_id_unlocked(cr_id, "decisions", "D")
        now = utc_now()
        value = {
            "schema_version": SCHEMA_VERSION,
            "kind": "decision",
            "decision_id": decision_id,
            "change_id": cr_id,
            "work_id": work_id,
            "question_id": question_id,
            "statement": validate_nonempty(statement, "statement"),
            "rationale": validate_nonempty(rationale, "rationale"),
            "authority": authority,
            "status": "ACTIVE",
            "evidence_refs": evidence_refs,
            "review_evidence_refs": review_refs,
            "approval_evidence_refs": approval_refs,
            "alternatives": unique_strings(alternatives or [], "alternatives"),
            "review_trigger": review_trigger,
            "supersedes": supersedes,
            "superseded_by": None,
            "decided_by": actor,
            "created_at": now,
            "updated_at": now,
        }
        value = self._write_sealed(self.decision_path(cr_id, decision_id), value)
        if supersedes:
            old = self.get_decision(supersedes)
            old["status"] = "SUPERSEDED"
            old["superseded_by"] = decision_id
            old["updated_at"] = now
            self._write_sealed(self.decision_path(cr_id, supersedes), old)
        question = self.get_question(question_id)
        question["status"] = "RESOLVED"
        question["resolution_decision_ref"] = decision_id
        question["evidence_refs"] = unique_strings(
            list(question.get("evidence_refs") or []) + evidence_refs + review_refs + approval_refs,
            "question.evidence_refs",
        )
        question["updated_at"] = now
        self._write_sealed(self.question_path(cr_id, question_id), question)
        work = self.get_work(work_id)
        if decision_id not in work["decision_refs"]:
            work["decision_refs"].append(decision_id)
            self._write_work_unlocked(
                work, "work.decision_linked", actor,
                {"decision_id": decision_id},
            )
        change = self.get_change(cr_id)
        if decision_id not in change["decision_refs"]:
            change["decision_refs"].append(decision_id)
            change["updated_at"] = now
            self._write_sealed(self.change_path(cr_id), change)
        self._append_event_unlocked(
            "decision.recorded", "decision", decision_id, actor,
            {"authority": authority, "question_id": question_id, "supersedes": supersedes},
        )
        return value

    def build_work_context(self, work_id):
        work = self.get_work(work_id)
        change = self.get_change(work["change_id"])
        app = self.get_app(work["target_app"])
        contracts = [self.get_contract(ref) for ref in work.get("contract_refs") or []]
        decisions = [self.get_decision(ref) for ref in work.get("decision_refs") or []]
        questions = [self.get_question(ref) for ref in work.get("question_refs") or []]
        evidence_ids = []
        for ref in (work.get("input_evidence_refs") or []) + (work.get("evidence_refs") or []):
            if ref not in evidence_ids:
                evidence_ids.append(ref)
        for decision in decisions:
            for field in ("evidence_refs", "review_evidence_refs", "approval_evidence_refs"):
                for ref in decision.get(field) or []:
                    if ref not in evidence_ids:
                        evidence_ids.append(ref)
        for question in questions:
            for ref in question.get("evidence_refs") or []:
                if ref not in evidence_ids:
                    evidence_ids.append(ref)
        evidence = [self.get_evidence(ref) for ref in evidence_ids]
        dependencies = []
        for dependency_id in work.get("depends_on") or []:
            dependency = self.get_work(dependency_id)
            dependencies.append({
                "work_id": dependency_id,
                "target_app": dependency.get("target_app"),
                "status": dependency.get("status"),
                "result": dependency.get("result"),
            })
        context = {
            "schema_version": SCHEMA_VERSION,
            "kind": "work_context",
            "generated_at": utc_now(),
            "project": self.project,
            "app": app,
            "change": change,
            "work": work,
            "dependencies": dependencies,
            "contracts": contracts,
            "decisions": decisions,
            "questions": questions,
            "evidence": evidence,
            "instructions": {
                "source_of_truth": "Project Store objects, not rendered handoff text",
                "entry_point": "/work",
                "completion_rule": "record evidence and transition the Work durably before the worker exits",
            },
        }
        return seal(context)

    def render_handoff(self, work_id):
        context = self.build_work_context(work_id)
        work = context["work"]
        change = context["change"]
        lines = [
            "# AMPLAI Work Handoff — %s" % work_id,
            "",
            "> This is a generated view. The Project Store is the source of truth.",
            "",
            "## Change",
            "",
            "- CR: `%s`" % change["change_id"],
            "- Goal: %s" % change["goal"],
            "- Status: `%s`" % change["status"],
            "",
            "## Assigned Work",
            "",
            "- Target app: `%s`" % work["target_app"],
            "- Status: `%s`" % work["status"],
            "- Goal: %s" % work["goal"],
            "- Priority: `%s`" % work["priority"],
            "",
            "### Acceptance",
            "",
        ]
        for item in work.get("acceptance") or []:
            lines.append("- %s" % item)
        if not work.get("acceptance"):
            lines.append("- (none)")
        lines.extend(["", "## Dependencies", ""])
        for dependency in context["dependencies"]:
            lines.append(
                "- `%s` — `%s` — %s" %
                (dependency["work_id"], dependency["status"], dependency.get("target_app"))
            )
            result = dependency.get("result") or {}
            if result.get("summary"):
                lines.append("  - Result: %s" % result["summary"])
        if not context["dependencies"]:
            lines.append("- none")
        lines.extend(["", "## Active Decisions", ""])
        for decision in context["decisions"]:
            lines.append(
                "- `%s` [%s] %s" %
                (decision["decision_id"], decision["authority"], decision["statement"])
            )
        if not context["decisions"]:
            lines.append("- none")
        lines.extend(["", "## Open Questions", ""])
        open_questions = [q for q in context["questions"] if q.get("status") == "OPEN"]
        for question in open_questions:
            lines.append(
                "- `%s` [%s/%s] %s" %
                (question["question_id"], question["required_authority"],
                 question["evidence_plan"]["mode"], question["question"])
            )
        if not open_questions:
            lines.append("- none")
        lines.extend(["", "## Contract References", ""])
        for contract in context["contracts"]:
            lines.append(
                "- `%s` — %s — %s" %
                (contract["contract_ref"], contract["status"], contract["source_ref"])
            )
        if not context["contracts"]:
            lines.append("- none")
        lines.extend([
            "", "## Resume", "",
            "Use `/work` in the target app. Read the durable Work Context first, then update the same Work object before ending the run.",
            "",
            "Context hash: `%s`" % context[HASH_FIELD],
            "",
        ])
        return "\n".join(lines)

    def session_path(self, app_id):
        return os.path.join(self.home, ".amplai", "local", "sessions", app_id + ".json")

    def get_session(self, app_id):
        path = self.session_path(app_id)
        if not os.path.exists(path):
            return {"schema_version": SCHEMA_VERSION, "kind": "session_state", "app_id": app_id, "work_sessions": {}}
        return self._read_sealed(path, "session state %s" % app_id)

    def update_session(self, app_id, work_id=None, session_id=None, event=None, data=None):
        with self.lock():
            state = self.get_session(app_id)
            state.setdefault("work_sessions", {})
            if work_id and session_id:
                state["work_sessions"][work_id] = session_id
            state["last_event"] = event
            state["last_event_at"] = utc_now()
            state["last_event_data"] = copy.deepcopy(data or {})
            write_json_atomic(self.session_path(app_id), seal(state), mode=0o600)
        return state

    def session_id_for_work(self, app_id, work_id):
        return (self.get_session(app_id).get("work_sessions") or {}).get(work_id)

    def policy_drift(self, desired):
        """Top-level policy keys where the central store differs from `desired`."""
        drift = []
        if not isinstance(desired, dict):
            return drift
        ignored = (HASH_FIELD, "kind", "schema_version", "runtime_protocol")
        for key in sorted(set(desired) | set(self.policy)):
            if key in ignored:
                continue
            if self.policy.get(key) != desired.get(key):
                drift.append(key)
        return drift

    def set_policy(self, policy, actor="human"):
        if not isinstance(policy, dict):
            raise ValidationError("policy must be a JSON object")
        effective = copy.deepcopy(policy)
        effective.pop(HASH_FIELD, None)
        effective["kind"] = "policy"
        effective["schema_version"] = SCHEMA_VERSION
        effective["runtime_protocol"] = RUNTIME_PROTOCOL
        with self.lock():
            previous = self.policy.get(HASH_FIELD)
            self.policy = self._write_sealed(self.policy_path, effective)
            self._append_event_unlocked(
                "policy.updated", "policy", self.project["project_id"], actor,
                {"from": previous, "to": self.policy.get(HASH_FIELD)},
            )
        return self.policy

    def policy_enforcement_report(self):
        report = []
        for item in self.policy.get("forbidden_automatic_actions") or []:
            mechanism = ENFORCED_PROHIBITIONS.get(item)
            report.append({
                "prohibition": item,
                "machine_enforced": bool(mechanism),
                "mechanism": mechanism or "worker prompt guidance only",
            })
        return report

    def status_summary(self):
        counts = {}
        by_app = {}
        for work in self.list_work():
            status = work["status"]
            counts[status] = counts.get(status, 0) + 1
            app_id = work["target_app"]
            by_app.setdefault(app_id, {})
            by_app[app_id][status] = by_app[app_id].get(status, 0) + 1
        changes = {}
        for change in self.list_changes():
            status = change["status"]
            changes[status] = changes.get(status, 0) + 1
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": self.project["project_id"],
            "project_home": self.home,
            "changes": changes,
            "work": counts,
            "work_by_app": by_app,
            "next_ready": {
                app["app_id"]: ((self.next_ready(app["app_id"]) or {}).get("work_id"))
                for app in self.list_apps()
            },
        }

    def verify(self, include_local=False):
        findings = []

        def check(label, fn):
            try:
                fn()
            except Exception as exc:
                findings.append({"severity": "ERROR", "object": label, "message": str(exc)})

        check("project", lambda: self._read_sealed(self.project_path, "project"))
        check("policy", lambda: self._read_sealed(self.policy_path, "policy"))
        check("event_chain", self.verify_event_chain)
        for app in self.list_apps():
            app_id = app["app_id"]
            if include_local:
                check("local_app:%s" % app_id, lambda app_id=app_id: self.get_local_app(app_id))
        this_host = socket.gethostname()
        for app in self.list_apps():
            local = None
            try:
                local = self.get_local_app(app["app_id"], required=False)
            except AmplaiError:
                local = None
            if not local or not local.get("auto_start"):
                continue
            args = [str(item) for item in (local.get("runner") or {}).get("args") or []]
            bypass = [flag for flag in UNATTENDED_BYPASS_FLAGS if flag in args]
            if bypass:
                findings.append({
                    "severity": "WARNING", "object": "app:%s" % app["app_id"],
                    "message": "auto_start is on and the worker bypasses permission "
                               "checks (%s); an unattended agent can edit, commit and "
                               "push in this repository" % ", ".join(bypass),
                })
        for work in self.list_work(statuses=ACTIVE_WORK_STATUSES):
            host = work.get("claimed_host")
            if host and host != this_host:
                findings.append({
                    "severity": "WARNING", "object": work["work_id"],
                    "message": "claimed on another host (%s); its lease is not visible "
                               "here and this host will not recover it" % host,
                })
        for change in self.list_changes():
            cr_id = change["change_id"]
            check("change:%s" % cr_id, lambda cr_id=cr_id: self.get_change(cr_id))
            check("cycle:%s" % cr_id, lambda cr_id=cr_id: self._assert_acyclic_unlocked(cr_id))
            for work in self._list_objects(cr_id, "work"):
                work_id = work["work_id"]
                check("context:%s" % work_id, lambda work_id=work_id: self.build_work_context(work_id))
            for question in self._list_objects(cr_id, "questions"):
                if question.get("status") == "RESOLVED" and not question.get("resolution_decision_ref"):
                    findings.append({
                        "severity": "ERROR", "object": question["question_id"],
                        "message": "RESOLVED question lacks resolution_decision_ref",
                    })
            for decision in self._list_objects(cr_id, "decisions"):
                if decision.get("status") == "SUPERSEDED" and not decision.get("superseded_by"):
                    findings.append({
                        "severity": "ERROR", "object": decision["decision_id"],
                        "message": "SUPERSEDED decision lacks superseded_by",
                    })
        return {
            "ok": not any(item["severity"] == "ERROR" for item in findings),
            "findings": findings,
            "policy_enforcement": self.policy_enforcement_report(),
            "summary": self.status_summary(),
        }


ROOT_OBJECT_KINDS = {"project.json": "project", "policy.json": "policy"}


def store_object_kind(relative_path, value):
    """The store kind of an object, falling back to its well-known path."""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if kind in RESEALABLE_KINDS:
        return kind
    return ROOT_OBJECT_KINDS.get(relative_path)


def reseal_object(home, relative_path, actor="human"):
    """Re-record content_hash after an approved manual edit.

    The Runtime refuses to read objects whose hash does not match, so this must
    work without constructing a ProjectStore: a broken project.json or
    policy.json would otherwise be unrecoverable.
    """
    home = os.path.abspath(os.path.expanduser(home))
    rel = safe_relative(relative_path)
    path = os.path.join(home, rel)
    if not os.path.isfile(path):
        raise NotFoundError("no such Project Store object: %s" % rel)
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError("object must be a JSON object: %s" % rel)
    kind = store_object_kind(rel, value)
    if kind is None:
        raise ValidationError(
            "refusing to reseal unknown kind %r: %s" % (value.get("kind"), rel)
        )
    before = value.get(HASH_FIELD)
    sealed = seal(value)
    if before == sealed[HASH_FIELD]:
        return {"path": rel, "kind": kind, "changed": False,
                "content_hash": before}
    write_json_atomic(path, sealed)
    try:
        store = ProjectStore(home)
        with store.lock():
            store._append_event_unlocked(
                "object.resealed", kind, rel, actor,
                {"from": before, "to": sealed[HASH_FIELD]},
            )
    except AmplaiError:
        # The store may still be unreadable while a multi-object repair is in
        # progress; the reseal itself already succeeded.
        pass
    return {"path": rel, "kind": kind, "changed": True,
            "content_hash": sealed[HASH_FIELD], "previous_content_hash": before}


def scan_unsealed(home):
    """Every store object whose content_hash no longer matches its body."""
    home = os.path.abspath(os.path.expanduser(home))
    broken = []
    for root, dirs, names in os.walk(home):
        dirs[:] = [d for d in dirs if d not in (".git",)]
        if os.path.join(home, ".amplai", "local") in root:
            continue
        for name in sorted(names):
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, home).replace(os.sep, "/")
            try:
                value = read_json(path)
            except AmplaiError:
                continue
            if store_object_kind(rel, value) is None:
                continue
            if value.get(HASH_FIELD) != hash_object(value):
                broken.append(rel)
    return broken


def discover_project_home(repo_root=None, explicit=None):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get("AMPLAI_PROJECT_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    repo_root = os.path.abspath(repo_root or os.getcwd())
    local_path = os.path.join(repo_root, ".ai-team", "local", "project.json")
    if os.path.exists(local_path):
        local = read_json(local_path)
        verify_seal(local, "local project binding")
        local_home = local.get("project_home")
        if local_home:
            expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(local_home)))
            if os.path.exists(os.path.join(expanded, "project.json")):
                return expanded
    app_path = os.path.join(repo_root, ".ai-team", "app.json")
    if os.path.exists(app_path):
        app = read_json(app_path)
        verify_seal(app, "app identity")
        local_hint = app.get("project_home_hint")
        if local_hint:
            expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(local_hint)))
            if os.path.exists(os.path.join(expanded, "project.json")):
                return expanded
    raise NotFoundError(
        "AMPLAI_PROJECT_HOME is not set and no usable project_home_hint exists"
    )


def load_app_identity(repo_root=None):
    repo_root = os.path.abspath(repo_root or os.getcwd())
    path = os.path.join(repo_root, ".ai-team", "app.json")
    if not os.path.isfile(path):
        raise NotFoundError("missing app identity: %s" % path)
    value = read_json(path)
    verify_seal(value, "app identity")
    return value
