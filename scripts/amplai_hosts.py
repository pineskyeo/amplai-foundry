#!/usr/bin/env python3
"""Host adapters for AMPLAI Loop Kit.

The Loop Runtime owns one Work protocol. Claude Code, Codex, and generic
commands are transport adapters only.  Keeping host-specific argv/session
rules here prevents the supervisor and hooks from growing provider branches.

This file intentionally remains Python 3.6 compatible because the Loop Kit is
installed into older application repositories.
"""
from __future__ import print_function

import io
import json
import os


class HostAdapterError(ValueError):
    pass


def _read_json_events(path):
    if not path or not os.path.exists(path):
        return []
    with io.open(path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read().strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except ValueError:
        events = []
        for line in text.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events


def _recursive_session_id(value):
    if isinstance(value, dict):
        for key in ("session_id", "sessionId", "thread_id", "threadId"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
        for child in value.values():
            found = _recursive_session_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _recursive_session_id(child)
            if found:
                return found
    return None


class HostAdapter(object):
    host_type = "base"
    hook_name = "generic"
    default_command = None
    public_work_entry = "work"

    def __init__(self, runner=None):
        self.runner = dict(runner or {})

    def command(self):
        value = self.runner.get("command") or self.default_command
        if not value:
            raise HostAdapterError("runner command is required for %s" % self.host_type)
        return value

    def args(self):
        return list(self.runner.get("args") or [])

    def build_command(self, prompt, session_id=None, mapping=None):
        raise NotImplementedError

    def redact_command(self, command):
        return list(command)

    def continuation_id_from_output(self, path):
        for event in _read_json_events(path):
            found = _recursive_session_id(event)
            if found:
                return found
        return None

    def hook_output(self, event_name, context, title=None):
        return {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": context,
            }
        }

    def session_id_from_hook_payload(self, payload):
        return _recursive_session_id(payload)

    def append_environment(self, name, value, environ):
        del name, value, environ


class ClaudeCodeHostAdapter(HostAdapter):
    host_type = "claude-code"
    hook_name = "claude"
    default_command = "claude"
    public_work_entry = "/work"

    def build_command(self, prompt, session_id=None, mapping=None):
        del mapping
        result = [self.command()] + self.args() + ["--output-format", "json"]
        if session_id:
            result += ["--resume", session_id]
        result += ["-p", prompt]
        return result

    def redact_command(self, command):
        if len(command) >= 2 and command[-2] == "-p":
            return command[:-1] + ["<prompt>"]
        return list(command)

    def hook_output(self, event_name, context, title=None):
        result = super(ClaudeCodeHostAdapter, self).hook_output(event_name, context, title)
        if title:
            result["hookSpecificOutput"]["sessionTitle"] = title
        return result

    def append_environment(self, name, value, environ):
        import shlex

        path = environ.get("CLAUDE_ENV_FILE")
        if not path or value is None:
            return
        try:
            with io.open(path, "a", encoding="utf-8") as handle:
                handle.write("export %s=%s\n" % (name, shlex.quote(str(value))))
        except Exception:
            # Hooks are best-effort context helpers.  Environment injection must
            # never stop a Claude session.
            pass


class CodexHostAdapter(HostAdapter):
    host_type = "codex"
    hook_name = "codex"
    default_command = "codex"
    public_work_entry = "$work"

    def build_command(self, prompt, session_id=None, mapping=None):
        del mapping
        args = self.args()
        result = [self.command(), "exec"]
        if "--json" not in args and "--experimental-json" not in args:
            result.append("--json")
        result += args
        if session_id:
            result += ["resume", session_id]
        result.append(prompt)
        return result

    def redact_command(self, command):
        return command[:-1] + ["<prompt>"] if command else []

    def continuation_id_from_output(self, path):
        events = _read_json_events(path)
        for event in events:
            if isinstance(event, dict) and event.get("type") == "thread.started":
                value = event.get("thread_id") or event.get("threadId")
                if isinstance(value, str) and value:
                    return value
        for event in events:
            found = _recursive_session_id(event)
            if found:
                return found
        return None


class CommandHostAdapter(HostAdapter):
    host_type = "command"
    hook_name = "command"
    public_work_entry = "work"

    def build_command(self, prompt, session_id=None, mapping=None):
        del prompt, session_id
        mapping = mapping or {}
        return [self.command()] + [str(item).format(**mapping) for item in self.args()]


HOST_ADAPTERS = {
    "claude-code": ClaudeCodeHostAdapter,
    "claude": ClaudeCodeHostAdapter,
    "codex": CodexHostAdapter,
    "command": CommandHostAdapter,
}


def get_host_adapter(runner=None, host_type=None):
    runner = dict(runner or {})
    selected = host_type or runner.get("type") or "claude-code"
    adapter = HOST_ADAPTERS.get(selected)
    if adapter is None:
        raise HostAdapterError("unsupported runner type: %s" % selected)
    return adapter(runner)


def detect_hook_adapter(payload=None, requested="auto", environ=None):
    payload = payload or {}
    environ = environ or os.environ
    if requested in ("claude", "claude-code"):
        return ClaudeCodeHostAdapter()
    if requested == "codex":
        return CodexHostAdapter()
    if environ.get("CLAUDE_PROJECT_DIR") or environ.get("CLAUDE_ENV_FILE"):
        return ClaudeCodeHostAdapter()
    # Codex project hooks expose cwd/session metadata but no Claude-specific
    # environment.  An explicit --host is still preferred for deterministic CI.
    return CodexHostAdapter()


def continuation_id_from_output(path, host_type=None):
    if host_type:
        return get_host_adapter(host_type=host_type).continuation_id_from_output(path)
    # Compatibility helper for callers created before HostAdapter existed.
    codex = CodexHostAdapter().continuation_id_from_output(path)
    if codex:
        return codex
    return ClaudeCodeHostAdapter().continuation_id_from_output(path)
