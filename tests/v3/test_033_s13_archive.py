"""Work 033 S13: the elite archive per (domain x cost band) and lineage (interfaces.md §2.13,
§3.12, §9.9, §9.10).

``EliteArchive`` places a composition into (domain x cost band) buckets when its success there
is the best with n >= 4, counting ``trial-metrics`` with ``split == "development"`` only.
Focused and holdout results reach it only as the operator-only lineage ``verdict``, which
``proposer_view`` (the proposer's only archive input) omits. Hack-guard signals reject a candidate,
they are never a score.

Real: store, rc06 product rig, ``LocalMeta`` (the S11 ``World``); the trial and ``trial-metrics``
records are written as their writers shape them. No driver, docker or network is used.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_033_s11_stages import CELL, KEY, World, build_world, fault

from amplai_foundry.meta_harness.archive import KIND, MIN_N, EliteArchive, validate_archive
from amplai_foundry.runtime.contracts.identity import new_id
from amplai_foundry.runtime.execution import releases

Ref = dict[str, Any]


@pytest.fixture
def w(tmp_path):  # type: ignore[no-untyped-def]
    with build_world(tmp_path) as world:
        yield world


# -- helpers ---------------------------------------------------------------------------------------
def archive(w: World) -> EliteArchive:
    return EliteArchive(w.store, w.scope)


def champion(w: World) -> Ref:
    return dict(w.ops.app.compositions[CELL])


def candidate(w: World, name: str) -> tuple[str, Ref]:
    """A new composition derived from the champion: (proposal id, candidate ref)."""
    pid = w.propose("a1", suffix=name)
    return pid, dict(w.proposal(pid)["candidate_ref"])


def rows(
    w: World,
    composition: Ref,
    domain: str,
    solved: int,
    n: int,
    *,
    tokens: tuple[int | None, int | None] | None = (2, 2),
    split: str = "development",
    cell: str = CELL,
    guards: dict[str, Any] | None = None,
) -> list[Ref]:
    """Store ``n`` trials of the composition in a domain, ``solved`` of them successful, each with
    its ``trial-metrics`` record; the metrics refs."""
    out = []
    for i in range(n):
        trial = w.put(
            "eval-trial",
            new_id("trial"),
            {"composition_ref": composition, "task_id": f"{domain}-{split[:3]}-{i}",
             "task_class": domain, "success": i < solved},
        )  # fmt: skip
        value = {
            "schema": "amplai.trial-metrics.v1",
            "scope": w.scope.wire(),
            "trial_ref": trial,
            "split": split,
            "cell_id": cell,
            "domain": domain,
            "success": i < solved,
            "tokens": {"input": tokens[0], "output": tokens[1]} if tokens else {},
            "guards": guards or {},
        }
        out.append(w.put("trial-metrics", new_id("tm"), value))
    return out


def by_bucket(data: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(e["bucket"]["domain"], e["bucket"]["cost_band"]): e for e in data["elites"]}


def update(
    w: World, composition: Ref, pid: str, *, verdict: str | None = None,
    stage_metrics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:  # fmt: skip
    return archive(w).update(
        CELL, composition_ref=composition, stage_metrics=stage_metrics or [],
        proposal_id=pid, verdict=verdict,
    )  # fmt: skip


def promote_directly(w: World, composition: Ref) -> Ref:
    """Make a composition the cell's champion through a signed release (what promotion writes)."""
    release = releases.build(
        w.store, w.scope, w.d.contracts, "local-test-" + composition["id"], [composition],
        signer=KEY, key_id="local-authority", basis="S13 archive test",
    )  # fmt: skip
    with w.store.tx() as db:
        head = w.store.head(w.scope, releases.POINTER_KIND, releases.POINTER_ID, db=db)
        w.store.cas(
            db, w.scope, releases.POINTER_KIND, releases.POINTER_ID, head["row_version"],
            "active", {"release_ref": release},
        )  # fmt: skip
    return dict(release)


