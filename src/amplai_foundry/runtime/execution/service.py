"""Deterministic orchestration. Workers produce observations, not success or authority."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..budgets.service import BudgetService
from ..contracts.authority import Actor, Authority, intersect_capabilities
from ..contracts.gates import Observation, StateMachines
from ..contracts.identity import canonical, digest, new_id, now
from ..contracts.registry import Contracts
from ..contracts.semantics import check_context, check_contract, check_refs, resolve_ref
from ..errors import Conflict, Hold, RuntimeFault
from ..graphs.compiler import GraphCompiler, validate_graph
from ..storage.store import Scope, Store

if TYPE_CHECKING:
    from ..evidence.cas import ArtifactStore

WORKER_EVENTS = frozenset(
    {
        "run.started",
        "run.progress",
        "run.usage",
        "artifact.produced",
        "tool.pending",
        "tool.result",
        "run.stopped",
    }
)
PROFILE_REFS = ("composition_ref", "driver_profile_ref", "model_profile_ref", "environment_ref")


@dataclass(frozen=True)
class Lease:
    lease_id: str
    run_id: str
    work_id: str
    worker_id: str
    fencing_token: int
    owner_epoch: int
    expires_at_epoch: float

    def wire(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Runtime:
    def __init__(
        self, store: Store, contracts: Contracts, authority: Authority, artifacts: ArtifactStore
    ) -> None:
        self.store, self.contracts, self.authority, self.artifacts = (
            store,
            contracts,
            authority,
            artifacts,
        )
        self.machines = StateMachines(contracts)
        self.graphs = GraphCompiler(contracts)
        self.budgets = BudgetService(store)

    def save_graph(
        self, actor: Actor, draft: dict[str, Any], contract_ref: dict[str, Any]
    ) -> dict[str, Any]:
        actor.require("graph.propose")
        contract = self.store.get(actor.scope, "goal-contract", contract_ref)
        graph = self.graphs.compile(draft, contract, contract_ref)
        check_refs(self.store, actor.scope, graph)
        from ..goals.validation import validate_bindings

        validate_bindings(self.store, self.contracts, actor.scope, contract)
        plan = self.store.get(actor.scope, "verification-plan", contract["verification_plan_ref"])
        bindings = {b["acceptance_id"]: b for b in plan["bindings"]}
        for node in graph["nodes"]:
            target = self.store.get(actor.scope, "app-binding", node["target_ref"])
            installed = {digest(r) for r in target["verifier_profile_refs"]}
            if digest(node["verification_profile_ref"]) not in installed:
                raise Hold("NODE_VERIFIER", "Target app has not installed this verifier revision")
            if len(
                intersect_capabilities(
                    node["capabilities"], target["requested_capabilities_ceiling"]
                )
            ) != len(node["capabilities"]):
                raise Hold("NODE_CAPABILITY", "One app cannot lend authority to another")
            ports = {p["name"] for p in node["produces"]}
            for criterion_id in node["acceptance_ids"]:
                binding = bindings[criterion_id]
                if binding["verifier_ref"] != node["verification_profile_ref"]:
                    raise Hold(
                        "NODE_VERIFIER", "Work and acceptance name different verifier profiles"
                    )
                if binding["subject_selector"] not in ports:
                    raise Hold("NODE_SUBJECT", "A verifier subject must be an output of this work")
        with self.store.tx() as db:
            return self.store.put(
                db, actor.scope, "workgraph", graph["graph_id"], graph["revision"], graph
            )

    def _profile(
        self,
        scope: Scope,
        profile: dict[str, Any],
        capabilities: list[dict[str, Any]],
        classification: str,
    ) -> dict[str, Any]:
        for field in PROFILE_REFS:
            resolve_ref(self.store, scope, profile[field])
        driver = self.store.get(scope, "driver-capabilities", profile["driver_profile_ref"])
        if driver["maturity"] != "qualified":
            raise Hold(
                "DRIVER_UNQUALIFIED",
                "Experimental or disabled drivers cannot receive unattended work",
            )
        if not set(driver["qualified"]) <= set(driver["observed"]) or not set(
            driver["observed"]
        ) <= set(driver["declared"]):
            raise Hold("DRIVER_CAPABILITIES", "Driver capability layers are inconsistent")
        _, qualification = resolve_ref(self.store, scope, driver["qualification_report_ref"])
        if (
            qualification.get("status") != "pass"
            or qualification.get("driver_version") != driver["driver_version"]
        ):
            raise Hold(
                "DRIVER_QUALIFICATION", "No passing qualification for the exact driver version"
            )
        _, environment = resolve_ref(self.store, scope, profile["environment_ref"])
        if environment.get("status") != "qualified" or not environment.get("containment_enforced"):
            raise Hold(
                "SANDBOX_UNQUALIFIED",
                "A worktree is not a sandbox; enforced containment is required",
            )
        if driver["environment_ref"] != profile["environment_ref"]:
            raise Hold("DRIVER_ENVIRONMENT", "Driver was qualified in another environment")
        _, model = resolve_ref(self.store, scope, profile["model_profile_ref"])
        self.contracts.validate("model-profile", model)
        if not model["enabled"] or model["driver_profile_ref"] != profile["driver_profile_ref"]:
            raise Hold("MODEL_PROFILE", "Model is disabled or bound to another driver")
        if classification not in model["data_classes_allowed"]:
            raise Hold(
                "DATA_EGRESS", "Data classification is not permitted for this model endpoint"
            )
        if not set(model["required_capabilities"]) <= set(driver["qualified"]):
            raise Hold(
                "MODEL_CAPABILITY", "Model requires capabilities not qualified on this driver"
            )
        if (
            qualification.get("environment_ref", profile["environment_ref"])
            != profile["environment_ref"]
        ):
            raise Hold(
                "DRIVER_QUALIFICATION_ENVIRONMENT", "Qualification belongs to another environment"
            )
        if qualification.get("driver_id", driver["driver_id"]) != driver["driver_id"]:
            raise Hold("DRIVER_QUALIFICATION_ID", "Qualification belongs to another driver")
        if not set(c["action"] for c in capabilities) <= set(driver["qualified"]):
            raise Hold(
                "DRIVER_CAPABILITY", "Driver qualification does not cover the requested actions"
            )
        if len(intersect_capabilities(capabilities, environment.get("capabilities", []))) != len(
            capabilities
        ):
            raise Hold(
                "SANDBOX_CAPABILITY", "Environment cannot enforce the requested capability boundary"
            )
        if any(c["effect_class"] == "production_control" for c in capabilities):
            raise Hold(
                "PRODUCTION_DISABLED",
                "Production effects require a separate explicitly qualified deployment profile",
            )
        return {"driver": driver, "environment": environment, "model": model}

    def activate(
        self,
        actor: Actor,
        contract_ref: dict[str, Any],
        graph_ref: dict[str, Any],
        grant_ref: dict[str, Any],
        profile: dict[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        actor.require("goal.activate")
        scope = actor.scope
        contract = self.store.get(scope, "goal-contract", contract_ref)
        graph = self.store.get(scope, "workgraph", graph_ref)
        check_contract(contract)
        validate_graph(graph, contract, contract_ref)
        check_refs(self.store, scope, contract)
        check_refs(self.store, scope, graph)
        intent = self.store.get(scope, "intent-envelope", contract["intent_ref"])
        self._profile(
            scope, profile, contract["requested_capabilities"], intent["data_classification"]
        )
        self.authority.preflight(
            scope,
            grant_ref,
            subject_id=actor.subject_id,
            contract_ref=contract_ref,
            graph_ref=graph_ref,
            capabilities=contract["requested_capabilities"],
        )
        context = self.store.get(scope, "context-bundle", contract["context_bundle_ref"])
        check_context(context, context["core_refs"])
        with self.store.tx() as db:
            head = self.store.head(scope, "goal", contract["goal_id"], db=db)
            if head["data"].get("candidate_contract_ref") != contract_ref:
                raise Hold("CONTRACT_NOT_CANDIDATE", "Contract has not completed admission")
            if (
                head["data"].get("active_contract_ref")
                and contract_ref["revision"] <= head["data"]["active_contract_ref"]["revision"]
            ):
                raise Conflict(
                    "REVISION_NOT_NEW", "New activation requires a new contract revision"
                )
            active = db.execute(
                "SELECT COUNT(*) FROM leases WHERE tenant=? AND project=? AND work_id IN "
                "(SELECT id FROM heads WHERE tenant=? AND project=? AND kind='work' "
                "AND state IN ('leased','running','verifying'))",
                (*scope.keys(), *scope.keys()),
            ).fetchone()[0]
            if head["state"] in {"active", "verifying"} or (
                head["data"].get("active_contract_ref") and active
            ):
                raise Hold(
                    "DRAIN_REQUIRED",
                    "Existing running work must be stopped and reconciled "
                    "before revision activation",
                )
            observations = {
                g: Observation.check(True, r)
                for g, r in {
                    "G-01": "Authenticated scoped actor",
                    "G-03": "Contract and verifier bindings checked",
                    "G-04": "Pinned governing context current",
                    "G-05": "Deterministic DAG checks passed",
                    "G-06": "Live scoped grant and capability intersection checked",
                }.items()
            }
            command = (
                "activate_new_revision"
                if head["state"] in {"verified", "failed", "cancelled"}
                else "activate_revision"
            )
            state, gates = self.machines.transition("goal", head["state"], command, observations)
            data = {
                **head["data"],
                "active_contract_ref": contract_ref,
                "active_graph_ref": graph_ref,
                "execution_epoch": head["data"]["execution_epoch"] + 1,
                "grant_ref": grant_ref,
                "authority_subject_id": actor.subject_id,
                "profile": profile,
                "started_at_epoch": self.store.clock(),
                "gate_results": gates,
                "last_dispatch": 0,
                "priority": head["data"].get("priority", 0),
                "admission_paused": False,
            }
            version = self.store.cas(
                db, scope, "goal", contract["goal_id"], expected_version, state, data
            )
            for node in graph["nodes"]:
                self.store.cas(
                    db,
                    scope,
                    "work",
                    node["work_id"],
                    0,
                    "pending",
                    {
                        "node": node,
                        "goal_id": contract["goal_id"],
                        "contract_ref": contract_ref,
                        "graph_ref": graph_ref,
                        "execution_epoch": data["execution_epoch"],
                        "attempts": 0,
                        "failure_signatures": [],
                        "run_ids": [],
                        "outputs": {},
                        "verdict_refs": [],
                    },
                )
            self.store.event(
                db,
                scope,
                "goal",
                contract["goal_id"],
                "contract.activated",
                {"contract_ref": contract_ref, "graph_ref": graph_ref},
            )
        return {"goal_id": contract["goal_id"], "row_version": version, "state": state}

    def _dependencies(
        self, db: sqlite3.Connection, scope: Scope, node: dict[str, Any], graph: dict[str, Any]
    ) -> bool:
        by_id = {n["node_id"]: n for n in graph["nodes"]}
        states = []
        for dep in node["depends_on"]:
            states.append(
                self.store.head(scope, "work", by_id[dep]["work_id"], db=db)["state"] == "succeeded"
            )
        return (
            (any(states) if states else True)
            if node["join"] == "any_success_read_only"
            else all(states)
        )

    def _check_lease(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        run_id: str,
        worker_id: str,
        lease_id: str,
        fence: int,
    ) -> Lease:
        row = db.execute(
            "SELECT * FROM leases WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), run_id),
        ).fetchone()
        if (
            not row
            or row["worker_id"] != worker_id
            or row["lease_id"] != lease_id
            or row["fence"] != fence
            or row["epoch"] != self.store.epoch
            or row["expires"] <= self.store.clock()
        ):
            raise Hold(
                "STALE_LEASE",
                "Run owner, fencing token, epoch, or lease expiry is no longer current",
            )
        run = self.store.head(scope, "run", run_id, db=db)
        if run["state"] not in {"created", "running", "pausing", "verifying"}:
            raise Hold("STALE_RUN", "Paused or terminal run no longer owns execution admission")
        work = self.store.head(scope, "work", row["work_id"], db=db)
        if work["data"].get("current_run_id") != run_id:
            raise Hold("STALE_RUN", "Run is not the current work attempt")
        goal = self.store.head(scope, "goal", work["data"]["goal_id"], db=db)
        if (
            goal["data"]["active_contract_ref"] != work["data"]["contract_ref"]
            or goal["data"]["active_graph_ref"] != work["data"]["graph_ref"]
            or goal["data"]["execution_epoch"] != work["data"]["execution_epoch"]
        ):
            raise Hold("STALE_EXECUTION", "Contract or graph was superseded")
        return Lease(
            row["lease_id"],
            row["run_id"],
            row["work_id"],
            row["worker_id"],
            row["fence"],
            row["epoch"],
            row["expires"],
        )

    def lease(self, actor: Actor, run_id: str, lease_id: str, fence: int) -> Lease:
        actor.require("worker.execute")
        with self.store._lock:
            return self._check_lease(
                self.store.conn, actor.scope, run_id, actor.subject_id, lease_id, fence
            )

    def claim(self, worker: Actor, *, goal_id: str | None = None) -> dict[str, Any] | None:
        worker.require("worker.execute")
        scope = worker.scope
        try:
            kill = self.store.head(scope, "runtime-control", "kill")
        except RuntimeFault as exc:
            if exc.code != "NOT_FOUND":
                raise
        else:
            if kill["data"].get("enabled") or kill["state"] == "enabled":
                raise Hold("KILL_SWITCH", "New work admission is disabled by the operator")
        # Candidate selection is outside the write transaction; live authority IO also
        # stays outside.
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id,data FROM heads WHERE tenant=? AND project=? AND kind='goal' "
                "AND state IN ('ready','active')",
                scope.keys(),
            ).fetchall()
        candidates = [
            (r["id"], json.loads(r["data"])) for r in rows if not goal_id or r["id"] == goal_id
        ]
        candidates.sort(
            key=lambda pair: (-pair[1].get("priority", 0), pair[1].get("last_dispatch", 0), pair[0])
        )
        holds = []
        for gid, goal_data in candidates:
            if goal_data.get("admission_paused"):
                continue
            contract = self.store.get(scope, "goal-contract", goal_data["active_contract_ref"])
            graph = self.store.get(scope, "workgraph", goal_data["active_graph_ref"])
            intent = self.store.get(scope, "intent-envelope", contract["intent_ref"])
            try:
                self._profile(
                    scope,
                    goal_data["profile"],
                    contract["requested_capabilities"],
                    intent["data_classification"],
                )
                self.authority.preflight(
                    scope,
                    goal_data["grant_ref"],
                    subject_id=goal_data["authority_subject_id"],
                    contract_ref=goal_data["active_contract_ref"],
                    graph_ref=goal_data["active_graph_ref"],
                    capabilities=contract["requested_capabilities"],
                )
                with self.store.tx() as db:
                    goal = self.store.head(scope, "goal", gid, db=db)
                    if (
                        goal["state"] not in {"ready", "active"}
                        or goal["data"].get("admission_paused")
                        or goal["data"]["active_graph_ref"] != goal_data["active_graph_ref"]
                    ):
                        continue
                    for node in graph["nodes"]:
                        work = self.store.head(scope, "work", node["work_id"], db=db)
                        if work["state"] not in {"pending", "ready"} or not self._dependencies(
                            db, scope, node, graph
                        ):
                            continue
                        if node["strategy"] == "direct" and work["data"]["attempts"] >= 1:
                            raise Hold(
                                "DIRECT_ATTEMPT",
                                "Direct execution cannot silently become a repair loop",
                            )
                        if self.store.clock() < work["data"].get("retry_not_before", 0):
                            continue
                        if (
                            len(work["data"]["failure_signatures"]) >= 3
                            and len(set(work["data"]["failure_signatures"][-3:])) == 1
                        ):
                            raise Hold(
                                "REPEATED_FAILURE",
                                "Three equal failure signatures require escalation",
                            )
                        conflicts = False
                        for claim in node["resource_claims"]:
                            resource_rows = db.execute(
                                "SELECT mode FROM resources "
                                "WHERE tenant=? AND project=? AND resource=?",
                                (*scope.keys(), claim["resource"]),
                            ).fetchall()
                            if any(
                                claim["mode"] == "exclusive_write" or r["mode"] == "exclusive_write"
                                for r in resource_rows
                            ):
                                conflicts = True
                            if claim["mode"] == "shared_read" and len(resource_rows) >= 4:
                                conflicts = True
                        if conflicts:
                            continue
                        run_id = new_id("run")
                        attempt = work["data"]["attempts"] + 1
                        self.budgets.reserve(
                            db,
                            scope,
                            gid,
                            run_id,
                            contract["budget"],
                            node["budget"],
                            attempt=attempt,
                            goal_started=goal["data"]["started_at_epoch"],
                        )
                        previous = db.execute(
                            "SELECT fence FROM leases WHERE tenant=? AND project=? AND work_id=?",
                            (*scope.keys(), node["work_id"]),
                        ).fetchone()
                        fence = (previous[0] + 1) if previous else 1
                        lease = Lease(
                            new_id("lease"),
                            run_id,
                            node["work_id"],
                            worker.subject_id,
                            fence,
                            self.store.epoch,
                            self.store.clock() + 120,
                        )
                        db.execute(
                            "INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?,?,?,?,0)",
                            (
                                *scope.keys(),
                                node["work_id"],
                                run_id,
                                lease.lease_id,
                                worker.subject_id,
                                fence,
                                self.store.epoch,
                                lease.expires_at_epoch,
                            ),
                        )
                        for claim in node["resource_claims"]:
                            db.execute(
                                "INSERT INTO resources VALUES(?,?,?,?,?)",
                                (*scope.keys(), claim["resource"], run_id, claim["mode"]),
                            )
                        observations = {
                            g: Observation.check(True, r)
                            for g, r in {
                                "G-05": "Dependencies and typed DAG checked",
                                "G-09": "New server-owned lease and current execution refs",
                                "G-06": "Current scoped authority",
                                "G-07": "Qualified pinned driver and containment",
                                "G-08": "Atomic budget and resource reservation",
                            }.items()
                        }
                        state = work["state"]
                        gates: list[dict[str, Any]] = []
                        if state == "pending":
                            state, gates = self.machines.transition(
                                "work", state, "dependencies_ready", observations
                            )
                        state, claim_gates = self.machines.transition(
                            "work", state, "claim", observations
                        )
                        gates += claim_gates
                        data = {
                            **work["data"],
                            "attempts": attempt,
                            "current_run_id": run_id,
                            "run_ids": [*work["data"]["run_ids"], run_id],
                        }
                        self.store.cas(
                            db, scope, "work", node["work_id"], work["row_version"], state, data
                        )
                        profile = goal["data"]["profile"]
                        record = {
                            "schema_version": "3.0.0",
                            "run_id": run_id,
                            "scope": scope.wire(),
                            "root_goal_id": gid,
                            "parent_run_id": None,
                            "work_id": node["work_id"],
                            "attempt": attempt,
                            "contract_ref": goal_data["active_contract_ref"],
                            "graph_ref": goal_data["active_graph_ref"],
                            **{k: profile[k] for k in PROFILE_REFS},
                            "context_bundle_ref": contract["context_bundle_ref"],
                            "status": "created",
                            "usage": {
                                "input_tokens": None,
                                "output_tokens": None,
                                "cost_microunits": None,
                                "currency": contract["budget"]["currency"],
                                "status": "unknown",
                                "source_ref": None,
                            },
                            "started_at": now(),
                            "finished_at": None,
                            "last_event_seq": 0,
                            "failure_signatures": [],
                            "steering_refs": [],
                            "effect_refs": [],
                            "evidence_refs": [],
                            "verdict_refs": [],
                            "gate_results": gates,
                        }
                        self.contracts.validate("run-record", record)
                        self.store.put(db, scope, "run-record", run_id, 1, record)
                        self.store.cas(
                            db,
                            scope,
                            "run",
                            run_id,
                            0,
                            "created",
                            {
                                "record": record,
                                "worker_seq": 0,
                                "lease": lease.wire(),
                                "process_stopped": False,
                            },
                        )
                        dispatch_id = new_id("dispatch")
                        payload = {
                            "run_id": run_id,
                            "lease": lease.wire(),
                            "node": node,
                            "contract_ref": goal_data["active_contract_ref"],
                            "graph_ref": goal_data["active_graph_ref"],
                            "profile": profile,
                        }
                        db.execute(
                            "INSERT INTO worker_dispatch VALUES(?,?,?,?,?,?,?,NULL)",
                            (
                                *scope.keys(),
                                dispatch_id,
                                run_id,
                                worker.subject_id,
                                digest(payload),
                                "pending",
                            ),
                        )
                        payload["dispatch_id"] = dispatch_id
                        self.store.event(db, scope, "run", run_id, "work.dispatch", payload)
                        if goal["state"] == "ready":
                            goal_state, _gg = self.machines.transition(
                                "goal", "ready", "dispatch", observations
                            )
                        else:
                            goal_state = "active"
                        self.store.cas(
                            db,
                            scope,
                            "goal",
                            gid,
                            goal["row_version"],
                            goal_state,
                            {**goal["data"], "last_dispatch": self.store.clock()},
                        )
                        return payload
            except Hold as exc:
                holds.append({"goal_id": gid, "code": exc.code, "reason": exc.message})
        if holds:
            raise Hold(
                "NO_ADMISSIBLE_WORK", "Candidate work is blocked by admission checks", details=holds
            )
        return None

    def _run_state(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        run_id: str,
        head: dict[str, Any],
        state: str,
        data: dict[str, Any],
    ) -> None:
        record = {**data["record"], "status": state}
        if state in {"succeeded", "failed", "cancelled", "lost"}:
            record["finished_at"] = record["finished_at"] or now()
        self.contracts.validate("run-record", record)
        self.store.put(db, scope, "run-record", run_id, head["row_version"] + 1, record)
        self.store.cas(
            db, scope, "run", run_id, head["row_version"], state, {**data, "record": record}
        )

    def start(self, worker: Actor, dispatch: dict[str, Any], session_handle: str) -> dict[str, Any]:
        worker.require("worker.execute")
        if (
            not isinstance(session_handle, str)
            or not session_handle.strip()
            or session_handle in {"latest", "continue"}
        ):
            raise RuntimeFault("SESSION_REQUIRED", "An exact driver session handle is required")
        from .envelope import assert_execution_live

        assert_execution_live(self, worker, dispatch)
        lease = dispatch["lease"]
        scope = worker.scope
        with self.store.tx() as db:
            self._check_lease(
                db,
                scope,
                dispatch["run_id"],
                worker.subject_id,
                lease["lease_id"],
                lease["fencing_token"],
            )
            row = db.execute(
                "SELECT * FROM worker_dispatch WHERE tenant=? AND project=? AND dispatch_id=?",
                (*scope.keys(), dispatch["dispatch_id"]),
            ).fetchone()
            original = {k: v for k, v in dispatch.items() if k != "dispatch_id"}
            if row and digest(original) != row["payload_digest"]:
                raise Conflict(
                    "DISPATCH_TAMPERED",
                    "Dispatch payload differs from the transactionally stored claim",
                )
            if (
                not row
                or row["worker_id"] != worker.subject_id
                or row["run_id"] != dispatch["run_id"]
            ):
                raise RuntimeFault(
                    "DISPATCH_BINDING", "Dispatch does not belong to this worker/run"
                )
            run = self.store.head(scope, "run", dispatch["run_id"], db=db)
            goal = self.store.head(scope, "goal", run["data"]["record"]["root_goal_id"], db=db)
            if goal["state"] != "active" or goal["data"].get("admission_paused"):
                raise Hold("START_PAUSED", "No new start after pause or cancellation")
            if row["state"] == "acknowledged":
                if run["data"].get("session_handle") != session_handle:
                    raise Conflict(
                        "SESSION_CONFLICT", "Duplicate dispatch started a different session"
                    )
                return {"status": "acknowledged", "session_handle": session_handle}
            obs = {"G-09": Observation.check(True, "Lease and exact session binding checked")}
            state, _gates = self.machines.transition("run", run["state"], "start_ack", obs)
            self._run_state(
                db,
                scope,
                dispatch["run_id"],
                run,
                state,
                {**run["data"], "session_handle": session_handle},
            )
            work = self.store.head(scope, "work", lease["work_id"], db=db)
            state, _ = self.machines.transition("work", work["state"], "start_ack", obs)
            self.store.cas(
                db, scope, "work", lease["work_id"], work["row_version"], state, work["data"]
            )
            db.execute(
                "UPDATE worker_dispatch SET state='acknowledged' "
                "WHERE tenant=? AND project=? AND dispatch_id=?",
                (*scope.keys(), dispatch["dispatch_id"]),
            )
            self.store.event(
                db,
                scope,
                "run",
                dispatch["run_id"],
                "run.started",
                {"session_handle": session_handle},
            )
        return {"status": "acknowledged", "session_handle": session_handle}

    def heartbeat(
        self, worker: Actor, run_id: str, lease_id: str, fence: int, sequence: int
    ) -> dict[str, Any]:
        worker.require("worker.execute")
        with self.store.tx() as db:
            self._check_lease(db, worker.scope, run_id, worker.subject_id, lease_id, fence)
            row = db.execute(
                "SELECT heartbeat_seq FROM leases WHERE tenant=? AND project=? AND run_id=?",
                (*worker.scope.keys(), run_id),
            ).fetchone()
            if sequence <= row[0]:
                raise Conflict("HEARTBEAT_SEQUENCE", "Heartbeat sequence must increase")
            expiry = self.store.clock() + 120
            db.execute(
                "UPDATE leases SET expires=?,heartbeat_seq=? "
                "WHERE tenant=? AND project=? AND run_id=?",
                (expiry, sequence, *worker.scope.keys(), run_id),
            )
        return {"expires_at_epoch": expiry, "fencing_token": fence}

    def worker_event(
        self,
        worker: Actor,
        run_id: str,
        lease_id: str,
        fence: int,
        *,
        message_id: str,
        sequence: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        worker.require("worker.execute")
        if event_type not in WORKER_EVENTS:
            raise RuntimeFault(
                "SERVER_ONLY_EVENT", "Worker cannot publish control-plane authority/verdict events"
            )
        if len(canonical(payload)) > 1024 * 1024:
            raise RuntimeFault("EVENT_SIZE", "Worker event exceeds the ingress limit")
        fingerprint = digest(
            {"run_id": run_id, "sequence": sequence, "type": event_type, "payload": payload}
        )
        with self.store.tx() as db:
            prior = db.execute(
                "SELECT digest,result FROM inbox "
                "WHERE tenant=? AND project=? AND producer=? AND message_id=?",
                (*worker.scope.keys(), worker.subject_id, message_id),
            ).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise Conflict(
                        "INBOX_CONFLICT", "Worker event ID was reused for different content"
                    )
                cached: dict[str, Any] = json.loads(prior[1])
                return cached
            self._check_lease(db, worker.scope, run_id, worker.subject_id, lease_id, fence)
            run = self.store.head(worker.scope, "run", run_id, db=db)
            if sequence != run["data"]["worker_seq"] + 1:
                raise Conflict("WORKER_SEQUENCE", "Worker event stream has a gap or replay")
            if run["state"] not in {"created", "running", "pausing"}:
                raise Hold("RUN_EVENT_STATE", "Run no longer admits worker progress")
            data = {**run["data"], "worker_seq": sequence}
            # Raw usage is an observation. Only the qualified driver collector can settle billing.
            if event_type == "run.usage":
                data["usage_observation"] = payload
            if event_type == "run.stopped":
                data["stop_observation"] = payload
            server_seq = self.store.event(
                db,
                worker.scope,
                "run",
                run_id,
                event_type,
                {"worker": worker.subject_id, "sequence": sequence, "payload": payload},
            )
            data["record"] = {**data["record"], "last_event_seq": server_seq}
            self._run_state(db, worker.scope, run_id, run, run["state"], data)
            result = {"accepted_sequence": sequence, "server_cursor": server_seq}
            db.execute(
                "INSERT INTO inbox VALUES(?,?,?,?,?,?)",
                (
                    *worker.scope.keys(),
                    worker.subject_id,
                    message_id,
                    fingerprint,
                    canonical(result),
                ),
            )
        return result

    def output_ready(
        self,
        worker: Actor,
        run_id: str,
        lease_id: str,
        fence: int,
        outputs: dict[str, dict[str, Any]],
        *,
        usage: dict[str, Any],
        process_stopped: bool,
    ) -> dict[str, Any]:
        # Called by a qualified driver collector, not directly by an HTTP worker event.
        worker.require("worker.execute")
        if process_stopped is not True:
            raise Hold("PROCESS_NOT_STOPPED", "Output collection is not process termination")
        self.contracts.validate(
            "run-record",
            {**self.store.head(worker.scope, "run", run_id)["data"]["record"], "usage": usage},
        )
        for artifact in outputs.values():
            self.artifacts.read(worker.scope, artifact)
        with self.store.tx() as db:
            lease = self._check_lease(db, worker.scope, run_id, worker.subject_id, lease_id, fence)
            work = self.store.head(worker.scope, "work", lease.work_id, db=db)
            run = self.store.head(worker.scope, "run", run_id, db=db)
            ports = {p["name"]: p for p in work["data"]["node"]["produces"]}
            if set(outputs) - set(ports) or any(
                p["required"] and k not in outputs for k, p in ports.items()
            ):
                raise Hold("OUTPUT_COVERAGE", "Required named outputs are missing or unknown")
            if any(ports[k]["media_type"] != a["media_type"] for k, a in outputs.items()):
                raise Hold("OUTPUT_TYPE", "Output media type differs from graph")
            pending = db.execute(
                "SELECT id FROM heads WHERE tenant=? AND project=? AND kind='effect' "
                "AND state IN ('prepared','dispatched','unknown')",
                worker.scope.keys(),
            ).fetchall()
            # Filter by run; unrelated projects/runs do not block each other.
            for row in pending:
                effect = self.store.head(worker.scope, "effect", row[0], db=db)
                if effect["data"]["request"]["run_id"] == run_id:
                    raise Hold(
                        "EFFECT_PENDING", "External effects must be resolved before verification"
                    )
            obs = {
                "G-09": Observation.check(True, "Current lease and execution revision"),
                "G-11": Observation.check(True, "Actual named output bytes verified"),
            }
            work_state, _gates = self.machines.transition(
                "work", work["state"], "output_ready", obs
            )
            # Run command name is defined by the approved state machine, not invented.
            command = next(
                t["command"]
                for t in self.machines.machines["run"]["transitions"]
                if run["state"] in t["from"] and t["to"] == "verifying"
            )
            run_state, _ = self.machines.transition("run", run["state"], command, obs)
            data = {
                **run["data"],
                "process_stopped": True,
                "record": {**run["data"]["record"], "usage": usage},
            }
            self._run_state(db, worker.scope, run_id, run, run_state, data)
            self.store.cas(
                db,
                worker.scope,
                "work",
                lease.work_id,
                work["row_version"],
                work_state,
                {**work["data"], "outputs": outputs},
            )
            self.budgets.settle(db, worker.scope, run_id, usage)
            self.store.event(
                db, worker.scope, "run", run_id, "verification.requested", {"outputs": outputs}
            )
        return {"status": "verifying", "run_id": run_id}

    def reap(
        self, scope: Scope, process_probe: Callable[[str, str], object]
    ) -> list[dict[str, Any]]:
        self.store.assert_outside_tx()
        with self.store._lock:
            stale = [
                dict(r)
                for r in self.store.conn.execute(
                    "SELECT * FROM leases "
                    "WHERE tenant=? AND project=? AND (epoch<>? OR expires<=?)",
                    (*scope.keys(), self.store.epoch, self.store.clock()),
                ).fetchall()
            ]
        results = []
        for row in stale:
            stopped = process_probe(row["worker_id"], row["run_id"]) is True
            with self.store.tx() as db:
                run = self.store.head(scope, "run", row["run_id"], db=db)
                if run["state"] in {"succeeded", "failed", "cancelled", "lost"}:
                    continue
                unknown = []
                effects = db.execute(
                    "SELECT id,data FROM heads WHERE tenant=? AND project=? AND kind='effect' "
                    "AND state IN ('dispatched','unknown')",
                    scope.keys(),
                ).fetchall()
                unknown = [
                    r["id"]
                    for r in effects
                    if json.loads(r["data"])["request"]["run_id"] == row["run_id"]
                ]
                state = "unknown_effect" if unknown else "lost" if stopped else run["state"]
                self._run_state(
                    db,
                    scope,
                    row["run_id"],
                    run,
                    state,
                    {
                        **run["data"],
                        "process_stopped": stopped,
                        "recovery_hold": "unknown_effect"
                        if unknown
                        else "process_unconfirmed"
                        if not stopped
                        else None,
                    },
                )
                work = self.store.head(scope, "work", row["work_id"], db=db)
                if work["state"] not in {"succeeded", "failed", "cancelled", "superseded"}:
                    self.store.cas(
                        db,
                        scope,
                        "work",
                        row["work_id"],
                        work["row_version"],
                        "blocked",
                        {**work["data"], "hold_reason": "recovery_required"},
                    )
                if stopped and not unknown:
                    db.execute(
                        "DELETE FROM resources WHERE tenant=? AND project=? AND run_id=?",
                        (*scope.keys(), row["run_id"]),
                    )
                    self.budgets.settle(db, scope, row["run_id"], run["data"]["record"]["usage"])
                self.store.event(
                    db,
                    scope,
                    "run",
                    row["run_id"],
                    "recovery.observed",
                    {"process_stopped": stopped, "unknown_effect_ids": unknown},
                )
                results.append(
                    {
                        "run_id": row["run_id"],
                        "state": state,
                        "process_stopped": stopped,
                        "unknown_effects": unknown,
                    }
                )
        return results
