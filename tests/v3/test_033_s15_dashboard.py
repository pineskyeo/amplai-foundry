"""Work 033 S15: the operator dashboard (interfaces.md §2.16, §3.13, §8.10, §11.2, §12.1; AC-09).

`meta_harness/dashboard.py` builds a `DashboardFeed` read-only from stored records and renders the
static pages of §11.2; `amplai meta dashboard --out DIR` (`runtime/meta_commands/dashboard.py`)
builds them on demand. Two kinds of fixtures:

- a hand-built `DashboardFeed` (every page filled, hostile strings in record string fields) that
  is rendered with `render_pages`, so each page and its escaping is checked on its own;
- a real `Store` seeded with records shaped as their writers shape them, read back by
  `build_feed` / `build`, so the read-only build, the no-data rule and the withholding of
  validation/holdout task ids, trace text, prompts, hidden tests and leak-index tokens are checked
  end to end.

Every page must pass `inspect_page` (`verification/runtime/render_acceptance.py:86-132`) with
outcome `pass`. No driver, docker or network is used.
"""

from __future__ import annotations

import json
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from amplai_foundry.meta_harness import dashboard as dash
from amplai_foundry.meta_harness.dashboard import (
    AblationRow,
    ApprovalRow,
    BestComposition,
    BudgetView,
    CellPage,
    ComponentState,
    CompositionStat,
    CorpusView,
    CostPerSolved,
    DashboardFeed,
    DomainRow,
    EvaluationView,
    EvaluatorVersionRow,
    ExperimentView,
    HeadroomRow,
    JudgeCallRow,
    JudgeQualificationRow,
    LayerView,
    LineageRow,
    ManifestView,
    MatrixCell,
    NightRow,
    QualityView,
    QuotaRow,
    ReleaseView,
    StageRow,
    StandingApproval,
    StrategyRow,
    TableView,
    TaskResult,
)
from amplai_foundry.runtime.cli import app
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.meta_commands import dashboard as command
from amplai_foundry.runtime.storage.store import Scope, Store
from amplai_foundry.verification.runtime.render_acceptance import inspect_page

SCOPE = Scope("tenant-s15", "project-s15")
NOW = "2026-10-02T00:00:00Z"
HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)>&"\''
HOSTILE_SCRIPT = "<script>alert"  # the raw start of the hostile string
ESCAPED_SCRIPT = "&lt;script&gt;alert"

FIXED_PAGES = (
    "index.html",
    "lineage.html",
    "approvals.html",
    "corpus.html",
    "experiments.html",
    "layers.html",
    "strategies.html",
    "judges.html",
    "budget.html",
    "evaluation.html",
)


# --- helpers ---------------------------------------------------------------------------------


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.srcs: list[str] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        for name, value in attrs:
            if name == "href" and value is not None:
                self.hrefs.append(value)
            if name in ("src", "srcset", "data", "action", "poster") and value is not None:
                self.srcs.append(value)


def links(text: str) -> _Links:
    parser = _Links()
    parser.feed(text)
    return parser


def write_pages(pages: dict[str, str], out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, text in pages.items():
        paths[name] = out / name
        paths[name].write_text(text, encoding="utf-8")
    return paths


def assert_all_pass(pages: dict[str, str], tmp_path: Path) -> None:
    for name, path in write_pages(pages, tmp_path / "inspect").items():
        report = inspect_page(path)
        assert report["outcome"] == "pass", (name, report["findings"])
        assert report["findings"] == []


def assert_static(pages: dict[str, str]) -> None:
    """No script, no external src/href, relative links only, links resolve inside the site."""
    for name, text in pages.items():
        parsed = links(text)
        assert "script" not in parsed.tags, name
        assert "iframe" not in parsed.tags and "link" not in parsed.tags, name
        assert parsed.srcs == [], (name, parsed.srcs)
        for href in parsed.hrefs:
            assert not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href), (name, href)  # no scheme
            assert not href.startswith(("/", "//")), (name, href)
            target = href.split("#", 1)[0]
            assert target == "" or target in pages, (name, href)
        assert "http://" not in text and "https://" not in text, name
        assert "javascript:" not in text.lower(), name


def text_of(pages: dict[str, str]) -> str:
    return "\n".join(pages.values())


def ref(object_id: str, revision: int = 1) -> dict[str, Any]:
    return {"id": object_id, "revision": revision, "digest": "sha256:" + "0" * 64}


# --- a hand-built feed -----------------------------------------------------------------------


