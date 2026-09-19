"""Readiness and context completeness validator (design/12 §2-§3, SEM-03, INV-16).

The validator never picks a winner between conflicting active decisions and never
counts a structurally long list as complete. Its READY verdict carries the
invariant-discovery completeness marker that context assembly requires.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from amplai_foundry.runtime.contracts.identity import now
from amplai_foundry.runtime.contracts.semantics import READINESS_AREAS, resolve_ref
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.storage.store import Scope, Store

Ref = dict[str, Any]


class ReadinessValidator:
    def __init__(self, store: Store, contracts: Any) -> None:
        self.store, self.contracts = store, contracts

    # -- structure ---------------------------------------------------------------
    @staticmethod
    def _completeness(entries: list[dict[str, Any]]) -> None:
        areas = [str(item.get("area")) for item in entries]
        duplicates = sorted({a for a in areas if areas.count(a) > 1})
        missing = sorted(READINESS_AREAS - set(areas))
        unknown = sorted(set(areas) - READINESS_AREAS)
        if duplicates or missing or unknown or len(entries) != len(READINESS_AREAS):
            raise Hold(
                "READINESS_COMPLETENESS",
                "Eight distinct readiness areas are required; row count alone is not completeness",
                details={
                    "rows": len(entries),
                    "duplicates": duplicates,
                    "missing": missing,
                    "unknown": unknown,
                },
            )

    # -- invariants --------------------------------------------------------------
    def _current_invariants(self, scope: Scope, ref: Ref) -> Ref:
        kind, _ = resolve_ref(self.store, scope, ref)
        if kind != "invariant-registry":
            raise Hold("INVARIANTS_REF_KIND", "Context must bind an invariant registry object")
        latest = max(
            [
                r["revision"]
                for r, _ in self.store.list_objects(scope, "invariant-registry")
                if r["id"] == ref["id"]
            ]
            + [0]
        )
        if latest > ref["revision"]:
            raise Hold(
                "INVARIANTS_STALE",
                "A newer active invariant registry revision exists; refresh before readiness",
                details={"bound_revision": ref["revision"], "latest_revision": latest},
            )
        return ref

    # -- entries -----------------------------------------------------------------
    def _entries(self, scope: Scope, entries: list[dict[str, Any]], na_rules: set[str]) -> None:
        unresolved: list[str] = []
        conflicting: list[str] = []
        for item in entries:
            status = item.get("status")
            if status == "ready":
                if not item.get("source_refs") or not str(item.get("reason", "")).strip():
                    raise Hold(
                        "READINESS_UNGROUNDED",
                        "Ready requires current evidence and a reason",
                        details={"area": item["area"]},
                    )
                for ref in item["source_refs"]:
                    resolve_ref(self.store, scope, ref)
            elif status == "not_applicable":
                reason = str(item.get("reason", ""))
                rule_ok = any(reason.startswith(rule + ":") for rule in na_rules)
                verifier_rule = str(item.get("verifier_rule") or "").strip()
                if not rule_ok or not verifier_rule:
                    raise Hold(
                        "READINESS_NA_RULE",
                        "not_applicable needs an approved applicability rule and a verifier rule",
                        details={"area": item["area"]},
                    )
            elif status == "conflicting":
                conflicting.append(str(item["area"]))
            else:
                unresolved.append(str(item["area"]))
        if conflicting:
            raise Hold(
                "CONTEXT_NOT_READY",
                "Conflicting knowledge must be disclosed and resolved, not chosen silently",
                details={"conflicting_areas": conflicting, "conflicts": []},
            )
        if unresolved:
            raise Hold(
                "KNOWLEDGE_NOT_READY",
                "Unresolved knowledge areas: " + ", ".join(unresolved),
                details={"areas": unresolved},
            )

    # -- decisions ---------------------------------------------------------------
    def _decision_conflicts(self, scope: Scope, refs: Iterable[Ref]) -> list[dict[str, Any]]:
        active: dict[str, tuple[Ref, dict[str, Any]]] = {}
        for ref in refs:
            _, value = resolve_ref(self.store, scope, ref)
            if value.get("status", "active") != "active" or value.get("superseded_by"):
                continue
            active[str(value.get("memory_id") or ref["id"])] = (ref, value)
        seen: set[tuple[str, str]] = set()
        conflicts: list[dict[str, Any]] = []
        for left_id, (left_ref, left) in active.items():
            targets = list(left.get("contradicts") or [])
            targets += [
                str(r.get("target"))
                for r in left.get("relations") or []
                if isinstance(r, dict) and r.get("type") == "contradicts"
            ]
            for right_id in targets:
                if right_id not in active:
                    continue
                key = tuple(sorted((left_id, right_id)))
                if key in seen:
                    continue
                seen.add((key[0], key[1]))
                right_ref, right = active[right_id]
                conflicts.append(
                    {
                        "left": left_ref,
                        "right": right_ref,
                        "left_statement": left.get("statement") or left.get("summary"),
                        "right_statement": right.get("statement") or right.get("summary"),
                    }
                )
        return conflicts

    # -- verdict -----------------------------------------------------------------
    def evaluate(
        self,
        scope: Scope,
        entries: list[dict[str, Any]],
        *,
        invariant_registry_ref: Ref,
        active_decision_refs: Iterable[Ref] = (),
        na_rules: set[str] | None = None,
    ) -> dict[str, Any]:
        self._completeness(entries)
        registry_ref = self._current_invariants(scope, invariant_registry_ref)
        self._entries(scope, entries, na_rules or set())
        decisions = list(active_decision_refs)
        conflicts = self._decision_conflicts(scope, decisions)
        if conflicts:
            raise Hold(
                "CONTEXT_NOT_READY",
                "Active canonical decisions contradict each other; no arbitrary winner",
                details={"conflicting_areas": [], "conflicts": conflicts},
            )
        return {
            "verdict": "ready",
            "areas": [str(item["area"]) for item in entries],
            "invariant_registry_ref": registry_ref,
            "invariant_discovery_complete": True,
            "active_decision_refs": decisions,
            "evaluated_at": now(),
        }