# ==================================================================================================
# the head, the champion (§2.13)
# ==================================================================================================
def test_an_unwritten_archive_is_empty(w: World) -> None:
    a = archive(w)
    assert a.head(CELL) is None and a.head_ref(CELL) is None
    assert a.parents(CELL) == []
    assert a.proposer_view(CELL) == {"cell_id": CELL, "champion": None, "elites": [], "lineage": []}


def test_the_champion_is_the_cells_composition_of_the_active_release(w: World) -> None:
    a = archive(w)
    _cand_pid, cand = candidate(w, "champ")
    release = promote_directly(w, cand)
    effective = releases.effective(w.store, w.scope, w.ops.app.compositions)
    assert effective[CELL] == cand  # the release replaces the installed composition (class A)
    data = a.set_champion(CELL, composition_ref=effective[CELL], release_ref=release)
    assert data["cell_id"] == CELL and data["elites"] == [] and data["lineage"] == []
    assert (
        data["champion"]["composition_ref"] == cand and data["champion"]["release_ref"] == release
    )
    assert data["champion"]["since"]
    validate_archive(data)
    head = w.store.head(w.scope, KIND, "archive-" + CELL)
    assert head["data"] == data  # one head per cell, id archive-<cell_id>
    assert a.head_ref(CELL)["id"] == "archive-" + CELL


def test_the_champion_since_changes_only_when_the_champion_does(w: World) -> None:
    a = archive(w)
    base = champion(w)
    first = a.set_champion(CELL, composition_ref=base, release_ref=None)
    again = a.set_champion(CELL, composition_ref=base, release_ref=None)
    assert again["champion"] == first["champion"]
    _pid, cand = candidate(w, "next")
    other = a.set_champion(CELL, composition_ref=cand, release_ref=None)
    assert other["champion"]["composition_ref"] == cand
    assert other["champion"]["since"] >= first["champion"]["since"]
    same_composition_new_release = a.set_champion(
        CELL, composition_ref=cand, release_ref=promote_directly(w, cand)
    )
    assert same_composition_new_release["champion"]["since"] == other["champion"]["since"]


@pytest.mark.parametrize("bad", [None, "ref", {"id": "x"}, {"id": 1, "revision": 1, "digest": "d"}])
def test_the_champion_and_its_release_must_be_refs(w: World, bad: Any) -> None:
    fault("ELITE_ARCHIVE", archive(w).set_champion, CELL, composition_ref=bad, release_ref=None)
    fault(
        "ELITE_ARCHIVE",
        archive(w).set_champion,
        CELL,
        composition_ref=champion(w),
        release_ref=bad if bad is not None else "not a ref",
    )


# ==================================================================================================
# placement from development rows only (§9.9)
# ==================================================================================================
def two_compositions(w: World) -> tuple[Ref, Ref, str, str]:
    """A (low cost) and B (higher cost) with measured development rows in two domains."""
    pid_a, a = candidate(w, "elite-a")
    pid_b, b = candidate(w, "elite-b")
    rows(w, a, "bug", 2, 4, tokens=(2, 2))  # success .5, tokens/solved 8
    rows(w, b, "bug", 4, 4, tokens=(10, 10))  # success 1.0, tokens/solved 20
    rows(w, b, "feature", 3, 4, tokens=(10, 10))
    return a, b, pid_a, pid_b


def test_a_composition_is_placed_per_domain_and_cost_band(w: World) -> None:
    a, b, pid_a, pid_b = two_compositions(w)
    update(w, a, pid_a)
    data = update(w, b, pid_b)
    validate_archive(data)
    placed = by_bucket(data)
    # cost bands are terciles of tokens per solved task among the measured compositions
    assert set(placed) == {("bug", "low"), ("bug", "mid"), ("feature", "mid")}
    low, mid = placed[("bug", "low")], placed[("bug", "mid")]
    assert low["composition_ref"] == a and (low["success"], low["n"]) == (0.5, 4)
    assert low["tokens_per_solved"] == 8.0
    assert mid["composition_ref"] == b and (mid["success"], mid["n"]) == (1.0, 4)
    assert mid["tokens_per_solved"] == 20.0
    feature = placed[("feature", "mid")]
    assert (feature["success"], feature["n"], feature["tokens_per_solved"]) == (0.75, 4, 26.7)
    assert all(e["added_at"] for e in data["elites"])
    assert all(e["n"] >= MIN_N == 4 for e in data["elites"])