def rich_feed(*, hostile: str = "") -> DashboardFeed:
    """Every field of every view filled; `hostile` is appended to each record string field."""
    h = hostile
    cost = CostPerSolved(1_500_000.0, "USD", "estimated")
    best = BestComposition("comp-best" + h, 0.8, 5, 900.0, 0.4, 5, "comp-base", False)
    cell_a = MatrixCell(
        cell_id="codex-cli.gpt-x.high",
        driver_id="codex-cli",
        model="gpt-x" + h,
        effort="high",
        page="cell-codex-cli-gpt-x-high.html",
        tasks=5,
        pass_rate=0.8,
        interval=(0.5, 0.95),
        pass_k=3,
        pass_k_rate=0.6,
        informative=4,
        median_seconds=12.5,
        tokens_per_solved=1234.0,
        api_cost=cost,
        best=best,
        plan_id="plan-1",
        summarized_at="2026-10-01T00:00:00Z",
    )
    cell_b = MatrixCell(
        cell_id="codex-cli.gpt-x.low",
        driver_id="codex-cli",
        model="gpt-x" + h,
        effort="low",
        page="cell-codex-cli-gpt-x-low.html",
        tasks=0,
        pass_rate=None,
        interval=None,
        pass_k=None,
        pass_k_rate=None,
        informative=None,
        median_seconds=None,
        tokens_per_solved=None,
        api_cost=CostPerSolved(None, None, "no_data"),
        best=None,
        plan_id=None,
        summarized_at=None,
    )
    manifest = ManifestView(
        "comp-best" + h,
        (
            ComponentState("execution_strategy", "strategy@3", 3, "on", "single, repair_loop"),
            ComponentState("fast_checks", "-", None, "off", ""),
            ComponentState("retrieval", "retrieval@2" + h, 2, "on", h),
        ),
        None,
    )
    comps = (
        CompositionStat("comp-base", "comp-base@1", 5, 5, 2, 0.4, 2000.0, True, False, True, None),
        CompositionStat(
            "comp-best" + h, "comp-best@1", 5, 5, 4, 0.8, 900.0, False, True, True, manifest
        ),
        CompositionStat("comp-dom", "comp-dom@1", 5, 5, 1, 0.2, 5000.0, False, False, False, None),
    )
    page_a = CellPage(
        cell_id=cell_a.cell_id,
        page=cell_a.page,
        driver_id="codex-cli",
        model="gpt-x" + h,
        effort="high",
        champion="comp-best",
        compositions=comps,
        ablations=(
            AblationRow("prop-1", "prop-1-abl-1", "retrieval" + h, "concluded", "A", 0.2, 5),
        ),
        stages=(
            StageRow("prop-1", "screening", "concluded", "A", "exp-1", "experiment-exp-1.html", ()),
            StageRow("prop-1", "holdout", "waiting_approval", None, None, None, ("finding" + h,)),
        ),
        archive_lineage=(
            {"child": "comp-best", "parents": ["comp-base"], "proposal_id": "prop-1"},
        ),
    )
    page_b = CellPage(
        cell_a.cell_id.replace("high", "low"),
        cell_b.page,
        "codex-cli",
        "gpt-x",
        "low",
        None,
        (),
        (),
        (),
        (),
    )
    experiment = ExperimentView(
        experiment_id="exp-1",
        page="experiment-exp-1.html",
        proposal_id="prop-1",
        state="concluded",
        verdict="pass",
        decision_class="A",
        endpoint="pass_rate" + h,
        purpose="screening",
        delta=0.4,
        interval=(0.1, 0.7),
        task_count=5,
        safety_failures=0,
        reasons=("reason" + h,),
        stage="screening",
        splits=("development",),
        tasks=(
            TaskResult("dev-1", "development", (1, 3), (3, 3), None, False),
            TaskResult("dev-2", "development", (3, 3), (1, 3), None, True),
            TaskResult("holdout task #1", "holdout", (0, 1), (1, 1), (1, 1), False),
        ),
    )
    layer = LayerView(
        layer="L5",
        decisions=12,
        coverage=0.75,
        specialised_decisions=7,
        pooled_decisions=5,
        chosen={"single": 8, "repair_loop": 4},
        tables=(
            TableView(
                "table-L5-1",
                20,
                ("single", "repair_loop"),
                2,
                1,
                1,
                (
                    {
                        "bucket": "domain=py" + h,
                        "option": "single",
                        "n": 9,
                        "successes": 6,
                        "posterior_success": 0.62,
                        "interval": [0.3, 0.9],
                        "pooled_depth": 1,
                    },
                ),
            ),
        ),
        options=({"option": "single", "n": 9, "successes": 6, "rate": 0.66, "tokens_mean": 800.0},),
        regret={"success_regret_mean": 0.01, "cost_regret_mean": 25.0, "measured": 6},
        note="layer note" + h,
    )
    return DashboardFeed(
        generated_at=NOW,
        scope={"tenant_id": "tenant-s15", "project_id": "project-s15"},
        sources={"calibration-summary": 1, "trial-metrics": 15},
        efforts=("low", "high"),
        matrix=(cell_b, cell_a),
        cells=(page_a, page_b),
        release=ReleaseView("release-9" + h, False, ("cand__stale" + h,)),
        lineage=(
            LineageRow(
                "prop-1",
                cell_a.cell_id,
                "B",
                "promoted",
                None,
                "comp-base",
                "comp-best",
                "hypothesis" + h,
                (
                    StageRow(
                        "prop-1",
                        "screening",
                        "concluded",
                        "A",
                        "exp-1",
                        "experiment-exp-1.html",
                        (),
                    ),
                ),
                "pass",
                "release-9",
            ),
        ),
        approvals=(
            ApprovalRow("stage gate", "prop-1", "A", "stage holdout", None),
            ApprovalRow(
                "reconcile pending", "prop-2", "B", "2 allocation(s) unknown (IC-18)", None
            ),
            ApprovalRow(
                "evaluator change", "chg-1" + h, "-", "approve" + h, "2026-10-01T00:00:00Z"
            ),
        ),
        standing=(
            StandingApproval(
                "appr-1",
                "active",
                "operator" + h,
                "2026-09-30T12:00:00Z",
                "2026-09-30T00:00:00Z",
                "2026-10-07T00:00:00Z",
                {"nights": 7, "budget_trials": 40},
            ),
        ),
        corpora=(
            CorpusView(
                "corpus-1",
                "v1",
                (
                    DomainRow(
                        "python" + h,
                        {"development": 8, "validation": 4},
                        12,
                        10,
                        0.55,
                        {"informative": 6},
                    ),
                ),
                {"development": 8, "validation": 4},
                ("dev-sat",),
                1,
                ("dev-flaky",),
                0,
            ),
        ),
        experiments=(experiment,),
        layers=(layer,),
        strategies=(
            StrategyRow(
                "repair_loop" + h,
                cell_a.cell_id,
                10,
                5,
                0.7,
                3,
                0.5,
                1000.0,
                20.0,
                {"agent_calls": 2.0, "turns": 3.5, "attempts_used": None},
            ),
        ),
        judge_qualifications=(
            JudgeQualificationRow(
                "judge-1" + h,
                "v1",
                "pairwise",
                40,
                0.9,
                (0.8, 0.95),
                0.08,
                0.05,
                0.92,
                "pass",
                "2026-09-30T00:00:00Z",
            ),
        ),
        judge_calls=(JudgeCallRow("judge-1", "v1", "pairwise", 12, 3000, 1200, 800.0),),
        budget=BudgetView(
            nights=(
                NightRow(
                    "2026-10-01",
                    40,
                    {"explore": 0.5},
                    ("codex-cli.gpt-x.high",),
                    False,
                    "main",
                    38,
                    "budget",
                    3,
                    1,
                    "2026-10-01T05:00:00Z",
                ),
            ),
            quota=(QuotaRow("codex-cli", "2026-10-01", "5h", 30, 1000, 500, 100, 1, 0, None),),
            headroom=(HeadroomRow("codex-cli", 100000, ">= max observed", 45),),
            budget_trials=40,
            configured_trials=40,
            keep_operator_share=0.5,
            median_tokens_per_trial=2500.0,
        ),
        evaluation=EvaluationView(
            versions=(EvaluatorVersionRow("eval-2", "2026-09-29T00:00:00Z", "all_equal"),),
            quality=(
                QualityView(
                    "eval-2",
                    "quality-1",
                    "2026-10-01T00:00:00Z",
                    None,
                    {
                        "discrimination": {"champion": 0.8, "control": 0.2},
                        "saturation": 0.1,
                        "grader_flakiness" + h: 0.05,
                    },
                ),
            ),
            changes=(
                {"change_id": "chg-1", "state": "proposed", "reason": "r" + h, "vacuous": False},
            ),
            requalifications=(
                {"id": "requal-1", "evaluator_version": "eval-2", "all_equal": True, "reports": 9},
            ),
            trace_drops={"over_budget": 2},
        ),
        notes=("Calibrated figures come from calibration summaries.",),
    )


