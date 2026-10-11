"""Ask-task terms of the committed corpus 2.1.0 (PR #78 reviews).

Contract: specs/033-harness-taxonomy/interfaces.md §10.3 (ambiguity) and ``corpus_v2.grade``
(``planner_questions``: an ``ask`` task passes when some question names a ``must_mention_any``
term as a whole word or phrase). On-point questions about the open point must pass with the real
grader; off-point questions must not. No docker, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplai_foundry.meta_harness import corpus_v2

REPO = Path(__file__).resolve().parents[2]
V2_ROOT = REPO / "specs" / "033-harness-taxonomy" / "corpus"

MULTI_CATALOGUE_ON_POINT = (
    "If the same SKU appears in two files with different prices, which row should be used?",
    "How should duplicate SKUs across catalogue files be handled?",
    "Should a later file override an earlier one for the same SKU?",
    "Which catalogue takes priority when a SKU is repeated with a different price?",
)
MULTI_CATALOGUE_OFF_POINT = (
    "Should the first file be treated as the main list?",
    "Should --file override the default catalogue?",
)


def _task(task_id: str) -> corpus_v2.TaskV2:
    return corpus_v2.load(V2_ROOT).task(task_id)


@pytest.mark.parametrize("question", MULTI_CATALOGUE_ON_POINT)
def test_on_point_questions_about_conflicting_catalogue_rows_pass(
    question: str, tmp_path: Path
) -> None:
    task = _task("ambiguity-multi-catalogue")
    outcome = corpus_v2.grade(task, tmp_path, planner_questions=[question])
    assert outcome.success, outcome.detail


@pytest.mark.parametrize("question", MULTI_CATALOGUE_OFF_POINT)
def test_off_point_catalogue_questions_fail(question: str, tmp_path: Path) -> None:
    task = _task("ambiguity-multi-catalogue")
    outcome = corpus_v2.grade(task, tmp_path, planner_questions=[question])
    assert not outcome.success, outcome.detail


def test_a_question_about_orders_90_days_old_passes(tmp_path: Path) -> None:
    task = _task("ambiguity-dormant-customers")
    question = "Is a customer whose last order is 90 days old still active?"
    outcome = corpus_v2.grade(task, tmp_path, planner_questions=[question])
    assert outcome.success, outcome.detail
