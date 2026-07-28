"""Deterministic artifact classifier with an explicit ambiguous state."""

from __future__ import annotations

from amplai_foundry.intake.models import (
    ArtifactClassification,
    ArtifactKind,
)

_SIGNALS = {
    ArtifactKind.ROADMAP: ("roadmap", "로드맵", "phase ", "단계", "milestone"),
    ArtifactKind.ARCHITECTURE_PROPOSAL: (
        "architecture",
        "아키텍처",
        "proposal",
        "설계안",
    ),
    ArtifactKind.MEETING_NOTE: ("meeting", "회의", "참석자", "agenda", "안건"),
    ArtifactKind.INCIDENT: ("incident", "장애", "root cause", "재발", "장애 원인"),
    ArtifactKind.SOP: ("sop", "절차서", "작업 절차", "standard operating"),
    ArtifactKind.IMPLEMENTATION_RESULT: (
        "implemented",
        "구현 결과",
        "test passed",
        "검증 결과",
        "changed files",
    ),
}


class ArtifactClassifier:
    def classify(self, content: str, *, title: str, instruction: str) -> ArtifactClassification:
        metadata_haystack = f"{title}\n{instruction}".casefold()
        content_haystack = content[:20_000].casefold()
        scores = {
            kind: sum(
                1 for signal in signals if signal in metadata_haystack or signal in content_haystack
            )
            for kind, signals in _SIGNALS.items()
        }
        content_scores = {
            kind: sum(1 for signal in signals if signal in content_haystack)
            for kind, signals in _SIGNALS.items()
        }
        best_score = max(scores.values(), default=0)
        winners = sorted(
            (kind for kind, score in scores.items() if score == best_score and score > 0),
            key=lambda item: item.value,
        )
        content_grounded = len(winners) == 1 and content_scores[winners[0]] > 0
        if best_score < 2 or len(winners) != 1 or not content_grounded:
            reason = (
                "NO_CONTENT_GROUNDING"
                if best_score >= 2 and len(winners) == 1 and not content_grounded
                else "NO_STRONG_CLASSIFICATION"
                if best_score < 2
                else "CLASSIFICATION_TIE"
            )
            return ArtifactClassification(
                kind=ArtifactKind.UNKNOWN,
                confidence=0.0 if best_score == 0 else 0.5,
                reason_codes=[reason],
                held=True,
            )
        confidence = min(0.99, 0.65 + best_score * 0.08)
        return ArtifactClassification(
            kind=winners[0],
            confidence=confidence,
            reason_codes=[f"{winners[0].value.upper()}_SIGNALS_{best_score}"],
            held=False,
        )
