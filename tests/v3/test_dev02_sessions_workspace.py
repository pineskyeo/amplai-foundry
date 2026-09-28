"""Native state is version-bound. Portable workspaces contain checked file bytes."""

import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from amplai_foundry.agent_drivers.protocol import EventNormalizer, JsonlDecoder, SessionJournal
from amplai_foundry.agent_drivers.sessions import SessionStore
from amplai_foundry.runtime.contracts.registry import strict_json_loads
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution.envelope import execution_envelope
from amplai_foundry.runtime.storage.store import Scope
from amplai_foundry.sandbox.local import DataSandbox
from amplai_foundry.sandbox.workspace import WorkspaceManager


def _append_process(root, prefix):
    journal = SessionJournal(Path(root))
    for n in range(12):
        journal.append("dispatch-one", f"{prefix}-{n}", {"worker": prefix, "n": n})


def test_dev02_journal_survives_fresh_process_and_concurrent_writers(tmp_path):
    root = tmp_path / "sessions"
    j = SessionJournal(root)
    j.create("dispatch-one", {"goal": "one"})
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_append_process, args=(str(root), f"p-{n}")) for n in range(3)]
    for p in processes:
        p.start()
    for p in processes:
        p.join(10)
        assert p.exitcode == 0
    events = SessionJournal(root).events_after("dispatch-one", 0)
    assert len(events) == 36 and [e["cursor"] for e in events] == list(range(1, 37))
    assert len({e["event_id"] for e in events}) == 36
    assert (root / "dispatch-one.json").stat().st_mode & 0o077 == 0


def test_dev02_journal_dispatch_compare_and_swap_only_one_start(tmp_path):
    root = tmp_path / "s"
    SessionJournal(root).create("dispatch-one", {"a": 1})

    def change(_):
        try:
            return SessionJournal(root).transition("dispatch-one", {"prepared"}, "starting")[
                "state"
            ]
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(change, range(8)))
    assert results.count("starting") == 1 and results.count("conflict") == 7
    with pytest.raises(Conflict):
        SessionJournal(root).create("dispatch-one", {"a": 2})


@pytest.mark.parametrize("identifier", ["../bad", "/tmp/a", "", "a/b", "a\\b", "x\x00y"])
def test_dev02_journal_rejects_path_ids(tmp_path, identifier):
    with pytest.raises(RuntimeFault):
        SessionJournal(tmp_path / "s").create(identifier, {})


def test_dev02_journal_dedupe_and_cursor_boundaries(tmp_path):
    j = SessionJournal(tmp_path / "s")
    j.create("dispatch-one", {})
    assert j.append("dispatch-one", "event-one", {"ok": True}) == 1
    assert j.append("dispatch-one", "event-one", {"ok": True}) == 1
    with pytest.raises(Conflict):
        j.append("dispatch-one", "event-one", {"ok": False})
    assert not j.events_after("dispatch-one", 1)
    for cursor in [-1, 2, True, "1"]:
        with pytest.raises(Hold):
            j.events_after("dispatch-one", cursor)


@pytest.mark.parametrize(
    "raw", [b'{"a":1,"a":2}', b'{"n":NaN}', b'{"n":Infinity}', b'{"n":-Infinity}', b'{"n":1e9999}']
)
def test_dev02_jsonl_rejects_ambiguous_json(raw):
    with pytest.raises(Hold):
        JsonlDecoder().feed(raw + b"\n")
    with pytest.raises(RuntimeFault):
        strict_json_loads(raw)


def test_dev02_jsonl_partial_utf8_and_trailing_record():
    raw = json.dumps({"type": "item.completed", "text": "한글"}, ensure_ascii=False).encode()
    parser = JsonlDecoder()
    out = []
    for n in range(0, len(raw), 2):
        out.extend(parser.feed(raw[n : n + 2]))
    assert out == [] and parser.feed(b"", final=True)[0]["text"] == "한글"
    with pytest.raises(Hold):
        JsonlDecoder().feed(b'{"a":', final=True)
    with pytest.raises(Hold):
        JsonlDecoder(max_line_bytes=3).feed(b"1234")
    with pytest.raises(Hold):
        JsonlDecoder(max_total_bytes=3).feed(b"1234")


@pytest.mark.parametrize(
    "provider,event", [("codex", {"type": "new.unknown"}), ("claude", {"type": "unknown"})]
)
def test_dev02_unknown_native_events_hold(provider, event):
    with pytest.raises(Hold):
        EventNormalizer(provider).accept(event)


