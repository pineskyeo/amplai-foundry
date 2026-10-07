"""Work 033 IC-30 (A), operator decision 2026-10-07: the nightly identity's mechanical screen.

interfaces.md "Open Operator Decision IC-30", "Operator Decisions Of 2026-10-07", IC-17, §8.8.

Real: the S11 world (`MetaHarness` with the leak-gate hook over the frozen corpus v2 leak index,
`LocalMetaApprovals`, `LocalMetaOps`), the nightly identity of `meta_local.nightly_actor`.

Covered: `MetaHarness.screen` admits `harness.screen` alone for a class A candidate only (every
changed component class A) and moves the machine by the same gate as the operator's screen; a
class B candidate screened with `harness.screen` alone holds CODE_REVIEW_REQUIRED, before and
after the operator's review receipt; the leak gate refuses a planted leak for the nightly screen
exactly as for the operator's (same code, same findings) and runs before the class check; an
actor with neither permission is FORBIDDEN; the nightly identity still cannot review, reject,
evaluate, approve an experiment, a canary, a promotion or a rollback, run a canary or promote;
the operator path (`harness.review`) is unchanged for both classes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_033_s11_stages import World, build_world, fault, hold, leaky

from amplai_foundry.meta_harness.service import SCREEN_PERMISSION, only_class_a
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.execution.meta_local import (
    META_OPERATOR_PERMISSIONS,
    NIGHTLY_EXCLUDED,
    NIGHTLY_PERMISSIONS,
    nightly_actor,
)


@pytest.fixture
def w(tmp_path: Path):  # type: ignore[no-untyped-def]
    with build_world(tmp_path) as world:
        yield world


def screen_as_nightly(w: World, pid: str) -> dict[str, Any]:
    return w.local.meta.screen(nightly_actor(w.scope), pid)


# ==================================================================================================
# the permission
# ==================================================================================================
def test_only_the_nightly_identity_gains_harness_screen() -> None:
    assert SCREEN_PERMISSION == "harness.screen"
    assert SCREEN_PERMISSION in NIGHTLY_PERMISSIONS
    assert SCREEN_PERMISSION not in NIGHTLY_EXCLUDED
    # the operator keeps harness.review, which screens any class; it needs no harness.screen
    assert "harness.review" in META_OPERATOR_PERMISSIONS
    assert SCREEN_PERMISSION not in META_OPERATOR_PERMISSIONS
    # IC-17 stays: the nightly identity never holds these
    for never in ("harness.review", "harness.propose", "corpus.holdout.evaluate",
                  "canary.approve", "canary.run", "release.promote", "release.rollback",
                  "experiment.reconcile", "nightly.approve"):  # fmt: skip
        assert never not in NIGHTLY_PERMISSIONS and never in NIGHTLY_EXCLUDED


def test_only_class_a_needs_every_changed_component_class_a() -> None:
    a = {"surface_class": "A"}
    b = {"surface_class": "B"}
    assert only_class_a({"surface_class": "A", "changed_components": [a, a]})
    assert not only_class_a({"surface_class": "A", "changed_components": [a, b]})
    assert not only_class_a({"surface_class": "B", "changed_components": [a]})
    assert not only_class_a({"surface_class": "C", "changed_components": []})
    assert not only_class_a({})


# ==================================================================================================
# class A: screened by the nightly identity through the same gate
# ==================================================================================================
def test_the_nightly_identity_screens_a_class_a_draft(w: World) -> None:
    pid = w.propose("a2")
    assert w.head(pid)["data"]["classification"]["surface_class"] == "A"
    out = screen_as_nightly(w, pid)
    assert out["state"] == "screened" and w.state(pid) == "screened"
    assert out["classification"]["surface_class"] == "A"
    gates = w.head(pid)["data"]["gate_results"]
    other = w.propose("a1")
    w.ops.screen(other)  # the operator's screen of another class A draft
    assert gates == w.head(other)["data"]["gate_results"]  # the same gate, the same results


def test_a_class_a_draft_screened_once_cannot_be_screened_again(w: World) -> None:
    pid = w.propose("a1")
    screen_as_nightly(w, pid)
    # the evolution machine has no screen from screened
    fault("ILLEGAL_TRANSITION", screen_as_nightly, w, pid)
    assert w.state(pid) == "screened"


# ==================================================================================================
# class B: never by harness.screen alone
# ==================================================================================================
def test_a_class_b_draft_is_not_screened_by_the_nightly_identity(w: World) -> None:
    pid = w.propose("b2")
    error = hold("CODE_REVIEW_REQUIRED", screen_as_nightly, w, pid)
    assert "IC-30" in error.message
    assert w.state(pid) == "draft"
    # the operator's review receipt does not hand the screen to the night
    w.ops.review(pid, outcome="pass", note="read the diff of both components")
    hold("CODE_REVIEW_REQUIRED", screen_as_nightly, w, pid)
    assert w.state(pid) == "draft"
    # the operator screens it (harness.review), as before
    assert w.local.meta.screen(w.operator, pid)["state"] == "screened"


# ==================================================================================================
# the leak gate and the protected-surface order are the operator's
# ==================================================================================================
def test_a_planted_leak_is_refused_by_the_nightly_screen_exactly_as_by_the_operators(
    w: World,
) -> None:
    secret = "test_bug_val_00_ok"
    pid = leaky(w, f"Make {secret} pass first.")
    nightly = hold("LEAK_GATE", screen_as_nightly, w, pid)
    operator = hold("LEAK_GATE", w.local.meta.screen, w.operator, pid)
    assert nightly.details == operator.details and nightly.details
    assert nightly.message == operator.message
    assert secret not in repr(nightly.details) and secret not in str(nightly)
    assert w.state(pid) == "draft"


def test_the_leak_gate_runs_before_the_class_check_for_the_nightly_screen(w: World) -> None:
    """A class B draft with a leak is refused by the leak gate, not merely by its class."""
    pid = w.propose("b2", suffix="b2leak", hypothesis="Fix BUG-HOL-00 properly")
    nightly = hold("LEAK_GATE", screen_as_nightly, w, pid)
    assert isinstance(nightly.details, list)
    assert "task id" in nightly.details[0]["statement"]
    assert w.state(pid) == "draft"


def test_a_protected_surface_finding_holds_the_nightly_screen_first(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The protected-surface check is unchanged and comes first for the nightly identity too."""
    pid = w.propose("a1")
    meta = w.local.meta
    real = meta.compositions.classify

    def class_c(scope: Any, baseline: Any, candidate: Any) -> dict[str, Any]:
        return {**real(scope, baseline, candidate), "surface_class": "C"}

    monkeypatch.setattr(meta.compositions, "classify", class_c)
    hold("PROTECTED_META_SURFACE", screen_as_nightly, w, pid)
    hold("PROTECTED_META_SURFACE", meta.screen, w.operator, pid)
    assert w.state(pid) == "draft"


