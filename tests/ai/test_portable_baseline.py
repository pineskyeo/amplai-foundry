"""Portable baseline behavior; all generated files stay in disposable fixtures."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / ".specify/scripts/taskify_to_tasks_md.py"
VALIDATOR = ROOT / ".agents/skills/taskify/scripts/validate_task_manifest.py"
FEATURE = ROOT / "specs/012-portable-document-lifecycle"
BASELINE = ROOT / "tools/amplai-loop-kit/baseline"


def run_python(script: Path, *args: object, isolated: bool = True):
    flags = ["-I", "-S", "-B"] if isolated else ["-B"]
    return subprocess.run(
        [sys.executable, *flags, str(script), *(str(a) for a in args)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )


def manifest_set(tmp_path: Path, *, declared: bool = True):
    manifest_dir = tmp_path / "task-manifests"
    manifest_dir.mkdir()
    task = json.loads((FEATURE / "task-manifests/PORTABLE-DOC-T001.yaml").read_text())
    task["id"] = "CANARY-T001"
    task["status"] = "ready"
    task["completion"] = {
        "all_acceptance_passed": False, "required_evidence_present": False,
        "completed_at": None, "evidence_paths": [],
    }
    task["acceptance"]["commands"] = [
        {"id": "check", "kind": "test", "run": "python3 -c 'assert 2 + 2 == 4'", "required": True}
    ]
    task["dependencies"]["tasks"] = []
    index = {
        "feature": {"id": "canary", "title": "Portable canary"},
        "tasks": [{"id": task["id"], "title": task["title"], "status": "ready"}],
        "execution": {"waves": [{"tasks": [task["id"]]}]},
    }
    if declared:
        index["slices"] = [{"id": "S01", "title": "Runnable canary", "tasks": [task["id"]]}]
    (manifest_dir / "index.yaml").write_text(json.dumps(index))
    (manifest_dir / "CANARY-T001.yaml").write_text(json.dumps(task))
    return manifest_dir, index, task


def test_json_generator_without_site_packages(tmp_path):
    manifest_dir, _, _ = manifest_set(tmp_path)
    result = run_python(GENERATOR, manifest_dir)
    assert result.returncode == 0, result.stderr
    rendered = (tmp_path / "tasks.md").read_text()
    assert "## Slice S01" in rendered
    assert "Eval:\n- `python3 -c 'assert 2 + 2 == 4'`" in rendered


def test_json_validator_without_site_packages(tmp_path):
    manifest_dir, _, _ = manifest_set(tmp_path)
    result = run_python(VALIDATOR, manifest_dir)
    assert result.returncode == 0, result.stderr
    assert "PASS: validated 1 task" in result.stdout


@pytest.mark.parametrize("declared", [True, False])
def test_generator_connects_declared_and_legacy_waves_to_eval(tmp_path, declared):
    manifest_dir, _, _ = manifest_set(tmp_path, declared=declared)
    result = run_python(GENERATOR, manifest_dir, isolated=False)
    assert result.returncode == 0, result.stderr
    rendered = (tmp_path / "tasks.md").read_text()
    assert "## Slice S01" in rendered
    assert "Acceptance:\n" in rendered
    assert "Eval:\n- `python3 -c 'assert 2 + 2 == 4'`" in rendered
    listed = subprocess.run(
        ["bash", "scripts/eval.sh", "--tasks", str(tmp_path / "tasks.md"), "--list-slices"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert listed.returncode == 0, listed.stderr
    assert "  S01" in listed.stdout


def test_generator_preserves_handwritten_output(tmp_path):
    manifest_dir, _, _ = manifest_set(tmp_path)
    output = tmp_path / "tasks.md"
    output.write_text("# Developer-owned task notes\n")
    result = run_python(GENERATOR, manifest_dir)
    assert result.returncode == 1
    assert "Refusing to overwrite" in result.stderr
    assert output.read_text() == "# Developer-owned task notes\n"


def test_generator_leaves_empty_eval_visible_and_blocked_not_executable(tmp_path):
    manifest_dir, index, task = manifest_set(tmp_path)
    task["acceptance"]["commands"] = []
    (manifest_dir / "CANARY-T001.yaml").write_text(json.dumps(task))
    result = run_python(GENERATOR, manifest_dir)
    assert result.returncode == 0, result.stderr
    assert "Eval:\n\nDependencies:" in (tmp_path / "tasks.md").read_text()
    index["tasks"][0]["status"] = "blocked"
    (manifest_dir / "index.yaml").write_text(json.dumps(index))
    assert run_python(GENERATOR, manifest_dir).returncode == 0
    text = (tmp_path / "tasks.md").read_text()
    assert "## Slice " not in text
    assert "## Blocked — DO NOT EXECUTE" in text


@pytest.mark.parametrize("case", ["requirements", "invariants", "gate", "approval", "done", "dependency", "cycle"])
def test_json_validator_preserves_existing_guards(tmp_path, case):
    manifest_dir, _, task = manifest_set(tmp_path)
    if case == "requirements":
        task["source"]["requirements"] = []
    elif case == "invariants":
        task["implementation"]["invariants"] = []
    elif case == "gate":
        task["acceptance"]["commands"][0]["required"] = False
    elif case == "approval":
        task["risk"]["approvals"] = []
    elif case == "done":
        task["status"] = "done"
    elif case == "dependency":
        task["dependencies"]["tasks"] = ["MISSING-T001"]
    else:
        other = copy.deepcopy(task)
        other["id"] = "CANARY-T002"
        other["dependencies"]["tasks"] = [task["id"]]
        task["dependencies"]["tasks"] = [other["id"]]
        (manifest_dir / "CANARY-T002.yaml").write_text(json.dumps(other))
    (manifest_dir / "CANARY-T001.yaml").write_text(json.dumps(task))
    result = run_python(VALIDATOR, manifest_dir)
    assert result.returncode == 1, result.stderr
    assert "FAILED:" in result.stdout


def test_optional_legacy_yaml_works_without_weakening_json_path(tmp_path):
    import yaml

    manifest_dir, index, task = manifest_set(tmp_path)
    (manifest_dir / "index.yaml").write_text(yaml.safe_dump(index))
    (manifest_dir / "CANARY-T001.yaml").write_text(yaml.safe_dump(task))
    for script in (GENERATOR, VALIDATOR):
        legacy = run_python(script, manifest_dir, isolated=False)
        assert legacy.returncode == 0, legacy.stderr
        unavailable = run_python(script, manifest_dir)
        assert unavailable.returncode != 0
        assert "PyYAML" in unavailable.stderr or "PyYAML" in unavailable.stdout
        assert "Traceback" not in unavailable.stderr


def test_task_tools_use_python36_grammar_and_no_new_runtime_annotations():
    # Grammar check on the available host is not a Python 3.6 execution claim.
    for script in (GENERATOR, VALIDATOR):
        source = script.read_text()
        tree = ast.parse(source, feature_version=(3, 6))
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in ast.walk(tree)
        )


def assemble_baseline(tmp_path):
    """Materialize the finite payload, not an installer/host activation claim."""
    repo = tmp_path / "repository"
    repo.mkdir()
    manifest = json.loads((BASELINE / "manifest.json").read_text())
    actual = {
        path.relative_to(BASELINE / "payload").as_posix()
        for path in (BASELINE / "payload").rglob("*") if path.is_file()
    }
    assert actual == {item["path"] for item in manifest["files"]}
    for item in manifest["files"]:
        source = BASELINE / "payload" / item["path"]
        assert "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest() == item["sha256"]
        target = repo / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(int(item["mode"], 8))
    for item in manifest["mirrors"]:
        target = repo / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(item["target"], target_is_directory=True)
    for item in manifest["markers"]:
        source = BASELINE / "fragments" / item["fragment"]
        assert "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest() == item["sha256"]
        (repo / item["path"]).write_bytes(source.read_bytes())
    return repo


def fixture_command(repo, *args):
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GIT_", "AMPLAI_", "PYTHON", "SPECIFY_", "EVAL_"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [str(a) for a in args], cwd=repo, env=env,
        capture_output=True, text=True, timeout=30,
    )


def test_finite_generic_payload_runs_real_controller_and_eval(tmp_path):
    repo = assemble_baseline(tmp_path)
    assert fixture_command(repo, "git", "init", "--quiet").returncode == 0
    marker = repo / ".specify/feature.json"
    marker.write_text(json.dumps({"feature_directory": "specs/canary"}))
    feature = repo / "specs/canary"
    feature.mkdir(parents=True)
    contract = json.loads((repo / ".ai-team/contracts/work-contract.template.json").read_text())
    contract.update(id="CANARY", goal="Verify portable baseline runtime", status="ready")
    contract["scope"] = {"include": ["scripts/loopctl.py"], "exclude": ["live systems"]}
    contract["knowledge"]["readiness"] = "required"
    (feature / "work-contract.json").write_text(json.dumps(contract))
    readiness = json.loads((repo / ".ai-team/knowledge/knowledge-readiness.template.json").read_text())
    readiness.update(work_id="CANARY", work_type="new_feature", risk="normal")
    for check in readiness["checks"].values():
        check.update(status="ready", evidence=[{"type": "fixture", "path": "AGENTS.md"}])
    (feature / "knowledge-readiness.json").write_text(json.dumps(readiness))
    # Reuse the actual JSON task/manifest fixtures with a harmless observable Eval.
    manifest_set(feature)
    commands = [
        ["scripts/loopctl.py", "doctor"],
        ["scripts/loopctl.py", "contract", "validate", feature / "work-contract.json"],
        ["scripts/loopctl.py", "readiness", "evaluate", feature / "knowledge-readiness.json"],
        ["scripts/loopctl.py", "context", "build", feature],
        ["scripts/loopctl.py", "context", "validate", feature / "context-pack.json"],
        ["scripts/loopctl.py", "environment", "capture", feature],
        [".agents/skills/taskify/scripts/validate_task_manifest.py", feature / "task-manifests"],
        [".specify/scripts/taskify_to_tasks_md.py", feature / "task-manifests"],
    ]
    for args in commands:
        result = fixture_command(repo, sys.executable, "-I", "-S", "-B", *args)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
    result = fixture_command(repo, "bash", "scripts/eval.sh", "--feature", "specs/canary", "--slice", "S01")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "EVAL: PASS" in result.stdout
    # The full generic profile executes the selected manifest, not a private .venv/Vault command.
    result = fixture_command(repo, sys.executable, "-I", "-S", "-B", ".ai-team/verifiers/run.py", "--profile", "v2")
    assert result.returncode == 0, (result.stdout, result.stderr)
    context = json.loads((feature / "context-pack.json").read_text())
    assert context["active_decisions"] == []
    assert {x["path"] for x in context["required_knowledge"]} == {
        "AGENTS.md", ".ai-team/runtime/WORKFLOW.md", ".ai-team/verifiers/registry.json",
    }
    fingerprint = json.loads((feature / "environment.json").read_text())
    assert fingerprint["safe_environment"] == {}
    assert fingerprint["fixture_refs"] == []


@pytest.mark.parametrize("removed", [
    "scripts/loopv2.py", ".ai-team/contracts/work-contract.schema.json",
    ".ai-team/knowledge/decisions.index.json", ".ai-team/policy/permissions.json",
    ".agents/skills/speckit-plan/SKILL.md", ".agents/skills/taskify/agents/openai.yaml",
    ".agents/skills/taskify/references/manifest-contract.md",
    ".agents/skills/taskify/assets/task-index.yaml",
    ".specify/scripts/bash/common.sh", ".specify/templates/plan-template.md",
])
def test_missing_generic_capability_fails_instead_of_marker_only_success(tmp_path, removed):
    repo = assemble_baseline(tmp_path)
    (repo / removed).unlink()  # Disposable fixture only.
    result = fixture_command(repo, sys.executable, "-I", "-S", "-B", "scripts/loopctl.py", "doctor")
    assert result.returncode != 0


def test_generic_mirrors_and_mandatory_skills_cannot_be_downgraded(tmp_path):
    repo = assemble_baseline(tmp_path)
    link = repo / ".claude/skills/work"
    link.unlink()
    link.symlink_to("../../.agents/skills/design", target_is_directory=True)
    result = fixture_command(repo, sys.executable, "-I", "-S", "-B", "scripts/loopctl.py", "doctor")
    assert result.returncode != 0
    assert "symlink" in result.stdout
    link.unlink()
    link.symlink_to("../../.agents/skills/work", target_is_directory=True)
    shutil.rmtree(repo / ".agents/skills/code-review")
    result = fixture_command(repo, sys.executable, "-I", "-S", "-B", "scripts/loopctl.py", "doctor")
    assert result.returncode != 0
    assert "code-review" in result.stdout


def test_generic_payload_excludes_private_profiles_and_source_state():
    manifest = json.loads((BASELINE / "manifest.json").read_text())
    assert len(manifest["required_skills"]) == 12
    forbidden = ("src/amplai_foundry", "vault/", "messenger-governance-closure", "specs/003-slack", "/Users/", "cortex.spec")
    for item in manifest["files"]:
        text = (BASELINE / "payload" / item["path"]).read_text()
        assert not any(value in item["path"] or value in text for value in forbidden), item["path"]
        assert not item["path"].startswith((".ai-team/local/", ".ai-team/install/", ".ai-team/backups/", "specs/"))
        assert item["path"] != ".ai-team/app.json"
    assert (BASELINE / "payload/.ai-team/knowledge/claims.jsonl").read_text().strip() == ""
    assert json.loads((BASELINE / "payload/.ai-team/knowledge/decisions.index.json").read_text())["entries"] == []
    for path in manifest["shared_sources"]:
        assert (BASELINE / "payload" / path).read_bytes() == (ROOT / path).read_bytes(), path


def test_foundry_retains_its_full_skill_and_verifier_profile():
    result = run_python(ROOT / "scripts/loopctl.py", "doctor")
    assert result.returncode == 0, (result.stdout, result.stderr)
    profile = json.loads((ROOT / ".ai-team/runtime/repository-profile.json").read_text())
    assert profile["id"] == "foundry"
    assert len(profile["additional_skills"]) == 6
    registry = json.loads((ROOT / ".ai-team/verifiers/registry.json").read_text())
    assert {"pytest", "ruff-check", "ruff-format", "mypy", "schema", "vault-lint", "project-pack", "kit-seal"} <= {x["id"] for x in registry["checks"]}


def install_canary(target, *args, package=None):
    package = package or BASELINE.parent
    return subprocess.run(
        [sys.executable, "-B", str(package / "install.py"), "--target", str(target),
         "--app-id", "canary", "--project-id", "portable-test", *args],
        cwd=target, capture_output=True, text=True, timeout=30,
    )


def canary_inventory(target):
    result = {}
    for item in sorted(target.rglob("*")):
        rel = item.relative_to(target).as_posix()
        if rel.startswith(".ai-team/backups/") or "__pycache__" in item.parts:
            continue
        if item.is_symlink():
            result[rel] = ("symlink", os.readlink(item))
        elif item.is_file():
            result[rel] = ("file", item.read_bytes(), item.stat().st_mode & 0o777)
    return result


def test_s02_fresh_installer_has_complete_baseline_and_composed_receipt(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Application rules\nKeep local ownership.\n")
    (tmp_path / "CLAUDE.md").write_text("# Application rules for Claude\n")
    result = install_canary(tmp_path, "--bootstrap-baseline", "--repo-profile", "generic")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    paths = [item["path"] for item in report["actions"]]
    assert len(paths) == len(set(paths))
    manifest = json.loads((BASELINE / "manifest.json").read_text())
    for item in manifest["files"]:
        assert (tmp_path / item["path"]).is_file(), item["path"]
    for item in manifest["mirrors"]:
        assert os.readlink(tmp_path / item["path"]) == item["target"]
    assert "Keep local ownership." in (tmp_path / "AGENTS.md").read_text()
    assert "AMPLAI-ASYNC-BEGIN" in (tmp_path / ".agents/skills/work/SKILL.md").read_text()
    doctor = fixture_command(tmp_path, "scripts/loopctl.py", "doctor")
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    state = json.loads((tmp_path / ".ai-team/install/amplai-loop-kit.json").read_text())
    assert state["baseline"]["profile"] == "generic"
    assert state["package_sha256"].startswith("sha256:")


def test_s02_profile_omitted_repeat_and_uninstall_preserve_user_content(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# User rules\n")
    (tmp_path / "CLAUDE.md").write_text("# User Claude rules\n")
    (tmp_path / "user.py").write_text("# untouched\n")
    before = canary_inventory(tmp_path)
    result = install_canary(tmp_path, "--bootstrap-baseline")
    assert result.returncode == 0, result.stderr
    installed = canary_inventory(tmp_path)
    result = install_canary(tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["actions"] == []
    assert canary_inventory(tmp_path) == installed
    result = install_canary(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert canary_inventory(tmp_path) == before


@pytest.mark.parametrize("change", ["store-drift", "extra", "missing", "symlink", "duplicate"])
def test_s02_full_package_seal_rejects_before_target_mutation(tmp_path, change):
    package = tmp_path / "kit"
    shutil.copytree(BASELINE.parent, package, ignore=shutil.ignore_patterns("__pycache__"))
    target = tmp_path / "app"
    target.mkdir()
    (target / "keep.txt").write_text("keep\n")
    before = canary_inventory(target)
    store = package / "payload/store/supervisor/run"
    if change == "store-drift":
        store.write_bytes(store.read_bytes() + b"\n# same version, changed bytes\n")
    elif change == "extra":
        (package / "unlisted.py").write_text("raise RuntimeError('unlisted')\n")
    elif change == "missing":
        store.unlink()
    elif change == "symlink":
        original = store.read_bytes()
        outside = tmp_path / "outside-run"
        outside.write_bytes(original)
        store.unlink()
        store.symlink_to(outside)
    else:
        path = package / "CHECKSUMS.sha256"
        path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    result = install_canary(target, "--bootstrap-baseline", package=package)
    assert result.returncode != 0
    assert "package" in result.stderr.lower(), result.stderr
    assert canary_inventory(target) == before


def test_s02_baseline_local_edit_conflict_is_non_mutating(tmp_path):
    result = install_canary(tmp_path, "--bootstrap-baseline")
    assert result.returncode == 0, result.stderr
    path = tmp_path / "scripts/loopctl.py"
    path.write_bytes(path.read_bytes() + b"\n# user edit\n")
    before = canary_inventory(tmp_path)
    for args in [(), ("--uninstall",)]:
        result = install_canary(tmp_path, *args)
        assert result.returncode != 0
        assert "local" in result.stderr or "changed" in result.stderr
        assert canary_inventory(tmp_path) == before


def installer_object(target, *args, package=None):
    package = package or BASELINE.parent
    spec = importlib.util.spec_from_file_location("portable_installer_canary", package / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    options = module.build_parser().parse_args([
        "--target", str(target), "--app-id", "canary", "--project-id", "portable-test", *args,
    ])
    return module, module.Installer(options)


@pytest.mark.parametrize("operation", ["fresh-symlink", "uninstall-file", "uninstall-symlink"])
def test_s02_mid_transaction_failure_restores_files_modes_and_links(tmp_path, monkeypatch, operation):
    (tmp_path / "AGENTS.md").write_text("# Local rules\n")
    (tmp_path / "AGENTS.md").chmod(0o600)
    args = ["--bootstrap-baseline"]
    if operation.startswith("uninstall"):
        result = install_canary(tmp_path, *args)
        assert result.returncode == 0, result.stderr
        args = ["--uninstall"]
    before = canary_inventory(tmp_path)
    _, instance = installer_object(tmp_path, *args)
    original = instance.tree.write
    fired = []

    def fail_after_write(rel, desired, expected):
        original(rel, desired, expected)
        selected = ((operation == "fresh-symlink" and desired["kind"] == "symlink")
                    or (operation == "uninstall-file" and desired["kind"] == "absent"
                        and expected["kind"] == "file")
                    or (operation == "uninstall-symlink" and desired["kind"] == "absent"
                        and expected["kind"] == "symlink"))
        if selected and not rel.startswith(".ai-team/backups/") and not fired:
            fired.append(rel)
            raise OSError("injected after committed write")

    monkeypatch.setattr(instance.tree, "write", fail_after_write)
    with pytest.raises(OSError, match="injected"):
        instance.execute()
    assert len(fired) == 1
    assert canary_inventory(tmp_path) == before


def test_s02_concurrent_installers_serialize_without_mutation(tmp_path):
    module, first = installer_object(tmp_path, "--bootstrap-baseline")
    _, second = installer_object(tmp_path, "--bootstrap-baseline")
    before = canary_inventory(tmp_path)
    first.tree.lock()
    try:
        with pytest.raises(module.InstallError, match="another installer"):
            # Use the same exception type as the actual second module instance.
            try:
                second.execute()
            except Exception as exc:
                raise module.InstallError(str(exc))
        assert canary_inventory(tmp_path) == before
    finally:
        first.tree.close()


def test_s02_preflight_drift_preserves_the_new_user_bytes(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Original user text\n")
    module, instance = installer_object(tmp_path, "--bootstrap-baseline")
    (tmp_path / "AGENTS.md").write_text("# Changed after snapshot\n")
    before = canary_inventory(tmp_path)
    with pytest.raises(module.InstallError, match="changed since preflight"):
        instance.execute()
    assert canary_inventory(tmp_path) == before


def test_s02_preexisting_identity_binding_and_matching_hook_are_borrowed(tmp_path):
    module, instance = installer_object(tmp_path, "--bootstrap-baseline")
    instance.tree.close()
    identity = {"schema_version": "1.0", "kind": "app_identity", "runtime_protocol": module.PROTOCOL,
                "project_id": "portable-test", "app_id": "canary", "local_note": "keep"}
    binding = dict(identity, kind="local_project_binding", project_home=str(tmp_path / "unused-store"))
    for rel, value in [(".ai-team/app.json", identity), (".ai-team/local/project.json", binding)]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(module.seal(value)) + "\n")
    hooks = json.loads((BASELINE.parent / "manifest.json").read_text())["claude_hooks"]
    settings = module.merge_hooks({"permissions": {"defaultMode": "default"}, "user": True}, hooks)
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(settings) + "\n")
    before_identity = (tmp_path / ".ai-team/app.json").read_bytes()
    before_binding = (tmp_path / ".ai-team/local/project.json").read_bytes()
    result = install_canary(tmp_path, "--bootstrap-baseline")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".ai-team/app.json").read_bytes() == before_identity
    assert (tmp_path / ".ai-team/local/project.json").read_bytes() == before_binding
    result = install_canary(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".ai-team/app.json").read_bytes() == before_identity
    assert (tmp_path / ".ai-team/local/project.json").read_bytes() == before_binding
    assert json.loads(path.read_text()) == settings


def test_s02_real_installed_baseline_runs_the_complete_controller_canary(tmp_path, monkeypatch):
    def materialize(path):
        repo = path / "repository"
        repo.mkdir()
        result = install_canary(repo, "--bootstrap-baseline")
        assert result.returncode == 0, result.stderr
        return repo
    monkeypatch.setattr(sys.modules[__name__], "assemble_baseline", materialize)
    test_finite_generic_payload_runs_real_controller_and_eval(tmp_path)


@pytest.mark.parametrize("phase", ["preexisting", "after-parent-open"])
def test_s02_output_does_not_follow_substituted_parent(tmp_path, monkeypatch, phase):
    target = tmp_path / "app"
    target.mkdir()
    selected = target / "selected"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside is untouched\n")
    module, instance = installer_object(target, "--bootstrap-baseline")
    tree = instance.tree
    fired = []
    before = canary_inventory(outside)
    original_sync = module.os.fsync

    def substitute():
        selected.rename(target / "preserved-selected")
        selected.symlink_to(outside, target_is_directory=True)
        fired.append(True)

    if phase == "preexisting":
        substitute()
    else:
        tree.parent("selected/new.txt")
        def sync_then_substitute(fd):
            original_sync(fd)
            if not fired:
                substitute()
        monkeypatch.setattr(module.os, "fsync", sync_then_substitute)
    try:
        with pytest.raises((OSError, module.InstallError)):
            tree.write("selected/new.txt", {"kind": "file", "data": b"owned", "mode": 0o644}, {"kind": "absent"})
        assert fired
        assert canary_inventory(outside) == before
        assert not list((target / "preserved-selected").iterdir())
    finally:
        tree.close()


def test_s02_package_snapshot_is_used_after_source_mutation(tmp_path):
    package = tmp_path / "kit"
    shutil.copytree(BASELINE.parent, package, ignore=shutil.ignore_patterns("__pycache__"))
    target = tmp_path / "app"
    target.mkdir()
    _, instance = installer_object(target, "--bootstrap-baseline", package=package)
    source = package / "baseline/payload/scripts/loopctl.py"
    original = source.read_bytes()
    source.write_text("raise RuntimeError('after-snapshot injection')\n")
    instance.execute()
    assert (target / "scripts/loopctl.py").read_bytes() == original


def test_s02_same_version_sealed_upgrade_updates_exact_baseline_bytes(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    result = install_canary(target, "--bootstrap-baseline")
    assert result.returncode == 0, result.stderr
    package = tmp_path / "next-kit"
    shutil.copytree(BASELINE.parent, package, ignore=shutil.ignore_patterns("__pycache__"))
    source = package / "baseline/payload/scripts/loopctl.py"
    source.write_bytes(source.read_bytes() + b"\n# approved isolated upgrade fixture\n")
    reseal = run_python(package / "seal.py")
    assert reseal.returncode == 0, reseal.stderr
    before = json.loads((target / ".ai-team/install/amplai-loop-kit.json").read_text())
    result = install_canary(target, package=package)
    assert result.returncode == 0, result.stderr
    after = json.loads((target / ".ai-team/install/amplai-loop-kit.json").read_text())
    assert before["package_version"] == after["package_version"]
    assert before["package_sha256"] != after["package_sha256"]
    assert (target / "scripts/loopctl.py").read_bytes() == source.read_bytes()
    result = install_canary(target, package=package)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["actions"] == []


def test_s02_identical_unmanaged_files_and_mirrors_are_not_deleted(tmp_path):
    source = BASELINE / "payload/.agents/skills/taskify/scripts/validate_task_manifest.py"
    target = tmp_path / ".agents/skills/taskify/scripts/validate_task_manifest.py"
    target.parent.mkdir(parents=True)
    shutil.copy2(source, target)
    link = tmp_path / ".claude/skills/taskify"
    link.parent.mkdir(parents=True)
    link.symlink_to("../../.agents/skills/taskify", target_is_directory=True)
    runtime = tmp_path / "scripts/amplai_runtime.py"
    runtime.parent.mkdir()
    shutil.copy2(BASELINE.parent / "payload/scripts/amplai_runtime.py", runtime)
    before = canary_inventory(tmp_path)
    result = install_canary(tmp_path, "--bootstrap-baseline")
    assert result.returncode == 0, result.stderr
    result = install_canary(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert canary_inventory(tmp_path) == before


def test_s02_uninstall_does_not_delete_local_managed_marker_edit(tmp_path):
    result = install_canary(tmp_path, "--bootstrap-baseline")
    assert result.returncode == 0, result.stderr
    path = tmp_path / "AGENTS.md"
    path.write_text(path.read_text().replace("<!-- AMPLAI-BASELINE-END -->", "Local rule.\n<!-- AMPLAI-BASELINE-END -->"))
    before = canary_inventory(tmp_path)
    result = install_canary(tmp_path, "--uninstall")
    assert result.returncode != 0
    assert canary_inventory(tmp_path) == before


@pytest.mark.parametrize("operation", ["install", "uninstall", "staged-write"])
def test_s02_hard_stop_has_explicit_recoverable_transaction(tmp_path, operation):
    (tmp_path / "AGENTS.md").write_text("# Preserved initial instructions\n")
    if operation == "uninstall":
        result = install_canary(tmp_path, "--bootstrap-baseline")
        assert result.returncode == 0, result.stderr
    before = canary_inventory(tmp_path)
    script = """
