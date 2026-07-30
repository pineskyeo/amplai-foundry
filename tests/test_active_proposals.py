from __future__ import annotations

import multiprocessing
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.active_proposals import (
    ActiveProposalNotFoundError,
    ActiveProposalRepository,
    ActiveProposalStatus,
    DefinitionCASConflictError,
    InvalidProposalTransitionError,
    state_transition_allowed,
)
from amplai_foundry.governance.definitions import (
    ProposalDefinitionManifest,
    canonicalize_definition,
)
from amplai_foundry.governance.models import ProposalRef
from amplai_foundry.governance.object_store import (
    DefinitionObjectIntegrityError,
    DefinitionObjectRef,
    ImmutableDefinitionObjectStore,
)
from amplai_foundry.governance.store import GovernanceStore

PROJECT = ProjectRef(project_id="amplai", namespace="org/default/project/amplai")
OTHER_PROJECT = ProjectRef(project_id="cortex", namespace="org/default/project/cortex")
PROPOSAL = ProposalRef(
    project_ref=PROJECT,
    proposal_id="PROP-20260730-ABCDEF12",
)
OTHER_PROPOSAL = ProposalRef(
    project_ref=OTHER_PROJECT,
    proposal_id=PROPOSAL.proposal_id,
)


def _definition(
    objects: ImmutableDefinitionObjectStore,
    ref: ProposalRef,
    marker: str,
) -> DefinitionObjectRef:
    canonical = canonicalize_definition(
        ProposalDefinitionManifest(
            proposal_ref=ref,
            operations=({"marker": marker, "type": "CREATE"},),
            base_revision="a13d92f",
            validation_policy_ref="policy/proposal-v3",
        )
    )
    return objects.put_definition_object(ref, canonical.canonical_bytes, canonical.digest)


def _repository(
    tmp_path: Path,
    *,
    project_ref: ProjectRef = PROJECT,
    project_root: Path | None = None,
    store: GovernanceStore | None = None,
) -> tuple[GovernanceStore, ImmutableDefinitionObjectStore, ActiveProposalRepository]:
    governance_store = store or GovernanceStore(tmp_path / "governance.db")
    governance_store.initialize()
    root = project_root or tmp_path
    objects = ImmutableDefinitionObjectStore(project_ref, root)
    return governance_store, objects, ActiveProposalRepository(governance_store, objects)


class FailingRevisionRepository(ActiveProposalRepository):
    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        ref: ProposalRef,
        content_revision: int,
        digest: str,
        previous_digest: str | None,
        activated_from_status: ActiveProposalStatus | None,
    ) -> None:
        raise RuntimeError("injected revision insert failure")


class EventGovernanceStore(GovernanceStore):
    def __init__(self, path: Path, events: list[str]) -> None:
        super().__init__(path)
        self.events = events

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.events.append("connect")
        with super().connect() as connection:
            yield connection


class EventDefinitionReader:
    def __init__(self, delegate: ImmutableDefinitionObjectStore, events: list[str]) -> None:
        self.delegate = delegate
        self.events = events

    def get_definition_object(self, ref: ProposalRef, digest: str) -> bytes:
        self.events.append("verify-definition")
        return self.delegate.get_definition_object(ref, digest)


def _run_blocking_revision_activation(
    database: str,
    project_root: str,
    marker: str,
    expected_digest: str,
    expected_state_revision: int,
    next_digest: str,
) -> None:
    class BlockingRevisionRepository(ActiveProposalRepository):
        @staticmethod
        def _insert_revision(
            connection: sqlite3.Connection,
            ref: ProposalRef,
            content_revision: int,
            digest: str,
            previous_digest: str | None,
            activated_from_status: ActiveProposalStatus | None,
        ) -> None:
            Path(marker).write_text("ready", encoding="utf-8")
            time.sleep(60)

    objects = ImmutableDefinitionObjectStore(PROJECT, Path(project_root))
    repository = BlockingRevisionRepository(GovernanceStore(Path(database)), objects)
    repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=expected_digest,
        expected_state_revision=expected_state_revision,
        next_object_ref=DefinitionObjectRef(
            proposal_ref=PROPOSAL,
            object_kind="definition",
            digest=next_digest,
            path=Path(project_root),
        ),
    )