def test_an_elite_cites_its_development_metric_records_as_evidence(w: World) -> None:
    a, _b, pid_a, _pid_b = two_compositions(w)
    metrics = [r for r, _ in w.objects("trial-metrics")]
    data = update(w, a, pid_a)
    (elite,) = [e for e in data["elites"] if e["composition_ref"] == a]  # placement is cell-wide
    assert len(elite["evidence"]) == 4
    assert all(r in metrics for r in elite["evidence"])
    for ref in elite["evidence"]:
        assert w.store.get(w.scope, "trial-metrics", ref)["split"] == "development"


def test_a_bucket_needs_n_of_at_least_four(w: World) -> None:
    pid, comp = candidate(w, "few")
    rows(w, comp, "bug", 3, 3)
    assert update(w, comp, pid)["elites"] == []
    rows(w, comp, "bug", 1, 1)  # a fourth row
    assert [e["n"] for e in update(w, comp, pid)["elites"]] == [4]


def test_validation_and_holdout_rows_place_nothing(w: World) -> None:
    pid, comp = candidate(w, "outside")
    for split in ("validation", "holdout"):
        rows(w, comp, "bug", 4, 4, split=split)
    assert update(w, comp, pid)["elites"] == []
    # passed in as the stage's rows they change nothing either
    extra = [v for _r, v in w.objects("trial-metrics")]
    assert update(w, comp, pid, stage_metrics=extra)["elites"] == []
    assert archive(w).measurements(CELL) == {}


def test_validation_rows_do_not_change_an_existing_elite_or_its_numbers(w: World) -> None:
    a, b, pid_a, pid_b = two_compositions(w)
    update(w, a, pid_a)
    before = update(w, b, pid_b)
    rows(w, b, "bug", 0, 8, tokens=(777, 777), split="validation")  # would be worst and costly
    rows(w, a, "feature", 4, 4, tokens=(1, 1), split="holdout")
    after = update(w, b, pid_b, verdict="focused:passed")
    assert after["elites"] == before["elites"]
    assert all(e["n"] == 4 and e["tokens_per_solved"] < 1000 for e in after["elites"])


def test_rows_of_another_cell_are_ignored(w: World) -> None:
    pid, comp = candidate(w, "elsewhere")
    rows(w, comp, "bug", 4, 4, cell="claude-cli")
    assert update(w, comp, pid)["elites"] == []


def test_rows_without_a_result_a_domain_or_a_trial_are_left_out(w: World) -> None:
    pid, comp = candidate(w, "gaps")
    stored = rows(w, comp, "bug", 4, 4)
    for ref in stored[:2]:  # an unknown result and a missing domain
        value = dict(w.store.get(w.scope, "trial-metrics", ref))
        value["success" if ref == stored[0] else "domain"] = None
        w.put("trial-metrics", ref["id"], value, revision=ref["revision"] + 1)
    w.put(
        "trial-metrics", new_id("tm"),
        {"trial_ref": {"id": "no-such-trial", "revision": 1, "digest": "sha256:" + "0" * 64},
         "split": "development", "cell_id": CELL, "domain": "bug", "success": True,
         "tokens": {"input": 1, "output": 1}},
    )  # fmt: skip
    assert update(w, comp, pid)["elites"] == []  # two usable rows are left, n < 4


def test_the_best_success_in_a_bucket_wins_and_a_tie_keeps_the_incumbent(w: World) -> None:
    pid_c, c = candidate(w, "tie-c")
    pid_d, d = candidate(w, "tie-d")
    # unknown token usage: both land in the same (high) cost band
    rows(w, c, "bug", 3, 4, tokens=(None, None))
    rows(w, d, "bug", 3, 4, tokens=(None, None))
    update(w, c, pid_c)
    first = by_bucket(update(w, d, pid_d))[("bug", "high")]
    assert first["composition_ref"] == c and first["success"] == 0.75  # tie: the incumbent stays
    rows(w, d, "bug", 4, 4, tokens=(None, None))  # d now has 7/8
    placed = by_bucket(update(w, d, pid_d))[("bug", "high")]
    assert placed["composition_ref"] == d and placed["n"] == 8
    assert placed["tokens_per_solved"] is None  # unknown usage is not turned into a number


