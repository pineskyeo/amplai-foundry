from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from amplai_foundry.governance import (
    GitCASOutcome,
    GitPublishAmbiguousError,
    PublishGovernanceError,
    SubprocessGitCandidateInspector,
)
from amplai_foundry.governance.git_publish import _SubprocessGitBackend


def _git(repository: Path, *arguments: str, text: bool = True):
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=text,
    )


def _repository_fixture(tmp_path: Path) -> tuple[Path, str, str, bytes, bytes]:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "AMPLAI Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    tracked = repository / "artifact.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(repository, "add", "artifact.txt")
    _git(repository, "commit", "-q", "-m", "base")
    base = _git(repository, "rev-parse", "refs/heads/main").stdout.strip()

    tracked.write_text("candidate\n", encoding="utf-8")
    _git(repository, "add", "artifact.txt")
    tree = _git(repository, "write-tree").stdout.strip()
    candidate = _git(repository, "commit-tree", tree, "-p", base, "-m", "candidate").stdout.strip()
    artifact = _git(
        repository,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        f"{candidate}^{{tree}}",
        text=False,
    ).stdout
    request = json.dumps(
        {"candidate_commit": candidate, "canonical_ref": "refs/heads/main"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return repository, base, candidate, artifact, request


def test_git_backend_inspects_exact_candidate_parent_tree_and_request(tmp_path: Path) -> None:
    repository, base, candidate, artifact, request = _repository_fixture(tmp_path)
    backend = SubprocessGitCandidateInspector(repository)

    evidence = backend.inspect_candidate(
        candidate,
        artifact_bytes=artifact,
        publish_request_bytes=request,
    )

    assert evidence.candidate_commit == candidate
    assert evidence.parent_commit == base
    assert evidence.canonical_ref == "refs/heads/main"
    assert evidence.candidate_tree_digest == f"sha256:{hashlib.sha256(artifact).hexdigest()}"


def test_git_backend_rejects_unrooted_candidate_inputs(tmp_path: Path) -> None:
    repository, _base, candidate, artifact, request = _repository_fixture(tmp_path)
    backend = SubprocessGitCandidateInspector(repository)

    with pytest.raises(PublishGovernanceError, match="PUBLISH_CANDIDATE_TREE_MISMATCH"):
        backend.inspect_candidate(
            candidate,
            artifact_bytes=artifact + b"tamper",
            publish_request_bytes=request,
        )
    wrong_request = json.dumps(
        {"candidate_commit": "f" * 40, "canonical_ref": "refs/heads/main"}
    ).encode()
    with pytest.raises(PublishGovernanceError, match="PUBLISH_REQUEST_INVALID"):
        backend.inspect_candidate(
            candidate,
            artifact_bytes=artifact,
            publish_request_bytes=wrong_request,
        )
    with pytest.raises(PublishGovernanceError, match="PUBLISH_REQUEST_INVALID"):
        backend.inspect_candidate(
            candidate,
            artifact_bytes=artifact,
            publish_request_bytes=b'{"candidate_commit":',
        )
    extra_request = json.dumps(
        {
            "candidate_commit": candidate,
            "canonical_ref": "refs/heads/main",
            "extra": "forbidden",
        }
    ).encode()
    with pytest.raises(PublishGovernanceError, match="PUBLISH_REQUEST_INVALID"):
        backend.inspect_candidate(
            candidate,
            artifact_bytes=artifact,
            publish_request_bytes=extra_request,
        )
    with pytest.raises(PublishGovernanceError, match="PUBLISH_CANDIDATE_TREE_TOO_LARGE"):
        backend.inspect_candidate(
            candidate,
            artifact_bytes=b"x",
            publish_request_bytes=request,
        )


def test_git_ref_cas_updates_only_exact_expected_ref_and_is_reconcilable(tmp_path: Path) -> None:
    repository, base, candidate, _artifact, _request = _repository_fixture(tmp_path)
    backend = _SubprocessGitBackend(repository)
    tracked = repository / "artifact.txt"
    working_bytes = tracked.read_bytes()
    index_tree = _git(repository, "write-tree").stdout.strip()

    first = backend._compare_and_swap_ref(
        "refs/heads/main",
        expected_old_ref=base,
        candidate_commit=candidate,
    )
    replay = backend._compare_and_swap_ref(
        "refs/heads/main",
        expected_old_ref=base,
        candidate_commit=candidate,
    )

    assert first is GitCASOutcome.UPDATED
    assert replay is GitCASOutcome.UPDATED
    assert backend.read_ref("refs/heads/main") == candidate
    assert tracked.read_bytes() == working_bytes
    assert _git(repository, "write-tree").stdout.strip() == index_tree


def test_git_ref_cas_reports_competing_ref_without_overwrite(tmp_path: Path) -> None:
    repository, base, candidate, _artifact, _request = _repository_fixture(tmp_path)
    backend = _SubprocessGitBackend(repository)
    competing_tree = _git(repository, "rev-parse", f"{base}^{{tree}}").stdout.strip()
    competing = _git(
        repository,
        "commit-tree",
        competing_tree,
        "-p",
        base,
        "-m",
        "competing",
    ).stdout.strip()
    _git(repository, "update-ref", "refs/heads/main", competing, base)

    outcome = backend._compare_and_swap_ref(
        "refs/heads/main",
        expected_old_ref=base,
        candidate_commit=candidate,
    )

    assert outcome is GitCASOutcome.CONFLICT
    assert backend.read_ref("refs/heads/main") == competing


def test_git_publish_backend_has_no_working_tree_mutation_commands() -> None:
    implementation = Path(__file__).parents[1] / "src/amplai_foundry/governance/git_publish.py"
    payload = implementation.read_text(encoding="utf-8")
    for forbidden in ('"checkout"', '"reset"', '"merge"', '"commit-tree"'):
        assert forbidden not in payload
    assert "SubprocessGitPublishBackend" not in payload


def test_git_environment_cannot_redirect_bound_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_a, base_a, _candidate, _artifact, _request = _repository_fixture(tmp_path / "a")
    repository_b, base_b, _candidate_b, _artifact_b, _request_b = _repository_fixture(
        tmp_path / "b"
    )
    _git(repository_b, "commit", "--allow-empty", "-q", "-m", "different")
    base_b = _git(repository_b, "rev-parse", "refs/heads/main").stdout.strip()
    assert base_a != base_b
    monkeypatch.setenv("GIT_DIR", str(repository_b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repository_b))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(repository_b / ".git/objects"))

    inspector = SubprocessGitCandidateInspector(repository_a)

    assert inspector.read_ref("refs/heads/main") == base_a


def test_git_candidate_inspection_disables_replace_refs(tmp_path: Path) -> None:
    repository, base, benign, benign_artifact, _request = _repository_fixture(tmp_path)
    tracked = repository / "artifact.txt"
    tracked.write_text("malicious\n", encoding="utf-8")
    _git(repository, "add", "artifact.txt")
    malicious_tree = _git(repository, "write-tree").stdout.strip()
    malicious = _git(
        repository,
        "commit-tree",
        malicious_tree,
        "-p",
        base,
        "-m",
        "malicious",
    ).stdout.strip()
    _git(repository, "replace", malicious, benign)
    request = json.dumps(
        {"candidate_commit": malicious, "canonical_ref": "refs/heads/main"}
    ).encode()
    inspector = SubprocessGitCandidateInspector(repository)

    with pytest.raises(PublishGovernanceError, match="PUBLISH_CANDIDATE_TREE_MISMATCH"):
        inspector.inspect_candidate(
            malicious,
            artifact_bytes=benign_artifact,
            publish_request_bytes=request,
        )


def test_git_candidate_inspection_ignores_legacy_grafts(tmp_path: Path) -> None:
    repository, expected_base, approved, approved_artifact, _request = _repository_fixture(tmp_path)
    other_tree = _git(repository, "rev-parse", f"{expected_base}^{{tree}}").stdout.strip()
    other_parent = _git(repository, "commit-tree", other_tree, "-m", "other root").stdout.strip()
    malicious = _git(
        repository,
        "commit-tree",
        _git(repository, "rev-parse", f"{approved}^{{tree}}").stdout.strip(),
        "-p",
        other_parent,
        "-m",
        "wrong raw parent",
    ).stdout.strip()
    grafts = repository / ".git/info/grafts"
    grafts.write_text(f"{malicious} {expected_base}\n", encoding="ascii")
    request = json.dumps(
        {"candidate_commit": malicious, "canonical_ref": "refs/heads/main"}
    ).encode()
    inspector = SubprocessGitCandidateInspector(repository)

    evidence = inspector.inspect_candidate(
        malicious,
        artifact_bytes=approved_artifact,
        publish_request_bytes=request,
    )

    assert evidence.parent_commit == other_parent
    assert evidence.parent_commit != expected_base


def test_git_ref_cas_disables_reference_transaction_hook(tmp_path: Path) -> None:
    repository, base, candidate, _artifact, _request = _repository_fixture(tmp_path)
    sentinel = repository / "HOOK_MUTATED_WORKTREE"
    hook = repository / ".git/hooks/reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n", encoding="utf-8")
    hook.chmod(0o755)
    backend = _SubprocessGitBackend(repository)

    outcome = backend._compare_and_swap_ref(
        "refs/heads/main",
        expected_old_ref=base,
        candidate_commit=candidate,
    )

    assert outcome is GitCASOutcome.UPDATED
    assert not sentinel.exists()


