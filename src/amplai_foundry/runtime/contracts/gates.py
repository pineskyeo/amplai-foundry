"""Server-owned guards. There is deliberately no default-success path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import Hold, RuntimeFault
from .registry import Contracts


@dataclass(frozen=True)
class Observation:
    outcome: str
    reason: str
    evidence_refs: tuple[dict[str, Any], ...] = ()
    applicability_rule: str | None = None

    @classmethod
    def check(
        cls, predicate: bool, reason: str, evidence_refs: tuple[dict[str, Any], ...] = ()
    ) -> Observation:
        return cls("pass" if predicate else "hold", reason, evidence_refs)


class GateEngine:
    def __init__(self, contracts: Contracts):
        self.definitions = {g["id"]: g for g in contracts.gates["gates"]}

    def evaluate(
        self, required: list[str], observations: dict[str, Observation]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for gate in required:
            if gate not in self.definitions:
                raise RuntimeFault("UNKNOWN_GATE", gate)
            obs = observations.get(gate, Observation("hold", "No current server-side observation"))
            if obs.outcome not in {"pass", "fail", "hold", "not_applicable"}:
                raise RuntimeFault("INVALID_GATE_OUTCOME", gate)
            if obs.outcome == "not_applicable" and (not obs.applicability_rule or not obs.reason):
                obs = Observation("hold", "N/A requires a versioned applicability rule and reason")
            # G-01/03/06/08/09/11/12/13 cannot be waived on an execution transition.
            if obs.outcome == "not_applicable" and gate in {
                "G-01",
                "G-03",
                "G-06",
                "G-08",
                "G-09",
                "G-11",
                "G-12",
                "G-13",
            }:
                obs = Observation("hold", "This gate cannot be made inapplicable on execution")
            results.append(
                {
                    "gate_id": gate,
                    "outcome": obs.outcome,
                    "reason": obs.reason,
                    "evidence_refs": list(obs.evidence_refs),
                }
            )
        blocked = [r for r in results if r["outcome"] not in {"pass", "not_applicable"}]
        if blocked:
            raise Hold("GATE_BLOCKED", "Required gates are not satisfied", details=results)
        return results


class StateMachines:
    def __init__(self, contracts: Contracts):
        self.machines = contracts.states["machines"]
        self.gates = GateEngine(contracts)

    def transition(
        self, kind: str, current: str, command: str, observations: dict[str, Observation]
    ) -> tuple[str, list[dict[str, Any]]]:
        machine = self.machines.get(kind)
        if not machine:
            raise RuntimeFault("UNKNOWN_STATE_MACHINE", kind)
        rules = [
            t for t in machine["transitions"] if current in t["from"] and t["command"] == command
        ]
        if len(rules) != 1:
            raise RuntimeFault("ILLEGAL_TRANSITION", f"{kind}:{current} --{command}-->")
        rule = rules[0]
        return rule["to"], self.gates.evaluate(rule["guards"], observations)