@pytest.mark.parametrize("session", ["latest", "continue", "--continue", "", 4, True])
def test_dev02_native_sessions_are_not_implicit(session):
    with pytest.raises(Hold):
        EventNormalizer("codex").accept({"type": "thread.started", "thread_id": session})


def test_dev02_provider_changed_session_or_unknown_usage_is_not_success():
    p = EventNormalizer("codex", expected_session="exact-a")
    with pytest.raises(Hold):
        p.accept({"type": "thread.started", "thread_id": "exact-b"})
    result = p.accept(
        {"type": "turn.completed", "usage": {"input_tokens": True, "output_tokens": 1}}
    )
    assert result["usage"]["status"] == "unknown" and result["usage"]["cost_microunits"] is None
    assert "reasoning" not in result


def prepared_session(d):
    p = d.prepare()
    x = d.runtime.claim(d.worker)
    e = execution_envelope(d.runtime, d.worker, x)
    ss = SessionStore(d.store, d.contracts)
    snap = "sha256:" + "1" * 64
    h = ss.prepare(d.worker, x["dispatch_id"], e, x["profile"], workspace_digest=snap)
    h = ss.bind(d.worker, x["dispatch_id"], "native-exact", expected_version=h["row_version"])
    cp = ss.checkpoint(
        d.worker,
        x["dispatch_id"],
        expected_version=h["row_version"],
        process_stopped=True,
        snapshot_digest="sha256:" + "2" * 64,
        pending_effects=[],
        driver_receipt={"session_handle": "native-exact"},
    )
    return p, x, e, ss, cp


def test_dev02_server_session_reopen_and_resume_exact_binding(deployment):
    d = deployment
    _p, x, e, _ss, cp = prepared_session(d)
    other = SessionStore(d.store, d.contracts)
    result = other.resume_check(
        d.worker,
        x["dispatch_id"],
        e,
        x["profile"],
        workspace_digest=cp["workspace_digest"],
        pending_effects=[],
    )
    assert result["status"] == "resumable" and result["session_handle"] == "native-exact"
    with pytest.raises(Conflict):
        other.bind(d.worker, x["dispatch_id"], "changed-session", expected_version=3)


@pytest.mark.parametrize(
    "binding", ["contract_ref", "graph_ref", "context_bundle_ref", "composition_ref", "sandbox_ref"]
)
def test_dev02_session_revisions_do_not_resume(deployment, binding):
    d = deployment
    _p, x, e, ss, cp = prepared_session(d)
    e = deepcopy(e)
    e[binding]["revision"] += 1
    with pytest.raises(Hold):
        ss.resume_check(
            d.worker,
            x["dispatch_id"],
            e,
            x["profile"],
            workspace_digest=cp["workspace_digest"],
            pending_effects=[],
        )


@pytest.mark.parametrize("binding", ["driver_profile_ref", "model_profile_ref", "environment_ref"])
def test_dev02_changed_driver_profile_requires_new_session(deployment, binding):
    d = deployment
    _p, x, e, ss, cp = prepared_session(d)
    profile = deepcopy(x["profile"])
    profile[binding]["revision"] += 1
    result = ss.resume_check(
        d.worker,
        x["dispatch_id"],
        e,
        profile,
        workspace_digest=cp["workspace_digest"],
        pending_effects=[],
    )
    assert (
        result["status"] == "new_session_required" and result["private_reasoning_portable"] is False
    )


def test_dev02_session_snapshot_effects_owner_scope_fail_closed(deployment):
    d = deployment
    _p, x, e, ss, cp = prepared_session(d)
    with pytest.raises(Hold):
        ss.resume_check(
            d.worker,
            x["dispatch_id"],
            e,
            x["profile"],
            workspace_digest="sha256:" + "3" * 64,
            pending_effects=[],
        )
    with pytest.raises(Hold):
        ss.resume_check(
            d.worker,
            x["dispatch_id"],
            e,
            x["profile"],
            workspace_digest=cp["workspace_digest"],
            pending_effects=["unknown-effect"],
        )
    with pytest.raises(Hold):
        ss.resume_check(
            replace(d.worker, subject_id="other"),
            x["dispatch_id"],
            e,
            x["profile"],
            workspace_digest=cp["workspace_digest"],
            pending_effects=[],
        )
    with pytest.raises(Hold):
        ss.resume_check(
            replace(d.worker, scope=Scope("other", "project")),
            x["dispatch_id"],
            e,
            x["profile"],
            workspace_digest=cp["workspace_digest"],
            pending_effects=[],
        )


