from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from amplai_foundry.governance import (
    GitCASOutcome,
    PublishGovernanceError,
    SubprocessGitPublishBackend,
)


def _git(repository: Path, *arguments: str, text: bool = True):
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=text,
    )


def _repository_fixture(tmp_path: Path) -> tuple[Path, str, str, bytes, bytes]:
    repository = tmp_path / "repository"
    repository.mkdir()
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
    backend = SubprocessGitPublishBackend(repository)

    evidence = backend.inspect_candidate(
        candidate,
        artifact_bytes=artifact,
        publish_request_bytes=request,
    )

    assert evidence.candidate_commit == candidate
    assert evidence.parent_commit == base
    assert evidence.canonical_ref == "refs/heads/main"
    assert evidence.candidate_tree_digest.startswith("sha256:")


def test_git_backend_rejects_unrooted_candidate_inputs(tmp_path: Path) -> None:
    repository, _base, candidate, artifact, request = _repository_fixture(tmp_path)
    backend = SubprocessGitPublishBackend(repository)

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


def test_git_ref_cas_updates_only_exact_expected_ref_and_is_reconcilable(tmp_path: Path) -> None:
    repository, base, candidate, _artifact, _request = _repository_fixture(tmp_path)
    backend = SubprocessGitPublishBackend(repository)
    tracked = repository / "artifact.txt"
    working_bytes = tracked.read_bytes()
    index_tree = _git(repository, "write-tree").stdout.strip()

    first = backend.compare_and_swap_ref(
        "refs/heads/main",
        expected_old_ref=base,
        candidate_commit=candidate,
    )
    replay = backend.compare_and_swap_ref(
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
    backend = SubprocessGitPublishBackend(repository)
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

    outcome = backend.compare_and_swap_ref(
        "refs/heads/main",
        expected_old_ref=base,
        candidate_commit=candidate,
    )

    assert outcome is GitCASOutcome.CONFLICT
    assert backend.read_ref("refs/heads/main") == competing


def test_git_publish_backend_has_no_working_tree_mutation_commands() -> None:
    implementation = Path(__file__).parents[1] / "src/amplai_foundry/governance/git_publish.py"
    payload = implementation.read_text(encoding="utf-8")
    for forbidden in ('"checkout"', '"reset"', '"merge"', '"commit"'):
        assert forbidden not in payload
