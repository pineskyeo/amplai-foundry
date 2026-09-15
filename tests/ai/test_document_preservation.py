"""S06 preservation and recoverable retirement; all mutations use disposable Git."""

from __future__ import annotations

import copy
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ai import test_document_review as review

ROOT = Path(__file__).resolve().parents[2]
runtime = review.runtime


def fixture(tmp_path):
    root = review.fixture(tmp_path)
    policy = ROOT / ".ai-team/policy/gardening.json"
    (root / ".ai-team/policy/gardening.json").write_bytes(policy.read_bytes())
    (root / "docs/legacy.md").write_text(
        "Original context.\n\n# Unique Contract\nNever discard the prior release.\n\n"
        "## Incident Lesson\nRestore the exact version before retry.\n"
    )
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    review.git(root, "add", "--all")
    review.git(root, "commit", "--quiet", "-m", "Preservation fixture")
    return root


def sources(root):
    return [
        {
            "repository_id": "canary",
            "commit": review.git(root, "rev-parse", "HEAD"),
            "path": "docs/legacy.md",
            "doc_id": "canary:legacy",
        }
    ]


def split_source(engine, root):
    original = engine.preservation_ledger(root, sources(root))
    body = (root / "docs/legacy.md").read_bytes().splitlines(keepends=True)
    mappings = []
    for n, section in enumerate(original["sections"]):
        path = f"docs/retained-{n}.md"
        data = b"".join(body[section["line_start"] - 1 : section["line_end"]])
        (root / path).write_bytes(data)
        mappings.append(
            {
                "section_id": section["section_id"],
                "action": "copied",
                "destination": {
                    "path": path,
                    "doc_id": f"canary:retained-{n}",
                    "line_start": 1,
                    "line_end": len(data.splitlines()),
                },
                "reason": "Exact original text retained in the owning document.",
                "reviewer": "fixture-owner",
                "method": "byte comparison",
                "evidence": ["evidence/review.txt"],
            }
        )
    return engine.preservation_ledger(root, sources(root), mappings)


def request(engine, root):
    ledger = split_source(engine, root)
    return {
        "schema_version": "1.0",
        "work_id": "fixture-retirement",
        "targets": ["docs/legacy.md"],
        "preservation": ledger,
        "retention": [
            {
                "path": "docs/legacy.md",
                "hold": False,
                "reason": "Synthetic fixture has no retention obligation.",
                "evidence": ["evidence/review.txt"],
            }
        ],
        "references": [
            {
                "kind": kind,
                "status": "CLEAR",
                "reviewer": "fixture-owner",
                "reason": "All synthetic consumers reviewed in the bounded fixture.",
                "evidence": ["evidence/review.txt"],
            }
            for kind in engine.RETIREMENT_REFERENCE_KINDS
        ],
    }


def authorize(engine, root, plan, backup):
    approved = engine.retirement_authority_request(root, plan, backup, "retire")
    return lambda actual: actual == approved


def verifier(root):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import runpy; assert runpy.run_path('src/api.py')['VERSION'] == 1",
        ],
        cwd=root,
        capture_output=True,
        timeout=10,
    )
    return {
        "exit_code": result.returncode,
        "check_id": "fixture-source-version",
        "evidence_sha256": __import__("hashlib").sha256(result.stdout + result.stderr).hexdigest(),
    }


def test_user_backups_and_generated_cache_remain_report_only(runtime, tmp_path):
    root = fixture(tmp_path)
    for name in ("src/local.bak", "src/local.orig", "src/local.pyc", "docs/incident.bak"):
        (root / name).write_text("USER_OR_GENERATED_BYTES\n")
    report = runtime.garden_apply(str(root), "specs/canary", write=False)
    assert not report["applied"]
    assert report["report_only"] is True
    assert all(
        (root / name).exists() for name in ("src/local.bak", "src/local.orig", "src/local.pyc")
    )
    for item in report["candidates"]:
        assert item["recommended_action"] != "delete"
        if item["type"] in ("temporary_backup", "prunable_worktree"):
            assert item["safety"] == "HUMAN_GATED"