def test_dev02_workspace_roundtrip_actual_bytes_modes_and_isolation(deployment, tmp_path):
    d = deployment
    source = tmp_path / "input"
    source.mkdir()
    (source / "hello.py").write_text('print("한글")\n')
    (source / "hello.py").chmod(0o700)
    (source / "sub").mkdir()
    (source / "sub" / "value.bin").write_bytes(b"\x00\xff")
    ws = WorkspaceManager(tmp_path / "workspaces", d.artifacts)
    snapshot = ws.snapshot(d.scope, source)
    a = ws.materialize(d.scope, "run-one", snapshot)
    b = ws.materialize(d.scope, "run-two", snapshot)
    assert (a / "hello.py").read_bytes() == (source / "hello.py").read_bytes()
    assert (a / "hello.py").stat().st_mode & 0o111
    (a / "hello.py").write_text("changed")
    assert (b / "hello.py").read_bytes() == (source / "hello.py").read_bytes()
    with pytest.raises(Hold):
        ws.materialize(d.scope, "run-one", snapshot)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "a//b",
        "./file",
        "a\\b",
        ".env",
        "x/.git/config",
        "credentials.pem",
        "id.key",
        "secret.p12",
    ],
)
def test_dev02_workspace_paths_reject_control_and_secrets(name):
    with pytest.raises(Hold):
        WorkspaceManager.path(name)


def test_dev02_workspace_path_preserves_normal_relative_path():
    assert WorkspaceManager.path("a/b.txt") == "a/b.txt"


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo"])
def test_dev02_workspace_rejects_links_and_devices(deployment, tmp_path, kind):
    root = tmp_path / "input"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("private")
    if kind == "symlink":
        (root / "entry").symlink_to(outside)
    elif kind == "directory_symlink":
        (root / "entry").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside, root / "entry")
    else:
        os.mkfifo(root / "entry")
    ws = WorkspaceManager(tmp_path / "workspaces", deployment.artifacts)
    with pytest.raises((RuntimeFault, OSError)):
        ws.snapshot(deployment.scope, root)


@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
def test_dev02_data_collector_cannot_block_on_special_file(tmp_path, kind):
    box = DataSandbox(tmp_path / "box")
    outside = tmp_path / "outside"
    outside.write_text("secret")
    if kind == "hardlink":
        os.link(outside, box.root / "output")
    else:
        os.mkfifo(box.root / "output")
    with pytest.raises(RuntimeFault):
        box.read("output")
    with pytest.raises(RuntimeFault):
        box.write("output", b"new")


def test_dev02_workspace_quota_and_unstopped_collection(deployment, tmp_path):
    d = deployment
    root = tmp_path / "in"
    root.mkdir()
    (root / "big").write_bytes(b"x" * 20)
    ws = WorkspaceManager(tmp_path / "workspaces", d.artifacts, max_bytes=10, max_files=1)
    with pytest.raises(Hold):
        ws.snapshot(d.scope, root)
    root2 = ws.materialize(d.scope, "run-empty", ws.empty_snapshot(d.scope))
    with pytest.raises(Hold):
        ws.collect(d.scope, root2, {}, {"produces": []}, process_stopped=False)
    node = {"produces": [{"name": "result", "required": True, "media_type": "text/plain"}]}
    with pytest.raises(Hold):
        ws.collect(d.scope, root2, {}, node, process_stopped=True)
    with pytest.raises(Hold):
        ws.collect(d.scope, root2, {"unknown": "a"}, node, process_stopped=True)


def test_dev02_resume_workspace_compares_bytes_not_new_artifact_ids(deployment, tmp_path):
    d = deployment
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("original")
    ws = WorkspaceManager(tmp_path / "managed", d.artifacts)
    snap = ws.snapshot(d.scope, source)
    target = ws.materialize(d.scope, "run-one", snap)
    assert ws.assert_matches(d.scope, target, snap)
    (target / "code.py").write_text("tampered")
    with pytest.raises(Hold):
        ws.assert_matches(d.scope, target, snap)
    (target / "code.py").write_text("original")
    (target / "extra").write_text("new")
    with pytest.raises(Hold):
        ws.assert_matches(d.scope, target, snap)