def test_git_ref_cas_classifies_expected_unchanged_and_missing_candidate(
    tmp_path: Path,
) -> None:
    repository, base, _candidate, _artifact, _request = _repository_fixture(tmp_path)
    backend = _SubprocessGitBackend(repository)

    outcome = backend._compare_and_swap_ref(
        "refs/heads/main",
        expected_old_ref=base,
        candidate_commit="f" * 40,
    )

    assert outcome is GitCASOutcome.EXPECTED_UNCHANGED
    assert backend.read_ref("refs/heads/main") == base


def test_git_ref_cas_timeout_and_contradictory_success_are_ambiguous(
    tmp_path: Path,
) -> None:
    repository, base, candidate, _artifact, _request = _repository_fixture(tmp_path)
    backend = _SubprocessGitBackend(repository)
    original = backend._runner

    def timeout_update(arguments, text, timeout, environment):
        if "update-ref" in arguments:
            raise subprocess.TimeoutExpired(arguments, timeout)
        return original(arguments, text, timeout, environment)

    backend._runner = timeout_update
    with pytest.raises(GitPublishAmbiguousError):
        backend._compare_and_swap_ref(
            "refs/heads/main",
            expected_old_ref=base,
            candidate_commit=candidate,
        )

    def false_success(arguments, text, timeout, environment):
        if "update-ref" in arguments:
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")
        return original(arguments, text, timeout, environment)

    backend._runner = false_success
    with pytest.raises(GitPublishAmbiguousError):
        backend._compare_and_swap_ref(
            "refs/heads/main",
            expected_old_ref=base,
            candidate_commit=candidate,
        )


