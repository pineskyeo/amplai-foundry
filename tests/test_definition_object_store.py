from __future__ import annotations

import multiprocessing
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from amplai_foundry.domain.identity import MemoryRef, ProjectRef
from amplai_foundry.governance.definitions import (
    ApplyInputDescriptor,
    DefinitionEvidence,
    ProposalDefinitionManifest,
    canonicalize_definition,
)
from amplai_foundry.governance.models import ProposalRef
from amplai_foundry.governance.object_store import (
    DefinitionObjectCollisionError,
    DefinitionObjectIntegrityError,
    DefinitionObjectStoreError,
    ImmutableDefinitionObjectStore,
    sha256_digest,
)

PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
OTHER_PROJECT = ProjectRef(project_id="cortex", namespace="org/default/project/cortex")
PROPOSAL = ProposalRef(
    project_ref=PROJECT,
    proposal_id="PROP-20260730-ABCDEF12",
)
OTHER_PROPOSAL = ProposalRef(
    project_ref=OTHER_PROJECT,
    proposal_id="PROP-20260730-ABCDEF12",
)


def _manifest(*, operation_order: bool = False) -> ProposalDefinitionManifest:
    operation = {"type": "CREATE", "title": "프로젝트 identity"}
    if operation_order:
        operation = {"title": "프로젝트 identity", "type": "CREATE"}
    input_digest = sha256_digest(b"# Draft\n")
    return ProposalDefinitionManifest(
        proposal_ref=PROPOSAL,
        operations=(operation,),
        evidence=(
            DefinitionEvidence(
                source_ref=MemoryRef(
                    namespace="org/default/project/amplai",
                    local_id="SRC-0001",
                ),
                source_digest=f"sha256:{'1' * 64}",
            ),
        ),
        apply_inputs=(
            ApplyInputDescriptor(
                logical_name="draft-DEC-0001.md",
                object_digest=input_digest,
                media_type="text/markdown",
            ),
        ),
        preconditions=({"expected_revision": 1, "target": "DEC-0001"},),
        base_revision="a13d92f",
        validation_policy_ref="policy/proposal-v3",
    )


class FailingBeforePublishStore(ImmutableDefinitionObjectStore):
    def _before_publish(self, temporary: Path, canonical: Path) -> None:
        assert temporary.read_bytes() == b"# Draft\n"
        raise OSError("injected before publish")


class BlockingBeforePublishStore(ImmutableDefinitionObjectStore):
    def __init__(self, project_ref: ProjectRef, root: Path, marker: Path) -> None:
        super().__init__(project_ref, root)
        self.marker = marker

    def _before_publish(self, temporary: Path, canonical: Path) -> None:
        self.marker.write_text("ready", encoding="utf-8")
        time.sleep(60)


def _run_blocking_input_write(root: str, marker: str) -> None:
    store = BlockingBeforePublishStore(PROJECT, Path(root), Path(marker))
    payload = b"# Draft\n"
    store.put_input_object(PROPOSAL, payload, sha256_digest(payload))


def test_canonicalization_v1_is_deterministic_and_binds_all_contract_fields() -> None:
    first = canonicalize_definition(_manifest())
    reordered = canonicalize_definition(_manifest(operation_order=True))

    assert first.digest == reordered.digest
    assert first.digest == "sha256:da63a4092ea271943b08e9370b5422ec34ff5c6a6d2413501b901405f37bbaa2"
    assert first.canonical_bytes == reordered.canonical_bytes
    assert first.canonical_bytes.endswith(b"\n")
    assert first.manifest.definition_digest == first.digest

    changed_manifests = (
        _manifest().model_copy(
            update={
                "proposal_ref": ProposalRef(
                    project_ref=PROJECT,
                    proposal_id="PROP-20260730-12345678",
                )
            }
        ),
        _manifest().model_copy(
            update={"operations": ({"type": "UPDATE", "title": "프로젝트 identity"},)}
        ),
        _manifest().model_copy(
            update={
                "evidence": (
                    _manifest()
                    .evidence[0]
                    .model_copy(update={"source_digest": f"sha256:{'2' * 64}"}),
                )
            }
        ),
        _manifest().model_copy(
            update={
                "apply_inputs": (
                    _manifest().apply_inputs[0].model_copy(update={"logical_name": "other.md"}),
                )
            }
        ),
        _manifest().model_copy(update={"preconditions": ({"expected_revision": 2},)}),
        _manifest().model_copy(update={"base_revision": "b13d92f"}),
        _manifest().model_copy(update={"validation_policy_ref": "policy/strict-v3"}),
    )
    assert all(canonicalize_definition(item).digest != first.digest for item in changed_manifests)


def test_definition_and_input_objects_use_digest_paths_and_idempotent_retry(
    tmp_path: Path,
) -> None:
    store = ImmutableDefinitionObjectStore(PROJECT, tmp_path / ".amplai/proposals")
    definition = canonicalize_definition(_manifest())
    input_bytes = b"# Draft\n"
    input_digest = sha256_digest(input_bytes)

    definition_ref = store.put_definition_object(
        PROPOSAL,
        definition.canonical_bytes,
        definition.digest,
    )
    repeated_ref = store.put_definition_object(
        PROPOSAL,
        definition.canonical_bytes,
        definition.digest,
    )
    input_ref = store.put_input_object(PROPOSAL, input_bytes, input_digest)
    repeated_input_ref = store.put_input_object(PROPOSAL, input_bytes, input_digest)

    assert definition_ref == repeated_ref
    assert input_ref == repeated_input_ref
    assert definition_ref.path.name == f"{definition.digest}.yaml"
    assert input_ref.path.name == f"{input_digest}.md"
    assert store.get_definition_object(PROPOSAL, definition.digest) == definition.canonical_bytes
    assert store.get_input_object(PROPOSAL, input_digest) == input_bytes


