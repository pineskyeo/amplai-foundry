#!/usr/bin/env python3
"""Claude Code and Codex hook adapter for the AMPLAI Project Store.

SessionStart injects compact, factual work state. SessionEnd only records a
best-effort local checkpoint; durable Work transitions must happen inside
/work (Claude Code) or $work (Codex) before the session ends.
"""
from __future__ import print_function

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from amplai_runtime import (  # noqa: E402
    ACTIVE_WORK_STATUSES, AmplaiError, NotFoundError, ProjectStore,
    discover_project_home, load_app_identity,
)
from amplai_hosts import detect_hook_adapter  # noqa: E402


def stdin_json():
    try:
        text = sys.stdin.read()
        return json.loads(text) if text.strip() else {}
    except Exception:
        return {}


def detect_host(payload, requested="auto"):
    """Compatibility helper; new code uses HostAdapter directly."""
    return detect_hook_adapter(payload, requested).hook_name


def append_env(name, value, host="auto"):
    adapter = detect_hook_adapter({}, host)
    adapter.append_environment(name, value, os.environ)


def repo_root(payload):
    return (
        os.environ.get("AMPLAI_REPO_ROOT")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or payload.get("cwd")
        or os.getcwd()
    )


def open_store(payload):
    """(store, app_id) for a configured project, or None when idle.

    A repository can carry the kit without ever joining a Project Store.  That
    is the normal resting state, not a failure, so it must stay silent: a hook
    that writes to stderr on every session start reads as a broken hook.
    """
    repo = repo_root(payload)
    if not os.path.isfile(os.path.join(repo, ".ai-team", "app.json")):
        return None
    try:
        home = discover_project_home(repo)
    except NotFoundError:
        return None
    identity = load_app_identity(repo)
    return ProjectStore(home), identity["app_id"]


def session_start(payload, host="auto"):
    adapter = detect_hook_adapter(payload, host)
    host = adapter.hook_name
    opened = open_store(payload)
    if opened is None:
        return None
    store, app_id = opened
    active = store.list_work(target_app=app_id, statuses=ACTIVE_WORK_STATUSES)
    ready = store.next_ready(app_id)
    adapter.append_environment("AMPLAI_PROJECT_HOME", store.home, os.environ)
    adapter.append_environment("AMPLAI_APP_ID", app_id, os.environ)
    if active:
        adapter.append_environment("AMPLAI_WORK_ID", active[0]["work_id"], os.environ)
        adapter.append_environment("AMPLAI_CHANGE_ID", active[0]["change_id"], os.environ)
    lines = [
        "AMPLAI Project State",
        "Project: %s" % store.project["project_id"],
        "App: %s" % app_id,
        "Project Store: %s" % store.home,
    ]
    if active:
        for work in active:
            lines.append(
                "Active Work: %s [%s] — %s" %
                (work["work_id"], work["status"], work["goal"])
            )
    elif ready:
        lines.append(
            "Next READY Work: %s — %s" % (ready["work_id"], ready["goal"])
        )
        lines.append("Read it with: python3 scripts/amplai.py work context --id %s" % ready["work_id"])
    else:
        lines.append("No active or READY Work is assigned to this app.")
    lines.append("CR/Work/Decision/Evidence in the Project Store are authoritative; rendered handoff text is only a view.")
    title = "%s:%s" % (store.project["project_id"], app_id)
    return adapter.hook_output(
        "SessionStart", "\n".join(lines), title=title
    )


def session_end(payload, host="auto"):
    adapter = detect_hook_adapter(payload, host)
    host = adapter.hook_name
    opened = open_store(payload)
    if opened is None:
        return None
    store, app_id = opened
    work_id = os.environ.get("AMPLAI_WORK_ID")
    if not work_id:
        active = store.list_work(target_app=app_id, statuses=ACTIVE_WORK_STATUSES)
        if len(active) == 1:
            work_id = active[0]["work_id"]
    session_id = adapter.session_id_from_hook_payload(payload)
    store.update_session(
        app_id,
        work_id=work_id,
        session_id=session_id if work_id else None,
        event="SessionEnd",
        data={
            "reason": payload.get("reason"),
            "cwd": payload.get("cwd"),
            "transcript_path": payload.get("transcript_path"),
            "host": host,
        },
    )
    return None


def build_parser():
    parser = argparse.ArgumentParser(description="AMPLAI Claude Code/Codex hook adapter")
    parser.add_argument("event", choices=["session-start", "session-end"])
    parser.add_argument("--host", choices=["auto", "claude", "codex"], default="auto")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    payload = stdin_json()
    try:
        value = (
            session_start(payload, args.host)
            if args.event == "session-start"
            else session_end(payload, args.host)
        )
        if value is not None:
            print(json.dumps(value, ensure_ascii=False))
        return 0
    except (AmplaiError, OSError, ValueError, KeyError) as exc:
        # Hooks are additive context/checkpoint helpers and must never stop a
        # session.  An unconfigured repository returns early and says nothing;
        # reaching here means the store exists but could not be read, which is
        # worth one line on stderr.
        print("AMPLAI hook skipped: %s" % exc, file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
