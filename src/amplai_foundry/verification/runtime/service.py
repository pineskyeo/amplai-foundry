"""Independent, signed verification of actual artifact bytes; no worker self-grading."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from jsonschema import Draft202012Validator

from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.gates import Observation
from amplai_foundry.runtime.contracts.identity import (
    canonical,
    digest,
    new_id,
    now,
    sign,
    verify_signature,
)
from amplai_foundry.runtime.contracts.semantics import resolve_ref
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.storage.store import Scope


@dataclass(frozen=True)
class VerificationObservation:
    outcome: str
    reason: str
    details: dict[str, Any]
    exit_code: int | None = None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member: " + key)
        result[key] = value
    return result


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def _reject_constant(text: str) -> Any:
    raise ValueError("Non-JSON numeric constant: " + text)


class JsonVerifier:
    def __init__(self, schema: dict[str, Any], *, equals: dict[str, Any] | None = None) -> None:
        Draft202012Validator.check_schema(schema)
        self.schema, self.equals = schema, equals or {}

    def __call__(self, raw: bytes) -> VerificationObservation:
        try:
            value = json.loads(
                raw,
                object_pairs_hook=_strict_object,
                parse_float=_finite_float,
                parse_constant=_reject_constant,
            )
        except (ValueError, UnicodeDecodeError):
            return VerificationObservation("fail", "Artifact is not valid JSON", {"parse": False})
        errors = list(Draft202012Validator(self.schema).iter_errors(value))
        for pointer, expected in self.equals.items():
            current = value
            try:
                for part in pointer.lstrip("/").split("/") if pointer else []:
                    key = part.replace("~1", "/").replace("~0", "~")
                    current = current[int(key)] if isinstance(current, list) else current[key]
                if current != expected:
                    errors.append("Value does not match at " + pointer)
            except (KeyError, IndexError, TypeError, ValueError):
                errors.append("Missing field at " + pointer)
        return VerificationObservation(
            "fail" if errors else "pass",
            "Pinned schema and semantic field checks",
            {
                "schema_digest": digest(self.schema),
                "semantic_rule_digest": digest(self.equals),
                "error_count": len(errors),
                "errors": [str(e)[:512] for e in errors],
            },
        )


class VerificationService:
    def __init__(
        self,
        runtime: Any,
        *,
        signer: Ed25519PrivateKey,
        key_id: str,
        trusted_keys: dict[str, Ed25519PublicKey],
    ) -> None:
        self.runtime, self.store, self.artifacts = runtime, runtime.store, runtime.artifacts
        self.signer, self.key_id, self.keys = signer, key_id, trusted_keys
        self.runners: dict[str, Callable[[bytes], VerificationObservation]] = {}
        self.global_runners: dict[str, Callable[..., VerificationObservation]] = {}
        self.regression_runners: dict[str, Callable[..., VerificationObservation]] = {}
        self.human_receipts: dict[str, dict[str, Any]] = {}

    def _assert_current_run(
        self, actor: Actor, run: dict[str, Any], *, db: sqlite3.Connection | None = None
    ) -> None:
        if actor.kind != "service":
            raise RuntimeFault("VERIFIER_IDENTITY", "Trusted verification needs a service identity")
        record = run["data"]["record"]
        producer = run["data"].get("lease", {}).get("worker_id")
        if producer == actor.subject_id:
            raise RuntimeFault(
                "VERIFIER_INDEPENDENCE", "A producer cannot verify or finalize its own output"
            )
        work = self.store.head(actor.scope, "work", record["work_id"], db=db)
        goal = self.store.head(actor.scope, "goal", record["root_goal_id"], db=db)
        if (
            goal["data"]["active_contract_ref"] != record["contract_ref"]
            or goal["data"]["active_graph_ref"] != record["graph_ref"]
            or goal["data"]["execution_epoch"] != work["data"]["execution_epoch"]
            or work["data"].get("current_run_id") != record["run_id"]
        ):
            raise Hold(
                "STALE_VERIFICATION",
                "Old revisions, epochs or attempts cannot receive accepted verification",
            )
        if goal["state"] not in {"active", "verifying"} or goal["data"].get("admission_paused"):
            raise Hold(
                "VERIFICATION_GOAL_STATE",
                "Resolve cancellation or steering before accepting verification",
            )

    def register(
        self, profile_ref: dict[str, Any], runner: Callable[[bytes], VerificationObservation]
    ) -> None:
        key = digest(profile_ref)
        if key in self.runners:
            raise RuntimeFault("VERIFIER_DUPLICATE", "A verifier revision is already bound")
        self.runners[key] = runner

    def register_global(
        self, profile_ref: dict[str, Any], runner: Callable[..., VerificationObservation]
    ) -> None:
        key = digest(profile_ref)
        if key in self.global_runners and self.global_runners[key] is not runner:
            raise RuntimeFault("VERIFIER_DUPLICATE", "Global verifier revision is already bound")
        self.global_runners[key] = runner

    def register_regression(
        self, profile_ref: dict[str, Any], runner: Callable[..., VerificationObservation]
    ) -> None:
        key = digest(profile_ref)
        if key in self.regression_runners:
            raise RuntimeFault(
                "VERIFIER_DUPLICATE", "Protected regression revision is already bound"
            )
        self.regression_runners[key] = runner

    def human_accept(
        self,
        actor: Actor,
        run_id: str,
        acceptance_id: str,
        subject: dict[str, Any],
        outcome: str,
        reason: str,
        *,
        approval_ref: dict[str, Any],
        approval_check: Callable[..., Any],
    ) -> dict[str, Any]:
        actor.require("acceptance.human")
        if actor.kind != "human" or outcome not in {"pass", "fail"} or not reason.strip():
            raise Hold("HUMAN_RECEIPT", "Explicit authenticated human acceptance is required")
        run = self.store.head(actor.scope, "run", run_id)
        record = run["data"]["record"]
        worker = self.store.conn.execute(
            "SELECT worker_id FROM leases WHERE tenant=? AND project=? AND run_id=?",
            (*actor.scope.keys(), run_id),
        ).fetchone()
        if worker and worker[0] == actor.subject_id:
            raise Hold("HUMAN_INDEPENDENCE", "Producer cannot accept its own result")
        self.artifacts.read(actor.scope, subject)
        receipt = {
            "receipt_id": new_id("human-acceptance"),
            "scope": actor.scope.wire(),
            "run_id": run_id,
            "contract_ref": record["contract_ref"],
            "graph_ref": record["graph_ref"],
            "acceptance_id": acceptance_id,
            "subject_artifact": subject,
            "outcome": outcome,
            "reason": reason,
            "actor": actor.wire(),
        }
        approval_check(
            actor.scope,
            approval_ref,
            "acceptance.human",
            digest({k: v for k, v in receipt.items() if k != "receipt_id"}),
        )
        receipt["approval_ref"] = approval_ref
        receipt["issued_at"] = now()
        with self.store.tx() as db:
            receipt["attestation_ref"] = self._attest(db, actor.scope, "human-acceptance", receipt)
            ref = self.store.put(
                db, actor.scope, "human-acceptance", receipt["receipt_id"], 1, receipt
            )
            self.store.event(
                db, actor.scope, "run", run_id, "acceptance.human_recorded", {"receipt_ref": ref}
            )
        receipt_ref: dict[str, Any] = ref
        return receipt_ref

    def _human_observation(
        self, scope: Scope, run_id: str, acceptance_id: str, subject: dict[str, Any]
    ) -> VerificationObservation:
        receipts: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for ref, receipt in self.store.list_objects(scope, "human-acceptance"):
            if (
                receipt["run_id"] == run_id
                and receipt["acceptance_id"] == acceptance_id
                and receipt["subject_artifact"] == subject
            ):
                self.validate_attested(scope, "human-acceptance", receipt)
                receipts.append((ref, receipt))
        if not receipts:
            return VerificationObservation(
                "inconclusive", "No exact independently authenticated human acceptance receipt", {}
            )
        ref, receipt = receipts[-1]
        return VerificationObservation(
            receipt["outcome"], receipt["reason"], {"human_receipt_ref": ref}
        )

    def _attest(
        self, db: sqlite3.Connection, scope: Scope, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        value = sign(
            {
                "attestation_id": new_id("attest"),
                "scope": scope.wire(),
                "subject_kind": kind,
                "payload_digest": digest(payload),
                "payload": payload,
            },
            self.key_id,
            self.signer,
        )
        attested: dict[str, Any] = self.store.put(
            db, scope, "attestation", value["attestation_id"], 1, value
        )
        return attested

    def validate_attested(self, scope: Scope, kind: str, record: dict[str, Any]) -> None:
        attestation = self.store.get(scope, "attestation", record["attestation_ref"])
        verify_signature(attestation, self.keys)
        payload = {k: v for k, v in record.items() if k != "attestation_ref"}
        if (
            attestation["subject_kind"] != kind
            or attestation["payload_digest"] != digest(payload)
            or attestation["payload"] != payload
        ):
            raise Hold(
                "ATTESTATION_BINDING", "Attestation does not cover this exact evidence/verdict"
            )

    def verify(
        self, actor: Actor, run_id: str, acceptance_id: str, subject: dict[str, Any]
    ) -> dict[str, Any]:
        actor.require("verifier.run")
        scope = actor.scope
        if actor.kind != "service":
            raise RuntimeFault(
                "VERIFIER_IDENTITY", "Trusted verification requires a bound service identity"
            )
        run = self.store.head(scope, "run", run_id)
        record = run["data"]["record"]
        self._assert_current_run(actor, run)
        if run["state"] != "verifying" or not run["data"]["process_stopped"]:
            raise Hold(
                "VERIFICATION_RUN_STATE", "Producer must stop before independent verification"
            )
        contract = self.store.get(scope, "goal-contract", record["contract_ref"])
        criterion = next((a for a in contract["acceptance"] if a["id"] == acceptance_id), None)
        if not criterion:
            raise RuntimeFault("UNKNOWN_ACCEPTANCE", "Criterion not present in the contract")
        plan = self.store.get(scope, "verification-plan", contract["verification_plan_ref"])
        binding = next((b for b in plan["bindings"] if b["acceptance_id"] == acceptance_id), None)
        if not binding:
            raise Hold("VERIFIER_BINDING", "No independent verification-plan binding")
        work = self.store.head(scope, "work", record["work_id"])
        if acceptance_id not in work["data"]["node"]["acceptance_ids"]:
            raise Hold("WORK_ACCEPTANCE", "This acceptance is not assigned to this work")
        if subject != work["data"]["outputs"].get(binding["subject_selector"]):
            raise Hold("VERIFIER_SUBJECT", "Subject is not the named WorkGraph output")
        profile = self.store.get(scope, "verifier-profile", criterion["verifier_ref"])
        if profile["owner_subject_id"] != actor.subject_id or not profile["protected"]:
            raise RuntimeFault(
                "VERIFIER_OWNERSHIP", "Actor does not own a protected verifier profile"
            )
        if (
            binding["environment_ref"] != record["environment_ref"]
            or profile["environment_ref"] != record["environment_ref"]
        ):
            raise Hold("VERIFIER_ENVIRONMENT", "Verification must use the pinned environment")
        runner = self.runners.get(digest(criterion["verifier_ref"]))
        if runner is None and profile["kind"] != "human":
            raise Hold(
                "VERIFIER_NOT_INSTALLED", "No trusted runner for this immutable verifier profile"
            )
        raw = self.artifacts.read(scope, subject)
        started = now()
        if profile["kind"] == "human":
            observation = self._human_observation(scope, run_id, acceptance_id, subject)
        else:
            if runner is None:
                raise Hold("VERIFIER_RUNNER", "No trusted runner is bound to this verifier profile")
            observation = runner(raw)
        if not isinstance(observation, VerificationObservation) or observation.outcome not in {
            "pass",
            "fail",
            "inconclusive",
        }:
            raise Hold("VERIFIER_RESULT", "Verifier did not produce a typed observation")
        if criterion["human_acceptance_required"] and profile["kind"] != "human":
            observation = VerificationObservation(
                "inconclusive",
                "Mandatory human acceptance has not been supplied",
                observation.details,
                observation.exit_code,
            )
        finished = now()
        observed = self.artifacts.admit(
            scope,
            canonical(
                {
                    "outcome": observation.outcome,
                    "reason": observation.reason,
                    "details": observation.details,
                }
            ),
            "application/json",
            trust="verifier",
        )
        evidence = {
            "schema_version": "3.0.0",
            "evidence_id": new_id("evidence"),
            "scope": scope.wire(),
            "run_id": run_id,
            "contract_ref": record["contract_ref"],
            "graph_ref": record["graph_ref"],
            "subject_artifact": subject,
            "verifier_ref": criterion["verifier_ref"],
            "environment_ref": record["environment_ref"],
            "observations_artifact": observed,
            "started_at": started,
            "finished_at": finished,
            "exit_code": observation.exit_code,
            "producer": actor.wire(),
            "trust": "server_verified",
            "redacted": False,
        }
        with self.store.tx() as db:
            current = self.store.head(scope, "run", run_id, db=db)
            if current["row_version"] != run["row_version"] or current["state"] != "verifying":
                raise Conflict("VERIFIER_RACE", "Run changed during verification")
            self._assert_current_run(actor, current, db=db)
            evidence["attestation_ref"] = self._attest(db, scope, "evidence", evidence)
            self.runtime.contracts.validate("evidence", evidence)
            evidence_ref = self.store.put(
                db, scope, "evidence", evidence["evidence_id"], 1, evidence
            )
            verdict = {
                "schema_version": "3.0.0",
                "verdict_id": new_id("verdict"),
                "scope": scope.wire(),
                "contract_ref": record["contract_ref"],
                "graph_ref": record["graph_ref"],
                "acceptance_id": acceptance_id,
                "subject_artifact": subject,
                "evidence_refs": [evidence_ref],
                "verifier_ref": criterion["verifier_ref"],
                "outcome": observation.outcome,
                "reason": observation.reason,
                "waiver_decision_ref": None,
                "issued_at": finished,
            }
            verdict["attestation_ref"] = self._attest(db, scope, "verdict", verdict)
            self.runtime.contracts.validate("verdict", verdict)
            verdict_ref = self.store.put(db, scope, "verdict", verdict["verdict_id"], 1, verdict)
            data = {
                **current["data"],
                "record": {
                    **current["data"]["record"],
                    "evidence_refs": [*current["data"]["record"]["evidence_refs"], evidence_ref],
                    "verdict_refs": [*current["data"]["record"]["verdict_refs"], verdict_ref],
                },
            }
            self.runtime._run_state(db, scope, run_id, current, "verifying", data)
            self.store.event(
                db,
                scope,
                "run",
                run_id,
                "verdict.issued",
                {"verdict_ref": verdict_ref, "outcome": observation.outcome},
            )
        verdict_result: dict[str, Any] = verdict_ref
        return verdict_result

    def check_verdict(
        self,
        scope: Scope,
        ref: dict[str, Any],
        contract_ref: dict[str, Any],
        graph_ref: dict[str, Any],
        expected_subject: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        verdict: dict[str, Any] = self.store.get(scope, "verdict", ref)
        self.runtime.contracts.validate("verdict", verdict)
        self.validate_attested(scope, "verdict", verdict)
        if verdict["contract_ref"] != contract_ref or verdict["graph_ref"] != graph_ref:
            raise Hold("VERDICT_REVISION", "Verdict refers to another contract/graph revision")
        if expected_subject is not None and verdict["subject_artifact"] != expected_subject:
            raise Hold("VERDICT_SUBJECT", "Verdict refers to an old or different artifact")
        self.artifacts.read(scope, verdict["subject_artifact"])
        for evidence_ref in verdict["evidence_refs"]:
            evidence = self.store.get(scope, "evidence", evidence_ref)
            self.validate_attested(scope, "evidence", evidence)
            self.runtime.contracts.validate("evidence", evidence)
            if evidence["contract_ref"] != contract_ref or evidence["graph_ref"] != graph_ref:
                raise Hold(
                    "EVIDENCE_REVISION", "Evidence must cover the exact current contract and graph"
                )
            if (
                evidence["trust"] != "server_verified"
                or evidence["subject_artifact"] != verdict["subject_artifact"]
                or evidence["verifier_ref"] != verdict["verifier_ref"]
            ):
                raise Hold(
                    "EVIDENCE_TRUST", "Evidence is not trusted or covers a different subject"
                )
            observation = json.loads(
                self.artifacts.read(scope, evidence["observations_artifact"], trusted=True)
            )
            if observation["outcome"] != verdict["outcome"]:
                raise Hold(
                    "VERDICT_OBSERVATION", "Verdict differs from the actual verifier observation"
                )
        return verdict

    def finish_work(self, actor: Actor, run_id: str) -> dict[str, Any]:
        actor.require("verifier.run")
        scope = actor.scope
        run = self.store.head(scope, "run", run_id)
        record = run["data"]["record"]
        work = self.store.head(scope, "work", record["work_id"])
        self._assert_current_run(actor, run)
        if run["state"] != "verifying" or work["state"] != "verifying":
            raise Hold("WORK_NOT_VERIFYING", "Work must be awaiting trusted verification")
        with self.store._lock:
            reservation = self.store.conn.execute(
                "SELECT status FROM reservations WHERE tenant=? AND project=? AND run_id=?",
                (*scope.keys(), run_id),
            ).fetchone()
        if reservation is None or reservation["status"] in {"overrun", "active", "suspended"}:
            raise Hold(
                "BUDGET_VERIFICATION_HOLD",
                "Actual overrun or unsettled execution cannot become verified work",
            )
        contract = self.store.get(scope, "goal-contract", record["contract_ref"])
        plan = self.store.get(scope, "verification-plan", contract["verification_plan_ref"])
        bindings = {b["acceptance_id"]: b for b in plan["bindings"]}
        results = {}
        for ref in record["verdict_refs"]:
            verdict = self.store.get(scope, "verdict", ref)
            binding = bindings[verdict["acceptance_id"]]
            value = self.check_verdict(
                scope,
                ref,
                record["contract_ref"],
                record["graph_ref"],
                work["data"]["outputs"].get(binding["subject_selector"]),
            )
            results[value["acceptance_id"]] = value
        required = {c["id"] for c in contract["acceptance"] if c["mandatory"]} & set(
            work["data"]["node"]["acceptance_ids"]
        )
        if any(k not in results for k in required):
            raise Hold("VERDICT_COVERAGE", "Mandatory verification has not run")
        passed = all(results[k]["outcome"] == "pass" for k in required)
        if any(results[k]["outcome"] == "inconclusive" for k in required):
            raise Hold(
                "VERIFICATION_INCONCLUSIVE", "Inconclusive is not success or a repair authorization"
            )
        with self.store.tx() as db:
            current = self.store.head(scope, "run", run_id, db=db)
            w = self.store.head(scope, "work", record["work_id"], db=db)
            if (
                current["row_version"] != run["row_version"]
                or w["row_version"] != work["row_version"]
            ):
                raise Conflict("VERIFY_RACE", "Work changed during final verification checks")
            self._assert_current_run(actor, current, db=db)
            if passed:
                obs = {
                    "G-12": Observation.check(
                        True, "Every mandatory work criterion has an independently attested PASS"
                    )
                }
                rs, _ = self.runtime.machines.transition(
                    "run", run["state"], "verification_pass", obs
                )
                ws, _ = self.runtime.machines.transition(
                    "work", work["state"], "verification_pass", obs
                )
                self.runtime._run_state(db, scope, run_id, current, rs, current["data"])
                self.store.cas(
                    db,
                    scope,
                    "work",
                    record["work_id"],
                    w["row_version"],
                    ws,
                    {**w["data"], "verdict_refs": record["verdict_refs"]},
                )
                db.execute(
                    "DELETE FROM resources WHERE tenant=? AND project=? AND run_id=?",
                    (*scope.keys(), run_id),
                )
            else:
                signature = digest(
                    {
                        k: {"outcome": v["outcome"], "reason": v["reason"]}
                        for k, v in results.items()
                    }
                )
                data = {**current["data"], "record": {**record, "failure_signatures": [signature]}}
                self.runtime._run_state(db, scope, run_id, current, "failed", data)
                signatures = [*w["data"]["failure_signatures"], signature]
                node = w["data"]["node"]
                can_retry = (
                    node["strategy"] != "direct"
                    and w["data"]["attempts"]
                    < min(
                        node["budget"]["max_attempts"],
                        contract["budget"]["max_attempts"],
                        self.runtime.contracts.defaults[
                            "max_verifier_repair_attempts_including_initial"
                        ],
                    )
                    and not (len(signatures) >= 3 and len(set(signatures[-3:])) == 1)
                )
                state = "ready" if can_retry else "failed"
                self.store.cas(
                    db,
                    scope,
                    "work",
                    record["work_id"],
                    w["row_version"],
                    state,
                    {
                        **w["data"],
                        "failure_signatures": signatures,
                        "last_failed_verdict_refs": record["verdict_refs"],
                        "outputs": {},
                    },
                )
                db.execute(
                    "DELETE FROM resources WHERE tenant=? AND project=? AND run_id=?",
                    (*scope.keys(), run_id),
                )
            self.store.event(
                db,
                scope,
                "run",
                run_id,
                "work.verified",
                {"pass": passed, "work_id": record["work_id"]},
            )
        return {"work_id": record["work_id"], "outcome": "pass" if passed else "fail"}

    def finish_goal(
        self,
        actor: Actor,
        goal_id: str,
        global_checker: Callable[..., VerificationObservation] | None = None,
    ) -> dict[str, Any]:
        actor.require("verifier.run")
        scope = actor.scope
        head = self.store.head(scope, "goal", goal_id)
        if actor.kind != "service":
            raise RuntimeFault(
                "VERIFIER_IDENTITY", "Global verification requires a service identity"
            )
        if head["state"] not in {"active", "verifying"}:
            raise Hold("GOAL_NOT_VERIFYING", "Goal has not completed its required work")
        contract_ref = head["data"]["active_contract_ref"]
        graph_ref = head["data"]["active_graph_ref"]
        contract = self.store.get(scope, "goal-contract", contract_ref)
        graph = self.store.get(scope, "workgraph", graph_ref)
        _, global_profile = resolve_ref(self.store, scope, graph["global_verification_ref"])
        global_owner = global_profile.get("owner_subject_id", global_profile.get("owner"))
        if global_owner != actor.subject_id:
            raise RuntimeFault(
                "GLOBAL_VERIFIER_OWNERSHIP", "Actor does not own the pinned global verifier"
            )
        all_verdicts = []
        all_outputs = {}
        for node in graph["nodes"]:
            work = self.store.head(scope, "work", node["work_id"])
            if work["state"] != "succeeded":
                raise Hold(
                    "WORK_UNFINISHED",
                    "Every required graph work must succeed before integration acceptance",
                )
            self._assert_current_run(
                actor, self.store.head(scope, "run", work["data"]["current_run_id"])
            )
            all_outputs[node["node_id"]] = work["data"]["outputs"]
            for ref in work["data"]["verdict_refs"]:
                all_verdicts.append(self.check_verdict(scope, ref, contract_ref, graph_ref))
        required = {c["id"] for c in contract["acceptance"] if c["mandatory"]}
        if not required <= {v["acceptance_id"] for v in all_verdicts if v["outcome"] == "pass"}:
            raise Hold("GLOBAL_COVERAGE", "Global mandatory acceptance is not covered")
        with self.store._lock:
            effects = self.store.conn.execute(
                "SELECT data FROM heads WHERE tenant=? AND project=? AND kind='effect' "
                "AND state IN ('prepared','dispatched','unknown')",
                scope.keys(),
            ).fetchall()
        for row in effects:
            request = json.loads(row["data"])["request"]
            if request["contract_ref"] == contract_ref:
                raise Hold("EFFECT_PENDING", "Unresolved effects prevent goal success")
        # Exact immutable profile binding, not a callback supplied by a worker.
        installed = self.global_runners.get(digest(graph["global_verification_ref"]))
        if installed is None or (global_checker is not None and global_checker is not installed):
            raise Hold(
                "GLOBAL_VERIFIER_NOT_INSTALLED",
                "No exact trusted binding for the graph integration verifier",
            )
        if head["data"].get("admission_paused"):
            raise Hold("STEERING_PENDING", "Resolve queued steering before declaring goal success")
        totals = self.runtime.budgets.totals(scope, goal_id)
        if (
            totals["overruns"]
            or totals["reserved_or_spent_tokens"] > contract["budget"]["max_tokens"]
        ):
            raise Hold(
                "ROOT_BUDGET_OVERRUN",
                "Root budget violations cannot be accepted as successful work",
            )
        cap = contract["budget"]["max_cost_microunits"]
        spent = totals["reserved_or_spent_cost_microunits"]
        if cap is not None and (spent is None or spent > cap):
            raise Hold(
                "ROOT_COST_OVERRUN", "Unknown unbounded or excessive root cost prevents completion"
            )
        plan = self.store.get(scope, "verification-plan", contract["verification_plan_ref"])
        regressions = []
        for regression_ref in plan["protected_regression_refs"]:
            runner = self.regression_runners.get(digest(regression_ref))
            if runner is None:
                raise Hold(
                    "PROTECTED_REGRESSION_MISSING",
                    "A mandatory protected regression runner is not installed",
                )
            result = runner(contract, graph, all_outputs)
            if not isinstance(result, VerificationObservation) or result.outcome != "pass":
                raise Hold(
                    "PROTECTED_REGRESSION_FAILED", "A mandatory protected regression did not pass"
                )
            evidence = self.artifacts.admit(
                scope,
                canonical(
                    {
                        "profile_ref": regression_ref,
                        "outcome": result.outcome,
                        "reason": result.reason,
                        "details": result.details,
                    }
                ),
                "application/json",
                trust="verifier",
            )
            regressions.append(evidence)
        observation = installed(contract, graph, all_outputs)
        if not isinstance(observation, VerificationObservation) or observation.outcome != "pass":
            raise Hold(
                "INTEGRATION_FAILED",
                "Independent integration/freshness acceptance has not passed",
                details={"reason": getattr(observation, "reason", None)},
            )
        artifact = self.artifacts.admit(
            scope,
            canonical(
                {
                    "outcome": observation.outcome,
                    "details": observation.details,
                    "reason": observation.reason,
                }
            ),
            "application/json",
            trust="verifier",
        )
        value = {
            "goal_verification_id": new_id("goalcheck"),
            "scope": scope.wire(),
            "contract_ref": contract_ref,
            "graph_ref": graph_ref,
            "global_verification_ref": graph["global_verification_ref"],
            "protected_regression_artifacts": regressions,
            "observations_artifact": artifact,
            "verifier_actor": actor.wire(),
            "issued_at": now(),
            "outcome": "pass",
        }
        with self.store.tx() as db:
            current = self.store.head(scope, "goal", goal_id, db=db)
            if current["row_version"] != head["row_version"]:
                raise Conflict("GLOBAL_VERIFY_RACE", "Goal changed during integration verification")
            value["attestation_ref"] = self._attest(db, scope, "goal-verification", value)
            ref = self.store.put(
                db, scope, "goal-verification", value["goal_verification_id"], 1, value
            )
            observations = {
                g: Observation.check(True, r)
                for g, r in {
                    "G-11": "Actual evidence bytes verified",
                    "G-12": "All required work has independent PASS",
                    "G-13": "All mandatory and integration criteria passed on current refs",
                }.items()
            }
            state = current["state"]
            if state == "active":
                state, _ = self.runtime.machines.transition(
                    "goal", state, "all_required_work_finished", observations
                )
            state, _ = self.runtime.machines.transition("goal", state, "verify_pass", observations)
            self.store.cas(
                db,
                scope,
                "goal",
                goal_id,
                current["row_version"],
                state,
                {**current["data"], "goal_verification_ref": ref},
            )
            self.store.event(db, scope, "goal", goal_id, "goal.verified", {"verification_ref": ref})
        verification_ref: dict[str, Any] = ref
        return verification_ref
