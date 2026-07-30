"""Revision-aware roadmap diff, impact, apply, and planner logic."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Literal

from amplai_foundry.roadmaps.models import (
    RoadmapArtifactReport,
    RoadmapChange,
    RoadmapChangeProposal,
    RoadmapChangeType,
    RoadmapDefinition,
    RoadmapImpactAnalysis,
    RoadmapPhase,
    RoadmapPhaseStatus,
    RoadmapProposalStatus,
)
from amplai_foundry.roadmaps.proposals import RoadmapProposalRepository
from amplai_foundry.roadmaps.repository import RoadmapRepository

_TERMINAL_STATUSES = {"completed", "cancelled", "superseded"}
_SATISFIED_DEPENDENCY_STATUSES = {"completed"}
_PHASE_HEADING = re.compile(
    r"^#{1,4}\s+Phase\s+([0-9]+[A-Za-z]?)\b[^\n]*",
    flags=re.IGNORECASE | re.MULTILINE,
)
_STATUS_MARKER = re.compile(
    r"(?:\*\*)?(?:status|상태)(?:\*\*)?\s*:\s*(?:\*\*)?\s*([^\n]+)",
    flags=re.IGNORECASE,
)
_ORDER_MARKER = re.compile(
    r"(?:\*\*)?(?:order|순서)(?:\*\*)?\s*:\s*(?:\*\*)?\s*([0-9]+)",
    flags=re.IGNORECASE,
)


class RoadmapService:
    def propose_change(
        self,
        current: RoadmapDefinition,
        desired: RoadmapDefinition,
        *,
        created_at: datetime,
        created_by: str,
    ) -> RoadmapChangeProposal:
        if current.roadmap_id != desired.roadmap_id:
            raise ValueError("서로 다른 roadmap_id는 비교할 수 없습니다.")
        current_by_id = {phase.id: phase for phase in current.phases}
        desired_by_id = {phase.id: phase for phase in desired.phases}
        changes: list[RoadmapChange] = []
        for item_id in sorted(desired_by_id.keys() - current_by_id.keys()):
            changes.append(
                RoadmapChange(
                    item_id=item_id,
                    type=RoadmapChangeType.ADD,
                    after=desired_by_id[item_id].model_dump(mode="json"),
                    reason="desired roadmap에 새 stable item이 추가되었습니다.",
                )
            )
        for item_id in sorted(current_by_id.keys() - desired_by_id.keys()):
            before = current_by_id[item_id]
            changes.append(
                RoadmapChange(
                    item_id=item_id,
                    type=RoadmapChangeType.CANCEL,
                    before=before.model_dump(mode="json"),
                    after={
                        **before.model_dump(mode="json"),
                        "status": "cancelled",
                    },
                    reason="삭제 대신 cancelled lifecycle로 보존합니다.",
                )
            )
        for item_id in sorted(current_by_id.keys() & desired_by_id.keys()):
            before = current_by_id[item_id]
            after = desired_by_id[item_id]
            before_payload = before.model_dump(mode="json")
            after_payload = after.model_dump(mode="json")
            if before.order != after.order:
                changes.append(
                    RoadmapChange(
                        item_id=item_id,
                        type=RoadmapChangeType.MOVE,
                        before={"order": before.order},
                        after={"order": after.order},
                        reason="stable item ID를 유지한 채 순서가 이동했습니다.",
                    )
                )
            structural_before = {
                key: value for key, value in before_payload.items() if key != "order"
            }
            structural_after = {
                key: value for key, value in after_payload.items() if key != "order"
            }
            if structural_before != structural_after:
                changes.append(
                    RoadmapChange(
                        item_id=item_id,
                        type=RoadmapChangeType.UPDATE,
                        before=structural_before,
                        after=structural_after,
                        reason="phase 계약 또는 상태가 변경되었습니다.",
                    )
                )
        if not changes:
            raise ValueError("roadmap에 구조적 변경이 없습니다.")
        impact = self.analyze_impact(current, changes)
        seed = hashlib.sha256(
            (
                f"{current.roadmap_id}\0{current.version}\0"
                + "\0".join(
                    f"{change.item_id}:{change.type.value}:{change.after}" for change in changes
                )
            ).encode()
        ).hexdigest()
        return RoadmapChangeProposal(
            proposal_id=f"RMAP-{created_at:%Y%m%d}-{seed[:8].upper()}",
            roadmap_id=current.roadmap_id,
            base_version=current.version,
            proposed_version=current.version + 1,
            created_at=created_at,
            created_by=created_by,
            changes=changes,
            impact=impact,
        )

    @staticmethod
    def analyze_impact(
        current: RoadmapDefinition,
        changes: list[RoadmapChange],
    ) -> RoadmapImpactAnalysis:
        changed = {change.item_id for change in changes}
        direct = {phase.id for phase in current.phases if set(phase.depends_on) & changed}
        transitive = set(direct)
        while True:
            expanded = {
                phase.id
                for phase in current.phases
                if set(phase.depends_on) & (changed | transitive)
            }
            if expanded <= transitive:
                break
            transitive.update(expanded)
        return RoadmapImpactAnalysis(
            changed_items=sorted(changed),
            directly_dependent_items=sorted(direct),
            transitive_dependent_items=sorted(transitive - direct),
            current_focus_affected=current.current_focus in changed | transitive,
            requires_replan=bool(changed),
        )

    @staticmethod
    def _approve_legacy_proposal(
        proposal: RoadmapChangeProposal,
        *,
        approved_by: str,
        approved_at: datetime,
    ) -> RoadmapChangeProposal:
        if proposal.status is not RoadmapProposalStatus.DRAFT:
            raise ValueError("draft roadmap proposal만 승인할 수 있습니다.")
        return RoadmapChangeProposal.model_validate(
            {
                **proposal.model_dump(),
                "status": RoadmapProposalStatus.APPROVED,
                "approved_by": approved_by,
                "approved_at": approved_at,
            }
        )

    def _apply_legacy_proposal(
        self,
        repository: RoadmapRepository,
        proposal: RoadmapChangeProposal,
        *,
        applied_on: date,
        proposal_repository: RoadmapProposalRepository | None = None,
    ) -> RoadmapDefinition:
        with repository.locked():
            return self._apply_locked(
                repository,
                proposal,
                applied_on=applied_on,
                proposal_repository=proposal_repository,
            )

    def _apply_locked(
        self,
        repository: RoadmapRepository,
        proposal: RoadmapChangeProposal,
        *,
        applied_on: date,
        proposal_repository: RoadmapProposalRepository | None,
    ) -> RoadmapDefinition:
        if proposal.status is not RoadmapProposalStatus.APPROVED:
            raise ValueError("approved roadmap proposal만 apply할 수 있습니다.")
        current = repository.load()
        if current.roadmap_id != proposal.roadmap_id:
            raise ValueError(
                f"roadmap identity mismatch: current={current.roadmap_id} "
                f"proposal={proposal.roadmap_id}"
            )
        if current.version != proposal.base_version:
            raise ValueError(
                f"stale roadmap revision: current={current.version} "
                f"expected={proposal.base_version}"
            )
        phases = {phase.id: phase for phase in current.phases}
        for change in proposal.changes:
            existing = phases.get(change.item_id)
            if change.type is RoadmapChangeType.ADD:
                if existing is not None:
                    raise ValueError(f"ADD item이 이미 존재합니다: {change.item_id}")
                if change.after is None:
                    raise ValueError("ADD에는 after phase가 필요합니다.")
                added = RoadmapPhase.model_validate(change.after)
                if added.id != change.item_id:
                    raise ValueError("ADD item_id와 after.id가 다릅니다.")
                phases[change.item_id] = added
            elif change.type is RoadmapChangeType.CANCEL:
                phase = self._require_existing(existing, change)
                self._validate_before(phase, change)
                phases[change.item_id] = phase.model_copy(
                    update={"status": RoadmapPhaseStatus.CANCELLED}
                )
            elif change.type is RoadmapChangeType.MOVE:
                phase = self._require_existing(existing, change)
                self._validate_before(phase, change)
                if change.after is None:
                    raise ValueError("MOVE에는 after order가 필요합니다.")
                phases[change.item_id] = phase.model_copy(
                    update={"order": int(change.after["order"])}
                )
            elif change.type in {RoadmapChangeType.UPDATE, RoadmapChangeType.SUPERSEDE}:
                phase = self._require_existing(existing, change)
                self._validate_before(phase, change)
                if change.after is None:
                    raise ValueError(f"{change.type.value}에는 after가 필요합니다.")
                prior = phase.model_dump(mode="json")
                updated_phase = RoadmapPhase.model_validate({**prior, **change.after})
                if updated_phase.id != change.item_id:
                    raise ValueError(f"{change.type.value} item_id와 after.id가 다릅니다.")
                phases[change.item_id] = updated_phase
        updated = RoadmapDefinition.model_validate(
            {
                **current.model_dump(mode="json"),
                "version": proposal.proposed_version,
                "updated_at": applied_on,
                "approved_at": proposal.approved_at.date()
                if proposal.approved_at is not None
                else None,
                "approved_by": proposal.approved_by,
                "applied_proposal_id": proposal.proposal_id,
                "phases": [
                    phase.model_dump(mode="json")
                    for phase in sorted(phases.values(), key=lambda item: item.order)
                ],
            }
        )
        updated = self.rebuild_plan(updated)
        repository._save_legacy_fixture(updated)
        original_status = proposal.status
        proposal.status = RoadmapProposalStatus.APPLIED
        if proposal_repository is not None:
            try:
                proposal_repository._save_legacy_fixture(proposal)
            except Exception:
                proposal.status = original_status
                repository._save_legacy_fixture(current)
                raise
        return updated

    @staticmethod
    def _require_existing(
        phase: RoadmapPhase | None,
        change: RoadmapChange,
    ) -> RoadmapPhase:
        if phase is None:
            raise ValueError(f"{change.type.value} item이 없습니다: {change.item_id}")
        return phase

    @staticmethod
    def _validate_before(phase: RoadmapPhase, change: RoadmapChange) -> None:
        if change.before is None:
            return
        current = phase.model_dump(mode="json")
        mismatched = [key for key, value in change.before.items() if current.get(key) != value]
        if mismatched:
            raise ValueError(
                f"stale roadmap item {change.item_id}: " + ", ".join(sorted(mismatched))
            )

    @staticmethod
    def next_phase(roadmap: RoadmapDefinition) -> RoadmapPhase | None:
        by_id = {phase.id: phase for phase in roadmap.phases}
        eligible = [
            phase
            for phase in roadmap.phases
            if phase.status not in _TERMINAL_STATUSES
            and all(
                by_id[dependency].status in _SATISFIED_DEPENDENCY_STATUSES
                for dependency in phase.depends_on
            )
        ]
        return min(eligible, key=lambda item: item.order) if eligible else None

    def rebuild_plan(self, roadmap: RoadmapDefinition) -> RoadmapDefinition:
        next_item = self.next_phase(roadmap)
        return RoadmapDefinition.model_validate(
            {
                **roadmap.model_dump(mode="json"),
                "current_focus": next_item.id if next_item is not None else None,
            }
        )

    def propose_from_markdown(
        self,
        roadmap: RoadmapDefinition,
        content: str,
        *,
        created_at: datetime,
        created_by: str,
    ) -> RoadmapArtifactReport:
        """Derive only explicit status/order/add changes; hold all unparsed structure."""
        matches = list(_PHASE_HEADING.finditer(content))
        if not matches:
            return RoadmapArtifactReport(
                roadmap_id=roadmap.roadmap_id,
                referenced_phase_ids=[],
                unmatched_phase_labels=[],
                disposition="hold",
                reason_codes=["NO_PHASE_HEADINGS"],
            )
        phases = {phase.id: phase for phase in roadmap.phases}
        referenced: list[str] = []
        unmatched: list[str] = []
        next_order = max((phase.order for phase in phases.values()), default=-10) + 10
        for index, match in enumerate(matches):
            label = match.group(1)
            prefix = f"phase-{label.casefold()}"
            candidates = sorted(item for item in phases if item.startswith(prefix))
            section_end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            section = content[match.end() : section_end]
            status = self._explicit_status(section)
            order_match = _ORDER_MARKER.search(section)
            order = int(order_match.group(1)) if order_match else None
            if len(candidates) == 1:
                item_id = candidates[0]
                phase = phases[item_id]
                referenced.append(item_id)
                updates: dict[str, object] = {}
                if status is not None and status != phase.status:
                    updates["status"] = status
                if order is not None and order != phase.order:
                    updates["order"] = order
                if updates:
                    phases[item_id] = phase.model_copy(update=updates)
                continue
            unmatched.append(label)
            if candidates:
                continue
            item_id = f"phase-{label.casefold()}-proposed"
            if item_id in phases:
                continue
            phases[item_id] = RoadmapPhase(
                id=item_id,
                order=order if order is not None else next_order,
                status=status or RoadmapPhaseStatus.PLANNED,
                depends_on=[],
                definition_of_done=["Review the source-backed phase contract before approval."],
            )
            next_order = max(next_order + 10, phases[item_id].order + 10)

        try:
            desired = RoadmapDefinition.model_validate(
                {
                    **roadmap.model_dump(mode="json"),
                    "version": roadmap.version + 1,
                    "phases": [
                        phase.model_dump(mode="json")
                        for phase in sorted(phases.values(), key=lambda item: item.order)
                    ],
                }
            )
        except ValueError:
            return RoadmapArtifactReport(
                roadmap_id=roadmap.roadmap_id,
                referenced_phase_ids=sorted(set(referenced)),
                unmatched_phase_labels=sorted(set(unmatched)),
                disposition="review_required",
                reason_codes=["INVALID_EXPLICIT_ROADMAP_STRUCTURE"],
            )
        try:
            proposal = self.propose_change(
                roadmap,
                desired,
                created_at=created_at,
                created_by=created_by,
            )
        except ValueError:
            return RoadmapArtifactReport(
                roadmap_id=roadmap.roadmap_id,
                referenced_phase_ids=sorted(set(referenced)),
                unmatched_phase_labels=sorted(set(unmatched)),
                disposition="review_required",
                reason_codes=["STRUCTURAL_DIFF_NOT_DERIVED"],
            )
        proposal = proposal.model_copy(
            update={
                "changes": [
                    change.model_copy(
                        update={
                            "type": (
                                RoadmapChangeType.CANCEL
                                if change.type is RoadmapChangeType.UPDATE
                                and change.after is not None
                                and change.after.get("status") == "cancelled"
                                else RoadmapChangeType.SUPERSEDE
                                if change.type is RoadmapChangeType.UPDATE
                                and change.after is not None
                                and change.after.get("status") == "superseded"
                                else change.type
                            )
                        }
                    )
                    for change in proposal.changes
                ]
            }
        )
        proposal = RoadmapChangeProposal.model_validate(proposal.model_dump())
        return RoadmapArtifactReport(
            roadmap_id=roadmap.roadmap_id,
            referenced_phase_ids=sorted(set(referenced)),
            unmatched_phase_labels=sorted(set(unmatched)),
            disposition="review_required",
            reason_codes=["ROADMAP_CHANGE_PROPOSAL_CREATED"],
            change_proposal=proposal,
        )

    @staticmethod
    def _explicit_status(section: str) -> RoadmapPhaseStatus | None:
        match = _STATUS_MARKER.search(section)
        if match is None:
            return None
        value = match.group(1).casefold()
        if "미완료" in value or "not completed" in value:
            return None
        for status, signals in (
            (RoadmapPhaseStatus.COMPLETED, ("completed", "완료")),
            (RoadmapPhaseStatus.CANCELLED, ("cancelled", "canceled", "취소")),
            (RoadmapPhaseStatus.SUPERSEDED, ("superseded", "대체")),
            (RoadmapPhaseStatus.IN_PROGRESS, ("in progress", "in_progress", "진행")),
            (RoadmapPhaseStatus.PLANNED, ("planned", "계획")),
            (RoadmapPhaseStatus.BACKLOG, ("backlog", "백로그")),
        ):
            if any(signal in value for signal in signals):
                return status
        return None

    @staticmethod
    def analyze_markdown(
        roadmap: RoadmapDefinition,
        content: str,
    ) -> RoadmapArtifactReport:
        labels = _PHASE_HEADING.findall(content)
        known = {phase.id for phase in roadmap.phases}
        matched: list[str] = []
        unmatched: list[str] = []
        for label in labels:
            prefix = f"phase-{label.casefold()}"
            matches = sorted(item for item in known if item.startswith(prefix))
            if len(matches) == 1:
                matched.append(matches[0])
            else:
                unmatched.append(label)
        if not labels:
            disposition: Literal["no_structural_change", "review_required", "hold"] = "hold"
            reasons = ["NO_PHASE_HEADINGS"]
        elif unmatched:
            disposition = "review_required"
            reasons = ["UNMATCHED_PHASE_LABELS"]
        else:
            disposition = "review_required"
            reasons = ["STRUCTURAL_DIFF_NOT_DERIVED"]
        return RoadmapArtifactReport(
            roadmap_id=roadmap.roadmap_id,
            referenced_phase_ids=sorted(set(matched)),
            unmatched_phase_labels=sorted(set(unmatched)),
            disposition=disposition,
            reason_codes=reasons,
        )
