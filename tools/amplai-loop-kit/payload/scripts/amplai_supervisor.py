#!/usr/bin/env python3
"""Deterministic local supervisor for AMPLAI async cross-app Work.

The supervisor watches the central Project Store, reconciles dependencies,
claims READY work atomically, launches the target app's configured worker, and
maintains a host-local lease.  It never decides product or architecture.
"""
from __future__ import print_function

import argparse
import concurrent.futures
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from amplai_runtime import (  # noqa: E402
    ACTIVE_WORK_STATUSES, AmplaiError, ConflictError, LockError, NotFoundError,
    ProjectStore, ValidationError, discover_project_home, ensure_dir, utc_now,
)
from amplai_hosts import continuation_id_from_output, get_host_adapter  # noqa: E402

# A refused start is not the same failure as a broken Store, so callers can tell
# "someone else is already running" apart from "this run went wrong".
EXIT_ERROR = 2
EXIT_ALREADY_RUNNING = 3


def session_id_from_output(path):
    """Compatibility wrapper retained for external tests/tools."""
    return continuation_id_from_output(path)


class WorkerRunner(object):
    def __init__(self, store, work, token, worker_id):
        self.store = store
        self.work = work
        self.token = token
        self.worker_id = worker_id
        self.app_id = work["target_app"]
        self.local = store.get_local_app(self.app_id)
        self.repo_path = self.local["repo_path"]
        self.runner = self._runner_for_work()
        self.host = get_host_adapter(self.runner)
        self.policy = store.policy.get("supervisor") or {}
        self.workspace_path = self.repo_path

    def _runner_for_work(self):
        profile = self.work.get("runner_profile") or self.local.get("default_runner_profile")
        profiles = self.local.get("runner_profiles") or {}
        if profile and profile in profiles:
            runner = profiles[profile]
        else:
            runner = self.local["runner"]
        if profile and profile not in profiles and runner.get("type") != profile:
            raise ValidationError("runner profile is not configured: %s" % profile)
        bypass = self.store._unattended_bypass_flags(runner.get("args") or [])
        if bypass:
            raise ValidationError("runner has unattended permission bypass: %s" % ", ".join(bypass))
        return runner

    def _prepare_workspace(self):
        base_ref = self.work.get("base_ref")
        if not base_ref:
            # Pre-orchestration Work is retained for CLI compatibility.  A
            # request-correlated orchestration Work is never allowed through
            # this path: its creation contract requires an immutable ref.
            if self.work.get("request_ref"):
                raise ValidationError("managed workspace requires immutable base_ref")
            return self.repo_path
        path = os.path.join(
            self.store.home, ".amplai", "local", "workspaces", self.work["work_id"],
            "workspace",
        )
        root = os.path.dirname(path)
        marker_path = os.path.join(root, ".amplai-workspace.json")
        expected_marker = {
            "work_id": self.work["work_id"],
            "base_ref": base_ref,
            "repo_path": os.path.realpath(self.repo_path),
        }
        if os.path.islink(root) or os.path.islink(path):
            raise ValidationError("managed workspace path must not be a symlink")
        if os.path.isdir(path):
            try:
                with io.open(marker_path, encoding="utf-8") as handle:
                    marker = json.load(handle)
            except (IOError, OSError, ValueError):
                raise ValidationError("managed workspace ownership marker is missing")
            if marker != expected_marker:
                raise ValidationError("managed workspace ownership mismatch")
            try:
                dirty = subprocess.check_output(
                    ["git", "-C", path, "status", "--porcelain"], stderr=subprocess.STDOUT
                ).decode("utf-8").strip()
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ValidationError("managed workspace inspection failed: %s" % exc)
            if dirty:
                raise ValidationError("managed workspace is dirty")
            command = ["git", "-C", path, "rev-parse", "HEAD"]
        else:
            ensure_dir(root)
            command = ["git", "-C", self.repo_path, "worktree", "add", "--detach", path, base_ref]
        try:
            subprocess.check_output(command, stderr=subprocess.STDOUT)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValidationError("managed workspace preparation failed: %s" % exc)
        if os.path.isdir(path):
            actual = subprocess.check_output(
                ["git", "-C", path, "rev-parse", "HEAD"], stderr=subprocess.STDOUT
            ).decode("utf-8").strip()
            if actual != base_ref:
                raise ValidationError("managed workspace base_ref mismatch")
        if not os.path.exists(marker_path):
            with io.open(marker_path, "w", encoding="utf-8") as handle:
                json.dump(expected_marker, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            os.chmod(marker_path, 0o600)
        os.chmod(path, 0o700)
        return path

    def _prompt(self):
        entry_point = (
            self.host.public_design_entry
            if self.work.get("controller") == "design"
            else self.host.public_work_entry
        )
        return "\n".join([
            "AMPLAI durable Work assignment",
            "",
            "Project Store: %s" % self.store.home,
            "App: %s" % self.app_id,
            "Work ID: %s" % self.work["work_id"],
            "Change ID: %s" % self.work["change_id"],
            "",
            "Use the repository's %s entry point. The Project Store is the source of truth." % entry_point,
            "First inspect the Work Context with:",
            "  python3 scripts/amplai.py work context --id \"$AMPLAI_WORK_ID\"",
            "",
            "Resolve engineering questions with evidence under the Decision policy. Create cross-app Work instead of prose-only handoffs.",
            "Before this worker exits, durably transition the same Work to DONE, WAITING, BLOCKED, HUMAN_REQUIRED, or FAILED.",
            "AMPLAI_LEASE_TOKEN is already in this environment and scripts/amplai.py",
            "reads it automatically. Never pass it on a command line or print it.",
        ])

    def _command(self):
        session_id = self.store.session_id_for_work(self.app_id, self.work["work_id"])
        mapping = {
            "work_id": self.work["work_id"],
            "change_id": self.work["change_id"],
            "app_id": self.app_id,
            "project_home": self.store.home,
            "repo_path": self.workspace_path,
        }
        try:
            return self.host.build_command(self._prompt(), session_id, mapping)
        except ValueError as exc:
            raise ValidationError(str(exc))

    def _logged_command(self, command):
        return self.host.redact_command(command)

    def _run_dir(self):
        stamp = utc_now().replace(":", "").replace("-", "")
        path = os.path.join(
            self.store.home, ".amplai", "local", "runs",
            self.work["work_id"], "%s-attempt-%d" % (stamp, self.work["attempts"]),
        )
        ensure_dir(path)
        os.chmod(path, 0o700)
        return path

    def run(self):
        work_id = self.work["work_id"]
        try:
            self.workspace_path = self._prepare_workspace()
        except ValidationError as exc:
            self.store.fail_work(
                work_id, self.token, str(exc), retryable=False, actor=self.worker_id,
            )
            return {"work_id": work_id, "returncode": None, "error": str(exc)}
        self.store.start_work(work_id, self.token, actor=self.worker_id)
        run_dir = self._run_dir()
        stdout_path = os.path.join(run_dir, "stdout.log")
        stderr_path = os.path.join(run_dir, "stderr.log")
        command_path = os.path.join(run_dir, "command.json")
        command = self._command()
        logged_command = self._logged_command(command)
        with io.open(command_path, "w", encoding="utf-8") as handle:
            json.dump({
                "command": logged_command,
                "cwd": self.workspace_path,
                "work_id": work_id,
                "started_at": utc_now(),
            }, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(command_path, 0o600)
        env = os.environ.copy()
        env.update({
            "AMPLAI_PROJECT_HOME": self.store.home,
            "AMPLAI_APP_ID": self.app_id,
            "AMPLAI_WORK_ID": work_id,
            "AMPLAI_CHANGE_ID": self.work["change_id"],
            "AMPLAI_LEASE_TOKEN": self.token,
            "AMPLAI_SUPERVISED": "1",
        })
        timeout = int(self.runner.get("timeout_seconds") or self.policy.get("worker_timeout_seconds", 7200))
        heartbeat_seconds = max(5, int(self.policy.get("heartbeat_seconds", 30)))
        started = time.time()
        timed_out = False
        with io.open(stdout_path, "w", encoding="utf-8") as stdout_handle, \
                io.open(stderr_path, "w", encoding="utf-8") as stderr_handle:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.workspace_path,
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    universal_newlines=True,
                )
            except OSError as exc:
                self.store.fail_work(
                    work_id, self.token, "worker launch failed: %s" % exc,
                    retryable=False, actor=self.worker_id,
                )
                return {"work_id": work_id, "returncode": None, "error": str(exc), "run_dir": run_dir}
            next_heartbeat = time.time() + heartbeat_seconds
            while process.poll() is None:
                now = time.time()
                if now - started > timeout:
                    timed_out = True
                    try:
                        process.send_signal(signal.SIGINT)
                        process.wait(timeout=10)
                    except Exception:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except Exception:
                            process.kill()
                    break
                if now >= next_heartbeat:
                    try:
                        current = self.store.get_work(work_id)
                        if current.get("status") in ACTIVE_WORK_STATUSES:
                            self.store.heartbeat(work_id, self.token, actor=self.worker_id)
                    except (ConflictError, NotFoundError):
                        pass
                    next_heartbeat = now + heartbeat_seconds
                time.sleep(0.5)
            returncode = process.poll()
            if returncode is None:
                returncode = process.wait()
        session_id = self.host.continuation_id_from_output(stdout_path)
        if session_id:
            self.store.update_session(
                self.app_id, work_id=work_id, session_id=session_id,
                event="worker_result", data={"returncode": returncode},
            )
        current = self.store.get_work(work_id)
        if current.get("status") in ACTIVE_WORK_STATUSES:
            if timed_out:
                error = "worker timed out after %d seconds" % timeout
            elif returncode == 0:
                error = "worker exited without a durable Work transition"
            else:
                error = "worker exited with return code %s" % returncode
            retryable = bool(self.policy.get("auto_retry_worker_failures", True))
            try:
                current = self.store.fail_work(
                    work_id, self.token, error,
                    retryable=retryable, actor=self.worker_id,
                )
            except ConflictError:
                current = self.store.get_work(work_id)
        return {
            "work_id": work_id,
            "app_id": self.app_id,
            "returncode": returncode,
            "timed_out": timed_out,
            "session_id": session_id,
            "final_status": current.get("status"),
            "run_dir": run_dir,
        }


class Supervisor(object):
    def __init__(self, store, include_manual=False, max_workers=None, heartbeat=None):
        self.store = store
        self.include_manual = include_manual
        self.max_workers = int(max_workers or max(1, len(store.list_apps())))
        self.worker_prefix = "%s:%s" % (socket.gethostname(), os.getpid())
        # Called once per scan so a long-lived run keeps its Store lock alive.
        # It returns False once the lock has been reclaimed by someone else.
        self.heartbeat = heartbeat or (lambda: True)

    def launchable(self):
        self.store.reconcile()
        result = []
        active_by_app = {}
        for work in self.store.list_work(statuses=ACTIVE_WORK_STATUSES):
            active_by_app[work["target_app"]] = active_by_app.get(work["target_app"], 0) + 1
        for app in self.store.list_apps():
            app_id = app["app_id"]
            local = self.store.get_local_app(app_id, required=False)
            if not local:
                continue
            if not local.get("auto_start", False) and not self.include_manual:
                continue
            capacity = int(app.get("max_concurrency", 1)) - active_by_app.get(app_id, 0)
            if capacity <= 0:
                continue
            ready = []
            for work in self.store.list_work(target_app=app_id, statuses=["READY"]):
                if not self.store.retry_ready(work):
                    continue
                try:
                    WorkerRunner(self.store, work, None, "launch-check")
                except ValidationError:
                    continue
                ready.append(work)
            result.extend(ready[:capacity])
        result.sort(key=lambda item: (-int(item.get("priority", 0)), item.get("created_at", "")))
        return result

    def dry_run(self):
        return [{
            "work_id": work["work_id"],
            "change_id": work["change_id"],
            "target_app": work["target_app"],
            "priority": work["priority"],
            "goal": work["goal"],
        } for work in self.launchable()]

    def deferred(self):
        """READY Work held back by its retry backoff."""
        return [{
            "work_id": work["work_id"],
            "target_app": work["target_app"],
            "attempts": work.get("attempts", 0),
            "retry_not_before": work.get("retry_not_before"),
            "last_error": work.get("last_error"),
        } for work in self.store.list_work(statuses=["READY"])
            if not self.store.retry_ready(work)]

    def _claim(self, work):
        worker_id = "%s:%s" % (self.worker_prefix, work["target_app"])
        # Revalidate at the mutation boundary: a resealed local binding can
        # change after launchable() scanned it, but must never be claimed.
        WorkerRunner(self.store, work, None, worker_id)
        claimed, token = self.store.claim_work(
            work["work_id"], worker_id, actor="local-supervisor",
        )
        return WorkerRunner(self.store, claimed, token, worker_id)

    def run_until_quiescent(self, persistent=False):
        poll_seconds = max(1, int((self.store.policy.get("supervisor") or {}).get("poll_seconds", 5)))
        results = []
        futures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            while True:
                # Losing the lock means another supervisor now owns this Store.
                # Continuing would put two of them on the same work, which is
                # exactly what the lock exists to prevent, so stop instead.
                if self.heartbeat() is False:
                    raise LockError(
                        "lost the supervisor lock to another holder; stopping"
                    )
                for work in self.launchable():
                    if len(futures) >= self.max_workers:
                        break
                    if any(meta["work_id"] == work["work_id"] for meta in futures.values()):
                        continue
                    try:
                        runner = self._claim(work)
                    except ConflictError:
                        continue
                    future = pool.submit(runner.run)
                    futures[future] = {"work_id": work["work_id"], "app_id": work["target_app"]}
                if not futures:
                    # `--once` stops as soon as a full pass launches nothing.
                    # Work still inside its retry backoff is reported by
                    # `--dry-run` instead of being waited for here.
                    if not persistent:
                        break
                    time.sleep(poll_seconds)
                    continue
                # Block until a worker finishes or the poll interval elapses.
                # Re-scanning the store on a 0.25s spin reconciled every object
                # several times a second and starved the workers' own lock
                # acquisitions.
                done, _pending = concurrent.futures.wait(
                    list(futures), timeout=poll_seconds,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append({"error": str(exc), "type": exc.__class__.__name__})
                    del futures[future]
        return results


def build_parser():
    parser = argparse.ArgumentParser(description="AMPLAI deterministic Local Supervisor")
    parser.add_argument("--project-home")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Run until no auto-launchable Work remains")
    mode.add_argument("--run", action="store_true", help="Keep watching the Project Store")
    mode.add_argument("--dry-run", action="store_true", help="Show launch plan without claiming Work")
    parser.add_argument("--include-manual", action="store_true", help="Include apps with auto_start=false")
    parser.add_argument("--max-workers", type=int)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        home = discover_project_home(os.getcwd(), args.project_home)
        store = ProjectStore(home)

        if args.dry_run:
            # A plan costs nothing and claims nothing, so it must not be able to
            # lock out the supervisor that is actually running.
            supervisor = Supervisor(
                store, include_manual=args.include_manual, max_workers=args.max_workers,
            )
            value = {
                "ok": True,
                "launchable": supervisor.dry_run(),
                "deferred_by_backoff": supervisor.deferred(),
            }
            print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        # Only one supervisor may run against a Store.  The lock lives in the
        # Store, not in any application repository, so it holds no matter which
        # copy of this script was started.
        lock = store.supervisor_lock()
        try:
            lock.acquire()
        except LockError:
            owner = lock.describe_owner()
            print(json.dumps({
                "ok": False,
                "error": "another supervisor already holds this Project Store",
                "lock": lock.lock_dir,
                "held_by": owner,
            }, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
            return EXIT_ALREADY_RUNNING
        try:
            supervisor = Supervisor(
                store, include_manual=args.include_manual, max_workers=args.max_workers,
                heartbeat=lock.heartbeat,
            )
            value = {
                "ok": True,
                "mode": "run" if args.run else "once",
                "results": supervisor.run_until_quiescent(persistent=args.run),
                "status": store.status_summary(),
            }
        finally:
            try:
                lock.release()
            except LockError as exc:
                print("WARNING: %s" % exc, file=sys.stderr)
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (AmplaiError, ValidationError, OSError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