def test_direct_old_safe_helper_cannot_delete_or_prune(runtime, tmp_path, monkeypatch):
    root = fixture(tmp_path)
    target = root / "src/own.bak"
    target.write_text("KEEP\n")
    monkeypatch.setattr(runtime, "git", lambda *args: pytest.fail("No git mutation"))
    candidates = [
        {"safety": "SAFE_AUTO", "type": "temporary_backup", "target": {"path": "src/own.bak"}},
        {
            "safety": "SAFE_AUTO",
            "type": "prunable_worktree",
            "target": {"path": str(tmp_path / "foreign")},
        },
    ]
    assert runtime.apply_safe_candidates(str(root), candidates) == []
    assert target.read_text() == "KEEP\n"


def test_full_scan_requires_explicit_gardening_work(runtime, tmp_path):
    root = fixture(tmp_path)
    with pytest.raises(ValueError, match="EXPLICIT_GARDENING_WORK_REQUIRED"):
        runtime.garden_full(str(root))
    with pytest.raises(ValueError, match="EXPLICIT_GARDENING_WORK_REQUIRED"):
        runtime.garden_full(str(root), feature_raw="specs/canary")
    contract = root / "specs/canary/work-contract.json"
    value = json.loads(contract.read_text())
    value.update(work_type="repository_gardening", risk="high")
    contract.write_text(json.dumps(value))
    result = runtime.garden_full(str(root), feature_raw="specs/canary")
    assert result["report_only"] and not result["applied"]
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        runtime.garden_full(str(root), report_only=False, feature_raw="specs/canary")


def test_incremental_scope_does_not_expand_to_other_work_or_all_repo(runtime, tmp_path):
    root = fixture(tmp_path)
    contract_path = root / "specs/canary/work-contract.json"
    contract = json.loads(contract_path.read_text())
    contract["scope"]["include"] = ["src/**"]
    contract_path.write_text(json.dumps(contract))
    (root / "src/api.py").write_text("VERSION = 2\n")
    (root / "src/cache.pyc").write_bytes(b"cache")
    (root / "docs/unrelated.bak").write_text("private backup")
    report = runtime.garden_incremental(str(root), "specs/canary", write=False)
    paths = {item["target"]["path"] for item in report["candidates"]}
    assert "src/cache.pyc" in paths
    assert "docs/unrelated.bak" not in paths
    assert "." not in report["scope"]


def test_reference_scan_failure_and_limit_are_not_success(runtime, tmp_path, monkeypatch):
    root = fixture(tmp_path)
    for n in range(3):
        (root / f"scripts/unused_{n}.py").write_text("pass\n")
    review.git(root, "add", "--all")
    review.git(root, "commit", "--quiet", "-m", "Synthetic scripts")
    policy_path = root / ".ai-team/policy/gardening.json"
    policy = json.loads(policy_path.read_text())
    policy["scan_limits"]["incremental_reference_scan"] = 1
    policy_path.write_text(json.dumps(policy))
    contract_path = root / "specs/canary/work-contract.json"
    contract = json.loads(contract_path.read_text())
    contract["scope"]["include"] = ["scripts/**"]
    contract_path.write_text(json.dumps(contract))
    report = runtime.garden_incremental(str(root), "specs/canary", write=False)
    assert not report["pass"] and not report["scan_complete"]
    monkeypatch.setattr(
        runtime.subprocess,
        "check_output",
        lambda *a, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(128, "git")),
    )
    with pytest.raises(ValueError, match="REFERENCE_SCAN_UNAVAILABLE"):
        runtime.unreferenced_paths(str(root), ["scripts/unused_0.py"], 100)


