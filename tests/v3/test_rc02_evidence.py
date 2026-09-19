"""RC-02 evidence/CAS catalog cases T-052..T-056.

design/09, test-catalog group "evidence" (G-11). Each test names the catalog id
it evidences and exercises the real runtime/evidence/cas.py ArtifactStore and
verification/runtime/service.py VerificationService, not a re-implementation.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from amplai_foundry.runtime.contracts.identity import digest_bytes, new_id, now
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault


def new_revision(d, p, *, constraint_text):
    """Contract v2 candidate from the active v1 (mirrors
    test_rc01_e2e_cross_app.new_revision). Only used to obtain a distinct,
    valid contract/graph ref pair; activation is out of scope for these cases."""
    old = d.store.get(d.scope, "goal-contract", p["contract_ref"])
    candidate = deepcopy(old)
    candidate["revision"] = 2
    candidate["constraints"].append(
        {
            "id": "C-EVID",
            "statement": constraint_text,
            "source_refs": [old["policy_ref"]],
            "protected": False,
        }
    )
    candidate["created_at"] = now()
    head = d.store.head(d.scope, "goal", p["goal_id"])
    contract_ref = d.goals.freeze_contract(d.actor, candidate, expected_version=head["row_version"])
    graph = deepcopy(d.store.get(d.scope, "workgraph", p["graph_ref"]))
    graph["graph_id"] = new_id("graph")
    graph["revision"] = 2
    graph["contract_ref"] = contract_ref
    graph["previous_graph_ref"] = p["graph_ref"]
    graph["replan_reason"] = "constraint_add"
    for node in graph["nodes"]:
        node["work_id"] = new_id("work-" + node["target_ref"]["id"])
    graph["created_at"] = now()
    graph_ref = d.runtime.save_graph(d.actor, graph, contract_ref)
    return contract_ref, graph_ref


# ---------------------------------------------------------------- T-052


def test_t052_forged_digest_rejected_with_no_registry_entry(deployment):
    """given: agent declares artifact hash not matching uploaded bytes / when:
    commit artifact / expected: reject mismatched digest, quarantined from the
    registry (cas.py:89-91 ARTIFACT_DIGEST Conflict)."""
    d = deployment
    data = b'{"payload":"real bytes"}'
    forged_digest = "sha256:" + "0" * 64
    assert digest_bytes(data) != forged_digest
    before = d.store.conn.execute(
        "SELECT COUNT(*) FROM artifacts WHERE tenant=? AND project=?", d.scope.keys()
    ).fetchone()[0]
    with pytest.raises(Conflict) as exc:
        d.artifacts.admit(d.scope, data, "application/json", expected_digest=forged_digest)
    assert exc.value.code == "ARTIFACT_DIGEST"
    after = d.store.conn.execute(
        "SELECT COUNT(*) FROM artifacts WHERE tenant=? AND project=?", d.scope.keys()
    ).fetchone()[0]
    assert after == before, "a forged-digest claim must not create any registry entry"


# ---------------------------------------------------------------- T-053


def test_t053_exit_zero_without_evidence_is_not_accepted_as_pass(deployment):
    """given: worker exits 0 without required evidence / when: global (per-work)
    verify / expected: inconclusive/fail, no verified work (service.py:493-495
    VERDICT_COVERAGE Hold when mandatory acceptance has no verdict)."""
    d = deployment
    p = d.prepare()
    x = d.runtime.claim(d.worker)
    d.driver.run(d.runtime, d.worker, x, p["recipes"][x["lease"]["work_id"]])
    # driver process exited 0, but no acceptance criterion was independently verified
    with pytest.raises(Hold) as exc:
        d.verification.finish_work(d.verifier, x["run_id"])
    assert exc.value.code == "VERDICT_COVERAGE"
    work = d.store.head(d.scope, "work", x["lease"]["work_id"])
    assert work["state"] not in {"succeeded", "verified"}
    goal = d.store.head(d.scope, "goal", p["goal_id"])
    assert goal["state"] != "verified"


# ---------------------------------------------------------------- T-054


def test_t054_old_revision_verdict_not_accepted_toward_active_coverage(deployment):
    """given: v1 verdict passed but contract/graph v2 is now active / when: that
    verdict is submitted for active coverage / expected: not accepted
    (service.py:430-431 VERDICT_REVISION Hold in check_verdict)."""
    d = deployment
    p = d.prepare()
    report = d.execute(p)
    assert report["status"] == "verified"
    run_id = report["runs"][0]["run_id"]
    verdict_ref = d.store.head(d.scope, "run", run_id)["data"]["record"]["verdict_refs"][0]
    contract_ref2, graph_ref2 = new_revision(d, p, constraint_text="v2 for T-054")
    with pytest.raises(Hold) as exc:
        d.verification.check_verdict(d.scope, verdict_ref, contract_ref2, graph_ref2)
    assert exc.value.code == "VERDICT_REVISION"


# ---------------------------------------------------------------- T-055


def test_t055_legacy_imported_evidence_stays_untrusted_not_auto_server_verified(deployment):
    """given: a V2-imported log claims PASS / when: migrated into V3 as an
    artifact / expected: legacy_imported trust class is preserved; it is never
    silently treated as server_verified (cas.py:75 trust enum; cas.py:147-150
    UNTRUSTED_EVIDENCE on a trusted read)."""
    d = deployment
    payload = json.dumps({"outcome": "pass", "reason": "v2 migrated log"}).encode()
    ref = d.artifacts.admit(d.scope, payload, "application/json", trust="legacy_imported")
    row = d.store.conn.execute(
        "SELECT trust FROM artifacts WHERE tenant=? AND project=? AND id=?",
        (*d.scope.keys(), ref["id"]),
    ).fetchone()
    assert row["trust"] == "legacy_imported"
    with pytest.raises(Hold) as exc:
        d.artifacts.read(d.scope, ref, trusted=True)
    assert exc.value.code == "UNTRUSTED_EVIDENCE"
    # an ordinary (non-trusted) read still succeeds, but does not upgrade the trust class
    assert d.artifacts.read(d.scope, ref) == payload
    row_after = d.store.conn.execute(
        "SELECT trust FROM artifacts WHERE tenant=? AND project=? AND id=?",
        (*d.scope.keys(), ref["id"]),
    ).fetchone()
    assert row_after["trust"] == "legacy_imported", "no automatic promotion to server_verified"


# ---------------------------------------------------------------- T-056


def test_t056_cas_rename_without_registry_commit_is_orphan_not_exposed(deployment):
    """given: CAS rename completed but the registry commit crashed / when:
    recovery/GC inventories the store / expected: the bytes surface only as a
    reported orphan (cas.py:159-174 inventory, report_only); no unregistered
    ref is readable as a real artifact (cas.py:142-143 NOT_FOUND)."""
    d = deployment
    data = b'{"x":"orphan-after-crash"}'
    value_digest = digest_bytes(data)
    path = d.artifacts._path(d.scope, value_digest)  # creates the scope dir (0o700)
    path.write_bytes(data)  # simulate the completed os.replace() with no DB row committed
    report = d.artifacts.inventory(d.scope)
    assert value_digest[7:] in report["orphans"]
    assert report["missing"] == []
    assert report["deletion_mode"] == "report_only"
    fabricated_ref = {
        "id": "unregistered-id",
        "digest": value_digest,
        "media_type": "application/json",
        "size_bytes": len(data),
    }
    with pytest.raises(RuntimeFault) as exc:
        d.artifacts.read(d.scope, fabricated_ref)
    assert exc.value.code == "NOT_FOUND", "orphaned CAS bytes must not be exposed as a valid ref"
