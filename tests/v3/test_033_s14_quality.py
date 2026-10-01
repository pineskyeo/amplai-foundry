"""Work 033 S14: evaluation-quality metrics and the evaluator-change lifecycle
(interfaces.md §2.15, §3.13, §11.1, §12.1, D-105).

`evaluation/quality.py` computes the §11.1 metrics from stored records and drives the
`evaluator-change` head (proposed -> qualified -> approved | rejected); `meta evaluator ...`
(`runtime/meta_commands/evaluator.py`) exposes it. Fixtures here are written straight into a
`MetaReference` store (records are shaped as their writers shape them); the requalification and the
`evaluator-version` are the real S2 ones (`scripts/evaluator_requalify.py`,
`evaluation/versions.py`). The §7.8 Q-suite of `qualify_change` (IC-25) is run by an injected fake
runner that writes small fixture JUnit XML; the real suite is never run here (only a tiny generated
test directory exercises the default runner). Authority is `evaluator.approve` (IC-26). No driver,
docker or network is used.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import math
from pathlib import Path
from statistics import NormalDist, mean, stdev

import pytest
from test_033_s2_requalify import s2_store
from test_033_s2_service import fault, hold, make_world

from amplai_foundry.evaluation import quality, versions
from amplai_foundry.evaluation.corpus import CorpusService
from amplai_foundry.evaluation.quality import QualityService, total_variation
from amplai_foundry.runtime.contracts.identity import canonical, new_id

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "amplai_foundry"
EPOCH = "1970-01-01T00:00:00Z"
FAR_PAST = "2020-01-01T00:00:00Z"
FAR_FUTURE = "2099-01-01T00:00:00Z"
Z = NormalDist().inv_cdf(0.975)
ZERO = "sha256:" + "0" * 64
GONE = {"id": "artifact-gone", "digest": ZERO, "media_type": "application/json", "size_bytes": 1}


@pytest.fixture
def w(tmp_path):
    with make_world(tmp_path) as world:
        yield world


# --- helpers ---------------------------------------------------------------------------------


def put(w, kind, object_id, value, revision=1):
    store = w.m.d.store
    with store.tx() as db:
        return store.put(db, w.scope, kind, object_id, revision, {"scope": w.scope.wire(), **value})


def put_head(w, kind, object_id, state, data):
    store = w.m.d.store
    with store.tx() as db:
        store.cas(db, w.scope, kind, object_id, 0, state, data)


def set_head(w, kind, object_id, state, data):
    store = w.m.d.store
    with store.tx() as db:
        head = store.head(w.scope, kind, object_id, db=db)
        store.cas(db, w.scope, kind, object_id, head["row_version"], state, data)


def event(w, kind, object_id, event_type):
    store = w.m.d.store
    with store.tx() as db:
        store.event(db, w.scope, kind, object_id, event_type, {})


def artifact(w, payload):
    return w.m.d.artifacts.admit(w.scope, canonical(payload), "application/json", trust="verifier")


def quality_service(w, **kw):
    kw.setdefault("suite_runner", FakeSuite())  # never the real Q-suite
    return QualityService(w.m.d.store, w.scope, **kw)


def operator(w):
    """The meta operator of `local_deployment` (IC-26): the reviewer plus `evaluator.approve`."""
    actor = w.m.reviewer
    assert "corpus.manage" in actor.permissions and "evaluator.approve" not in actor.permissions
    return dataclasses.replace(actor, permissions=actor.permissions | {"evaluator.approve"})


def proposer_of(w):
    actor = operator(w)
    return dataclasses.replace(actor, permissions=actor.permissions | {"harness.propose"})


def service_of(w):
    return dataclasses.replace(operator(w), kind="service")


def proposer_only(w):
    return w.m.proposer


def other_scope(w):
    from amplai_foundry.runtime.storage.store import Scope

    return dataclasses.replace(operator(w), scope=Scope("other", "elsewhere"))


def no_approve(w):
    """A human operator without `evaluator.approve`."""
    actor = operator(w)
    return dataclasses.replace(actor, permissions=actor.permissions - {"evaluator.approve"})


def manage_only(w):
    """A human with `corpus.manage` and no `evaluator.approve` (the pre-IC-26 authority)."""
    return w.m.reviewer


def approve_only(w):
    """A human with `evaluator.approve` and no `corpus.manage`."""
    actor = operator(w)
    return dataclasses.replace(actor, permissions=actor.permissions - {"corpus.manage"})


def service_with_approve(w):
    return dataclasses.replace(operator(w), kind="service")


def proposer_with_approve(w):
    return proposer_of(w)


def version_ref(w, corpus_ref=None):
    """The eval-2 `evaluator-version` of the running code."""
    corpus_ref = corpus_ref or w.corpus(val=4)[0]
    return w.evaluator(corpus_ref)


def target(version="eval-3", **extra):
    return {
        "version": version,
        "changes": [{"kind": "retire_tasks", "detail": "retire the saturated tasks"}],
        **extra,
    }


def metric(w, name, **kw):
    """Measure the stored version eval-2 and return one metric plus the whole stored record."""
    service = quality_service(w)
    ref = service.measure(w.ev_ref, since=kw.pop("since", EPOCH), **kw)
    record = w.m.d.store.get(w.scope, "evaluation-quality", ref)
    return record["metrics"][name], record


SUITE_STEMS = [Path(f).stem for f in quality.QUALIFICATION_FILES]


def junit_xml(per_file=None, *, extra="", suite_attrs=""):
    """A pytest xunit2 JUnit document: `per_file` maps a Q-suite file stem to
    (passed, failed, errors, skipped), default one passing test per file."""
    per_file = per_file or {}
    cases = []
    for stem in SUITE_STEMS:
        passed, failed, errors, skipped = per_file.get(stem, (1, 0, 0, 0))
        for i in range(passed):
            cases.append(f'<testcase classname="{stem}" name="test_p{i}" time="0.001"/>')
        for i in range(failed):
            cases.append(
                f'<testcase classname="{stem}" name="test_f{i}"><failure message="x">no</failure>'
                "</testcase>"
            )
        for i in range(errors):
            cases.append(
                f'<testcase classname="{stem}" name="test_e{i}"><error message="x">no</error>'
                "</testcase>"
            )
        for i in range(skipped):
            cases.append(
                f'<testcase classname="{stem}" name="test_s{i}"><skipped message="s"/></testcase>'
            )
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="0" failures="0" tests="{len(cases)}"{suite_attrs}>'
        + "".join(cases)
        + extra
        + "</testsuite></testsuites>"
    )


class FakeSuite:
    """An injected Q-suite runner: writes fixture JUnit XML at the path it is given and returns a
    scripted exit code. It runs nothing."""

    def __init__(self, xml=None, returncode=0, before=None):
        self.xml = xml
        self.returncode = returncode
        self.before = before  # a callable run before the XML is written (e.g. to change code)
        self.calls = []

    def __call__(self, files, junit):
        self.calls.append(tuple(files))
        if self.before:
            self.before()
        if self.xml is not False:
            Path(junit).write_text(self.xml if self.xml is not None else junit_xml())
        return self.returncode


class FakeRequalifier:
    """The `requalify_scope`/`write_record` pair of the script, with a scripted verdict table."""

    def __init__(self, all_equal=True):
        self.all_equal = all_equal
        self.calls = []
        self._real = quality.load_requalifier()

    def requalify_scope(self, store, artifacts, scope, *, store_label, evaluator_version):
        self.calls.append(evaluator_version)
        row_ref = {"id": "report-x", "revision": 1, "digest": ZERO}
        rows = [
            {
                "report_ref": row_ref,
                "recorded_verdict": "pass",
                "recomputed_verdict": "pass" if self.all_equal else "fail",
                "equal": self.all_equal,
            }
        ]
        return {
            "schema": "amplai.evaluator-requalification.v1",
            "scope": scope.wire(),
            "evaluator_version": evaluator_version,
            "store": store_label,
            "reports": rows,
            "all_equal": self.all_equal,
        }

    def write_record(self, store, scope, value):
        return self._real.write_record(store, scope, value)


# --- status and requalify over S2 versions.py and the requalify script ------------------------


def test_status_of_an_empty_store_shows_the_running_code_only(w):
    status = quality_service(w).status()
    assert status["running_code"] == versions.code_digests()
    assert status["versions"] == []
    assert status["current_version_ref"] is None
    assert status["changes"] == []


def test_status_lists_the_stored_version_with_its_requalification(w):
    ref = version_ref(w)
    status = quality_service(w).status()
    assert status["current_version_ref"] == ref
    (row,) = status["versions"]
    stored = versions.read_version(w.m.d.store, w.scope, ref)
    assert row["version"] == "eval-2"
    assert row["ref"] == ref
    assert row["analysis_code_digest"] == stored["analysis_code_digest"]
    assert row["service_code_digest"] == stored["service_code_digest"]
    assert row["code_matches_running"] is True
    assert row["requalification_ref"] == stored["requalification_ref"]
    assert row["requalification_all_equal"] is True
    assert row["approved_by"] == operator(w).subject_id
    assert row["at"] == stored["at"]


def test_status_marks_a_version_of_other_code_and_has_no_current_version(w, monkeypatch):
    corpus_ref, _ = w.corpus(val=4)
    fake = {
        "analysis_code_digest": "sha256:" + "1" * 64,
        "service_code_digest": "sha256:" + "2" * 64,
    }
    with monkeypatch.context() as patch:
        patch.setattr(versions, "code_digests", lambda: dict(fake))
        w.evaluator(corpus_ref)
    status = quality_service(w).status()
    (row,) = status["versions"]
    assert row["code_matches_running"] is False
    assert status["current_version_ref"] is None
    assert status["running_code"] == versions.code_digests()


def test_status_lists_the_changes_with_their_state(w):
    version_ref(w)
    service = quality_service(w)
    change_id = service.propose_change(operator(w), to=target(), reason="retire saturated")
    (change,) = service.status()["changes"]
    assert change["change_id"] == change_id
    assert change["state"] == "proposed"


def test_requalify_writes_a_record_over_every_stored_report(w):
    s2_store(w)
    result = quality_service(w).requalify(operator(w), "eval-2")
    assert result["evaluator_version"] == "eval-2"
    assert result["reports"] == 9 and result["equal"] == 9 and result["all_equal"] is True
    record = w.m.d.store.get(w.scope, "evaluator-requalification", result["requalification_ref"])
    assert record["evaluator_version"] == "eval-2"
    assert record["all_equal"] is True
    assert len(record["reports"]) == 9
    # the record is the one `versions.write_version` accepts (S2)
    corpus_ref, _ = w.corpus(val=4)
    ref = versions.write_version(
        w.m.d.store,
        operator(w),
        corpus_ref=corpus_ref,
        requalification_ref=result["requalification_ref"],
    )
    assert versions.read_version(w.m.d.store, w.scope, ref)["version"] == "eval-2"


def test_requalify_names_the_version_being_qualified(w):
    result = quality_service(w).requalify(operator(w), "eval-5")
    record = w.m.d.store.get(w.scope, "evaluator-requalification", result["requalification_ref"])
    assert record["evaluator_version"] == "eval-5"
    assert result["reports"] == 0 and result["all_equal"] is True
    # a record for eval-5 never qualifies eval-2
    corpus_ref, _ = w.corpus(val=4)
    hold(
        "EVALUATOR_UNQUALIFIED",
        versions.write_version,
        w.m.d.store,
        operator(w),
        corpus_ref=corpus_ref,
        requalification_ref=result["requalification_ref"],
    )


@pytest.mark.parametrize("bad", ["v2", "eval-0", "eval-", "evaluator-2", ""])
def test_requalify_refuses_a_bad_version_name(w, bad):
    fault("EVALUATOR_VERSION", quality_service(w).requalify, operator(w), bad)


def test_requalify_is_refused_for_a_proposer_a_service_and_another_scope(w):
    service = quality_service(w)
    fault("FORBIDDEN", service.requalify, proposer_of(w), "eval-2")
    fault("FORBIDDEN", service.requalify, proposer_only(w), "eval-2")
    fault("FORBIDDEN", service.requalify, service_of(w), "eval-2")
    fault("FORBIDDEN", service.requalify, no_approve(w), "eval-2")
    fault("FORBIDDEN", service.requalify, manage_only(w), "eval-2")
    fault("FORBIDDEN", service.requalify, service_with_approve(w), "eval-2")
    fault("SCOPE_MISMATCH", service.requalify, other_scope(w), "eval-2")
    assert list(w.m.d.store.list_objects(w.scope, "evaluator-requalification")) == []


def test_version_ref_is_the_newest_stored_version_and_not_found_without_one(w):
    service = quality_service(w)
    fault("NOT_FOUND", service.version_ref)
    fault("NOT_FOUND", service.version_ref, "eval-2")
    ref = version_ref(w)
    assert service.version_ref() == ref
    assert service.version_ref("eval-2") == ref
    fault("NOT_FOUND", service.version_ref, "eval-9")
    fault("EVALUATOR_VERSION", service.version_ref, "latest")


# --- the evaluator-change lifecycle ----------------------------------------------------------


def test_a_change_goes_proposed_qualified_approved_and_writes_the_new_version(w):
    old = version_ref(w)
    service = quality_service(w)
    change_id = service.propose_change(operator(w), to=target(), reason="retire saturated tasks")
    change = service.change(change_id)
    assert change["state"] == "proposed"
    assert change["from"] == old
    assert change["to"] is None
    assert change["reason"] == "retire saturated tasks"
    assert change["requalification_ref"] is None
    assert change["proposed_by"]["subject_id"] == operator(w).subject_id
    assert change["target"]["version"] == "eval-3"
    # corpus and templates default to the replaced version's
    assert (
        change["target"]["corpus_ref"]
        == versions.read_version(w.m.d.store, w.scope, old)["corpus_ref"]
    )

    requal_ref = service.qualify_change(operator(w), change_id)
    change = service.change(change_id)
    assert change["state"] == "qualified"
    assert change["requalification_ref"] == requal_ref
    assert change["qualified_by"]["subject_id"] == operator(w).subject_id
    assert change["code_digests"] == versions.code_digests()
    assert change["quality_ref"]["id"].startswith("evalq-eval-2-")  # the replaced version's quality
    assert change["comparison"] == {"reports": 0, "equal": 0, "changed_reports": []}
    # IC-25: the Q-suite ran (fake runner) and its result is stored in the head
    suite = change["qualification_suite"]
    assert suite["returncode"] == 0 and suite["failed"] == 0 and suite["errors"] == 0
    assert suite["passed"] == len(quality.QUALIFICATION_FILES)
    assert [row["file"] for row in suite["files"]] == list(quality.QUALIFICATION_FILES)
    assert suite["code_digests"] == versions.code_digests() == change["code_digests"]
    assert suite["junit_digest"] == "sha256:" + hashlib.sha256(junit_xml().encode()).hexdigest()
    assert change["qualification_scope"]["suite_files"] == list(quality.QUALIFICATION_FILES)
    assert change["qualification_scope"]["vacuous"] is True  # no stored report in this scope
    requal = w.m.d.store.get(w.scope, "evaluator-requalification", requal_ref)
    assert requal["evaluator_version"] == "eval-3" and requal["all_equal"] is True
    # nothing is written as a version before the approval
    fault("NOT_FOUND", service.version_ref, "eval-3")

    new_ref = service.approve_change(operator(w), change_id)
    change = service.change(change_id)
    assert change["state"] == "approved"
    assert change["to"] == new_ref
    assert change["decided_by"]["subject_id"] == operator(w).subject_id
    assert new_ref["id"] == "evaluator-3"
    stored = versions.read_version(w.m.d.store, w.scope, new_ref)
    assert stored["version"] == "eval-3"
    assert stored["requalification_ref"] == requal_ref
    assert stored["approved_by"]["kind"] == "human"
    assert service.version_ref() == new_ref
    # the replaced version stays: a stage plan keeps its pinned version
    assert versions.read_version(w.m.d.store, w.scope, old)["version"] == "eval-2"


def test_each_step_of_the_lifecycle_appends_an_event(w):
    version_ref(w)
    service = quality_service(w)
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    service.qualify_change(operator(w), change_id)
    service.approve_change(operator(w), change_id)
    types = [
        e["event_type"]
        for e in w.m.d.store.events(w.scope, limit=1000)
        if e["aggregate_id"] == change_id
    ]
    assert types == [
        "evaluator.change.proposed",
        "evaluator.change.qualified",
        "evaluator.change.approved",
    ]


def test_the_first_evaluator_version_can_be_a_change_with_its_own_corpus(w):
    corpus_ref, _ = w.corpus(val=4)
    service = quality_service(w)
    fault(
        "EVALUATOR_CHANGE_TARGET", service.propose_change, operator(w), to=target("eval-1"),
        reason="first",
    )  # fmt: skip
    change_id = service.propose_change(
        operator(w), to=target("eval-1", corpus_ref=corpus_ref), reason="first version"
    )
    assert service.change(change_id)["from"] is None
    service.qualify_change(operator(w), change_id)
    # no replaced version, so nothing was measured
    assert service.change(change_id)["quality_ref"] is None
    new_ref = service.approve_change(operator(w), change_id)
    assert new_ref["id"] == "evaluator-1"


@pytest.mark.parametrize(
    "who",
    [
        "proposer_of",
        "proposer_only",
        "service_of",
        "no_approve",
        "manage_only",
        "service_with_approve",
        "proposer_with_approve",
    ],
)
def test_every_step_refuses_a_proposer_a_service_and_an_unprivileged_identity(w, who):
    version_ref(w)
    service = quality_service(w)
    bad = globals()[who](w)
    fault("FORBIDDEN", service.propose_change, bad, to=target(), reason="r")
    assert service._heads("evaluator-change") == []
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    fault("FORBIDDEN", service.qualify_change, bad, change_id)
    fault("FORBIDDEN", service.reject_change, bad, change_id, "no")
    assert service.change(change_id)["state"] == "proposed"
    service.qualify_change(operator(w), change_id)
    fault("FORBIDDEN", service.approve_change, bad, change_id)
    fault("FORBIDDEN", service.reject_change, bad, change_id, "no")
    # a refused approval leaves the change qualified and writes no version
    assert service.change(change_id)["state"] == "qualified"
    assert service.change(change_id)["to"] is None
    fault("NOT_FOUND", service.version_ref, "eval-3")
    assert service.approve_change(operator(w), change_id)["id"] == "evaluator-3"


def test_an_actor_of_another_scope_is_refused_at_every_step(w):
    version_ref(w)
    service = quality_service(w)
    stranger = other_scope(w)
    fault("SCOPE_MISMATCH", service.propose_change, stranger, to=target(), reason="r")
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    fault("SCOPE_MISMATCH", service.qualify_change, stranger, change_id)
    fault("SCOPE_MISMATCH", service.approve_change, stranger, change_id)
    fault("SCOPE_MISMATCH", service.reject_change, stranger, change_id, "no")


def test_the_version_writer_itself_refuses_a_non_human_approver(w):
    """approve_change reaches `versions.write_version`, which also refuses a non-human or a
    proposer approver (S2); the lifecycle never bypasses it."""
    corpus_ref, _ = w.corpus(val=4)
    requal = quality_service(w).requalify(operator(w), "eval-4")["requalification_ref"]
    for actor in (proposer_of(w), service_of(w)):
        fault(
            "FORBIDDEN", versions.write_version, w.m.d.store, actor, corpus_ref=corpus_ref,
            requalification_ref=requal, version="eval-4",
        )  # fmt: skip


def test_approve_needs_a_qualified_change(w):
    version_ref(w)
    service = quality_service(w)
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    err = hold("EVALUATOR_UNQUALIFIED", service.approve_change, operator(w), change_id)
    assert err.details == "proposed"
    fault("NOT_FOUND", service.version_ref, "eval-3")
    assert service.change(change_id)["state"] == "proposed"


def test_a_failed_requalification_keeps_the_change_proposed_and_unapprovable(w):
    version_ref(w)
    fake = FakeRequalifier(all_equal=False)
    service = quality_service(w, requalifier=fake)
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    ref = service.qualify_change(operator(w), change_id)
    assert fake.calls == ["eval-3"]
    change = service.change(change_id)
    assert change["state"] == "proposed"
    assert change["requalification_ref"] == ref
    assert change["qualified_by"] is None and change["code_digests"] is None
    assert change["comparison"] == {"reports": 1, "equal": 0, "changed_reports": ["report-x"]}
    hold("EVALUATOR_UNQUALIFIED", service.approve_change, operator(w), change_id)
    fault("NOT_FOUND", service.version_ref, "eval-3")
    # the unequal record cannot qualify a version even when written directly
    corpus_ref, _ = w.corpus(val=4)
    hold(
        "EVALUATOR_UNQUALIFIED", versions.write_version, w.m.d.store, operator(w),
        corpus_ref=corpus_ref, requalification_ref=ref, version="eval-3",
    )  # fmt: skip


def test_a_change_qualified_before_the_code_changed_must_be_qualified_again(w, monkeypatch):
    version_ref(w)
    service = quality_service(w)
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    service.qualify_change(operator(w), change_id)
    fake = {
        "analysis_code_digest": "sha256:" + "a" * 64,
        "service_code_digest": "sha256:" + "b" * 64,
    }
    monkeypatch.setattr(versions, "code_digests", lambda: dict(fake))
    hold("EVALUATOR_CHANGED", service.approve_change, operator(w), change_id)
    assert service.change(change_id)["state"] == "qualified"
    fault("NOT_FOUND", service.version_ref, "eval-3")


def test_only_one_change_is_open_at_a_time(w):
    version_ref(w)
    service = quality_service(w)
    first = service.propose_change(operator(w), to=target(), reason="r")
    err = hold(
        "EVALUATOR_CHANGE_OPEN",
        service.propose_change,
        operator(w),
        to=target("eval-4"),
        reason="r",
    )
    assert err.details == first
    service.qualify_change(operator(w), first)
    hold(
        "EVALUATOR_CHANGE_OPEN",
        service.propose_change,
        operator(w),
        to=target("eval-4"),
        reason="r",
    )
    service.reject_change(operator(w), first, "not now")
    second = service.propose_change(operator(w), to=target(), reason="again")
    assert second != first


def test_reject_closes_a_change_without_a_version(w):
    version_ref(w)
    service = quality_service(w)
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    assert service.reject_change(operator(w), change_id, "  too early  ") == "rejected"
    change = service.change(change_id)
    assert change["state"] == "rejected"
    assert change["reject_reason"] == "too early"
    assert change["decided_by"]["subject_id"] == operator(w).subject_id
    fault("NOT_FOUND", service.version_ref, "eval-3")
    # a closed change moves no further
    hold("EVALUATOR_CHANGE_STATE", service.qualify_change, operator(w), change_id)
    hold("EVALUATOR_CHANGE_STATE", service.reject_change, operator(w), change_id, "again")
    hold("EVALUATOR_UNQUALIFIED", service.approve_change, operator(w), change_id)


def test_an_approved_change_cannot_be_qualified_or_rejected_again(w):
    version_ref(w)
    service = quality_service(w)
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    service.qualify_change(operator(w), change_id)
    service.approve_change(operator(w), change_id)
    hold("EVALUATOR_CHANGE_STATE", service.qualify_change, operator(w), change_id)
    hold("EVALUATOR_CHANGE_STATE", service.reject_change, operator(w), change_id, "late")
    hold("EVALUATOR_UNQUALIFIED", service.approve_change, operator(w), change_id)
    assert service.change(change_id)["state"] == "approved"


def test_a_missing_reason_is_refused_for_a_proposal_and_a_rejection(w):
    version_ref(w)
    service = quality_service(w)
    for reason in ("", "   ", None, 3):
        fault(
            "EVALUATOR_CHANGE_REASON",
            service.propose_change,
            operator(w),
            to=target(),
            reason=reason,
        )
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    for reason in ("", "  ", None):
        fault("EVALUATOR_CHANGE_REASON", service.reject_change, operator(w), change_id, reason)
    assert service.change(change_id)["state"] == "proposed"


def test_an_unknown_change_id_is_not_found(w):
    version_ref(w)
    service = quality_service(w)
    fault("NOT_FOUND", service.change, "evalchange-nope")
    fault("NOT_FOUND", service.qualify_change, operator(w), "evalchange-nope")
    fault("NOT_FOUND", service.approve_change, operator(w), "evalchange-nope")
    fault("NOT_FOUND", service.reject_change, operator(w), "evalchange-nope", "r")


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"version": "eval-3"},
        {"changes": [{"kind": "retire_tasks", "detail": "x"}]},
        {**target(), "extra": 1},
        target("eval-2"),  # not higher than the replaced version
        target("eval-1"),
        {**target(), "changes": []},
        {**target(), "changes": "retire"},
        {**target(), "changes": [{"kind": "retire_tasks"}]},
        {**target(), "changes": [{"kind": "tune_everything", "detail": "x"}]},
        {**target(), "changes": [{"kind": "retire_tasks", "detail": "  "}]},
        {**target(), "changes": [{"kind": "retire_tasks", "detail": "x", "more": 1}]},
        {**target(), "corpus_ref": "not-a-ref"},
        {**target(), "stage_templates": ["x"]},
    ],
    ids=lambda v: json.dumps(v, default=str)[:60],
)
def test_a_malformed_target_is_a_target_fault_and_opens_nothing(w, bad):
    version_ref(w)
    service = quality_service(w)
    fault("EVALUATOR_CHANGE_TARGET", service.propose_change, operator(w), to=bad, reason="r")
    assert service._heads("evaluator-change") == []


@pytest.mark.parametrize("name", ["v3", "eval-0", "eval-", "evaluator-3"])
def test_a_bad_target_version_name_is_an_evaluator_version_fault(w, name):
    version_ref(w)
    fault(
        "EVALUATOR_VERSION", quality_service(w).propose_change, operator(w), to=target(name),
        reason="r",
    )  # fmt: skip


def test_the_target_corpus_must_be_a_corpus_record(w):
    ref = version_ref(w)
    service = quality_service(w)
    fault(
        "EVALUATOR_CHANGE_TARGET", service.propose_change, operator(w),
        to=target(corpus_ref=ref), reason="r",
    )  # fmt: skip
    other, _ = w.corpus(val=6)
    change_id = service.propose_change(
        operator(w),
        to=target(corpus_ref=other, stage_templates={"screening": {"repeats": 2}}),
        reason="a new corpus and stage templates",
    )
    service.qualify_change(operator(w), change_id)
    new_ref = service.approve_change(operator(w), change_id)
    stored = versions.read_version(w.m.d.store, w.scope, new_ref)
    assert stored["corpus_ref"] == other
    assert stored["stage_templates"] == {"screening": {"repeats": 2}}


def test_every_change_kind_of_the_spec_is_accepted(w):
    version_ref(w)
    service = quality_service(w)
    for index, kind in enumerate(
        (
            "retire_tasks",
            "add_tasks",
            "reweight_tasks",
            "repeats",
            "stage_templates",
            "analysis_code",
        )
    ):
        change_id = service.propose_change(
            operator(w),
            to={"version": f"eval-{3 + index}", "changes": [{"kind": kind, "detail": "d"}]},
            reason=kind,
        )
        service.reject_change(operator(w), change_id, "only checking the kind")
    assert set(quality.CHANGE_KINDS) == {
        "retire_tasks", "add_tasks", "reweight_tasks", "repeats", "stage_templates",
        "analysis_code",
    }  # fmt: skip


# --- a change never runs inside a candidate experiment -----------------------------------------


@pytest.mark.parametrize(
    ("kind", "state", "data"),
    [
        ("experiment", "running", {}),
        ("calibration-run", "running", {}),
        ("stage-run", "stages", {"stages": {"screening": {"state": "running"}}}),
    ],
)
def test_qualify_and_approve_wait_while_an_experiment_calibration_or_stage_runs(
    w, kind, state, data
):
    version_ref(w)
    service = quality_service(w)
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    put_head(w, kind, "busy-1", state, data)
    err = hold("EVALUATOR_CHANGE_ACTIVE", service.qualify_change, operator(w), change_id)
    assert err.details == ["busy-1"]
    assert service.change(change_id)["state"] == "proposed"
    # the one record is the version's own; the refused call wrote none
    assert len(list(w.m.d.store.list_objects(w.scope, "evaluator-requalification"))) == 1
    # finished work does not block; the change then qualifies, and approval waits again
    done = "finished" if kind != "stage-run" else "stages"
    done_data = data if kind != "stage-run" else {"stages": {"screening": {"state": "done"}}}
    set_head(w, kind, "busy-1", done, done_data)
    service.qualify_change(operator(w), change_id)
    set_head(w, kind, "busy-1", "running" if kind != "stage-run" else "stages", data)
    hold("EVALUATOR_CHANGE_ACTIVE", service.approve_change, operator(w), change_id)
    assert service.change(change_id)["state"] == "qualified"
    fault("NOT_FOUND", service.version_ref, "eval-3")
    set_head(w, kind, "busy-1", done, done_data)
    assert service.approve_change(operator(w), change_id)["id"] == "evaluator-3"


def test_a_refused_qualification_runs_no_requalification(w):
    version_ref(w)
    fake = FakeRequalifier()
    service = quality_service(w, requalifier=fake)
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    put_head(w, "experiment", "exp-live", "running", {})
    hold("EVALUATOR_CHANGE_ACTIVE", service.qualify_change, operator(w), change_id)
    assert fake.calls == []


def test_the_quality_track_is_not_imported_by_the_evaluation_service_a_stage_or_a_trial():
    """Nothing in this track is reachable from a candidate experiment (design 16 §3): only the
    meta command module imports `evaluation.quality`."""
    importers = set()
    for path in SRC.rglob("*.py"):
        if path.name == "quality.py" and path.parent.name == "evaluation":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [module] + [f"{module}.{a.name}" for a in node.names]
                if node.level and path.parent.name == "evaluation":
                    names += [a.name for a in node.names if node.module is None]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(
                n.endswith("evaluation.quality") or n == "quality" or n.endswith(".quality")
                for n in names
            ):
                importers.add(str(path.relative_to(SRC)))
    assert importers == {"runtime/meta_commands/evaluator.py"}


def test_the_quality_service_is_not_wired_into_a_deployment_or_a_stage_runner():
    for rel in (
        "evaluation/service.py",
        "evaluation/calibration.py",
        "meta_harness/stages.py",
        "meta_harness/service.py",
        "meta_harness/local_executor.py",
        "runtime/execution/meta_ops.py",
    ):
        path = SRC / rel
        if path.is_file():
            text = path.read_text()
            assert "QualityService" not in text and "evaluation.quality" not in text, rel


# --- the command group -----------------------------------------------------------------------


def test_the_meta_evaluator_group_is_registered_by_module_discovery():
    import typer
    from typer.testing import CliRunner

    from amplai_foundry.runtime.cli import app

    result = CliRunner().invoke(app, ["meta", "evaluator", "--help"])
    assert result.exit_code == 0, result.output
    for command in (
        "status", "requalify", "quality", "propose-change", "qualify-change", "approve-change",
        "reject-change",
    ):  # fmt: skip
        assert command in result.output
    assert typer  # the group is a typer sub-app of `meta`


def test_the_requalify_command_over_a_runtime_root_writes_one_record_per_scope(tmp_path):
    from typer.testing import CliRunner

    from amplai_foundry.runtime.cli import app

    with make_world(tmp_path) as world:
        s2_store(world)
        root = world.m.d.store.root
        scope = world.scope
    result = CliRunner().invoke(
        app, ["meta", "evaluator", "requalify", "--runtime-root", str(root)]
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["evaluator_version"] == "eval-2"
    (row,) = out["scopes"]
    assert row["scope"] == scope.wire()
    assert row["reports"] == 9 and row["equal"] == 9 and row["all_equal"] is True
    assert row["requalification_ref"]["id"].startswith("requal-")


def test_the_requalify_command_refuses_a_bad_version_and_a_missing_store(tmp_path):
    from typer.testing import CliRunner

    from amplai_foundry.runtime.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "meta",
            "evaluator",
            "requalify",
            "--runtime-root",
            str(tmp_path),
            "--evaluator-version",
            "v2",
        ],
    )
    assert result.exit_code != 0
    assert "EVALUATOR_VERSION" in result.output
    result = runner.invoke(app, ["meta", "evaluator", "requalify", "--runtime-root", str(tmp_path)])
    assert result.exit_code != 0
    assert "NOT_FOUND" in result.output


def test_the_quality_command_refuses_an_unreadable_input_file(tmp_path):
    from typer.testing import CliRunner

    from amplai_foundry.runtime.cli import app

    missing = tmp_path / "nope.json"
    result = CliRunner().invoke(
        app,
        [
            "meta",
            "evaluator",
            "quality",
            "--corpus-check",
            str(missing),
            "--config",
            str(tmp_path / "x.json"),
        ],
    )
    assert result.exit_code != 0
    assert "LOCAL_INPUT" in result.output


# --- §11.1 metrics from fixture records -------------------------------------------------------


def ordered(record):
    """The record with its metrics in the declared order (the store returns sorted keys)."""
    return {**record, "metrics": {k: record["metrics"][k] for k in quality.METRICS}}


def test_the_quality_record_is_stored_with_every_metric_and_a_new_revision_per_measurement(w):
    ref = version_ref(w)
    service = quality_service(w)
    first = service.measure(ref, since=EPOCH)
    record = w.m.d.store.get(w.scope, "evaluation-quality", first)
    assert first["id"].startswith("evalq-eval-2-") and first["revision"] == 1
    assert quality.METRICS == (
        "discrimination",
        "saturation",
        "grader_flakiness",
        "contamination",
        "dev_vs_holdout_gap",
        "agreement_with_real_outcomes",
        "domain_coverage",
        "cost_per_decision",
    )
    assert record["evaluator_version"] == "eval-2"
    assert record["evaluator_version_ref"] == ref
    assert record["since"] == EPOCH
    assert record["scope"] == w.scope.wire()
    assert record["inputs"] == {
        "calibration_summaries": [],
        "stage_plans": [],
        "corpus_check": False,
        "negative_controls": [],
    }
    quality.validate_quality(ordered(record))
    second = service.measure(ref, since=EPOCH)
    assert second["id"] == first["id"] and second["revision"] == 2


def test_a_metric_without_records_says_no_data_and_is_never_a_zero(w):
    version_ref(w)
    _, record = metric(w, "saturation")
    for name in (
        "discrimination",
        "saturation",
        "grader_flakiness",
        "dev_vs_holdout_gap",
        "agreement_with_real_outcomes",
        "domain_coverage",
        "cost_per_decision",
    ):
        assert record["metrics"][name]["status"] == "no_data", name
        assert record["metrics"][name]["reason"], name
        assert "share" not in record["metrics"][name]
    # contamination reads the version's own corpus, so it reports zero uses as figures
    assert record["metrics"]["contamination"]["status"] == "ok"
    assert record["metrics"]["contamination"]["holdout_uses"] == 0


def test_measure_refuses_a_bad_since_and_a_record_that_is_not_a_version(w):
    ref = version_ref(w)
    service = quality_service(w)
    for bad in ("yesterday", "2026-10-01T00:00:00", "", None, 5):
        fault("QUALITY_SINCE", service.measure, ref, since=bad)
    corpus_ref, _ = w.corpus(val=2)
    hold("EVALUATOR_UNQUALIFIED", service.measure, corpus_ref, since=EPOCH)
    assert list(w.m.d.store.list_objects(w.scope, "evaluation-quality")) == []


def test_validate_quality_refuses_a_malformed_record(w):
    ref = version_ref(w)
    _, record = metric(w, "saturation")
    record = ordered(record)
    quality.validate_quality(record)
    for bad in (
        {**record, "schema": "x"},
        {k: v for k, v in record.items() if k != "metrics"},
        {**record, "metrics": dict(reversed(list(record["metrics"].items())))},
        {**record, "metrics": {**record["metrics"], "saturation": {"status": "maybe"}}},
        {**record, "evaluator_version_ref": "eval-2"},
        [],
    ):
        fault("QUALITY_RECORD", quality.validate_quality, bad)
    assert ref


# ---- discrimination ----


def calibration_world(w, *, cells=("cell-a",), tasks=("t1", "t2", "t3", "t4")):
    """A calibration plan with a champion and a negative control (a variant) per cell, and a
    summary of the stored evaluator version."""
    ev = version_ref(w)
    compositions = {
        cell: [w.composition(f"champion-{cell}"), w.composition(f"bad-{cell}", "loop_sum")]
        for cell in cells
    }
    plan_ref = put(w, "calibration-plan", new_id("calplan"), {"composition_refs": compositions})
    summary_cells = {
        cell: {"tasks": {t: {"class": "informative"} for t in tasks}} for cell in cells
    }
    return ev, plan_ref, compositions, summary_cells


def add_summary(
    w, ev, plan_ref, summary_cells, *, at="2026-09-30T00:00:00Z", saturated=(), informative=()
):
    return put(
        w,
        "calibration-summary",
        new_id("calsum"),
        {
            "plan_ref": plan_ref,
            "evaluator_version_ref": ev,
            "summarized_at": at,
            "cells": summary_cells,
            "saturated_everywhere": list(saturated),
            "informative_any": list(informative),
        },
    )


def add_trials(w, plan_ref, cell, composition, outcomes, *, split="development", effects=()):
    """`outcomes`: {task: [success, ...]} one trial per element."""
    for task, results in outcomes.items():
        for index, success in enumerate(results):
            put(
                w,
                "calibration-trial",
                new_id("caltrial"),
                {
                    "calibration_plan_ref": plan_ref,
                    "cell_id": cell,
                    "composition_ref": composition,
                    "split": split,
                    "success": success,
                    "unknown_effects": list(effects) if index == 0 and effects else [],
                    "task_id": task,
                },
            )


def test_discrimination_is_the_champion_minus_negative_control_pass_rate_with_an_interval(w):
    ev, plan_ref, comps, summary_cells = calibration_world(w)
    add_summary(w, ev, plan_ref, summary_cells)
    champion, control = comps["cell-a"]
    add_trials(w, plan_ref, "cell-a", champion, {t: [True, True] for t in ("t1", "t2", "t3", "t4")})
    add_trials(
        w, plan_ref, "cell-a", control,
        {"t1": [False, False], "t2": [False, False], "t3": [False, True], "t4": [True, True]},
    )  # fmt: skip
    # noise that must not count: the validation split, an unknown result, an unknown effect
    add_trials(w, plan_ref, "cell-a", champion, {"t1": [False] * 5}, split="validation")
    add_trials(w, plan_ref, "cell-a", champion, {"t2": [None, None]})
    add_trials(w, plan_ref, "cell-a", control, {"t1": [True]}, effects=["write_unknown"])
    got, _ = metric(w, "discrimination", negative_controls={"cell-a": control})
    assert got["status"] == "ok"
    (row,) = got["cells"]
    assert row["cell_id"] == "cell-a"
    assert row["champion_ref"] == champion and row["negative_control_ref"] == control
    assert row["tasks"] == 4
    assert row["champion_pass_rate"] == 1.0
    assert row["negative_control_pass_rate"] == pytest.approx((0 + 0 + 0.5 + 1) / 4)
    deltas = [1.0, 1.0, 0.5, 0.0]
    assert row["difference"] == pytest.approx(mean(deltas))
    half = Z * stdev(deltas) / math.sqrt(4)
    assert row["interval"] == pytest.approx([mean(deltas) - half, mean(deltas) + half])
    assert row["confidence"] == 0.95
    assert row["discriminates"] is True


def test_a_negative_control_that_passes_as_often_as_the_champion_does_not_discriminate(w):
    ev, plan_ref, comps, summary_cells = calibration_world(w)
    add_summary(w, ev, plan_ref, summary_cells)
    champion, control = comps["cell-a"]
    same = {t: [True, False] for t in ("t1", "t2", "t3")}
    add_trials(w, plan_ref, "cell-a", champion, same)
    add_trials(w, plan_ref, "cell-a", control, same)
    got, _ = metric(w, "discrimination", negative_controls={"cell-a": control})
    (row,) = got["cells"]
    assert row["difference"] == 0
    assert row["interval"] == [0.0, 0.0]
    assert row["discriminates"] is False


def test_discrimination_with_one_task_has_no_interval_and_no_verdict(w):
    ev, plan_ref, comps, summary_cells = calibration_world(w)
    add_summary(w, ev, plan_ref, summary_cells)
    champion, control = comps["cell-a"]
    add_trials(w, plan_ref, "cell-a", champion, {"t1": [True]})
    add_trials(w, plan_ref, "cell-a", control, {"t1": [False]})
    got, _ = metric(w, "discrimination", negative_controls={"cell-a": control})
    (row,) = got["cells"]
    assert row["tasks"] == 1 and row["difference"] == 1.0
    assert row["interval"] is None and row["discriminates"] is None


def test_discrimination_is_per_cell_and_skips_a_control_that_is_not_a_plan_variant(w):
    ev, plan_ref, comps, summary_cells = calibration_world(w, cells=("cell-a", "cell-b"))
    add_summary(w, ev, plan_ref, summary_cells)
    for cell in ("cell-a", "cell-b"):
        champion, control = comps[cell]
        add_trials(w, plan_ref, cell, champion, {t: [True] for t in ("t1", "t2")})
        add_trials(w, plan_ref, cell, control, {t: [False] for t in ("t1", "t2")})
    got, _ = metric(w, "discrimination", negative_controls={"cell-a": comps["cell-a"][1]})
    assert [r["cell_id"] for r in got["cells"]] == ["cell-a"]
    # the control of cell-b named for cell-a is no variant of cell-a: nothing measured
    got, _ = metric(w, "discrimination", negative_controls={"cell-a": comps["cell-b"][1]})
    assert got["status"] == "no_data"
    # the champion itself is not a negative control
    got, _ = metric(w, "discrimination", negative_controls={"cell-a": comps["cell-a"][0]})
    assert got["status"] == "no_data"


def test_discrimination_is_no_data_without_a_declared_control_or_without_trials(w):
    ev, plan_ref, comps, summary_cells = calibration_world(w)
    add_summary(w, ev, plan_ref, summary_cells)
    got, _ = metric(w, "discrimination")
    assert got["status"] == "no_data" and "no negative control" in got["reason"]
    got, _ = metric(w, "discrimination", negative_controls={"cell-a": comps["cell-a"][1]})
    assert got["status"] == "no_data" and "development trials" in got["reason"]


def test_a_negative_control_must_be_a_record_ref(w):
    ev, plan_ref, _comps, summary_cells = calibration_world(w)
    add_summary(w, ev, plan_ref, summary_cells)
    service = quality_service(w)
    for bad in ("bad-prompt", {"id": "x"}, None):
        fault(
            "QUALITY_NEGATIVE_CONTROL", service.measure, ev, since=EPOCH,
            negative_controls={"cell-a": bad},
        )  # fmt: skip


# ---- saturation and grader flakiness ----


def test_saturation_is_the_share_of_tasks_saturated_in_every_cell(w):
    ev, plan_ref, _, summary_cells = calibration_world(w, cells=("cell-a", "cell-b"))
    add_summary(
        w,
        ev,
        plan_ref,
        summary_cells,
        saturated=["t1", "t2", "t-not-in-the-summary"],
        informative=["t3"],
    )
    got, record = metric(w, "saturation")
    assert got["status"] == "ok"
    assert got["share"] == 0.5
    latest = got["latest_summary"]
    assert latest["tasks"] == 4 and latest["saturated_everywhere"] == 2
    assert latest["informative_any"] == 1 and latest["share"] == 0.5
    assert len(record["inputs"]["calibration_summaries"]) == 1


def test_saturation_reports_the_newest_summary_and_honours_since_and_the_version(w):
    ev, plan_ref, _, summary_cells = calibration_world(w)
    add_summary(w, ev, plan_ref, summary_cells, at="2026-09-01T00:00:00Z", saturated=["t1"])
    add_summary(
        w, ev, plan_ref, summary_cells, at="2026-09-20T00:00:00Z", saturated=["t1", "t2", "t3"]
    )
    # a summary of another evaluator version is not this version's saturation
    other = {**ev, "id": "evaluator-9"}
    add_summary(w, other, plan_ref, summary_cells, at="2026-09-25T00:00:00Z", saturated=["t4"])
    got, _ = metric(w, "saturation")
    assert [r["share"] for r in got["rows"]] == [0.25, 0.75] or sorted(
        r["share"] for r in got["rows"]
    ) == [0.25, 0.75]
    assert got["share"] == 0.75
    assert got["latest_summary"]["summarized_at"] == "2026-09-20T00:00:00Z"
    got, _ = metric(w, "saturation", since="2026-09-10T00:00:00Z")
    assert len(got["rows"]) == 1 and got["share"] == 0.75
    got, _ = metric(w, "saturation", since="2026-10-01T00:00:00Z")
    assert got["status"] == "no_data"


def test_grader_flakiness_is_the_share_of_tasks_flagged_by_the_corpus_check(w):
    ev, plan_ref, _, summary_cells = calibration_world(w)
    summary_cells["cell-a"]["tasks"]["t3"] = {"class": "flaky_grading"}
    add_summary(w, ev, plan_ref, summary_cells)
    check = {
        "checked": 4,
        "skipped": [],
        "unfair": [],
        "repeats": 3,
        "tasks": {
            "t1": {"fair": True, "flaky": True},
            "t2": {"fair": True, "flaky": False},
            "t3": {"fair": True, "flaky": False},
            "t4": {"fair": True, "flaky": True},
        },
    }
    got, record = metric(w, "grader_flakiness", corpus_check=check)
    assert got["status"] == "ok"
    assert got["flagged"] == ["t1", "t4"]
    assert got["share"] == 0.5 and got["checked"] == 4
    assert got["repeats"] == 3 and got["meets_protocol"] is True
    assert got["calibration_flaky_tasks"] == ["t3"]
    assert record["inputs"]["corpus_check"] is True


def test_grader_flakiness_counts_checked_tasks_itself_and_flags_a_short_protocol(w):
    version_ref(w)
    check = {
        "repeats": 2,
        "tasks": {
            "a": {"fair": True, "flaky": False},
            "b": {"fair": True, "flaky": True},
            "c": {"fair": None, "flaky": None},  # skipped: not checked
        },
    }
    got, _ = metric(w, "grader_flakiness", corpus_check=check)
    assert got["checked"] == 2 and got["share"] == 0.5
    assert got["meets_protocol"] is False
    # nothing checked is no data, not a zero share
    got, _ = metric(w, "grader_flakiness", corpus_check={"repeats": 3, "tasks": {}})
    assert got["status"] == "no_data" and got["share"] is None


def test_grader_flakiness_is_no_data_without_the_check_output_but_lists_calibration_flaky(w):
    ev, plan_ref, _, summary_cells = calibration_world(w)
    summary_cells["cell-a"]["tasks"]["t2"] = {"class": "flaky_grading"}
    add_summary(w, ev, plan_ref, summary_cells)
    got, _ = metric(w, "grader_flakiness")
    assert got["status"] == "no_data"
    assert got["calibration_flaky_tasks"] == ["t2"]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"repeats": 3},
        {"tasks": {}},
        {"repeats": 0, "tasks": {}},
        {"repeats": "3", "tasks": {}},
        {"repeats": 3, "tasks": []},
        {"repeats": 3, "tasks": {"a": "flaky"}},
    ],
)
def test_a_malformed_corpus_check_is_a_quality_fault_and_writes_no_record(w, bad):
    ev = version_ref(w)
    fault("QUALITY_CORPUS_CHECK", quality_service(w).measure, ev, since=EPOCH, corpus_check=bad)
    assert list(w.m.d.store.list_objects(w.scope, "evaluation-quality")) == []


# ---- contamination, dev-vs-holdout gap and cost per decision (stage records) ----


def holdout_corpus(w):
    corpus_id = new_id("qcorpus")
    d, m = w.m.d, w.m
    cases = []
    for split, count in (("validation", 2), ("holdout", 2)):
        for i in range(count):
            data = canonical({"split": split, "i": i, "salt": corpus_id})
            cases.append(
                {
                    "case_id": f"{split[:3]}-{i}",
                    "split": split,
                    "task_class": "bug",
                    "artifact_ref": d.artifacts.admit(
                        w.scope, data, "application/json", trust="operator"
                    ),
                }
            )
    ref = CorpusService(d.store, d.artifacts).freeze(
        m.reviewer, corpus_id, cases, holdout_use_limit=3
    )
    return ref, cases


def stage_fixture(
    w, ev, proposal_id, stages, *, leak_scan="absent", created="2026-09-30T00:00:00Z"
):
    """A stage plan of `ev` for `proposal_id`, its change artifact and its stage-run head.
    `stages`: {stage: (verdict, delta | None, [tokens per trial, None for unknown],
    findings)}."""
    plan_ref = put(
        w,
        "stage-plan",
        new_id("stageplan"),
        {"evaluator_version_ref": ev, "created_at": created, "proposal_id": proposal_id},
    )
    change = {"change": "x"}
    if leak_scan != "absent":
        change["leak_scan"] = leak_scan
    change_artifact = artifact(w, change)
    put(w, "harness-change-proposal", proposal_id, {"change_artifact": change_artifact})
    entries = {}
    for stage, (verdict, delta, tokens, findings) in stages.items():
        run_refs = []
        for amount in tokens:
            value = {} if amount is None else {"input_tokens": amount - 10, "output_tokens": 10}
            run_refs.append(put(w, "eval-trial", new_id("trial"), value))
        analysis = artifact(w, {} if delta is None else {"candidate_minus_baseline": delta})
        report = {
            "verdict": verdict,
            "run_refs": run_refs,
            "analysis_artifact": analysis,
            **({"contamination_findings": findings} if findings else {}),
        }
        entries[stage] = {
            "state": "done",
            "report_ref": put(w, "eval-report", new_id("rep"), report),
        }
    put_head(w, "stage-run", "stagerun-" + proposal_id, "stages", {"stages": entries})
    return plan_ref


def build_stage_records(w):
    """Proposal A: screening pass (+0.30, 2 trials) and holdout fail (+0.10, 1 trial);
    B: screening inconclusive (+0.20, 3 trials, one without token usage) and holdout pass (-0.05,
    1 trial); C: screening pass only (2 trials)."""
    corpus_ref, _ = holdout_corpus(w)
    ev = version_ref(w, corpus_ref)
    stage_fixture(
        w, ev, "prop-a",
        {"screening": ("pass", 0.30, [150, 150], None), "holdout": ("fail", 0.10, [150], None)},
        leak_scan={"hits": 3},
    )  # fmt: skip
    stage_fixture(
        w, ev, "prop-b",
        {
            "screening": ("inconclusive", 0.20, [150, None, 150], [{"task": "v-0"}]),
            "holdout": ("pass", -0.05, [150], None),
        },
        leak_scan=None,
    )  # fmt: skip
    stage_fixture(
        w, ev, "prop-c", {"screening": ("pass", 0.5, [150, 150], None)}, leak_scan={"hits": 0}
    )
    return ev, corpus_ref


def test_dev_vs_holdout_gap_is_the_screening_delta_minus_the_holdout_delta_per_proposal(w):
    build_stage_records(w)
    got, record = metric(w, "dev_vs_holdout_gap")
    assert got["status"] == "ok"
    rows = {r["proposal_id"]: r for r in got["proposals"]}
    assert set(rows) == {"prop-a", "prop-b"}  # prop-c has no holdout report
    assert rows["prop-a"]["screening_delta"] == 0.30 and rows["prop-a"]["holdout_delta"] == 0.10
    assert rows["prop-a"]["gap"] == pytest.approx(0.20)
    assert rows["prop-b"]["gap"] == pytest.approx(0.25)
    assert got["mean_gap"] == pytest.approx(0.225)
    assert len(record["inputs"]["stage_plans"]) == 3


def test_the_gap_is_no_data_without_a_proposal_that_has_both_reports(w):
    corpus_ref, _ = holdout_corpus(w)
    ev = version_ref(w, corpus_ref)
    stage_fixture(w, ev, "only-screen", {"screening": ("pass", 0.2, [100], None)})
    stage_fixture(w, ev, "no-delta", {"screening": ("pass", None, [100], None),
                                      "holdout": ("pass", None, [100], None)})  # fmt: skip
    got, _ = metric(w, "dev_vs_holdout_gap")
    assert got["status"] == "no_data"


def test_stage_plans_of_another_version_or_before_since_are_not_counted(w):
    corpus_ref, _ = holdout_corpus(w)
    ev = version_ref(w, corpus_ref)
    stage_fixture(
        w, {**ev, "id": "evaluator-9"}, "other-version",
        {"screening": ("pass", 0.1, [100], None), "holdout": ("pass", 0.0, [100], None)},
    )  # fmt: skip
    stage_fixture(
        w, ev, "old",
        {"screening": ("pass", 0.1, [100], None), "holdout": ("pass", 0.0, [100], None)},
        created="2026-01-01T00:00:00Z",
    )  # fmt: skip
    assert metric(w, "dev_vs_holdout_gap")[0]["proposals"][0]["proposal_id"] == "old"
    got, record = metric(w, "dev_vs_holdout_gap", since="2026-06-01T00:00:00Z")
    assert got["status"] == "no_data" and record["inputs"]["stage_plans"] == []
    assert metric(w, "cost_per_decision", since="2026-06-01T00:00:00Z")[0]["status"] == "no_data"


def test_cost_per_decision_splits_concluded_from_inconclusive_verdicts(w):
    build_stage_records(w)
    got, _ = metric(w, "cost_per_decision")
    assert got["status"] == "ok"
    concluded = got["verdicts"]["concluded"]
    # prop-a screening+holdout, prop-b holdout, prop-c screening
    assert concluded["reports"] == 4 and concluded["trials"] == 6
    assert concluded["tokens"] == 6 * 150 and concluded["unknown_token_trials"] == 0
    assert concluded["trials_per_report"] == 1.5
    assert concluded["tokens_per_report"] == 6 * 150 / 4
    inconclusive = got["verdicts"]["inconclusive"]
    assert inconclusive["reports"] == 1 and inconclusive["trials"] == 3
    assert inconclusive["tokens"] == 300  # unknown usage is never counted as zero spend
    assert inconclusive["unknown_token_trials"] == 1
    assert got["concluded_share"] == pytest.approx(4 / 5)


def test_contamination_counts_holdout_uses_flagged_heads_findings_and_leak_scans(w):
    _ev, corpus_ref = build_stage_records(w)
    corpus = w.m.d.store.get(w.scope, "eval-corpus", corpus_ref)
    holdout_keys = [
        "case-" + c["artifact_ref"]["digest"][7:]
        for c in corpus["cases"]
        if c["split"] == "holdout"
    ]
    exp = [
        {"id": "exp-1", "revision": 1, "digest": ZERO},
        {"id": "exp-2", "revision": 1, "digest": ZERO},
    ]
    put_head(w, "holdout-use", corpus_ref["id"], "active",
             {"experiment_refs": exp, "contaminated": False, "use_limit": 3})  # fmt: skip
    put_head(w, "holdout-use", holdout_keys[0], "active",
             {"experiment_refs": exp, "contaminated": True, "use_limit": 3})  # fmt: skip
    put_head(w, "holdout-use", holdout_keys[1], "active",
             {"experiment_refs": exp[:1], "contaminated": False, "use_limit": 3})  # fmt: skip
    got, _ = metric(w, "contamination")
    assert got["status"] == "ok"
    assert got["corpus_ref"] == corpus_ref
    assert got["holdout_uses"] == 2
    assert got["holdout_use_limit"] == 3
    assert got["max_case_uses"] == 2
    assert got["contaminated_heads"] == 1
    assert got["corpus_findings"] == 0
    assert got["reports_with_findings"] == 1  # prop-b screening carries a finding
    assert got["leak_scan"] == {
        "proposals_scanned": 2,  # prop-a (3 hits), prop-c (0 hits)
        "proposals_unscanned": 1,  # prop-b: leak_scan is null
        "unreadable_change_artifacts": 0,
        "hits": 3,
    }


def test_a_change_artifact_that_cannot_be_read_is_counted_apart_never_as_clean(w):
    corpus_ref, _ = holdout_corpus(w)
    ev = version_ref(w, corpus_ref)
    stage_fixture(w, ev, "prop-x", {"screening": ("pass", 0.1, [100], None)}, leak_scan={"hits": 0})
    put(w, "harness-change-proposal", "prop-x",
        {"change_artifact": GONE}, revision=2)  # fmt: skip
    got, _ = metric(w, "contamination")
    assert got["leak_scan"]["unreadable_change_artifacts"] == 1
    assert got["leak_scan"]["proposals_scanned"] == 0


# ---- agreement with real outcomes (canary goals and merged PRs, D-078) ----


def real_goal(
    w, goal_id, status, *, finished, outcome=None, created=FAR_PAST, trial=None, task_class=None
):
    graph = {
        "id": f"graph-{goal_id}",
        "revision": 1,
        "digest": "sha256:" + goal_id.encode().hex().ljust(64, "0")[:64],
    }
    record = {
        "status": status,
        "created_at": created,
        "finished_at": finished,
        "graph_ref": graph,
        **({"publication_outcome": {"state": outcome}} if outcome else {}),
        **({"trial": {"trial_id": "t"}} if trial else {}),
    }
    put_head(w, "execution-plan", goal_id, status, record)
    if task_class:
        put(w, "task-class", f"tc-{goal_id}", {"graph_ref": graph, "task_class": task_class})


def promote(w, proposal_id, *, state="promoted", canary=(True, True, False), incidents=()):
    put_head(
        w, "evolution", proposal_id, state,
        {"canary_trials": [{"success": s} for s in canary], "canary_incidents": list(incidents)},
    )  # fmt: skip
    event(w, "evolution", proposal_id, "evolution.promoted")


def test_agreement_compares_real_goals_before_and_after_a_promotion(w):
    version_ref(w)
    promote(w, "prop-p", incidents=["inc-1"])
    # before the promotion
    real_goal(w, "g1", "verified", finished="2020-01-02T00:00:00Z", outcome="MERGED")
    real_goal(w, "g2", "failed", finished="2020-01-03T00:00:00Z")
    real_goal(w, "g3", "published", finished="2020-01-04T00:00:00Z", outcome="CLOSED")
    # after the promotion
    real_goal(w, "g4", "verified", finished=FAR_FUTURE, outcome="MERGED")
    real_goal(w, "g5", "published", finished="2099-02-01T00:00:00Z", outcome="MERGED")
    # not counted: still running, no finish time, a trial goal
    real_goal(w, "g6", "running", finished=None)
    real_goal(w, "g7", "verified", finished=None, outcome="MERGED")
    real_goal(w, "g8", "verified", finished="2099-03-01T00:00:00Z", outcome="MERGED", trial=True)
    got, _ = metric(w, "agreement_with_real_outcomes")
    assert got["status"] == "ok"
    (row,) = got["promotions"]
    assert row["proposal_id"] == "prop-p" and row["state"] == "promoted"
    assert row["canary"] == {
        "runs": 3, "successes": 2, "success_rate": pytest.approx(2 / 3), "incidents": 1,
    }  # fmt: skip
    before, after = row["before"], row["after"]
    assert before["goals"] == 3 and before["verified"] == 2
    assert before["merged"] == 1 and before["closed"] == 1
    assert before["verified_rate"] == pytest.approx(2 / 3)
    assert before["acceptance_rate"] == 0.5
    assert before["verified_and_merged_rate"] == pytest.approx(1 / 3)
    assert after["goals"] == 2 and after["verified"] == 2 and after["merged"] == 2
    assert after["closed"] == 0
    assert after["verified_rate"] == 1.0
    assert after["acceptance_rate"] == 1.0
    assert after["verified_and_merged_rate"] == 1.0


def test_agreement_with_no_goals_in_a_window_has_no_rate_not_a_zero(w):
    version_ref(w)
    promote(w, "prop-p", canary=())
    got, _ = metric(w, "agreement_with_real_outcomes")
    (row,) = got["promotions"]
    assert row["canary"]["success_rate"] is None
    for window in (row["before"], row["after"]):
        assert window["goals"] == 0
        assert window["verified_rate"] is None
        assert window["acceptance_rate"] is None
        assert window["verified_and_merged_rate"] is None


def test_agreement_counts_a_rolled_back_promotion_and_ignores_unpromoted_candidates(w):
    version_ref(w)
    assert metric(w, "agreement_with_real_outcomes")[0]["status"] == "no_data"
    put_head(w, "evolution", "prop-canary", "canary", {"canary_trials": [{"success": True}]})
    assert metric(w, "agreement_with_real_outcomes")[0]["status"] == "no_data"
    promote(w, "prop-rb", state="rolled_back")
    got, _ = metric(w, "agreement_with_real_outcomes")
    assert [r["proposal_id"] for r in got["promotions"]] == ["prop-rb"]
    assert got["promotions"][0]["state"] == "rolled_back"


def test_agreement_since_after_the_promotion_leaves_no_promotion(w):
    version_ref(w)
    promote(w, "prop-p")
    got, _ = metric(w, "agreement_with_real_outcomes", since=FAR_FUTURE)
    assert got["status"] == "no_data"


# ---- domain coverage ----


def test_domain_coverage_is_the_total_variation_between_corpus_and_real_goal_mix(w):
    corpus_id = new_id("mixcorpus")
    d = w.m.d
    cases = [
        {
            "case_id": f"c{i}",
            "split": "validation",
            "task_class": domain,
            "artifact_ref": d.artifacts.admit(
                w.scope, canonical({"i": i, "s": corpus_id}), "application/json", trust="operator"
            ),
        }
        for i, domain in enumerate(["bug", "bug", "feature", "feature", "docs", "docs"])
    ]
    corpus_ref = CorpusService(d.store, d.artifacts).freeze(
        w.m.reviewer, corpus_id, cases, holdout_use_limit=1
    )
    version_ref(w, corpus_ref)
    for i in range(3):
        real_goal(w, f"b{i}", "verified", finished=FAR_FUTURE, task_class="bug_fix")
    real_goal(w, "f0", "verified", finished=FAR_FUTURE, task_class="new_feature")
    real_goal(w, "u0", "verified", finished=FAR_FUTURE, task_class="design")  # unmapped
    real_goal(w, "n0", "verified", finished=FAR_FUTURE)  # no task-class record
    real_goal(w, "t0", "verified", finished=FAR_FUTURE, task_class="bug_fix", trial=True)
    real_goal(w, "old", "verified", finished=FAR_PAST, created="2019-01-01T00:00:00Z",
              task_class="refactor")  # fmt: skip
    got, _ = metric(w, "domain_coverage", since="2020-01-01T00:00:00Z")
    third = 1 / 3
    assert got["status"] == "ok"
    assert got["corpus_mix"] == {k: pytest.approx(third) for k in ("bug", "feature", "docs")}
    assert got["real_mix"] == {"bug": 0.75, "feature": 0.25}
    assert got["real_goals_mapped"] == 4
    assert got["unmapped_task_classes"] == {"design": 1, "unreported": 1}
    expected = 0.5 * (abs(third - 0.75) + abs(third - 0.25) + abs(third - 0))
    assert got["total_variation"] == pytest.approx(expected)
    assert got["total_variation"] == pytest.approx(
        total_variation(
            {"bug": third, "feature": third, "docs": third}, {"bug": 0.75, "feature": 0.25}
        )
    )
    assert got["corpus_domains_without_real_class"] == ["docs"]
    assert got["table"] == quality.TASK_CLASS_TO_DOMAIN
    # the real goal created before `since` counts when `since` is earlier
    got, _ = metric(w, "domain_coverage", since=EPOCH)
    assert got["real_goals_mapped"] == 5 and got["real_mix"]["refactor"] == 0.2


def test_domain_coverage_without_a_mapped_real_goal_is_no_data_with_the_unmapped_classes(w):
    version_ref(w)
    real_goal(w, "u1", "verified", finished=FAR_FUTURE, task_class="design")
    got, _ = metric(w, "domain_coverage")
    assert got["status"] == "no_data"
    assert got["unmapped_task_classes"] == {"design": 1}
    assert got["corpus_mix"]


def test_task_class_to_domain_is_a_declared_table_of_corpus_domains():
    assert quality.TASK_CLASS_TO_DOMAIN == {
        "bug_fix": "bug",
        "new_feature": "feature",
        "refactor": "refactor",
        "operations": "cli_ops",
    }


def test_total_variation_and_shares_are_the_plain_definitions():
    assert total_variation({"a": 1.0}, {"a": 1.0}) == 0
    assert total_variation({"a": 1.0}, {"b": 1.0}) == 1.0
    assert total_variation({"a": 0.5, "b": 0.5}, {"a": 0.25, "b": 0.75}) == 0.25
    assert quality.shares({"a": 1, "b": 3}) == {"a": 0.25, "b": 0.75}
    assert quality.shares({}) == {}
    assert quality.paired_interval([1.0]) is None
    assert quality.paired_interval([]) is None
    low, high = quality.paired_interval([0.0, 1.0, 2.0])
    assert low < 1.0 < high


# --- IC-25: qualify_change runs the §7.8 Q-suite and stores its JUnit result -------------------


def proposed(w, **kw):
    """A world with eval-2, a service with a fake suite and one open change."""
    version_ref(w)
    service = quality_service(w, **kw)
    return service, service.propose_change(operator(w), to=target(), reason="r")


def test_the_qualification_files_are_the_six_files_of_ic_25():
    assert quality.QUALIFICATION_FILES == (
        "tests/v3/test_033_golden_analysis.py",
        "tests/v3/test_033_s1_analysis.py",
        "tests/v3/test_033_s1_sequential.py",
        "tests/v3/test_033_s2_service.py",
        "tests/v3/test_033_s2_calibration.py",
        "tests/v3/test_033_s2_requalify.py",
    )
    for name in quality.QUALIFICATION_FILES:
        assert (REPO / name).is_file(), name


def test_qualify_runs_the_injected_suite_over_the_ic_25_files_and_stores_the_result(w):
    suite = FakeSuite()
    service, change_id = proposed(w, suite_runner=suite)
    assert suite.calls == []  # propose runs nothing
    service.qualify_change(operator(w), change_id)
    assert suite.calls == [quality.QUALIFICATION_FILES]
    stored = service.change(change_id)["qualification_suite"]
    assert set(stored) >= {"files", "passed", "failed", "errors", "junit_digest", "code_digests"}
    assert stored["tests"] == stored["passed"] == 6 and stored["skipped"] == 0
    assert all(r["tests"] == 1 and r["passed"] == 1 for r in stored["files"])
    assert service.change(change_id)["state"] == "qualified"


def test_the_default_suite_runner_is_the_pytest_subprocess_runner(w):
    plain = QualityService(w.m.d.store, w.scope)
    assert plain._suite_runner is quality.run_qualification_suite


def test_parse_junit_counts_per_file_and_in_total_and_digests_the_file(tmp_path):
    xml = junit_xml(
        {
            SUITE_STEMS[0]: (3, 0, 0, 0),
            SUITE_STEMS[1]: (2, 1, 0, 1),
            SUITE_STEMS[2]: (1, 0, 2, 0),
        }
    )
    path = tmp_path / "j.xml"
    path.write_text(xml)
    got = quality.parse_junit(path, quality.QUALIFICATION_FILES)
    rows = {r["file"]: r for r in got["files"]}
    first, second, third = (rows[f] for f in quality.QUALIFICATION_FILES[:3])
    assert (first["passed"], first["failed"], first["errors"], first["skipped"]) == (3, 0, 0, 0)
    assert (second["passed"], second["failed"], second["errors"], second["skipped"]) == (2, 1, 0, 1)
    assert (third["passed"], third["failed"], third["errors"]) == (1, 0, 2)
    assert (got["passed"], got["failed"], got["errors"], got["skipped"]) == (9, 1, 2, 1)
    assert got["tests"] == 13
    assert got["junit_digest"] == "sha256:" + hashlib.sha256(xml.encode()).hexdigest()


def test_parse_junit_matches_a_class_a_dotted_module_and_a_collection_error(tmp_path):
    files = ("tests/v3/test_a.py", "tests/v3/test_b.py", "tests/v3/test_c.py")
    xml = (
        '<testsuites><testsuite errors="1" failures="0" tests="3">'
        '<testcase classname="tests.v3.test_a.TestK" name="test_1"/>'
        '<testcase classname="test_b" name="test_2"><failure message="x"/></testcase>'
        '<testcase classname="" name="tests.v3.test_c"><error message="collection failure"/>'
        "</testcase></testsuite></testsuites>"
    )
    path = tmp_path / "j.xml"
    path.write_text(xml)
    rows = {r["file"]: r for r in quality.parse_junit(path, files)["files"]}
    assert rows["tests/v3/test_a.py"]["passed"] == 1
    assert rows["tests/v3/test_b.py"]["failed"] == 1
    assert rows["tests/v3/test_c.py"]["errors"] == 1


def test_parse_junit_never_drops_an_unattributed_failure_or_a_suite_level_count(tmp_path):
    files = ("tests/v3/test_a.py",)
    path = tmp_path / "j.xml"
    path.write_text(
        '<testsuites><testsuite errors="0" failures="0" tests="2">'
        '<testcase classname="test_a" name="t"/>'
        '<testcase classname="elsewhere" name="t"><failure message="x"/></testcase>'
        "</testsuite></testsuites>"
    )
    assert quality.parse_junit(path, files)["failed"] == 1
    path.write_text(
        '<testsuites><testsuite errors="2" failures="1" tests="1">'
        '<testcase classname="test_a" name="t"/></testsuite></testsuites>'
    )
    got = quality.parse_junit(path, files)
    assert (got["failed"], got["errors"]) == (1, 2)


@pytest.mark.parametrize("content", [None, "", "<testsuites>", "not xml at all"])
def test_parse_junit_refuses_a_missing_or_malformed_file(tmp_path, content):
    path = tmp_path / "j.xml"
    if content is not None:
        path.write_text(content)
    fault("EVALUATOR_SUITE_JUNIT", quality.parse_junit, path, quality.QUALIFICATION_FILES)


UNATTRIBUTED = '<testcase classname="x" name="y"><error message="e"/></testcase>'


@pytest.mark.parametrize(
    ("xml", "returncode"),
    [
        (junit_xml({SUITE_STEMS[1]: (2, 1, 0, 0)}), 1),  # a failed test
        (junit_xml({SUITE_STEMS[3]: (2, 0, 1, 0)}), 1),  # an errored test
        (junit_xml(), 1),  # a non-zero exit code over a clean file
        (junit_xml({SUITE_STEMS[4]: (0, 0, 0, 0)}), 5),  # a file that ran no test
        (junit_xml({SUITE_STEMS[5]: (0, 0, 0, 3)}), 0),  # a file whose tests were all skipped
        (junit_xml().replace('failures="0"', 'failures="1"'), 0),  # a suite-level failure count
        (junit_xml(extra=UNATTRIBUTED), 0),  # an unattributed error
    ],
    ids=["failed", "errored", "exit-code", "no-test", "all-skipped", "suite-count", "unattributed"],
)
def test_a_suite_that_is_not_clean_keeps_the_change_proposed_and_unapprovable(w, xml, returncode):
    service, change_id = proposed(w, suite_runner=FakeSuite(xml, returncode))
    service.qualify_change(operator(w), change_id)
    change = service.change(change_id)
    assert change["state"] == "proposed"
    assert change["qualified_by"] is None and change["code_digests"] is None
    assert change["qualification_suite"]["code_digests"] == versions.code_digests()
    assert change["qualification_scope"]["suite_passed"] is False
    assert change["qualification_scope"]["q02_all_equal"] is True
    hold("EVALUATOR_UNQUALIFIED", service.approve_change, operator(w), change_id)
    fault("NOT_FOUND", service.version_ref, "eval-3")
    # a later clean run qualifies the same change
    fixed = quality_service(w, suite_runner=FakeSuite())
    fixed.qualify_change(operator(w), change_id)
    assert fixed.change(change_id)["state"] == "qualified"


def test_a_failed_suite_stores_the_counts_the_operator_needs(w):
    service, change_id = proposed(
        w, suite_runner=FakeSuite(junit_xml({SUITE_STEMS[1]: (2, 3, 1, 0)}), 1)
    )
    service.qualify_change(operator(w), change_id)
    suite = service.change(change_id)["qualification_suite"]
    assert (suite["passed"], suite["failed"], suite["errors"], suite["returncode"]) == (7, 3, 1, 1)
    rows = {r["file"]: r for r in suite["files"]}
    assert rows[quality.QUALIFICATION_FILES[1]]["failed"] == 3


def test_a_passing_suite_does_not_qualify_a_change_whose_q02_differs(w):
    version_ref(w)
    fake = FakeRequalifier(all_equal=False)
    service = quality_service(w, requalifier=fake, suite_runner=FakeSuite())
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    service.qualify_change(operator(w), change_id)
    change = service.change(change_id)
    assert change["state"] == "proposed"
    assert change["qualification_scope"]["suite_passed"] is True
    assert change["qualification_scope"]["q02_all_equal"] is False
    hold("EVALUATOR_UNQUALIFIED", service.approve_change, operator(w), change_id)


def boom():
    raise quality.RuntimeFault("EVALUATOR_SUITE_RUN", "boom")


@pytest.mark.parametrize(
    ("runner", "code"),
    [
        (FakeSuite(xml=False), "EVALUATOR_SUITE_JUNIT"),
        (FakeSuite(xml="<testsuites>"), "EVALUATOR_SUITE_JUNIT"),
        (FakeSuite(before=boom), "EVALUATOR_SUITE_RUN"),
    ],
    ids=["missing-junit", "malformed-junit", "runner-fault"],
)
def test_a_missing_junit_a_malformed_junit_and_a_runner_fault_change_nothing(w, runner, code):
    service, change_id = proposed(w, suite_runner=runner)
    before = list(w.m.d.store.list_objects(w.scope, "evaluator-requalification"))
    fault(code, service.qualify_change, operator(w), change_id)
    assert service.change(change_id)["state"] == "proposed"
    assert service.change(change_id)["qualification_suite"] is None
    assert list(w.m.d.store.list_objects(w.scope, "evaluator-requalification")) == before


def test_a_refused_or_blocked_qualification_runs_no_suite(w):
    suite = FakeSuite()
    service, change_id = proposed(w, suite_runner=suite)
    fault("FORBIDDEN", service.qualify_change, manage_only(w), change_id)
    fault("FORBIDDEN", service.qualify_change, proposer_of(w), change_id)
    put_head(w, "experiment", "exp-live", "running", {})
    hold("EVALUATOR_CHANGE_ACTIVE", service.qualify_change, operator(w), change_id)
    assert suite.calls == []
    set_head(w, "experiment", "exp-live", "finished", {})
    service.qualify_change(operator(w), change_id)
    assert len(suite.calls) == 1


def test_a_closed_change_does_not_run_the_suite(w):
    suite = FakeSuite()
    service, change_id = proposed(w, suite_runner=suite)
    service.reject_change(operator(w), change_id, "no")
    hold("EVALUATOR_CHANGE_STATE", service.qualify_change, operator(w), change_id)
    assert suite.calls == []


def test_code_that_changes_while_the_suite_runs_is_a_hold_and_changes_nothing(w, monkeypatch):
    service, change_id = proposed(w)
    digests = iter(
        [
            {"analysis_code_digest": "sha256:" + "1" * 64, "service_code_digest": ZERO},
            {"analysis_code_digest": "sha256:" + "2" * 64, "service_code_digest": ZERO},
        ]
    )
    monkeypatch.setattr(quality.versions, "code_digests", lambda: next(digests))
    hold("EVALUATOR_CHANGED", service.qualify_change, operator(w), change_id)
    monkeypatch.undo()
    assert service.change(change_id)["state"] == "proposed"
    assert service.change(change_id)["code_digests"] is None


def test_approve_rechecks_the_digests_of_the_stored_suite_too(w):
    service, change_id = proposed(w)
    service.qualify_change(operator(w), change_id)
    head = w.m.d.store.head(w.scope, "evaluator-change", change_id)
    data = json.loads(json.dumps(head["data"]))
    data["qualification_suite"]["code_digests"]["analysis_code_digest"] = "sha256:" + "9" * 64
    set_head(w, "evaluator-change", change_id, "qualified", data)
    hold("EVALUATOR_CHANGED", service.approve_change, operator(w), change_id)
    fault("NOT_FOUND", service.version_ref, "eval-3")


@pytest.mark.parametrize("tamper", ["drop", "failed", "returncode", "file_missing"])
def test_approve_refuses_a_qualified_head_without_a_passing_suite(w, tamper):
    service, change_id = proposed(w)
    service.qualify_change(operator(w), change_id)
    head = w.m.d.store.head(w.scope, "evaluator-change", change_id)
    data = json.loads(json.dumps(head["data"]))
    suite = data["qualification_suite"]
    if tamper == "drop":
        del data["qualification_suite"]
    elif tamper == "failed":
        suite["failed"] = 1
    elif tamper == "returncode":
        suite["returncode"] = 1
    else:
        suite["files"] = suite["files"][:-1]
    set_head(w, "evaluator-change", change_id, "qualified", data)
    err = hold("EVALUATOR_UNQUALIFIED", service.approve_change, operator(w), change_id)
    assert err.details == "qualification_suite"
    fault("NOT_FOUND", service.version_ref, "eval-3")


def test_the_default_runner_builds_a_pytest_junit_command_with_this_interpreter(
    tmp_path, monkeypatch
):
    import subprocess
    import sys

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"], seen["kwargs"] = command, kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    for name in quality.QUALIFICATION_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("")
    monkeypatch.setattr(subprocess, "run", fake_run)
    code = quality.run_qualification_suite(
        quality.QUALIFICATION_FILES, tmp_path / "j.xml", root=tmp_path
    )
    assert code == 0
    command = seen["command"]
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert f"--junitxml={tmp_path / 'j.xml'}" in command
    assert tuple(command[-6:]) == quality.QUALIFICATION_FILES
    assert seen["kwargs"]["cwd"] == tmp_path
    assert seen["kwargs"]["timeout"] == quality.SUITE_TIMEOUT_SECONDS


def test_the_default_runner_refuses_missing_files_and_a_timeout(tmp_path, monkeypatch):
    import subprocess

    err = fault(
        "EVALUATOR_SUITE_MISSING",
        quality.run_qualification_suite,
        quality.QUALIFICATION_FILES,
        tmp_path / "j.xml",
        root=tmp_path,
    )
    assert err.details == list(quality.QUALIFICATION_FILES)
    (tmp_path / "t_a.py").write_text("")

    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, "run", slow)
    fault(
        "EVALUATOR_SUITE_RUN",
        quality.run_qualification_suite,
        ("t_a.py",),
        tmp_path / "j.xml",
        root=tmp_path,
    )


def test_the_default_runner_and_parser_agree_on_a_tiny_real_pytest_run(tmp_path):
    """The one real subprocess of this file: tiny generated test files (never the Q-suite)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_a():\n    assert True\n")
    (tmp_path / "tests" / "test_bad.py").write_text(
        "def test_b():\n    assert True\n\ndef test_c():\n    assert False\n"
    )
    files = ("tests/test_ok.py", "tests/test_bad.py")
    junit = tmp_path / "out" / "j.xml"
    junit.parent.mkdir()
    code = quality.run_qualification_suite(files, junit, root=tmp_path)
    assert code == 1
    got = quality.parse_junit(junit, files)
    rows = {r["file"]: r for r in got["files"]}
    assert rows["tests/test_ok.py"]["passed"] == 1 and rows["tests/test_ok.py"]["failed"] == 0
    assert rows["tests/test_bad.py"]["passed"] == 1 and rows["tests/test_bad.py"]["failed"] == 1
    assert got["failed"] == 1 and got["errors"] == 0
    assert got["junit_digest"] == "sha256:" + hashlib.sha256(junit.read_bytes()).hexdigest()


