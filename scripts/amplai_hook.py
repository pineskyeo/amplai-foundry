#!/usr/bin/env python3
"""Claude Code hook adapter for the AMPLAI Project Store.

SessionStart injects compact, factual work state. SessionEnd only records a
best-effort local checkpoint; durable Work transitions must happen inside
/work before the session ends.
"""
from __future__ import print_function

import argparse
import io
import json
import os
import shlex
import sys

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from amplai_runtime import (  # noqa: E402
    ACTIVE_WORK_STATUSES, AmplaiError, NotFoundError, ProjectStore,
    discover_project_home, load_app_identity,
)


def stdin_json():
    try:
        text = sys.stdin.read()
        return json.loads(text) if text.strip() else {}
    except Exception:
        return {}


def append_env(name, value):
    path = os.environ.get("CLAUDE_ENV_FILE")
    if not path or value is None:
        return
    try:
        with io.open(path, "a", encoding="utf-8") as handle:
            handle.write("export %s=%s\n" % (name, shlex.quote(str(value))))
    except Exception:
        pass


def repo_root(payload):
    return os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()


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


def session_start(payload):
    opened = open_store(payload)
    if opened is None:
        return None
    store, app_id = opened
    active = store.list_work(target_app=app_id, statuses=ACTIVE_WORK_STATUSES)
    ready = store.next_ready(app_id)
    append_env("AMPLAI_PROJECT_HOME", store.home)
    append_env("AMPLAI_APP_ID", app_id)
    if active:
        append_env("AMPLAI_WORK_ID", active[0]["work_id"])
        append_env("AMPLAI_CHANGE_ID", active[0]["change_id"])
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
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
            "sessionTitle": title,
        }
    }


def session_end(payload):
    opened = open_store(payload)
    if opened is None:
        return None
    store, app_id = opened
    work_id = os.environ.get("AMPLAI_WORK_ID")
    session_id = payload.get("session_id")
    store.update_session(
        app_id,
        work_id=work_id,
        session_id=session_id if work_id else None,
        event="SessionEnd",
        data={
            "reason": payload.get("reason"),
            "cwd": payload.get("cwd"),
            "transcript_path": payload.get("transcript_path"),
        },
    )
    return None


def build_parser():
    parser = argparse.ArgumentParser(description="AMPLAI Claude Code hook adapter")
    parser.add_argument("event", choices=["session-start", "session-end"])
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    payload = stdin_json()
    try:
        value = session_start(payload) if args.event == "session-start" else session_end(payload)
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
