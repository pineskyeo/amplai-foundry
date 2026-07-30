"""Git plumbing adapter for verified candidate inspection and exact ref CAS."""

from __future__ import annotations

import hashlib
import subprocess
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from amplai_foundry.governance.publish import (
    CandidateCommitEvidence,
    CanonicalBranchRef,
    GitObjectId,
    PublishGovernanceError,
)

_CANONICAL_REF_ADAPTER = TypeAdapter(CanonicalBranchRef)
_GIT_OBJECT_ID_ADAPTER = TypeAdapter(GitObjectId)


class GitCASOutcome(StrEnum):
    UPDATED = "updated"
    EXPECTED_UNCHANGED = "expected_unchanged"
    CONFLICT = "conflict"


class GitPublishAmbiguousError(PublishGovernanceError):
    def __init__(self) -> None:
        super().__init__("PUBLISH_GIT_RESULT_AMBIGUOUS")


class _PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_ref: CanonicalBranchRef
    candidate_commit: GitObjectId


class SubprocessGitPublishBackend:
    """Use Git plumbing only; never mutate canonical working-tree files."""

    def __init__(self, repository: Path, *, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Git timeout은 0보다 커야 합니다.")
        self.repository = repository.expanduser().resolve(strict=True)
        self.timeout_seconds = timeout_seconds
        git_dir = self._run("rev-parse", "--git-dir")
        if not git_dir.stdout.strip():
            raise PublishGovernanceError("PUBLISH_GIT_REPOSITORY_INVALID")

    def read_ref(self, canonical_ref: str) -> str:
        checked_ref = _CANONICAL_REF_ADAPTER.validate_python(canonical_ref)
        completed = self._run("rev-parse", "--verify", checked_ref)
        try:
            return _GIT_OBJECT_ID_ADAPTER.validate_python(completed.stdout.strip())
        except ValidationError as error:
            raise PublishGovernanceError("PUBLISH_GIT_REF_INVALID") from error

    def inspect_candidate(
        self,
        candidate_commit: str,
        *,
        artifact_bytes: bytes,
        publish_request_bytes: bytes,
    ) -> CandidateCommitEvidence:
        checked_candidate = _GIT_OBJECT_ID_ADAPTER.validate_python(candidate_commit)
        try:
            request = _PublishRequest.model_validate_json(publish_request_bytes)
        except (ValidationError, UnicodeDecodeError) as error:
            raise PublishGovernanceError("PUBLISH_REQUEST_INVALID") from error
        if request.candidate_commit != checked_candidate:
            raise PublishGovernanceError("PUBLISH_REQUEST_INVALID")
        resolved = self._run("rev-parse", "--verify", f"{checked_candidate}^{{commit}}")
        if resolved.stdout.strip() != checked_candidate:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_MISMATCH")
        ancestry = self._run("rev-list", "--parents", "-n", "1", checked_candidate)
        parts = ancestry.stdout.strip().split()
        if len(parts) != 2 or parts[0] != checked_candidate:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_PARENT_INVALID")
        parent = _GIT_OBJECT_ID_ADAPTER.validate_python(parts[1])
        tree_listing = self._run_bytes(
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            f"{checked_candidate}^{{tree}}",
        )
        if tree_listing != artifact_bytes:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_TREE_MISMATCH")
        return CandidateCommitEvidence(
            candidate_commit=checked_candidate,
            parent_commit=parent,
            candidate_tree_digest=f"sha256:{hashlib.sha256(tree_listing).hexdigest()}",
            canonical_ref=request.canonical_ref,
        )

    def compare_and_swap_ref(
        self,
        canonical_ref: str,
        *,
        expected_old_ref: str,
        candidate_commit: str,
    ) -> GitCASOutcome:
        checked_ref = _CANONICAL_REF_ADAPTER.validate_python(canonical_ref)
        expected = _GIT_OBJECT_ID_ADAPTER.validate_python(expected_old_ref)
        candidate = _GIT_OBJECT_ID_ADAPTER.validate_python(candidate_commit)
        if len(expected) != len(candidate) or expected == candidate:
            raise ValueError("CAS object identity가 유효하지 않습니다.")
        completed = self._run_unchecked(
            "update-ref",
            "--no-deref",
            checked_ref,
            candidate,
            expected,
        )
        try:
            actual = self.read_ref(checked_ref)
        except PublishGovernanceError as error:
            raise GitPublishAmbiguousError() from error
        if actual == candidate:
            return GitCASOutcome.UPDATED
        if actual == expected:
            return GitCASOutcome.EXPECTED_UNCHANGED
        if completed.returncode != 0:
            return GitCASOutcome.CONFLICT
        raise GitPublishAmbiguousError()

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        completed = self._run_unchecked(*arguments)
        if completed.returncode != 0:
            raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED")
        return completed

    def _run_unchecked(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ("git", "-C", str(self.repository), *arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitPublishAmbiguousError() from error

    def _run_bytes(self, *arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                ("git", "-C", str(self.repository), *arguments),
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED") from error
        if completed.returncode != 0:
            raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED")
        return completed.stdout