# --- AC-09: every page of §11.2 on a hand-built feed ----------------------------------------


def test_every_page_of_the_contract_is_rendered_and_passes_inspect_page(tmp_path):
    pages = dash.render_pages(rich_feed())
    assert set(FIXED_PAGES) <= set(pages)
    assert "cell-codex-cli-gpt-x-high.html" in pages and "cell-codex-cli-gpt-x-low.html" in pages
    assert "experiment-exp-1.html" in pages
    assert set(pages) == {
        *FIXED_PAGES,
        *(c.page for c in rich_feed().cells),
        "experiment-exp-1.html",
    }
    assert_all_pass(pages, tmp_path)


def test_every_page_is_static_with_relative_links_only(tmp_path):
    pages = dash.render_pages(rich_feed())
    assert_static(pages)
    for name, text in pages.items():
        assert text.startswith("<!doctype html>"), name
        assert "<html lang=" in text and '<meta charset="utf-8">' in text, name
        assert 'name="viewport"' in text, name
        assert (
            "default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; "
            "base-uri &#39;none&#39;; "
            "form-action &#39;none&#39;"
        ) in text, name
        assert text.count("<h1>") == 1 and "<main" in text and 'class="skip"' in text, name


def test_the_index_matrix_has_driver_model_rows_and_effort_columns(tmp_path):
    text = dash.render_pages(rich_feed())["index.html"]
    # the columns follow the feed's effort order, the row is "driver / model"
    assert text.index(">low</th>") < text.index(">high</th>")
    assert "codex-cli / gpt-x" in text
    # calibrated pass rate with its interval, pass^k, informative, median time, tokens per solved
    assert "80.0% [50.0%, 95.0%]" in text
    assert "60.0% (k=3)" in text
    assert "<dt>informative</dt><dd>4</dd>" in text
    assert "12.5 s" in text
    assert "<dt>tokens / solved</dt><dd>1,234</dd>" in text
    # API-equivalent cost per solved is marked descriptive (D-094)
    assert "API-equiv. cost / solved (descriptive)" in text and "1.5000 USD" in text
    # best composition of the cell and its delta to the baseline
    assert "comp-best (80.0%, +40.0 pts)" in text
    # the cell without records says so instead of inventing numbers
    assert 'class="nodata">no data' in text
    # every cell box links to its page
    for cell in rich_feed().cells:
        assert f'href="{cell.page}"' in text
    assert "active release release-9 (promoted)" in text


def test_an_unpriced_cell_is_shown_unpriced_and_a_baseline_best_is_marked():
    feed = rich_feed()
    cell = feed.matrix[1]
    cell = MatrixCell(
        **{
            **cell.__dict__,
            "api_cost": CostPerSolved(None, None, "unpriced:no_price"),
            "best": BestComposition("comp-base", 0.4, 5, 1.0, 0.0, 5, "comp-base", True),
        }
    )
    pages = dash.render_pages(DashboardFeed(**{**feed.__dict__, "matrix": (feed.matrix[0], cell)}))
    text = pages["index.html"]
    assert "unpriced (no_price)" in text
    assert "comp-base (40.0%, baseline)" in text


def test_a_cell_page_shows_manifests_frontier_ablation_and_stage_history(tmp_path):
    pages = dash.render_pages(rich_feed())
    text = pages["cell-codex-cli-gpt-x-high.html"]
    # manifest of each component: on/off and its version
    assert "Manifest of comp-best" in text
    assert "strategy@3" in text and '<span class="ok">on</span>' in text
    assert '<span class="muted">off</span>' in text
    assert "single, repair_loop" in text
    assert "<td>3</td>" in text  # the component version
    # success-vs-tokens frontier: one inline SVG built from numbers, one point per composition
    assert text.count("<svg ") == 1 and text.count("</svg>") == 1
    assert text.count("<circle ") == 3
    assert "<polyline" in text  # the frontier line joins the two non-dominated compositions
    assert "Success versus tokens per solved task" in text
    assert "<image" not in text and "data:" not in text
    # ablation contribution and stage history
    assert "Leave-one-out ablation" in text and "+20.0 pts" in text
    assert "retrieval" in text and "prop-1-abl-1" in text
    assert "holdout" in text and "waiting_approval" in text
    assert "Archive lineage" in text
    assert "baseline" in text and "champion" in text
    # a cell page without compositions says no data and still passes
    low = pages["cell-codex-cli-gpt-x-low.html"]
    assert 'class="nodata">no data' in low and "<svg" not in low
    assert_all_pass({"c.html": low}, tmp_path)


def test_the_lineage_page_follows_proposal_to_release_and_marks_stale_candidates():
    text = dash.render_pages(rich_feed())["lineage.html"]
    assert "Proposal → candidate → experiments → verdict → release" in text
    assert "comp-base → comp-best" in text
    assert 'href="experiment-exp-1.html">screening: concluded</a> (A)' in text
    assert "release-9" in text and "promoted" in text
    assert "stale: re-derive" in text and "cand__stale" in text


def test_the_approvals_page_lists_waiting_items_reconcile_pending_and_the_standing_approval():
    text = dash.render_pages(rich_feed())["approvals.html"]
    assert "Waiting for the operator" in text
    assert "stage holdout" in text and "reconcile pending" in text
    assert "(IC-18)" in text
    # the standing approval with its status and its dates
    assert "Standing approval nightly.explore (IC-17)" in text
    assert '<span class="ok">active</span>' in text
    for stamp in ("2026-09-30T12:00:00Z", "2026-09-30T00:00:00Z", "2026-10-07T00:00:00Z"):
        assert stamp in text
    assert "budget_trials" in text and "40" in text
    # the surface class column
    assert ">surface class</th>" in text