def test_every_source_byte_is_retained_not_just_summary(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    ledger = engine.preservation_ledger(root, sources(root))
    assert ledger["complete"] and ledger["coverage"] == 1.0
    assert len(ledger["sections"]) == 3
    assert all(s["action"] == "retained" for s in ledger["sections"])
    (root / "docs/legacy.md").write_text("Summary: follow the prior design.\n")
    broken = engine.preservation_ledger(root, sources(root))
    assert not broken["complete"] and broken["unprocessed_sections"]
    assert "Never discard" not in json.dumps(broken)


def test_exact_section_split_and_omission_detection(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    ledger = split_source(engine, root)
    assert ledger["complete"] and all(s["action"] == "copied" for s in ledger["sections"])
    mappings = ledger["mappings"]
    (root / "docs/legacy.md").write_text("Summary.\n")
    assert engine.preservation_ledger(root, sources(root), mappings)["complete"]
    (root / "docs/retained-2.md").write_text("Incident happened.\n")
    assert not engine.preservation_ledger(root, sources(root), mappings)["complete"]


def test_unknown_section_mapping_and_source_alias_fail(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    ledger = split_source(engine, root)
    wrong = copy.deepcopy(ledger["mappings"])
    wrong[0]["section_id"] = "invented"
    with pytest.raises(ValueError, match="INVALID_PRESERVATION"):
        engine.preservation_ledger(root, sources(root), wrong)
    (root / "docs/retained-0.md").unlink()
    (root / "docs/retained-0.md").symlink_to(tmp_path / "external-private.md")
    with pytest.raises(ValueError, match="UNSAFE_PATH"):
        engine.preservation_ledger(root, sources(root), ledger["mappings"])


def test_retirement_default_hold_and_exact_approval_required(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    data = request(engine, root)
    data["references"][-1]["status"] = "UNKNOWN"
    plan = engine.retirement_plan(root, data)
    assert plan["status"] == "HOLD" and "EXTERNAL_REFERENCE_UNAVAILABLE" in plan["hold_reasons"]
    data["references"][-1]["status"] = "CLEAR"
    plan = engine.retirement_plan(root, data)
    assert plan["status"] == "APPROVAL_REQUIRED"
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        engine._retire_approved(root, plan, tmp_path / "quarantine", verify=verifier)
    assert (root / "docs/legacy.md").exists()


@pytest.mark.parametrize("drift", ["head", "content", "references", "retention", "approval"])
def test_approval_binding_cannot_survive_drift(runtime, tmp_path, drift):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    plan = engine.retirement_plan(root, request(engine, root))
    backup = tmp_path / "quarantine"
    auth = authorize(engine, root, plan, backup)
    if drift == "head":
        review.git(root, "commit", "--allow-empty", "--quiet", "-m", "changed HEAD")
    elif drift == "content":
        (root / "docs/legacy.md").write_text("unapproved bytes")
    elif drift == "references":
        (root / "src/new-consumer.py").write_text("load('docs/legacy.md')\n")
    elif drift == "retention":
        plan["request"]["retention"][0]["hold"] = True
    else:

        def auth(_):
            return False

    with pytest.raises(ValueError):
        engine._retire_approved(root, plan, backup, authorize=auth, verify=verifier)
    assert (root / "docs/legacy.md").exists()
    assert not backup.exists()


def test_fixture_retirement_is_recoverable_and_reverified(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    original = (root / "docs/legacy.md").read_bytes()
    index = review.git(root, "ls-files", "--stage")
    plan = engine.retirement_plan(root, request(engine, root))
    backup = tmp_path / "quarantine"
    result = engine._retire_approved(
        root, plan, backup, authorize=authorize(engine, root, plan, backup), verify=verifier
    )
    assert result["status"] == "RETIRED" and result["verification"]["exit_code"] == 0
    assert not (root / "docs/legacy.md").exists()
    assert review.git(root, "diff", "--cached", "--name-only") == "docs/legacy.md"
    assert (backup / "tombstone.json").exists()
    recovered = engine._recover_retirement(root, plan, backup, verify=verifier)
    assert recovered["status"] == "RECOVERED"
    assert (root / "docs/legacy.md").read_bytes() == original
    assert review.git(root, "ls-files", "--stage") == index
    assert (backup / "journal.json").exists()


@pytest.mark.parametrize("stage", ["backup_ready", "file_retired", "index_staged", "verified"])
def test_failure_restores_only_own_exact_files(runtime, tmp_path, stage):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    original = (root / "docs/legacy.md").read_bytes()
    plan = engine.retirement_plan(root, request(engine, root))
    backup = tmp_path / "quarantine"

    def fault(event):
        if event == stage:
            raise RuntimeError("synthetic fault")

    with pytest.raises(RuntimeError, match="synthetic fault"):
        engine._retire_approved(
            root,
            plan,
            backup,
            authorize=authorize(engine, root, plan, backup),
            verify=verifier,
            fault=fault,
        )
    assert (root / "docs/legacy.md").read_bytes() == original
    assert not review.git(root, "diff", "--cached", "--name-only")
    assert backup.exists()


def test_reference_drift_after_backup_prevents_first_retirement(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    plan = engine.retirement_plan(root, request(engine, root))
    backup = tmp_path / "quarantine"
    events = []

    def fault(stage):
        events.append(stage)
        if stage == "backup_ready":
            (root / "src/new-consumer.py").write_text("load('docs/legacy.md')\n")

    with pytest.raises(ValueError, match="SOURCE_DRIFT"):
        engine._retire_approved(
            root,
            plan,
            backup,
            authorize=authorize(engine, root, plan, backup),
            verify=verifier,
            fault=fault,
        )
    assert "file_retired" not in events
    assert (root / "docs/legacy.md").exists()
    assert (root / "src/new-consumer.py").exists()


@pytest.mark.parametrize("stage", ["file_retired", "index_staged"])
def test_actual_killed_process_recovers_from_prepared_journal(runtime, tmp_path, stage):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    original = (root / "docs/legacy.md").read_bytes()
    plan = engine.retirement_plan(root, request(engine, root))
    backup = tmp_path / "quarantine"

    def child():
        engine._retire_approved(
            root,
            plan,
            backup,
            authorize=authorize(engine, root, plan, backup),
            verify=verifier,
            fault=lambda event: os._exit(73) if event == stage else None,
        )

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(15)
    if process.is_alive():
        process.kill()
        process.join(5)
        pytest.fail("Owned retirement child timed out")
    assert process.exitcode == 73
    assert not (root / "docs/legacy.md").exists()
    assert not (backup / "tombstone.json").exists()
    result = engine._recover_retirement(root, plan, backup, verify=verifier)
    assert result["status"] == "RECOVERED"
    assert (root / "docs/legacy.md").read_bytes() == original
    assert not review.git(root, "diff", "--cached", "--name-only")


@pytest.mark.parametrize("drift", ["foreign_content", "backup_content", "index", "symlink"])
def test_recovery_refuses_to_overwrite_foreign_or_corrupt_bytes(runtime, tmp_path, drift):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    plan = engine.retirement_plan(root, request(engine, root))
    backup = tmp_path / "quarantine"
    engine._retire_approved(
        root, plan, backup, authorize=authorize(engine, root, plan, backup), verify=verifier
    )
    target = root / "docs/legacy.md"
    if drift == "foreign_content":
        target.write_text("FOREIGN EDIT")
    elif drift == "backup_content":
        next(backup.glob("source-*.bin")).write_bytes(b"CORRUPT")
    elif drift == "index":
        (root / "src/foreign.py").write_text("USER INDEX CHANGE\n")
        review.git(root, "add", "src/foreign.py")
    else:
        target.symlink_to(tmp_path / "outside")
    index = (root / ".git/index").read_bytes()
    with pytest.raises(ValueError):
        engine._recover_retirement(root, plan, backup, verify=verifier)
    assert (root / ".git/index").read_bytes() == index
    if drift == "foreign_content":
        assert target.read_text() == "FOREIGN EDIT"
    assert backup.exists()


def test_retirement_failed_verifier_is_not_done_and_restores_source(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    plan = engine.retirement_plan(root, request(engine, root))
    backup = tmp_path / "quarantine"
    with pytest.raises(ValueError, match="VERIFICATION_FAILED"):
        engine._retire_approved(
            root,
            plan,
            backup,
            authorize=authorize(engine, root, plan, backup),
            verify=lambda _: {"exit_code": 1, "check_id": "negative", "evidence_sha256": "a" * 64},
        )
    assert (root / "docs/legacy.md").exists()
    assert not (backup / "tombstone.json").exists()
    assert json.loads((backup / "journal.json").read_text())["status"] != "RETIRED"


def test_existing_or_alias_backup_never_overwritten(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    plan = engine.retirement_plan(root, request(engine, root))
    backup = tmp_path / "quarantine"
    backup.mkdir()
    (backup / "user.txt").write_text("KEEP")
    with pytest.raises(FileExistsError):
        engine._retire_approved(
            root, plan, backup, authorize=authorize(engine, root, plan, backup), verify=verifier
        )
    assert (backup / "user.txt").read_text() == "KEEP"
    alias = tmp_path / "alias"
    alias.symlink_to(backup, target_is_directory=True)
    with pytest.raises(ValueError, match="UNSAFE_PATH"):
        engine.retirement_authority_request(root, plan, alias, "retire")


@pytest.mark.parametrize("hold", ["retention", "static_reference", "missing_scan"])
def test_age_or_zero_grep_or_history_does_not_waive_holds(runtime, tmp_path, hold):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    data = request(engine, root)
    if hold == "retention":
        data["retention"][0]["hold"] = True
    elif hold == "static_reference":
        (root / "src/reader.py").write_text("load('legacy.md')\n")
    else:
        data["references"].pop()
    os.utime(root / "docs/legacy.md", (1, 1))
    result = engine.retirement_plan(root, data)
    assert result["status"] == "HOLD"
    assert (root / "docs/legacy.md").exists()


def test_preservation_rechecks_all_source_and_destination_bytes(runtime, tmp_path, monkeypatch):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    original = engine.SourceTree.read
    changed = False

    def drift(tree, path, *args, **kwargs):
        nonlocal changed
        value = original(tree, path, *args, **kwargs)
        if path == "docs/legacy.md" and not changed:
            changed = True
            (root / path).write_text("CHANGED DURING READ\n")
        return value

    monkeypatch.setattr(engine.SourceTree, "read", drift)
    with pytest.raises(ValueError, match="SOURCE_DRIFT"):
        engine.preservation_ledger(root, sources(root))


@pytest.mark.parametrize("bad", ["repository_id", "range", "duplicate_destination"])
def test_preservation_cannot_invent_origin_or_destination_locator(runtime, tmp_path, bad):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    src = sources(root)
    ledger = split_source(engine, root)
    mappings = ledger["mappings"]
    if bad == "repository_id":
        src[0]["repository_id"] = "foreign-owner"
    elif bad == "range":
        mappings[0]["destination"]["line_end"] = 999999
        assert not engine.preservation_ledger(root, src, mappings)["complete"]
        return
    else:
        mappings[1]["destination"]["doc_id"] = mappings[0]["destination"]["doc_id"]
    with pytest.raises(ValueError):
        engine.preservation_ledger(root, src, mappings)


def test_recovery_index_hash_cannot_be_reapproved_by_editing_journal(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    plan = engine.retirement_plan(root, request(engine, root))
    backup = tmp_path / "quarantine"
    engine._retire_approved(
        root, plan, backup, authorize=authorize(engine, root, plan, backup), verify=verifier
    )
    (root / "src/new-index.py").write_text("USER CHANGE\n")
    review.git(root, "add", "src/new-index.py")
    foreign = (root / ".git/index").read_bytes()
    (backup / "candidate.index").write_bytes(foreign)
    journal = json.loads((backup / "journal.json").read_text())
    journal["index_after_sha256"] = engine.digest(foreign)
    (backup / "journal.json").write_text(json.dumps(journal))
    with pytest.raises(ValueError, match="RECOVERY_MISMATCH"):
        engine._recover_retirement(root, plan, backup, verify=verifier)
    assert (root / ".git/index").read_bytes() == foreign


def test_actual_cli_preserves_and_plans_without_mutating_source(runtime, tmp_path):
    root = fixture(tmp_path)
    engine = runtime.amplai_docs
    draft = tmp_path / "preservation-input.json"
    draft.write_text(json.dumps({"sources": sources(root)}))
    command = [sys.executable, "-B", str(ROOT / "scripts/loopctl.py")]
    result = subprocess.run(
        [*command, "docs", "preserve", "--input", str(draft)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["complete"]
    data = request(engine, root)
    draft.write_text(json.dumps(data))
    before = (root / "docs/legacy.md").read_bytes()
    index = (root / ".git/index").read_bytes()
    result = subprocess.run(
        [*command, "docs", "plan-retirement", "--input", str(draft)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "APPROVAL_REQUIRED"
    assert (root / "docs/legacy.md").read_bytes() == before
    assert (root / ".git/index").read_bytes() == index
    invalid = subprocess.run(
        [*command, "garden", "full", "--report-only"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (
        invalid.returncode != 0
        and "EXPLICIT_GARDENING_WORK_REQUIRED" in invalid.stdout + invalid.stderr
    )


def test_installed_sealed_baseline_runs_preservation_and_report_only_garden(runtime, tmp_path):
    source = fixture(tmp_path)
    target = tmp_path / "installed"
    target.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "tools/amplai-loop-kit/install.py"),
            "--target",
            str(target),
            "--bootstrap-baseline",
            "--app-id",
            "preservation-canary",
            "--project-id",
            "canary",
            "--no-git",
        ],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("docs", "src", "evidence", "specs"):
        shutil.copytree(source / name, target / name, dirs_exist_ok=True)
    for name in ("policy/documentation.json", "runtime/repository-profile.json"):
        shutil.copyfile(source / ".ai-team" / name, target / ".ai-team" / name)
    for name in ("amplai_docs.py", "loopctl.py", "loopv2.py"):
        assert (target / "scripts" / name).read_bytes() == (ROOT / "scripts" / name).read_bytes()
    review.git(target, "init", "--quiet", "-b", "main")
    review.git(target, "add", "--all")
    review.git(target, "commit", "--quiet", "-m", "Installed fixture baseline")
    draft = tmp_path / "installed-preserve.json"
    draft.write_text(json.dumps({"sources": sources(target)}))
    command = [sys.executable, "-B", str(target / "scripts/loopctl.py")]
    result = subprocess.run(
        [*command, "docs", "preserve", "--input", str(draft)],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["complete"]
    (target / "src/user.bak").write_text("KEEP USER BACKUP\n")
    report = subprocess.run(
        [*command, "garden", "apply", "specs/canary"],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert report.returncode == 0, report.stdout + report.stderr
    assert json.loads(report.stdout)["report_only"]
    assert (target / "src/user.bak").read_text() == "KEEP USER BACKUP\n"


def test_read_only_plan_does_not_run_repository_fsmonitor_hook(runtime, tmp_path):
    root = fixture(tmp_path)
    data = request(runtime.amplai_docs, root)
    hook = root / ".git/hooks/fixture-fsmonitor"
    hook.write_text("#!/bin/sh\n: > .git/FS_MONITOR_EXECUTED\nprintf '2\\n\\000'\n")
    hook.chmod(0o700)
    review.git(root, "config", "core.fsmonitor", str(hook))
    before = (root / ".git/index").read_bytes()
    runtime.amplai_docs.retirement_plan(root, data)
    runtime.garden_incremental(str(root), "specs/canary", write=False)
    assert not (root / ".git/FS_MONITOR_EXECUTED").exists()
    assert (root / ".git/index").read_bytes() == before