def test_initial_activation_requires_verified_object_and_creates_qualified_revision(
    tmp_path: Path,
) -> None:
    _store, objects, repository = _repository(tmp_path)
    definition = _definition(objects, PROPOSAL, "initial")

    view = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=definition,
    )

    assert view.proposal_ref == PROPOSAL
    assert view.active_definition_digest == definition.digest
    assert (view.content_revision, view.state_revision, view.decision_epoch) == (1, 1, 1)
    assert view.status is ActiveProposalStatus.DRAFT
    assert repository.get(PROPOSAL) == view
    assert repository.list(PROJECT) == (view,)
    revisions = repository.list_definition_revisions(PROPOSAL)
    assert len(revisions) == 1
    assert revisions[0].definition_digest == definition.digest
    assert revisions[0].previous_definition_digest is None
    assert revisions[0].activated_from_status is None


def test_missing_or_corrupt_definition_fails_before_initial_state_mutation(tmp_path: Path) -> None:
    _store, objects, repository = _repository(tmp_path)
    missing = DefinitionObjectRef(
        proposal_ref=PROPOSAL,
        object_kind="definition",
        digest=f"sha256:{'1' * 64}",
        path=tmp_path / "missing.yaml",
    )

    with pytest.raises(DefinitionObjectIntegrityError):
        repository.activate_definition_revision(
            PROPOSAL,
            expected_active_digest=None,
            expected_state_revision=0,
            next_object_ref=missing,
        )
    assert repository.get(PROPOSAL) is None

    definition = _definition(objects, PROPOSAL, "corrupt")
    definition.path.write_bytes(b"corrupt")
    with pytest.raises(DefinitionObjectIntegrityError):
        repository.activate_definition_revision(
            PROPOSAL,
            expected_active_digest=None,
            expected_state_revision=0,
            next_object_ref=definition,
        )
    assert repository.get(PROPOSAL) is None


def test_definition_revisions_increment_all_counters_and_changes_requested_returns_to_draft(
    tmp_path: Path,
) -> None:
    _store, objects, repository = _repository(tmp_path)
    first = _definition(objects, PROPOSAL, "first")
    second = _definition(objects, PROPOSAL, "second")
    third = _definition(objects, PROPOSAL, "third")
    current = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=first,
    )

    current = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=current.active_definition_digest,
        expected_state_revision=current.state_revision,
        next_object_ref=second,
    )
    assert (current.content_revision, current.state_revision, current.decision_epoch) == (2, 2, 2)

    current = repository.transition_state(
        PROPOSAL,
        expected_status=current.status,
        expected_state_revision=current.state_revision,
        next_status=ActiveProposalStatus.REVIEWED,
    )
    current = repository.transition_state(
        PROPOSAL,
        expected_status=current.status,
        expected_state_revision=current.state_revision,
        next_status=ActiveProposalStatus.CHANGES_REQUESTED,
    )
    assert current.status is ActiveProposalStatus.CHANGES_REQUESTED
    with pytest.raises(InvalidProposalTransitionError):
        repository.transition_state(
            PROPOSAL,
            expected_status=current.status,
            expected_state_revision=current.state_revision,
            next_status=ActiveProposalStatus.DRAFT,
        )
    assert repository.get(PROPOSAL) == current

    current = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=current.active_definition_digest,
        expected_state_revision=current.state_revision,
        next_object_ref=third,
    )
    assert current.status is ActiveProposalStatus.DRAFT
    assert (current.content_revision, current.state_revision, current.decision_epoch) == (3, 5, 3)
    revisions = repository.list_definition_revisions(PROPOSAL)
    assert tuple(item.activated_from_status for item in revisions) == (
        None,
        ActiveProposalStatus.DRAFT,
        ActiveProposalStatus.CHANGES_REQUESTED,
    )