import importlib.util, os, stat, sys
spec = importlib.util.spec_from_file_location('canary', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
args = module.build_parser().parse_args(['--target', sys.argv[2], '--app-id', 'canary',
    '--project-id', 'portable-test', '--uninstall' if sys.argv[3] == 'uninstall' else '--bootstrap-baseline'])
instance = module.Installer(args)
original = instance.tree.write
def stop(rel, desired, expected):
    original(rel, desired, expected)
    selected = desired['kind'] == ('absent' if sys.argv[3] == 'uninstall' else 'file')
    if selected and not rel.startswith('.ai-team/backups/') and not rel.endswith('.transaction.json'):
        os._exit(77)
instance.tree.write = stop
if sys.argv[3] == 'staged-write':
    original_sync = module.os.fsync
    def stop_after_stage(fd):
        original_sync(fd)
        if (stat.S_ISREG(os.fstat(fd).st_mode) and instance.journal is not None
                and os.path.isfile(os.path.join(sys.argv[2], module.JOURNAL_REL))):
            os._exit(77)
    module.os.fsync = stop_after_stage
instance.execute()
"""
    stopped = subprocess.run([sys.executable, "-B", "-c", script, str(BASELINE.parent / "install.py"),
                              str(tmp_path), operation], capture_output=True, text=True, timeout=30)
    assert stopped.returncode == 77, stopped.stderr
    journal = tmp_path / ".ai-team/install/amplai-loop-kit.transaction.json"
    assert journal.is_file(), "hard-stop ownership must survive the process"
    pending = canary_inventory(tmp_path)
    result = install_canary(tmp_path, "--bootstrap-baseline")
    assert result.returncode != 0
    assert "recover" in result.stderr
    assert canary_inventory(tmp_path) == pending
    result = install_canary(tmp_path, "--recover")
    assert result.returncode == 0, result.stderr
    assert not journal.exists()
    assert canary_inventory(tmp_path) == before


def test_s02_preexisting_matching_marker_and_json_rules_survive_uninstall(tmp_path):
    # Extension-only target: identical existing content is borrowed, not adopted.
    work = tmp_path / ".agents/skills/work/SKILL.md"
    work.parent.mkdir(parents=True)
    fragment = (BASELINE.parent / "fragments/work.md").read_text()
    work.write_text("# User work\n" + fragment)
    design = tmp_path / ".agents/skills/design/SKILL.md"
    design.parent.mkdir(parents=True)
    design.write_text("# User design\n")
    workflow = tmp_path / ".ai-team/runtime/WORKFLOW.md"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("# User workflow\n")
    extension = json.loads((BASELINE.parent / "fragments/policy-extension.json").read_text())
    policy = dict(extension, schema_version="2.0", runtime="amplai-loop-v2", public_commands=["work", "design"])
    path = workflow.parent / "policy.json"
    path.write_text(json.dumps(policy) + "\n")
    original_work = work.read_bytes()
    result = install_canary(tmp_path)
    assert result.returncode == 0, result.stderr
    result = install_canary(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert work.read_bytes() == original_work
    assert json.loads(path.read_text()) == policy


def test_s02_installed_hosts_select_same_real_work_design_context_and_evidence(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    store_path = tmp_path / "disposable-store"
    result = install_canary(repo, "--bootstrap-baseline", "--project-home", str(store_path), "--no-git")
    assert result.returncode == 0, result.stderr
    spec = importlib.util.spec_from_file_location("installed_context_canary", repo / "scripts/amplai_runtime.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    assert Path(runtime.__file__).resolve().is_relative_to(repo)
    store = runtime.ProjectStore(str(store_path))
    change = store.create_change("canary", "host-neutral context", "canary", affected_apps=["canary"])
    store.activate_change(change["change_id"])
    evidence = store.add_evidence(change["change_id"], "test", "Shared verified fixture evidence",
                                 "fixture", "canary", app_id="canary")
    for controller in ("work", "design"):
        work = store.create_work(change["change_id"], "canary", "Select the same " + controller,
                                 acceptance=["same evidence"], controller=controller,
                                 input_evidence_refs=[evidence["evidence_id"]])
        store.activate_work(work["work_id"])
        contexts, hooks = [], []
        for host in ("claude", "codex"):
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AMPLAI_", "CLAUDE_", "GIT_", "PYTHON"))}
            env.update(AMPLAI_REPO_ROOT=str(repo), AMPLAI_PROJECT_HOME=str(store_path), AMPLAI_HOST=host,
                       PYTHONDONTWRITEBYTECODE="1")
            context = subprocess.run([sys.executable, "-B", "scripts/amplai.py", "--json", "work", "context",
                                      "--id", work["work_id"]], cwd=repo, env=env, capture_output=True,
                                     text=True, input="", timeout=30)
            assert context.returncode == 0, context.stderr
            value = json.loads(context.stdout)
            value = value.get("data", value)
            value.pop("generated_at", None)
            value.pop("content_hash", None)
            contexts.append(value)
            hook = subprocess.run([sys.executable, "-B", "scripts/amplai_hook.py", "session-start", "--host", host],
                                  cwd=repo, env=env, capture_output=True, text=True, input="{}", timeout=30)
            assert hook.returncode == 0 and not hook.stderr, hook.stderr
            hooks.append(json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"])
        assert contexts[0] == contexts[1]
        assert contexts[0]["work"]["controller"] == controller
        assert contexts[0]["evidence"][0]["evidence_id"] == evidence["evidence_id"]
        assert hooks[0] == hooks[1]


S18_BORROWED_CASES = [
    ("scripts/amplai_docs.py", False),
    (".agents/skills/work/SKILL.md", False),
    (".agents/skills/work/SKILL.md", True),
]


def s18_seed_borrowed(target, relative, already_composed=False):
    initial = (BASELINE / "payload" / relative).read_bytes()
    if already_composed:
        _, instance = installer_object(target, "--bootstrap-baseline", "--no-git")
        try:
            item = next(row for row in instance.baseline["files"] if row["path"] == relative)
            _, initial = instance.composed_baseline(item)
        finally:
            instance.tree.close()
    path = target / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(initial)
    path.chmod(0o600)
    return path, initial


def s18_receipt(target, relative):
    state = json.loads((target / ".ai-team/install/amplai-loop-kit.json").read_text())
    return state["baseline"]["files"][relative]


def s18_package_update(tmp_path, relative, *, mode=None, content=False):
    package = tmp_path / "updated-kit"
    shutil.copytree(BASELINE.parent, package, ignore=shutil.ignore_patterns("__pycache__"))
    manifest_path = package / "baseline/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    item = next(row for row in manifest["files"] if row["path"] == relative)
    if mode is not None:
        item["mode"] = format(mode, "o")
    if content:
        source = package / "baseline/payload" / relative
        source.write_bytes(source.read_bytes() + b"\n# Synthetic public package update\n")
    manifest_path.write_text(json.dumps(manifest))
    resealed = run_python(package / "seal.py")
    assert resealed.returncode == 0, resealed.stdout + resealed.stderr
    assert run_python(package / "seal.py", "--verify").returncode == 0
    return package


@pytest.mark.parametrize("relative,already_composed", S18_BORROWED_CASES)
def test_s18_borrowed_repeated_install_and_uninstall_keep_bytes_and_mode(
    tmp_path, relative, already_composed
):
    path, initial = s18_seed_borrowed(tmp_path, relative, already_composed)
    for attempt in range(3):
        result = install_canary(tmp_path, "--bootstrap-baseline", "--no-git")
        assert result.returncode == 0, result.stderr
        assert path.stat().st_mode & 0o777 == 0o600
        receipt = s18_receipt(tmp_path, relative)
        assert receipt["created"] is False
        assert receipt["mode"] == 0o600
        if attempt:
            assert json.loads(result.stdout)["actions"] == []
    removed = install_canary(tmp_path, "--uninstall", "--no-git")
    assert removed.returncode == 0, removed.stderr
    assert path.read_bytes() == initial
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("relative,already_composed", S18_BORROWED_CASES)
def test_s18_package_mode_update_does_not_adopt_borrowed_permissions(
    tmp_path, relative, already_composed
):
    target = tmp_path / "target"
    target.mkdir()
    path, initial = s18_seed_borrowed(target, relative, already_composed)
    assert install_canary(target, "--bootstrap-baseline", "--no-git").returncode == 0
    package = s18_package_update(tmp_path, relative, mode=0o750)
    updated = install_canary(target, "--bootstrap-baseline", "--no-git", package=package)
    assert updated.returncode == 0, updated.stderr
    assert path.stat().st_mode & 0o777 == 0o600
    assert s18_receipt(target, relative)["mode"] == 0o600
    assert s18_receipt(target, relative)["created"] is False
    removed = install_canary(target, "--uninstall", "--no-git", package=package)
    assert removed.returncode == 0, removed.stderr
    assert path.read_bytes() == initial
    assert path.stat().st_mode & 0o777 == 0o600


def test_s18_installer_owned_mode_updates_remain_supported(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    relative = "scripts/amplai_docs.py"
    assert install_canary(target, "--bootstrap-baseline", "--no-git").returncode == 0
    assert s18_receipt(target, relative)["created"] is True
    package = s18_package_update(tmp_path, relative, mode=0o750)
    updated = install_canary(target, "--bootstrap-baseline", "--no-git", package=package)
    assert updated.returncode == 0, updated.stderr
    assert (target / relative).stat().st_mode & 0o777 == 0o750
    assert s18_receipt(target, relative)["mode"] == 0o750
    assert s18_receipt(target, relative)["created"] is True


@pytest.mark.parametrize("relative,already_composed", S18_BORROWED_CASES)
def test_s18_borrowed_local_mode_drift_conflicts_and_force_keeps_local_mode(
    tmp_path, relative, already_composed
):
    path, initial = s18_seed_borrowed(tmp_path, relative, already_composed)
    assert install_canary(tmp_path, "--bootstrap-baseline", "--no-git").returncode == 0
    path.chmod(0o640)
    before = canary_inventory(tmp_path)
    conflict = install_canary(tmp_path, "--bootstrap-baseline", "--no-git")
    assert conflict.returncode != 0
    assert "local modifications" in conflict.stderr
    assert canary_inventory(tmp_path) == before
    accepted = install_canary(tmp_path, "--bootstrap-baseline", "--no-git", "--force")
    assert accepted.returncode == 0, accepted.stderr
    assert path.stat().st_mode & 0o777 == 0o640
    assert s18_receipt(tmp_path, relative)["mode"] == 0o640
    assert s18_receipt(tmp_path, relative)["created"] is False
    removed = install_canary(tmp_path, "--uninstall", "--no-git")
    assert removed.returncode == 0, removed.stderr
    assert path.read_bytes() == initial
    assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("relative,already_composed", S18_BORROWED_CASES)
@pytest.mark.parametrize("force", [False, True])
def test_s18_borrowed_package_content_change_still_requires_reconciliation(
    tmp_path, relative, already_composed, force
):
    target = tmp_path / "target"
    target.mkdir()
    s18_seed_borrowed(target, relative, already_composed)
    assert install_canary(target, "--bootstrap-baseline", "--no-git").returncode == 0
    package = s18_package_update(tmp_path, relative, content=True)
    before = canary_inventory(target)
    extra = ["--force"] if force else []
    conflict = install_canary(
        target, "--bootstrap-baseline", "--no-git", *extra, package=package
    )
    assert conflict.returncode != 0
    assert "explicit reconciliation" in conflict.stderr
    assert canary_inventory(target) == before
