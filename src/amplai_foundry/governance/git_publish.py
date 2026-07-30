"""Git plumbing boundaries for verified candidates and durable-intent ref CAS."""

from __future__ import annotations

import hashlib
import os
import secrets
import select
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from amplai_foundry.governance.legacy_gates import legacy_mutation_block
from amplai_foundry.governance.publish import (
    CandidateCommitEvidence,
    CanonicalBranchRef,
    GitObjectId,
    PublishGovernanceError,
    PublishIntentState,
    PublishIntentView,
    PublishPreparationService,
)
from amplai_foundry.governance.store import GovernanceStore, governance_transaction

_CANONICAL_REF_ADAPTER = TypeAdapter(CanonicalBranchRef)
_GIT_OBJECT_ID_ADAPTER = TypeAdapter(GitObjectId)
_CommandRunner = Callable[
    [tuple[str, ...], bool, float, Mapping[str, str]],
    subprocess.CompletedProcess[Any],
]


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


def _subprocess_runner(
    arguments: tuple[str, ...],
    text: bool,
    timeout: float,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=text,
        timeout=timeout,
        env=environment,
    )


class _SubprocessGitBackend:
    """Private Git command boundary; mutating CAS is coordinator-only."""

    def __init__(
        self,
        repository: Path,
        *,
        timeout_seconds: float = 10.0,
        runner: _CommandRunner = _subprocess_runner,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Git timeout은 0보다 커야 합니다.")
        self.repository = repository.expanduser().resolve(strict=True)
        if not self.repository.is_dir():
            raise PublishGovernanceError("PUBLISH_GIT_REPOSITORY_INVALID")
        self.timeout_seconds = timeout_seconds
        self._runner = runner
        self._environment = self._sanitized_environment()
        git_dir_result = self._bootstrap("rev-parse", "--absolute-git-dir")
        top_level_result = self._bootstrap("rev-parse", "--show-toplevel")
        self.git_dir = Path(git_dir_result.stdout.strip()).resolve(strict=True)
        top_level = Path(top_level_result.stdout.strip()).resolve(strict=True)
        if top_level != self.repository or not self.git_dir.is_dir():
            raise PublishGovernanceError("PUBLISH_GIT_REPOSITORY_INVALID")
        self._repository_identity = self._path_identity(self.repository)
        self._git_dir_identity = self._path_identity(self.git_dir)

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
        object_kind = self._run("cat-file", "-t", checked_candidate).stdout.strip()
        if object_kind != "commit":
            raise PublishGovernanceError("PUBLISH_CANDIDATE_MISMATCH")
        commit_bytes = self._run_bytes(
            "cat-file",
            "commit",
            checked_candidate,
            max_output_bytes=1024 * 1024,
        )
        headers = commit_bytes.split(b"\n\n", 1)[0].splitlines()
        tree_headers = tuple(line[5:] for line in headers if line.startswith(b"tree "))
        parent_headers = tuple(line[7:] for line in headers if line.startswith(b"parent "))
        if len(tree_headers) != 1 or len(parent_headers) != 1:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_PARENT_INVALID")
        try:
            tree_object = _GIT_OBJECT_ID_ADAPTER.validate_python(tree_headers[0].decode("ascii"))
            parent = _GIT_OBJECT_ID_ADAPTER.validate_python(parent_headers[0].decode("ascii"))
        except (UnicodeDecodeError, ValidationError) as error:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_PARENT_INVALID") from error
        if len(tree_object) != len(checked_candidate) or len(parent) != len(checked_candidate):
            raise PublishGovernanceError("PUBLISH_CANDIDATE_PARENT_INVALID")
        tree_listing = self._run_bytes(
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            tree_object,
            max_output_bytes=len(artifact_bytes),
        )
        if tree_listing != artifact_bytes:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_TREE_MISMATCH")
        return CandidateCommitEvidence(
            candidate_commit=checked_candidate,
            parent_commit=parent,
            candidate_tree_digest=f"sha256:{hashlib.sha256(tree_listing).hexdigest()}",
            canonical_ref=request.canonical_ref,
        )

    def _compare_and_swap_ref(
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
        try:
            completed = self._execute(
                (
                    "update-ref",
                    "--no-deref",
                    checked_ref,
                    candidate,
                    expected,
                ),
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitPublishAmbiguousError() from error
        try:
            actual = self.read_ref(checked_ref)
        except PublishGovernanceError as error:
            raise GitPublishAmbiguousError() from error
        if actual == candidate:
            return GitCASOutcome.UPDATED
        if completed.returncode != 0 and actual == expected:
            return GitCASOutcome.EXPECTED_UNCHANGED
        if completed.returncode != 0:
            return GitCASOutcome.CONFLICT
        raise GitPublishAmbiguousError()

    def _bootstrap(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._runner(
                ("git", "-C", str(self.repository), *arguments),
                True,
                self.timeout_seconds,
                self._environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED") from error
        if completed.returncode != 0:
            raise PublishGovernanceError("PUBLISH_GIT_REPOSITORY_INVALID")
        return cast(subprocess.CompletedProcess[str], completed)

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._execute(arguments, text=True)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED") from error
        if completed.returncode != 0:
            raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED")
        return cast(subprocess.CompletedProcess[str], completed)

    def _run_bytes(self, *arguments: str, max_output_bytes: int) -> bytes:
        if max_output_bytes < 1:
            raise PublishGovernanceError("PUBLISH_CANDIDATE_TREE_MISMATCH")
        self._verify_repository_identity()
        command = (
            "git",
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.repository}",
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        )
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._environment,
                start_new_session=True,
            )
        except OSError as error:
            raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED") from error
        assert process.stdout is not None
        output = bytearray()
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, self.timeout_seconds)
                readable, _, _ = select.select((process.stdout,), (), (), remaining)
                if not readable:
                    raise subprocess.TimeoutExpired(command, self.timeout_seconds)
                chunk = os.read(process.stdout.fileno(), min(65_536, max_output_bytes + 1))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_output_bytes:
                    raise PublishGovernanceError("PUBLISH_CANDIDATE_TREE_TOO_LARGE")
            return_code = process.wait(timeout=max(deadline - time.monotonic(), 0.001))
        except subprocess.TimeoutExpired as error:
            self._terminate_process_group(process)
            raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED") from error
        except PublishGovernanceError:
            self._terminate_process_group(process)
            raise
        finally:
            process.stdout.close()
        if return_code != 0:
            raise PublishGovernanceError("PUBLISH_GIT_COMMAND_FAILED")
        return bytes(output)

    def _execute(
        self,
        arguments: tuple[str, ...],
        *,
        text: bool,
    ) -> subprocess.CompletedProcess[Any]:
        self._verify_repository_identity()
        return self._runner(
            (
                "git",
                f"--git-dir={self.git_dir}",
                f"--work-tree={self.repository}",
                "-c",
                "core.hooksPath=/dev/null",
                *arguments,
            ),
            text,
            self.timeout_seconds,
            self._environment,
        )

    def _verify_repository_identity(self) -> None:
        if (
            self._path_identity(self.repository) != self._repository_identity
            or self._path_identity(self.git_dir) != self._git_dir_identity
        ):
            raise PublishGovernanceError("PUBLISH_GIT_REPOSITORY_CHANGED")

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(ProcessLookupError):
            process.kill()
        process.wait()

    @staticmethod
    def _path_identity(path: Path) -> tuple[int, int]:
        try:
            status = path.stat()
        except OSError as error:
            raise PublishGovernanceError("PUBLISH_GIT_REPOSITORY_CHANGED") from error
        return status.st_dev, status.st_ino

    @staticmethod
    def _sanitized_environment() -> dict[str, str]:
        return {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }


class SubprocessGitCandidateInspector:
    """Public read-only inspector used by publish preparation."""

    def __init__(self, repository: Path, *, timeout_seconds: float = 10.0) -> None:
        self._backend = _SubprocessGitBackend(
            repository,
            timeout_seconds=timeout_seconds,
        )

    def read_ref(self, canonical_ref: str) -> str:
        return self._backend.read_ref(canonical_ref)

    def inspect_candidate(
        self,
        candidate_commit: str,
        *,
        artifact_bytes: bytes,
        publish_request_bytes: bytes,
    ) -> CandidateCommitEvidence:
        return self._backend.inspect_candidate(
            candidate_commit,
            artifact_bytes=artifact_bytes,
            publish_request_bytes=publish_request_bytes,
        )


class FencedGitPublishCoordinator:
    """The only public production path that may invoke canonical Git ref CAS."""

    def __init__(
        self,
        store: GovernanceStore,
        repository: Path,
        *,
        coordinator_id: str,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not coordinator_id.strip() or len(coordinator_id) > 128:
            raise ValueError("coordinator_id가 유효하지 않습니다.")
        self.store = store
        self.coordinator_id = coordinator_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backend = _SubprocessGitBackend(
            repository,
            timeout_seconds=timeout_seconds,
        )

    def publish_prepared_ref(self, intent_id: str) -> GitCASOutcome:
        intent, roots, claim_id, claim_fence = self._claim_prepared(intent_id)
        artifact_bytes = cast(bytes, roots[0])
        publish_request_bytes = cast(bytes, roots[1])
        try:
            evidence = self._backend.inspect_candidate(
                intent.candidate_commit,
                artifact_bytes=artifact_bytes,
                publish_request_bytes=publish_request_bytes,
            )
            if (
                evidence.candidate_commit != intent.candidate_commit
                or evidence.parent_commit != intent.expected_old_ref
                or evidence.candidate_tree_digest != intent.candidate_tree_digest
                or evidence.canonical_ref != intent.canonical_ref
            ):
                raise PublishGovernanceError("PUBLISH_CANDIDATE_MISMATCH")
            actual = self._backend.read_ref(intent.canonical_ref)
        except PublishGovernanceError:
            self._release_pre_cas_claim(claim_id, claim_fence)
            raise
        if actual == intent.candidate_commit:
            return GitCASOutcome.UPDATED
        if actual != intent.expected_old_ref:
            return GitCASOutcome.CONFLICT
        return self._compare_and_swap_under_claim(
            intent,
            claim_id=claim_id,
            claim_fence=claim_fence,
        )

    def _compare_and_swap_under_claim(
        self,
        intent: PublishIntentView,
        *,
        claim_id: str,
        claim_fence: int,
    ) -> GitCASOutcome:
        with self.store.connect() as connection, governance_transaction(connection):
            block = legacy_mutation_block(connection, intent.proposal_ref)
            if block is not None:
                raise PublishGovernanceError(block)
            rooted = connection.execute(
                """
                SELECT 1
                FROM governance_publish_claims c
                JOIN governance_publish_intents i ON i.intent_id = c.intent_id
                JOIN governance_project_publish_gates g
                  ON g.project_namespace = i.project_namespace
                 AND g.project_id = i.project_id
                WHERE c.claim_id = ? AND c.intent_id = ?
                  AND c.coordinator_id = ? AND c.claim_fencing_token = ?
                  AND c.state = 'active' AND i.status = 'prepared'
                  AND g.state = 'locked' AND g.active_intent_id = i.intent_id
                """,
                (claim_id, intent.intent_id, self.coordinator_id, claim_fence),
            ).fetchone()
            if rooted is None:
                raise PublishGovernanceError("PUBLISH_CLAIM_STALE")
            actual = self._backend.read_ref(intent.canonical_ref)
            if actual == intent.candidate_commit:
                return GitCASOutcome.UPDATED
            if actual != intent.expected_old_ref:
                return GitCASOutcome.CONFLICT
            return self._backend._compare_and_swap_ref(
                intent.canonical_ref,
                expected_old_ref=intent.expected_old_ref,
                candidate_commit=intent.candidate_commit,
            )

    def _claim_prepared(
        self,
        intent_id: str,
    ) -> tuple[PublishIntentView, tuple[object, ...], str, int]:
        with self.store.connect() as connection, governance_transaction(connection):
            intent = PublishPreparationService._intent_view(connection, intent_id)
            block = legacy_mutation_block(connection, intent.proposal_ref)
            if block is not None:
                raise PublishGovernanceError(block)
            roots = connection.execute(
                """
                SELECT a.artifact_bytes, p.publish_request_bytes, g.state,
                       g.active_intent_id, g.canonical_ref
                FROM governance_publish_intents i
                JOIN governance_staging_artifacts a
                  ON a.job_id = i.job_id AND a.fencing_token = i.fencing_token
                 AND a.artifact_digest = i.staged_artifact_digest
                JOIN governance_publish_inputs p
                  ON p.job_id = i.job_id AND p.fencing_token = i.fencing_token
                 AND p.publish_request_digest = i.publish_request_digest
                JOIN governance_project_publish_gates g
                  ON g.project_namespace = i.project_namespace
                 AND g.project_id = i.project_id
                WHERE i.intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            self._verify_prepared_roots(intent, roots)
            active = connection.execute(
                """
                SELECT 1 FROM governance_publish_claims
                WHERE intent_id = ? AND state = 'active'
                """,
                (intent_id,),
            ).fetchone()
            if active is not None:
                raise PublishGovernanceError("PUBLISH_CLAIM_ACTIVE")
            sequence = connection.execute(
                """
                SELECT COALESCE(MAX(claim_fencing_token), 0) + 1
                FROM governance_publish_claims WHERE intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            claim_fence = int(sequence[0])
            claim_id = f"PCL-{secrets.token_hex(8).upper()}"
            connection.execute(
                """
                INSERT INTO governance_publish_claims(
                    claim_id, intent_id, project_namespace, project_id,
                    coordinator_id, claim_fencing_token, state, claimed_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL)
                """,
                (
                    claim_id,
                    intent_id,
                    intent.proposal_ref.project_ref.namespace,
                    intent.proposal_ref.project_ref.project_id,
                    self.coordinator_id,
                    claim_fence,
                    self._timestamp(self._clock()),
                ),
            )
            return intent, cast(tuple[object, ...], roots), claim_id, claim_fence

    def _release_pre_cas_claim(self, claim_id: str, claim_fence: int) -> None:
        with self.store.connect() as connection, governance_transaction(connection):
            updated = connection.execute(
                """
                UPDATE governance_publish_claims
                SET state = 'released', resolved_at = ?
                WHERE claim_id = ? AND coordinator_id = ?
                  AND claim_fencing_token = ? AND state = 'active'
                """,
                (
                    self._timestamp(self._clock()),
                    claim_id,
                    self.coordinator_id,
                    claim_fence,
                ),
            )
            if updated.rowcount != 1:
                raise PublishGovernanceError("PUBLISH_CLAIM_STALE")

    @staticmethod
    def _verify_prepared_roots(
        intent: PublishIntentView,
        roots: tuple[object, ...] | None,
    ) -> None:
        if (
            intent.status is not PublishIntentState.PREPARED
            or roots is None
            or str(roots[2]) != "locked"
            or str(roots[3]) != intent.intent_id
            or str(roots[4]) != intent.canonical_ref
        ):
            raise PublishGovernanceError("PUBLISH_INTENT_NOT_PREPARED")

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock은 timezone-aware datetime을 반환해야 합니다.")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