def test_state_only_transition_matrix_is_exact_and_preserves_definition_counters(
    tmp_path: Path,
) -> None:
    expected = {
        (ActiveProposalStatus.DRAFT, ActiveProposalStatus.REVIEWED),
        (ActiveProposalStatus.DRAFT, ActiveProposalStatus.SUPERSEDED),
        (ActiveProposalStatus.REVIEWED, ActiveProposalStatus.APPROVED),
        (ActiveProposalStatus.REVIEWED, ActiveProposalStatus.REJECTED),
        (ActiveProposalStatus.REVIEWED, ActiveProposalStatus.CHANGES_REQUESTED),
        (ActiveProposalStatus.APPROVED, ActiveProposalStatus.APPLY_REQUESTED),
        (ActiveProposalStatus.APPLY_REQUESTED, ActiveProposalStatus.APPLIED),
        (ActiveProposalStatus.APPLY_REQUESTED, ActiveProposalStatus.APPLY_FAILED),
        (ActiveProposalStatus.APPLY_FAILED, ActiveProposalStatus.APPLY_REQUESTED),
    }
    actual = {
        (current, next_status)
        for current in ActiveProposalStatus
        for next_status in ActiveProposalStatus
        if state_transition_allowed(current, next_status)
    }
    assert actual == expected

    _store, objects, repository = _repository(tmp_path)
    definition = _definition(objects, PROPOSAL, "state-only")
    initial = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=definition,
    )
    current = initial
    for next_status in (
        ActiveProposalStatus.REVIEWED,
        ActiveProposalStatus.APPROVED,
        ActiveProposalStatus.APPLY_REQUESTED,
        ActiveProposalStatus.APPLY_FAILED,
        ActiveProposalStatus.APPLY_REQUESTED,
        ActiveProposalStatus.APPLIED,
    ):
        current = repository.transition_state(
            PROPOSAL,
            expected_status=current.status,
            expected_state_revision=current.state_revision,
            next_status=next_status,
        )
    assert current.state_revision == initial.state_revision + 6
    assert current.active_definition_digest == initial.active_definition_digest
    assert current.content_revision == initial.content_revision
    assert current.decision_epoch == initial.decision_epoch
    with pytest.raises(InvalidProposalTransitionError):
        repository.transition_state(
            PROPOSAL,
            expected_status=current.status,
            expected_state_revision=current.state_revision,
            next_status=ActiveProposalStatus.DRAFT,
        )


def test_stale_cas_and_invalid_transition_leave_aggregate_unchanged(tmp_path: Path) -> None:
    _store, objects, repository = _repository(tmp_path)
    first = _definition(objects, PROPOSAL, "first")
    second = _definition(objects, PROPOSAL, "second")
    initial = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=first,
    )

    with pytest.raises(DefinitionCASConflictError, match="DEFINITION_CAS_CONFLICT"):
        repository.activate_definition_revision(
            PROPOSAL,
            expected_active_digest=f"sha256:{'2' * 64}",
            expected_state_revision=initial.state_revision,
            next_object_ref=second,
        )
    assert repository.get(PROPOSAL) == initial

    with pytest.raises(DefinitionCASConflictError, match="PROPOSAL_STATE_STALE"):
        repository.transition_state(
            PROPOSAL,
            expected_status=ActiveProposalStatus.DRAFT,
            expected_state_revision=99,
            next_status=ActiveProposalStatus.REVIEWED,
        )
    assert repository.get(PROPOSAL) == initial

    with pytest.raises(InvalidProposalTransitionError):
        repository.transition_state(
            PROPOSAL,
            expected_status=initial.status,
            expected_state_revision=initial.state_revision,
            next_status=ActiveProposalStatus.APPROVED,
        )
    assert repository.get(PROPOSAL) == initial


def test_revision_insert_failure_rolls_back_pointer_and_all_counters(tmp_path: Path) -> None:
    store, objects, repository = _repository(tmp_path)
    first = _definition(objects, PROPOSAL, "first")
    second = _definition(objects, PROPOSAL, "second")
    initial = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=first,
    )
    failing = FailingRevisionRepository(store, objects)

    with pytest.raises(RuntimeError, match="injected"):
        failing.activate_definition_revision(
            PROPOSAL,
            expected_active_digest=initial.active_definition_digest,
            expected_state_revision=initial.state_revision,
            next_object_ref=second,
        )

    assert repository.get(PROPOSAL) == initial
    assert len(repository.list_definition_revisions(PROPOSAL)) == 1


def test_hard_kill_after_pointer_update_rolls_back_entire_revision_transaction(
    tmp_path: Path,
) -> None:
    store, objects, repository = _repository(tmp_path)
    first = _definition(objects, PROPOSAL, "first")
    second = _definition(objects, PROPOSAL, "second")
    initial = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=first,
    )
    marker = tmp_path / "revision-update-ready"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_run_blocking_revision_activation,
        args=(
            str(store.path),
            str(tmp_path),
            str(marker),
            initial.active_definition_digest,
            initial.state_revision,
            second.digest,
        ),
    )
    process.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and process.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "child가 pointer update checkpoint에 도달하지 못했습니다."

    process.kill()
    process.join(timeout=5)
    assert not process.is_alive()

    assert repository.get(PROPOSAL) == initial
    assert len(repository.list_definition_revisions(PROPOSAL)) == 1