def test_the_corpus_experiments_layers_strategies_judges_budget_evaluation_pages():
    pages = dash.render_pages(rich_feed())
    corpus = pages["corpus.html"]
    assert "Domains by measured difficulty" in corpus and "python" in corpus
    assert "development 8, validation 4" in corpus
    assert "Saturated: dev-sat (+1 withheld)" in corpus and "Flaky grading: dev-flaky" in corpus
    experiments = pages["experiments.html"]
    assert 'href="experiment-exp-1.html"' in experiments and "pass_rate" in experiments
    assert "+40.0 pts [10.0%, 70.0%]" in experiments
    one = pages["experiment-exp-1.html"]
    assert "1/3" in one and "3/3" in one
    assert one.count('class="bad">regression') == 1  # only the task that got worse is marked
    assert "holdout task #1" in one
    layers = pages["layers.html"]
    assert "L5" in layers and "75.0%" in layers and "domain=py" in layers
    assert "Measured per option" in layers
    strategies = pages["strategies.html"]
    assert "repair_loop" in strategies and "70.0%" in strategies and "50.0% (k=3)" in strategies
    judges = pages["judges.html"]
    assert "judge-1" in judges and "pairwise" in judges and "1,200" in judges
    budget = pages["budget.html"]
    assert "Nightly B: 40 trials" in budget and "2026-10-01" in budget
    assert "Observed quota windows" in budget and "&gt;= max observed" in budget
    evaluation = pages["evaluation.html"]
    assert "eval-2" in evaluation and "discrimination" in evaluation and "requal-1" in evaluation
    assert "over_budget" in evaluation


# --- hostile strings are escaped -------------------------------------------------------------


def test_a_hostile_string_in_any_record_field_is_escaped_on_every_page(tmp_path):
    feed = rich_feed(hostile=HOSTILE)
    pages = dash.render_pages(feed)
    assert_all_pass(pages, tmp_path)
    assert_static(pages)
    everything = text_of(pages)
    assert HOSTILE_SCRIPT not in everything
    assert "<img" not in everything
    assert "onerror=alert(1)>" not in everything
    assert ESCAPED_SCRIPT in everything
    # per page: wherever the hostile text was placed the page holds only the escaped form
    for name in ("index.html", "lineage.html", "approvals.html", "layers.html", "strategies.html"):
        assert HOSTILE_SCRIPT not in pages[name], name
    assert ESCAPED_SCRIPT in pages["lineage.html"]
    assert ESCAPED_SCRIPT in pages["cell-codex-cli-gpt-x-high.html"]
    assert ESCAPED_SCRIPT in pages["experiment-exp-1.html"]
    assert ESCAPED_SCRIPT in pages["evaluation.html"]
    # an attribute-breaking quote does not leave a raw quote inside an href or title attribute
    assert 'title="' not in everything.replace("<title id=", "")  # SVG titles are elements only


def test_an_attribute_injection_in_a_page_link_cannot_break_out_of_the_href():
    feed = rich_feed()
    bad = '"><script>alert(1)</script>.html'
    cell = MatrixCell(**{**feed.matrix[1].__dict__, "page": bad})
    pages = dash.render_pages(DashboardFeed(**{**feed.__dict__, "matrix": (feed.matrix[0], cell)}))
    assert "<script" not in pages["index.html"]
    assert "&quot;&gt;&lt;script&gt;" in pages["index.html"]


def test_a_hostile_feed_json_never_holds_raw_markup(tmp_path):
    feed = rich_feed(hostile=HOSTILE)
    written = dash.write_site(feed, tmp_path / "site", feed_json=True)
    wire = (tmp_path / "site" / "feed.json").read_text(encoding="utf-8")
    assert tmp_path / "site" / "feed.json" in written
    assert "<" not in wire and ">" not in wire and "&" not in wire
    assert json.loads(wire)["scope"] == {"tenant_id": "tenant-s15", "project_id": "project-s15"}
    # the hostile text survived intact as data
    assert json.loads(wire)["matrix"][1]["model"] == "gpt-x" + HOSTILE


# --- missing record kinds render as "no data" ------------------------------------------------


