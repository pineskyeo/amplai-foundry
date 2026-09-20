"""S20: unsafe optional aliases are not ordinary absent examples."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ai import test_document_html as views
from ai import test_document_lifecycle as docs
from ai import test_document_typed_visibility as typed

typed_engine = typed.typed_engine
ALIAS = "assets/PRIVATE_CANARY.bin"
UNSAFE = (
    "escaping_leaf",
    "escaping_parent",
    "dangling_leaf",
    "dangling_parent",
    "cyclic_leaf",
    "cyclic_parent",
)


def arrange(root, parent, kind, hint="known_absent", fallback=False):
    outside = parent / "outside"
    outside.mkdir()
    (outside / "PRIVATE_CANARY.bin").write_bytes(b"NONSECRET SYNTHETIC FIXTURE ONLY\n")
    assets = root / "assets"
    if kind == "escaping_parent":
        assets.symlink_to(outside, target_is_directory=True)
    elif kind == "dangling_parent":
        assets.symlink_to("missing-directory", target_is_directory=True)
    elif kind == "cyclic_parent":
        assets.symlink_to("assets", target_is_directory=True)
    elif kind == "inside_alias_absent_child":
        (root / "public-targets").mkdir()
        assets.symlink_to("public-targets", target_is_directory=True)
    else:
        assets.mkdir()
        alias = assets / "PRIVATE_CANARY.bin"
        if kind == "escaping_leaf":
            alias.symlink_to(outside / "PRIVATE_CANARY.bin")
        elif kind == "dangling_leaf":
            alias.symlink_to("missing-target.bin")
        elif kind == "cyclic_leaf":
            alias.symlink_to("PRIVATE_CANARY.bin")
        elif kind in ("inside_public", "inside_restricted"):
            (assets / "target.bin").write_bytes(b"IN-REPOSITORY FIXTURE\n")
            alias.symlink_to("target.bin")
    level = "PUBLIC" if kind in ("inside_public", "inside_alias_absent_child") else "RESTRICTED"
    routes = [typed.route("assets", level), typed.route("public-targets", "PUBLIC")]
    if fallback:
        (root / "docs/assets").mkdir()
        (root / "docs" / ALIAS).write_bytes(b"SAFE ALTERNATE FIXTURE\n")
        routes.append(typed.route("docs/assets", "PUBLIC"))
    typed.policy_routes(root, routes, hint)
    docs.document(root, security="PUBLIC", body="# Guide\nOptional example `" + ALIAS + "`.\n")


@pytest.mark.parametrize(
    "kind",
    (
        *UNSAFE,
        "inside_restricted",
        "inside_public",
        "ordinary_absence",
        "inside_alias_absent_child",
    ),
)
@pytest.mark.parametrize("hint", ["known_absent", "ignore_patterns"])
def test_s20_optional_alias_security_all_consumers(typed_engine, tmp_path, kind, hint):
    root = docs.repository(tmp_path)
    arrange(root, tmp_path, kind, hint)
    docs.reviewed(typed_engine, root)
    assert typed_engine.read_document(root, "docs/guide.md")["freshness"] == "verified"
    for consumer in typed_engine.CURRENT_CONSUMERS:
        for history in (False, True):

            def call(consumer=consumer, history=history):
                return typed.select(typed_engine, root, consumer=consumer, history=history)

            if kind in UNSAFE or kind == "inside_restricted":
                typed.assert_denied(typed_engine, call)
            else:
                assert call()["count"] == 1


@pytest.mark.parametrize("kind", UNSAFE)
def test_s20_safe_alternate_cannot_hide_unsafe_candidate(typed_engine, tmp_path, kind):
    root = docs.repository(tmp_path)
    arrange(root, tmp_path, kind, fallback=True)
    docs.reviewed(typed_engine, root)
    typed.assert_denied(typed_engine, lambda: typed.select(typed_engine, root))
    typed.assert_denied(typed_engine, lambda: typed.select(typed_engine, root, history=True))


@pytest.mark.parametrize("position", ["leaf", "parent"])
@pytest.mark.parametrize("kind", ["escaping", "dangling", "cyclic"])
def test_s20_unmatched_glob_literal_fallback_keeps_alias_status(
    typed_engine, tmp_path, position, kind
):
    root = docs.repository(tmp_path)
    assets = root / "assets"
    assets.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "PRIVATE_CANARY.bin").write_bytes(b"SYNTHETIC ONLY\n")
    alias = assets / ("PRIVATE_CANARY[x].bin" if position == "leaf" else "[x]")
    if kind == "escaping":
        target = outside / "PRIVATE_CANARY.bin" if position == "leaf" else outside
    elif kind == "dangling":
        target = "missing-target"
    else:
        target = alias.name
    alias.symlink_to(target, target_is_directory=position == "parent")
    name = str(alias.relative_to(root))
    if position == "parent":
        name += "/PRIVATE_CANARY.bin"
    typed.policy_routes(root, [typed.route("assets", "RESTRICTED")])
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["reference_scan"] = {"known_absent": ["assets/**"]}
    docs.put_json(policy_path, policy)
    docs.document(root, security="PUBLIC", body="# Guide\nOptional example `" + name + "`.\n")
    docs.reviewed(typed_engine, root)
    typed.assert_denied(typed_engine, lambda: typed.select(typed_engine, root))
    typed.assert_denied(typed_engine, lambda: typed.select(typed_engine, root, history=True))


def bind_release(engine, root, release_path):
    record = engine.read_document(root, "docs/guide.md")
    store = json.loads((root / ".ai-team/knowledge/document-reviews.json").read_text())
    review = next(row for row in store["reviews"] if row["doc_id"] == "canary:guide")
    release = json.loads((root / release_path).read_text())
    release["documents"][0] = {
        "doc_id": "canary:guide",
        "source_sha256": record["source_sha256"],
        "snapshot_sha256": engine.release_snapshot_digest(record),
        "review_sha256": engine.object_digest(review),
    }
    verification = release.pop("verification")
    proof = json.loads((root / verification["path"]).read_text())
    proof["input_sha256"] = engine.object_digest(release)
    docs.put_json(root / verification["path"], proof)
    verification["sha256"] = views.sha((root / verification["path"]).read_bytes())
    release["verification"] = verification
    docs.put_json(root / release_path, release)


@pytest.mark.parametrize("kind", ["escaping_leaf", "dangling_parent", "cyclic_leaf"])
@pytest.mark.parametrize("security", ["PUBLIC", "INTERNAL"])
def test_s20_view_input_rejects_optional_unsafe_alias(typed_engine, tmp_path, kind, security):
    root, release_path = views.fixture(typed_engine, tmp_path, security=security)
    arrange(root, tmp_path, kind)
    if security == "INTERNAL":
        docs.document(root, security=security, body="# Guide\nOptional example `" + ALIAS + "`.\n")
    docs.reviewed(typed_engine, root)
    bind_release(typed_engine, root, release_path)
    with pytest.raises(typed_engine.DocumentError) as caught:
        typed_engine.prepare_view_input(root, release_path, audience="operator", security=security)
    assert caught.value.code in ("CROSS_BOUNDARY_REFERENCE", "CROSS_BOUNDARY_VIEW_REFERENCE")
    assert "PRIVATE_CANARY" not in str(caught.value)


@pytest.mark.parametrize("kind", UNSAFE)
def test_s20_installed_cli_rejects_optional_unsafe_alias(tmp_path, kind):
    installed = tmp_path / "installed"
    installed.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(docs.ROOT / "tools/amplai-loop-kit/install.py"),
            "--target",
            str(installed),
            "--bootstrap-baseline",
            "--app-id",
            "alias-canary",
            "--project-id",
            "canary",
            "--no-git",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    source = installed / "scripts/amplai_docs.py"
    assert source.read_bytes() == (docs.ROOT / "scripts/amplai_docs.py").read_bytes()
    fixture_parent = tmp_path / "fixture"
    fixture_parent.mkdir()
    root = docs.repository(fixture_parent)
    arrange(root, fixture_parent, kind)
    for name in ("docs", "src", "assets"):
        origin, target = root / name, installed / name
        if origin.is_symlink():
            target.symlink_to(os.readlink(origin), target_is_directory=True)
        else:
            shutil.copytree(origin, target, symlinks=True)
    destination = installed / ".ai-team/policy/documentation.json"
    shutil.copyfile(root / ".ai-team/policy/documentation.json", destination)
    spec = importlib.util.spec_from_file_location("installed_optional_alias_engine", source)
    engine = importlib.util.module_from_spec(spec)
    exec(compile(source.read_bytes(), str(source), "exec", dont_inherit=True), engine.__dict__)
    assert Path(engine.__file__).resolve() == source.resolve()
    docs.reviewed(engine, installed)
    for command in ("inventory", "query", "index"):
        for history in (False, True):
            args = ["docs", command, "--release", "release-2", "--security", "PUBLIC"]
            if history:
                args.append("--history")
            output = docs.loop_command(installed, *args)
            assert output.returncode != 0, output.stdout
            assert "CROSS_BOUNDARY_REFERENCE" in output.stdout
            assert "PRIVATE_CANARY" not in output.stdout + output.stderr


def test_s20_candidate_prefix_resolution_spends_shared_budget(typed_engine, tmp_path):
    root = docs.repository(tmp_path)
    (root / "assets/a/b/c/d/e/f").mkdir(parents=True)
    typed.policy_routes(root, [typed.route("assets", "PUBLIC")])
    with typed_engine.SourceTree(root) as tree:
        config, _ = typed_engine.configuration(tree)
        config["max_files"] = 6
        typed.assert_denied(
            typed_engine,
            lambda: typed_engine.reference_visibility(
                tree,
                config,
                [],
                "docs/guide.md",
                "Optional example `assets/a/b/c/d/e/f/missing.bin`.",
                "PUBLIC",
            ),
            "SCOPE_INCOMPLETE",
        )


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_s20_resolution_errors_are_not_absence(typed_engine, tmp_path, monkeypatch, error_number):
    root = docs.repository(tmp_path)
    (root / "assets").mkdir()
    target = root / ALIAS
    target.write_bytes(b"PUBLIC SYNTHETIC FIXTURE\n")
    typed.policy_routes(root, [typed.route("assets", "PUBLIC")])
    stat = typed_engine.os.stat

    def failed(path, *args, **kwargs):
        if os.path.abspath(path) == str(target):
            raise OSError(error_number, "PRIVATE_CANARY synthetic failure")
        return stat(path, *args, **kwargs)

    with typed_engine.SourceTree(root) as tree:
        config, _ = typed_engine.configuration(tree)
        with monkeypatch.context() as patch:
            patch.setattr(typed_engine.os, "stat", failed)
            typed.assert_denied(
                typed_engine,
                lambda: typed_engine.reference_visibility(
                    tree,
                    config,
                    [],
                    "docs/guide.md",
                    "Optional example `" + ALIAS + "`.",
                    "PUBLIC",
                ),
            )