# --- IC-25: Q-02 over zero or many stored reports; vacuous is flagged and shown ---------------


def test_q02_over_zero_reports_is_allowed_flagged_vacuous_and_shown_by_status(w):
    service, change_id = proposed(w)
    service.qualify_change(operator(w), change_id)
    change = service.change(change_id)
    assert change["state"] == "qualified"
    assert change["comparison"]["reports"] == 0
    assert change["qualification_scope"]["vacuous"] is True
    status = service.status()
    (row,) = status["changes"]
    assert row["qualification_scope"]["vacuous"] is True
    (version,) = status["versions"]
    assert version["requalification_vacuous"] is True  # the eval-2 record holds no report
    assert service.requalify(operator(w), "eval-5")["vacuous"] is True


def test_q02_over_stored_reports_is_not_vacuous(w):
    s2_store(w)
    service = quality_service(w)
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    service.qualify_change(operator(w), change_id)
    change = service.change(change_id)
    assert change["state"] == "qualified"
    assert change["comparison"]["reports"] == 9 and change["comparison"]["equal"] == 9
    assert change["qualification_scope"]["vacuous"] is False
    assert service.requalify(operator(w), "eval-2")["vacuous"] is False


def test_the_status_command_shows_the_vacuous_flag_and_the_provisional_table(w, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from amplai_foundry.runtime.cli import app
    from amplai_foundry.runtime.meta_commands import evaluator as command

    service, change_id = proposed(w)
    service.qualify_change(operator(w), change_id)

    @contextmanager
    def opened(_config):
        yield SimpleNamespace(store=w.m.d.store, scope=w.scope)

    monkeypatch.setattr(command, "opened_deployment", opened)
    result = CliRunner().invoke(app, ["meta", "evaluator", "status"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["changes"][0]["qualification_scope"]["vacuous"] is True
    assert out["versions"][0]["requalification_vacuous"] is True
    assert out["task_class_to_domain"]["status"] == quality.TASK_CLASS_TO_DOMAIN_PROVISIONAL
    assert out["qualification_suite_files"] == list(quality.QUALIFICATION_FILES)


# --- IC-26: evaluator.approve, not corpus.manage ----------------------------------------------


def test_evaluator_approve_without_corpus_manage_runs_the_whole_lifecycle(w):
    version_ref(w)
    service = quality_service(w)
    actor = approve_only(w)
    assert "corpus.manage" not in actor.permissions
    change_id = service.propose_change(actor, to=target(), reason="r")
    service.qualify_change(actor, change_id)
    assert service.approve_change(actor, change_id)["id"] == "evaluator-3"
    second = service.propose_change(actor, to=target("eval-4"), reason="r")
    service.reject_change(actor, second, "no")
    assert service.requalify(actor, "eval-5")["all_equal"] is True


def test_corpus_manage_alone_no_longer_suffices_for_any_evaluator_call(w):
    version_ref(w)
    service = quality_service(w)
    actor = manage_only(w)
    assert "corpus.manage" in actor.permissions and "evaluator.approve" not in actor.permissions
    err = fault("FORBIDDEN", service.propose_change, actor, to=target(), reason="r")
    assert "evaluator.approve" in err.message
    fault("FORBIDDEN", service.requalify, actor, "eval-2")
    change_id = service.propose_change(operator(w), to=target(), reason="r")
    fault("FORBIDDEN", service.qualify_change, actor, change_id)
    fault("FORBIDDEN", service.reject_change, actor, change_id, "no")
    service.qualify_change(operator(w), change_id)
    fault("FORBIDDEN", service.approve_change, actor, change_id)
    assert service.change(change_id)["state"] == "qualified"


def test_the_permission_is_evaluator_approve_and_the_operator_set_grants_it_to_no_proposer():
    from amplai_foundry.runtime.execution import meta_local

    assert quality.CHANGE_PERMISSION == "evaluator.approve"
    assert "evaluator.approve" in meta_local.META_OPERATOR_PERMISSIONS
    assert "harness.propose" not in meta_local.META_OPERATOR_PERMISSIONS
    assert "evaluator.approve" not in meta_local.PROPOSER_PERMISSIONS


def test_the_requalify_command_over_a_runtime_root_needs_evaluator_approve(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from amplai_foundry.runtime.cli import app
    from amplai_foundry.runtime.execution import meta_local
    from amplai_foundry.runtime.storage.store import Store

    with make_world(tmp_path) as world:
        s2_store(world)
        root = world.m.d.store.root
        scope = world.scope
        before = list(world.m.d.store.list_objects(scope, "evaluator-requalification"))
    monkeypatch.setattr(
        meta_local,
        "META_OPERATOR_PERMISSIONS",
        meta_local.META_OPERATOR_PERMISSIONS - {"evaluator.approve"},
    )
    result = CliRunner().invoke(
        app, ["meta", "evaluator", "requalify", "--runtime-root", str(root)]
    )
    assert result.exit_code != 0
    assert "FORBIDDEN" in result.output and "evaluator.approve" in result.output
    with Store(root) as store:
        assert list(store.list_objects(scope, "evaluator-requalification")) == before


# --- IC-27: the domain table is labelled provisional in code and output -----------------------


def test_the_domain_coverage_output_labels_the_table_provisional(w):
    version_ref(w)
    got, _ = metric(w, "domain_coverage")
    assert got["status"] == "no_data"
    assert got["table_status"] == quality.TASK_CLASS_TO_DOMAIN_PROVISIONAL
    assert "IC-27" in quality.TASK_CLASS_TO_DOMAIN_PROVISIONAL
    real_goal(w, "b0", "verified", finished=FAR_FUTURE, task_class="bug_fix")
    got, _ = metric(w, "domain_coverage")
    assert got["status"] == "ok"
    assert got["table_status"] == quality.TASK_CLASS_TO_DOMAIN_PROVISIONAL
    assert got["table"] == quality.TASK_CLASS_TO_DOMAIN


def test_the_status_output_labels_the_domain_table_provisional(w):
    got = quality_service(w).status()["task_class_to_domain"]
    assert got == {
        "table": quality.TASK_CLASS_TO_DOMAIN,
        "status": quality.TASK_CLASS_TO_DOMAIN_PROVISIONAL,
    }


def test_the_domain_table_is_marked_provisional_in_the_code():
    text = (SRC / "evaluation" / "quality.py").read_text()
    assert "PROVISIONAL (IC-27)" in text