def test_git_ref_cas_post_update_read_failure_is_ambiguous(tmp_path: Path) -> None:
    repository, base, candidate, _artifact, _request = _repository_fixture(tmp_path)
    backend = _SubprocessGitBackend(repository)
    original = backend._runner
    updated = False

    def fail_readback(arguments, text, timeout, environment):
        nonlocal updated
        if "update-ref" in arguments:
            result = original(arguments, text, timeout, environment)
            updated = True
            return result
        if updated and "rev-parse" in arguments:
            raise subprocess.TimeoutExpired(arguments, timeout)
        return original(arguments, text, timeout, environment)

    backend._runner = fail_readback
    with pytest.raises(GitPublishAmbiguousError):
        backend._compare_and_swap_ref(
            "refs/heads/main",
            expected_old_ref=base,
            candidate_commit=candidate,
        )
    assert _git(repository, "rev-parse", "refs/heads/main").stdout.strip() == candidate


def test_read_only_timeout_is_not_publish_ambiguity(tmp_path: Path) -> None:
    repository, _base, _candidate, _artifact, _request = _repository_fixture(tmp_path)
    inspector = SubprocessGitCandidateInspector(repository)

    def timeout_read(arguments, text, timeout, environment):
        raise subprocess.TimeoutExpired(arguments, timeout)

    inspector._backend._runner = timeout_read
    with pytest.raises(PublishGovernanceError) as captured:
        inspector.read_ref("refs/heads/main")
    assert not isinstance(captured.value, GitPublishAmbiguousError)


def test_candidate_must_have_exactly_one_parent(tmp_path: Path) -> None:
    repository, base, candidate, artifact, _request = _repository_fixture(tmp_path)
    tree = _git(repository, "rev-parse", f"{candidate}^{{tree}}").stdout.strip()
    root = _git(repository, "commit-tree", tree, "-m", "root").stdout.strip()
    merge = _git(
        repository,
        "commit-tree",
        tree,
        "-p",
        base,
        "-p",
        candidate,
        "-m",
        "merge",
    ).stdout.strip()
    inspector = SubprocessGitCandidateInspector(repository)
    for invalid in (root, merge):
        request = json.dumps(
            {"candidate_commit": invalid, "canonical_ref": "refs/heads/main"}
        ).encode()
        with pytest.raises(PublishGovernanceError, match="PUBLISH_CANDIDATE_PARENT_INVALID"):
            inspector.inspect_candidate(
                invalid,
                artifact_bytes=artifact,
                publish_request_bytes=request,
            )


def test_mutating_git_backend_is_private_and_coordinator_only() -> None:
    import amplai_foundry.governance as governance

    assert not hasattr(governance, "_SubprocessGitBackend")
    assert not hasattr(SubprocessGitCandidateInspector, "compare_and_swap_ref")
    implementation = Path(__file__).parents[1] / "src/amplai_foundry/governance"
    production_calls = []
    for source in implementation.glob("*.py"):
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            if "._compare_and_swap_ref(" in line:
                production_calls.append((source.name, number))
    assert len(production_calls) == 1
    assert production_calls[0][0] == "git_publish.py"