def test_concurrent_definition_cas_has_exactly_one_winner(tmp_path: Path) -> None:
    _store, objects, repository = _repository(tmp_path)
    initial_object = _definition(objects, PROPOSAL, "initial")
    candidates = (
        _definition(objects, PROPOSAL, "candidate-a"),
        _definition(objects, PROPOSAL, "candidate-b"),
    )
    initial = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=initial_object,
    )

    def activate(candidate: DefinitionObjectRef) -> str:
        try:
            repository.activate_definition_revision(
                PROPOSAL,
                expected_active_digest=initial.active_definition_digest,
                expected_state_revision=initial.state_revision,
                next_object_ref=candidate,
            )
        except DefinitionCASConflictError:
            return "conflict"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(activate, candidates))

    assert sorted(results) == ["accepted", "conflict"]
    current = repository.get(PROPOSAL)
    assert current is not None
    assert current.active_definition_digest in {candidate.digest for candidate in candidates}
    assert (current.content_revision, current.state_revision, current.decision_epoch) == (2, 2, 2)
    assert len(repository.list_definition_revisions(PROPOSAL)) == 2


def test_concurrent_state_cas_has_exactly_one_winner_and_one_revision_increment(
    tmp_path: Path,
) -> None:
    _store, objects, repository = _repository(tmp_path)
    definition = _definition(objects, PROPOSAL, "state-race")
    initial = repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=definition,
    )

    def transition(next_status: ActiveProposalStatus) -> str:
        try:
            repository.transition_state(
                PROPOSAL,
                expected_status=initial.status,
                expected_state_revision=initial.state_revision,
                next_status=next_status,
            )
        except DefinitionCASConflictError:
            return "conflict"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                transition,
                (ActiveProposalStatus.REVIEWED, ActiveProposalStatus.SUPERSEDED),
            )
        )

    assert sorted(results) == ["accepted", "conflict"]
    current = repository.get(PROPOSAL)
    assert current is not None
    assert current.status in {ActiveProposalStatus.REVIEWED, ActiveProposalStatus.SUPERSEDED}
    assert current.state_revision == 2
    assert (current.content_revision, current.decision_epoch) == (1, 1)


def test_same_local_proposal_id_is_qualified_in_one_governance_store(tmp_path: Path) -> None:
    store = GovernanceStore(tmp_path / "governance.db")
    store.initialize()
    amplai_root = tmp_path / "amplai"
    cortex_root = tmp_path / "cortex"
    amplai_root.mkdir()
    cortex_root.mkdir()
    _, amplai_objects, amplai = _repository(
        tmp_path,
        project_ref=PROJECT,
        project_root=amplai_root,
        store=store,
    )
    _, cortex_objects, cortex = _repository(
        tmp_path,
        project_ref=OTHER_PROJECT,
        project_root=cortex_root,
        store=store,
    )
    amplai_definition = _definition(amplai_objects, PROPOSAL, "amplai")
    cortex_definition = _definition(cortex_objects, OTHER_PROPOSAL, "cortex")

    amplai.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=amplai_definition,
    )
    cortex.activate_definition_revision(
        OTHER_PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=cortex_definition,
    )

    assert amplai.get(PROPOSAL) is not None
    assert cortex.get(OTHER_PROPOSAL) is not None
    assert amplai.list(PROJECT)[0].proposal_ref == PROPOSAL
    assert cortex.list(OTHER_PROJECT)[0].proposal_ref == OTHER_PROPOSAL


def test_definition_verification_finishes_before_database_connection(tmp_path: Path) -> None:
    events: list[str] = []
    store = EventGovernanceStore(tmp_path / "governance.db", events)
    store.initialize()
    events.clear()
    objects = ImmutableDefinitionObjectStore(PROJECT, tmp_path)
    definition = _definition(objects, PROPOSAL, "ordered")
    reader = EventDefinitionReader(objects, events)
    repository = ActiveProposalRepository(store, reader)

    repository.activate_definition_revision(
        PROPOSAL,
        expected_active_digest=None,
        expected_state_revision=0,
        next_object_ref=definition,
    )

    assert events == ["verify-definition", "connect"]


def test_wrong_project_object_ref_does_not_disclose_or_create_proposal(tmp_path: Path) -> None:
    _store, _objects, repository = _repository(tmp_path)
    foreign = DefinitionObjectRef(
        proposal_ref=OTHER_PROPOSAL,
        object_kind="definition",
        digest=f"sha256:{'5' * 64}",
        path=tmp_path / "foreign.yaml",
    )

    with pytest.raises(ActiveProposalNotFoundError, match="PROPOSAL_NOT_FOUND"):
        repository.activate_definition_revision(
            PROPOSAL,
            expected_active_digest=None,
            expected_state_revision=0,
            next_object_ref=foreign,
        )
    assert repository.get(PROPOSAL) is None
