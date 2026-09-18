"""Conservative strategy policy. Selection never issues authority or skips verification.

Inputs are pinned contract facts and server-observed task characteristics, not a
model's self-rating. Unknown capability/budget is HOLD rather than a weaker mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ..errors import Hold, RuntimeFault

RISK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
PROTECTED = frozenset(
    {
        ".ai-team",
        ".github",
        "verifiers",
        "policies",
        "migrations",
        "authority",
        "holdout",
        "eval",
        "contracts",
    }
)


@dataclass(frozen=True)
class TaskFacts:
    changed_paths: tuple[str, ...] = ()
    deterministic_verifier: bool = False
    local_change: bool = False
    uncertainty: str = "unknown"
    unresolved_questions: bool = False
    read_only: bool = False
    target_count: int = 1


@dataclass(frozen=True)
class StrategyDecision:
    strategy: str
    risk_floor: str
    reasons: tuple[str, ...]
    mandatory_gates: tuple[str, ...] = (
        "scope",
        "authority",
        "containment",
        "budget",
        "evidence",
        "independent_verification",
    )


class StrategyRouter:
    def select(
        self, contract: dict, facts: TaskFacts, *, available: frozenset[str]
    ) -> StrategyDecision:
        if contract.get("risk") not in RISK or facts.uncertainty not in {
            "low",
            "medium",
            "high",
            "unknown",
        }:
            raise RuntimeFault(
                "STRATEGY_INPUT", "Risk and uncertainty must be explicit policy values"
            )
        if facts.target_count < 1:
            raise Hold("TARGET_UNKNOWN", "An admitted target is required")
        risk = contract["risk"]
        for name in facts.changed_paths:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise Hold("STRATEGY_PATH", "Changed paths must be repository-relative")
            if set(path.parts) & PROTECTED or path.name in {"AGENTS.md", "CLAUDE.md"}:
                risk = max((risk, "high"), key=RISK.__getitem__)
        budget = contract["budget"]
        if budget["max_wall_seconds"] <= 0 or budget["max_tokens"] <= 0:
            raise Hold("STRATEGY_BUDGET", "No admitted execution budget")
        reasons = ["Risk can be raised, never automatically reduced"]
        if facts.unresolved_questions:
            if not facts.read_only:
                raise Hold("DISCOVERY_REQUIRED", "Resolve blocking questions before implementation")
            strategy = "discovery"
            reasons.append("Unanswered questions: unrelated read-only discovery only")
        elif facts.uncertainty in {"high", "unknown"}:
            strategy = "deliberative"
            reasons.append("Explicit planning/review boundary before implementation")
        elif (
            facts.local_change
            and facts.deterministic_verifier
            and risk == "low"
            and facts.target_count == 1
        ):
            strategy = "direct"
            reasons.append("One attempt; independent verification remains mandatory")
        else:
            strategy = "bounded_loop"
            reasons.append("Each verifier repair creates a new Run within the root budget")
        if strategy not in available:
            raise Hold(
                "STRATEGY_UNSUPPORTED", "Required strategy is not qualified; no silent downgrade"
            )
        if facts.target_count > 1:
            reasons.append("Cross-app work remains explicit WorkGraph nodes with a global verifier")
        return StrategyDecision(strategy, risk, tuple(reasons))

    @staticmethod
    def validate_node(node: dict, contract: dict) -> None:
        strategy = node["strategy"]
        if strategy == "direct" and contract["risk"] != "low":
            raise Hold("DIRECT_RISK", "Direct mode is restricted to low-risk admitted contracts")
        if strategy == "discovery" and any(
            c["effect_class"] != "pure_read" for c in node["capabilities"]
        ):
            raise Hold("DISCOVERY_WRITE", "Discovery may not perform writes")
