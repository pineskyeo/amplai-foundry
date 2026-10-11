"""Intent resolution and contract admission. Planning is untrusted; authority is not."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from ..contracts.authority import Actor
from ..contracts.identity import digest, new_id, now
from ..contracts.intake import check_question_kind
from ..contracts.registry import Contracts
from ..contracts.semantics import (
    check_contract,
    check_readiness,
    check_refs,
)
from ..errors import Conflict, Hold, RuntimeFault
from ..storage.store import Scope, Store


class AppRegistry:
    def __init__(self, store: Store, contracts: Contracts) -> None:
        self.store, self.contracts = store, contracts

    def register(self, actor: Actor, binding: dict[str, Any]) -> dict[str, Any]:
        actor.require("app.register")
        self.contracts.validate("app-binding", binding)
        if binding["scope"] != actor.scope.wire():
            raise RuntimeFault("SCOPE_MISMATCH", "App binding scope differs")
        aliases = [x.casefold() for x in binding["aliases"]]
        if len(aliases) != len(set(aliases)):
            raise RuntimeFault("DUPLICATE_ALIAS", "App aliases repeat")
        with self.store.tx() as db:
            ref = self.store.put(
                db,
                actor.scope,
                "app-binding",
                binding["app_id"],
                binding["registry_revision"],
                binding,
            )
        return ref

    @staticmethod
    def _mentioned(name: str, text: str) -> bool:
        # Unicode word boundaries alone miss Korean particles (e.g. Cortex와).
        particle = r"(?:에서|에게|으로|하고|을|를|은|는|이|가|과|와|의|도|로|랑)"
        boundary = r"(?=$|[^\w]|" + particle + r"(?=$|[^\w]))"
        return re.search(r"(?<![\w])" + re.escape(name) + boundary, text, re.IGNORECASE) is not None

    def resolve(
        self, scope: Scope, text: str, hints: list[str]
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        latest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for ref, binding in self.store.list_objects(scope, "app-binding"):
            old = latest.get(binding["app_id"])
            if old is None or ref["revision"] > old[0]["revision"]:
                latest[binding["app_id"]] = (ref, binding)
        names: dict[str, set[str]] = {}
        for app_id, pair in latest.items():
            binding = pair[1]
            for name in {binding["app_id"], *binding["aliases"], binding["repo_identity"]}:
                names.setdefault(name.casefold(), set()).add(app_id)
        selected = set()
        if hints:
            for hint in hints:
                matches = names.get(hint.casefold(), set())
                if len(matches) != 1:
                    raise Hold(
                        "TARGET_AMBIGUOUS",
                        "A target hint is unknown or ambiguous",
                        details={"hint": hint, "candidate_app_ids": sorted(matches)},
                    )
                selected.update(matches)
        else:
            for name, matches in names.items():
                if self._mentioned(name, text):
                    if len(matches) != 1:
                        raise Hold(
                            "TARGET_AMBIGUOUS",
                            "A mentioned ID or alias refers to several apps",
                            details={"name": name, "candidate_app_ids": sorted(matches)},
                        )
                    selected.update(matches)
            if not selected and len(latest) == 1:
                selected.update(latest)
        if not selected:
            raise Hold("TARGET_REQUIRED", "No authorized app is supported by the available context")
        return [latest[k] for k in sorted(selected)]


class GoalService:
    def __init__(self, store: Store, contracts: Contracts) -> None:
        self.store, self.contracts = store, contracts
        self.apps = AppRegistry(store, contracts)

    def submit(
        self,
        actor: Actor,
        *,
        text: str,
        mode: str = "work",
        target_hints: list[str] | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        channel: str = "cli",
        external_message_id: str | None = None,
        classification: str = "internal",
        key: str,
    ) -> dict[str, Any]:
        actor.require("goal.submit")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeFault("INTENT_EMPTY", "An intent must contain meaningful text")
        value: dict[str, Any] = {
            "schema_version": "3.0.0",
            "intent_id": new_id("intent"),
            "scope": actor.scope.wire(),
            "actor": actor.wire(),
            "mode": mode,
            "text": text,
            "source_channel": channel,
            "external_message_id": external_message_id,
            "target_hints": target_hints or [],
            "attachment_refs": attachment_refs or [],
            "received_at": now(),
            "data_classification": classification,
        }
        self.contracts.validate("intent-envelope", value)
        from ..evidence.cas import ArtifactStore

        artifacts = ArtifactStore(self.store)
        for artifact in value["attachment_refs"]:
            artifacts.read(actor.scope, artifact)
        payload = {k: v for k, v in value.items() if k not in {"intent_id", "received_at"}}

        def operation(db: sqlite3.Connection) -> dict[str, Any]:
            ref = self.store.put(db, actor.scope, "intent-envelope", value["intent_id"], 1, value)
            goal_id = new_id("goal")
            self.store.cas(
                db,
                actor.scope,
                "goal",
                goal_id,
                0,
                "draft",
                {
                    "intent_ref": ref,
                    "active_contract_ref": None,
                    "active_graph_ref": None,
                    "execution_epoch": 0,
                },
            )
            self.store.event(
                db, actor.scope, "goal", goal_id, "intent.submitted", {"intent_ref": ref}
            )
            return {"intent_ref": ref, "goal_id": goal_id}

        return self.store.command(actor.scope, actor.subject_id, key, payload, operation)

    def resolve(
        self,
        actor: Actor,
        goal_id: str,
        readiness: list[dict[str, Any]],
        *,
        facts: list[dict[str, Any]] | None = None,
        na_rules: set[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        actor.require("goal.resolve")
        head = self.store.head(actor.scope, "goal", goal_id)
        if head["state"] not in {"draft", "discovering", "awaiting_decision", "blocked"}:
            raise Conflict("GOAL_STATE", "Goal is not in discovery")
        intent_ref = head["data"]["intent_ref"]
        intent = self.store.get(actor.scope, "intent-envelope", intent_ref)
        target_refs = []
        questions = []
        status = "resolved"
        findings = []
        try:
            target_refs = [
                ref
                for ref, _ in self.apps.resolve(actor.scope, intent["text"], intent["target_hints"])
            ]
            check_readiness(readiness, na_rules=na_rules)
            check_refs(self.store, actor.scope, {"readiness": readiness, "facts": facts or []})
        except Hold as exc:
            status = "hold"
            findings.append(exc.message)
        with self.store.tx() as db:
            if status == "hold":
                question_text = " / ".join(findings)
                question_id = (
                    "q-"
                    + digest(
                        {"goal": goal_id, "question": question_text, "intent_ref": intent_ref}
                    )[7:31]
                )
                q: dict[str, Any] = {
                    "schema_version": "3.0.0",
                    "question_id": question_id,
                    "scope": actor.scope.wire(),
                    "goal_id": goal_id,
                    "kind": "business_intent",
                    "question": question_text,
                    "options": [],
                    "evidence_checked": [],
                    "blocked_capabilities": [],
                    "assigned_actor_id": actor.subject_id,
                    "status": "open",
                    "answer": None,
                    "asked_at": now(),
                }
                existing = db.execute(
                    "SELECT revision,digest,data FROM objects "
                    "WHERE tenant=? AND project=? AND kind=? AND id=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (*actor.scope.keys(), "question", question_id),
                ).fetchone()
                if existing:
                    questions = [
                        {
                            "id": question_id,
                            "revision": existing["revision"],
                            "digest": existing["digest"],
                        }
                    ]
                else:
                    self.contracts.validate("question", q)
                    questions = [self.store.put(db, actor.scope, "question", question_id, 1, q)]
            value = {
                "schema_version": "3.0.0",
                "resolution_id": new_id("resolution"),
                "scope": actor.scope.wire(),
                "intent_ref": intent_ref,
                "status": status,
                "target_refs": target_refs,
                "facts": facts or [],
                "readiness": readiness,
                "question_refs": questions,
                "candidate_contract_ref": None,
                "checked_at": now(),
            }
            self.contracts.validate("resolution", value)
            ref = self.store.put(db, actor.scope, "resolution", value["resolution_id"], 1, value)
            self.store.cas(
                db,
                actor.scope,
                "goal",
                goal_id,
                head["row_version"],
                "discovering" if status == "resolved" else "awaiting_decision",
                {**head["data"], "resolution_ref": ref},
            )
            self.store.event(
                db,
                actor.scope,
                "goal",
                goal_id,
                "intent.resolved",
                {"resolution_ref": ref, "status": status},
            )
        return ref, value

    def freeze_contract(
        self,
        actor: Actor,
        contract: dict[str, Any],
        *,
        expected_version: int,
        na_rules: set[str] | None = None,
    ) -> dict[str, Any]:
        actor.require("contract.propose")
        self.contracts.validate("goal-contract", contract)
        scope = actor.scope
        head = self.store.head(scope, "goal", contract["goal_id"])
        if contract["scope"] != scope.wire():
            raise Hold("CONTRACT_SCOPE", "Contract scope must come from the authenticated actor")
        if contract["intent_ref"] != head["data"]["intent_ref"] or contract[
            "resolution_ref"
        ] != head["data"].get("resolution_ref"):
            raise Hold(
                "STALE_INTENT", "Resolve the current intent revision before freezing a contract"
            )
        if head["row_version"] != expected_version:
            raise Conflict("STALE_VERSION", "Goal changed while planning")
        resolution = self.store.get(scope, "resolution", contract["resolution_ref"])
        if resolution["status"] != "resolved" or resolution["intent_ref"] != contract["intent_ref"]:
            raise Hold("RESOLUTION_REQUIRED", "Contract must bind a resolved intent")
        intent = self.store.get(scope, "intent-envelope", contract["intent_ref"])
        if contract["mode"] != intent["mode"] or contract["targets"] != resolution["target_refs"]:
            raise Hold(
                "INTENT_BINDING",
                "Contract changes target or work/design mode without a new resolution",
            )
        previous = (
            self.store.get(scope, "goal-contract", head["data"]["active_contract_ref"])
            if head["data"].get("active_contract_ref")
            else None
        )
        if previous is None and contract["revision"] != 1:
            raise Hold("CONTRACT_REVISION", "The first frozen contract starts at revision one")
        check_contract(contract, previous=previous)
        check_readiness(resolution["readiness"], na_rules=na_rules)
        check_refs(self.store, scope, resolution)
        check_refs(self.store, scope, contract)
        from .validation import validate_bindings

        validate_bindings(self.store, self.contracts, scope, contract)
        plan = self.store.get(scope, "verification-plan", contract["verification_plan_ref"])
        bindings = {b["acceptance_id"]: b for b in plan["bindings"]}
        for criterion in contract["acceptance"]:
            if criterion["id"] not in bindings:
                raise Hold(
                    "VERIFICATION_BINDING",
                    "Acceptance has no independent verification-plan binding",
                )
            if bindings[criterion["id"]]["verifier_ref"] != criterion["verifier_ref"]:
                raise Hold("VERIFICATION_BINDING", "Contract and plan name different verifiers")
        with self.store.tx() as db:
            ref = self.store.put(
                db, scope, "goal-contract", contract["goal_id"], contract["revision"], contract
            )
            self.store.cas(
                db,
                scope,
                "goal",
                contract["goal_id"],
                expected_version,
                head["state"],
                {**head["data"], "candidate_contract_ref": ref},
            )
            self.store.event(
                db, scope, "goal", contract["goal_id"], "contract.frozen", {"contract_ref": ref}
            )
        return ref

    def refine_intent(
        self,
        actor: Actor,
        question_ref: dict[str, Any],
        text: str,
        *,
        target_hints: list[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve pre-contract questions without inventing a nonexistent contract ref.

        The normative Question answer requires a real contract. Before that exists,
        append an authenticated Intent revision and supersede the initial question.
        The separate refinement receipt preserves who answered what and why.
        """
        actor.require("question.answer")
        scope = actor.scope
        question = self.store.get(scope, "question", question_ref)
        if question["assigned_actor_id"] != actor.subject_id:
            raise RuntimeFault("QUESTION_ACTOR", "Question belongs to another actor")
        check_question_kind(actor, question["kind"])  # H-4: refinement too
        if question["status"] != "open":
            raise Conflict("QUESTION_CLOSED", "Question is no longer open")
        goal = self.store.head(scope, "goal", question["goal_id"])
        if goal["data"].get("active_contract_ref"):
            raise Hold("STEERING_REQUIRED", "An active contract must use revision-bound steering")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeFault("ANSWER_EMPTY", "A nonempty answer is required")
        previous_ref = goal["data"]["intent_ref"]
        previous = self.store.get(scope, "intent-envelope", previous_ref)
        value = {
            **previous,
            "actor": actor.wire(),
            "text": previous["text"] + "\n\nAuthenticated clarification:\n" + text,
            "target_hints": target_hints if target_hints is not None else previous["target_hints"],
            "received_at": now(),
        }
        self.contracts.validate("intent-envelope", value)
        superseded = {**question, "status": "superseded"}
        self.contracts.validate("question", superseded)
        with self.store.tx() as db:
            current = self.store.head(scope, "goal", question["goal_id"], db=db)
            if current["row_version"] != goal["row_version"]:
                raise Conflict("ANSWER_REVISION", "Goal changed during clarification")
            latest = db.execute(
                "SELECT MAX(revision) FROM objects "
                "WHERE tenant=? AND project=? AND kind=? AND id=?",
                (*scope.keys(), "question", question["question_id"]),
            ).fetchone()[0]
            if latest != question_ref["revision"]:
                raise Conflict("QUESTION_REVISION", "Question has already changed")
            intent_ref = self.store.put(
                db,
                scope,
                "intent-envelope",
                value["intent_id"],
                previous_ref["revision"] + 1,
                value,
            )
            new_question_ref = self.store.put(
                db,
                scope,
                "question",
                question["question_id"],
                question_ref["revision"] + 1,
                superseded,
            )
            receipt = {
                "refinement_id": new_id("refinement"),
                "scope": scope.wire(),
                "question_ref": question_ref,
                "previous_intent_ref": previous_ref,
                "new_intent_ref": intent_ref,
                "actor": actor.wire(),
                "text": text,
                "recorded_at": now(),
            }
            receipt_ref = self.store.put(
                db, scope, "intent-refinement", receipt["refinement_id"], 1, receipt
            )
            self.store.cas(
                db,
                scope,
                "goal",
                question["goal_id"],
                current["row_version"],
                "draft",
                {
                    **current["data"],
                    "intent_ref": intent_ref,
                    "resolution_ref": None,
                    "candidate_contract_ref": None,
                },
            )
            self.store.event(
                db,
                scope,
                "goal",
                question["goal_id"],
                "intent.refined",
                {"refinement_ref": receipt_ref, "question_ref": new_question_ref},
            )
        return {
            "intent_ref": intent_ref,
            "refinement_ref": receipt_ref,
            "question_ref": new_question_ref,
            "status": "discovery_required",
        }

    def answer(
        self,
        actor: Actor,
        question_ref: dict[str, Any],
        text: str,
        contract_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if contract_ref is None:
            return self.refine_intent(actor, question_ref, text)
        actor.require("question.answer")
        q = self.store.get(actor.scope, "question", question_ref)
        if q["assigned_actor_id"] != actor.subject_id:
            raise RuntimeFault("QUESTION_ACTOR", "Question is assigned to another actor")
        check_question_kind(actor, q["kind"])  # H-4
        if q["status"] != "open":
            raise Conflict("QUESTION_CLOSED", "Question already has a terminal resolution")
        current = self.store.head(actor.scope, "goal", q["goal_id"])["data"].get(
            "active_contract_ref"
        )
        if current is not None and current != contract_ref:
            raise Conflict("ANSWER_REVISION", "Answer does not apply to the active contract")
        q = {
            **q,
            "status": "answered",
            "answer": {
                "actor": actor.wire(),
                "text": text,
                "applies_to_contract_ref": contract_ref,
                "answered_at": now(),
            },
        }
        self.contracts.validate("question", q)
        with self.store.tx() as db:
            rows = db.execute(
                "SELECT MAX(revision) FROM objects "
                "WHERE tenant=? AND project=? AND kind=? AND id=?",
                (*actor.scope.keys(), "question", q["question_id"]),
            ).fetchone()
            if rows[0] != question_ref["revision"]:
                raise Conflict("QUESTION_REVISION", "Question was updated")
            return self.store.put(
                db, actor.scope, "question", q["question_id"], question_ref["revision"] + 1, q
            )


class ContractCritic:
    def __init__(self, contracts: Contracts) -> None:
        self.contracts = contracts

    def review(
        self,
        candidate: dict[str, Any],
        *,
        previous: dict[str, Any] | None = None,
        model_findings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        try:
            self.contracts.validate("goal-contract", candidate)
            check_contract(candidate, previous=previous)
        except RuntimeFault as exc:
            findings.append({"code": exc.code, "severity": "blocking", "statement": exc.message})
        # Model findings can add a hold, never grant permission or overrule deterministic findings.
        for finding in model_findings or []:
            if finding.get("severity") in {"blocking", "critical"}:
                findings.append(finding)
        return {
            "contract_digest": digest(candidate),
            "outcome": "hold" if findings else "pass",
            "findings": findings,
        }

    def negotiate(
        self,
        initial: dict[str, Any],
        planner: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
        reviewer: Callable[[dict[str, Any]], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        candidate = deepcopy(initial)
        for round_number in range(2):
            report = self.review(candidate, model_findings=reviewer(deepcopy(candidate)))
            if report["outcome"] == "pass":
                return candidate
            if round_number == 0:
                candidate = deepcopy(planner(deepcopy(candidate), deepcopy(report)))
        raise Hold(
            "CRITIC_LIMIT", "Two critique rounds did not resolve the contract", details=report
        )