def test_concurrent_same_object_writes_publish_one_complete_object(tmp_path: Path) -> None:
    store = ImmutableDefinitionObjectStore(PROJECT, tmp_path / "objects")
    payload = b"# Concurrent Draft\n"
    digest = sha256_digest(payload)

    with ThreadPoolExecutor(max_workers=8) as executor:
        references = tuple(
            executor.map(
                lambda _: store.put_input_object(PROPOSAL, payload, digest),
                range(16),
            )
        )

    assert len({reference.path for reference in references}) == 1
    assert references[0].path.read_bytes() == payload
    assert list(references[0].path.parent.glob("*.tmp")) == []


def test_expected_digest_and_definition_ref_must_match_canonical_bytes(tmp_path: Path) -> None:
    store = ImmutableDefinitionObjectStore(PROJECT, tmp_path / "objects")
    definition = canonicalize_definition(_manifest())

    with pytest.raises(DefinitionObjectIntegrityError, match="expected identity"):
        store.put_definition_object(
            PROPOSAL,
            definition.canonical_bytes,
            f"sha256:{'2' * 64}",
        )
    with pytest.raises(DefinitionObjectStoreError, match="PROPOSAL_NOT_FOUND"):
        store.put_definition_object(
            OTHER_PROPOSAL,
            definition.canonical_bytes,
            definition.digest,
        )
    with pytest.raises(DefinitionObjectIntegrityError, match="input bytes"):
        store.put_input_object(PROPOSAL, b"input", f"sha256:{'3' * 64}")


def test_existing_different_bytes_are_never_overwritten(tmp_path: Path) -> None:
    store = ImmutableDefinitionObjectStore(PROJECT, tmp_path / "objects")
    payload = b"# Draft\n"
    digest = sha256_digest(payload)
    path = store.put_input_object(PROPOSAL, payload, digest).path
    path.write_bytes(b"corrupt")

    with pytest.raises(DefinitionObjectCollisionError, match="다른 bytes"):
        store.put_input_object(PROPOSAL, payload, digest)
    assert path.read_bytes() == b"corrupt"
    with pytest.raises(DefinitionObjectIntegrityError, match="integrity"):
        store.get_input_object(PROPOSAL, digest)


def test_noncanonical_or_tampered_definition_fails_closed(tmp_path: Path) -> None:
    store = ImmutableDefinitionObjectStore(PROJECT, tmp_path / "objects")
    definition = canonicalize_definition(_manifest())
    noncanonical = definition.canonical_bytes.replace(b'"base_revision"', b'"base_revision" ')

    with pytest.raises(DefinitionObjectIntegrityError, match="canonical bytes"):
        store.put_definition_object(PROPOSAL, noncanonical, definition.digest)

    path = store.put_definition_object(
        PROPOSAL,
        definition.canonical_bytes,
        definition.digest,
    ).path
    path.write_bytes(definition.canonical_bytes + b" ")
    with pytest.raises(DefinitionObjectIntegrityError, match="integrity"):
        store.get_definition_object(PROPOSAL, definition.digest)


def test_write_failure_cleans_temp_and_exposes_no_canonical_object(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    store = FailingBeforePublishStore(PROJECT, root)
    payload = b"# Draft\n"
    digest = sha256_digest(payload)

    with pytest.raises(DefinitionObjectStoreError, match="기록할 수 없습니다"):
        store.put_input_object(PROPOSAL, payload, digest)

    target_dir = root / PROPOSAL.proposal_id / "inputs"
    assert not (target_dir / f"{digest}.md").exists()
    assert list(target_dir.glob("*.tmp")) == []


def test_symlinked_object_root_is_rejected_before_external_write(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    linked_root = tmp_path / "linked-objects"
    linked_root.symlink_to(external, target_is_directory=True)
    store = ImmutableDefinitionObjectStore(PROJECT, linked_root)
    payload = b"# Draft\n"

    with pytest.raises(DefinitionObjectStoreError, match="symlink"):
        store.put_input_object(PROPOSAL, payload, sha256_digest(payload))

    assert list(external.iterdir()) == []


def test_hard_kill_before_publish_exposes_no_partial_canonical_object(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    marker = tmp_path / "ready"
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_run_blocking_input_write, args=(str(root), str(marker)))
    process.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and process.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "writer가 pre-publish checkpoint에 도달하지 못했습니다."

    process.kill()
    process.join(timeout=5)
    assert not process.is_alive()

    payload = b"# Draft\n"
    canonical = root / PROPOSAL.proposal_id / "inputs" / f"{sha256_digest(payload)}.md"
    assert not canonical.exists()


def test_atomic_publish_fsyncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    payload = b"# Draft\n"
    ImmutableDefinitionObjectStore(PROJECT, tmp_path / "objects").put_input_object(
        PROPOSAL,
        payload,
        sha256_digest(payload),
    )

    assert calls == ["file", "directory"]


def test_directory_fsync_failure_leaves_only_complete_retryable_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_fsync = os.fsync
    failed = False

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal failed
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not failed:
            failed = True
            raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_first_directory_fsync)
    payload = b"# Durable Draft\n"
    digest = sha256_digest(payload)
    store = ImmutableDefinitionObjectStore(PROJECT, tmp_path / "objects")

    with pytest.raises(DefinitionObjectStoreError, match="기록할 수 없습니다"):
        store.put_input_object(PROPOSAL, payload, digest)

    path = tmp_path / "objects" / PROPOSAL.proposal_id / "inputs" / f"{digest}.md"
    assert path.read_bytes() == payload

    monkeypatch.setattr(os, "fsync", original_fsync)
    assert store.put_input_object(PROPOSAL, payload, digest).path == path
