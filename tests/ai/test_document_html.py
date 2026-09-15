"""S05 deterministic, release-bound, audience-safe offline view contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ai import test_document_lifecycle as docs
from ai import test_document_review as review_checks

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def engine():
    spec = importlib.util.spec_from_file_location(
        "document_html_canary", ROOT / "scripts/amplai_docs.py"
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    assert callable(getattr(value, "build_view", None)), (
        "S05 requires the actual offline view engine"
    )
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def verify_fixture_source(root, version):
    check = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import runpy,sys; assert runpy.run_path(sys.argv[1])['VERSION'] == int(sys.argv[2]); "
            "print('Verified synthetic fixture VERSION=' + sys.argv[2])",
            str(root / "src/api.py"),
            str(version),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert check.returncode == 0, check.stderr
    evidence = root / "specs/canary/release-check.txt"
    evidence.write_text(check.stdout)
    return check, evidence


def fixture(engine, tmp_path, *, security="INTERNAL", body=None, linked=None, **metadata):
    root = docs.repository(tmp_path)
    docs.prepare_loop(root)
    if linked:
        docs.document(root, **linked)
    docs.document(
        root,
        security=security,
        body=body
        or (
            "# Guide\n\n## Deploy\n\nCheck the configured version.\n\n```sh\nprintf fixture\n```\n"
            "\n## Recover\n\nRestore the previously verified bundle.\n"
        ),
        **metadata,
    )
    docs.reviewed(engine, root)
    record = engine.read_document(root, "docs/guide.md")
    review = json.loads((root / ".ai-team/knowledge/document-reviews.json").read_text())["reviews"][
        0
    ]
    release = {
        "schema_version": "1.0",
        "release_id": "release-2",
        "status": "verified",
        "security": security,
        "audiences": metadata.get("audiences", ["operator"]),
        "components": {
            name: {
                "revision": "fixture-2",
                "path": "src/api.py",
                "sha256": sha((root / "src/api.py").read_bytes()),
            }
            for name in ("ui", "backend", "contracts", "ontology", "kit")
        },
        "documents": [
            {
                "doc_id": "canary:guide",
                "source_sha256": record["source_sha256"],
                "snapshot_sha256": engine.object_digest(record["snapshot"]),
                "review_sha256": engine.object_digest(review),
            }
        ],
    }
    check, evidence = verify_fixture_source(root, 1)
    proof = {
        "schema_version": "1.0",
        "verdict": "PASS",
        "input_sha256": engine.object_digest(release),
        "checks": [
            {
                "id": "synthetic-local-fixture",
                "exit_code": check.returncode,
                "evidence": {
                    "path": "specs/canary/release-check.txt",
                    "sha256": sha(evidence.read_bytes()),
                },
            }
        ],
    }
    docs.put_json(root / "specs/canary/release-proof.json", proof)
    release["verification"] = {
        "path": "specs/canary/release-proof.json",
        "sha256": sha((root / "specs/canary/release-proof.json").read_bytes()),
    }
    docs.put_json(root / "specs/canary/release.json", release)
    return root, "specs/canary/release.json"


def inventory(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_same_reviewed_source_builds_identical_bytes_and_idempotent_retry(engine, tmp_path):
    root, release = fixture(engine, tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    result = engine.build_view(root, release, one, audience="operator")
    assert result["complete"] is True
    engine.build_view(root, release, two, audience="operator")
    assert inventory(one) == inventory(two)
    assert engine.build_view(root, release, one, audience="operator") == result
    assert engine.validate_view(root, release, one, audience="operator")["valid"]
    assert (one / "index.html").exists()
    assert all("timestamp" not in p and "sourcemap" not in p for p in inventory(one))


@pytest.mark.parametrize("drift", ["html", "extra", "source", "evidence", "component", "review"])
def test_source_or_generated_drift_cannot_validate_or_overwrite_old_view(engine, tmp_path, drift):
    root, release = fixture(engine, tmp_path)
    output = tmp_path / "site"
    engine.build_view(root, release, output, audience="operator")
    if drift == "html":
        (output / "index.html").write_text("Manually edited generated output")
    elif drift == "extra":
        (output / "extra.txt").write_text("Untracked output")
    elif drift == "source":
        path = root / "docs/guide.md"
        path.write_text(path.read_text() + "\nChanged operating procedure.\n")
    elif drift == "evidence":
        (root / "specs/canary/release-check.txt").write_text("Changed execution evidence")
    elif drift == "component":
        (root / "src/api.py").write_text("VERSION = 77\n")
    else:
        path = root / ".ai-team/knowledge/document-reviews.json"
        value = json.loads(path.read_text())
        value["reviews"][0]["reviewer"] = "Another reviewer"
        docs.put_json(path, value)
    before = inventory(output)
    assert not engine.validate_view(root, release, output, audience="operator")["valid"]
    with pytest.raises((engine.DocumentError, OSError)):
        engine.build_view(root, release, output, audience="operator")
    assert inventory(output) == before


def renderer_only_xss_files(engine, tmp_path):
    """Exercise the renderer beneath intake, not fabricated publication approval.

    S15 rejects active embedded languages at intake. The renderer still needs its
    own escaping/unsafe-link tests: rejection must not replace those assertions.
    """
    root, release = fixture(engine, tmp_path)
    value = engine.prepare_view_input(root, release, "operator")
    value["documents"][0]["title"] = (
        'Title <img src=https://example.invalid/inert onerror="alert(1)">'
    )
    value["documents"][0]["body"] = (
        "# Guide\n\n```html\n<script>window.BAD=1</script>\n"
        '<iframe src="https://example.invalid"></iframe>\n```\n\n'
        "[click](javascript:alert)\n\n## Deploy\n\nUse the verified bundle.\n\n"
        "## Recover\n\nKeep the previous bundle.\n"
    )
    return engine.render_view_files(value)


@pytest.mark.parametrize("surface", ["body", "title", "scheme"])
def test_active_source_forms_are_rejected_before_bundle_creation(engine, tmp_path, surface):
    output = tmp_path / "site"
    with pytest.raises(engine.DocumentError, match="UNSUPPORTED_PARSER"):
        root, release = fixture(
            engine,
            tmp_path,
            title='Title <img src=https://example.invalid/inert onerror="alert(1)">'
            if surface == "title"
            else "Title",
            body="<script>window.BAD=1</script>"
            if surface == "body"
            else "[click](javascript:alert)"
            if surface == "scheme"
            else "# Safe body\n",
        )
        engine.build_view(root, release, output, audience="operator")
    assert not output.exists()


def test_raw_html_title_code_and_unsafe_link_are_inert_offline_text(engine, tmp_path):
    files = renderer_only_xss_files(engine, tmp_path)
    output = tmp_path / "site"
    output.mkdir()
    for name, data in files.items():
        (output / name).write_bytes(data)
    content = "\n".join(p.read_text() for p in output.glob("*.html"))
    assert "&lt;script&gt;window.BAD=1&lt;/script&gt;" in content
    assert "<script" not in content.lower()
    assert "<iframe" not in content.lower()
    assert "<img" not in content.lower()
    assert 'href="javascript:' not in content.lower()
    assert 'src="http' not in content.lower()
    assert "Content-Security-Policy" in content
    assert "@media print" in content
    assert 'name="viewport"' in content
    assert 'href="#content"' in content


def test_audience_sections_preserve_runbook_procedure_not_a_report_template(engine, tmp_path):
    root, release = fixture(
        engine,
        tmp_path,
        audiences=["operator", "developer"],
        sections={"operator": ["Deploy", "Recover"], "developer": ["Build"]},
        body=(
            "# Guide\n\n## Deploy\nOperator deployment command.\n\n## Recover\n"
            "Operator rollback command.\n\n## Build\nDeveloper build command.\n"
        ),
    )
    operator, developer = tmp_path / "operator", tmp_path / "developer"
    engine.build_view(root, release, operator, audience="operator")
    engine.build_view(root, release, developer, audience="developer")
    op = "\n".join(p.read_text() for p in operator.glob("*.html"))
    dev = "\n".join(p.read_text() for p in developer.glob("*.html"))
    assert "Operator deployment command." in op and "Operator rollback command." in op
    assert "Developer build command." not in op
    assert "Developer build command." in dev and "Operator deployment command." not in dev


@pytest.mark.parametrize(
    "fault", ["unverified", "missing_component", "missing_check", "wrong_proof"]
)
def test_version_label_or_pass_label_alone_is_not_a_verified_release(engine, tmp_path, fault):
    root, release = fixture(engine, tmp_path)
    path = root / release
    value = json.loads(path.read_text())
    if fault == "unverified":
        value["status"] = "candidate"
    elif fault == "missing_component":
        del value["components"]["ontology"]
    elif fault == "missing_check":
        proof_path = root / value["verification"]["path"]
        proof = json.loads(proof_path.read_text())
        proof["checks"] = []
        docs.put_json(proof_path, proof)
        value["verification"]["sha256"] = sha(proof_path.read_bytes())
    else:
        value["verification"]["sha256"] = "0" * 64
    docs.put_json(path, value)
    with pytest.raises(engine.DocumentError):
        engine.build_view(root, release, tmp_path / "site", audience="operator")
    assert not (tmp_path / "site").exists()


def test_external_builder_receives_only_allowlisted_input_and_requires_separate_digest(
    engine, tmp_path
):
    root, release = fixture(engine, tmp_path, security="PUBLIC")
    docs.document(
        root,
        "docs/PRIVATE_CANARY.md",
        doc_id="canary:private",
        topic_id="canary:private",
        security="RESTRICTED",
        title="PRIVATE_TITLE_CANARY",
        dependencies=[],
        body="PRIVATE_BODY_CANARY",
    )
    prepared = engine.prepare_view_input(root, release, audience="operator", security="PUBLIC")
    serialized = json.dumps(prepared, ensure_ascii=False)
    assert "PRIVATE_" not in serialized
    approved = engine.object_digest(prepared)
    source = tmp_path / "approved-input"
    source.mkdir()
    docs.put_json(source / "approved-input.json", prepared)
    output = tmp_path / "external"
    result = engine.build_approved_view(source, output, approved_sha256=approved)
    assert result["complete"] is True
    assert "PRIVATE_" not in "\n".join(p.read_text() for p in output.rglob("*") if p.is_file())
    assert not (source / ".git").exists()
    with pytest.raises(engine.DocumentError):
        engine.build_approved_view(source, tmp_path / "wrong", approved_sha256="0" * 64)
    (source / "unapproved.txt").write_text("PRIVATE_EXTRA_CANARY")
    with pytest.raises(engine.DocumentError):
        engine.build_approved_view(source, tmp_path / "extra", approved_sha256=approved)


def test_internal_checkout_cannot_be_used_as_external_builder_input(engine, tmp_path):
    root, release = fixture(engine, tmp_path)
    with pytest.raises(engine.DocumentError):
        engine.prepare_view_input(root, release, audience="operator", security="PUBLIC")
    with pytest.raises(engine.DocumentError):
        engine.build_view(
            root, release, tmp_path / "public", audience="operator", security="PUBLIC"
        )


def test_latest_pointer_only_resolves_explicit_verified_release(engine, tmp_path):
    root, release = fixture(engine, tmp_path)
    value = {
        "schema_version": "1.0",
        "latest": "release-2",
        "releases": [
            {
                "release_id": "release-2",
                "path": release,
                "sha256": sha((root / release).read_bytes()),
            }
        ],
    }
    docs.put_json(root / "specs/canary/releases.json", value)
    selected = engine.resolve_release(root, "specs/canary/releases.json", "latest")
    assert selected == release
    value["latest"] = "unverified-head"
    docs.put_json(root / "specs/canary/releases.json", value)
    with pytest.raises(engine.DocumentError):
        engine.resolve_release(root, "specs/canary/releases.json", "latest")


def test_conflicting_active_owner_cannot_hide_outside_release_manifest(engine, tmp_path):
    root, release = fixture(engine, tmp_path)
    docs.document(root, "docs/duplicate.md", doc_id="canary:duplicate")
    with pytest.raises(engine.DocumentError, match="CANONICAL_CONFLICT"):
        engine.build_view(root, release, tmp_path / "site", audience="operator")


@pytest.mark.parametrize(
    "body,sections",
    [
        ("## Deploy\nFirst.\n## Deploy\nSecond.\n", {"operator": ["Deploy"]}),
        ("```md\n## Deploy\n```\n## Actual\nInstructions.\n", {"operator": ["Deploy"]}),
        ("## Deploy\nInstructions.\n", {"operator": ["Missing"]}),
        ("## Deploy\nInstructions.\n", {"developer": ["Deploy"]}),
    ],
)
def test_invalid_or_ambiguous_audience_section_selection_fails(engine, tmp_path, body, sections):
    root, release = fixture(engine, tmp_path, body=body, sections=sections)
    with pytest.raises(engine.DocumentError, match="INVALID_AUDIENCE_SECTIONS"):
        engine.build_view(root, release, tmp_path / "site", audience="operator")


def test_history_requires_opt_in_and_is_visibly_not_current_guidance(engine, tmp_path):
    root, release = fixture(engine, tmp_path, lifecycle="archived", authority="historical")
    with pytest.raises(engine.DocumentError):
        engine.build_view(root, release, tmp_path / "current", audience="operator")
    output = tmp_path / "history"
    engine.build_view(root, release, output, audience="operator", history=True)
    assert all("not current operating guidance" in p.read_text() for p in output.glob("*.html"))
    assert not engine.validate_view(root, release, output, audience="operator")["valid"]


@pytest.mark.parametrize("kind", ["agent_trace", "work_memory", "handoff", "raw_evidence"])
def test_internal_execution_records_are_not_audience_guides(engine, tmp_path, kind):
    root, release = fixture(engine, tmp_path, kind=kind)
    with pytest.raises(engine.DocumentError, match="INTERNAL_TRACE_NOT_A_DOCUMENT"):
        engine.build_view(root, release, tmp_path / "site", audience="operator")


def test_source_anchor_and_markdown_table_remain_usable_in_generated_page(engine, tmp_path):
    root, release = fixture(
        engine,
        tmp_path,
        body=(
            "# Guide\n\n[Deploy](#deploy)\n\n## Deploy\n\n| Command | Safety |\n"
            "| --- | --- |\n| `check` | Read-only |\n"
        ),
    )
    output = tmp_path / "site"
    engine.build_view(root, release, output, audience="operator")
    page = next(output.glob("document-*.html")).read_text()
    assert 'href="#section-deploy"' in page and 'id="section-deploy"' in page
    assert '<th scope="col">Command</th>' in page
    assert "<td>Read-only</td>" in page


def test_document_heading_cannot_collide_with_skip_link_target(engine, tmp_path):
    from html.parser import HTMLParser

    root, release = fixture(
        engine, tmp_path, body="# Guide\n\n[Content](#content)\n\n## Content\nProcedure.\n"
    )
    output = tmp_path / "site"
    engine.build_view(root, release, output, audience="operator")
    identities = []
    links = []

    class Reader(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if "id" in attributes:
                identities.append(attributes["id"])
            if tag == "a":
                links.append(attributes.get("href", ""))

    Reader().feed(next(output.glob("document-*.html")).read_text())
    assert len(identities) == len(set(identities))
    assert "#content" in links  # Reserved skip link.
    assert "#section-content" in links  # Owner's source heading, distinct target.


def test_existing_empty_directory_and_symlink_destination_are_not_replaced(engine, tmp_path):
    root, release = fixture(engine, tmp_path)
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(engine.DocumentError):
        engine.build_view(root, release, output, audience="operator")
    assert list(output.iterdir()) == []
    link = tmp_path / "link"
    link.symlink_to(output, target_is_directory=True)
    with pytest.raises(engine.DocumentError):
        engine.build_view(root, release, link, audience="operator")
    assert link.is_symlink() and list(output.iterdir()) == []


def test_valid_bundle_cannot_validate_through_output_directory_alias(engine, tmp_path):
    root, release = fixture(engine, tmp_path)
    output = tmp_path / "site"
    engine.build_view(root, release, output, audience="operator")
    alias = tmp_path / "alias"
    alias.symlink_to(output, target_is_directory=True)
    assert not engine.validate_view(root, release, alias, audience="operator")["valid"]


@pytest.mark.parametrize(
    "fault", ["bundle_alias", "file_alias", "duplicate_key", "internal", "extra_directory"]
)
def test_approved_external_input_is_finite_nofollow_and_strict(engine, tmp_path, fault):
    root, release = fixture(engine, tmp_path, security="PUBLIC")
    value = engine.prepare_view_input(root, release, audience="operator", security="PUBLIC")
    bundle = tmp_path / "approved"
    bundle.mkdir()
    docs.put_json(bundle / "approved-input.json", value)
    approval = engine.object_digest(value)
    if fault == "bundle_alias":
        alias = tmp_path / "alias"
        alias.symlink_to(bundle, target_is_directory=True)
        bundle = alias
    elif fault == "file_alias":
        (bundle / "approved-input.json").rename(tmp_path / "outside.json")
        (bundle / "approved-input.json").symlink_to(tmp_path / "outside.json")
    elif fault == "duplicate_key":
        source = json.dumps(value)
        (bundle / "approved-input.json").write_text('{"schema_version":"1.0",' + source[1:])
    elif fault == "internal":
        value["security"] = "INTERNAL"
        approval = engine.object_digest(value)
        docs.put_json(bundle / "approved-input.json", value)
    else:
        (bundle / ".hidden").mkdir()
    with pytest.raises(engine.DocumentError):
        engine.build_approved_view(bundle, tmp_path / "output", approval)
    assert not (tmp_path / "output").exists()


def test_source_drift_after_first_write_removes_only_own_unchanged_files(
    engine, tmp_path, monkeypatch
):
    root, release = fixture(engine, tmp_path)
    output = tmp_path / "site"
    original = engine.os.fsync
    fired = False

    def inject(fd):
        nonlocal fired
        if output.exists() and not fired:
            fired = True
            (output / "user.txt").write_text("User-owned addition")
            (root / "src/api.py").write_text("Changed source during build")
        return original(fd)

    monkeypatch.setattr(engine.os, "fsync", inject)
    with pytest.raises(engine.DocumentError):
        engine.build_view(root, release, output, audience="operator")
    assert inventory(output) == {"user.txt": b"User-owned addition"}


@pytest.mark.parametrize("installed", [False, True], ids=["source", "sealed-install"])
def test_normal_cli_workflow_builds_and_validates_configured_views(engine, tmp_path, installed):
    root, release = fixture(engine, tmp_path)
    if installed:
        root = install_fixture_inputs(root, tmp_path / "installed-workflow")
    output = root / "specs/canary/html"
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["offline_views"] = {
        "schema_version": "1.0",
        "entries": [
            {
                "release_manifest": release,
                "output": "specs/canary/html",
                "audience": "operator",
                "security": "INTERNAL",
                "history": False,
            }
        ],
    }
    docs.put_json(policy_path, policy)
    review_checks.git(root, "init", "--quiet", "-b", "main")
    review_checks.git(root, "add", "--all")
    review_checks.git(root, "commit", "--quiet", "-m", "Fixture view configuration baseline")
    # An actual feature change must flow through discovery and attributed review,
    # not an empty-work NOT_APPLICABLE result or direct fixture approval shortcut.
    (root / "src/api.py").write_text("VERSION = 2\n")
    verify_fixture_source(root, 2)
    impact = docs.loop_command(root, "docs", "impact", "specs/canary")
    assert impact.returncode == 3, impact.stdout + impact.stderr
    report = json.loads(impact.stdout)
    assert {x["path"] for x in report["impacted_documents"]} == {"docs/guide.md"}
    reviewed = docs.loop_command(
        root,
        "docs",
        "review",
        "specs/canary",
        "--document",
        "docs/guide.md",
        "--outcome",
        "reviewed_unchanged",
        "--reason",
        "Guide uses the verified release set, not a literal VERSION; fixture VERSION=2 passed.",
        "--reviewer",
        "fixture-operator",
        "--method",
        "Exact source check and runbook comparison",
        "--evidence",
        "specs/canary/release-check.txt",
        "--snapshot",
        report["dependency_snapshot_hash"],
    )
    assert reviewed.returncode == 0, reviewed.stdout + reviewed.stderr
    assert docs.loop_command(root, "docs", "sync-views").returncode != 0
    refresh_release(engine, root, release)
    missing = docs.loop_command(root, "docs", "validate", "specs/canary")
    assert missing.returncode == 1
    result = docs.loop_command(root, "docs", "sync-views", "specs/canary")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["checked"] == 1
    assert docs.loop_command(root, "docs", "validate", "specs/canary").returncode == 0
    (output / "index.html").write_text("Manually edited generated content")
    assert docs.loop_command(root, "docs", "validate", "specs/canary").returncode == 1
    assert docs.loop_command(root, "docs", "sync-views").returncode != 0


def refresh_release(engine, root, release):
    record = engine.read_document(root, "docs/guide.md")
    review = json.loads((root / ".ai-team/knowledge/document-reviews.json").read_text())["reviews"][
        0
    ]
    value = json.loads((root / release).read_text())
    for component in value["components"].values():
        component["revision"] = "fixture-version-2"
        component["sha256"] = sha((root / component["path"]).read_bytes())
    value["documents"] = [
        {
            "doc_id": "canary:guide",
            "source_sha256": record["source_sha256"],
            "snapshot_sha256": engine.object_digest(record["snapshot"]),
            "review_sha256": engine.object_digest(review),
        }
    ]
    proof_path = root / value["verification"]["path"]
    proof = json.loads(proof_path.read_text())
    for check in proof["checks"]:
        check["evidence"]["sha256"] = sha((root / check["evidence"]["path"]).read_bytes())
    proof["input_sha256"] = engine.object_digest(
        {k: v for k, v in value.items() if k != "verification"}
    )
    docs.put_json(proof_path, proof)
    value["verification"]["sha256"] = sha(proof_path.read_bytes())
    docs.put_json(root / release, value)


def test_isolated_browser_offline_noscript_keyboard_mobile_and_print(engine, tmp_path):
    root, release = fixture(
        engine,
        tmp_path,
        title='Runbook <img src=https://example.invalid/inert alt="inert">',
        body=(
            '# Guide\n\n<iframe src="https://example.invalid"></iframe>\n\n## Deploy\n\n'
            "Use the verified bundle.\n\n| Command | Safety |\n| --- | --- |\n"
            "| `check` | Read-only |\n\n## Recover\n\nKeep the previous bundle.\n"
        ),
    )
    docs.document(
        root,
        "docs/private.md",
        doc_id="canary:private",
        topic_id="canary:private",
        security="RESTRICTED",
        title="PRIVATE_TITLE_CANARY",
        body="PRIVATE_BODY_CANARY",
    )
    output = tmp_path / "site"
    engine.build_view(root, release, output, audience="operator")
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tools/amplai-loop-kit/tests/document-browser.mjs"),
            str(output),
            str(tmp_path / "browser-evidence"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["verdict"] == "PASS"

    # Independent renderer-only adversarial fixture; not a source-release validation.
    fixture_root = tmp_path / "renderer-fixture"
    fixture_root.mkdir()
    renderer_output = tmp_path / "renderer-only"
    renderer_output.mkdir()
    for name, data in renderer_only_xss_files(engine, fixture_root).items():
        (renderer_output / name).write_bytes(data)
    renderer_check = subprocess.run(
        [
            "node",
            str(ROOT / "tools/amplai-loop-kit/tests/document-browser.mjs"),
            str(renderer_output),
            str(tmp_path / "renderer-browser-evidence"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert renderer_check.returncode == 0, renderer_check.stdout + renderer_check.stderr
    assert json.loads(renderer_check.stdout)["verdict"] == "PASS"


def install_fixture_inputs(source, target):
    target.mkdir()
    installed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "tools/amplai-loop-kit/install.py"),
            "--target",
            str(target),
            "--bootstrap-baseline",
            "--app-id",
            "html-canary",
            "--project-id",
            "canary",
            "--no-git",
        ],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    doctor = docs.loop_command(target, "doctor")
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    # Copy only synthetic owner inputs; installed scripts remain package-owned.
    for name in ("docs", "src", "evidence", "specs"):
        shutil.copytree(source / name, target / name, dirs_exist_ok=True)
    for name in (
        "policy/documentation.json",
        "runtime/repository-profile.json",
        "knowledge/document-reviews.json",
    ):
        shutil.copyfile(source / ".ai-team" / name, target / ".ai-team" / name)
    for name in ("amplai_docs.py", "loopctl.py"):
        assert (target / "scripts" / name).read_bytes() == (ROOT / "scripts" / name).read_bytes()
    return target


def test_actual_sealed_install_builds_and_checks_views_without_source_checkout(engine, tmp_path):
    source, release = fixture(engine, tmp_path)
    target = install_fixture_inputs(source, tmp_path / "installed")
    output = target / "specs/canary/html"
    args = ("--release-manifest", release, "--output", str(output), "--audience", "operator")
    built = docs.loop_command(target, "docs", "build", *args)
    assert built.returncode == 0, built.stdout + built.stderr
    assert docs.loop_command(target, "docs", "validate-view", *args).returncode == 0
    (output / "index.html").write_text("Unexpected generated change")
    assert docs.loop_command(target, "docs", "validate-view", *args).returncode == 1


@pytest.mark.parametrize(
    "target", ["PRIVATE.md", "%50RIVATE.md", "PRIVATE.md?raw=1#hidden", "<PRIVATE.md>"]
)
def test_html_private_reference_guard_uses_the_same_normalized_destination(
    engine, tmp_path, target
):
    root, release = fixture(
        engine,
        tmp_path,
        security="PUBLIC",
        body="# Guide\n[PRIVATE_TITLE_CANARY](" + target + ")\n",
        linked={
            "path": "docs/PRIVATE.md",
            "doc_id": "canary:private",
            "topic_id": "canary:private",
            "security": "RESTRICTED",
            "title": "PRIVATE_TITLE_CANARY",
        },
    )
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_VIEW_REFERENCE") as error:
        engine.prepare_view_input(root, release, audience="operator", security="PUBLIC")
    assert "PRIVATE_TITLE_CANARY" not in str(error.value)
    assert not (tmp_path / "site").exists()


@pytest.mark.parametrize(
    "target", ["%67uide.md#deploy", "<guide.md#deploy>", 'guide.md#deploy "Deploy"']
)
def test_html_normalized_current_links_remain_navigable(engine, tmp_path, target):
    root, release = fixture(
        engine, tmp_path, security="PUBLIC", body="# Guide\n\n## Deploy\n[Deploy](" + target + ")\n"
    )
    output = tmp_path / "site"
    prepared = engine.prepare_view_input(root, release, audience="operator", security="PUBLIC")
    approved = tmp_path / "approved-input"
    approved.mkdir()
    docs.put_json(approved / "approved-input.json", prepared)
    engine.build_approved_view(approved, output, approved_sha256=engine.object_digest(prepared))
    content = "\n".join(path.read_text() for path in output.glob("*.html"))
    assert '.html#section-deploy"' in content
    assert engine.validate_view(root, release, output, audience="operator", security="PUBLIC")[
        "valid"
    ]