def test_a_composition_with_no_solved_task_or_unknown_usage_is_in_the_high_band(w: World) -> None:
    pid, comp = candidate(w, "unsolved")
    rows(w, comp, "bug", 0, 4, tokens=(5, 5))
    (elite,) = update(w, comp, pid)["elites"]
    assert elite["bucket"] == {"domain": "bug", "cost_band": "high"}
    assert elite["success"] == 0.0 and elite["tokens_per_solved"] is None


def test_update_is_idempotent_and_order_independent(w: World) -> None:
    a, b, pid_a, pid_b = two_compositions(w)
    update(w, a, pid_a)
    forward = update(w, b, pid_b)
    again = update(w, b, pid_b)
    assert again["elites"] == forward["elites"] and len(again["lineage"]) == 2
    other = EliteArchive(w.store, w.scope)
    assert {k: (e["composition_ref"], e["success"]) for k, e in by_bucket(forward).items()} == {
        k: (e["composition_ref"], e["success"])
        for k, e in by_bucket(other.head(CELL) or {}).items()
    }


def test_stage_rows_not_stored_yet_count_but_only_development_ones(w: World) -> None:
    pid, comp = candidate(w, "extra")
    stored = rows(w, comp, "bug", 4, 4)
    values = [w.store.get(w.scope, "trial-metrics", r) for r in stored]
    # rows only passed in (the store of a new archive does not have them): remove nothing, add
    extras = [{**v, "split": "validation"} for v in values]
    assert update(w, comp, pid, stage_metrics=extras)["elites"][0]["n"] == 4  # the stored four


# ==================================================================================================
# hack guards reject only, never a score (§9.10)
# ==================================================================================================
def guard_finding(w: World, pid: str, finding: str = "HACK_GUARD: ask-back rate shifted") -> None:
    with w.store.tx() as db:
        w.store.cas(
            db, w.scope, "stage-run", "stagerun-" + pid, 0, "running",
            {"stages": {"screening": {"guard_findings": [finding], "state": "failed"}}},
        )  # fmt: skip


def test_a_candidate_with_a_hack_guard_finding_is_never_placed(w: World) -> None:
    pid_ok, ok = candidate(w, "guard-ok")
    pid_bad, bad = candidate(w, "guard-bad")
    rows(w, ok, "bug", 3, 4, tokens=(10, 10))
    rows(w, bad, "bug", 4, 4, tokens=(10, 10))  # better than ok: only the guard excludes it
    guard_finding(w, pid_bad)
    update(w, ok, pid_ok)
    data = update(w, bad, pid_bad)
    assert [e["composition_ref"] for e in data["elites"]] == [ok]
    assert any(entry["child"] == bad for entry in data["lineage"])  # still part of the lineage


def test_a_finding_that_is_not_a_hack_guard_does_not_reject(w: World) -> None:
    pid, comp = candidate(w, "other-finding")
    rows(w, comp, "bug", 4, 4)
    guard_finding(w, pid, "LEAK_GATE: not a guard")
    assert [e["composition_ref"] for e in update(w, comp, pid)["elites"]] == [comp]


def test_guard_signals_never_enter_a_ranking(w: World) -> None:
    pid_a, a = candidate(w, "sig-a")
    pid_b, b = candidate(w, "sig-b")
    noisy = {"ask_back": True, "edit_files": 99, "edit_lines": 9999, "test_file_edits": 5,
             "verified_hidden_fail": True, "broken_tool_calls": None}  # fmt: skip
    rows(w, a, "bug", 3, 4, tokens=(None, None), guards=noisy)
    rows(w, b, "bug", 3, 4, tokens=(None, None), guards={})
    update(w, a, pid_a)
    placed = by_bucket(update(w, b, pid_b))[("bug", "high")]
    assert placed["composition_ref"] == a and placed["success"] == 0.75  # signals: no effect
    assert "guards" not in json.dumps(placed)


