import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from amplai_hosts import (  # noqa: E402
    ClaudeCodeHostAdapter,
    CodexHostAdapter,
    CommandHostAdapter,
    continuation_id_from_output,
    detect_hook_adapter,
    get_host_adapter,
)


class HostAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="amplai-host-")

    def tearDown(self):
        shutil.rmtree(self.temp)

    def _output(self, name, text):
        path = os.path.join(self.temp, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_claude_command_and_resume(self):
        host = ClaudeCodeHostAdapter({"args": ["--model", "sonnet"]})
        command = host.build_command("PROMPT", "session-1")
        self.assertEqual(command[:3], ["claude", "--model", "sonnet"])
        self.assertIn("--output-format", command)
        self.assertEqual(command[-4:], ["--resume", "session-1", "-p", "PROMPT"])
        self.assertEqual(host.redact_command(command)[-1], "<prompt>")
        self.assertEqual(host.public_work_entry, "/work")

    def test_codex_command_and_resume(self):
        host = CodexHostAdapter({"args": ["--sandbox", "workspace-write"]})
        command = host.build_command("PROMPT", "thread-1")
        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        self.assertEqual(command[-3:], ["resume", "thread-1", "PROMPT"])
        self.assertEqual(host.redact_command(command)[-1], "<prompt>")
        self.assertEqual(host.public_work_entry, "$work")

    def test_codex_thread_started_wins(self):
        path = self._output(
            "codex.jsonl",
            json.dumps({"type": "thread.started", "thread_id": "thread-42"}) + "\n"
            + json.dumps({"session_id": "fallback"}) + "\n",
        )
        self.assertEqual(CodexHostAdapter().continuation_id_from_output(path), "thread-42")
        self.assertEqual(continuation_id_from_output(path), "thread-42")

    def test_claude_session_id_from_json(self):
        path = self._output("claude.json", json.dumps({"result": {"session_id": "s-9"}}))
        self.assertEqual(ClaudeCodeHostAdapter().continuation_id_from_output(path), "s-9")

    def test_command_adapter_formats_only_declared_mapping(self):
        host = CommandHostAdapter({"command": "runner", "args": ["--work={work_id}"]})
        self.assertEqual(host.build_command("ignored", mapping={"work_id": "W1"}), ["runner", "--work=W1"])

    def test_registry_and_hook_detection(self):
        self.assertIsInstance(get_host_adapter({"type": "codex"}), CodexHostAdapter)
        self.assertIsInstance(
            detect_hook_adapter({}, "auto", {"CLAUDE_PROJECT_DIR": "/repo"}),
            ClaudeCodeHostAdapter,
        )
        self.assertIsInstance(detect_hook_adapter({}, "auto", {}), CodexHostAdapter)

    def test_hook_output_shape_is_host_specific_only_in_adapter(self):
        claude = ClaudeCodeHostAdapter().hook_output("SessionStart", "ctx", title="p:a")
        codex = CodexHostAdapter().hook_output("SessionStart", "ctx", title="p:a")
        self.assertEqual(claude["hookSpecificOutput"]["sessionTitle"], "p:a")
        self.assertNotIn("sessionTitle", codex["hookSpecificOutput"])


if __name__ == "__main__":
    unittest.main()