# ==================================================================================================
# who may screen at all
# ==================================================================================================
def test_an_actor_with_neither_review_nor_screen_is_forbidden(w: World) -> None:
    pid = w.propose("a1")
    nobody = replace(nightly_actor(w.scope), permissions=frozenset({"experiment.run"}))
    error = fault("FORBIDDEN", w.local.meta.screen, nobody, pid)
    assert "harness.screen" in error.message
    assert w.state(pid) == "draft"


def test_the_proposer_cannot_screen_its_own_draft_even_with_harness_screen(w: World) -> None:
    pid = w.propose("a1")
    proposer = w.local.proposer
    fault("FORBIDDEN", w.local.meta.screen, proposer, pid)
    with_screen = replace(proposer, permissions=proposer.permissions | {SCREEN_PERMISSION})
    hold("SELF_APPROVAL", w.local.meta.screen, with_screen, pid)
    assert w.state(pid) == "draft"


# ==================================================================================================
# every other gate stays human (IC-17 unchanged)
# ==================================================================================================
def test_the_nightly_identity_still_cannot_review_reject_approve_canary_or_promote(
    w: World,
) -> None:
    nightly = nightly_actor(w.scope)
    meta, approvals = w.local.meta, w.local.approvals
    b = w.propose("b2")
    a = w.propose("a2")
    screen_as_nightly(w, a)
    ref = w.refs["corpus_ref"]
    fault("FORBIDDEN", meta.record_review, nightly, b, ref)
    fault("FORBIDDEN", meta.reject, nightly, a, "not the night's call")
    fault("FORBIDDEN", meta.evaluate, nightly, a, ref)
    fault("FORBIDDEN", meta.request_promotion, nightly, a)
    fault("FORBIDDEN", meta.approve_canary, nightly, a, ref, ref)
    fault("FORBIDDEN", meta.start_canary, nightly, a)
    fault("FORBIDDEN", meta.promote, nightly, a, {})
    fault("FORBIDDEN", meta.rollback, nightly, a, ref, ref, ref)
    for action in ("experiment.execute", "canary.execute", "release.promote", "release.rollback"):
        hold("APPROVAL_HUMAN", approvals.issue, nightly, action, digest({"a": action}))
    assert w.state(a) == "screened" and w.state(b) == "draft"
    assert w.objects("harness-review") == []


# ==================================================================================================
# the operator path is unchanged
# ==================================================================================================
def test_the_operator_screens_class_a_and_reviewed_class_b_as_before(w: World) -> None:
    assert SCREEN_PERMISSION not in w.operator.permissions
    a = w.propose("a2")
    assert w.local.meta.screen(w.operator, a)["state"] == "screened"
    b = w.propose("b2")
    hold("CODE_REVIEW_REQUIRED", w.local.meta.screen, w.operator, b)
    w.ops.review(b, outcome="pass", note="read the diff of both components")
    assert w.local.meta.screen(w.operator, b)["state"] == "screened"