# ==================================================================================================
# lineage (§2.13, §9.9) and the proposer's view
# ==================================================================================================
def test_lineage_records_the_proposal_its_baseline_parent_and_the_operator_verdict(
    w: World,
) -> None:
    pid, comp = candidate(w, "lin")
    data = update(w, comp, pid, verdict="focused:non_inferior")
    (entry,) = data["lineage"]
    assert entry["child"] == comp and entry["proposal_id"] == pid
    assert entry["parents"] == [champion(w)]  # the baseline of the proposal
    assert entry["verdict"] == "focused:non_inferior"
    # a later update without a verdict keeps it; a new verdict replaces it
    assert update(w, comp, pid)["lineage"][0]["verdict"] == "focused:non_inferior"
    assert update(w, comp, pid, verdict="holdout:non_inferior")["lineage"][0]["verdict"] == (
        "holdout:non_inferior"
    )
    assert len(update(w, comp, pid)["lineage"]) == 1


def test_lineage_follows_component_parents_to_archived_compositions(w: World) -> None:
    components = w.ops.components
    proposer_actor = w.local.proposer
    v1 = w.component("env_bootstrap", "lineage", enabled=True, tree_depth=3)
    v2 = components.register(
        proposer_actor, component_id="env_bootstrap.lineage", kind="env_bootstrap",
        content={"enabled": True, "facts": ["repo_tree"], "tree_depth": 1,
                 "tree_max_entries": 50, "max_chars": 500},
        source="proposer", rationale="a refinement of v1", parent=v1,
    )  # fmt: skip
    assert v2["revision"] == v1["revision"] + 1
    pid1 = w.ops.propose_components(
        cell_id=CELL, changes={"env_bootstrap": v1}, suffix="lin-v1", hypothesis="h1",
        expected_benefit="b", risks=["r"], observation_refs=[w.ops._observation("x")],
        prediction=None,
    )  # fmt: skip
    pid2 = w.ops.propose_components(
        cell_id=CELL, changes={"env_bootstrap": v2}, suffix="lin-v2", hypothesis="h2",
        expected_benefit="b", risks=["r"], observation_refs=[w.ops._observation("y")],
        prediction=None,
    )  # fmt: skip
    first, second = (dict(w.proposal(p)["candidate_ref"]) for p in (pid1, pid2))
    update(w, first, pid1)
    data = update(w, second, pid2)
    entry = next(e for e in data["lineage"] if e["proposal_id"] == pid2)
    assert champion(w) in entry["parents"]  # the proposal's baseline
    assert first in entry["parents"]  # the archived composition that holds the parent component
    chain = archive(w).lineage(second)
    assert [e["proposal_id"] for e in chain] == [pid2, pid1]  # ancestry, breadth first


def test_the_proposer_view_has_no_verdict_and_no_validation_or_holdout_numbers(w: World) -> None:
    a, b, pid_a, pid_b = two_compositions(w)
    # the operator-only outcome of later stages: a verdict and validation/holdout rows
    rows(w, b, "bug", 8, 8, tokens=(4242, 4242), split="validation")
    rows(w, b, "bug", 8, 8, tokens=(4242, 4242), split="holdout")
    update(w, a, pid_a, verdict="focused:regression")
    data = update(w, b, pid_b, verdict="holdout:non_inferior")
    archive(w).set_champion(CELL, composition_ref=champion(w), release_ref=None)
    view = archive(w).proposer_view(CELL)
    assert set(view) == {"cell_id", "champion", "elites", "lineage"}
    assert all(set(entry) == {"child", "parents", "proposal_id"} for entry in view["lineage"])
    flat = json.dumps(view)
    for operator_only in ("verdict", "focused", "holdout", "regression", "non_inferior"):
        assert operator_only not in flat
    assert all(e["n"] == 4 and e["tokens_per_solved"] < 1000 for e in view["elites"])
    assert view["elites"] == data["elites"]  # development-derived, unchanged by the later rows
    # the operator's query keeps the verdicts
    verdicts = {e["verdict"] for e in archive(w).lineage(b)}
    assert "holdout:non_inferior" in verdicts