def _empty_store(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    with Store(root):
        pass
    return root


def seed(store: Store, kind: str, object_id: str, value: dict[str, Any], revision: int = 1):
    with store.tx() as db:
        return store.put(db, SCOPE, kind, object_id, revision, {"scope": SCOPE.wire(), **value})


def seed_head(store: Store, kind: str, object_id: str, state: str, data: dict[str, Any]) -> None:
    with store.tx() as db:
        store.cas(db, SCOPE, kind, object_id, 0, state, data)


def test_an_empty_store_renders_every_page_as_no_data_and_passes_inspect_page(tmp_path):
    root = _empty_store(tmp_path)
    feed = dash.build_feed(root, SCOPE, now_utc=NOW)
    assert feed.matrix == () and feed.cells == () and feed.experiments == ()
    assert feed.lineage == () and feed.approvals == () and feed.standing == ()
    assert feed.corpora == () and feed.strategies == ()
    assert feed.release is None
    pages = dash.render_pages(feed)
    assert set(pages) == set(FIXED_PAGES)  # no cell and no experiment page without records
    assert_all_pass(pages, tmp_path)
    assert_static(pages)
    for name in FIXED_PAGES:
        assert "no data" in pages[name], name
    assert pages["index.html"].count("no data") >= 1
    assert "no data" in pages["budget.html"] and "no data" in pages["judges.html"]
    # nothing invented: no number is shown as measured
    assert not re.search(r"\d%", pages["index.html"].split("<main")[1].split("</main>")[0])


def test_one_missing_record_kind_renders_no_data_only_on_its_own_page(tmp_path):
    root = tmp_path / "runtime"
    with Store(root) as store:
        seed(
            store,
            "harness-cell",
            "cell-1",
            {"driver_id": "codex-cli", "provider_model_id": "m1", "effort": "high"},
        )
        seed(store, "evaluator-version", "evaluator-2", {"version": "eval-2", "approved_at": NOW})
    pages = dash.render_pages(dash.build_feed(root, SCOPE, now_utc=NOW))
    assert "codex-cli / m1" in pages["index.html"]
    assert 'class="nodata">no data' in pages["index.html"]  # the cell has no calibration
    assert "no data" in pages["judges.html"] and "no data" in pages["lineage.html"]
    assert_all_pass(pages, tmp_path)


# --- a seeded store: the end-to-end build ----------------------------------------------------

TRACE_CANARY = "TRACE-CANARY-7d1f-agent-said-secret"
LEAK_CANARY = "LEAKTOKEN-9c2e-ngram"
PROMPT_CANARY = "HOLDOUT-PROMPT-CANARY-4b8a"
HIDDEN_CANARY = "HIDDEN-TEST-CANARY-61e0"
VALIDATION_ID = "val-secret-task-31"
HOLDOUT_ID = "hold-secret-task-77"
DEV_TASKS = ("dev-a", "dev-b", "dev-c", "dev-d", "dev-e")
CELL_A, CELL_B = "cell-a-high", "cell-b-low"


def seed_world(store: Store, *, hostile: str = "") -> None:
    base = seed(store, "harness-composition", "comp-base", {"composition_id": "comp-base"})
    best = seed(store, "harness-composition", "comp-best", {"composition_id": "comp-best"})
    plan = seed(
        store,
        "calibration-plan",
        "plan-1",
        {"composition_refs": {CELL_A: [base, best]}},
    )
    seed(
        store,
        "harness-cell",
        CELL_A,
        {"driver_id": "codex-cli", "provider_model_id": "gpt-x" + hostile, "effort": "high"},
    )
    seed(
        store,
        "harness-cell",
        CELL_B,
        {"driver_id": "codex-cli", "provider_model_id": "gpt-x" + hostile, "effort": "low"},
    )
    seed(
        store,
        "calibration-summary",
        "summary-1",
        {
            "plan_ref": plan,
            "summarized_at": "2026-10-01T00:00:00Z",
            "cells": {
                CELL_A: {
                    "tasks": {t: {"class": "informative"} for t in DEV_TASKS},
                    "pass_rate": 0.8,
                    "pass_rate_interval": [0.5, 0.95],
                    "pass_k": {"k": 1, "rate": 0.8},
                    "median_seconds": 12.5,
                    "tokens_per_solved": 1234,
                }
            },
        },
    )
    # development trials: the baseline solves 2 of 5 tasks, the best composition 4 of 5
    solved = {"comp-base": {"dev-a", "dev-b"}, "comp-best": {"dev-a", "dev-b", "dev-c", "dev-d"}}
    for composition, comp_ref in (("comp-base", base), ("comp-best", best)):
        for task in DEV_TASKS:
            trial = seed(
                store,
                "calibration-trial",
                f"ct-{composition}-{task}",
                {"composition_ref": comp_ref},
            )
            seed(
                store,
                "trial-metrics",
                f"tm-{composition}-{task}",
                {
                    "task_id": task,
                    "cell_id": CELL_A,
                    "split": "development",
                    "domain": "python",
                    "arm": "baseline" if composition == "comp-base" else "candidate",
                    "strategy": "single",
                    "source": "calibration",
                    "calibration_plan_ref": plan,
                    "trial_ref": trial,
                    "success": task in solved[composition],
                    "tokens": {"input": 600, "output": 400},
                    "wall_seconds": 10.0,
                    "api_cost": {
                        "status": "estimated",
                        "cost_microunits": 1_000_000,
                        "currency": "USD",
                    },
                },
            )
    # a validation task and a holdout task: their ids must never be shown
    for task, split in ((VALIDATION_ID, "validation"), (HOLDOUT_ID, "holdout")):
        seed(
            store,
            "trial-metrics",
            f"tm-{split}",
            {
                "task_id": task,
                "cell_id": CELL_A,
                "split": split,
                "domain": "python",
                "arm": "candidate",
                "strategy": "single",
                "source": "stage",
                "experiment_ref": ref("exp-1"),
                "success": True,
                "tokens": {"input": 1, "output": 1},
                "wall_seconds": 1.0,
            },
        )
    seed(
        store,
        "corpus-task-index",
        "index-1",
        {
            "tasks": [
                {"case_id": "dev-a", "split": "development"},
                {
                    "case_id": VALIDATION_ID,
                    "split": "validation",
                    "prompt": PROMPT_CANARY,
                    "hidden_tests": HIDDEN_CANARY,
                },
                {"case_id": HOLDOUT_ID, "split": "holdout", "prompt": PROMPT_CANARY},
            ]
        },
    )
    # records the build must never read or show
    seed(store, "harness-trace", "trace-1", {"turns": [{"text": TRACE_CANARY}]})
    seed(store, "leak-index", "leak-1", {"tokens": [LEAK_CANARY]})
    seed(store, "judge-label-set", "labels-1", {"items": [{"text": TRACE_CANARY + "-label"}]})
    seed(
        store,
        "harness-change-proposal",
        "prop-1",
        {
            "proposal_id": "prop-1",
            "surface_class": "B",
            "baseline_ref": base,
            "candidate_ref": best,
            "hypothesis": f"fixes {HOLDOUT_ID} and {VALIDATION_ID} {hostile}",
        },
    )
    seed_head(store, "evolution", "prop-1", "draft", {})
    seed_head(
        store,
        "meta-budget",
        "prop-1",
        "open",
        {"allocations": {"a1": {"status": "unknown"}, "a2": {"status": "settled"}}},
    )
    seed_head(
        store,
        "nightly-run",
        "2026-10-01",
        "finished",
        {"queued_confirmations": [ref("conf-1")], "finished_at": "2026-10-01T05:00:00Z"},
    )
    seed(
        store,
        "meta-approval",
        "approval-1",
        {
            "approval_id": "appr-1",
            "action": "nightly.explore",
            "approved_by": {"kind": "human", "subject_id": "operator" + hostile},
            "approved_at": "2026-09-30T12:00:00Z",
            "valid_from": "2026-09-30T00:00:00Z",
            "valid_until": "2026-10-07T00:00:00Z",
            "nights": 7,
        },
    )


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "runtime"
    with Store(root) as store:
        seed_world(store)
    return root


def test_a_seeded_store_builds_the_matrix_from_calibration_and_trial_metrics(world, tmp_path):
    feed = dash.build_feed(world, SCOPE, now_utc=NOW)
    by_cell = {m.cell_id: m for m in feed.matrix}
    a = by_cell[CELL_A]
    assert (a.driver_id, a.model, a.effort) == ("codex-cli", "gpt-x", "high")
    assert a.pass_rate == 0.8 and a.interval == (0.5, 0.95)
    assert (a.pass_k, a.pass_k_rate) == (1, 0.8) and a.informative == 5
    assert a.median_seconds == 12.5 and a.tokens_per_solved == 1234
    # API-equivalent cost: ten priced rows of 1 USD over the six solved ones, descriptive
    assert a.api_cost.status == "estimated" and a.api_cost.currency == "USD"
    assert round(a.api_cost.microunits or 0.0) == round(10_000_000 / 6)
    # best composition and its paired delta over the baseline (4/5 - 2/5)
    assert a.best is not None and a.best.composition_id == "comp-best"
    assert a.best.tasks == 5 and a.best.baseline_id == "comp-base"
    assert a.best.delta == pytest.approx(0.4) and a.best.delta_tasks == 5
    assert by_cell[CELL_B].pass_rate is None and by_cell[CELL_B].best is None
    assert feed.efforts == ("low", "high")
    pages = dash.render_pages(feed)
    assert_all_pass(pages, tmp_path)
    assert "comp-best (80.0%, +40.0 pts)" in pages["index.html"]
    assert "80.0% [50.0%, 95.0%]" in pages["index.html"]
    cell_page = pages[a.page]
    assert "comp-base" in cell_page and "comp-best" in cell_page and "<circle" in cell_page


def test_the_seeded_approvals_page_has_class_b_review_reconcile_pending_and_standing(
    world, tmp_path
):
    feed = dash.build_feed(world, SCOPE, now_utc=NOW)
    kinds = {a.kind for a in feed.approvals}
    assert {"evolution", "reconcile pending", "queued confirmation"} <= kinds
    review = next(a for a in feed.approvals if a.kind == "evolution")
    assert review.surface_class == "B" and "class B code review" in review.detail
    (standing,) = feed.standing
    assert standing.status == "active" and standing.approval_id == "appr-1"
    assert (standing.valid_from, standing.valid_until) == (
        "2026-09-30T00:00:00Z",
        "2026-10-07T00:00:00Z",
    )
    text = dash.render_pages(feed)["approvals.html"]
    assert "reconcile pending" in text and "2026-10-07T00:00:00Z" in text
    # the same standing approval is expired later and not yet valid earlier (dates, not a flag)
    later = dash.build_feed(world, SCOPE, now_utc="2026-10-08T00:00:00Z")
    earlier = dash.build_feed(world, SCOPE, now_utc="2026-09-29T00:00:00Z")
    assert later.standing[0].status == "expired" and earlier.standing[0].status == "not_yet_valid"


def test_no_trace_text_prompt_hidden_test_leak_token_or_withheld_task_id_reaches_a_page(
    world, tmp_path
):
    feed = dash.build_feed(world, SCOPE, now_utc=NOW)
    site = tmp_path / "site"
    dash.write_site(feed, site, feed_json=True)
    blob = "\n".join(p.read_text(encoding="utf-8") for p in site.iterdir())
    for secret in (TRACE_CANARY, LEAK_CANARY, PROMPT_CANARY, HIDDEN_CANARY):
        assert secret not in blob, secret
    # a validation or holdout task is never named by its id, in a field or in a free text
    assert VALIDATION_ID not in blob and HOLDOUT_ID not in blob
    lineage = dash.render_pages(feed)["lineage.html"]
    assert "[withheld]" in lineage
    # the build did not read those kinds at all
    assert not {"harness-trace", "leak-index", "judge-label-set"} & set(feed.sources)


def test_a_hostile_string_in_a_stored_record_is_escaped_in_the_built_pages(tmp_path):
    root = tmp_path / "runtime"
    with Store(root) as store:
        seed_world(store, hostile=HOSTILE)
    feed = dash.build_feed(root, SCOPE, now_utc=NOW)
    pages = dash.render_pages(feed)
    assert_all_pass(pages, tmp_path)
    assert_static(pages)
    everything = text_of(pages)
    assert HOSTILE_SCRIPT not in everything and "<img" not in everything
    assert ESCAPED_SCRIPT in everything
    # the cell id slug used in a file name carries no markup
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+\.html", name) for name in pages)


def test_a_record_of_an_unexpected_shape_is_skipped_not_fatal(world, tmp_path):
    with Store(world) as store:
        seed(store, "trial-metrics", "tm-bad-1", {"task_id": 5, "cell_id": None})
        seed(
            store,
            "trial-metrics",
            "tm-bad-2",
            {"task_id": "dev-a", "cell_id": CELL_A, "tokens": "many", "success": "yes"},
        )
        seed(
            store, "calibration-summary", "summary-bad", {"cells": "not-a-dict", "plan_ref": "nope"}
        )
        seed(store, "harness-cell", "cell-bad", {"driver_id": 7, "effort": ["x"]})
        seed_head(
            store, "experiment", "exp-bad", "concluded", {"experiment_ref": "x", "report_ref": 3}
        )
    feed = dash.build_feed(world, SCOPE, now_utc=NOW)
    pages = dash.render_pages(feed)
    assert_all_pass(pages, tmp_path)
    assert "exp-bad" in pages["experiments.html"]


# --- the build is read-only ------------------------------------------------------------------


def snapshot(root: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{root / 'runtime.sqlite3'}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            t: conn.execute(
                f'SELECT count(*), coalesce(sum(length(CAST(rowid AS TEXT))),0) FROM "{t}"'
            ).fetchall()
            for t in sorted(tables)
        } | {
            "heads": conn.execute(
                "SELECT id,state,row_version,data FROM heads ORDER BY 1,2"
            ).fetchall(),
            "objects": conn.execute(
                "SELECT kind,id,revision,digest FROM objects ORDER BY 1,2,3"
            ).fetchall(),
            "events": conn.execute("SELECT count(*) FROM events").fetchall(),
        }
    finally:
        conn.close()


def test_building_the_feed_and_the_pages_writes_nothing_to_the_store(world, tmp_path):
    before = snapshot(world)
    files_before = sorted(p.name for p in world.iterdir())
    feed = dash.build_feed(world, SCOPE, now_utc=NOW)
    dash.write_site(feed, tmp_path / "site", feed_json=True)
    assert snapshot(world) == before
    assert sorted(p.name for p in world.iterdir()) == files_before  # no new file in the root


def test_build_feed_on_an_open_readonly_store_does_not_write_or_take_the_owner_lock(
    world, tmp_path
):
    before = snapshot(world)
    with Store(world) as owner:  # a running deployment holds the owner lock
        with Store(world, readonly=True) as reader:
            assert reader.readonly
            feed = dash.build_feed(reader, SCOPE, now_utc=NOW)
        assert owner.epoch >= 1
    assert feed.matrix
    # only the owner open above moved the epoch; the dashboard build added no record
    after = snapshot(world)
    assert after["objects"] == before["objects"] and after["heads"] == before["heads"]


def test_the_page_writer_touches_only_the_page_files_in_the_output_directory(world, tmp_path):
    out = tmp_path / "nested" / "site"
    feed = dash.build_feed(world, SCOPE, now_utc=NOW)
    written = dash.write_site(feed, out)
    assert {p.name for p in written} == {p.name for p in out.iterdir()}
    assert all(p.suffix == ".html" for p in out.iterdir())
    assert not (out / "feed.json").exists()
    dash.write_site(feed, out, feed_json=True)
    assert (out / "feed.json").is_file()


# --- the command: amplai meta dashboard ------------------------------------------------------


def local_config(tmp_path: Path, root: Path, **overrides: Any) -> Path:
    value: dict[str, Any] = {
        "schema_version": "local-1",
        "runtime_root": str(root),
        "workspace_root": str(tmp_path / "workspace"),
        "scope": SCOPE.wire(),
        "operator_subject": "operator",
        "operator_token_file": str(tmp_path / "token"),
        "signing_key_file": str(tmp_path / "sign"),
        "verifier_key_file": str(tmp_path / "verify"),
        "codex": {
            "credential_home": str(tmp_path / "codex"),
            "egress_profile": "egress.json",
            "egress_qualification": "egress-qual.json",
        },
        "apps": [
            {
                "app_id": "demo",
                "repo": str(tmp_path / "repo"),
                "container_profile": "profile.json",
                "qualification_report": "qual.json",
                "verifiers": [{"id": "v", "argv": ["true"], "description": "d"}],
            }
        ],
        **overrides,
    }
    path = tmp_path / "local.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_the_command_writes_the_pages_and_reports_what_it_read(world, tmp_path):
    config = local_config(tmp_path, world)
    before = snapshot(world)
    out = tmp_path / "pages"
    result = command.build(config, out)
    assert set(FIXED_PAGES) <= set(result["pages"])
    assert result["cells"] == sorted(result["cells"]) or set(result["cells"]) == {CELL_A, CELL_B}
    assert set(result["cells"]) == {CELL_A, CELL_B}
    assert result["out"] == str(out.absolute())
    assert result["records_read"]["calibration-summary"] == 1
    assert {p.name for p in out.iterdir()} == set(result["pages"])
    assert "feed.json" not in result["pages"]
    assert_all_pass({p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}, tmp_path)
    assert snapshot(world) == before  # read-only: no boot, no release pointer move, no record
    withfeed = command.build(config, tmp_path / "pages2", feed_json=True)
    assert "feed.json" in withfeed["pages"]


def test_the_command_reads_the_nightly_configuration_next_to_the_stored_records(world, tmp_path):
    config = local_config(tmp_path, world, meta={"nightly": {"budget_trials": 33}})
    out = tmp_path / "pages"
    command.build(config, out)
    text = (out / "budget.html").read_text(encoding="utf-8")
    assert "33 configured" in text


def test_the_cli_command_is_registered_and_prints_json(world, tmp_path):
    config = local_config(tmp_path, world)
    help_text = CliRunner().invoke(app, ["meta", "dashboard", "--help"])
    assert help_text.exit_code == 0, help_text.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", help_text.output)
    assert "--out" in plain and "--feed-json" in plain
    out = tmp_path / "pages"
    result = CliRunner().invoke(
        app, ["meta", "dashboard", "--out", str(out), "--config", str(config), "--feed-json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "index.html" in payload["pages"] and "feed.json" in payload["pages"]
    assert (out / "index.html").is_file()


def test_the_out_option_is_required():
    result = CliRunner().invoke(app, ["meta", "dashboard"])
    assert result.exit_code != 0


# --- negative cases: Hold and RuntimeFault codes ---------------------------------------------


def test_an_unknown_local_config_version_is_a_hold(world, tmp_path):
    config = local_config(tmp_path, world, schema_version="local-9")
    with pytest.raises(Hold) as caught:
        command.build(config, tmp_path / "pages")
    assert caught.value.code == "CONFIG_VERSION"
    assert not (tmp_path / "pages").exists()  # nothing was written
    result = CliRunner().invoke(
        app, ["meta", "dashboard", "--out", str(tmp_path / "pages"), "--config", str(config)]
    )
    assert result.exit_code != 0 and "CONFIG_VERSION" in result.output


def test_a_missing_or_unreadable_config_is_a_local_input_fault(tmp_path):
    for content in (None, "{not json", json.dumps({"schema_version": "local-1"})):
        path = tmp_path / "local.json"
        path.unlink(missing_ok=True)
        if content is not None:
            path.write_text(content, encoding="utf-8")
        with pytest.raises(RuntimeFault) as caught:
            command.build(path, tmp_path / "pages")
        assert not isinstance(caught.value, Hold)
        assert caught.value.code == "LOCAL_INPUT"
    assert not (tmp_path / "pages").exists()


def test_a_config_with_an_unknown_key_is_a_local_input_fault(world, tmp_path):
    config = local_config(tmp_path, world, surprise=True)
    with pytest.raises(RuntimeFault) as caught:
        command.build(config, tmp_path / "pages")
    assert caught.value.code == "LOCAL_INPUT"


def test_a_missing_runtime_store_is_not_found_and_creates_nothing(tmp_path):
    root = tmp_path / "no-runtime"
    config = local_config(tmp_path, root)
    with pytest.raises(RuntimeFault) as caught:
        command.build(config, tmp_path / "pages")
    assert not isinstance(caught.value, Hold) and caught.value.code == "NOT_FOUND"
    assert not root.exists() and not (tmp_path / "pages").exists()
    result = CliRunner().invoke(
        app, ["meta", "dashboard", "--out", str(tmp_path / "pages"), "--config", str(config)]
    )
    assert result.exit_code != 0 and "NOT_FOUND" in result.output


def test_a_relative_runtime_root_resolves_against_the_config_directory(world, tmp_path):
    config = local_config(tmp_path, Path("runtime"))  # tmp_path/runtime is the seeded store
    result = command.build(config, tmp_path / "pages")
    assert "index.html" in result["pages"]


def test_a_store_of_an_incompatible_schema_is_a_hold_and_stays_untouched(tmp_path):
    root = tmp_path / "runtime"
    with Store(root):
        pass
    conn = sqlite3.connect(root / "runtime.sqlite3")
    conn.execute("UPDATE meta SET value='4' WHERE key='schema_major'")
    conn.commit()
    conn.close()
    before = snapshot(root)
    with pytest.raises(Hold) as caught:
        dash.build_feed(root, SCOPE, now_utc=NOW)
    assert caught.value.code == "SCHEMA_VERSION"
    assert snapshot(root) == before


def test_a_scope_with_no_records_is_not_another_scope_s_data(world, tmp_path):
    other = Scope("tenant-other", "project-other")
    feed = dash.build_feed(world, other, now_utc=NOW)
    assert feed.matrix == () and feed.scope == other.wire()
    pages = dash.render_pages(feed)
    assert "gpt-x" not in text_of(pages) and CELL_A not in text_of(pages)
    assert_all_pass(pages, tmp_path)


def test_the_dashboard_module_declares_no_store_write_and_no_script():
    source = Path(dash.__file__).read_text(encoding="utf-8")
    for forbidden in ("store.put(", "store.cas(", "store.event(", "store.tx(", ".admit("):
        assert forbidden not in source, forbidden
    for forbidden in ("harness-trace", "leak-index", "judge-label-set"):
        # named only in the module docstring, never read
        assert source.count(f'"{forbidden}"') == 0, forbidden


# --- clarification after S12, S15: the matrix key, the night's approvals, §14 Q11 -------------


def test_two_cells_with_one_driver_model_effort_triple_both_appear_keyed_by_cell_id(tmp_path):
    root = tmp_path / "runtime"
    with Store(root) as store:
        for cell in ("cell-one", "cell-two"):
            value = {"driver_id": "codex-cli", "provider_model_id": "m1", "effort": "high"}
            seed(store, "harness-cell", cell, value)
    feed = dash.build_feed(root, SCOPE, now_utc=NOW)
    assert sorted(m.cell_id for m in feed.matrix) == ["cell-one", "cell-two"]
    text = dash.render_pages(feed)["index.html"]
    # one row each: neither cell hides the other in the (driver, model, effort) slot
    assert "codex-cli / m1 (cell-one)" in text and "codex-cli / m1 (cell-two)" in text
    for cell in feed.cells:
        assert f'href="{cell.page}"' in text
    assert_all_pass(dash.render_pages(feed), tmp_path)


def test_the_approvals_page_lists_the_nights_reconcile_pending_queued_and_failed_items(
    world, tmp_path
):
    with Store(world) as store:
        seed_head(store, "nightly-run", "night-2026-10-01", "stopped", {
            "date": "2026-10-01", "reconcile_pending": ["prop-unknown"],
            "queued_confirmations": [], "finished_at": NOW,
        })  # fmt: skip
        seed_head(store, "stage-queue", "stagequeue-prop-q-focused", "queued", {
            "proposal_id": "prop-q", "stage": "focused", "subject_digest": "sha256:" + "a" * 64,
            "queued_at": NOW,
        })  # fmt: skip
        seed_head(store, "stage-queue", "stagequeue-prop-a-focused", "approved", {
            "proposal_id": "prop-a", "stage": "focused", "subject_digest": "sha256:" + "b" * 64,
            "queued_at": NOW, "approved_at": NOW,
        })  # fmt: skip
        seed_head(store, "stage-queue", "stagequeue-prop-r-focused", "ran", {
            "proposal_id": "prop-r", "stage": "focused", "subject_digest": "sha256:" + "c" * 64,
        })  # fmt: skip
        seed_head(store, "evolution", "prop-f", "screened", {})
        seed_head(store, "stage-run", "stagerun-prop-f", "screening:failed", {
            "stages": {"screening": {
                "state": "failed",
                "guard_findings": ["SCREENING_FAILED: decision class regression"],
            }},
        })  # fmt: skip
        seed_head(store, "evolution", "prop-gone", "rejected", {})
        seed_head(store, "stage-run", "stagerun-prop-gone", "screening:failed", {
            "stages": {"screening": {"state": "failed", "guard_findings": []}},
        })  # fmt: skip
    feed = dash.build_feed(world, SCOPE, now_utc=NOW)
    rows = {(a.kind, a.subject): a for a in feed.approvals}
    pending = rows[("reconcile pending", "prop-unknown")].detail
    assert "unknown effect on night-2026-10-01" in pending
    queued = rows[("queued confirmation", "prop-q")].detail
    assert "--stage focused --queue" in queued and "sha256:" + "a" * 64 in queued
    assert "runs on the next night" in rows[("queued confirmation", "prop-a")].detail
    assert ("queued confirmation", "prop-r") not in rows  # already ran
    failed = rows[("screening failed", "prop-f")].detail
    assert "decision class regression" in failed and "amplai meta reject" in failed
    assert ("screening failed", "prop-gone") not in rows  # the operator rejected it already
    pages = dash.render_pages(feed)
    assert "approve-stage prop-q --stage focused --queue" in pages["approvals.html"]
    assert_all_pass(pages, tmp_path)


WRITER = """
import sys, time
from pathlib import Path
from amplai_foundry.runtime.storage.store import Scope, Store

root, ready, done = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
scope = Scope("tenant-s15", "project-s15")
with Store(root) as store:
    mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    i = 0
    while not done.exists():
        with store.tx() as db:
            store.put(db, scope, "harness-cell", f"writer-cell-{i}", 1, {
                "scope": scope.wire(), "driver_id": "codex-cli", "provider_model_id": "w",
                "effort": "low"})
        i += 1
        if i == 1:
            ready.write_text(mode)
        time.sleep(0.005)
print(i)
"""


def test_q11_a_readonly_open_builds_the_feed_while_a_writer_process_owns_the_store_in_wal(
    world, tmp_path
):
    """§14 Q11: another process owns the store (owner lock, WAL journal) and keeps writing while
    the dashboard opens it read-only and builds the feed."""
    import subprocess
    import sys
    import time as _time

    ready, done = tmp_path / "ready", tmp_path / "done"
    writer = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(world), str(ready), str(done)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    counts: list[int] = []
    try:
        deadline = _time.monotonic() + 30
        while not ready.exists() and writer.poll() is None and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert ready.exists(), "the writer did not start"
        assert ready.read_text() == "wal"
        with pytest.raises(Hold) as owned:  # the writer holds the owner lock
            Store(world)
        assert owned.value.code == "ACTIVE_OWNER"
        for _ in range(3):
            feed = dash.build_feed(world, SCOPE, now_utc=NOW)
            assert {CELL_A, CELL_B} <= {m.cell_id for m in feed.matrix}
            counts.append(sum(1 for m in feed.matrix if m.cell_id.startswith("writer-cell-")))
            _time.sleep(0.05)
        assert counts == sorted(counts) and counts[-1] >= 1  # the reader sees committed writes
        assert_all_pass(dash.render_pages(feed), tmp_path)
    finally:
        done.write_text("1")
        out, err = writer.communicate(timeout=30)
    assert writer.returncode == 0, err
    assert int(out.strip()) >= counts[-1]