def test_the_proposer_view_is_a_copy(w: World) -> None:
    a, _b, pid_a, _pid_b = two_compositions(w)
    update(w, a, pid_a)
    view = archive(w).proposer_view(CELL)
    view["elites"].clear()
    assert archive(w).proposer_view(CELL)["elites"]


# ==================================================================================================
# parents for the proposer (§9.9)
# ==================================================================================================
def test_parents_are_the_champion_then_elites_of_the_champions_weakest_buckets(w: World) -> None:
    a, b, _pid_a, pid_b = two_compositions(w)
    a_ref = a
    archive(w).set_champion(CELL, composition_ref=a_ref, release_ref=None)
    update(w, b, pid_b)
    parents = archive(w).parents(CELL, k=3)
    assert parents[0] == a_ref  # the champion first
    assert parents[1:] == [b]  # B holds the elites of the buckets where A is weakest
    assert len(parents) == len({p["id"] for p in parents})  # no composition twice
    assert archive(w).parents(CELL, k=1) == [a_ref]
    assert archive(w).parents(CELL, k=2) == [a_ref, b]


def test_parents_never_use_validation_or_holdout_numbers(w: World) -> None:
    a, b, _pid_a, pid_b = two_compositions(w)
    archive(w).set_champion(CELL, composition_ref=a, release_ref=None)
    update(w, b, pid_b)
    before = archive(w).parents(CELL, k=3)
    rows(w, a, "feature", 4, 4, split="validation")  # would make the champion strong on feature
    rows(w, a, "bug", 0, 8, split="holdout")
    assert archive(w).parents(CELL, k=3) == before


@pytest.mark.parametrize("k", [0, -1, True, "3", 2.5, None])
def test_parents_takes_a_positive_integer(w: World, k: Any) -> None:
    fault("ELITE_ARCHIVE", archive(w).parents, CELL, k=k)


# ==================================================================================================
# refusals
# ==================================================================================================
@pytest.mark.parametrize(
    "kwargs",
    [
        {"composition_ref": "ref"},
        {"composition_ref": {"id": "x", "revision": "1", "digest": "d"}},
        {"proposal_id": ""},
        {"proposal_id": 5},
        {"verdict": 5},
    ],
)
def test_update_refuses_malformed_arguments(w: World, kwargs: dict[str, Any]) -> None:
    args: dict[str, Any] = {
        "composition_ref": champion(w),
        "stage_metrics": [],
        "proposal_id": "p",
        "verdict": None,
    }
    args.update(kwargs)
    fault("ELITE_ARCHIVE", archive(w).update, CELL, **args)


def test_lineage_of_a_non_ref_is_refused(w: World) -> None:
    fault("ELITE_ARCHIVE", archive(w).lineage, "not a ref")
    assert archive(w).lineage(champion(w)) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["elites"].append({"bucket": {"domain": "bug", "cost_band": "low"}}),
        lambda d: d.update(extra=1),
        lambda d: d["elites"][0].update(n=3),  # an elite needs n >= 4
        lambda d: d["elites"][0]["bucket"].update(cost_band="cheap"),
        lambda d: d["lineage"][0].update(verdict=5),
        lambda d: d["lineage"][0].update(parents=["not a ref"]),
        lambda d: d.update(champion={"composition_ref": "x"}),
    ],
)
def test_the_stored_head_shape_is_validated(w: World, mutate: Any) -> None:
    a, _b, pid_a, _pid_b = two_compositions(w)
    archive(w).set_champion(CELL, composition_ref=a, release_ref=None)
    data = update(w, a, pid_a, verdict="x")
    validate_archive(data)
    broken = json.loads(json.dumps(data))
    mutate(broken)
    fault("ELITE_ARCHIVE", validate_archive, broken)
